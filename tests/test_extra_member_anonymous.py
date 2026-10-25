"""
Add Extra Member for an admin who has "Remain anonymous" on in the group.

The button click is a callback query and carries the REAL admin. Their typed answer arrives from
GroupAnonymousBot, which has its own (empty) python-telegram-bot user_data - so the prompt stored at click time
used to be invisible and the typed name was silently ignored. Click and answer below get separate user_data but a
shared chat_data, exactly like PTB does it.
"""
import sqlite3
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, ".")
import event_engine
import handlers
import utils
from utils import GROUP_ANONYMOUS_BOT_ID
from tests.helpers import make_bot, make_chat, make_context, make_message, make_update, make_user
from tests.test_handlers_async import _make_fake_button_query

HUB = "-100"


def message_update(user_id, text="Andr", *, anonymous=False, sender_chat=None, username="someone"):
    chat = make_chat(chat_id=int(HUB), chat_type="supergroup")
    msg = make_message(chat=chat)
    msg.text, msg.delete = text, AsyncMock()
    msg.sender_chat = chat if anonymous else sender_chat
    if anonymous:
        user_id, username = GROUP_ANONYMOUS_BOT_ID, "GroupAnonymousBot"
    return make_update(chat=chat, user=make_user(user_id=user_id, username=username), message=msg)


def context_with(chat_data):
    ctx = make_context(bot=make_bot())
    ctx.chat_data = chat_data
    return ctx


class TestPromptStore:
    def test_the_click_remembers_the_prompt_for_the_clicker_and_for_the_chat(self):
        ctx = context_with({})
        utils.remember_extra_player_prompt(ctx, "ev1", make_user(user_id=42, username="realadmin"))
        assert ctx.user_data["awaiting_extra_player_for"] == "ev1"
        pending = ctx.chat_data["awaiting_extra_player"]
        assert (pending["event_id"], pending["actor_id"], pending["actor_name"]) == ("ev1", 42, "realadmin")

    def test_a_context_without_a_usable_chat_store_still_works_for_normal_typing(self):
        ctx = make_context()                       # chat_data is just a MagicMock attribute here
        utils.remember_extra_player_prompt(ctx, "ev1", make_user(user_id=42))
        assert utils.has_extra_player_prompt(message_update(42), ctx)
        assert utils.pop_extra_player_prompt(message_update(42), ctx)["event_id"] == "ev1"

    def test_typing_as_yourself_uses_your_own_prompt_and_spends_the_chat_copy(self):
        ctx = context_with({})
        utils.remember_extra_player_prompt(ctx, "ev1", make_user(user_id=42))
        got = utils.pop_extra_player_prompt(message_update(42), ctx)
        assert got == {"event_id": "ev1", "actor_id": None, "actor_name": None}
        assert ctx.chat_data == {} and ctx.user_data == {}

    def test_an_anonymous_post_answers_the_chat_prompt_and_learns_who_really_clicked(self):
        click_ctx = context_with({})
        utils.remember_extra_player_prompt(click_ctx, "ev1", make_user(user_id=42, username="realadmin"))
        anon_ctx = context_with(click_ctx.chat_data)             # other user_data, same chat_data
        upd = message_update(0, anonymous=True)
        assert utils.has_extra_player_prompt(upd, anon_ctx)
        got = utils.pop_extra_player_prompt(upd, anon_ctx)
        assert got == {"event_id": "ev1", "actor_id": 42, "actor_name": "realadmin"}
        assert click_ctx.chat_data == {}

    def test_it_is_consumed_only_once(self):
        click_ctx = context_with({})
        utils.remember_extra_player_prompt(click_ctx, "ev1", make_user(user_id=42))
        anon_ctx = context_with(click_ctx.chat_data)
        assert utils.pop_extra_player_prompt(message_update(0, anonymous=True), anon_ctx) is not None
        assert utils.pop_extra_player_prompt(message_update(0, anonymous=True), anon_ctx) is None

    def test_an_ordinary_members_message_can_neither_see_nor_consume_it(self):
        click_ctx = context_with({})
        utils.remember_extra_player_prompt(click_ctx, "ev1", make_user(user_id=42))
        member_ctx = context_with(click_ctx.chat_data)
        upd = message_update(77)
        assert not utils.has_extra_player_prompt(upd, member_ctx)
        assert utils.pop_extra_player_prompt(upd, member_ctx) is None
        assert "awaiting_extra_player" in click_ctx.chat_data

    def test_another_admin_typing_as_themselves_leaves_it_alone(self):
        click_ctx = context_with({})
        utils.remember_extra_player_prompt(click_ctx, "ev1", make_user(user_id=42))
        other_admin_ctx = context_with(click_ctx.chat_data)
        assert utils.pop_extra_player_prompt(message_update(43), other_admin_ctx) is None
        assert "awaiting_extra_player" in click_ctx.chat_data

    def test_the_chat_prompt_expires(self):
        click_ctx = context_with({})
        utils.remember_extra_player_prompt(click_ctx, "ev1", make_user(user_id=42))
        click_ctx.chat_data["awaiting_extra_player"]["ts"] -= utils.EXTRA_PLAYER_PROMPT_TTL + 1
        anon_ctx = context_with(click_ctx.chat_data)
        assert not utils.has_extra_player_prompt(message_update(0, anonymous=True), anon_ctx)
        assert click_ctx.chat_data == {}, "an expired prompt is cleaned up"

    def test_a_post_whose_sender_chat_is_the_group_counts_as_anonymous_even_without_the_bot_id(self):
        chat = make_chat(chat_id=int(HUB), chat_type="supergroup")
        msg = make_message(chat=chat)
        msg.sender_chat = chat
        assert utils.is_anonymous_admin_post(make_update(chat=chat, user=make_user(user_id=5), message=msg))

    def test_a_linked_channels_post_is_not_an_anonymous_admin(self):
        other = make_chat(chat_id=-999, chat_type="channel")
        assert not utils.is_anonymous_admin_post(message_update(5, sender_chat=other))

    def test_a_plain_message_is_not_anonymous(self):
        assert not utils.is_anonymous_admin_post(message_update(5))


def seed(db_path):
    """An event in verification mode, shared to a child chat where Andr is a tracked member."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon, event_status, "
        "going_data, notgoing_data, counters_data, kicked_data) VALUES ('ev1','-100','1','Party','👍','❌',1,'[]','[]','{}','[]')"
    )
    conn.execute("INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type) VALUES ('ev1','-200','5','-visible','channel')")
    conn.execute("INSERT INTO main_group_users (chat_id, username, user_id, first_name, last_name) VALUES ('-200','Andr','555','Andrew','Smith')")
    conn.commit()
    conn.close()


async def press_add_extra_member(chat_data, actor_id=42, actor_name="realadmin"):
    bot = make_bot()
    bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
    query = _make_fake_button_query("addext_ev1", HUB, actor_id, actor_name)
    query.message.reply_text = AsyncMock()
    upd = MagicMock()
    upd.callback_query = query
    upd.effective_user = MagicMock(id=actor_id)
    ctx = make_context(bot=bot)
    ctx.chat_data = chat_data
    with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock, return_value=None):
        await event_engine.button_handler(upd, ctx)
    return ctx


class FakeActions:
    def __init__(self):
        self.rows = []

    async def append_row(self, row):
        self.rows.append(row)


class FakeBook:
    def __init__(self, ws):
        self.ws = ws

    async def worksheet(self, name):
        return self.ws


async def say(upd, chat_data, *, ctx=None, actions=None):
    """Sends a message through the real text router."""
    if ctx is None:
        ctx = context_with(chat_data)
    ctx.bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
    ctx.bot.send_message = AsyncMock()
    book = FakeBook(actions) if actions is not None else None
    with patch("handlers.get_sheet_for_chat", new_callable=AsyncMock, return_value="sheet" if actions is not None else None), \
         patch("handlers.open_spreadsheet", new_callable=AsyncMock, return_value=book), \
         patch("handlers.schedule_view_refresh", new_callable=AsyncMock), \
         patch("handlers.track_everyone_message", new_callable=AsyncMock) as chatter:
        await handlers.global_text_router(upd, ctx)
    return chatter


def event_users(db_path):
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT chat_id, user_id, username, status FROM event_users WHERE event_id = 'ev1'").fetchall()
    conn.close()
    return rows


class TestTheRealFlow:
    async def test_an_anonymous_admin_can_add_a_member_of_a_shared_chat(self, db_path):
        """The reported case: the event is shared to the child chat, the admin is anonymous in the hub."""
        seed(db_path)
        chat_data = {}
        click_ctx = await press_add_extra_member(chat_data)
        assert click_ctx.user_data["awaiting_extra_player_for"] == "ev1"

        actions = FakeActions()
        chatter = await say(message_update(0, anonymous=True), chat_data, actions=actions)

        assert event_users(db_path) == [("-200", "555", "Andr", "going")], "recorded under the child chat's own list"
        chatter.assert_not_called()
        assert actions.rows[0][1:4] == ["ADD_EXTRA_PLAYER", "realadmin", "42"], "logs who really clicked, not the anonymous bot"
        assert chat_data == {}

    async def test_a_normal_admin_still_works_and_spends_the_chat_prompt(self, db_path):
        seed(db_path)
        chat_data = {}
        click_ctx = await press_add_extra_member(chat_data)
        actions = FakeActions()
        await say(message_update(42, username="realadmin"), chat_data, ctx=click_ctx, actions=actions)
        assert event_users(db_path) == [("-200", "555", "Andr", "going")]
        assert actions.rows[0][2:4] == ["realadmin", "42"]
        assert chat_data == {}

    async def test_an_ordinary_members_message_is_not_taken_as_the_username(self, db_path):
        seed(db_path)
        chat_data = {}
        await press_add_extra_member(chat_data)
        chatter = await say(message_update(77, username="chatty"), chat_data)
        assert event_users(db_path) == []
        chatter.assert_awaited_once()
        assert "awaiting_extra_player" in chat_data, "the admin's prompt is still waiting"

    async def test_an_anonymous_message_with_nothing_pending_is_just_chat(self, db_path):
        seed(db_path)
        chatter = await say(message_update(0, anonymous=True), {})
        assert event_users(db_path) == []
        chatter.assert_awaited_once()

    async def test_an_expired_prompt_is_ignored(self, db_path):
        seed(db_path)
        chat_data = {}
        await press_add_extra_member(chat_data)
        chat_data["awaiting_extra_player"]["ts"] -= utils.EXTRA_PLAYER_PROMPT_TTL + 1
        chatter = await say(message_update(0, anonymous=True), chat_data)
        assert event_users(db_path) == []
        chatter.assert_awaited_once()

    async def test_only_the_first_anonymous_message_is_taken(self, db_path):
        seed(db_path)
        chat_data = {}
        await press_add_extra_member(chat_data)
        await say(message_update(0, anonymous=True), chat_data)
        chatter = await say(message_update(0, "hello", anonymous=True), chat_data)
        assert len(event_users(db_path)) == 1
        chatter.assert_awaited_once()

    async def test_a_non_admin_cannot_start_the_prompt_so_nothing_is_waiting(self, db_path):
        seed(db_path)
        chat_data = {}
        bot = make_bot()
        bot.get_chat_member = AsyncMock(return_value=MagicMock(status="member"))
        query = _make_fake_button_query("addext_ev1", HUB, 99, "random")
        query.message.reply_text = AsyncMock()
        upd = MagicMock()
        upd.callback_query = query
        upd.effective_user = MagicMock(id=99)
        ctx = make_context(bot=bot)
        ctx.chat_data = chat_data
        with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock, return_value=None):
            await event_engine.button_handler(upd, ctx)
        assert chat_data == {} and ctx.user_data == {}
