"""
Why a button click used to take seconds, and what keeps it fast now:
  * the click no longer writes to Google Sheets (two calls, >= 1.1 s apart each) - its Actions row goes to the outbox
    (actions_log.py) and a background task writes a burst of rows in ONE append, losing nothing;
  * "is the clicker an admin?" (a Telegram call) is asked only by the actions that need it, not by Going / Not Going / ADD / DROP.
"""
import asyncio
import json
import sqlite3
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, ".")
import actions_log
import db as db_module
import event_engine
import handlers
from tests.helpers import make_bot, make_callback_update, make_context, make_user
from tests.test_handlers_async import FakeSpreadsheet, MAIN_CHAT, insert_event

ROW = ["ev1", "GOING", "alice", "1", "22.11.2026 10:00:00.000", MAIN_CHAT]


def outbox(db_path):
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT hub_chat_id, row_json, attempts FROM actions_outbox ORDER BY id").fetchall()
    conn.close()
    return rows


@pytest.fixture(autouse=True)
def _fresh_backoff():
    actions_log._blocked_until.clear(); actions_log._backoff.clear()
    yield
    actions_log._blocked_until.clear(); actions_log._backoff.clear()


async def flush(ss, sheet="sheet1", wait_over=True):
    """One flush_once; `wait_over`: the pause after an earlier failure has passed (the tests do not sleep for it)."""
    if wait_over:
        actions_log._blocked_until.clear()
    with patch("actions_log.get_sheet_for_chat", new_callable=AsyncMock, return_value=sheet), \
         patch("actions_log.open_spreadsheet", new_callable=AsyncMock, return_value=ss):
        return await actions_log.flush_once()


async def click(data, user, chat_id=int(MAIN_CHAT), bot=None):
    bot = bot or make_bot()
    bot.get_chat_member = AsyncMock(return_value=MagicMock(status="member"))
    ctx = make_context(bot=bot)
    with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock) as sheet_for, \
         patch("event_engine.open_spreadsheet", new_callable=AsyncMock) as open_ss:
        await handlers.button_handler(make_callback_update(data, chat_id=chat_id, user=user), ctx)
    return bot, ctx, sheet_for, open_ss


class TestTheClickDoesNotWaitForGoogle:
    @pytest.mark.parametrize("data,uid", [("going_ev1", 1), ("notgoing_ev1", 1), ("add_ev1", 1), ("sub_ev1", 1)])
    async def test_a_click_makes_no_google_call_and_leaves_its_row_in_the_outbox(self, db_path, data, uid):
        insert_event(db_path, event_id="ev1", chat_id=MAIN_CHAT, going=json.dumps(["alice (1)"]), counters=json.dumps({"alice": 1}))
        _bot, ctx, sheet_for, open_ss = await click(data, make_user(user_id=uid, username="alice"))
        sheet_for.assert_not_called(); open_ss.assert_not_called()
        rows = outbox(db_path)
        assert len(rows) == 1 and rows[0][0] == MAIN_CHAT and json.loads(rows[0][1])[0:2] == ["ev1", data.split("_")[0].upper()]
        assert ctx.application.create_task.called, "the post is redrawn"

    async def test_a_click_that_changes_nothing_logs_nothing(self, db_path):
        insert_event(db_path, event_id="ev1", chat_id=MAIN_CHAT, event_status=2)
        await click("going_ev1", make_user(user_id=1, username="alice"))
        assert outbox(db_path) == []

    async def test_the_clicker_is_not_looked_up_for_going_but_is_for_an_admin_action(self, db_path):
        insert_event(db_path, event_id="ev1", chat_id=MAIN_CHAT)
        bot, *_ = await click("going_ev1", make_user(user_id=5, username="bob"))
        bot.get_chat_member.assert_not_called()
        bot, *_ = await click("cancel_ev1", make_user(user_id=5, username="bob"))
        bot.get_chat_member.assert_called_once()

    async def test_the_creator_is_not_looked_up_either(self, db_path):
        insert_event(db_path, event_id="ev1", chat_id=MAIN_CHAT)
        conn = sqlite3.connect(db_path); conn.execute("UPDATE events SET created_by_user_id = '5'"); conn.commit(); conn.close()
        bot, *_ = await click("cancel_ev1", make_user(user_id=5, username="bob"))
        bot.get_chat_member.assert_not_called()

    async def test_a_non_admin_is_still_refused_an_admin_action(self, db_path):
        insert_event(db_path, event_id="ev1", chat_id=MAIN_CHAT)
        bot, *_ = await click("cancel_ev1", make_user(user_id=6, username="eve"))
        await asyncio.sleep(0)
        assert bot.get_chat_member.call_count == 1
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT event_status FROM events WHERE event_id='ev1'").fetchone() == (0,)
        conn.close()


class TestOutbox:
    async def test_a_burst_of_clicks_is_one_append_in_order(self, db_path):
        rows = [["ev1", a, "u", str(i), "d", MAIN_CHAT] for i, a in enumerate(["GOING", "ADD", "NOTGOING", "SUB"])]
        for r in rows:
            actions_log.queue_action(MAIN_CHAT, r)
        ss = FakeSpreadsheet()
        ws = await ss.worksheet("Actions")
        ws.append_rows = AsyncMock()
        assert await flush(ss) == 4
        ws.append_rows.assert_called_once_with(rows)
        assert outbox(db_path) == []

    async def test_rows_of_two_hubs_go_to_their_own_sheets(self, db_path):
        actions_log.queue_action("-1", ["a"]); actions_log.queue_action("-2", ["b"]); actions_log.queue_action("-1", ["c"])
        books = {"s1": FakeSpreadsheet(), "s2": FakeSpreadsheet()}
        async def sheet_for(hub): return {"-1": "s1", "-2": "s2"}[hub]
        async def open_ss(sheet): return books[sheet]
        with patch("actions_log.get_sheet_for_chat", new=sheet_for), patch("actions_log.open_spreadsheet", new=open_ss):
            assert await actions_log.flush_once() == 3
        assert books["s1"].worksheets["Actions"].appended_rows == [["a"], ["c"]]
        assert books["s2"].worksheets["Actions"].appended_rows == [["b"]]

    async def test_nothing_is_lost_when_google_fails_and_the_next_try_writes_it_in_order(self, db_path):
        actions_log.queue_action(MAIN_CHAT, ["first"]); actions_log.queue_action(MAIN_CHAT, ["second"])
        ss = FakeSpreadsheet()
        ws = await ss.worksheet("Actions")
        ws.append_rows = AsyncMock(side_effect=RuntimeError("quota"))
        assert await flush(ss) == 0
        assert [json.loads(r[1]) for r in outbox(db_path)] == [["first"], ["second"]] and {r[2] for r in outbox(db_path)} == {1}
        actions_log.queue_action(MAIN_CHAT, ["third"])
        ws.append_rows = AsyncMock()
        assert await flush(ss) == 3
        ws.append_rows.assert_called_once_with([["first"], ["second"], ["third"]])

    async def test_a_row_that_never_succeeds_is_dropped_after_the_limit_and_does_not_block_the_next_one(self, db_path):
        actions_log.queue_action(MAIN_CHAT, ["stuck"])
        ss = FakeSpreadsheet()
        ws = await ss.worksheet("Actions")
        ws.append_rows = AsyncMock(side_effect=RuntimeError("no such tab"))
        for _ in range(actions_log.MAX_ATTEMPTS):
            await flush(ss)
        assert outbox(db_path) == []
        actions_log.queue_action(MAIN_CHAT, ["fine"])
        ws.append_rows = AsyncMock()
        assert await flush(ss) == 1

    async def test_a_failed_hub_is_left_alone_for_a_while_not_retried_on_every_click(self, db_path):
        actions_log.queue_action(MAIN_CHAT, ["a"])
        ss = FakeSpreadsheet()
        ws = await ss.worksheet("Actions")
        ws.append_rows = AsyncMock(side_effect=RuntimeError("quota"))
        await flush(ss)
        actions_log.queue_action(MAIN_CHAT, ["b"])
        await flush(ss, wait_over=False)
        await flush(ss, wait_over=False)
        assert ws.append_rows.await_count == 1, "inside the pause Google is not called again"

    async def test_no_access_to_the_sheet_never_drops_rows_and_says_what_to_do(self, db_path, caplog):
        actions_log.queue_action(MAIN_CHAT, ["keep me"])
        ss = FakeSpreadsheet()
        ws = await ss.worksheet("Actions")
        ws.append_rows = AsyncMock(side_effect=RuntimeError("{'code': 403, 'message': 'The caller does not have permission', 'status': 'PERMISSION_DENIED'}"))
        for _ in range(actions_log.MAX_ATTEMPTS + 5):
            await flush(ss)
        assert [json.loads(r[1]) for r in outbox(db_path)] == [["keep me"]] and outbox(db_path)[0][2] == 0
        assert "service account" in caplog.text and "Editor" in caplog.text
        ws.append_rows = AsyncMock()
        assert await flush(ss) == 1                      # access given: the waiting row is written

    async def test_a_hub_without_a_premium_sheet_has_its_rows_dropped(self, db_path):
        actions_log.queue_action(MAIN_CHAT, ["x"])
        with patch("actions_log.get_sheet_for_chat", new_callable=AsyncMock, return_value=None):
            assert await actions_log.flush_once() == 0
        assert outbox(db_path) == []

    async def test_what_was_waiting_when_the_bot_stopped_is_written_after_the_restart(self, db_path):
        actions_log.queue_action(MAIN_CHAT, ["before the restart"])
        ss = FakeSpreadsheet()
        assert await flush(ss) == 1                      # a new process reads the same table
        assert (await ss.worksheet("Actions")).appended_rows == [["before the restart"]]

    async def test_the_background_task_writes_after_a_short_wait_and_can_be_stopped(self, db_path):
        ss = FakeSpreadsheet()
        with patch.object(actions_log, "FLUSH_DELAY", 0), \
             patch("actions_log.get_sheet_for_chat", new_callable=AsyncMock, return_value="s"), \
             patch("actions_log.open_spreadsheet", new_callable=AsyncMock, return_value=ss):
            task = asyncio.ensure_future(actions_log.run_flusher())
            await asyncio.sleep(0)
            actions_log.queue_action(MAIN_CHAT, ["bg"])
            for _ in range(50):
                await asyncio.sleep(0.01)
                if actions_log.pending_count() == 0:
                    break
            task.cancel()
        assert (await ss.worksheet("Actions")).appended_rows == [["bg"]]
