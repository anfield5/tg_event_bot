"""scripts/fix_eventusers_ids.py - usernames in EventUsers (and event_users) become real user ids."""
import sqlite3
import sys
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

sys.path.insert(0, ".")
import handlers
import scripts.audit_event_users as audit
import scripts.fix_eventusers_ids as fix
from tests.test_audit_event_users import HUB, person, seed
from tests.test_eventusers_lifecycle import Book, Tab

USERS_HEADER = ["USER_ID", "FIRST_NAME", "LAST_NAME", "USER_NAME", "CHAT_ID", "STATUS", "DATE_start", "DATE_end", "ARCHIVED_USER_NAME"]


def users_tab(*rows):
    """rows: (user_id, current username, archived usernames)"""
    return Tab(4, USERS_HEADER, [[uid, "", "", name, HUB, "MEMBER", "", "", archived] for uid, name, archived in rows])


def book_with(*, users=(), eventusers=(), events=()):
    book = Book()
    book.tabs["Users"] = users_tab(*users)
    book.tabs["EventUsers"].rows += [list(r) for r in eventusers]
    book.tabs["Events"].rows[1:] = [list(r) for r in events]
    return book


def patched(book):
    stack = ExitStack()
    for module in ("scripts.fix_eventusers_ids", "scripts.audit_event_users", "sheets"):
        stack.enter_context(patch(f"{module}.get_sheet_for_chat", new_callable=AsyncMock, return_value="sid"))
        stack.enter_context(patch(f"{module}.open_spreadsheet", new_callable=AsyncMock, return_value=book))
    return stack


async def run(book, db_path, apply=False):
    with patched(book):
        return await fix.fix_hub(HUB, db_path, apply=apply)


def db_ids(db_path, event_id="ev1"):
    conn = sqlite3.connect(db_path)
    try:
        return sorted(r[0] for r in conn.execute("SELECT user_id FROM event_users WHERE event_id = ?", (event_id,)))
    finally:
        conn.close()


def sql(db_path, statement, *params):
    conn = sqlite3.connect(db_path)
    conn.execute(statement, params)
    conn.commit()
    conn.close()


class TestTheLookup:
    def test_the_users_tab_matches_without_case_and_at_sign(self):
        index = fix.username_index([{"USER_ID": "777", "USER_NAME": "TypedName", "ARCHIVED_USER_NAME": ""}], [])
        assert fix.resolve(index, "@typedname") == ("ok", "777", "Users tab")
        assert fix.resolve(index, " TYPEDNAME ")[1] == "777"

    def test_an_earlier_username_of_someone_who_renamed_matches_too(self):
        index = fix.username_index([{"USER_ID": "5", "USER_NAME": "newname", "ARCHIVED_USER_NAME": "oldname, older;oldest"}], [])
        assert [fix.resolve(index, n)[1] for n in ("oldname", "older", "oldest")] == ["5", "5", "5"]
        assert fix.resolve(index, "oldname")[2] == "Users tab (earlier username)"

    def test_the_database_is_a_source_too_and_cyrillic_works(self):
        index = fix.username_index([], [("Иван", "42")])
        assert fix.resolve(index, "иван") == ("ok", "42", "database")

    def test_only_real_numeric_ids_are_used(self):
        index = fix.username_index([{"USER_ID": "typedname", "USER_NAME": "typedname"}, {"USER_ID": "", "USER_NAME": "ghost"}], [("x", "also_a_name")])
        assert fix.resolve(index, "typedname")[0] == "unknown" and fix.resolve(index, "ghost")[0] == "unknown" and fix.resolve(index, "x")[0] == "unknown"

    def test_two_people_for_one_name_is_ambiguous_and_never_guessed(self):
        index = fix.username_index([{"USER_ID": "1", "USER_NAME": "sam"}, {"USER_ID": "2", "USER_NAME": "Sam"}], [])
        assert fix.resolve(index, "sam") == ("ambiguous", ["1", "2"], None)

    def test_the_same_person_from_two_sources_is_still_one_answer(self):
        index = fix.username_index([{"USER_ID": "9", "USER_NAME": "kim"}], [("kim", "9")])
        assert fix.resolve(index, "kim") == ("ok", "9", "Users tab")


class TestThePlan:
    INDEX = {"typedname": {"777": "Users tab"}, "sam": {"1": "Users tab", "2": "database"}}

    def plan(self, sheet_rows, db_rows=(), names=None):
        return fix.plan_hub(sheet_rows, list(db_rows), names or {"ev1": "Match"}, self.INDEX)

    def test_numeric_rows_are_left_alone(self):
        plan = self.plan([(2, "ev1", "1"), (3, "ev1", "-1001")])
        assert plan.fixes == [] and plan.sheet_placeholders == 0 and plan.sheet_rows == 2

    def test_a_username_becomes_a_cell_fix_and_a_database_fix(self):
        plan = self.plan([(2, "ev1", "1"), (3, "ev1", "typedname")], [("ev1", "1"), ("ev1", "typedname")])
        assert [(f.kind, f.old, f.new, f.row) for f in plan.fixes] == [(fix.DB_ROW, "typedname", "777", 0), (fix.SHEET_CELL, "typedname", "777", 3)]

    def test_a_database_row_of_an_open_event_is_fixed_though_it_is_not_in_the_sheet_yet(self):
        plan = self.plan([], [("ev1", "typedname")])
        assert [f.kind for f in plan.fixes] == [fix.DB_ROW]

    def test_the_same_name_in_two_events_is_two_fixes(self):
        plan = self.plan([(2, "ev1", "typedname"), (3, "ev2", "typedname")], [], {"ev1": "A", "ev2": "B"})
        assert [(f.event_id, f.row) for f in plan.fixes] == [("ev1", 2), ("ev2", 3)]

    def test_unknown_ambiguous_and_empty_are_listed_not_touched(self):
        plan = self.plan([(2, "ev1", "stranger"), (3, "ev1", "sam"), (4, "ev1", "")])
        assert plan.fixes == [] and [u[2] for u in plan.unknown] == ["stranger"]
        assert plan.ambiguous[0][2:] == ("sam", ["1", "2"]) and plan.empty == [("ev1", 4)]

    def test_when_the_id_is_already_in_the_event_it_is_a_conflict_not_a_merge(self):
        plan = self.plan([(2, "ev1", "777"), (3, "ev1", "typedname")])
        assert plan.fixes == [] and plan.conflicts[0][2:] == ("typedname", "777")

    def test_the_same_goes_for_an_id_that_is_only_in_the_database(self):
        plan = self.plan([(3, "ev1", "typedname")], [("ev1", "777")])
        assert plan.fixes == [] and len(plan.conflicts) == 1

    def test_the_same_id_in_another_event_is_no_conflict(self):
        plan = self.plan([(2, "ev2", "777"), (3, "ev1", "typedname")], names={"ev1": "A", "ev2": "B"})
        assert len(plan.fixes) == 1


class TestAgainstASheet:
    def event(self, db_path, **people):
        seed(db_path); person(db_path, 1, "alice"); person(db_path, "typedname", "typedname")

    async def test_the_dry_run_changes_nothing(self, db_path):
        self.event(db_path)
        book = book_with(users=[("777", "TypedName", "")], eventusers=[["ev1", "1"], ["ev1", "typedname"]])
        before = [list(r) for r in book.tabs["EventUsers"].rows]
        plan, results, left = await run(book, db_path)
        assert len(plan.fixes) == 2 and results == [] and left is None
        assert book.tabs["EventUsers"].rows == before and db_ids(db_path) == ["1", "typedname"]

    async def test_applied_the_sheet_and_the_database_both_get_the_id_and_the_audit_agrees(self, db_path):
        self.event(db_path)
        book = book_with(users=[("777", "TypedName", "")], eventusers=[["ev1", "1"], ["ev1", "typedname"]],
                         events=[["ev1", "Match", "01.10.2026 12:00:00.000", "42", "", "d", "CLOSED", 2]])
        plan, results, left = await run(book, db_path, apply=True)
        assert sorted(book.roster()) == ["1", "777"] and db_ids(db_path) == ["1", "777"] and left == 0
        assert [n for _, n in results] == ["database updated", "cell updated"]
        with patched(book):
            findings, _ = await audit.audit_hub(HUB, db_path, None)
        assert [f for f in findings if f.severity == audit.PROBLEM] == [] and not any(f.code == "NO_REAL_ID" for f in findings)

    async def test_running_it_again_has_nothing_left_to_do(self, db_path):
        self.event(db_path)
        book = book_with(users=[("777", "typedname", "")], eventusers=[["ev1", "1"], ["ev1", "typedname"]])
        await run(book, db_path, apply=True)
        plan, _, _ = await run(book, db_path, apply=True)
        assert plan.fixes == [] and plan.sheet_placeholders == 0

    async def test_a_name_the_users_tab_does_not_know_is_found_in_the_database_of_a_monitored_chat(self, db_path):
        self.event(db_path)
        sql(db_path, "INSERT INTO sub_chats (chat_id, owner_chat_id, is_monitored) VALUES ('-300', ?, 1)", HUB)
        sql(db_path, "INSERT INTO main_group_users (chat_id, username, user_id) VALUES ('-300', 'typedname', '888')")
        book = book_with(eventusers=[["ev1", "typedname"]])
        plan, _, _ = await run(book, db_path, apply=True)
        assert sorted(book.roster()) == ["888"] and plan.fixes[0].source == "database"

    async def test_and_in_a_chat_the_event_was_shared_to(self, db_path):
        self.event(db_path)
        sql(db_path, "INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type) VALUES ('ev1', '-200', '5', '-visible', 'channel')")
        sql(db_path, "INSERT INTO main_group_users (chat_id, username, user_id) VALUES ('-200', 'typedname', '555')")
        plan, _, _ = await run(book_with(eventusers=[["ev1", "typedname"]]), db_path)
        assert [f.new for f in plan.fixes if f.kind == fix.SHEET_CELL] == ["555"]

    async def test_somebody_tracked_only_in_an_unrelated_chat_is_not_used(self, db_path):
        self.event(db_path)
        sql(db_path, "INSERT INTO main_group_users (chat_id, username, user_id) VALUES ('-999', 'typedname', '666')")
        plan, _, _ = await run(book_with(eventusers=[["ev1", "typedname"]]), db_path)
        assert plan.fixes == [] and len(plan.unknown) == 1

    async def test_another_hubs_events_are_never_touched(self, db_path):
        self.event(db_path)
        seed(db_path, event_id="other", chat="-999"); person(db_path, "typedname", "typedname", chat="-999", event_id="other")
        await run(book_with(users=[("777", "typedname", "")], eventusers=[["ev1", "typedname"]]), db_path, apply=True)
        assert db_ids(db_path, "other") == ["typedname"] and db_ids(db_path, "ev1") == ["1", "777"]

    async def test_one_failing_write_is_reported_and_the_rest_still_happen(self, db_path):
        seed(db_path); person(db_path, "aa", "aa"); person(db_path, "bb", "bb")
        book = book_with(users=[("1", "aa", ""), ("2", "bb", "")], eventusers=[["ev1", "aa"], ["ev1", "bb"]])
        real_update = book.tabs["EventUsers"].update

        async def flaky(rng, values):
            if rng == "B2":
                raise RuntimeError("quota exceeded")
            return await real_update(rng, values)
        book.tabs["EventUsers"].update = flaky
        _, results, left = await run(book, db_path, apply=True)
        assert [n for _, n in results if n.startswith("FAILED")] == ["FAILED: quota exceeded"]
        assert book.tabs["EventUsers"].rows[1:] == [["ev1", "aa"], ["ev1", "2"]] and left == 1

    async def test_the_pause_only_starts_after_the_first_batch_of_writes(self, db_path):
        seed(db_path)
        users, rows = [], []
        for n in range(fix.FAST_UPDATES + 3):
            person(db_path, f"n{n}", f"n{n}")
            users.append((str(100 + n), f"n{n}", "")); rows.append(["ev1", f"n{n}"])
        with patch("scripts.fix_eventusers_ids.asyncio.sleep", new=AsyncMock()) as pause:
            await run(book_with(users=users, eventusers=rows), db_path, apply=True)
        assert pause.await_count == 3

    async def test_a_hub_without_a_sheet_is_skipped_and_a_missing_tab_is_an_error(self, db_path):
        with patch("scripts.fix_eventusers_ids.get_sheet_for_chat", new_callable=AsyncMock, return_value=None), \
             patch("scripts.fix_eventusers_ids.open_spreadsheet", new_callable=AsyncMock, return_value=None):
            assert await fix.fix_hub(HUB, db_path) == (None, None, None)
        book = book_with()
        del book.tabs["EventUsers"]
        try:
            await run(book, db_path)
        except RuntimeError as e:
            assert "no EventUsers tab" in str(e)
        else:
            raise AssertionError("expected an error")

    async def test_the_unique_attendees_count_stops_counting_one_person_twice(self, db_path):
        """Typed in at one event, a real id at another: two 'people' before, one after."""
        seed(db_path, event_id="e1", created="01.10.2026 12:00:00.000"); seed(db_path, event_id="e2", created="02.10.2026 12:00:00.000")
        sql(db_path, "UPDATE events SET closed_date = created_date WHERE event_id IN ('e1', 'e2')")
        person(db_path, "typedname", "typedname", event_id="e1"); person(db_path, 777, "typedname", event_id="e2")
        before = handlers._count_unique_attendees(HUB, "all")
        book = book_with(users=[("777", "typedname", "")], eventusers=[["e1", "typedname"], ["e2", "777"]])
        await run(book, db_path, apply=True)
        assert (before, handlers._count_unique_attendees(HUB, "all")) == (2, 1)


class TestTheRunAndTheReport:
    def two_hubs(self, db_path):
        sql(db_path, "INSERT INTO all_groups (chat_id, chat_name, type, sheet_id) VALUES ('-100', 'Football', 'PRO', 'SHEET_NO_ACCESS')")
        sql(db_path, "INSERT INTO all_groups (chat_id, chat_name, type, sheet_id) VALUES ('-200', 'Chess', 'PRO', 'SHEET_OK')")
        seed(db_path, event_id="ok1", chat="-200", status=2); person(db_path, "typedname", "typedname", chat="-200", event_id="ok1")

    def patched_two(self, error):
        book = book_with(users=[("777", "typedname", "")], eventusers=[["ok1", "typedname"]])
        book.tabs["Users"].rows[1][4] = "-200"

        async def open_sheet(target):
            if target == "-100":
                raise error
            return book
        stack = ExitStack()
        for module in ("scripts.fix_eventusers_ids", "scripts.audit_event_users", "sheets"):
            stack.enter_context(patch(f"{module}.get_sheet_for_chat", new=AsyncMock(side_effect=lambda chat: chat)))
            stack.enter_context(patch(f"{module}.open_spreadsheet", new=AsyncMock(side_effect=open_sheet)))
        return stack, book

    async def test_a_hub_that_cannot_be_opened_is_reported_and_the_others_are_still_fixed(self, db_path, capsys):
        self.two_hubs(db_path)
        stack, book = self.patched_two(Exception("{'code': 403, 'status': 'PERMISSION_DENIED'}"))
        with stack:
            results = await fix._run(db_path=db_path, apply=True)
            wrong = fix._print_report(results, apply=True)
        out = capsys.readouterr().out
        assert wrong == 1 and "COULD NOT BE CHECKED" in out and "Football" in out and "SHEET_NO_ACCESS" in out
        assert book.roster("ok1") == ["777"]

    async def test_the_report_of_a_dry_run_lists_every_change_and_says_nothing_was_changed(self, db_path, capsys):
        seed(db_path); person(db_path, "typedname", "typedname"); person(db_path, "stranger", "stranger")
        book = book_with(users=[("777", "typedname", "")], eventusers=[["ev1", "typedname"], ["ev1", "stranger"]])
        with patched(book):
            wrong = fix._print_report({HUB: await fix.fix_hub(HUB, db_path)}, apply=False)
        out = capsys.readouterr().out
        assert wrong == 0 and "typedname -> 777" in out and "[Users tab]" in out
        assert "no id for this person" in out and "stranger" in out and "nothing was changed" in out and "copy database.db first" in out

    async def test_a_clean_hub_says_so(self, capsys):
        assert fix._print_report({HUB: (fix.Plan(sheet_rows=3), [], None)}, apply=False) == 0
        assert "Nothing to fix." in capsys.readouterr().out
