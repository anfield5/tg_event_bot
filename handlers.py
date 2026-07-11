import json
import re
import sqlite3
from uuid import uuid4

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import DEFAULT_GOING_ICON, DEFAULT_NOTGOING_ICON, DEFAULT_CLOSE_ICON, logger
from utils import escape_markdown, now2ddmmyy
from db import track_user
from sheets import get_sheet_for_chat, log_action_to_google, agcm, sync_event_users_to_google

def parse_user_args(args):
    raw_string = " ".join(args)
    tokens = re.split(r'[\s,]+', raw_string)
    return [t.lstrip('@').strip() for t in tokens if t.strip()]

def create_event_keyboard(event_id, is_open, going_icon, notgoing_icon, going_list=None, counters=None):
    """
    Generates dynamic contextual keyboards based on the event state:
    is_open = 1: Active registration layout
    is_open = 2: Admin validation layout (Verification Mode)
    is_open = 0: Fully locked state (Empty keyboard)
    """
    if is_open == 0:
        return InlineKeyboardMarkup([])

    buttons = []

    # State 1: Standard Active Voting Mode
    if is_open == 1:
        buttons.append([
            InlineKeyboardButton(f"{going_icon} Going", callback_data=f"going_{event_id}"),
            InlineKeyboardButton(f"{notgoing_icon} Not Going", callback_data=f"notgoing_{event_id}")
        ])
        buttons.append([
            InlineKeyboardButton("➕ Add Guest", callback_data=f"add_{event_id}"),
            InlineKeyboardButton("➖ Remove Guest", callback_data=f"sub_{event_id}")
        ])
        buttons.append([
            InlineKeyboardButton(f"{DEFAULT_CLOSE_ICON} Close Event", callback_data=f"close_{event_id}")
        ])
        return InlineKeyboardMarkup(buttons)

    # State 2: Verification / Roster Correction Mode (Admin Only GUI)
    if is_open == 2:
        going_list = going_list or []
        counters = counters or {}

        # Collect all unique usernames that either are going or have active guest counters
        all_tracked_users = set()
        for entry in going_list:
            all_tracked_users.add(entry.split(" (")[0])
        for username in counters.keys():
            all_tracked_users.add(username)

        # Render explicit rows for every relevant player setup
        for username in sorted(all_tracked_users):
            guest_count = counters.get(username, 0)
            
            # Check if this user is actually in the going list or just a phantom guest container
            is_present = any(u.split(" (")[0] == username for u in going_list)
            
            # Visual indicator: if user is not going themselves, add a small warning sign
            status_name = username if is_present else f"💤 {username}"
            
            row = [
                InlineKeyboardButton(f"❌ {status_name}", callback_data=f"kick_{event_id}:{username}"),
                InlineKeyboardButton("➖", callback_data=f"decgst_{event_id}:{username}"),
                InlineKeyboardButton(f"{guest_count} G.", callback_data="noop"),
                InlineKeyboardButton("➕", callback_data=f"incgst_{event_id}:{username}")
            ]
            buttons.append(row)

        # Dynamic management tools row
        buttons.append([
            InlineKeyboardButton("➕ Add Extra Player", callback_data=f"addext_{event_id}")
        ])
        # Final commitment action trigger row
        buttons.append([
            InlineKeyboardButton("💾 Save & Sync to Google", callback_data=f"save_{event_id}")
        ])
        return InlineKeyboardMarkup(buttons)

    return InlineKeyboardMarkup([])

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "*Event Bot Commands:*\n\n"
        "/newevent `event_name` `going_icon` `notgoing_icon` — create a new event\n"
        "/editevent `event_name` `going_icon` `notgoing_icon` — update open event details\n"
        "/notify `text_msg` — notify active users who haven't responded\n"
        "/adduser `user1, user2` — add users into tracking matrix\n"
        "/updateuser `user1` `-passive/-active` — change user status\n"
        "/listusers — show tracking user roster manifest\n"
        "/help — show this help manifest"
    )
    await update.message.reply_text(help_text, parse_mode="MarkdownV2")

async def track_everyone_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or not update.effective_user:
        return

    chat_id = str(update.effective_chat.id)
    user = update.effective_user
    username_raw = user.username if user.username else user.first_name
    
    # 1. Check if an admin is currently adding an extra player manually
    if "awaiting_extra_player_for" in context.user_data:
        # Verify if the sender is an admin
        chat_member = await context.bot.get_chat_member(chat_id=update.effective_chat.id, user_id=user.id)
        if chat_member.status in ["administrator", "creator"]:
            event_id = context.user_data.pop("awaiting_extra_player_for")
            extra_name = update.message.text.strip().lstrip('@')
            
            # Delete admin's text message to keep the group history clean
            try:
                await update.message.delete()
            except Exception:
                pass

            # Inject the new name directly into SQLite arrays
            conn = sqlite3.connect("database.db")
            cursor = conn.cursor()
            cursor.execute("SELECT name, going_icon, notgoing_icon, going_data, notgoing_data, counters_data FROM events WHERE event_id = ?", (event_id,))
            row = cursor.fetchone()
            
            if row:
                name, going_icon, notgoing_icon, going_data, notgoing_data, counters_data = row
                going = json.loads(going_data)
                not_going = set(json.loads(notgoing_data))
                counters = json.loads(counters_data)
                
                # Check duplication layout boundaries
                going_usernames = {u.split(" (")[0] for u in going}
                if extra_name not in going_usernames:
                    # Using a placeholder ID since this was manually typed by admin
                    going.append(f"{extra_name} (000000)") 
                    not_going.discard(extra_name)
                    
                    cursor.execute("UPDATE events SET going_data = ?, notgoing_data = ? WHERE event_id = ?", 
                                   (json.dumps(going), json.dumps(list(not_going)), event_id))
                    conn.commit()
                
                # Re-render the validation screen block instantly
                going_list_text = "\n".join([f"• {escape_markdown(u.split(' (')[0])}" for u in going]) if going else ""
                counter_lines = [f"• {escape_markdown(k)} \(\+{count} g\.\)" for k, count in counters.items()]
                counter_text = "\n".join(counter_lines) if counter_lines else ""
                not_going_list_text = "\n".join([f"• {escape_markdown(u)}" for u in not_going]) if not_going else ""
                total_going = len(going) + sum(counters.values())
                
                text = (
                    f"⚠️ *ROSTER VERIFICATION IN PROGRESS*\n_Review structural datasets before save_\n\n"
                    f"*{escape_markdown(name)}*\n\n"
                    f"{going_icon} *Going* \({total_going}\):\n{going_list_text}\n"
                    f"{'' if not counter_text else '*Guests:*'}\n{counter_text}\n"
                    f"{notgoing_icon} *Not Going* \({len(not_going)}\):\n{not_going_list_text}"
                )
                
                keyboard = create_event_keyboard(event_id, 2, going_icon, notgoing_icon, going, counters)
                
                # Update the original poll layout anchor message
                cursor.execute("SELECT message_id FROM events WHERE event_id = ?", (event_id,))
                msg_row = cursor.fetchone()
                if msg_row:
                    try:
                        await context.bot.edit_message_text(chat_id=chat_id, message_id=int(msg_row[0]), text=text, reply_markup=keyboard, parse_mode="MarkdownV2")
                    except Exception as e:
                        logger.error(f"Failed to refresh UI after extra player addition: {e}")
            
            conn.close()
            return

    # 2. Default tracking behavior if context isn't locked by FSM admin operations
    track_user(chat_id, username_raw, "active")

async def newevent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /newevent <event_name> [going_icon] [notgoing_icon]")
        return

    event_name_raw = context.args[0]
    going_icon = context.args[1] if len(context.args) >= 2 else DEFAULT_GOING_ICON
    notgoing_icon = context.args[2] if len(context.args) >= 3 else DEFAULT_NOTGOING_ICON

    event_id = str(uuid4())[:8]
    chat_id = str(update.effective_chat.id)
    user = update.effective_user
    username_raw = user.username if user.username else user.first_name
    user_id = user.id
    
    track_user(chat_id, username_raw, "active")

    text = f"*{escape_markdown(event_name_raw)}*\n\n{going_icon} *Going* \(0\):\n\n{notgoing_icon} *Not Going* \(0\):\n"
    keyboard = create_event_keyboard(event_id, 1, going_icon, notgoing_icon)
    
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
        # Written by user_id instead of username string node
        await ws.append_row([event_id, event_name_raw, now2ddmmyy(), str(user_id), "", "OPEN", ""])
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
    
    if is_open != 1:
        await update.message.reply_text("Last event is not in active state and cannot be modified.")
        conn.close()
        return

    new_name = context.args[0]
    new_going = context.args[1] if len(context.args) >= 2 else current_going_icon
    new_notgoing = context.args[2] if len(context.args) >= 3 else current_notgoing_icon

    cursor.execute("UPDATE events SET name = ?, going_icon = ?, notgoing_icon = ? WHERE event_id = ?", (new_name, new_going, new_notgoing, event_id))
    conn.commit()
    conn.close()

    going = json.loads(going_data)
    not_going = json.loads(notgoing_data)
    counters = json.loads(counters_data)
    
    going_list_text = "\n".join([f"• {escape_markdown(u.split(' (')[0])}" for u in going]) if going else ""
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
    
    keyboard = create_event_keyboard(event_id, is_open, new_going, new_notgoing)
    
    try:
        await context.bot.edit_message_text(chat_id=chat_id, message_id=int(message_id), text=text, reply_markup=keyboard, parse_mode="MarkdownV2")
        await update.message.reply_text("Event metadata updated successfully.")
    except Exception as e:
        logger.error(f"Telegram UI update failed: {e}")

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

async def updateuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /updateuser <username> -active/-passive")
        return

    chat_id = str(update.effective_chat.id)
    raw_args = context.args

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
        await update.message.reply_text(f"Updated status to [{target_status}]: {', '.join(updated_users)}")

async def listusers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT username, status FROM chat_users WHERE chat_id = ? ORDER BY username ASC", (chat_id,))
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("No users registered yet.")
        return

    lines = [f"{r[0]} - {r[1]}" for r in rows]
    await update.message.reply_text("\n".join(lines))

async def notify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /notify <text>")
        return

    notification_msg = " ".join(context.args)
    chat_id = str(update.effective_chat.id)

    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT going_data, notgoing_data FROM events WHERE chat_id = ? ORDER BY rowid DESC LIMIT 1", (chat_id,))
    event_row = cursor.fetchone()
    
    if not event_row:
        await update.message.reply_text("No active events found.")
        conn.close()
        return

    going_users = {u.split(" (")[0] for u in json.loads(event_row[0])}
    notgoing_users = set(json.loads(event_row[1]))
    decided_users = going_users.union(notgoing_users)

    cursor.execute("SELECT username FROM chat_users WHERE chat_id = ? AND status = 'active'", (chat_id,))
    active_known_users = {r[0] for r in cursor.fetchall()}
    conn.close()

    silent_users = active_known_users.difference(decided_users)

    if not silent_users:
        await update.message.reply_text("All active users have made a decision.")
        return

    mentions = [f"@{u}" if not u.isdigit() else f"User {u}" for u in silent_users]
    await update.message.reply_text(f"{notification_msg}\n\n*Please respond:*\n" + ", ".join(mentions))

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    callback_data = query.data
    
    # Check if this query comes from verification mode sub-actions (contains colon separator)
    if ":" in callback_data:
        action_prefix, target_username = callback_data.split(":", 1)
        action, event_id = action_prefix.split("_", 1)
    else:
        action, event_id = callback_data.split("_", 1)
        target_username = None
    
    # Fetch user chat interaction status details
    user = query.from_user
    chat_member = await context.bot.get_chat_member(chat_id=query.message.chat_id, user_id=user.id)
    is_admin = chat_member.status in ["administrator", "creator"]
    
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id, message_id, name, going_icon, notgoing_icon, is_open, going_data, notgoing_data, counters_data FROM events WHERE event_id = ?", (event_id,))
    row = cursor.fetchone()
    
    if not row:
        await query.answer("Event data array missing.", show_alert=True)
        conn.close()
        return
        
    chat_id, message_id, name, going_icon, notgoing_icon, is_open, going_data, notgoing_data, counters_data = row
    going = json.loads(going_data)
    not_going = set(json.loads(notgoing_data))
    counters = json.loads(counters_data)
    
    username_raw = user.username if user.username else user.first_name
    user_id = user.id

    track_user(chat_id, username_raw, "active")

    # Enforce global lock state guard check
    if is_open == 0:
        await query.answer("This event context is locked permanently!", show_alert=True)
        conn.close()
        return

    going_usernames = {u.split(" (")[0] for u in going}

    # Restrict administrative operational nodes to group admins
    if action in ["close", "kick", "modgst", "save"]:
        if not is_admin:
            await query.answer("Access Denied: Administrative permissions required.", show_alert=True)
            conn.close()
            return

    # Process Active Registration State Loops (is_open == 1)
    if is_open == 1:
        if action in ["going", "notgoing", "add", "sub"]:
            context.application.create_task(log_action_to_google(chat_id, event_id, action, username_raw, user_id))

        if action == "going":
            if username_raw not in going_usernames:
                going.append(f"{username_raw} ({user_id})")
            not_going.discard(username_raw)
            await query.answer("Status updated: Going")
        elif action == "notgoing":
            going = [u for u in going if u.split(" (")[0] != username_raw]
            not_going.add(username_raw)
            await query.answer("Status updated: Not Going")
        elif action == "add":
            counters[username_raw] = counters.get(username_raw, 0) + 1
            await query.answer(f"Guest registered (+{counters[username_raw]})")
        elif action == "sub":
            if username_raw in counters:
                if counters[username_raw] > 1:
                    counters[username_raw] -= 1
                else:
                    counters.pop(username_raw)
                await query.answer("Guest unregistered")
            else:
                await query.answer("No extra guests matched.", show_alert=True)
                conn.close()
                return
        elif action == "close":
            # Shift state boundary index matrix forward into Verification Mode (is_open = 2)
            is_open = 2
            await query.answer("Roster verification mode engaged.")
    # Process Admin Roster Verification State Loops (is_open == 2)
    elif is_open == 2:

        if action == "kick" and target_username:
            # Check if this player has registered guests
            guest_count = counters.get(target_username, 0)
            
            if guest_count > 0:
                # If they have guests, we don't delete the row. 
                # We convert the player into a "pure guest container" (e.g., changing name visual indicator)
                # To do this safely without breaking IDs, we can append a special marker to their entry in 'going'
                # or just alert the admin to reduce guests to 0 first.
                await query.answer(f"@{target_username} has {guest_count} guests! Lower guests to 0 before kicking, or use 'Add Extra Player' for his friends.", show_alert=True)
                conn.close()
                return
            else:
                # If no guests, safe to delete completely
                going = [u for u in going if u.split(" (")[0] != target_username]
                counters.pop(target_username, None)
                await query.answer(f"Removed @{target_username}")    
            
        elif action == "incgst" and target_username:
            # Incremental step boost
            counters[target_username] = counters.get(target_username, 0) + 1
            await query.answer(f"Guests up for @{target_username}")
            
        elif action == "decgst" and target_username:
            # Decremental step reduction
            if target_username in counters:
                if counters[target_username] > 1:
                    counters[target_username] -= 1
                else:
                    counters.pop(target_username)
                await query.answer(f"Guests down for @{target_username}")
            else:
                await query.answer("No guests to remove.", show_alert=True)
                conn.close()
                return
                
        elif action == "addext":
            # Set state locks inside current conversation context to catch next text input
            context.user_data["awaiting_extra_player_for"] = event_id
            await query.answer("Type the extra player's name directly in this chat...", show_alert=True)
            conn.close()
            return
            
        elif action == "save":
            is_open = 0
            await query.answer("Data verified. Committing final structures...")

    # Write changes into internal persistence engine layer
    cursor.execute("UPDATE events SET is_open = ?, going_data = ?, notgoing_data = ?, counters_data = ? WHERE event_id = ?", 
                   (is_open, json.dumps(going), json.dumps(list(not_going)), json.dumps(counters), event_id))
    conn.commit()
    conn.close()

    # Rebuild Markdown text layouts
    going_list_text = "\n".join([f"• {escape_markdown(u.split(' (')[0])}" for u in going]) if going else ""
    counter_lines = [f"• {escape_markdown(k)} \(\+{count} g\.\)" for k, count in counters.items()]
    counter_text = "\n".join(counter_lines) if counter_lines else ""
    not_going_list_text = "\n".join([f"• {escape_markdown(u)}" for u in not_going]) if not_going else ""
    total_going = len(going) + sum(counters.values())
    
    # Prepend operational header alert layout nodes if in verification status state
    header = "⚠️ *ROSTER VERIFICATION IN PROGRESS*\n_Review structural datasets before save_\n\n" if is_open == 2 else ""
    
    text = (
        f"{header}*{escape_markdown(name)}*\n\n"
        f"{going_icon} *Going* \({total_going}\):\n{going_list_text}\n"
        f"{'' if not counter_text else '*Guests:*'}\n{counter_text}\n"
        f"{notgoing_icon} *Not Going* \({len(not_going)}\):\n{not_going_list_text}"
    )
    
    keyboard = create_event_keyboard(event_id, is_open, going_icon, notgoing_icon, going, counters)
    
    try:
        await query.edit_message_text(text=text, reply_markup=keyboard, parse_mode="MarkdownV2")
    except Exception as e:
        logger.error(f"Telegram UI update failed: {e}")

    # Final Synchronization Step Pipeline (Only runs when 'save' token passes cleanly)
    if action == "save":
        try:
            sheet_target = await get_sheet_for_chat(chat_id)
            gc = await agcm.authorize()
            ss = await gc.open(sheet_target)
            ws = await ss.worksheet("Events")
            records = await ws.get_all_records()
            for idx, r in enumerate(records, start=2):
                if str(r.get("EVENT_ID")) == str(event_id):
                    await ws.update(f"E{idx}:G{idx}", [[now2ddmmyy(), "CLOSED", total_going]])
                    break
            
            # Filter and parse explicit real user_id integers from the validated dataset array
            going_ids = []
            for entry in going:
                match = re.search(r'\((\d+)\)', entry)
                if match:
                    going_ids.append(match.group(1))
            
            # Fire sync arrays up into EventUsers manifest matrix
            context.application.create_task(sync_event_users_to_google(chat_id, event_id, going_ids))
        except Exception as e:
            logger.error(f"Google Sheets final metrics log failed: {e}")