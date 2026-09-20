"""
Tests for scripts/sync_bot_roles.py - queries Telegram's live
getChatMember for the bot's own status in every registered group/
channel and backfills the role column accordingly.
"""
import sys
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, ".")
import db as db_module
import scripts.sync_bot_roles as sync_roles


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "test.db")
    monkeypatch.setattr(db_module, "DB_PATH", path)
    db_module.init_db(db_path=path)
    return path


def _make_bot(status_by_chat_id):
    """status_by_chat_id: {chat_id_int: "member"|"administrator"|"left"}."""
    bot = MagicMock()
    bot.id = 999999
    bot.__aenter__ = AsyncMock(return_value=bot)
    bot.__aexit__ = AsyncMock(return_value=False)

    async def _get_chat_member(chat_id, user_id):
        status = status_by_chat_id.get(chat_id, "left")
        return MagicMock(status=status)

    bot.get_chat_member = AsyncMock(side_effect=_get_chat_member)
    return bot


class TestSyncBotRoles:
    async def test_detects_admin_role_for_a_group(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO all_groups (chat_id, chat_name) VALUES ('-100', 'My Group')")
        conn.commit()
        conn.close()

        bot = _make_bot({-100: "administrator"})
        with patch("scripts.sync_bot_roles.Bot", return_value=bot), \
             patch("scripts.sync_bot_roles.TELEGRAM_TOKEN", "fake_token"):
            await sync_roles._run(db_path=db_path)

        conn = sqlite3.connect(db_path)
        role = conn.execute("SELECT role FROM all_groups WHERE chat_id='-100'").fetchone()[0]
        assert role == "ADMIN"

    async def test_detects_member_role_for_a_channel(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO all_channels (chat_id, chat_name) VALUES ('-200', 'My Channel')")
        conn.commit()
        conn.close()

        bot = _make_bot({-200: "member"})
        with patch("scripts.sync_bot_roles.Bot", return_value=bot), \
             patch("scripts.sync_bot_roles.TELEGRAM_TOKEN", "fake_token"):
            await sync_roles._run(db_path=db_path)

        conn = sqlite3.connect(db_path)
        role = conn.execute("SELECT role FROM all_channels WHERE chat_id='-200'").fetchone()[0]
        assert role == "MEMBER"

    async def test_left_chat_is_skipped_not_deleted(self, db_path):
        """A chat the bot has actually left must be left untouched by
        this script - real removal happens via register_chat_removed()
        triggered by a genuine my_chat_member update, not silently
        here."""
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO all_groups (chat_id, chat_name, role) VALUES ('-100', 'My Group', 'MEMBER')")
        conn.commit()
        conn.close()

        bot = _make_bot({-100: "left"})
        with patch("scripts.sync_bot_roles.Bot", return_value=bot), \
             patch("scripts.sync_bot_roles.TELEGRAM_TOKEN", "fake_token"):
            await sync_roles._run(db_path=db_path)

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT chat_id, role FROM all_groups WHERE chat_id='-100'").fetchone()
        assert row == ("-100", "MEMBER"), "row must still exist, role left as before"

    async def test_target_single_chat_ignores_others(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO all_groups (chat_id, chat_name) VALUES ('-100', 'G1')")
        conn.execute("INSERT INTO all_groups (chat_id, chat_name) VALUES ('-200', 'G2')")
        conn.commit()
        conn.close()

        bot = _make_bot({-100: "administrator", -200: "administrator"})
        with patch("scripts.sync_bot_roles.Bot", return_value=bot), \
             patch("scripts.sync_bot_roles.TELEGRAM_TOKEN", "fake_token"):
            await sync_roles._run(target_chat_id="-100", db_path=db_path)

        conn = sqlite3.connect(db_path)
        role_100 = conn.execute("SELECT role FROM all_groups WHERE chat_id='-100'").fetchone()[0]
        role_200 = conn.execute("SELECT role FROM all_groups WHERE chat_id='-200'").fetchone()[0]
        assert role_100 == "ADMIN"
        assert role_200 == "MEMBER", "untargeted chat must be left at its default, never queried"

    async def test_no_token_does_not_crash(self, db_path):
        with patch("scripts.sync_bot_roles.TELEGRAM_TOKEN", None):
            await sync_roles._run(db_path=db_path)  # must not raise
