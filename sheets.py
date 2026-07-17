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

    Columns: USER_ID, USER_NAME, PLACE_ID, STATUS, ARCHIVED USER_NAME
    A row is uniquely identified by (USER_ID, PLACE_ID) - the same person can
    have separate rows for separate places/groups managed by this bot.

    Behavior:
      - Member not yet in the sheet for this place -> append a new row with
        STATUS = "Member" (ARCHIVED USER_NAME left blank).
      - Member already in the sheet whose USER_NAME changed -> the old name
        is appended to ARCHIVED USER_NAME (comma-separated), and USER_NAME is
        updated to the current one. If they were previously "Left", their
        STATUS flips back to "Member".
      - Any existing row for this PLACE_ID that ISN'T in current_members ->
        STATUS is set to "Left" (their row/history is kept, not deleted).
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
                if old_name and old_name != username:
                    archived     = str(rec.get("ARCHIVED USER_NAME", "")).strip()
                    new_archived = f"{archived},{old_name}" if archived else old_name
                    await ws.update(f"B{idx}", [[username]])
                    await ws.update(f"E{idx}", [[new_archived]])
                if str(rec.get("STATUS", "")).strip().lower() == "left":
                    await ws.update(f"D{idx}", [["Member"]])
            else:
                await ws.append_row([str(user_id), username, str(chat_id), "Member", ""])

        for key, (idx, rec) in index.items():
            uid, place = key
            if place == str(chat_id) and key not in current_keys:
                if str(rec.get("STATUS", "")).strip().lower() != "left":
                    await ws.update(f"D{idx}", [["Left"]])
    except Exception as e:
        logger.error(f"Google Sheets Users synchronization failed: {repr(e)}")
        raise


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
