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
        gc = await agcm.authorize()
        ss = await gc.open(sheet_target)
        ws = await ss.worksheet("Actions")
        # Updated layout constraint: EVENT_ID, ACTION, USER_NAME, USER_ID, DATE
        await ws.append_row([event_id, action_name, username, str(user_id), now2ddmmyy()])
    except Exception as e:
        logger.error(f"Google Sheets Actions log failed: {repr(e)}")

async def sync_event_users_to_google(chat_id, event_id, going_list):
    try:
        sheet_target = await get_sheet_for_chat(chat_id)
        gc = await agcm.authorize()
        ss = await gc.open(sheet_target)
        ws = await ss.worksheet("EventUsers")
        
        # We need to map usernames/first_names back to user_ids if possible,
        # but since we only have user_id on active button interactions,
        # we append rows dynamically as requested: Event_ID, user_id
        # For simplicity and exact match to rule #4, we push the dataset
        # Inside sheets.py -> sync_event_users_to_google function
        rows_to_append = [[event_id, str(uid)] for uid in going_list]
        if rows_to_append:
            await ws.append_rows(rows_to_append)
        else:
            logger.info("Roster was empty at commitment index. Skipping EventUsers rows insert.")
    except Exception as e:
        logger.error(f"Google Sheets EventUsers synchronization failed: {repr(e)}")
