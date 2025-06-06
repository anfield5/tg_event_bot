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

TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")

print("Bot is starting...")

# Google Sheets authentication
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
credentials = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS_JSON, scope)
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

    # Create new event event_name, going_icon (optional), notgoing_icon (optional)
async def add_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Please provide event name after command.")
        return

    # Parse arguments: event_name, going_icon (optional), notgoing_icon (optional)
    event_name = context.args[0]
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
        "name": event_name,
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

    username = update.effective_user.username or update.effective_user.first_name

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
    events_sheet.append_row([event_id, event_name, now_str, 0, 0])

    # update latest created event(updates event_name, going_icon, notgoing_icon)
async def update_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Please provide parameters: event_name [going_icon] [notgoing_icon]")
        return

    # Get the last created event
    if not events_data:
        await update.message.reply_text("No events to update.")
        return

    last_event_id = list(events_data.keys())[-1]
    event = events_data[last_event_id]

    event_name = context.args[0]
    going_icon = event.get("going_icon", "✅")
    notgoing_icon = event.get("notgoing_icon", "❌")

    if len(context.args) >= 2:
        going_icon = context.args[1]
    if len(context.args) >= 3:
        notgoing_icon = context.args[2]

    event["name"] = event_name
    event["going_icon"] = going_icon
    event["notgoing_icon"] = notgoing_icon

    username = update.effective_user.username or update.effective_user.first_name

    keyboard = create_event_keyboard(last_event_id, event["going"], event["not_going"], event["counters"], event["is_open"], event["user_choices"], username, going_icon, notgoing_icon)

    going_list_text = "\n".join(event["going"]) if event["going"] else ""
    counter_lines = [f"{count}, from {user_name}" for user_name, count in event["counters"].items()]
    counter_text = "\n".join(counter_lines) if counter_lines else ""
    not_going_list_text = "\n".join(event["not_going"]) if event["not_going"] else ""

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
    username = user.username if user.username else user.first_name

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
        # Add counter regardless of status
        counters[username] = counters.get(username, 0) + 1

    elif action == "sub":
        if not is_open:
            await query.answer("Event is closed.", show_alert=True)
            return
        if username in counters:
            if counters[username] > 1:
                counters[username] -= 1
            else:
                del counters[username]

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

    going_list_text = "\n".join(going) if going else ""
    counter_lines = [f"{count}, from {user_name}" for user_name, count in counters.items()]
    counter_text = "\n".join(counter_lines) if counter_lines else ""
    not_going_list_text = "\n".join(not_going) if not_going else ""

    text = (
        f"*{event['name']}*\n\n"
        f"{going_icon} *Going* ({len(going)}):\n{going_list_text}\n"
        f"{counter_text}\n"
        f"{notgoing_icon} *Not going* ({len(not_going)}):\n{not_going_list_text}"
    )

    keyboard = create_event_keyboard(event_id, going, not_going, counters, is_open, user_choices, username, going_icon, notgoing_icon)

    try:
        await query.edit_message_text(
            text=text,
            reply_markup=keyboard,
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.error(f"Failed to update message: {e}")

    now_str = datetime.now().isoformat()
    actions_sheet.append_row([event_id, now_str, username, user.id, action])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Use /add_event <event_name> [going_icon] [notgoing_icon] to create event.")


def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add_event", add_event))
    app.add_handler(CommandHandler("update_event", update_event))
    app.add_handler(CallbackQueryHandler(button_handler))

    app.run_polling()


if __name__ == "__main__":
    main()
