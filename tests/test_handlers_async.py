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
        "INSERT INTO main_chat_settings (chat_id, type, subs_date_start, subs_date_end) VALUES (?, 'premium', ?, ?)",
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
        must be the OPEN state (Going/Not Going/ADD/Remove/Verification Mode/
        Cancel Event) - NOT the verification-mode-only keyboard (Add Extra
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
        assert any("Verification Mode" in t for t in texts), "Verification Mode button missing"
        assert any("Cancel Event" in t for t in texts), "Cancel Event button missing"

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
        conn.execute("INSERT INTO main_group_users VALUES ('-100123','alice',NULL,'active')")
        conn.execute("INSERT INTO main_group_users VALUES ('-100123','bob',  NULL,'active')")
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
        conn.execute("INSERT INTO main_group_users VALUES ('-100123','alice',NULL,'active')")
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


# ── /updateuser ───────────────────────────────────────────────────────────────

class TestUpdateuser:
    """Verifies new -a/-active/-p/-passive flag syntax."""

    async def _run(self, db_path, args):
        """Helper: insert a known user then run /updateuser with given args."""
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO main_group_users VALUES ('-100123','alice',NULL,'active')")
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
        conn.execute("INSERT INTO main_group_users VALUES ('-100123','alice',NULL,'active')")
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
        conn.execute("INSERT INTO main_group_users VALUES ('-100123','alice',NULL,'active')")
        conn.execute("INSERT INTO main_group_users VALUES ('-100123','bob',  NULL,'passive')")
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
            "INSERT INTO sub_groups (chat_id, alias) VALUES (?, ?)", (chat_id, alias)
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
        cursor.execute("SELECT * FROM sub_groups WHERE alias = 'myalias'")
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
        ctx.args = ["-200", "-v"]

        await handlers.shareevent(upd, ctx)

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT share_mode FROM event_shares WHERE event_id='ev1' AND chat_id='-200'")
        row = cursor.fetchone()
        conn.close()
        assert row == ("-visible",)

    async def test_free_tier_blocks_after_3_shares_to_same_target(self, db_path):
        """
        Free-tier hubs may share up to FREE_SHAREEVENT_LIMIT_PER_TARGET (3)
        DISTINCT events to the same target chat - the 4th must be rejected
        with the exact requested message.
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
        assert sent_text == "You used all free limit for /shareevent, move to premium subscription for more"

    async def test_premium_tier_has_no_shareevent_limit(self, db_path):
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
        assert count == 4, "premium hubs must not be limited"

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
        assert "premium" in reply.lower()

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
        cursor.execute("SELECT COUNT(*) FROM sub_groups")
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
        cursor.execute("SELECT alias FROM sub_groups WHERE chat_id='-200'")
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
        cursor.execute("SELECT is_monitored FROM sub_groups WHERE chat_id='-200'")
        row = cursor.fetchone()
        conn.close()
        assert row == (1,)


# ── is_premium() ────────────────────────────────────────────────────────────

class TestIsPremium:

    def test_no_row_is_not_premium(self, db_path):
        assert handlers.is_premium("-999") is False

    def test_free_type_is_not_premium(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO main_chat_settings (chat_id, type) VALUES ('-100','free')")
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
        conn.execute("INSERT INTO main_chat_settings (chat_id, type) VALUES ('-100','premium')")
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

            await handlers.setsub(upd, ctx)

            msg.reply_text.assert_not_awaited()

    async def test_owner_can_turn_on(self, db_path):
        with patch("subscription.OWNER_USER_IDS", {self.OWNER_ID}), \
             patch("subscription.sync_control_sheet_main", new_callable=AsyncMock):
            chat = make_chat()
            user = make_user(user_id=self.OWNER_ID)
            msg  = make_message(chat=chat)
            upd  = make_update(chat=chat, user=user, message=msg)
            ctx  = make_context(args=["-100", "on", "30"])
            ctx.bot.get_chat = AsyncMock(return_value=MagicMock(title="Some Group", username=None))

            await handlers.setsub(upd, ctx)

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

            await handlers.setsub(upd, ctx)

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

            await handlers.setsub(upd, ctx)

            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT subs_date_end FROM main_chat_settings WHERE chat_id='-100'")
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
        assert "(premium)" in alias_btn.text
        assert "(premium)" in monitor_btn.text
        assert alias_btn.callback_data == "noop"
        assert monitor_btn.callback_data == "noop"

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
        assert "(premium)" not in alias_btn.text
        assert "(premium)" not in monitor_btn.text
        assert alias_btn.callback_data == "help_alias"
        assert monitor_btn.callback_data == "help_monitoring"

    async def test_distribution_and_lifecycle_buttons_always_active(self, db_path):
        """These two sections are free for everyone, regardless of tier."""
        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context()

        await handlers.help_command(upd, ctx)

        keyboard = msg.reply_text.call_args.kwargs.get("reply_markup") or msg.reply_text.call_args.args[-1]
        flat = [btn for row in keyboard.inline_keyboard for btn in row]
        dist_btn = next(b for b in flat if "Distribution" in b.text)
        life_btn = next(b for b in flat if "Lifecycle" in b.text)
        assert dist_btn.callback_data == "help_distribution"
        assert life_btn.callback_data == "help_lifecycle"


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
            "INSERT INTO event_shares VALUES (NULL,'ev1','-200','42','-visible','group')"
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
        conn.execute("INSERT INTO main_group_users VALUES ('-100123','alice','111','active')")
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
        conn.execute("INSERT INTO main_group_users VALUES ('-100123','bob','222','active')")
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
        conn.execute("INSERT INTO main_group_users VALUES ('-100123','carol','333','active')")
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

    async def test_users_without_id_are_skipped_not_removed(self, db_path):
        # Insert a user without a user_id
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO main_group_users VALUES ('-100123','bob',NULL,'active')")
        conn.commit()
        conn.close()

        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))

        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(bot=bot)

        await handlers.refreshusers(upd, ctx)

        # bob has no stored user_id, so he can't be membership-checked -
        # he must be left alone (not removed), and the reply should call
        # out that some users couldn't be verified.
        users = get_users(db_path)
        assert dict(users)["bob"] == "active"
        msg.reply_text.assert_awaited_once()
        reply = msg.reply_text.call_args.args[0]
        assert "⚠️" in reply

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
        conn.execute("INSERT INTO main_group_users VALUES ('-100123','existingadmin','555','passive')")
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

    async def test_without_root_flag_does_not_touch_google_sheets(self, db_path):
        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))

        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(bot=bot, args=[])

        sync_mock = AsyncMock()
        with patch("handlers.sync_users_sheet", sync_mock):
            await handlers.refreshusers(upd, ctx)

        sync_mock.assert_not_awaited()
        reply = msg.reply_text.call_args.args[0]
        assert "Users tab" not in reply

    async def test_root_flag_appends_new_member_to_users_sheet(self, db_path):
        """
        -r/-root: a chat administrator not yet present in the Users tab must
        get a new row: USER_ID, USER_NAME, PLACE_ID, STATUS="Member", "".
        """
        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
        admin_user = make_user(user_id=555, username="newadmin", first_name="New")
        admin_user.is_bot = False
        bot.get_chat_administrators = AsyncMock(return_value=[MagicMock(user=admin_user)])

        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(bot=bot, args=["-r"])

        fake_ss = FakeSpreadsheet()
        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("handlers.open_spreadsheet", new_callable=AsyncMock, return_value=fake_ss):
            await handlers.refreshusers(upd, ctx)

        ws = fake_ss.worksheets["Users"]
        assert ws.appended_rows == [["555", "newadmin", "-100123", "Member", ""]]
        reply = msg.reply_text.call_args.args[0]
        assert "Users tab" in reply

    async def test_root_long_form_flag_also_works(self, db_path):
        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
        admin_user = make_user(user_id=555, username="newadmin", first_name="New")
        admin_user.is_bot = False
        bot.get_chat_administrators = AsyncMock(return_value=[MagicMock(user=admin_user)])

        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(bot=bot, args=["-root"])

        fake_ss = FakeSpreadsheet()
        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("handlers.open_spreadsheet", new_callable=AsyncMock, return_value=fake_ss):
            await handlers.refreshusers(upd, ctx)

        ws = fake_ss.worksheets["Users"]
        assert ws.appended_rows == [["555", "newadmin", "-100123", "Member", ""]]

    async def test_root_flag_archives_changed_username(self, db_path):
        """A changed USER_NAME must be archived (comma-joined) and updated, not overwritten silently."""
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO main_group_users VALUES ('-100123','oldname','111','active')")
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
        ctx  = make_context(bot=bot, args=["-r"])

        fake_ss = FakeSpreadsheet()
        ws = FakeWorksheet()
        ws.records = [{"USER_ID": "111", "USER_NAME": "oldname", "PLACE_ID": "-100123",
                       "STATUS": "Member", "ARCHIVED USER_NAME": ""}]
        fake_ss.worksheets["Users"] = ws

        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("handlers.open_spreadsheet", new_callable=AsyncMock, return_value=fake_ss):
            await handlers.refreshusers(upd, ctx)

        assert ws.cell_updates["B2"] == [["newname"]]
        assert ws.cell_updates["E2"] == [["oldname"]]

    async def test_root_flag_appends_further_archived_names_with_comma(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO main_group_users VALUES ('-100123','secondname','111','active')")
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
        ctx  = make_context(bot=bot, args=["-r"])

        fake_ss = FakeSpreadsheet()
        ws = FakeWorksheet()
        ws.records = [{"USER_ID": "111", "USER_NAME": "secondname", "PLACE_ID": "-100123",
                       "STATUS": "Member", "ARCHIVED USER_NAME": "firstname"}]
        fake_ss.worksheets["Users"] = ws

        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("handlers.open_spreadsheet", new_callable=AsyncMock, return_value=fake_ss):
            await handlers.refreshusers(upd, ctx)

        assert ws.cell_updates["E2"] == [["firstname,secondname"]]

    async def test_root_flag_marks_departed_user_as_left_in_sheet(self, db_path):
        """A Users-sheet row for this PLACE_ID whose person is confirmed gone must get STATUS=Left."""
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO main_group_users VALUES ('-100123','gone','222','active')")
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
        ctx  = make_context(bot=bot, args=["-r"])

        fake_ss = FakeSpreadsheet()
        ws = FakeWorksheet()
        ws.records = [{"USER_ID": "222", "USER_NAME": "gone", "PLACE_ID": "-100123",
                       "STATUS": "Member", "ARCHIVED USER_NAME": ""}]
        fake_ss.worksheets["Users"] = ws

        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("handlers.open_spreadsheet", new_callable=AsyncMock, return_value=fake_ss):
            await handlers.refreshusers(upd, ctx)

        assert ws.cell_updates["D2"] == [["Left"]]

    async def test_root_flag_does_not_touch_rows_from_other_places(self, db_path):
        """A Users-sheet row belonging to a DIFFERENT PLACE_ID must never be touched by this chat's refresh."""
        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
        bot.get_chat_administrators = AsyncMock(return_value=[])

        chat = make_chat(chat_id=-100123)
        msg  = make_message(chat=chat)
        upd  = make_update(chat=chat, message=msg)
        ctx  = make_context(bot=bot, args=["-r"])

        fake_ss = FakeSpreadsheet()
        ws = FakeWorksheet()
        ws.records = [{"USER_ID": "999", "USER_NAME": "elsewhere", "PLACE_ID": "-999999",
                       "STATUS": "Member", "ARCHIVED USER_NAME": ""}]
        fake_ss.worksheets["Users"] = ws

        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("handlers.open_spreadsheet", new_callable=AsyncMock, return_value=fake_ss):
            await handlers.refreshusers(upd, ctx)

        assert ws.cell_updates == {}
        assert ws.appended_rows == []


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

        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("handlers.open_spreadsheet",   new_callable=AsyncMock):
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

        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("handlers.open_spreadsheet",   new_callable=AsyncMock):
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

        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("handlers.open_spreadsheet",   new_callable=AsyncMock):
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

        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("handlers.open_spreadsheet",   new_callable=AsyncMock):
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

        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("handlers.open_spreadsheet",   new_callable=AsyncMock):
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

        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("handlers.open_spreadsheet",   new_callable=AsyncMock):
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

        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("handlers.open_spreadsheet",   new_callable=AsyncMock):
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
        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("handlers.open_spreadsheet",   new_callable=AsyncMock):
            await handlers.button_handler(add_upd, ctx)

        notgoing_upd = make_callback_update("notgoing_ev1", chat_id=-200, user=user)
        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("handlers.open_spreadsheet",   new_callable=AsyncMock):
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

        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("handlers.open_spreadsheet",   new_callable=AsyncMock):
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

        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("handlers.open_spreadsheet",   new_callable=AsyncMock):
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
        conn.execute("INSERT INTO event_shares VALUES (NULL,'ev1','-200','42','-visible','group')")
        conn.commit()
        conn.close()
        insert_event_user(db_path, event_id="ev1", chat_id="-200", user_id="9",
                           username="onlyguests", status="", guests=2)

        bot = make_bot()
        ctx = make_context(bot=bot)

        await handlers.update_all_shared_views(ctx, "ev1")

        child_calls = [c for c in bot.edit_message_text.call_args_list if c.kwargs.get("chat_id") == -200]
        text = child_calls[0].kwargs.get("text", "")
        assert "2, from: onlyguests" in text
        assert "• onlyguests\n" not in text, "a guest-only registrant must not get their own 'going' name bullet"


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
        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("handlers.open_spreadsheet", new_callable=AsyncMock, return_value=fake_ss), \
             patch("handlers.sync_event_users_sheet", new_callable=AsyncMock):
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

        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("handlers.open_spreadsheet", new_callable=AsyncMock, return_value=fake_ss), \
             patch("handlers.sync_event_users_sheet", new_callable=AsyncMock):
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
        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("handlers.open_spreadsheet", new_callable=AsyncMock, return_value=fake_ss), \
             patch("handlers.sync_event_users_sheet", sync_mock):
            await handlers.button_handler(upd, ctx)

        sync_mock.assert_awaited_once()
        _, _, going_ids = sync_mock.call_args.args
        # alice (main hub) + bob + carol (two DIFFERENT child chats) must all
        # be present; dave (notgoing) must be excluded.
        assert sorted(going_ids) == ["1", "2", "3"]

    async def test_extra_player_is_included_in_event_users_export(self, db_path):
        """
        Regression test for bug #4: a name added via "Add Extra Player" has
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
        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("handlers.open_spreadsheet", new_callable=AsyncMock, return_value=fake_ss), \
             patch("handlers.sync_event_users_sheet", sync_mock):
            await handlers.button_handler(upd, ctx)

        sync_mock.assert_awaited_once()
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
        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("handlers.open_spreadsheet", new_callable=AsyncMock, return_value=fake_ss):
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
        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("handlers.open_spreadsheet", new_callable=AsyncMock, return_value=fake_ss):
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
        with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock), \
             patch("handlers.open_spreadsheet", new_callable=AsyncMock, return_value=fake_ss):
            await handlers.button_handler(upd, ctx)

        ws = fake_ss.worksheets["Actions"]
        assert ws.appended_rows[0][1] == "GOING"


class TestSharedLabelAndIcon:
    """The child-chat broadcast text must say 'SHARED:' with the ↪️ icon."""

    async def test_shared_label_uses_new_icon_and_short_text(self, db_path):
        insert_event(db_path, event_id="ev1", chat_id=MAIN_CHAT)
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO event_shares VALUES (NULL,'ev1','-200','42','-visible','group')")
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
        assert "↪️ *SHARED:" in text
        assert "SHARED EVENT" not in text
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
        assert "2, from: alice" in text

    async def test_guest_line_present_in_child_view(self, db_path):
        insert_event(db_path, event_id="ev1", chat_id=MAIN_CHAT)
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO event_shares VALUES (NULL,'ev1','-200','42','-visible','group')")
        conn.commit()
        conn.close()
        insert_event_user(db_path, event_id="ev1", chat_id="-200", user_id="9",
                           username="channelfan", status="going", guests=4)

        bot = make_bot()
        ctx = make_context(bot=bot)

        await handlers.update_all_shared_views(ctx, "ev1")

        child_calls = [c for c in bot.edit_message_text.call_args_list if c.kwargs.get("chat_id") == -200]
        text = child_calls[0].kwargs.get("text", "")
        assert "4, from: channelfan" in text
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
