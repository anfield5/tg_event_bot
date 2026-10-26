"""
One-time fix: every row of the EventUsers tab should carry a real numeric Telegram USER_ID - and where it holds a
USERNAME instead, put the id there.

How a username gets there: Add Extra Member, for someone the bot has never seen (not in the hub, not in a shared or a
monitored chat), stores the typed name as the person's "id" - there is no real one to store. Later the bot may see
them (they join, press a button) and so learn the id, but the old rows keep the name.

For every hub that has a Sheet this script
  1. reads the EventUsers tab and finds the rows whose USER_ID is not a number;
  2. looks each name up - case-insensitively, without the "@" - in the hub's `Users` tab (USER_NAME and also
     ARCHIVED_USER_NAME, the earlier usernames of someone who renamed) and in the database's tracked users of the hub's
     chats (the hub, the chats its events were shared to, the chats it monitors);
  3. replaces the name with the id - in the EventUsers cell AND in the database's event_users row, because the tab is
     written FROM the database: fixing only the tab would be undone by the next re-save and the audit would flag the
     difference. The database rows of events that are still open are fixed too, so their next Save & Close exports ids.

What it leaves alone (and lists, so you can decide):
  * a name nobody is known by            - the bot has no id for that person anywhere; nothing can be done until it sees them
  * a name that fits SEVERAL ids         - e.g. a username that changed hands; never guessed
  * a name whose id is ALREADY in that event - the same person is listed twice (once typed in, once for real); merging
                                           would change the headcount, so it is yours to decide
  * rows with an empty USER_ID

Take care with old events: a Telegram username can be released and taken by somebody else. The plan names every
replacement, so read it before --apply. A DRY RUN by default; --apply changes the Sheet and the database (copy the
database first). Running it again changes nothing more.

    python3 scripts/fix_eventusers_ids.py                       # show the plan for every hub with a Sheet
    python3 scripts/fix_eventusers_ids.py --chat-id -1001234567
    python3 scripts/fix_eventusers_ids.py --apply
"""
import sys
import argparse
import asyncio
import sqlite3
from dataclasses import dataclass, field

sys.path.insert(0, ".")
from db import init_db, DB_PATH  # noqa: E402
from sheets import get_sheet_for_chat, open_spreadsheet  # noqa: E402
import scripts.audit_event_users as audit  # noqa: E402

SHEET_CELL, DB_ROW = "EVENTUSERS_CELL", "EVENT_USERS_ROW"
FAST_UPDATES = 30     # the first sheet writes go out back to back; after that one a second (Sheets allows ~60 a minute)
UPDATE_PAUSE = 1.1


@dataclass
class Fix:
    kind: str
    event_id: str
    event_name: str
    old: str
    new: str
    source: str
    row: int = 0          # the sheet row, for SHEET_CELL


@dataclass
class Plan:
    fixes: list = field(default_factory=list)
    unknown: list = field(default_factory=list)      # [(event_id, event_name, name)]
    ambiguous: list = field(default_factory=list)    # [(event_id, event_name, name, [ids])]
    conflicts: list = field(default_factory=list)    # [(event_id, event_name, name, id)]
    empty: list = field(default_factory=list)        # [(event_id, sheet row)]
    sheet_rows: int = 0
    sheet_placeholders: int = 0


def norm(name) -> str:
    return str(name or "").strip().lstrip("@").strip().casefold()


def username_index(users_records, tracked_pairs) -> dict:
    """{normalized name: {user_id: where it came from}} from the Users tab (current and earlier usernames) and from the
    database's tracked users. Only real numeric ids go in."""
    index = {}

    def add(name, user_id, source):
        key, uid = norm(name), str(user_id).strip()
        if key and audit._is_real_id(uid):
            index.setdefault(key, {}).setdefault(uid, source)

    for record in users_records:
        uid = record.get("USER_ID", "")
        add(record.get("USER_NAME", ""), uid, "Users tab")
        for old in str(record.get("ARCHIVED_USER_NAME", "") or "").replace(";", ",").split(","):
            add(old, uid, "Users tab (earlier username)")
    for username, user_id in tracked_pairs:
        add(username, user_id, "database")
    return index


def resolve(index: dict, name: str):
    """-> ("ok", id, source) | ("ambiguous", [ids], None) | ("unknown", None, None)"""
    candidates = index.get(norm(name), {})
    if len(candidates) == 1:
        uid, source = next(iter(candidates.items()))
        return "ok", uid, source
    if candidates:
        return "ambiguous", sorted(candidates), None
    return "unknown", None, None


def plan_hub(sheet_rows: list, db_rows: list, event_names: dict, index: dict) -> Plan:
    """
    sheet_rows: [(sheet row number, event_id, USER_ID)] from the EventUsers tab.
    db_rows:    [(event_id, user_id)] of the hub's event_users rows.
    -> the Plan: what to replace, and what cannot be.
    """
    plan = Plan(sheet_rows=len(sheet_rows))
    name_of = lambda event_id: event_names.get(event_id, event_id)
    taken = {(e, str(u)) for _, e, u in sheet_rows if audit._is_real_id(u)} | {(e, str(u)) for e, u in db_rows if audit._is_real_id(u)}

    pairs = {}     # (event_id, name) -> {"sheet": [rows], "db": bool}, in the order first met
    for row, event_id, user_id in sheet_rows:
        if not str(user_id).strip():
            plan.empty.append((event_id, row))
        elif not audit._is_real_id(user_id):
            plan.sheet_placeholders += 1
            pairs.setdefault((event_id, str(user_id).strip()), {"sheet": [], "db": False})["sheet"].append(row)
    for event_id, user_id in db_rows:
        if str(user_id).strip() and not audit._is_real_id(user_id):
            pairs.setdefault((event_id, str(user_id).strip()), {"sheet": [], "db": False})["db"] = True

    for (event_id, name), where in pairs.items():
        verdict, found, source = resolve(index, name)
        if verdict == "unknown":
            plan.unknown.append((event_id, name_of(event_id), name))
        elif verdict == "ambiguous":
            plan.ambiguous.append((event_id, name_of(event_id), name, found))
        elif (event_id, found) in taken:
            plan.conflicts.append((event_id, name_of(event_id), name, found))
        else:
            if where["db"]:
                plan.fixes.append(Fix(DB_ROW, event_id, name_of(event_id), name, found, source))
            for row in where["sheet"]:
                plan.fixes.append(Fix(SHEET_CELL, event_id, name_of(event_id), name, found, source, row))
    return plan


async def fix_hub(chat_id, db_path, apply=False) -> tuple:
    """-> (Plan, [(Fix, note)] when applied, placeholders left in the tab after applying) - or (None, None, None) with no Sheet."""
    from gspread.exceptions import WorksheetNotFound

    ss = await open_spreadsheet(await get_sheet_for_chat(chat_id))
    if not ss:
        return None, None, None
    try:
        eu_ws = await ss.worksheet("EventUsers")
    except WorksheetNotFound:
        raise RuntimeError("the sheet has no EventUsers tab")
    try:
        users_records = await (await ss.worksheet("Users")).get_all_records()
    except WorksheetNotFound:
        users_records = []

    eu_values = await eu_ws.get_all_values()
    sheet_rows = [(n, str(r[0]).strip(), str(r[1]).strip() if len(r) > 1 else "")
                  for n, r in enumerate(eu_values[1:], start=2) if r and str(r[0]).strip()]

    conn = sqlite3.connect(db_path)
    try:
        event_names = {str(e): (n or str(e)) for e, n in conn.execute("SELECT event_id, name FROM events WHERE chat_id = ?", (chat_id,))}
        scope = {str(chat_id)}
        scope |= {str(r[0]) for r in conn.execute(
            "SELECT DISTINCT s.chat_id FROM event_shares s JOIN events e ON e.event_id = s.event_id WHERE e.chat_id = ?", (chat_id,))}
        scope |= {str(r[0]) for r in conn.execute("SELECT chat_id FROM sub_chats WHERE owner_chat_id = ? AND is_monitored = 1", (chat_id,))}
        marks = ",".join("?" * len(scope))
        tracked = conn.execute(f"SELECT username, user_id FROM main_group_users WHERE chat_id IN ({marks})", tuple(scope)).fetchall()
        db_rows = conn.execute(
            "SELECT u.event_id, u.user_id FROM event_users u JOIN events e ON e.event_id = u.event_id WHERE e.chat_id = ?", (chat_id,)).fetchall()
        plan = plan_hub(sheet_rows, [(str(e), str(u)) for e, u in db_rows], event_names, username_index(users_records, tracked))

        results, left = [], None
        if apply:
            written = 0
            for fix in (f for f in plan.fixes if f.kind == DB_ROW):
                try:
                    conn.execute("UPDATE event_users SET user_id = ? WHERE event_id = ? AND user_id = ?", (fix.new, fix.event_id, fix.old))
                    conn.commit()
                    results.append((fix, "database updated"))
                except Exception as e:
                    results.append((fix, f"FAILED: {e}"))
            for fix in (f for f in plan.fixes if f.kind == SHEET_CELL):
                try:
                    await eu_ws.update(f"B{fix.row}", [[fix.new]])
                    results.append((fix, "cell updated"))
                    written += 1
                    if written > FAST_UPDATES:
                        await asyncio.sleep(UPDATE_PAUSE)
                except Exception as e:
                    results.append((fix, f"FAILED: {e}"))
            after = await eu_ws.get_all_values()
            left = sum(1 for r in after[1:] if len(r) > 1 and str(r[0]).strip() and str(r[1]).strip() and not audit._is_real_id(r[1]))
    finally:
        conn.close()
    return plan, results, left


async def _run(target_chat_id=None, db_path=DB_PATH, apply=False):
    init_db(db_path=db_path)
    conn = sqlite3.connect(db_path)
    if target_chat_id:
        chat_ids = [target_chat_id]
    else:
        chat_ids = [r[0] for r in conn.execute("SELECT chat_id FROM all_groups WHERE sheet_id IS NOT NULL AND sheet_id != ''")]
    conn.close()
    results = {}
    for chat_id in chat_ids:
        try:
            results[chat_id] = await fix_hub(chat_id, db_path, apply=apply)
        except Exception as e:   # one hub's sheet failing (no access, deleted...) must not stop the others
            results[chat_id] = (None, audit.describe_hub_error(db_path, chat_id, e), None)
    return results


def _print_report(results, apply) -> int:
    """Prints the plan / outcome; returns how many things went wrong (hubs that could not be read, failed writes)."""
    wrong = 0
    for chat_id, (plan, done, left) in results.items():
        if plan is None:
            if isinstance(done, str):
                wrong += 1
                print(f"\nHub {chat_id}: COULD NOT BE CHECKED - {done}")
            else:
                print(f"\nHub {chat_id}: no usable Google Sheet - skipped.")
            continue
        cells = [f for f in plan.fixes if f.kind == SHEET_CELL]
        print(f"\nHub {chat_id}: EventUsers has {plan.sheet_rows} row(s), {plan.sheet_placeholders} with a username instead of an id - "
              f"{len(cells)} can be fixed, {len(plan.unknown)} unknown, {len(plan.ambiguous)} ambiguous, {len(plan.conflicts)} already listed with their id")
        outcome = {id(f): note for f, note in (done or [])}
        for f in plan.fixes:
            where = "EventUsers tab" if f.kind == SHEET_CELL else "database event_users"
            note = f"  -> {outcome[id(f)]}" if id(f) in outcome else ""
            print(f"  {where}: {f.event_name} ({f.event_id}): {f.old} -> {f.new}   [{f.source}]{note}")
        for event_id, event_name, name in plan.unknown:
            print(f"  left as is (the bot has no id for this person): {event_name} ({event_id}): {name}")
        for event_id, event_name, name, ids in plan.ambiguous:
            print(f"  left as is (the name fits several ids {audit._shown(ids)}): {event_name} ({event_id}): {name}")
        for event_id, event_name, name, uid in plan.conflicts:
            print(f"  left as is (id {uid} is already in this event - the same person twice?): {event_name} ({event_id}): {name}")
        for event_id, row in plan.empty:
            print(f"  left as is (empty USER_ID): event {event_id}, sheet row {row}")
        failed = [n for _, n in (done or []) if n.startswith("FAILED")]
        wrong += len(failed)
        if apply:
            print(f"  after the fix: {left} row(s) still hold a username" + (f", {len(failed)} write(s) failed" if failed else ""))
    fixable = sum(len(p.fixes) for p, _, _ in results.values() if p)
    if not apply:
        print("\nNothing to fix." if not fixable else f"\n{fixable} change(s) planned - nothing was changed. Run again with --apply "
              "(copy database.db first).")
    else:
        print("\nDone." if not wrong else f"\nDone, but {wrong} thing(s) failed.")
    return wrong


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Replace usernames in the EventUsers tab (and event_users) with real user ids (dry run unless --apply).")
    parser.add_argument("--chat-id", default=None, help="Fix a single hub instead of every hub that has a Sheet.")
    parser.add_argument("--apply", action="store_true", help="Actually change the Sheet and the database (without it: only print the plan).")
    args = parser.parse_args()
    outcome = asyncio.run(_run(args.chat_id, apply=args.apply))
    sys.exit(1 if _print_report(outcome, args.apply) else 0)
