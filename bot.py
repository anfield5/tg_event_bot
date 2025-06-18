import logging
import os
import json
import re
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

TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
# GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
# credentials_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")

print("Bot is starting...")

# Escape Markdown special chars function
def escape_markdown(text):
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

# Google Sheets authentication
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
credentials = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS_JSON, scope)
# credentials = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS_PATH, scope)
client = gspread.authorize(credentials)

events_sheet = client.open(GOOGLE_SHEET_NAME).worksheet("Events")
actions_sheet = client.open(GOOGLE_SHEET_NAME).worksheet("EventActions")

events_data = {}

def create_event_keyboard(event_id, going, not_going, counters, is_open, user_choices, username, going_icon, notgoing_icon):
    buttons = [[], [], []]

    if not is_open:
        buttons[0].append(
            InlineKeyboardButton("🟢 Open Event", callback_data=f"open_{event_id}")
        )
        return InlineKeyboardMarkup(buttons)

    chosen = user_choices.get(username)

    going_text = f"{going_icon} Going"
    notgoing_text = f"{notgoing_icon} Not Going"

    going_button = InlineKeyboardButton(going_text, callback_data=f"going_{event_id}")
    not_going_button = InlineKeyboardButton(notgoing_text, callback_data=f"notgoing_{event_id}")

    if chosen == "going":
        buttons[0].append(not_going_button)
    elif chosen == "notgoing":
        buttons[0].append(going_button)
    else:
        buttons[0].extend([going_button, not_going_button])

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

    event_name_raw = context.args[0]
    going_icon = "✅"
    notgoing_icon = "❌"

    if len(context.args) >= 2:
        going_icon = context.args[1]
    if len(context.args) >= 3:
        notgoing_icon = context.args[2]

    event_id = str(uuid4())[:8]
    is_open = True

    going = set()
    not_going = set()
    counters = {}
    user_choices = {}

    events_data[event_id] = {
        "name": event_name_raw,
        "going": going,
        "not_going": not_going,
        "counters": counters,
        "user_choices": user_choices,
        "is_open": is_open,
        "message_id": None,
        "chat_id": update.effective_chat.id,
        "going_icon": going_icon,
        "notgoing_icon": notgoing_icon,
    }

    username = update.effective_user.username or update.effective_user.first_name or str(update.effective_user.id)
    username = escape_markdown(username)
    event_name = escape_markdown(event_name_raw)

    keyboard = create_event_keyboard(event_id, going, not_going, counters, is_open, user_choices, username, going_icon, notgoing_icon)

    text = (
        f"*{event_name}*\n\n"
        f"{going_icon} *Going* (0):\n\n"
        f"{notgoing_icon} *Not going* (0):\n"
    )

    message = await update.message.reply_text(
        text, reply_markup=keyboard, parse_mode="Markdown"
    )

    events_data[event_id]["message_id"] = message.message_id

    now_str = datetime.now().isoformat()
    events_sheet.append_row([event_id, event_name_raw, now_str, 0, 0])

async def update_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Please provide parameters: event_name [going_icon] [notgoing_icon]")
        return

    if not events_data:
        await update.message.reply_text("No events to update.")
        return

    last_event_id = list(events_data.keys())[-1]
    event = events_data[last_event_id]

    event_name_raw = context.args[0]
    going_icon = event.get("going_icon", "✅")
    notgoing_icon = event.get("notgoing_icon", "❌")

    if len(context.args) >= 2:
        going_icon = context.args[1]
    if len(context.args) >= 3:
        notgoing_icon = context.args[2]

    event["name"] = event_name_raw
    event["going_icon"] = going_icon
    event["notgoing_icon"] = notgoing_icon

    username = update.effective_user.username or update.effective_user.first_name or str(update.effective_user.id)
    username = escape_markdown(username)
    event_name = escape_markdown(event_name_raw)

    keyboard = create_event_keyboard(last_event_id, event["going"], event["not_going"], event["counters"], event["is_open"], event["user_choices"], username, going_icon, notgoing_icon)

    going_list_text = "\n".join([escape_markdown(u) for u in event["going"]]) if event["going"] else ""
    counter_lines = [f"{count}, from {escape_markdown(user_name)}" for user_name, count in event["counters"].items()]
    counter_text = "\n".join(counter_lines) if counter_lines else ""
    not_going_list_text = "\n".join([escape_markdown(u) for u in event["not_going"]]) if event["not_going"] else ""

    text = (
        f"*{event_name}*\n\n"
        f"{going_icon} *Going* ({len(event['going'])}):\n{going_list_text}\n"
        f"{counter_text}\n"
        f"{notgoing_icon} *Not going* ({len(event['not_going'])}):\n{not_going_list_text}"
    )

    try:
        await update.message.reply_text(text=text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Failed to update event message: {e}")

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
    username_raw = user.username if user.username else user.first_name
    username = escape_markdown(username_raw)

    going = event["going"]
    not_going = event["not_going"]
    counters = event["counters"]
    user_choices = event["user_choices"]
    is_open = event["is_open"]
    going_icon = event.get("going_icon", "✅")
    notgoing_icon = event.get("notgoing_icon", "❌")

    if action == "going":
        if not is_open:
            await query.answer("Event is closed.", show_alert=True)
            return
        if user_choices.get(username_raw) == "going":
            await query.answer("You already chose Going.", show_alert=True)
            return

        user_choices[username_raw] = "going"
        going.add(username_raw)
        not_going.discard(username_raw)

    elif action == "notgoing":
        if not is_open:
            await query.answer("Event is closed.", show_alert=True)
            return
        if user_choices.get(username_raw) == "notgoing":
            await query.answer("You already chose Not Going.", show_alert=True)
            return

        user_choices[username_raw] = "notgoing"
        not_going.add(username_raw)
        going.discard(username_raw)

    elif action == "add":
        if not is_open:
            await query.answer("Event is closed.", show_alert=True)
            return
        counters[username_raw] = counters.get(username_raw, 0) + 1

    elif action == "sub":
        if not is_open:
            await query.answer("Event is closed.", show_alert=True)
            return
        if username_raw in counters:
            if counters[username_raw] > 1:
                counters[username_raw] -= 1
            else:
                counters.pop(username_raw)

    elif action == "close":
        event["is_open"] = False
        is_open = False
    elif action == "open":
        event["is_open"] = True
        is_open = True

    keyboard = create_event_keyboard(event_id, going, not_going, counters, is_open, user_choices, username, going_icon, notgoing_icon)

    going_list_text = "\n".join([escape_markdown(u) for u in going]) if going else ""
    counter_lines = [f"{count}, from {escape_markdown(user_name)}" for user_name, count in counters.items()]
    counter_text = "\n".join(counter_lines) if counter_lines else ""
    not_going_list_text = "\n".join([escape_markdown(u) for u in not_going]) if not_going else ""

    text = (
        f"*{escape_markdown(event['name'])}*\n\n"
        f"{going_icon} *Going* ({len(going)}):\n{going_list_text}\n"
        f"{counter_text}\n"
        f"{notgoing_icon} *Not going* ({len(not_going)}):\n{not_going_list_text}"
    )

    try:
        await query.edit_message_text(text=text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Failed to update message: {e}")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("add_event", add_event))
    app.add_handler(CommandHandler("update_event", update_event))
    app.add_handler(CallbackQueryHandler(button_handler))

    print("Bot started")
    app.run_polling()

if __name__ == "__main__":
    main()
