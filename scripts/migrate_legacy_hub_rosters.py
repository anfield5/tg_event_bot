"""
One-off: move the hub's own participants of old events from the event's JSON columns into event_users.

Since the unified event_users model the hub's people live in event_users like everyone else's. An event that no click has
touched since then keeps them only in its old JSON columns (going_data, notgoing_data, counters_data, kicked_data) - and if
a child chat's people already got rows, the event is half in one place and half in the other. The post still draws the
hub's people from the JSON, so it looks right, but the Events tab AMOUNT, the EventUsers tab, /stats and the audit scripts
read event_users and miss them (an event whose post says TOTAL Going: 15 is exported as 7). This script finds those
events and, with --apply, does for them what the next click would have done (db.migrate_event_to_event_users) - and also
moves the people that function leaves out because it cannot match them to a user id (a Not Going name without a link, a
name that only brought guests): they are keyed by their name, as every unresolved entry already is.

    python3 scripts/migrate_legacy_hub_rosters.py                  # every hub in the database, dry run
    python3 scripts/migrate_legacy_hub_rosters.py --chat-id -1001234567
    python3 scripts/migrate_legacy_hub_rosters.py --apply

A DRY RUN unless --apply: it lists each event with its headcount now and after the move. Touches only the database - never the
posts and never the Google Sheet. Afterwards bring the Sheet in line:
    python3 scripts/repair_event_users.py --apply      (EventUsers tab + the Events row, AMOUNT included)
    python3 scripts/check_events_amount.py             (should now say all amounts are right)
Safe to run again: an event that has hub rows is left alone.
"""
import sys
import argparse
import sqlite3

sys.path.insert(0, ".")
from db import DB_PATH, event_headcount  # noqa: E402
import scripts.audit_event_users as audit  # noqa: E402


def run(db_path=DB_PATH, target_chat_id=None, apply=False) -> list:
    """-> [{"chat_id", "event_id", "name", "status", "before", "after", "migrated"}] for every event whose hub people are unmigrated."""
    conn = sqlite3.connect(db_path)
    found = []
    try:
        if target_chat_id:
            events = conn.execute("SELECT event_id, chat_id, name, event_status FROM events WHERE chat_id = ? ORDER BY ROWID", (str(target_chat_id),)).fetchall()
        else:
            events = conn.execute("SELECT event_id, chat_id, name, event_status FROM events ORDER BY ROWID").fetchall()
        for event_id, chat_id, name, status in events:
            legacy = audit.hub_json_unmigrated(conn, event_id, chat_id)
            if not legacy:
                continue
            before = event_headcount(event_id, db_path)["total"]
            after = audit.effective_roster(conn, event_id, status, None, chat_id)["headcount"]
            entry = {"chat_id": chat_id, "event_id": event_id, "name": name, "status": status,
                     "before": before, "after": after, "migrated": False}
            if apply:
                audit.migrate_hub_legacy(conn.cursor(), event_id, chat_id, legacy)
                conn.commit()
                entry["migrated"] = True
            found.append(entry)
    finally:
        conn.close()
    return found


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Move the hub's participants of old events from the JSON columns into event_users (dry run unless --apply).")
    parser.add_argument("--chat-id", default=None, help="A single hub instead of every hub.")
    parser.add_argument("--apply", action="store_true", help="Do the move (without it: only report).")
    args = parser.parse_args()
    results = run(DB_PATH, args.chat_id, args.apply)
    for r in results:
        print(f"{'MIGRATED' if r['migrated'] else 'would migrate'}  {r['name']} ({r['event_id']}, hub {r['chat_id']}): "
              f"headcount {r['before']} -> {r['after']}")
    print(f"{len(results)} event(s) with the hub's participants only in the old JSON columns" +
          ("" if args.apply else " - dry run, nothing written; add --apply to move them."))
    if results and args.apply:
        print("Now: python3 scripts/repair_event_users.py --apply   and   python3 scripts/check_events_amount.py")
