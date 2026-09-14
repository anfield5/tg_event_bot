"""
One-time backfill: events.created_date/closed_date were added as real DB
columns in v4.3.6 (previously only ever written to the bound Google
Sheet's "Events" tab, never persisted in the DB itself). Any event closed
BEFORE that deploy has NULL created_date/closed_date in the DB, even
though the exact same values already sit in its hub's Sheet.

This script reads every PRO hub's bound "Events" tab and copies
CREATED_DATE/CLOSED_AT back into the DB - but ONLY into rows where the DB
value is currently NULL. It never overwrites a value the DB already has
(from a /newevent or Save&Close that ran after v4.3.6), so it's safe to
run repeatedly or accidentally twice.

Run once, from the project root, against your real database.db:
    python3 scripts/backfill_event_dates.py

Or target a single hub instead of every PRO hub (useful for testing, or
to backfill just one group without touching every other hub's Sheet):
    python3 scripts/backfill_event_dates.py <chat_id>

Matches rows by EVENT_ID (column A) - the same identifier already used
as the DB's own primary key, so no ambiguity in which Sheet row belongs
to which DB row. CREATED_DATE is the Sheet's column C, CLOSED_AT is
column F - copied as-is (already in now2ddmmyy()'s DD.MM.YYYY
HH:MM:SS.fff format), no reparsing/reformatting needed since the DB
column is a plain TEXT field expecting exactly that format.
"""
import sys
import asyncio
import sqlite3

sys.path.insert(0, ".")
from db import init_db, DB_PATH, get_connection  # noqa: E402
from sheets import get_sheet_for_chat, open_spreadsheet  # noqa: E402


async def _backfill_one_hub(chat_id: str, db_path: str) -> tuple[int, int]:
    """Returns (created_date_filled, closed_date_filled) counts for this hub."""
    sheet_id = await get_sheet_for_chat(chat_id)
    if not sheet_id:
        print(f"  chat_id={chat_id}: no bound Sheet (free tier, expired, or no sheet_id set) - skipped")
        return 0, 0

    try:
        ss = await open_spreadsheet(sheet_id)
        ws = await ss.worksheet("Events")
        records = await ws.get_all_records()
    except Exception as e:
        print(f"  chat_id={chat_id}: could not read Events tab ({e}) - skipped")
        return 0, 0

    created_filled = 0
    closed_filled = 0
    conn = sqlite3.connect(db_path)
    try:
        for row in records:
            event_id = str(row.get("EVENT_ID", "")).strip()
            if not event_id:
                continue
            sheet_created = str(row.get("CREATED_DATE", "")).strip() or None
            sheet_closed = str(row.get("CLOSED_AT", "")).strip() or None

            db_row = conn.execute(
                "SELECT created_date, closed_date FROM events WHERE event_id = ? AND chat_id = ?",
                (event_id, chat_id),
            ).fetchone()
            if db_row is None:
                continue  # Sheet row with no matching DB row at all - nothing to backfill
            db_created, db_closed = db_row

            if db_created is None and sheet_created:
                conn.execute("UPDATE events SET created_date = ? WHERE event_id = ?", (sheet_created, event_id))
                created_filled += 1
            if db_closed is None and sheet_closed:
                conn.execute("UPDATE events SET closed_date = ? WHERE event_id = ?", (sheet_closed, event_id))
                closed_filled += 1
        conn.commit()
    finally:
        conn.close()

    print(f"  chat_id={chat_id}: filled created_date for {created_filled}, closed_date for {closed_filled} event(s)")
    return created_filled, closed_filled


async def _run(target_chat_id: str = None, db_path: str = DB_PATH):
    init_db(db_path=db_path)  # make sure created_date/closed_date columns exist
    conn = sqlite3.connect(db_path)
    if target_chat_id:
        chat_ids = [target_chat_id]
    else:
        rows = conn.execute("SELECT chat_id FROM all_groups WHERE type = 'PRO'").fetchall()
        chat_ids = [r[0] for r in rows]
    conn.close()

    if not chat_ids:
        print("No PRO hubs found - nothing to backfill.")
        return

    print(f"Backfilling {len(chat_ids)} PRO hub(s)...")
    total_created, total_closed = 0, 0
    for chat_id in chat_ids:
        created, closed = await _backfill_one_hub(chat_id, db_path)
        total_created += created
        total_closed += closed

    print(f"\nDone. created_date filled for {total_created} event(s) total, "
          f"closed_date filled for {total_closed} event(s) total.")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(_run(target))
