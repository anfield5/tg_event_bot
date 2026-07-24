from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ChatMemberHandler,
    filters,
)
from config import TELEGRAM_TOKEN, logger
from db import init_db, track_user, delete_tracked_user
from sheets import log_user_presence, mark_user_left
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


async def on_chat_member_update(update, context):
    """
    Automatically tracks users who join the group as 'active', and fully
    removes users who leave/are kicked - both from the local list
    (/listusers, main_group_users) and, in Google Sheets, from the "Users"
    tab (STATUS -> LEFT, DATE_end set for this place_id) plus a
    UserPresenceLog entry - all in real time, without waiting for a manual
    /refreshusers -r/-g run.
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
        # User left or was removed - drop them from /listusers entirely,
        # and mirror that in the Google Sheets Users/UserPresenceLog tabs.
        username = user.username or user.first_name or f"user{user.id}"
        delete_tracked_user(chat_id, user_id=str(user.id))
        logger.info(f"Removed @{username} from tracked users (left/kicked) in chat {chat_id}")
        try:
            await mark_user_left(chat_id, str(user.id))
        except Exception as e:
            logger.error(f"Failed to update Users/UserPresenceLog sheets for @{username} leaving chat {chat_id}: {e}")


def main():
    if not TELEGRAM_TOKEN:
        logger.error("BOT_TOKEN is required in environment variables!")
        return

    init_db()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

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

    # 5. Text message router (extra player input + @everyone)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, global_text_router))

    logger.info("Bot started. Polling...")
    app.run_polling(allowed_updates=["message", "callback_query", "chat_member"])


if __name__ == "__main__":
    main()
