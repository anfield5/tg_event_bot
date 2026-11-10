"""
/setalias, /removealias, /listaliases (aliases.py) - PRO-only short names for the chats an event can be shared to.
/setalias verifies the bot AND the caller are admins in the target, refuses an anonymous admin (nothing vouches for their
rights in another chat), and keeps a row that /addmonitor made instead of adding a second one for the same chat.
"""
import sqlite3
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest

sys.path.insert(0, ".")
import aliases
from tests.test_monitors import ALL_GOOD, BOT_ID, HUB, ME, TARGET, bot_with, call, rows, sql
from utils import GROUP_ANONYMOUS_BOT_ID


class TestItIsProOnly:
    @pytest.mark.parametrize("command,args", [(aliases.setalias, [TARGET, "tv"]), (aliases.removealias, ["tv"]), (aliases.listalias, [])])
    async def test_a_free_hub_gets_the_upgrade_message_and_nothing_happens(self, db_path, command, args):
        sql(db_path, "INSERT INTO all_groups (chat_id, chat_name, type) VALUES (?, 'Hub', 'FREE')", HUB)
        replies = await call(command, args)
        assert len(replies) == 1 and "PRO" in replies[0]
        assert rows(db_path) == []


class TestSetAlias:
    async def test_it_needs_a_chat_and_a_name(self, db_path, pro):
        assert "Syntax error" in (await call(aliases.setalias, [TARGET]))[0]

    @pytest.mark.parametrize("name", ["main", "all", "MAIN", "All"])
    async def test_the_names_who_uses_are_reserved(self, db_path, pro, name):
        reply = (await call(aliases.setalias, [TARGET, name]))[0]
        assert "reserved name" in reply and rows(db_path) == []

    @pytest.mark.parametrize("typed,stored", [("-200555", "-200555"), ("200555", "200555")])
    async def test_a_numeric_chat_is_stored_as_text_of_that_number(self, db_path, pro, typed, stored):
        bot = bot_with({(HUB, ME): "administrator", (stored, BOT_ID): "administrator", (stored, ME): "creator"})
        await call(aliases.setalias, [typed, "tv"], bot)
        assert rows(db_path) == [(stored, None, None, HUB, "tv", 0)]

    async def test_the_alias_is_stored_in_lower_case(self, db_path, pro):
        await call(aliases.setalias, [TARGET, "TvChannel"])
        assert rows(db_path)[0][4] == "tvchannel"

    async def test_success_says_what_was_mapped_to_what(self, db_path, pro):
        reply = (await call(aliases.setalias, [TARGET, "tv"]))[0]
        assert "✅" in reply and "`__tv__`" in reply and TARGET in reply

    async def test_a_username_is_accepted_as_the_target(self, db_path, pro):
        bot = bot_with({(HUB, ME): "administrator", ("@news", BOT_ID): "administrator", ("@news", ME): "creator"})
        await call(aliases.setalias, ["@news", "n"], bot)
        assert rows(db_path)[0][0] == "@news"

    async def test_a_chat_that_does_not_exist_is_said_so(self, db_path, pro):
        bot = bot_with(ALL_GOOD)
        bot.get_chat = AsyncMock(side_effect=BadRequest("Chat not found"))
        assert (await call(aliases.setalias, [TARGET, "tv"], bot))[0] == "No channel/group with such ID"
        assert rows(db_path) == []

    async def test_any_other_failure_to_look_it_up_asks_for_the_bot_to_be_added(self, db_path, pro):
        bot = bot_with(ALL_GOOD)
        bot.get_chat = AsyncMock(side_effect=RuntimeError("network"))
        assert "Add @EventPlanCheckBot" in (await call(aliases.setalias, [TARGET, "tv"], bot))[0]
        assert rows(db_path) == []

    @pytest.mark.parametrize("bot_status", ["member", "left", RuntimeError("kicked")])
    async def test_the_bot_must_be_an_admin_of_the_target(self, db_path, pro, bot_status):
        bot = bot_with({**ALL_GOOD, (TARGET, BOT_ID): bot_status})
        assert "Add @EventPlanCheckBot" in (await call(aliases.setalias, [TARGET, "tv"], bot))[0]
        assert rows(db_path) == []

    async def test_an_anonymous_admin_is_asked_to_post_normally(self, db_path, pro):
        replies = await call(aliases.setalias, [TARGET, "tv"], user_id=GROUP_ANONYMOUS_BOT_ID, sender_chat=MagicMock())
        assert "Remain anonymous" in replies[0] and rows(db_path) == []

    @pytest.mark.parametrize("user_status", ["member", "left", RuntimeError("forbidden")])
    async def test_the_caller_must_be_an_admin_of_the_target(self, db_path, pro, user_status):
        bot = bot_with({**ALL_GOOD, (TARGET, ME): user_status})
        assert "admin rights in target" in (await call(aliases.setalias, [TARGET, "tv"], bot))[0]
        assert rows(db_path) == []

    async def test_a_name_that_is_already_taken_by_this_hub_is_refused(self, db_path, pro):
        await call(aliases.setalias, [TARGET, "tv"])
        other = "-300777"
        bot = bot_with({(HUB, ME): "administrator", (other, BOT_ID): "administrator", (other, ME): "creator"})
        assert "already exists" in (await call(aliases.setalias, [other, "tv"], bot))[0]
        assert len(rows(db_path)) == 1

    async def test_a_chat_that_already_has_an_alias_is_refused(self, db_path, pro):
        await call(aliases.setalias, [TARGET, "tv"])
        assert "already been added" in (await call(aliases.setalias, [TARGET, "radio"]))[0]
        assert [r[4] for r in rows(db_path)] == ["tv"]

    async def test_a_chat_that_was_added_as_a_monitor_gets_its_alias_on_the_same_row(self, db_path, pro):
        sql(db_path, "INSERT INTO sub_chats (chat_id, chat_type, chat_name, owner_chat_id, is_monitored) VALUES (?, 'group', 'Target Group', ?, 1)", TARGET, HUB)
        await call(aliases.setalias, [TARGET, "tv"])
        assert rows(db_path) == [(TARGET, "group", "Target Group", HUB, "tv", 1)]

    async def test_two_hubs_may_use_the_same_alias_for_different_chats(self, db_path, pro):
        sql(db_path, "INSERT INTO sub_chats (chat_id, owner_chat_id, alias) VALUES ('-900', '-999', 'tv')")
        await call(aliases.setalias, [TARGET, "tv"])
        assert sorted((r[3], r[4]) for r in rows(db_path)) == [("-100123", "tv"), ("-999", "tv")]

    async def test_a_race_that_slips_past_the_checks_is_reported_not_raised(self, db_path, pro):
        """Two concurrent /setalias for the same name: the UNIQUE constraint is the last line of defence."""
        class Conn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def cursor(self): return self
            def fetchone(self): return None
            def commit(self): pass
            def execute(self, statement, params=()):
                if statement.startswith("INSERT"):
                    raise sqlite3.IntegrityError("UNIQUE constraint failed")
        with patch("aliases.get_connection", return_value=Conn()):
            assert "already in use" in (await call(aliases.setalias, [TARGET, "tv"]))[0]


class TestRemoveAlias:
    def alias(self, db_path, name="tv", monitored=0, owner=HUB, chat=TARGET):
        sql(db_path, "INSERT INTO sub_chats (chat_id, chat_type, chat_name, owner_chat_id, alias, is_monitored) VALUES (?, 'group', 'Target Group', ?, ?, ?)",
            chat, owner, name, monitored)

    async def test_it_needs_a_name(self, db_path, pro):
        assert "Syntax error" in (await call(aliases.removealias, []))[0]

    async def test_an_unknown_alias_is_not_found(self, db_path, pro):
        assert "not found" in (await call(aliases.removealias, ["tv"]))[0]

    async def test_the_row_is_deleted_and_the_name_is_case_insensitive(self, db_path, pro):
        self.alias(db_path)
        assert "removed" in (await call(aliases.removealias, ["TV"]))[0]
        assert rows(db_path) == []

    async def test_a_chat_that_is_also_monitored_keeps_its_monitoring(self, db_path, pro):
        self.alias(db_path, monitored=1)
        await call(aliases.removealias, ["tv"])
        assert rows(db_path) == [(TARGET, "group", "Target Group", HUB, None, 1)]

    async def test_another_hubs_alias_cannot_be_removed(self, db_path, pro):
        self.alias(db_path, owner="-999")
        assert "not found" in (await call(aliases.removealias, ["tv"]))[0]
        assert len(rows(db_path)) == 1

    async def test_an_older_alias_without_an_owner_can_be_removed(self, db_path, pro):
        self.alias(db_path, owner=None)
        await call(aliases.removealias, ["tv"])
        assert rows(db_path) == []


class TestListAliases:
    async def test_none_configured(self, db_path, pro):
        assert "No aliases configured" in (await call(aliases.listalias, []))[0]

    async def test_it_lists_alias_type_name_and_id_of_this_hubs_aliases_only(self, db_path, pro):
        sql(db_path, "INSERT INTO sub_chats (chat_id, owner_chat_id, alias) VALUES (?, ?, 'tv')", TARGET, HUB)
        sql(db_path, "INSERT INTO sub_chats (chat_id, owner_chat_id, alias) VALUES ('-300', NULL, 'old')")
        sql(db_path, "INSERT INTO sub_chats (chat_id, owner_chat_id, alias) VALUES ('-400', '-999', 'theirs')")
        sql(db_path, "INSERT INTO sub_chats (chat_id, chat_type, owner_chat_id, is_monitored) VALUES ('-500', 'group', ?, 1)", HUB)
        bot = bot_with(ALL_GOOD, chat=MagicMock(title="Big_TV (HD)", type="channel"))
        text = (await call(aliases.listalias, [], bot))[0]
        assert "Aliasname: tv" in text and "Type: Public Channel" in text and "Name: Big\\_TV \\(HD\\)" in text and "ID: \\-200555" in text
        assert "Aliasname: old" in text, "a row without an owner belongs to every hub"
        assert "theirs" not in text and "-500" not in text

    async def test_a_group_is_shown_as_a_group(self, db_path, pro):
        sql(db_path, "INSERT INTO sub_chats (chat_id, owner_chat_id, alias) VALUES (?, ?, 'tv')", TARGET, HUB)
        assert "Type: Group" in (await call(aliases.listalias, []))[0]

    async def test_a_chat_the_bot_can_no_longer_see_is_marked_not_dropped(self, db_path, pro):
        sql(db_path, "INSERT INTO sub_chats (chat_id, owner_chat_id, alias) VALUES (?, ?, 'tv')", TARGET, HUB)
        bot = bot_with(ALL_GOOD, chat_error=True)
        text = (await call(aliases.listalias, [], bot))[0]
        assert "Name: Node Disconnected" in text and "Type: Unknown" in text and "Aliasname: tv" in text

    async def test_a_username_target_is_looked_up_by_name(self, db_path, pro):
        sql(db_path, "INSERT INTO sub_chats (chat_id, owner_chat_id, alias) VALUES ('@news', ?, 'n')", HUB)
        bot = bot_with(ALL_GOOD)
        await call(aliases.listalias, [], bot)
        bot.get_chat.assert_awaited_once_with("@news")
