import json
import re
import sqlite3
import asyncio
from uuid import uuid4

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes
from telegram.error import BadRequest

from keyboard import create_event_keyboard
from subscription import (
    is_premium, require_premium, setsub,
    FEATURE_MATRIX, SUBS_DATE_FORMAT, _push_control_sheet_main,
)
from aliases import setalias, removealias, listalias
from monitors import addmonitor, removemonitor, listmonitors

from config import (
    DEFAULT_GOING_ICON, DEFAULT_NOTGOING_ICON, ICON_VERIFICATION, ICON_GUEST, logger,
    ICON_KICK, ICON_RETURN, ICON_ADD,
    ICON_CANCEL_EVENT, ICON_SAVE, ICON_SHARED, ICON_STATS, ICON_WARNING,
    ICON_ERROR, ICON_CLOCK, ICON_NOTIFY, ICON_CLEAN, ICON_ADMIN_ONLY, ICON_GLOBE, ICON_PREMIUM,
    FREE_SHAREEVENT_LIMIT_PER_TARGET, OWNER_USER_IDS,
)
from utils import escape_markdown, now2ddmmyy, parse_event_date, is_real_admin, GROUP_ANONYMOUS_BOT_ID
from db import track_user, DB_PATH, get_connection, get_display_name
from hub_resolver import resolve_hub_chat_id, register_hub_command, _get_known_candidate_chats
from sheets import (
    get_sheet_for_chat, open_spreadsheet, sync_users_sheet, sync_event_users_sheet,
    log_user_presence,
)

# One lock per event_id so that two near-simultaneous button clicks on the
# same event can't interleave their read-modify-write and silently drop one.
_event_locks = {}


def get_event_lock(event_id: str) -> asyncio.Lock:
    lock = _event_locks.get(event_id)
    if lock is None:
        lock = asyncio.Lock()
        _event_locks[event_id] = lock
    return lock


# events.event_status: -1 canceled / 0 open / 1 verification / 2 closed


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
# Inline keyboard builder (moved to keyboard.py)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------

async def _help_target_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Which chat's premium status should gate the Aliases/Monitoring help
    buttons. In a group, that's the group itself. In a DM:
      - Use whichever group is currently "stuck" for this conversation
        (see hub_resolver.py) if one has already been selected.
      - Otherwise, look up admin groups the same way other DM commands do.
        If there's exactly one, use it AND remember it as the selection
        (so /help behaves consistently whether or not it's the first
        command run in the conversation). With zero or multiple matches,
        fall back to the DM's own id (never premium) rather than forcing
        a picker here - a help menu shouldn't demand a group choice.
    """
    chat = update.effective_chat
    if chat.type != "private":
        return chat.id

    selected = context.user_data.get("selected_hub_chat_id")
    if selected is not None:
        return selected

    admin_of = await _get_known_candidate_chats(context, update.effective_user.id)
    if len(admin_of) == 1:
        context.user_data["selected_hub_chat_id"] = admin_of[0][0]
        return admin_of[0][0]

    return chat.id


def _build_main_help_keyboard(chat_id) -> InlineKeyboardMarkup:
    """
    Aliases/Monitoring buttons are shown either as normal (premium hub) or
    marked with ICON_PREMIUM (free hub). Tapping the free version opens an
    upgrade-info message (see upgrade_info_callback_handler) instead of
    doing nothing, so there's an actual next step rather than a dead end.
    """
    premium = is_premium(chat_id)
    alias_btn = InlineKeyboardButton(
        "⚙️ Aliases" if premium else f"⚙️ Aliases {ICON_PREMIUM}",
        callback_data="help_alias" if premium else "upgrade_info",
    )
    monitor_btn = InlineKeyboardButton(
        "🔍 Monitoring" if premium else f"🔍 Monitoring {ICON_PREMIUM}",
        callback_data="help_monitoring" if premium else "upgrade_info",
    )
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗳 Event Lifecycle", callback_data="help_lifecycle"),
         InlineKeyboardButton("📢 Distribution", callback_data="help_distribution")],
        [alias_btn, monitor_btn],
    ])


async def userid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Replies with the caller's own numeric Telegram user_id - the exact
    value to put in OWNER_USER_IDS (.env) to grant owner-only command
    access. A user_id isn't sensitive/secret, so this is safe for anyone
    to run.
    """
    await update.message.reply_text(
        f'Your user id: "{update.effective_user.id}"',
    )


async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Replies with the CURRENT chat's numeric ID - the exact value to pass to
    /setsub, /addmonitor, etc. A chat_id isn't sensitive/secret, so this is
    safe for anyone in the group/channel to run.
    """
    await update.message.reply_text(
        f"This chat's ID: {update.effective_chat.id}",
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_owner_request = bool(context.args) and context.args[0].strip().lower() in ("-a", "--admin", "--owner")
    if is_owner_request and update.effective_user.id in OWNER_USER_IDS:
        owner_help = (
            "🔑 *Owner\\-Only Commands*\n\n"
            "/setsub \\[chat\\_id\\] on \\[days\\] \\- Activate/extend PRO for a group\n"
            "/setsub \\[chat\\_id\\] off \\- Deactivate PRO for a group immediately\n"
            "/allgroups \\[\\-pro\\] \\- List every group the bot is in, 10 at a time\n"
            "/allchannels \\- List every channel the bot is in, 10 at a time\n\n"
            "These are gated on your personal Telegram user\\_id \\(OWNER\\_USER\\_IDS\\), "
            "not on chat admin status \\- posting anonymously \\(as the group/channel itself\\) "
            "can't be verified and will be rejected\\."
        )
        await update.message.reply_text(owner_help, parse_mode="MarkdownV2")
        return

    main_help = (
        "📖 *Main Commands*\n\n"
        "/newevent \\[name\\] \\[\\-date dd\\.mm\\.yyyy \\[HH:MM\\]\\]\\[\\-gi \\<emoji\\>\\]\\[\\-ni \\<emoji\\>\\] \\- Create a new event\n"
        "\\-gi \\| \\-goingicon \\<emoji\\> \\- Custom Going icon\n"
        "\\-ni \\| \\-notgoingicon \\<emoji\\> \\- Custom Not Going icon\n"
        "/editevent \\[name\\] \\[\\-date \\.\\.\\.\\] \\- Edit the active event\n"
        "/notify \\- Ping users who haven't responded\n"
        "/refreshusers \\- Sync user list, Google Sheets, and remove unverifiable users for THIS group\n"
        "/refreshusersall \\- Same as /refreshusers, but for every monitored group/channel\n"
        "/listusers \\- Show all tracked users\n"
        "/adduser \\[user\\_id\\|username\\] \\[\\.\\.\\.\\] \\- Manually add users to tracked list\n"
        "/status \\- Show this group's subscription type\\, due date\\, and bound sheet\n"
        "/switchgroup \\- \\(DM only\\) switch which group your commands target\n\n"
        "📚 *More Info*"
    )

    keyboard = _build_main_help_keyboard(await _help_target_chat_id(update, context))
    await update.message.reply_text(main_help, parse_mode="MarkdownV2", reply_markup=keyboard)


async def help_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    # Defensive: these two sections are premium-only. The free-tier keyboard
    # never sends these callback_data values in the first place (it sends
    # "upgrade_info" instead), but re-check here too in case the tier changed
    # between the button being shown and being tapped.
    if query.data in ("help_alias", "help_monitoring") and not is_premium(await _help_target_chat_id(update, context)):
        await query.answer("This section is PRO-only.", show_alert=True)
        return

    await query.answer()

    help_sections = {
        "help_alias": (
            "⚙️ *Alias Subsystem*\n\n"
            "/setalias \\[target\\_id\\] \\[aliasname\\] \\- Bind alias to chat ID\n"
            "/removealias \\[aliasname\\] \\- Remove alias\n"
            "/listaliases \\- Show all aliases\n\n"
            "Aliases let you use memorable names instead of numeric chat IDs when sharing events"
        ),
        "help_distribution": (
            "📢 *Distribution Control*\n\n"
            "/shareevent \\[target\\_alias/id\\] \\[\\-v \\| \\-h \\| \\-oc\\] \\- Share active event\n\n"
            "Modes:\n"
            "  • \\-v \\(visible\\): Show full event in child chat\n"
            "  • \\-h \\(hidden\\): Hide event, only show going/notgoing counts\n"
            "  • \\-oc \\(onlycount\\): Show only total going count\n\n"
            "Defaults to \\-oc if no mode is given"
        ),
        "help_monitoring": (
            "🔍 *Monitoring System*\n\n"
            "/addmonitor \\[chat\\_id\\] \\- Add group/channel to monitor list\n"
            "/removemonitor \\[chat\\_id\\] \\- Remove from monitor list\n"
            "/listmonitors \\- Show all monitored groups/channels\n\n"
            "Monitored chats are tracked for user presence and can be synced with /refreshusers \\-g"
        ),
        "help_lifecycle": (
            f"🗳 *Event Lifecycle Buttons*\n\n"
            f"  • Going / Not Going / ADD / Remove \\- open voting, available to everyone\n"
            f"  • {ICON_VERIFICATION} Verify&Close \\- admin only, locks voting and opens roster review\n"
            f"  • {ICON_CANCEL_EVENT} Cancel Event \\- admin only, cancels immediately \\(CANCELED in Events sheet\\)\n"
            f"  • {ICON_KICK} Kick / {ICON_RETURN} Return \\- admin only, toggle person in/out of going list\n"
            f"  • \\- / \\+ \\- admin only, adjust guest count\n"
            f"  • \\{ICON_ADD} Add Extra Member \\- admin only, add by username\n"
            f"  • {ICON_SAVE} Save & Close Event \\- admin only, finalize and export to EventUsers"
        ),
    }
    
    section_text = help_sections.get(query.data, "Unknown section")
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back", callback_data="help_back")],
    ])
    
    await query.edit_message_text(section_text, parse_mode="MarkdownV2", reply_markup=keyboard)


async def help_back_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    main_help = (
        "📖 *Main Commands*\n\n"
        "/newevent \\[name\\] \\[\\-date dd\\.mm\\.yyyy \\[HH:MM\\]\\] \\- Create a new event\n"
        "/editevent \\[name\\] \\[\\-date \\.\\.\\.\\] \\- Edit the active event\n"
        "/notify \\- Ping users who haven't responded\n"
        "/refreshusers \\- Sync user list, Google Sheets, and remove unverifiable users for THIS group\n"
        "/refreshusersall \\- Same as /refreshusers, but for every monitored group/channel\n"
        "/listusers \\- Show all tracked users\n"
        "/adduser \\[user\\_id\\|username\\] \\[\\.\\.\\.\\] \\- Manually add users to tracked list\n"
        "/status \\- Show this group's subscription type\\, due date\\, and bound sheet\n"
        "/switchgroup \\- \\(DM only\\) switch which group your commands target\n\n"
        "📚 *More Info*"
    )

    keyboard = _build_main_help_keyboard(await _help_target_chat_id(update, context))
    await query.edit_message_text(main_help, parse_mode="MarkdownV2", reply_markup=keyboard)


async def upgrade_info_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Shown when a free-tier hub taps the locked Aliases/Monitoring button on
    /help - replaces the old dead-end alert ("This section is PRO-only.")
    with an actual next step: what PRO unlocks, the hub's current tier, a
    button to message the bot owner directly, and a way back to /help
    instead of a conversational dead end.
    """
    query = update.callback_query
    await query.answer()

    chat_id = await _help_target_chat_id(update, context)
    current_tier = "PRO" if is_premium(chat_id) else "FREE"

    text = (
        f"⚡ *Aliases and Monitoring are PRO features*\n\n"
        f"With PRO, this hub gets:\n"
        f"• Custom aliases for child groups/channels\n"
        f"• Group/channel monitoring \\(/addmonitor\\)\n"
        f"• Your own Google Sheet, auto\\-synced with every event\n\n"
        f"Currently: {current_tier}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Message the bot owner", url="https://t.me/anefex")],
        [InlineKeyboardButton("◀️ Back to /help", callback_data="help_back")],
    ])
    await query.edit_message_text(text, parse_mode="MarkdownV2", reply_markup=keyboard)


# ---------------------------------------------------------------------------
# Alias routing system
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Event lifecycle
# ---------------------------------------------------------------------------

@register_hub_command("newevent")
async def newevent(update: Update, context: ContextTypes.DEFAULT_TYPE, override_chat_id: str = None):
    """
    Creates a new Going/Not-Going event.

    Flags:
        -gi / -goingicon <emoji>
        -ni / -notgoingicon <emoji>
        -date / -d <dd.mm.yyyy> [HH:MM]
    """
    chat_id = await resolve_hub_chat_id(update, context, "newevent", override_chat_id)
    if chat_id is None:
        return
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
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO events
                    (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
                     event_status, going_data, notgoing_data, counters_data, event_date)
                VALUES (?, ?, ?, ?, ?, ?, 0, '[]', '[]', '{}', ?)
                """,
                (event_id, chat_id, str(message.message_id),
                 event_name_raw, going_icon, notgoing_icon, event_date),
            )
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to save new event: {e}")
        await message.reply_text("❌ Database error: could not create event\\.", parse_mode="MarkdownV2")
        return

    date_line = f"{ICON_CLOCK} {escape_markdown(event_date)}\n" if event_date else ""
    text = (
        f"*{escape_markdown(event_name_raw)}*\n"
        f"{date_line}\n"
        f"*Going* \\(0\\):\n\n"
        f"*Not Going* \\(0\\):\n"
    )
    keyboard = create_event_keyboard(event_id, 0, going_icon, notgoing_icon, [], {})

    try:
        sent_msg = await context.bot.send_message(
            chat_id=chat_id, text=text, reply_markup=keyboard, parse_mode="MarkdownV2"
        )
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE events SET message_id = ? WHERE event_id = ?",
                (str(sent_msg.message_id), event_id),
            )
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to send event message: {e}")

    # Log to Google Sheets Events tab (premium hubs only - free hubs write
    # nothing to Sheets at all, silently and by design)
    # Columns: EVENT_ID, EVENT_NAME, CREATED_AT, CREATED_BY, EVENT_DATE, CLOSED_AT, STATUS, AMOUNT
    if is_premium(chat_id):
        sheet_target = await get_sheet_for_chat(chat_id)
        if not sheet_target:
            await message.reply_text("Please specify google sheet for save")
        else:
            try:
                ss = await open_spreadsheet(sheet_target)
                ws = await ss.worksheet("Events")
                await ws.append_row([
                    event_id, event_name_raw, now2ddmmyy(), user_raw, event_date or "", "", "OPEN", 0,
                ])
            except Exception as e:
                logger.error(f"Failed to log event creation to Google Sheets: {e}")


@register_hub_command("editevent")
async def editevent(update: Update, context: ContextTypes.DEFAULT_TYPE, override_chat_id: str = None):
    """
    Edits name, icons, or date of the current chat's active event.
    Only provided flags are updated; omitted ones keep their existing values.
    """
    chat_id = await resolve_hub_chat_id(update, context, "editevent", override_chat_id)
    if chat_id is None:
        return
    args    = context.args
    if not args:
        await update.message.reply_text(
            "❌ *Syntax error:* Provide at least one value to update\\.", parse_mode="MarkdownV2"
        )
        return

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT event_id, name, going_icon, notgoing_icon, event_date
            FROM events
            WHERE chat_id = ? AND event_status IN (0, 1)
            ORDER BY ROWID DESC LIMIT 1
            """,
            (chat_id,),
        )
        row = cursor.fetchone()
        if not row:
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

    await update.message.reply_text(
        "⚙️ *Event updated\\. Refreshing views\\.*", parse_mode="MarkdownV2"
    )
    context.application.create_task(schedule_view_refresh(context, event_id))

    # Sync updated name/date to Google Sheets Events tab
    try:
        sheet_target = await get_sheet_for_chat(chat_id)
        if not sheet_target:
            return
        ss = await open_spreadsheet(sheet_target)
        ws = await ss.worksheet("Events")
        records = await ws.get_all_records()
        for idx, r in enumerate(records, start=2):
            if str(r.get("EVENT_ID")) == str(event_id):
                # Column B = EVENT_NAME (2nd column), Column E = EVENT_DATE (5th column)
                if new_name is not None:
                    await ws.update(f"B{idx}", [[updated_name]])
                if date_raw is not None:
                    await ws.update(f"E{idx}", [[updated_date or ""]])
                break
    except Exception as e:
        logger.error(f"Failed to sync event update to Google Sheets: {e}")


@register_hub_command("notify")
async def notify(update: Update, context: ContextTypes.DEFAULT_TYPE, override_chat_id: str = None):
    """
    Pings all active users who haven't responded to the current event yet.
    Usage: /notify [text_msg]
    """
    chat_id = await resolve_hub_chat_id(update, context, "notify", override_chat_id)
    if chat_id is None:
        return
    message = update.message
    args = context.args

    # Get custom message if provided
    text_msg = " ".join(args) if args else ""

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT event_id, name, going_data, notgoing_data
            FROM events
            WHERE chat_id = ? AND event_status = 0
            ORDER BY ROWID DESC LIMIT 1
            """,
            (chat_id,),
        )
        event_row = cursor.fetchone()
        if not event_row:
            await message.reply_text(
                "❌ No active event found\\.", parse_mode="MarkdownV2"
            )
            return

        event_id, event_name, going_data, notgoing_data = event_row
        going_users   = {u.split(" (")[0] for u in json.loads(going_data)}
        notgoing_users = set(json.loads(notgoing_data))
        decided_users  = going_users | notgoing_users

        cursor.execute(
            "SELECT username FROM main_group_users WHERE chat_id = ? AND status = 'active'", (chat_id,)
        )
        all_active = cursor.fetchall()

    if not all_active:
        await message.reply_text(
            f"{ICON_STATS} No active users tracked in this chat\\.", parse_mode="MarkdownV2"
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

    if text_msg:
        header = f"{ICON_NOTIFY} {escape_markdown(event_name)}: {escape_markdown(text_msg)}\n_Please submit your status_\n\n"
    else:
        header = f"{ICON_NOTIFY} {escape_markdown(event_name)}\n_Please submit your status_\n\n"
    users_list = "\n".join(pending)
    await message.reply_text(header + users_list, parse_mode="MarkdownV2")


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------

@register_hub_command("updateuser")
async def updateuser(update: Update, context: ContextTypes.DEFAULT_TYPE, override_chat_id: str = None):
    """
    Updates a user's status in the current chat's registry.

    Usage: /updateuser [username(s)] [-a|-active|-p|-passive]
        -a  / -active   → status becomes 'active'
        -p  / -passive  → status becomes 'passive'

    Multiple users can be specified separated by commas:
        /updateuser @anfield, 8043690847, @anreon -a
    """
    chat_id = await resolve_hub_chat_id(update, context, "updateuser", override_chat_id)
    if chat_id is None:
        return
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


@register_hub_command("listusers")
async def listusers(update: Update, context: ContextTypes.DEFAULT_TYPE, override_chat_id: str = None):
    chat_id = await resolve_hub_chat_id(update, context, "listusers", override_chat_id)
    if chat_id is None:
        return
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT username, status FROM main_group_users WHERE chat_id = ?", (chat_id,))
        rows = cursor.fetchall()

    if not rows:
        await update.message.reply_text(
            f"{ICON_STATS} No users tracked for this chat\\.", parse_mode="MarkdownV2"
        )
        return

    lines = [f"• @{escape_markdown(r[0])} \\(`{escape_markdown(r[1])}`\\)" for r in rows]
    text  = f"{ICON_STATS} *Tracked Users:*\n\n" + "\n".join(lines)
    await update.message.reply_text(text, parse_mode="MarkdownV2")


@register_hub_command("adduser")
async def adduser(update: Update, context: ContextTypes.DEFAULT_TYPE, override_chat_id: str = None):
    """
    Manually adds users to the tracked user list (/listusers).
    Usage: /adduser <user_id|username> [user_id|username ...] [--chat_id chat_id | --monitor name]

    --chat_id / --monitor are how this feeds a monitored child group or
    channel's entry in main_group_users (there's no other way to populate
    it for a chat the bot doesn't otherwise see button clicks/messages in -
    see /refreshusers -g, which reads exactly these rows per monitor
    chat_id to sync the Google Sheets Users tab for that place).
    --monitor is just a friendlier alternative to --chat_id: it resolves a
    chat_name already registered via /addmonitor instead of requiring the
    admin to look up and paste the raw numeric chat_id.

    Examples:
      /adduser 123456789
      /adduser username1 username2
      /adduser 123456789 --chat_id -1001234567890
      /adduser username1 username2 --monitor "Downtown Channel"
    """
    args = context.args
    if len(args) < 1:
        await update.message.reply_text(
            "❌ *Syntax error:* `/adduser <user_id|username> [user_id|username ...] [--chat_id chat_id | --monitor name]`\n\n"
            "Examples:\n"
            "  `/adduser 123456789`\n"
            "  `/adduser username1 username2`\n"
            "  `/adduser 123456789 --chat_id -1001234567890`\n"
            "  `/adduser username1 username2 --monitor \"Downtown Channel\"`",
            parse_mode="MarkdownV2",
        )
        return

    chat_id = await resolve_hub_chat_id(update, context, "adduser", override_chat_id)
    if chat_id is None:
        return
    user_id = update.effective_user.id

    # Only admins can use this command
    if not await is_real_admin(context.bot, chat_id, update.effective_user, message=update.message):
        await update.message.reply_text(f"{ICON_ADMIN_ONLY} Only admins can use /adduser\\.", parse_mode="MarkdownV2")
        return

    # Parse --chat_id / --monitor parameter
    target_chat_id = chat_id
    user_identifiers = []
    i = 0
    while i < len(args):
        if args[i] == "--chat_id" and i + 1 < len(args):
            target_chat_id = args[i + 1]
            i += 2
        elif args[i] == "--monitor" and i + 1 < len(args):
            monitor_name = args[i + 1]
            with get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT chat_id FROM sub_groups WHERE chat_name = ? AND is_monitored = 1 "
                    "AND (owner_chat_id = ? OR owner_chat_id IS NULL)",
                    (monitor_name, chat_id),
                )
                monitor_row = cursor.fetchone()
            if not monitor_row:
                await update.message.reply_text(
                    f"❌ No monitor named `{escape_markdown(monitor_name)}` found\\. Check `/listmonitors`\\.",
                    parse_mode="MarkdownV2",
                )
                return
            target_chat_id = monitor_row[0]
            i += 2
        else:
            user_identifiers.append(args[i])
            i += 1

    added = []
    failed = []

    for identifier in user_identifiers:
        try:
            # Check if identifier is a numeric user_id
            if identifier.lstrip("-").isdigit():
                target_user_id = identifier
                try:
                    member = await context.bot.get_chat_member(target_chat_id, int(target_user_id))
                    if member.status in ("left", "kicked"):
                        # getChatMember succeeding with status=left/kicked is a
                        # VALID response (Telegram remembers a former member),
                        # not an exception - must be checked explicitly, or
                        # anyone who ever passed through the chat (or never
                        # was in it at all, depending on visibility settings)
                        # gets silently tracked as a current active member.
                        failed.append(f"{identifier}: not currently in that chat (status={member.status})")
                    else:
                        username = member.user.username or member.user.first_name or f"user{target_user_id}"
                        track_user(target_chat_id, username, "active", user_id=target_user_id)
                        added.append(f"@{escape_markdown(username)} \\({target_user_id}\\)")
                except Exception as e:
                    # If can't get user from Telegram, fail - don't add without real user_id
                    failed.append(f"{identifier}: {e}")
            else:
                # Treat as username. The Bot API's getChatMember only accepts
                # a numeric user_id - there is no way to look up a chat
                # member by @username directly. The only Bot-API-supported
                # path to resolve a username we've never seen before is the
                # chat's administrator list (getChatAdministrators returns
                # full User objects, username included) - a regular,
                # non-admin member can only be resolved once they've
                # interacted with the bot at least once (which then already
                # stores their user_id, making this whole branch moot for
                # them - see /refreshusers, ChatMemberHandler in main.py).
                target_username = identifier.lstrip("@")
                try:
                    admins = await context.bot.get_chat_administrators(target_chat_id)
                    match = next(
                        (a.user for a in admins if a.user.username and a.user.username.lower() == target_username.lower()),
                        None,
                    )
                    if match is None:
                        failed.append(
                            f"{identifier}: can't resolve a username to a user_id unless they're an "
                            f"admin of that chat or have already interacted with the bot"
                        )
                    else:
                        resolved_user_id = str(match.id)
                        resolved_username = match.username or match.first_name or target_username
                        track_user(target_chat_id, resolved_username, "active", user_id=resolved_user_id)
                        added.append(f"@{escape_markdown(resolved_username)} \\({resolved_user_id}\\)")
                except Exception as e:
                    # If can't resolve, fail - don't add without real user_id
                    failed.append(f"{identifier}: {e}")
        except Exception as e:
            failed.append(f"{escape_markdown(identifier)}: {escape_markdown(str(e))}")

    lines = []
    if target_chat_id != chat_id:
        lines.append(f"🎯 Target chat: `{target_chat_id}`")
    if added:
        lines.append(f"✅ Added: {', '.join(added)}")
    if failed:
        lines.append(f"❌ Failed: {', '.join(failed)}")
    if not lines:
        lines.append("❌ No users were added\\.")

    await update.message.reply_text("\n".join(lines))




# ---------------------------------------------------------------------------
# Subscription (owner-controlled, manual payment confirmation)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Subscription (moved to subscription.py)
# ---------------------------------------------------------------------------


@register_hub_command("refreshusers")
async def refreshusers(update: Update, context: ContextTypes.DEFAULT_TYPE, override_chat_id: str = None):
    """
    Synchronizes the tracked user list (the one /listusers shows) with actual
    chat membership, for THIS group only:
      - Removes (deletes) tracked users who are confirmed to have left/been
        kicked from the group, don't exist as Telegram users anymore, or are
        otherwise stale/unresolvable records.
      - Adds any chat administrator who isn't tracked yet, with status
        'active' by default.
      - Removes tracked users with no stored user_id outright (there's no
        reliable way to verify their membership without one - see the note
        below), instead of leaving them around forever.
      - Syncs the Google Sheets "Users" tab for this group (no-op on the
        free tier, since sheets are premium-only).

    To sync ALL monitored groups/channels in one go instead of just this
    one, see /refreshusersall.

    On the "adding missing members" side: the Telegram Bot API has no
    endpoint that lists every regular member of a group (only admins, via
    getChatAdministrators, which is what powers the "add" step below). Rank-
    and-file members who aren't admins get picked up automatically instead -
    either the moment they join (ChatMemberHandler in main.py) or the first
    time they click a button/send a message - not retroactively by this
    command alone. Users without a stored user_id yet (i.e. who joined
    before that tracking existed and have never interacted) get removed
    outright by this command, since getChatMember requires a numeric
    user_id, not a @username, so there's no way to ever confirm they're
    still here.
    """
    chat_id = await resolve_hub_chat_id(update, context, "refreshusers", override_chat_id)
    if chat_id is None:
        return

    # Only admins may run this
    if not await is_real_admin(context.bot, chat_id, update.effective_user, message=update.message):
        await update.message.reply_text(f"{ICON_ADMIN_ONLY} Only admins can use /refreshusers\\.", parse_mode="MarkdownV2")
        return

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT username, user_id, status FROM main_group_users WHERE chat_id = ?", (chat_id,)
        )
        rows = cursor.fetchall()

        # ── 1. Remove confirmed-departed/invalid/unverifiable users ──────────
        removed        = []
        still_present  = []  # (user_id, LIVE username straight from Telegram) - verified currently in the chat

        for username, user_id, status in rows:
            if not user_id:
                # No stored user_id at all - can never be membership-checked
                # (getChatMember requires a numeric ID, not a @username), so
                # there's no way to confirm they're still here. Remove
                # outright rather than keeping stale/unverifiable rows
                # around forever.
                removed.append(username)
                continue
            try:
                m = await context.bot.get_chat_member(
                    chat_id=int(chat_id), user_id=int(user_id)
                )
                if m.status in ["left", "kicked"]:
                    removed.append(username)
                else:
                    # Use the live Telegram username (public @handle preferred),
                    # not the possibly-stale one stored locally - this is what
                    # lets the Users sheet sync actually detect a name change.
                    live_username = getattr(m.user, "username", None) or getattr(m.user, "first_name", None) or username
                    still_present.append((user_id, live_username, m.user.first_name, m.user.last_name))
            except BadRequest as e:
                # "User not found" / "Chat member not found" - this could mean:
                # 1. User actually left the group
                # 2. User was just re-added but bot hasn't cached them yet
                # 3. Temporary API issue
                # To avoid false positives for recently re-added users, keep them
                # in the list. If they're truly gone, they'll be removed next time.
                logger.error(f"refreshusers: BadRequest for user {username} (user_id={user_id}): {e}")
                still_present.append((user_id, username, None, None))
            except Exception as e:
                # Any other error - keep them in list to avoid false removals
                logger.error(f"refreshusers: Exception for user {username} (user_id={user_id}): {e}")
                still_present.append((user_id, username, None, None))

        if removed:
            cursor.executemany(
                "DELETE FROM main_group_users WHERE chat_id = ? AND username = ?",
                [(chat_id, u) for u in removed],
            )
            conn.commit()

        # ── 2. Add missing chat administrators as 'active' ──────────────────────
        added = []
        try:
            admins = await context.bot.get_chat_administrators(chat_id)
            cursor.execute("SELECT username FROM main_group_users WHERE chat_id = ?", (chat_id,))
            already_tracked = {r[0] for r in cursor.fetchall()}

            for admin_member in admins:
                u = admin_member.user
                if u.is_bot:
                    continue
                uname = u.username or u.first_name or f"user{u.id}"
                if uname not in already_tracked:
                    track_user(chat_id, uname, "active", user_id=str(u.id),
                               first_name=u.first_name, last_name=u.last_name)
                    added.append(uname)
                still_present.append((str(u.id), uname, u.first_name, u.last_name))
        except Exception as e:
            logger.error(f"refreshusers: could not fetch chat administrators: {e}")

    # Dedupe still_present by user_id (an admin who was already tracked
    # would otherwise appear twice - once from step 1, once from step 2).
    # Also filter out entries without valid user_id for Google Sheets sync
    still_present = list({
        str(uid): (uid, uname, first_name, last_name)
        for uid, uname, first_name, last_name in still_present if uid and uid != uname
    }.values())

    lines = []
    if removed:
        mentions = ", ".join(f"@{escape_markdown(u)}" for u in removed)
        lines.append(f"{ICON_CLEAN} Removed \\(left, invalid, or unverifiable\\): {mentions}")
    if added:
        mentions = ", ".join(f"@{escape_markdown(u)}" for u in added)
        lines.append(f"➕ Added \\(new admins found\\): {mentions}")
    if not lines:
        lines.append("✅ Nothing to change \\- list already matches the group\\.")

    # ── 3. Sync the Google Sheets "Users" tab too (no-op on free tier) ──────
    try:
        await sync_users_sheet(chat_id, still_present)
        lines.append(f"{ICON_STATS} Users tab in Google Sheets synced\\.")
    except Exception as e:
        logger.error(f"refreshusers: Users sheet sync failed: {e}")
        lines.append(f"{ICON_WARNING} Could not sync the Users tab in Google Sheets\\.")

    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


@register_hub_command("refreshusersall")
async def refreshusersall(update: Update, context: ContextTypes.DEFAULT_TYPE, override_chat_id: str = None):
    """
    Same as /refreshusers, but for every monitored group/channel under this
    hub in one go (see /addmonitor). This is a heavier, potentially slow
    bulk operation - it makes live Telegram API calls for every tracked
    user AND every admin in EVERY monitored chat, one after another - so
    it's kept as its own explicit command rather than a flag on the
    lightweight, everyday /refreshusers.
    """
    chat_id = await resolve_hub_chat_id(update, context, "refreshusersall", override_chat_id)
    if chat_id is None:
        return

    if not await is_real_admin(context.bot, chat_id, update.effective_user, message=update.message):
        await update.message.reply_text(f"{ICON_ADMIN_ONLY} Only admins can use /refreshusersall\\.", parse_mode="MarkdownV2")
        return

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT chat_id, chat_type, chat_name FROM sub_groups WHERE is_monitored = 1 AND (owner_chat_id = ? OR owner_chat_id IS NULL)",
            (chat_id,),
        )
        monitors = cursor.fetchall()

    if not monitors:
        await update.message.reply_text(
            f"{ICON_GLOBE} No monitored groups/channels configured\\. See /addmonitor\\.", parse_mode="MarkdownV2"
        )
        return

    lines = [f"{ICON_GLOBE} *Processing monitored groups/channels:*"]
    for monitor_chat_id, chat_type, chat_name in monitors:
        try:
            with get_connection() as conn_mon:
                cursor_mon = conn_mon.cursor()

                # Local sync for monitored group (remove departed, add admins)
                cursor_mon.execute(
                    "SELECT username, user_id, status FROM main_group_users WHERE chat_id = ?",
                    (monitor_chat_id,),
                )
                monitor_rows = cursor_mon.fetchall()

                monitor_removed = []
                monitor_present = []

                for username, user_id, status in monitor_rows:
                    if not user_id:
                        monitor_removed.append(username)
                        continue
                    try:
                        m = await context.bot.get_chat_member(
                            chat_id=int(monitor_chat_id), user_id=int(user_id)
                        )
                        if m.status in ["left", "kicked"]:
                            monitor_removed.append(username)
                        else:
                            live_username = getattr(m.user, "username", None) or getattr(m.user, "first_name", None) or username
                            monitor_present.append((user_id, live_username, m.user.first_name, m.user.last_name))
                    except BadRequest:
                        monitor_removed.append(username)
                    except Exception:
                        monitor_removed.append(username)

                if monitor_removed:
                    cursor_mon.executemany(
                        "DELETE FROM main_group_users WHERE chat_id = ? AND username = ?",
                        [(monitor_chat_id, u) for u in monitor_removed],
                    )
                    conn_mon.commit()

                # Add missing admins for monitored group
                monitor_added = []
                try:
                    monitor_admins = await context.bot.get_chat_administrators(int(monitor_chat_id))
                    cursor_mon.execute("SELECT username FROM main_group_users WHERE chat_id = ?", (monitor_chat_id,))
                    monitor_tracked = {r[0] for r in cursor_mon.fetchall()}

                    for admin_member in monitor_admins:
                        u = admin_member.user
                        if u.is_bot:
                            continue
                        uname = u.username or u.first_name or f"user{u.id}"
                        if uname not in monitor_tracked:
                            track_user(monitor_chat_id, uname, "active", user_id=str(u.id),
                                       first_name=u.first_name, last_name=u.last_name)
                            monitor_added.append(uname)
                        monitor_present.append((str(u.id), uname, u.first_name, u.last_name))
                except Exception as e:
                    logger.error(f"refreshusersall: could not fetch admins for {chat_name}: {e}")

            # Dedupe monitor_present
            monitor_present = list({
                str(uid): (uid, uname, first_name, last_name)
                for uid, uname, first_name, last_name in monitor_present
            }.values())

            # Sync to sheets with place_id (each monitor gets its own place_id)
            await sync_users_sheet(monitor_chat_id, monitor_present)

            status_line = f"  ✅ Synced: `{escape_markdown(chat_name)}`"
            if monitor_removed:
                status_line += f" \\(-{len(monitor_removed)}\\)"
            if monitor_added:
                status_line += f" \\(+{len(monitor_added)}\\)"
            lines.append(status_line)
        except Exception as e:
            logger.error(f"refreshusersall failed for {chat_name}: {e}")
            lines.append(f"  ❌ Failed: `{escape_markdown(chat_name)}`")

    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


# ---------------------------------------------------------------------------
# Event sharing
# ---------------------------------------------------------------------------

@register_hub_command("shareevent")
async def shareevent(update: Update, context: ContextTypes.DEFAULT_TYPE, override_chat_id: str = None):
    """
    Forwards a synced sub-view of the active event to a child group/channel.
    All error messages route back to the main hub group.
    """
    main_hub_chat_id = await resolve_hub_chat_id(update, context, "shareevent", override_chat_id)
    if main_hub_chat_id is None:
        return
    user_id          = update.effective_user.id
    args             = context.args

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

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT event_id, name, event_status, going_icon, notgoing_icon
            FROM events
            WHERE chat_id = ? AND event_status IN (0, 1)
            ORDER BY ROWID DESC LIMIT 1
            """,
            (str(main_hub_chat_id),),
        )
        event_row = cursor.fetchone()
        if not event_row:
            await context.bot.send_message(
                chat_id=main_hub_chat_id,
                text="❌ No active event found for this group\\.",
                parse_mode="MarkdownV2",
            )
            return

        event_id, name, event_status, going_icon, notgoing_icon = event_row

        cursor.execute(
            "SELECT chat_id FROM sub_groups WHERE alias = ? AND (owner_chat_id = ? OR owner_chat_id IS NULL)",
            (target_input.lower(), str(main_hub_chat_id)),
        )
        alias_row        = cursor.fetchone()
        target_chat_raw  = alias_row[0] if alias_row else target_input

        if str(target_chat_raw) == str(main_hub_chat_id):
            await context.bot.send_message(
                chat_id=main_hub_chat_id,
                text=f"{ICON_WARNING} Cannot share an event to the same group that owns it\\.",
                parse_mode="MarkdownV2",
            )
            return

        if not is_premium(main_hub_chat_id):
            cursor.execute(
                """
                SELECT COUNT(*) FROM event_shares es
                JOIN events e ON es.event_id = e.event_id
                WHERE e.chat_id = ? AND es.chat_id = ?
                """,
                (str(main_hub_chat_id), str(target_chat_raw)),
            )
            (share_count,) = cursor.fetchone()
            if share_count >= FREE_SHAREEVENT_LIMIT_PER_TARGET:
                await context.bot.send_message(
                    chat_id=main_hub_chat_id,
                    text="You used all free limit for /shareevent, move to PRO subscription for more",
                )
                return

        cursor.execute(
            "SELECT message_id FROM event_shares WHERE event_id = ? AND chat_id = ?",
            (event_id, str(target_chat_raw)),
        )
        if cursor.fetchone():
            await context.bot.send_message(
                chat_id=main_hub_chat_id,
                text=f"{ICON_WARNING} This group or channel has already been added\\.",
                parse_mode="MarkdownV2",
            )
            return

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
            text=f"{ICON_WARNING} Unexpected error reaching target chat\\.",
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

    if user_id == GROUP_ANONYMOUS_BOT_ID:
        await context.bot.send_message(
            chat_id=main_hub_chat_id,
            text=f"{ICON_ADMIN_ONLY} Please disable \"Remain anonymous\" and re\\-run /shareevent \\- "
                 f"your admin status in the target chat can't be verified anonymously\\.",
            parse_mode="MarkdownV2",
        )
        return

    try:
        user_member = await context.bot.get_chat_member(chat_id=target_chat_api, user_id=user_id)
        if user_member.status in ["left", "kicked"]:
            await context.bot.send_message(
                chat_id=main_hub_chat_id,
                text=f"{ICON_ADMIN_ONLY} You are not a member of that chat\\.",
                parse_mode="MarkdownV2",
            )
            return
        if user_member.status not in ["administrator", "creator"]:
            await context.bot.send_message(
                chat_id=main_hub_chat_id,
                text=f"{ICON_ADMIN_ONLY} You need admin rights in the target chat to share there\\.",
                parse_mode="MarkdownV2",
            )
            return
    except Exception as e:
        logger.error(f"shareevent user check: {e}")
        await context.bot.send_message(
            chat_id=main_hub_chat_id,
            text=f"{ICON_WARNING} Could not verify your membership in the target chat\\.",
            parse_mode="MarkdownV2",
        )
        return

    try:
        sent = await context.bot.send_message(
            chat_id=target_chat_api,
            text=f"{ICON_SHARED} *{escape_markdown(name)}*\n_Synchronising\\.\\.\\._",
            parse_mode="MarkdownV2",
        )
        with get_connection() as conn:
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
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT username FROM main_group_users WHERE chat_id = ? AND status = 'active'", (chat_id,)
        )
        rows = cursor.fetchall()

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


def _mention_link(chat_id: str, username: str, user_id=None) -> str:
    """
    Builds a clickable MarkdownV2 mention - [First Last](tg://user?id=...) -
    using the stored first_name/last_name for this user_id if we have it on
    file (see db.get_display_name), falling back to the plain @username/
    display name we were given if we don't have a resolvable user_id at all
    (e.g. someone added via /adduser whose name was never captured).

    Unlike Telegram's own automatic @mention linking, this works even for
    users who have no @username set - the whole point of this feature.

    `user_id` can be omitted if the caller only has a plain username on
    hand (e.g. entries in the Not Going list, which - unlike Going - don't
    carry a "(user_id)" suffix) - in that case it's looked up from
    main_group_users by (chat_id, username).
    """
    if user_id is None:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT user_id FROM main_group_users WHERE chat_id = ? AND username = ?",
                (str(chat_id), username),
            )
            row = cursor.fetchone()
            user_id = row[0] if row else None

    if not user_id or not str(user_id).lstrip("-").isdigit():
        return escape_markdown(username)

    display = get_display_name(str(chat_id), str(user_id), username)
    return f"[{escape_markdown(display)}](tg://user?id={user_id})"


async def update_all_shared_views(context: ContextTypes.DEFAULT_TYPE, event_id: str):
    """
    Re-renders EVERY view of one event after its state changed: the master
    post in the hub group, plus every child chat/channel it's been shared
    to (via /shareevent). Called after every going/notgoing/kick/guest
    click, /editevent, Save & Close, and Cancel Event - normally through
    schedule_view_refresh() (see its own docstring for why it's not called
    directly), never straight from a click handler.

    What it does, roughly in order:
      1. Reads the master event row (going/notgoing lists, guest counters,
         kicked list, event_status) - this is the single source of truth
         that everything below gets rendered from.
      2. Reads every event_shares row (one per chat/channel this event has
         been shared to) plus that child chat's own event_users rows
         (their own going/notgoing/guest state, tracked independently of
         the master hub's own going list).
      3. Builds the master hub's message text + keyboard (via
         create_event_keyboard) and edits that message in place.
      4. For each child share: builds that child's own message text +
         keyboard (which only shows THAT child's participants, not the
         whole event) and edits it too - respecting whatever share mode
         (-visible/-onlycount/-hidden) it was shared with.

    All DB reads happen up front in ONE connection, closed before any
    Telegram API calls are made - editing N child chats means N sequential
    network calls, and there's no reason to hold a SQLite connection open
    across all of them.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT chat_id, message_id, name, going_icon, notgoing_icon,
                   event_status, going_data, notgoing_data, counters_data, event_date, kicked_data
            FROM events WHERE event_id = ?
            """,
            (event_id,),
        )
        master = cursor.fetchone()
        if not master:
            return

        (main_chat_id, main_msg_id, name, going_icon, notgoing_icon,
         event_status, going_data, notgoing_data, counters_data, event_date, kicked_data) = master

        master_going     = json.loads(going_data)
        master_not_going = json.loads(notgoing_data)
        master_counters  = json.loads(counters_data)
        master_kicked    = set(json.loads(kicked_data or "[]"))

        cursor.execute(
            "SELECT chat_id, message_id, share_mode FROM event_shares WHERE event_id = ?", (event_id,)
        )
        shares = cursor.fetchall()

        # Fetch every share's event_users rows up front, while the
        # connection is open, instead of re-querying inside the loop below -
        # that loop also makes Telegram API calls (get_chat), which
        # shouldn't happen while holding a DB connection open.
        per_share_users = {}
        for s_chat_id, _, _ in shares:
            cursor.execute(
                "SELECT username, status, guests, user_id FROM event_users "
                "WHERE event_id = ? AND chat_id = ?",
                (event_id, str(s_chat_id)),
            )
            per_share_users[str(s_chat_id)] = cursor.fetchall()

    child_data              = {}
    total_child_going       = 0
    child_addons_for_master = []

    for s_chat_id, _, _ in shares:
        users      = per_share_users[str(s_chat_id)]
        users_list = []
        chat_sum   = 0
        for username, status, guests, u_id in users:
            if status == "going":
                users_list.append(f"{going_icon} {_mention_link(s_chat_id, username, u_id)}")
                chat_sum += 1
            if guests > 0:
                users_list.append(f"{ICON_GUEST} {guests}, from: {_mention_link(s_chat_id, username, u_id)}")
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
                f"\n\n*Going from {escape_markdown(chat_title)}*"
                f" \\({chat_sum}\\):\n" + "\n".join(users_list)
            )
            child_addons_for_master.append(block)

    master_shares_block = "".join(child_addons_for_master)

   
    total_master_guests = sum(master_counters.values())
    total_master_going  = len(master_going) + total_master_guests
    current_post_total  = total_master_going
    global_total        = current_post_total + total_child_going

    going_names_list = [
        f"{going_icon} {_mention_link(main_chat_id, u.split(' (')[0], u.split('(')[-1].rstrip(')') if '(' in u else None)}"
        for u in master_going
    ]

    # Guest lines are now folded directly into the Going list instead of a
    # separate "Guests:" section - one line per contributor, "N, from: Name".
    guest_lines = []
    for entry in master_going:
        u_name = entry.split(" (")[0]
        u_id   = entry.split("(")[-1].rstrip(")") if "(" in entry else None
        if master_counters.get(u_name, 0) > 0:
            guest_lines.append(f"{ICON_GUEST} {master_counters[u_name]}, from: {_mention_link(main_chat_id, u_name, u_id)}")
    # Also include guests from users who are not going (kicked users with guests)
    for k, count in master_counters.items():
        if k not in {u.split(" (")[0] for u in master_going} and count > 0:
            guest_lines.append(f"{ICON_GUEST} {count}, from: {_mention_link(main_chat_id, k)}")

    going_list_text = "\n".join(going_names_list + guest_lines)

    not_going_list_text = (
        "\n".join(f"{notgoing_icon} {_mention_link(main_chat_id, u)}" for u in master_not_going)
        if master_not_going else ""
    )

    # Header: changed wording
    header      = f"{ICON_WARNING} *SQUAD VERIFICATION*\n_Review members before save_\n\n" if event_status == 1 else ""
    date_line   = f"{ICON_CLOCK} {escape_markdown(event_date)}\n" if event_date else ""
    title_line  = f"{ICON_CANCEL_EVENT} *CANCELED* ~{escape_markdown(name)}~" if event_status == -1 else f"*{escape_markdown(name)}*"

    master_text = (
        f"{header}{title_line}\n\n {date_line}\n"
        f"*Going* \\({total_master_going}\\):\n{going_list_text}\n\n"
        f"*Not Going* \\({len(master_not_going)}\\):\n{not_going_list_text}"
        f"{master_shares_block}\n\n"
        f"{ICON_STATS} *TOTAL Going:* {global_total}"
    )

    # Keyboard buttons for master (verification mode needs child rows too)
    with get_connection() as conn2:
        cursor2 = conn2.cursor()
        cursor2.execute(
            "SELECT username, guests, status FROM event_users "
            "WHERE event_id = ? AND (guests > 0 OR status IN ('going', 'kicked'))",
            (event_id,),
        )
        all_child_going_for_buttons = cursor2.fetchall()

    master_keyboard = create_event_keyboard(
        event_id, event_status, going_icon, notgoing_icon,
        master_going, master_counters,
        is_child=False,
        child_users_rows=all_child_going_for_buttons,
        kicked_users=master_kicked,
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
    child_title_name    = (
        f"{ICON_CANCEL_EVENT} *CANCELED* ~{escape_markdown(name)}~"
        if event_status == -1 else f"*{escape_markdown(name)}*"
    )
    for s_chat_id, _, _ in shares:
        await _get_title(s_chat_id)

    async def _render_and_edit_child(s_chat_id, s_msg_id, mode):
        c_info = child_data.get(str(s_chat_id), {"users_text": "", "count": 0})

        if mode == "-visible":
            child_text = (
                f"{ICON_SHARED} {child_title_name}\n"
                f"{date_line} \n"
                f"*Going from {escaped_main_title}* \\({current_post_total}\\):\n{going_list_text}\n\n"
            )
            for other_id, _, _ in shares:
                if str(other_id) != str(s_chat_id):
                    o_title = title_cache.get(str(other_id), "Group")
                    o_info  = child_data.get(str(other_id), {"users_text": "", "count": 0})
                    if o_info["count"] > 0:
                        child_text += (
                            f"*Going from {escape_markdown(o_title)}*"
                            f" \\({o_info['count']}\\):\n{o_info['users_text']}\n\n"
                        )

        elif mode == "-onlycount":
            child_text = (
                f"{ICON_SHARED} {child_title_name}\n"
                f"{date_line} \n"
                f"*Going from {escaped_main_title}:* {current_post_total}\n\n"
            )
            for other_id, _, _ in shares:
                if str(other_id) != str(s_chat_id):
                    o_title = title_cache.get(str(other_id), "Group")
                    o_info  = child_data.get(str(other_id), {"count": 0})
                    child_text += f"*Going from {escape_markdown(o_title)}:* {o_info['count']}\n"
            child_text += "\n"

        else:  # "-hidden"
            child_text = (
                f"{ICON_SHARED} {child_title_name}\n\n_Data hidden by admin\\._\n"
                f"{date_line} \n"
            )

        child_text += (
            f"*Going here:* \\({c_info['count']}\\)\n{c_info['users_text']}\n\n"
            f"{ICON_STATS} *Total Going \\(all groups\\):* {global_total}\n"
        )

        child_keyboard = create_event_keyboard(
            event_id, event_status, going_icon, notgoing_icon, is_child=True
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
    The main state machine: every inline keyboard click across every chat
    (master hub or child) comes through here. Broad shape of what happens
    on each click:

      1. Parse callback_data into (action, event_id) - or
         (action, event_id, target_username) for per-person actions like
         kick_<id>:<username> during verification.
      2. Figure out whether this click came from the master hub or a child
         chat (click_chat_id vs the event's own chat_id), and whether the
         clicker is an admin (needed to gate admin-only actions: close,
         cancel, kick/return, save, add-extra-player).
      3. Acquire this event's lock (get_event_lock) so two near-simultaneous
         clicks on the SAME event can't interleave their read-modify-write
         and silently drop one - this is the one place all DB writes for
         an event go through.
      4. Inside the lock: read the current event row, apply exactly ONE
         state change based on (event_status, action, is_child), then
         write it back in a single UPDATE.
      5. Outside the lock (after committing): schedule a re-render of every
         view of this event via schedule_view_refresh(), and best-effort
         log the action to the Google Sheets "Actions" tab.

    event_status branches (see keyboard.py's create_event_keyboard for the
    matching button layout each one shows):
      0  (open)         - going/notgoing/add/sub/close/cancel
      1  (verification)  - kick/return/incgst/decgst/addext/save, master
                            hub only; child-chat clicks during verification
                            only ever touch guest counts/kick-return for
                            THAT child's own participants (event_users table)
      2  (closed) / -1 (canceled) - no buttons are shown at all (see
                            create_event_keyboard), so this function should
                            never actually receive a click for these -
                            treated as a no-op safety net if it somehow does.
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

    if callback_data.startswith("help_"):
        # These belong to help_callback_handler/help_back_handler - should
        # never reach here if those are registered before button_handler in
        # main.py, but guard anyway rather than silently misparsing
        # "help_alias" as action="help", event_id="alias".
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

    is_admin = await is_real_admin(context.bot, query.message.chat.id, user)

    data_changed = False
    event_status = 0

    lock = get_event_lock(event_id)
    async with lock:
        # track_user() opens its OWN separate SQLite connection - calling it
        # from inside the transaction below (before it commits) causes
        # "database is locked", since two connections would be trying to
        # write to the same file at once. Collect what needs tracking here,
        # and only actually call track_user() after the transaction below
        # has committed and closed.
        pending_track_user = []
        try:
            with get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT chat_id, message_id, name, going_icon, notgoing_icon,
                           event_status, going_data, notgoing_data, counters_data, event_date, kicked_data
                    FROM events WHERE event_id = ?
                    """,
                    (event_id,),
                )
                row = cursor.fetchone()
                if not row:
                    return

                (main_chat_id, main_msg_id, name, going_icon, notgoing_icon,
                 event_status, going_data, notgoing_data, counters_data, event_date, kicked_data) = row

                going     = json.loads(going_data)
                not_going = set(json.loads(notgoing_data))
                counters  = json.loads(counters_data)
                kicked    = json.loads(kicked_data or "[]")

                if event_status in (2, -1):
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
                        try:
                            await query.answer(
                                text=f"{ICON_WARNING} You are already added to this event in another group/channel",
                                show_alert=True,
                            )
                        except Exception:
                            pass
                        return

                # ── Child-chat interaction ────────────────────────────────────
                if is_click_in_child:
                    if action not in ["going", "notgoing", "add", "sub"]:
                        return

                    cursor.execute(
                        "SELECT status, guests FROM event_users WHERE event_id = ? AND chat_id = ? AND user_id = ?",
                        (event_id, click_chat_id, str(user_id)),
                    )
                    u_row          = cursor.fetchone()
                    current_status = u_row[0] if u_row else "none"
                    current_guests = u_row[1] if u_row else 0

                    if action == "going":
                        # In child chats, Going should only set status to 'going', never toggle off
                        cursor.execute(
                            "INSERT OR REPLACE INTO event_users (event_id, chat_id, user_id, username, status, guests) VALUES (?, ?, ?, ?, 'going', ?)",
                            (event_id, click_chat_id, str(user_id), username_raw, current_guests),
                        )
                        pending_track_user.append(
                            (click_chat_id, username_raw, str(user_id), user.first_name, user.last_name)
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
                        pending_track_user.append(
                            (click_chat_id, username_raw, str(user_id), user.first_name, user.last_name)
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
                        # NOTE: In child chats, user must have status (going/notgoing)
                        # Sub Guest only decrements guests, never removes the user
                        if current_guests > 0:
                            new_guests = current_guests - 1
                            cursor.execute(
                                "UPDATE event_users SET guests = ? WHERE event_id = ? AND chat_id = ? AND user_id = ?",
                                (new_guests, event_id, click_chat_id, str(user_id)),
                            )
                            data_changed = True
                        else:
                            return

                    conn.commit()

                    if data_changed:
                        try:
                            sheet_target = await get_sheet_for_chat(main_chat_id)
                            if sheet_target:
                                ss = await open_spreadsheet(sheet_target)
                                ws = await ss.worksheet("Actions")
                                await ws.append_row([
                                    event_id, action.upper(), username_raw,
                                    str(user_id), now2ddmmyy(), str(click_chat_id),
                                ])
                        except Exception as e:
                            logger.error(f"Sheets child action log failed: {e}")
                        context.application.create_task(schedule_view_refresh(context, event_id))
                    return

                # ── Admin-only actions guard ──────────────────────────────────
                if action in ["close", "kick", "save", "incgst", "decgst", "addext", "cancel"]:
                    if not is_admin:
                        return

                # ── Master open (event_status == 0) ───────────────────────────
                if event_status == 0:
                    if action == "going":
                        if username_raw not in going_usernames:
                            going.append(f"{username_raw} ({user_id})")
                        not_going.discard(username_raw)
                        # Store user_id for refreshusers
                        pending_track_user.append(
                            (click_chat_id, username_raw, str(user_id), user.first_name, user.last_name)
                        )
                        data_changed = True
                    elif action == "notgoing":
                        going    = [u for u in going if u.split(" (")[0] != username_raw]
                        not_going.add(username_raw)
                        pending_track_user.append(
                            (click_chat_id, username_raw, str(user_id), user.first_name, user.last_name)
                        )
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
                                # Don't remove from counters if user is in going list
                                # Keep them with 0 guests if they're going
                                if username_raw not in going_usernames:
                                    counters.pop(username_raw)
                                else:
                                    counters[username_raw] = 0
                            data_changed = True
                        else:
                            return
                    elif action == "close":
                        event_status = 1
                        data_changed = True
                    elif action == "cancel":
                        event_status = -1
                        data_changed = True

                # ── Master verification (event_status == 1) ───────────────────
                elif event_status == 1:
                    if action == "addext":
                        context.user_data["awaiting_extra_player_for"] = event_id
                        await query.message.reply_text(
                            "📝 *Verification Mode:* Type the extra member's username:",
                            parse_mode="MarkdownV2",
                        )
                        return

                    is_target_child  = target_username and target_username.startswith("ch-")
                    clean_target_usr = target_username.replace("ch-", "", 1) if is_target_child else target_username

                    if action == "kick" and target_username:
                        if is_target_child:
                            # 'kicked' is distinct from '' (guest-only, never
                            # declared going) so the keyboard can tell the two
                            # apart and only show Return for genuinely-kicked
                            # people - see create_event_keyboard's docstring.
                            cursor.execute(
                                "UPDATE event_users SET status = 'kicked' WHERE event_id = ? AND username = ?",
                                (event_id, clean_target_usr),
                            )
                        else:
                            going = [u for u in going if u.split(" (")[0] != clean_target_usr]
                            # Don't pop counters - guests should remain even after user is kicked
                            if clean_target_usr not in kicked:
                                kicked.append(clean_target_usr)
                        data_changed = True

                    elif action == "return" and target_username:
                        if is_target_child:
                            # Set status back to 'going'
                            cursor.execute(
                                "UPDATE event_users SET status = 'going' WHERE event_id = ? AND username = ?",
                                (event_id, clean_target_usr),
                            )
                        else:
                            if clean_target_usr in kicked:
                                kicked.remove(clean_target_usr)
                            # Add user back to going list
                            # Try to find if they have a stored user_id
                            cursor.execute(
                                "SELECT user_id FROM main_group_users WHERE username = ? AND chat_id = ?",
                                (clean_target_usr, main_chat_id),
                            )
                            user_id_row = cursor.fetchone()

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
                        event_status = 2
                        data_changed = True

                cursor.execute(
                    "UPDATE events SET event_status = ?, going_data = ?, notgoing_data = ?, counters_data = ?, kicked_data = ? WHERE event_id = ?",
                    (event_status, json.dumps(going), json.dumps(list(not_going)), json.dumps(counters), json.dumps(kicked), event_id),
                )
                conn.commit()

        except Exception as db_err:
            logger.error(f"SQLite transaction failure: {db_err}")
            return

        # Now that the transaction above has committed and its connection
        # is closed, it's safe for track_user() to open its own connection
        # without hitting "database is locked".
        for t_chat_id, t_username, t_user_id, t_first_name, t_last_name in pending_track_user:
            track_user(t_chat_id, t_username, "active", user_id=t_user_id,
                       first_name=t_first_name, last_name=t_last_name)

        # Log action to Sheets
        if data_changed:
            try:
                sheet_target = await get_sheet_for_chat(main_chat_id)
                if sheet_target:
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
                if not ss:
                    pass  # free tier / no sheet configured / expired - SQLite-only save, nothing more to do
                else:
                    # 1. Collect master going user_ids (stored as "username (user_id)").
                    #    Entries added via "Add Extra Member" should have user_id
                    #    If no user_id is available, use username as fallback
                    master_going_ids = []
                    for entry in going:
                        m = re.search(r'\(([^)]+)\)', entry)
                        if m:
                            master_going_ids.append(m.group(1))
                        else:
                            # Extra player without user_id - use username as user_id
                            username = entry.split(" (")[0]
                            master_going_ids.append(username)

                    # 2. Collect child going user_ids from event_users table
                    with get_connection() as conn_eu:
                        cursor_eu = conn_eu.cursor()
                        cursor_eu.execute(
                            "SELECT user_id FROM event_users WHERE event_id = ? AND status = 'going'",
                            (event_id,),
                        )
                        child_going_ids = [r[0] for r in cursor_eu.fetchall()]

                        all_going_ids = master_going_ids + child_going_ids

                        # 3. Compute total for Events sheet
                        # Include all child users (going + those with guests) and their guests
                        cursor_eu.execute(
                            "SELECT status, guests FROM event_users WHERE event_id = ? AND (status = 'going' OR guests > 0)",
                            (event_id,),
                        )
                        child_rows = cursor_eu.fetchall()
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

        # ── Cancel Event: mark Events row as Canceled, write NOTHING to EventUsers ─
        if action == "cancel":
            try:
                sheet_target = await get_sheet_for_chat(main_chat_id)
                ss           = await open_spreadsheet(sheet_target)
                if not ss:
                    pass  # free tier / no sheet configured / expired - SQLite-only cancel, nothing more to do
                else:
                    ws      = await ss.worksheet("Events")
                    records = await ws.get_all_records()
                    found   = False
                    for idx, r in enumerate(records, start=2):
                        if str(r.get("EVENT_ID")) == str(event_id):
                            # Update existing row to CANCELED
                            await ws.update(f"F{idx}:H{idx}", [[now2ddmmyy(), "CANCELED", 0]])
                            found = True
                            break
                    if not found:
                        # Only append if row doesn't exist
                        await ws.append_row([
                            event_id, name, now2ddmmyy(), username_raw,
                            event_date or "", now2ddmmyy(), "CANCELED", 0,
                        ])
                    # Intentionally NOT calling sync_event_users_sheet here -
                    # a cancelled event must not write anything to EventUsers.
            except Exception as e:
                logger.error(f"Sheets cancel pipeline failed: {e}")


# ---------------------------------------------------------------------------
# Extra player input handler
# ---------------------------------------------------------------------------

async def handle_extra_player_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles raw text when an admin is expected to type an extra player's username
    during verification mode (event_status == 1).
    """
    event_id = context.user_data.get("awaiting_extra_player_for")
    if not event_id:
        return

    context.user_data.pop("awaiting_extra_player_for", None)
    chat_id  = str(update.effective_chat.id)
    raw_text = update.message.text.strip()

    if not await is_real_admin(context.bot, update.effective_chat.id, update.effective_user, message=update.message):
        return

    target_username = raw_text.lstrip('@').strip()
    if not target_username:
        await update.message.reply_text("❌ Invalid username.")
        return

    lock = get_event_lock(event_id)
    async with lock:
        try:
            with get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT going_data, counters_data, notgoing_data FROM events WHERE event_id = ?", (event_id,)
                )
                row = cursor.fetchone()
                if not row:
                    return

                going, counters = json.loads(row[0]), json.loads(row[1])
                not_going       = json.loads(row[2])
                if target_username not in {u.split(" (")[0] for u in going}:
                    # Resolve the real Telegram user_id via main_group_users (the
                    # /listusers table) - this is the only reliable source we have,
                    # since Telegram's getChatMember requires a numeric user_id and
                    # has no "look up by username" mode to fall back on.
                    cursor.execute(
                        "SELECT user_id FROM main_group_users WHERE chat_id = ? AND username = ?",
                        (chat_id, target_username),
                    )
                    user_row = cursor.fetchone()
                    user_id = user_row[0] if user_row and user_row[0] else None

                    if user_id:
                        going.append(f"{target_username} ({user_id})")
                    else:
                        # No known id for this username - mark it explicitly rather
                        # than fabricating a fake one, so this is easy to spot and
                        # fix later (e.g. via /refreshusers) in EventUsers.
                        going.append(f"{target_username} (no_id_in_main_group)")

                # If this person had previously been marked Not Going, being added
                # as an extra player means they're going now - they must not remain
                # in the not-going list too.
                if target_username in not_going:
                    not_going.remove(target_username)

                cursor.execute(
                    "UPDATE events SET going_data = ?, counters_data = ?, notgoing_data = ? WHERE event_id = ?",
                    (json.dumps(going), json.dumps(counters), json.dumps(not_going), event_id),
                )
                conn.commit()
        except Exception as e:
            logger.error(f"Extra player DB failure: {e}")
            return

    try:
        await update.message.delete()
    except Exception:
        pass

    try:
        sheet_target = await get_sheet_for_chat(chat_id)
        if sheet_target:
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


# ---------------------------------------------------------------------------
# Global text router
# ---------------------------------------------------------------------------

async def global_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Routes all text messages: extra-player input first, then @everyone tracking."""
    if context.user_data.get("awaiting_extra_player_for"):
        await handle_extra_player_input(update, context)
        return
    await track_everyone_message(update, context)
