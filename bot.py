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

# Логирование
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
GOOGLE_CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")

# Декодирование U+XXXX в emoji
def decode_emoji(emoji_code):
    if emoji_code and emoji_code.startswith("U+"):
        return emoji_code.replace("U+", "\\u").encode().decode("unicode_escape")
    return emoji_code or ""

# Загружаем emoji из .env (если нет — будет пусто)
GOING_ICON = decode_emoji(os.getenv("DEFAULT_GOING_ICON", ""))
NOTGOING_ICON = decode_emoji(os.getenv("DEFAULT_NOTGOING_ICON", ""))
OPEN_EVENT_ICON = decode_emoji(os.getenv("OPEN_EVENT_ICON", ""))
CLOSE_EVENT_ICON = decode_emoji(os.getenv("CLOSE_EVENT_ICON", ""))

# Авторизация Google Sheets
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
credentials = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDENTIALS_PATH, scope)
client = gspread.authorize(credentials)

events_sheet = client.open(GOOGLE_SHEET_NAME).worksheet("Events")
actions_sheet = client.open(GOOGLE_SHEET_NAME).worksheet("EventActions")

events_data = {}

def create_event_keyboard(event_id, going, not_going, counters, is_open, user_choices, username, going_icon, notgoing_icon):
    buttons = [[], [], []]

    if not is_open:
        buttons[0].append(
            InlineKeyboardButton(f"{OPEN_EVENT_ICON} Open Event", callback_data=f"open_{event_id}")
        )
        return InlineKeyboardMarkup(buttons)

    chosen = user_choices.get(username)

    going_button = InlineKeyboardButton(f"{going_icon} Going", callback_data=f"going_{event_id}")
    notgoing_button = InlineKeyboardButton(f"{notgoing_icon} Not Going", callback_data=f"notgoing_{event_id}")

    if chosen == "going":
        buttons[0].append(notgoing_button)
    elif chosen == "notgoing":
        buttons[0].append(going_button)
    else:
        buttons[0].extend([going_button, notgoing_button])

    buttons[1] = [
        InlineKeyboardButton("Add", callback_data=f"add_{event_id}"),
        InlineKeyboardButton("Sub", callback_data=f"sub_{event_id}")
    ]

    buttons[2].append(
        InlineKeyboardButton(f"{CLOSE_EVENT_ICON} Close Event", callback_data=f"close_{event_id}")
    )

    return InlineKeyboardMarkup(buttons)


def update_event_text(event_id):
    event = events_data[event_id]
    going_icon = event.get("going_icon", "")
    notgoing_icon = event.get("notgoing_icon", "")

    going_list_text = "\n".join(event["going"]) if event["going"] else ""
    counter_lines = [f"{count}, from {user}" for user, count in event["counters"].items()]
    counter_text = "\n".join(counter_lines) if counter_lines else ""
    notgoing_list_text = "\n".join(event["not_going"]) if event["not_going"] else ""

    return (
        f"*{event['name']}*\n\n"
        f"{going_icon} *Going* ({len(event['going'])}):\n{going_list_text}\n"
        f"{counter_text}\n"
        f"{notgoing_icon} *Not going* ({len(event['not_going'])}):\n{notgoing_list_text}"
    )


async def add_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Please provide event name after command.")
        return

    event_name = context.args[0]
    going_icon = decode_emoji(context.args[1]) if len(context.args) > 1 else GOING_ICON
    notgoing_icon = decode_emoji(context.args[2]) if len(context.args) > 2 else NOTGOING_ICON

    event_id = str(uuid4())[:8]
    is_open = True

    event_data = {
        "name": event_name,
        "going": set(),
        "not_going": set(),
        "counters": {},
        "user_choices": {},
        "is_open": is_open,
        "message_id": None,
        "chat_id": update.effective_chat.id,
        "going_icon": going_icon,
        "notgoing_icon": notgoing_icon,
    }

    events_data[event_id] = event_data
    username = update.effective_user.username or update.effective_user.first_name

    keyboard = create_event_keyboard(
        event_id, set(), set(), {}, is_open, {}, username, going_icon, notgoing_icon
    )

    text = (
        f"*{event_name}*\n\n"
        f"{going_icon} *Going* (0):\n\n"
        f"{notgoing_icon} *Not going* (0):\n"
    )

    message = await update.message.reply_text(text, reply_markup=keyboard, parse_mode="Markdown")
    event_data["message_id"] = message.message_id

    now_str = datetime.now().isoformat()
    events_sheet.append_row([event_id, event_name, now_str, 0, 0])


async def update_event(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Please provide event ID.")
        return

    event_id = context.args[0]
    event = events_data.get(event_id)

    if not event:
        await update.message.reply_text("Event not found.")
        return

    text = update_event_text(event_id)
    keyboard = create_event_keyboard(
        event_id,
        event["going"],
        event["not_going"],
        event["counters"],
        event["is_open"],
        event["user_choices"],
        update.effective_user.username or update.effective_user.first_name,
        event["going_icon"],
        event["notgoing_icon"]
    )

    try:
        await context.bot.edit_message_text(
            chat_id=event["chat_id"],
            message_id=event["message_id"],
            text=text,
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Failed to update event message: {e}")
        await update.message.reply_text("Failed to update event message.")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    try:
        action, event_id = data.split("_", 1)
    except ValueError:
        return

    event = events_data.get(event_id)
    if not event:
        await query.edit_message_text("Event not found or expired.")
        return

    user = query.from_user
    username = user.username or user.first_name

    if action == "going":
        if event["is_open"]:
            event["user_choices"][username] = "going"
            event["going"].add(username)
            event["not_going"].discard(username)

    elif action == "notgoing":
        if event["is_open"]:
            event["user_choices"][username] = "notgoing"
            event["not_going"].add(username)
            event["going"].discard(username)

    elif action == "add":
        if event["is_open"]:
            event["counters"][username] = event["counters"].get(username, 0) + 1

    elif action == "sub":
        if event["is_open"] and username in event["counters"]:
            if event["counters"][username] > 1:
                event["counters"][username] -= 1
            else:
                del event["counters"][username]

    elif action == "close":
        if event["is_open"]:
            event["is_open"] = False
            total_going = len(event["going"]) + sum(event["counters"].values())
            total_notgoing = len(event["not_going"])

            for i, row in enumerate(events_sheet.get_all_records(), start=2):
                if row["EVENT_ID"] == event_id:
                    events_sheet.update_cell(i, 4, total_going)
                    events_sheet.update_cell(i, 5, total_notgoing)
                    break

    elif action == "open":
        if not event["is_open"]:
            event["is_open"] = True

    now_str = datetime.now().isoformat()
    actions_sheet.append_row([event_id, now_str, username, user.id, action])

    text = update_event_text(event_id)
    keyboard = create_event_keyboard(
        event_id,
        event["going"],
        event["not_going"],
        event["counters"],
        event["is_open"],
        event["user_choices"],
        username,
        event["going_icon"],
        event["notgoing_icon"]
    )

    try:
        await query.edit_message_text(text=text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Failed to update message: {e}")


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
