"""
Check that the Events tab holds the right AMOUNT for every event - the last column (H; AMOUNT in the code and in most templates,
GOING_COUNT in some - the bot and this script go by its POSITION).

What the bot writes there: at Save & Close, the number of people the event ended with - every person who is going
PLUS every guest, from every chat of the event (db.headcount_from_rows; it is also the "TOTAL Going" the post shows) -
and 0 for an event that is open or canceled. It is a SNAPSHOT taken from `event_users` at that moment; the database
keeps no copy of it, so this script recomputes it from `event_users` and compares.

For every hub with a Sheet, and every event of it (all of them, or the last N days):

    AMOUNT_WRONG        the tab says one number, the database gives another (both are shown, with how the database
                        number is made up)
    AMOUNT_NOT_A_NUMBER the cell is empty or text
    EVENT_NOT_IN_TAB    the event has no row in the Events tab at all (scripts/sync_events_to_sheets.py adds them)
    EVENTS_ROW_DUPLICATE (note) the tab has several rows for one EVENT_ID - the bot only ever updates the first
    AMOUNT_INCLUDES_GUESTS_OF_NOT_GOING (note) the number includes guests whose host is not going (clicked Not Going,
                        only ever added guests, or was kicked). That is how the post counts too, so the numbers agree -
                        but the -limit check counts only the guests of people who ARE going, so the two can differ.

With --apply it writes the right number into column H of the rows that are wrong - only that cell, only those rows.
A DRY RUN otherwise. Never touches the database.

    python3 scripts/check_events_amount.py                        # every hub with a Sheet, every event
    python3 scripts/check_events_amount.py --chat-id -1001234567 --days 90
    python3 scripts/check_events_amount.py --apply
"""
import sys
import argparse
import asyncio
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

sys.path.insert(0, ".")
from db import init_db, DB_PATH  # noqa: E402
from sheets import get_sheet_for_chat, open_spreadsheet  # noqa: E402
import scripts.audit_event_users as audit  # noqa: E402
from scripts.audit_event_users import Finding, PROBLEM, INFO  # noqa: E402

FAST_UPDATES = 30     # the first writes go out back to back; after that one a second (Sheets allows ~60 a minute)
UPDATE_PAUSE = 1.1


@dataclass
class Fix:
    event_id: str
    event_name: str
    row: int
    old: str
    new: int


@dataclass
class Result:
    findings: list = field(default_factory=list)
    fixes: list = field(default_factory=list)
    checked: int = 0
    correct: int = 0


def expected_amount(status: int, roster: dict) -> int:
    """What the tab should hold: the final headcount for a closed event, 0 for one that is open or canceled."""
    return roster["headcount"] if status == 2 else 0


def check_event(event: dict, roster: dict, tab_row) -> tuple:
    """-> ([Finding], expected amount or None when the row is right). `tab_row` is the Events-tab row as a dict, or None."""
    findings = []

    def add(code, severity, detail):
        findings.append(Finding(code, severity, event["event_id"], event["name"], detail))

    expected = expected_amount(event["status"], roster)
    if tab_row is None:
        add("EVENT_NOT_IN_TAB", PROBLEM, f"no row in the Events tab (the amount would be {expected})")
        return findings, None

    raw = str(tab_row.get("AMOUNT", "")).strip()
    try:
        actual = int(raw)
    except ValueError:
        add("AMOUNT_NOT_A_NUMBER", PROBLEM, f"the cell holds {raw!r}, it should be {expected}")
        return findings, expected
    wrong = actual != expected
    if wrong:
        parts = roster["breakdown"]
        how = f"{parts['going']} going + {parts['guests']} guest(s)" if event["status"] == 2 else f"the event is {audit.SHEET_STATUS[event['status']]}"
        add("AMOUNT_WRONG", PROBLEM, f"the tab says {actual}, the database gives {expected} ({how})")
    if event["status"] == 2 and roster["breakdown"]["guests_of_not_going"]:
        add("AMOUNT_INCLUDES_GUESTS_OF_NOT_GOING", INFO,
            f"{roster['breakdown']['guests_of_not_going']} of the guests belong to people who are not going - counted, as the post does")
    return findings, expected if wrong else None


async def check_hub(chat_id, db_path, cutoff, apply=False):
    """-> Result, or None when the hub has no usable Sheet."""
    ss = await open_spreadsheet(await get_sheet_for_chat(chat_id))
    if not ss:
        return None

    rows_by_event, duplicated = {}, set()
    for number, record in await audit.read_events_tab(ss):
        event_id = str(record["EVENT_ID"]).strip()
        if event_id in rows_by_event:
            duplicated.add(event_id)
        else:
            rows_by_event[event_id] = {**record, "_row": number}

    result = Result()
    conn = sqlite3.connect(db_path)
    try:
        db_events = conn.execute(
            "SELECT event_id, name, event_status, created_date, going_data FROM events WHERE chat_id = ? ORDER BY ROWID", (chat_id,)
        ).fetchall()
        for event_id, name, status, created_raw, going_data in db_events:
            created = audit._parse_created(created_raw)
            if cutoff is not None and (created is None or created < cutoff):
                continue
            event = {"event_id": str(event_id), "name": name or str(event_id), "status": status}
            roster = audit.effective_roster(conn, event_id, status, going_data)
            if "breakdown" not in roster:      # an old event with only its frozen list: no breakdown to show
                roster["breakdown"] = {"going": roster["headcount"], "guests": 0, "guests_of_not_going": 0, "total": roster["headcount"]}
            row = rows_by_event.get(event["event_id"])
            findings, fix_to = check_event(event, roster, row)
            result.checked += 1
            result.findings += findings
            if not any(f.severity == PROBLEM for f in findings):
                result.correct += 1
            if event["event_id"] in duplicated:
                result.findings.append(Finding("EVENTS_ROW_DUPLICATE", INFO, event["event_id"], event["name"],
                                               "several rows for this event in the Events tab - the bot only updates the first"))
            if fix_to is not None and row is not None:
                result.fixes.append(Fix(event["event_id"], event["name"], row["_row"], str(row["AMOUNT"]), fix_to))
    finally:
        conn.close()

    if apply and result.fixes:
        ws = await ss.worksheet("Events")
        for written, fix in enumerate(result.fixes, start=1):
            try:
                await ws.update(f"H{fix.row}", [[fix.new]])
                result.findings.append(Finding("AMOUNT_FIXED", INFO, fix.event_id, fix.event_name, f"H{fix.row}: {fix.old!r} -> {fix.new}"))
            except Exception as e:
                result.findings.append(Finding("AMOUNT_FIX_FAILED", PROBLEM, fix.event_id, fix.event_name, f"H{fix.row}: {e}"))
            if written > FAST_UPDATES:
                await asyncio.sleep(UPDATE_PAUSE)
    return result


async def _run(target_chat_id=None, days=None, db_path=DB_PATH, apply=False):
    init_db(db_path=db_path)
    cutoff = None if days is None else datetime.now() - timedelta(days=days)
    conn = sqlite3.connect(db_path)
    if target_chat_id:
        chat_ids = [target_chat_id]
    else:
        chat_ids = [r[0] for r in conn.execute("SELECT chat_id FROM all_groups WHERE sheet_id IS NOT NULL AND sheet_id != ''")]
    conn.close()
    results = {}
    for chat_id in chat_ids:
        try:
            results[chat_id] = await check_hub(chat_id, db_path, cutoff, apply=apply)
        except Exception as e:   # one hub's sheet failing (no access, deleted...) must not stop the others
            results[chat_id] = audit.describe_hub_error(db_path, chat_id, e)
    return results


def _print_report(results, apply) -> int:
    """Prints the report; returns the number of problems (wrong amounts still standing, unreadable hubs, failed writes)."""
    problems = 0
    for chat_id, result in results.items():
        if result is None:
            print(f"\nHub {chat_id}: no usable Google Sheet - skipped.")
            continue
        if isinstance(result, str):
            problems += 1
            print(f"\nHub {chat_id}: COULD NOT BE CHECKED - {result}")
            continue
        fixed = {f.event_id for f in result.findings if f.code == "AMOUNT_FIXED"}
        standing = [f for f in result.findings if f.severity == PROBLEM and not (f.code == "AMOUNT_WRONG" and f.event_id in fixed)
                    and not (f.code == "AMOUNT_NOT_A_NUMBER" and f.event_id in fixed)]
        problems += len(standing)
        print(f"\nHub {chat_id}: {result.checked} event(s) checked, {result.correct} with the right amount - "
              f"{len([f for f in result.findings if f.severity == PROBLEM])} problem(s), "
              f"{len([f for f in result.findings if f.severity == INFO and f.code != 'AMOUNT_FIXED'])} note(s)")
        for f in sorted(result.findings, key=lambda f: (f.severity != PROBLEM, f.event_id, f.code)):
            print(f"  [{f.severity}] {f.event_name} ({f.event_id}) {f.code}: {f.detail}")
    if not apply:
        fixable = sum(len(r.fixes) for r in results.values() if isinstance(r, Result))
        print("\nAll amounts are right." if not problems and not fixable else
              f"\n{fixable} amount(s) can be corrected - nothing was changed. Run again with --apply to write them." if fixable else
              f"\n{problems} problem(s) found.")
    else:
        print("\nDone." if not problems else f"\nDone, but {problems} problem(s) remain.")
    return problems


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Check the amount column of the Events tab against the database (dry run unless --apply).")
    parser.add_argument("--chat-id", default=None, help="Check a single hub instead of every hub that has a Sheet.")
    parser.add_argument("--days", type=int, default=None, help="Only events created in the last N days (default: every event).")
    parser.add_argument("--apply", action="store_true", help="Write the right amount into the cells that are wrong (without it: only report).")
    args = parser.parse_args()
    outcome = asyncio.run(_run(args.chat_id, args.days, apply=args.apply))
    sys.exit(1 if _print_report(outcome, args.apply) else 0)
