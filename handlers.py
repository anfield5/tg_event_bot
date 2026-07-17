import json
import re
import sqlite3
import asyncio
from uuid import uuid4

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from telegram.error import BadRequest

from config import DEFAULT_GOING_ICON, DEFAULT_NOTGOING_ICON, DEFAULT_CLOSE_ICON, logger
from utils import escape_markdown, now2ddmmyy, parse_event_date
from db import track_user, DB_PATH
from sheets import get_sheet_for_chat, open_spreadsheet, sync_users_sheet, sync_event_users_sheet

# One lock per event_id so that two near-simultaneous button clicks on the
# same event can't interleave their read-modify-write and silently drop one.
_event_locks = {}


def get_event_lock(event_id: str) -> asyncio.Lock:
    lock = _event_locks.get(event_id)
    if lock is None:
        lock = asyncio.Lock()
        _event_locks[event_id] = lock
    return lock


# ---------------------------------------------------------------------------
# Share-mode alias map
# ---------------------------------------------------------------------------
MODE_MAP = {
    "-v": "-visible", "--visible": "-visible", "-visible": "-visible",
    "-h": "-hidden",  "--hidden":  "-hidden",  "-hidden":  "-hidden",
    "-oc": "-onlycount", "--count": "-onlycount", "-onlycount": "-onlycount",
}

# ---------------------------------------------------------------------------
# Argument parsers
# ---------------------------------------------------------------------------

def parse_event_args(args: list):
    """
    Parses arguments for /newevent and /editevent.

    Supported flags
    ───────────────
    -gi / -goingicon <emoji>   – custom Going icon
    -ni / -notgoingicon <emoji>– custom Not-Going icon
    -d / -date <dd.mm.yyyy>     – event date (optionally followed by HH:MM)
        Examples:
            -date 14.07.2026
            -date 14.07.2026 19:00

    Returns: (event_name, going_icon, notgoing_icon, event_date_raw)
    event_date_raw is the raw token(s) joined; validation happens in the caller.
    """
    going_icon    = None
    notgoing_icon = None
    event_date    = None

    gi_flags   = {"-gi", "-goingicon"}
    ni_flags   = {"-ni", "-notgoingicon"}
    date_flags = {"-d", "-date"}

    tokens       = args[:]
    clean_tokens = []

    i = 0
    while i < len(tokens):
        token = tokens[i]

        if token in gi_flags and i + 1 < len(tokens):
            going_icon = tokens[i + 1]
            i += 2

        elif token in ni_flags and i + 1 < len(tokens):
            notgoing_icon = tokens[i + 1]
            i += 2

        elif token in date_flags and i + 1 < len(tokens):
            date_val = tokens[i + 1]
            # If next token looks like a time (HH:MM), consume it too
            if i + 2 < len(tokens) and re.match(r'^\d{2}:\d{2}$', tokens[i + 2]):
                date_val += " " + tokens[i + 2]
                i += 3
            else:
                i += 2
            event_date = date_val

        else:
            clean_tokens.append(token)
            i += 1

    event_name = " ".join(clean_tokens) if clean_tokens else None
    return event_name, going_icon, notgoing_icon, event_date


def parse_user_args(args: list) -> list:
    """
    Tokenises arguments, strips '@' prefixes, and splits on spaces or commas.
    """
    if not args:
        return []
    raw_string = " ".join(args)
    tokens = re.split(r'[\s,]+', raw_string)
    return [t.lstrip('@').strip() for t in tokens if t.strip()]


# ---------------------------------------------------------------------------
# Inline keyboard builder
# ---------------------------------------------------------------------------

def create_event_keyboard(
    event_id: str,
    is_open: int,
    going_icon: str,
    notgoing_icon: str,
    going_list: list = None,
    counters: dict = None,
    is_child: bool = False,
    child_users_rows: list = None,
) -> InlineKeyboardMarkup:
    """
    Generates dynamic inline keyboards.

    is_open == 0  → empty keyboard (event closed)
    is_open == 1  → Going / Not Going / Add & Sub guest / Close button
    is_open == 2  → Verification mode:
                    Each participant is rendered over TWO rows:
                        Row A: [👤 name]          [❌ Kick]
                        Row B: [N G.]  [➖]  [➕]
                    NOTE: Telegram's Bot API has no way to set a custom
                    color on button text - the ➕/➖ (Heavy Plus/Minus Sign)
                    glyphs are used bare here because most emoji fonts
                    (including Telegram's own) already render them in an
                    orange/rust tone by default; there's no way to force a
                    different or more saturated orange beyond that.
    """
    if is_open == 0:
        return InlineKeyboardMarkup([])

    buttons = []

    if is_open == 1:
        buttons.append([
            InlineKeyboardButton(f"{going_icon} Going",        callback_data=f"going_{event_id}"),
            InlineKeyboardButton(f"{notgoing_icon} Not Going", callback_data=f"notgoing_{event_id}"),
        ])
        buttons.append([
            InlineKeyboardButton("➕ ADD", callback_data=f"add_{event_id}"),
            InlineKeyboardButton("➖ Remove", callback_data=f"sub_{event_id}"),
        ])
        if not is_child:
            buttons.append([
                InlineKeyboardButton(f"{DEFAULT_CLOSE_ICON} Verification Mode", callback_data=f"close_{event_id}"),
            ])

    elif is_open == 2 and not is_child:
        # ── Master participants ────────────────────────────────────────────
        going_list   = going_list   or []
        counters     = counters     or {}
        child_users_rows = child_users_rows or []

        # Collect all usernames who are going OR have guests
        going_usernames = {entry.split(" (")[0] for entry in going_list}
        all_relevant_users = going_usernames.union(set(counters.keys()))

        for username in all_relevant_users:
            guest_count = counters.get(username, 0)
            is_going = username in going_usernames
            is_kicked = not is_going and guest_count > 0

            # Row A: name + kick/return button
            icon = "👤" if is_going else "👤"
            if is_kicked:
                buttons.append([
                    InlineKeyboardButton(f"{icon} {username}",  callback_data="noop"),
                    InlineKeyboardButton("↩️ Return", callback_data=f"return_{event_id}:{username}"),
                ])
            else:
                buttons.append([
                    InlineKeyboardButton(f"{icon} {username}",  callback_data="noop"),
                    InlineKeyboardButton("❌ Kick",          callback_data=f"kick_{event_id}:{username}"),
                ])
            # Row B: guest count + ➖ + ➕ (➖ before ➕; these glyphs render
            # orange by default in most emoji fonts - see docstring above)
            # Show "2G from username" format for clarity
            guest_label = f"{guest_count}G from {username}" if guest_count > 0 else "0G"
            buttons.append([
                InlineKeyboardButton(guest_label,  callback_data="noop"),
                InlineKeyboardButton("−", callback_data=f"decgst_{event_id}:{username}"),
                InlineKeyboardButton("+", callback_data=f"incgst_{event_id}:{username}"),
            ])

        # ── Child-chat participants ────────────────────────────────────────
        for ch_username, ch_guests in child_users_rows:
            # Row A
            buttons.append([
                InlineKeyboardButton(f"📢 {ch_username}", callback_data="noop"),
                InlineKeyboardButton("❌ Kick",            callback_data=f"kick_{event_id}:ch-{ch_username}"),
            ])
            # Row B
            guest_label = f"{ch_guests}G from {ch_username}" if ch_guests > 0 else "0G"
            buttons.append([
                InlineKeyboardButton(guest_label,  callback_data="noop"),
                InlineKeyboardButton("−", callback_data=f"decgst_{event_id}:ch-{ch_username}"),
                InlineKeyboardButton("+", callback_data=f"incgst_{event_id}:ch-{ch_username}"),
            ])

        buttons.append([
            InlineKeyboardButton("➕ Add Extra Player", callback_data=f"addext_{event_id}"),
        ])
        buttons.append([
            InlineKeyboardButton("💾 Save & Close Event", callback_data=f"save_{event_id}"),
        ])

    return InlineKeyboardMarkup(buttons)


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📖 *Available Commands*\n\n"
        "/newevent \\[name\\] \\[\\-date dd\\.mm\\.yyyy \\[HH:MM\\]\\] \\- Create a new event\n"
        "/editevent \\[name\\] \\[\\-date \\.\\.\\.\\] \\- Edit the active event\n"
        "/notify \\- Ping users who haven't responded\n"
        "/updateuser \\[username\\(s\\)\\] \\[\\-a\\|\\-active\\|\\-p\\|\\-passive\\] \\- Update user status \\(multiple users separated by commas\\)\n"
        "/refreshusers \\[\\-r\\|\\-root\\] \\- Sync user list with current group members \\(with \\-r/\\-root, also syncs the Users tab in Google Sheets\\)\n"
        "/listusers \\- Show all tracked users\n\n"
        "⚙️ *Alias Subsystem:*\n"
        "/setalias \\[target\\_id\\] \\[aliasname\\] \\- Bind alias to chat ID\n"
        "/removealias \\[aliasname\\] \\- Remove alias\n"
        "/listalias \\- Show all aliases\n\n"
        "📢 *Distribution Control:*\n"
        "/shareevent \\[target\\_alias/id\\] \\[\\-v \\| \\-h \\| \\-oc\\] \\- Share active event \\(defaults to \\-oc if no mode is given\\)"
    )
    await update.message.reply_text(help_text, parse_mode="MarkdownV2")


# ---------------------------------------------------------------------------
# Alias routing system
# ---------------------------------------------------------------------------

async def setalias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Binds a custom alias to a Telegram Chat ID."""
    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "❌ *Syntax error:* Usage: `/setalias [id_group/id_channel] [aliasname]`",
            parse_mode="MarkdownV2",
        )
        return

    target_chat_input = args[0].strip()
    alias_name        = args[1].strip().lower()
    user_id           = update.effective_user.id

    try:
        if target_chat_input.startswith("-") and target_chat_input[1:].isdigit():
            target_chat_id = int(target_chat_input)
        elif target_chat_input.isdigit():
            target_chat_id = int(target_chat_input)
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
        await update.message.reply_text(
            "Add @EventPlanCheckBot to target group/channel as admin\.", parse_mode="MarkdownV2"
        )
        return

    try:
        bot_member = await context.bot.get_chat_member(chat_id=target_chat_id, user_id=context.bot.id)
        if bot_member.status not in ["administrator", "creator"]:
            await update.message.reply_text(
                "Add @EventPlanCheckBot to target group/channel as admin\.", parse_mode="MarkdownV2"
            )
            return
    except Exception:
        await update.message.reply_text(
            "Add @EventPlanCheckBot to target group/channel as admin\.", parse_mode="MarkdownV2"
        )
        return

    try:
        user_member = await context.bot.get_chat_member(chat_id=target_chat_id, user_id=user_id)
        if user_member.status not in ["administrator", "creator"]:
            await update.message.reply_text(
                "Only users with admin rights in target groups/channels can make event shares to them",
                parse_mode="MarkdownV2",
            )
            return
    except Exception:
        await update.message.reply_text(
            "Only users with admin rights in target groups/channels can make event shares to them",
            parse_mode="MarkdownV2",
        )
        return

    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id FROM chat_aliases WHERE alias = ?", (alias_name,))
    if cursor.fetchone():
        await update.message.reply_text("Alias already exist", parse_mode="MarkdownV2")
        conn.close()
        return

    cursor.execute("SELECT alias FROM chat_aliases WHERE chat_id = ?", (str(target_chat_id),))
    if cursor.fetchone():
        await update.message.reply_text(
            "⚠️ This group or channel has already been added\. Please check its existing alias\.",
            parse_mode="MarkdownV2",
        )
        conn.close()
        return

    cursor.execute("INSERT INTO chat_aliases (chat_id, alias) VALUES (?, ?)", (str(target_chat_id), alias_name))
    conn.commit()
    conn.close()

    await update.message.reply_text(
        rf"✅ Alias `__{escape_markdown(alias_name)}__` mapped to node ID `{target_chat_id}`\.",
        parse_mode="MarkdownV2",
    )


async def removealias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Removes an alias from the routing table."""
    args = context.args
    if not args:
        await update.message.reply_text(
            "❌ *Syntax error:* Usage: `/removealias [aliasname]`", parse_mode="MarkdownV2"
        )
        return

    alias_name = args[0].strip().lower()
    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id FROM chat_aliases WHERE alias = ?", (alias_name,))
    if not cursor.fetchone():
        await update.message.reply_text("🔍 Alias not found\.", parse_mode="MarkdownV2")
        conn.close()
        return

    cursor.execute("DELETE FROM chat_aliases WHERE alias = ?", (alias_name,))
    conn.commit()
    conn.close()
    await update.message.reply_text(
        f"🗑️ Alias `__{escape_markdown(alias_name)}__` removed\.", parse_mode="MarkdownV2"
    )


async def listalias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shows all active routing aliases."""
    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT alias, chat_id FROM chat_aliases")
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("📋 No aliases configured\.", parse_mode="MarkdownV2")
        return

    blocks = []
    for alias, cid in rows:
        try:
            chat_obj = await context.bot.get_chat(int(cid) if cid.replace("-", "").isdigit() else cid)
            c_name = chat_obj.title or "Unknown"
            c_type = "Public Channel" if chat_obj.type == "channel" else "Group"
        except Exception:
            c_name = "Node Disconnected"
            c_type  = "Unknown"

        blocks.append(
            f"Aliasname: {escape_markdown(alias)}\n"
            f"Type: {escape_markdown(c_type)}\n"
            f"Name: {escape_markdown(c_name)}\n"
            f"ID: {escape_markdown(str(cid))}"
        )

    text = "📋 *Distribution Routes:*\n\n" + "\n\n".join(blocks)
    await update.message.reply_text(text, parse_mode="MarkdownV2")


# ---------------------------------------------------------------------------
# Event lifecycle
# ---------------------------------------------------------------------------

async def newevent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Creates a new Going/Not-Going event.

    Flags:
        -gi / -goingimage <emoji>
        -ni / -notgoingimage <emoji>
        -date / -d <dd.mm.yyyy> [HH:MM]
    """
    chat_id  = str(update.effective_chat.id)
    message  = update.message
    args     = context.args
    user_raw = (
        update.effective_user.username
        if update.effective_user.username
        else update.effective_user.first_name
    )

    if not args:
        await message.reply_text(
            "❌ *Syntax error:* Event name is required\\.",
            parse_mode="MarkdownV2",
        )
        return

    event_name_raw, g_icon, n_icon, date_raw = parse_event_args(args)
    going_icon    = g_icon if g_icon else DEFAULT_GOING_ICON
    notgoing_icon = n_icon if n_icon else DEFAULT_NOTGOING_ICON

    # Validate date if supplied
    event_date = None
    if date_raw:
        event_date = parse_event_date(date_raw)
        if event_date is None:
            await message.reply_text(
                "❌ *Invalid date format\\.* Use `dd\\.mm\\.yyyy` or `dd\\.mm\\.yyyy HH:MM`\\.",
                parse_mode="MarkdownV2",
            )
            return

    event_id = str(uuid4())[:8]

    try:
        conn   = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO events
                (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
                 is_open, going_data, notgoing_data, counters_data, event_date)
            VALUES (?, ?, ?, ?, ?, ?, 1, '[]', '[]', '{}', ?)
            """,
            (event_id, chat_id, str(message.message_id),
             event_name_raw, going_icon, notgoing_icon, event_date),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to save new event: {e}")
        await message.reply_text("❌ Database error: could not create event\\.", parse_mode="MarkdownV2")
        return

    date_line = f"📅 {escape_markdown(event_date)}\n" if event_date else ""
    text = (
        f"*{escape_markdown(event_name_raw)}*\n"
        f"{date_line}\n"
        f"{going_icon} *Going* \\(0\\):\n\n"
        f"{notgoing_icon} *Not Going* \\(0\\):\n"
    )
    keyboard = create_event_keyboard(event_id, 1, going_icon, notgoing_icon, [], {})

    try:
        sent_msg = await context.bot.send_message(
            chat_id=chat_id, text=text, reply_markup=keyboard, parse_mode="MarkdownV2"
        )
        conn   = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE events SET message_id = ? WHERE event_id = ?",
            (str(sent_msg.message_id), event_id),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Failed to send event message: {e}")

    # Log to Google Sheets Events tab
    # Columns: EVENT_ID, EVENT_NAME, CREATED_AT, CREATED_BY, EVENT_DATE, CLOSED_AT, STATUS, AMOUNT
    try:
        sheet_target = await get_sheet_for_chat(chat_id)
        ss = await open_spreadsheet(sheet_target)
        ws = await ss.worksheet("Events")
        await ws.append_row([
            event_id, event_name_raw, now2ddmmyy(), user_raw, event_date or "", "", "OPEN", 0,
        ])
    except Exception as e:
        logger.error(f"Failed to log event creation to Google Sheets: {e}")


async def editevent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Edits name, icons, or date of the current chat's active event.
    Only provided flags are updated; omitted ones keep their existing values.
    """
    chat_id = str(update.effective_chat.id)
    args    = context.args
    if not args:
        await update.message.reply_text(
            "❌ *Syntax error:* Provide at least one value to update\\.", parse_mode="MarkdownV2"
        )
        return

    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT event_id, name, going_icon, notgoing_icon, event_date
        FROM events
        WHERE chat_id = ? AND is_open > 0
        ORDER BY ROWID DESC LIMIT 1
        """,
        (chat_id,),
    )
    row = cursor.fetchone()
    if not row:
        conn.close()
        await update.message.reply_text(
            "❌ No active event found to edit\\.", parse_mode="MarkdownV2"
        )
        return

    event_id, current_name, current_gi, current_ni, current_date = row
    new_name, new_gi, new_ni, date_raw = parse_event_args(args)

    updated_name = new_name   if new_name   else current_name
    updated_gi   = new_gi     if new_gi     else current_gi
    updated_ni   = new_ni     if new_ni     else current_ni

    # Date: update only if -date was explicitly supplied
    updated_date = current_date
    if date_raw is not None:
        parsed = parse_event_date(date_raw)
        if parsed is None:
            conn.close()
            await update.message.reply_text(
                "❌ *Invalid date format\\.* Use `dd\\.mm\\.yyyy` or `dd\\.mm\\.yyyy HH:MM`\\.",
                parse_mode="MarkdownV2",
            )
            return
        updated_date = parsed

    cursor.execute(
        """
        UPDATE events
        SET name = ?, going_icon = ?, notgoing_icon = ?, event_date = ?
        WHERE event_id = ?
        """,
        (updated_name, updated_gi, updated_ni, updated_date, event_id),
    )
    conn.commit()
    conn.close()

    await update.message.reply_text(
        "⚙️ *Event updated\\. Refreshing views\\.*", parse_mode="MarkdownV2"
    )
    context.application.create_task(schedule_view_refresh(context, event_id))

    # Sync updated date to Google Sheets Events tab
    if date_raw is not None:
        try:
            sheet_target = await get_sheet_for_chat(chat_id)
            ss = await open_spreadsheet(sheet_target)
            ws = await ss.worksheet("Events")
            records = await ws.get_all_records()
            for idx, r in enumerate(records, start=2):
                if str(r.get("EVENT_ID")) == str(event_id):
                    # Column E = EVENT_DATE (5th column)
                    await ws.update(f"E{idx}", [[updated_date or ""]])
                    break
        except Exception as e:
            logger.error(f"Failed to sync event date to Sheets: {e}")


async def notify(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Pings all active users who haven't responded to the current event yet.
    """
    chat_id = str(update.effective_chat.id)
    message = update.message

    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT event_id, name, going_data, notgoing_data
        FROM events
        WHERE chat_id = ? AND is_open = 1
        ORDER BY ROWID DESC LIMIT 1
        """,
        (chat_id,),
    )
    event_row = cursor.fetchone()
    if not event_row:
        conn.close()
        await message.reply_text(
            "❌ No active event found\\.", parse_mode="MarkdownV2"
        )
        return

    event_id, event_name, going_data, notgoing_data = event_row
    going_users   = {u.split(" (")[0] for u in json.loads(going_data)}
    notgoing_users = set(json.loads(notgoing_data))
    decided_users  = going_users | notgoing_users

    cursor.execute(
        "SELECT username FROM chat_users WHERE chat_id = ? AND status = 'active'", (chat_id,)
    )
    all_active = cursor.fetchall()
    conn.close()

    if not all_active:
        await message.reply_text(
            "📊 No active users tracked in this chat\\.", parse_mode="MarkdownV2"
        )
        return

    pending = []
    for (uname,) in all_active:
        if uname and uname not in decided_users:
            pending.append(f"@{escape_markdown(uname)}")

    if not pending:
        await message.reply_text(
            "✅ All registered users have already responded\\.", parse_mode="MarkdownV2"
        )
        return

    header = f"🔔 *Notification for:* `{escape_markdown(event_name)}`\n_Please submit your status_\n\n"
    await message.reply_text(header, parse_mode="MarkdownV2")

    chunk_size = 5
    for i in range(0, len(pending), chunk_size):
        await message.reply_text(" ".join(pending[i:i + chunk_size]))


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------

async def updateuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Updates a user's status in the current chat's registry.

    Usage: /updateuser [username(s)] [-a|-active|-p|-passive]
        -a  / -active   → status becomes 'active'
        -p  / -passive  → status becomes 'passive'

    Multiple users can be specified separated by commas:
        /updateuser @anfield, 8043690847, @anreon -a
    """
    chat_id = str(update.effective_chat.id)
    args    = context.args

    if len(args) < 2:
        await update.message.reply_text(
            "❌ *Syntax error:* `/updateuser [username\\(s\\)] [-a|-active|-p|-passive]`\\.",
            parse_mode="MarkdownV2",
        )
        return

    # The last argument is the flag, everything before it is usernames
    flag = args[-1].lower().strip()
    users_raw = " ".join(args[:-1])

    # Split by comma first, then by space
    usernames = []
    for part in users_raw.split(','):
        for u in part.split():
            clean_u = u.lstrip('@').strip()
            if clean_u:
                usernames.append(clean_u)

    active_flags  = {"-a", "-active"}
    passive_flags = {"-p", "-passive"}

    if flag in active_flags:
        status = "active"
    elif flag in passive_flags:
        status = "passive"
    else:
        await update.message.reply_text(
            "❌ *Validation error:* Use `-a`/`-active` or `-p`/`-passive`\\.",
            parse_mode="MarkdownV2",
        )
        return

    updated = []
    for username in usernames:
        track_user(chat_id, username, status)
        updated.append(username)

    if len(updated) == 1:
        await update.message.reply_text(
            f"⚙️ Status for @{escape_markdown(updated[0])} updated to `{status}`\\.",
            parse_mode="MarkdownV2",
        )
    else:
        mentions = ", ".join(f"@{escape_markdown(u)}" for u in updated)
        await update.message.reply_text(
            f"⚙️ Status for {mentions} updated to `{status}`\\.",
            parse_mode="MarkdownV2",
        )


async def listusers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.effective_chat.id)
    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT username, status FROM chat_users WHERE chat_id = ?", (chat_id,))
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text(
            "📊 No users tracked for this chat\\.", parse_mode="MarkdownV2"
        )
        return

    lines = [f"• @{escape_markdown(r[0])} \\(`{escape_markdown(r[1])}`\\)" for r in rows]
    text  = "📊 *Tracked Users:*\n\n" + "\n".join(lines)
    await update.message.reply_text(text, parse_mode="MarkdownV2")


async def refreshusers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Synchronizes the tracked user list (the one /listusers shows) with actual
    chat membership:
      - Removes (deletes) tracked users who are confirmed to have left/been
        kicked from the group, don't exist as Telegram users anymore, or are
        otherwise stale/unresolvable records.
      - Adds any chat administrator who isn't tracked yet, with status
        'active' by default.

    With the -r/-root flag (/refreshusers -r or /refreshusers -root), this
    ALSO synchronizes the Google Sheets "Users" tab for this chat:
      - new members not yet in the sheet get a row with STATUS = "Member"
      - members whose USER_NAME changed get their old name archived into
        ARCHIVED USER_NAME (comma-separated) and USER_NAME updated
      - members no longer in the group get STATUS = "Left"
    Without the flag, only the local /listusers table is touched.

    On the "adding missing members" side: the Telegram Bot API has no
    endpoint that lists every regular member of a group (only admins, via
    getChatAdministrators, which is what powers the "add" step below). Rank-
    and-file members who aren't admins get picked up automatically instead -
    either the moment they join (ChatMemberHandler in main.py) or the first
    time they click a button/send a message - not retroactively by this
    command alone. Users without a stored user_id yet (i.e. who joined
    before that tracking existed and have never interacted) can't be
    membership-checked here either, since getChatMember requires a numeric
    user_id, not a @username; they're reported separately rather than
    removed, since we simply have no way to confirm they left.
    """
    chat_id   = str(update.effective_chat.id)
    root_sync = any(a.strip().lower() in ("-r", "-root", "--root") for a in context.args)

    # Only admins may run this
    try:
        member = await context.bot.get_chat_member(
            chat_id=update.effective_chat.id, user_id=update.effective_user.id
        )
        if member.status not in ["administrator", "creator"]:
            await update.message.reply_text("⛔️ Only admins can use /refreshusers\\.", parse_mode="MarkdownV2")
            return
    except Exception as e:
        logger.error(f"refreshusers: admin check failed: {e}")
        return

    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT username, user_id, status FROM chat_users WHERE chat_id = ?", (chat_id,)
    )
    rows = cursor.fetchall()
    conn.close()

    # ── 1. Remove confirmed-departed/invalid users, track who's still here ──
    removed        = []
    skipped        = 0
    still_present  = []  # (user_id, LIVE username straight from Telegram) - verified currently in the chat

    for username, user_id, status in rows:
        if not user_id:
            # User without stored ID - delete them as they can't be verified
            removed.append(username)
            continue
        try:
            m = await context.bot.get_chat_member(
                chat_id=update.effective_chat.id, user_id=int(user_id)
            )
            if m.status in ["left", "kicked"]:
                removed.append(username)
            else:
                # Use the live Telegram username (public @handle preferred),
                # not the possibly-stale one stored locally - this is what
                # lets the Users sheet sync actually detect a name change.
                live_username = getattr(m.user, "username", None) or getattr(m.user, "first_name", None) or username
                still_present.append((user_id, live_username))
        except BadRequest:
            # "User not found" / "Chat member not found" etc. - covers both
            # someone who left AND a stale record that never corresponded to
            # a real, reachable Telegram user in this chat.
            removed.append(username)
        except Exception:
            # Any other error - treat as invalid and remove
            removed.append(username)

    if removed:
        conn   = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.executemany(
            "DELETE FROM chat_users WHERE chat_id = ? AND username = ?",
            [(chat_id, u) for u in removed],
        )
        conn.commit()
        conn.close()

    # ── 2. Add missing chat administrators as 'active' ──────────────────────
    added = []
    try:
        admins = await context.bot.get_chat_administrators(update.effective_chat.id)
        conn   = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT username FROM chat_users WHERE chat_id = ?", (chat_id,))
        already_tracked = {r[0] for r in cursor.fetchall()}
        conn.close()

        for admin_member in admins:
            u = admin_member.user
            if u.is_bot:
                continue
            uname = u.username or u.first_name or f"user{u.id}"
            if uname not in already_tracked:
                track_user(chat_id, uname, "active", user_id=str(u.id))
                added.append(uname)
            still_present.append((str(u.id), uname))
    except Exception as e:
        logger.error(f"refreshusers: could not fetch chat administrators: {e}")

    # Dedupe still_present by user_id (an admin who was already tracked
    # would otherwise appear twice - once from step 1, once from step 2).
    still_present = list({str(uid): (uid, uname) for uid, uname in still_present}.values())

    lines = []
    if removed:
        mentions = ", ".join(f"@{escape_markdown(u)}" for u in removed)
        lines.append(f"🧹 Removed \\(left or invalid\\): {mentions}")
    if added:
        mentions = ", ".join(f"@{escape_markdown(u)}" for u in added)
        lines.append(f"➕ Added \\(new admins found\\): {mentions}")
    if not lines:
        lines.append("✅ Nothing to change \\- list already matches the group\\.")

    # ── 3. Optionally sync the Google Sheets "Users" tab too ────────────────
    if root_sync:
        try:
            await sync_users_sheet(chat_id, still_present)
            lines.append("📊 Users tab in Google Sheets synced\\.")
        except Exception as e:
            logger.error(f"refreshusers: Users sheet sync failed: {e}")
            lines.append("⚠️ Could not sync the Users tab in Google Sheets\\.")

    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


# ---------------------------------------------------------------------------
# Event sharing
# ---------------------------------------------------------------------------

async def shareevent(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Forwards a synced sub-view of the active event to a child group/channel.
    All error messages route back to the main hub group.
    """
    current_chat_obj = update.effective_chat
    main_hub_chat_id = current_chat_obj.id
    user_id          = update.effective_user.id
    args             = context.args

    if current_chat_obj.type not in ["group", "supergroup"]:
        await context.bot.send_message(
            chat_id=main_hub_chat_id,
            text="❌ This command can only be used in the main hub group\\.",
            parse_mode="MarkdownV2",
        )
        return

    if len(args) < 1:
        await context.bot.send_message(
            chat_id=main_hub_chat_id,
            text="❌ *Syntax error:* `/shareevent [target_alias/id] [mode_optional]`",
            parse_mode="MarkdownV2",
        )
        return

    target_input = args[0].strip()
    mode = "-onlycount"
    if len(args) > 1 and args[1].strip().lower() in MODE_MAP:
        mode = MODE_MAP[args[1].strip().lower()]

    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT event_id, name, is_open, going_icon, notgoing_icon
        FROM events
        WHERE chat_id = ? AND is_open > 0
        ORDER BY ROWID DESC LIMIT 1
        """,
        (str(main_hub_chat_id),),
    )
    event_row = cursor.fetchone()
    if not event_row:
        conn.close()
        await context.bot.send_message(
            chat_id=main_hub_chat_id,
            text="❌ No active event found for this group\\.",
            parse_mode="MarkdownV2",
        )
        return

    event_id, name, is_open, going_icon, notgoing_icon = event_row

    cursor.execute("SELECT chat_id FROM chat_aliases WHERE alias = ?", (target_input.lower(),))
    alias_row        = cursor.fetchone()
    target_chat_raw  = alias_row[0] if alias_row else target_input

    if str(target_chat_raw) == str(main_hub_chat_id):
        conn.close()
        await context.bot.send_message(
            chat_id=main_hub_chat_id,
            text="⚠️ Cannot share an event to the same group that owns it\\.",
            parse_mode="MarkdownV2",
        )
        return

    cursor.execute(
        "SELECT message_id FROM event_shares WHERE event_id = ? AND chat_id = ?",
        (event_id, str(target_chat_raw)),
    )
    if cursor.fetchone():
        conn.close()
        await context.bot.send_message(
            chat_id=main_hub_chat_id,
            text="⚠️ This group or channel has already been added\\.",
            parse_mode="MarkdownV2",
        )
        return

    conn.close()

    try:
        if str(target_chat_raw).lstrip("-").isdigit():
            target_chat_api = int(target_chat_raw)
        else:
            target_chat_api = target_chat_raw
    except ValueError:
        await context.bot.send_message(
            chat_id=main_hub_chat_id,
            text="🔍 Group or channel not found\\.",
            parse_mode="MarkdownV2",
        )
        return

    try:
        target_chat_obj = await context.bot.get_chat(target_chat_api)
        chat_type_flag  = "channel" if target_chat_obj.type == "channel" else "group"
    except BadRequest as br:
        logger.error(f"shareevent get_chat: {br}")
        await context.bot.send_message(
            chat_id=main_hub_chat_id,
            text="🔍 Chat not found or bot is not a member\\.",
            parse_mode="MarkdownV2",
        )
        return
    except Exception as e:
        logger.error(f"shareevent get_chat unexpected: {e}")
        await context.bot.send_message(
            chat_id=main_hub_chat_id,
            text="⚠️ Unexpected error reaching target chat\\.",
            parse_mode="MarkdownV2",
        )
        return

    try:
        bot_member = await context.bot.get_chat_member(chat_id=target_chat_api, user_id=context.bot.id)
        if bot_member.status in ["left", "kicked"]:
            await context.bot.send_message(
                chat_id=main_hub_chat_id,
                text="🤖 Bot is not a member of that chat\\. Add and promote it to admin first\\.",
                parse_mode="MarkdownV2",
            )
            return
        if bot_member.status not in ["administrator", "creator"]:
            await context.bot.send_message(
                chat_id=main_hub_chat_id,
                text="🤖 Bot is in the chat but not an admin\\. Please promote it\\.",
                parse_mode="MarkdownV2",
            )
            return
    except Exception as e:
        logger.error(f"shareevent bot privileges check: {e}")
        await context.bot.send_message(
            chat_id=main_hub_chat_id,
            text="🤖 Could not verify bot status in target chat\\.",
            parse_mode="MarkdownV2",
        )
        return

    try:
        user_member = await context.bot.get_chat_member(chat_id=target_chat_api, user_id=user_id)
        if user_member.status in ["left", "kicked"]:
            await context.bot.send_message(
                chat_id=main_hub_chat_id,
                text="⛔️ You are not a member of that chat\\.",
                parse_mode="MarkdownV2",
            )
            return
        if user_member.status not in ["administrator", "creator"]:
            await context.bot.send_message(
                chat_id=main_hub_chat_id,
                text="⛔️ You need admin rights in the target chat to share there\\.",
                parse_mode="MarkdownV2",
            )
            return
    except Exception as e:
        logger.error(f"shareevent user check: {e}")
        await context.bot.send_message(
            chat_id=main_hub_chat_id,
            text="⚠️ Could not verify your membership in the target chat\\.",
            parse_mode="MarkdownV2",
        )
        return

    try:
        sent = await context.bot.send_message(
            chat_id=target_chat_api,
            text=f"↪️ *SHARED: {escape_markdown(name)}*\n_Synchronising\\.\\.\\._",
            parse_mode="MarkdownV2",
        )
        conn   = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO event_shares
                (event_id, chat_id, message_id, share_mode, chat_type)
            VALUES (?, ?, ?, ?, ?)
            """,
            (event_id, str(target_chat_api), str(sent.message_id), mode, chat_type_flag),
        )
        conn.commit()
        conn.close()
        await context.bot.send_message(
            chat_id=main_hub_chat_id,
            text="🚀 Event shared successfully\\.",
            parse_mode="MarkdownV2",
        )
    except Exception as e:
        logger.error(f"shareevent send failed: {e}")
        await context.bot.send_message(
            chat_id=main_hub_chat_id,
            text="❌ Failed to send the event to the target chat\\.",
            parse_mode="MarkdownV2",
        )
        return

    context.application.create_task(schedule_view_refresh(context, event_id))


# ---------------------------------------------------------------------------
# @everyone tracking
# ---------------------------------------------------------------------------

async def track_everyone_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Responds to @everyone by mentioning all active tracked users."""
    message = update.effective_message
    if not message or not message.text:
        return
    if "@everyone" not in message.text:
        return

    chat_id = str(update.effective_chat.id)
    conn    = sqlite3.connect(DB_PATH)
    cursor  = conn.cursor()
    cursor.execute(
        "SELECT username FROM chat_users WHERE chat_id = ? AND status = 'active'", (chat_id,)
    )
    rows = cursor.fetchall()
    conn.close()

    mentions = [f"@{r[0]}" for r in rows if r[0]]
    if not mentions:
        return

    chunk_size = 5
    for i in range(0, len(mentions), chunk_size):
        await message.reply_text(" ".join(mentions[i:i + chunk_size]))


# ---------------------------------------------------------------------------
# Shared-view renderer
# ---------------------------------------------------------------------------

_refresh_state = {}


def _get_refresh_state(event_id):
    state = _refresh_state.get(event_id)
    if state is None:
        state = {"lock": asyncio.Lock(), "pending": False}
        _refresh_state[event_id] = state
    return state


async def schedule_view_refresh(context: ContextTypes.DEFAULT_TYPE, event_id: str):
    """
    Coalesces bursts of update_all_shared_views() calls for the same event.

    Every going/notgoing/add/sub/kick/... click schedules a full re-render of
    the master post PLUS every shared child chat/channel. Without coalescing,
    N rapid clicks (very common in a busy public channel with many
    subscribers) each launched their OWN independent, fully sequential
    broadcast concurrently with the others - flooding Telegram's per-chat
    edit-message rate limit. The practical symptom was exactly what got
    reported: "Going" appearing to hang/load forever, and other actions
    looking like they "don't work" (the DB write actually succeeded, but the
    on-screen update got stuck behind a pile-up of redundant duplicate
    broadcasts all competing for the same rate-limited edit calls).

    This makes sure at most ONE broadcast is in flight per event at a time;
    if new changes arrive while one is running, they collapse into a single
    extra pass at the end instead of spawning another full broadcast.
    """
    state = _get_refresh_state(event_id)
    if state["lock"].locked():
        state["pending"] = True
        return
    async with state["lock"]:
        state["pending"] = False
        await update_all_shared_views(context, event_id)
        while state["pending"]:
            state["pending"] = False
            await update_all_shared_views(context, event_id)


async def update_all_shared_views(context: ContextTypes.DEFAULT_TYPE, event_id: str):
    """
    Cascades layout changes to all downstream linked endpoints.
    Called after every state mutation.
    """
    conn   = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT chat_id, message_id, name, going_icon, notgoing_icon,
               is_open, going_data, notgoing_data, counters_data, event_date
        FROM events WHERE event_id = ?
        """,
        (event_id,),
    )
    master = cursor.fetchone()
    if not master:
        conn.close()
        return

    (main_chat_id, main_msg_id, name, going_icon, notgoing_icon,
     is_open, going_data, notgoing_data, counters_data, event_date) = master

    master_going     = json.loads(going_data)
    master_not_going = json.loads(notgoing_data)
    master_counters  = json.loads(counters_data)

    cursor.execute(
        "SELECT chat_id, message_id, share_mode FROM event_shares WHERE event_id = ?", (event_id,)
    )
    shares = cursor.fetchall()

    child_data              = {}
    total_child_going       = 0
    child_addons_for_master = []

    for s_chat_id, _, _ in shares:
        cursor.execute(
            "SELECT username, status, guests FROM event_users "
            "WHERE event_id = ? AND chat_id = ? AND guests > 0",
            (event_id, str(s_chat_id)),
        )
        users      = cursor.fetchall()
        users_list = []
        chat_sum   = 0
        for username, status, guests in users:
            if status == "going":
                users_list.append(f"• {escape_markdown(username)}")
                chat_sum += 1
            if guests > 0:
                users_list.append(f"• {guests}, from: {escape_markdown(username)}")
                chat_sum += guests

        child_data[str(s_chat_id)] = {
            "users_text": "\n".join(users_list),
            "count":      chat_sum,
        }
        total_child_going += chat_sum

        try:
            chat_obj   = await context.bot.get_chat(
                int(s_chat_id) if s_chat_id.replace("-", "").isdigit() else s_chat_id
            )
            chat_title = chat_obj.title or "Child Group"
        except Exception:
            chat_title = "Child Group"

        if chat_sum > 0:
            # FIX: channel/group name without quotes around it
            block = (
                f"\n\n{going_icon} *Going from {escape_markdown(chat_title)}*"
                f" \\({chat_sum}\\):\n" + "\n".join(users_list)
            )
            child_addons_for_master.append(block)

    conn.close()

    master_shares_block = "".join(child_addons_for_master)

   
    total_master_guests = sum(master_counters.values())
    total_master_going  = len(master_going) + total_master_guests
    current_post_total  = total_master_going
    global_total        = current_post_total + total_child_going

    going_names_list = [f"• {escape_markdown(u.split(' (')[0])}" for u in master_going]

    # Guest lines are now folded directly into the Going list instead of a
    # separate "Guests:" section - one line per contributor, "N, from: Name".
    guest_lines = []
    for entry in master_going:
        u_name = entry.split(" (")[0]
        if master_counters.get(u_name, 0) > 0:
            guest_lines.append(f"• {master_counters[u_name]}, from: {escape_markdown(u_name)}")
    # Also include guests from users who are not going (kicked users with guests)
    for k, count in master_counters.items():
        if k not in {u.split(" (")[0] for u in master_going} and count > 0:
            guest_lines.append(f"• {count}, from: {escape_markdown(k)}")

    going_list_text = "\n".join(going_names_list + guest_lines)

    not_going_list_text = (
        "\n".join(f"• {escape_markdown(u)}" for u in master_not_going)
        if master_not_going else ""
    )

    # Header: changed wording
    header    = "⚠️ *SQUAD VERIFICATION*\n_Review members before save_\n\n" if is_open == 2 else ""
    date_line = f"📅 {escape_markdown(event_date)}\n" if event_date else ""

    master_text = (
        f"{header}*{escape_markdown(name)}*\n{date_line}\n"
        f"{going_icon} *Going* \\({total_master_going}\\):\n{going_list_text}\n\n"
        f"{notgoing_icon} *Not Going* \\({len(master_not_going)}\\):\n{not_going_list_text}"
        f"{master_shares_block}\n\n"
        f"📊 *TOTAL Going:* {global_total}"
    )

    # Keyboard buttons for master (verification mode needs child rows too)
    conn2   = sqlite3.connect(DB_PATH)
    cursor2 = conn2.cursor()
    cursor2.execute(
        "SELECT username, guests FROM event_users WHERE event_id = ? AND guests > 0",
        (event_id,),
    )
    all_child_going_for_buttons = cursor2.fetchall()
    conn2.close()

    master_keyboard = create_event_keyboard(
        event_id, is_open, going_icon, notgoing_icon,
        master_going, master_counters,
        is_child=False,
        child_users_rows=all_child_going_for_buttons,
    )

    try:
        await context.bot.edit_message_text(
            chat_id=int(main_chat_id),
            message_id=int(main_msg_id),
            text=master_text,
            reply_markup=master_keyboard,
            parse_mode="MarkdownV2",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Master view sync failed: {e}")
    except Exception as e:
        logger.error(f"Master view sync failed: {e}")

    # ── Child views ───────────────────────────────────────────────────────
    # Prefetch every chat title exactly once. The old version called
    # get_chat() for the main hub's title inside every iteration, AND for
    # every OTHER share's title inside every iteration's own render pass -
    # an O(N^2) storm of API calls for N shares, all sequential. That alone
    # could make a several-child event take a very long time to refresh.
    title_cache = {}

    async def _get_title(chat_ref):
        key = str(chat_ref)
        if key not in title_cache:
            try:
                obj = await context.bot.get_chat(
                    int(chat_ref) if str(chat_ref).replace("-", "").isdigit() else chat_ref
                )
                title_cache[key] = obj.title or "Group"
            except Exception:
                title_cache[key] = "Group"
        return title_cache[key]

    main_title          = await _get_title(main_chat_id)
    escaped_main_title  = escape_markdown(main_title)
    for s_chat_id, _, _ in shares:
        await _get_title(s_chat_id)

    async def _render_and_edit_child(s_chat_id, s_msg_id, mode):
        c_info = child_data.get(str(s_chat_id), {"users_text": "", "count": 0})

        if mode == "-visible":
            child_text = (
                f"↪️ *SHARED: {escape_markdown(name)}*\n\n"
                f"{going_icon} *Going from {escaped_main_title}* \\({current_post_total}\\):\n{going_list_text}\n\n"
            )
            for other_id, _, _ in shares:
                if str(other_id) != str(s_chat_id):
                    o_title = title_cache.get(str(other_id), "Group")
                    o_info  = child_data.get(str(other_id), {"users_text": "", "count": 0})
                    if o_info["count"] > 0:
                        child_text += (
                            f"{going_icon} *Going from {escape_markdown(o_title)}*"
                            f" \\({o_info['count']}\\):\n{o_info['users_text']}\n\n"
                        )

        elif mode == "-onlycount":
            child_text = (
                f"↪️ *SHARED: {escape_markdown(name)}*\n\n"
                f"{going_icon} *Going from {escaped_main_title}:* {current_post_total}\n\n"
            )
            for other_id, _, _ in shares:
                if str(other_id) != str(s_chat_id):
                    o_title = title_cache.get(str(other_id), "Group")
                    o_info  = child_data.get(str(other_id), {"count": 0})
                    child_text += f"{going_icon} *Going from {escape_markdown(o_title)}:* {o_info['count']}\n"
            child_text += "\n"

        else:  # "-hidden"
            child_text = f"↪️ *SHARED: {escape_markdown(name)}*\n\n_Data hidden by admin\\._\n\n"

        child_text += (
            f"{going_icon} *Going here:* \\({c_info['count']}\\)\n{c_info['users_text']}\n\n"
            f"📊 *Total Going \\(all groups\\):* {global_total}\n"
        )

        child_keyboard = create_event_keyboard(
            event_id, is_open, going_icon, notgoing_icon, is_child=True
        )
        try:
            await context.bot.edit_message_text(
                chat_id=int(s_chat_id) if str(s_chat_id).replace("-", "").isdigit() else s_chat_id,
                message_id=int(s_msg_id),
                text=child_text,
                reply_markup=child_keyboard,
                parse_mode="MarkdownV2",
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                logger.error(f"Child view update failed for {s_chat_id}: {e}")
        except Exception as e:
            logger.error(f"Child view update failed for {s_chat_id}: {e}")

    if shares:
        # Fire every child chat's update concurrently rather than one at a
        # time - previously a slow/rate-limited edit on ONE chat delayed the
        # update of every other chat queued behind it in the loop.
        # return_exceptions=True keeps one failing/rate-limited chat from
        # aborting the others (each already has its own try/except above,
        # this is just an extra safety net around the gather itself).
        await asyncio.gather(
            *[_render_and_edit_child(s_chat_id, s_msg_id, mode) for s_chat_id, s_msg_id, mode in shares],
            return_exceptions=True,
        )


# ---------------------------------------------------------------------------
# Button handler (main state machine)
# ---------------------------------------------------------------------------

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles all inline keyboard interactions.
    """
    query         = update.callback_query
    callback_data = query.data
    click_chat_id = str(query.message.chat_id)
    user          = query.from_user
    user_id       = user.id

    # FIX: no '(id1234)' suffix when user has no @username
    username_raw = user.username if user.username else (user.first_name or f"user{user_id}")

    try:
        await query.answer()
    except Exception as e:
        logger.error(f"Failed to answer callback: {e}")

    if callback_data == "noop":
        return

    action = target_username = event_id = None

    try:
        if ":" in callback_data:
            action_prefix, target_username = callback_data.split(":", 1)
            action, event_id = (
                action_prefix.split("_", 1) if "_" in action_prefix else (None, None)
            )
        else:
            action, event_id = (
                callback_data.split("_", 1) if "_" in callback_data else (None, None)
            )
    except Exception as e:
        logger.error(f"Callback parse error: {e}")
        return

    if not action or not event_id:
        return

    try:
        chat_member = await context.bot.get_chat_member(
            chat_id=query.message.chat_id, user_id=user.id
        )
        is_admin = chat_member.status in ["administrator", "creator"]
    except Exception:
        is_admin = False

    data_changed = False
    is_open      = 1

    lock = get_event_lock(event_id)
    async with lock:
        try:
            conn   = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT chat_id, message_id, name, going_icon, notgoing_icon,
                       is_open, going_data, notgoing_data, counters_data, event_date
                FROM events WHERE event_id = ?
                """,
                (event_id,),
            )
            row = cursor.fetchone()
            if not row:
                conn.close()
                return

            (main_chat_id, main_msg_id, name, going_icon, notgoing_icon,
             is_open, going_data, notgoing_data, counters_data, event_date) = row

            going     = json.loads(going_data)
            not_going = set(json.loads(notgoing_data))
            counters  = json.loads(counters_data)

            if is_open == 0:
                conn.close()
                return

            is_click_in_child  = (int(click_chat_id) != int(main_chat_id))
            going_usernames    = {u.split(" (")[0] for u in going}

            # Extract the real user_id from each "name (user_id)" master
            # going-list entry, so cross-chat protection compares actual
            # Telegram users rather than display-name strings. Comparing by
            # name text was a bug: any two different people who happen to
            # render with the same name (extremely common in a busy public
            # channel full of subscribers without an @username, who all show
            # up by first_name only) would falsely collide, silently
            # blocking the second person's Going/Add/Sub click in every
            # child chat.
            master_going_user_ids = set()
            for entry in going:
                m = re.search(r'\((\d+)\)', entry)
                if m:
                    master_going_user_ids.add(m.group(1))

            # ── Cross-chat protection ─────────────────────────────────────
            if action in ["going", "add", "sub"]:
                user_already_registered = False

                if str(user_id) in master_going_user_ids and is_click_in_child:
                    user_already_registered = True

                cursor.execute(
                    "SELECT chat_id FROM event_users WHERE event_id = ? AND user_id = ? AND status = 'going'",
                    (event_id, str(user_id)),
                )
                for (recorded_chat_id,) in cursor.fetchall():
                    if str(recorded_chat_id) != str(click_chat_id):
                        user_already_registered = True
                        break
                    if not is_click_in_child:
                        user_already_registered = True
                        break

                if user_already_registered:
                    conn.close()
                    try:
                        await query.answer(
                            text="⚠️ You are already added to this event in another group/channel",
                            show_alert=True,
                        )
                    except Exception:
                        pass
                    return

            # ── Child-chat interaction ────────────────────────────────────
            if is_click_in_child:
                if action not in ["going", "notgoing", "add", "sub"]:
                    conn.close()
                    return

                cursor.execute(
                    "SELECT status, guests FROM event_users WHERE event_id = ? AND chat_id = ? AND user_id = ?",
                    (event_id, click_chat_id, str(user_id)),
                )
                u_row          = cursor.fetchone()
                current_status = u_row[0] if u_row else "none"
                current_guests = u_row[1] if u_row else 0

                if action == "going":
                    if current_status == "going":
                        if current_guests > 0:
                            cursor.execute(
                                "UPDATE event_users SET status = '' WHERE event_id = ? AND chat_id = ? AND user_id = ?",
                                (event_id, click_chat_id, str(user_id)),
                            )
                        else:
                            cursor.execute(
                                "DELETE FROM event_users WHERE event_id = ? AND chat_id = ? AND user_id = ?",
                                (event_id, click_chat_id, str(user_id)),
                            )
                    else:
                        cursor.execute(
                            "INSERT OR REPLACE INTO event_users (event_id, chat_id, user_id, username, status, guests) VALUES (?, ?, ?, ?, 'going', ?)",
                            (event_id, click_chat_id, str(user_id), username_raw, current_guests),
                        )
                    data_changed = True
                elif action == "notgoing":
                    if current_guests > 0:
                        cursor.execute(
                            "INSERT OR REPLACE INTO event_users (event_id, chat_id, user_id, username, status, guests) VALUES (?, ?, ?, ?, 'notgoing', ?)",
                            (event_id, click_chat_id, str(user_id), username_raw, current_guests),
                        )
                    else:
                        cursor.execute(
                            "DELETE FROM event_users WHERE event_id = ? AND chat_id = ? AND user_id = ?",
                            (event_id, click_chat_id, str(user_id)),
                        )
                    data_changed = True
                elif action == "add":
                    # NOTE: does NOT force status='going' - mirrors the main
                    # hub, where Add Guest only ever touches the guest
                    # counter and is completely independent of whether the
                    # clicker themselves is going/not going/undeclared.
                    preserved_status = current_status if current_status != "none" else ""
                    cursor.execute(
                        "INSERT OR REPLACE INTO event_users (event_id, chat_id, user_id, username, status, guests) VALUES (?, ?, ?, ?, ?, ?)",
                        (event_id, click_chat_id, str(user_id), username_raw, preserved_status, current_guests + 1),
                    )
                    data_changed = True
                elif action == "sub":
                    # NOTE: mirrors the main hub - Sub Guest only ever
                    # touches the guest counter, and never removes a Going/
                    # Not Going declaration as a side effect.
                    if current_guests > 0:
                        new_guests = current_guests - 1
                        if new_guests == 0 and current_status not in ("going", "notgoing"):
                            cursor.execute(
                                "DELETE FROM event_users WHERE event_id = ? AND chat_id = ? AND user_id = ?",
                                (event_id, click_chat_id, str(user_id)),
                            )
                        else:
                            cursor.execute(
                                "UPDATE event_users SET guests = ? WHERE event_id = ? AND chat_id = ? AND user_id = ?",
                                (new_guests, event_id, click_chat_id, str(user_id)),
                            )
                        data_changed = True
                    else:
                        conn.close()
                        return

                conn.commit()
                conn.close()

                if data_changed:
                    try:
                        sheet_target = await get_sheet_for_chat(main_chat_id)
                        ss           = await open_spreadsheet(sheet_target)
                        ws           = await ss.worksheet("Actions")
                        await ws.append_row([
                            event_id, action.upper(), username_raw,
                            str(user_id), now2ddmmyy(), str(click_chat_id),
                        ])
                    except Exception as e:
                        logger.error(f"Sheets child action log failed: {e}")
                    context.application.create_task(schedule_view_refresh(context, event_id))
                return

            # ── Admin-only actions guard ──────────────────────────────────
            if action in ["close", "kick", "save", "incgst", "decgst", "addext"]:
                if not is_admin:
                    conn.close()
                    return

            # ── Master open (is_open == 1) ────────────────────────────────
            if is_open == 1:
                if action == "going":
                    if username_raw not in going_usernames:
                        going.append(f"{username_raw} ({user_id})")
                    not_going.discard(username_raw)
                    # Store user_id for refreshusers
                    track_user(click_chat_id, username_raw, "active", user_id=str(user_id))
                    data_changed = True
                elif action == "notgoing":
                    going    = [u for u in going if u.split(" (")[0] != username_raw]
                    not_going.add(username_raw)
                    # NOTE: guests are intentionally left untouched here -
                    # they're only ever added/removed via Add Guest/Sub
                    # Guest, never as a side effect of opting out.
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
                    is_open      = 2
                    data_changed = True

            # ── Master verification (is_open == 2) ───────────────────────
            elif is_open == 2:
                if action == "addext":
                    context.user_data["awaiting_extra_player_for"] = event_id
                    conn.close()
                    await query.message.reply_text(
                        "📝 *Verification Mode:* Type the extra player's username:",
                        parse_mode="MarkdownV2",
                    )
                    return

                is_target_child  = target_username and target_username.startswith("ch-")
                clean_target_usr = target_username.replace("ch-", "", 1) if is_target_child else target_username

                if action == "kick" and target_username:
                    if is_target_child:
                        # Only clear status, keep guests intact
                        cursor.execute(
                            "UPDATE event_users SET status = '' WHERE event_id = ? AND username = ?",
                            (event_id, clean_target_usr),
                        )
                    else:
                        going = [u for u in going if u.split(" (")[0] != clean_target_usr]
                        # Don't pop counters - guests should remain even after user is kicked
                    data_changed = True

                elif action == "return" and target_username:
                    if is_target_child:
                        # Set status back to 'going'
                        cursor.execute(
                            "UPDATE event_users SET status = 'going' WHERE event_id = ? AND username = ?",
                            (event_id, clean_target_usr),
                        )
                    else:
                        # Add user back to going list
                        # Try to find if they have a stored user_id
                        conn_check = sqlite3.connect(DB_PATH)
                        cursor_check = conn_check.cursor()
                        cursor_check.execute(
                            "SELECT user_id FROM chat_users WHERE username = ? AND chat_id = ?",
                            (clean_target_usr, chat_id),
                        )
                        user_id_row = cursor_check.fetchone()
                        conn_check.close()

                        if user_id_row and user_id_row[0]:
                            going.append(f"{clean_target_usr} ({user_id_row[0]})")
                        else:
                            going.append(clean_target_usr)
                    data_changed = True

                elif action == "incgst" and target_username:
                    if is_target_child:
                        cursor.execute(
                            "UPDATE event_users SET guests = guests + 1 WHERE event_id = ? AND username = ?",
                            (event_id, clean_target_usr),
                        )
                    else:
                        counters[clean_target_usr] = counters.get(clean_target_usr, 0) + 1
                    data_changed = True

                elif action == "decgst" and target_username:
                    if is_target_child:
                        cursor.execute(
                            "SELECT guests FROM event_users WHERE event_id = ? AND username = ?",
                            (event_id, clean_target_usr),
                        )
                        cg_row = cursor.fetchone()
                        if cg_row and cg_row[0] > 0:
                            cursor.execute(
                                "UPDATE event_users SET guests = guests - 1 WHERE event_id = ? AND username = ?",
                                (event_id, clean_target_usr),
                            )
                    else:
                        if clean_target_usr in counters:
                            if counters[clean_target_usr] > 1:
                                counters[clean_target_usr] -= 1
                            else:
                                counters.pop(clean_target_usr)
                    data_changed = True

                elif action == "save":
                    is_open      = 0
                    data_changed = True

            cursor.execute(
                "UPDATE events SET is_open = ?, going_data = ?, notgoing_data = ?, counters_data = ? WHERE event_id = ?",
                (is_open, json.dumps(going), json.dumps(list(not_going)), json.dumps(counters), event_id),
            )
            conn.commit()
            conn.close()

        except Exception as db_err:
            logger.error(f"SQLite transaction failure: {db_err}")
            return

        # Log action to Sheets
        if data_changed:
            try:
                sheet_target = await get_sheet_for_chat(main_chat_id)
                ss           = await open_spreadsheet(sheet_target)
                ws           = await ss.worksheet("Actions")
                if action == "incgst":
                    logged_action = "ADD_editmode"
                elif action == "decgst":
                    logged_action = "SUB_editmode"
                else:
                    logged_action = action.upper()
                await ws.append_row([
                    event_id, logged_action, username_raw,
                    str(user_id), now2ddmmyy(), str(click_chat_id),
                ])
            except Exception as e:
                logger.error(f"Sheets master action log failed: {e}")

            context.application.create_task(schedule_view_refresh(context, event_id))

        # ── Save & Close Event: write ALL going users to EventUsers sheet ─
        if action == "save":
            try:
                sheet_target = await get_sheet_for_chat(main_chat_id)
                ss           = await open_spreadsheet(sheet_target)

                # 1. Collect master going user_ids (stored as "username (user_id)").
                #    Entries added via "Add Extra Player" have no real
                #    Telegram user_id (no parenthesised id suffix) - use
                #    their username directly.
                master_going_ids = []
                for entry in going:
                    m = re.search(r'\((\d+)\)', entry)
                    if m:
                        master_going_ids.append(m.group(1))
                    else:
                        # Extra player - use username as user_id
                        username = entry.split(" (")[0]
                        master_going_ids.append(username)

                # 2. Collect child going user_ids from event_users table
                conn_eu   = sqlite3.connect(DB_PATH)
                cursor_eu = conn_eu.cursor()
                cursor_eu.execute(
                    "SELECT user_id FROM event_users WHERE event_id = ? AND status = 'going'",
                    (event_id,),
                )
                child_going_ids = [r[0] for r in cursor_eu.fetchall()]
                conn_eu.close()

                all_going_ids = master_going_ids + child_going_ids

                # 3. Compute total for Events sheet
                # Include all child users (going + those with guests) and their guests
                conn_t   = sqlite3.connect(DB_PATH)
                cursor_t = conn_t.cursor()
                cursor_t.execute(
                    "SELECT status, guests FROM event_users WHERE event_id = ? AND (status = 'going' OR guests > 0)",
                    (event_id,),
                )
                child_rows     = cursor_t.fetchall()
                conn_t.close()
                # Count child users who are going, plus all their guests (including from non-going users)
                child_going_count = sum(1 for status, guests in child_rows if status == 'going')
                child_guests_total = sum(guests for status, guests in child_rows)
                # Master: going users + all guests (including from non-going users)
                total_going    = len(going) + sum(counters.values()) + child_going_count + child_guests_total

                # 4. Update Events sheet row
                ws      = await ss.worksheet("Events")
                records = await ws.get_all_records()
                found   = False
                for idx, r in enumerate(records, start=2):
                    if str(r.get("EVENT_ID")) == str(event_id):
                        await ws.update(f"F{idx}:H{idx}", [[now2ddmmyy(), "CLOSED", total_going]])
                        found = True
                        break
                if not found:
                    await ws.append_row([
                        event_id, name, now2ddmmyy(), username_raw,
                        event_date or "", now2ddmmyy(), "CLOSED", total_going,
                    ])

                # 5. Write all going user_ids to EventUsers sheet
                context.application.create_task(
                    sync_event_users_sheet(main_chat_id, event_id, all_going_ids)
                )

            except Exception as e:
                logger.error(f"Sheets save pipeline failed: {e}")


# ---------------------------------------------------------------------------
# Extra player input handler
# ---------------------------------------------------------------------------

async def handle_extra_player_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles raw text when an admin is expected to type an extra player's username
    during verification mode (is_open == 2).
    """
    event_id = context.user_data.get("awaiting_extra_player_for")
    if not event_id:
        return

    context.user_data.pop("awaiting_extra_player_for", None)
    chat_id  = str(update.effective_chat.id)
    raw_text = update.message.text.strip()

    try:
        member = await context.bot.get_chat_member(
            chat_id=update.effective_chat.id, user_id=update.effective_user.id
        )
        if member.status not in ["administrator", "creator"]:
            return
    except Exception as e:
        logger.error(f"Extra player admin check failed: {e}")
        return

    target_username = raw_text.lstrip('@').strip()
    if not target_username:
        await update.message.reply_text("❌ Invalid username.")
        return

    try:
        conn   = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT going_data, counters_data FROM events WHERE event_id = ?", (event_id,)
        )
        row = cursor.fetchone()
        if not row:
            conn.close()
            return

        going, counters = json.loads(row[0]), json.loads(row[1])
        if target_username not in {u.split(" (")[0] for u in going}:
            # Try to get real user_id from Telegram for the username
            try:
                member = await context.bot.get_chat_member(
                    chat_id=update.effective_chat.id, username=target_username
                )
                user_id = member.user.id
                going.append(f"{target_username} ({user_id})")
            except Exception:
                # If can't resolve, add without user_id
                going.append(target_username)

        cursor.execute(
            "UPDATE events SET going_data = ?, counters_data = ? WHERE event_id = ?",
            (json.dumps(going), json.dumps(counters), event_id),
        )
        conn.commit()
        conn.close()

        try:
            await update.message.delete()
        except Exception:
            pass

        try:
            sheet_target = await get_sheet_for_chat(chat_id)
            ss           = await open_spreadsheet(sheet_target)
            ws           = await ss.worksheet("Actions")
            # Record the user who clicked the button, not the added player
            user_raw = update.effective_user.username if update.effective_user.username else update.effective_user.first_name
            await ws.append_row([
                event_id, "ADD_EXTRA_PLAYER", user_raw, str(update.effective_user.id), now2ddmmyy(), str(chat_id),
            ])
        except Exception as e:
            logger.error(f"Sheets extra player log failed: {e}")

        context.application.create_task(schedule_view_refresh(context, event_id))

    except Exception as e:
        logger.error(f"Extra player DB failure: {e}")


# ---------------------------------------------------------------------------
# Global text router
# ---------------------------------------------------------------------------

async def global_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Routes all text messages: extra-player input first, then @everyone tracking."""
    if context.user_data.get("awaiting_extra_player_for"):
        await handle_extra_player_input(update, context)
        return
    await track_everyone_message(update, context)
