import json
import gspread_asyncio
from google.oauth2.service_account import Credentials
from config import GOOGLE_CREDENTIALS_JSON, GLOBAL_DEFAULT_SHEET, logger
from utils import now2ddmmyy
import sqlite3

def get_credentials():
    credentials_info = json.loads(GOOGLE_CREDENTIALS_JSON)
    credentials_info["private_key"] = credentials_info["private_key"].replace("\\n", "\n")
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    return Credentials.from_service_account_info(credentials_info, scopes=scope)

agcm = gspread_asyncio.AsyncioGspreadClientManager(get_credentials)

# Cache of already-opened spreadsheets, keyed by sheet name/title.
# Without this, every button click re-resolves the spreadsheet by name
# via the Drive API, which is slow and eats into API quota.
_spreadsheet_cache = {}


async def open_spreadsheet(sheet_target):
    if sheet_target in _spreadsheet_cache:
        return _spreadsheet_cache[sheet_target]
    gc = await agcm.authorize()
    ss = await gc.open(sheet_target)
    _spreadsheet_cache[sheet_target] = ss
    return ss


async def get_sheet_for_chat(chat_id):
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT sheet_name FROM chat_settings WHERE chat_id = ?", (str(chat_id),))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else GLOBAL_DEFAULT_SHEET


async def log_action_to_google(chat_id, event_id, action_name, username, user_id):
    try:
        sheet_target = await get_sheet_for_chat(chat_id)
        ss = await open_spreadsheet(sheet_target)
        ws = await ss.worksheet("Actions")
        # Layout: EVENT_ID, ACTION, USER_NAME, USER_ID, DATE
        await ws.append_row([event_id, action_name, username, str(user_id), now2ddmmyy()])
    except Exception as e:
        logger.error(f"Google Sheets Actions log failed: {repr(e)}")


async def sync_users_sheet(chat_id, current_members: list):
    """
    Syncs the "Users" worksheet for a given chat/place with its current
    (best-known) membership.

    current_members: list of (user_id, username) tuples for people confirmed
    to currently be in `chat_id` right now.

    Columns: USER_ID, USER_NAME, PLACE_ID, STATUS, DATE_start, DATE_end, ARCHIVED_USER_NAME
    A row is uniquely identified by (USER_ID, PLACE_ID) - the same person can
    have separate rows for separate places/groups managed by this bot.

    Behavior:
      - Member not yet in the sheet for this place -> append a new row with
        STATUS = "MEMBER", DATE_start = current date, DATE_end = blank.
      - Member already in the sheet whose USER_NAME changed -> the old name
        is appended to ARCHIVED_USER_NAME (comma-separated), and USER_NAME is
        updated to the current one. If they were previously "LEFT", their
        STATUS flips back to "MEMBER" and DATE_end is cleared.
      - Any existing row for this PLACE_ID that ISN'T in current_members ->
        STATUS is set to "LEFT" and DATE_end is set to current date
        (their row/history is kept, not deleted).
    """
    try:
        sheet_target = await get_sheet_for_chat(chat_id)
        ss = await open_spreadsheet(sheet_target)
        ws = await ss.worksheet("Users")
        records = await ws.get_all_records()

        index = {}
        for idx, r in enumerate(records, start=2):
            key = (str(r.get("USER_ID", "")).strip(), str(r.get("PLACE_ID", "")).strip())
            index[key] = (idx, r)

        current_keys = set()
        for user_id, username in current_members:
            if not user_id or not username:
                continue
            key = (str(user_id), str(chat_id))
            current_keys.add(key)

            if key in index:
                idx, rec = index[key]
                old_name = str(rec.get("USER_NAME", "")).strip()
                status = str(rec.get("STATUS", "")).strip().lower()
                
                if old_name and old_name != username:
                    archived     = str(rec.get("ARCHIVED_USER_NAME", "")).strip()
                    new_archived = f"{archived},{old_name}" if archived else old_name
                    await ws.update(f"B{idx}", [[username]])
                    await ws.update(f"G{idx}", [[new_archived]])
                
                if status == "left":
                    # LEFT -> MEMBER: update status, clear DATE_end, update DATE_start
                    await ws.update(f"D{idx}", [["MEMBER"]])
                    await ws.update(f"E{idx}", [[now2ddmmyy()]])
                    await ws.update(f"F{idx}", [[""]])
                # If status is already MEMBER, do nothing
            else:
                # New user: add with MEMBER status
                await ws.append_row([str(user_id), username, str(chat_id), "MEMBER", now2ddmmyy(), "", ""])

        # Handle users who left the group
        for key, (idx, rec) in index.items():
            uid, place = key
            if place == str(chat_id) and key not in current_keys:
                status = str(rec.get("STATUS", "")).strip().lower()
                if status == "member":
                    # MEMBER -> LEFT: update status, set DATE_end, add to UserPresenceLog
                    date_start = str(rec.get("DATE_start", "")).strip()
                    await ws.update(f"D{idx}", [["LEFT"]])
                    await ws.update(f"F{idx}", [[now2ddmmyy()]])
                    # Add to UserPresenceLog only if not already logged
                    await log_user_presence_if_not_exists(chat_id, uid, place, date_start, now2ddmmyy())
                # If status is already LEFT, do nothing
    except Exception as e:
        logger.error(f"Google Sheets Users synchronization failed: {repr(e)}")
        raise


async def mark_user_left(chat_id, user_id):
    """
    Marks a single user as LEFT in the "Users" worksheet for this place_id
    (chat_id), the instant ChatMemberHandler confirms they left/were kicked -
    without needing the full current-membership snapshot that
    sync_users_sheet()'s diff-based approach requires.

    Mirrors the "MEMBER -> LEFT" branch of sync_users_sheet(): sets STATUS to
    LEFT and DATE_end to today for the (USER_ID, PLACE_ID) row, then logs the
    presence interval to UserPresenceLog (idempotent - log_user_presence_if_not_exists
    skips it if already logged for that USER_ID/PLACE_ID/DATE_start).

    No-op if there's no row for this (user_id, place_id) yet, or if it's
    already LEFT.
    """
    try:
        sheet_target = await get_sheet_for_chat(chat_id)
        ss = await open_spreadsheet(sheet_target)
        ws = await ss.worksheet("Users")
        records = await ws.get_all_records()

        for idx, r in enumerate(records, start=2):
            if (str(r.get("USER_ID", "")).strip() == str(user_id)
                    and str(r.get("PLACE_ID", "")).strip() == str(chat_id)):
                status = str(r.get("STATUS", "")).strip().lower()
                if status == "member":
                    date_start = str(r.get("DATE_start", "")).strip()
                    date_end = now2ddmmyy()
                    await ws.update(f"D{idx}", [["LEFT"]])
                    await ws.update(f"F{idx}", [[date_end]])
                    await log_user_presence_if_not_exists(chat_id, user_id, chat_id, date_start, date_end)
                # Already LEFT (or any other status) -> nothing to do
                return
        # No row for this (user_id, place_id) yet - nothing to mark
    except Exception as e:
        logger.error(f"Google Sheets Users mark-left failed for user {user_id} in chat {chat_id}: {repr(e)}")


async def sync_event_users_sheet(chat_id, event_id, user_ids):
    """
    Writes all going user_ids (master + child chats) to the EventUsers sheet.
    Expects user_ids as a flat list of string IDs.
    Columns: EVENT_ID, USER_ID
    """
    try:
        sheet_target = await get_sheet_for_chat(chat_id)
        ss = await open_spreadsheet(sheet_target)
        ws = await ss.worksheet("EventUsers")
        rows_to_append = [[event_id, str(uid)] for uid in user_ids if uid]
        if rows_to_append:
            await ws.append_rows(rows_to_append)
        else:
            logger.info("Roster was empty at commitment index. Skipping EventUsers rows insert.")
    except Exception as e:
        logger.error(f"Google Sheets EventUsers synchronization failed: {repr(e)}")


async def log_user_presence(chat_id, user_id, date_start, date_end):
    """
    Logs user presence to UserPresenceLog sheet when a user leaves a monitored
    group or the main group.
    Columns: USER_ID, PLACE_ID, DATE_start, DATE_end
    """
    try:
        sheet_target = await get_sheet_for_chat(chat_id)
        ss = await open_spreadsheet(sheet_target)
        ws = await ss.worksheet("UserPresenceLog")
        await ws.append_row([str(user_id), str(chat_id), date_start, date_end])
    except Exception as e:
        logger.error(f"Google Sheets UserPresenceLog synchronization failed: {repr(e)}")

async def log_user_presence_if_not_exists(chat_id, user_id, place_id, date_start, date_end):
    """
    Logs user presence to UserPresenceLog sheet when a user leaves a monitored
    group or the main group.
    Columns: USER_ID, PLACE_ID, DATE_start, DATE_end
    """
    try:
        sheet_target = await get_sheet_for_chat(chat_id)
        ss = await open_spreadsheet(sheet_target)
        ws = await ss.worksheet("UserPresenceLog")
        records = await ws.get_all_records()

        # if it exists in UserPresenceLog with same USER_ID, PLACE_ID and same DATE_start — we do NOT write a duplicate.
        for r in records:
            if (str(r.get("USER_ID", "")).strip() == str(user_id) and 
                str(r.get("PLACE_ID", "")).strip() == str(place_id) and 
                str(r.get("DATE_start", "")).strip() == str(date_start)):
                return  # already logged

        await ws.append_row([str(user_id), str(place_id), str(date_start), str(date_end)])
    except Exception as e:
        logger.error(f"Google Sheets UserPresenceLog check failed: {repr(e)}")
