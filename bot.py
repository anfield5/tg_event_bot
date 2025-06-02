import logging
import os
from datetime import datetime
from uuid import uuid4

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, CommandHandler, ContextTypes

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv

load_dotenv()

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# Load environment variables
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON_PATH")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")

# Google Sheets setup
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
credentials = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS_JSON, scope)
client = gspread.authorize(credentials)

# Open sheets
events_sheet = client.open(GOOGLE_SHEET_NAME).worksheet("Events")
actions_sheet = client.open(GOOGLE_SHEET_NAME).worksheet("EventActions")

# In-memory event storage: event_id -> data
events_data = {}


def create_event_keyboard(event_id, going, not_going, is_open):
    # Disable buttons logic
    # If is_open = True: Open Event button disabled, Close Event enabled
    # If is_open = False: Open Event enabled, Close Event disabled

    buttons = [
        [
            InlineKeyboardButton(
                "✅ Going", callback_data=f"going_{event_id}"
            ),
            InlineKeyboardButton(
                "❌ Not going", callback_data=f"notgoing_{event_id}"
            ),
        ],
        [
            InlineKeyboardButton(
                "🟢 Open Event",
                callback_data=f"open_{event_id}" if not is_open else "noop",
            ),
            InlineKeyboardButton(
                "🔴 Close Event",
                callback_data=f"close_{event_id}" if is_open else "noop",
            ),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


async def add_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /add_event Event Name
    if not context.args:
        await update.message.reply_text("Please provide event name after command.")
        return

    event_name = " ".join(context.args)
    event_id = str(uuid4())[:8]
    is_open = True
    going = set()
    not_going = set()

    # Store event data in memory
    events_data[event_id] = {
        "name": event_name,
        "going": going,
        "not_going": not_going,
        "is_open": is_open,
        "message_id": None,
        "chat_id": update.effective_chat.id,
    }

    keyboard = create_event_keyboard(event_id, going, not_going, is_open)

    message = await update.message.reply_text(
        f"📅 *{event_name}*\n\n"
        f"🟢 *Going* (0):\n"
        f"❌ *Not going* (0):\n",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )

    # Save message id to update later
    events_data[event_id]["message_id"] = message.message_id

    # Add row to Google Sheet "Events"
    now_str = datetime.now().isoformat()
    events_sheet.append_row(
        [event_id, event_name, now_str, 0, 0]
    )


async def update_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # /update_event event_id new event name
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /update_event <event_id> <new event name>")
        return

    event_id = context.args[0]
    new_name = " ".join(context.args[1:])

    if event_id not in events_data:
        await update.message.reply_text("Event ID not found.")
        return

    events_data[event_id]["name"] = new_name

    # Update Telegram message
    going = events_data[event_id]["going"]
    not_going = events_data[event_id]["not_going"]
    is_open = events_data[event_id]["is_open"]

    chat_id = events_data[event_id]["chat_id"]
    message_id = events_data[event_id]["message_id"]

    keyboard = create_event_keyboard(event_id, going, not_going, is_open)

    text = (
        f"📅 *{new_name}*\n\n"
        f"🟢 *Going* ({len(going)}):\n"
        f"❌ *Not going* ({len(not_going)}):\n"
    )
    try:
        await context.bot.edit_message_text(
            text=text,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Failed to edit message: {e}")

    # Update event name in Google Sheet "Events"
    all_records = events_sheet.get_all_records()
    for idx, record in enumerate(all_records, start=2):
        if record["id event"] == event_id:
            events_sheet.update_cell(idx, 2, new_name)  # column 2 = event name
            break

    await update.message.reply_text("Event updated.")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    if data == "noop":
        # Disabled button pressed, do nothing
        return

    # Data format: action_eventid
    try:
        action, event_id = data.split("_", 1)
    except ValueError:
        return

    if event_id not in events_data:
        await query.edit_message_text("Event not found or expired.")
        return

    event = events_data[event_id]

    user = query.from_user
    user_id = user.id
    username = user.username if user.username else user.first_name

    going = event["going"]
    not_going = event["not_going"]
    is_open = event["is_open"]

    if action == "going":
        going.add(username)
        not_going.discard(username)
    elif action == "notgoing":
        not_going.add(username)
        going.discard(username)
    elif action == "open":
        event["is_open"] = True
        is_open = True
    elif action == "close":
        event["is_open"] = False
        is_open = False

        # Update final counts in Google Sheets for closed event
        all_records = events_sheet.get_all_records()
        for idx, record in enumerate(all_records, start=2):
            if record["id event"] == event_id:
                events_sheet.update_cell(idx, 4, len(going))     # going participants count
                events_sheet.update_cell(idx, 5, len(not_going)) # not going participants count
                break
    else:
        return

    # Update Google Sheet EventActions
    now_str = datetime.now().isoformat()
    actions_sheet.append_row(
        [event_id, now_str, username, user_id, action]
    )

    # Update Telegram message with new lists and keyboard
    going_list_text = "\n".join(going) if going else ""
    not_going_list_text = "\n".join(not_going) if not_going else ""

    text = (
        f"📅 *{event['name']}*\n\n"
        f"🟢 *Going* ({len(going)}):\n{going_list_text}\n\n"
        f"❌ *Not going* ({len(not_going)}):\n{not_going_list_text}"
    )

    keyboard = create_event_keyboard(event_id, going, not_going, is_open)

    try:
        await query.edit_message_text(
            text=text,
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Failed to update message: {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hello! Use /add_event <event name> to create a new event.\n"
        "Use /update_event <event_id> <new event name> to rename an event."
    )


def main():
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("add_event", add_event))
    application.add_handler(CommandHandler("update_event", update_event))
    application.add_handler(CallbackQueryHandler(button_handler))

    application.run_polling()


if __name__ == "__main__":
    main()
