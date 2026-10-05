"""
/version - owner-only, DM-only (Type 3, like /lockbot): which build is
actually running, when it started, and the Python / python-telegram-bot
versions. Silence for everyone who isn't an owner.
"""
import sys
import time
from unittest.mock import patch

import pytest

sys.path.insert(0, ".")
import subscription
import help_system
from config import BOT_VERSION
from utils import format_uptime
from tests.helpers import make_chat, make_user, make_message, make_update, make_context, make_bot

OWNER = 555


async def _run(chat_type="private", user_id=OWNER):
    chat = make_chat(chat_id=-999 if chat_type != "private" else 999, chat_type=chat_type)
    msg = make_message(chat=chat)
    upd = make_update(chat=chat, user=make_user(user_id=user_id), message=msg)
    with patch("subscription.OWNER_USER_IDS", {OWNER}):
        await subscription.version_command(upd, make_context(bot=make_bot()))
    return msg


class TestVersionCommand:
    async def test_owner_in_dm_gets_the_report(self):
        msg = await _run()
        text = msg.reply_text.call_args.args[0]
        assert msg.reply_text.call_args.kwargs["parse_mode"] == "MarkdownV2"
        assert BOT_VERSION.replace(".", "\\.") in text
        for expected in ("Version:", "Started:", "up ", "Python:", "python\\-telegram\\-bot:"):
            assert expected in text

    async def test_reports_the_version_it_is_actually_running(self):
        with patch("subscription.BOT_VERSION", "9.8.7"):
            msg = await _run()
        assert "9\\.8\\.7" in msg.reply_text.call_args.args[0]

    async def test_uptime_is_measured_from_the_process_start(self):
        with patch("subscription.STARTED_AT", time.time() - 3700):
            msg = await _run()
        assert "up 1h 1m" in msg.reply_text.call_args.args[0]

    async def test_non_owner_in_dm_gets_total_silence(self):
        msg = await _run(user_id=1)
        msg.reply_text.assert_not_called()

    async def test_non_owner_in_a_group_gets_total_silence(self):
        msg = await _run(chat_type="supergroup", user_id=1)
        msg.reply_text.assert_not_called()

    async def test_owner_in_a_group_is_told_it_is_dm_only(self):
        msg = await _run(chat_type="supergroup")
        assert "only works in a DM" in msg.reply_text.call_args.args[0]


class TestVersionIsDocumentedAndRegistered:
    def test_owner_help_lists_it(self):
        text = help_system._build_owner_help_text()
        assert "/version" in text
        assert text.index("/version") < text.index("/stats")

    def test_command_is_registered(self):
        source = open("main.py", encoding="utf-8").read()
        assert 'CommandHandler("version", version_command)' in source


class TestFormatUptime:
    @pytest.mark.parametrize("seconds, expected", [
        (-5, "0s"), (0, "0s"), (59, "59s"), (60, "1m 0s"), (125, "2m 5s"),
        (3599, "59m 59s"), (3600, "1h 0m"), (7500, "2h 5m"), (86399, "23h 59m"),
        (86400, "1d 0h 0m"), (93784, "1d 2h 3m"),
    ])
    def test_format(self, seconds, expected):
        assert format_uptime(seconds) == expected


import sqlite3
from unittest.mock import AsyncMock

import db as db_module


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "test.db")
    monkeypatch.setattr(db_module, "DB_PATH", path)
    db_module.init_db(db_path=path)
    return path


class _FakeWorksheet:
    def __init__(self):
        self.updates = []

    async def get_all_values(self):
        return []

    async def update(self, cell_range, values):
        self.updates.append(values)

    async def batch_clear(self, ranges):
        pass


class _FakeSpreadsheet:
    def __init__(self):
        self.worksheets = {}

    async def worksheet(self, name):
        return self.worksheets.setdefault(name, _FakeWorksheet())


class TestVersionInBotconfig:
    """/version is listed in BOTCONFIG as an owner-only feature."""

    def test_seeded_as_an_owner_level_feature(self, db_path):
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT feature_label, min_tier, description FROM all_features "
                           "WHERE feature_key = 'version'").fetchone()
        assert row is not None
        label, tier, description = row
        assert label.startswith("/version") and tier == "OWNER"
        assert "Owner-only" in description

    async def test_botconfig_shows_it_as_available_to_owners_only(self, db_path):
        fake_ss = _FakeSpreadsheet()
        with patch("sheets.CONTROL_SHEET_ID", "fake"), \
             patch("sheets.open_spreadsheet", new_callable=AsyncMock, return_value=fake_ss):
            await subscription._push_control_sheet_botconfig()

        grid = fake_ss.worksheets["BOTCONFIG"].updates[0]
        assert grid[0][4] == "OWNER"
        row = next(r for r in grid if r[0] == "version")
        assert row[2:5] == ["no", "no", "yes"], "FREE no / PRO no / OWNER yes"
        assert "Owner-only" in row[5]
