"""
Lets EVERY group-scoped command work when sent as a direct message to the
bot, not just from inside the group chat itself - and, once a group is
picked in a DM, "sticks" to that group for every following DM command
until the user explicitly switches (see /switchgroup below), instead of
asking "which group?" every single time.

How it works:
  1. resolve_hub_chat_id() is the first thing each supported command calls.
     From a group chat, it just returns that chat's own ID immediately -
     zero change from how these commands worked before this feature
     existed. The sticky-selection mechanism below never applies inside a
     group at all.
  2. From a private chat (DM):
       - If a group is already "selected" for this conversation
         (context.user_data["selected_hub_chat_id"]), it's used
         immediately - no lookup, no picker, no repeated questions.
       - Otherwise, every group the bot is currently in (all_groups UNIONed
         with the older main_group_users, which catches groups added
         before all_groups existed) is checked with a LIVE
         get_chat_member call - not a cached/assumed status - to see if
         the person messaging the bot is a real admin there.
           - No matches: told plainly, nothing else happens.
           - Exactly one match: used immediately AND remembered as the
             selected group for next time.
           - Multiple matches: an inline keyboard listing the group names
             is shown, and the original command + its arguments are
             stashed in context.user_data so they can be replayed once a
             group is picked - the pick is then also remembered.
  3. hub_pick_callback_handler() handles that keyboard's button taps - it
     looks up which underlying command was pending, restores its
     arguments, remembers the chosen group as the new selection, and calls
     the command again with the chosen group's ID.
  4. /switchgroup clears the current selection and shows the picker again,
     the one deliberate action that returns to "which group?" mode.

Extending this to more commands: add the command's name -> function to
HUB_COMMAND_REGISTRY, and change that command to call
resolve_hub_chat_id(update, context, "commandname", override_chat_id) at
the top instead of reading update.effective_chat.id directly.
"""

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from db import get_connection
from utils import escape_markdown

# Filled in by aliases.py, handlers.py, monitors.py, subscription.py (and
# whichever other modules opt into this) at import time - kept here rather
# than imported directly to avoid a circular import.
HUB_COMMAND_REGISTRY = {}


def register_hub_command(name):
    """Decorator: registers a command function under `name` for replay
    after a DM group-picker selection. Does not change the function itself
    at all - just records it for hub_pick_callback_handler to find later."""
    def _wrap(fn):
        HUB_COMMAND_REGISTRY[name] = fn
        return fn
    return _wrap


async def _get_known_candidate_chats(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """
    Returns [(chat_id, display_name), ...] for every chat_id the bot has ANY
    record of (from all_groups, populated going forward by the
    MY_CHAT_MEMBER handler, UNIONed with main_group_users, which has
    existed much longer and is populated by the older per-user
    ChatMemberHandler plus most commands) where `user_id` is a real,
    currently-verified admin.

    all_groups alone would miss every group the bot was added to BEFORE
    that table existed - main_group_users is the fallback that catches
    those, since it's been populated since v2.0.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id, chat_name FROM all_groups")
        from_all_groups = {str(cid): name for cid, name in cursor.fetchall()}
        cursor.execute("SELECT DISTINCT chat_id FROM main_group_users")
        from_main_group_users = {str(row[0]) for row in cursor.fetchall()}

    all_chat_ids = set(from_all_groups) | from_main_group_users

    admin_of = []
    for candidate_chat_id in all_chat_ids:
        try:
            member = await context.bot.get_chat_member(int(candidate_chat_id), user_id)
            if member.status in ("administrator", "creator"):
                display_name = from_all_groups.get(candidate_chat_id)
                if not display_name:
                    try:
                        chat_obj = await context.bot.get_chat(int(candidate_chat_id))
                        display_name = chat_obj.title or chat_obj.username or candidate_chat_id
                    except Exception:
                        display_name = candidate_chat_id
                admin_of.append((candidate_chat_id, display_name))
        except Exception:
            # Bot may have been removed from that chat since it was last
            # seen, or the chat_id is stale/inaccessible - skip it rather
            # than failing the whole lookup over one bad candidate.
            continue

    return admin_of


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start - only meaningful in a private chat with the bot. Lists every
    group the user administers (where the bot is already present), so they
    immediately know what they can run DM commands for - or tells them
    plainly if they're not an admin anywhere yet.
    """
    chat = update.effective_chat
    if chat.type != "private":
        return

    admin_of = await _get_known_candidate_chats(context, update.effective_user.id)

    if not admin_of:
        await update.message.reply_text(
            "👋 Hi\\! I don't see you as an admin of any group I'm currently in\\.\n\n"
            "Add me to a group and make yourself an admin there to start using event commands "
            "\\(from the group itself, or right here in this DM\\)\\.",
            parse_mode="MarkdownV2",
        )
        return

    if len(admin_of) == 1:
        context.user_data["selected_hub_chat_id"] = admin_of[0][0]
        await update.message.reply_text(
            f"👋 Hi\\! You're an admin of 1 group I'm in: {escape_markdown(admin_of[0][1])}\\.\n\n"
            f"You can run commands like /newevent, /listusers, etc\\(check /help for more\\) "
            f"right here in this DM\\.",
            parse_mode="MarkdownV2",
        )
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(name, callback_data=f"switchpick_{chat_id}")]
        for chat_id, name in admin_of
    ])
    await update.message.reply_text(
        f"👋 Hi\\! You're an admin of {len(admin_of)} groups I'm in \\- which one would you like to work with?\n\n"
        f"You can run commands like /newevent, /listusers, etc\\(check /help for more\\) "
        f"right here in this DM once you pick one\\.",
        reply_markup=keyboard,
        parse_mode="MarkdownV2",
    )


async def switchgroup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /switchgroup - the one deliberate action that clears whichever group is
    currently "stuck" for this DM conversation (see resolve_hub_chat_id)
    and shows the picker again, so the next DM command can target a
    different group.
    """
    chat = update.effective_chat
    if chat.type != "private":
        return

    context.user_data.pop("selected_hub_chat_id", None)

    admin_of = await _get_known_candidate_chats(context, update.effective_user.id)
    if not admin_of:
        await update.message.reply_text(
            "You're not an admin of any group I'm currently in\\.", parse_mode="MarkdownV2"
        )
        return

    if len(admin_of) == 1:
        context.user_data["selected_hub_chat_id"] = admin_of[0][0]
        await update.message.reply_text(
            f"You're only an admin of one group I'm in \\({escape_markdown(admin_of[0][1])}\\), "
            f"so that's still the one selected\\.",
            parse_mode="MarkdownV2",
        )
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(name, callback_data=f"switchpick_{chat_id}")]
        for chat_id, name in admin_of
    ])
    await update.message.reply_text(
        "Which group would you like to switch to?", reply_markup=keyboard, parse_mode="MarkdownV2"
    )


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

    # Sticky selection: once a group has been picked (or auto-detected as
    # the only option) for this DM conversation, every following DM command
    # uses it directly - no repeated lookups, no repeated questions - until
    # /switchgroup is used.
    selected = context.user_data.get("selected_hub_chat_id")
    if selected is not None:
        return str(selected)

    user_id = update.effective_user.id
    admin_of = await _get_known_candidate_chats(context, user_id)

    if not admin_of:
        await update.message.reply_text(
            "You're not an admin of any group I'm currently in\\. Add me to a group first, "
            "or run this command directly inside the group instead\\.",
            parse_mode="MarkdownV2",
        )
        return None

    if len(admin_of) == 1:
        context.user_data["selected_hub_chat_id"] = admin_of[0][0]
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


class _ReplayUpdate:
    """
    A minimal stand-in for a real Update, used only to replay a hub command
    after the DM group-picker. The real update.message is None for a
    callback-query-only update (the actual message lives at
    update.callback_query.message instead), and Update/CallbackContext
    objects may be immutable - so rather than risk mutating framework
    objects, this just exposes exactly what the replayed command functions
    read: .message (for reply_text), .effective_chat, .effective_user.
    """
    def __init__(self, message, chat, user):
        self.message = message
        self.effective_chat = chat
        self.effective_user = user
        self.effective_message = message


class _ReplayContext:
    """Same idea as _ReplayUpdate, but for CallbackContext - only .bot,
    .args, and .user_data are ever read by the replayed commands."""
    def __init__(self, bot, args, user_data):
        self.bot = bot
        self.args = args
        self.user_data = user_data


async def hub_pick_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles a tap on either picker keyboard:
      - "hubpick_<id>"    - from resolve_hub_chat_id, replays the pending
                            command for the chosen group.
      - "switchpick_<id>" - from /switchgroup, just sets the new selection,
                            nothing to replay.
    """
    query = update.callback_query
    await query.answer()

    action, chosen_chat_id = query.data.split("_", 1)

    if action == "switchpick":
        context.user_data["selected_hub_chat_id"] = chosen_chat_id
        await query.edit_message_text(
            "✅ Switched \\- your next commands will target that group\\.", parse_mode="MarkdownV2"
        )
        return

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

    context.user_data["selected_hub_chat_id"] = chosen_chat_id
    await query.edit_message_text("Got it \\- running that now\\.\\.\\.", parse_mode="MarkdownV2")

    replay_update = _ReplayUpdate(message=query.message, chat=query.message.chat, user=query.from_user)
    replay_context = _ReplayContext(bot=context.bot, args=pending["args"], user_data=context.user_data)
    await handler_fn(replay_update, replay_context, override_chat_id=chosen_chat_id)
