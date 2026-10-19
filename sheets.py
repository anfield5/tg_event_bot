import json
import asyncio
import gspread_asyncio
from gspread.exceptions import WorksheetNotFound
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials
from config import GOOGLE_CREDENTIALS_JSON, CONTROL_SHEET_ID, logger
from utils import now2ddmmyy
import sqlite3
import db

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


async def open_spreadsheet(sheet_id, _retries=4, _retry_delay=2.0):
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

    Retries up to 4 times (with exponential backoff: 2s, 4s, 8s, 16s) on a
    Google OAuth token-refresh failure ("invalid_grant: Invalid grant:
    account not found") - a documented, sometimes-transient error (server
    clock skew or a flaky token refresh, not always a genuinely revoked
    credential) that otherwise surfaces as a confusing, unrecoverable
    "Failed" partway through a bulk operation like /refreshusersall, for
    what may just be a one-off hiccup. Strengthened from a single 1.5s
    retry after a real report where that still exhausted every attempt
    with server clock sync and the fix's own deployment both confirmed
    fine - if this STILL exhausts every attempt, the credential itself
    (or a rate limit on it) needs checking directly in Google Cloud
    Console, not another code-side retry.
    """
    if not sheet_id:
        return None
    if sheet_id in _spreadsheet_cache:
        return _spreadsheet_cache[sheet_id]
    last_error = None
    for attempt in range(_retries + 1):
        try:
            gc = await agcm.authorize()
            ss = await gc.open_by_key(sheet_id)
            _spreadsheet_cache[sheet_id] = ss
            return ss
        except Exception as e:
            last_error = e
            if "invalid_grant" not in str(e) or attempt == _retries:
                raise
            logger.warning(
                f"open_spreadsheet: transient invalid_grant on attempt {attempt + 1}/{_retries + 1} "
                f"for sheet {sheet_id}, retrying: {e}"
            )
            await asyncio.sleep(_retry_delay * (2 ** attempt))
    raise last_error


async def get_sheet_for_chat(chat_id):
    """
    Resolves which spreadsheet ID this chat's hub should write to - or None
    if it shouldn't write to Sheets at all right now:
      - free tier              -> None (no Sheets writes for free hubs, period)
      - premium, no sheet_id   -> None (nothing configured yet to write to)
      - premium, has sheet_id  -> that sheet_id
    """
    conn = sqlite3.connect(db.DB_PATH)
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


async def sync_users_sheet(chat_id, current_members: list, sheet_owner_chat_id=None, left_members=None):
    """
    Syncs the "Users" worksheet for a given chat/place with its current
    (best-known) membership.

    current_members: list of (user_id, username) or
    (user_id, username, first_name, last_name) tuples for people confirmed
    to currently be in `chat_id` right now. The 2-tuple form is still
    accepted for backward compatibility - first_name/last_name just won't
    be written for those entries.

    Columns: USER_ID, FIRST_NAME, LAST_NAME, USER_NAME, CHAT_ID, STATUS,
    DATE_start, DATE_end, ARCHIVED_USER_NAME.
    A row is uniquely identified by (USER_ID, CHAT_ID) - the same person can
    have separate rows for separate places/groups managed by this bot.

    `sheet_owner_chat_id`: which hub's spreadsheet to write to, if
    different from `chat_id` itself - needed for a monitored child chat
    (see /addmonitor), which is never independently registered in
    all_groups with its own PRO subscription/sheet_id. Rows are still
    correctly tagged with `chat_id` (the actual place these people are
    members of); only the SHEET LOOKUP uses `sheet_owner_chat_id`.
    Defaults to `chat_id` itself, matching every other caller (a plain
    hub calling this about its own membership).

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

    UserPresenceLog (one row per completed stay: USER_ID, CHAT_ID,
    DATE_start, DATE_end) is kept so that EVERY stay that ends gets a row:
      - It is written FIRST, in one read + one append, and only then are
        statuses flipped to LEFT. If the log write fails, nothing is flipped:
        the person stays MEMBER and the next refresh retries (the log is
        de-duplicated, so a retry never doubles a row). Without this a failed
        log write left a LEFT row that nothing would ever log again.
      - It goes into the sheet of `sheet_owner_chat_id` - the same one the
        Users tab uses. (A monitored child chat has no sheet of its own; its
        stays used to be looked up under the child's own id and silently
        skipped, so only the hub itself was ever logged.)
      - Rows that are ALREADY "LEFT" but missing from the log (an earlier
        failed write, or the previous stay of someone who has just come back)
        are logged too, before a rejoin overwrites their DATE_start/DATE_end.

    `left_members` - (user_id, username, first_name, last_name, left_at) for people a refresh confirmed
    as gone. Anyone with NO Users row yet (never synced: they joined and left between two refreshes, or
    never interacted with the bot) gets one with STATUS "LEFT", plus their UserPresenceLog row. Their join
    time is unknown, so DATE_start is set one hour before `left_at`. People who already have a row, and
    people who are actually present, are ignored here - the normal MEMBER -> LEFT path handles them.

    Returns None when this hub has no sheet at all (nothing to do), True when
    everything was written, and False when something couldn't be (details in
    the log) - the Users/log changes that failed are retried by the next call.
    """
    sheet_target = await get_sheet_for_chat(sheet_owner_chat_id or chat_id)
    ss = await open_spreadsheet(sheet_target)
    if not ss:
        return None  # free tier / no sheet configured / subscription expired - nothing to write
    try:
        ws = await ss.worksheet("Users")
        records = await ws.get_all_records()

        index = {}
        for idx, r in enumerate(records, start=2):
            key = (str(r.get("USER_ID", "")).strip(), str(r.get("CHAT_ID", "")).strip())
            index[key] = (idx, r)

        # Who is here right now (people without an id/username can't be keyed).
        members = []
        for member in current_members:
            if len(member) == 4:
                user_id, username, first_name, last_name = member
            else:
                user_id, username = member
                first_name, last_name = None, None
            if not user_id or not username:
                continue
            members.append((user_id, username, first_name, last_name))
        current_keys = {(str(m[0]), str(chat_id)) for m in members}

        now = now2ddmmyy()

        # 1. Presence log FIRST (see the docstring): every stay that has ended,
        #    from the sheet's state BEFORE anything below overwrites it.
        leaving, stays = [], []
        for (uid, place), (idx, rec) in index.items():
            if place != str(chat_id):
                continue
            status = str(rec.get("STATUS", "")).strip().lower()
            date_start = str(rec.get("DATE_start", "")).strip()
            if status == "member" and (uid, place) not in current_keys:
                leaving.append((idx, date_start))
                stays.append((uid, place, date_start, now))
            elif status == "left":
                date_end = str(rec.get("DATE_end", "")).strip()
                if date_end:
                    stays.append((uid, place, date_start, date_end))
        # Confirmed leavers the sheet has never heard of (see the docstring).
        new_left_rows = []
        for l_uid, l_username, l_first, l_last, l_at in (left_members or []):
            l_uid = str(l_uid)
            if (l_uid, str(chat_id)) in index or (l_uid, str(chat_id)) in current_keys:
                continue
            l_end = str(l_at or now)
            l_start = _shift_hours(l_end, -1)
            stays.append((l_uid, str(chat_id), l_start, l_end))
            new_left_rows.append([l_uid, l_first or "", l_last or "", l_username or "", str(chat_id),
                                  "LEFT", l_start, l_end, ""])
        presence_ok = True
        try:
            await log_presence_stays(ss, stays)
        except Exception as e:
            presence_ok = False
            logger.error(
                f"Google Sheets UserPresenceLog write failed for {chat_id} - nobody is flipped to LEFT "
                f"this round, the next refresh retries: {repr(e)}"
            )

        # 2. Members who are here now.
        for user_id, username, first_name, last_name in members:
            key = (str(user_id), str(chat_id))
            if key in index:
                idx, rec = index[key]
                old_name = str(rec.get("USER_NAME", "")).strip()
                status = str(rec.get("STATUS", "")).strip().lower()

                if old_name and old_name != username:
                    archived     = str(rec.get("ARCHIVED_USER_NAME", "")).strip()
                    new_archived = f"{archived},{old_name}" if archived else old_name
                    await ws.update(f"D{idx}", [[username]])
                    await ws.update(f"I{idx}", [[new_archived]])

                if status == "left" and presence_ok:
                    # LEFT -> MEMBER: update status, clear DATE_end, update DATE_start
                    # (their previous stay was logged in step 1 before this overwrites it)
                    await ws.update(f"F{idx}", [["MEMBER"]])
                    await ws.update(f"G{idx}", [[now2ddmmyy()]])
                    await ws.update(f"H{idx}", [[""]])
                # If status is already MEMBER, do nothing

                # Only write what actually differs: this used to rewrite both cells for EVERY
                # present member on EVERY refresh - 2 API calls per person, each throttled to
                # ~1/s by gspread_asyncio, to store the values that were already there.
                if first_name and str(rec.get("FIRST_NAME", "")).strip() != first_name:
                    await ws.update(f"B{idx}", [[first_name]])
                if last_name and str(rec.get("LAST_NAME", "")).strip() != last_name:
                    await ws.update(f"C{idx}", [[last_name]])
            else:
                # New user: add with MEMBER status
                await ws.append_row([str(user_id), first_name or "", last_name or "", username, str(chat_id),
                                      "MEMBER", now2ddmmyy(), "", ""])

        # 3. Members who left: only now that their stay is safely in the log.
        if presence_ok:
            for idx, _date_start in leaving:
                await ws.update(f"F{idx}", [["LEFT"]])
                await ws.update(f"H{idx}", [[now]])
            if new_left_rows:
                await ws.append_rows(new_left_rows)
        return presence_ok
    except Exception as e:
        logger.error(f"Google Sheets Users synchronization failed: {repr(e)}")
        return False

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


def _shift_hours(date_str, hours):
    """`date_str` moved by `hours`, in the bot's own stored format (DD.MM.YYYY HH:MM:SS.mmm); "" if unreadable."""
    for fmt in ("%d.%m.%Y %H:%M:%S.%f", "%d.%m.%Y %H:%M:%S"):
        try:
            return (datetime.strptime(str(date_str).strip(), fmt) + timedelta(hours=hours)).strftime(
                "%d.%m.%Y %H:%M:%S.%f")[:-3]
        except ValueError:
            continue
    return ""


def _presence_key(user_id, chat_id, date_start, date_end):
    """
    Identity of one logged stay: (user, chat, DATE_start) - a stay has exactly
    one start. A stay with no recorded start falls back to its end date, so two
    blank-start stays of the same person aren't mistaken for one another.
    """
    uid, cid = str(user_id).strip(), str(chat_id).strip()
    start, end = str(date_start or "").strip(), str(date_end or "").strip()
    return (uid, cid, start) if start else (uid, cid, "", end)


async def log_presence_stays(ss, stays) -> int:
    """
    Appends every stay in `stays` - (user_id, chat_id, date_start, date_end) -
    that isn't already in the hub's UserPresenceLog tab, with ONE read and ONE
    append however many there are. Returns how many rows were written.

    A spreadsheet without that tab is tolerated (nothing to write to; a warning
    is logged). Every other failure is RAISED, so the caller can leave the
    "LEFT" flips undone and retry on the next refresh instead of ending up with
    a LEFT row that has no log entry.
    """
    if not stays:
        return 0
    try:
        ws = await ss.worksheet("UserPresenceLog")
    except WorksheetNotFound:
        logger.warning("UserPresenceLog tab not found in this spreadsheet - presence history can't be recorded")
        return 0
    existing = await ws.get_all_records()
    have = {
        _presence_key(r.get("USER_ID"), r.get("CHAT_ID"), r.get("DATE_start"), r.get("DATE_end"))
        for r in existing
    }
    rows = []
    for uid, cid, start, end in stays:
        key = _presence_key(uid, cid, start, end)
        if key in have:
            continue
        have.add(key)
        rows.append([str(uid), str(cid), str(start or ""), str(end or "")])
    if rows:
        await ws.append_rows(rows)
    return len(rows)


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
    subs_date_start, subs_date_end, visibility, date_bot_add, role) tuples.
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
                   "SUBS_DATE_START", "SUBS_DATE_END", "VISIBILITY", "DATE_BOT_ADD", "ROLE"]
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

    rows: list of (chat_id, chat_name, visibility, date_bot_add, role) tuples.
    Returns True on success, False on failure.
    """
    if not CONTROL_SHEET_ID:
        logger.error("sync_control_sheet_channels: CONTROL_SHEET_ID is not configured.")
        return False
    try:
        ss = await open_spreadsheet(CONTROL_SHEET_ID)
        ws = await ss.worksheet("CHANNELS")
        header = ["CHAT_ID", "CHAT_NAME", "VISIBILITY", "DATE_BOT_ADD", "ROLE"]
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

    rows: list of (chat_id, date_bot_add, date_bot_remove) tuples.
    Returns True on success, False on failure.
    """
    if not CONTROL_SHEET_ID:
        logger.error("sync_control_sheet_chats_log: CONTROL_SHEET_ID is not configured.")
        return False
    try:
        ss = await open_spreadsheet(CONTROL_SHEET_ID)
        ws = await ss.worksheet("chats_log")
        header = ["CHAT_ID", "DATE_BOT_ADD", "DATE_BOT_REMOVE"]
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


_TIER_ORDER = {"FREE": 0, "PRO": 1, "OWNER": 2}


async def sync_control_sheet_botconfig(feature_rows: list):
    """
    Overwrites the "BOTCONFIG" tab of the Control Sheet with the current
    all_features table (db.get_all_features()) - the actual source of
    truth for what's available at each tier, not just reference data.

    feature_rows: list of (feature_key, feature_label, min_tier,
    limit_count, description) tuples. For each row, FREE/PRO/OWNER columns
    are computed from the tier hierarchy (OWNER >= PRO >= FREE): a feature
    with min_tier='PRO' shows "no" under FREE and "yes" under PRO/OWNER.
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
        header = ["FEATURE_KEY", "FEATURE", "FREE", "PRO", "OWNER", "DESCRIPTION"]
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
                _cell("OWNER"),
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


# Tabs whose first column is an EVENT_ID that belongs to exactly one event.
EVENT_LINKED_TABS = ("Events", "Actions", "EventUsers")


def _contiguous_runs(rows):
    """[2, 3, 5, 8, 9] -> [(2, 3), (5, 5), (8, 9)] (1-based, inclusive)."""
    runs = []
    for r in rows:
        if runs and r == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], r)
        else:
            runs.append((r, r))
    return runs


async def mark_event_row_canceled(ss, event_id, closed_date, fallback_row):
    """
    Marks the event's row in the Events tab CANCELED (CLOSED_AT, STATUS, GOING_COUNT = 0), or
    appends `fallback_row` if the event has no row there yet. Shared by the Cancel button and
    /newevent's self-healing of an orphaned event, which used to carry identical copies of this.
    Takes the already-opened spreadsheet so each caller keeps its own sheet lookup. Raises on failure.
    """
    ws = await ss.worksheet("Events")
    records = await ws.get_all_records()
    for idx, r in enumerate(records, start=2):
        if str(r.get("EVENT_ID")) == str(event_id):
            await ws.update(f"F{idx}:H{idx}", [[closed_date, "CANCELED", 0]])
            return
    await ws.append_row(fallback_row)


async def delete_event_from_sheet(chat_id, event_id):
    """
    Removes EVERY row belonging to `event_id` from the hub's Events, Actions
    and EventUsers tabs (the header row is never touched). All deletions go
    out as ONE spreadsheet batchUpdate - a single API request (Google
    applies a batch atomically) - with each tab's row ranges ordered
    bottom-to-top, so removing a range never shifts the indices of the
    ranges still waiting to be removed.

    Returns {tab: rows_removed}, or None when this hub has no Sheet
    connected at all (free tier / expired / not set up - nothing to clean).
    A tab missing from the sheet is skipped. Any connection/auth/API
    failure is RAISED - the caller must treat that as "nothing was
    cleaned" rather than carry on with a half-finished delete.
    """
    sheet_target = await get_sheet_for_chat(chat_id)
    ss = await open_spreadsheet(sheet_target)
    if not ss:
        return None

    counts, requests = {}, []
    for tab in EVENT_LINKED_TABS:
        try:
            ws = await ss.worksheet(tab)
        except WorksheetNotFound:
            counts[tab] = 0
            continue
        column_a = await ws.col_values(1)
        rows = [i for i, value in enumerate(column_a, start=1)
                if i > 1 and str(value).strip() == str(event_id)]
        counts[tab] = len(rows)
        for start, end in reversed(_contiguous_runs(rows)):
            requests.append({"deleteDimension": {"range": {
                "sheetId": ws.id, "dimension": "ROWS",
                "startIndex": start - 1, "endIndex": end,
            }}})

    if requests:
        await ss.batch_update({"requests": requests})
    return counts
