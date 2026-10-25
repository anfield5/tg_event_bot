"""
Read-only audit: do the places that record WHO came to an event agree with each other?

  1. SQLite `event_users`      - the bot's own records: per event and chat, user_id, names, status, guests.
                                 Everything else is built from this.
  2. Google Sheet `EventUsers` - what Save & Close exported: EVENT_ID, USER_ID (ids only, no names).
     Google Sheet `Events`     - STATUS and GOING_COUNT of each event.
  3. The event's post in the group (only with --telegram) - what people actually SEE.

For every hub that has a Sheet, and every event created in the last N days, it reports (problems exit with code 1):

  DB <-> Sheet (always)
    EVENTUSERS_MISSING / ID_NOT_IN_SHEET / ID_NOT_IN_DB / SHEET_DUPLICATE
                         the closed event's roster in the DB differs from its EventUsers rows
    ROWS_FOR_UNFINISHED_EVENT
                         EventUsers rows exist for an event that is open / canceled again (a reopen whose sheet
                         cleanup didn't happen)
    EVENTS_ROW_MISSING / EVENTS_STATUS / EVENTS_COUNT
                         the Events tab disagrees with the DB (status, or GOING_COUNT vs going + guests)
    DB_SAME_PERSON_TWICE one person going in two chats of the same event
    SHEET_ORPHAN_EVENT   (info) EventUsers rows of an event the DB doesn't know
    EVENTUSERS_TAB_MISSING

  What a Telegram post can and cannot prove (only with --telegram --to <chat id>)
    The Bot API has NO way to read an old message. The one trick is to FORWARD it to a chat and read the forwarded
    copy - this script does that into the chat you give with --to (use your own DM with the bot) and deletes the copy
    straight away. It fails for groups that forbid forwarding, and it is slow-ish on purpose (throttled).
    A post is only an OUTPUT of the database (the bot renders it from event_users), so comparing them finds stale or
    failed edits, not a wrong roster. And a post carries a person's id only when the name is a clickable mention
    (-clc on, id known); with -clc off, or for someone typed in by name, the post shows a NAME and nothing else.
    POST_TOTAL / POST_MISSING_PERSON / POST_EXTRA_PERSON / POST_UNREADABLE, plus POST_NO_IDS (info) when the post
    has no mention links at all, so only names could be compared.

  NO_REAL_ID (info)
    People typed in with Add Extra Member whom the bot has never seen: the post shows their name, but their real
    Telegram id is stored NOWHERE - the DB and the EventUsers tab hold the USERNAME in the id column. The script lists
    them (and says when the same username is now known with a real id).

Never writes anything. To FIX what it finds, use scripts/repair_event_users.py (a dry run until you add --apply).
Run from the project root, against your real database.db:
    python3 scripts/audit_event_users.py                         # every hub with a Sheet, last 60 days
    python3 scripts/audit_event_users.py --days 14 --chat-id -1001234567
    python3 scripts/audit_event_users.py --all-time
    python3 scripts/audit_event_users.py --telegram --to 123456789 --limit 10
    python3 scripts/audit_event_users.py --json > audit.json
"""
import sys
import argparse
import asyncio
import json
import re
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta

sys.path.insert(0, ".")
from db import init_db, DB_PATH  # noqa: E402
from sheets import get_sheet_for_chat, open_spreadsheet  # noqa: E402

DATE_FMT = "%d.%m.%Y %H:%M:%S.%f"
SHEET_STATUS = {-1: "CANCELED", 0: "OPEN", 1: "OPEN", 2: "CLOSED"}   # what the Events tab holds (verification isn't written)
PROBLEM, INFO = "PROBLEM", "info"
SHOW = 8                     # how many ids/names a finding lists before saying "+N more"
FORWARD_PAUSE = 0.4          # seconds between forwards - be gentle with Telegram


@dataclass
class Finding:
    code: str
    severity: str
    event_id: str
    event_name: str
    detail: str


def _is_real_id(uid) -> bool:
    return bool(re.fullmatch(r"-?\d+", str(uid)))


def _shown(items, limit=SHOW) -> str:
    items = list(items)
    text = ", ".join(str(i) for i in items[:limit])
    return text + (f" ... (+{len(items) - limit} more)" if len(items) > limit else "")


def _parse_created(raw):
    try:
        return datetime.strptime(raw, DATE_FMT) if raw else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# The database side
# ---------------------------------------------------------------------------

def db_roster(conn, event_id) -> dict:
    """What the DB says about one event: who is going (once each), the headcount Save & Close would export, odd cases."""
    rows = conn.execute(
        "SELECT chat_id, user_id, username, first_name, last_name, status, guests FROM event_users WHERE event_id = ?",
        (event_id,),
    ).fetchall()
    going_rows = [r for r in rows if r[5] == "going" and r[1]]
    ids = list(dict.fromkeys(str(r[1]) for r in going_rows))
    chats_of = {}
    for r in going_rows:
        chats_of.setdefault(str(r[1]), set()).add(str(r[0]))
    return {
        "rows": rows,
        "going_rows": going_rows,
        "ids": ids,
        # the same formula Save & Close uses for GOING_COUNT: every going person plus every guest, from any row
        "headcount": sum((1 if r[5] == "going" else 0) + (r[6] or 0) for r in rows),
        "names": {str(r[1]): (r[2] or r[3] or str(r[1])) for r in going_rows},
        "placeholders": [uid for uid in ids if not _is_real_id(uid)],
        "in_two_chats": sorted(uid for uid, chats in chats_of.items() if len(chats) > 1),
    }


def legacy_roster(going_data_raw) -> dict:
    """For events that never got event_users rows (closed before that table existed): the frozen going_data JSON."""
    try:
        entries = json.loads(going_data_raw or "[]")
    except (TypeError, ValueError):
        entries = []
    ids, name_only = [], []
    for entry in entries:
        m = re.search(r"\((-?\d+)\)\s*$", str(entry))
        if m:
            ids.append(m.group(1))
        else:
            name_only.append(str(entry))
    return {"ids": list(dict.fromkeys(ids)), "name_only": name_only}


def effective_roster(conn, event_id, status, going_data_raw) -> dict:
    """db_roster, except that a CLOSED event with no event_users rows at all (closed before that table existed) falls
    back on its frozen going_data list."""
    roster = db_roster(conn, event_id)
    if not roster["rows"] and status == 2:
        legacy = legacy_roster(going_data_raw)
        roster["ids"], roster["names"] = legacy["ids"], {}
        roster["headcount"] = len(legacy["ids"]) + len(legacy["name_only"])
    return roster


def known_ids_by_username(conn) -> dict:
    """{username (casefolded): a real numeric id} from everything the bot has tracked - to tell when a typed-in name is
    nowadays known with a real id."""
    found = {}
    for username, user_id in conn.execute("SELECT username, user_id FROM main_group_users WHERE username IS NOT NULL"):
        if username and _is_real_id(user_id):
            found.setdefault(str(username).casefold(), str(user_id))
    return found


# ---------------------------------------------------------------------------
# DB <-> Sheet
# ---------------------------------------------------------------------------

def compare_event(event: dict, roster: dict, sheet_ids: list, events_row, resolvable: dict = None) -> list:
    """
    event: {"event_id", "name", "status"}. sheet_ids: every USER_ID the EventUsers tab holds for this event (with
    repeats). events_row: this event's row of the Events tab as a dict, or None. -> [Finding]
    """
    resolvable = resolvable or {}
    findings = []
    status = event["status"]

    def add(code, severity, detail):
        findings.append(Finding(code, severity, event["event_id"], event["name"], detail))

    def label(uid):
        name = roster["names"].get(uid)
        return f"{uid} ({name})" if name and name != uid else uid

    in_sheet = set(sheet_ids)
    duplicated = sorted(uid for uid in in_sheet if sheet_ids.count(uid) > 1)
    if duplicated:
        add("SHEET_DUPLICATE", PROBLEM, f"listed more than once in EventUsers: {_shown(duplicated)}")

    if status == 2:
        expected = roster["ids"]
        if expected and not sheet_ids:
            add("EVENTUSERS_MISSING", PROBLEM, f"{len(expected)} going person(s) in the DB, no EventUsers rows at all")
        else:
            missing = [uid for uid in expected if uid not in in_sheet]
            extra = sorted(in_sheet - set(expected))
            if missing:
                add("ID_NOT_IN_SHEET", PROBLEM, f"in the DB as going, missing from EventUsers: {_shown(label(u) for u in missing)}")
            if extra:
                add("ID_NOT_IN_DB", PROBLEM, f"in EventUsers, not going in the DB: {_shown(extra)}")
        if roster["in_two_chats"]:
            add("DB_SAME_PERSON_TWICE", PROBLEM, f"going in more than one chat of this event: {_shown(label(u) for u in roster['in_two_chats'])}")
        if roster["placeholders"]:
            nowadays = [f"{u} -> {resolvable[u.casefold()]}" for u in roster["placeholders"] if u.casefold() in resolvable]
            detail = (f"{len(roster['placeholders'])} person(s) typed in by name - the post shows the name, but their real "
                      f"Telegram id is stored nowhere (the id column holds the username): {_shown(roster['placeholders'])}")
            if nowadays:
                detail += f". Known with a real id today: {_shown(nowadays)}"
            add("NO_REAL_ID", INFO, detail)
    elif sheet_ids:
        add("ROWS_FOR_UNFINISHED_EVENT", PROBLEM,
            f"{len(in_sheet)} EventUsers row(s) although the event is {SHEET_STATUS[status]} - an unfinished event has no final roster")

    if events_row is None:
        add("EVENTS_ROW_MISSING", PROBLEM, "no row for this event in the Events tab")
    else:
        sheet_status = str(events_row.get("STATUS", "")).strip().upper()
        if sheet_status != SHEET_STATUS[status]:
            add("EVENTS_STATUS", PROBLEM, f"Events tab says {sheet_status or 'empty'}, the DB says {SHEET_STATUS[status]}")
        if status == 2:
            try:
                count = int(str(events_row.get("GOING_COUNT", "")).strip())
            except ValueError:
                add("EVENTS_COUNT", PROBLEM, f"GOING_COUNT is {events_row.get('GOING_COUNT')!r}, not a number")
            else:
                if count != roster["headcount"]:
                    add("EVENTS_COUNT", PROBLEM, f"GOING_COUNT is {count}, the DB has {roster['headcount']} (going + guests)")
    return findings


# ---------------------------------------------------------------------------
# Telegram posts
# ---------------------------------------------------------------------------

def _u16(text: str) -> int:
    """Length in UTF-16 code units - the unit Telegram's entity offsets are counted in (an emoji is 2)."""
    return len(text.encode("utf-16-le")) // 2


def _field(obj, key):
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def _mention_id(entity):
    """The user id a clickable name points at, or None. Telegram gives either a text_mention (with the user) or a
    text_link to tg://user?id=N."""
    kind = _field(entity, "type")
    if kind == "text_mention":
        user = _field(entity, "user")
        uid = _field(user, "id") if user is not None else None
        return str(uid) if uid is not None else None
    if kind == "text_link":
        m = re.match(r"tg://user\?id=(-?\d+)", str(_field(entity, "url") or ""))
        return m.group(1) if m else None
    return None


def parse_post(text: str, entities) -> dict:
    """
    Reads a hub post as Telegram returns it (plain text + entities): the declared TOTAL and, for every
    "Going (N)" / "Going from <chat> (N)" section, the people listed - each as {"name", "id"} where id is the
    mention's user id or None for plain text. Guest lines ("<icon> 2, from: Name") are not people.
    """
    lines = (text or "").split("\n")
    starts, pos = [], 0
    for line in lines:
        starts.append(pos)
        pos += _u16(line) + 1
    mentions = []
    for e in entities or []:
        uid = _mention_id(e)
        if uid is not None:
            mentions.append((_field(e, "offset") or 0, uid))

    parsed = {"total": None, "sections": [], "mention_count": len(mentions)}
    section = None
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if re.match(r"^Going \((\d+)\):$", stripped):
            section = {"title": "Going", "people": [], "guest_lines": 0}
            parsed["sections"].append(section)
            continue
        m = re.match(r"^Going from (.+) \((\d+)\):$", stripped)
        if m:
            section = {"title": f"Going from {m.group(1)}", "people": [], "guest_lines": 0}
            parsed["sections"].append(section)
            continue
        m = re.search(r"TOTAL Going:\s*(\d+)", stripped)
        if m:
            parsed["total"] = int(m.group(1))
            section = None
            continue
        if stripped.startswith(("Not Going", "Waitlist")):
            section = None
            continue
        if section is None or not stripped:
            continue
        if re.match(r"^\S+ \d+, from: ", stripped):
            section["guest_lines"] += 1
            continue
        parts = stripped.split(" ", 1)
        name = parts[1].strip() if len(parts) == 2 else parts[0]
        line_end = starts[idx] + _u16(line)
        uid = next((u for off, u in sorted(mentions) if starts[idx] <= off < line_end), None)
        section["people"].append({"name": name, "id": uid})
    return parsed


def compare_post(event: dict, roster: dict, parsed: dict) -> list:
    """What the post shows against what the DB says. Finds stale/failed edits - not a wrong roster (see the docstring)."""
    findings = []

    def add(code, severity, detail):
        findings.append(Finding(code, severity, event["event_id"], event["name"], detail))

    if parsed["total"] is None and not parsed["sections"]:
        add("POST_UNREADABLE", INFO, "the post has no Going section or TOTAL line - an unfamiliar layout")
        return findings
    if parsed["total"] is not None and parsed["total"] != roster["headcount"]:
        add("POST_TOTAL", PROBLEM, f"the post says TOTAL Going {parsed['total']}, the DB has {roster['headcount']} (going + guests)")

    db_people = []
    for r in roster["going_rows"]:
        _chat, uid, username, first, last, _status, _guests = r
        names = {n.casefold() for n in (username, first, f"{first} {last}".strip() if first and last else None) if n}
        db_people.append({"id": str(uid), "label": username or first or str(uid), "names": names})

    unmatched_db = list(db_people)
    extra_in_post = []
    for section in parsed["sections"]:
        for person in section["people"]:
            idx = None
            if person["id"] is not None:
                idx = next((i for i, d in enumerate(unmatched_db) if d["id"] == person["id"]), None)
            if idx is None:
                key = person["name"].casefold()
                idx = next((i for i, d in enumerate(unmatched_db) if key in d["names"]), None)
            if idx is None:
                extra_in_post.append(person["name"])
            else:
                unmatched_db.pop(idx)
    if unmatched_db:
        add("POST_MISSING_PERSON", PROBLEM, f"going in the DB, not shown in the post: {_shown(d['label'] for d in unmatched_db)}")
    if extra_in_post:
        add("POST_EXTRA_PERSON", PROBLEM, f"shown in the post, not going in the DB: {_shown(extra_in_post)}")
    if parsed["mention_count"] == 0 and any(_is_real_id(u) for u in roster["ids"]):
        add("POST_NO_IDS", INFO, "the post has no clickable names, so only NAMES could be compared (ids can't be checked from it)")
    return findings


async def read_post(bot, from_chat_id, message_id, to_chat_id):
    """
    The post's text and entities as Telegram holds them now, or None if it can't be read. The Bot API cannot read an
    old message, so it is forwarded to `to_chat_id` and the copy is deleted right away.
    """
    try:
        forwarded = await bot.forward_message(chat_id=to_chat_id, from_chat_id=from_chat_id, message_id=int(message_id))
    except Exception:
        return None
    try:
        return forwarded.text, list(forwarded.entities or [])
    finally:
        try:
            await bot.delete_message(chat_id=to_chat_id, message_id=forwarded.message_id)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Running it
# ---------------------------------------------------------------------------

async def read_sheet_state(ss):
    """-> ({event_id: [USER_ID, ...]} from the EventUsers tab, or None when that tab is missing,
            {event_id: Events-tab row as a dict}). Two reads, whatever the size."""
    from gspread.exceptions import WorksheetNotFound

    try:
        eu_values = await (await ss.worksheet("EventUsers")).get_all_values()
    except WorksheetNotFound:
        eu_values = None
    try:
        events_records = await (await ss.worksheet("Events")).get_all_records()
    except WorksheetNotFound:
        events_records = []
    by_event = None
    if eu_values is not None:
        by_event = {}
        for row in eu_values[1:]:
            if len(row) >= 2 and str(row[0]).strip():
                by_event.setdefault(str(row[0]).strip(), []).append(str(row[1]).strip())
    return by_event, {str(r.get("EVENT_ID", "")).strip(): r for r in events_records}


async def audit_hub(chat_id, db_path, cutoff, bot=None, to_chat_id=None, post_limit=15) -> tuple:
    """-> (findings, stats) for one hub, or (None, None) when it has no usable Sheet."""
    ss = await open_spreadsheet(await get_sheet_for_chat(chat_id))
    if not ss:
        return None, None

    findings, stats = [], {"events": 0, "closed": 0, "posts_read": 0, "posts_unreadable": 0}
    hub_finding = lambda code, sev, detail: findings.append(Finding(code, sev, "-", "(hub)", detail))

    sheet_ids_by_event, events_rows = await read_sheet_state(ss)
    eu_values = sheet_ids_by_event is not None
    if not eu_values:
        hub_finding("EVENTUSERS_TAB_MISSING", PROBLEM, "the sheet has no EventUsers tab")
        sheet_ids_by_event = {}

    conn = sqlite3.connect(db_path)
    try:
        resolvable = known_ids_by_username(conn)
        db_events = conn.execute(
            "SELECT event_id, name, event_status, created_date, message_id, going_data FROM events WHERE chat_id = ? ORDER BY ROWID",
            (chat_id,),
        ).fetchall()
        known_event_ids = {str(r[0]) for r in db_events}

        reads_left = post_limit
        for event_id, name, status, created_raw, message_id, going_data in db_events:
            created = _parse_created(created_raw)
            if cutoff is not None and (created is None or created < cutoff):
                continue
            stats["events"] += 1
            event = {"event_id": str(event_id), "name": name or str(event_id), "status": status}
            roster = effective_roster(conn, event_id, status, going_data)
            if status == 2:
                stats["closed"] += 1
            if eu_values:
                findings += compare_event(event, roster, sheet_ids_by_event.get(str(event_id), []),
                                          events_rows.get(str(event_id)), resolvable)
            if bot is not None and status == 2 and message_id and reads_left > 0:
                reads_left -= 1
                post = await read_post(bot, chat_id, message_id, to_chat_id)
                await asyncio.sleep(FORWARD_PAUSE)
                if post is None:
                    stats["posts_unreadable"] += 1
                    findings.append(Finding("POST_UNREADABLE", INFO, event["event_id"], event["name"],
                                            "couldn't read the post (forwarding is restricted, or the post is gone)"))
                else:
                    stats["posts_read"] += 1
                    findings += compare_post(event, roster, parse_post(*post))
    finally:
        conn.close()

    orphans = sorted(set(sheet_ids_by_event) - known_event_ids)
    if orphans:
        hub_finding("SHEET_ORPHAN_EVENT", INFO,
                    f"EventUsers rows of {len(orphans)} event id(s) the DB doesn't know (deleted events, or older than this database): {_shown(orphans)}")
    return findings, stats


async def _run(target_chat_id=None, days=60, db_path=DB_PATH, telegram=False, to_chat_id=None, post_limit=15):
    init_db(db_path=db_path)
    cutoff = None if days is None else datetime.now() - timedelta(days=days)
    conn = sqlite3.connect(db_path)
    if target_chat_id:
        chat_ids = [target_chat_id]
    else:
        chat_ids = [r[0] for r in conn.execute("SELECT chat_id FROM all_groups WHERE sheet_id IS NOT NULL AND sheet_id != ''")]
    conn.close()

    bot_cm = None
    bot = None
    if telegram:
        from telegram import Bot
        import config
        bot_cm = Bot(config.TELEGRAM_TOKEN)
        await bot_cm.initialize()
        bot = bot_cm
    results = {}
    try:
        for chat_id in chat_ids:
            findings, stats = await audit_hub(chat_id, db_path, cutoff, bot=bot, to_chat_id=to_chat_id, post_limit=post_limit)
            results[chat_id] = (findings, stats)
    finally:
        if bot_cm is not None:
            await bot_cm.shutdown()
    return results


def _print_report(results, days):
    problems = 0
    window = "all time" if days is None else f"the last {days} day(s)"
    for chat_id, (findings, stats) in results.items():
        if findings is None:
            print(f"\nHub {chat_id}: no usable Google Sheet - skipped.")
            continue
        hub_problems = [f for f in findings if f.severity == PROBLEM]
        problems += len(hub_problems)
        extra = f", {stats['posts_read']} post(s) read ({stats['posts_unreadable']} unreadable)" if stats["posts_read"] or stats["posts_unreadable"] else ""
        print(f"\nHub {chat_id}: {stats['events']} event(s) from {window}, {stats['closed']} closed{extra} - "
              f"{len(hub_problems)} problem(s), {len(findings) - len(hub_problems)} note(s)")
        for f in sorted(findings, key=lambda f: (f.severity != PROBLEM, f.event_id, f.code)):
            print(f"  [{f.severity}] {f.event_name} ({f.event_id}) {f.code}: {f.detail}")
    print(f"\n{'No problems found.' if not problems else f'{problems} problem(s) found.'}")
    return problems


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Read-only audit of who attended each event: DB vs Google Sheet (vs Telegram posts).")
    parser.add_argument("--chat-id", default=None, help="Audit a single hub instead of every hub that has a Sheet.")
    parser.add_argument("--days", type=int, default=60, help="Only events created in the last N days (default 60).")
    parser.add_argument("--all-time", action="store_true", help="Every event, ignoring --days.")
    parser.add_argument("--telegram", action="store_true", help="Also read each closed event's post back (forwards it to --to and deletes the copy).")
    parser.add_argument("--to", default=None, help="Chat id to forward posts into for --telegram (your own DM with the bot).")
    parser.add_argument("--limit", type=int, default=15, help="With --telegram: at most this many posts per hub (default 15).")
    parser.add_argument("--json", action="store_true", help="Print the findings as JSON instead of text.")
    args = parser.parse_args()
    if args.telegram and not args.to:
        parser.error("--telegram needs --to <chat id> (the chat the posts are forwarded into; your DM with the bot works).")
    days = None if args.all_time else args.days
    results = asyncio.run(_run(args.chat_id, days, telegram=args.telegram, to_chat_id=args.to, post_limit=args.limit))
    if args.json:
        print(json.dumps({chat: ({"findings": [asdict(f) for f in fs], "stats": st} if fs is not None else None)
                          for chat, (fs, st) in results.items()}, ensure_ascii=False, indent=2))
        sys.exit(1 if any(f.severity == PROBLEM for fs, _ in results.values() if fs for f in fs) else 0)
    sys.exit(1 if _print_report(results, days) else 0)
