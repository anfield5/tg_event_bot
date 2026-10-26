"""The Events tab's amount: how it is defined (one function), how scripts/check_events_amount.py checks it, and that the
number written at Save & Close is the one the post shows."""
import sqlite3
import sys
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

sys.path.insert(0, ".")
import db as db_module
import scripts.audit_event_users as audit
import scripts.check_events_amount as check
from tests.test_audit_event_users import HUB, CHILD, hub_post, person, roster_of, seed, share
from tests.test_eventusers_lifecycle import Book, World


def closed(db_path, event_id="ev1", status=2, **kwargs):
    seed(db_path, event_id=event_id, status=status, **kwargs)


def book_with(*events, header=None):
    book = Book()
    if header:
        book.tabs["Events"].rows[0] = list(header)
    book.tabs["Events"].rows[1:] = [list(r) for r in events]
    return book


def row(event_id="ev1", amount=0, status="CLOSED"):
    return [event_id, "Match", "01.10.2026 12:00:00.000", "42", "", "d", status, amount]


def patched(book):
    stack = ExitStack()
    for module in ("scripts.check_events_amount", "scripts.audit_event_users", "sheets"):
        stack.enter_context(patch(f"{module}.get_sheet_for_chat", new_callable=AsyncMock, return_value="sid"))
        stack.enter_context(patch(f"{module}.open_spreadsheet", new_callable=AsyncMock, return_value=book))
    return stack


async def run(book, db_path, apply=False, cutoff=None):
    with patched(book):
        return await check.check_hub(HUB, db_path, cutoff, apply=apply)


def codes(result):
    return sorted(f.code for f in result.findings)


class TestTheDefinition:
    def test_going_people_plus_every_guest(self):
        assert db_module.headcount_from_rows([("going", 2), ("going", 0), ("notgoing", 1), ("kicked", 3), ("notselected", 1)]) == {
            "going": 2, "guests": 7, "guests_of_not_going": 5, "total": 9}

    def test_nothing_is_nothing(self):
        assert db_module.headcount_from_rows([])["total"] == 0 and db_module.headcount_from_rows([("going", None)])["total"] == 1

    def test_an_event_is_counted_across_all_its_chats(self, db_path):
        closed(db_path); person(db_path, 1, "alice", guests=2); person(db_path, 2, "bob", chat=CHILD, guests=1)
        assert db_module.event_headcount("ev1", db_path)["total"] == 5


class TestOneEvent:
    def _check(self, db_path, tab_row, status=2):
        return check.check_event({"event_id": "ev1", "name": "Match", "status": status}, roster_of(db_path), tab_row)

    def test_a_right_amount_is_no_finding(self, db_path):
        closed(db_path); person(db_path, 1, "alice", guests=1)
        assert self._check(db_path, {"AMOUNT": 2}) == ([], None)

    def test_a_wrong_amount_shows_both_numbers_and_how_the_database_one_is_made(self, db_path):
        closed(db_path); person(db_path, 1, "alice", guests=1); person(db_path, 2, "bob")
        findings, fix = self._check(db_path, {"AMOUNT": "9"})
        assert fix == 3 and [f.code for f in findings] == ["AMOUNT_WRONG"]
        assert "tab says 9" in findings[0].detail and "gives 3" in findings[0].detail and "2 going + 1 guest" in findings[0].detail

    def test_an_empty_or_text_cell(self, db_path):
        closed(db_path); person(db_path, 1, "alice")
        for raw in ("", "n/a"):
            findings, fix = self._check(db_path, {"AMOUNT": raw})
            assert [f.code for f in findings] == ["AMOUNT_NOT_A_NUMBER"] and fix == 1

    def test_no_row_at_all(self, db_path):
        closed(db_path); person(db_path, 1, "alice")
        findings, fix = self._check(db_path, None)
        assert [f.code for f in findings] == ["EVENT_NOT_IN_TAB"] and fix is None

    def test_an_open_or_canceled_event_should_say_zero(self, db_path):
        closed(db_path, status=0); person(db_path, 1, "alice")
        assert self._check(db_path, {"AMOUNT": 0}, status=0) == ([], None)
        findings, fix = self._check(db_path, {"AMOUNT": 1}, status=0)
        assert fix == 0 and "OPEN" in findings[0].detail
        assert self._check(db_path, {"AMOUNT": 0}, status=-1) == ([], None)

    def test_guests_of_people_who_are_not_going_are_counted_and_pointed_out(self, db_path):
        closed(db_path); person(db_path, 1, "alice"); person(db_path, 2, "bob", status="notgoing", guests=2)
        findings, fix = self._check(db_path, {"AMOUNT": 3})
        assert fix is None and [(f.code, f.severity) for f in findings] == [("AMOUNT_INCLUDES_GUESTS_OF_NOT_GOING", audit.INFO)]


class TestAgainstASheet:
    async def test_every_amount_right(self, db_path):
        closed(db_path); person(db_path, 1, "alice", guests=1)
        result = await run(book_with(row(amount=2)), db_path)
        assert result.findings == [] and (result.checked, result.correct) == (1, 1)

    async def test_the_dry_run_reports_and_changes_nothing(self, db_path):
        closed(db_path); person(db_path, 1, "alice")
        book = book_with(row(amount=9))
        before = [list(r) for r in book.tabs["Events"].rows]
        result = await run(book, db_path)
        assert codes(result) == ["AMOUNT_WRONG"] and len(result.fixes) == 1 and book.tabs["Events"].rows == before

    async def test_applied_only_the_amount_cell_of_the_wrong_rows_is_written(self, db_path):
        closed(db_path); person(db_path, 1, "alice", guests=1)
        closed(db_path, "ev2"); person(db_path, 2, "bob", event_id="ev2")
        book = book_with(row("ev1", 9), row("ev2", 1))
        result = await run(book, db_path, apply=True)
        assert book.tabs["Events"].rows[1] == row("ev1", 2) and book.tabs["Events"].rows[2] == row("ev2", 1)
        assert [f.code for f in result.findings if f.code.startswith("AMOUNT_FIX")] == ["AMOUNT_FIXED"]

    async def test_a_second_run_finds_nothing_to_do(self, db_path):
        closed(db_path); person(db_path, 1, "alice")
        book = book_with(row(amount=9))
        await run(book, db_path, apply=True)
        again = await run(book, db_path, apply=True)
        assert again.fixes == [] and again.correct == 1

    async def test_the_header_names_do_not_matter_AMOUNT_and_CREATED_AT_as_in_the_docs(self, db_path):
        """The first version read the column by the name GOING_COUNT and called every row's amount 'not a number'."""
        closed(db_path); person(db_path, 1, "alice", guests=1)
        header = ["EVENT_ID", "EVENT_NAME", "CREATED_AT", "CREATED_BY", "EVENT_DATE", "CLOSED_AT", "STATUS", "AMOUNT"]
        book = book_with(row(amount=2), header=header)
        assert (await run(book, db_path)).findings == []
        book = book_with(row(amount=7), header=header)
        assert codes(await run(book, db_path)) == ["AMOUNT_WRONG"]

    async def test_the_audit_and_the_repair_do_not_depend_on_the_header_either(self, db_path):
        import scripts.repair_event_users as repair
        closed(db_path); person(db_path, 1, "alice")
        header = ["EVENT_ID", "EVENT_NAME", "CREATED_AT", "CREATED_BY", "EVENT_DATE", "CLOSED_AT", "STATUS", "AMOUNT"]
        book = book_with(row(amount=1), header=header)
        book.tabs["EventUsers"].rows += [["ev1", "1"]]
        with patched(book):
            findings, _ = await audit.audit_hub(HUB, db_path, None)
            assert [f for f in findings if f.severity == audit.PROBLEM] == []
        book = book_with(row(amount=5), header=header)
        book.tabs["EventUsers"].rows += [["ev1", "1"]]
        with patched(book), patch("scripts.repair_event_users.get_sheet_for_chat", new=AsyncMock(return_value="sid")), \
                patch("scripts.repair_event_users.open_spreadsheet", new=AsyncMock(return_value=book)):
            plan, _, left = await repair.repair_hub(HUB, db_path, None, apply=True)
        assert [s.kind for s in plan] == [repair.FIX_ROW] and book.tabs["Events"].rows[1][7] == 1 and left == 0

    async def test_a_missing_row_and_a_duplicated_row(self, db_path):
        closed(db_path); person(db_path, 1, "alice")
        closed(db_path, "ev2"); person(db_path, 2, "bob", event_id="ev2")
        result = await run(book_with(row("ev2", 1), row("ev2", 1)), db_path)
        assert codes(result) == ["EVENTS_ROW_DUPLICATE", "EVENT_NOT_IN_TAB"]

    async def test_only_the_first_of_two_rows_is_corrected_because_that_is_the_one_the_bot_uses(self, db_path):
        closed(db_path); person(db_path, 1, "alice")
        book = book_with(row("ev1", 9), row("ev1", 9))
        await run(book, db_path, apply=True)
        assert [r[7] for r in book.tabs["Events"].rows[1:]] == [1, 9]

    async def test_the_time_window(self, db_path):
        from datetime import datetime
        closed(db_path, created="01.01.2020 12:00:00.000"); person(db_path, 1, "alice")
        assert (await run(book_with(row(amount=9)), db_path, cutoff=datetime(2026, 1, 1))).checked == 0
        assert (await run(book_with(row(amount=9)), db_path, cutoff=None)).checked == 1

    async def test_an_event_closed_before_event_users_existed_is_checked_by_its_frozen_list(self, db_path):
        import json
        closed(db_path, going=json.dumps(["alice (1)", "bob (2)"]))
        assert (await run(book_with(row(amount=2)), db_path)).findings == []
        assert codes(await run(book_with(row(amount=3)), db_path)) == ["AMOUNT_WRONG"]

    async def test_a_failed_write_is_a_problem_and_the_rest_still_happen(self, db_path):
        closed(db_path); person(db_path, 1, "alice")
        closed(db_path, "ev2"); person(db_path, 2, "bob", event_id="ev2")
        book = book_with(row("ev1", 9), row("ev2", 9))
        real = book.tabs["Events"].update

        async def flaky(rng, values):
            if rng == "H2":
                raise RuntimeError("quota exceeded")
            return await real(rng, values)
        book.tabs["Events"].update = flaky
        result = await run(book, db_path, apply=True)
        assert "AMOUNT_FIX_FAILED" in codes(result) and book.tabs["Events"].rows[2][7] == 1

    async def test_the_pause_only_starts_after_the_first_batch(self, db_path):
        rows = []
        for n in range(check.FAST_UPDATES + 2):
            closed(db_path, f"e{n}"); person(db_path, n + 1, f"u{n}", event_id=f"e{n}"); rows.append(row(f"e{n}", 9))
        with patch("scripts.check_events_amount.asyncio.sleep", new=AsyncMock()) as pause:
            await run(book_with(*rows), db_path, apply=True)
        assert pause.await_count == 2


class TestTheRunAndTheReport:
    def two_hubs(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO all_groups (chat_id, chat_name, type, sheet_id) VALUES ('-100', 'Football', 'PRO', 'SHEET_NO_ACCESS')")
        conn.execute("INSERT INTO all_groups (chat_id, chat_name, type, sheet_id) VALUES ('-200', 'Chess', 'PRO', 'SHEET_OK')")
        conn.commit()
        conn.close()
        seed(db_path, event_id="ok1", chat="-200", status=2); person(db_path, 1, "alice", chat="-200", event_id="ok1")

    async def test_an_unreadable_hub_is_reported_and_the_other_is_still_checked(self, db_path, capsys):
        self.two_hubs(db_path)
        book = book_with(row("ok1", 9))

        async def open_sheet(target):
            if target == "-100":
                raise Exception("{'code': 403, 'status': 'PERMISSION_DENIED'}")
            return book
        with ExitStack() as stack:
            for module in ("scripts.check_events_amount", "scripts.audit_event_users", "sheets"):
                stack.enter_context(patch(f"{module}.get_sheet_for_chat", new=AsyncMock(side_effect=lambda chat: chat)))
                stack.enter_context(patch(f"{module}.open_spreadsheet", new=AsyncMock(side_effect=open_sheet)))
            problems = check._print_report(await check._run(db_path=db_path), apply=False)
        out = capsys.readouterr().out
        assert "COULD NOT BE CHECKED" in out and "Football" in out and "AMOUNT_WRONG" in out and problems == 2

    async def test_the_report_of_a_dry_run_says_how_many_can_be_corrected(self, db_path, capsys):
        closed(db_path); person(db_path, 1, "alice")
        with patched(book_with(row(amount=9))):
            check._print_report({HUB: await check.check_hub(HUB, db_path, None)}, apply=False)
        out = capsys.readouterr().out
        assert "1 amount(s) can be corrected" in out and "nothing was changed" in out and "--apply" in out

    async def test_after_applying_nothing_is_left_standing(self, db_path, capsys):
        closed(db_path); person(db_path, 1, "alice")
        with patched(book_with(row(amount=9))):
            problems = check._print_report({HUB: await check.check_hub(HUB, db_path, None, apply=True)}, apply=True)
        assert problems == 0 and "AMOUNT_FIXED" in capsys.readouterr().out

    def test_a_clean_run_says_so(self, capsys):
        assert check._print_report({HUB: check.Result(checked=3, correct=3)}, apply=False) == 0
        assert "All amounts are right." in capsys.readouterr().out


class TestTheNumberTheBotWrites:
    async def test_save_and_close_writes_the_same_number_the_database_formula_gives(self, db_path):
        w = World(db_path)
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE event_users SET guests = 2 WHERE user_id = '1'")                    # alice brings two
        conn.execute("UPDATE event_users SET status = 'notgoing', guests = 1 WHERE user_id = '2'")   # bob is out but his guest stays
        conn.commit()
        conn.close()
        await w.press("close_ev1")
        await w.press("save_ev1")
        assert w.book.events_row()[7] == db_module.event_headcount("ev1", db_path)["total"] == 4

    async def test_the_post_shows_the_same_total_in_every_state(self, db_path):
        """One definition, several places that must agree: the TOTAL a post prints (rendered by the real code, read back
        the way Telegram returns it) against db.headcount_from_rows - going people with guests, a person who clicked
        Not Going and kept a guest, someone who only ever added guests, a kicked person's guest, other chats."""
        closed(db_path); share(db_path)
        person(db_path, 1, "alice", guests=2)
        person(db_path, 2, "bob", status="notgoing", guests=1)
        person(db_path, 3, "carol", status="notselected", guests=1)
        person(db_path, 4, "dave", status="kicked", guests=1)
        person(db_path, 5, "erin", chat=CHILD, guests=1)
        person(db_path, 6, "frank", chat="-400")                 # a monitored chat the event was never shared to
        parsed = audit.parse_post(*await hub_post(db_path))
        # alice 1+2, bob 1, carol 1, dave 1, erin 1+1, frank 1
        assert parsed["total"] == db_module.event_headcount("ev1", db_path)["total"] == 9
