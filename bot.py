import logging
import os
import codecs
import json
import re
from datetime import datetime
from uuid import uuid4
from dotenv import load_dotenv

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update

import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    ContextTypes,
    MessageHandler,
    filters
)

load_dotenv()

from dotenv import load_dotenv, find_dotenv

HELP_TEXT = (
    "*Event Bot Commands:*\n\n"
    "/addevent <event\\_name> \\[going\\_icon] \\[notgoing\\_icon] — create a new event\n"
    "/editevent <event\\_name> \\[going\\_icon] \\[notgoing\\_icon] — update the name or icons of the latest event\n"
    "/help — show this help message\n\n"
    "*Interactive buttons:*\n"
    "✅ Going / ❌ Not Going — mark your attendance\n"
    "➕ Add / ➖ Sub — specify/change the number of people you’re bringing\n"
    "🔴 Close Event — close the event for further responses\n"
    "🟢 Open Event — reopen the event for participation\n\n"
    "Supports multiple events at once and saves data to Google Sheets\\."
)


# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

DEFAULT_GOING_ICON = codecs.decode(os.getenv("DEFAULT_GOING_ICON"), "unicode_escape")
DEFAULT_NOTGOING_ICON = codecs.decode(os.getenv("DEFAULT_NOTGOING_ICON"), "unicode_escape")
DEFAULT_OPEN_ICON = codecs.decode(os.getenv("DEFAULT_OPEN_ICON"), "unicode_escape")
DEFAULT_CLOSE_ICON = codecs.decode(os.getenv("DEFAULT_CLOSE_ICON"), "unicode_escape")

TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME")
# Google Sheets authentication
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

raw_credentials = os.getenv("GOOGLE_CREDENTIALS_JSON")
credentials_info = json.loads(raw_credentials)
credentials_info["private_key"] = credentials_info["private_key"].replace("\\n", "\n")

credentials = ServiceAccountCredentials.from_json_keyfile_dict(credentials_info, scope)
client = gspread.authorize(credentials)

# Open Google Sheets
events_sheet = client.open(GOOGLE_SHEET_NAME).worksheet("Events")
actions_sheet = client.open(GOOGLE_SHEET_NAME).worksheet("EventActions")
events_data = {}

# Escape Markdown special chars function
def escape_markdown(text):
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

# Function to get current date and time in dd.mm.yyyy HH:MM:SS.mmm format 
def now2ddmmyy():
    now = datetime.now()
    return now.strftime("%d.%m.%Y %H:%M:%S.%f")[:-3]  # Drop to milliseconds

# Command to show help message
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT, parse_mode="MarkdownV2")

# Handler for new chat members
async def greet_new_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.chat_member
    if result.new_chat_member.status in ["member", "administrator"] and result.old_chat_member.status == "left":
        chat = update.effective_chat
        await context.bot.send_message(chat_id=chat.id, text=HELP_TEXT, parse_mode="Markdown")

# Handler for private messages to greet user
async def greet_user_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type == "private":
        await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")

# Event keyboard creation function
# Event keyboard has 2 statuses: opened and closed
# If event is closed, only Open Event button is available
# If event is open, buttons Going/NotGoing, Add/Sub and Close Event are available
def create_event_keyboard(event_id, going, not_going, counters, is_open, user_choices, username, going_icon, notgoing_icon):
    buttons = [[], [], []]

    if not is_open:
        buttons[0].append(
            InlineKeyboardButton(f"{DEFAULT_OPEN_ICON} Open Event", callback_data=f"open_{event_id}")
        )
        return InlineKeyboardMarkup(buttons)

    # Buttons Going/NotGoing always available if event is open
    going_text = f"{going_icon} Going"
    notgoing_text = f"{notgoing_icon} Not Going"

    going_button = InlineKeyboardButton(going_text, callback_data=f"going_{event_id}")
    not_going_button = InlineKeyboardButton(notgoing_text, callback_data=f"notgoing_{event_id}")
    buttons[0].extend([going_button, not_going_button])

    # Buttons Add/Sub always available if event is open
    buttons[1] = [
        InlineKeyboardButton("Add", callback_data=f"add_{event_id}"),
        InlineKeyboardButton("Sub", callback_data=f"sub_{event_id}")
    ]

    # Button Close Event
    buttons[2].append(
        InlineKeyboardButton(f"{DEFAULT_CLOSE_ICON} Close Event", callback_data=f"close_{event_id}")
    )

    return InlineKeyboardMarkup(buttons)

def update_event_on_close(event_id, going_count, notgoing_count, closed_by_username):
    """
    Updates the Events sheet with final stats after event is closed.

    Params:
    - event_id (str): unique ID of the event
    - going_users (dict): dict of users who selected 'going'
    - notgoing_users (dict): dict of users who selected 'not going'
    - closed_by_username (str): user who clicked 'Close Event'
    """

    sheet = client.open(GOOGLE_SHEET_NAME).worksheet("Events")
    records = sheet.get_all_records()
    
    for idx, row in enumerate(records, start=2):  # start=2 to account for header row
        if str(row.get("EVENT_ID")) == str(event_id):
            # Prepare updated values

            # Update columns:
            # Column G (7): GOING
            # Column H (8): NOT GOING
            # Column E (5): FINISHED_AT
            # Column F (6): FINISHED_BY

            sheet.update_cell(idx, 5, now2ddmmyy())
            sheet.update_cell(idx, 6, closed_by_username)
            sheet.update_cell(idx, 7, going_count)
            sheet.update_cell(idx, 8, notgoing_count)
            break

# Function to add a new event
async def addevent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Please provide event name after command.")
        return

    event_name_raw = context.args[0]
    going_icon = DEFAULT_GOING_ICON
    notgoing_icon = DEFAULT_NOTGOING_ICON

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
    # username without markdown escaping for Google Sheets
    username_raw = username
    # username with markdown escaping for Telegram message
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
    events_data[event_id]["chat_id"] = message.chat.id
    # Log the Event in the Events sheet
    events_sheet.append_row([event_id, event_name_raw, now2ddmmyy(), username_raw, "","",0, 0])

# Edit Google Sheets with the new event name, edit icons for going/notgoing
async def editevent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Please provide parameters: event_name [going_icon] [notgoing_icon]")
        return

    if not events_data:
        await update.message.reply_text("No events to update.")
        return

    # Get the last event from events_data
    last_event_id = list(events_data.keys())[-1]
    event = events_data[last_event_id]

    user = update.effective_user
    username_raw = user.username if user.username else user.first_name

    event_name = context.args[0]
    going_icon = event.get("going_icon", DEFAULT_GOING_ICON)
    notgoing_icon = event.get("notgoing_icon", DEFAULT_NOTGOING_ICON)

    if len(context.args) >= 2:
        going_icon = context.args[1]
    if len(context.args) >= 3:
        notgoing_icon = context.args[2]

    # Update event data
    event["name"] = event_name
    event["going_icon"] = going_icon
    event["notgoing_icon"] = notgoing_icon

    # Update Google Sheets
    try:
        sheet = client.open(GOOGLE_SHEET_NAME).worksheet("Events")
        all_records = sheet.get_all_records()
        for idx, row in enumerate(all_records, start=2):  # start=2 — с учётом заголовков
            if row["EVENT_ID"] == last_event_id:
                sheet.update_cell(idx, 2, event_name)  # EVENT_NAME
                break
    except Exception as e:
        logger.error(f"Failed to update Google Sheets: {e}")

    # Update telegram message
    keyboard = create_event_keyboard(
        last_event_id,
        event["going"],
        event["not_going"],
        event["counters"],
        event["is_open"],
        event["user_choices"],
        username_raw,
        going_icon,
        notgoing_icon,
    )

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
        await context.bot.edit_message_text(
            chat_id=event["chat_id"],
            message_id=event["message_id"],
            text=text,
            reply_markup=keyboard,
            parse_mode="Markdown"
        )
        # Log the action update in the Actions sheet
        actions_sheet.append_row([last_event_id, now2ddmmyy(), username_raw, user.id, "update"])
    except Exception as e:
        logger.error(f"Failed to edit event message: {e}")
        await update.message.reply_text("Failed to update the original event message.")

# Handler for button clicks
# Handles all button clicks for the event management
# Actions: going, notgoing, add, sub, close, open
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
    going_icon = event.get("going_icon", DEFAULT_GOING_ICON)
    notgoing_icon = event.get("notgoing_icon", DEFAULT_NOTGOING_ICON)

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
        update_event_on_close(
            event_id=event_id,
            going_count=(len(going) + sum(counters.values())),
            notgoing_count=len(not_going),
            closed_by_username=username_raw)
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
        f"{going_icon} *Going* ({len(going)+sum(counters.values())}):\n{going_list_text}\n"
        f"{counter_text}\n"
        f"{notgoing_icon} *Not going* ({len(not_going)}):\n{not_going_list_text}"
    )
    # Log the action in the Actions sheet
    actions_sheet.append_row([event_id, now2ddmmyy(), username_raw, user.id, action])

    try:
        await query.edit_message_text(text=text, reply_markup=keyboard, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Failed to update message: {e}")

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Command handlers
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("addevent", addevent))
    app.add_handler(CommandHandler("editevent", editevent))

    # Message handlers
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, greet_user_private))

    # Chat member updates (for new users or groups)
    app.add_handler(ChatMemberHandler(greet_new_chat, ChatMemberHandler.CHAT_MEMBER))

    # Callback handler (for inline buttons)
    app.add_handler(CallbackQueryHandler(button_handler))

    app.run_polling()


if __name__ == "__main__":
    main()
