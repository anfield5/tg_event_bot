"""
Shared mock factory helpers used by conftest.py and test modules.
Extracted here so test files can do:   from tests.helpers import make_update, ...
(conftest.py is loaded by pytest automatically and cannot be imported directly.)
"""

from unittest.mock import AsyncMock, MagicMock


def make_user(user_id=111, username="testuser", first_name="Test"):
    """Returns a mock telegram.User."""
    u            = MagicMock()
    u.id         = user_id
    u.username   = username
    u.first_name = first_name
    return u


def make_chat(chat_id=-100123, chat_type="supergroup", title="TestGroup"):
    """Returns a mock telegram.Chat."""
    c       = MagicMock()
    c.id    = chat_id
    c.type  = chat_type
    c.title = title
    return c


def make_message(message_id=1, chat=None, user=None, text=""):
    """Returns a mock telegram.Message with an async reply_text."""
    msg            = MagicMock()
    msg.message_id = message_id
    msg.chat       = chat or make_chat()
    msg.from_user  = user or make_user()
    msg.text       = text
    msg.reply_text = AsyncMock(return_value=MagicMock(message_id=99))
    msg.delete     = AsyncMock()
    return msg


def make_bot(bot_id=777):
    """Returns a mock telegram.Bot with the most-used async methods pre-wired."""
    bot                          = MagicMock()
    bot.id                       = bot_id
    bot.send_message             = AsyncMock(return_value=MagicMock(message_id=99))
    bot.edit_message_text        = AsyncMock()
    bot.get_chat_member          = AsyncMock(return_value=MagicMock(status="administrator"))
    bot.get_chat                 = AsyncMock(return_value=MagicMock(title="TestChat", type="group"))
    # refreshusers() uses this to add missing chat administrators; default to
    # "no admins found" so tests that don't care about this feature aren't
    # affected by it.
    bot.get_chat_administrators  = AsyncMock(return_value=[])
    return bot


def make_callback_query(data, chat_id=-100123, user=None, message_id=1):
    """Returns a mock telegram.CallbackQuery for testing button_handler()."""
    cq              = MagicMock()
    cq.data         = data
    cq.from_user    = user or make_user()
    cq.message      = MagicMock()
    cq.message.chat_id   = chat_id
    cq.message.message_id = message_id
    cq.answer       = AsyncMock()
    return cq


def make_callback_update(data, chat_id=-100123, user=None, message_id=1):
    """Returns a mock telegram.Update carrying a callback_query - what button_handler() reads."""
    upd               = MagicMock()
    upd.callback_query = make_callback_query(data, chat_id=chat_id, user=user, message_id=message_id)
    return upd


def make_update(message=None, chat=None, user=None, text=""):
    """Returns a mock telegram.Update carrying a message."""
    upd                   = MagicMock()
    upd.effective_chat    = chat  or make_chat()
    upd.effective_user    = user  or make_user()
    upd.message           = message or make_message(
        chat=upd.effective_chat,
        user=upd.effective_user,
        text=text,
    )
    upd.effective_message = upd.message
    return upd


def make_context(bot=None, args=None, user_data=None):
    """Returns a mock telegram.ext.CallbackContext."""
    ctx                          = MagicMock()
    ctx.bot                      = bot  or make_bot()
    ctx.args                     = args or []
    ctx.user_data                = user_data if user_data is not None else {}
    ctx.application              = MagicMock()

    def _discard_task(coro):
        # Background tasks (schedule_view_refresh / update_all_shared_views)
        # are intentionally NOT run during a unit test that's only checking
        # one code path - but the coroutine object still needs to be closed,
        # or Python emits "coroutine was never awaited" warnings (and, worse,
        # a real un-run coroutine holding open resources).
        coro.close()
        return MagicMock()

    ctx.application.create_task  = MagicMock(side_effect=_discard_task)
    return ctx
