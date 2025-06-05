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

# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# 
TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")

print("Bot is starting...")

# Connecting to Google Sheets
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
credentials = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS_JSON, scope)
client = gspread.authorize(credentials)

events_sheet = client.open(GOOGLE_SHEET_NAME).worksheet("Events")
actions_sheet = client.open(GOOGLE_SHEET_NAME).worksheet("EventActions")

# Storage for events data
events_data = {}

def create_event_keyboard(event_id, going, not_going, counters, is_open, user_choices, username):
    buttons = [[], [], []]

    if not is_open:
        buttons[0].append(
            InlineKeyboardButton("🟢 Open Event", callback_data=f"open_{event_id}")
        )
        return InlineKeyboardMarkup(buttons)

    chosen = user_choices.get(username)

    going_button = InlineKeyboardButton("✅ Going", callback_data=f"going_{event_id}")
    not_going_button = InlineKeyboardButton("❌ Not Going", callback_data=f"notgoing_{event_id}")

    if chosen == "going":
        buttons[0].append(not_going_button)
    elif chosen == "notgoing":
        buttons[0].append(going_button)
    else:
        buttons[0].extend([going_button, not_going_button])

    # Shopw Add/Sub always, if event is open
    buttons[1] = [
        InlineKeyboardButton("Add", callback_data=f"add_{event_id}"),
        InlineKeyboardButton("Sub", callback_data=f"sub_{event_id}")
    ]

    buttons[2].append(
        InlineKeyboardButton("🔴 Close Event", callback_data=f"close_{event_id}")
    )

    return InlineKeyboardMarkup(buttons)


async def add_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Please provide event name after command.")
        return

    event_name = " ".join(context.args)
    event_id = str(uuid4())[:8]
    is_open = True

    going = set()
    not_going = set()
    counters = {}  # username -> count
    user_choices = {}  # username -> 'going' or 'notgoing' or None

    events_data[event_id] = {
        "name": event_name,
        "going": going,
        "not_going": not_going,
        "counters": counters,
        "user_choices": user_choices,
        "is_open": is_open,
        "message_id": None,
        "chat_id": update.effective_chat.id,
    }

    username = update.effective_user.username or update.effective_user.first_name

    keyboard = create_event_keyboard(event_id, going, not_going, counters, is_open, user_choices, username)

    text = (
        f"📅 *{event_name}*\n\n"
        f"✅ *Going* (0):\n\n"
        f"❌ *Not going* (0):\n"
    )

    message = await update.message.reply_text(
        text, reply_markup=keyboard, parse_mode="Markdown"
    )

    events_data[event_id]["message_id"] = message.message_id

    now_str = datetime.now().isoformat()
    events_sheet.append_row([event_id, event_name, now_str, 0, 0])


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    if data == "noop":
        return

    try:
        action, event_id = data.split("_", 1)
    except ValueError:
        return

    if event_id not in events_data:
        await query.edit_message_text("Event not found or expired.")
        return

    event = events_data[event_id]
    user = query.from_user
    username = user.username if user.username else user.first_name

    going = event["going"]
    not_going = event["not_going"]
    counters = event["counters"]
    user_choices = event["user_choices"]
    is_open = event["is_open"]

    if action == "going":
        if not is_open:
            await query.answer("Event is closed.", show_alert=True)
            return
        if user_choices.get(username) == "going":
            await query.answer("You already chose Going.", show_alert=True)
            return

        user_choices[username] = "going"
        going.add(username)
        not_going.discard(username)

    elif action == "notgoing":
        if not is_open:
            await query.answer("Event is closed.", show_alert=True)
            return
        if user_choices.get(username) == "notgoing":
            await query.answer("You already chose Not Going.", show_alert=True)
            return

        user_choices[username] = "notgoing"
        not_going.add(username)
        going.discard(username)

    elif action == "add":
        if not is_open:
            await query.answer("Event is closed.", show_alert=True)
            return

        counters[username] = counters.get(username, 0) + 1

    elif action == "sub":
        if not is_open:
            await query.answer("Event is closed.", show_alert=True)
            return

        if counters.get(username, 0) > 0:
            counters[username] -= 1
            if counters[username] == 0:
                counters.pop(username)
        else:
            await query.answer("Counter is already zero.", show_alert=True)
            return

    elif action == "close":
        if not is_open:
            await query.answer("Event already closed.", show_alert=True)
            return

        event["is_open"] = False
        is_open = False

        total_going = len(going) + sum(counters.values())
        total_not_going = len(not_going)

        all_records = events_sheet.get_all_records()
        for idx, record in enumerate(all_records, start=2):
            if record["EVENT_ID"] == event_id:
                events_sheet.update_cell(idx, 4, total_going)     # GOING
                events_sheet.update_cell(idx, 5, total_not_going) # NOT GOING
                break

    elif action == "open":
        if is_open:
            await query.answer("Event already open.", show_alert=True)
            return
        event["is_open"] = True
        is_open = True

    else:
        return

    # Text with lists and counters
    going_list_text = "\n".join(going) if going else ""
    counter_lines = [f"{count}, from {user_name}" for user_name, count in counters.items()]
    counter_text = "\n".join(counter_lines) if counter_lines else ""
    not_going_list_text = "\n".join(not_going) if not_going else ""

    text = (
        f"📅 *{event['name']}*\n\n"
        f"✅ *Going* ({len(going)}):\n{going_list_text}\n"
        f"{counter_text}\n"
        f"❌ *Not going* ({len(not_going)}):\n{not_going_list_text}"
    )

    keyboard = create_event_keyboard(event_id, going, not_going, counters, is_open, user_choices, username)

    try:
        await query.edit_message_text(
            text=text,
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Failed to update message: {e}")

    # Add action to sheet EventActions
    now_str = datetime.now().isoformat()
    actions_sheet.append_row([event_id, now_str, username, user.id, action])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Use /add_event <event_name> to create an event.")


def main():
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("add_event", add_event))
    application.add_handler(CallbackQueryHandler(button_handler))

    application.run_polling()


if __name__ == "__main__":
    main()
