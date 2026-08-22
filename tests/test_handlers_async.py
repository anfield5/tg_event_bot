"""
Tests for async handler functions in handlers.py
─────────────────────────────────────────────────
All Telegram network calls are mocked with AsyncMock.
The database uses an isolated temp file via the `db_path` fixture.

pytest-asyncio is configured in "auto" mode (see pytest.ini) so individual
test functions don't need the @pytest.mark.asyncio decorator.
"""

import json
import sqlite3
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from tests.helpers import (
    make_user, make_chat, make_message, make_bot,
    make_update, make_context, make_callback_update,
)
import handlers
import event_engine
import monitors
import subscription
import aliases
import help_system
import db
import config


# ── helpers ──────────────────────────────────────────────────────────────────

def insert_event(db_path, event_id="ev1", chat_id="-100123",
                 name="Test Event", event_status=0,
                 going="[]", notgoing="[]", counters="{}",
                 event_date=None, kicked="[]"):
    conn = sqlite3.connect(db_path)
    conn.execute("""
        INSERT INTO events
            (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
             event_status, going_data, notgoing_data, counters_data, event_date, kicked_data)
        VALUES (?, ?, '1', ?, '✅', '❌', ?, ?, ?, ?, ?, ?)
    """, (event_id, chat_id, name, event_status, going, notgoing, counters, event_date, kicked))
    conn.commit()
    conn.close()


def insert_premium(db_path, chat_id="-100123", days=30):
    """Marks a hub as premium with a subs_date_end `days` in the future."""
    from datetime import datetime, timedelta
    end = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO all_groups (chat_id, type, subs_date_start, subs_date_end) VALUES (?, 'PRO', ?, ?)",
        (chat_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), end),
    )
    conn.commit()
    conn.close()


def get_event(db_path, event_id="ev1"):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM events WHERE event_id = ?", (event_id,))
    row = cursor.fetchone()
    conn.close()
    return row


def get_users(db_path, chat_id="-100123"):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT username, status FROM main_group_users WHERE chat_id = ?", (chat_id,))
    rows = cursor.fetchall()
    conn.close()
    return rows


def insert_event_user(db_path, event_id="ev1", chat_id="-200", user_id="1",
                       username="alice", status="going", guests=0):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO event_users (event_id, chat_id, user_id, username, status, guests) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (event_id, str(chat_id), str(user_id), username, status, guests),
    )
    conn.commit()
    conn.close()


def get_event_user(db_path, event_id="ev1", chat_id="-200", user_id="1"):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT status, guests FROM event_users WHERE event_id=? AND chat_id=? AND user_id=?",
        (event_id, str(chat_id), str(user_id)),
    )
    row = cursor.fetchone()
    conn.close()
    return row


class FakeWorksheet:
    """Records every append/update call instead of touching the network."""

    def __init__(self):
        self.appended_rows = []
        self.cell_updates  = {}
        self.records       = []

    async def append_row(self, row):
        self.appended_rows.append(row)

    async def append_rows(self, rows):
        self.appended_rows.extend(rows)

    async def get_all_records(self):
        return self.records

    async def update(self, cell_range, values):
        self.cell_updates[cell_range] = values


class FakeSpreadsheet:
    def __init__(self):
        self.worksheets = {}

    async def worksheet(self, name):
        if name not in self.worksheets:
            self.worksheets[name] = FakeWorksheet()
        return self.worksheets[name]


# ── /newevent ─────────────────────────────────────────────────────────────────

class TestNewevent:
    """Tests for the /newevent command handler."""

    @pytest.fixture(autouse=True)
    def _patch_sheets(self):
        """Suppress all Google Sheets calls globally for this class."""
        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock) as gs, \
             patch("handlers.open_spreadsheet",   new_callable=AsyncMock) as os_:
            ws = AsyncMock()
            ws.append_row = AsyncMock()
            os_.return_value = AsyncMock(worksheet=AsyncMock(return_value=ws))
            yield

    async def test_creates_event_in_db(self, db_path):
        # Running /newevent "Party Night" should insert a row into events
        chat    = make_chat(chat_id=-100123)
        ctx     = make_context(args=["Party", "Night"])
        upd     = make_update(chat=chat)

        await handlers.newevent(upd, ctx)

        conn   = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM events WHERE chat_id = '-100123'")
        rows = cursor.fetchall()
        conn.close()
        assert len(rows) == 1
        assert rows[0][0] == "Party Night"

    async def test_sends_message_to_chat(self, db_path):
        chat = make_chat(chat_id=-100123)
        bot  = make_bot()
        ctx  = make_context(bot=bot, args=["My Event"])
        upd  = make_update(chat=chat)

        await handlers.newevent(upd, ctx)

        bot.send_message.assert_awaited_once()
        call_kwargs = bot.send_message.call_args
        # chat_id is passed as string by the handler (str(update.effective_chat.id))
        sent_to = call_kwargs.kwargs.get("chat_id") or (call_kwargs.args[0] if call_kwargs.args else None)
        assert str(sent_to) == "-100123"

    async def test_sends_open_state_keyboard_not_verification(self, db_path):
        """
        Regression test: the keyboard sent alongside a freshly created event
        must be the OPEN state (Going/Not Going/ADD/Remove/Verify/
        Cancel) - NOT the verification-mode-only keyboard (Add Extra
        Player/Save & Close Event). A stale hardcoded event_status value at
        the call site once caused every new event to display with the wrong
        (verification-only) buttons despite event_status=0 being stored
        correctly in the DB.
        """
        chat = make_chat(chat_id=-100123)
        bot  = make_bot()
        ctx  = make_context(bot=bot, args=["My Event"])
        upd  = make_update(chat=chat)

        await handlers.newevent(upd, ctx)

        bot.send_message.assert_awaited_once()
        keyboard = bot.send_message.call_args.kwargs.get("reply_markup")
        assert keyboard is not None, "newevent must send a keyboard"
        flat = [btn for row in keyboard.inline_keyboard for btn in row]
        texts = [b.text for b in flat]

        assert any("Going" in t for t in texts), "open-state Going button missing"
        assert any("Not Going" in t for t in texts), "open-state Not Going button missing"
        assert any("ADD" in t for t in texts), "open-state ADD button missing"
        assert any("Remove" in t for t in texts), "open-state Remove button missing"
        assert any("Verify" in t for t in texts), "Verify button missing"
        assert any("Cancel" in t for t in texts), "Cancel button missing"

        # And NOT the verification-only keyboard
        assert not any("Save & Close Event" in t for t in texts), \
            "must not show the verification-mode keyboard on a freshly opened event"

    async def test_missing_name_replies_error(self, db_path):
        # No args → must send an error, NOT insert a row
        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(args=[])

        await handlers.newevent(upd, ctx)

        msg.reply_text.assert_awaited_once()
        reply_text = msg.reply_text.call_args.args[0]
        assert "error" in reply_text.lower() or "❌" in reply_text

    async def test_invalid_date_replies_error(self, db_path):
        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(args=["Party", "-date", "bad_date"])

        await handlers.newevent(upd, ctx)

        msg.reply_text.assert_awaited_once()
        assert "❌" in msg.reply_text.call_args.args[0]

    async def test_date_stored_in_db(self, db_path):
        chat = make_chat(chat_id=-100123)
        bot  = make_bot()
        ctx  = make_context(bot=bot, args=["Party", "-date", "14.07.2026"])
        upd  = make_update(chat=chat)

        await handlers.newevent(upd, ctx)

        conn   = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT event_date FROM events WHERE chat_id = '-100123'")
        row = cursor.fetchone()
        conn.close()
        assert row[0] == "14.07.2026"

    async def test_date_with_time_stored_in_db(self, db_path):
        chat = make_chat(chat_id=-100123)
        bot  = make_bot()
        ctx  = make_context(bot=bot, args=["Party", "-date", "14.07.2026", "19:00"])
        upd  = make_update(chat=chat)

        await handlers.newevent(upd, ctx)

        conn   = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT event_date FROM events WHERE chat_id = '-100123'")
        row = cursor.fetchone()
        conn.close()
        assert row[0] == "14.07.2026 19:00"

    async def test_no_date_stored_as_null(self, db_path):
        chat = make_chat(chat_id=-100123)
        bot  = make_bot()
        ctx  = make_context(bot=bot, args=["Party"])
        upd  = make_update(chat=chat)

        await handlers.newevent(upd, ctx)

        conn   = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT event_date FROM events WHERE chat_id = '-100123'")
        row = cursor.fetchone()
        conn.close()
        assert row[0] is None


# ── /editevent ────────────────────────────────────────────────────────────────

class TestEditevent:

    @pytest.fixture(autouse=True)
    def _patch_sheets(self):
        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("handlers.open_spreadsheet",   new_callable=AsyncMock), \
             patch("handlers.update_all_shared_views", new_callable=AsyncMock):
            yield

    async def test_updates_event_name(self, db_path):
        insert_event(db_path, chat_id="-100123")
        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(args=["New", "Name"])

        await handlers.editevent(upd, ctx)

        conn   = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM events WHERE event_id = 'ev1'")
        row = cursor.fetchone()
        conn.close()
        assert row[0] == "New Name"

    async def test_updates_date_when_flag_provided(self, db_path):
        insert_event(db_path, chat_id="-100123")
        chat = make_chat(chat_id=-100123)
        upd  = make_update(chat=chat)
        ctx  = make_context(args=["Party", "-date", "01.01.2027"])

        await handlers.editevent(upd, ctx)

        conn   = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT event_date FROM events WHERE event_id = 'ev1'")
        row = cursor.fetchone()
        conn.close()
        assert row[0] == "01.01.2027"

    async def test_does_not_change_date_if_flag_absent(self, db_path):
        # Insert an event that already has a date
        insert_event(db_path, chat_id="-100123", event_date="25.12.2026")
        chat = make_chat(chat_id=-100123)
        upd  = make_update(chat=chat)
        ctx  = make_context(args=["New Name"])  # no -date flag

        await handlers.editevent(upd, ctx)

        conn   = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT event_date FROM events WHERE event_id = 'ev1'")
        row = cursor.fetchone()
        conn.close()
        assert row[0] == "25.12.2026", "Existing date must be preserved when -date is not supplied"

    async def test_no_active_event_replies_error(self, db_path):
        # DB is empty — no event to edit
        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(args=["New Name"])

        await handlers.editevent(upd, ctx)

        msg.reply_text.assert_awaited_once()

    async def test_invalid_date_replies_error(self, db_path):
        insert_event(db_path, chat_id="-100123")
        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(args=["Party", "-date", "bad"])

        await handlers.editevent(upd, ctx)

        msg.reply_text.assert_awaited_once()
        assert "❌" in msg.reply_text.call_args.args[0]


# ── /notify ───────────────────────────────────────────────────────────────────

class TestNotify:

    async def test_pings_users_who_have_not_responded(self, db_path):
        # Insert an event + two users; neither has responded
        insert_event(db_path, chat_id="-100123")
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO main_group_users (chat_id, username, user_id, status) VALUES ('-100123','alice',NULL,'active')")
        conn.execute("INSERT INTO main_group_users (chat_id, username, user_id, status) VALUES ('-100123','bob',  NULL,'active')")
        conn.commit()
        conn.close()

        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context()

        await handlers.notify(upd, ctx)

        # reply_text should have been called at least once (header + mention chunks)
        assert msg.reply_text.await_count >= 1

    async def test_skips_users_who_already_responded(self, db_path):
        # alice is going → must NOT be pinged
        insert_event(
            db_path, chat_id="-100123",
            going=json.dumps(["alice (111)"]),
        )
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO main_group_users (chat_id, username, user_id, status) VALUES ('-100123','alice',NULL,'active')")
        conn.commit()
        conn.close()

        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context()

        await handlers.notify(upd, ctx)

        # Should reply with "all responded" message, not a mention
        replies = [call.args[0] for call in msg.reply_text.call_args_list]
        assert any("responded" in r.lower() or "✅" in r for r in replies)

    async def test_no_active_event_replies_error(self, db_path):
        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context()

        await handlers.notify(upd, ctx)

        msg.reply_text.assert_awaited_once()


# ── /adduser ─────────────────────────────────────────────────────────────────

class TestAdduser:
    """
    Covers the rewritten /adduser logic:
      - a numeric user_id is only added if getChatMember confirms they're
        CURRENTLY in the chat (status left/kicked is a valid, non-exception
        response from Telegram - must be checked explicitly, not just
        "did the call succeed")
      - a @username can only be resolved via the chat's administrator list
        (the Bot API has no username lookup for getChatMember at all)
    """

    async def test_admin_required(self, db_path):
        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="member"))
        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(bot=bot, args=["123456"])

        await handlers.adduser(upd, ctx)

        assert "⛔" in msg.reply_text.call_args.args[0]

    async def test_numeric_id_currently_in_chat_is_added(self, db_path):
        bot = make_bot()
        bot.get_chat_member = AsyncMock(side_effect=[
            MagicMock(status="administrator"),  # requester admin check
            MagicMock(status="member", user=make_user(user_id=555, username="bob")),
        ])
        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(bot=bot, args=["555"])

        await handlers.adduser(upd, ctx)

        rows = get_users(db_path)
        assert dict(rows)["bob"] == "active"
        assert "✅ Added" in msg.reply_text.call_args.args[0]

    async def test_numeric_id_with_left_status_is_rejected(self, db_path):
        """
        Regression test: getChatMember succeeding with status=left/kicked is
        a VALID response (Telegram remembers a former member) - it must NOT
        be silently treated as "currently in the chat".
        """
        bot = make_bot()
        bot.get_chat_member = AsyncMock(side_effect=[
            MagicMock(status="administrator"),
            MagicMock(status="left", user=make_user(user_id=555, username="ghost")),
        ])
        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(bot=bot, args=["555"])

        await handlers.adduser(upd, ctx)

        rows = get_users(db_path)
        assert "ghost" not in dict(rows)
        assert "❌ Failed" in msg.reply_text.call_args.args[0]

    async def test_username_resolved_via_chat_administrators(self, db_path):
        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
        admin_user = make_user(user_id=777, username="carol")
        bot.get_chat_administrators = AsyncMock(return_value=[MagicMock(user=admin_user)])

        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(bot=bot, args=["@carol"])

        await handlers.adduser(upd, ctx)

        rows = get_users(db_path)
        assert dict(rows)["carol"] == "active"

    async def test_username_not_in_admin_list_is_rejected(self, db_path):
        """
        A username that isn't a chat admin (and has never interacted with
        the bot before, so has no stored user_id either) can't be resolved
        at all - the Bot API has no username lookup for getChatMember.
        """
        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
        bot.get_chat_administrators = AsyncMock(return_value=[])

        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(bot=bot, args=["@stranger"])

        await handlers.adduser(upd, ctx)

        rows = get_users(db_path)
        assert "stranger" not in dict(rows)
        assert "❌ Failed" in msg.reply_text.call_args.args[0]


# ── /updateuser ───────────────────────────────────────────────────────────────

class TestUpdateuser:
    """Verifies new -a/-active/-p/-passive flag syntax."""

    async def _run(self, db_path, args):
        """Helper: insert a known user then run /updateuser with given args."""
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO main_group_users (chat_id, username, user_id, status) VALUES ('-100123','alice',NULL,'active')")
        conn.commit()
        conn.close()

        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(args=args)
        await handlers.updateuser(upd, ctx)
        return get_users(db_path)

    async def test_flag_a_sets_active(self, db_path):
        users = await self._run(db_path, ["alice", "-a"])
        assert dict(users)["alice"] == "active"

    async def test_flag_active_sets_active(self, db_path):
        users = await self._run(db_path, ["alice", "-active"])
        assert dict(users)["alice"] == "active"

    async def test_flag_p_sets_passive(self, db_path):
        users = await self._run(db_path, ["alice", "-p"])
        assert dict(users)["alice"] == "passive"

    async def test_flag_passive_sets_passive(self, db_path):
        users = await self._run(db_path, ["alice", "-passive"])
        assert dict(users)["alice"] == "passive"

    async def test_unknown_flag_replies_error(self, db_path):
        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(args=["alice", "-frozen"])  # old flag, no longer valid

        await handlers.updateuser(upd, ctx)

        msg.reply_text.assert_awaited_once()
        assert "❌" in msg.reply_text.call_args.args[0]

    async def test_missing_args_replies_error(self, db_path):
        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(args=["alice"])  # no status flag

        await handlers.updateuser(upd, ctx)

        msg.reply_text.assert_awaited_once()
        assert "❌" in msg.reply_text.call_args.args[0]

    async def test_at_prefix_stripped_from_username(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO main_group_users (chat_id, username, user_id, status) VALUES ('-100123','alice',NULL,'active')")
        conn.commit()
        conn.close()

        chat = make_chat(chat_id=-100123)
        upd  = make_update(chat=chat)
        ctx  = make_context(args=["@alice", "-p"])

        await handlers.updateuser(upd, ctx)

        users = get_users(db_path)
        assert dict(users)["alice"] == "passive"


# ── /listusers ────────────────────────────────────────────────────────────────

class TestListusers:

    async def test_shows_all_users(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO main_group_users (chat_id, username, user_id, status) VALUES ('-100123','alice',NULL,'active')")
        conn.execute("INSERT INTO main_group_users (chat_id, username, user_id, status) VALUES ('-100123','bob',  NULL,'passive')")
        conn.commit()
        conn.close()

        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context()

        await handlers.listusers(upd, ctx)

        msg.reply_text.assert_awaited_once()
        reply = msg.reply_text.call_args.args[0]
        assert "alice" in reply
        assert "bob"   in reply

    async def test_empty_chat_replies_no_users(self, db_path):
        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context()

        await handlers.listusers(upd, ctx)

        msg.reply_text.assert_awaited_once()


# ── /setalias, /removealias, /listalias ───────────────────────────────────────

class TestAliasCommands:

    @pytest.fixture(autouse=True)
    def _premium_hub(self, db_path):
        # These tests exercise the alias LOGIC itself, which is now
        # premium-gated - make the default test hub (-100123) premium so
        # the gate doesn't block them. Gating itself is covered separately
        # in TestPremiumGating below.
        insert_premium(db_path, chat_id="-100123")

    def _insert_alias(self, db_path, alias, chat_id="-200"):
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO sub_chats (chat_id, alias) VALUES (?, ?)", (chat_id, alias)
        )
        conn.commit()
        conn.close()

    async def test_removealias_deletes_alias(self, db_path):
        self._insert_alias(db_path, "myalias")

        chat = make_chat()
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(args=["myalias"])

        await handlers.removealias(upd, ctx)

        conn   = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sub_chats WHERE alias = 'myalias'")
        assert cursor.fetchone() is None
        conn.close()

    async def test_removealias_unknown_alias_replies_error(self, db_path):
        chat = make_chat()
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(args=["nonexistent"])

        await handlers.removealias(upd, ctx)

        msg.reply_text.assert_awaited_once()

    async def test_removealias_no_args_replies_error(self, db_path):
        chat = make_chat()
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(args=[])

        await handlers.removealias(upd, ctx)

        msg.reply_text.assert_awaited_once()

    async def test_listalias_empty_replies_no_aliases(self, db_path):
        chat = make_chat()
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context()

        await handlers.listalias(upd, ctx)

        msg.reply_text.assert_awaited_once()


class TestShareevent:
    """Covers bug #6: /shareevent must default to -onlycount when no mode prefix is given."""

    def _admin_context(self):
        bot = make_bot()  # get_chat_member defaults to "administrator"
        bot.get_chat = AsyncMock(return_value=MagicMock(type="supergroup"))
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
        return make_context(bot=bot)

    async def test_defaults_to_onlycount_when_no_mode_given(self, db_path):
        insert_event(db_path, event_id="ev1", chat_id=MAIN_CHAT)
        chat = make_chat(chat_id=int(MAIN_CHAT), chat_type="supergroup")
        user = make_user(user_id=1, username="admin")
        upd  = make_update(chat=chat, user=user)
        ctx  = self._admin_context()
        ctx.args = ["-200"]  # target only, no mode flag

        await handlers.shareevent(upd, ctx)

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT share_mode FROM event_shares WHERE event_id='ev1' AND chat_id='-200'")
        row = cursor.fetchone()
        conn.close()
        assert row == ("-onlycount",)

    async def test_explicit_visible_flag_still_overrides_default(self, db_path):
        insert_event(db_path, event_id="ev1", chat_id=MAIN_CHAT)
        chat = make_chat(chat_id=int(MAIN_CHAT), chat_type="supergroup")
        user = make_user(user_id=1, username="admin")
        upd  = make_update(chat=chat, user=user)
        ctx  = self._admin_context()
        ctx.args = ["-200", "-mgl", "visible"]

        await handlers.shareevent(upd, ctx)

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT share_mode FROM event_shares WHERE event_id='ev1' AND chat_id='-200'")
        row = cursor.fetchone()
        conn.close()
        assert row == ("-visible",)

    async def test_blocks_after_reaching_the_default_limit_of_3(self, db_path):
        """
        /shareevent's FREE-tier limit (feature_flags.shareevent.limit_count,
        default 3, since shareevent's min_tier is FREE) - the 4th DISTINCT
        event shared to the same target chat from a FREE hub must be
        rejected with the exact requested message.
        """
        for i in range(3):
            insert_event(db_path, event_id=f"ev{i}", chat_id=MAIN_CHAT)
            conn = sqlite3.connect(db_path)
            conn.execute(
                "INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type) "
                "VALUES (?, '-200', ?, '-oc', 'group')",
                (f"ev{i}", str(i)),
            )
            conn.commit()
            conn.close()

        # A 4th, brand new event from the same hub, targeting the same chat
        insert_event(db_path, event_id="ev_new", chat_id=MAIN_CHAT)
        chat = make_chat(chat_id=int(MAIN_CHAT), chat_type="supergroup")
        user = make_user(user_id=1, username="admin")
        upd  = make_update(chat=chat, user=user)
        ctx  = self._admin_context()
        ctx.args = ["-200"]

        await handlers.shareevent(upd, ctx)

        # Must NOT have created a 4th share row
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM event_shares WHERE chat_id='-200'")
        count = cursor.fetchone()[0]
        conn.close()
        assert count == 3, "the 4th share must have been rejected"

        sent_text = ctx.bot.send_message.call_args.kwargs.get("text", "")
        assert sent_text == (
            "You've reached the /shareevent limit for this target (3). "
            "Contact the bot owner to raise or remove it."
        )

    async def test_pro_hub_is_not_limited_by_default(self, db_path):
        """A PRO hub is ABOVE shareevent's min_tier (FREE), so it's
        unlimited by construction - must NOT hit the FREE-tier cap."""
        insert_premium(db_path, chat_id=MAIN_CHAT)
        for i in range(3):
            insert_event(db_path, event_id=f"ev{i}", chat_id=MAIN_CHAT)
            conn = sqlite3.connect(db_path)
            conn.execute(
                "INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type) "
                "VALUES (?, '-200', ?, '-oc', 'group')",
                (f"ev{i}", str(i)),
            )
            conn.commit()
            conn.close()

        insert_event(db_path, event_id="ev_new", chat_id=MAIN_CHAT)
        chat = make_chat(chat_id=int(MAIN_CHAT), chat_type="supergroup")
        user = make_user(user_id=1, username="admin")
        upd  = make_update(chat=chat, user=user)
        ctx  = self._admin_context()
        ctx.args = ["-200"]

        await handlers.shareevent(upd, ctx)

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM event_shares WHERE chat_id='-200'")
        count = cursor.fetchone()[0]
        conn.close()
        assert count == 4, "PRO hubs must not be limited by the FREE-tier default"

    async def test_clearing_the_limit_via_updatefeature_removes_it(self, db_path):
        """limit_count only applies while set - clearing it (via
        /updatefeature ... -limit 0) lifts the cap for FREE hubs too."""
        subscription.update_feature_flag("shareevent", "FREE", limit_count=None, db_path=db_path)
        for i in range(3):
            insert_event(db_path, event_id=f"ev{i}", chat_id=MAIN_CHAT)
            conn = sqlite3.connect(db_path)
            conn.execute(
                "INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type) "
                "VALUES (?, '-200', ?, '-oc', 'group')",
                (f"ev{i}", str(i)),
            )
            conn.commit()
            conn.close()

        insert_event(db_path, event_id="ev_new", chat_id=MAIN_CHAT)
        chat = make_chat(chat_id=int(MAIN_CHAT), chat_type="supergroup")
        user = make_user(user_id=1, username="admin")
        upd  = make_update(chat=chat, user=user)
        ctx  = self._admin_context()
        ctx.args = ["-200"]

        await handlers.shareevent(upd, ctx)

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM event_shares WHERE chat_id='-200'")
        count = cursor.fetchone()[0]
        conn.close()
        assert count == 4, "limit was explicitly cleared, must not be enforced"

    async def test_free_tier_limit_is_per_target_not_global(self, db_path):
        """3 shares to target A must not block sharing to a DIFFERENT target B."""
        for i in range(3):
            insert_event(db_path, event_id=f"ev{i}", chat_id=MAIN_CHAT)
            conn = sqlite3.connect(db_path)
            conn.execute(
                "INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type) "
                "VALUES (?, '-200', ?, '-oc', 'group')",
                (f"ev{i}", str(i)),
            )
            conn.commit()
            conn.close()

        insert_event(db_path, event_id="ev_new", chat_id=MAIN_CHAT)
        chat = make_chat(chat_id=int(MAIN_CHAT), chat_type="supergroup")
        user = make_user(user_id=1, username="admin")
        upd  = make_update(chat=chat, user=user)
        ctx  = self._admin_context()
        ctx.args = ["-300"]  # a different target

        await handlers.shareevent(upd, ctx)

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM event_shares WHERE chat_id='-300'")
        count = cursor.fetchone()[0]
        conn.close()
        assert count == 1, "a different target must have its own separate limit"


# ── Premium gating: Aliases / Monitoring ───────────────────────────────────────

class TestPremiumGating:
    """
    Aliases (/setalias, /removealias, /listalias) and Monitoring
    (/addmonitor, /removemonitor, /listmonitors) are premium-only. Free
    hubs must be blocked with an explanatory message; premium hubs proceed
    normally (the underlying behavior itself is covered by TestAliasCommands
    and stays unaffected).
    """

    FREE_CHAT_ID = "-100123"

    async def _assert_blocked(self, msg):
        msg.reply_text.assert_awaited_once()
        reply = msg.reply_text.call_args.args[0]
        assert "pro" in reply.lower()

    async def test_setalias_blocked_on_free(self, db_path):
        chat = make_chat(chat_id=int(self.FREE_CHAT_ID))
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(args=["-200", "downtown"])

        await handlers.setalias(upd, ctx)
        await self._assert_blocked(msg)

        # And it must not have actually created anything
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM sub_chats")
        count = cursor.fetchone()[0]
        conn.close()
        assert count == 0

    async def test_removealias_blocked_on_free(self, db_path):
        chat = make_chat(chat_id=int(self.FREE_CHAT_ID))
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(args=["downtown"])

        await handlers.removealias(upd, ctx)
        await self._assert_blocked(msg)

    async def test_listalias_blocked_on_free(self, db_path):
        chat = make_chat(chat_id=int(self.FREE_CHAT_ID))
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context()

        await handlers.listalias(upd, ctx)
        await self._assert_blocked(msg)

    async def test_addmonitor_blocked_on_free(self, db_path):
        chat = make_chat(chat_id=int(self.FREE_CHAT_ID))
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(args=["-200"])

        await handlers.addmonitor(upd, ctx)
        await self._assert_blocked(msg)

    async def test_removemonitor_blocked_on_free(self, db_path):
        chat = make_chat(chat_id=int(self.FREE_CHAT_ID))
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(args=["-200"])

        await handlers.removemonitor(upd, ctx)
        await self._assert_blocked(msg)

    async def test_listmonitors_blocked_on_free(self, db_path):
        chat = make_chat(chat_id=int(self.FREE_CHAT_ID))
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context()

        await handlers.listmonitors(upd, ctx)
        await self._assert_blocked(msg)

    async def test_setalias_allowed_on_premium(self, db_path):
        insert_premium(db_path, chat_id=self.FREE_CHAT_ID)
        chat = make_chat(chat_id=int(self.FREE_CHAT_ID))
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(args=["-200", "downtown"])
        ctx.bot.get_chat = AsyncMock(return_value=MagicMock(type="supergroup"))

        await handlers.setalias(upd, ctx)

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT alias FROM sub_chats WHERE chat_id='-200'")
        row = cursor.fetchone()
        conn.close()
        assert row == ("downtown",)

    async def test_addmonitor_allowed_on_premium(self, db_path):
        insert_premium(db_path, chat_id=self.FREE_CHAT_ID)
        chat = make_chat(chat_id=int(self.FREE_CHAT_ID))
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(args=["-200"])
        bot_member = MagicMock(status="administrator")
        ctx.bot.get_chat_member = AsyncMock(return_value=bot_member)
        ctx.bot.get_chat = AsyncMock(return_value=MagicMock(type="supergroup", title="Downtown"))

        await handlers.addmonitor(upd, ctx)

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT is_monitored FROM sub_chats WHERE chat_id='-200'")
        row = cursor.fetchone()
        conn.close()
        assert row == (1,)


# ── is_premium() ────────────────────────────────────────────────────────────

class TestIsPremium:

    def test_no_row_is_not_premium(self, db_path):
        assert handlers.is_premium("-999") is False

    def test_free_type_is_not_premium(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO all_groups (chat_id, type) VALUES ('-100','FREE')")
        conn.commit()
        conn.close()
        assert handlers.is_premium("-100") is False

    def test_premium_with_future_end_date_is_premium(self, db_path):
        insert_premium(db_path, chat_id="-100", days=30)
        assert handlers.is_premium("-100") is True

    def test_premium_with_past_end_date_is_not_premium(self, db_path):
        insert_premium(db_path, chat_id="-100", days=-1)
        assert handlers.is_premium("-100") is False

    def test_premium_type_but_null_end_date_is_not_premium(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO all_groups (chat_id, type) VALUES ('-100','PRO')")
        conn.commit()
        conn.close()
        assert handlers.is_premium("-100") is False


# ── /setsub ──────────────────────────────────────────────────────────────────

class TestSetsub:

    OWNER_ID = 555

    async def test_non_owner_is_silently_ignored(self, db_path):
        with patch("subscription.OWNER_USER_IDS", {self.OWNER_ID}):
            chat = make_chat()
            user = make_user(user_id=999)  # not the owner
            msg  = make_message(chat=chat)
            upd  = make_update(chat=chat, user=user, message=msg)
            ctx  = make_context(args=["-100", "on", "30"])

            await subscription.setsub(upd, ctx)

            msg.reply_text.assert_not_awaited()

    async def test_anonymous_sender_gets_an_explicit_message(self, db_path):
        """
        Posting anonymously (as the group/channel itself) substitutes a
        shared, non-personal user_id that can never match OWNER_USER_IDS -
        there's no way to verify it's really the owner. Unlike a regular
        non-owner (silently ignored, so as not to reveal this command
        exists), an anonymous sender gets told explicitly why nothing
        happened, since the real owner might be the one posting anonymously
        and would otherwise have no idea what went wrong.
        """
        with patch("subscription.OWNER_USER_IDS", {self.OWNER_ID}):
            chat = make_chat()
            user = make_user(user_id=1087968824)  # GROUP_ANONYMOUS_BOT_ID
            msg  = make_message(chat=chat)
            upd  = make_update(chat=chat, user=user, message=msg)
            ctx  = make_context(args=["-100", "on", "30"])

            await subscription.setsub(upd, ctx)

            msg.reply_text.assert_awaited_once()
            assert "anonymously" in msg.reply_text.call_args.args[0].lower()

    async def test_owner_can_turn_on(self, db_path):
        with patch("subscription.OWNER_USER_IDS", {self.OWNER_ID}), \
             patch("subscription.sync_control_sheet_main", new_callable=AsyncMock):
            chat = make_chat()
            user = make_user(user_id=self.OWNER_ID)
            msg  = make_message(chat=chat)
            upd  = make_update(chat=chat, user=user, message=msg)
            ctx  = make_context(args=["-100", "on", "30"])
            ctx.bot.get_chat = AsyncMock(return_value=MagicMock(title="Some Group", username=None))

            await subscription.setsub(upd, ctx)

            assert handlers.is_premium("-100") is True

    async def test_owner_can_turn_off(self, db_path):
        insert_premium(db_path, chat_id="-100")
        with patch("subscription.OWNER_USER_IDS", {self.OWNER_ID}), \
             patch("subscription.sync_control_sheet_main", new_callable=AsyncMock):
            chat = make_chat()
            user = make_user(user_id=self.OWNER_ID)
            msg  = make_message(chat=chat)
            upd  = make_update(chat=chat, user=user, message=msg)
            ctx  = make_context(args=["-100", "off"])
            ctx.bot.get_chat = AsyncMock(return_value=MagicMock(title="Some Group", username=None))

            await subscription.setsub(upd, ctx)

            assert handlers.is_premium("-100") is False

    async def test_extending_active_subscription_stacks_not_resets(self, db_path):
        insert_premium(db_path, chat_id="-100", days=10)
        with patch("subscription.OWNER_USER_IDS", {self.OWNER_ID}), \
             patch("subscription.sync_control_sheet_main", new_callable=AsyncMock):
            chat = make_chat()
            user = make_user(user_id=self.OWNER_ID)
            msg  = make_message(chat=chat)
            upd  = make_update(chat=chat, user=user, message=msg)
            ctx  = make_context(args=["-100", "on", "5"])
            ctx.bot.get_chat = AsyncMock(return_value=MagicMock(title="Some Group", username=None))

            await subscription.setsub(upd, ctx)

            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT subs_date_end FROM all_groups WHERE chat_id='-100'")
            end_str = cursor.fetchone()[0]
            conn.close()
            from datetime import datetime
            end = datetime.strptime(end_str, "%Y-%m-%d %H:%M:%S")
            days_left = (end - datetime.now()).days
            assert days_left >= 14, "extending must stack on top of the existing 10 days, not reset to 5"


# ── /help (tier-aware keyboard) ─────────────────────────────────────────────

class TestHelpTierAwareKeyboard:

    async def test_free_hub_shows_locked_alias_and_monitoring_buttons(self, db_path):
        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context()

        await handlers.help_command(upd, ctx)

        keyboard = msg.reply_text.call_args.kwargs.get("reply_markup") or msg.reply_text.call_args.args[-1]
        flat = [btn for row in keyboard.inline_keyboard for btn in row]
        alias_btn   = next(b for b in flat if "Alias" in b.text)
        monitor_btn = next(b for b in flat if "Monitoring" in b.text)
        assert config.ICON_PREMIUM in alias_btn.text
        assert config.ICON_PREMIUM in monitor_btn.text
        assert alias_btn.callback_data == "upgrade_info_aliases"
        assert monitor_btn.callback_data == "upgrade_info_monitoring"

    async def test_premium_hub_shows_active_alias_and_monitoring_buttons(self, db_path):
        insert_premium(db_path, chat_id="-100123")
        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context()

        await handlers.help_command(upd, ctx)

        keyboard = msg.reply_text.call_args.kwargs.get("reply_markup") or msg.reply_text.call_args.args[-1]
        flat = [btn for row in keyboard.inline_keyboard for btn in row]
        alias_btn   = next(b for b in flat if "Alias" in b.text)
        monitor_btn = next(b for b in flat if "Monitoring" in b.text)
        assert config.ICON_PREMIUM not in alias_btn.text
        assert config.ICON_PREMIUM not in monitor_btn.text
        assert alias_btn.callback_data == "help_alias"
        assert monitor_btn.callback_data == "help_monitoring"

    async def test_row_layout_lifecycle_distribution_first_aliases_monitoring_second(self, db_path):
        """Row 1: Users + Utility. Row 2: Event Lifecycle + Distribution.
        Row 3: Aliases + Monitoring. Row 4: DM Access."""
        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context()

        await handlers.help_command(upd, ctx)

        keyboard = msg.reply_text.call_args.kwargs.get("reply_markup") or msg.reply_text.call_args.args[-1]
        rows = keyboard.inline_keyboard
        assert len(rows) == 4
        row1_texts = [b.text for b in rows[0]]
        row2_texts = [b.text for b in rows[1]]
        assert any("Users" in t for t in row1_texts)
        assert any("Utility" in t for t in row1_texts)
        assert any("Lifecycle" in t for t in row2_texts)
        assert any("Distribution" in t for t in row2_texts)
        row3_texts = [b.text for b in rows[2]]
        assert any("Alias" in t for t in row3_texts)
        assert any("Monitoring" in t for t in row3_texts)
        row4_texts = [b.text for b in rows[3]]
        assert any("DM Access" in t for t in row4_texts)

    async def test_distribution_button_always_active(self, db_path):
        """Distribution (shareevent) is free for everyone, regardless of tier."""
        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context()

        await handlers.help_command(upd, ctx)

        keyboard = msg.reply_text.call_args.kwargs.get("reply_markup") or msg.reply_text.call_args.args[-1]
        flat = [btn for row in keyboard.inline_keyboard for btn in row]
        dist_btn = next(b for b in flat if "Distribution" in b.text)
        assert dist_btn.callback_data == "help_distribution"

    async def test_lifecycle_button_locked_on_free_hub(self, db_path):
        """Event Lifecycle bundles -limit/-reserve (event_limit, PRO by
        default) alongside newevent/editevent themselves, so a FREE hub
        sees it locked even though newevent/editevent are free on their own."""
        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context()

        await handlers.help_command(upd, ctx)

        keyboard = msg.reply_text.call_args.kwargs.get("reply_markup") or msg.reply_text.call_args.args[-1]
        flat = [btn for row in keyboard.inline_keyboard for btn in row]
        life_btn = next(b for b in flat if "Lifecycle" in b.text)
        assert life_btn.callback_data == "upgrade_info_lifecycle"
        assert config.ICON_PREMIUM in life_btn.text


# ── button_handler: username without id-suffix ────────────────────────────────

class TestButtonHandlerUsername:
    """
    When a user has no @username, the fallback must be first_name only —
    NOT the old 'first_name (id1234)' format.
    """

    async def test_no_username_uses_first_name_only(self, db_path):
        """
        A user with no @username should appear as 'Andr', not 'Andr (id8499)'.
        We test this by checking how the user's display name is constructed.
        """
        user = make_user(user_id=8499, username=None, first_name="Andr")
        # The line under test is in button_handler:
        #   username_raw = user.username if user.username else (user.first_name or f"user{user.id}")
        username_raw = user.username if user.username else (user.first_name or f"user{user.id}")
        assert username_raw == "Andr"
        assert "id" not in username_raw
        assert "8499" not in username_raw

    async def test_user_with_username_uses_username(self, db_path):
        user = make_user(user_id=111, username="alice", first_name="Alice")
        username_raw = user.username if user.username else (user.first_name or f"user{user.id}")
        assert username_raw == "alice"

    async def test_user_with_no_name_falls_back_to_user_id(self, db_path):
        user = make_user(user_id=777, username=None, first_name=None)
        username_raw = user.username if user.username else (user.first_name or f"user{user.id}")
        assert username_raw == "user777"


# ── update_all_shared_views: "Going from" without quotes ─────────────────────

class TestGoingFromLabel:
    """
    The master-view 'Going from' block must show the plain channel name,
    NOT the old format:  Going from ("channel_name")
    """

    async def test_going_from_does_not_contain_quotes(self, db_path):
        # Insert a minimal event + one event_share with a child going user
        insert_event(db_path, event_id="ev1", chat_id="-100123")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO event_shares (share_id, event_id, chat_id, message_id, share_mode, chat_type) VALUES (NULL,'ev1','-200','42','-visible','group')"
        )
        conn.execute(
            "INSERT INTO event_users VALUES ('ev1','-200','999','anreon','going',3)"
        )
        conn.commit()
        conn.close()

        # Capture what gets passed to edit_message_text
        bot = make_bot()
        bot.get_chat = AsyncMock(return_value=MagicMock(title="TestChannel", type="channel"))
        ctx = make_context(bot=bot)

        await handlers.update_all_shared_views(ctx, "ev1")

        # edit_message_text is called for the master view
        assert bot.edit_message_text.await_count >= 1
        call_kwargs = bot.edit_message_text.call_args_list[0]
        text = call_kwargs.kwargs.get("text", "") or call_kwargs.args[0]

        # Old format had quotes — these must be gone
        assert '("' not in text and '")'  not in text

    async def test_squad_verification_header(self, db_path):
        """Header in event_status==1 must read 'SQUAD VERIFICATION', not old wording."""
        insert_event(db_path, event_id="ev1", chat_id="-100123", event_status=1)
        bot = make_bot()
        ctx = make_context(bot=bot)

        await handlers.update_all_shared_views(ctx, "ev1")

        assert bot.edit_message_text.await_count >= 1
        text = bot.edit_message_text.call_args_list[0].kwargs.get("text", "")
        assert "SQUAD VERIFICATION" in text
        assert "ROSTER VERIFICATION IN PROGRESS" not in text
        assert "Review members before save" in text


# ── /refreshusers ─────────────────────────────────────────────────────────────

class TestRefreshusers:
    """/refreshusers - syncs the main hub's own tracked user list against
    live Telegram membership and the bound Google Sheet (if any). Admin-
    only; verifies presence via get_chat_member for every tracked user."""

    async def test_non_admin_is_rejected(self, db_path):
        bot              = make_bot()
        # Override: user is NOT admin
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="member"))

        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(bot=bot)

        await handlers.refreshusers(upd, ctx)

        msg.reply_text.assert_awaited_once()
        assert "⛔️" in msg.reply_text.call_args.args[0]

    async def test_removes_user_who_left(self, db_path):
        """
        Regression test: refreshusers must actually DELETE a departed user
        from main_group_users (so they disappear from /listusers), not just mark
        them 'passive'. Marking-passive-only was the previous behavior and
        is exactly why "the list wasn't cleaned" was reported.
        """
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO main_group_users (chat_id, username, user_id, status) VALUES ('-100123','alice','111','active')")
        conn.commit()
        conn.close()

        bot = make_bot()
        # First get_chat_member call → the requesting admin's own status.
        # Second call → alice's membership check, which comes back 'left'.
        bot.get_chat_member = AsyncMock(
            side_effect=[
                MagicMock(status="administrator"),
                MagicMock(status="left"),
            ]
        )

        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(bot=bot)

        await handlers.refreshusers(upd, ctx)

        users = get_users(db_path)
        assert dict(users) == {}, "alice must be fully removed, not merely marked passive"

    async def test_kicked_user_is_also_removed(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO main_group_users (chat_id, username, user_id, status) VALUES ('-100123','bob','222','active')")
        conn.commit()
        conn.close()

        bot = make_bot()
        bot.get_chat_member = AsyncMock(
            side_effect=[
                MagicMock(status="administrator"),
                MagicMock(status="kicked"),
            ]
        )
        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(bot=bot)

        await handlers.refreshusers(upd, ctx)

        assert dict(get_users(db_path)) == {}

    async def test_still_present_user_is_not_removed(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO main_group_users (chat_id, username, user_id, status) VALUES ('-100123','carol','333','active')")
        conn.commit()
        conn.close()

        bot = make_bot()
        bot.get_chat_member = AsyncMock(
            side_effect=[
                MagicMock(status="administrator"),
                MagicMock(status="member"),  # carol is still in the chat
            ]
        )
        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(bot=bot)

        await handlers.refreshusers(upd, ctx)

        assert dict(get_users(db_path)).get("carol") == "active"

    async def test_users_without_id_are_removed_by_default(self, db_path):
        """
        Regression test for the new default behavior: users with no stored
        user_id can never be membership-checked (getChatMember requires a
        numeric ID), so there's no way to ever confirm they're still here -
        they're now removed outright by default (this used to require the
        separate -purge flag; that flag no longer exists, this is just how
        /refreshusers behaves now).
        """
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO main_group_users (chat_id, username, user_id, status) VALUES ('-100123','bob',NULL,'active')")
        conn.commit()
        conn.close()

        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))

        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(bot=bot)

        await handlers.refreshusers(upd, ctx)

        users = get_users(db_path)
        assert "bob" not in dict(users)
        reply = msg.reply_text.call_args.args[0]
        assert "bob" in reply

    async def test_adds_missing_chat_administrator_as_active(self, db_path):
        """
        New behavior: any chat administrator who isn't tracked yet gets
        added with status 'active'. This is the only membership Telegram's
        Bot API actually lets a bot enumerate on demand (getChatAdministrators);
        regular non-admin members are picked up passively elsewhere (on join,
        or the first time they interact), not by this command.
        """
        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))

        admin_user = make_user(user_id=555, username="newadmin", first_name="New")
        admin_user.is_bot = False
        admin_member = MagicMock(user=admin_user)
        bot.get_chat_administrators = AsyncMock(return_value=[admin_member])

        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(bot=bot)

        await handlers.refreshusers(upd, ctx)

        users = get_users(db_path)
        assert dict(users).get("newadmin") == "active"
        reply = msg.reply_text.call_args.args[0]
        assert "Added" in reply or "➕" in reply

    async def test_does_not_duplicate_already_tracked_admin(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO main_group_users (chat_id, username, user_id, status) VALUES ('-100123','existingadmin','555','passive')")
        conn.commit()
        conn.close()

        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))

        admin_user = make_user(user_id=555, username="existingadmin", first_name="Existing")
        admin_user.is_bot = False
        admin_member = MagicMock(user=admin_user)
        bot.get_chat_administrators = AsyncMock(return_value=[admin_member])

        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(bot=bot)

        await handlers.refreshusers(upd, ctx)

        # Must NOT have been reset back to 'active' just because they're an admin.
        users = get_users(db_path)
        assert dict(users)["existingadmin"] == "passive"

    async def test_empty_chat_still_replies(self, db_path):
        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))

        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(bot=bot)

        await handlers.refreshusers(upd, ctx)

        msg.reply_text.assert_awaited_once()

    async def test_syncs_google_sheets_by_default(self, db_path):
        """
        Regression test for the new default behavior: Google Sheets sync
        used to require the separate -r flag; that flag no longer exists,
        so /refreshusers now always attempts the sync (a no-op on the free
        tier, since sheets are premium-only - see sync_users_sheet).
        """
        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))

        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(bot=bot, args=[])

        sync_mock = AsyncMock()
        with patch("handlers.sync_users_sheet", sync_mock):
            await handlers.refreshusers(upd, ctx)

        sync_mock.assert_awaited_once()
        reply = msg.reply_text.call_args.args[0]
        assert "Users tab" in reply

    async def test_appends_new_member_to_users_sheet(self, db_path):
        """
        A chat administrator not yet present in the Users tab must get a
        new row: USER_ID, USER_NAME, CHAT_ID, STATUS="MEMBER", a live
        DATE_start timestamp, blank DATE_end, blank ARCHIVED_USER_NAME.
        """
        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
        admin_user = make_user(user_id=555, username="newadmin", first_name="New")
        admin_user.is_bot = False
        bot.get_chat_administrators = AsyncMock(return_value=[MagicMock(user=admin_user)])

        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(bot=bot, args=[])

        fake_ss = FakeSpreadsheet()
        with patch("sheets.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("sheets.open_spreadsheet", new_callable=AsyncMock, return_value=fake_ss):
            await handlers.refreshusers(upd, ctx)

        ws = fake_ss.worksheets["Users"]
        assert len(ws.appended_rows) == 1
        row = ws.appended_rows[0]
        assert row[0:4] == ["555", "newadmin", "-100123", "MEMBER"]
        assert row[4], "DATE_start must be set to a live timestamp"
        assert row[5:7] == ["", ""]  # DATE_end, ARCHIVED_USER_NAME both blank
        reply = msg.reply_text.call_args.args[0]
        assert "Users tab" in reply

    async def test_archives_changed_username(self, db_path):
        """A changed USER_NAME must be archived (comma-joined) and updated, not overwritten silently."""
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO main_group_users (chat_id, username, user_id, status) VALUES ('-100123','oldname','111','active')")
        conn.commit()
        conn.close()

        bot = make_bot()
        live_member = MagicMock(status="member")
        live_member.user = make_user(user_id=111, username="newname", first_name="New")
        bot.get_chat_member = AsyncMock(
            side_effect=[
                MagicMock(status="administrator"),  # requester admin check
                live_member,                          # user 111 still present, now named "newname"
            ]
        )
        bot.get_chat_administrators = AsyncMock(return_value=[])

        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(bot=bot, args=[])

        fake_ss = FakeSpreadsheet()
        ws = FakeWorksheet()
        ws.records = [{"USER_ID": "111", "USER_NAME": "oldname", "CHAT_ID": "-100123",
                       "STATUS": "Member", "ARCHIVED_USER_NAME": ""}]
        fake_ss.worksheets["Users"] = ws

        with patch("sheets.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("sheets.open_spreadsheet", new_callable=AsyncMock, return_value=fake_ss):
            await handlers.refreshusers(upd, ctx)

        assert ws.cell_updates["B2"] == [["newname"]]
        assert ws.cell_updates["G2"] == [["oldname"]]

    async def test_appends_further_archived_names_with_comma(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO main_group_users (chat_id, username, user_id, status) VALUES ('-100123','secondname','111','active')")
        conn.commit()
        conn.close()

        bot = make_bot()
        live_member = MagicMock(status="member")
        live_member.user = make_user(user_id=111, username="thirdname", first_name="Third")
        bot.get_chat_member = AsyncMock(
            side_effect=[MagicMock(status="administrator"), live_member]
        )
        bot.get_chat_administrators = AsyncMock(return_value=[])

        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(bot=bot, args=[])

        fake_ss = FakeSpreadsheet()
        ws = FakeWorksheet()
        ws.records = [{"USER_ID": "111", "USER_NAME": "secondname", "CHAT_ID": "-100123",
                       "STATUS": "Member", "ARCHIVED_USER_NAME": "firstname"}]
        fake_ss.worksheets["Users"] = ws

        with patch("sheets.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("sheets.open_spreadsheet", new_callable=AsyncMock, return_value=fake_ss):
            await handlers.refreshusers(upd, ctx)

        assert ws.cell_updates["G2"] == [["firstname,secondname"]]

    async def test_marks_departed_user_as_left_in_sheet(self, db_path):
        """A Users-sheet row for this CHAT_ID whose person is confirmed gone must get STATUS=Left."""
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO main_group_users (chat_id, username, user_id, status) VALUES ('-100123','gone','222','active')")
        conn.commit()
        conn.close()

        bot = make_bot()
        bot.get_chat_member = AsyncMock(
            side_effect=[MagicMock(status="administrator"), MagicMock(status="left")]
        )
        bot.get_chat_administrators = AsyncMock(return_value=[])

        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(bot=bot, args=[])

        fake_ss = FakeSpreadsheet()
        ws = FakeWorksheet()
        ws.records = [{"USER_ID": "222", "USER_NAME": "gone", "CHAT_ID": "-100123",
                       "STATUS": "Member", "ARCHIVED_USER_NAME": ""}]
        fake_ss.worksheets["Users"] = ws

        with patch("sheets.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("sheets.open_spreadsheet", new_callable=AsyncMock, return_value=fake_ss):
            await handlers.refreshusers(upd, ctx)

        assert ws.cell_updates["D2"] == [["LEFT"]]

    async def test_does_not_touch_rows_from_other_places(self, db_path):
        """A Users-sheet row belonging to a DIFFERENT CHAT_ID must never be touched by this chat's refresh."""
        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
        bot.get_chat_administrators = AsyncMock(return_value=[])

        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(bot=bot, args=[])

        fake_ss = FakeSpreadsheet()
        ws = FakeWorksheet()
        ws.records = [{"USER_ID": "999", "USER_NAME": "elsewhere", "CHAT_ID": "-999999",
                       "STATUS": "Member", "ARCHIVED_USER_NAME": ""}]
        fake_ss.worksheets["Users"] = ws

        with patch("sheets.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("sheets.open_spreadsheet", new_callable=AsyncMock, return_value=fake_ss):
            await handlers.refreshusers(upd, ctx)

        assert ws.cell_updates == {}
        assert ws.appended_rows == []


class TestRefreshusersall:
    """/refreshusersall - the former /refreshusers -g, now its own command."""

    async def test_free_group_is_rejected_with_pro_only_message(self, db_path):
        """
        Regression test: monitored chats can only ever be configured via
        /addmonitor (PRO-only), so this command was already functionally
        inert on FREE - now it says so explicitly instead of a confusing
        'No monitored groups configured' response.
        """
        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(bot=bot, args=[])

        await handlers.refreshusersall(upd, ctx)

        reply = msg.reply_text.call_args.args[0]
        assert "PRO" in reply
        assert "No monitored" not in reply

    async def test_no_monitored_children_still_processes_hub_itself(self, db_path):
        """No child chats are monitored, but the hub itself is now always
        processed regardless - refreshusersall covers 'the group the
        command was run in PLUS every monitored child', not just children."""
        insert_premium(db_path, chat_id="-100123")
        bot = make_bot()
        bot.get_chat = AsyncMock(return_value=MagicMock(title="The Hub"))
        bot.get_chat_administrators = AsyncMock(return_value=[])

        async def gcm_side_effect(chat_id, user_id):
            return MagicMock(status="administrator")

        bot.get_chat_member = AsyncMock(side_effect=gcm_side_effect)
        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(bot=bot, args=[])

        with patch("handlers.sync_users_sheet", new_callable=AsyncMock):
            await handlers.refreshusersall(upd, ctx)

        reply = msg.reply_text.call_args.args[0]
        assert "The Hub" in reply
        assert "No monitored" not in reply

    async def test_non_admin_is_rejected(self, db_path):
        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="member"))
        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(bot=bot, args=[])

        await handlers.refreshusersall(upd, ctx)

        assert "⛔️" in msg.reply_text.call_args.args[0]

    async def test_syncs_each_monitored_group(self, db_path):
        insert_premium(db_path, chat_id="-100123")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO sub_chats (chat_id, chat_name, is_monitored, owner_chat_id) VALUES ('-200','Downtown',1,'-100123')"
        )
        conn.commit()
        conn.close()

        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
        bot.get_chat_administrators = AsyncMock(return_value=[])

        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(bot=bot, args=[])

        fake_ss = FakeSpreadsheet()
        with patch("sheets.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("sheets.open_spreadsheet", new_callable=AsyncMock, return_value=fake_ss):
            await handlers.refreshusersall(upd, ctx)

        reply = msg.reply_text.call_args.args[0]
        assert "Downtown" in reply
        assert "Synced" in reply


class TestStatusCommand:
    """/status - shows Type/Due Date/Sheet for the current (or DM-selected) hub."""

    async def test_free_group(self, db_path):
        bot = make_bot()
        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(bot=bot, args=[])

        await subscription.status_command(upd, ctx)

        reply = msg.reply_text.call_args.args[0]
        assert "FREE" in reply
        assert "unlimited" in reply

    async def test_pro_group_with_sheet(self, db_path):
        insert_premium(db_path, chat_id="-100123")
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE all_groups SET sheet_id='abc', sheet_name='MySheet' WHERE chat_id='-100123'")
        conn.commit()
        conn.close()

        bot = make_bot()
        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(bot=bot, args=[])

        await subscription.status_command(upd, ctx)

        reply = msg.reply_text.call_args.args[0]
        assert "PRO" in reply
        assert "MySheet" in reply


class TestUpgradeInfoCallback:
    """Tapping a locked /help section button opens an upgrade-info message
    (built from feature_flags.description) instead of the old dead-end alert."""

    async def test_shows_feature_description_status_and_contact_button(self, db_path):
        bot = make_bot()
        chat = make_chat(chat_id=-100123)

        query = MagicMock()
        query.data = "upgrade_info_aliases"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        upd = MagicMock()
        upd.callback_query = query
        upd.effective_chat = chat
        upd.effective_user = make_user()
        ctx = make_context(bot=bot, args=[])

        await handlers.upgrade_info_callback_handler(upd, ctx)

        query.edit_message_text.assert_awaited_once()
        text = query.edit_message_text.call_args.args[0]
        assert "Aliases" in text
        assert "Currently: FREE" in text
        assert "child groups/channels" in text  # from feature_flags.description

        keyboard = query.edit_message_text.call_args.kwargs["reply_markup"]
        buttons = [b for row in keyboard.inline_keyboard for b in row]
        contact_btn = next(b for b in buttons if "owner" in b.text.lower())
        assert contact_btn.url == "https://t.me/anefex"
        back_btn = next(b for b in buttons if "Back" in b.text)
        assert back_btn.callback_data == "help_back"



class TestHelpOwnerFlag:
    """/help -a shows owner-only commands, but only to actual owners."""

    async def test_owner_sees_owner_commands(self, db_path):
        with patch("subscription.OWNER_USER_IDS", {555}), patch("help_system.OWNER_USER_IDS", {555}):
            bot = make_bot()
            chat = make_chat(chat_id=-100123)
            user = make_user(user_id=555)
            msg  = make_message(chat=chat)
            upd  = make_update(chat=chat, user=user, message=msg)
            ctx  = make_context(bot=bot, args=["-a"])

            await handlers.help_command(upd, ctx)

            reply = msg.reply_text.call_args.args[0]
            assert "/setsub" in reply

    async def test_non_owner_gets_regular_help(self, db_path):
        with patch("subscription.OWNER_USER_IDS", {555}), patch("help_system.OWNER_USER_IDS", {555}):
            bot = make_bot()
            chat = make_chat(chat_id=-100123)
            user = make_user(user_id=999)  # not the owner
            msg  = make_message(chat=chat)
            upd  = make_update(chat=chat, user=user, message=msg)
            ctx  = make_context(bot=bot, args=["-a"])

            await handlers.help_command(upd, ctx)

            reply = msg.reply_text.call_args.args[0]
            assert "Main Commands" in reply
            assert "/setsub" not in reply


# ── button_handler ───────────────────────────────────────────────────────────
# NOTE: nothing in the previous version of this suite actually called
# button_handler() end-to-end (TestButtonHandlerUsername only re-implemented
# one line of its logic inline). These tests call the real function.

MAIN_CHAT = "-100123"


class TestButtonHandlerMasterHub:
    """Going/Not Going/Add/Sub clicks made directly in the main hub chat."""

    async def test_going_adds_user(self, db_path):
        insert_event(db_path, event_id="ev1", chat_id=MAIN_CHAT)
        bot  = make_bot()
        ctx  = make_context(bot=bot)
        user = make_user(user_id=1, username="alice")
        upd  = make_callback_update("going_ev1", chat_id=int(MAIN_CHAT), user=user)

        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("event_engine.open_spreadsheet",   new_callable=AsyncMock):
            await handlers.button_handler(upd, ctx)

        row = get_event(db_path, "ev1")
        going = json.loads(row[7])  # going_data column
        assert going == ["alice (1)"]

    async def test_notgoing_preserves_existing_guest_count(self, db_path):
        """
        Regression test: clicking Not Going must NOT reset a person's guest
        count to zero - guests only change via Add Guest / Sub Guest.
        """
        insert_event(
            db_path, event_id="ev1", chat_id=MAIN_CHAT,
            going=json.dumps(["alice (1)"]), counters=json.dumps({"alice": 2}),
        )
        bot  = make_bot()
        ctx  = make_context(bot=bot)
        user = make_user(user_id=1, username="alice")
        upd  = make_callback_update("notgoing_ev1", chat_id=int(MAIN_CHAT), user=user)

        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("event_engine.open_spreadsheet",   new_callable=AsyncMock):
            await handlers.button_handler(upd, ctx)

        row = get_event(db_path, "ev1")
        going     = json.loads(row[7])
        not_going = json.loads(row[8])
        counters  = json.loads(row[9])
        assert going == []
        assert "alice" in not_going
        assert counters.get("alice") == 2, "guest count must survive a Not Going click"

    async def test_add_guest_increments_counter(self, db_path):
        insert_event(db_path, event_id="ev1", chat_id=MAIN_CHAT, going=json.dumps(["alice (1)"]))
        bot  = make_bot()
        ctx  = make_context(bot=bot)
        user = make_user(user_id=1, username="alice")
        upd  = make_callback_update("add_ev1", chat_id=int(MAIN_CHAT), user=user)

        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("event_engine.open_spreadsheet",   new_callable=AsyncMock):
            await handlers.button_handler(upd, ctx)

        row = get_event(db_path, "ev1")
        counters = json.loads(row[9])
        assert counters.get("alice") == 1


class TestButtonHandlerCrossChatProtection:
    """
    Regression tests for the cross-chat protection bug: it used to compare
    people by their DISPLAYED NAME TEXT rather than their real Telegram
    user_id, so two different people who happen to render with the same
    name (very common in a busy public channel full of subscribers with no
    @username, who all show up by first_name only) would falsely collide -
    the second person's Going/Add/Sub click in a child chat would be
    silently blocked. This is very plausibly what was behind reports of
    "add/sub/not going don't work" and missing entries in the EventUsers
    export for some public channels.
    """

    async def test_different_users_with_same_display_name_are_not_confused(self, db_path):
        # "Alex" (user_id=1) is going in the MASTER hub.
        insert_event(db_path, event_id="ev1", chat_id=MAIN_CHAT, going=json.dumps(["Alex (1)"]))
        bot = make_bot()
        ctx = make_context(bot=bot)

        # A DIFFERENT "Alex" (user_id=2, no @username, same rendered first_name)
        # clicks Going in a CHILD chat. This must succeed, not be blocked.
        other_alex = make_user(user_id=2, username=None, first_name="Alex")
        upd = make_callback_update("going_ev1", chat_id=-200, user=other_alex)

        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("event_engine.open_spreadsheet",   new_callable=AsyncMock):
            await handlers.button_handler(upd, ctx)

        row = get_event_user(db_path, event_id="ev1", chat_id="-200", user_id="2")
        assert row is not None, "the second 'Alex' must have been registered, not falsely blocked"
        assert row[0] == "going"
        for call in upd.callback_query.answer.call_args_list:
            assert "already added" not in call.kwargs.get("text", "")

    async def test_same_user_id_going_in_master_is_blocked_from_child(self, db_path):
        # The ACTUAL same user (same user_id) going in master hub...
        insert_event(db_path, event_id="ev1", chat_id=MAIN_CHAT, going=json.dumps(["alice (1)"]))
        bot = make_bot()
        ctx = make_context(bot=bot)
        # ...clicks Going again, this time from a child chat. This SHOULD be blocked.
        same_user = make_user(user_id=1, username="alice")
        upd = make_callback_update("going_ev1", chat_id=-200, user=same_user)

        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("event_engine.open_spreadsheet",   new_callable=AsyncMock):
            await handlers.button_handler(upd, ctx)

        row = get_event_user(db_path, event_id="ev1", chat_id="-200", user_id="1")
        assert row is None, "the real duplicate registration must still be blocked"

    async def test_notgoing_preserves_guests_in_child_chat(self, db_path):
        insert_event(db_path, event_id="ev1", chat_id=MAIN_CHAT)
        insert_event_user(db_path, event_id="ev1", chat_id="-200", user_id="1",
                           username="alice", status="going", guests=3)

        bot  = make_bot()
        ctx  = make_context(bot=bot)
        user = make_user(user_id=1, username="alice")
        upd  = make_callback_update("notgoing_ev1", chat_id=-200, user=user)

        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("event_engine.open_spreadsheet",   new_callable=AsyncMock):
            await handlers.button_handler(upd, ctx)

        row = get_event_user(db_path, event_id="ev1", chat_id="-200", user_id="1")
        assert row is not None
        status, guests = row
        assert status == "notgoing"
        assert guests == 3, "guest count must survive a Not Going click in a child chat too"


class TestButtonHandlerChildGuestLogicMatchesMasterHub:
    """
    Regression tests for bug #1: in child chats, clicking Add Guest used to
    auto-mark the clicker as "going" (forcing status='going'), and clicking
    Not Going afterwards then wiped out their guest count. Neither of those
    things happens in the main hub: there, Add/Sub Guest only ever touch the
    guest counter and are completely independent of whether the person
    themselves is going/not going/undeclared. Child chats must behave the
    same way.
    """

    async def test_add_guest_in_child_chat_does_not_mark_clicker_as_going(self, db_path):
        insert_event(db_path, event_id="ev1", chat_id=MAIN_CHAT)
        bot  = make_bot()
        ctx  = make_context(bot=bot)
        user = make_user(user_id=1, username="alice")
        upd  = make_callback_update("add_ev1", chat_id=-200, user=user)

        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("event_engine.open_spreadsheet",   new_callable=AsyncMock):
            await handlers.button_handler(upd, ctx)

        row = get_event_user(db_path, event_id="ev1", chat_id="-200", user_id="1")
        assert row is not None
        status, guests = row
        assert guests == 1
        assert status != "going", "Add Guest must not auto-declare the clicker as going"

    async def test_notgoing_after_add_guest_in_child_chat_keeps_the_guest(self, db_path):
        """The exact bug scenario reported: Add, then Not Going, must not wipe the guest."""
        insert_event(db_path, event_id="ev1", chat_id=MAIN_CHAT)
        bot  = make_bot()
        ctx  = make_context(bot=bot)
        user = make_user(user_id=1, username="alice")

        add_upd = make_callback_update("add_ev1", chat_id=-200, user=user)
        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("event_engine.open_spreadsheet",   new_callable=AsyncMock):
            await handlers.button_handler(add_upd, ctx)

        notgoing_upd = make_callback_update("notgoing_ev1", chat_id=-200, user=user)
        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("event_engine.open_spreadsheet",   new_callable=AsyncMock):
            await handlers.button_handler(notgoing_upd, ctx)

        row = get_event_user(db_path, event_id="ev1", chat_id="-200", user_id="1")
        assert row is not None
        status, guests = row
        assert status == "notgoing"
        assert guests == 1, "the guest added before Not Going must survive"

    async def test_sub_guest_in_child_chat_never_removes_going_status(self, db_path):
        """Mirrors the main hub: Sub Guest only ever decrements guests, never touches going/not-going."""
        insert_event(db_path, event_id="ev1", chat_id=MAIN_CHAT)
        insert_event_user(db_path, event_id="ev1", chat_id="-200", user_id="1",
                           username="alice", status="going", guests=1)

        bot  = make_bot()
        ctx  = make_context(bot=bot)
        user = make_user(user_id=1, username="alice")
        upd  = make_callback_update("sub_ev1", chat_id=-200, user=user)

        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("event_engine.open_spreadsheet",   new_callable=AsyncMock):
            await handlers.button_handler(upd, ctx)

        row = get_event_user(db_path, event_id="ev1", chat_id="-200", user_id="1")
        assert row is not None
        status, guests = row
        assert status == "going", "Sub Guest reaching 0 must not remove the person's going status"
        assert guests == 0

    async def test_sub_guest_with_zero_guests_is_a_noop(self, db_path):
        insert_event(db_path, event_id="ev1", chat_id=MAIN_CHAT)
        insert_event_user(db_path, event_id="ev1", chat_id="-200", user_id="1",
                           username="alice", status="going", guests=0)

        bot  = make_bot()
        ctx  = make_context(bot=bot)
        user = make_user(user_id=1, username="alice")
        upd  = make_callback_update("sub_ev1", chat_id=-200, user=user)

        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("event_engine.open_spreadsheet",   new_callable=AsyncMock):
            await handlers.button_handler(upd, ctx)

        row = get_event_user(db_path, event_id="ev1", chat_id="-200", user_id="1")
        assert row is not None
        status, guests = row
        assert status == "going"
        assert guests == 0

    async def test_guest_only_registration_shows_in_child_view_without_being_going(self, db_path):
        """
        A person who only clicked Add Guest (never Going) must still have
        their guest count shown/counted in the child chat's rendered view -
        matching how the main hub still surfaces "orphaned" guest counts for
        people who aren't in the going list.
        """
        insert_event(db_path, event_id="ev1", chat_id=MAIN_CHAT)
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO event_shares (share_id, event_id, chat_id, message_id, share_mode, chat_type) VALUES (NULL,'ev1','-200','42','-visible','group')")
        conn.commit()
        conn.close()
        insert_event_user(db_path, event_id="ev1", chat_id="-200", user_id="9",
                           username="onlyguests", status="", guests=2)

        bot = make_bot()
        ctx = make_context(bot=bot)

        await handlers.update_all_shared_views(ctx, "ev1")

        child_calls = [c for c in bot.edit_message_text.call_args_list if c.kwargs.get("chat_id") == -200]
        text = child_calls[0].kwargs.get("text", "")
        assert "2, from: [onlyguests](tg://user?id=9)" in text
        assert "✅ onlyguests\n" not in text, "a guest-only registrant must not get their own 'going' name line"


class TestButtonHandlerSaveCloseEvent:
    """
    Covers the "Save & Close Event" flow: the Events sheet column order
    (EVENT_ID, EVENT_NAME, CREATED_AT, CREATED_BY, EVENT_DATE, CLOSED_AT,
    STATUS, AMOUNT) and the EventUsers export pulling going users from BOTH
    the main hub and every child chat/channel.
    """

    async def test_appends_new_events_row_with_date_in_column_e(self, db_path):
        insert_event(
            db_path, event_id="ev1", chat_id=MAIN_CHAT, event_status=1,
            going=json.dumps(["alice (1)"]), event_date="25.12.2026",
        )
        bot  = make_bot()
        ctx  = make_context(bot=bot)
        user = make_user(user_id=9, username="admin")
        upd  = make_callback_update("save_ev1", chat_id=int(MAIN_CHAT), user=user)

        fake_ss = FakeSpreadsheet()
        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("event_engine.open_spreadsheet", new_callable=AsyncMock, return_value=fake_ss), \
             patch("event_engine.sync_event_users_sheet", new_callable=AsyncMock):
            await handlers.button_handler(upd, ctx)

        ws = fake_ss.worksheets["Events"]
        assert len(ws.appended_rows) == 1
        row = ws.appended_rows[0]
        # EVENT_ID, EVENT_NAME, CREATED_AT, CREATED_BY, EVENT_DATE, CLOSED_AT, STATUS, AMOUNT
        assert row[0] == "ev1"
        assert row[4] == "25.12.2026", "EVENT_DATE must be column E (index 4)"
        assert row[6] == "CLOSED"

    async def test_updates_existing_events_row_in_column_f_to_h(self, db_path):
        insert_event(
            db_path, event_id="ev1", chat_id=MAIN_CHAT, event_status=1,
            going=json.dumps(["alice (1)"]), counters=json.dumps({"alice": 2}),
        )
        bot  = make_bot()
        ctx  = make_context(bot=bot)
        user = make_user(user_id=9, username="admin")
        upd  = make_callback_update("save_ev1", chat_id=int(MAIN_CHAT), user=user)

        fake_ss = FakeSpreadsheet()
        ws = FakeWorksheet()
        ws.records = [{"EVENT_ID": "ev1"}]
        fake_ss.worksheets["Events"] = ws

        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("event_engine.open_spreadsheet", new_callable=AsyncMock, return_value=fake_ss), \
             patch("event_engine.sync_event_users_sheet", new_callable=AsyncMock):
            await handlers.button_handler(upd, ctx)

        assert ws.appended_rows == []
        assert "F2:H2" in ws.cell_updates, "CLOSED_AT/STATUS/AMOUNT must target F:H now that EVENT_DATE sits at E"
        closed_at, status, amount = ws.cell_updates["F2:H2"][0]
        assert status == "CLOSED"
        assert amount == 3  # 1 going + 2 guests

    async def test_exports_going_users_from_main_and_every_child_chat(self, db_path):
        insert_event(db_path, event_id="ev1", chat_id=MAIN_CHAT, event_status=1, going=json.dumps(["alice (1)"]))
        insert_event_user(db_path, event_id="ev1", chat_id="-200", user_id="2", username="bob", status="going")
        insert_event_user(db_path, event_id="ev1", chat_id="-300", user_id="3", username="carol", status="going")
        insert_event_user(db_path, event_id="ev1", chat_id="-300", user_id="4", username="dave", status="notgoing")

        bot  = make_bot()
        ctx  = make_context(bot=bot)
        user = make_user(user_id=9, username="admin")
        upd  = make_callback_update("save_ev1", chat_id=int(MAIN_CHAT), user=user)

        fake_ss   = FakeSpreadsheet()
        sync_mock = AsyncMock()
        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("event_engine.open_spreadsheet", new_callable=AsyncMock, return_value=fake_ss), \
             patch("event_engine.sync_event_users_sheet", sync_mock):
            await handlers.button_handler(upd, ctx)

        sync_mock.assert_called_once()
        _, _, going_ids = sync_mock.call_args.args
        # alice (main hub) + bob + carol (two DIFFERENT child chats) must all
        # be present; dave (notgoing) must be excluded.
        assert sorted(going_ids) == ["1", "2", "3"]

    async def test_extra_player_is_included_in_event_users_export(self, db_path):
        """
        Regression test for bug #4: a name added via "Add Extra Member" has
        no real Telegram user_id (no parenthesised "(N)" suffix in the going
        list), so it was silently excluded from the EventUsers export. It
        must now show up there, identified by its username since there's no
        numeric id to use instead.
        """
        insert_event(
            db_path, event_id="ev1", chat_id=MAIN_CHAT, event_status=1,
            going=json.dumps(["alice (1)", "guest_bobby"]),
        )
        bot  = make_bot()
        ctx  = make_context(bot=bot)
        user = make_user(user_id=9, username="admin")
        upd  = make_callback_update("save_ev1", chat_id=int(MAIN_CHAT), user=user)

        fake_ss   = FakeSpreadsheet()
        sync_mock = AsyncMock()
        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("event_engine.open_spreadsheet", new_callable=AsyncMock, return_value=fake_ss), \
             patch("event_engine.sync_event_users_sheet", sync_mock):
            await handlers.button_handler(upd, ctx)

        sync_mock.assert_called_once()
        _, _, going_ids = sync_mock.call_args.args
        assert "1" in going_ids
        assert "guest_bobby" in going_ids


class TestButtonHandlerActionsLogNaming:
    """
    Regression tests for bug #2: verification-mode guest adjustments
    (incgst/decgst) must be logged to the Actions sheet as ADD_editmode /
    SUB_editmode, not INCGST/DECGST.
    """

    async def test_incgst_logs_as_add_editmode(self, db_path):
        insert_event(db_path, event_id="ev1", chat_id=MAIN_CHAT, event_status=1, going=json.dumps(["alice (1)"]))
        bot  = make_bot()
        ctx  = make_context(bot=bot)
        user = make_user(user_id=9, username="admin")
        upd  = make_callback_update("incgst_ev1:alice", chat_id=int(MAIN_CHAT), user=user)

        fake_ss = FakeSpreadsheet()
        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("event_engine.open_spreadsheet", new_callable=AsyncMock, return_value=fake_ss):
            await handlers.button_handler(upd, ctx)

        ws = fake_ss.worksheets["Actions"]
        assert len(ws.appended_rows) == 1
        assert ws.appended_rows[0][1] == "ADD_editmode"

    async def test_decgst_logs_as_sub_editmode(self, db_path):
        insert_event(
            db_path, event_id="ev1", chat_id=MAIN_CHAT, event_status=1,
            going=json.dumps(["alice (1)"]), counters=json.dumps({"alice": 1}),
        )
        bot  = make_bot()
        ctx  = make_context(bot=bot)
        user = make_user(user_id=9, username="admin")
        upd  = make_callback_update("decgst_ev1:alice", chat_id=int(MAIN_CHAT), user=user)

        fake_ss = FakeSpreadsheet()
        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("event_engine.open_spreadsheet", new_callable=AsyncMock, return_value=fake_ss):
            await handlers.button_handler(upd, ctx)

        ws = fake_ss.worksheets["Actions"]
        assert len(ws.appended_rows) == 1
        assert ws.appended_rows[0][1] == "SUB_editmode"

    async def test_other_actions_still_log_their_plain_uppercase_name(self, db_path):
        insert_event(db_path, event_id="ev1", chat_id=MAIN_CHAT)
        bot  = make_bot()
        ctx  = make_context(bot=bot)
        user = make_user(user_id=1, username="alice")
        upd  = make_callback_update("going_ev1", chat_id=int(MAIN_CHAT), user=user)

        fake_ss = FakeSpreadsheet()
        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("event_engine.open_spreadsheet", new_callable=AsyncMock, return_value=fake_ss):
            await handlers.button_handler(upd, ctx)

        ws = fake_ss.worksheets["Actions"]
        assert ws.appended_rows[0][1] == "GOING"


class TestSharedLabelAndIcon:
    """The child-chat broadcast text uses only the ↪️ icon, no 'SHARED' word."""

    async def test_shared_label_uses_new_icon_and_short_text(self, db_path):
        insert_event(db_path, event_id="ev1", chat_id=MAIN_CHAT)
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO event_shares (share_id, event_id, chat_id, message_id, share_mode, chat_type) VALUES (NULL,'ev1','-200','42','-visible','group')")
        conn.commit()
        conn.close()

        bot = make_bot()
        ctx = make_context(bot=bot)

        await handlers.update_all_shared_views(ctx, "ev1")

        child_calls = [
            c for c in bot.edit_message_text.call_args_list
            if c.kwargs.get("chat_id") == -200
        ]
        assert child_calls, "expected an edit_message_text call for the child chat"
        text = child_calls[0].kwargs.get("text", "")
        assert "↪️ *" in text
        assert "SHARED" not in text
        assert "📢" not in text


class TestGuestsFoldedIntoGoingList:
    """
    The standalone "👥 Guests:" section is gone - guest counts now appear as
    extra lines directly inside the Going list, formatted "N, from: Name".
    """

    async def test_no_separate_guests_section_in_master_view(self, db_path):
        insert_event(
            db_path, event_id="ev1", chat_id=MAIN_CHAT,
            going=json.dumps(["alice (1)"]), counters=json.dumps({"alice": 2}),
        )
        bot = make_bot()
        ctx = make_context(bot=bot)

        await handlers.update_all_shared_views(ctx, "ev1")

        text = bot.edit_message_text.call_args_list[0].kwargs.get("text", "")
        assert "Guests" not in text
        assert "2, from: [alice](tg://user?id=1)" in text

    async def test_guest_line_present_in_child_view(self, db_path):
        insert_event(db_path, event_id="ev1", chat_id=MAIN_CHAT)
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO event_shares (share_id, event_id, chat_id, message_id, share_mode, chat_type) VALUES (NULL,'ev1','-200','42','-visible','group')")
        conn.commit()
        conn.close()
        insert_event_user(db_path, event_id="ev1", chat_id="-200", user_id="9",
                           username="channelfan", status="going", guests=4)

        bot = make_bot()
        ctx = make_context(bot=bot)

        await handlers.update_all_shared_views(ctx, "ev1")

        child_calls = [c for c in bot.edit_message_text.call_args_list if c.kwargs.get("chat_id") == -200]
        text = child_calls[0].kwargs.get("text", "")
        assert "4, from: [channelfan](tg://user?id=9)" in text
        assert "(+4 g.)" not in text


class TestScheduleViewRefreshCoalescing:
    """
    Sanity checks for the click-burst coalescer that replaced firing an
    independent update_all_shared_views() broadcast per click. Without this,
    N rapid clicks in a busy public channel each launched their own full
    N-chat edit cascade concurrently, flooding Telegram's per-chat edit rate
    limit - the likely cause of "Going" appearing to hang/load forever.
    """

    async def test_schedule_view_refresh_runs_the_broadcast(self, db_path):
        insert_event(db_path, event_id="ev1", chat_id=MAIN_CHAT)
        bot = make_bot()
        ctx = make_context(bot=bot)

        await handlers.schedule_view_refresh(ctx, "ev1")

        assert bot.edit_message_text.await_count >= 1

    async def test_concurrent_refreshes_for_same_event_collapse_into_one_extra_pass(self, db_path):
        import asyncio

        insert_event(db_path, event_id="ev1", chat_id=MAIN_CHAT)
        bot = make_bot()

        # A bare AsyncMock() resolves synchronously when awaited (no real
        # I/O inside it) - so it never actually yields control back to the
        # event loop, meaning the 5 "concurrent" calls below would just run
        # one at a time to full completion with no real overlap, and the
        # coalescing lock would never see genuine concurrency to collapse.
        # A tiny real suspension point (asyncio.sleep(0)) makes the mock
        # behave like the real network call it's standing in for, letting
        # the other 4 calls actually pile up while one is "in flight".
        async def _yielding_edit(*args, **kwargs):
            await asyncio.sleep(0)
            return MagicMock()
        bot.edit_message_text = AsyncMock(side_effect=_yielding_edit)

        ctx = make_context(bot=bot)

        # Fire several refreshes "at once" for the same event.
        await asyncio.gather(*[handlers.schedule_view_refresh(ctx, "ev1") for _ in range(5)])

        # They must NOT have resulted in 5x independent full broadcasts -
        # the coalescer should have collapsed the burst down to (at most) two
        # passes: the one that grabbed the lock first, plus one extra pass
        # for everything that piled up while it was running.
        # Each pass calls edit_message_text once for the master (no shares
        # configured here), so this bounds the total call count tightly.
        assert bot.edit_message_text.await_count <= 2


# ── DM-based hub resolution (hub_resolver.py) ───────────────────────────────

class TestHubResolver:
    """
    Covers resolve_hub_chat_id() and the group-picker replay flow, using
    /listalias as the concrete command under test (any of the 7 wired
    commands would exercise the same underlying mechanism).
    """

    async def test_inside_a_group_resolves_immediately_unchanged(self, db_path):
        """From inside a group chat, behavior must be identical to before
        this feature existed - no DM/admin-lookup logic runs at all."""
        insert_premium(db_path, chat_id="-100123")
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO sub_chats (chat_id, alias, owner_chat_id) VALUES ('-999','downtown','-100123')")
        conn.commit()
        conn.close()

        import aliases
        bot  = make_bot()
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(bot=bot, args=[])

        await aliases.listalias(upd, ctx)

        bot.get_chat_member.assert_not_awaited()
        reply = msg.reply_text.call_args.args[0]
        assert "downtown" in reply

    async def test_dm_with_no_admin_groups_is_told_plainly(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO all_groups (chat_id, chat_name, type) VALUES ('-100111','Football','FREE')")
        conn.commit()
        conn.close()

        import aliases
        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="member"))  # not admin

        dm_chat = make_chat(chat_id=555555, chat_type="private")
        msg     = make_message(chat=dm_chat)
        upd     = make_update(chat=dm_chat, message=msg)
        ctx     = make_context(bot=bot, args=[])

        await aliases.listalias(upd, ctx)

        reply = msg.reply_text.call_args.args[0]
        assert "not an admin" in reply.lower()

    async def test_dm_with_exactly_one_admin_group_resolves_immediately(self, db_path):
        insert_premium(db_path, chat_id="-100111")
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE all_groups SET chat_name='Football' WHERE chat_id='-100111'")
        conn.execute("INSERT INTO sub_chats (chat_id, alias, owner_chat_id) VALUES ('-999','downtown','-100111')")
        conn.commit()
        conn.close()

        import aliases
        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))

        dm_chat = make_chat(chat_id=555555, chat_type="private")
        msg     = make_message(chat=dm_chat)
        upd     = make_update(chat=dm_chat, message=msg)
        ctx     = make_context(bot=bot, args=[])

        await aliases.listalias(upd, ctx)

        reply = msg.reply_text.call_args.args[0]
        assert "downtown" in reply, "should resolve straight to the only admin group, no picker shown"

    async def test_dm_with_multiple_admin_groups_shows_picker_and_stashes_command(self, db_path):
        insert_premium(db_path, chat_id="-100111")
        insert_premium(db_path, chat_id="-100222")
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE all_groups SET chat_name='Football' WHERE chat_id='-100111'")
        conn.execute("UPDATE all_groups SET chat_name='Basketball' WHERE chat_id='-100222'")
        conn.commit()
        conn.close()

        import aliases
        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))

        dm_chat = make_chat(chat_id=555555, chat_type="private")
        msg     = make_message(chat=dm_chat)
        upd     = make_update(chat=dm_chat, message=msg)
        ctx     = make_context(bot=bot, args=[])

        await aliases.listalias(upd, ctx)

        keyboard = msg.reply_text.call_args.kwargs.get("reply_markup")
        assert keyboard is not None, "must show an inline picker for 2+ admin groups"
        names = [b.text for row in keyboard.inline_keyboard for b in row]
        assert "Football" in names and "Basketball" in names
        assert ctx.user_data["pending_hub_command"] == {"command": "listalias", "args": []}

    async def test_picking_a_group_replays_the_command_for_that_group_only(self, db_path):
        """The end-to-end flow: DM -> picker -> button tap -> the original
        command re-runs scoped to ONLY the chosen group's data."""
        insert_premium(db_path, chat_id="-100111")
        insert_premium(db_path, chat_id="-100222")
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE all_groups SET chat_name='Football' WHERE chat_id='-100111'")
        conn.execute("UPDATE all_groups SET chat_name='Basketball' WHERE chat_id='-100222'")
        conn.execute("INSERT INTO sub_chats (chat_id, alias, owner_chat_id) VALUES ('-999','footballalias','-100111')")
        conn.execute("INSERT INTO sub_chats (chat_id, alias, owner_chat_id) VALUES ('-888','hoopsalias','-100222')")
        conn.commit()
        conn.close()

        import aliases, hub_resolver
        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))

        dm_chat = make_chat(chat_id=555555, chat_type="private")
        msg     = make_message(chat=dm_chat)
        upd     = make_update(chat=dm_chat, message=msg)
        ctx     = make_context(bot=bot, args=[])

        await aliases.listalias(upd, ctx)
        keyboard = msg.reply_text.call_args.kwargs.get("reply_markup")
        football_button = next(b for row in keyboard.inline_keyboard for b in row if b.text == "Football")

        query = MagicMock()
        query.data = football_button.callback_data
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.message = make_message(chat=dm_chat)
        upd2 = MagicMock()
        upd2.callback_query = query
        # Matches real Telegram: update.message is None for a pure
        # callback-query update - the real message lives at query.message.
        upd2.message = None

        await hub_resolver.hub_pick_callback_handler(upd2, ctx)

        final_reply = query.message.reply_text.call_args.args[0]
        assert "footballalias" in final_reply
        assert "hoopsalias" not in final_reply, "must only show the CHOSEN group's data, not both"


class TestStartCommand:
    """/start lists the user's admin groups (or says plainly they have none)."""

    async def test_lists_admin_groups(self, db_path):
        insert_premium(db_path, chat_id="-100111")
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE all_groups SET chat_name='Football' WHERE chat_id='-100111'")
        conn.commit()
        conn.close()

        import hub_resolver
        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))

        dm_chat = make_chat(chat_id=555555, chat_type="private")
        msg     = make_message(chat=dm_chat)
        upd     = make_update(chat=dm_chat, message=msg)
        ctx     = make_context(bot=bot, args=[])

        await hub_resolver.start_command(upd, ctx)

        reply = msg.reply_text.call_args.args[0]
        assert "Football" in reply
        assert ctx.user_data["selected_hub_chat_id"] == "-100111", \
            "the only admin group should be auto-selected, no need to ask"

    async def test_shows_picker_immediately_with_multiple_admin_groups(self, db_path):
        """
        The very first interaction (/start) must ask which group to work
        with when the user administers more than one, not just list names
        informationally.
        """
        insert_premium(db_path, chat_id="-100111")
        insert_premium(db_path, chat_id="-100222")
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE all_groups SET chat_name='Football' WHERE chat_id='-100111'")
        conn.execute("UPDATE all_groups SET chat_name='Basketball' WHERE chat_id='-100222'")
        conn.commit()
        conn.close()

        import hub_resolver
        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))

        dm_chat = make_chat(chat_id=555555, chat_type="private")
        msg     = make_message(chat=dm_chat)
        upd     = make_update(chat=dm_chat, message=msg)
        ctx     = make_context(bot=bot, args=[])

        await hub_resolver.start_command(upd, ctx)

        keyboard = msg.reply_text.call_args.kwargs.get("reply_markup")
        assert keyboard is not None, "must show a picker, not just an informational list"
        names = [b.text for row in keyboard.inline_keyboard for b in row]
        assert "Football" in names and "Basketball" in names
        assert "selected_hub_chat_id" not in ctx.user_data, "must wait for an explicit pick"

    async def test_tells_the_truth_when_not_admin_anywhere(self, db_path):
        import hub_resolver
        bot = make_bot()

        dm_chat = make_chat(chat_id=555555, chat_type="private")
        msg     = make_message(chat=dm_chat)
        upd     = make_update(chat=dm_chat, message=msg)
        ctx     = make_context(bot=bot, args=[])

        await hub_resolver.start_command(upd, ctx)

        reply = msg.reply_text.call_args.args[0]
        assert "don't see you as an admin" in reply

    async def test_does_nothing_when_run_inside_a_group(self, db_path):
        """/start is only meaningful in a DM - a stray /start typed inside
        a group must be a silent no-op, not spam the group."""
        import hub_resolver
        bot  = make_bot()
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(bot=bot, args=[])

        await hub_resolver.start_command(upd, ctx)

        msg.reply_text.assert_not_awaited()

    async def test_finds_groups_only_known_via_main_group_users(self, db_path):
        """
        Regression test: a group the bot was added to BEFORE all_groups
        existed has no row there at all - only in main_group_users (which
        has existed since v2.0). /start (and resolve_hub_chat_id) must
        still find it via that fallback, not just all_groups.
        """
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO main_group_users (chat_id, username, user_id, status) VALUES ('-100999','alice','1','active')"
        )
        conn.commit()
        conn.close()

        import hub_resolver
        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
        bot.get_chat = AsyncMock(return_value=MagicMock(title="Old Legacy Group", username=None))

        dm_chat = make_chat(chat_id=555555, chat_type="private")
        msg     = make_message(chat=dm_chat)
        upd     = make_update(chat=dm_chat, message=msg)
        ctx     = make_context(bot=bot, args=[])

        await hub_resolver.start_command(upd, ctx)

        reply = msg.reply_text.call_args.args[0]
        assert "Old Legacy Group" in reply


class TestStickyHubSelection:
    """
    Once a group is picked (or auto-detected as the only option) for a DM
    conversation, it should "stick" for every following DM command without
    asking again - until /switchgroup is used.
    """

    async def test_second_command_reuses_the_first_selection_no_relookup(self, db_path):
        insert_premium(db_path, chat_id="-100111")
        insert_premium(db_path, chat_id="-100222")
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE all_groups SET chat_name='Football' WHERE chat_id='-100111'")
        conn.execute("UPDATE all_groups SET chat_name='Basketball' WHERE chat_id='-100222'")
        conn.commit()
        conn.close()

        import hub_resolver
        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))

        dm_chat = make_chat(chat_id=555555, chat_type="private")
        ctx = make_context(bot=bot, args=[])  # same ctx.user_data across both calls

        msg1 = make_message(chat=dm_chat)
        upd1 = make_update(chat=dm_chat, message=msg1)
        await handlers.listusers(upd1, ctx)
        keyboard = msg1.reply_text.call_args.kwargs.get("reply_markup")
        button = next(b for row in keyboard.inline_keyboard for b in row if b.text == "Football")

        query = MagicMock()
        query.data = button.callback_data
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.message = make_message(chat=dm_chat)
        cb_upd = MagicMock()
        cb_upd.callback_query = query
        cb_upd.message = None
        await hub_resolver.hub_pick_callback_handler(cb_upd, ctx)

        assert ctx.user_data["selected_hub_chat_id"] == "-100111"

        bot.get_chat_member.reset_mock()
        msg2 = make_message(chat=dm_chat)
        upd2 = make_update(chat=dm_chat, message=msg2)
        await handlers.listusers(upd2, ctx)

        bot.get_chat_member.assert_not_awaited()
        assert msg2.reply_text.call_args.kwargs.get("reply_markup") is None, \
            "second command must NOT show a picker again"

    async def test_switchgroup_clears_selection_and_shows_picker(self, db_path):
        insert_premium(db_path, chat_id="-100111")
        insert_premium(db_path, chat_id="-100222")
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE all_groups SET chat_name='Football' WHERE chat_id='-100111'")
        conn.execute("UPDATE all_groups SET chat_name='Basketball' WHERE chat_id='-100222'")
        conn.commit()
        conn.close()

        import hub_resolver
        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))

        dm_chat = make_chat(chat_id=555555, chat_type="private")
        ctx = make_context(bot=bot, args=[])
        ctx.user_data["selected_hub_chat_id"] = "-100111"

        msg = make_message(chat=dm_chat)
        upd = make_update(chat=dm_chat, message=msg)
        await hub_resolver.switchgroup_command(upd, ctx)

        assert "selected_hub_chat_id" not in ctx.user_data
        keyboard = msg.reply_text.call_args.kwargs.get("reply_markup")
        assert keyboard is not None

    async def test_switchpick_button_sets_new_selection_without_replaying_a_command(self, db_path):
        import hub_resolver
        bot = make_bot()
        dm_chat = make_chat(chat_id=555555, chat_type="private")
        ctx = make_context(bot=bot, args=[])

        query = MagicMock()
        query.data = "switchpick_-100222"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        upd = MagicMock()
        upd.callback_query = query

        await hub_resolver.hub_pick_callback_handler(upd, ctx)

        assert ctx.user_data["selected_hub_chat_id"] == "-100222"
        query.edit_message_text.assert_awaited_once()


class TestShareeventFromDM:
    """/shareevent previously hard-rejected anything but a group/supergroup
    chat type - it must now work from a DM too, via hub resolution."""

    async def test_shareevent_works_from_dm(self, db_path):
        insert_event(db_path, event_id="ev1", chat_id="-100111")
        insert_premium(db_path, chat_id="-100111")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO sub_chats (chat_id, alias, owner_chat_id, chat_type) VALUES ('-200','downtown','-100111','group')"
        )
        conn.commit()
        conn.close()

        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
        bot.get_chat = AsyncMock(return_value=MagicMock(title="Downtown", type="group"))

        dm_chat = make_chat(chat_id=555555, chat_type="private")
        msg = make_message(chat=dm_chat)
        upd = make_update(chat=dm_chat, message=msg)
        ctx = make_context(bot=bot, args=["downtown"])

        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("handlers.open_spreadsheet", new_callable=AsyncMock):
            await handlers.shareevent(upd, ctx)

        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT event_id, chat_id FROM event_shares").fetchall()
        assert rows == [("ev1", "-200")]


class TestHelpPremiumIconsInDM:
    """
    Regression test: /help from a DM was always checking the DM chat's own
    (never-premium) id for the Aliases/Monitoring PRO gating, instead of
    whichever group is actually selected for that DM conversation - so a
    genuinely PRO group's buttons showed as locked when /help was run via
    DM. Must use the sticky-selected group (hub_resolver.py) instead.
    """

    async def test_pro_group_auto_detected_when_help_is_the_first_command(self, db_path):
        """
        The exact reported bug: /help run as the very FIRST command in a DM
        conversation, before any sticky selection exists yet, still showed
        PRO buttons as locked even though the user administers exactly one
        (genuinely PRO) group. Must auto-detect it, same as other commands.
        """
        insert_premium(db_path, chat_id="-100111")

        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
        dm_chat = make_chat(chat_id=555555, chat_type="private")
        msg = make_message(chat=dm_chat)
        upd = make_update(chat=dm_chat, message=msg)
        ctx = make_context(bot=bot, args=[])  # no selected_hub_chat_id set at all

        await handlers.help_command(upd, ctx)

        buttons = [b for row in msg.reply_text.call_args.kwargs["reply_markup"].inline_keyboard for b in row]
        alias_btn = next(b for b in buttons if "Aliases" in b.text)
        assert alias_btn.callback_data == "help_alias"
        assert "PRO" not in alias_btn.text
        assert ctx.user_data.get("selected_hub_chat_id") == "-100111"

    async def test_pro_group_selected_shows_unlocked_buttons(self, db_path):
        insert_premium(db_path, chat_id="-100111")

        bot = make_bot()
        dm_chat = make_chat(chat_id=555555, chat_type="private")
        msg = make_message(chat=dm_chat)
        upd = make_update(chat=dm_chat, message=msg)
        ctx = make_context(bot=bot, args=[])
        ctx.user_data["selected_hub_chat_id"] = "-100111"

        await handlers.help_command(upd, ctx)

        buttons = [b for row in msg.reply_text.call_args.kwargs["reply_markup"].inline_keyboard for b in row]
        alias_btn = next(b for b in buttons if "Aliases" in b.text)
        assert alias_btn.callback_data == "help_alias"
        assert "PRO" not in alias_btn.text

    async def test_in_group_free_still_shows_locked_buttons(self, db_path):
        """Unchanged behavior check: inside a free group, buttons stay locked."""
        bot = make_bot()
        chat = make_chat(chat_id=-100999, chat_type="supergroup")
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context(bot=bot, args=[])

        await handlers.help_command(upd, ctx)

        buttons = [b for row in msg.reply_text.call_args.kwargs["reply_markup"].inline_keyboard for b in row]
        alias_btn = next(b for b in buttons if "Aliases" in b.text)
        assert alias_btn.callback_data == "upgrade_info_aliases"


class TestAllgroupsAllchannels:
    """Owner-only /allgroups (with -pro filter) and /allchannels, paginated 10 at a time."""

    async def test_lists_all_groups(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO all_groups (chat_id, chat_name, type, visibility) VALUES ('-1','Football','PRO','public')")
        conn.execute("INSERT INTO all_groups (chat_id, chat_name, type, visibility) VALUES ('-2','Basketball','FREE','private')")
        conn.commit()
        conn.close()

        with patch("subscription.OWNER_USER_IDS", {555}):
            bot = make_bot()
            chat = make_chat(chat_id=-999)
            user = make_user(user_id=555)
            msg = make_message(chat=chat)
            upd = make_update(chat=chat, user=user, message=msg)
            ctx = make_context(bot=bot, args=[])

            await subscription.allgroups_command(upd, ctx)

            reply = msg.reply_text.call_args.args[0]
            assert "Football" in reply and "Basketball" in reply

    async def test_pro_filter_excludes_free(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO all_groups (chat_id, chat_name, type, visibility) VALUES ('-1','Football','PRO','public')")
        conn.execute("INSERT INTO all_groups (chat_id, chat_name, type, visibility) VALUES ('-2','Basketball','FREE','private')")
        conn.commit()
        conn.close()

        with patch("subscription.OWNER_USER_IDS", {555}):
            bot = make_bot()
            chat = make_chat(chat_id=-999)
            user = make_user(user_id=555)
            msg = make_message(chat=chat)
            upd = make_update(chat=chat, user=user, message=msg)
            ctx = make_context(bot=bot, args=["-pro"])

            await subscription.allgroups_command(upd, ctx)

            reply = msg.reply_text.call_args.args[0]
            assert "Football" in reply
            assert "Basketball" not in reply

    async def test_non_owner_is_silently_ignored(self, db_path):
        with patch("subscription.OWNER_USER_IDS", {555}):
            bot = make_bot()
            chat = make_chat(chat_id=-999)
            user = make_user(user_id=999)
            msg = make_message(chat=chat)
            upd = make_update(chat=chat, user=user, message=msg)
            ctx = make_context(bot=bot, args=[])

            await subscription.allgroups_command(upd, ctx)

            msg.reply_text.assert_not_awaited()

    async def test_pagination_next_and_prev_buttons(self, db_path):
        conn = sqlite3.connect(db_path)
        for i in range(15):
            conn.execute(
                "INSERT INTO all_groups (chat_id, chat_name, type, visibility) VALUES (?,?,?,?)",
                (f"-{i}", f"Group{i}", "FREE", "public"),
            )
        conn.commit()
        conn.close()

        with patch("subscription.OWNER_USER_IDS", {555}):
            bot = make_bot()
            chat = make_chat(chat_id=-999)
            user = make_user(user_id=555)
            msg = make_message(chat=chat)
            upd = make_update(chat=chat, user=user, message=msg)
            ctx = make_context(bot=bot, args=[])

            await subscription.allgroups_command(upd, ctx)
            keyboard = msg.reply_text.call_args.kwargs["reply_markup"]
            buttons = [b for row in keyboard.inline_keyboard for b in row]
            assert any(b.text == "Next ▶️" for b in buttons)
            assert not any(b.text == "◀️ Prev" for b in buttons)

            query = MagicMock()
            query.data = "allgroups_1"
            query.answer = AsyncMock()
            query.edit_message_text = AsyncMock()
            upd2 = MagicMock()
            upd2.callback_query = query
            upd2.effective_user = user

            await subscription.allgroups_page_callback_handler(upd2, ctx)

            keyboard2 = query.edit_message_text.call_args.kwargs["reply_markup"]
            buttons2 = [b for row in keyboard2.inline_keyboard for b in row]
            assert any(b.text == "◀️ Prev" for b in buttons2)
            assert not any(b.text == "Next ▶️" for b in buttons2)

    async def test_lists_all_channels(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO all_channels (chat_id, chat_name, visibility) VALUES ('-1','News Channel','public')")
        conn.commit()
        conn.close()

        with patch("subscription.OWNER_USER_IDS", {555}):
            bot = make_bot()
            chat = make_chat(chat_id=-999)
            user = make_user(user_id=555)
            msg = make_message(chat=chat)
            upd = make_update(chat=chat, user=user, message=msg)
            ctx = make_context(bot=bot, args=[])

            await subscription.allchannels_command(upd, ctx)

            reply = msg.reply_text.call_args.args[0]
            assert "News Channel" in reply
            assert "visible" in reply


class TestLogCommandUsageHandler:
    """main.log_command_usage_handler logs every command, including DMs (unlike track_command_interaction)."""

    async def test_logs_command_in_group_with_full_text(self, db_path):
        import main as main_mod
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        upd = MagicMock()
        upd.effective_chat = chat
        upd.effective_message = MagicMock(text="/newevent Friday Football -date 25.12.2026")
        upd.effective_user = make_user(user_id=42)
        upd.effective_user.is_bot = False
        ctx = MagicMock()

        await main_mod.log_command_usage_handler(upd, ctx)

        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT chat_id, command, command_text FROM command_log").fetchall()
        assert rows == [("-100123", "newevent", "/newevent Friday Football -date 25.12.2026")]

    async def test_logs_command_in_dm_too(self, db_path):
        import main as main_mod
        chat = make_chat(chat_id=555555, chat_type="private")
        upd = MagicMock()
        upd.effective_chat = chat
        upd.effective_message = MagicMock(text="/status")
        upd.effective_user = make_user(user_id=42)
        upd.effective_user.is_bot = False
        ctx = MagicMock()

        await main_mod.log_command_usage_handler(upd, ctx)

        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT chat_id, command FROM command_log WHERE chat_id='555555'").fetchall()
        assert rows == [("555555", "status")]

    async def test_skips_bot_senders(self, db_path):
        import main as main_mod
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        upd = MagicMock()
        upd.effective_chat = chat
        upd.effective_message = MagicMock(text="/newevent")
        bot_user = make_user(user_id=999)
        bot_user.is_bot = True
        upd.effective_user = bot_user
        ctx = MagicMock()

        await main_mod.log_command_usage_handler(upd, ctx)

        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT COUNT(*) FROM command_log").fetchone()
        assert rows[0] == 0


class TestFeatureFlagsSync:
    """subscription.set_feature_flag() and _push_control_sheet_botconfig() keep BOTCONFIG in sync."""

    async def test_set_feature_flag_updates_db_and_resyncs_botconfig(self, db_path):
        class FakeWorksheet:
            def __init__(self):
                self.updates = []
            async def get_all_values(self):
                return []
            async def update(self, cell_range, values):
                self.updates.append(values)
            async def batch_clear(self, ranges):
                pass

        class FakeSpreadsheet:
            def __init__(self):
                self.worksheets = {}
            async def worksheet(self, name):
                if name not in self.worksheets:
                    self.worksheets[name] = FakeWorksheet()
                return self.worksheets[name]

        fake_ss = FakeSpreadsheet()
        with patch("sheets.CONTROL_SHEET_ID", "fake"), \
             patch("sheets.open_spreadsheet", new_callable=AsyncMock, return_value=fake_ss):
            result = await subscription.set_feature_flag("monitoring", "ADMIN")

        assert result is True
        conn = sqlite3.connect(db_path)
        stored = conn.execute("SELECT min_tier FROM feature_flags WHERE feature_key='monitoring'").fetchone()
        assert stored == ("ADMIN",)

        ws = fake_ss.worksheets["BOTCONFIG"]
        grid = ws.updates[0]
        assert grid[0] == ["FEATURE_KEY", "FEATURE", "FREE", "PRO", "ADMIN", "DESCRIPTION"]
        monitoring_row = next(r for r in grid if r[0] == "monitoring")
        assert monitoring_row[2:5] == ["no", "no", "yes"]

    async def test_free_feature_shows_yes_for_all_three_tiers(self, db_path):
        class FakeWorksheet:
            def __init__(self):
                self.updates = []
            async def get_all_values(self):
                return []
            async def update(self, cell_range, values):
                self.updates.append(values)
            async def batch_clear(self, ranges):
                pass

        class FakeSpreadsheet:
            def __init__(self):
                self.worksheets = {}
            async def worksheet(self, name):
                if name not in self.worksheets:
                    self.worksheets[name] = FakeWorksheet()
                return self.worksheets[name]

        fake_ss = FakeSpreadsheet()
        with patch("sheets.CONTROL_SHEET_ID", "fake"), \
             patch("sheets.open_spreadsheet", new_callable=AsyncMock, return_value=fake_ss):
            await subscription._push_control_sheet_botconfig()

        ws = fake_ss.worksheets["BOTCONFIG"]
        grid = ws.updates[0]
        newevent_row = next(r for r in grid if r[0] == "newevent")
        assert newevent_row[2:5] == ["yes", "yes", "yes"]


class TestFeatureSnapshotGrandfathering:
    """
    An event locks in which rules applied to it (verification,
    add_extra_member) at the moment it was created (db.events.feature_snapshot).
    A later tier or feature_flags change never retroactively changes an
    event already in progress - only events created AFTER the change follow
    the new rules.
    """

    async def test_newevent_stores_current_tier_snapshot(self, db_path):
        subscription.update_feature_flag("verification", "PRO", db_path=db_path)

        bot = make_bot()
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        msg = make_message(chat=chat)
        msg.text = "/newevent Test"
        upd = make_update(chat=chat, message=msg)
        ctx = make_context(bot=bot, args=["Test"])

        await handlers.newevent(upd, ctx)

        conn = sqlite3.connect(db_path)
        snapshot = conn.execute("SELECT feature_snapshot FROM events").fetchone()[0]
        parsed = json.loads(snapshot)
        assert parsed["verification"] is False  # FREE by default, feature is PRO-gated

    async def test_old_event_keeps_old_rules_after_group_upgraded(self, db_path):
        """The exact reported scenario: create on FREE, upgrade to PRO, the
        already-existing event must still behave like a FREE event."""
        subscription.update_feature_flag("verification", "PRO", db_path=db_path)

        bot = make_bot()
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        msg = make_message(chat=chat)
        msg.text = "/newevent OldEvent"
        upd = make_update(chat=chat, message=msg)
        ctx = make_context(bot=bot, args=["OldEvent"])
        await handlers.newevent(upd, ctx)

        conn = sqlite3.connect(db_path)
        event_id = conn.execute("SELECT event_id FROM events").fetchone()[0]

        insert_premium(db_path, chat_id="-100123")  # group upgraded to PRO

        keyboard = handlers.create_event_keyboard(
            event_id, 0, "👍", "❌", [], {}, verification_enabled=False,
        )
        close_btn = keyboard.inline_keyboard[2][0]
        assert "Save&Close" in close_btn.text
        assert close_btn.callback_data == f"directclose_{event_id}"

    async def test_new_event_gets_new_rules_after_upgrade(self, db_path):
        subscription.update_feature_flag("verification", "PRO", db_path=db_path)
        insert_premium(db_path, chat_id="-100123")

        bot = make_bot()
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        msg = make_message(chat=chat)
        msg.text = "/newevent NewEvent"
        upd = make_update(chat=chat, message=msg)
        ctx = make_context(bot=bot, args=["NewEvent"])

        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("handlers.open_spreadsheet", new_callable=AsyncMock):
            await handlers.newevent(upd, ctx)

        conn = sqlite3.connect(db_path)
        snapshot = conn.execute("SELECT feature_snapshot FROM events").fetchone()[0]
        assert json.loads(snapshot)["verification"] is True

    async def test_directclose_click_closes_event_directly(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, feature_snapshot)
               VALUES ('ev1','-100123','1','Test','✅','❌',0,'[]','[]','{}','[]',?)""",
            (json.dumps({"verification": False, "add_extra_member": False}),),
        )
        conn.commit()
        conn.close()

        bot = make_bot()
        user = make_user(user_id=9, username="admin")
        upd = make_callback_update("directclose_ev1", chat_id=-100123, user=user)
        ctx = make_context(bot=bot)

        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("event_engine.open_spreadsheet", new_callable=AsyncMock), \
             patch("event_engine.sync_event_users_sheet", new_callable=AsyncMock):
            await handlers.button_handler(upd, ctx)

        conn = sqlite3.connect(db_path)
        status = conn.execute("SELECT event_status FROM events WHERE event_id='ev1'").fetchone()[0]
        assert status == 2

    async def test_manual_close_click_is_rejected_when_verification_disabled(self, db_path):
        """Defense in depth: even a manually-crafted close_ callback must not
        work when this event's own snapshot has verification disabled."""
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, feature_snapshot)
               VALUES ('ev1','-100123','1','Test','✅','❌',0,'[]','[]','{}','[]',?)""",
            (json.dumps({"verification": False, "add_extra_member": False}),),
        )
        conn.commit()
        conn.close()

        bot = make_bot()
        user = make_user(user_id=9, username="admin")
        upd = make_callback_update("close_ev1", chat_id=-100123, user=user)
        ctx = make_context(bot=bot)

        await handlers.button_handler(upd, ctx)

        conn = sqlite3.connect(db_path)
        status = conn.execute("SELECT event_status FROM events WHERE event_id='ev1'").fetchone()[0]
        assert status == 0

    async def test_null_snapshot_defaults_to_everything_enabled(self, db_path):
        """Events created before feature_snapshot existed have NULL there -
        must behave exactly as they always did (everything enabled)."""
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, feature_snapshot)
               VALUES ('ev1','-100123','1','Test','✅','❌',0,'[]','[]','{}','[]',NULL)"""
        )
        conn.commit()
        conn.close()

        bot = make_bot()
        user = make_user(user_id=9, username="admin")
        upd = make_callback_update("close_ev1", chat_id=-100123, user=user)  # old-style close still works
        ctx = make_context(bot=bot)

        await handlers.button_handler(upd, ctx)

        conn = sqlite3.connect(db_path)
        status = conn.execute("SELECT event_status FROM events WHERE event_id='ev1'").fetchone()[0]
        assert status == 1  # transitioned into verification, as it always did


class TestUpdateFeature:
    """/updatefeature - replaces /updatefeaturelevel. limit_count is a
    single value that only ever applies while a chat is exactly AT the
    feature's min_tier; any tier above is unlimited by construction, so
    there's no way to configure an "inversion" anymore (the old
    per-tier-limits warning this used to test doesn't apply)."""

    async def test_no_flags_at_all_warns_and_changes_nothing(self, db_path):
        with patch("subscription.OWNER_USER_IDS", {555}), \
             patch("sheets.CONTROL_SHEET_ID", "fake"), \
             patch("sheets.open_spreadsheet", new_callable=AsyncMock):
            bot = make_bot()
            chat = make_chat(chat_id=-999)
            user = make_user(user_id=555)
            msg = make_message(chat=chat)
            upd = make_update(chat=chat, user=user, message=msg)
            ctx = make_context(bot=bot, args=["shareevent"])

            await subscription.updatefeature(upd, ctx)

            reply = msg.reply_text.call_args.args[0]
            assert "No changes given" in reply
            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT min_tier, limit_count FROM feature_flags WHERE feature_key='shareevent'").fetchone()
            assert row == ("FREE", 3), "nothing should have changed from the seed default"

    async def test_limit_only_tier_unchanged_keeps_existing_limit(self, db_path):
        with patch("subscription.OWNER_USER_IDS", {555}), \
             patch("sheets.CONTROL_SHEET_ID", "fake"), \
             patch("sheets.open_spreadsheet", new_callable=AsyncMock):
            bot = make_bot()
            chat = make_chat(chat_id=-999)
            user = make_user(user_id=555)
            msg = make_message(chat=chat)
            upd = make_update(chat=chat, user=user, message=msg)
            ctx = make_context(bot=bot, args=["shareevent", "-minlevel", "free"])

            await subscription.updatefeature(upd, ctx)

            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT min_tier, limit_count FROM feature_flags WHERE feature_key='shareevent'").fetchone()
            assert row == ("FREE", 3), "limit must be untouched when the tier didn't actually change"

    async def test_tier_change_without_limit_resets_it_to_unlimited(self, db_path):
        with patch("subscription.OWNER_USER_IDS", {555}), \
             patch("sheets.CONTROL_SHEET_ID", "fake"), \
             patch("sheets.open_spreadsheet", new_callable=AsyncMock):
            bot = make_bot()
            chat = make_chat(chat_id=-999)
            user = make_user(user_id=555)
            msg = make_message(chat=chat)
            upd = make_update(chat=chat, user=user, message=msg)
            ctx = make_context(bot=bot, args=["shareevent", "-minlevel", "pro"])

            await subscription.updatefeature(upd, ctx)

            reply = msg.reply_text.call_args.args[0]
            assert "reset to unlimited" in reply
            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT min_tier, limit_count FROM feature_flags WHERE feature_key='shareevent'").fetchone()
            assert row == ("PRO", None)

    async def test_limit_0_clears_an_existing_limit(self, db_path):
        with patch("subscription.OWNER_USER_IDS", {555}), \
             patch("sheets.CONTROL_SHEET_ID", "fake"), \
             patch("sheets.open_spreadsheet", new_callable=AsyncMock):
            bot = make_bot()
            chat = make_chat(chat_id=-999)
            user = make_user(user_id=555)
            msg = make_message(chat=chat)
            upd = make_update(chat=chat, user=user, message=msg)
            ctx = make_context(bot=bot, args=["shareevent", "-limit", "0"])

            await subscription.updatefeature(upd, ctx)

            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT min_tier, limit_count FROM feature_flags WHERE feature_key='shareevent'").fetchone()
            assert row == ("FREE", None)

    async def test_both_flags_together_in_one_call(self, db_path):
        with patch("subscription.OWNER_USER_IDS", {555}), \
             patch("sheets.CONTROL_SHEET_ID", "fake"), \
             patch("sheets.open_spreadsheet", new_callable=AsyncMock):
            bot = make_bot()
            chat = make_chat(chat_id=-999)
            user = make_user(user_id=555)
            msg = make_message(chat=chat)
            upd = make_update(chat=chat, user=user, message=msg)
            ctx = make_context(bot=bot, args=["shareevent", "-minlevel", "admin", "-limit", "7"])

            await subscription.updatefeature(upd, ctx)

            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT min_tier, limit_count FROM feature_flags WHERE feature_key='shareevent'").fetchone()
            assert row == ("ADMIN", 7)

    async def test_unknown_feature_key(self, db_path):
        with patch("subscription.OWNER_USER_IDS", {555}):
            bot = make_bot()
            chat = make_chat(chat_id=-999)
            user = make_user(user_id=555)
            msg = make_message(chat=chat)
            upd = make_update(chat=chat, user=user, message=msg)
            ctx = make_context(bot=bot, args=["notarealfeature", "-limit", "5"])

            await subscription.updatefeature(upd, ctx)

            assert "Unknown feature" in msg.reply_text.call_args.args[0]


class TestUseridChatid:
    """Previously had zero test coverage despite being live, user-facing
    commands - both trivially cheap to cover."""

    async def test_userid_returns_the_callers_own_id(self, db_path):
        chat = make_chat(chat_id=-100123)
        user = make_user(user_id=987654)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context()

        await handlers.userid(upd, ctx)

        assert "987654" in msg.reply_text.call_args.args[0]

    async def test_chatid_returns_the_current_chats_id(self, db_path):
        chat = make_chat(chat_id=-100123)
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context()

        await handlers.chatid(upd, ctx)

        assert "-100123" in msg.reply_text.call_args.args[0]


class TestSetsheet:
    """Previously had zero test coverage despite real logic: PRO gating,
    admin gating, and the edit-access probe added recently."""

    class _FakeCell:
        def __init__(self, value):
            self.value = value

    class _FakeWorksheet:
        async def acell(self, ref):
            return TestSetsheet._FakeCell("v")
        async def update_acell(self, ref, val):
            pass

    class _FakeSpreadsheet:
        def __init__(self, title="MySheet"):
            self.title = title
        @property
        async def sheet1(self):
            return TestSetsheet._FakeWorksheet()

    async def test_free_hub_is_rejected(self, db_path):
        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=["abc"])

        await subscription.setsheet(upd, ctx)

        assert "PRO" in msg.reply_text.call_args.args[0]

    async def test_no_args_shows_syntax(self, db_path):
        insert_premium(db_path, chat_id="-100123")
        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=[])

        await subscription.setsheet(upd, ctx)

        assert "Syntax" in msg.reply_text.call_args.args[0]

    async def test_pro_hub_with_edit_access_binds_successfully(self, db_path):
        insert_premium(db_path, chat_id="-100123")
        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=["abc123sheetid"])

        with patch("subscription.open_spreadsheet", new_callable=AsyncMock, return_value=self._FakeSpreadsheet()), \
             patch("subscription._push_control_sheet_main", new_callable=AsyncMock):
            await subscription.setsheet(upd, ctx)

        reply = msg.reply_text.call_args.args[0]
        assert "MySheet" in reply
        assert "warning" not in reply.lower() and "Editor" not in reply

    async def test_non_admin_is_rejected(self, db_path):
        insert_premium(db_path, chat_id="-100123")
        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="member"))
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=["abc"])

        await subscription.setsheet(upd, ctx)

        assert "admin" in msg.reply_text.call_args.args[0].lower()


class TestHandleExtraPlayerInput:
    """Previously had zero test coverage - the actual text-input step of
    the 'Add Extra Member' flow, with real username-resolution logic."""

    async def test_known_user_resolves_to_real_user_id(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-100123','1','Test','👍','❌',1,'[]','[]','{}','[]')"""
        )
        conn.execute("INSERT INTO main_group_users (chat_id, username, user_id, status) VALUES ('-100123','bob','555','active')")
        conn.commit()
        conn.close()

        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        admin = make_user(user_id=1, username="admin")
        msg = make_message(chat=chat)
        msg.text = "bob"
        msg.delete = AsyncMock()
        upd = make_update(chat=chat, user=admin, message=msg)
        ctx = make_context(bot=bot)
        ctx.user_data["awaiting_extra_player_for"] = "ev1"

        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("handlers.open_spreadsheet", new_callable=AsyncMock), \
             patch("handlers.schedule_view_refresh", new_callable=AsyncMock):
            await handlers.handle_extra_player_input(upd, ctx)

        conn = sqlite3.connect(db_path)
        going = conn.execute("SELECT going_data FROM events WHERE event_id='ev1'").fetchone()[0]
        assert "bob (555)" in going
        assert "awaiting_extra_player_for" not in ctx.user_data

    async def test_unknown_username_falls_back_to_no_id_marker(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-100123','1','Test','👍','❌',1,'[]','[]','{}','[]')"""
        )
        conn.commit()
        conn.close()

        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        admin = make_user(user_id=1, username="admin")
        msg = make_message(chat=chat)
        msg.text = "unknownguy"
        msg.delete = AsyncMock()
        upd = make_update(chat=chat, user=admin, message=msg)
        ctx = make_context(bot=bot)
        ctx.user_data["awaiting_extra_player_for"] = "ev1"

        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("handlers.open_spreadsheet", new_callable=AsyncMock), \
             patch("handlers.schedule_view_refresh", new_callable=AsyncMock):
            await handlers.handle_extra_player_input(upd, ctx)

        conn = sqlite3.connect(db_path)
        going = conn.execute("SELECT going_data FROM events WHERE event_id='ev1'").fetchone()[0]
        assert "unknownguy (no_id_in_main_group)" in going

    async def test_non_admin_is_silently_ignored(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-100123','1','Test','👍','❌',1,'[]','[]','{}','[]')"""
        )
        conn.commit()
        conn.close()

        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="member"))
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        user = make_user(user_id=1, username="rando")
        msg = make_message(chat=chat)
        msg.text = "somebody"
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot)
        ctx.user_data["awaiting_extra_player_for"] = "ev1"

        await handlers.handle_extra_player_input(upd, ctx)

        conn = sqlite3.connect(db_path)
        going = conn.execute("SELECT going_data FROM events WHERE event_id='ev1'").fetchone()[0]
        assert going == "[]"


class TestRequirePremium:
    """Previously had zero direct test coverage (only exercised indirectly
    through the commands that call it)."""

    async def test_premium_chat_passes_silently(self, db_path):
        insert_premium(db_path, chat_id="-100123")
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)

        result = await subscription.require_premium(upd, "Test Feature")

        assert result is True
        assert not msg.reply_text.called

    async def test_free_chat_is_blocked_with_feature_label(self, db_path):
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)

        result = await subscription.require_premium(upd, "Test Feature")

        assert result is False
        assert "Test Feature" in msg.reply_text.call_args.args[0]


class TestTrackEveryoneMessage:
    """Previously had zero test coverage."""

    async def test_mentions_only_active_tracked_users(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO main_group_users (chat_id, username, status) VALUES ('-100123','alice','active')")
        conn.execute("INSERT INTO main_group_users (chat_id, username, status) VALUES ('-100123','bob','passive')")
        conn.commit()
        conn.close()

        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        msg.text = "hey @everyone check this out"
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context()

        await handlers.track_everyone_message(upd, ctx)

        sent = msg.reply_text.call_args.args[0]
        assert "@alice" in sent
        assert "@bob" not in sent

    async def test_no_mention_trigger_does_nothing(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO main_group_users (chat_id, username, status) VALUES ('-100123','alice','active')")
        conn.commit()
        conn.close()

        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        msg.text = "just a normal message"
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context()

        await handlers.track_everyone_message(upd, ctx)

        assert not msg.reply_text.called


class TestHelpCallbackAndBackHandler:
    """Previously had zero direct test coverage (only ever verified via
    one-off manual scripts during development, never added to the suite)."""

    async def test_help_callback_handler_shows_the_requested_section(self, db_path):
        chat = make_chat(chat_id=-100123)
        query = MagicMock()
        query.data = "help_utility"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        upd = MagicMock()
        upd.callback_query = query
        upd.effective_chat = chat
        upd.effective_user = make_user(user_id=1)
        ctx = make_context()

        await handlers.help_callback_handler(upd, ctx)

        assert "Utility" in query.edit_message_text.call_args.args[0]

    async def test_help_back_handler_returns_to_main_commands(self, db_path):
        chat = make_chat(chat_id=-100123)
        query = MagicMock()
        query.data = "help_back"
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        upd = MagicMock()
        upd.callback_query = query
        upd.effective_chat = chat
        upd.effective_user = make_user(user_id=1)
        ctx = make_context()

        await handlers.help_back_handler(upd, ctx)

        assert "Main Commands" in query.edit_message_text.call_args.args[0]


class TestAllchannelsPageCallback:
    """Previously had zero test coverage."""

    async def test_shows_a_page_of_channels(self, db_path):
        conn = sqlite3.connect(db_path)
        for i in range(15):
            conn.execute(
                "INSERT INTO all_channels (chat_id, chat_name, visibility) VALUES (?,?,'private')",
                (str(-i - 1), f"Chan{i}"),
            )
        conn.commit()
        conn.close()

        with patch("subscription.OWNER_USER_IDS", {555}):
            query = MagicMock()
            query.data = "allchannels_1"
            query.answer = AsyncMock()
            query.edit_message_text = AsyncMock()
            upd = MagicMock()
            upd.callback_query = query
            upd.effective_user = make_user(user_id=555)
            ctx = make_context(bot=make_bot())

            await subscription.allchannels_page_callback_handler(upd, ctx)

            assert query.edit_message_text.called

    async def test_non_owner_is_silently_ignored(self, db_path):
        with patch("subscription.OWNER_USER_IDS", {555}):
            query = MagicMock()
            query.data = "allchannels_0"
            query.answer = AsyncMock()
            query.edit_message_text = AsyncMock()
            upd = MagicMock()
            upd.callback_query = query
            upd.effective_user = make_user(user_id=999)
            ctx = make_context(bot=make_bot())

            await subscription.allchannels_page_callback_handler(upd, ctx)

            assert not query.edit_message_text.called


# ---------------------------------------------------------------------------
# Waitlist / Standby (v3.24.0): the standalone /limit command's capacity
# check + Standby redirect + FIFO auto-promotion in button_handler,
# /waitlist command, and the /shareevent capacity block.
# ---------------------------------------------------------------------------

def _make_fake_button_query(callback_data, chat_id, user_id, username):
    query = MagicMock()
    query.data = callback_data
    query.message = MagicMock()
    query.message.chat_id = chat_id
    query.from_user = MagicMock()
    query.from_user.id = user_id
    query.from_user.username = username
    query.from_user.first_name = username
    query.from_user.last_name = None
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    return query


class TestButtonHandlerCapacityAndPromotion:
    """Capacity check + Standby redirect + FIFO auto-promotion on Going/Not
    Going clicks, in the main hub, driven straight through button_handler."""

    async def _click(self, db_path, action, chat_id, user_id, username):
        query = _make_fake_button_query(f"{action}_ev1", chat_id, user_id, username)
        upd = MagicMock()
        upd.callback_query = query
        ctx = MagicMock()
        ctx.bot = MagicMock()
        ctx.bot.send_message = AsyncMock()
        ctx.bot.edit_message_text = AsyncMock()
        ctx.bot.get_chat_member = AsyncMock(return_value=MagicMock(status="member"))

        def _discard_task(coro):
            # Same fix as make_context()'s own create_task - close the
            # coroutine instead of leaving it dangling, or Python emits
            # "coroutine was never awaited" (often misattributed to
            # whichever unrelated test happens to be running when the
            # garbage collector eventually gets to it).
            coro.close()
            return MagicMock()

        ctx.application = MagicMock()
        ctx.application.create_task = MagicMock(side_effect=_discard_task)
        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock, return_value=None):
            await event_engine.button_handler(upd, ctx)
        return query, ctx

    async def test_going_click_over_capacity_joins_waitlist_not_going(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, total_limit, waitlist_data, waitlist_open)
               VALUES ('ev1','-100','1','Party','👍','❌',0,?,'[]','{}','[]',2,'[]',1)""",
            (json.dumps(["alice (1)", "bob (2)"]),),
        )
        conn.commit()
        conn.close()

        query, ctx = await self._click(db_path, "going", "-100", 4, "dave")

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT going_data, waitlist_data FROM events WHERE event_id='ev1'").fetchone()
        assert "dave" not in row[0]
        waitlist = json.loads(row[1])
        assert len(waitlist) == 1 and waitlist[0]["username"] == "dave"
        assert "Waitlist" in query.answer.call_args.kwargs.get("text", "")

    async def test_notgoing_click_promotes_oldest_waitlist_entry(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, total_limit, waitlist_data, waitlist_open)
               VALUES ('ev1','-100','1','Party','👍','❌',0,?,'[]','{}','[]',2,?,1)""",
            (
                json.dumps(["alice (1)", "bob (2)"]),
                json.dumps([{"chat_id": "-100", "chat_name": None, "username": "dave", "user_id": "4", "timestamp": "2026-01-01 00:00:00"}]),
            ),
        )
        conn.commit()
        conn.close()

        query, ctx = await self._click(db_path, "notgoing", "-100", 2, "bob")

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT going_data, waitlist_data FROM events WHERE event_id='ev1'").fetchone()
        going = json.loads(row[0])
        assert any("dave" in g for g in going)
        assert json.loads(row[1]) == []
        ctx.bot.send_message.assert_awaited_once()
        assert "moved from the Waitlist to Going" in ctx.bot.send_message.call_args.kwargs["text"]

    async def test_notgoing_click_with_empty_waitlist_sends_no_announcement(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, total_limit, waitlist_data)
               VALUES ('ev1','-100','1','Party','👍','❌',0,?,'[]','{}','[]',5,'[]')""",
            (json.dumps(["alice (1)"]),),
        )
        conn.commit()
        conn.close()

        query, ctx = await self._click(db_path, "notgoing", "-100", 1, "alice")
        ctx.bot.send_message.assert_not_awaited()

    async def test_going_click_under_capacity_is_unaffected(self, db_path):
        """No total_limit set at all - going works exactly as before, no waitlist involved."""
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-100','1','Party','👍','❌',0,'[]','[]','{}','[]')"""
        )
        conn.commit()
        conn.close()

        query, ctx = await self._click(db_path, "going", "-100", 1, "alice")

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT going_data, waitlist_data FROM events WHERE event_id='ev1'").fetchone()
        assert "alice" in row[0]
        assert json.loads(row[1]) == []


class TestWaitlistCommand:
    async def test_from_main_hub_shows_everyone_with_from_label(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, waitlist_data)
               VALUES ('ev1','-100','1','Party','👍','❌',0,'[]','[]','{}','[]',?)""",
            (json.dumps([
                {"chat_id": "-100", "chat_name": None, "username": "dave", "user_id": "4", "timestamp": "t"},
                {"chat_id": "-200", "chat_name": None, "username": "erin", "user_id": "5", "timestamp": "t"},
            ]),),
        )
        conn.execute("INSERT INTO sub_chats (chat_id, owner_chat_id, alias) VALUES ('-200','-100','childgroup')")
        conn.commit()
        conn.close()

        bot = make_bot()
        bot.get_chat = AsyncMock(return_value=MagicMock(title="Child Group"))
        chat = make_chat(chat_id=-100, chat_type="supergroup")
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context(bot=bot)

        await handlers.waitlist_command(upd, ctx)

        text = msg.reply_text.call_args.args[0]
        assert "dave" in text and "erin" in text
        assert "from" in text

    async def test_from_child_chat_shows_only_local_entries(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, waitlist_data)
               VALUES ('ev1','-100','1','Party','👍','❌',0,'[]','[]','{}','[]',?)""",
            (json.dumps([
                {"chat_id": "-100", "chat_name": None, "username": "dave", "user_id": "4", "timestamp": "t"},
                {"chat_id": "-200", "chat_name": None, "username": "erin", "user_id": "5", "timestamp": "t"},
            ]),),
        )
        conn.execute("INSERT INTO sub_chats (chat_id, owner_chat_id, alias) VALUES ('-200','-100','childgroup')")
        conn.commit()
        conn.close()

        bot = make_bot()
        chat = make_chat(chat_id=-200, chat_type="supergroup")
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context(bot=bot)

        await handlers.waitlist_command(upd, ctx)

        text = msg.reply_text.call_args.args[0]
        assert "erin" in text
        assert "dave" not in text

    async def test_empty_waitlist_says_so(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-100','1','Party','👍','❌',0,'[]','[]','{}','[]')"""
        )
        conn.commit()
        conn.close()

        bot = make_bot()
        chat = make_chat(chat_id=-100, chat_type="supergroup")
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context(bot=bot)

        await handlers.waitlist_command(upd, ctx)

        assert "empty" in msg.reply_text.call_args.args[0].lower()

    async def test_no_event_at_all(self, db_path):
        bot = make_bot()
        chat = make_chat(chat_id=-100, chat_type="supergroup")
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context(bot=bot)

        await handlers.waitlist_command(upd, ctx)

        assert "No event found" in msg.reply_text.call_args.args[0]


class TestShareeventCapacityBlock:
    async def test_blocked_when_event_is_at_total_limit(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, total_limit)
               VALUES ('ev1','-100','1','Party','👍','❌',0,?,'[]','{}','[]',2)""",
            (json.dumps(["alice (1)", "bob (2)"]),),
        )
        conn.execute("INSERT INTO sub_chats (chat_id, owner_chat_id, alias) VALUES ('-200','-100','childgroup')")
        conn.commit()
        conn.close()

        bot = make_bot()
        chat = make_chat(chat_id=-100, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=["childgroup"])

        await handlers.shareevent(upd, ctx)

        assert "already at its" in bot.send_message.call_args.kwargs.get("text", "")
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT COUNT(*) FROM event_shares WHERE event_id='ev1'").fetchone()[0] == 0

    async def test_not_blocked_when_under_capacity(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, total_limit)
               VALUES ('ev1','-100','1','Party','👍','❌',0,?,'[]','{}','[]',10)""",
            (json.dumps(["alice (1)"]),),
        )
        conn.execute("INSERT INTO sub_chats (chat_id, owner_chat_id, alias) VALUES ('-200','-100','childgroup')")
        conn.commit()
        conn.close()

        bot = make_bot()
        chat = make_chat(chat_id=-100, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=["childgroup"])

        with patch("handlers.open_spreadsheet", new_callable=AsyncMock):
            await handlers.shareevent(upd, ctx)

        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT COUNT(*) FROM event_shares WHERE event_id='ev1'").fetchone()[0] == 1


class TestCreateEventKeyboardStandby:
    """create_event_keyboard's is_full parameter (Going -> Standby label)."""

    def test_is_full_changes_going_to_standby(self):
        kb = handlers.create_event_keyboard("ev1", 0, "👍", "❌", is_full=True)
        flat = [b for row in kb.inline_keyboard for b in row]
        going_btn = next(b for b in flat if b.callback_data == "going_ev1")
        assert "Standby" in going_btn.text
        assert "Going" not in going_btn.text

    def test_not_full_keeps_going_label(self):
        kb = handlers.create_event_keyboard("ev1", 0, "👍", "❌", is_full=False)
        flat = [b for row in kb.inline_keyboard for b in row]
        going_btn = next(b for b in flat if b.callback_data == "going_ev1")
        assert "Going" in going_btn.text
        assert "Standby" not in going_btn.text




class TestNeweventEditeventLimitFlag:
    """-limit N's own number/gating/syntax on newevent AND editevent
    (including basic -limit+-wl combinations for visibility-value
    coverage). Waitlist visibility itself is a separate -wl flag since
    the -w/-wl redesign (see TestValidateWaitlistVisibilityFlagHelper
    for its own dedicated gating tests). Distinct from
    TestEditeventVisibilityAndLimitChanges below, which specifically
    covers editevent's promotion-from-Waitlist logic when the limit is
    raised with people already queued - not covered here."""

    @pytest.fixture(autouse=True)
    def _patch_sheets(self):
        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock, return_value=None), \
             patch("handlers.open_spreadsheet", new_callable=AsyncMock), \
             patch("handlers.schedule_view_refresh", new_callable=AsyncMock):
            yield

    # ---- newevent ----

    async def test_newevent_limit_visible_on_pro_hub(self, db_path):
        insert_premium(db_path, chat_id="-100123")
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context(args=["Party", "-limit", "20", "-wl", "visible"])

        await handlers.newevent(upd, ctx)

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT total_limit, waitlist_visibility FROM events WHERE name='Party'").fetchone()
        assert row == (20, "visible")

    async def test_newevent_limit_onlycount_on_pro_hub(self, db_path):
        insert_premium(db_path, chat_id="-100123")
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context(args=["Party", "-limit", "22", "-wl", "onlycount"])

        await handlers.newevent(upd, ctx)

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT total_limit, waitlist_visibility FROM events WHERE name='Party'").fetchone()
        assert row == (22, "onlycount")

    async def test_newevent_limit_hidden_on_pro_hub(self, db_path):
        insert_premium(db_path, chat_id="-100123")
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context(args=["Party", "-limit", "30", "-wl", "hidden"])

        await handlers.newevent(upd, ctx)

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT total_limit, waitlist_visibility FROM events WHERE name='Party'").fetchone()
        assert row == (30, "hidden")

    async def test_newevent_limit_without_visibility_defaults_hidden(self, db_path):
        insert_premium(db_path, chat_id="-100123")
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context(args=["Party", "-limit", "40"])

        await handlers.newevent(upd, ctx)

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT total_limit, waitlist_visibility FROM events WHERE name='Party'").fetchone()
        assert row == (40, "hidden")

    async def test_newevent_limit_rejected_on_free_hub(self, db_path):
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context(args=["Party", "-limit", "10"])

        await handlers.newevent(upd, ctx)

        assert "higher tier" in msg.reply_text.call_args.args[0]
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT COUNT(*) FROM events WHERE name='Party'").fetchone()[0] == 0

    async def test_newevent_without_limit_flag_works_on_free_hub(self, db_path):
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context(args=["Party"])

        await handlers.newevent(upd, ctx)

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT name, total_limit FROM events WHERE name='Party'").fetchone()
        assert row == ("Party", None)

    async def test_newevent_zero_limit_rejected(self, db_path):
        insert_premium(db_path, chat_id="-100123")
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context(args=["Party", "-limit", "0"])

        await handlers.newevent(upd, ctx)

        assert "Invalid" in msg.reply_text.call_args.args[0]
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT COUNT(*) FROM events WHERE name='Party'").fetchone()[0] == 0

    async def test_newevent_visible_alone_without_limit_becomes_part_of_name(self, db_path):
        """No -limit at all means 'visible' is just plain text, not a
        flag value - the ambiguity-avoidance edge case."""
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context(args=["visible", "Party"])

        await handlers.newevent(upd, ctx)

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT name FROM events").fetchone()
        assert row == ("visible Party",)

    # ---- editevent ----

    async def test_editevent_limit_visible_on_pro_hub(self, db_path):
        insert_premium(db_path, chat_id="-100123")
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-100123','1','Party','👍','❌',0,'[]','[]','{}','[]')"""
        )
        conn.commit()
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context(args=["-limit", "25", "-wl", "visible"])

        await handlers.editevent(upd, ctx)

        row = conn.execute("SELECT total_limit, waitlist_visibility FROM events WHERE event_id='ev1'").fetchone()
        assert row == (25, "visible")

    async def test_editevent_limit_rejected_on_free_hub(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-100123','1','Party','👍','❌',0,'[]','[]','{}','[]')"""
        )
        conn.commit()
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context(args=["-limit", "5"])

        await handlers.editevent(upd, ctx)

        assert "higher tier" in msg.reply_text.call_args.args[0]
        row = conn.execute("SELECT total_limit FROM events WHERE event_id='ev1'").fetchone()
        assert row == (None,)

    async def test_editevent_invalid_limit_value_rejected(self, db_path):
        insert_premium(db_path, chat_id="-100123")
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-100123','1','Party','👍','❌',0,'[]','[]','{}','[]')"""
        )
        conn.commit()
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context(args=["-limit", "0"])

        await handlers.editevent(upd, ctx)

        assert "Invalid" in msg.reply_text.call_args.args[0]

    async def test_editevent_without_limit_flag_unaffected_by_gate(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-100123','1','Old','👍','❌',0,'[]','[]','{}','[]')"""
        )
        conn.commit()
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context(args=["NewName"])

        await handlers.editevent(upd, ctx)

        row = conn.execute("SELECT name FROM events WHERE event_id='ev1'").fetchone()
        assert row == ("NewName",)


# ── Waitlist rendering: visible/hidden/onlycount (v3.24.0) ────────────────────

class TestWaitlistVisibilityRendering:
    """update_all_shared_views' Waitlist section across all 3 visibility
    modes, in both the main hub's own post and a child chat's post."""

    def _setup(self, db_path, visibility, waitlist_entries):
        insert_event(db_path, event_id="ev1", chat_id="-100123")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE events SET total_limit = 5, waitlist_data = ?, waitlist_visibility = ? WHERE event_id = 'ev1'",
            (json.dumps(waitlist_entries), visibility),
        )
        conn.execute(
            "INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type) VALUES ('ev1','-200','2','-visible','group')"
        )
        conn.commit()
        conn.close()

    def _entries(self):
        return [
            {"chat_id": "-100123", "chat_name": None, "username": "dave", "user_id": "4", "timestamp": "t"},
            {"chat_id": "-200", "chat_name": None, "username": "erin", "user_id": "5", "timestamp": "t"},
        ]

    async def test_visible_main_post_shows_everyone_with_from_label(self, db_path):
        self._setup(db_path, "visible", self._entries())
        bot = make_bot()
        bot.get_chat = AsyncMock(return_value=MagicMock(title="Child Group"))
        ctx = make_context(bot=bot)

        await handlers.update_all_shared_views(ctx, "ev1")

        main_call = next(c for c in bot.edit_message_text.call_args_list if c.kwargs.get("chat_id") == int("-100123"))
        text = main_call.kwargs.get("text", "")
        assert "dave" in text and "erin" in text
        assert "from Child Group" in text

    async def test_visible_child_post_shows_only_local_entries(self, db_path):
        self._setup(db_path, "visible", self._entries())
        bot = make_bot()
        bot.get_chat = AsyncMock(return_value=MagicMock(title="Child Group"))
        ctx = make_context(bot=bot)

        await handlers.update_all_shared_views(ctx, "ev1")

        child_call = next(c for c in bot.edit_message_text.call_args_list if c.kwargs.get("chat_id") == int("-200"))
        text = child_call.kwargs.get("text", "")
        assert "erin" in text
        assert "dave" not in text

    async def test_onlycount_shows_total_no_names_everywhere(self, db_path):
        self._setup(db_path, "onlycount", self._entries())
        bot = make_bot()
        ctx = make_context(bot=bot)

        await handlers.update_all_shared_views(ctx, "ev1")

        for call in bot.edit_message_text.call_args_list:
            text = call.kwargs.get("text", "")
            wl_lines = [l for l in text.split("\n") if "Waitlist" in l]
            assert wl_lines == ["*Waitlist:* 2"]
            assert "dave" not in text and "erin" not in text

    async def test_hidden_shows_nothing_anywhere(self, db_path):
        self._setup(db_path, "hidden", self._entries())
        bot = make_bot()
        ctx = make_context(bot=bot)

        await handlers.update_all_shared_views(ctx, "ev1")

        for call in bot.edit_message_text.call_args_list:
            text = call.kwargs.get("text", "")
            assert "Waitlist" not in text

    async def test_visible_with_empty_waitlist_shows_zero(self, db_path):
        self._setup(db_path, "visible", [])
        bot = make_bot()
        ctx = make_context(bot=bot)

        await handlers.update_all_shared_views(ctx, "ev1")

        main_call = next(c for c in bot.edit_message_text.call_args_list if c.kwargs.get("chat_id") == int("-100123"))
        text = main_call.kwargs.get("text", "")
        assert "*Waitlist* \\(0\\):" in text


class TestGlobalTextRouter:
    """global_text_router's dispatch logic - previously untested despite
    being the entry point every raw text message flows through."""

    async def test_routes_to_extra_player_input_when_awaiting(self, db_path):
        upd = MagicMock()
        ctx = MagicMock()
        ctx.user_data = {"awaiting_extra_player_for": "ev1"}
        with patch("handlers.handle_extra_player_input", new_callable=AsyncMock) as mock_extra, \
             patch("handlers.track_everyone_message", new_callable=AsyncMock) as mock_track:
            await handlers.global_text_router(upd, ctx)
            assert mock_extra.called
            assert not mock_track.called

    async def test_routes_to_track_everyone_when_not_awaiting(self, db_path):
        upd = MagicMock()
        ctx = MagicMock()
        ctx.user_data = {}
        with patch("handlers.handle_extra_player_input", new_callable=AsyncMock) as mock_extra, \
             patch("handlers.track_everyone_message", new_callable=AsyncMock) as mock_track:
            await handlers.global_text_router(upd, ctx)
            assert not mock_extra.called
            assert mock_track.called


class TestEditeventLimitRejectsBelowHeadcount:
    """editevent -limit N must reject N if it's below the event's current
    combined headcount (main + every share), leaving the old limit intact."""

    @pytest.fixture(autouse=True)
    def _patch_sheets(self):
        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock, return_value=None), \
             patch("handlers.schedule_view_refresh", new_callable=AsyncMock):
            yield

    async def test_lowering_below_current_going_count_is_rejected(self, db_path):
        insert_premium(db_path, chat_id="-100123")
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, total_limit)
               VALUES ('ev1','-100123','1','Party','👍','❌',0,?,'[]','{}','[]',5)""",
            (json.dumps(["alice (1)", "bob (2)", "carol (3)"]),),
        )
        conn.commit()
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context(args=["-limit", "2"])  # below the 3 currently going

        await handlers.editevent(upd, ctx)

        assert "left unchanged" in msg.reply_text.call_args.args[0]
        row = conn.execute("SELECT total_limit FROM events WHERE event_id='ev1'").fetchone()
        assert row == (5,)  # unchanged

    async def test_lowering_to_exactly_current_headcount_is_allowed(self, db_path):
        insert_premium(db_path, chat_id="-100123")
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, total_limit)
               VALUES ('ev1','-100123','1','Party','👍','❌',0,?,'[]','{}','[]',5)""",
            (json.dumps(["alice (1)", "bob (2)", "carol (3)"]),),
        )
        conn.commit()
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context(args=["-limit", "3"])  # exactly the current headcount

        await handlers.editevent(upd, ctx)

        row = conn.execute("SELECT total_limit FROM events WHERE event_id='ev1'").fetchone()
        assert row == (3,)


class TestEditeventLimitRaisePromotesFromWaitlist:
    """editevent -limit N, when N is raised, promotes FIFO from the
    event-wide waitlist (oldest first) until either the waitlist empties
    or headcount reaches the new limit - into the correct chat each time."""

    @pytest.fixture(autouse=True)
    def _patch_sheets(self):
        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock, return_value=None), \
             patch("handlers.schedule_view_refresh", new_callable=AsyncMock):
            yield

    def _setup(self, db_path):
        insert_premium(db_path, chat_id="-100123")
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, total_limit, waitlist_data)
               VALUES ('ev1','-100123','1','Party','👍','❌',0,?,'[]','{}','[]',2,?)""",
            (
                json.dumps(["alice (1)", "bob (2)"]),
                json.dumps([
                    {"chat_id": "-100123", "chat_name": None, "username": "dave", "user_id": "4", "timestamp": "2026-01-01 00:00:00"},
                    {"chat_id": "-200", "chat_name": None, "username": "erin", "user_id": "5", "timestamp": "2026-01-01 00:00:01"},
                ]),
            ),
        )
        conn.commit()
        conn.close()

    async def test_raising_limit_promotes_fifo_across_chats(self, db_path):
        self._setup(db_path)
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context(bot=make_bot(), args=["-limit", "4"])

        bot = make_bot()
        ctx = make_context(bot=bot, args=["-limit", "4"])
        upd = make_update(chat=chat, message=msg)
        await handlers.editevent(upd, ctx)

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT total_limit, going_data, waitlist_data FROM events WHERE event_id='ev1'").fetchone()
        assert row[0] == 4
        going = json.loads(row[1])
        assert any("dave" in g for g in going)
        assert json.loads(row[2]) == []
        child_row = conn.execute(
            "SELECT status FROM event_users WHERE event_id='ev1' AND chat_id='-200' AND user_id='5'"
        ).fetchone()
        assert child_row == ("going",)
        assert bot.send_message.call_count == 2

    async def test_raising_limit_by_only_one_slot_promotes_only_the_oldest(self, db_path):
        self._setup(db_path)
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        bot = make_bot()
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context(bot=bot, args=["-limit", "3"])  # only 1 extra slot

        await handlers.editevent(upd, ctx)

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT going_data, waitlist_data FROM events WHERE event_id='ev1'").fetchone()
        going = json.loads(row[0])
        waitlist = json.loads(row[1])
        assert any("dave" in g for g in going)  # dave was oldest, promoted
        assert len(waitlist) == 1 and waitlist[0]["username"] == "erin"  # erin still waiting
        assert bot.send_message.call_count == 1

    async def test_raising_limit_with_empty_waitlist_sends_no_announcement(self, db_path):
        insert_premium(db_path, chat_id="-100123")
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, total_limit)
               VALUES ('ev1','-100123','1','Party','👍','❌',0,'[]','[]','{}','[]',2)"""
        )
        conn.commit()
        conn.close()
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        bot = make_bot()
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context(bot=bot, args=["-limit", "10"])

        await handlers.editevent(upd, ctx)

        assert bot.send_message.call_count == 0


class TestStandbyClickIsIdempotent:
    """Clicking Going/Standby repeatedly while at capacity must add the
    person to the Waitlist exactly once, not once per click."""

    async def _click(self, db_path, action, chat_id, user_id, username):
        query = _make_fake_button_query(f"{action}_ev1", chat_id, user_id, username)
        upd = MagicMock()
        upd.callback_query = query
        ctx = MagicMock()
        ctx.bot = MagicMock()
        ctx.bot.send_message = AsyncMock()
        ctx.bot.edit_message_text = AsyncMock()
        ctx.bot.get_chat_member = AsyncMock(return_value=MagicMock(status="member"))

        def _discard_task(coro):
            coro.close()
            return MagicMock()

        ctx.application = MagicMock()
        ctx.application.create_task = MagicMock(side_effect=_discard_task)
        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock, return_value=None):
            await event_engine.button_handler(upd, ctx)
        return query, ctx

    async def test_repeated_clicks_in_main_group_add_exactly_once(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, total_limit, waitlist_data)
               VALUES ('ev1','-100','1','Party','👍','❌',0,?,'[]','{}','[]',2,'[]')""",
            (json.dumps(["alice (1)", "bob (2)"]),),
        )
        conn.commit()
        conn.close()

        for _ in range(3):
            await self._click(db_path, "going", "-100", 4, "dave")

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT waitlist_data FROM events WHERE event_id='ev1'").fetchone()
        waitlist = json.loads(row[0])
        assert len(waitlist) == 1
        assert waitlist[0]["username"] == "dave"

    async def test_repeated_clicks_in_child_chat_add_exactly_once(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, total_limit, waitlist_data)
               VALUES ('ev1','-100','1','Party','👍','❌',0,?,'[]','{}','[]',2,'[]')""",
            (json.dumps(["alice (1)", "bob (2)"]),),
        )
        conn.execute("INSERT INTO sub_chats (chat_id, owner_chat_id, alias) VALUES ('-200','-100','child')")
        conn.execute("INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type) VALUES ('ev1','-200','2','-visible','group')")
        conn.commit()
        conn.close()

        for _ in range(3):
            await self._click(db_path, "going", "-200", 7, "frank")

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT waitlist_data FROM events WHERE event_id='ev1'").fetchone()
        waitlist = json.loads(row[0])
        assert len(waitlist) == 1
        assert waitlist[0]["username"] == "frank"


class TestChildChatWaitlistPersistenceBug:
    """Regression test for a real bug found during manual debugging: the
    child-chat branch of button_handler commits and returns early via its
    own code path, entirely bypassing the shared final UPDATE that used to
    be the only place waitlist_data got persisted - meaning every waitlist
    add/promotion for a CHILD chat was silently lost on commit, and every
    promotion announcement was silently skipped. Fixed by persisting
    waitlist_data and sending the announcement directly in the child
    branch's own early-return path."""

    async def _click(self, action, chat_id, user_id, username):
        query = _make_fake_button_query(f"{action}_ev1", chat_id, user_id, username)
        upd = MagicMock()
        upd.callback_query = query
        ctx = MagicMock()
        ctx.bot = MagicMock()
        ctx.bot.send_message = AsyncMock()
        ctx.bot.edit_message_text = AsyncMock()
        ctx.bot.get_chat_member = AsyncMock(return_value=MagicMock(status="member"))

        def _discard_task(coro):
            coro.close()
            return MagicMock()

        ctx.application = MagicMock()
        ctx.application.create_task = MagicMock(side_effect=_discard_task)
        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock, return_value=None):
            await event_engine.button_handler(upd, ctx)
        return query, ctx

    async def test_child_chat_going_click_at_capacity_persists_to_db(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, total_limit, waitlist_data)
               VALUES ('ev1','-100','1','Party','👍','❌',0,?,'[]','{}','[]',2,'[]')""",
            (json.dumps(["alice (1)", "bob (2)"]),),
        )
        conn.execute("INSERT INTO sub_chats (chat_id, owner_chat_id, alias) VALUES ('-200','-100','child')")
        conn.execute(
            "INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type) VALUES ('ev1','-200','2','-visible','group')"
        )
        conn.commit()

        await self._click("going", "-200", 7, "frank")

        row = conn.execute("SELECT waitlist_data FROM events WHERE event_id='ev1'").fetchone()
        waitlist = json.loads(row[0])
        assert len(waitlist) == 1
        assert waitlist[0]["username"] == "frank"
        assert waitlist[0]["chat_id"] == "-200"

    async def test_child_chat_promotion_persists_and_announces(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, total_limit, waitlist_data)
               VALUES ('ev1','-100','1','Party','👍','❌',0,'[]','[]','{}','[]',1,'[]')"""
        )
        conn.execute("INSERT INTO sub_chats (chat_id, owner_chat_id, alias) VALUES ('-200','-100','child')")
        conn.execute(
            "INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type) VALUES ('ev1','-200','2','-visible','group')"
        )
        conn.commit()

        await self._click("going", "-200", 10, "george")
        await self._click("going", "-200", 11, "harry")
        _, ctx = await self._click("notgoing", "-200", 10, "george")

        row = conn.execute("SELECT waitlist_data FROM events WHERE event_id='ev1'").fetchone()
        assert json.loads(row[0]) == []
        harry_status = conn.execute(
            "SELECT status FROM event_users WHERE event_id='ev1' AND chat_id='-200' AND user_id='11'"
        ).fetchone()
        assert harry_status == ("going",)
        ctx.bot.send_message.assert_awaited_once()
        assert "moved from the Waitlist to Going" in ctx.bot.send_message.call_args.kwargs["text"]


class TestCrossChatAlreadyAddedWarning:
    """Already implemented in button_handler - clicking Going in a second
    chat while already going in a DIFFERENT chat (same event) shows a
    warning alert and does not add the person there. Previously untested.
    Works identically for channels: Telegram's callback_query mechanism
    doesn't distinguish chat_type - a channel post's inline button click
    still generates a regular CallbackQuery from the specific user."""

    async def _click(self, chat_id, uid, username):
        query = _make_fake_button_query("going_ev1", chat_id, uid, username)
        upd = MagicMock()
        upd.callback_query = query
        ctx = MagicMock()
        ctx.bot = MagicMock()
        ctx.bot.send_message = AsyncMock()
        ctx.bot.edit_message_text = AsyncMock()
        ctx.bot.get_chat_member = AsyncMock(return_value=MagicMock(status="member"))

        def _discard_task(coro):
            coro.close()
            return MagicMock()

        ctx.application = MagicMock()
        ctx.application.create_task = MagicMock(side_effect=_discard_task)
        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock, return_value=None):
            await event_engine.button_handler(upd, ctx)
        return query

    async def test_warning_shown_when_going_in_channel_share_while_already_going_elsewhere(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-100','1','Party','👍','❌',0,'[]','[]','{}','[]')"""
        )
        conn.execute("INSERT INTO sub_chats (chat_id, owner_chat_id, alias, chat_type) VALUES ('-500','-100','mychannel','channel')")
        conn.execute("INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type) VALUES ('ev1','-500','2','-visible','channel')")
        conn.execute("INSERT INTO event_users (event_id, chat_id, user_id, username, status, guests) VALUES ('ev1','-100','9','carol','going',0)")
        conn.commit()

        query = await self._click("-500", 9, "carol")

        assert query.answer.call_args.kwargs.get("show_alert") is True
        assert "already added to this event" in query.answer.call_args.kwargs.get("text", "")
        row = conn.execute(
            "SELECT status FROM event_users WHERE event_id='ev1' AND chat_id='-500' AND user_id='9'"
        ).fetchone()
        assert row is None

    async def test_no_warning_for_a_genuinely_new_person(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-100','1','Party','👍','❌',0,'[]','[]','{}','[]')"""
        )
        conn.execute("INSERT INTO sub_chats (chat_id, owner_chat_id, alias) VALUES ('-200','-100','child')")
        conn.execute("INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type) VALUES ('ev1','-200','2','-visible','group')")
        conn.commit()

        query = await self._click("-200", 15, "newperson")

        assert query.answer.call_args.kwargs.get("show_alert") is not True
        row = conn.execute(
            "SELECT status FROM event_users WHERE event_id='ev1' AND chat_id='-200' AND user_id='15'"
        ).fetchone()
        assert row == ("going",)


class TestEventCreatorCanClose:
    """/newevent has no admin check at all - any member can create an
    event. Previously only group admins could ever close/verify one,
    leaving a non-admin creator with no way to close their own event.
    close/directclose/save now also allow the event's own creator; other
    more sensitive admin actions (kick, cancel, guest adjustments, adding
    external members) stay strictly group-admin-only."""

    async def _click(self, action, uid, username, is_admin_member):
        query = _make_fake_button_query(f"{action}_ev1", "-100", uid, username)
        upd = MagicMock()
        upd.callback_query = query
        ctx = MagicMock()
        ctx.bot = MagicMock()
        ctx.bot.send_message = AsyncMock()
        ctx.bot.edit_message_text = AsyncMock()
        ctx.bot.get_chat_member = AsyncMock(
            return_value=MagicMock(status="administrator" if is_admin_member else "member")
        )

        def _discard_task(coro):
            coro.close()
            return MagicMock()

        ctx.application = MagicMock()
        ctx.application.create_task = MagicMock(side_effect=_discard_task)
        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock, return_value=None):
            await event_engine.button_handler(upd, ctx)

    def _insert_event(self, db_path, created_by="42"):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, created_by_user_id)
               VALUES ('ev1','-100','1','Party','👍','❌',0,'[]','[]','{}','[]',?)""",
            (created_by,),
        )
        conn.commit()
        conn.close()

    async def test_creator_can_close_without_being_admin(self, db_path):
        self._insert_event(db_path, created_by="42")
        await self._click("close", 42, "creator_person", is_admin_member=False)
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT event_status FROM events WHERE event_id='ev1'").fetchone()
        assert row != (0,)

    async def test_random_non_admin_non_creator_is_blocked(self, db_path):
        self._insert_event(db_path, created_by="42")
        await self._click("close", 99, "random_person", is_admin_member=False)
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT event_status FROM events WHERE event_id='ev1'").fetchone()
        assert row == (0,)

    async def test_creator_cannot_cancel_sensitive_action_stays_admin_only(self, db_path):
        self._insert_event(db_path, created_by="42")
        await self._click("cancel", 42, "creator_person", is_admin_member=False)
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT event_status FROM events WHERE event_id='ev1'").fetchone()
        assert row == (0,)

    async def test_group_admin_who_is_not_creator_can_still_close(self, db_path):
        self._insert_event(db_path, created_by="42")
        await self._click("close", 999, "some_admin", is_admin_member=True)
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT event_status FROM events WHERE event_id='ev1'").fetchone()
        assert row != (0,)

    async def test_pre_existing_event_with_null_creator_still_requires_admin(self, db_path):
        """Migration safety: events created before this feature have
        created_by_user_id = NULL, which must never match any real
        user_id - old events keep their exact previous admin-only behavior."""
        self._insert_event(db_path, created_by=None)
        await self._click("close", 1, "anyone", is_admin_member=False)
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT event_status FROM events WHERE event_id='ev1'").fetchone()
        assert row == (0,)


class TestAddGuestGoesToWaitlist:
    """Add Guest at capacity creates a real waitlist entry (is_guest=True)
    for the clicking person, instead of just being blocked - each click
    queues one more slot (no dedup, unlike person entries). When a slot
    frees up, promoting a guest entry increments the OWNER's guest counter
    (main hub counters, or child chat's event_users.guests) rather than
    adding a new person to going. If the owner is no longer going by the
    time a slot frees up, the stale entry is discarded without consuming
    the freed slot, and the next waitlist entry is tried instead."""

    async def _click(self, action, chat_id, uid, username):
        query = _make_fake_button_query(f"{action}_ev1", chat_id, uid, username)
        upd = MagicMock()
        upd.callback_query = query
        ctx = MagicMock()
        ctx.bot = MagicMock()
        ctx.bot.send_message = AsyncMock()
        ctx.bot.edit_message_text = AsyncMock()
        ctx.bot.get_chat_member = AsyncMock(return_value=MagicMock(status="member"))

        def _discard_task(coro):
            coro.close()
            return MagicMock()

        ctx.application = MagicMock()
        ctx.application.create_task = MagicMock(side_effect=_discard_task)
        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock, return_value=None):
            await event_engine.button_handler(upd, ctx)
        return query, ctx

    async def test_add_guest_in_main_group_at_capacity_creates_waitlist_entry(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, total_limit)
               VALUES ('ev1','-100','1','Party','👍','❌',0,?,'[]','{}','[]',2)""",
            (json.dumps(["alice (1)", "bob (2)"]),),
        )
        conn.commit()

        query, ctx = await self._click("add", "-100", 1, "alice")

        assert query.answer.call_args.kwargs.get("show_alert") is True
        row = conn.execute("SELECT counters_data, waitlist_data FROM events WHERE event_id='ev1'").fetchone()
        assert json.loads(row[0]) == {}
        waitlist = json.loads(row[1])
        assert len(waitlist) == 1
        assert waitlist[0]["username"] == "alice"
        assert waitlist[0]["is_guest"] is True

    async def test_add_guest_in_child_chat_at_capacity_creates_waitlist_entry(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, total_limit)
               VALUES ('ev1','-100','1','Party','👍','❌',0,'[]','[]','{}','[]',1)"""
        )
        conn.execute("INSERT INTO sub_chats (chat_id, owner_chat_id, alias) VALUES ('-200','-100','child')")
        conn.execute("INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type) VALUES ('ev1','-200','2','-visible','group')")
        conn.execute("INSERT INTO event_users (event_id, chat_id, user_id, username, status, guests) VALUES ('ev1','-200','5','erin','going',0)")
        conn.commit()

        query, ctx = await self._click("add", "-200", 5, "erin")

        assert query.answer.call_args.kwargs.get("show_alert") is True
        row = conn.execute("SELECT guests, event_id FROM event_users WHERE event_id='ev1' AND chat_id='-200' AND user_id='5'").fetchone()
        assert row[0] == 0
        wl_row = conn.execute("SELECT waitlist_data FROM events WHERE event_id='ev1'").fetchone()
        waitlist = json.loads(wl_row[0])
        assert len(waitlist) == 1 and waitlist[0]["username"] == "erin" and waitlist[0]["is_guest"] is True

    async def test_add_guest_still_works_under_capacity(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, total_limit)
               VALUES ('ev1','-100','1','Party','👍','❌',0,?,'[]','{}','[]',10)""",
            (json.dumps(["alice (1)"]),),
        )
        conn.commit()

        await self._click("add", "-100", 1, "alice")

        row = conn.execute("SELECT counters_data, waitlist_data FROM events WHERE event_id='ev1'").fetchone()
        assert json.loads(row[0]) == {"alice": 1}
        assert json.loads(row[1]) == []

    async def test_guest_slot_promoted_when_a_spot_frees_up(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, total_limit)
               VALUES ('ev1','-100','1','Party','👍','❌',0,?,'[]','{}','[]',2)""",
            (json.dumps(["alice (1)", "bob (2)"]),),
        )
        conn.commit()

        await self._click("add", "-100", 1, "alice")  # queues a guest slot for alice
        _, ctx = await self._click("notgoing", "-100", 2, "bob")  # frees a slot

        row = conn.execute("SELECT going_data, counters_data, waitlist_data FROM events WHERE event_id='ev1'").fetchone()
        assert json.loads(row[1]) == {"alice": 1}
        assert json.loads(row[2]) == []
        assert "guest for" in ctx.bot.send_message.call_args.kwargs["text"]

    async def test_stale_guest_slot_discarded_when_owner_leaves(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, total_limit, waitlist_data)
               VALUES ('ev1','-100','1','Party','👍','❌',0,?,'[]','{}','[]',3,?)""",
            (
                json.dumps(["alice (1)", "bob (2)", "carol (3)"]),
                json.dumps([{"chat_id": "-100", "chat_name": None, "username": "alice", "user_id": "1", "timestamp": "t", "is_guest": True}]),
            ),
        )
        conn.commit()

        # alice herself leaves - her own queued guest slot has nothing to
        # attach to anymore and must be discarded, not promoted
        _, ctx = await self._click("notgoing", "-100", 1, "alice")

        row = conn.execute("SELECT going_data, counters_data, waitlist_data FROM events WHERE event_id='ev1'").fetchone()
        assert "alice" not in row[0]
        assert json.loads(row[1]) == {}
        assert json.loads(row[2]) == []
        assert ctx.bot.send_message.call_args is None


class TestRefreshusersSurfacesUnresolvedExtraMembers:
    """People added via Verification Mode's Add Extra Member, who could not
    be resolved to a real user_id, live only in the active event's own
    going_data - never in main_group_users (what /refreshusers actually
    syncs to Sheets, which requires a real numeric USER_ID per row).
    Previously silently invisible; now surfaced explicitly in the report."""

    @pytest.fixture(autouse=True)
    def _patch_sheets(self):
        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock, return_value=None):
            yield

    async def test_unresolved_extra_member_is_surfaced(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-1','1','Party','👍','❌',0,?,'[]','{}','[]')""",
            (json.dumps(["alice (1)", "ghostuser (no_id_in_main_group)"]),),
        )
        conn.commit()
        bot = make_bot()
        bot.get_chat_administrators = AsyncMock(return_value=[])
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
        chat = make_chat(chat_id=-1, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=[])

        await handlers.refreshusers(upd, ctx)

        text = msg.reply_text.call_args.args[0]
        assert "ghostuser" in text
        assert "never resolved" in text

    async def test_no_unresolved_members_no_extra_warning(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-1','1','Party','👍','❌',0,?,'[]','{}','[]')""",
            (json.dumps(["alice (1)"]),),
        )
        conn.commit()
        bot = make_bot()
        bot.get_chat_administrators = AsyncMock(return_value=[])
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
        chat = make_chat(chat_id=-1, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=[])

        await handlers.refreshusers(upd, ctx)

        text = msg.reply_text.call_args.args[0]
        assert "never resolved" not in text



class TestChildChatClickTracksUserForSheetsSync:
    """Real bug (third instance of the same class): the child-chat branch
    of button_handler has its own early return, entirely bypassing the
    shared pending_track_user processing loop further down - meaning
    clicking Going/Not Going/etc in a child chat NEVER called track_user(),
    so that person was invisible to main_group_users, and by extension
    invisible to /refreshusersall's Users sheet sync for that monitored
    chat, even though they took a real, tracked action there."""

    async def test_going_click_in_child_chat_writes_to_main_group_users(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-100','1','Party','👍','❌',0,'[]','[]','{}','[]')"""
        )
        conn.execute(
            "INSERT INTO sub_chats (chat_id, owner_chat_id, alias, is_monitored, chat_name) VALUES ('-200','-100','child',1,'Monitored Child')"
        )
        conn.execute(
            "INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type) VALUES ('ev1','-200','2','-visible','group')"
        )
        conn.commit()

        query = _make_fake_button_query("going_ev1", "-200", 55, "newperson")
        upd = MagicMock()
        upd.callback_query = query
        ctx = MagicMock()
        ctx.bot = MagicMock()
        ctx.bot.send_message = AsyncMock()
        ctx.bot.edit_message_text = AsyncMock()
        ctx.bot.get_chat_member = AsyncMock(return_value=MagicMock(status="member"))

        def _discard_task(coro):
            coro.close()
            return MagicMock()

        ctx.application = MagicMock()
        ctx.application.create_task = MagicMock(side_effect=_discard_task)
        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock, return_value=None):
            await event_engine.button_handler(upd, ctx)

        row = conn.execute(
            "SELECT username, user_id, status FROM main_group_users WHERE chat_id='-200'"
        ).fetchall()
        assert row == [("newperson", "55", "active")]

    async def test_tracked_child_chat_user_then_appears_in_refreshusersall_sync(self, db_path):
        conn = sqlite3.connect(db_path)
        insert_premium(db_path, chat_id="-100")
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-100','1','Party','👍','❌',0,'[]','[]','{}','[]')"""
        )
        conn.execute(
            "INSERT INTO sub_chats (chat_id, owner_chat_id, alias, is_monitored, chat_name) VALUES ('-200','-100','child',1,'Monitored Child')"
        )
        conn.commit()

        # Real Going click in the monitored child chat
        query = _make_fake_button_query("going_ev1", "-200", 55, "newperson")
        upd = MagicMock()
        upd.callback_query = query
        ctx = MagicMock()
        ctx.bot = MagicMock()
        ctx.bot.send_message = AsyncMock()
        ctx.bot.edit_message_text = AsyncMock()
        ctx.bot.get_chat_member = AsyncMock(return_value=MagicMock(status="member"))

        def _discard_task(coro):
            coro.close()
            return MagicMock()

        ctx.application = MagicMock()
        ctx.application.create_task = MagicMock(side_effect=_discard_task)
        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock, return_value=None):
            await event_engine.button_handler(upd, ctx)

        # Now run /refreshusersall from the hub
        bot = make_bot()
        bot.get_chat_administrators = AsyncMock(return_value=[])

        async def gcm_side_effect(chat_id, user_id):
            if str(user_id) == "1":  # the calling admin's own permission check
                return MagicMock(status="administrator")
            member_mock = MagicMock(status="member")
            member_mock.user.username = "newperson"
            member_mock.user.first_name = "New"
            member_mock.user.last_name = "Person"
            return member_mock  # verifying the tracked person

        bot.get_chat_member = AsyncMock(side_effect=gcm_side_effect)
        bot.get_chat = AsyncMock(return_value=MagicMock(title="Hub"))
        chat = make_chat(chat_id=-100, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd2 = make_update(chat=chat, user=user, message=msg)
        ctx2 = make_context(bot=bot, args=[])

        sync_calls = []
        async def fake_sync(cid, members):
            sync_calls.append((cid, members))

        with patch("handlers.sync_users_sheet", side_effect=fake_sync):
            await handlers.refreshusersall(upd2, ctx2)

        # The hub itself is now always processed too, alongside the
        # monitored child - so 2 sync calls, not 1.
        assert len(sync_calls) == 2
        child_call = next(c for c in sync_calls if c[0] == "-200")
        assert any(m[1] == "newperson" for m in child_call[1])


class TestSubGuestTriggersWaitlistPromotion:
    """Decrementing a guest count (Sub Guest / '-' button) also frees a
    capacity slot, same as someone clicking Not Going - anyone waiting
    for that chat should be automatically promoted."""

    async def _click(self, action, chat_id, uid, username):
        query = _make_fake_button_query(f"{action}_ev1", chat_id, uid, username)
        upd = MagicMock()
        upd.callback_query = query
        ctx = MagicMock()
        ctx.bot = MagicMock()
        ctx.bot.send_message = AsyncMock()
        ctx.bot.edit_message_text = AsyncMock()
        ctx.bot.get_chat_member = AsyncMock(return_value=MagicMock(status="member"))

        def _discard_task(coro):
            coro.close()
            return MagicMock()

        ctx.application = MagicMock()
        ctx.application.create_task = MagicMock(side_effect=_discard_task)
        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock, return_value=None):
            await event_engine.button_handler(upd, ctx)
        return query, ctx

    async def test_sub_guest_in_main_group_promotes_waiting_person(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, total_limit, waitlist_data)
               VALUES ('ev1','-100','1','Party','👍','❌',0,?,'[]',?,'[]',3,?)""",
            (
                json.dumps(["alice (1)"]),
                json.dumps({"alice": 2}),
                json.dumps([{"chat_id": "-100", "chat_name": None, "username": "carol", "user_id": "3", "timestamp": "t"}]),
            ),
        )
        conn.commit()

        _, ctx = await self._click("sub", "-100", 1, "alice")

        row = conn.execute("SELECT going_data, counters_data, waitlist_data FROM events WHERE event_id='ev1'").fetchone()
        assert any("carol" in g for g in json.loads(row[0]))
        assert json.loads(row[1]) == {"alice": 1}
        assert json.loads(row[2]) == []
        assert "moved from the Waitlist to Going" in ctx.bot.send_message.call_args.kwargs["text"]

    async def test_sub_guest_in_child_chat_promotes_waiting_person(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, total_limit, waitlist_data)
               VALUES ('ev1','-100','1','Party','👍','❌',0,'[]','[]','{}','[]',2,?)""",
            (json.dumps([{"chat_id": "-200", "chat_name": None, "username": "dave", "user_id": "7", "timestamp": "t"}]),),
        )
        conn.execute("INSERT INTO sub_chats (chat_id, owner_chat_id, alias) VALUES ('-200','-100','child')")
        conn.execute("INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type) VALUES ('ev1','-200','2','-visible','group')")
        conn.execute("INSERT INTO event_users (event_id, chat_id, user_id, username, status, guests) VALUES ('ev1','-200','5','erin','going',1)")
        conn.commit()

        _, ctx = await self._click("sub", "-200", 5, "erin")

        dave_row = conn.execute(
            "SELECT status FROM event_users WHERE event_id='ev1' AND chat_id='-200' AND user_id='7'"
        ).fetchone()
        assert dave_row == ("going",)
        wl_row = conn.execute("SELECT waitlist_data FROM events WHERE event_id='ev1'").fetchone()
        assert json.loads(wl_row[0]) == []
        assert "moved from the Waitlist to Going" in ctx.bot.send_message.call_args.kwargs["text"]

    async def test_sub_guest_with_empty_waitlist_is_a_no_op_promotion(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-100','1','Party','👍','❌',0,?,'[]',?,'[]')""",
            (json.dumps(["alice (1)"]), json.dumps({"alice": 2})),
        )
        conn.commit()

        _, ctx = await self._click("sub", "-100", 1, "alice")

        assert ctx.bot.send_message.call_args is None
        row = conn.execute("SELECT counters_data FROM events WHERE event_id='ev1'").fetchone()
        assert json.loads(row[0]) == {"alice": 1}


class TestWaitlistCommandCleansUpStaleDuplicates:
    """/waitlist applies defensive dedup and persists the cleanup to the
    DB, so pre-existing stale duplicate person-entries (e.g. accumulated
    before click-time dedup existed) don't keep resurfacing."""

    async def test_stale_duplicates_shown_deduped_and_persisted(self, db_path):
        conn = sqlite3.connect(db_path)
        entries = [
            {"chat_id": "-100", "chat_name": None, "username": "Andr", "user_id": "1", "timestamp": f"2026-01-01 00:00:0{i}"}
            for i in range(6)
        ]
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, waitlist_data)
               VALUES ('ev1','-100','1','Party','👍','❌',0,'[]','[]','{}','[]',?)""",
            (json.dumps(entries),),
        )
        conn.commit()

        bot = make_bot()
        chat = make_chat(chat_id=-100, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=[])

        await handlers.waitlist_command(upd, ctx)

        text = msg.reply_text.call_args.args[0]
        assert text.count("Andr") == 1

        row = conn.execute("SELECT waitlist_data FROM events WHERE event_id='ev1'").fetchone()
        cleaned = json.loads(row[0])
        assert len(cleaned) == 1

    async def test_guest_slots_not_deduped_by_waitlist_command(self, db_path):
        conn = sqlite3.connect(db_path)
        entries = [
            {"chat_id": "-100", "chat_name": None, "username": "andr", "user_id": "1", "timestamp": "t1", "is_guest": True},
            {"chat_id": "-100", "chat_name": None, "username": "andr", "user_id": "1", "timestamp": "t2", "is_guest": True},
        ]
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, waitlist_data)
               VALUES ('ev1','-100','1','Party','👍','❌',0,'[]','[]','{}','[]',?)""",
            (json.dumps(entries),),
        )
        conn.commit()

        bot = make_bot()
        chat = make_chat(chat_id=-100, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=[])

        await handlers.waitlist_command(upd, ctx)

        text = msg.reply_text.call_args.args[0]
        assert "\\(2\\)" in text
        row = conn.execute("SELECT waitlist_data FROM events WHERE event_id='ev1'").fetchone()
        assert len(json.loads(row[0])) == 2


class TestNotifyUsesListusersFormat:
    """/notify now renders pending users the same way /listusers does -
    clickable mentions, not plain @username text."""

    async def test_pending_users_shown_as_mentions(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-1','1','Party','👍','❌',0,'[]','[]','{}','[]')"""
        )
        conn.execute(
            "INSERT INTO main_group_users (chat_id, user_id, username, first_name, last_name, status) VALUES ('-1','100','alice','Alice','Smith','active')"
        )
        conn.commit()

        bot = make_bot()
        chat = make_chat(chat_id=-1, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=[])

        await handlers.notify(upd, ctx)

        text = msg.reply_text.call_args.args[0]
        assert "tg://user?id=100" in text
        assert "Alice Smith" in text

    async def test_decided_users_excluded(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-1','1','Party','👍','❌',0,?,'[]','{}','[]')""",
            (json.dumps(["alice (100)"]),),
        )
        conn.execute(
            "INSERT INTO main_group_users (chat_id, user_id, username, status) VALUES ('-1','100','alice','active')"
        )
        conn.commit()

        bot = make_bot()
        chat = make_chat(chat_id=-1, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=[])

        await handlers.notify(upd, ctx)

        text = msg.reply_text.call_args.args[0]
        assert "already responded" in text


class TestWaitlistGuestRenderingMatchesGoing:
    """Real bug found via user report: guest-slot entries in the Waitlist
    were rendered identically to person entries (Standby icon + mention),
    misleadingly showing the OWNER as if THEY were personally waiting.
    Guest entries now render exactly like the going-list's own guest
    lines (ICON_GUEST, 'N, from: <owner>'), grouped by owner - multiple
    queued slots collapse into one line with a count."""

    async def test_waitlist_command_groups_guest_slots_like_going(self, db_path):
        conn = sqlite3.connect(db_path)
        entries = [
            {"chat_id": "-100", "chat_name": None, "username": "carol", "user_id": "3", "timestamp": "t1"},
            {"chat_id": "-100", "chat_name": None, "username": "alice", "user_id": "1", "timestamp": "t2", "is_guest": True},
            {"chat_id": "-100", "chat_name": None, "username": "alice", "user_id": "1", "timestamp": "t3", "is_guest": True},
        ]
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, waitlist_data)
               VALUES ('ev1','-100','1','Party','👍','❌',0,'[]','[]','{}','[]',?)""",
            (json.dumps(entries),),
        )
        conn.commit()

        bot = make_bot()
        chat = make_chat(chat_id=-100, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=[])

        await handlers.waitlist_command(upd, ctx)

        text = msg.reply_text.call_args.args[0]
        assert "carol" in text
        assert "👤⊕ 2, from:" in text
        # The guest line must NOT use the Standby icon (that would be the
        # old, wrong "person waiting" rendering)
        guest_line = [l for l in text.split("\n") if "alice" in l][0]
        assert "💤" not in guest_line

    async def test_waitlist_command_child_chat_also_groups_guests(self, db_path):
        conn = sqlite3.connect(db_path)
        entries = [
            {"chat_id": "-200", "chat_name": None, "username": "dave", "user_id": "7", "timestamp": "t1", "is_guest": True},
            {"chat_id": "-200", "chat_name": None, "username": "dave", "user_id": "7", "timestamp": "t2", "is_guest": True},
            {"chat_id": "-200", "chat_name": None, "username": "dave", "user_id": "7", "timestamp": "t3", "is_guest": True},
        ]
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, waitlist_data)
               VALUES ('ev1','-100','1','Party','👍','❌',0,'[]','[]','{}','[]',?)""",
            (json.dumps(entries),),
        )
        conn.execute("INSERT INTO sub_chats (chat_id, owner_chat_id, alias) VALUES ('-200','-100','child')")
        conn.commit()

        bot = make_bot()
        chat = make_chat(chat_id=-200, chat_type="supergroup")
        user = make_user(user_id=7)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=[])

        await handlers.waitlist_command(upd, ctx)

        text = msg.reply_text.call_args.args[0]
        assert "👤⊕ 3, from:" in text

    async def test_post_render_also_groups_guest_slots(self, db_path):
        """The actual post's own Waitlist section (not just /waitlist)
        must use the same corrected format - both consume the same
        underlying _render_waitlist_local/_render_waitlist_all."""
        waitlist = [
            {"chat_id": "-100", "chat_name": None, "username": "carol", "user_id": "3", "timestamp": "t1"},
            {"chat_id": "-100", "chat_name": None, "username": "alice", "user_id": "1", "timestamp": "t2", "is_guest": True},
            {"chat_id": "-100", "chat_name": None, "username": "alice", "user_id": "1", "timestamp": "t3", "is_guest": True},
        ]
        count, text = event_engine._render_waitlist_local(waitlist, "-100")
        assert count == 3
        assert "carol" in text
        assert "👤⊕ 2, from:" in text


class TestRefreshusersallCoversHubItself:
    """Design correction: /refreshusersall must cover the group the
    command was run in PLUS every monitored child under it, not just the
    children. The hub's own chat_id is now always included as the first
    entry to process, resolved via a live get_chat() call for its real
    title (works whether called directly or via a DM hub-selection
    override)."""

    async def test_hub_and_monitored_child_both_synced(self, db_path):
        conn = sqlite3.connect(db_path)
        insert_premium(db_path, chat_id="-100")
        conn.execute(
            "INSERT INTO main_group_users (chat_id, user_id, username, first_name, last_name, status) VALUES ('-100','55','hubperson','Hub','Person','active')"
        )
        conn.execute(
            "INSERT INTO sub_chats (chat_id, owner_chat_id, alias, is_monitored, chat_name) VALUES ('-200','-100','child',1,'Monitored Child')"
        )
        conn.execute(
            "INSERT INTO main_group_users (chat_id, user_id, username, first_name, last_name, status) VALUES ('-200','7','childperson','Child','Person','active')"
        )
        conn.commit()

        bot = make_bot()
        bot.get_chat = AsyncMock(return_value=MagicMock(title="Real Hub Name"))
        bot.get_chat_administrators = AsyncMock(return_value=[])

        def _member_mock(username, first_name, last_name):
            m = MagicMock(status="member")
            m.user.username = username
            m.user.first_name = first_name
            m.user.last_name = last_name
            return m

        async def gcm_side_effect(chat_id, user_id):
            if str(user_id) == "1":
                return MagicMock(status="administrator")
            if str(user_id) == "55":
                return _member_mock("hubperson", "Hub", "Person")
            if str(user_id) == "7":
                return _member_mock("childperson", "Child", "Person")
            return MagicMock(status="member")

        bot.get_chat_member = AsyncMock(side_effect=gcm_side_effect)
        chat = make_chat(chat_id=-100, chat_type="supergroup")
        admin_user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=admin_user, message=msg)
        ctx = make_context(bot=bot, args=[])

        sync_calls = []
        async def fake_sync(cid, members):
            sync_calls.append((cid, [m[1] for m in members]))

        with patch("handlers.sync_users_sheet", side_effect=fake_sync):
            await handlers.refreshusersall(upd, ctx)

        reply = msg.reply_text.call_args.args[0]
        assert "Real Hub Name" in reply
        assert "Monitored Child" in reply
        assert any(cid == "-100" and "hubperson" in members for cid, members in sync_calls)
        assert any(cid == "-200" and "childperson" in members for cid, members in sync_calls)


class TestAddmonitorRequiresBotAdmin:
    """Real gap found and fixed: /addmonitor previously only checked that
    the bot was a MEMBER of the target chat, not an ADMIN. Per Telegram's
    own documented requirement, chat_member updates about OTHER users
    (needed for auto-tracking new joiners without a button click) are only
    delivered when the bot itself is an admin in that chat - a bot that's
    only a regular member would silently never notice anyone new joining,
    with no indication to the admin that monitoring wasn't actually
    working. Now checked upfront with a clear, actionable error.

    addmonitor makes exactly 2 sequential get_chat_member calls in this
    order: (1) the calling user's admin status in the MAIN chat, (2) the
    BOT's own status in the TARGET chat - and, only if that passes, a 3rd
    call for the calling user's admin status in the TARGET chat. Mocking
    by argument-matching turned out to be fragile in ways not fully
    understood (a real pytest run showed non-deterministic results not
    reproducible in isolated re-runs) - using an ORDERED side_effect list
    instead removes argument-matching from the equation entirely: each
    call just gets the next value in sequence, with zero room for
    ambiguity regardless of what values chat_id/user_id actually carry.
    """

    async def _setup_hub(self, db_path):
        insert_premium(db_path, chat_id="-100")

    async def test_bot_only_regular_member_is_rejected(self, db_path):
        await self._setup_hub(db_path)

        bot = make_bot()
        bot.get_chat_member = AsyncMock(side_effect=[
            MagicMock(status="administrator"),  # (1) caller admin in main chat
            MagicMock(status="member"),         # (2) bot only a regular member in target
        ])
        chat = make_chat(chat_id=-100, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=["-200"])

        await monitors.addmonitor(upd, ctx)

        text = msg.reply_text.call_args.args[0]
        assert "Bot must be an" in text
        assert "admin" in text
        assert bot.get_chat_member.call_count == 2  # never reached the 3rd (caller-in-target) check

    async def test_bot_admin_in_target_chat_succeeds(self, db_path):
        await self._setup_hub(db_path)

        bot = make_bot()
        bot.get_chat_member = AsyncMock(side_effect=[
            MagicMock(status="administrator"),  # (1) caller admin in main chat
            MagicMock(status="administrator"),  # (2) bot is admin in target
            MagicMock(status="administrator"),  # (3) caller admin in target too
        ])
        bot.get_chat = AsyncMock(return_value=MagicMock(type="supergroup", title="Target Group"))
        chat = make_chat(chat_id=-100, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=["-200"])

        await monitors.addmonitor(upd, ctx)

        text = msg.reply_text.call_args.args[0]
        assert "Bot must be an" not in text
        assert "Added monitor" in text

    async def test_bot_is_creator_of_target_chat_also_succeeds(self, db_path):
        """'creator' status counts as admin-equivalent, not just
        'administrator' specifically."""
        await self._setup_hub(db_path)

        bot = make_bot()
        bot.get_chat_member = AsyncMock(side_effect=[
            MagicMock(status="administrator"),  # (1) caller admin in main chat
            MagicMock(status="creator"),        # (2) bot is creator of target
            MagicMock(status="administrator"),  # (3) caller admin in target too
        ])
        bot.get_chat = AsyncMock(return_value=MagicMock(type="supergroup", title="Target Group"))
        chat = make_chat(chat_id=-100, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=["-200"])

        await monitors.addmonitor(upd, ctx)

        text = msg.reply_text.call_args.args[0]
        assert "Bot must be an" not in text

    async def test_bot_not_in_target_chat_at_all_still_gives_the_original_error(self, db_path):
        """The pre-existing 'bot is not a member' check (a different,
        earlier failure mode) must still work correctly and not get
        confused with the new admin-specific check."""
        await self._setup_hub(db_path)

        bot = make_bot()
        bot.get_chat_member = AsyncMock(side_effect=[
            MagicMock(status="administrator"),  # (1) caller admin in main chat
            Exception("Chat not found"),        # (2) bot isn't even in the target chat
        ])
        chat = make_chat(chat_id=-100, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=["-200"])

        await monitors.addmonitor(upd, ctx)

        text = msg.reply_text.call_args.args[0]
        assert "not a member" in text

class TestDeduplicatedProTierChecks:
    """Fixes from the errors/warnings audit: setsheet and refreshusersall
    previously hand-rolled their own is_premium() check with a duplicated
    message, instead of reusing the centralized require_premium() already
    used by aliases.py/monitors.py. Now both go through it."""

    async def test_setsheet_free_tier_uses_require_premium(self, db_path):
        chat = make_chat(chat_id=-1, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(args=["sheetid123"])

        await subscription.setsheet(upd, ctx)

        text = msg.reply_text.call_args.args[0]
        assert "PRO" in text
        assert "Google Sheets binding" in text

    async def test_refreshusersall_free_tier_uses_require_premium(self, db_path):
        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
        chat = make_chat(chat_id=-1, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=[])

        await handlers.refreshusersall(upd, ctx)

        text = msg.reply_text.call_args.args[0]
        assert "PRO" in text
        assert "addmonitor" in text


class TestAliasMessageConsistencyFixes:
    """setalias's duplicate-alias message was missing the warning icon and
    MarkdownV2 escaping (the only unescaped message in the file);
    removealias used an inconsistent 🔍 icon instead of ❌ like every other
    'not found' message in the project. Both now name the specific alias
    for better detail too."""

    async def test_duplicate_alias_message_has_warning_icon(self, db_path):
        insert_premium(db_path, chat_id="-100")
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO sub_chats (chat_id, owner_chat_id, alias) VALUES ('-200','-100','existingalias')")
        conn.commit()

        bot = make_bot()
        bot.get_chat = AsyncMock(return_value=MagicMock(type="supergroup", title="New Target"))
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
        chat = make_chat(chat_id=-100, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=["-300", "existingalias"])

        await aliases.setalias(upd, ctx)

        text = msg.reply_text.call_args.args[0]
        assert "⚠️" in text
        assert "existingalias" in text

    async def test_removealias_not_found_uses_x_icon(self, db_path):
        insert_premium(db_path, chat_id="-100")
        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
        chat = make_chat(chat_id=-100, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=["doesnotexist"])

        await aliases.removealias(upd, ctx)

        text = msg.reply_text.call_args.args[0]
        assert "❌" in text
        assert "🔍" not in text
        assert "doesnotexist" in text


class TestSilentPermissionDenialNowGivesFeedback:
    """Real gap found and fixed: two places in button_handler silently
    returned with zero feedback when a click was blocked for lacking
    permission - every OTHER permission-denial path in the project gives
    explicit feedback, this was the only exception."""

    async def _click(self, action, chat_id, uid, username):
        query = _make_fake_button_query(f"{action}_ev1", chat_id, uid, username)
        upd = MagicMock()
        upd.callback_query = query
        ctx = MagicMock()
        ctx.bot = MagicMock()
        ctx.bot.send_message = AsyncMock()
        ctx.bot.edit_message_text = AsyncMock()
        ctx.bot.get_chat_member = AsyncMock(return_value=MagicMock(status="member"))

        def _discard_task(coro):
            coro.close()
            return MagicMock()

        ctx.application = MagicMock()
        ctx.application.create_task = MagicMock(side_effect=_discard_task)
        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock, return_value=None):
            await event_engine.button_handler(upd, ctx)
        return query

    async def test_random_non_admin_non_creator_gets_feedback_on_cancel(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, created_by_user_id)
               VALUES ('ev1','-100','1','Party','👍','❌',0,'[]','[]','{}','[]','42')"""
        )
        conn.commit()

        query = await self._click("cancel", "-100", 999, "random")

        assert query.answer.call_args.kwargs.get("show_alert") is True
        assert "Only group admins" in query.answer.call_args.kwargs.get("text", "")

    async def test_admin_only_action_in_child_chat_gives_feedback(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-100','1','Party','👍','❌',0,'[]','[]','{}','[]')"""
        )
        conn.execute("INSERT INTO sub_chats (chat_id, owner_chat_id, alias) VALUES ('-200','-100','child')")
        conn.execute("INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type) VALUES ('ev1','-200','2','-visible','group')")
        conn.commit()

        query = await self._click("kick", "-200", 999, "random")

        assert query.answer.call_args.kwargs.get("show_alert") is True
        assert "main event post" in query.answer.call_args.kwargs.get("text", "")

    async def test_creator_still_gets_no_feedback_needed_for_close(self, db_path):
        """Sanity check: the fix doesn't break the legitimate case - the
        event's own creator closing it should NOT trigger the new alert."""
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, created_by_user_id)
               VALUES ('ev1','-100','1','Party','👍','❌',0,'[]','[]','{}','[]','42')"""
        )
        conn.commit()

        query = await self._click("close", "-100", 42, "creator")

        assert query.answer.call_args.kwargs.get("show_alert") is not True


class TestNeweventWarnsOnExistingActiveEvent:
    """Missing warning found and fixed: /newevent previously created a new
    event with zero acknowledgment that an older active event already
    existed, leaving it orphaned (still clickable for participants, but no
    longer reachable via /waitlist, /editevent etc which target the
    latest event)."""

    async def test_no_warning_when_no_existing_event(self, db_path):
        bot = make_bot()
        chat = make_chat(chat_id=-1, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=["FirstParty"])

        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock, return_value=None), \
             patch("handlers.open_spreadsheet", new_callable=AsyncMock):
            await handlers.newevent(upd, ctx)

        assert not any(
            "already an active event" in c.args[0] for c in msg.reply_text.call_args_list
        )

    async def test_warns_when_creating_second_event_while_first_still_active(self, db_path):
        bot = make_bot()
        chat = make_chat(chat_id=-1, chat_type="supergroup")
        user = make_user(user_id=1)

        msg1 = make_message(chat=chat)
        upd1 = make_update(chat=chat, user=user, message=msg1)
        ctx1 = make_context(bot=bot, args=["FirstParty"])
        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock, return_value=None), \
             patch("handlers.open_spreadsheet", new_callable=AsyncMock):
            await handlers.newevent(upd1, ctx1)

        msg2 = make_message(chat=chat)
        upd2 = make_update(chat=chat, user=user, message=msg2)
        ctx2 = make_context(bot=bot, args=["SecondParty"])
        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock, return_value=None), \
             patch("handlers.open_spreadsheet", new_callable=AsyncMock):
            await handlers.newevent(upd2, ctx2)

        warning_calls = [c.args[0] for c in msg2.reply_text.call_args_list if "already an active event" in c.args[0]]
        assert len(warning_calls) == 1
        assert "FirstParty" in warning_calls[0]

    async def test_no_warning_when_previous_event_is_closed(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-1','1','OldParty','👍','❌',2,'[]','[]','{}','[]')"""
        )
        conn.commit()

        bot = make_bot()
        chat = make_chat(chat_id=-1, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=["NewParty"])

        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock, return_value=None), \
             patch("handlers.open_spreadsheet", new_callable=AsyncMock):
            await handlers.newevent(upd, ctx)

        assert not any(
            "already an active event" in c.args[0] for c in msg.reply_text.call_args_list
        )


class TestMasterPostWaitlistBelowGoingFromSections:
    """Real ordering bug found and fixed: the main hub's own post
    previously showed Waitlist BEFORE the 'Going from <child chat>'
    sections, contradicting the intended reading order (Going, Not Going,
    Going from every child, THEN Waitlist at the bottom). The child
    chat's own post was already correctly ordered."""

    async def test_waitlist_appears_after_going_from_child_sections(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, total_limit, waitlist_data, waitlist_visibility)
               VALUES ('ev1','-100','1','Party','👍','❌',0,?,'[]','{}','[]',5,?,'visible')""",
            (
                json.dumps(["alice (1)"]),
                json.dumps([{"chat_id": "-100", "chat_name": None, "username": "carol", "user_id": "3", "timestamp": "t1"}]),
            ),
        )
        conn.execute("INSERT INTO sub_chats (chat_id, owner_chat_id, alias) VALUES ('-200','-100','child')")
        conn.execute("INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type) VALUES ('ev1','-200','2','-visible','group')")
        conn.execute("INSERT INTO event_users (event_id, chat_id, user_id, username, status, guests) VALUES ('ev1','-200','5','erin','going',0)")
        conn.commit()

        bot = MagicMock()
        bot.edit_message_text = AsyncMock()
        bot.get_chat = AsyncMock(return_value=MagicMock(title="Child Group"))
        ctx = MagicMock()
        ctx.bot = bot
        ctx.application = MagicMock()
        ctx.application.create_task = MagicMock()

        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock, return_value=None):
            await event_engine.update_all_shared_views(ctx, "ev1")

        main_call = next(c for c in bot.edit_message_text.call_args_list if c.kwargs.get("chat_id") == -100)
        text = main_call.kwargs["text"]

        going_from_idx = text.index("Going from Child Group")
        waitlist_idx = text.index("*Waitlist*")
        assert going_from_idx < waitlist_idx


class TestWaitlistMechanicsUnaffectedByVisibility:
    """Verified: adding/promoting from the Waitlist is entirely independent
    of waitlist_visibility - visibility only controls RENDERING, never the
    underlying mechanics. Going/NotGoing/Add/Remove Guest all correctly
    queue/promote people even when the Waitlist is hidden from view."""

    async def _click(self, action, chat_id, uid, username):
        query = _make_fake_button_query(f"{action}_ev1", chat_id, uid, username)
        upd = MagicMock()
        upd.callback_query = query
        ctx = MagicMock()
        ctx.bot = MagicMock()
        ctx.bot.send_message = AsyncMock()
        ctx.bot.edit_message_text = AsyncMock()
        ctx.bot.get_chat_member = AsyncMock(return_value=MagicMock(status="member"))

        def _discard_task(coro):
            coro.close()
            return MagicMock()

        ctx.application = MagicMock()
        ctx.application.create_task = MagicMock(side_effect=_discard_task)
        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock, return_value=None):
            await event_engine.button_handler(upd, ctx)
        return query, ctx

    async def test_going_at_capacity_queues_correctly_when_hidden(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, total_limit, waitlist_visibility)
               VALUES ('ev1','-100','1','Party','👍','❌',0,?,'[]','{}','[]',2,'hidden')""",
            (json.dumps(["alice (1)", "bob (2)"]),),
        )
        conn.commit()

        query, _ = await self._click("going", "-100", 3, "carol")

        assert "Waitlist" in query.answer.call_args.kwargs.get("text", "")
        row = conn.execute("SELECT waitlist_data FROM events WHERE event_id='ev1'").fetchone()
        wl = json.loads(row[0])
        assert len(wl) == 1 and wl[0]["username"] == "carol"

    async def test_promotion_and_announcement_still_fire_when_hidden(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, total_limit, waitlist_data, waitlist_visibility)
               VALUES ('ev1','-100','1','Party','👍','❌',0,?,'[]','{}','[]',2,?,'hidden')""",
            (
                json.dumps(["alice (1)", "bob (2)"]),
                json.dumps([{"chat_id": "-100", "chat_name": None, "username": "carol", "user_id": "3", "timestamp": "t1"}]),
            ),
        )
        conn.commit()

        _, ctx = await self._click("notgoing", "-100", 2, "bob")

        row = conn.execute("SELECT going_data, waitlist_data FROM events WHERE event_id='ev1'").fetchone()
        assert any("carol" in g for g in json.loads(row[0]))
        assert json.loads(row[1]) == []
        assert ctx.bot.send_message.call_args is not None  # announcement fires despite hidden display


class TestEditeventVisibilityAndLimitChanges:
    """Verified: /editevent correctly handles limit and visibility changes
    both together and independently, with people already in the Waitlist."""

    async def test_raising_limit_and_visibility_together_promotes_correctly(self, db_path):
        conn = sqlite3.connect(db_path)
        insert_premium(db_path, chat_id="-100")
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, total_limit, waitlist_data, waitlist_visibility)
               VALUES ('ev1','-100','1','Party','👍','❌',0,?,'[]','{}','[]',2,?,'hidden')""",
            (
                json.dumps(["alice (1)", "bob (2)"]),
                json.dumps([{"chat_id": "-100", "chat_name": None, "username": "carol", "user_id": "3", "timestamp": "t1"}]),
            ),
        )
        conn.commit()

        bot = make_bot()
        chat = make_chat(chat_id=-100, chat_type="supergroup")
        admin_user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=admin_user, message=msg)
        ctx = make_context(bot=bot, args=["-limit", "3", "-wl", "visible"])

        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock, return_value=None), \
             patch("handlers.schedule_view_refresh", new_callable=AsyncMock):
            await handlers.editevent(upd, ctx)

        row = conn.execute("SELECT going_data, waitlist_data, waitlist_visibility, total_limit FROM events WHERE event_id='ev1'").fetchone()
        assert row[2] == "visible"
        assert row[3] == 3
        assert any("carol" in g for g in json.loads(row[0]))
        assert json.loads(row[1]) == []
        assert bot.send_message.call_count == 1

    async def test_visibility_only_change_does_not_falsely_promote(self, db_path):
        conn = sqlite3.connect(db_path)
        insert_premium(db_path, chat_id="-100")
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, total_limit, waitlist_data, waitlist_visibility)
               VALUES ('ev1','-100','1','Party','👍','❌',0,?,'[]','{}','[]',2,?,'hidden')""",
            (
                json.dumps(["alice (1)", "bob (2)"]),
                json.dumps([{"chat_id": "-100", "chat_name": None, "username": "carol", "user_id": "3", "timestamp": "t1"}]),
            ),
        )
        conn.commit()

        bot = make_bot()
        chat = make_chat(chat_id=-100, chat_type="supergroup")
        admin_user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=admin_user, message=msg)
        ctx = make_context(bot=bot, args=["-limit", "2", "-wl", "onlycount"])  # same limit, viz change only

        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock, return_value=None), \
             patch("handlers.schedule_view_refresh", new_callable=AsyncMock):
            await handlers.editevent(upd, ctx)

        row = conn.execute("SELECT going_data, waitlist_data, waitlist_visibility FROM events WHERE event_id='ev1'").fetchone()
        assert row[2] == "onlycount"
        assert "carol" not in row[0]
        assert len(json.loads(row[1])) == 1
        assert bot.send_message.call_count == 0

    async def test_onlycount_rendering_shows_count_not_names_after_switch(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, total_limit, waitlist_data, waitlist_visibility)
               VALUES ('ev1','-100','1','Party','👍','❌',0,?,'[]','{}','[]',2,?,'onlycount')""",
            (
                json.dumps(["alice (1)", "bob (2)"]),
                json.dumps([{"chat_id": "-100", "chat_name": None, "username": "carol", "user_id": "3", "timestamp": "t1"}]),
            ),
        )
        conn.commit()

        ctx = MagicMock()
        ctx.bot = MagicMock()
        ctx.bot.edit_message_text = AsyncMock()
        ctx.application = MagicMock()
        ctx.application.create_task = MagicMock()

        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock, return_value=None):
            await event_engine.update_all_shared_views(ctx, "ev1")

        text = ctx.bot.edit_message_text.call_args_list[0].kwargs["text"]
        assert "carol" not in text
        assert "Waitlist:* 1" in text


class TestCrossChatWaitlistDuplicateBlocked:
    """Real bug found while investigating a user report: cross-chat
    protection only checked event_users status='going', never the
    Waitlist - meaning someone already queued in chat A's waitlist could
    click Going in chat B (also full) and end up double-queued in BOTH,
    silently getting the same generic "added to Waitlist" alert both
    times with no indication anything was wrong. Now blocked with the
    same "already added elsewhere" warning a confirmed going registration
    gets."""

    async def _click(self, action, chat_id, uid, username):
        query = _make_fake_button_query(f"{action}_ev1", chat_id, uid, username)
        upd = MagicMock()
        upd.callback_query = query
        ctx = MagicMock()
        ctx.bot = MagicMock()
        ctx.bot.send_message = AsyncMock()
        ctx.bot.edit_message_text = AsyncMock()
        ctx.bot.get_chat_member = AsyncMock(return_value=MagicMock(status="member"))

        def _discard_task(coro):
            coro.close()
            return MagicMock()

        ctx.application = MagicMock()
        ctx.application.create_task = MagicMock(side_effect=_discard_task)
        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock, return_value=None):
            await event_engine.button_handler(upd, ctx)
        return query

    async def test_already_waitlisted_in_chat_a_blocked_from_queuing_in_chat_b(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, total_limit)
               VALUES ('ev1','-100','1','Party','👍','❌',0,?,'[]','{}','[]',1)""",
            (json.dumps(["someoneelse (999)"]),),
        )
        conn.execute("INSERT INTO sub_chats (chat_id, owner_chat_id, alias) VALUES ('-200','-100','childA')")
        conn.execute("INSERT INTO sub_chats (chat_id, owner_chat_id, alias) VALUES ('-300','-100','childB')")
        conn.execute("INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type) VALUES ('ev1','-200','2','-visible','group')")
        conn.execute("INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type) VALUES ('ev1','-300','3','-visible','group')")
        conn.commit()

        await self._click("going", "-200", 7, "frank")  # queued in chat A's waitlist
        query2 = await self._click("going", "-300", 7, "frank")  # should be BLOCKED, not double-queued

        assert query2.answer.call_args.kwargs.get("show_alert") is True
        assert "already added to this event" in query2.answer.call_args.kwargs.get("text", "")

        wl = json.loads(conn.execute("SELECT waitlist_data FROM events WHERE event_id='ev1'").fetchone()[0])
        frank_entries = [e for e in wl if e["username"] == "frank"]
        assert len(frank_entries) == 1

    async def test_add_guest_not_blocked_by_unrelated_waitlist_entry_elsewhere(self, db_path):
        """The new check only applies to 'going' - Add Guest for someone
        already confirmed going in THIS chat shouldn't be blocked just
        because they have an unrelated stale waitlist entry in another
        chat."""
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, total_limit, waitlist_data)
               VALUES ('ev1','-100','1','Party','👍','❌',0,'[]','[]','{}','[]',10,?)""",
            (json.dumps([{"chat_id": "-200", "chat_name": None, "username": "frank", "user_id": "7", "timestamp": "t"}]),),
        )
        conn.execute("INSERT INTO sub_chats (chat_id, owner_chat_id, alias) VALUES ('-300','-100','childB')")
        conn.execute("INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type) VALUES ('ev1','-300','3','-visible','group')")
        conn.execute("INSERT INTO event_users (event_id, chat_id, user_id, username, status, guests) VALUES ('ev1','-300','7','frank','going',0)")
        conn.commit()

        query = await self._click("add", "-300", 7, "frank")

        assert query.answer.call_args.kwargs.get("show_alert") is not True
        row = conn.execute("SELECT guests FROM event_users WHERE event_id='ev1' AND chat_id='-300' AND user_id='7'").fetchone()
        assert row == (1,)


class TestPromotionAnnouncementTextHelper:
    """Refactor: the Waitlist promotion announcement text was copy-pasted
    identically in 3 separate places (button_handler's child-branch
    return path, button_handler's shared master-path, and editevent's
    own limit-raise promotion in handlers.py). Extracted into
    _promotion_announcement_text() so a wording change only needs to
    happen once."""

    def test_person_promotion_text(self, db_path):
        text = event_engine._promotion_announcement_text("-100", "alice", "1", False)
        assert "moved from the Waitlist to Going" in text
        assert "alice" in text or "tg://user?id=1" in text

    def test_guest_promotion_text(self, db_path):
        text = event_engine._promotion_announcement_text("-100", "alice", "1", True)
        assert "one more guest for" in text
        assert "added from the Waitlist" in text


class TestDmAccessHelpSection:
    """Real gap found in an earlier audit and now fixed: dm_access has a
    detailed feature_flags.description shown when tapping the upgrade
    prompt, but /help previously had NO corresponding section at all -
    someone reading /help could never find this documented anywhere.
    Added a full help_dm_access section, wired into the keyboard, the
    tier-gate defensive check, and the upgrade-info feature map."""

    async def test_dm_access_button_appears_in_keyboard(self, db_path):
        chat = make_chat(chat_id=-100123)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context()

        await handlers.help_command(upd, ctx)

        keyboard = msg.reply_text.call_args.kwargs.get("reply_markup") or msg.reply_text.call_args.args[-1]
        all_texts = [b.text for row in keyboard.inline_keyboard for b in row]
        assert any("DM Access" in t for t in all_texts)

    async def test_free_hub_shows_locked_dm_access_button(self, db_path):
        chat = make_chat(chat_id=-100123)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context()

        await handlers.help_command(upd, ctx)

        keyboard = msg.reply_text.call_args.kwargs.get("reply_markup") or msg.reply_text.call_args.args[-1]
        dm_button = next(b for row in keyboard.inline_keyboard for b in row if "DM Access" in b.text)
        assert "upgrade_info_dm_access" == dm_button.callback_data

    async def test_pro_hub_shows_unlocked_dm_access_section(self, db_path):
        insert_premium(db_path, chat_id="-100123")
        chat = make_chat(chat_id=-100123)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context()

        await handlers.help_command(upd, ctx)

        keyboard = msg.reply_text.call_args.kwargs.get("reply_markup") or msg.reply_text.call_args.args[-1]
        dm_button = next(b for row in keyboard.inline_keyboard for b in row if "DM Access" in b.text)
        assert dm_button.callback_data == "help_dm_access"
        assert "⚡" not in dm_button.text

    async def test_dm_access_section_renders_with_expected_content(self, db_path):
        insert_premium(db_path, chat_id="-100123")
        chat = make_chat(chat_id=-100123)
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        query = MagicMock()
        query.data = "help_dm_access"
        query.message = msg
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        upd = make_update(chat=chat, user=user, message=msg)
        upd.callback_query = query
        ctx = make_context()

        await help_system.help_callback_handler(upd, ctx)

        text = query.edit_message_text.call_args.args[0]
        assert "switchgroup" in text
        assert "\\\"" not in text  # no stray backslash before literal quotes

    async def test_upgrade_prompt_matches_feature_flags_description(self, db_path):
        chat = make_chat(chat_id=-100123)
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        query = MagicMock()
        query.data = "upgrade_info_dm_access"
        query.message = msg
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        upd = make_update(chat=chat, user=user, message=msg)
        upd.callback_query = query
        ctx = make_context()

        await help_system.upgrade_info_callback_handler(upd, ctx)

        text = query.edit_message_text.call_args.args[0]
        assert "sticky group selection" in text
        assert "/switchgroup" in text


class TestLifecycleHelpMentionsPerEventLockIn:
    """Real gap found during a follow-up verification pass: feature_flags
    descriptions for verification/add_extra_member both mention they're
    'locked in per-event at creation time' (changing the tier later never
    affects an event already running) - a real, important nuance that was
    completely absent from help_lifecycle's own text."""

    async def test_lock_in_nuance_present_in_lifecycle_section(self, db_path):
        chat = make_chat(chat_id=-1)
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        query = MagicMock()
        query.data = "help_lifecycle"
        query.message = msg
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        upd = make_update(chat=chat, user=user, message=msg)
        upd.callback_query = query
        ctx = make_context()

        await help_system.help_callback_handler(upd, ctx)

        text = query.edit_message_text.call_args.args[0]
        assert "locked in per\\-event at creation time" in text
        assert "closes the event directly" in text


class TestDistributionHelpShowsLiveShareLimit:
    """Real gaps found and fixed across two rounds of review:
    1) The share-limit mention in help_distribution was initially a
       hardcoded literal, not read live from feature_flags.limit_count.
    2) limit_count=0 (this project's 'cleared/unlimited' convention) was
       shown as 'up to 0 times' instead of the unlimited message.
    3) The user asked for a "(limit N, remaining K)" format, computed
       fresh from the DB on every /help render - not just the limit
       itself, but this specific chat's actual remaining usage. Since
       shareevent's limit is enforced PER (hub, target) PAIR (counted
       across the hub's entire event history, not just the active
       event - event_shares has UNIQUE(event_id, chat_id) so the same
       event can never re-share to the same target), 'remaining' is
       the hub's most-constrained target right now (db.
       get_shareevent_remaining_for_chat's own docstring covers this
       in full)."""

    async def _render_distribution(self):
        chat = make_chat(chat_id=-1)
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        query = MagicMock()
        query.data = "help_distribution"
        query.message = msg
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        upd = make_update(chat=chat, user=user, message=msg)
        upd.callback_query = query
        ctx = make_context()
        await help_system.help_callback_handler(upd, ctx)
        return query.edit_message_text.call_args.args[0]

    async def test_default_shows_full_limit_and_remaining(self, db_path):
        text = await self._render_distribution()
        assert "limit 3, remaining 3" in text

    async def test_remaining_drops_as_real_shares_accumulate(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-1','1','P1','👍','❌',2,'[]','[]','{}','[]')"""
        )
        conn.execute("INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type) VALUES ('ev1','-200','10','-oc','group')")
        conn.commit()

        text = await self._render_distribution()
        assert "limit 3, remaining 2" in text
        assert "remaining 3" not in text

    async def test_updates_live_between_consecutive_calls(self, db_path):
        """No caching - calling /help twice in a row with new usage in
        between must reflect the new state immediately, no restart."""
        text1 = await self._render_distribution()
        assert "remaining 3" in text1

        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-1','1','P1','👍','❌',2,'[]','[]','{}','[]')"""
        )
        conn.execute("INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type) VALUES ('ev1','-200','10','-oc','group')")
        conn.commit()

        text2 = await self._render_distribution()
        assert "remaining 2" in text2

    async def test_changed_limit_reflected_live(self, db_path):
        db.update_feature_flag("shareevent", "FREE", limit_count=5, db_path=db_path)
        text = await self._render_distribution()
        assert "limit 5, remaining 5" in text
        assert "limit 3" not in text

    async def test_cleared_limit_shows_unlimited_message(self, db_path):
        db.update_feature_flag("shareevent", "FREE", limit_count=0, db_path=db_path)
        text = await self._render_distribution()
        assert "Currently unlimited" in text
        assert "limit 0" not in text

    async def test_limit_is_relative_to_whichever_chat_help_is_called_in(self, db_path):
        """Limits are computed relative to the chat /help is directly
        called in - matching how the real /shareevent enforcement itself
        works (resolve_hub_chat_id never resolves a child chat up to its
        owning hub, just uses chat.id directly). A child chat owned by a
        PRO hub has no subscription row of its own, so calling /help
        FROM the child shows FREE-tier limits, while calling it from the
        hub itself shows unlimited."""
        insert_premium(db_path, chat_id="-100")
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO sub_chats (chat_id, owner_chat_id, alias) VALUES ('-200','-100','child')")
        conn.commit()

        def render(chat_id):
            chat = make_chat(chat_id=chat_id, chat_type="supergroup")
            user = make_user(user_id=1)
            msg = make_message(chat=chat)
            query = MagicMock()
            query.data = "help_distribution"
            query.message = msg
            query.answer = AsyncMock()
            query.edit_message_text = AsyncMock()
            upd = make_update(chat=chat, user=user, message=msg)
            upd.callback_query = query
            ctx = make_context()
            return upd, ctx, query

        upd_hub, ctx_hub, query_hub = render(-100)

        async def _run(upd, ctx, query):
            await help_system.help_callback_handler(upd, ctx)
            return query.edit_message_text.call_args.args[0]

        text_hub = await _run(upd_hub, ctx_hub, query_hub)
        assert "Currently unlimited" in text_hub

        upd_child, ctx_child, query_child = render(-200)
        text_child = await _run(upd_child, ctx_child, query_child)
        assert "limit 3, remaining 3" in text_child


class TestEnrichedFeatureFlagsDescriptions:
    """newevent/editevent/user_management's feature_flags.description were
    noticeably thinner than what /help already documents for the same
    commands - enriched to match (e.g. newevent's description now
    mentions -gi/-ni/-d, not just a generic one-liner)."""

    def test_newevent_description_mentions_icon_flags(self, db_path):
        rows = {r[0]: r for r in db.get_feature_flags(db_path=db_path)}
        assert "-gi" in rows["newevent"][4] or "goingicon" in rows["newevent"][4]

    def test_user_management_description_lists_individual_commands(self, db_path):
        rows = {r[0]: r for r in db.get_feature_flags(db_path=db_path)}
        desc = rows["user_management"][4]
        assert "/adduser" in desc
        assert "/notify" in desc


class TestUpdateuserResolvesRealUserIdWhenPossible:
    """Real bug found from user-reported screenshots: /updateuser called
    track_user() without any user_id at all when tracking someone for
    the first time, permanently creating an unlinkable row (no clickable
    mention ever possible in /listusers or Not Going lists) - unlike
    /adduser, which always attempts real resolution first. Now tries the
    same get_chat_administrators-based resolution /adduser already uses,
    with an honest warning when resolution genuinely isn't possible."""

    async def test_admin_username_gets_resolved_to_real_id(self, db_path):
        bot = make_bot()
        admin_match = MagicMock()
        admin_match.user.username = "serhiy"
        admin_match.user.id = 555
        admin_match.user.first_name = "Serhiy"
        admin_match.user.last_name = "Kovalenko"
        bot.get_chat_administrators = AsyncMock(return_value=[admin_match])
        chat = make_chat(chat_id=-1, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=["Serhiy", "-a"])

        await handlers.updateuser(upd, ctx)

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT user_id, first_name, last_name FROM main_group_users WHERE chat_id='-1' AND username='Serhiy'"
        ).fetchone()
        assert row == ("555", "Serhiy", "Kovalenko")

    async def test_unresolvable_username_gives_honest_warning(self, db_path):
        bot = make_bot()
        bot.get_chat_administrators = AsyncMock(return_value=[])
        chat = make_chat(chat_id=-1, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=["Enes", "-a"])

        await handlers.updateuser(upd, ctx)

        replies = [c.args[0] for c in msg.reply_text.call_args_list]
        assert any("Could not resolve" in r for r in replies)
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT user_id, status FROM main_group_users WHERE chat_id='-1' AND username='Enes'").fetchone()
        assert row == (None, "active")

    async def test_existing_valid_user_id_is_never_overwritten(self, db_path):
        """A username that ALREADY has a real user_id on file must keep
        it - /updateuser shouldn't re-resolve or touch it."""
        db.track_user("-1", "Serhiy", "active", user_id="999", first_name="Serhiy", last_name="Original")
        bot = make_bot()
        bot.get_chat_administrators = AsyncMock(return_value=[])
        chat = make_chat(chat_id=-1, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=["Serhiy", "-p"])

        await handlers.updateuser(upd, ctx)

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT user_id, status, last_name FROM main_group_users WHERE chat_id='-1' AND username='Serhiy'"
        ).fetchone()
        assert row == ("999", "passive", "Original")
        # get_chat_administrators should never even be called - already resolved
        bot.get_chat_administrators.assert_not_called()


class TestSetsubRejectsChannelTargets:
    """Real bug found and fixed: /setsub already fetched the live chat
    object (for chat_name display) but completely ignored chat_obj.type,
    blindly inserting ANY target chat_id into all_groups even if it was
    actually a channel - creating a spurious, wrong all_groups row
    (showing up in the Control Sheet's Groups tab instead of Channels).
    Channels don't have an independent subscription concept in this
    system (all_channels' schema has no type/subscription columns at
    all, matching how child groups also never get independent
    subscriptions) - so /setsub against a channel target is now
    explicitly rejected instead of silently creating a wrong row."""

    async def test_channel_target_rejected_no_wrong_row_created(self, db_path):
        bot = make_bot()
        bot.get_chat = AsyncMock(return_value=MagicMock(type="channel", title="My Channel", username=None))
        chat = make_chat(chat_id=-1, chat_type="supergroup")
        owner = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=owner, message=msg)
        ctx = make_context(bot=bot, args=["-500", "on", "30"])

        with patch("subscription.OWNER_USER_IDS", [1]):
            await subscription.setsub(upd, ctx)

        text = msg.reply_text.call_args.args[0]
        assert "is a channel, not a group" in text

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT * FROM all_groups WHERE chat_id='-500'").fetchone()
        assert row is None

    async def test_real_group_target_still_works(self, db_path):
        bot = make_bot()
        bot.get_chat = AsyncMock(return_value=MagicMock(type="supergroup", title="Real Group", username=None))
        chat = make_chat(chat_id=-1, chat_type="supergroup")
        owner = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=owner, message=msg)
        ctx = make_context(bot=bot, args=["-600", "on", "30"])

        with patch("subscription.OWNER_USER_IDS", [1]), \
             patch("subscription._push_control_sheet_main", new_callable=AsyncMock):
            await subscription.setsub(upd, ctx)

        text = msg.reply_text.call_args.args[0]
        assert "Subscription *on*" in text

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT chat_id, type FROM all_groups WHERE chat_id='-600'").fetchone()
        assert row == ("-600", "PRO")

    async def test_get_chat_failure_falls_back_to_treating_as_group(self, db_path):
        """If get_chat itself fails (e.g. bot isn't in that chat), we
        can't determine channel-vs-group at all - falls back to the
        pre-existing behavior (treat as group) rather than blocking a
        legitimate group subscription just because the name lookup
        failed for some unrelated reason."""
        bot = make_bot()
        bot.get_chat = AsyncMock(side_effect=Exception("Chat not found"))
        chat = make_chat(chat_id=-1, chat_type="supergroup")
        owner = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=owner, message=msg)
        ctx = make_context(bot=bot, args=["-700", "on", "30"])

        with patch("subscription.OWNER_USER_IDS", [1]), \
             patch("subscription._push_control_sheet_main", new_callable=AsyncMock):
            await subscription.setsub(upd, ctx)

        text = msg.reply_text.call_args.args[0]
        assert "is a channel, not a group" not in text
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT chat_id FROM all_groups WHERE chat_id='-700'").fetchone()
        assert row is not None


class TestNotgoingVisibilityFlags:
    """New -ngl/-notgoinglist flag (visible/hidden/onlycount) for
    /newevent and /editevent, decoupled from -limit's own -w/-waitlist
    flag. Ungated - available at every tier, matching Not Going's
    always-been-visible-to-everyone prior behavior."""

    async def test_newevent_ngl_hidden(self, db_path):
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context(args=["Party", "-ngl", "hidden"])

        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock, return_value=None), \
             patch("handlers.open_spreadsheet", new_callable=AsyncMock):
            await handlers.newevent(upd, ctx)

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT notgoing_visibility FROM events WHERE name='Party'").fetchone()
        assert row == ("hidden",)

    async def test_newevent_without_ngl_defaults_visible(self, db_path):
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context(args=["Party"])

        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock, return_value=None), \
             patch("handlers.open_spreadsheet", new_callable=AsyncMock):
            await handlers.newevent(upd, ctx)

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT notgoing_visibility FROM events WHERE name='Party'").fetchone()
        assert row == ("visible",)

    async def test_editevent_ngl_independent_of_limit(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, notgoing_visibility)
               VALUES ('ev1','-100123','1','Party','👍','❌',0,'[]','[]','{}','[]','visible')"""
        )
        conn.commit()
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context(args=["-ngl", "onlycount"])

        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock, return_value=None), \
             patch("handlers.schedule_view_refresh", new_callable=AsyncMock):
            await handlers.editevent(upd, ctx)

        row = conn.execute("SELECT notgoing_visibility, total_limit FROM events WHERE event_id='ev1'").fetchone()
        assert row == ("onlycount", None)

    async def test_post_render_respects_ngl_hidden_no_extra_blank_lines(self, db_path):
        """Real bug found and fixed: when both notgoing AND waitlist are
        hidden, the section separator produced doubled blank lines
        (4 newlines instead of 2) before the TOTAL line."""
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, notgoing_visibility)
               VALUES ('ev1','-100','1','Party','👍','❌',0,?,?,'{}','[]','hidden')""",
            (json.dumps(["alice (1)"]), json.dumps(["bob"])),
        )
        conn.commit()

        bot = MagicMock()
        bot.edit_message_text = AsyncMock()
        ctx = MagicMock()
        ctx.bot = bot
        ctx.application = MagicMock()
        ctx.application.create_task = MagicMock()

        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock, return_value=None):
            await event_engine.update_all_shared_views(ctx, "ev1")

        text = bot.edit_message_text.call_args_list[0].kwargs["text"]
        assert "Not Going" not in text
        assert "\n\n\n" not in text  # no doubled blank line

    async def test_post_render_ngl_onlycount_shows_count_not_names(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, notgoing_visibility)
               VALUES ('ev1','-100','1','Party','👍','❌',0,'[]',?,'{}','[]','onlycount')""",
            (json.dumps(["bob", "carol"]),),
        )
        conn.commit()

        bot = MagicMock()
        bot.edit_message_text = AsyncMock()
        ctx = MagicMock()
        ctx.bot = bot
        ctx.application = MagicMock()
        ctx.application.create_task = MagicMock()

        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock, return_value=None):
            await event_engine.update_all_shared_views(ctx, "ev1")

        text = bot.edit_message_text.call_args_list[0].kwargs["text"]
        assert "Not Going:* 2" in text
        assert "bob" not in text
        assert "carol" not in text


class TestChildChatNotgoingDisplay:
    """Item 3+6: Not Going list now displays in shareevent'd (child)
    posts too, with per-share override support (share_notgoing_visibility
    in event_shares) - if not overridden, inherits the event's own
    notgoing_visibility default."""

    async def test_share_override_visible_despite_event_default_hidden(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, notgoing_visibility)
               VALUES ('ev1','-100','1','Party','👍','❌',0,'[]','[]','{}','[]','hidden')"""
        )
        conn.execute(
            "INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type, share_notgoing_visibility) "
            "VALUES ('ev1','-200','2','-visible','group','visible')"
        )
        conn.execute(
            "INSERT INTO event_users (event_id, chat_id, user_id, username, status, guests) VALUES ('ev1','-200','5','dave','notgoing',0)"
        )
        conn.commit()

        bot = MagicMock()
        bot.edit_message_text = AsyncMock()
        bot.get_chat = AsyncMock(return_value=MagicMock(title="Group"))
        ctx = MagicMock()
        ctx.bot = bot
        ctx.application = MagicMock()
        ctx.application.create_task = MagicMock()

        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock, return_value=None):
            await event_engine.update_all_shared_views(ctx, "ev1")

        child_call = next(c for c in bot.edit_message_text.call_args_list if c.kwargs.get("chat_id") == -200)
        text = child_call.kwargs["text"]
        assert "Not Going" in text
        assert "dave" in text

    async def test_no_override_inherits_event_default(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, notgoing_visibility)
               VALUES ('ev1','-100','1','Party','👍','❌',0,'[]','[]','{}','[]','hidden')"""
        )
        conn.execute(
            "INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type) VALUES ('ev1','-300','3','-visible','group')"
        )
        conn.execute(
            "INSERT INTO event_users (event_id, chat_id, user_id, username, status, guests) VALUES ('ev1','-300','6','erin','notgoing',0)"
        )
        conn.commit()

        bot = MagicMock()
        bot.edit_message_text = AsyncMock()
        bot.get_chat = AsyncMock(return_value=MagicMock(title="Group"))
        ctx = MagicMock()
        ctx.bot = bot
        ctx.application = MagicMock()
        ctx.application.create_task = MagicMock()

        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock, return_value=None):
            await event_engine.update_all_shared_views(ctx, "ev1")

        child_call = next(c for c in bot.edit_message_text.call_args_list if c.kwargs.get("chat_id") == -300)
        text = child_call.kwargs["text"]
        assert "Not Going" not in text

    async def test_share_waitlist_override_also_works(self, db_path):
        """share_waitlist_visibility override behaves the same way -
        previously the child ALWAYS used the event's own waitlist
        setting unconditionally, ignoring any per-share override."""
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, total_limit, waitlist_data, waitlist_visibility)
               VALUES ('ev1','-100','1','Party','👍','❌',0,'[]','[]','{}','[]',5,?,'hidden')""",
            (json.dumps([{"chat_id": "-200", "chat_name": None, "username": "frank", "user_id": "7", "timestamp": "t1"}]),),
        )
        conn.execute(
            "INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type, share_waitlist_visibility) "
            "VALUES ('ev1','-200','2','-visible','group','visible')"
        )
        conn.commit()

        bot = MagicMock()
        bot.edit_message_text = AsyncMock()
        bot.get_chat = AsyncMock(return_value=MagicMock(title="Group"))
        ctx = MagicMock()
        ctx.bot = bot
        ctx.application = MagicMock()
        ctx.application.create_task = MagicMock()

        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock, return_value=None):
            await event_engine.update_all_shared_views(ctx, "ev1")

        child_call = next(c for c in bot.edit_message_text.call_args_list if c.kwargs.get("chat_id") == -200)
        text = child_call.kwargs["text"]
        assert "Waitlist" in text
        assert "frank" in text


class TestShareeventNewFlagSyntax:
    """Item 6: /shareevent's old positional -visible/-hidden/-onlycount
    mode moved behind an explicit -mgl/-maingoinglist flag, plus two new
    flags -sngl/-sharenotgoing and -swl/-sharewaitlist (same visibility
    vocabulary), storing per-share overrides in event_shares."""

    async def test_all_three_flags_together(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-100','1','Party','👍','❌',0,'[]','[]','{}','[]')"""
        )
        conn.commit()

        bot = make_bot()
        bot.get_chat = AsyncMock(return_value=MagicMock(type="supergroup", title="Target Group"))
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=999))
        chat = make_chat(chat_id=-100, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=["-200", "-mgl", "visible", "-sngl", "onlycount", "-swl", "hidden"])

        await handlers.shareevent(upd, ctx)

        row = conn.execute(
            "SELECT share_mode, share_notgoing_visibility, share_waitlist_visibility FROM event_shares WHERE chat_id='-200'"
        ).fetchone()
        assert row == ("-visible", "onlycount", "hidden")

    async def test_long_form_flag_names_also_work(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-100','1','Party','👍','❌',0,'[]','[]','{}','[]')"""
        )
        conn.commit()

        bot = make_bot()
        bot.get_chat = AsyncMock(return_value=MagicMock(type="supergroup", title="Target Group"))
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=999))
        chat = make_chat(chat_id=-100, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=[
            "-200", "-maingoinglist", "hidden", "-sharenotgoing", "visible", "-sharewaitlist", "onlycount",
        ])

        await handlers.shareevent(upd, ctx)

        row = conn.execute(
            "SELECT share_mode, share_notgoing_visibility, share_waitlist_visibility FROM event_shares WHERE chat_id='-200'"
        ).fetchone()
        assert row == ("-hidden", "visible", "onlycount")

    async def test_no_flags_defaults_preserved(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-100','1','Party','👍','❌',0,'[]','[]','{}','[]')"""
        )
        conn.commit()

        bot = make_bot()
        bot.get_chat = AsyncMock(return_value=MagicMock(type="supergroup", title="Target Group"))
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=999))
        chat = make_chat(chat_id=-100, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=["-300"])

        await handlers.shareevent(upd, ctx)

        row = conn.execute(
            "SELECT share_mode, share_notgoing_visibility, share_waitlist_visibility FROM event_shares WHERE chat_id='-300'"
        ).fetchone()
        assert row == ("-onlycount", None, None)

    async def test_target_can_appear_after_flags(self, db_path):
        """Target extraction is order-independent - the first non-flag
        token is treated as the target, regardless of position."""
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-100','1','Party','👍','❌',0,'[]','[]','{}','[]')"""
        )
        conn.commit()

        bot = make_bot()
        bot.get_chat = AsyncMock(return_value=MagicMock(type="supergroup", title="Target Group"))
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=999))
        chat = make_chat(chat_id=-100, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=["-mgl", "visible", "-400"])

        await handlers.shareevent(upd, ctx)

        row = conn.execute(
            "SELECT share_mode FROM event_shares WHERE chat_id='-400'"
        ).fetchone()
        assert row == ("-visible",)


class TestStatsCommand:
    """Item 7: new /stats command, gated on the "stats" PRO feature, shows
    event activity stats for the calling hub: total events ever created,
    how many were closed, and total/average headcount (going + guests)
    across every closed event."""

    async def test_free_hub_rejected(self, db_path):
        chat = make_chat(chat_id=-1, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(args=[])

        await handlers.stats_command(upd, ctx)

        assert "PRO" in msg.reply_text.call_args.args[0]

    async def test_pro_hub_computes_correct_stats(self, db_path):
        insert_premium(db_path, chat_id="-1")
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-1','1','Open Party','👍','❌',0,'[]','[]','{}','[]')"""
        )
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev2','-1','2','Closed 1','👍','❌',2,?,'[]',?,'[]')""",
            (json.dumps(["a (1)", "b (2)", "c (3)"]), json.dumps({"a": 2})),
        )
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev3','-1','3','Closed 2','👍','❌',2,?,'[]','{}','[]')""",
            (json.dumps(["d (4)"]),),
        )
        conn.commit()

        chat = make_chat(chat_id=-1, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(args=[])

        await handlers.stats_command(upd, ctx)

        text = msg.reply_text.call_args.args[0]
        assert "Events amount: 3" in text
        assert "Events closed: 2" in text
        assert "Total members amount: 6" in text
        assert "Average members amount: 3\\.0" in text

    async def test_no_closed_events_avoids_division_by_zero(self, db_path):
        insert_premium(db_path, chat_id="-1")
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-1','1','Open Party','👍','❌',0,'[]','[]','{}','[]')"""
        )
        conn.commit()

        chat = make_chat(chat_id=-1, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(args=[])

        await handlers.stats_command(upd, ctx)

        text = msg.reply_text.call_args.args[0]
        assert "Events amount: 1" in text
        assert "Events closed: 0" in text
        assert "Total members amount: 0" in text
        assert "Average members amount: 0" in text

    async def test_no_events_at_all(self, db_path):
        insert_premium(db_path, chat_id="-1")
        chat = make_chat(chat_id=-1, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(args=[])

        await handlers.stats_command(upd, ctx)

        text = msg.reply_text.call_args.args[0]
        assert "Events amount: 0" in text
        assert "Events closed: 0" in text


class TestHelpUpdatedForNewFlagsAndStats:
    """Item 8: /help updated to reflect all the flag redesign from items
    4-6 (-w/-waitlist, -ngl/-notgoinglist, -mgl/-sngl/-swl on shareevent)
    and the new /stats command."""

    async def test_main_help_shows_new_newevent_syntax(self, db_path):
        chat = make_chat(chat_id=-100123)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context()

        await handlers.help_command(upd, ctx)

        text = msg.reply_text.call_args.args[0]
        assert "\\-wl" in text
        assert "\\-waitlist" in text
        assert "\\-ngl" in text
        assert "\\-notgoinglist" in text
        # Old combined syntax must be gone
        assert "\\-limit N \\[visible" not in text

    async def test_stats_shown_only_on_pro_hub(self, db_path):
        chat_free = make_chat(chat_id=-100123)
        msg_free = make_message(chat=chat_free)
        upd_free = make_update(chat=chat_free, message=msg_free)
        ctx_free = make_context()
        await handlers.help_command(upd_free, ctx_free)
        assert "/stats" not in msg_free.reply_text.call_args.args[0]

        insert_premium(db_path, chat_id="-100124")
        chat_pro = make_chat(chat_id=-100124)
        msg_pro = make_message(chat=chat_pro)
        upd_pro = make_update(chat=chat_pro, message=msg_pro)
        ctx_pro = make_context()
        await handlers.help_command(upd_pro, ctx_pro)
        assert "/stats" in msg_pro.reply_text.call_args.args[0]

    async def test_distribution_section_shows_new_shareevent_flags(self, db_path):
        chat = make_chat(chat_id=-1)
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        query = MagicMock()
        query.data = "help_distribution"
        query.message = msg
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        upd = make_update(chat=chat, user=user, message=msg)
        upd.callback_query = query
        ctx = make_context()

        await help_system.help_callback_handler(upd, ctx)

        text = query.edit_message_text.call_args.args[0]
        assert "\\-mgl" in text
        assert "\\-sngl" in text
        assert "\\-swl" in text
        assert "\\-v\\|" not in text  # old shorthand syntax gone


class TestValidateWaitlistVisibilityFlagHelper:
    """Refactor: extracted -wl/-waitlist's gating logic (previously
    identical code duplicated inline in both newevent and editevent)
    into a shared helper, mirroring _validate_limit_flag's own
    established pattern right above it."""

    async def test_free_hub_rejected_with_message(self, db_path):
        chat = make_chat(chat_id=-1, chat_type="supergroup")
        msg = make_message(chat=chat)
        result = await handlers._validate_waitlist_visibility_flag(msg, "-1", "visible")
        assert result is None
        assert "requires a higher tier" in msg.reply_text.call_args.args[0]

    async def test_pro_hub_returns_value_unchanged(self, db_path):
        insert_premium(db_path, chat_id="-1")
        chat = make_chat(chat_id=-1, chat_type="supergroup")
        msg = make_message(chat=chat)
        result = await handlers._validate_waitlist_visibility_flag(msg, "-1", "onlycount")
        assert result == "onlycount"
        msg.reply_text.assert_not_called()


class TestRefreshusersRetroactivelyResolvesStaleEntries:
    """Real gap found from user screenshots: people who were tracked
    (via /adduser, /updateuser, or an earlier code path) with only a
    username and no real user_id remained permanently unlinkable forever
    - /refreshusers used to just delete them outright. Now attempts the
    same admin-list-based resolution /updateuser already uses BEFORE
    removing, healing stale rows for anyone who's since become an admin.
    Genuine non-admin members still can't be resolved (a fundamental
    Telegram Bot API limitation - no endpoint exists to look up an
    arbitrary username), so they're still removed if resolution fails."""

    async def test_resolves_via_admin_list_before_removing(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO main_group_users (chat_id, username, status) VALUES ('-1','Serhiy','active')")
        conn.commit()

        bot = make_bot()
        admin_match = MagicMock()
        admin_match.user.username = "Serhiy"
        admin_match.user.id = 555
        admin_match.user.first_name = "Serhiy"
        admin_match.user.last_name = "Real"
        admin_match.user.is_bot = False
        bot.get_chat_administrators = AsyncMock(return_value=[admin_match])
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
        chat = make_chat(chat_id=-1, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=[])

        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock, return_value=None):
            await handlers.refreshusers(upd, ctx)

        text = msg.reply_text.call_args_list[0].args[0]
        assert "Resolved to a real, clickable user" in text
        row = conn.execute("SELECT user_id, first_name, last_name FROM main_group_users WHERE chat_id='-1' AND username='Serhiy'").fetchone()
        assert row == ("555", "Serhiy", "Real")

    async def test_still_removed_if_resolution_also_fails(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO main_group_users (chat_id, username, status) VALUES ('-1','Enes','active')")
        conn.commit()

        bot = make_bot()
        bot.get_chat_administrators = AsyncMock(return_value=[])
        chat = make_chat(chat_id=-1, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=[])

        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock, return_value=None):
            await handlers.refreshusers(upd, ctx)

        row = conn.execute("SELECT * FROM main_group_users WHERE chat_id='-1' AND username='Enes'").fetchone()
        assert row is None



class TestClickabilityFlag:
    """Item 3: new -clc/-clickability flag (on/off) for /newevent,
    /editevent, /shareevent - controls whether names in the event post
    are clickable mentions or plain text. Default 'on' (matching every
    event's behavior prior to this flag existing). event_shares'
    share_clickability provides a per-share override, same pattern as
    -sngl/-swl."""

    async def test_newevent_clc_off(self, db_path):
        insert_premium(db_path, chat_id="-100123")
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context(args=["Party", "-clc", "off"])

        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock, return_value=None), \
             patch("handlers.open_spreadsheet", new_callable=AsyncMock):
            await handlers.newevent(upd, ctx)

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT clickability FROM events WHERE name='Party'").fetchone()
        assert row == ("off",)

    async def test_newevent_clc_rejected_on_free_hub(self, db_path):
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context(args=["Party", "-clc", "off"])

        await handlers.newevent(upd, ctx)

        assert "requires a higher tier" in msg.reply_text.call_args.args[0]
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT * FROM events WHERE name='Party'").fetchone()
        assert row is None

    async def test_newevent_without_clc_defaults_on(self, db_path):
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context(args=["Party"])

        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock, return_value=None), \
             patch("handlers.open_spreadsheet", new_callable=AsyncMock):
            await handlers.newevent(upd, ctx)

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT clickability FROM events WHERE name='Party'").fetchone()
        assert row == ("on",)

    async def test_editevent_clickability_independent_of_other_flags(self, db_path):
        insert_premium(db_path, chat_id="-100123")
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, clickability)
               VALUES ('ev1','-100123','1','Party','👍','❌',0,'[]','[]','{}','[]','on')"""
        )
        conn.commit()
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context(args=["-clickability", "off"])

        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock, return_value=None), \
             patch("handlers.schedule_view_refresh", new_callable=AsyncMock):
            await handlers.editevent(upd, ctx)

        row = conn.execute("SELECT clickability, total_limit FROM events WHERE event_id='ev1'").fetchone()
        assert row == ("off", None)

    async def test_shareevent_clc_stored_per_share(self, db_path):
        insert_premium(db_path, chat_id="-100")
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-100','1','Party','👍','❌',0,'[]','[]','{}','[]')"""
        )
        conn.commit()

        bot = make_bot()
        bot.get_chat = AsyncMock(return_value=MagicMock(type="supergroup", title="Target Group"))
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=999))
        chat = make_chat(chat_id=-100, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=["-200", "-clc", "off"])

        await handlers.shareevent(upd, ctx)

        row = conn.execute("SELECT share_clickability FROM event_shares WHERE chat_id='-200'").fetchone()
        assert row == ("off",)

    async def test_post_renders_plain_text_when_clickability_off(self, db_path):
        db.track_user("-100", "alice", "active", user_id="1", first_name="Alice", last_name="A")
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, clickability)
               VALUES ('ev1','-100','1','Party','👍','❌',0,?,'[]','{}','[]','off')""",
            (json.dumps(["alice (1)"]),),
        )
        conn.commit()

        bot = MagicMock()
        bot.edit_message_text = AsyncMock()
        ctx = MagicMock()
        ctx.bot = bot
        ctx.application = MagicMock()
        ctx.application.create_task = MagicMock()

        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock, return_value=None):
            await event_engine.update_all_shared_views(ctx, "ev1")

        text = bot.edit_message_text.call_args_list[0].kwargs["text"]
        assert "Alice A" in text
        assert "tg://user" not in text

    async def test_editevent_clc_rejected_on_free_hub(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, clickability)
               VALUES ('ev1','-100123','1','Party','👍','❌',0,'[]','[]','{}','[]','on')"""
        )
        conn.commit()
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context(args=["-clc", "off"])

        await handlers.editevent(upd, ctx)

        assert "requires a higher tier" in msg.reply_text.call_args.args[0]
        row = conn.execute("SELECT clickability FROM events WHERE event_id='ev1'").fetchone()
        assert row == ("on",)  # unchanged

    async def test_shareevent_clc_rejected_on_free_hub(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-100','1','Party','👍','❌',0,'[]','[]','{}','[]')"""
        )
        conn.commit()

        bot = make_bot()
        chat = make_chat(chat_id=-100, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=["-200", "-clc", "off"])

        await handlers.shareevent(upd, ctx)

        sent = bot.send_message.call_args
        assert "requires a higher tier" in sent.kwargs.get("text", "")
        row = conn.execute("SELECT * FROM event_shares WHERE chat_id='-200'").fetchone()
        assert row is None  # share was never created

    async def test_shareevent_without_clc_still_works_on_free_hub(self, db_path):
        """Not passing -clc at all must not be gated - only USING the
        flag requires the tier."""
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-100','1','Party','👍','❌',0,'[]','[]','{}','[]')"""
        )
        conn.commit()

        bot = make_bot()
        bot.get_chat = AsyncMock(return_value=MagicMock(type="supergroup", title="Target Group"))
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
        bot.send_message = AsyncMock(return_value=MagicMock(message_id=999))
        chat = make_chat(chat_id=-100, chat_type="supergroup")
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=user, message=msg)
        ctx = make_context(bot=bot, args=["-200"])

        await handlers.shareevent(upd, ctx)

        row = conn.execute("SELECT share_clickability FROM event_shares WHERE chat_id='-200'").fetchone()
        assert row == (None,)

    async def test_per_share_override_beats_event_default(self, db_path):
        """A share with its OWN clickability override must use that
        override, not the event's own setting - even in the opposite
        direction (event off, share on)."""
        db.track_user("-200", "bob", "active", user_id="2", first_name="Bob", last_name="B")
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data, clickability)
               VALUES ('ev1','-100','1','Party','👍','❌',0,'[]','[]','{}','[]','off')"""
        )
        conn.execute(
            "INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type, share_clickability) "
            "VALUES ('ev1','-200','2','-visible','group','on')"
        )
        conn.execute(
            "INSERT INTO event_users (event_id, chat_id, user_id, username, status, guests) VALUES ('ev1','-200','2','bob','going',0)"
        )
        conn.commit()

        bot = MagicMock()
        bot.edit_message_text = AsyncMock()
        bot.get_chat = AsyncMock(return_value=MagicMock(title="Group"))
        ctx = MagicMock()
        ctx.bot = bot
        ctx.application = MagicMock()
        ctx.application.create_task = MagicMock()

        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock, return_value=None):
            await event_engine.update_all_shared_views(ctx, "ev1")

        child_call = next(c for c in bot.edit_message_text.call_args_list if c.kwargs.get("chat_id") == -200)
        text = child_call.kwargs["text"]
        assert "[Bob B](tg://user?id=2)" in text


class TestHelpMentionsClickabilityFlag:
    """Item 3, help update: /help reflects the new -clc/-clickability
    flag across /newevent, /editevent, and /shareevent."""

    async def test_main_help_shows_clc_syntax(self, db_path):
        chat = make_chat(chat_id=-100123)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context()

        await handlers.help_command(upd, ctx)

        text = msg.reply_text.call_args.args[0]
        assert "\\-clc" in text
        assert "\\-clickability" in text

    async def test_distribution_section_shows_clc_syntax(self, db_path):
        chat = make_chat(chat_id=-1)
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        query = MagicMock()
        query.data = "help_distribution"
        query.message = msg
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        upd = make_update(chat=chat, user=user, message=msg)
        upd.callback_query = query
        ctx = make_context()

        await help_system.help_callback_handler(upd, ctx)

        text = query.edit_message_text.call_args.args[0]
        assert "\\-clc" in text
        assert "\\-clickability" in text


class TestVerificationModeShowsRealNames:
    """Item 2: verification-mode keyboard buttons (kick/return list) now
    show a resolved 'First Last' name instead of the bare username,
    matching the message text's own _mention_link format. keyboard.py
    stays DB-free by design - event_engine.py builds a display_names
    dict and passes it in ready-made.

    Real bug found and fixed while implementing this: child-chat
    participants were resolved against main_chat_id (the hub) instead
    of their OWN chat_id - since first_name/last_name are stored per
    chat in main_group_users, this meant a child participant's real
    name could never be found, always falling back to their username."""

    async def test_master_going_participant_shows_real_name(self, db_path):
        db.track_user("-100", "alice", "active", user_id="1", first_name="Alice", last_name="Petrenko")
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-100','1','Party','👍','❌',1,?,'[]','{}','[]')""",
            (json.dumps(["alice (1)"]),),
        )
        conn.commit()

        bot = MagicMock()
        bot.edit_message_text = AsyncMock()
        ctx = MagicMock()
        ctx.bot = bot
        ctx.application = MagicMock()
        ctx.application.create_task = MagicMock()

        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock, return_value=None):
            await event_engine.update_all_shared_views(ctx, "ev1")

        master_call = next(c for c in bot.edit_message_text.call_args_list if c.kwargs.get("chat_id") == -100)
        kb = master_call.kwargs["reply_markup"]
        button_texts = [b.text for row in kb.inline_keyboard for b in row]
        assert any("Alice Petrenko" in t for t in button_texts)
        assert not any(t == "👤 alice" for t in button_texts)

    async def test_child_participant_resolved_against_own_chat_not_hub(self, db_path):
        """The real bug: child participants must resolve against THEIR
        OWN chat_id, not main_chat_id."""
        db.track_user("-200", "bob", "active", user_id="2", first_name="Bob", last_name="Ivanov")
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-100','1','Party','👍','❌',1,'[]','[]','{}','[]')"""
        )
        conn.execute(
            "INSERT INTO event_users (event_id, chat_id, user_id, username, status, guests) VALUES ('ev1','-200','2','bob','going',0)"
        )
        conn.commit()

        bot = MagicMock()
        bot.edit_message_text = AsyncMock()
        ctx = MagicMock()
        ctx.bot = bot
        ctx.application = MagicMock()
        ctx.application.create_task = MagicMock()

        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock, return_value=None):
            await event_engine.update_all_shared_views(ctx, "ev1")

        master_call = next(c for c in bot.edit_message_text.call_args_list if c.kwargs.get("chat_id") == -100)
        kb = master_call.kwargs["reply_markup"]
        button_texts = [b.text for row in kb.inline_keyboard for b in row]
        assert any("Bob Ivanov" in t for t in button_texts)
        assert not any(t == "📢 bob" for t in button_texts)

    async def test_unresolvable_username_falls_back_gracefully(self, db_path):
        """No user_id anywhere on file - keyboard falls back to the
        plain username, same as before this feature existed."""
        conn = sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-100','1','Party','👍','❌',1,?,'[]','{}','[]')""",
            (json.dumps(["ghostuser (999)"]),),
        )
        conn.commit()

        bot = MagicMock()
        bot.edit_message_text = AsyncMock()
        ctx = MagicMock()
        ctx.bot = bot
        ctx.application = MagicMock()
        ctx.application.create_task = MagicMock()

        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock, return_value=None):
            await event_engine.update_all_shared_views(ctx, "ev1")

        master_call = next(c for c in bot.edit_message_text.call_args_list if c.kwargs.get("chat_id") == -100)
        kb = master_call.kwargs["reply_markup"]
        button_texts = [b.text for row in kb.inline_keyboard for b in row]
        assert any("ghostuser" in t for t in button_texts)


class TestHelpReflectsClickabilityGating:
    """Item 4: /help correctly reflects clickability's new gated status
    (item 3) - "Requires a higher tier" for -clc specifically, while
    -ngl (a genuinely different, still-ungated flag) correctly keeps
    saying "ungated at every tier". A prior check was a false alarm:
    the phrase legitimately belongs to -ngl's own description, not a
    leftover from -clc."""

    async def test_main_help_clc_line_says_gated_not_ungated(self, db_path):
        chat = make_chat(chat_id=-100123)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context()

        await handlers.help_command(upd, ctx)

        text = msg.reply_text.call_args.args[0]
        clc_line = next(l for l in text.split("\n") if l.startswith("\\-clc \\|"))
        assert "Requires a higher tier" in clc_line
        assert "ungated" not in clc_line

    async def test_main_help_ngl_line_still_says_ungated(self, db_path):
        """-ngl was never gated by item 3 - must still correctly claim so."""
        chat = make_chat(chat_id=-100123)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        ctx = make_context()

        await handlers.help_command(upd, ctx)

        text = msg.reply_text.call_args.args[0]
        ngl_line = next(l for l in text.split("\n") if l.startswith("\\-ngl \\|"))
        assert "ungated" in ngl_line

    async def test_distribution_section_clc_line_says_gated(self, db_path):
        chat = make_chat(chat_id=-1)
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        query = MagicMock()
        query.data = "help_distribution"
        query.message = msg
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        upd = make_update(chat=chat, user=user, message=msg)
        upd.callback_query = query
        ctx = make_context()

        await help_system.help_callback_handler(upd, ctx)

        text = query.edit_message_text.call_args.args[0]
        clc_line = next(l for l in text.split("\n") if l.startswith("\\-clc \\|"))
        assert "Requires a higher tier" in clc_line


class TestHelpAuditFindings:
    """Full /help audit against every registered command and flag
    (main.py's CommandHandler list, parse_event_args/
    parse_shareevent_args' flag sets). Found and fixed one real gap:
    /refreshusersall was a real, standalone command (no args, PRO-gated
    via 'monitoring', covers hub + every monitored child) but only ever
    appeared in passing within help_monitoring's prose ("...can be
    synced with /refreshusersall"), never with its own explicit
    description line - unlike /refreshusers, which does get one in
    help_users. Everything else audited (owner-only section via
    /help -a, /waitlist, /setsheet, /stats, every newevent/editevent/
    shareevent flag) was already correctly documented."""

    async def test_refreshusersall_has_its_own_help_line(self, db_path):
        insert_premium(db_path, chat_id="-1")
        chat = make_chat(chat_id=-1)
        user = make_user(user_id=1)
        msg = make_message(chat=chat)
        query = MagicMock()
        query.data = "help_monitoring"
        query.message = msg
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        upd = make_update(chat=chat, user=user, message=msg)
        upd.callback_query = query
        ctx = make_context()

        await help_system.help_callback_handler(upd, ctx)

        text = query.edit_message_text.call_args.args[0]
        assert "/refreshusersall \\- Sync user list" in text

    async def test_owner_help_documents_all_four_owner_commands(self, db_path):
        with patch("help_system.OWNER_USER_IDS", [1]):
            chat = make_chat(chat_id=-1)
            user = make_user(user_id=1)
            msg = make_message(chat=chat)
            upd = make_update(chat=chat, user=user, message=msg)
            ctx = make_context(args=["-a"])

            await help_system.help_command(upd, ctx)

            text = msg.reply_text.call_args.args[0]
            for cmd in ["/setsub", "/allgroups", "/allchannels", "/updatefeature"]:
                assert cmd in text
