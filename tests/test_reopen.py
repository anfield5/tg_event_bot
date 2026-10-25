"""
/reopen - the owner-level 🔓 Reopen button: mode switch, the button on the hub post, the press itself, and the
Google Sheet cleanup that keeps the next Save & Close from listing everybody twice.
"""
import re
import sqlite3
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from gspread.exceptions import WorksheetNotFound

sys.path.insert(0, ".")
import db as db_module
import handlers
import reopen
import sheets
from keyboard import create_event_keyboard
from tests.helpers import make_bot, make_chat, make_context, make_message, make_update, make_user
from tests.test_help_audit import _gather

HUB, OWNER = "-100", 555


def plain(text):
    return text.replace("\\", "")


def seed(db_path, event_id="ev1", status=2, name="Party", hub=HUB):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon, event_status, "
        "going_data, notgoing_data, counters_data, kicked_data, closed_date) "
        "VALUES (?, ?, '1', ?, '👍', '❌', ?, '[]', '[]', '{}', '[]', '01.10.2026 20:00:00.000')",
        (event_id, hub, name, status),
    )
    conn.commit()
    conn.close()


def state(db_path, event_id="ev1"):
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT event_status, closed_date FROM events WHERE event_id = ?", (event_id,)).fetchone()
    conn.close()
    return row


# ── the stored switch ────────────────────────────────────────────────────────

class TestSwitchStorage:
    def test_off_by_default_and_per_hub(self, db_path):
        assert not db_module.is_reopen_active(HUB)
        db_module.set_reopen_mode(HUB, True, OWNER)
        assert db_module.is_reopen_active(HUB) and not db_module.is_reopen_active("-200")

    def test_it_remembers_who_and_when_and_switches_off_again(self, db_path):
        db_module.set_reopen_mode(HUB, True, OWNER)
        db_module.set_reopen_mode(HUB, False, 7)
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT active, set_by, set_at FROM reopen_mode WHERE chat_id = ?", (HUB,)).fetchone()
        conn.close()
        assert row[0] == 0 and row[1] == "7" and row[2]

    def test_the_feature_is_seeded_owner_level(self, db_path):
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT min_tier FROM all_features WHERE feature_key = 'reopen'").fetchone() == ("OWNER",)
        conn.close()

    def test_latest_event_and_the_blocking_event(self, db_path):
        seed(db_path, "old", status=0, name="Old one")
        seed(db_path, "new", status=2, name="New one")
        assert db_module.latest_event_of_hub(HUB) == ("new", 2, "New one")
        assert db_module.other_active_event(HUB, "new") == "Old one"
        assert db_module.other_active_event(HUB, "old") is None

    @pytest.mark.parametrize("status,mode,expected", [(2, True, True), (-1, True, True), (0, True, False),
                                                     (1, True, False), (2, False, False)])
    def test_when_the_button_is_offered(self, db_path, status, mode, expected):
        seed(db_path, status=status)
        db_module.set_reopen_mode(HUB, mode)
        assert db_module.reopen_button_offered(HUB, "ev1") is expected

    def test_never_for_an_event_that_is_not_the_newest(self, db_path):
        seed(db_path, "old", status=2); seed(db_path, "new", status=2)
        db_module.set_reopen_mode(HUB, True)
        assert db_module.reopen_button_offered(HUB, "new") and not db_module.reopen_button_offered(HUB, "old")


# ── the keyboard ─────────────────────────────────────────────────────────────

def callbacks(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


class TestKeyboard:
    @pytest.mark.parametrize("status", [2, -1])
    def test_closed_and_canceled_posts_get_the_button_when_offered(self, status):
        kb = create_event_keyboard("ev1", status, "g", "n", reopen_available=True)
        assert callbacks(kb) == ["reopen_ev1"]
        assert kb.inline_keyboard[0][0].text == "🔓 Reopen"

    @pytest.mark.parametrize("status", [2, -1])
    def test_otherwise_they_stay_empty(self, status):
        assert callbacks(create_event_keyboard("ev1", status, "g", "n")) == []

    def test_child_posts_never_get_it(self):
        assert callbacks(create_event_keyboard("ev1", 2, "g", "n", is_child=True, reopen_available=True)) == []

    @pytest.mark.parametrize("status", [0, 1])
    def test_live_events_ignore_the_flag(self, status):
        assert not any(c.startswith("reopen_") for c in callbacks(create_event_keyboard("ev1", status, "g", "n", reopen_available=True)))


# ── the post ─────────────────────────────────────────────────────────────────

async def hub_markup(event_id="ev1", with_share=False):
    bot = make_bot()
    bot.get_chat = AsyncMock(return_value=MagicMock(title="News", type="channel"))
    await handlers.update_all_shared_views(make_context(bot=bot), event_id)
    return [c.kwargs["reply_markup"] for c in bot.edit_message_text.call_args_list]


class TestThePost:
    async def test_the_hub_post_shows_the_button_while_the_mode_is_on(self, db_path):
        seed(db_path)
        db_module.set_reopen_mode(HUB, True)
        assert callbacks((await hub_markup())[0]) == ["reopen_ev1"]

    async def test_and_loses_it_when_the_mode_is_off(self, db_path):
        seed(db_path)
        db_module.set_reopen_mode(HUB, True)
        db_module.set_reopen_mode(HUB, False)
        assert callbacks((await hub_markup())[0]) == []

    async def test_a_canceled_event_gets_it_too(self, db_path):
        seed(db_path, status=-1)
        db_module.set_reopen_mode(HUB, True)
        assert callbacks((await hub_markup())[0]) == ["reopen_ev1"]

    async def test_an_older_event_never_does(self, db_path):
        seed(db_path, "old", status=2); seed(db_path, "new", status=2)
        db_module.set_reopen_mode(HUB, True)
        assert callbacks((await hub_markup("old"))[0]) == []
        assert callbacks((await hub_markup("new"))[0]) == ["reopen_new"]

    async def test_shared_chats_never_get_it(self, db_path):
        seed(db_path)
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type) VALUES ('ev1','-200','5','-visible','channel')")
        conn.commit()
        conn.close()
        db_module.set_reopen_mode(HUB, True)
        markups = await hub_markup()
        assert len(markups) >= 2 and callbacks(markups[0]) == ["reopen_ev1"]
        assert all(callbacks(m) == [] for m in markups[1:])


# ── the command ──────────────────────────────────────────────────────────────

async def command(args, user_id=OWNER, status="administrator"):
    bot = make_bot()
    bot.get_chat_member = AsyncMock(return_value=MagicMock(status=status))
    chat = make_chat(chat_id=int(HUB), chat_type="supergroup")
    msg = make_message(chat=chat)
    upd = make_update(chat=chat, user=make_user(user_id=user_id), message=msg)
    with patch("subscription.OWNER_USER_IDS", {OWNER}), \
         patch("reopen.schedule_view_refresh", new=AsyncMock()) as refresh:
        await reopen.reopen_command(upd, make_context(bot=bot, args=args))
    return msg, refresh


class TestCommand:
    async def test_on_switches_it_on_tells_where_and_refreshes_the_post(self, db_path):
        seed(db_path, name="Football")
        msg, refresh = await command(["on"])
        assert db_module.is_reopen_active(HUB)
        text = plain(msg.reply_text.call_args.args[0])
        assert "ON" in text and "Football" in text and "owners can use it" in text
        refresh.assert_called_once()
        assert refresh.call_args.args[1] == "ev1"

    async def test_off_switches_it_off_and_refreshes(self, db_path):
        seed(db_path)
        db_module.set_reopen_mode(HUB, True)
        msg, refresh = await command(["off"])
        assert not db_module.is_reopen_active(HUB)
        assert "OFF" in plain(msg.reply_text.call_args.args[0]) and "gone" in plain(msg.reply_text.call_args.args[0])
        refresh.assert_called_once()

    async def test_on_with_nothing_to_show_it_under_does_not_touch_any_post(self, db_path):
        seed(db_path, status=0)
        msg, refresh = await command(["on"])
        assert db_module.is_reopen_active(HUB) and "next event" in plain(msg.reply_text.call_args.args[0])
        refresh.assert_not_called()

    async def test_no_argument_just_reports(self, db_path):
        seed(db_path)
        msg, refresh = await command([])
        assert "OFF" in plain(msg.reply_text.call_args.args[0]) and not db_module.is_reopen_active(HUB)
        refresh.assert_not_called()

    async def test_a_wrong_argument_gets_the_usage(self, db_path):
        msg, _ = await command(["maybe"])
        assert "Usage" in plain(msg.reply_text.call_args.args[0]) and not db_module.is_reopen_active(HUB)

    async def test_for_anyone_who_is_not_an_owner_the_command_is_ignored(self, db_path):
        seed(db_path)
        msg, refresh = await command(["on"], user_id=1)
        assert msg.reply_text.call_count == 0 and not db_module.is_reopen_active(HUB)
        refresh.assert_not_called()

    async def test_opened_up_to_pro_it_belongs_to_the_groups_admins_only(self, db_path):
        from datetime import datetime, timedelta
        end = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE all_features SET min_tier = 'PRO' WHERE feature_key = 'reopen'")
        conn.execute("INSERT INTO all_groups (chat_id, type, subs_date_start, subs_date_end) VALUES (?, 'PRO', ?, ?)", (HUB, end, end))
        conn.commit()
        conn.close()
        seed(db_path)
        await command(["on"], user_id=1, status="administrator")
        assert db_module.is_reopen_active(HUB)
        db_module.set_reopen_mode(HUB, False)
        msg, _ = await command(["on"], user_id=2, status="member")
        assert "Only admins" in plain(msg.reply_text.call_args.args[0]) and not db_module.is_reopen_active(HUB)


# ── the press ────────────────────────────────────────────────────────────────

async def press(event_id="ev1", user_id=OWNER, in_chat=HUB, sheet=None, status="administrator"):
    query = MagicMock()
    query.data = f"reopen_{event_id}"
    query.message = MagicMock()
    query.message.chat_id = int(in_chat)
    query.from_user = make_user(user_id=user_id, username="boss")
    query.answer = AsyncMock()
    upd = MagicMock()
    upd.callback_query = query
    bot = make_bot()
    bot.get_chat_member = AsyncMock(return_value=MagicMock(status=status))
    sheet_mock = sheet or AsyncMock(return_value=None)
    with patch("subscription.OWNER_USER_IDS", {OWNER}), \
         patch("reopen.reopen_event_in_sheet", new=sheet_mock), \
         patch("reopen.schedule_view_refresh", new=AsyncMock()) as refresh:
        await reopen.reopen_callback_handler(upd, make_context(bot=bot))
    return query, sheet_mock, refresh


def refused(query):
    assert query.answer.await_count == 1, "Telegram takes one answer per press"
    assert query.answer.call_args.kwargs.get("show_alert") is True
    return plain(query.answer.call_args.args[0])


class TestThePress:
    @pytest.mark.parametrize("status", [2, -1])
    async def test_a_closed_or_canceled_event_goes_back_to_open(self, db_path, status):
        seed(db_path, status=status)
        db_module.set_reopen_mode(HUB, True)
        query, sheet, refresh = await press()
        assert state(db_path) == (0, None), "open again, and the old closing time is gone"
        assert query.answer.call_args.kwargs.get("show_alert") is not True
        assert "reopened" in plain(query.answer.call_args.args[0])
        refresh.assert_called_once()
        sheet.assert_awaited_once()
        row = sheet.call_args.kwargs["actions_row"]
        assert row[:4] == ["ev1", "REOPEN", "boss", str(OWNER)]

    async def test_a_non_owner_is_refused_and_nothing_changes(self, db_path):
        seed(db_path); db_module.set_reopen_mode(HUB, True)
        query, sheet, refresh = await press(user_id=1)
        assert "Owners only" in refused(query)
        assert state(db_path)[0] == 2
        sheet.assert_not_called(); refresh.assert_not_called()

    async def test_a_stale_button_after_the_mode_was_switched_off(self, db_path):
        seed(db_path)
        query, sheet, _ = await press()
        assert "mode is off" in refused(query) and state(db_path)[0] == 2
        sheet.assert_not_called()

    async def test_an_event_that_is_already_open(self, db_path):
        seed(db_path, status=0); db_module.set_reopen_mode(HUB, True)
        assert "already open" in refused((await press())[0])

    async def test_only_the_newest_event_can_be_reopened(self, db_path):
        seed(db_path, "old", status=2); seed(db_path, "new", status=2)
        db_module.set_reopen_mode(HUB, True)
        query, sheet, _ = await press("old")
        assert "newest event" in refused(query) and state(db_path, "old")[0] == 2
        sheet.assert_not_called()

    async def test_refused_while_another_event_is_still_open_and_it_is_named(self, db_path):
        seed(db_path, "older", status=0, name="Still running"); seed(db_path, "newer", status=2)
        db_module.set_reopen_mode(HUB, True)
        query, sheet, _ = await press("newer")
        message = refused(query)
        assert "Still running" in message and "Close or cancel it first" in message
        assert state(db_path, "newer")[0] == 2
        sheet.assert_not_called()

    async def test_refused_while_another_event_is_in_verification(self, db_path):
        seed(db_path, "older", status=1, name="Under review"); seed(db_path, "newer", status=-1)
        db_module.set_reopen_mode(HUB, True)
        assert "Under review" in refused((await press("newer"))[0])

    async def test_pressed_from_another_chat_is_refused(self, db_path):
        seed(db_path); db_module.set_reopen_mode(HUB, True)
        assert "where the event was created" in refused((await press(in_chat="-200"))[0])
        assert state(db_path)[0] == 2

    async def test_an_unknown_event(self, db_path):
        assert "no longer exists" in refused((await press("ghost"))[0])

    async def test_a_sheets_failure_changes_nothing_and_says_so(self, db_path):
        seed(db_path); db_module.set_reopen_mode(HUB, True)
        query, _, refresh = await press(sheet=AsyncMock(side_effect=RuntimeError("quota")))
        assert "nothing was changed" in refused(query)
        assert state(db_path) == (2, "01.10.2026 20:00:00.000")
        refresh.assert_not_called()

    async def test_pressing_twice_only_works_once(self, db_path):
        seed(db_path); db_module.set_reopen_mode(HUB, True)
        await press()
        assert "already open" in refused((await press())[0])

    async def test_the_mode_stays_on_but_the_button_is_gone_until_the_event_is_closed_again(self, db_path):
        seed(db_path); db_module.set_reopen_mode(HUB, True)
        await press()
        assert db_module.is_reopen_active(HUB) and not db_module.reopen_button_offered(HUB, "ev1")
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE events SET event_status = 2 WHERE event_id = 'ev1'")
        conn.commit()
        conn.close()
        assert db_module.reopen_button_offered(HUB, "ev1")

    async def test_when_opened_up_to_pro_a_plain_member_still_cannot(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE all_features SET min_tier = 'FREE' WHERE feature_key = 'reopen'")
        conn.commit()
        conn.close()
        seed(db_path); db_module.set_reopen_mode(HUB, True)
        query, _, _ = await press(user_id=1, status="member")
        assert "Owners only" in refused(query) and state(db_path)[0] == 2
        await press(user_id=2, status="administrator")
        assert state(db_path)[0] == 0


# ── the sheet half ───────────────────────────────────────────────────────────

class Tab:
    def __init__(self, sheet_id, rows):
        self.id, self.rows, self.append_fails = sheet_id, rows, False

    async def col_values(self, col):
        return [r[col - 1] if len(r) >= col else "" for r in self.rows]

    async def get_all_records(self):
        header = self.rows[0]
        return [dict(zip(header, r + [""] * (len(header) - len(r)))) for r in self.rows[1:]]

    async def update(self, rng, values):
        m = re.match(r"([A-Z])(\d+):[A-Z]\d+", rng)
        start, row = ord(m.group(1)) - ord("A"), int(m.group(2))
        for offset, value in enumerate(values[0]):
            self.rows[row - 1][start + offset] = value

    async def append_row(self, row):
        if self.append_fails:
            raise RuntimeError("quota")
        self.rows.append(row)


class Book:
    def __init__(self, tabs):
        self.tabs, self.batch_fails = tabs, False

    async def worksheet(self, name):
        if name not in self.tabs:
            raise WorksheetNotFound(name)
        return self.tabs[name]

    async def batch_update(self, body):
        if self.batch_fails:
            raise RuntimeError("down")
        for req in body["requests"]:
            r = req["deleteDimension"]["range"]
            tab = next(t for t in self.tabs.values() if t.id == r["sheetId"])
            del tab.rows[r["startIndex"]:r["endIndex"]]


def book():
    header = ["EVENT_ID", "EVENT_NAME", "CREATED_DATE", "CREATED_BY", "EVENT_DATE", "CLOSED_AT", "STATUS", "GOING_COUNT"]
    return Book({
        "Events": Tab(1, [header, ["ev1", "Party", "c", "u", "", "01.10.2026", "CLOSED", 7], ["ev2", "Other", "c", "u", "", "02.10.2026", "CLOSED", 3]]),
        "EventUsers": Tab(2, [["EVENT_ID", "USER_ID"], ["ev1", "1"], ["ev2", "9"], ["ev1", "2"], ["ev1", "3"], ["ev2", "8"]]),
        "Actions": Tab(3, [["EVENT_ID", "ACTION", "USER_NAME", "USER_ID", "DATE", "CHAT_ID"]]),
    })


async def run_sheet(b, **kwargs):
    with patch("sheets.get_sheet_for_chat", new_callable=AsyncMock, return_value="sid"), \
         patch("sheets.open_spreadsheet", new_callable=AsyncMock, return_value=b):
        return await sheets.reopen_event_in_sheet(HUB, "ev1", **kwargs)


class TestSheetCleanup:
    async def test_the_events_row_goes_back_to_open_and_only_this_events_roster_is_removed(self, db_path):
        b = book()
        result = await run_sheet(b)
        assert result == {"events_row": True, "event_users_removed": 3}
        assert b.tabs["Events"].rows[1][5:] == ["", "OPEN", 0]
        assert b.tabs["Events"].rows[2][5:] == ["02.10.2026", "CLOSED", 3], "the other event is untouched"
        assert b.tabs["EventUsers"].rows == [["EVENT_ID", "USER_ID"], ["ev2", "9"], ["ev2", "8"]]

    async def test_doing_it_again_changes_nothing(self, db_path):
        b = book()
        await run_sheet(b)
        before = [list(r) for r in b.tabs["EventUsers"].rows]
        assert (await run_sheet(b))["event_users_removed"] == 0
        assert b.tabs["EventUsers"].rows == before

    async def test_the_next_save_does_not_list_anybody_twice(self, db_path):
        b = book()
        await run_sheet(b)
        for uid in ("1", "2", "3"):                       # what Save & Close appends again
            await b.tabs["EventUsers"].append_row(["ev1", uid])
        assert sum(1 for r in b.tabs["EventUsers"].rows if r[0] == "ev1") == 3

    async def test_the_actions_row_is_logged(self, db_path):
        b = book()
        await run_sheet(b, actions_row=["ev1", "REOPEN", "boss", "555", "d", HUB])
        assert b.tabs["Actions"].rows[-1][1] == "REOPEN"

    async def test_a_failing_actions_log_does_not_undo_or_raise(self, db_path):
        b = book()
        b.tabs["Actions"].append_fails = True
        assert (await run_sheet(b, actions_row=["ev1", "REOPEN", "boss", "555", "d", HUB]))["event_users_removed"] == 3

    async def test_missing_tabs_are_skipped(self, db_path):
        assert await run_sheet(Book({})) == {"events_row": False, "event_users_removed": 0}

    async def test_a_hub_without_a_sheet_is_a_no_op(self, db_path):
        with patch("sheets.get_sheet_for_chat", new_callable=AsyncMock, return_value=None), \
             patch("sheets.open_spreadsheet", new_callable=AsyncMock, return_value=None):
            assert await sheets.reopen_event_in_sheet(HUB, "ev1") is None

    async def test_failures_are_raised_so_nothing_gets_reopened(self, db_path):
        b = book()
        b.batch_fails = True
        with pytest.raises(RuntimeError):
            await run_sheet(b)
        with patch("sheets.get_sheet_for_chat", new_callable=AsyncMock, return_value="sid"), \
             patch("sheets.open_spreadsheet", new_callable=AsyncMock, side_effect=RuntimeError("invalid_grant")):
            with pytest.raises(RuntimeError):
                await sheets.reopen_event_in_sheet(HUB, "ev1")


# ── around it ────────────────────────────────────────────────────────────────

class TestAroundIt:
    def test_it_is_registered(self):
        text = open("main.py", encoding="utf-8").read()
        assert 'CommandHandler("reopen", reopen_command)' in text
        assert 'pattern="^reopen_"' in text
        assert text.index('pattern="^reopen_"') < text.index("CallbackQueryHandler(button_handler)")

    async def test_a_locked_bot_blocks_the_button_for_everyone_but_owners(self, db_path):
        from telegram.ext import ApplicationHandlerStop
        import main
        def update_from(user_id):
            upd = MagicMock()
            upd.effective_user = make_user(user_id=user_id)
            upd.callback_query.data = "reopen_ev1"
            upd.callback_query.answer = AsyncMock()
            return upd
        with patch("main.is_bot_locked", return_value=True), patch("main.OWNER_USER_IDS", {OWNER}):
            with pytest.raises(ApplicationHandlerStop):
                await main.lock_gate(update_from(1), make_context())
            assert await main.lock_gate(update_from(OWNER), make_context()) is None

    async def test_the_owner_screen_documents_it_with_the_commands_that_work_in_groups(self, db_path):
        text = plain((await _gather(OWNER))["owner"])
        assert text.index("also work inside a group") < text.index("/reopen")

    async def test_the_lifecycle_help_points_owners_at_it_and_nobody_else(self, db_path):
        assert "/reopen on" in plain((await _gather(OWNER))["help_lifecycle"])
        assert "/reopen" not in plain((await _gather(1))["help_lifecycle"])
