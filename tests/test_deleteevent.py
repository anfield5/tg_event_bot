"""
Tests for /deleteevent - permanent removal of ONE event: how it's found
(list / event_id / name search incl. Cyrillic), who may delete it, the
confirmation screen, and the removal itself (Google Sheet, DB, Telegram
posts) including what happens when parts of it fail.
"""
import sys
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, ".")
import db as db_module
import handlers
import event_engine
import subscription
from tests.helpers import make_chat, make_user, make_message, make_update, make_context, make_bot

HUB = "-100"
SHEET_COUNTS = {"Events": 1, "Actions": 3, "EventUsers": 2}


OWNER = 777


def _set_tier(db_path, tier):
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE all_features SET min_tier = ? WHERE feature_key = 'deleteevent'", (tier,))
    conn.commit()
    conn.close()


def _make_premium(db_path, chat_id=HUB):
    from datetime import datetime, timedelta
    end = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO all_groups (chat_id, type, subs_date_start, subs_date_end) VALUES (?, 'PRO', ?, ?)",
                 (chat_id, end, end))
    conn.commit()
    conn.close()


@pytest.fixture()
def seeded_db(tmp_path, monkeypatch):
    """The database exactly as init_db() seeds it - deleteevent is owner-level (OWNER)."""
    path = str(tmp_path / "test.db")
    monkeypatch.setattr(db_module, "DB_PATH", path)
    db_module.init_db(db_path=path)
    return path


@pytest.fixture()
def db_path(seeded_db):
    """Same database with the feature opened up, so the behaviour tests below
    exercise the command itself - TestFeatureGate covers the gate."""
    _set_tier(seeded_db, "FREE")
    return seeded_db


def _add_event(db_path, event_id, name, chat_id=HUB, status=0, created_by="1",
               message_id="500", created_date="01.10.2026 12:00:00.000"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon, "
        "event_status, going_data, notgoing_data, counters_data, kicked_data, "
        "created_by_user_id, created_date) "
        "VALUES (?, ?, ?, ?, '👍', '❌', ?, '[]', '[]', '{}', '[]', ?, ?)",
        (event_id, chat_id, message_id, name, status, created_by, created_date),
    )
    conn.commit()
    conn.close()


def _add_children(db_path, event_id):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO event_users (event_id, chat_id, user_id, username, status, guests) "
                 "VALUES (?, '-100', '7', 'alice', 'going', 1)", (event_id,))
    conn.execute("INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type) "
                 "VALUES (?, '-200', '600', '-visible', 'channel')", (event_id,))
    conn.commit()
    conn.close()


def _count(db_path, table, event_id):
    conn = sqlite3.connect(db_path)
    n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE event_id = ?", (event_id,)).fetchone()[0]
    conn.close()
    return n


async def _command(args, user_id=1, admin=True):
    bot = make_bot()
    bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator" if admin else "member"))
    chat = make_chat(chat_id=int(HUB), chat_type="supergroup")
    msg = make_message(chat=chat)
    upd = make_update(chat=chat, user=make_user(user_id=user_id), message=msg)
    await handlers.deleteevent(upd, make_context(bot=bot, args=args))
    return msg


def _callback(data, user_id=1, admin=True):
    bot = make_bot()
    bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator" if admin else "member"))
    query = MagicMock()
    query.data = data
    query.message = MagicMock()
    query.message.chat = MagicMock(type="supergroup")
    query.message.sender_chat = None
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    upd = MagicMock()
    upd.message = None  # a real callback-query Update has no .message
    upd.callback_query = query
    upd.effective_user = make_user(user_id=user_id)
    return upd, make_context(bot=bot), query, bot


def _reply(msg):
    call = msg.reply_text.call_args
    return call.args[0], call.kwargs.get("reply_markup")


def _buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


class TestFindingTheEvent:
    async def test_no_argument_lists_latest_events_newest_first(self, db_path):
        _add_event(db_path, "ev1", "First")
        _add_event(db_path, "ev2", "Second")
        text, markup = _reply(await _command([]))
        labels = [b.text for b in _buttons(markup)]
        assert "Pick the event to delete" in text
        assert labels[0].startswith("Second") and labels[1].startswith("First")
        assert labels[-1] == "✖ Close"

    async def test_list_is_capped_and_says_so(self, db_path):
        for i in range(12):
            _add_event(db_path, f"ev{i:02d}", f"Event{i}")
        text, markup = _reply(await _command([]))
        assert len(_buttons(markup)) == 10 + 1  # 10 events + Close
        assert "latest 10 of 12" in text

    async def test_callback_data_fits_telegrams_64_byte_limit(self, db_path):
        _add_event(db_path, "abcd1234", "X", chat_id="-1001234567890123")
        bot = make_bot()
        markup = handlers._events_picker_markup("-1001234567890123", [("abcd1234", "X", 0, "", "1")])
        for b in _buttons(markup):
            assert len(b.callback_data.encode()) <= 64

    async def test_exact_event_id_goes_straight_to_confirmation(self, db_path):
        _add_event(db_path, "ev1", "First")
        _add_event(db_path, "ev2", "Second")
        text, markup = _reply(await _command(["ev2"]))
        assert "Delete this event?" in text and "Second" in text
        assert [b.callback_data for b in _buttons(markup)][0] == f"delevyes_{HUB}:ev2"

    async def test_name_search_is_case_insensitive_for_cyrillic(self, db_path):
        _add_event(db_path, "ev1", "Футбол(#38) у суботу")
        _add_event(db_path, "ev2", "ВОЛЕЙБОЛ")
        text1, _ = _reply(await _command(["футбол"]))
        text2, _ = _reply(await _command(["волейбол"]))
        assert "Delete this event?" in text1
        assert "Delete this event?" in text2

    async def test_multi_word_query_is_joined(self, db_path):
        _add_event(db_path, "ev1", "Футбол(#38) у суботу")
        text, _ = _reply(await _command(["Футбол(#38)", "у"]))
        assert "Delete this event?" in text

    async def test_several_matches_are_listed_to_choose_from(self, db_path):
        _add_event(db_path, "ev1", "Футбол #1")
        _add_event(db_path, "ev2", "Футбол #2")
        text, markup = _reply(await _command(["футбол"]))
        assert "Several events match" in text
        assert len(_buttons(markup)) == 2 + 1

    async def test_no_match_says_so(self, db_path):
        _add_event(db_path, "ev1", "First")
        text, markup = _reply(await _command(["nothing"]))
        assert "No event matching" in text and markup is None

    async def test_empty_hub(self, db_path):
        text, _ = _reply(await _command([]))
        assert "No events found" in text

    async def test_non_admin_only_sees_their_own_events(self, db_path):
        _add_event(db_path, "mine", "Mine", created_by="42")
        _add_event(db_path, "theirs", "Theirs", created_by="1")
        _, markup = _reply(await _command([], user_id=42, admin=False))
        labels = [b.text for b in _buttons(markup)]
        assert any(l.startswith("Mine") for l in labels)
        assert not any(l.startswith("Theirs") for l in labels)

    async def test_non_admin_without_own_events_is_told_why(self, db_path):
        _add_event(db_path, "theirs", "Theirs", created_by="1")
        text, _ = _reply(await _command([], user_id=99, admin=False))
        assert "only delete events you created" in text


class TestConfirmationAndCancel:
    async def test_pick_shows_the_confirmation_with_yes_and_cancel(self, db_path):
        _add_event(db_path, "ev1", "First")
        _add_children(db_path, "ev1")
        upd, ctx, query, _ = _callback(f"delevpick_{HUB}:ev1")
        await handlers.deleteevent_callback_handler(upd, ctx)
        text = query.edit_message_text.call_args.args[0]
        markup = query.edit_message_text.call_args.kwargs["reply_markup"]
        assert "Delete this event?" in text and "cannot be undone" in text
        assert "1 participant" in text and "1 shared post" in text
        assert [b.callback_data for b in _buttons(markup)] == [f"delevyes_{HUB}:ev1", f"delevno_{HUB}"]
        assert _count(db_path, "events", "ev1") == 1, "showing the screen must not delete anything"

    async def test_cancel_button_deletes_nothing(self, db_path):
        _add_event(db_path, "ev1", "First")
        upd, ctx, query, _ = _callback(f"delevno_{HUB}")
        await handlers.deleteevent_callback_handler(upd, ctx)
        assert "nothing was deleted" in query.edit_message_text.call_args.args[0]
        assert _count(db_path, "events", "ev1") == 1


class TestDeletion:
    async def _yes(self, event_id="ev1", user_id=1, admin=True, sheet=SHEET_COUNTS, sheet_error=None,
                   tg_error=None):
        upd, ctx, query, bot = _callback(f"delevyes_{HUB}:{event_id}", user_id=user_id, admin=admin)
        if tg_error:
            bot.delete_message = AsyncMock(side_effect=tg_error)
        sheets_mock = AsyncMock(return_value=sheet, side_effect=sheet_error)
        with patch("handlers.delete_event_from_sheet", sheets_mock):
            await handlers.deleteevent_callback_handler(upd, ctx)
        return query, bot, sheets_mock

    async def test_removes_the_event_everywhere_and_only_that_event(self, db_path):
        _add_event(db_path, "ev1", "First")
        _add_children(db_path, "ev1")
        _add_event(db_path, "ev2", "Second", message_id="501")
        _add_children(db_path, "ev2")

        query, bot, sheets_mock = await self._yes("ev1")

        for table in ("events", "event_users", "event_shares"):
            assert _count(db_path, table, "ev1") == 0, table
            assert _count(db_path, table, "ev2") == 1, f"{table}: the OTHER event must be untouched"
        sheets_mock.assert_awaited_once_with(HUB, "ev1")
        deleted = sorted((c.kwargs["chat_id"], c.kwargs["message_id"]) for c in bot.delete_message.call_args_list)
        assert deleted == [(-200, 600), (-100, 500)] or deleted == [(-200, 600), (int(HUB), 500)]
        result = query.edit_message_text.call_args.args[0]
        assert "Deleted" in result and "6 row" in result and "2 of 2 removed" in result

    async def test_sheet_failure_deletes_nothing_anywhere(self, db_path):
        _add_event(db_path, "ev1", "First")
        _add_children(db_path, "ev1")

        query, bot, _ = await self._yes("ev1", sheet_error=Exception("invalid_grant"))

        for table in ("events", "event_users", "event_shares"):
            assert _count(db_path, table, "ev1") == 1, f"{table} must be intact"
        bot.delete_message.assert_not_called()
        result = query.edit_message_text.call_args.args[0]
        assert "Nothing was deleted" in result and "invalid" in result

    async def test_hub_without_a_sheet_still_deletes_from_the_db(self, db_path):
        _add_event(db_path, "ev1", "First")
        query, _, _ = await self._yes("ev1", sheet=None)
        assert _count(db_path, "events", "ev1") == 0
        assert "not connected" in query.edit_message_text.call_args.args[0]

    async def test_telegram_failures_do_not_undo_or_fail_the_deletion(self, db_path):
        _add_event(db_path, "ev1", "First")
        _add_children(db_path, "ev1")
        query, _, _ = await self._yes("ev1", tg_error=Exception("message can't be deleted"))
        assert _count(db_path, "events", "ev1") == 0
        assert "0 of 2 removed" in query.edit_message_text.call_args.args[0]

    async def test_unauthorized_clicker_cannot_delete(self, db_path):
        _add_event(db_path, "ev1", "First", created_by="1")
        query, bot, sheets_mock = await self._yes("ev1", user_id=99, admin=False)
        assert _count(db_path, "events", "ev1") == 1
        sheets_mock.assert_not_called()
        query.answer.assert_awaited_once()
        assert query.answer.call_args.kwargs.get("show_alert") is True

    async def test_creator_may_delete_their_own_event_but_not_others(self, db_path):
        _add_event(db_path, "mine", "Mine", created_by="42")
        _add_event(db_path, "theirs", "Theirs", created_by="1", message_id="501")
        await self._yes("mine", user_id=42, admin=False)
        await self._yes("theirs", user_id=42, admin=False)
        assert _count(db_path, "events", "mine") == 0
        assert _count(db_path, "events", "theirs") == 1

    async def test_cannot_reach_another_hubs_event_with_a_forged_hub_id(self, db_path):
        _add_event(db_path, "evX", "Other hub's event", chat_id="-300")
        _add_event(db_path, "mine", "Mine here")  # so the clicker has *something* deletable in HUB
        query, _, sheets_mock = await self._yes("evX")
        assert _count(db_path, "events", "evX") == 1
        sheets_mock.assert_not_called()

    async def test_double_tap_on_yes_is_harmless(self, db_path):
        _add_event(db_path, "ev1", "First")
        _add_event(db_path, "ev2", "Second", message_id="501")
        await self._yes("ev1")
        query, _, sheets_mock = await self._yes("ev1")  # second press: event already gone
        assert query.answer.call_args.kwargs.get("show_alert") is True
        sheets_mock.assert_not_called()

    async def test_a_pending_view_refresh_for_the_deleted_event_does_not_crash(self, db_path):
        _add_event(db_path, "ev1", "First")
        await self._yes("ev1")
        ctx = make_context(bot=make_bot())
        await event_engine.update_all_shared_views(ctx, "ev1")  # must simply find nothing and return


class TestFeatureGate:
    """deleteevent is its own feature (all_features), seeded owner-level
    (OWNER). The gate follows whatever min_tier is set NOW, so opening it up
    later is a plain /updatefeature change."""

    def test_seeded_as_owner_level(self, seeded_db):
        assert subscription.feature_min_tier("deleteevent") == "OWNER"

    async def test_non_owner_gets_total_silence_even_as_group_admin(self, seeded_db):
        _add_event(seeded_db, "ev1", "First")
        with patch("subscription.OWNER_USER_IDS", {OWNER}):
            msg = await _command([], user_id=1, admin=True)
        msg.reply_text.assert_not_called()

    async def test_owner_can_use_it(self, seeded_db):
        _add_event(seeded_db, "ev1", "First")
        with patch("subscription.OWNER_USER_IDS", {OWNER}):
            msg = await _command([], user_id=OWNER)
        text, _ = _reply(msg)
        assert "Pick the event to delete" in text

    async def test_stale_button_of_a_non_owner_does_nothing_but_dismisses_the_spinner(self, seeded_db):
        _add_event(seeded_db, "ev1", "First")
        upd, ctx, query, bot = _callback(f"delevyes_{HUB}:ev1", user_id=1)
        sheets_mock = AsyncMock(return_value=SHEET_COUNTS)
        with patch("subscription.OWNER_USER_IDS", {OWNER}), patch("handlers.delete_event_from_sheet", sheets_mock):
            await handlers.deleteevent_callback_handler(upd, ctx)
        assert _count(seeded_db, "events", "ev1") == 1
        sheets_mock.assert_not_called()
        query.answer.assert_awaited_once()
        query.edit_message_text.assert_not_called()

    async def test_owner_button_press_deletes(self, seeded_db):
        _add_event(seeded_db, "ev1", "First")
        upd, ctx, query, bot = _callback(f"delevyes_{HUB}:ev1", user_id=OWNER)
        with patch("subscription.OWNER_USER_IDS", {OWNER}), \
             patch("handlers.delete_event_from_sheet", AsyncMock(return_value=SHEET_COUNTS)):
            await handlers.deleteevent_callback_handler(upd, ctx)
        assert _count(seeded_db, "events", "ev1") == 0

    async def test_lowered_to_pro_a_premium_hubs_admin_can_use_it(self, seeded_db):
        _set_tier(seeded_db, "PRO")
        _make_premium(seeded_db)
        _add_event(seeded_db, "ev1", "First")
        with patch("subscription.OWNER_USER_IDS", {OWNER}):
            msg = await _command([], user_id=1, admin=True)
        text, _ = _reply(msg)
        assert "Pick the event to delete" in text

    async def test_lowered_to_pro_a_free_hub_gets_the_upgrade_message(self, seeded_db):
        _set_tier(seeded_db, "PRO")
        _add_event(seeded_db, "ev1", "First")
        with patch("subscription.OWNER_USER_IDS", {OWNER}):
            msg = await _command([], user_id=1, admin=True)
        text, markup = _reply(msg)
        assert "PRO" in text and markup is None

    async def test_lowered_to_free_everyone_with_rights_can_use_it(self, seeded_db):
        _set_tier(seeded_db, "FREE")
        _add_event(seeded_db, "ev1", "First")
        with patch("subscription.OWNER_USER_IDS", {OWNER}):
            msg = await _command([], user_id=1, admin=True)
        text, _ = _reply(msg)
        assert "Pick the event to delete" in text

    def test_feature_available_matrix(self, seeded_db):
        with patch("subscription.OWNER_USER_IDS", {OWNER}):
            assert subscription.feature_available(HUB, OWNER, "deleteevent") is True
            assert subscription.feature_available(HUB, 1, "deleteevent") is False
            assert subscription.feature_available(HUB, OWNER, "no_such_feature") is False

    def test_help_line_is_shown_only_when_the_command_is_available(self, seeded_db):
        import help_system
        assert "/deleteevent" not in help_system._build_main_help_text()
        assert "/deleteevent" in help_system._build_main_help_text(has_deleteevent=True)


class TestHelpMentions:
    """/deleteevent must be documented wherever it is available - and only
    shown to people who can actually use it."""

    @staticmethod
    def _owner_only():
        return patch("subscription.OWNER_USER_IDS", {OWNER})

    async def _main_help(self, user_id):
        import help_system
        chat = make_chat(chat_id=int(HUB), chat_type="supergroup")
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=make_user(user_id=user_id), message=msg)
        await help_system.help_command(upd, make_context(args=[]))
        return msg.reply_text.call_args.args[0]

    async def _lifecycle_section(self, user_id):
        import help_system
        chat = make_chat(chat_id=int(HUB), chat_type="supergroup")
        msg = make_message(chat=chat)
        query = MagicMock()
        query.data = "help_lifecycle"
        query.message = msg
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        upd = make_update(chat=chat, user=make_user(user_id=user_id), message=msg)
        upd.callback_query = query
        await help_system.help_callback_handler(upd, make_context())
        return query.edit_message_text.call_args.args[0]

    async def test_main_help_shows_it_to_the_owner(self, seeded_db):
        with self._owner_only():
            assert "/deleteevent" in await self._main_help(OWNER)

    async def test_main_help_hides_it_from_everyone_else(self, seeded_db):
        with self._owner_only():
            assert "/deleteevent" not in await self._main_help(1)

    async def test_main_help_shows_it_to_all_once_opened_up(self, seeded_db):
        _set_tier(seeded_db, "FREE")
        with self._owner_only():
            assert "/deleteevent" in await self._main_help(1)

    async def test_event_lifecycle_section_points_to_it_for_the_owner_only(self, seeded_db):
        with self._owner_only():
            assert "/deleteevent" in await self._lifecycle_section(OWNER)
            assert "/deleteevent" not in await self._lifecycle_section(1)

    def test_owner_help_documents_it_as_a_separate_not_dm_only_block(self, seeded_db):
        import help_system
        text = help_system._build_owner_help_text()
        assert "/deleteevent" in text
        assert "not DM" in text, "the header above says owner commands are DM-only - this one isn't"
        assert text.index("/stats") < text.index("/deleteevent")
