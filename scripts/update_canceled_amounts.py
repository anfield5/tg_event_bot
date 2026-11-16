"""
One-off: give every event that was CANCELED earlier its AMOUNT in the Events tab.

Until 4.9.0 cancelling an event wrote AMOUNT = 0 whatever the event had at that moment. Now the bot writes the headcount
the event had when it was canceled (every person going plus every guest, db.headcount_from_rows - the "TOTAL Going" of
the post). The database keeps that data (a canceled event keeps its event_users rows) but no copy of the number, so
this script recomputes it for the canceled events and writes it into column H of the rows that differ - only that cell.

    python3 scripts/update_canceled_amounts.py                       # every hub with a Sheet, dry run
    python3 scripts/update_canceled_amounts.py --chat-id -1001234567 --days 365
    python3 scripts/update_canceled_amounts.py --apply               # write the amounts

A DRY RUN unless --apply. Never touches the database. Safe to run again: rows that are right are left alone.
(scripts/check_events_amount.py checks the AMOUNT of events in every state; this is the same check limited to canceled ones.)
"""
import sys
import argparse
import asyncio

sys.path.insert(0, ".")
from db import DB_PATH  # noqa: E402
import scripts.check_events_amount as amount  # noqa: E402

CANCELED = (-1,)


async def _run(target_chat_id=None, days=None, db_path=DB_PATH, apply=False):
    return await amount._run(target_chat_id, days, db_path=db_path, apply=apply, statuses=CANCELED)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Write the headcount into the AMOUNT of already canceled events (dry run unless --apply).")
    parser.add_argument("--chat-id", default=None, help="A single hub instead of every hub that has a Sheet.")
    parser.add_argument("--days", type=int, default=None, help="Only events created in the last N days (default: every event).")
    parser.add_argument("--apply", action="store_true", help="Write the amounts (without it: only report).")
    args = parser.parse_args()
    outcome = asyncio.run(_run(args.chat_id, args.days, apply=args.apply))
    sys.exit(1 if amount._print_report(outcome, args.apply) else 0)
