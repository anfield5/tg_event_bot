"""/chatid: in a group, that group's id; in a private DM, the id of the group the DM is working with."""
import sqlite3
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, ".")
import help_system
import hub_resolver
from tests.helpers import make_bot, make_chat, make_context, make_message, make_update, make_user
from tests.test_dm_replay import pick_then_replay

HUB = "-100"


def add_group(db_path, name="Football", tier="FREE"):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO all_groups (chat_id, chat_name, type) VALUES (?, ?, ?)", (HUB, name, tier))
    conn.commit()
    conn.close()


async def in_dm(selected=HUB, status="administrator", override=None, title="Telegram Title"):
    bot = make_bot()
    bot.get_chat_member = AsyncMock(return_value=MagicMock(status=status))
    bot.get_chat = AsyncMock(return_value=MagicMock(title=title))
    chat = make_chat(chat_id=777, chat_type="private")
    msg = make_message(chat=chat)
    ctx = make_context(bot=bot)
    if selected:
        ctx.user_data["selected_hub_chat_id"] = selected
    await help_system.chatid(make_update(chat=chat, user=make_user(user_id=1), message=msg), ctx, override_chat_id=override)
    return [c.args[0] for c in msg.reply_text.call_args_list]


class TestInAGroup:
    @pytest.mark.parametrize("chat_type", ["supergroup", "group", "channel"])
    async def test_it_is_the_chat_itself_as_before(self, db_path, chat_type):
        chat = make_chat(chat_id=-100123, chat_type=chat_type)
        msg = make_message(chat=chat)
        await help_system.chatid(make_update(chat=chat, message=msg), make_context())
        assert msg.reply_text.call_args.args[0] == "This chat's ID: -100123"


class TestInADm:
    async def test_it_shows_the_selected_groups_id_and_name_not_the_dms_own(self, db_path):
        add_group(db_path)
        text = (await in_dm())[-1]
        assert "-100" in text and "Football" in text
        assert "777" not in text, "the DM's own chat id (= the user's id) is what this used to print"

    async def test_a_free_group_works_too_because_its_id_is_what_setsub_needs(self, db_path):
        add_group(db_path, tier="FREE")
        text = (await in_dm())[-1]
        assert "-100" in text and "PRO feature" not in text

    async def test_the_name_comes_from_telegram_when_the_bot_has_none_stored(self, db_path):
        assert "(Telegram Title)" in (await in_dm())[-1]

    async def test_without_any_name_it_is_just_the_id(self, db_path):
        bot_text = (await in_dm(title=None))[-1]
        assert "-100" in bot_text

    async def test_it_points_at_switchgroup(self, db_path):
        add_group(db_path)
        assert "/switchgroup" in (await in_dm())[-1]

    async def test_a_single_administered_group_is_picked_automatically(self, db_path):
        add_group(db_path)
        text = (await in_dm(selected=None))[-1]
        assert "-100" in text and "Football" in text

    async def test_someone_who_administers_nothing_is_told_so(self, db_path):
        add_group(db_path)
        text = (await in_dm(selected=None, status="member"))[-1].replace("\\", "")
        assert "not an admin of any group" in text and "-100" not in text

    async def test_the_group_picker_replay_works(self, db_path):
        add_group(db_path)
        ctx, picker = await pick_then_replay("chatid", [])
        assert "-100" in picker.reply_text.call_args.args[0]

    def test_it_is_registered_for_that_replay(self):
        assert hub_resolver.HUB_COMMAND_REGISTRY["chatid"] is help_system.chatid


class TestTheGateIsStillTheDefault:
    async def _resolve(self, db_path, **kwargs):
        bot = make_bot()
        chat = make_chat(chat_id=777, chat_type="private")
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=make_user(user_id=1), message=msg)
        result = await hub_resolver.resolve_hub_chat_id(upd, make_context(bot=bot), "x", override_chat_id=HUB, **kwargs)
        return result, msg

    async def test_other_commands_are_still_blocked_for_a_free_group(self, db_path):
        add_group(db_path, tier="FREE")
        result, msg = await self._resolve(db_path)
        assert result is None and "PRO feature" in msg.reply_text.call_args.args[0].replace("\\", "")

    async def test_only_a_command_that_asks_to_skip_it_gets_through(self, db_path):
        add_group(db_path, tier="FREE")
        result, msg = await self._resolve(db_path, dm_access_required=False)
        assert result == HUB and msg.reply_text.call_count == 0
