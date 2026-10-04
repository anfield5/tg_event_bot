"""
The top feature tier is called OWNER (it used to be ADMIN): it is gated on
OWNER_USER_IDS - WHO is asking - not on anyone being a chat admin. Covers the
one-time conversion of existing databases, /updatefeature's level names, and
that the old -a flag of /stats no longer opens the owner report. (The renamed
-o/-owner flags of /help and /stats and BOTCONFIG's OWNER header are covered
where those features' own tests live.)
"""
import sys
import sqlite3
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, ".")
import db as db_module
import handlers
import subscription
from tests.helpers import make_chat, make_user, make_message, make_update, make_context, make_bot

OWNER_ID = 555


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "test.db")
    monkeypatch.setattr(db_module, "DB_PATH", path)
    db_module.init_db(db_path=path)
    return path


def _tiers(path):
    conn = sqlite3.connect(path)
    rows = dict(conn.execute("SELECT feature_key, min_tier FROM all_features").fetchall())
    conn.close()
    return rows


class TestAdminTierIsConvertedToOwner:
    def test_fresh_database_never_contains_the_old_name(self, db_path):
        assert "ADMIN" not in set(_tiers(db_path).values())

    def test_existing_admin_rows_become_owner_and_others_are_untouched(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE all_features SET min_tier = 'ADMIN' WHERE feature_key IN ('setsub', 'owner_overview', 'deleteevent')")
        conn.execute("UPDATE all_features SET min_tier = 'FREE' WHERE feature_key = 'monitoring'")  # an owner's own override
        conn.commit()
        conn.close()

        db_module.init_db(db_path=db_path)  # what a restart does

        tiers = _tiers(db_path)
        assert tiers["setsub"] == tiers["owner_overview"] == tiers["deleteevent"] == "OWNER"
        assert tiers["monitoring"] == "FREE", "a tier the owner set by hand must survive"
        assert tiers["aliases"] == "PRO"
        assert "ADMIN" not in set(tiers.values())

    def test_conversion_is_idempotent(self, db_path):
        before = _tiers(db_path)
        db_module.init_db(db_path=db_path)
        db_module.init_db(db_path=db_path)
        assert _tiers(db_path) == before


class TestUpdatefeatureLevelNames:
    async def _run(self, db_path, level):
        with patch("subscription.OWNER_USER_IDS", {OWNER_ID}), \
             patch("sheets.CONTROL_SHEET_ID", "fake"), \
             patch("sheets.open_spreadsheet", new_callable=AsyncMock):
            chat = make_chat(chat_id=-999, chat_type="private")
            msg = make_message(chat=chat)
            upd = make_update(chat=chat, user=make_user(user_id=OWNER_ID), message=msg)
            await subscription.updatefeature(upd, make_context(bot=make_bot(), args=["shareevent", "-minlevel", level]))
        return msg.reply_text.call_args.args[0]

    async def test_owner_is_accepted(self, db_path):
        await self._run(db_path, "owner")
        assert _tiers(db_path)["shareevent"] == "OWNER"

    async def test_old_admin_name_is_rejected_and_the_message_names_owner(self, db_path):
        before = _tiers(db_path)["shareevent"]
        text = await self._run(db_path, "admin")
        assert "owner" in text and "admin" not in text.split("one of")[-1]
        assert _tiers(db_path)["shareevent"] == before, "a rejected level must change nothing"


class TestStatsOldFlagNoLongerOpensTheOwnerReport:
    async def test_dash_a_is_not_the_owner_report_any_more(self, db_path):
        chat = make_chat(chat_id=-999, chat_type="private")
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=make_user(user_id=OWNER_ID), message=msg)
        with patch("handlers.OWNER_USER_IDS", {OWNER_ID}):
            await handlers.stats_command(upd, make_context(bot=make_bot(), args=["-a"]))
        texts = " ".join(c.args[0] for c in msg.reply_text.call_args_list if c.args)
        assert "Bot added to groups" not in texts


class TestStatsOwnerFlagForms:
    """/stats takes the owner flag in both forms, like /help: -o and -owner."""

    async def _run(self, flag, user_id):
        chat = make_chat(chat_id=-999, chat_type="private")
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=make_user(user_id=user_id), message=msg)
        with patch("handlers.OWNER_USER_IDS", {OWNER_ID}):
            await handlers.stats_command(upd, make_context(bot=make_bot(), args=[flag]))
        return " ".join(c.args[0] for c in msg.reply_text.call_args_list if c.args)

    @pytest.mark.parametrize("flag", ["-o", "-owner"])
    async def test_owner_gets_the_report_with_either_form(self, db_path, flag):
        assert "Bot added to groups" in await self._run(flag, OWNER_ID)

    @pytest.mark.parametrize("flag", ["-o", "-owner"])
    async def test_non_owner_gets_total_silence_with_either_form(self, db_path, flag):
        assert await self._run(flag, 1) == ""
