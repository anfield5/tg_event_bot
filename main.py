from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ChatMemberHandler,
    filters,
)
from telegram.request import HTTPXRequest
from config import TELEGRAM_TOKEN, TELEGRAM_PROXY, BOT_VERSION, CONTROL_SHEET_ID, OWNER_USER_IDS, logger
from db import init_db, track_user, register_chat_added, register_chat_removed
from sheets import log_user_presence, sync_control_sheet_subconfig
from hub_resolver import hub_pick_callback_handler, start_command
from handlers import (
    help_command, help_callback_handler, help_back_handler, userid, chatid,
    newevent, editevent,
    notify,
    updateuser, listusers, refreshusers, adduser,
    shareevent,
    setalias, removealias, listalias,
    addmonitor, removemonitor, listmonitors,
    track_everyone_message,
    button_handler,
    global_text_router,
)
from subscription import setsub, setsheet, syncgroups, _push_control_sheet_main, _push_control_sheet_channels, FEATURE_MATRIX
from utils import now2ddmmyy


async def on_chat_member_update(update, context):
    """
    Automatically tracks users who join the group as 'active',
    and marks users who leave/are kicked as 'passive'.
    This powers /refreshusers without requiring manual /adduser.
    Also logs user presence to UserPresenceLog sheet.
    """
    result = update.chat_member
    if not result:
        return

    chat_id    = str(result.chat.id)
    new_member = result.new_chat_member
    user       = new_member.user

    if new_member.status in ["member", "administrator", "creator", "restricted"]:
        # User joined or was added
        username = user.username or user.first_name or f"user{user.id}"
        track_user(chat_id, username, "active", user_id=str(user.id))
        logger.info(f"Auto-tracked new member @{username} in chat {chat_id}")

    elif new_member.status in ["left", "kicked"]:
        # User left or was removed
        username = user.username or user.first_name or f"user{user.id}"
        track_user(chat_id, username, "passive", user_id=str(user.id))
        logger.info(f"Marked @{username} as passive (left/kicked) in chat {chat_id}")
        # UserPresenceLog will be updated by sync_users_sheet when status changes to LEFT


async def _sync_control_sheet_on_startup(application):
    """
    Runs once after the bot finishes initializing. If CONTROL_SHEET_ID is
    configured, pushes the current all_groups + all_channels + feature
    matrix to the Control Sheet right away - otherwise the sheet would stay
    empty until the first /setsub call or a manual /syncgroups.
    """
    if not CONTROL_SHEET_ID:
        return
    groups_ok    = await _push_control_sheet_main()
    channels_ok  = await _push_control_sheet_channels()
    subconfig_ok = await sync_control_sheet_subconfig(FEATURE_MATRIX)
    if groups_ok and channels_ok and subconfig_ok:
        logger.info("Control Sheet synced at startup (GROUPS + CHANNELS + SUB_CONFIG).")
    else:
        logger.error(
            f"Control Sheet startup sync incomplete - GROUPS: {'ok' if groups_ok else 'FAILED'}, "
            f"CHANNELS: {'ok' if channels_ok else 'FAILED'}, "
            f"SUB_CONFIG: {'ok' if subconfig_ok else 'FAILED'}. Check CONTROL_SHEET_ID, sharing "
            f"permissions, and that all three tabs exist with the exact names 'GROUPS', 'CHANNELS', and 'SUB_CONFIG'."
        )


async def on_my_chat_member_update(update, context):
    """
    Tracks the BOT'S OWN membership changes (added to / removed from a
    group or channel) - a DIFFERENT Telegram update type from regular user
    membership changes (see on_chat_member_update above, which only fires
    for OTHER users). Populates all_groups/all_channels the instant the bot
    joins a new chat (default type 'free' for groups), and moves that row
    into all_chats_bot_log with a removal timestamp the instant it's kicked
    or leaves. Also pushes the Control Sheet's GROUPS/CHANNELS tabs right
    away, so they never lag behind reality waiting for the next /setsub or
    bot restart.
    """
    result = update.my_chat_member
    if not result:
        return

    chat        = result.chat
    chat_id     = str(chat.id)
    old_status  = result.old_chat_member.status
    new_status  = result.new_chat_member.status

    was_present = old_status in ("member", "administrator", "creator")
    is_present  = new_status in ("member", "administrator", "creator")

    if is_present and not was_present:
        chat_name  = chat.title or chat.username or chat_id
        # A chat has a public @username iff it's discoverable/joinable by
        # anyone via that handle - the standard signal for "public" vs
        # "private" (invite-link-only) groups and channels.
        visibility = "public" if chat.username else "private"
        chat_type  = "channel" if chat.type == "channel" else "group"
        register_chat_added(chat_id, chat_name, chat_type, visibility, now2ddmmyy())
        logger.info(f"Bot added to {chat_type} {chat_id} ({chat_name}, {visibility})")
    elif was_present and not is_present:
        register_chat_removed(chat_id, now2ddmmyy())
        logger.info(f"Bot removed from chat {chat_id}")
    else:
        return  # neither an add nor a removal (e.g. restricted <-> member) - nothing to sync

    if not CONTROL_SHEET_ID:
        return
    try:
        await _push_control_sheet_main()
        await _push_control_sheet_channels()
    except Exception as e:
        logger.error(f"Control Sheet sync after bot membership change failed: {e}")


def main():
    if not TELEGRAM_TOKEN:
        logger.error("BOT_TOKEN is required in environment variables!")
        return

    init_db()

    proxy_kwargs = {}
    if TELEGRAM_PROXY:
        proxy_kwargs["proxy"] = TELEGRAM_PROXY
        logger.info("Using proxy for Telegram API requests.")

    # Two SEPARATE HTTPXRequest instances, each with its own connection
    # pool - get_updates holds one connection open for a long time (long-
    # polling waits for new updates), while every other call (sendMessage,
    # editMessageText, etc.) needs its own pool so it's never blocked
    # waiting on that long-lived connection. connection_pool_size is raised
    # above PTB's default for the general-purpose request object since this
    # bot edits several child-chat messages concurrently via asyncio.gather
    # (see update_all_shared_views) - a too-small pool here causes exactly
    # "Pool timeout: All connections in the connection pool are occupied."
    request = HTTPXRequest(
        connect_timeout=20.0, read_timeout=20.0,
        connection_pool_size=16,
        **proxy_kwargs,
    )
    get_updates_request = HTTPXRequest(
        connect_timeout=20.0, read_timeout=20.0,
        connection_pool_size=4,
        **proxy_kwargs,
    )

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .request(request)
        .get_updates_request(get_updates_request)
        .post_init(_sync_control_sheet_on_startup)
        .build()
    )

    # 1. Inline button callbacks - pattern-specific handlers MUST be
    # registered before the catch-all button_handler. PTB checks handlers in
    # registration order within the same group and stops at the first one
    # whose check passes; button_handler has no pattern (matches every
    # callback_query), so if it were registered first it would swallow
    # "help_*" callbacks before help_callback_handler/help_back_handler ever
    # ran - which is exactly why the /help inline buttons did nothing.
    #
    # help_back_handler ("^help_back$") must ALSO come before
    # help_callback_handler ("^help_"), since the broader "^help_" pattern
    # matches "help_back" too - registered in the other order, the Back
    # button would always fall through to help_callback_handler's "Unknown
    # section" fallback instead of returning to the main help menu.
    app.add_handler(CallbackQueryHandler(help_back_handler, pattern="^help_back$"))
    app.add_handler(CallbackQueryHandler(help_callback_handler, pattern="^help_"))
    app.add_handler(CallbackQueryHandler(hub_pick_callback_handler, pattern="^hubpick_"))
    app.add_handler(CallbackQueryHandler(button_handler))

    # 2. Chat member join/leave tracking
    app.add_handler(ChatMemberHandler(on_chat_member_update, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(on_my_chat_member_update, ChatMemberHandler.MY_CHAT_MEMBER))

    # 3. Core commands
    app.add_handler(CommandHandler("start",        start_command))
    app.add_handler(CommandHandler("help",         help_command))
    app.add_handler(CommandHandler("userid",       userid))
    app.add_handler(CommandHandler("chatid",       chatid))
    app.add_handler(CommandHandler("newevent",     newevent))
    app.add_handler(CommandHandler("editevent",    editevent))
    app.add_handler(CommandHandler("notify",       notify))
    app.add_handler(CommandHandler("updateuser",   updateuser))
    app.add_handler(CommandHandler("listusers",    listusers))
    app.add_handler(CommandHandler("refreshusers", refreshusers))
    app.add_handler(CommandHandler("adduser",      adduser))
    app.add_handler(CommandHandler("shareevent",   shareevent))

    # 4. Alias subsystem
    app.add_handler(CommandHandler("setalias",    setalias))
    app.add_handler(CommandHandler("removealias", removealias))
    app.add_handler(CommandHandler("listaliases",  listalias))

    # 5. Monitor subsystem
    app.add_handler(CommandHandler("addmonitor",    addmonitor))
    app.add_handler(CommandHandler("removemonitor", removemonitor))
    app.add_handler(CommandHandler("listmonitors",  listmonitors))

    # 6. Subscription control (owner-only, checked inside setsub itself)
    app.add_handler(CommandHandler("setsub", setsub))
    app.add_handler(CommandHandler("setsheet", setsheet))
    app.add_handler(CommandHandler("syncgroups", syncgroups))

    # 5. Text message router (extra player input + @everyone)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, global_text_router))

    logger.info(f"Bot v{BOT_VERSION} started. Polling...")
    logger.info(f"OWNER_USER_IDS configured: {OWNER_USER_IDS or '(empty - no owner-only commands will work)'}")
    app.run_polling(allowed_updates=["message", "callback_query", "chat_member"])


if __name__ == "__main__":
    main()
