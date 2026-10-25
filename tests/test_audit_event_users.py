"""scripts/audit_event_users.py - the read-only reconciliation of event_users, the Sheet tabs and (opt-in) the posts."""
import json
import sqlite3
import sys
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, ".")
import handlers
import scripts.audit_event_users as audit
from tests.helpers import make_bot, make_context
from tests.test_eventusers_lifecycle import World, first_close, Book

HUB, CHILD = "-100", "-200"


def codes(findings):
    return sorted(f.code for f in findings)


def seed(db_path, status=2, event_id="ev1", chat=HUB, message_id="1", clickability="on", created="01.10.2026 12:00:00.000", going="[]"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon, event_status, going_data, "
        "notgoing_data, counters_data, kicked_data, created_date, clickability) VALUES (?, ?, ?, 'Match', '👍', '❌', ?, ?, '[]', '{}', '[]', ?, ?)",
        (event_id, chat, message_id, status, going, created, clickability))
    conn.commit()
    conn.close()


def person(db_path, uid, username, chat=HUB, status="going", guests=0, first=None, last=None, event_id="ev1"):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO event_users (event_id, chat_id, user_id, username, first_name, last_name, status, guests) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (event_id, chat, str(uid), username, first, last, status, guests))
    conn.commit()
    conn.close()


def roster_of(db_path, event_id="ev1"):
    conn = sqlite3.connect(db_path)
    try:
        return audit.db_roster(conn, event_id)
    finally:
        conn.close()


EVENT = {"event_id": "ev1", "name": "Match", "status": 2}


def good_row(count):
    return {"EVENT_ID": "ev1", "STATUS": "CLOSED", "GOING_COUNT": count}


class TestTheRoster:
    def test_going_people_once_each_with_the_headcount_save_and_close_uses(self, db_path):
        seed(db_path)
        person(db_path, 1, "alice", guests=2)
        person(db_path, 2, "bob", chat=CHILD)
        person(db_path, 3, "carol", status="notgoing", guests=1)         # a guest of someone not going still counts
        person(db_path, 4, "dave", status="kicked")
        r = roster_of(db_path)
        assert r["ids"] == ["1", "2"] and r["headcount"] == 2 + 2 + 1

    def test_the_same_person_in_two_chats_is_noticed(self, db_path):
        seed(db_path)
        person(db_path, 1, "alice"); person(db_path, 1, "alice", chat=CHILD)
        r = roster_of(db_path)
        assert r["ids"] == ["1"] and r["in_two_chats"] == ["1"]

    def test_people_typed_in_by_name_are_placeholders(self, db_path):
        seed(db_path)
        person(db_path, 1, "alice"); person(db_path, "typedname", "typedname")
        assert roster_of(db_path)["placeholders"] == ["typedname"]

    def test_the_frozen_legacy_list_gives_ids_and_names_without_ids(self):
        got = audit.legacy_roster(json.dumps(["alice (1)", "bob (2)", "typed_in"]))
        assert got == {"ids": ["1", "2"], "name_only": ["typed_in"]}
        assert audit.legacy_roster("garbage") == {"ids": [], "name_only": []}


class TestDbAgainstTheSheet:
    def _check(self, db_path, sheet_ids, events_row="auto", status=2, resolvable=None):
        r = roster_of(db_path)
        row = good_row(r["headcount"]) if events_row == "auto" else events_row
        return audit.compare_event({**EVENT, "status": status}, r, sheet_ids, row, resolvable)

    def test_everything_agrees(self, db_path):
        seed(db_path); person(db_path, 1, "alice"); person(db_path, 2, "bob", chat=CHILD)
        assert self._check(db_path, ["1", "2"]) == []

    def test_the_order_in_the_sheet_does_not_matter(self, db_path):
        seed(db_path); person(db_path, 1, "alice"); person(db_path, 2, "bob")
        assert self._check(db_path, ["2", "1"]) == []

    def test_a_person_missing_from_the_sheet_is_named(self, db_path):
        seed(db_path); person(db_path, 1, "alice"); person(db_path, 2, "bob")
        f = self._check(db_path, ["1"], events_row=good_row(2))
        assert codes(f) == ["ID_NOT_IN_SHEET"] and "bob" in f[0].detail

    def test_someone_in_the_sheet_who_is_not_going(self, db_path):
        seed(db_path); person(db_path, 1, "alice")
        assert codes(self._check(db_path, ["1", "9"])) == ["ID_NOT_IN_DB"]

    def test_no_rows_at_all(self, db_path):
        seed(db_path); person(db_path, 1, "alice")
        assert codes(self._check(db_path, [])) == ["EVENTUSERS_MISSING"]

    def test_a_doubled_roster(self, db_path):
        seed(db_path); person(db_path, 1, "alice")
        assert codes(self._check(db_path, ["1", "1"])) == ["SHEET_DUPLICATE"]

    def test_rows_for_an_event_that_is_open_again(self, db_path):
        seed(db_path, status=0); person(db_path, 1, "alice")
        f = self._check(db_path, ["1"], events_row={"STATUS": "OPEN"}, status=0)
        assert codes(f) == ["ROWS_FOR_UNFINISHED_EVENT"]

    def test_an_open_event_without_rows_is_fine(self, db_path):
        seed(db_path, status=0); person(db_path, 1, "alice")
        assert self._check(db_path, [], events_row={"STATUS": "OPEN"}, status=0) == []

    def test_the_events_row(self, db_path):
        seed(db_path); person(db_path, 1, "alice", guests=1)
        assert codes(self._check(db_path, ["1"], events_row=None)) == ["EVENTS_ROW_MISSING"]
        assert codes(self._check(db_path, ["1"], events_row={"STATUS": "OPEN", "GOING_COUNT": 2})) == ["EVENTS_STATUS"]
        assert codes(self._check(db_path, ["1"], events_row=good_row(5))) == ["EVENTS_COUNT"]
        assert codes(self._check(db_path, ["1"], events_row=good_row("n/a"))) == ["EVENTS_COUNT"]

    def test_a_canceled_event_is_expected_to_say_canceled(self, db_path):
        seed(db_path, status=-1)
        assert self._check(db_path, [], events_row={"STATUS": "CANCELED"}, status=-1) == []
        assert codes(self._check(db_path, [], events_row={"STATUS": "OPEN"}, status=-1)) == ["EVENTS_STATUS"]

    def test_one_person_in_two_chats_is_a_problem(self, db_path):
        seed(db_path); person(db_path, 1, "alice"); person(db_path, 1, "alice", chat=CHILD)
        assert "DB_SAME_PERSON_TWICE" in codes(self._check(db_path, ["1"], events_row=good_row(2)))

    def test_people_typed_in_by_name_are_reported_as_a_note_not_a_problem(self, db_path):
        seed(db_path); person(db_path, 1, "alice"); person(db_path, "typedname", "typedname")
        f = self._check(db_path, ["1", "typedname"])
        assert [(x.code, x.severity) for x in f] == [("NO_REAL_ID", audit.INFO)]
        assert "stored nowhere" in f[0].detail

    def test_it_says_when_such_a_name_is_nowadays_known_with_a_real_id(self, db_path):
        seed(db_path); person(db_path, "typedname", "typedname")
        f = self._check(db_path, ["typedname"], resolvable={"typedname": "777"})
        assert "typedname -> 777" in f[0].detail

    def test_long_lists_are_cut(self):
        assert audit._shown(range(20)).endswith("(+12 more)")


# ── the whole run against a spreadsheet ──────────────────────────────────────

async def run_audit(book, db_path, cutoff=None, **kwargs):
    with patch("scripts.audit_event_users.get_sheet_for_chat", new_callable=AsyncMock, return_value="sid"), \
         patch("scripts.audit_event_users.open_spreadsheet", new_callable=AsyncMock, return_value=book):
        return await audit.audit_hub(HUB, db_path, cutoff, **kwargs)


class TestAcrossAHub:
    async def test_a_consistent_hub_has_no_findings(self, db_path):
        seed(db_path); person(db_path, 1, "alice"); person(db_path, 2, "bob", chat=CHILD)
        book = Book()
        book.tabs["Events"].rows[1] = ["ev1", "Match", "c", "42", "", "01.10.2026", "CLOSED", 2]
        book.tabs["EventUsers"].rows += [["ev1", "1"], ["ev1", "2"]]
        findings, stats = await run_audit(book, db_path)
        assert findings == [] and stats["events"] == 1 and stats["closed"] == 1

    async def test_a_hub_without_a_sheet_is_skipped(self, db_path):
        with patch("scripts.audit_event_users.get_sheet_for_chat", new_callable=AsyncMock, return_value=None), \
             patch("scripts.audit_event_users.open_spreadsheet", new_callable=AsyncMock, return_value=None):
            assert await audit.audit_hub(HUB, db_path, None) == (None, None)

    async def test_the_time_window(self, db_path):
        from datetime import datetime
        seed(db_path, event_id="old", created="01.01.2020 12:00:00.000"); person(db_path, 1, "alice", event_id="old")
        book = Book()
        findings, stats = await run_audit(book, db_path, cutoff=datetime(2026, 1, 1))
        assert stats["events"] == 0
        findings, stats = await run_audit(book, db_path, cutoff=None)
        assert stats["events"] == 1 and "EVENTUSERS_MISSING" in codes(findings)

    async def test_a_missing_tab_is_reported_once(self, db_path):
        seed(db_path); person(db_path, 1, "alice")
        book = Book()
        del book.tabs["EventUsers"]
        findings, _ = await run_audit(book, db_path)
        assert [f.code for f in findings if f.severity == audit.PROBLEM and f.event_id == "-"] == ["EVENTUSERS_TAB_MISSING"]

    async def test_rows_of_events_the_db_does_not_know_are_a_note(self, db_path):
        seed(db_path); person(db_path, 1, "alice")
        book = Book()
        book.tabs["Events"].rows[1] = ["ev1", "Match", "c", "42", "", "d", "CLOSED", 1]
        book.tabs["EventUsers"].rows += [["ev1", "1"], ["ghost", "5"], ["ghost", "6"]]
        findings, _ = await run_audit(book, db_path)
        assert [(f.code, f.severity) for f in findings] == [("SHEET_ORPHAN_EVENT", audit.INFO)]

    async def test_an_event_closed_before_event_users_existed_is_compared_by_its_frozen_list(self, db_path):
        seed(db_path, going=json.dumps(["alice (1)", "bob (2)"]))
        book = Book()
        book.tabs["Events"].rows[1] = ["ev1", "Match", "c", "42", "", "d", "CLOSED", 2]
        book.tabs["EventUsers"].rows += [["ev1", "1"], ["ev1", "2"]]
        findings, _ = await run_audit(book, db_path)
        assert findings == []


class TestAgainstTheRealFlows:
    """The audit on a spreadsheet written by the real handlers: it has to agree with a good flow and catch a bad one."""

    def _patched(self, w):
        stack = ExitStack()
        stack.enter_context(patch("scripts.audit_event_users.get_sheet_for_chat", new_callable=AsyncMock, return_value="sid"))
        stack.enter_context(patch("scripts.audit_event_users.open_spreadsheet", new_callable=AsyncMock, return_value=w.book))
        return stack

    async def _audit(self, w, db_path):
        with self._patched(w):
            return (await audit.audit_hub("-100123", db_path, None))[0]

    async def test_a_normal_close_audits_clean(self, db_path):
        w = World(db_path)
        await first_close(w)
        assert [f for f in await self._audit(w, db_path) if f.severity == audit.PROBLEM] == []

    async def test_a_reopen_whose_sheet_cleanup_did_not_happen_is_caught_then_healed_by_the_next_save(self, db_path):
        w = World(db_path)
        await first_close(w)
        await w.reopen(cleanup=False)
        assert "ROWS_FOR_UNFINISHED_EVENT" in codes(await self._audit(w, db_path))
        await w.press("close_ev1"); await w.press("return_ev1:ch-bob"); await w.add_extra("dave"); await w.press("save_ev1")
        assert [f for f in await self._audit(w, db_path) if f.severity == audit.PROBLEM] == []

    async def test_a_row_lost_from_the_sheet_is_found(self, db_path):
        w = World(db_path)
        await first_close(w)
        w.book.tabs["EventUsers"].rows.pop()
        assert "ID_NOT_IN_SHEET" in codes(await self._audit(w, db_path))


# ── reading the post back ────────────────────────────────────────────────────

def md2_to_entities(md):
    """What Telegram does with the MarkdownV2 the bot sends: plain text + entities, offsets in UTF-16 units."""
    out, entities, open_marks, units = [], [], {}, 0
    names = {"*": "bold", "_": "italic", "~": "strikethrough"}

    def emit(ch):
        nonlocal units
        out.append(ch)
        units += len(ch.encode("utf-16-le")) // 2

    def unescape(s):
        return "".join(s[i + 1] if s[i] == "\\" and i + 1 < len(s) else s[i] for i in range(len(s)) if not (i > 0 and s[i - 1] == "\\" and s[i] != "\\") or True)

    i = 0
    while i < len(md):
        c = md[i]
        if c == "\\" and i + 1 < len(md):
            emit(md[i + 1]); i += 2; continue
        if c in names:
            if c in open_marks:
                start = open_marks.pop(c)
                entities.append({"type": names[c], "offset": start, "length": units - start})
            else:
                open_marks[c] = units
            i += 1; continue
        if c == "[":
            j = md.index("](", i); k = md.index(")", j)
            start = units
            label, t = md[i + 1:j], 0
            while t < len(label):
                if label[t] == "\\" and t + 1 < len(label):
                    emit(label[t + 1]); t += 2
                else:
                    emit(label[t]); t += 1
            entities.append({"type": "text_link", "offset": start, "length": units - start, "url": md[j + 2:k]})
            i = k + 1; continue
        emit(c); i += 1
    return "".join(out), entities


async def hub_post(db_path):
    bot = make_bot()
    bot.get_chat = AsyncMock(return_value=MagicMock(title="News", type="channel"))
    await handlers.update_all_shared_views(make_context(bot=bot), "ev1")
    return md2_to_entities(bot.edit_message_text.call_args_list[0].kwargs["text"])


def share(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type) VALUES ('ev1', ?, '5', '-visible', 'channel')", (CHILD,))
    conn.commit()
    conn.close()


def standard_event(db_path, **kwargs):
    seed(db_path, **kwargs)
    share(db_path)
    person(db_path, 1, "alice", guests=2, first="Alice", last="A")
    person(db_path, 2, "bob", chat=CHILD, first="Bob")


class TestThePost:
    async def test_the_real_post_parses_into_its_people_and_total(self, db_path):
        standard_event(db_path)
        parsed = audit.parse_post(*await hub_post(db_path))
        assert parsed["total"] == 4
        assert [(s["title"], [p["id"] for p in s["people"]], s["guest_lines"]) for s in parsed["sections"]] == [
            ("Going", ["1"], 1), ("Going from News", ["2"], 0)]
        assert [p["name"] for s in parsed["sections"] for p in s["people"]] == ["alice", "bob"], "the lists show usernames"

    async def test_the_real_post_agrees_with_the_db(self, db_path):
        standard_event(db_path)
        parsed = audit.parse_post(*await hub_post(db_path))
        assert audit.compare_post(EVENT, roster_of(db_path), parsed) == []

    async def test_a_post_that_is_stale_is_caught(self, db_path):
        standard_event(db_path)
        text, entities = await hub_post(db_path)
        person(db_path, 3, "carol", first="Carol")                       # joined after the post was last edited
        found = audit.compare_post(EVENT, roster_of(db_path), audit.parse_post(text, entities))
        assert codes(found) == ["POST_MISSING_PERSON", "POST_TOTAL"]

    async def test_someone_in_the_post_but_not_in_the_db_is_caught(self, db_path):
        standard_event(db_path)
        parsed = audit.parse_post(*await hub_post(db_path))
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE event_users SET status = 'notgoing' WHERE user_id = '2'")
        conn.commit()
        conn.close()
        assert "POST_EXTRA_PERSON" in codes(audit.compare_post(EVENT, roster_of(db_path), parsed))

    async def test_plain_names_are_matched_by_name_and_the_note_says_ids_could_not_be_checked(self, db_path):
        standard_event(db_path, clickability="off")
        parsed = audit.parse_post(*await hub_post(db_path))
        assert parsed["mention_count"] == 0
        found = audit.compare_post(EVENT, roster_of(db_path), parsed)
        assert [(f.code, f.severity) for f in found] == [("POST_NO_IDS", audit.INFO)]

    async def test_a_person_typed_in_by_name_is_matched_by_name(self, db_path):
        seed(db_path); person(db_path, 1, "alice", first="Alice"); person(db_path, "typedname", "typedname")
        parsed = audit.parse_post(*await hub_post(db_path))
        assert audit.compare_post(EVENT, roster_of(db_path), parsed) == []

    async def test_names_with_markdown_characters_survive(self, db_path):
        seed(db_path); person(db_path, 1, "a_b*c", first="A_B", last="(x.y)")
        parsed = audit.parse_post(*await hub_post(db_path))
        assert parsed["sections"][0]["people"][0] == {"name": "a_b*c", "id": "1"}

    def test_both_kinds_of_mention_entity_are_understood(self):
        text = "Going (2):\n👍 Alice\n👍 Bob\n\nTOTAL Going: 2"
        entities = [{"type": "text_link", "offset": 14, "length": 5, "url": "tg://user?id=1"},
                    {"type": "text_mention", "offset": 22, "length": 3, "user": {"id": 2}}]
        assert [p["id"] for p in audit.parse_post(text, entities)["sections"][0]["people"]] == ["1", "2"]

    def test_utf16_offsets_are_used_so_emoji_before_a_name_do_not_shift_it(self):
        text = "Going (1):\n👍👍👍 Zed\n\nTOTAL Going: 1"
        start = len("Going (1):\n") + 0
        offset = (len("Going (1):\n") + len("👍👍👍 ") + 3)           # each emoji is 2 UTF-16 units: 3 extra
        parsed = audit.parse_post(text, [{"type": "text_link", "offset": offset, "length": 3, "url": "tg://user?id=9"}])
        assert parsed["sections"][0]["people"] == [{"name": "Zed", "id": "9"}] and start

    def test_an_unfamiliar_layout_is_a_note(self):
        f = audit.compare_post(EVENT, {"headcount": 0, "going_rows": [], "ids": []}, audit.parse_post("hello", []))
        assert [x.code for x in f] == ["POST_UNREADABLE"]


class TestReadingItBack:
    async def test_the_forwarded_copy_is_read_and_then_deleted(self):
        bot = MagicMock()
        bot.forward_message = AsyncMock(return_value=MagicMock(text="hi", entities=[1], message_id=77))
        bot.delete_message = AsyncMock()
        assert await audit.read_post(bot, "-100", "5", "123") == ("hi", [1])
        bot.forward_message.assert_awaited_once_with(chat_id="123", from_chat_id="-100", message_id=5)
        bot.delete_message.assert_awaited_once_with(chat_id="123", message_id=77)

    async def test_a_restricted_group_is_just_unreadable(self):
        bot = MagicMock()
        bot.forward_message = AsyncMock(side_effect=RuntimeError("protected content"))
        bot.delete_message = AsyncMock()
        assert await audit.read_post(bot, "-100", "5", "123") is None
        bot.delete_message.assert_not_called()

    async def test_a_failing_cleanup_does_not_lose_the_text(self):
        bot = MagicMock()
        bot.forward_message = AsyncMock(return_value=MagicMock(text="hi", entities=None, message_id=77))
        bot.delete_message = AsyncMock(side_effect=RuntimeError("too old"))
        assert await audit.read_post(bot, "-100", "5", "123") == ("hi", [])

    async def test_the_hub_run_reads_posts_only_for_closed_events_up_to_the_limit(self, db_path):
        standard_event(db_path)
        text, entities = await hub_post(db_path)
        seed(db_path, event_id="ev2", status=0, message_id="2")
        seed(db_path, event_id="ev3", status=2, message_id="3")
        book = Book()
        book.tabs["Events"].rows[1] = ["ev1", "Match", "c", "42", "", "d", "CLOSED", 4]
        bot = MagicMock()
        bot.forward_message = AsyncMock(return_value=MagicMock(text=text, entities=[
            MagicMock(type=e["type"], offset=e["offset"], length=e["length"], url=e.get("url"), user=None) for e in entities],
            message_id=1))
        bot.delete_message = AsyncMock()
        with patch("scripts.audit_event_users.asyncio.sleep", new=AsyncMock()):
            _findings, stats = await run_audit(book, db_path, bot=bot, to_chat_id="123", post_limit=1)
        assert stats["posts_read"] == 1 and bot.forward_message.await_count == 1, "one post only: the limit, and not the open event"


class TestTheRunAndTheReport:
    def _hub(self, db_path, chat_id=HUB, sheet_id="sid"):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO all_groups (chat_id, type, sheet_id) VALUES (?, 'PRO', ?)", (chat_id, sheet_id))
        conn.commit()
        conn.close()

    async def _run(self, db_path, book, **kwargs):
        with patch("scripts.audit_event_users.get_sheet_for_chat", new_callable=AsyncMock, return_value="sid"), \
             patch("scripts.audit_event_users.open_spreadsheet", new_callable=AsyncMock, return_value=book):
            return await audit._run(db_path=db_path, days=None, **kwargs)

    async def test_it_audits_every_hub_that_has_a_sheet_and_only_those(self, db_path):
        self._hub(db_path, HUB); self._hub(db_path, "-300", sheet_id="")
        seed(db_path); person(db_path, 1, "alice")
        results = await self._run(db_path, Book())
        assert list(results) == [HUB]

    async def test_a_single_hub_can_be_named(self, db_path):
        seed(db_path); person(db_path, 1, "alice")
        results = await self._run(db_path, Book(), target_chat_id=HUB)
        assert list(results) == [HUB]

    async def test_the_report_counts_problems_and_leaves_notes_out_of_it(self, db_path, capsys):
        self._hub(db_path)
        seed(db_path); person(db_path, 1, "alice"); person(db_path, "typedname", "typedname")
        book = Book()
        book.tabs["Events"].rows[1] = ["ev1", "Match", "c", "42", "", "d", "CLOSED", 2]
        book.tabs["EventUsers"].rows += [["ev1", "typedname"]]               # alice is missing
        problems = audit._print_report(await self._run(db_path, book), None)
        out = capsys.readouterr().out
        assert problems == 1
        assert "ID_NOT_IN_SHEET" in out and "NO_REAL_ID" in out and "1 problem(s) found." in out

    async def test_a_clean_run_says_so_and_reports_zero(self, db_path, capsys):
        self._hub(db_path)
        seed(db_path); person(db_path, 1, "alice")
        book = Book()
        book.tabs["Events"].rows[1] = ["ev1", "Match", "c", "42", "", "d", "CLOSED", 1]
        book.tabs["EventUsers"].rows += [["ev1", "1"]]
        assert audit._print_report(await self._run(db_path, book), 60) == 0
        assert "No problems found." in capsys.readouterr().out

    async def test_it_never_writes_to_the_spreadsheet_or_the_database(self, db_path):
        self._hub(db_path)
        seed(db_path); person(db_path, 1, "alice")
        book = Book()
        book.tabs["EventUsers"].rows += [["ev1", "999"]]
        before_sheet = [[list(r) for r in t.rows] for t in book.tabs.values()]
        conn = sqlite3.connect(db_path)
        before_db = conn.execute("SELECT * FROM event_users").fetchall()
        conn.close()
        await self._run(db_path, book)
        assert [[list(r) for r in t.rows] for t in book.tabs.values()] == before_sheet and book.batch_calls == []
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT * FROM event_users").fetchall() == before_db
