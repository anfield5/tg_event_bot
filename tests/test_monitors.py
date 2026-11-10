"""
/addmonitor, /removemonitor, /listmonitors (monitors.py) - PRO-only. /addmonitor is the delicate one: it needs an admin
of the hub AND of the target chat, the bot to be an admin there too, and it refuses an anonymous admin because that
person's rights in a DIFFERENT chat cannot be verified.
"""
import sqlite3
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, ".")
import monitors
from tests.helpers import make_bot, make_chat, make_context, make_message, make_update, make_user
from utils import GROUP_ANONYMOUS_BOT_ID

HUB, TARGET, BOT_ID, ME = "-100123", "-200555", 777, 42


def sql(db_path, statement, *params):
    conn = sqlite3.connect(db_path)
    conn.execute(statement, params)
    conn.commit()
    conn.close()


def rows(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT chat_id, chat_type, chat_name, owner_chat_id, alias, is_monitored FROM sub_chats ORDER BY id").fetchall()
    finally:
        conn.close()


def bot_with(members: dict, chat=None, chat_error=False):
    """members: {(chat_id, user_id): status or an Exception} - what get_chat_member says."""
    bot = make_bot(BOT_ID)

    async def get_chat_member(chat_id, user_id):
        answer = members.get((str(chat_id), int(user_id)), "left")
        if isinstance(answer, Exception):
            raise answer
        return MagicMock(status=answer)
    bot.get_chat_member = AsyncMock(side_effect=get_chat_member)
    if chat_error:
        bot.get_chat = AsyncMock(side_effect=RuntimeError("chat not found"))
    else:
        bot.get_chat = AsyncMock(return_value=chat or MagicMock(title="Target Group", type="supergroup", id=int(TARGET)))
    return bot


ALL_GOOD = {(HUB, ME): "administrator", (TARGET, BOT_ID): "administrator", (TARGET, ME): "creator"}


async def call(command, args, bot=None, user_id=ME, sender_chat=None):
    bot = bot or bot_with(ALL_GOOD)
    chat = make_chat(chat_id=int(HUB), chat_type="supergroup")
    user = make_user(user_id=user_id, username="boss")
    msg = make_message(chat=chat, user=user)
    msg.sender_chat = sender_chat
    await command(make_update(chat=chat, user=user, message=msg), make_context(bot=bot, args=args))
    return [c.args[0] for c in msg.reply_text.call_args_list]


class TestItIsProOnly:
    @pytest.mark.parametrize("command,args", [(monitors.addmonitor, [TARGET]), (monitors.removemonitor, [TARGET]), (monitors.listmonitors, [])])
    async def test_a_free_hub_gets_the_upgrade_message_and_nothing_happens(self, db_path, command, args):
        sql(db_path, "INSERT INTO all_groups (chat_id, chat_name, type) VALUES (?, 'Hub', 'FREE')", HUB)
        replies = await call(command, args)
        assert len(replies) == 1 and "PRO" in replies[0]
        assert rows(db_path) == []


class TestAddMonitor:
    async def test_it_needs_a_chat_id(self, db_path, pro):
        assert "Syntax error" in (await call(monitors.addmonitor, []))[0]

    async def test_someone_who_is_not_an_admin_of_the_hub_is_refused(self, db_path, pro):
        bot = bot_with({**ALL_GOOD, (HUB, ME): "member"})
        assert "admin in the main group" in (await call(monitors.addmonitor, [TARGET], bot))[0]
        assert rows(db_path) == []

    async def test_a_chat_the_bot_is_not_in_is_refused(self, db_path, pro):
        bot = bot_with({**ALL_GOOD, (TARGET, BOT_ID): RuntimeError("kicked")})
        assert "not a member" in (await call(monitors.addmonitor, [TARGET], bot))[0]
        assert rows(db_path) == []

    @pytest.mark.parametrize("status", ["member", "restricted", "left"])
    async def test_the_bot_must_be_an_admin_there_or_it_would_never_see_anyone_join(self, db_path, pro, status):
        bot = bot_with({**ALL_GOOD, (TARGET, BOT_ID): status})
        assert "must be an *admin*" in (await call(monitors.addmonitor, [TARGET], bot))[0]
        assert rows(db_path) == []

    async def test_an_anonymous_admin_is_asked_to_post_normally(self, db_path, pro):
        """Telegram only vouches for an anonymous poster in the chat the message is in - not in another chat."""
        replies = await call(monitors.addmonitor, [TARGET], user_id=GROUP_ANONYMOUS_BOT_ID, sender_chat=make_chat(chat_id=int(HUB)))
        assert "disable" in replies[0] and "Remain anonymous" in replies[0]
        assert rows(db_path) == []

    @pytest.mark.parametrize("status", ["member", "left"])
    async def test_someone_who_is_not_an_admin_of_the_target_is_refused(self, db_path, pro, status):
        bot = bot_with({**ALL_GOOD, (TARGET, ME): status})
        assert "admin in the target" in (await call(monitors.addmonitor, [TARGET], bot))[0]
        assert rows(db_path) == []

    async def test_an_admin_status_that_cannot_be_checked_is_not_assumed(self, db_path, pro):
        bot = bot_with({**ALL_GOOD, (TARGET, ME): RuntimeError("forbidden")})
        assert "Could not verify" in (await call(monitors.addmonitor, [TARGET], bot))[0]
        assert rows(db_path) == []

    async def test_a_chat_that_cannot_be_described_is_not_added(self, db_path, pro):
        bot = bot_with(ALL_GOOD, chat_error=True)
        assert "Could not retrieve" in (await call(monitors.addmonitor, [TARGET], bot))[0]
        assert rows(db_path) == []

    async def test_success_stores_the_name_the_type_and_the_hub_that_owns_it(self, db_path, pro):
        replies = await call(monitors.addmonitor, [TARGET])
        assert replies == ["✅ Added monitor: `Target Group` \\(group\\)"]
        assert rows(db_path) == [(TARGET, "group", "Target Group", HUB, None, 1)]

    async def test_a_channel_is_stored_as_a_channel(self, db_path, pro):
        bot = bot_with(ALL_GOOD, chat=MagicMock(title="News", type="channel", id=int(TARGET)))
        assert "\\(channel\\)" in (await call(monitors.addmonitor, [TARGET], bot))[0]
        assert rows(db_path)[0][1] == "channel"

    async def test_what_is_stored_is_the_numeric_id_even_when_a_public_username_was_typed(self, db_path, pro):
        """A @username stored as the id would break every later int(chat_id) - /refreshusersall would skip the chat."""
        bot = bot_with({**ALL_GOOD, ("@newschannel", BOT_ID): "administrator", ("@newschannel", ME): "creator"})
        await call(monitors.addmonitor, ["@newschannel"], bot)
        assert rows(db_path)[0][0] == TARGET

    async def test_an_existing_alias_row_is_switched_on_not_duplicated(self, db_path, pro):
        sql(db_path, "INSERT INTO sub_chats (chat_id, chat_type, chat_name, owner_chat_id, alias, is_monitored) VALUES (?, 'group', 'Old', ?, 'tv', 0)", TARGET, HUB)
        await call(monitors.addmonitor, [TARGET])
        assert rows(db_path) == [(TARGET, "group", "Target Group", HUB, "tv", 1)]

    async def test_adding_the_same_chat_twice_keeps_one_row(self, db_path, pro):
        await call(monitors.addmonitor, [TARGET])
        await call(monitors.addmonitor, [TARGET])
        assert len(rows(db_path)) == 1

    async def test_another_hubs_row_for_the_same_chat_is_left_alone(self, db_path, pro):
        sql(db_path, "INSERT INTO sub_chats (chat_id, chat_type, chat_name, owner_chat_id, is_monitored) VALUES (?, 'group', 'Theirs', '-999', 1)", TARGET)
        await call(monitors.addmonitor, [TARGET])
        assert sorted(r[3] for r in rows(db_path)) == ["-100123", "-999"]

    async def test_a_database_failure_is_reported_not_raised(self, db_path, pro):
        with patch("monitors.get_connection", side_effect=RuntimeError("disk full")):
            assert "Failed to add monitor" in (await call(monitors.addmonitor, [TARGET]))[0]

    async def test_a_name_with_markdown_characters_is_escaped(self, db_path, pro):
        bot = bot_with(ALL_GOOD, chat=MagicMock(title="My_Group (v2)", type="supergroup", id=int(TARGET)))
        assert "`My\\_Group \\(v2\\)`" in (await call(monitors.addmonitor, [TARGET], bot))[0]


class TestRemoveMonitor:
    def monitor(self, db_path, alias=None, owner=HUB, chat=TARGET, monitored=1):
        sql(db_path, "INSERT INTO sub_chats (chat_id, chat_type, chat_name, owner_chat_id, alias, is_monitored) VALUES (?, 'group', 'Target Group', ?, ?, ?)",
            chat, owner, alias, monitored)

    async def test_it_needs_a_chat_id(self, db_path, pro):
        assert "Syntax error" in (await call(monitors.removemonitor, []))[0]

    async def test_a_chat_that_is_not_monitored_is_not_found(self, db_path, pro):
        assert "Monitor not found" in (await call(monitors.removemonitor, [TARGET]))[0]

    async def test_the_row_is_deleted_when_the_chat_has_no_alias(self, db_path, pro):
        self.monitor(db_path)
        assert (await call(monitors.removemonitor, [TARGET]))[0] == "✅ Removed monitor: `Target Group`"
        assert rows(db_path) == []

    async def test_when_the_chat_also_has_an_alias_only_the_monitoring_is_switched_off(self, db_path, pro):
        self.monitor(db_path, alias="tv")
        await call(monitors.removemonitor, [TARGET])
        assert rows(db_path) == [(TARGET, "group", "Target Group", HUB, "tv", 0)]

    async def test_another_hubs_monitor_cannot_be_removed(self, db_path, pro):
        self.monitor(db_path, owner="-999")
        assert "Monitor not found" in (await call(monitors.removemonitor, [TARGET]))[0]
        assert len(rows(db_path)) == 1

    async def test_an_older_row_without_an_owner_can_be_removed(self, db_path, pro):
        self.monitor(db_path, owner=None)
        await call(monitors.removemonitor, [TARGET])
        assert rows(db_path) == []

    async def test_a_row_that_is_only_an_alias_is_not_a_monitor(self, db_path, pro):
        self.monitor(db_path, alias="tv", monitored=0)
        assert "Monitor not found" in (await call(monitors.removemonitor, [TARGET]))[0]
        assert len(rows(db_path)) == 1


class TestListMonitors:
    async def test_none_configured(self, db_path, pro):
        assert "No monitors configured" in (await call(monitors.listmonitors, []))[0]

    async def test_it_lists_name_type_and_id_of_this_hubs_monitors_only(self, db_path, pro):
        sql(db_path, "INSERT INTO sub_chats (chat_id, chat_type, chat_name, owner_chat_id, is_monitored) VALUES ('-1', 'group', 'Mine', ?, 1)", HUB)
        sql(db_path, "INSERT INTO sub_chats (chat_id, chat_type, chat_name, owner_chat_id, is_monitored) VALUES ('-2', 'channel', 'Old', NULL, 1)")
        sql(db_path, "INSERT INTO sub_chats (chat_id, chat_type, chat_name, owner_chat_id, is_monitored) VALUES ('-3', 'group', 'Theirs', '-999', 1)")
        sql(db_path, "INSERT INTO sub_chats (chat_id, chat_type, chat_name, owner_chat_id, alias, is_monitored) VALUES ('-4', 'group', 'AliasOnly', ?, 'x', 0)", HUB)
        text = (await call(monitors.listmonitors, []))[0]
        assert "name: `Mine`" in text and "type: group" in text and "id: `\\-1`" in text
        assert "Old" in text and "channel" in text, "a row without an owner belongs to every hub"
        assert "Theirs" not in text and "AliasOnly" not in text

    async def test_names_are_escaped(self, db_path, pro):
        sql(db_path, "INSERT INTO sub_chats (chat_id, chat_type, chat_name, owner_chat_id, is_monitored) VALUES ('-1', 'group', 'A_B (c)', ?, 1)", HUB)
        assert "`A\\_B \\(c\\)`" in (await call(monitors.listmonitors, []))[0]
