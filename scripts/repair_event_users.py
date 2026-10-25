"""
One-time repair: make the Google Sheet say what the DATABASE says about who attended each event.

Run scripts/audit_event_users.py first - it shows what is wrong; this script fixes it. It uses the same checks.

Why the database is the side to trust: the bot writes every click, kick and Add Extra Member to `event_users` FIRST,
and the post in the group is rendered FROM it a moment later. So a person who is in the post is in `event_users`
(unless the edit of the post failed, in which case the DB is simply ahead of it); the Sheet is the side that gets
written last, in one go at Save & Close, and so the one that can be missing things - a failed write, a quota error,
a reopen whose cleanup never happened, an event closed before a fix. Nothing here reads the posts.

For every hub that has a Sheet, and every event created in the last N days:

  closed event, EventUsers differs from the DB   -> the event's EventUsers rows are REPLACED: its old rows removed, one
                                                    row per going person written (the same call Save & Close makes)
  closed event, Events row missing / wrong       -> the row is added, or its CLOSED_AT / STATUS / GOING_COUNT corrected
  open / canceled event with EventUsers rows     -> those rows are removed (an unfinished event has no final roster)
  open / canceled event, Events row wrong        -> its STATUS corrected
  --resolve-ids                                  -> people typed in by name (their "id" is their username) whom the bot
                                                    has SINCE seen with a real Telegram id get that id in event_users
                                                    first, so the EventUsers rows carry it too. Opt-in: it edits the DB.

It never touches an event outside the window, another event's rows, the Actions tab, or the posts. A DRY RUN by
default - it only prints the plan; add --apply to do it. After applying it audits the hub again and says what is left.

    python3 scripts/repair_event_users.py                              # show the plan, last 60 days, every hub with a Sheet
    python3 scripts/repair_event_users.py --chat-id -1001234567 --days 14
    python3 scripts/repair_event_users.py --all-time --apply
    python3 scripts/repair_event_users.py --apply --resolve-ids
"""
import sys
import argparse
import asyncio
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

sys.path.insert(0, ".")
from db import init_db, DB_PATH  # noqa: E402
from sheets import get_sheet_for_chat, open_spreadsheet, sync_event_users_sheet  # noqa: E402
import scripts.audit_event_users as audit  # noqa: E402

REWRITE, REMOVE, ADD_ROW, FIX_ROW, RESOLVE = "REWRITE_EVENTUSERS", "REMOVE_STALE_ROWS", "ADD_EVENTS_ROW", "FIX_EVENTS_ROW", "RESOLVE_ID"
ROSTER_PROBLEMS = {"EVENTUSERS_MISSING", "ID_NOT_IN_SHEET", "ID_NOT_IN_DB", "SHEET_DUPLICATE"}


@dataclass
class Step:
    kind: str
    event_id: str
    event_name: str
    detail: str
    data: dict = field(default_factory=dict)


def events_row_values(info: dict, headcount: int) -> list:
    """CLOSED_AT, STATUS, GOING_COUNT exactly as the bot writes them for an event in this state."""
    status = info["status"]
    if status == 2:
        return [info["closed_date"] or "", "CLOSED", headcount]
    if status == -1:
        return [info["closed_date"] or "", "CANCELED", 0]
    return ["", "OPEN", 0]


def resolved_roster(roster: dict, mapping: dict) -> dict:
    """The roster as it will be once the typed-in names in `mapping` ({placeholder: real id}) have their real ids."""
    ids = list(dict.fromkeys(mapping.get(u, u) for u in roster["ids"]))
    return {**roster, "ids": ids, "placeholders": [u for u in ids if not audit._is_real_id(u)]}


def plan_event(info: dict, roster: dict, sheet_ids: list, events_row, resolvable: dict, resolve_ids: bool, sheet_tab_ok: bool = True) -> list:
    """The steps that bring one event's Sheet rows in line with the DB (see the module docstring)."""
    steps = []

    def add(kind, detail, **data):
        steps.append(Step(kind, info["event_id"], info["name"], detail, data))

    mapping = {}
    if resolve_ids and info["status"] == 2:
        mapping = {u: resolvable[u.casefold()] for u in roster["placeholders"] if u.casefold() in resolvable}
        for placeholder, real in mapping.items():
            add(RESOLVE, f"{placeholder} -> {real}", placeholder=placeholder, real=real)
        roster = resolved_roster(roster, mapping)

    codes = {f.code for f in audit.compare_event(info, roster, sheet_ids, events_row, resolvable)}
    if sheet_tab_ok:
        if info["status"] == 2 and codes & ROSTER_PROBLEMS:
            add(REWRITE, f"replace the event's EventUsers rows with the {len(roster['ids'])} going person(s) in the DB")
        elif info["status"] != 2 and "ROWS_FOR_UNFINISHED_EVENT" in codes:
            add(REMOVE, "remove the EventUsers rows of an event that is not closed")
    if "EVENTS_ROW_MISSING" in codes:
        add(ADD_ROW, f"add the event's row to the Events tab ({events_row_values(info, roster['headcount'])[1]})")
    elif codes & {"EVENTS_STATUS", "EVENTS_COUNT"}:
        values = events_row_values(info, roster["headcount"])
        add(FIX_ROW, f"set the Events row to CLOSED_AT={values[0] or 'empty'}, STATUS={values[1]}, GOING_COUNT={values[2]}")
    return steps


def _event_info(row) -> dict:
    keys = ("event_id", "name", "status", "created_date", "created_by", "event_date", "closed_date", "going_data")
    info = dict(zip(keys, row))
    info["event_id"] = str(info["event_id"])
    info["name"] = info["name"] or info["event_id"]
    return info


async def _events_row_index(ss, event_id):
    ws = await ss.worksheet("Events")
    for idx, record in enumerate(await ws.get_all_records(), start=2):
        if str(record.get("EVENT_ID")) == str(event_id):
            return ws, idx
    return ws, None


async def apply_step(step: Step, chat_id: str, ss, conn, info: dict) -> str:
    """Does one step; returns a short note. Raises on failure (the caller reports it and carries on)."""
    if step.kind == RESOLVE:
        taken = conn.execute("SELECT 1 FROM event_users WHERE event_id = ? AND user_id = ?", (step.event_id, step.data["real"])).fetchone()
        if taken:
            return "skipped - that person already has a row in this event"
        conn.execute("UPDATE event_users SET user_id = ? WHERE event_id = ? AND user_id = ?", (step.data["real"], step.event_id, step.data["placeholder"]))
        conn.commit()
        return "event_users updated"

    roster = audit.effective_roster(conn, step.event_id, info["status"], info["going_data"])
    if step.kind == REWRITE:
        await sync_event_users_sheet(chat_id, step.event_id, roster["ids"])
        return f"{len(roster['ids'])} row(s) written"
    if step.kind == REMOVE:
        await sync_event_users_sheet(chat_id, step.event_id, [])
        return "rows removed"

    values = events_row_values(info, roster["headcount"])
    ws, idx = await _events_row_index(ss, step.event_id)
    if step.kind == ADD_ROW:
        await ws.append_row([step.event_id, info["name"], info["created_date"] or "", info["created_by"] or "",
                             info["event_date"] or "", *values])
        return "row added"
    if idx is None:
        return "skipped - the row is gone"
    await ws.update(f"F{idx}:H{idx}", [values])
    return "row updated"


async def repair_hub(chat_id, db_path, cutoff, apply=False, resolve_ids=False) -> tuple:
    """-> (steps planned, [(step, note or error)] when applied, problems left after) - or (None, None, None) with no Sheet."""
    ss = await open_spreadsheet(await get_sheet_for_chat(chat_id))
    if not ss:
        return None, None, None

    sheet_ids_by_event, events_rows = await audit.read_sheet_state(ss)
    tab_ok = sheet_ids_by_event is not None
    sheet_ids_by_event = sheet_ids_by_event or {}

    conn = sqlite3.connect(db_path)
    plan, infos, results = [], {}, []
    try:
        resolvable = audit.known_ids_by_username(conn)
        rows = conn.execute(
            "SELECT event_id, name, event_status, created_date, created_by_user_id, event_date, closed_date, going_data "
            "FROM events WHERE chat_id = ? ORDER BY ROWID", (chat_id,)).fetchall()
        for row in rows:
            info = _event_info(row)
            created = audit._parse_created(row[3])
            if cutoff is not None and (created is None or created < cutoff):
                continue
            roster = audit.effective_roster(conn, info["event_id"], info["status"], info["going_data"])
            steps = plan_event(info, roster, sheet_ids_by_event.get(info["event_id"], []), events_rows.get(info["event_id"]),
                               resolvable, resolve_ids, tab_ok)
            infos[info["event_id"]] = info
            plan += steps

        if apply:
            for step in plan:
                try:
                    results.append((step, await apply_step(step, chat_id, ss, conn, infos[step.event_id])))
                except Exception as e:
                    results.append((step, f"FAILED: {e}"))
    finally:
        conn.close()

    left = None
    if apply:
        findings, _ = await audit.audit_hub(chat_id, db_path, cutoff)
        left = sum(1 for f in (findings or []) if f.severity == audit.PROBLEM)
    return plan, results, left


async def _run(target_chat_id=None, days=60, db_path=DB_PATH, apply=False, resolve_ids=False):
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
            results[chat_id] = await repair_hub(chat_id, db_path, cutoff, apply=apply, resolve_ids=resolve_ids)
        except Exception as e:   # one hub's sheet failing (no access, deleted...) must not stop the others
            results[chat_id] = (None, audit.describe_hub_error(db_path, chat_id, e), None)
    return results


def _print_report(results, apply) -> int:
    """Prints the plan / outcome; returns how many things went wrong (failed steps + problems left after applying)."""
    wrong = 0
    for chat_id, (plan, done, left) in results.items():
        if plan is None:
            if isinstance(done, str):
                wrong += 1
                print(f"\nHub {chat_id}: COULD NOT BE REPAIRED - {done}")
            else:
                print(f"\nHub {chat_id}: no usable Google Sheet - skipped.")
            continue
        print(f"\nHub {chat_id}: {len(plan)} step(s) {'applied' if apply else 'planned'}")
        outcomes = {id(step): note for step, note in (done or [])}
        for step in plan:
            note = f"  -> {outcomes[id(step)]}" if id(step) in outcomes else ""
            print(f"  {step.kind}: {step.event_name} ({step.event_id}) - {step.detail}{note}")
        failed = [n for _, n in (done or []) if n.startswith("FAILED")]
        wrong += len(failed)
        if apply:
            wrong += left or 0
            print(f"  after the repair: {left} problem(s) left by the audit" + (f", {len(failed)} step(s) failed" if failed else ""))
    total = sum(len(p) for p, _, _ in results.values() if p)
    if not apply:
        print("\nNothing to repair." if not total else f"\n{total} step(s) planned - nothing was changed. Run again with --apply to do them.")
    else:
        print("\nDone." if not wrong else f"\nDone, but {wrong} thing(s) still need a look.")
    return wrong


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Make the Google Sheet say what the database says about who attended each event (dry run unless --apply).")
    parser.add_argument("--chat-id", default=None, help="Repair a single hub instead of every hub that has a Sheet.")
    parser.add_argument("--days", type=int, default=60, help="Only events created in the last N days (default 60).")
    parser.add_argument("--all-time", action="store_true", help="Every event, ignoring --days.")
    parser.add_argument("--apply", action="store_true", help="Actually change the Sheet (without it: only print the plan).")
    parser.add_argument("--resolve-ids", action="store_true", help="Also give typed-in people their real Telegram id in event_users when the bot has seen them since (edits the DB).")
    args = parser.parse_args()
    outcome = asyncio.run(_run(args.chat_id, None if args.all_time else args.days, apply=args.apply, resolve_ids=args.resolve_ids))
    sys.exit(1 if _print_report(outcome, args.apply) else 0)
