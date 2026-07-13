import json
import re
import sqlite3
from uuid import uuid4

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from telegram.error import BadRequest

from config import DEFAULT_GOING_ICON, DEFAULT_NOTGOING_ICON, DEFAULT_CLOSE_ICON, logger
from utils import escape_markdown, now2ddmmyy
from db import track_user
from sheets import get_sheet_for_chat, agcm, sync_event_users_to_google

MODE_MAP = {
    "-v": "-visible", "--visible": "-visible", "-visible": "-visible",
    "-h": "-hidden", "--hidden": "-hidden", "-hidden": "-hidden",
    "-oc": "-onlycount", "--count": "-onlycount", "-onlycount": "-onlycount"
}

def parse_event_args(args):
    """
    Parses arguments for /newevent and /editevent to extract custom icons via flags.
    Supported pairs: -gi/--goingimage, -ni/--notgoingimage
    Returns: (event_name, going_icon, notgoing_icon)
    """
    going_icon = None
    notgoing_icon = None
    
    gi_flags = ["-gi", "-goingimage"]
    ni_flags = ["-ni", "-notgoingimage"]
    
    tokens = args[:]
    clean_tokens = []
    
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in gi_flags and i + 1 < len(tokens):
            going_icon = tokens[i+1]
            i += 2
        elif token in ni_flags and i + 1 < len(tokens):
            notgoing_icon = tokens[i+1]
            i += 2
        else:
            clean_tokens.append(token)
            i += 1
            
    event_name = " ".join(clean_tokens) if clean_tokens else None
    return event_name, going_icon, notgoing_icon

def parse_user_args(args):
    """
    Tokenizes arguments and strips formatting symbols like '@' from names.
    """
    if not args:
        return []
    raw_string = " ".join(args)
    tokens = re.split(r'[\s,]+', raw_string)
    return [t.lstrip('@').strip() for t in tokens if t.strip()]

def create_event_keyboard(event_id, is_open, going_icon, notgoing_icon, going_list=None, counters=None, is_child=False, child_users_rows=None):
    """
    Generates dynamic inline keyboards. Natively uses customized event icons for both master and child views.
    Includes active cross-network administrative layout items inside verification matrix.
    """
    if is_open == 0:
        return InlineKeyboardMarkup([])
        
    buttons = []
    if is_open == 1:
        buttons.append([
            InlineKeyboardButton(f"{going_icon} Going", callback_data=f"going_{event_id}"),
            InlineKeyboardButton(f"{notgoing_icon} Not Going", callback_data=f"notgoing_{event_id}")
        ])
        buttons.append([
            InlineKeyboardButton("➕ Add Guest", callback_data=f"add_{event_id}"),
            InlineKeyboardButton("➖ Sub Guest", callback_data=f"sub_{event_id}")
        ])
        if not is_child:
            buttons.append([
                InlineKeyboardButton(f"{DEFAULT_CLOSE_ICON} Verification Mode", callback_data=f"close_{event_id}")
            ])
    elif is_open == 2 and not is_child:
        # 1. Render primary master hub participants buttons
        if going_list:
            for entry in going_list:
                username = entry.split(" (")[0]
                guest_count = counters.get(username, 0) if counters else 0
                buttons.append([
                    InlineKeyboardButton(f"👤 {username}", callback_data="noop"),
                    InlineKeyboardButton(f"{guest_count} G.", callback_data="noop"),
                    InlineKeyboardButton("➕", callback_data=f"incgst_{event_id}:{username}"),
                    InlineKeyboardButton("➖", callback_data=f"decgst_{event_id}:{username}"),
                    InlineKeyboardButton("❌ Kick", callback_data=f"kick_{event_id}:{username}")
                ])
        
        # 2. Render dynamic child nodes participants buttons with cross-network target identifiers
        if child_users_rows:
            for ch_username, ch_guests in child_users_rows:
                buttons.append([
                    InlineKeyboardButton(f"📢 {ch_username}", callback_data="noop"),
                    InlineKeyboardButton(f"{ch_guests} G.", callback_data="noop"),
                    InlineKeyboardButton("➕", callback_data=f"incgst_{event_id}:ch-{ch_username}"),
                    InlineKeyboardButton("➖", callback_data=f"decgst_{event_id}:ch-{ch_username}"),
                    InlineKeyboardButton("❌ Kick", callback_data=f"kick_{event_id}:ch-{ch_username}")
                ])

        buttons.append([
            InlineKeyboardButton("➕ Add Extra Player", callback_data=f"addext_{event_id}")
        ])
        buttons.append([
            InlineKeyboardButton("💾 Save & Lock Roster", callback_data=f"save_{event_id}")
        ])
    return InlineKeyboardMarkup(buttons)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Renders structured MarkdownV2 help map documentation interface.
    """
    help_text = (
        "📖 *Available Commands Map*\n\n"
        "/newevent \\[name\\] \\- Initialize a new registration event\n"
        "/editevent \\- Modify setup interface components\n"
        "/notify \\- Broadcast operational triggers\n"
        "/adduser \\[username\\] \\- Force add user tracking context\n"
        "/updateuser \\[username\\] \\[status\\] \\- Update tracking status\n"
        "/listusers \\- Render project user database analytics\n\n"
        "⚙️ *Alias Subsystem:*\n"
        "/setalias \\[target\\_id\\] \\[aliasname\\] \\- Bind custom layout alias\n"
        "/removealias \\[aliasname\\] \\- Delete alias link\n"
        "/listalias \\- View mapped configuration aliases\n\n"
        "📢 *Distribution Control:*\n"
        "/shareevent \\[target\\_alias/id\\] \\[\\-v \\| \\-h \\| \\-oc\\] \\- Share active event node"
    )
    await update.message.reply_text(help_text, parse_mode="MarkdownV2")


# ==================== ALIAS ROUTING SYSTEM ====================

async def setalias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Binds a custom alpha-numerical alias string to a physical Telegram Chat ID destination.
    """
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("❌ *Syntax error:* Usage: `/setalias [id_group/id_channel] [aliasname]`", parse_mode="MarkdownV2")
        return

    target_chat_input = args[0].strip()
    alias_name = args[1].strip().lower()
    user_id = update.effective_user.id

    try:
        if target_chat_input.startswith("-") and target_chat_input[1:].isdigit():
            target_chat_id = int(target_chat_input)
        elif target_chat_input.isdigit():
            target_chat_id = int(target_chat_id)
        else:
            target_chat_id = target_chat_input
    except ValueError:
        await update.message.reply_text("No channel/group with such ID", parse_mode="MarkdownV2")
        return

    try:
        await context.bot.get_chat(target_chat_id)
    except BadRequest:
        await update.message.reply_text("No channel/group with such ID", parse_mode="MarkdownV2")
        return
    except Exception:
        await update.message.reply_text("Add @EventPlanCheckBot to target group/channel as admin.", parse_mode="MarkdownV2")
        return

    try:
        bot_member = await context.bot.get_chat_member(chat_id=target_chat_id, user_id=context.bot.id)
        if bot_member.status not in ["administrator", "creator"]:
            await update.message.reply_text("Add @EventPlanCheckBot to target group/channel as admin.", parse_mode="MarkdownV2")
            return
    except Exception:
        await update.message.reply_text("Add @EventPlanCheckBot to target group/channel as admin.", parse_mode="MarkdownV2")
        return

    try:
        user_member = await context.bot.get_chat_member(chat_id=target_chat_id, user_id=user_id)
        if user_member.status not in ["administrator", "creator"]:
            await update.message.reply_text("Only users with admin rights in target groups/channels can make event shares to them", parse_mode="MarkdownV2")
            return
    except Exception:
        await update.message.reply_text("Only users with admin rights in target groups/channels can make event shares to them", parse_mode="MarkdownV2")
        return

    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id FROM chat_aliases WHERE alias = ?", (alias_name,))
    if cursor.fetchone():
        await update.message.reply_text("Alias already exist", parse_mode="MarkdownV2")
        conn.close()
        return

    cursor.execute("SELECT alias FROM chat_aliases WHERE chat_id = ?", (str(target_chat_id),))
    existing_alias = cursor.fetchone()
    if existing_alias:
        await update.message.reply_text("⚠️ This group or channel has already been added. Please check its existing alias.", parse_mode="MarkdownV2")
        conn.close()
        return

    cursor.execute("INSERT INTO chat_aliases (chat_id, alias) VALUES (?, ?)", (str(target_chat_id), alias_name))
    conn.commit()
    conn.close()

    await update.message.reply_text(f"✅ Alias `__{escape_markdown(alias_name)}__` mapped to node ID `{target_chat_id}`\.", parse_mode="MarkdownV2")


async def removealias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Unlinks and purges custom routing metadata keys from storage indices.
    """
    args = context.args
    if not args:
        await update.message.reply_text("❌ *Syntax error:* Usage: `/removealias [aliasname]`", parse_mode="MarkdownV2")
        return

    alias_name = args[0].strip().lower()
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id FROM chat_aliases WHERE alias = ?", (alias_name,))
    if not cursor.fetchone():
        await update.message.reply_text("🔍 Alias not found.", parse_mode="MarkdownV2")
        conn.close()
        return

    cursor.execute("DELETE FROM chat_aliases WHERE alias = ?", (alias_name,))
    conn.commit()
    conn.close()

    await update.message.reply_text(f"🗑️ Alias `__{escape_markdown(alias_name)}__` purged from environments.", parse_mode="MarkdownV2")


async def listalias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Renders custom routing layouts using a key-value structural representation matching user UX criteria.
    Properly escapes ID hyphens for MarkdownV2 parsing compatibility.
    """
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT alias, chat_id FROM chat_aliases")
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("📋 No configuration routes active in network.", parse_mode="MarkdownV2")
        return

    blocks = []
    for alias, cid in rows:
        try:
            chat_obj = await context.bot.get_chat(int(cid) if cid.replace("-", "").isdigit() else cid)
            c_name = chat_obj.title or "Unknown"
            c_type = "Public Channel" if chat_obj.type == "channel" else "Group"
        except Exception:
            c_name = "Node Disconnected"
            c_type = "Unknown"
            
        block = (
            f"Aliasname: {escape_markdown(alias)}\n"
            f"Type: {escape_markdown(c_type)}\n"
            f"Name: {escape_markdown(c_name)}\n"
            f"ID: {escape_markdown(str(cid))}"
        )
        blocks.append(block)

    text = "📋 *System Distribution Routes Map:*\n\n" + "\n\n".join(blocks)
    await update.message.reply_text(text, parse_mode="MarkdownV2")


# ==================== OPERATIONAL CORE LIFECYCLE ====================

async def newevent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Initializes a localized database schema state tracker object for interactive polling.
    Parses customized icon flags natively.
    """
    chat_id = str(update.effective_chat.id)
    message = update.message
    args = context.args
    user_raw = update.effective_user.username if update.effective_user.username else update.effective_user.first_name
    if not args:
        await message.reply_text("❌ *Syntax execution failure:* Event name string parameter token is required\.", parse_mode="MarkdownV2")
        return
        
    event_name_raw, g_icon, n_icon = parse_event_args(args)
    going_icon = g_icon if g_icon else DEFAULT_GOING_ICON
    notgoing_icon = n_icon if n_icon else DEFAULT_NOTGOING_ICON
    
    event_id = str(uuid4())[:8]
    
    try:
        conn = sqlite3.connect("database.db")
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon, is_open, going_data, notgoing_data, counters_data)
            VALUES (?, ?, ?, ?, ?, ?, 1, '[]', '[]', '{}')
        """, (event_id, chat_id, str(message.message_id), event_name_raw, going_icon, notgoing_icon))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to save new event to database: {e}")
        await message.reply_text("❌ *Database operational crash:* Could not initialize event transaction\.", parse_mode="MarkdownV2")
        return

    text = (
        f"*{escape_markdown(event_name_raw)}*\n\n"
        f"{going_icon} *Going* \(0\):\n\n"
        f"{notgoing_icon} *Not Going* \(0\):\n"
    )
    keyboard = create_event_keyboard(event_id, 1, going_icon, notgoing_icon, [], {})
    
    try:
        sent_msg = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard, parse_mode="MarkdownV2")
        conn = sqlite3.connect("database.db")
        cursor = conn.cursor()
        cursor.execute("UPDATE events SET message_id = ? WHERE event_id = ?", (str(sent_msg.message_id), event_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to deploy main event interface view: {e}")

    # Log initial creation parameters to Events Google Sheet right away
    try:
        sheet_target = await get_sheet_for_chat(chat_id)
        gc = await agcm.authorize()
        ss = await gc.open(sheet_target)
        ws = await ss.worksheet("Events")
        # Structure: EVENT_ID, EVENT_NAME, CREATED_AT, CREATED_BY, CLOSED_AT, STATUS, AMOUNT
        await ws.append_row([event_id, event_name_raw, now2ddmmyy(), user_raw, "", "OPEN", 0])
    except Exception as e:
        logger.error(f"Failed to log initial event creation row to Google Sheets: {e}")


async def editevent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Modifies the active event metadata parameters (name, icons) and updates all deployment frames.
    """
    chat_id = str(update.effective_chat.id)
    args = context.args
    if not args:
        await update.message.reply_text("❌ *Syntax error:* Expected parameters to modify event layout\.", parse_mode="MarkdownV2")
        return

    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT event_id, name, going_icon, notgoing_icon FROM events 
        WHERE chat_id = ? AND is_open > 0 
        ORDER BY ROWID DESC LIMIT 1
    """, (chat_id,))
    row = cursor.fetchone()
    
    if not row:
        conn.close()
        await update.message.reply_text("❌ *Lookup failure:* No active event found to modify\.", parse_mode="MarkdownV2")
        return
        
    event_id, current_name, current_gi, current_ni = row
    new_name, new_gi, new_ni = parse_event_args(args)
    
    updated_name = new_name if new_name else current_name
    updated_gi = new_gi if new_gi else current_gi
    updated_ni = new_ni if new_ni else current_ni
    
    cursor.execute("""
        UPDATE events 
        SET name = ?, going_icon = ?, notgoing_icon = ? 
        WHERE event_id = ?
    """, (updated_name, updated_gi, updated_ni, event_id))
    conn.commit()
    conn.close()
    
    await update.message.reply_text("⚙️ *Configuration updated:* Refreshing network layout views\.", parse_mode="MarkdownV2")
    context.application.create_task(update_all_shared_views(context, event_id))


async def notify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Broadcasts a notification ping to all registered active chat users 
    who have not yet interacted with the active event (neither Going nor Not Going).
    """
    chat_id = str(update.effective_chat.id)
    message = update.message

    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT event_id, name, going_data, notgoing_data FROM events 
        WHERE chat_id = ? AND is_open = 1 
        ORDER BY ROWID DESC LIMIT 1
    """, (chat_id,))
    event_row = cursor.fetchone()
    
    if not event_row:
        conn.close()
        await message.reply_text("❌ *Notification failure:* No active registration event found to broadcast\.", parse_mode="MarkdownV2")
        return
        
    event_id, event_name, going_data, notgoing_data = event_row
    
    going_users = {u.split(" (")[0] for u in json.loads(going_data)}
    notgoing_users = set(json.loads(notgoing_data))
    decided_users = going_users.union(notgoing_users)
    
    cursor.execute("SELECT username FROM chat_users WHERE chat_id = ? AND status = 'active'", (chat_id,))
    all_active_rows = cursor.fetchall()
    conn.close()
    
    if not all_active_rows:
        await message.reply_text("📊 *Notification state:* No active tracked users registered in this node database\.", parse_mode="MarkdownV2")
        return
        
    pending_mentions = []
    for (uname,) in all_active_rows:
        if uname and uname not in decided_users:
            pending_mentions.append(f"@{escape_markdown(uname)}")
            
    if not pending_mentions:
        await message.reply_text("✅ *All systems clear:* Every registered user has already responded to the active roster\.", parse_mode="MarkdownV2")
        return
        
    header_text = f"🔔 *Notification Alert for Event:* `{escape_markdown(event_name)}`\n_Please submit your operational status_\n\n"
    await message.reply_text(header_text, parse_mode="MarkdownV2")
    
    chunk_size = 5
    for i in range(0, len(pending_mentions), chunk_size):
        chunk = pending_mentions[i:i + chunk_size]
        await message.reply_text(" ".join(chunk))


async def adduser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    args = parse_user_args(context.args)
    if not args:
        await update.message.reply_text("❌ *Syntax error:* Missing valid user reference tokens\.", parse_mode="MarkdownV2")
        return
    for u in args:
        track_user(chat_id, u, "active")
    await update.message.reply_text(f"✅ Registered tracking matrix context mapping for {len(args)} components\.", parse_mode="MarkdownV2")

async def updateuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("❌ *Syntax error:* Usage: `/updateuser [username] [active|frozen]`\.", parse_mode="MarkdownV2")
        return
    username = args[0].lstrip('@').strip()
    status = args[1].lower().strip()
    if status not in ["active", "frozen"]:
        await update.message.reply_text("❌ *Validation error:* Status parameters must be strictly 'active' or 'frozen'\.", parse_mode="MarkdownV2")
        return
    track_user(chat_id, username, status)
    await update.message.reply_text(f"⚙️ Structural configuration metadata matching updated for @{escape_markdown(username)}\.", parse_mode="MarkdownV2")

async def listusers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT username, status FROM chat_users WHERE chat_id = ?", (chat_id,))
    rows = cursor.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("📊 No trackable registration mapping datasets found for this cluster node\.", parse_mode="MarkdownV2")
        return
    lines = [f"• @{escape_markdown(r[0])} \(`{escape_markdown(r[1])}`\)" for r in rows]
    text = "📊 *Registered Project Component Nodes:*\n\n" + "\n".join(lines)
    await update.message.reply_text(text, parse_mode="MarkdownV2")


# ==================== DISTRIBUTION ENGAGEMENT ENGINE ====================

async def shareevent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Forwards and binds synchronized sub-views to child chats/channels.
    All error messages are strictly routed back to the main hub group.
    """
    current_chat_obj = update.effective_chat
    main_hub_chat_id = current_chat_obj.id
    user_id = update.effective_user.id
    args = context.args

    if current_chat_obj.type not in ["group", "supergroup"]:
        await context.bot.send_message(
            chat_id=main_hub_chat_id,
            text="❌ This command can only be executed within the main control hub group\.",
            parse_mode="MarkdownV2"
        )
        return

    if len(args) < 1:
        await context.bot.send_message(
            chat_id=main_hub_chat_id,
            text="❌ *Syntax error:* Expected: `/shareevent [target_alias/id] [mode_optional]`",
            parse_mode="MarkdownV2"
        )
        return

    target_input = args[0].strip()
    mode = "-visible"
    if len(args) > 1 and args[1].strip().lower() in MODE_MAP:
        mode = MODE_MAP[args[1].strip().lower()]

    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT event_id, name, is_open, going_icon, notgoing_icon 
        FROM events 
        WHERE chat_id = ? AND is_open > 0 
        ORDER BY ROWID DESC LIMIT 1
    """, (str(main_hub_chat_id),))
    event_row = cursor.fetchone()
    
    if not event_row:
        conn.close()
        await context.bot.send_message(
            chat_id=main_hub_chat_id,
            text="❌ *Lookup failure:* No active event session found generated by this group\.",
            parse_mode="MarkdownV2"
        )
        return

    event_id, name, is_open, going_icon, notgoing_icon = event_row

    cursor.execute("SELECT chat_id FROM chat_aliases WHERE alias = ?", (target_input.lower(),))
    alias_row = cursor.fetchone()
    target_chat_raw = alias_row[0] if alias_row else target_input

    if str(target_chat_raw) == str(main_hub_chat_id):
        conn.close()
        await context.bot.send_message(
            chat_id=main_hub_chat_id,
            text="⚠️ This group or channel has already been added. Please check its existing alias.",
            parse_mode="MarkdownV2"
        )
        return

    cursor.execute("SELECT message_id FROM event_shares WHERE event_id = ? AND chat_id = ?", (event_id, str(target_chat_raw)))
    if cursor.fetchone():
        conn.close()
        await context.bot.send_message(
            chat_id=main_hub_chat_id,
            text="⚠️ This group or channel has already been added. Please check its existing alias.",
            parse_mode="MarkdownV2"
        )
        return

    conn.close()

    try:
        if target_chat_raw.startswith("-") and target_chat_raw[1:].isdigit():
            target_chat_api = int(target_chat_raw)
        elif target_chat_raw.isdigit():
            target_chat_api = int(target_chat_raw)
        else:
            target_chat_api = target_chat_raw
    except ValueError:
        await context.bot.send_message(
            chat_id=main_hub_chat_id,
            text="🔍 Group or channel not found. Please double-check the ID and try again.",
            parse_mode="MarkdownV2"
        )
        return

    # 1. Verify bot access to target node
    try:
        target_chat_obj = await context.bot.get_chat(target_chat_api)
        chat_type_flag = "channel" if target_chat_obj.type == "channel" else "group"
    except BadRequest as br:
        logger.error(f"Shareevent get_chat BadRequest: {br}")
        await context.bot.send_message(
            chat_id=main_hub_chat_id,
            text="🔍 Target destination node not found. Ensure the ID/alias is valid and the bot is present there.",
            parse_mode="MarkdownV2"
        )
        return
    except Exception as e:
        logger.error(f"Shareevent get_chat unexpected error: {e}")
        await context.bot.send_message(
            chat_id=main_hub_chat_id,
            text="🤖 Event Bot is missing or cannot access the target node. Please add the bot to the destination group or channel first.",
            parse_mode="MarkdownV2"
        )
        return

    # 2. Verify bot administrative privileges
    try:
        bot_member = await context.bot.get_chat_member(chat_id=target_chat_api, user_id=context.bot.id)
        if bot_member.status not in ["administrator", "creator"]:
            await context.bot.send_message(
                chat_id=main_hub_chat_id,
                text="🤖 Event Bot is present but has no admin rights. Please promote the bot in the target node.",
                parse_mode="MarkdownV2"
            )
            return
    except Exception as e:
        logger.error(f"Shareevent bot privileges check failed: {e}")
        await context.bot.send_message(
            chat_id=main_hub_chat_id,
            text="🤖 Could not verify Bot privileges in target node. Ensure it is promoted to admin.",
            parse_mode="MarkdownV2"
        )
        return

    # 3. Verify user administrative privileges and actual presence in the target node
    try:
        user_member = await context.bot.get_chat_member(chat_id=target_chat_api, user_id=user_id)
        if user_member.status not in ["administrator", "creator"]:
            await context.bot.send_message(
                chat_id=main_hub_chat_id,
                text="⛔️ Admin rights required. To use /shareevent, you need to be an administrator in the target group or channel.",
                parse_mode="MarkdownV2"
            )
            return
    except BadRequest as br:
        logger.error(f"Shareevent user verification BadRequest: {br}")
        await context.bot.send_message(
            chat_id=main_hub_chat_id,
            text="❌ *Access Denied:* You are not a member of the target chat or the bot cannot verify your profile there\.",
            parse_mode="MarkdownV2"
        )
        return
    except Exception as e:
        logger.error(f"Shareevent user verification failed: {e}")
        await context.bot.send_message(
            chat_id=main_hub_chat_id,
            text="⛔️ Structural validation error. Failed to confirm your administrative status in the target node.",
            parse_mode="MarkdownV2"
        )
        return

    # 4. Attempt to deploy the synchronized sub-view message
    try:
        sent = await context.bot.send_message(
            chat_id=target_chat_api,
            text=f"📢 *SHARED EVENT: {escape_markdown(name)}*\n_Synchronizing active event streams\.\.\._",
            parse_mode="MarkdownV2"
        )
        
        conn = sqlite3.connect("database.db")
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type)
            VALUES (?, ?, ?, ?, ?)
        """, (event_id, str(target_chat_api), str(sent.message_id), mode, chat_type_flag))
        conn.commit()
        conn.close()
        
        await context.bot.send_message(
            chat_id=main_hub_chat_id,
            text="🚀 Content distribution successful for target context node\.",
            parse_mode="MarkdownV2"
        )
    except Exception as e:
        logger.error(f"Failed to instantiate link sharing: {e}")
        await context.bot.send_message(
            chat_id=main_hub_chat_id,
            text="❌ *Interface sharing failed:* Deployment execution fault\.",
            parse_mode="MarkdownV2"
        )
        return

    context.application.create_task(update_all_shared_views(context, event_id))


async def track_everyone_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Interceptors for keyword mentions to call registered cluster components.
    """
    message = update.effective_message
    if not message or not message.text:
        return
    text_raw = message.text
    if "@everyone" in text_raw or "everyone" in text_raw.lower():
        chat_id = str(update.effective_chat.id)
        conn = sqlite3.connect("database.db")
        cursor = conn.cursor()
        cursor.execute("SELECT username FROM chat_users WHERE chat_id = ? AND status = 'active'", (chat_id,))
        rows = cursor.fetchall()
        conn.close()
        if not rows:
            return
        mentions = [f"@{r[0]}" for r in rows if r[0]]
        if mentions:
            chunk_size = 5
            for i in range(0, len(mentions), chunk_size):
                chunk = mentions[i:i + chunk_size]
                await message.reply_text(" ".join(chunk))


async def update_all_shared_views(context: ContextTypes.DEFAULT_TYPE, event_id: str):
    """
    Cascades layout rendering changes to all downstream linked cluster endpoints.
    Ensures mathematical mapping rules are fully met.
    """
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id, message_id, name, going_icon, notgoing_icon, is_open, going_data, notgoing_data, counters_data FROM events WHERE event_id = ?", (event_id,))
    master = cursor.fetchone()
    if not master:
        conn.close()
        return
        
    main_chat_id, main_msg_id, name, going_icon, notgoing_icon, is_open, going_data, notgoing_data, counters_data = master
    master_going = json.loads(going_data)
    master_not_going = json.loads(notgoing_data)
    master_counters = json.loads(counters_data)
    
    cursor.execute("SELECT chat_id, message_id, share_mode FROM event_shares WHERE event_id = ?", (event_id,))
    shares = cursor.fetchall()
    
    child_data = {}
    total_child_going = 0
    master_shares_block = ""
    
    # Process localized dynamic counts for downstream child nodes
    child_addons_for_master = []
    for s_chat_id, _, _ in shares:
        cursor.execute("SELECT username, guests FROM event_users WHERE event_id = ? AND chat_id = ? AND status = 'going'", (event_id, str(s_chat_id)))
        users = cursor.fetchall()
        users_list = []
        chat_sum = 0
        for username, guests in users:
            guest_str = f" \(\+{guests} g\.\)" if guests > 0 else ""
            users_list.append(f"• {escape_markdown(username)}{guest_str}")
            chat_sum += 1 + guests
            
        child_data[str(s_chat_id)] = {
            "users_text": "\n".join(users_list) if users_list else "",
            "count": chat_sum
        }
        total_child_going += chat_sum

        try:
            chat_obj = await context.bot.get_chat(int(s_chat_id) if s_chat_id.replace("-", "").isdigit() else s_chat_id)
            chat_title = chat_obj.title or "Child Network"
        except Exception:
            chat_title = "Child Network"
        
        if chat_sum > 0:
            block = f"\n\n🟢 *Going from \\(\"{escape_markdown(chat_title)}\"\\)* \({chat_sum}\):\n" + "\n".join(users_list)
            child_addons_for_master.append(block)
        
    conn.close()
    
    # Compile multi-group visualization chunks safely outside loops
    if child_addons_for_master:
        master_shares_block = "".join(child_addons_for_master)
        
    total_master_going = len(master_going)
    total_master_guests = sum(master_counters.values())
    
    current_post_total = total_master_going + total_master_guests
    global_total = current_post_total + total_child_going
    
    going_list_text = "\n".join([f"• {escape_markdown(u.split(' (')[0])}" for u in master_going]) if master_going else ""
    counter_lines = []
    for entry in master_going:
        u_name = entry.split(' (')[0]
        if u_name in master_counters:
            counter_lines.append(f"• {escape_markdown(u_name)} \(\+{master_counters[u_name]} g\.\)")
    for k, count in master_counters.items():
        if k not in {u.split(' (')[0] for u in master_going}:
            counter_lines.append(f"• {escape_markdown(k)} \(\+{count} g\.\)")
            
    counter_text = "\n".join(counter_lines) if counter_lines else ""
    not_going_list_text = "\n".join([f"• {escape_markdown(u)}" for u in master_not_going]) if master_not_going else ""
    
    header = "⚠️ *ROSTER VERIFICATION IN PROGRESS*\n_Review structural datasets before save_\n\n" if is_open == 2 else ""

    master_text = (
        f"{header}*{escape_markdown(name)}*\n\n"
        f"{going_icon} *Going* \({total_master_going}\):\n{going_list_text}\n\n"
        f"👥 *Guests*:\n{counter_text if counter_text else '_No guests registered_'}\n\n"
        f"{notgoing_icon} *Not Going* \({len(master_not_going)}\):\n{not_going_list_text}"
        f"{master_shares_block}\n\n"
        f"📊 *TOTAL Going:* {global_total}"
    )
    
    conn_keyboard = sqlite3.connect("database.db")
    cursor_keyboard = conn_keyboard.cursor()
    cursor_keyboard.execute("SELECT username, guests FROM event_users WHERE event_id = ? AND status = 'going'", (event_id,))
    all_child_going_for_buttons = cursor_keyboard.fetchall()
    conn_keyboard.close()

    master_keyboard = create_event_keyboard(
        event_id, is_open, going_icon, notgoing_icon, 
        master_going, master_counters, is_child=False, 
        child_users_rows=all_child_going_for_buttons
    )
    
    try:
        await context.bot.edit_message_text(chat_id=int(main_chat_id), message_id=int(main_msg_id), text=master_text, reply_markup=master_keyboard, parse_mode="MarkdownV2")
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Master view UI sync crash: {e}")
    except Exception as e:
        logger.error(f"Master view UI sync crash: {e}")

    # Re-rendering downstream target node sub views layout frames
    for s_chat_id, s_msg_id, mode in shares:
        try:
            main_chat_obj = await context.bot.get_chat(int(main_chat_id))
            main_title = main_chat_obj.title or "Main Hub"
        except Exception:
            main_title = "Main Hub"

        c_info = child_data.get(str(s_chat_id), {"users_text": "", "count": 0})
        escaped_main_title = escape_markdown(main_title)
        
        if mode == "-visible":
            child_text = (
                f"📢 *SHARED EVENT: {escape_markdown(name)}*\n\n"
                f"🟢 *Going \({escaped_main_title}\)* \({current_post_total}\):\n{going_list_text}\n"
            )
            if counter_text:
                child_text += f"{counter_text}\n\n"
            else:
                child_text += "\n"
                
            for other_id, _, _ in shares:
                if str(other_id) != str(s_chat_id):
                    try:
                        o_obj = await context.bot.get_chat(int(other_id) if str(other_id).replace("-", "").isdigit() else other_id)
                        o_title = o_obj.title or "Other Group"
                    except Exception:
                        o_title = "Other Group"
                    o_info = child_data.get(str(other_id), {"users_text": "", "count": 0})
                    if o_info["count"] > 0:
                        child_text += f"🟢 *Going \({escape_markdown(o_title)}\)* \({o_info['count']}\):\n{o_info['users_text']}\n\n"
                        
        elif mode == "-onlycount":
            child_text = (
                f"📢 *SHARED EVENT: {escape_markdown(name)}*\n\n"
                f"🟢 *Going \({escaped_main_title}\):* {current_post_total}\n\n"
            )
            for other_id, _, _ in shares:
                if str(other_id) != str(s_chat_id):
                    try:
                        o_obj = await context.bot.get_chat(int(other_id) if str(other_id).replace("-", "").isdigit() else other_id)
                        o_title = o_obj.title or "Other Group"
                    except Exception:
                        o_title = "Other Group"
                    o_info = child_data.get(str(other_id), {"count": 0})
                    child_text += f"🟢 *Going \({escape_markdown(o_title)}\):* {o_info['count']}\n"
            child_text += "\n"
            
        elif mode == "-hidden":
            child_text = f"📢 *SHARED EVENT: {escape_markdown(name)}*\n\n_Data context hidden by administration\._\n\n"

        child_text += f"👉 *Going here:* \({c_info['count']}\)\n{c_info['users_text']}\n\n"
        child_text += f"📊 *Total Going\(all groups\):* {global_total}\n"
        
        child_keyboard = create_event_keyboard(event_id, is_open, going_icon, notgoing_icon, is_child=True)
        try:
            await context.bot.edit_message_text(chat_id=int(s_chat_id) if s_chat_id.replace("-", "").isdigit() else s_chat_id, message_id=int(s_msg_id), text=child_text, reply_markup=child_keyboard, parse_mode="MarkdownV2")
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                logger.error(f"Child view update failed for {s_chat_id}: {e}")
        except Exception as e:
            logger.error(f"Child view update failed for {s_chat_id}: {e}")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Main state machine input gateway processor handling structural button interactions.
    Handles verification modification arrays across primary and shared child datasets.
    """
    query = update.callback_query
    callback_data = query.data
    click_chat_id = str(query.message.chat_id)
    user = query.from_user
    user_id = user.id
    username_raw = user.username if user.username else user.first_name

    try:
        await query.answer()
    except Exception as e:
        logger.error(f"Failed to answer callback query: {e}")

    if callback_data == "noop":
        return

    action = None
    event_id = None
    target_username = None

    try:
        if ":" in callback_data:
            action_prefix, target_username = callback_data.split(":", 1)
            action, event_id = action_prefix.split("_", 1) if "_" in action_prefix else (None, None)
        else:
            action, event_id = callback_data.split("_", 1) if "_" in callback_data else (None, None)
    except Exception as e:
        logger.error(f"Critical exception raised during callback string parsing: {e}")
        return

    if not action or not event_id:
        return

    try:
        chat_member = await context.bot.get_chat_member(chat_id=query.message.chat_id, user_id=user.id)
        is_admin = chat_member.status in ["administrator", "creator"]
    except Exception as e:
        logger.error(f"Failed to extract chat member administrative rights layout: {e}")
        is_admin = False

    data_changed = False
    is_open = 1

    try:
        conn = sqlite3.connect("database.db")
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id, message_id, name, going_icon, notgoing_icon, is_open, going_data, notgoing_data, counters_data FROM events WHERE event_id = ?", (event_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return
            
        main_chat_id, main_msg_id, name, going_icon, notgoing_icon, is_open, going_data, notgoing_data, counters_data = row
        going = json.loads(going_data)
        not_going = set(json.loads(notgoing_data))
        counters = json.loads(counters_data)
        
        if is_open == 0:
            conn.close()
            return

        is_click_in_child = (int(click_chat_id) != int(main_chat_id))
        going_usernames = {u.split(" (")[0] for u in going}

        # ==================== CROSS-CHAT PROTECTION VALIDATION ENGINE ====================
        if action in ["going", "add", "sub"]:
            user_already_registered_somewhere = False
            
            if username_raw in going_usernames or username_raw in counters:
                if is_click_in_child:
                    user_already_registered_somewhere = True
            
            cursor.execute("SELECT chat_id FROM event_users WHERE event_id = ? AND user_id = ? AND status = 'going'", (event_id, str(user_id)))
            active_shares_records = cursor.fetchall()
            for (recorded_chat_id,) in active_shares_records:
                if str(recorded_chat_id) != str(click_chat_id):
                    user_already_registered_somewhere = True
                    break
                if not is_click_in_child:
                    user_already_registered_somewhere = True
                    break
                    
            if user_already_registered_somewhere:
                conn.close()
                try:
                    await query.answer(text="⚠️ You are already added to the event in different group/channel", show_alert=True)
                except Exception:
                    pass
                return

        # ==================== INTERACTION PROCESSORS ====================
        if is_click_in_child:
            if action not in ["going", "notgoing", "add", "sub"]:
                conn.close()
                return
            cursor.execute("SELECT status, guests FROM event_users WHERE event_id = ? AND chat_id = ? AND user_id = ?", (event_id, click_chat_id, str(user_id)))
            u_row = cursor.fetchone()
            current_status = u_row[0] if u_row else "none"
            current_guests = u_row[1] if u_row else 0
            
            if action == "going":
                if current_status == "going":
                    cursor.execute("DELETE FROM event_users WHERE event_id = ? AND chat_id = ? AND user_id = ?", (event_id, click_chat_id, str(user_id)))
                else:
                    cursor.execute("INSERT OR REPLACE INTO event_users (event_id, chat_id, user_id, username, status, guests) VALUES (?, ?, ?, ?, 'going', ?)", (event_id, click_chat_id, str(user_id), username_raw, current_guests))
                data_changed = True
            elif action == "notgoing":
                cursor.execute("INSERT OR REPLACE INTO event_users (event_id, chat_id, user_id, username, status, guests) VALUES (?, ?, ?, ?, 'notgoing', 0)", (event_id, click_chat_id, str(user_id), username_raw))
                data_changed = True
            elif action == "add":
                new_guests = current_guests + 1
                cursor.execute("INSERT OR REPLACE INTO event_users (event_id, chat_id, user_id, username, status, guests) VALUES (?, ?, ?, ?, 'going', ?)", (event_id, click_chat_id, str(user_id), username_raw, new_guests))
                data_changed = True
            elif action == "sub":
                if current_guests > 0:
                    new_guests = current_guests - 1
                    cursor.execute("UPDATE event_users SET guests = ? WHERE event_id = ? AND chat_id = ? AND user_id = ?", (new_guests, event_id, click_chat_id, str(user_id)))
                    data_changed = True
                else:
                    if current_status == "going":
                        cursor.execute("DELETE FROM event_users WHERE event_id = ? AND chat_id = ? AND user_id = ?", (event_id, click_chat_id, str(user_id)))
                        data_changed = True
                    else:
                        conn.close()
                        return
            conn.commit()
            conn.close()
            
            # Real-time action logging to Google Sheets Actions tab for Child channel interactions
            if data_changed:
                try:
                    sheet_target = await get_sheet_for_chat(main_chat_id)
                    gc = await agcm.authorize()
                    ss = await gc.open(sheet_target)
                    actions_ws = await ss.worksheet("Actions")
                    # Fields: EVENT_ID, ACTION, USER_NAME, USER_ID, DATE, PLACE_ID
                    await actions_ws.append_row([event_id, action.upper(), username_raw, str(user_id), now2ddmmyy(), str(click_chat_id)])
                except Exception as log_err:
                    logger.error(f"Google Sheets Actions log failed for child update: {log_err}")
                    
                context.application.create_task(update_all_shared_views(context, event_id))
            return

        if action in ["close", "kick", "modgst", "save", "incgst", "decgst", "addext"]:
            if not is_admin:
                conn.close()
                return

        if is_open == 1:
            if action == "going":
                if username_raw not in going_usernames:
                    going.append(f"{username_raw} ({user_id})")
                not_going.discard(username_raw)
                data_changed = True
            elif action == "notgoing":
                going = [u for u in going if u.split(" (")[0] != username_raw]
                not_going.add(username_raw)
                counters.pop(username_raw, None)
                data_changed = True
            elif action == "add":
                counters[username_raw] = counters.get(username_raw, 0) + 1
                data_changed = True
            elif action == "sub":
                if username_raw in counters:
                    if counters[username_raw] > 1:
                        counters[username_raw] -= 1
                    else:
                        counters.pop(username_raw)
                    data_changed = True
                else:
                    conn.close()
                    return
            elif action == "close":
                is_open = 2
                data_changed = True

        elif is_open == 2:
            if action == "addext":
                context.user_data["awaiting_extra_player_for"] = event_id
                conn.close()
                # Send prompt message to the chat so the administrator receives active feedback
                await query.message.reply_text("📝 *Verification Mode:* Please type the extra player's username:")
                return
            
            is_target_child = target_username and target_username.startswith("ch-")
            clean_target_username = target_username.replace("ch-", "", 1) if is_target_child else target_username

            if action == "kick" and target_username:
                if is_target_child:
                    cursor.execute("DELETE FROM event_users WHERE event_id = ? AND username = ?", (event_id, clean_target_username))
                else:
                    going = [u for u in going if u.split(" (")[0] != clean_target_username]
                    counters.pop(clean_target_username, None)
                data_changed = True
                
            elif action == "incgst" and target_username:
                if is_target_child:
                    cursor.execute("UPDATE event_users SET guests = guests + 1 WHERE event_id = ? AND username = ?", (event_id, clean_target_username))
                else:
                    counters[clean_target_username] = counters.get(clean_target_username, 0) + 1
                data_changed = True
                
            elif action == "decgst" and target_username:
                if is_target_child:
                    cursor.execute("SELECT guests FROM event_users WHERE event_id = ? AND username = ?", (event_id, clean_target_username))
                    cg_row = cursor.fetchone()
                    if cg_row and cg_row[0] > 0:
                        cursor.execute("UPDATE event_users SET guests = guests - 1 WHERE event_id = ? AND username = ?", (event_id, clean_target_username))
                else:
                    if clean_target_username in counters:
                        if counters[clean_target_username] > 1:
                            counters[clean_target_username] -= 1
                        else:
                            counters.pop(clean_target_username)
                data_changed = True
                
            elif action == "save":
                is_open = 0
                data_changed = True

        cursor.execute("UPDATE events SET is_open = ?, going_data = ?, notgoing_data = ?, counters_data = ? WHERE event_id = ?", 
                       (is_open, json.dumps(going), json.dumps(list(not_going)), json.dumps(counters), event_id))
        conn.commit()
        conn.close()
    except Exception as db_err:
        logger.error(f"SQLite transaction execution failure: {db_err}")
        return

    # Real-time action logging to Google Sheets Actions tab for Master group interactions
    if data_changed:
        try:
            sheet_target = await get_sheet_for_chat(main_chat_id)
            gc = await agcm.authorize()
            ss = await gc.open(sheet_target)
            actions_ws = await ss.worksheet("Actions")
            # Fields: EVENT_ID, ACTION, USER_NAME, USER_ID, DATE, PLACE_ID
            await actions_ws.append_row([event_id, action.upper(), username_raw, str(user_id), now2ddmmyy(), str(click_chat_id)])
        except Exception as log_err:
            logger.error(f"Google Sheets Actions log failed for master update: {log_err}")

        context.application.create_task(update_all_shared_views(context, event_id))

    # ==================== GOOGLE SHEETS SYNCHRONIZATION DATA PIPELINE ====================
    if action == "save":
        try:
            sheet_target = await get_sheet_for_chat(main_chat_id)
            gc = await agcm.authorize()
            ss = await gc.open(sheet_target)
            
            # 1. Fetch proper structural numbers of child entities
            conn_totals = sqlite3.connect("database.db")
            cursor_totals = conn_totals.cursor()
            cursor_totals.execute("SELECT guests FROM event_users WHERE event_id = ? AND status = 'going'", (event_id,))
            child_rows = cursor_totals.fetchall()
            conn_totals.close()
            
            total_child_count = len(child_rows) + sum([r[0] for r in child_rows])
            total_going = len(going) + sum(counters.values()) + total_child_count
            
            # 2. Update/Synchronize row inside "Events" sheet
            # Map order: EVENT_ID, EVENT_NAME, CREATED_AT, CREATED_BY, CLOSED_AT, STATUS, AMOUNT
            ws = await ss.worksheet("Events")
            records = await ws.get_all_records()
            
            found = False
            for idx, r in enumerate(records, start=2):
                if str(r.get("EVENT_ID")) == str(event_id):
                    # Update CLOSED_AT, STATUS, AMOUNT (Cols E, F, G)
                    await ws.update(f"E{idx}:G{idx}", [[now2ddmmyy(), "CLOSED", total_going]])
                    found = True
                    break
            
            if not found:
                await ws.append_row([event_id, name, now2ddmmyy(), username_raw, now2ddmmyy(), "CLOSED", total_going])

            # 4. Invoke user telemetry synchronization handlers
            going_ids = []
            for entry in going:
                match = re.search(r'\((\d+)\)', entry)
                if match:
                    going_ids.append(match.group(1))
            context.application.create_task(sync_event_users_to_google(main_chat_id, event_id, going_ids))
        except Exception as e:
            logger.error(f"Google Sheets final metrics log failed: {e}")


async def handle_extra_player_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Interceptors for raw text input when an administrator is explicitly 
    expected to type an extra player's username during verification mode.
    """
    # Check if we are currently awaiting an extra player input for an event
    event_id = context.user_data.get("awaiting_extra_player_for")
    if not event_id:
        return

    # Clear the state immediately to prevent duplicate message handling
    context.user_data.pop("awaiting_extra_player_for", None)
    
    chat_id = str(update.effective_chat.id)
    raw_text = update.message.text.strip()
    
    # Verify that the user who sent the text has administrator privileges
    try:
        chat_member = await context.bot.get_chat_member(chat_id=update.effective_chat.id, user_id=update.effective_user.id)
        if chat_member.status not in ["administrator", "creator"]:
            return
    except Exception as e:
        logger.error(f"Failed to verify admin status during extra player input: {e}")
        return

    # Parse and clean up the player's username (remove leading @ and extra spaces)
    target_username = raw_text.lstrip('@').strip()
    if not target_username:
        await update.message.reply_text("❌ Invalid username provided.")
        return

    try:
        conn = sqlite3.connect("database.db")
        cursor = conn.cursor()
        
        # Fetch the current state of the event data
        cursor.execute("SELECT going_data, counters_data FROM events WHERE event_id = ?", (event_id,))
        row = cursor.fetchone()
        
        if not row:
            conn.close()
            return
            
        going_data, counters_data = row
        going = json.loads(going_data)
        counters = json.loads(counters_data)
        
        going_usernames = {u.split(" (")[0] for u in going}
        
        # If the player is not in the Going list, add them as a virtual user
        if target_username not in going_usernames:
            going.append(f"{target_username}")
            
        # Commit updated JSON structures back to the database
        cursor.execute("UPDATE events SET going_data = ?, counters_data = ? WHERE event_id = ?", 
                       (json.dumps(going), json.dumps(counters), event_id))
        conn.commit()
        conn.close()
        
        # Delete the admin's text message to keep the chat clean (optional)
        try:
            await update.message.delete()
        except Exception:
            pass

        # Log the addition action to Google Sheets "Actions" tab
        try:
            sheet_target = await get_sheet_for_chat(chat_id)
            gc = await agcm.authorize()
            ss = await gc.open(sheet_target)
            actions_ws = await ss.worksheet("Actions")
            # Fields: EVENT_ID, ACTION, USER_NAME, USER_ID, DATE, PLACE_ID
            await actions_ws.append_row([
                event_id, 
                "ADD_EXTRA_PLAYER", 
                target_username, 
                "0",  # External/extra players do not have a physical Telegram ID in this context
                now2ddmmyy(), 
                str(chat_id)
            ])
        except Exception as log_err:
            logger.error(f"Google Sheets Actions log failed for addext: {log_err}")

        # Trigger interface updates for all shared views across active channels
        context.application.create_task(update_all_shared_views(context, event_id))
        
    except Exception as db_err:
        logger.error(f"SQLite transaction execution failure inside extra player: {db_err}")


async def global_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Routes all text messages. First checks if we are expecting an extra player, 
    if not - falls back to tracking everyone mentions.
    """
    # Check if a custom event registration sub-state flag context is present
    if context.user_data.get("awaiting_extra_player_for"):
        await handle_extra_player_input(update, context)
        return
        
    # Execute default global message monitoring logic fallback
    await track_everyone_message(update, context)