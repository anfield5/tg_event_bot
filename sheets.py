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


def get_service_account_email():
    """
    Returns just the service account's email address (the one that needs
    Editor access on any Sheet this bot should be able to write to) -
    used for reminders in /setsub and /setsheet. Returns None if
    GOOGLE_CREDENTIALS_JSON isn't configured or doesn't parse.
    """
    if not GOOGLE_CREDENTIALS_JSON:
        return None
    try:
        return json.loads(GOOGLE_CREDENTIALS_JSON).get("client_email")
    except (json.JSONDecodeError, AttributeError):
        return None

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
        "SELECT type, sheet_id, subs_date_end FROM all_groups WHERE chat_id = ?",
        (str(chat_id),),
    )
    row = cursor.fetchone()
    conn.close()

    if not row:
        return None  # unregistered hub defaults to free - no Sheets writes

    chat_type, sheet_id, subs_date_end = row
    if chat_type != "PRO":
        return None

    if not subs_date_end:
        return None
    try:
        if datetime.strptime(subs_date_end, _SUBS_DATE_FORMAT) <= datetime.now():
            return None  # premium subscription expired - treat as free
    except ValueError:
        return None

    return sheet_id or None


async def sync_users_sheet(chat_id, current_members: list):
    """
    Syncs the "Users" worksheet for a given chat/place with its current
    (best-known) membership.

    current_members: list of (user_id, username) or
    (user_id, username, first_name, last_name) tuples for people confirmed
    to currently be in `chat_id` right now. The 2-tuple form is still
    accepted for backward compatibility - first_name/last_name just won't
    be written for those entries.

    Columns: USER_ID, USER_NAME, CHAT_ID, STATUS, DATE_start, DATE_end,
    ARCHIVED_USER_NAME, FIRST_NAME, LAST_NAME.
    A row is uniquely identified by (USER_ID, CHAT_ID) - the same person can
    have separate rows for separate places/groups managed by this bot.

    Behavior:
      - Member not yet in the sheet for this place -> append a new row with
        STATUS = "MEMBER", DATE_start = current date, DATE_end = blank.
      - Member already in the sheet whose USER_NAME changed -> the old name
        is appended to ARCHIVED_USER_NAME (comma-separated), and USER_NAME is
        updated to the current one. If they were previously "LEFT", their
        STATUS flips back to "MEMBER" and DATE_end is cleared.
      - FIRST_NAME/LAST_NAME are (re)written whenever we have a fresh value
        for them, even for an already-known row - covers someone who
        changed their Telegram name, or whose name simply wasn't captured
        the first time this feature existed.
      - Any existing row for this CHAT_ID that ISN'T in current_members ->
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
            key = (str(r.get("USER_ID", "")).strip(), str(r.get("CHAT_ID", "")).strip())
            index[key] = (idx, r)

        current_keys = set()
        for member in current_members:
            if len(member) == 4:
                user_id, username, first_name, last_name = member
            else:
                user_id, username = member
                first_name, last_name = None, None
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

                if first_name:
                    await ws.update(f"H{idx}", [[first_name]])
                if last_name:
                    await ws.update(f"I{idx}", [[last_name]])
            else:
                # New user: add with MEMBER status
                await ws.append_row([str(user_id), username, str(chat_id), "MEMBER", now2ddmmyy(), "",
                                      "", first_name or "", last_name or ""])

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


async def log_user_presence_if_not_exists(chat_id, user_id, presence_chat_id, date_start, date_end):
    """
    Logs user presence to UserPresenceLog sheet when a user leaves a monitored
    group or the main group.
    Columns: USER_ID, CHAT_ID, DATE_start, DATE_end
    """
    sheet_target = await get_sheet_for_chat(chat_id)
    ss = await open_spreadsheet(sheet_target)
    if not ss:
        return  # free tier / no sheet configured / subscription expired - nothing to write
    try:
        ws = await ss.worksheet("UserPresenceLog")
        records = await ws.get_all_records()

        # if it exists in UserPresenceLog with same USER_ID, CHAT_ID and same DATE_start — we do NOT write a duplicate.
        for r in records:
            if (str(r.get("USER_ID", "")).strip() == str(user_id) and 
                str(r.get("CHAT_ID", "")).strip() == str(presence_chat_id) and 
                str(r.get("DATE_start", "")).strip() == str(date_start)):
                return  # already logged

        await ws.append_row([str(user_id), str(presence_chat_id), str(date_start), str(date_end)])
    except Exception as e:
        logger.error(f"Google Sheets UserPresenceLog check failed: {repr(e)}")


async def sync_control_sheet_main(rows: list) -> bool:
    """
    Overwrites the "GROUPS" tab of the Control Sheet (CONTROL_SHEET_ID) with
    the current contents of all_groups, so the bot owner can see
    every group using the bot and its subscription status in one place,
    without needing a Telegram command or a separate web dashboard.

    This is a ONE-WAY push (SQLite -> Sheet). Editing a cell in the Sheet by
    hand does NOT change anything back in SQLite - /setsub remains the only
    way to actually change a subscription. This tab is a read-only mirror
    for visibility, not a control surface (yet).

    rows: list of (chat_id, chat_name, type, sheet_id, sheet_name,
    subs_date_start, subs_date_end, visibility, date_bot_add) tuples.
    Returns True on success, False on failure (logged either way) - so
    callers can tell the user honestly instead of always claiming success.
    """
    if not CONTROL_SHEET_ID:
        logger.error("sync_control_sheet_main: CONTROL_SHEET_ID is not configured.")
        return False
    try:
        ss = await open_spreadsheet(CONTROL_SHEET_ID)
        ws = await ss.worksheet("GROUPS")
        header = ["CHAT_ID", "CHAT_NAME", "TYPE", "SHEET_ID", "SHEET_NAME",
                   "SUBS_DATE_START", "SUBS_DATE_END", "VISIBILITY", "DATE_BOT_ADD"]
        body   = [[str(v) if v is not None else "" for v in row] for row in rows]
        grid   = [header] + body

        # Find out how many rows this tab currently holds BEFORE writing,
        # so we know whether any stale trailing rows need clearing after -
        # deliberately done this way round (write first, trim leftovers
        # after) so a failure never leaves the tab completely blank: worst
        # case on a partial failure is a few harmless stale rows left below
        # the fresh data, not "everything is gone".
        try:
            existing_row_count = len(await ws.get_all_values())
        except Exception:
            existing_row_count = 0

        await ws.update("A1", grid)

        if existing_row_count > len(grid):
            await ws.batch_clear([f"A{len(grid) + 1}:Z{existing_row_count}"])

        return True
    except Exception as e:
        logger.error(f"Google Sheets Control/Groups sync failed: {repr(e)}")
        return False


async def sync_control_sheet_channels(rows: list) -> bool:
    """
    Overwrites the "CHANNELS" tab of the Control Sheet with the current
    contents of all_channels - every channel the bot is currently a member
    of. Same one-way push, write-first-then-trim pattern as
    sync_control_sheet_main (see its docstring for why).

    rows: list of (chat_id, chat_name, visibility, date_bot_add) tuples.
    Returns True on success, False on failure.
    """
    if not CONTROL_SHEET_ID:
        logger.error("sync_control_sheet_channels: CONTROL_SHEET_ID is not configured.")
        return False
    try:
        ss = await open_spreadsheet(CONTROL_SHEET_ID)
        ws = await ss.worksheet("CHANNELS")
        header = ["CHAT_ID", "CHAT_NAME", "VISIBILITY", "DATE_BOT_ADD"]
        body   = [[str(v) if v is not None else "" for v in row] for row in rows]
        grid   = [header] + body

        try:
            existing_row_count = len(await ws.get_all_values())
        except Exception:
            existing_row_count = 0

        await ws.update("A1", grid)

        if existing_row_count > len(grid):
            await ws.batch_clear([f"A{len(grid) + 1}:Z{existing_row_count}"])

        return True
    except Exception as e:
        logger.error(f"Google Sheets Control/Channels sync failed: {repr(e)}")
        return False


async def sync_control_sheet_chats_log(rows: list) -> bool:
    """
    Overwrites the "chats_log" tab of the Control Sheet with the current
    contents of all_chats_bot_log - the historical trail of every time the
    bot was added to and later removed from a group/channel (all_groups/
    all_channels only ever reflect PRESENT chats; this is the append-only
    history). Same one-way push, write-first-then-trim pattern as
    sync_control_sheet_main/sync_control_sheet_channels.

    rows: list of (chat_id, date_bot_add, date_bot_removed) tuples.
    Returns True on success, False on failure.
    """
    if not CONTROL_SHEET_ID:
        logger.error("sync_control_sheet_chats_log: CONTROL_SHEET_ID is not configured.")
        return False
    try:
        ss = await open_spreadsheet(CONTROL_SHEET_ID)
        ws = await ss.worksheet("chats_log")
        header = ["CHAT_ID", "DATE_BOT_ADD", "DATE_BOT_REMOVED"]
        body   = [[str(v) if v is not None else "" for v in row] for row in rows]
        grid   = [header] + body

        try:
            existing_row_count = len(await ws.get_all_values())
        except Exception:
            existing_row_count = 0

        await ws.update("A1", grid)

        if existing_row_count > len(grid):
            await ws.batch_clear([f"A{len(grid) + 1}:Z{existing_row_count}"])

        return True
    except Exception as e:
        logger.error(f"Google Sheets Control/chats_log sync failed: {repr(e)}")
        return False


_TIER_ORDER = {"FREE": 0, "PRO": 1, "ADMIN": 2}


async def sync_control_sheet_botconfig(feature_rows: list):
    """
    Overwrites the "BOTCONFIG" tab of the Control Sheet with the current
    feature_flags table (db.get_feature_flags()) - the actual source of
    truth for what's available at each tier, not just reference data.

    feature_rows: list of (feature_key, feature_label, min_tier,
    limit_count, description) tuples. For each row, FREE/PRO/ADMIN columns
    are computed from the tier hierarchy (ADMIN >= PRO >= FREE): a feature
    with min_tier='PRO' shows "no" under FREE and "yes" under PRO/ADMIN.
    limit_count only ever applies to the tier that EQUALS min_tier exactly
    (shown as "yes(limit N)") - any tier above min_tier is unlimited by
    construction and just shows a plain "yes".

    Returns True on success, False on failure (logged either way).
    """
    if not CONTROL_SHEET_ID:
        logger.error("sync_control_sheet_botconfig: CONTROL_SHEET_ID is not configured.")
        return False
    try:
        ss = await open_spreadsheet(CONTROL_SHEET_ID)
        ws = await ss.worksheet("BOTCONFIG")
        header = ["FEATURE_KEY", "FEATURE", "FREE", "PRO", "ADMIN", "DESCRIPTION"]
        body = []
        for feature_key, feature_label, min_tier, limit_count, description in feature_rows:
            required = _TIER_ORDER.get(min_tier, 0)

            def _cell(tier_name):
                if _TIER_ORDER[tier_name] < required:
                    return "no"
                if tier_name == min_tier and limit_count is not None:
                    return f"yes(limit {limit_count})"
                return "yes"

            body.append([
                feature_key,
                feature_label,
                _cell("FREE"),
                _cell("PRO"),
                _cell("ADMIN"),
                description or "",
            ])
        grid = [header] + body

        try:
            existing_row_count = len(await ws.get_all_values())
        except Exception:
            existing_row_count = 0

        await ws.update("A1", grid)

        if existing_row_count > len(grid):
            await ws.batch_clear([f"A{len(grid) + 1}:Z{existing_row_count}"])

        return True
    except Exception as e:
        logger.error(f"Google Sheets Control/BOTCONFIG sync failed: {repr(e)}")
        return False
