import logging
import os
import uuid
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# ENV variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Set up Google Sheets client
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name("google-credentials.json", scope)
client = gspread.authorize(creds)

events_sheet = client.open(GOOGLE_SHEET_NAME).worksheet("Events")
actions_sheet = client.open(GOOGLE_SHEET_NAME).worksheet("EventActions")

# In-memory event state (not persistent across restarts)
event_data = {}

def get_display_text(event_id):
    data = event_data.get(event_id, {"name": "", "going": [], "not_going": []})
    text = f"📅 *{data['name']}*\n\n✅ *Going*:\n"
    text += "\n".join([f"• {name}" for name in data["going"]]) or "Nobody yet"
    text += "\n\n❌ *Not going*:\n"
    text += "\n".join([f"• {name}" for name in data["not_going"]]) or "Nobody yet"
    return text

async def start_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /event <event name>")
        return

    event_name = " ".join(context.args)
    event_id = str(uuid.uuid4())

    event_data[event_id] = {
        "name": event_name,
        "going": [],
        "not_going": [],
        "message_id": None
    }

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    events_sheet.append_row([event_id, now, 0, 0])

    buttons = [
        [InlineKeyboardButton("✅ Going", callback_data=f"{event_id}:going"),
         InlineKeyboardButton("❌ Not going", callback_data=f"{event_id}:notgoing")],
        [InlineKeyboardButton("🔒 Close event", callback_data=f"{event_id}:close"),
         InlineKeyboardButton("🔓 Open event", callback_data=f"{event_id}:open")]
    ]

    reply_markup = InlineKeyboardMarkup(buttons)
    message = await update.message.reply_markdown(get_display_text(event_id), reply_markup=reply_markup)
    event_data[event_id]["message_id"] = message.message_id

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    user_display = user.username or user.first_name or "Unknown"

    event_id, action = query.data.split(":")
    data = event_data.get(event_id)

    if not data:
        await query.edit_message_text("⚠️ This event is no longer active.")
        return

    if action in ["going", "notgoing"]:
        if action == "going":
            if user_display not in data["going"]:
                data["going"].append(user_display)
            if user_display in data["not_going"]:
                data["not_going"].remove(user_display)
        elif action == "notgoing":
            if user_display not in data["not_going"]:
                data["not_going"].append(user_display)
            if user_display in data["going"]:
                data["going"].remove(user_display)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        actions_sheet.append_row([
            event_id,
            now,
            user.username or "",
            user.id,
            action
        ])

        # Update counts in Events sheet
        try:
            cell = events_sheet.find(event_id)
            row = cell.row
            events_sheet.update_cell(row, 3, len(data["going"]))
            events_sheet.update_cell(row, 4, len(data["not_going"]))
        except Exception as e:
            logger.error(f"Error updating count in Events sheet: {e}")

        await query.edit_message_text(
            get_display_text(event_id),
            reply_markup=query.message.reply_markup,
            parse_mode="Markdown"
        )

    elif action == "close":
        await query.edit_message_reply_markup(None)
    elif action == "open":
        # Optional: Re-enable buttons (not implemented)
        pass

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("event", start_event))
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
