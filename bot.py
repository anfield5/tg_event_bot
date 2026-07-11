import logging
import os
import codecs
import json
import re
import sqlite3
from datetime import datetime
from uuid import uuid4
from dotenv import load_dotenv

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# Async libraries for Google Sheets API
import gspread_asyncio
from google.oauth2.service_account import Credentials

load_dotenv()

# Logger configuration
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Default UI icons with safe fallback configurations
DEFAULT_GOING_ICON = codecs.decode(os.getenv("DEFAULT_GOING_ICON", "✅"), "unicode_escape")
DEFAULT_NOTGOING_ICON = codecs.decode(os.getenv("DEFAULT_NOTGOING_ICON", "❌"), "unicode_escape")
DEFAULT_OPEN_ICON = codecs.decode(os.getenv("DEFAULT_OPEN_ICON", "🟢"), "unicode_escape")
DEFAULT_CLOSE_ICON = codecs.decode(os.getenv("DEFAULT_CLOSE_ICON", "🔴"), "unicode_escape")

TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
GLOBAL_DEFAULT_SHEET = os.getenv("GOOGLE_SHEET_NAME")

# --- DATABASE INIT ---
def init_db():
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_settings (
            chat_id TEXT PRIMARY KEY,
            sheet_name TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            chat_id TEXT,
            message_id TEXT,
            name TEXT,
            going_icon TEXT,
            notgoing_icon TEXT,
            is_open INTEGER,
            going_data TEXT,
            notgoing_data TEXT,
            counters_data TEXT
        )
    """)
    
    # Track unique users seen in chat with status matrix (default: active)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_users (
            chat_id TEXT,
            username TEXT,
            status TEXT DEFAULT 'active',
            PRIMARY KEY (chat_id, username)
        )
    """)
    
    # Migration helper: Check if status column exists in database, add if missing
    cursor.execute("PRAGMA table_info(chat_users)")
    columns = [col[1] for col in cursor.fetchall()]
    if "status" not in columns:
        cursor.execute("ALTER TABLE chat_users ADD COLUMN status TEXT DEFAULT 'active'")
        
    conn.commit()
    conn.close()

init_db()

# --- ASYNC GOOGLE CLIENT ---
def get_credentials():
    raw_credentials = os.getenv("GOOGLE_CREDENTIALS_JSON")
    credentials_info = json.loads(raw_credentials)
    credentials_info["private_key"] = credentials_info["private_key"].replace("\\n", "\n")
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    return Credentials.from_service_account_info(credentials_info, scopes=scope)

agcm = gspread_asyncio.AsyncioGspreadClientManager(get_credentials)

# --- UTILS ---
def escape_markdown(text):
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def now2ddmmyy():
    return datetime.now().strftime("%d.%m.%Y %H:%M:%S.%f")[:-3]

async def get_sheet_for_chat(chat_id):
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT sheet_name FROM chat_settings WHERE chat_id = ?", (str(chat_id),))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else GLOBAL_DEFAULT_SHEET

async def log_action_to_google(chat_id, event_id, action_name, username):
    try:
        sheet_target = await get_sheet_for_chat(chat_id)
        gc = await agcm.authorize()
        ss = await gc.open(sheet_target)
        ws = await ss.worksheet("Actions")
        await ws.append_row([event_id, action_name, username, now2ddmmyy()])
    except Exception as e:
        logger.error(f"Google Sheets Actions log failed. Error details: {repr(e)}")

def track_user(chat_id, username, status="active"):
    if not username:
        return
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    # Insert with default status, or update if user already exists
    cursor.execute("""
        INSERT INTO chat_users (chat_id, username, status) VALUES (?, ?, ?)
        ON CONFLICT(chat_id, username) DO UPDATE SET status = excluded.status
    """, (str(chat_id), username, status))
    conn.commit()
    conn.close()

def parse_user_args(args):
    # Flatten arguments and split by comma or spaces to handle varied inputs
    raw_string = " ".join(args)
    tokens = re.split(r'[\s,]+', raw_string)
    return [t.lstrip('@').strip() for t in tokens if t.strip()]

def create_event_keyboard(event_id, is_open, going_icon, notgoing_icon):
    buttons = [[], [], []]
    if not is_open:
        buttons[0].append(InlineKeyboardButton(f"{DEFAULT_OPEN_ICON} Reopen Event", callback_data=f"open_{event_id}"))
        return InlineKeyboardMarkup(buttons)

    buttons[0].extend([
        InlineKeyboardButton(f"{going_icon} Going", callback_data=f"going_{event_id}"),
        InlineKeyboardButton(f"{notgoing_icon} Not Going", callback_data=f"notgoing_{event_id}")
    ])
    buttons[1].extend([
        InlineKeyboardButton("➕ Add Guest", callback_data=f"add_{event_id}"),
        InlineKeyboardButton("➖ Remove Guest", callback_data=f"sub_{event_id}")
    ])
    buttons[2].append(InlineKeyboardButton(f"{DEFAULT_CLOSE_ICON} Close Event", callback_data=f"close_{event_id}"))
    return InlineKeyboardMarkup(buttons)

# --- COMMAND HANDLERS ---
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "*Event Bot Commands:*\n\n"
        "/newevent `event_name` `going_icon` `notgoing_icon` — create a new event\n"
        "/editevent `event_name` `going_icon` `notgoing_icon` — update the name or icons of the latest event\n"
        "/notify `text_msg` — notify active users who haven't made a choice yet\n"
        "/adduser `user1, user2` — add users into database matrix as active\n"
        "/updateuser `user1` `-passive/-active` — update specified user activity status\n"
        "/listusers — show tracking user status manifest list\n"
        "/help — show this help message\n\n"
        "*Interactive buttons:*\n"
        "✅ Going / ❌ Not Going — mark your attendance\n"
        "➕ Add / ➖ Sub — specify/change the number of people you’re bringing\n"
        "🔴 Close Event — close the event for further responses\n"
        "🟢 Open Event — reopen the event for participation\n\n"
        "Supports multiple events at once and saves data to Google Sheets\."
    )
    await update.message.reply_text(help_text, parse_mode="MarkdownV2")

async def track_everyone_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat and update.effective_user:
        chat_id = str(update.effective_chat.id)
        user = update.effective_user
        username_raw = user.username if user.username else user.first_name
        # Auto-track unseen active talkers
        track_user(chat_id, username_raw, "active")

async def newevent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /newevent <event_name> [going_icon] [notgoing_icon]")
        return

    event_name_raw = context.args[0]
    going_icon = DEFAULT_GOING_ICON
    notgoing_icon = DEFAULT_NOTGOING_ICON
    
    if len(context.args) == 2:
        going_icon = context.args[1]
    elif len(context.args) >= 3:
        going_icon = context.args[1]
        notgoing_icon = context.args[2]

    event_id = str(uuid4())[:8]
    chat_id = str(update.effective_chat.id)
    username_raw = update.effective_user.username or update.effective_user.first_name or str(update.effective_user.id)
    
    track_user(chat_id, username_raw, "active")

    text = f"*{escape_markdown(event_name_raw)}*\n\n{going_icon} *Going* \(0\):\n\n{notgoing_icon} *Not Going* \(0\):\n"
    keyboard = create_event_keyboard(event_id, True, going_icon, notgoing_icon)
    
    message = await update.message.reply_text(text, reply_markup=keyboard, parse_mode="MarkdownV2")

    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon, is_open, going_data, notgoing_data, counters_data)
        VALUES (?, ?, ?, ?, ?, ?, 1, '[]', '[]', '{}')
    """, (event_id, chat_id, str(message.message_id), event_name_raw, going_icon, notgoing_icon))
    conn.commit()
    conn.close()

    try:
        sheet_target = await get_sheet_for_chat(chat_id)
        gc = await agcm.authorize()
        ss = await gc.open(sheet_target)
        ws = await ss.worksheet("Events")
        await ws.append_row([event_id, event_name_raw, now2ddmmyy(), username_raw, "", "OPEN", ""])
    except Exception as e:
        logger.error(f"Google Sheets log error: {e}")

async def editevent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /editevent <event_name> [going_icon] [notgoing_icon]")
        return

    chat_id = str(update.effective_chat.id)
    
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT event_id, message_id, name, going_icon, notgoing_icon, is_open, going_data, notgoing_data, counters_data 
        FROM events WHERE chat_id = ? ORDER BY rowid DESC LIMIT 1
    """, (chat_id,))
    row = cursor.fetchone()
    
    if not row:
        await update.message.reply_text("No events found in this chat to edit.")
        conn.close()
        return

    event_id, message_id, current_name, current_going_icon, current_notgoing_icon, is_open, going_data, notgoing_data, counters_data = row
    
    if not is_open:
        await update.message.reply_text("Last event was closed and can't be edited.")
        conn.close()
        return

    new_name = context.args[0]
    new_going = context.args[1] if len(context.args) >= 2 else current_going_icon
    new_notgoing = context.args[2] if len(context.args) >= 3 else current_notgoing_icon

    cursor.execute("""
        UPDATE events SET name = ?, going_icon = ?, notgoing_icon = ? WHERE event_id = ?
    """, (new_name, new_going, new_notgoing, event_id))
    conn.commit()
    conn.close()

    going = json.loads(going_data)
    not_going = json.loads(notgoing_data)
    counters = json.loads(counters_data)
    
    going_list_text = "\n".join([f"• {escape_markdown(u)}" for u in going]) if going else ""
    counter_lines = [f"• {escape_markdown(k)} \(\+{count} g\.\)" for k, count in counters.items()]
    counter_text = "\n".join(counter_lines) if counter_lines else ""
    not_going_list_text = "\n".join([f"• {escape_markdown(u)}" for u in not_going]) if not_going else ""
    total_going = len(going) + sum(counters.values())
    
    text = (
        f"*{escape_markdown(new_name)}*\n\n"
        f"{new_going} *Going* \({total_going}\):\n{going_list_text}\n"
        f"{'' if not counter_text else '*Guests:*'}\n{counter_text}\n"
        f"{new_notgoing} *Not Going* \({len(not_going)}\):\n{not_going_list_text}"
    )
    
    keyboard = create_event_keyboard(event_id, bool(is_open), new_going, new_notgoing)
    
    try:
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=int(message_id),
            text=text,
            reply_markup=keyboard,
            parse_mode="MarkdownV2"
        )
        await update.message.reply_text("Event metadata updated successfully in Telegram.")
    except Exception as e:
        logger.error(f"Telegram UI update failed during editevent: {e}")

    try:
        sheet_target = await get_sheet_for_chat(chat_id)
        gc = await agcm.authorize()
        ss = await gc.open(sheet_target)
        ws = await ss.worksheet("Events")
        records = await ws.get_all_records()
        for idx, r in enumerate(records, start=2):
            if str(r.get("EVENT_ID")) == str(event_id):
                await ws.update_cell(idx, 2, new_name)
                break
    except Exception as e:
        logger.error(f"Google Sheets sync failed during editevent: {e}")

async def adduser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /adduser <username1>, <username2> ...")
        return

    chat_id = str(update.effective_chat.id)
    usernames = parse_user_args(context.args)
    added_users = []

    for u in usernames:
        if u:
            track_user(chat_id, u, "active")
            added_users.append(f"@{u}")

    if added_users:
        await update.message.reply_text(f"Successfully added active users: {', '.join(added_users)}")
    else:
        await update.message.reply_text("No valid usernames found.")

async def updateuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /updateuser <username1> <username2> -active/-passive")
        return

    chat_id = str(update.effective_chat.id)
    raw_args = context.args

    # Check for presence of target flag in the final argument node
    target_status = "active"
    if raw_args[-1] in ["-active", "-passive"]:
        target_status = raw_args[-1].replace("-", "")
        raw_args = raw_args[:-1]

    usernames = parse_user_args(raw_args)
    updated_users = []

    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    
    for u in usernames:
        if u:
            cursor.execute("""
                INSERT INTO chat_users (chat_id, username, status) VALUES (?, ?, ?)
                ON CONFLICT(chat_id, username) DO UPDATE SET status = excluded.status
            """, (chat_id, u, target_status))
            updated_users.append(f"@{u}")
            
    conn.commit()
    conn.close()

    if updated_users:
        await update.message.reply_text(f"Updated users status profile to [{target_status}]: {', '.join(updated_users)}")
    else:
        await update.message.reply_text("No valid usernames processed for state modifications.")

async def listusers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT username, status FROM chat_users WHERE chat_id = ? ORDER BY username ASC", (chat_id,))
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("No users registered in this chat yet.")
        return

    lines = [f"{r[0]} - {r[1]}" for r in rows]
    await update.message.reply_text("\n".join(lines))

async def notify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /notify <your_message_text>")
        return

    notification_msg = " ".join(context.args)
    chat_id = str(update.effective_chat.id)

    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT going_data, notgoing_data FROM events WHERE chat_id = ? ORDER BY rowid DESC LIMIT 1", (chat_id,))
    event_row = cursor.fetchone()
    
    if not event_row:
        await update.message.reply_text("No active events found to reference for notification lists.")
        conn.close()
        return

    going_users = set(json.loads(event_row[0]))
    notgoing_users = set(json.loads(event_row[1]))
    decided_users = going_users.union(notgoing_users)

    # CRITICAL EXTENSION: Query only users marked explicitly with active status profile nodes
    cursor.execute("SELECT username FROM chat_users WHERE chat_id = ? AND status = 'active'", (chat_id,))
    active_known_users = {r[0] for r in cursor.fetchall()}
    conn.close()

    silent_users = active_known_users.difference(decided_users)

    if not silent_users:
        await update.message.reply_text("All users have made a decision.")
        return

    mentions = [f"@{u}" if not u.isdigit() else f"User {u}" for u in silent_users]
    output_text = f"{notification_msg}\n\n*Please respond:*\n" + ", ".join(mentions)
    await update.message.reply_text(output_text)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action, event_id = query.data.split("_", 1)
    
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id, message_id, name, going_icon, notgoing_icon, is_open, going_data, notgoing_data, counters_data FROM events WHERE event_id = ?", (event_id,))
    row = cursor.fetchone()
    
    if not row:
        await query.answer("Event not found.", show_alert=True)
        conn.close()
        return
        
    chat_id, message_id, name, going_icon, notgoing_icon, is_open, going_data, notgoing_data, counters_data = row
    going = set(json.loads(going_data))
    not_going = set(json.loads(notgoing_data))
    counters = json.loads(counters_data)
    
    user = query.from_user
    username_raw = user.username if user.username else user.first_name

    track_user(chat_id, username_raw, "active")

    if action in ["going", "notgoing", "add", "sub"] and not is_open:
        await query.answer("This event is already closed!", show_alert=True)
        conn.close()
        return

    if action in ["going", "notgoing", "add", "sub"]:
        context.application.create_task(log_action_to_google(chat_id, event_id, action, username_raw))

    if action == "going":
        going.add(username_raw)
        not_going.discard(username_raw)
        await query.answer("Status updated to: Going")
    elif action == "notgoing":
        not_going.add(username_raw)
        going.discard(username_raw)
        await query.answer("Status updated to: Not Going")
    elif action == "add":
        counters[username_raw] = counters.get(username_raw, 0) + 1
        await query.answer(f"Guest added. Total: {counters[username_raw]}")
    elif action == "sub":
        if username_raw in counters:
            if counters[username_raw] > 1:
                counters[username_raw] -= 1
                await query.answer(f"Guest removed. Total: {counters[username_raw]}")
            else:
                counters.pop(username_raw)
                await query.answer("All your guests removed.")
        else:
            await query.answer("You haven't added any guests yet.", show_alert=True)
    elif action == "close":
        is_open = 0
        await query.answer("Event closed.")
    elif action == "open":
        is_open = 1
        await query.answer("Event reopened.")

    cursor.execute("""
        UPDATE events SET is_open = ?, going_data = ?, notgoing_data = ?, counters_data = ? WHERE event_id = ?
    """, (is_open, json.dumps(list(going)), json.dumps(list(not_going)), json.dumps(counters), event_id))
    conn.commit()
    conn.close()

    going_list_text = "\n".join([f"• {escape_markdown(u)}" for u in going]) if going else ""
    counter_lines = [f"• {escape_markdown(k)} \(\+{count} g\.\)" for k, count in counters.items()]
    counter_text = "\n".join(counter_lines) if counter_lines else ""
    not_going_list_text = "\n".join([f"• {escape_markdown(u)}" for u in not_going]) if not_going else ""
    total_going = len(going) + sum(counters.values())
    
    text = (
        f"*{escape_markdown(name)}*\n\n"
        f"{going_icon} *Going* \({total_going}\):\n{going_list_text}\n"
        f"{'' if not counter_text else '*Guests:*'}\n{counter_text}\n"
        f"{notgoing_icon} *Not Going* \({len(not_going)}\):\n{not_going_list_text}"
    )
    
    keyboard = create_event_keyboard(event_id, bool(is_open), going_icon, notgoing_icon)
    
    try:
        await query.edit_message_text(text=text, reply_markup=keyboard, parse_mode="MarkdownV2")
    except Exception as e:
        logger.error(f"Telegram UI update failed: {e}")

    if action in ["close", "open"]:
        try:
            sheet_target = await get_sheet_for_chat(chat_id)
            gc = await agcm.authorize()
            ss = await gc.open(sheet_target)
            ws = await ss.worksheet("Events")
            records = await ws.get_all_records()
            for idx, r in enumerate(records, start=2):
                if str(r.get("EVENT_ID")) == str(event_id):
                    status_str = "CLOSED" if action == "close" else "OPEN"
                    closed_at_str = now2ddmmyy() if action == "close" else ""
                    amount_str = total_going if action == "close" else ""
                    
                    await ws.update(f"E{idx}:G{idx}", [[closed_at_str, status_str, amount_str]])
                    break
        except Exception as e:
            logger.error(f"Google Sheets status update failed: {e}")

def main():
    if not TELEGRAM_TOKEN:
        logger.error("BOT_TOKEN is missing!")
        return
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    
    # Message parser listener node for broad member registry synchronization
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, track_everyone_message))
    
    # Registration of command routing nodes
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("newevent", newevent))
    app.add_handler(CommandHandler("editevent", editevent))
    app.add_handler(CommandHandler("notify", notify))
    app.add_handler(CommandHandler("adduser", adduser))
    app.add_handler(CommandHandler("updateuser", updateuser))
    app.add_handler(CommandHandler("listusers", listusers))
    
    app.add_handler(CallbackQueryHandler(button_handler))
    
    logger.info("Bot started successfully...")
    app.run_polling()

if __name__ == "__main__":
    main()