"""
The EventUsers tab through an event's whole life, on a spreadsheet that REMEMBERS what is written to it, driven by the
real handlers: Verify -> Add Extra Member / Kick / Return -> Save & Close -> /reopen -> Verify again -> Save & Close.
The tab must always hold exactly the event's FINAL roster - once per person.
"""
import asyncio
import re
import sqlite3
import sys
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, ".")
import db as db_module
import event_engine
import handlers
import reopen
import sheets
from tests.helpers import make_bot, make_callback_update, make_chat, make_context, make_message, make_update, make_user
from tests.test_handlers_async import MAIN_CHAT, insert_event, insert_event_user
from utils import remember_extra_player_prompt

HUB, CHILD, OWNER = MAIN_CHAT, "-200", 555
ADMIN = make_user(user_id=9, username="admin")


class Tab:
    def __init__(self, sheet_id, header, rows=()):
        self.id, self.rows, self.fail_append = sheet_id, [list(header)] + [list(r) for r in rows], False

    async def col_values(self, col):
        return [r[col - 1] if len(r) >= col else "" for r in self.rows]

    async def get_all_values(self):
        return [list(r) for r in self.rows]

    async def get_all_records(self):
        header = self.rows[0]
        return [dict(zip(header, r + [""] * (len(header) - len(r)))) for r in self.rows[1:]]

    async def update(self, rng, values):
        m = re.match(r"([A-Z])(\d+)(?::[A-Z]\d+)?$", rng)
        start, row = ord(m.group(1)) - ord("A"), int(m.group(2))
        for offset, value in enumerate(values[0]):
            self.rows[row - 1][start + offset] = value

    async def append_row(self, row):
        self.rows.append(list(row))

    async def append_rows(self, rows):
        if self.fail_append:
            raise RuntimeError("quota exceeded")
        self.rows.extend(list(r) for r in rows)


class Book:
    def __init__(self):
        self.batch_calls = []
        self.tabs = {
            "Events": Tab(1, ["EVENT_ID", "EVENT_NAME", "CREATED_DATE", "CREATED_BY", "EVENT_DATE", "CLOSED_AT", "STATUS", "AMOUNT"],
                          [["ev1", "Match", "c", "42", "", "", "OPEN", 0]]),
            "EventUsers": Tab(2, ["EVENT_ID", "USER_ID"]),
            "Actions": Tab(3, ["EVENT_ID", "ACTION", "USER_NAME", "USER_ID", "DATE", "CHAT_ID"]),
        }

    async def worksheet(self, name):
        from gspread.exceptions import WorksheetNotFound
        if name not in self.tabs:
            raise WorksheetNotFound(name)
        return self.tabs[name]

    async def batch_update(self, body):
        self.batch_calls.append(body)
        for req in body["requests"]:
            r = req["deleteDimension"]["range"]
            tab = next(t for t in self.tabs.values() if t.id == r["sheetId"])
            del tab.rows[r["startIndex"]:r["endIndex"]]

    def roster(self, event_id="ev1"):
        return [r[1] for r in self.tabs["EventUsers"].rows[1:] if r[0] == event_id]

    def events_row(self):
        return self.tabs["Events"].rows[1]


class World:
    """One event, a spreadsheet that remembers, and helpers that press the real buttons."""

    def __init__(self, db_path):
        self.book, self.bot, self.tasks = Book(), make_bot(), []
        insert_event(db_path, event_id="ev1", chat_id=HUB, name="Match", event_status=0)
        insert_event_user(db_path, "ev1", HUB, "1", "alice", "going")
        insert_event_user(db_path, "ev1", CHILD, "2", "bob", "going")
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type) VALUES ('ev1', ?, '5', '-visible', 'channel')", (CHILD,))
        conn.execute("INSERT INTO main_group_users (chat_id, username, user_id) VALUES (?, 'carol', '3')", (CHILD,))
        conn.execute("INSERT INTO main_group_users (chat_id, username, user_id) VALUES (?, 'dave', '4')", (HUB,))
        conn.commit()
        conn.close()

    def context(self):
        ctx = make_context(bot=self.bot)
        ctx.chat_data = {}
        ctx.application.create_task = lambda coro: self.tasks.append(asyncio.ensure_future(coro))
        return ctx

    def patches(self):
        stack = ExitStack()
        for module in ("event_engine", "sheets", "handlers"):
            stack.enter_context(patch(f"{module}.get_sheet_for_chat", new_callable=AsyncMock, return_value="sid"))
            stack.enter_context(patch(f"{module}.open_spreadsheet", new_callable=AsyncMock, return_value=self.book))
        for module in ("event_engine", "handlers", "reopen"):
            stack.enter_context(patch(f"{module}.schedule_view_refresh", new=AsyncMock()))
        stack.enter_context(patch("subscription.OWNER_USER_IDS", {OWNER}))
        stack.enter_context(patch("event_engine.REOPEN_REFRESH_PAUSE", 0))
        return stack

    async def _settle(self):
        await asyncio.gather(*self.tasks)
        self.tasks.clear()

    async def press(self, data):
        ctx = self.context()
        with self.patches():
            await event_engine.button_handler(make_callback_update(data, chat_id=int(HUB), user=ADMIN), ctx)
            await self._settle()

    async def add_extra(self, name):
        ctx = self.context()
        remember_extra_player_prompt(ctx, "ev1", ADMIN)
        chat = make_chat(chat_id=int(HUB), chat_type="supergroup")
        msg = make_message(chat=chat)
        msg.text, msg.delete, msg.sender_chat = name, AsyncMock(), None
        with self.patches():
            await handlers.handle_extra_player_input(make_update(chat=chat, user=ADMIN, message=msg), ctx)
            await self._settle()

    async def reopen(self, cleanup=True):
        db_module.set_reopen_mode(HUB, True)
        query = MagicMock()
        query.data, query.answer = "reopen_ev1", AsyncMock()
        query.message = MagicMock()
        query.message.chat_id = int(HUB)
        query.from_user = make_user(user_id=OWNER, username="boss")
        upd = MagicMock()
        upd.callback_query = query
        ctx = self.context()
        with self.patches():
            if not cleanup:
                with patch("reopen.reopen_event_in_sheet", new=AsyncMock()):
                    await reopen.reopen_callback_handler(upd, ctx)
            else:
                await reopen.reopen_callback_handler(upd, ctx)
            await self._settle()
        return query


async def first_close(w):
    """Verify, add carol (a member of the shared child chat), kick bob, Save & Close."""
    await w.press("close_ev1")
    await w.add_extra("carol")
    await w.press("kick_ev1:ch-bob")
    await w.press("save_ev1")


class TestNormalFlow:
    async def test_people_added_during_verify_are_exported_and_kicked_ones_are_not(self, db_path):
        w = World(db_path)
        await first_close(w)
        assert sorted(w.book.roster()) == ["1", "3"], "alice (hub) + carol (added in verify); bob was kicked"
        assert w.book.events_row()[5:8][1:] == ["CLOSED", 2] and w.book.events_row()[5]

    async def test_reopen_empties_the_tab_for_that_event(self, db_path):
        w = World(db_path)
        await first_close(w)
        query = await w.reopen()
        assert w.book.roster() == [] and w.book.events_row()[5:] == ["", "OPEN", 0]
        assert "reopened" in query.answer.call_args.args[0]

    async def test_after_reopen_and_a_second_verify_the_tab_holds_exactly_the_new_roster(self, db_path):
        w = World(db_path)
        await first_close(w)
        await w.reopen()
        await w.press("close_ev1")                 # Verify again
        await w.press("return_ev1:ch-bob")         # bob comes back
        await w.add_extra("dave")                  # and someone new is added
        await w.press("save_ev1")
        assert sorted(w.book.roster()) == ["1", "2", "3", "4"]
        assert w.book.events_row()[6] == "CLOSED" and w.book.events_row()[7] == 4


class TestWhenTheCleanupDidNotHappen:
    async def test_a_second_save_still_ends_with_one_row_per_person(self, db_path):
        """e.g. the sheet was unreachable or not bound at the moment of the reopen: the old rows are still there."""
        w = World(db_path)
        await first_close(w)
        await w.reopen(cleanup=False)
        assert sorted(w.book.roster()) == ["1", "3"], "the stale rows are still there"
        await w.press("close_ev1")
        await w.press("return_ev1:ch-bob")
        await w.add_extra("dave")
        await w.press("save_ev1")
        assert sorted(w.book.roster()) == ["1", "2", "3", "4"], "replaced, not appended to"

    async def test_a_save_with_nobody_going_clears_what_was_there(self, db_path):
        w = World(db_path)
        w.book.tabs["EventUsers"].rows += [["ev1", "99"], ["other", "7"]]
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE event_users SET status = 'notgoing' WHERE event_id = 'ev1'")
        conn.commit()
        conn.close()
        await w.press("close_ev1")
        await w.press("save_ev1")
        assert w.book.roster() == [] and w.book.roster("other") == ["7"], "only this event's rows go"


class TestWhenTheSheetFails:
    async def test_a_failed_eventusers_write_is_reported_in_the_chat(self, db_path):
        w = World(db_path)
        await w.press("close_ev1")
        w.book.tabs["EventUsers"].fail_append = True
        await w.press("save_ev1")
        warnings = [c.kwargs.get("text", "") for c in w.bot.send_message.call_args_list]
        assert any("Google Sheets sync failed" in t for t in warnings), "it used to fail silently, only in the log"
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT event_status FROM events WHERE event_id = 'ev1'").fetchone()[0] == 2, "the event itself is saved"


async def run_sync(book, roster, event_id="ev1"):
    with patch("sheets.get_sheet_for_chat", new_callable=AsyncMock, return_value="sid"), \
         patch("sheets.open_spreadsheet", new_callable=AsyncMock, return_value=book):
        return await sheets.sync_event_users_sheet(HUB, event_id, roster)


class TestTheWriteItself:
    async def test_the_first_save_is_a_plain_append_with_no_removal(self):
        book = Book()
        await run_sync(book, ["1", "2"])
        assert book.roster() == ["1", "2"] and book.batch_calls == []

    async def test_it_replaces_this_events_rows_and_leaves_every_other_event_alone(self):
        book = Book()
        book.tabs["EventUsers"].rows += [["ev1", "9"], ["other", "7"], ["ev1", "8"], ["other", "6"]]
        await run_sync(book, ["1", "2"])
        assert sorted(book.roster()) == ["1", "2"]
        assert book.roster("other") == ["7", "6"]

    async def test_the_same_person_given_twice_is_written_once_and_blanks_are_skipped(self):
        book = Book()
        await run_sync(book, ["1", "1", "", None, "2", 1])
        assert book.roster() == ["1", "2"]

    async def test_running_it_again_ends_in_the_same_state(self):
        book = Book()
        await run_sync(book, ["1", "2"])
        await run_sync(book, ["1", "2"])
        assert book.roster() == ["1", "2"]

    async def test_nobody_going_still_removes_the_old_rows(self):
        book = Book()
        book.tabs["EventUsers"].rows += [["ev1", "9"]]
        await run_sync(book, [])
        assert book.roster() == []

    async def test_a_hub_without_a_sheet_does_nothing(self):
        with patch("sheets.get_sheet_for_chat", new_callable=AsyncMock, return_value=None), \
             patch("sheets.open_spreadsheet", new_callable=AsyncMock, return_value=None):
            assert await sheets.sync_event_users_sheet(HUB, "ev1", ["1"]) is None

    async def test_a_missing_tab_is_an_error_not_silence(self):
        import pytest
        from gspread.exceptions import WorksheetNotFound
        book = Book()
        del book.tabs["EventUsers"]
        with pytest.raises(WorksheetNotFound):
            await run_sync(book, ["1"])

    async def test_an_api_failure_is_raised(self):
        import pytest
        book = Book()
        book.tabs["EventUsers"].fail_append = True
        with pytest.raises(RuntimeError):
            await run_sync(book, ["1"])
