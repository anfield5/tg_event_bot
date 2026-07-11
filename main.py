from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from config import TELEGRAM_TOKEN, logger
from db import init_db
from handlers import (
    help_command, newevent, editevent, notify, adduser, 
    updateuser, listusers, track_everyone_message, button_handler
)

def main():
    if not TELEGRAM_TOKEN:
        logger.error("BOT_TOKEN is strictly required in environments variables!")
        return
        
    # Standard DB initialization pipeline execution
    init_db()
    
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    # Global messages parser configuration tracking routing logic
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, track_everyone_message))
    
    # Core command endpoints routing setup
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("newevent", newevent))
    app.add_handler(CommandHandler("editevent", editevent))
    app.add_handler(CommandHandler("notify", notify))
    app.add_handler(CommandHandler("adduser", adduser))
    app.add_handler(CommandHandler("updateuser", updateuser))
    app.add_handler(CommandHandler("listusers", listusers))
    
    # Dynamic callback keyboard buttons routing link configuration
    app.add_handler(CallbackQueryHandler(button_handler))
    
    logger.info("Modular configuration deployed. Bot execution polling active...")
    app.run_polling()

if __name__ == "__main__":
    main()