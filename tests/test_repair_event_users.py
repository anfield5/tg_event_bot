"""scripts/repair_event_users.py - makes the Sheet say what the DB says; a dry run unless told to apply."""
import sqlite3
import sys
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

sys.path.insert(0, ".")
import scripts.repair_event_users as repair
from tests.test_audit_event_users import CHILD, HUB, person, seed
from tests.test_eventusers_lifecycle import Book, World, first_close

CLOSED_AT = "01.10.2026 20:00:00.000"


def closed_event(db_path, event_id="ev1", status=2, **kwargs):
    seed(db_path, event_id=event_id, status=status, **kwargs)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE events SET closed_date = ?, created_by_user_id = '42' WHERE event_id = ?", (CLOSED_AT, event_id))
    conn.commit()
    conn.close()


def book_with(*, events=(), users=()):
    book = Book()
    book.tabs["Events"].rows[1:] = [list(r) for r in events]
    book.tabs["EventUsers"].rows += [list(r) for r in users]
    return book


def good_events_row(event_id="ev1", count=2, status="CLOSED"):
    return [event_id, "Match", "01.10.2026 12:00:00.000", "42", "", CLOSED_AT, status, count]


def patched(book):
    stack = ExitStack()
    for module in ("scripts.repair_event_users", "scripts.audit_event_users", "sheets"):
        stack.enter_context(patch(f"{module}.get_sheet_for_chat", new_callable=AsyncMock, return_value="sid"))
        stack.enter_context(patch(f"{module}.open_spreadsheet", new_callable=AsyncMock, return_value=book))
    return stack


async def run(book, db_path, apply=False, resolve_ids=False, cutoff=None, chat=HUB):
    with patched(book):
        return await repair.repair_hub(chat, db_path, cutoff, apply=apply, resolve_ids=resolve_ids)


def kinds(plan):
    return sorted(s.kind for s in plan)


def snapshot(book):
    return {name: [list(r) for r in tab.rows] for name, tab in book.tabs.items()}


class TestThePlan:
    async def test_a_consistent_hub_needs_nothing(self, db_path):
        closed_event(db_path); person(db_path, 1, "alice"); person(db_path, 2, "bob")
        plan, _, _ = await run(book_with(events=[good_events_row()], users=[["ev1", "1"], ["ev1", "2"]]), db_path)
        assert plan == []

    async def test_the_default_is_a_dry_run_that_changes_nothing(self, db_path):
        closed_event(db_path); person(db_path, 1, "alice"); person(db_path, 2, "bob")
        book = book_with(events=[good_events_row()], users=[["ev1", "1"]])
        before = snapshot(book)
        plan, results, left = await run(book, db_path)
        assert kinds(plan) == [repair.REWRITE] and results == [] and left is None
        assert snapshot(book) == before

    async def test_a_hub_without_a_sheet_is_skipped(self, db_path):
        with patch("scripts.repair_event_users.get_sheet_for_chat", new_callable=AsyncMock, return_value=None), \
             patch("scripts.repair_event_users.open_spreadsheet", new_callable=AsyncMock, return_value=None):
            assert await repair.repair_hub(HUB, db_path, None) == (None, None, None)

    async def test_events_outside_the_window_are_left_alone(self, db_path):
        from datetime import datetime
        closed_event(db_path, created="01.01.2020 12:00:00.000"); person(db_path, 1, "alice")
        plan, _, _ = await run(book_with(events=[good_events_row(count=1)]), db_path, cutoff=datetime(2026, 1, 1))
        assert plan == []


class TestWhatItFixes:
    async def test_a_person_missing_from_the_sheet_is_added_by_replacing_the_roster(self, db_path):
        closed_event(db_path); person(db_path, 1, "alice"); person(db_path, 2, "bob", chat=CHILD)
        book = book_with(events=[good_events_row()], users=[["ev1", "1"]])
        plan, results, left = await run(book, db_path, apply=True)
        assert kinds(plan) == [repair.REWRITE] and results[0][1] == "2 row(s) written"
        assert sorted(book.roster()) == ["1", "2"] and left == 0

    async def test_a_doubled_roster_and_a_stranger_are_cleaned_up(self, db_path):
        closed_event(db_path); person(db_path, 1, "alice")
        book = book_with(events=[good_events_row(count=1)], users=[["ev1", "1"], ["ev1", "1"], ["ev1", "9"]])
        _, _, left = await run(book, db_path, apply=True)
        assert book.roster() == ["1"] and left == 0

    async def test_an_empty_tab_is_filled(self, db_path):
        closed_event(db_path); person(db_path, 1, "alice")
        book = book_with(events=[good_events_row(count=1)])
        await run(book, db_path, apply=True)
        assert book.roster() == ["1"]

    async def test_rows_of_an_event_that_is_open_again_are_removed_and_its_status_fixed(self, db_path):
        closed_event(db_path, status=0); person(db_path, 1, "alice")
        book = book_with(events=[good_events_row(status="CLOSED")], users=[["ev1", "1"]])
        plan, _, left = await run(book, db_path, apply=True)
        assert kinds(plan) == [repair.FIX_ROW, repair.REMOVE] and book.roster() == []
        assert book.events_row()[5:] == ["", "OPEN", 0] and left == 0

    async def test_a_missing_events_row_is_added_the_way_the_bot_writes_it(self, db_path):
        closed_event(db_path); person(db_path, 1, "alice", guests=2)
        book = book_with(users=[["ev1", "1"]])
        plan, _, left = await run(book, db_path, apply=True)
        assert kinds(plan) == [repair.ADD_ROW]
        assert book.tabs["Events"].rows[1] == ["ev1", "Match", "01.10.2026 12:00:00.000", "42", "", CLOSED_AT, "CLOSED", 3]
        assert left == 0

    async def test_a_wrong_count_or_status_is_corrected_in_place(self, db_path):
        closed_event(db_path); person(db_path, 1, "alice", guests=1)
        book = book_with(events=[good_events_row(count=99, status="OPEN")], users=[["ev1", "1"]])
        plan, _, left = await run(book, db_path, apply=True)
        assert kinds(plan) == [repair.FIX_ROW] and book.events_row()[5:] == [CLOSED_AT, "CLOSED", 2] and left == 0

    async def test_a_canceled_event_is_written_as_canceled(self, db_path):
        closed_event(db_path, status=-1); person(db_path, 1, "alice")
        book = book_with(events=[good_events_row(status="OPEN")])
        await run(book, db_path, apply=True)
        assert book.events_row()[5:] == [CLOSED_AT, "CANCELED", 0]

    async def test_a_missing_eventusers_tab_still_gets_the_events_row_repaired(self, db_path):
        closed_event(db_path); person(db_path, 1, "alice")
        book = book_with(events=[good_events_row(count=7)])
        del book.tabs["EventUsers"]
        plan, _, _ = await run(book, db_path)
        assert kinds(plan) == [repair.FIX_ROW], "it cannot write rows to a tab that is not there - the audit reports that"

    async def test_other_events_the_actions_tab_and_other_rows_are_never_touched(self, db_path):
        closed_event(db_path); person(db_path, 1, "alice")
        closed_event(db_path, "ev2"); person(db_path, 7, "zed", event_id="ev2")
        book = book_with(events=[good_events_row(count=1), good_events_row("ev2", 1)],
                         users=[["ev1", "9"], ["ev2", "7"], ["ghost", "5"]])
        book.tabs["Actions"].rows.append(["ev1", "GOING", "alice", "1", "d", HUB])
        await run(book, db_path, apply=True)
        assert book.roster("ev1") == ["1"] and book.roster("ev2") == ["7"] and book.roster("ghost") == ["5"]
        assert book.tabs["Actions"].rows[1:] == [["ev1", "GOING", "alice", "1", "d", HUB]]

    async def test_running_it_twice_changes_nothing_the_second_time(self, db_path):
        closed_event(db_path); person(db_path, 1, "alice"); person(db_path, 2, "bob")
        book = book_with(events=[good_events_row(count=5)], users=[["ev1", "1"], ["ev1", "1"]])
        await run(book, db_path, apply=True)
        again, _, left = await run(book, db_path, apply=True)
        assert again == [] and left == 0


class TestTypedInPeople:
    def _known(self, db_path, username="typedname", user_id="777"):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO main_group_users (chat_id, username, user_id) VALUES (?, ?, ?)", (HUB, username, user_id))
        conn.commit()
        conn.close()

    def _ids(self, db_path):
        conn = sqlite3.connect(db_path)
        try:
            return sorted(r[0] for r in conn.execute("SELECT user_id FROM event_users WHERE event_id = 'ev1'"))
        finally:
            conn.close()

    async def test_without_the_flag_the_database_is_never_edited(self, db_path):
        closed_event(db_path); person(db_path, "typedname", "typedname"); self._known(db_path)
        book = book_with(events=[good_events_row(count=1)], users=[["ev1", "typedname"]])
        plan, _, _ = await run(book, db_path, apply=True)
        assert plan == [] and self._ids(db_path) == ["typedname"]

    async def test_with_the_flag_they_get_their_real_id_in_the_db_and_the_sheet(self, db_path):
        closed_event(db_path); person(db_path, 1, "alice"); person(db_path, "typedname", "typedname"); self._known(db_path)
        book = book_with(events=[good_events_row()], users=[["ev1", "1"], ["ev1", "typedname"]])
        plan, _, left = await run(book, db_path, apply=True, resolve_ids=True)
        assert kinds(plan) == [repair.RESOLVE, repair.REWRITE]
        assert self._ids(db_path) == ["1", "777"] and sorted(book.roster()) == ["1", "777"] and left == 0

    async def test_the_dry_run_shows_it_and_edits_nothing(self, db_path):
        closed_event(db_path); person(db_path, "typedname", "typedname"); self._known(db_path)
        book = book_with(events=[good_events_row(count=1)], users=[["ev1", "typedname"]])
        plan, _, _ = await run(book, db_path, resolve_ids=True)
        assert any("typedname -> 777" in s.detail for s in plan) and self._ids(db_path) == ["typedname"]

    async def test_someone_never_seen_stays_as_he_is(self, db_path):
        closed_event(db_path); person(db_path, "stranger", "stranger")
        book = book_with(events=[good_events_row(count=1)], users=[["ev1", "stranger"]])
        plan, _, _ = await run(book, db_path, apply=True, resolve_ids=True)
        assert plan == [] and self._ids(db_path) == ["stranger"]

    async def test_a_person_who_already_has_a_row_is_not_overwritten(self, db_path):
        closed_event(db_path); person(db_path, "typedname", "typedname"); person(db_path, "777", "typedname"); self._known(db_path)
        book = book_with(events=[good_events_row(count=2)], users=[["ev1", "typedname"], ["ev1", "777"]])
        _, results, _ = await run(book, db_path, apply=True, resolve_ids=True)
        assert any("already has a row" in note for _, note in results) and self._ids(db_path) == ["777", "typedname"]


class TestWhenSomethingFails:
    async def test_a_failing_step_is_reported_and_the_rest_still_run(self, db_path):
        closed_event(db_path); person(db_path, 1, "alice")
        closed_event(db_path, "ev2"); person(db_path, 2, "bob", event_id="ev2")
        book = book_with(events=[good_events_row(count=1), good_events_row("ev2", 1)])
        real = repair.sync_event_users_sheet

        async def flaky(chat_id, event_id, ids):
            if event_id == "ev1":
                raise RuntimeError("quota exceeded")
            return await real(chat_id, event_id, ids)
        with patch("scripts.repair_event_users.sync_event_users_sheet", new=flaky):
            _, results, left = await run(book, db_path, apply=True)
        assert [n for _, n in results if n.startswith("FAILED")] == ["FAILED: quota exceeded"]
        assert book.roster("ev2") == ["2"] and left == 1, "the audit afterwards still sees the one that failed"


class TestTheCommandLine:
    async def test_every_hub_with_a_sheet_and_no_other(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO all_groups (chat_id, type, sheet_id) VALUES ('-100', 'PRO', 'sid')")
        conn.execute("INSERT INTO all_groups (chat_id, type, sheet_id) VALUES ('-300', 'PRO', '')")
        conn.commit()
        conn.close()
        closed_event(db_path); person(db_path, 1, "alice")
        with patched(book_with(events=[good_events_row(count=1)], users=[["ev1", "1"]])):
            results = await repair._run(db_path=db_path, days=None)
        assert list(results) == ["-100"]

    async def test_the_report_of_a_dry_run_says_nothing_was_changed(self, db_path, capsys):
        closed_event(db_path); person(db_path, 1, "alice")
        book = book_with(events=[good_events_row(count=1)])
        with patched(book):
            wrong = repair._print_report({HUB: await repair.repair_hub(HUB, db_path, None)}, apply=False)
        out = capsys.readouterr().out
        assert wrong == 0 and "REWRITE_EVENTUSERS" in out and "nothing was changed" in out and "--apply" in out

    async def test_the_report_after_applying_says_how_it_went(self, db_path, capsys):
        closed_event(db_path); person(db_path, 1, "alice")
        book = book_with(events=[good_events_row(count=1)])
        with patched(book):
            wrong = repair._print_report({HUB: await repair.repair_hub(HUB, db_path, None, apply=True)}, apply=True)
        out = capsys.readouterr().out
        assert wrong == 0 and "1 row(s) written" in out and "0 problem(s) left" in out and "Done." in out

    def test_a_clean_hub_says_so(self, capsys):
        assert repair._print_report({HUB: ([], [], None)}, apply=False) == 0
        assert "Nothing to repair." in capsys.readouterr().out


class TestOnWhatTheRealFlowsLeaveBehind:
    async def test_a_row_lost_from_the_sheet_after_a_normal_close_is_restored(self, db_path):
        w = World(db_path)
        await first_close(w)
        w.book.tabs["EventUsers"].rows.pop()
        with patch("scripts.repair_event_users.get_sheet_for_chat", new_callable=AsyncMock, return_value="sid"), \
             patch("scripts.repair_event_users.open_spreadsheet", new_callable=AsyncMock, return_value=w.book), \
             patch("scripts.audit_event_users.get_sheet_for_chat", new_callable=AsyncMock, return_value="sid"), \
             patch("scripts.audit_event_users.open_spreadsheet", new_callable=AsyncMock, return_value=w.book):
            with w.patches():
                plan, _, left = await repair.repair_hub("-100123", db_path, None, apply=True)
        assert kinds(plan) == [repair.REWRITE] and sorted(w.book.roster()) == ["1", "3"] and left == 0
