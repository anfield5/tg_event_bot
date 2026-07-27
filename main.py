from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ChatMemberHandler,
    filters,
)
from telegram.request import HTTPXRequest
from config import TELEGRAM_TOKEN, TELEGRAM_PROXY, BOT_VERSION, CONTROL_SHEET_ID, logger
from db import init_db, track_user
from sheets import log_user_presence, sync_control_sheet_subconfig
from handlers import (
    help_command, help_callback_handler, help_back_handler,
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
from subscription import setsub, syncgroups, _push_control_sheet_main, FEATURE_MATRIX


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
    configured, pushes the current main_chat_settings + feature matrix to
    the Control Sheet right away - otherwise the sheet would stay empty
    until the first /setsub call or a manual /syncgroups.
    """
    if not CONTROL_SHEET_ID:
        return
    try:
        await _push_control_sheet_main()
        await sync_control_sheet_subconfig(FEATURE_MATRIX)
        logger.info("Control Sheet synced at startup.")
    except Exception as e:
        logger.error(f"Control Sheet startup sync failed: {e}")


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
    app.add_handler(CallbackQueryHandler(button_handler))

    # 2. Chat member join/leave tracking
    app.add_handler(ChatMemberHandler(on_chat_member_update, ChatMemberHandler.CHAT_MEMBER))

    # 3. Core commands
    app.add_handler(CommandHandler("help",         help_command))
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
    app.add_handler(CommandHandler("syncgroups", syncgroups))

    # 5. Text message router (extra player input + @everyone)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, global_text_router))

    logger.info(f"Bot v{BOT_VERSION} started. Polling...")
    app.run_polling(allowed_updates=["message", "callback_query", "chat_member"])


if __name__ == "__main__":
    main()
