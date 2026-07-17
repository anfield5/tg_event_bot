from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ChatMemberHandler,
    filters,
)
from config import TELEGRAM_TOKEN, logger
from db import init_db, track_user
from handlers import (
    help_command,
    newevent, editevent,
    notify,
    updateuser, listusers, refreshusers,
    shareevent,
    setalias, removealias, listalias,
    track_everyone_message,
    button_handler,
    global_text_router,
)


async def on_chat_member_update(update, context):
    """
    Automatically tracks users who join the group as 'active',
    and marks users who leave/are kicked as 'passive'.
    This powers /refreshusers without requiring manual /adduser.
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


def main():
    if not TELEGRAM_TOKEN:
        logger.error("BOT_TOKEN is required in environment variables!")
        return

    init_db()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # 1. Inline button callbacks (must be first)
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
    app.add_handler(CommandHandler("shareevent",   shareevent))

    # 4. Alias subsystem
    app.add_handler(CommandHandler("setalias",    setalias))
    app.add_handler(CommandHandler("removealias", removealias))
    app.add_handler(CommandHandler("listalias",   listalias))

    # 5. Text message router (extra player input + @everyone)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, global_text_router))

    logger.info("Bot started. Polling...")
    app.run_polling(allowed_updates=["message", "callback_query", "chat_member"])


if __name__ == "__main__":
    main()
