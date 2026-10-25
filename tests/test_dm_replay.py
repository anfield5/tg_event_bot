"""
Commands run from a DM go through hub_resolver: the group is resolved (or picked with buttons, after which the command
is REPLAYED on a stand-in update/context). Everything that is documented as working from a DM has to work on that
replay path too.
"""
import ast
import inspect
import re
import sqlite3
import sys
import textwrap
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, ".")
import handlers
import hub_resolver
import reopen
import who
from tests.helpers import make_bot, make_chat, make_context, make_message, make_update, make_user
from tests.test_help_audit import _make_pro

HUB, OWNER = "-100", 555
_REGISTER_THEMSELVES_ON_IMPORT = (reopen, who)


def seed_event(db_path, status=0, name="Old name"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon, event_status, going_data, "
        "notgoing_data, counters_data, kicked_data) VALUES ('ev1', ?, '1', ?, '👍', '❌', ?, '[]', '[]', '{}', '[]')",
        (HUB, name, status),
    )
    conn.commit()
    conn.close()


async def pick_then_replay(command, args, user_id=1):
    """The DM flow: pending command stored, the admin taps the group button, the command runs."""
    bot = make_bot()
    bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
    bot.get_chat = AsyncMock(return_value=MagicMock(title="Group", type="supergroup"))
    ctx = make_context(bot=bot)
    ctx.user_data["pending_hub_command"] = {"command": command, "args": list(args)}
    chat = make_chat(chat_id=777, chat_type="private")
    picker_message = make_message(chat=chat)
    picker_message.edit_text = AsyncMock()
    query = MagicMock()
    query.data = f"hubpick_{HUB}"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    query.message = picker_message
    query.from_user = make_user(user_id=user_id)
    upd = MagicMock()
    upd.callback_query = query
    with patch("subscription.OWNER_USER_IDS", {OWNER}), \
         patch("handlers.schedule_view_refresh", new=AsyncMock()), \
         patch("reopen.schedule_view_refresh", new=AsyncMock()):
        await hub_resolver.hub_pick_callback_handler(upd, ctx)
    return ctx, picker_message


class TestTheReplayContext:
    def test_it_carries_what_it_was_given_and_forwards_the_rest(self):
        real = MagicMock()
        real.application = "the-application"
        ctx = hub_resolver._ReplayContext(bot="b", args=["x"], user_data={"k": 1}, real_context=real)
        assert (ctx.bot, ctx.args, ctx.user_data) == ("b", ["x"], {"k": 1})
        assert ctx.application == "the-application" and ctx.chat_data is real.chat_data

    def test_without_a_real_context_unknown_attributes_still_fail_loudly(self):
        ctx = hub_resolver._ReplayContext(bot="b", args=[], user_data={})
        with pytest.raises(AttributeError):
            ctx.application


class TestCommandsFinishAfterTheGroupPicker:
    async def test_editevent_saves_AND_refreshes_the_posts(self, db_path):
        """The reported class of bug: it replied 'Event updated. Refreshing views.' and then crashed on
        context.application, so the views were never refreshed and the Sheets sync never ran."""
        _make_pro(db_path); seed_event(db_path)
        ctx, picker = await pick_then_replay("editevent", ["NewName"])
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT name FROM events WHERE event_id = 'ev1'").fetchone()[0] == "NewName"
        texts = [c.args[0] for c in picker.reply_text.call_args_list]
        assert any("Event updated" in t for t in texts)
        assert ctx.application.create_task.called, "the view refresh was scheduled"

    async def test_reopen_switches_the_mode_and_refreshes_the_post(self, db_path):
        import db as db_module
        _make_pro(db_path); seed_event(db_path, status=2)
        ctx, picker = await pick_then_replay("reopen", ["on"], user_id=OWNER)
        assert db_module.is_reopen_active(HUB)
        assert ctx.application.create_task.called
        assert any("Reopen mode is ON" in c.args[0].replace("\\", "").replace("*", "") for c in picker.reply_text.call_args_list)

    async def test_a_command_that_never_needed_application_still_works(self, db_path):
        _make_pro(db_path)
        ctx, picker = await pick_then_replay("waitlist", [])
        assert "No event found" in picker.reply_text.call_args.args[0]


class TestNamesAndShapes:
    def test_every_registered_hub_command_is_a_real_command(self):
        main_text = open("main.py", encoding="utf-8").read()
        real = set(re.findall(r'CommandHandler\("([a-z]+)"', main_text))
        unknown = set(hub_resolver.HUB_COMMAND_REGISTRY) - real
        assert unknown == set(), f"registered under a name that is not a command: {unknown}"

    def test_the_replay_update_offers_everything_the_hub_commands_read_from_update(self):
        offered = {"message", "effective_chat", "effective_user", "effective_message"}
        problems = {}
        for name, fn in hub_resolver.HUB_COMMAND_REGISTRY.items():
            tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
            used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
                    and isinstance(n.value, ast.Name) and n.value.id == "update"}
            if used - offered:
                problems[name] = sorted(used - offered)
        assert problems == {}, f"read from update but missing on _ReplayUpdate: {problems}"

    async def test_the_free_group_dm_notice_names_the_real_command(self, db_path):
        bot = make_bot()
        chat = make_chat(chat_id=777, chat_type="private")
        msg = make_message(chat=chat)
        ctx = make_context(bot=bot)
        ctx.user_data["selected_hub_chat_id"] = HUB                 # a FREE group: no PRO row
        await handlers.listalias(make_update(chat=chat, user=make_user(user_id=1), message=msg), ctx)
        text = msg.reply_text.call_args.args[0].replace("\\", "")
        assert "/listaliases directly" in text and "/listalias directly" not in text
