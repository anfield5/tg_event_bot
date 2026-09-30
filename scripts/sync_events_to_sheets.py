"""
One-time (and safe-to-rerun) sync: if Google Sheets writes were broken for
a period (bad/expired credentials, etc.), any event created, closed, or
cancelled during that window exists in the DB but is MISSING from that
hub's Events/Actions sheets entirely - the same failure mode this whole
troubleshooting thread was about (newevent/Save&Close/Cancel's Sheets
write silently failing, now visibly warned about, but the missed rows
still need a one-time catch-up).

Only considers events from the last N days (default 21 - "the last 3
weeks") by created_date, so a normal re-run doesn't re-scan the entire
history every time; pass --days to change the window.

This script, for every PRO hub with a sheet_id configured:
  1. Reads every event in the DB for that hub with created_date within
     the window.
  2. Reads that hub's own Events tab, collecting which EVENT_IDs are
     ALREADY there.
  3. Appends a row for every DB event NOT yet in the sheet - using the
     exact same 8-column layout /newevent's own write uses (EVENT_ID,
     EVENT_NAME, CREATED_DATE, CREATED_BY, EVENT_DATE, CLOSED_AT, STATUS,
     GOING_COUNT).
  4. For any CLOSED event that's missing, ALSO backfills its EventUsers
     rows (event_id, user_id for everyone who was "going"), matching
     exactly what Save & Close itself writes.
  5. ALSO backfills Actions - see the important caveat below.

IMPORTANT CAVEAT about Actions: the DB does NOT keep a click-by-click
history the way the Actions sheet normally records one row per button
press. It only keeps each person's CURRENT final status (going/notgoing/
kicked) per event, and the event's own created_date/closed_date. So
Actions backfill is an APPROXIMATION, not a perfect reconstruction:
  - One synthetic row per event_users entry, using their final status
    (GOING/NOTGOING/KICKED) as the ACTION and the event's closed_date
    (or created_date if still open) as the DATE - not their real,
    individual click timestamp, which isn't stored anywhere.
  - Someone who clicked Going, then Not Going, then Going again during
    the outage only gets ONE row here (their final state) - the
    intermediate clicks are genuinely unrecoverable.
  - One row for the event's own lifecycle action (SAVE/DIRECTCLOSE/
    CANCEL) if the event reached that state, using created_by_user_id
    as a stand-in for who performed it (the real closer/canceller isn't
    separately stored).
Only added when that event_id has ZERO existing Actions rows at all -
if even one genuine row already made it through before the outage
started, this script leaves that event's Actions history alone rather
than mixing real and synthetic rows together.

Never touches a row that's already present - matching by EVENT_ID -
so it's safe to run repeatedly or accidentally twice; it only ever
ADDS missing rows, never edits/overwrites an existing one.

Run once, from the project root, against your real database.db:
    python3 scripts/sync_events_to_sheets.py

Change the time window (default 21 days):
    python3 scripts/sync_events_to_sheets.py --days 30

Target a single hub instead of every PRO hub:
    python3 scripts/sync_events_to_sheets.py --chat-id -1001234567
"""
import sys
import argparse
import asyncio
import sqlite3
from datetime import datetime, timedelta

sys.path.insert(0, ".")
from db import init_db, DB_PATH  # noqa: E402
from sheets import get_sheet_for_chat, open_spreadsheet  # noqa: E402

STATUS_LABELS = {-1: "CANCELED", 0: "OPEN", 1: "VERIFICATION", 2: "CLOSED"}
CLOSE_ACTION_LABELS = {-1: "CANCEL", 2: "SAVE"}
DATE_FMT = "%d.%m.%Y %H:%M:%S.%f"


def _parse_created_date(raw):
    if not raw:
        return None
    try:
        return datetime.strptime(raw, DATE_FMT)
    except ValueError:
        return None


async def _sync_one_hub(chat_id: str, db_path: str, cutoff: datetime) -> tuple:
    """Returns (events_added, event_users_backfilled, actions_added) for this hub."""
    sheet_id = await get_sheet_for_chat(chat_id)
    if not sheet_id:
        print(f"  chat_id={chat_id}: no bound Sheet - skipped")
        return 0, 0, 0

    try:
        ss = await open_spreadsheet(sheet_id)
    except Exception as e:
        print(f"  chat_id={chat_id}: could not open Sheet ({e}) - skipped")
        return 0, 0, 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    all_events = conn.execute(
        "SELECT event_id, name, created_date, created_by_user_id, event_date, "
        "closed_date, event_status FROM events WHERE chat_id = ?",
        (chat_id,),
    ).fetchall()
    db_events = [r for r in all_events if (_parse_created_date(r["created_date"]) or datetime.min) >= cutoff]

    if not db_events:
        conn.close()
        print(f"  chat_id={chat_id}: no events in the last window - nothing to check")
        return 0, 0, 0

    try:
        ws_events = await ss.worksheet("Events")
        events_existing_ids = {str(r.get("EVENT_ID", "")) for r in await ws_events.get_all_records()}
    except Exception as e:
        conn.close()
        print(f"  chat_id={chat_id}: could not read Events tab ({e}) - skipped")
        return 0, 0, 0

    try:
        ws_actions = await ss.worksheet("Actions")
        actions_existing_ids = {str(r.get("EVENT_ID", "")) for r in await ws_actions.get_all_records()}
    except Exception as e:
        print(f"  chat_id={chat_id}: could not read Actions tab ({e}) - Actions backfill skipped for this hub")
        ws_actions, actions_existing_ids = None, set()

    events_added = 0
    event_users_backfilled = 0
    actions_added = 0

    for row in db_events:
        event_id = row["event_id"]
        eu_rows = conn.execute(
            "SELECT user_id, username, status, guests FROM event_users WHERE event_id = ?",
            (event_id,),
        ).fetchall()

        # ── Events tab ──────────────────────────────────────────────
        if event_id not in events_existing_ids:
            status_label = STATUS_LABELS.get(row["event_status"], "OPEN")
            going_count = 0
            if row["event_status"] == 2:
                going_count = sum((1 if r["status"] == "going" else 0) + (r["guests"] or 0) for r in eu_rows)
            try:
                await ws_events.append_row([
                    event_id, row["name"] or "", row["created_date"] or "",
                    row["created_by_user_id"] or "", row["event_date"] or "",
                    row["closed_date"] or "", status_label, going_count,
                ])
                events_added += 1
                print(f"  chat_id={chat_id}: added Events row for {event_id} ({status_label})")
            except Exception as e:
                print(f"  chat_id={chat_id}: failed to add Events row for {event_id}: {e}")

            if row["event_status"] == 2:
                try:
                    ws_eu = await ss.worksheet("EventUsers")
                    eu_existing = await ws_eu.get_all_records()
                    if not any(str(r.get("EVENT_ID", "")) == event_id for r in eu_existing):
                        going_ids = [r["user_id"] for r in eu_rows if r["status"] == "going" and r["user_id"]]
                        if going_ids:
                            await ws_eu.append_rows([[event_id, str(uid)] for uid in going_ids])
                        event_users_backfilled += 1
                        print(f"    -> backfilled {len(going_ids)} EventUsers row(s) for {event_id}")
                except Exception as e:
                    print(f"    -> failed to backfill EventUsers for {event_id}: {e}")

        # ── Actions tab (approximation - see module docstring) ─────
        if ws_actions is not None and event_id not in actions_existing_ids:
            date_for_actions = row["closed_date"] or row["created_date"] or ""
            synthetic_rows = []
            for r in eu_rows:
                if r["status"] not in ("going", "notgoing", "kicked"):
                    continue
                synthetic_rows.append([
                    event_id, r["status"].upper(), r["username"] or "", str(r["user_id"] or ""),
                    date_for_actions, chat_id,
                ])
            close_action = CLOSE_ACTION_LABELS.get(row["event_status"])
            if close_action:
                synthetic_rows.append([
                    event_id, close_action, "", row["created_by_user_id"] or "",
                    date_for_actions, chat_id,
                ])
            if synthetic_rows:
                try:
                    await ws_actions.append_rows(synthetic_rows)
                    actions_added += 1
                    print(f"    -> backfilled {len(synthetic_rows)} approximate Actions row(s) for {event_id}")
                except Exception as e:
                    print(f"    -> failed to backfill Actions for {event_id}: {e}")

    conn.close()
    return events_added, event_users_backfilled, actions_added


async def _run(target_chat_id: str = None, days: int = 21, db_path: str = DB_PATH):
    init_db(db_path=db_path)
    cutoff = datetime.now() - timedelta(days=days)
    conn = sqlite3.connect(db_path)
    if target_chat_id:
        chat_ids = [target_chat_id]
    else:
        rows = conn.execute("SELECT chat_id FROM all_groups WHERE type = 'PRO'").fetchall()
        chat_ids = [r[0] for r in rows]
    conn.close()

    if not chat_ids:
        print("No PRO hubs found - nothing to sync.")
        return

    print(f"Checking {len(chat_ids)} PRO hub(s), events created in the last {days} day(s)...")
    total_events, total_eu, total_actions = 0, 0, 0
    for chat_id in chat_ids:
        events_added, eu_backfilled, actions_added = await _sync_one_hub(chat_id, db_path, cutoff)
        total_events += events_added
        total_eu += eu_backfilled
        total_actions += actions_added

    print(f"\nDone. {total_events} Events row(s) added, {total_eu} event(s)' EventUsers backfilled, "
          f"{total_actions} event(s)' Actions backfilled.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill DB events missing from Google Sheets.")
    parser.add_argument("--chat-id", default=None, help="Target a single hub instead of every PRO hub.")
    parser.add_argument("--days", type=int, default=21, help="Only consider events created in the last N days (default 21).")
    args = parser.parse_args()
    asyncio.run(_run(target_chat_id=args.chat_id, days=args.days))
