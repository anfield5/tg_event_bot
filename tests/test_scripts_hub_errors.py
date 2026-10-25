"""
A hub whose Google Sheet cannot be opened (the bot's service account has no access: 403 PERMISSION_DENIED - the real
failure that crashed the whole audit run) must be REPORTED, naming the hub and the sheet, and must not stop the others.
"""
import json
import sqlite3
import sys
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

sys.path.insert(0, ".")
import scripts.audit_event_users as audit
import scripts.repair_event_users as repair
from tests.test_audit_event_users import seed, person
from tests.test_eventusers_lifecycle import Book

DENIED = Exception("{'code': 403, 'message': 'The caller does not have permission', 'status': 'PERMISSION_DENIED'}")
GONE = Exception("{'code': 404, 'message': 'Requested entity was not found.', 'status': 'NOT_FOUND'}")


def two_hubs(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO all_groups (chat_id, chat_name, type, sheet_id) VALUES ('-100', 'Football', 'PRO', 'SHEET_NO_ACCESS')")
    conn.execute("INSERT INTO all_groups (chat_id, chat_name, type, sheet_id) VALUES ('-200', 'Chess', 'PRO', 'SHEET_OK')")
    conn.commit()
    conn.close()
    seed(db_path, event_id="ok1", chat="-200", status=2)
    person(db_path, 1, "alice", chat="-200", event_id="ok1")


def patched(error):
    book = Book()
    book.tabs["Events"].rows[1:] = [["ok1", "Match", "c", "42", "", "d", "CLOSED", 1]]
    book.tabs["EventUsers"].rows += [["ok1", "1"]]

    async def open_sheet(target):
        if target == "-100":
            raise error
        return book

    stack = ExitStack()
    for module in ("scripts.audit_event_users", "scripts.repair_event_users", "sheets"):
        stack.enter_context(patch(f"{module}.get_sheet_for_chat", new=AsyncMock(side_effect=lambda chat: chat)))
        stack.enter_context(patch(f"{module}.open_spreadsheet", new=AsyncMock(side_effect=open_sheet)))
    return stack


class TestTheAudit:
    async def test_the_other_hubs_are_still_audited(self, db_path):
        two_hubs(db_path)
        with patched(DENIED):
            results = await audit._run(db_path=db_path, days=None)
        assert set(results) == {"-100", "-200"}
        assert results["-100"][0] is None and "error" in results["-100"][1]
        assert results["-200"][0] == [] and results["-200"][1]["closed"] == 1

    async def test_the_report_names_the_hub_the_sheet_and_what_to_do(self, db_path, capsys):
        two_hubs(db_path)
        with patched(DENIED):
            problems = audit._print_report(await audit._run(db_path=db_path, days=None), None)
        out = capsys.readouterr().out
        assert problems == 1, "an unreadable hub counts as a problem"
        assert "COULD NOT BE AUDITED" in out and "Football" in out and "SHEET_NO_ACCESS" in out
        assert "service account" in out and "share it" in out
        assert "Hub -200" in out and "No problems" not in out

    async def test_a_deleted_sheet_gets_its_own_hint(self, db_path, capsys):
        two_hubs(db_path)
        with patched(GONE):
            audit._print_report(await audit._run(db_path=db_path, days=None), None)
        assert "/setsheet" in capsys.readouterr().out

    async def test_json_and_the_exit_status_carry_it_too(self, db_path):
        two_hubs(db_path)
        with patched(DENIED):
            results = await audit._run(db_path=db_path, days=None)
        data = json.loads(json.dumps(audit.results_to_dict(results)))
        assert "PERMISSION_DENIED" in data["-100"]["error"] and data["-200"]["findings"] == []
        assert audit.has_problems(results) is True

    async def test_a_hub_without_a_sheet_is_still_just_skipped(self, db_path):
        two_hubs(db_path)
        with patch("scripts.audit_event_users.get_sheet_for_chat", new=AsyncMock(return_value=None)), \
             patch("scripts.audit_event_users.open_spreadsheet", new=AsyncMock(return_value=None)):
            results = await audit._run(db_path=db_path, days=None)
        assert audit.has_problems(results) is False and audit.results_to_dict(results)["-100"] is None


class TestTheRepair:
    async def test_the_other_hubs_are_still_handled_and_the_failure_is_reported(self, db_path, capsys):
        two_hubs(db_path)
        with patched(DENIED):
            results = await repair._run(db_path=db_path, days=None)
            wrong = repair._print_report(results, apply=False)
        out = capsys.readouterr().out
        assert wrong == 1 and "COULD NOT BE REPAIRED" in out and "Football" in out and "SHEET_NO_ACCESS" in out
        assert results["-200"][0] == [], "the healthy hub was looked at and needs nothing"

    async def test_nothing_is_written_to_the_failing_hub_or_anywhere_in_a_dry_run(self, db_path):
        two_hubs(db_path)
        with patched(DENIED):
            results = await repair._run(db_path=db_path, days=None)
        assert results["-100"][1] and results["-100"][2] is None
