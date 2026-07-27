import json
import gspread_asyncio
from datetime import datetime
from google.oauth2.service_account import Credentials
from config import GOOGLE_CREDENTIALS_JSON, CONTROL_SHEET_ID, logger
from utils import now2ddmmyy
import sqlite3

def get_credentials():
    credentials_info = json.loads(GOOGLE_CREDENTIALS_JSON)
    credentials_info["private_key"] = credentials_info["private_key"].replace("\\n", "\n")
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    return Credentials.from_service_account_info(credentials_info, scopes=scope)

agcm = gspread_asyncio.AsyncioGspreadClientManager(get_credentials)

# Cache of already-opened spreadsheets, keyed by spreadsheet ID.
# Without this, every button click re-resolves the spreadsheet via the
# Drive API, which is slow and eats into API quota.
_spreadsheet_cache = {}

# Matches subscription.SUBS_DATE_FORMAT - duplicated here (rather than
# imported) to avoid a sheets<->subscription circular import, since
# subscription.py already imports sync_control_sheet_main/subconfig FROM
# this module.
_SUBS_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


async def open_spreadsheet(sheet_id):
    """
    Opens a spreadsheet by its ID (gc.open_by_key), not by title. A title
    isn't guaranteed unique - two different customers could both leave their
    copy of the shared template named "Events" and there is no reliable way
    to know which one gc.open(name) would return. The spreadsheet ID (the
    long string in the sheet's URL) IS globally unique, so this can never
    resolve to the wrong customer's data.

    Returns None (no network call at all) if sheet_id is falsy - the normal,
    expected case for a free-tier hub or a premium hub that hasn't
    configured a sheet yet. Callers must check for a None return.
    """
    if not sheet_id:
        return None
    if sheet_id in _spreadsheet_cache:
        return _spreadsheet_cache[sheet_id]
    gc = await agcm.authorize()
    ss = await gc.open_by_key(sheet_id)
    _spreadsheet_cache[sheet_id] = ss
    return ss


async def get_sheet_for_chat(chat_id):
    """
    Resolves which spreadsheet ID this chat's hub should write to - or None
    if it shouldn't write to Sheets at all right now:
      - free tier              -> None (no Sheets writes for free hubs, period)
      - premium, no sheet_id   -> None (nothing configured yet to write to)
      - premium, has sheet_id  -> that sheet_id
    """
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute(
        "SELECT type, sheet_id, subs_date_end FROM main_chat_settings WHERE chat_id = ?",
        (str(chat_id),),
    )
    row = cursor.fetchone()
    conn.close()

    if not row:
        return None  # unregistered hub defaults to free - no Sheets writes

    chat_type, sheet_id, subs_date_end = row
    if chat_type != "premium":
        return None

    if not subs_date_end:
        return None
    try:
        if datetime.strptime(subs_date_end, _SUBS_DATE_FORMAT) <= datetime.now():
            return None  # premium subscription expired - treat as free
    except ValueError:
        return None

    return sheet_id or None


async def log_action_to_google(chat_id, event_id, action_name, username, user_id):
    sheet_target = await get_sheet_for_chat(chat_id)
    ss = await open_spreadsheet(sheet_target)
    if not ss:
        return  # free tier / no sheet configured / subscription expired - nothing to write
    try:
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
    sheet_target = await get_sheet_for_chat(chat_id)
    ss = await open_spreadsheet(sheet_target)
    if not ss:
        return  # free tier / no sheet configured / subscription expired - nothing to write
    try:
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


async def sync_event_users_sheet(chat_id, event_id, user_ids):
    """
    Writes all going user_ids (master + child chats) to the EventUsers sheet.
    Expects user_ids as a flat list of string IDs.
    Columns: EVENT_ID, USER_ID
    """
    sheet_target = await get_sheet_for_chat(chat_id)
    ss = await open_spreadsheet(sheet_target)
    if not ss:
        return  # free tier / no sheet configured / subscription expired - nothing to write
    try:
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
    sheet_target = await get_sheet_for_chat(chat_id)
    ss = await open_spreadsheet(sheet_target)
    if not ss:
        return  # free tier / no sheet configured / subscription expired - nothing to write
    try:
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
    sheet_target = await get_sheet_for_chat(chat_id)
    ss = await open_spreadsheet(sheet_target)
    if not ss:
        return  # free tier / no sheet configured / subscription expired - nothing to write
    try:
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


async def sync_control_sheet_main(rows: list) -> bool:
    """
    Overwrites the "Main" tab of the Control Sheet (CONTROL_SHEET_ID) with
    the current contents of main_chat_settings, so the bot owner can see
    every group using the bot and its subscription status in one place,
    without needing a Telegram command or a separate web dashboard.

    This is a ONE-WAY push (SQLite -> Sheet). Editing a cell in the Sheet by
    hand does NOT change anything back in SQLite - /setsub remains the only
    way to actually change a subscription. This tab is a read-only mirror
    for visibility, not a control surface (yet).

    rows: list of (chat_id, chat_name, type, sheet_id, subs_date_start, subs_date_end) tuples.
    Returns True on success, False on failure (logged either way) - so
    callers can tell the user honestly instead of always claiming success.
    """
    if not CONTROL_SHEET_ID:
        logger.error("sync_control_sheet_main: CONTROL_SHEET_ID is not configured.")
        return False
    try:
        ss = await open_spreadsheet(CONTROL_SHEET_ID)
        ws = await ss.worksheet("Main")
        await ws.clear()
        header = ["CHAT_ID", "CHAT_NAME", "TYPE", "SHEET_ID", "SUBS_DATE_START", "SUBS_DATE_END"]
        body   = [[str(v) if v is not None else "" for v in row] for row in rows]
        await ws.update("A1", [header] + body)
        return True
    except Exception as e:
        logger.error(f"Google Sheets Control/Main sync failed: {repr(e)}")
        return False


async def sync_control_sheet_subconfig(feature_rows: list):
    """
    Overwrites the "sub_config" tab of the Control Sheet with the free vs
    premium feature matrix. feature_rows: list of
    (feature, free_status, premium_status) tuples - see
    handlers.FEATURE_MATRIX, which is the actual source of truth this sheet
    documents (kept as plain reference data here, not read back by the bot).
    Returns True on success, False on failure (logged either way).
    """
    if not CONTROL_SHEET_ID:
        logger.error("sync_control_sheet_subconfig: CONTROL_SHEET_ID is not configured.")
        return False
    try:
        ss = await open_spreadsheet(CONTROL_SHEET_ID)
        ws = await ss.worksheet("sub_config")
        await ws.clear()
        header = ["FEATURE", "FREE", "PREMIUM"]
        body   = [[str(v) for v in row] for row in feature_rows]
        await ws.update("A1", [header] + body)
        return True
    except Exception as e:
        logger.error(f"Google Sheets Control/sub_config sync failed: {repr(e)}")
        return False
