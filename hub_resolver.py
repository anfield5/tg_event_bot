"""
Lets group-configuration commands (currently: /setalias, /removealias,
/listalias - see HUB_COMMAND_REGISTRY below) work when sent as a direct
message to the bot, not just from inside the group chat itself.

How it works:
  1. resolve_hub_chat_id() is the first thing each supported command calls.
     From a group chat, it just returns that chat's own ID immediately -
     zero change from how these commands worked before this feature
     existed.
  2. From a private chat (DM), it looks across every group the bot is
     currently in (all_groups) and checks - with a LIVE get_chat_member
     call, not a cached/assumed status - whether the person messaging the
     bot is a real admin there.
       - No matches: told plainly, nothing else happens.
       - Exactly one match: used immediately, no extra step.
       - Multiple matches: an inline keyboard listing the group names is
         shown, and the original command + its arguments are stashed in
         context.user_data so they can be replayed once a group is picked.
  3. hub_pick_callback_handler() handles that keyboard's button clicks -
     it looks up which underlying command was pending, restores its
     arguments, and calls it again with the chosen group's ID.

Extending this to more commands: add the command's name -> function to
HUB_COMMAND_REGISTRY, and change that command to call
resolve_hub_chat_id(update, context, "commandname", override_chat_id) at
the top instead of reading update.effective_chat.id directly.
"""

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from db import get_connection

# Filled in by aliases.py (and whichever other modules opt into this) at
# import time - kept here rather than imported directly to avoid a circular
# import (aliases.py needs to import resolve_hub_chat_id from this module).
HUB_COMMAND_REGISTRY = {}


def register_hub_command(name):
    """Decorator: registers a command function under `name` for replay
    after a DM group-picker selection. Does not change the function itself
    at all - just records it for hub_pick_callback_handler to find later."""
    def _wrap(fn):
        HUB_COMMAND_REGISTRY[name] = fn
        return fn
    return _wrap


async def resolve_hub_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE,
                               command_name: str, override_chat_id: str = None):
    """
    Returns the hub chat_id (str) the calling command should operate on, or
    None if the caller should just `return` (either an error was already
    shown to the user, or a group-picker was shown and we're now waiting on
    their choice - which will re-invoke the command with override_chat_id
    set, skipping straight past this whole DM-resolution dance).
    """
    if override_chat_id is not None:
        return str(override_chat_id)

    chat = update.effective_chat
    if chat.type != "private":
        return str(chat.id)

    user_id = update.effective_user.id

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id, chat_name FROM all_groups")
        candidates = cursor.fetchall()

    admin_of = []
    for candidate_chat_id, candidate_chat_name in candidates:
        try:
            member = await context.bot.get_chat_member(int(candidate_chat_id), user_id)
            if member.status in ("administrator", "creator"):
                admin_of.append((candidate_chat_id, candidate_chat_name or candidate_chat_id))
        except Exception:
            # Bot may have been removed from that chat since it was last
            # registered, or the chat_id is stale/inaccessible - skip it
            # rather than failing the whole lookup over one bad candidate.
            continue

    if not admin_of:
        await update.message.reply_text(
            "You're not an admin of any group I'm currently in\\. Add me to a group first, "
            "or run this command directly inside the group instead\\.",
            parse_mode="MarkdownV2",
        )
        return None

    if len(admin_of) == 1:
        return admin_of[0][0]

    context.user_data["pending_hub_command"] = {
        "command": command_name,
        "args": list(context.args) if context.args else [],
    }
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(name, callback_data=f"hubpick_{chat_id}")]
        for chat_id, name in admin_of
    ])
    await update.message.reply_text(
        "You're an admin of more than one group I'm in \\- which one is this for?",
        reply_markup=keyboard,
        parse_mode="MarkdownV2",
    )
    return None


async def hub_pick_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles a tap on the group-picker keyboard shown by resolve_hub_chat_id()."""
    query = update.callback_query
    await query.answer()

    chosen_chat_id = query.data.split("_", 1)[1]
    pending = context.user_data.pop("pending_hub_command", None)
    if not pending:
        await query.edit_message_text(
            "This selection has expired \\- please re\\-run the command\\.", parse_mode="MarkdownV2"
        )
        return

    handler_fn = HUB_COMMAND_REGISTRY.get(pending["command"])
    if not handler_fn:
        await query.edit_message_text(
            "Something went wrong \\- please re\\-run the command\\.", parse_mode="MarkdownV2"
        )
        return

    context.args = pending["args"]
    await query.edit_message_text("Got it \\- running that now\\.\\.\\.", parse_mode="MarkdownV2")
    await handler_fn(update, context, override_chat_id=chosen_chat_id)
