"""/waitlist typed in a private DM with the bot: the group comes from the DM's selected hub (it used to answer
"No event found for this group", because the DM's own chat id was treated as the group)."""
import json
import sqlite3
import sys
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, ".")
import handlers
import hub_resolver
from tests.helpers import make_bot, make_chat, make_context, make_message, make_update, make_user
from tests.test_help_audit import _make_pro

HUB = "-100"


def seed(db_path, waitlist=True):
    entries = [
        {"chat_id": "-100", "chat_name": None, "username": "dave", "user_id": "4", "timestamp": "t"},
        {"chat_id": "-200", "chat_name": None, "username": "erin", "user_id": "5", "timestamp": "t"},
    ] if waitlist else []
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon, event_status, going_data, "
        "notgoing_data, counters_data, kicked_data, waitlist_data) VALUES ('ev1', ?, '1', 'Party', '👍', '❌', 0, '[]', '[]', '{}', '[]', ?)",
        (HUB, json.dumps(entries)),
    )
    conn.execute("INSERT INTO sub_chats (chat_id, owner_chat_id, alias) VALUES ('-200', ?, 'childgroup')", (HUB,))
    conn.commit()
    conn.close()


async def in_dm(selected=HUB, override=None, member_status="administrator"):
    bot = make_bot()
    bot.get_chat = AsyncMock(return_value=MagicMock(title="Child Group"))
    bot.get_chat_member = AsyncMock(return_value=MagicMock(status=member_status))
    chat = make_chat(chat_id=777, chat_type="private")
    msg = make_message(chat=chat)
    upd = make_update(chat=chat, user=make_user(user_id=1), message=msg)
    ctx = make_context(bot=bot)
    if selected is not None:
        ctx.user_data["selected_hub_chat_id"] = selected
    await handlers.waitlist_command(upd, ctx, override_chat_id=override)
    return [c.args[0] for c in msg.reply_text.call_args_list]


class TestWaitlistFromADm:
    async def test_it_shows_the_selected_groups_full_waitlist(self, db_path):
        _make_pro(db_path); seed(db_path)
        text = (await in_dm())[-1]
        assert "dave" in text and "erin" in text and "from" in text, "hub-style: everyone, labelled by chat"

    async def test_this_is_what_used_to_fail(self, db_path):
        _make_pro(db_path); seed(db_path)
        assert "No event found" not in (await in_dm())[-1]

    async def test_an_empty_list_says_so(self, db_path):
        _make_pro(db_path); seed(db_path, waitlist=False)
        assert "currently empty" in (await in_dm())[-1].replace("\\", "")

    async def test_a_group_without_any_event(self, db_path):
        _make_pro(db_path)
        assert "No event found" in (await in_dm())[-1]

    async def test_a_free_group_gets_the_dm_is_pro_notice_not_a_list(self, db_path):
        seed(db_path)                                            # no PRO row for the hub
        text = (await in_dm())[-1].replace("\\", "")
        assert "PRO feature" in text and "/waitlist" in text and "dave" not in text

    async def test_the_group_is_found_by_itself_when_the_person_administers_just_one(self, db_path):
        _make_pro(db_path); seed(db_path)
        texts = await in_dm(selected=None)
        assert "dave" in texts[-1] and "erin" in texts[-1]

    async def test_someone_who_administers_nothing_is_told_so(self, db_path):
        _make_pro(db_path); seed(db_path)
        text = (await in_dm(selected=None, member_status="member"))[-1].replace("\\", "")
        assert "not an admin of any group" in text and "dave" not in text

    async def test_the_picker_replay_path_works(self, db_path):
        _make_pro(db_path); seed(db_path)
        text = (await in_dm(selected=None, override=HUB))[-1]
        assert "dave" in text and "erin" in text

    def test_it_is_registered_for_that_replay(self):
        assert hub_resolver.HUB_COMMAND_REGISTRY["waitlist"] is handlers.waitlist_command


class TestGroupsAreUnchanged:
    async def _in_group(self, chat_id):
        bot = make_bot()
        bot.get_chat = AsyncMock(return_value=MagicMock(title="Child Group"))
        chat = make_chat(chat_id=chat_id, chat_type="supergroup")
        msg = make_message(chat=chat)
        await handlers.waitlist_command(make_update(chat=chat, message=msg), make_context(bot=bot))
        return msg.reply_text.call_args.args[0]

    async def test_the_hub_still_shows_everyone(self, db_path):
        seed(db_path)
        text = await self._in_group(-100)
        assert "dave" in text and "erin" in text

    async def test_a_child_chat_still_shows_only_its_own(self, db_path):
        seed(db_path)
        text = await self._in_group(-200)
        assert "erin" in text and "dave" not in text
