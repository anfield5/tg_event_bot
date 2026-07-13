from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, MessageHandler, filters
from config import TELEGRAM_TOKEN, logger
from db import init_db
from handlers import (
    help_command, 
    newevent, editevent, 
    notify, adduser, updateuser, listusers, 
    shareevent, setalias, removealias, listalias,
    track_everyone_message, 
    button_handler,
    global_text_router
)

def main():
    # Verify presence of essential environment authentication parameters
    if not TELEGRAM_TOKEN:
        logger.error("BOT_TOKEN is strictly required in environment variables!")
        return
        
    # Standard DB initialization pipeline execution and automatic migration guard
    init_db()
    
    # Initialize application wrapper instance via modern Telegram API factory patterns
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    # 1. CRITICAL: Dynamic callback keyboard buttons routing link configuration must be first
    app.add_handler(CallbackQueryHandler(button_handler))
    
    # 2. Core command endpoints routing setup
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("newevent", newevent))
    app.add_handler(CommandHandler("editevent", editevent))
    app.add_handler(CommandHandler("notify", notify))
    app.add_handler(CommandHandler("adduser", adduser))
    app.add_handler(CommandHandler("updateuser", updateuser))
    app.add_handler(CommandHandler("listusers", listusers))
    app.add_handler(CommandHandler("shareevent", shareevent))
    
    # 3. Dynamic layout alias routing subsystem endpoints
    app.add_handler(CommandHandler("setalias", setalias))
    app.add_handler(CommandHandler("removealias", removealias))
    app.add_handler(CommandHandler("listalias", listalias))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, global_text_router))
    
    logger.info("Modular configuration deployed. Bot execution polling active...")
    

    app.run_polling()

if __name__ == "__main__":
    main()