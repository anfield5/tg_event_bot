import json
import re
from uuid import uuid4

from telegram import Update
from telegram.ext import ContextTypes
from telegram.error import BadRequest

from keyboard import create_event_keyboard
from subscription import is_premium, has_feature
from aliases import setalias, removealias, listalias
from monitors import addmonitor, removemonitor, listmonitors
from help_system import (
    userid, chatid, help_command, help_callback_handler, help_back_handler,
    upgrade_info_callback_handler,
)
from event_engine import (
    get_event_lock, schedule_view_refresh, update_all_shared_views, button_handler, _mention_link,
)

from config import (
    DEFAULT_GOING_ICON, DEFAULT_NOTGOING_ICON, logger,
    ICON_SHARED, ICON_STATS, ICON_WARNING,
    ICON_CLOCK, ICON_NOTIFY, ICON_CLEAN, ICON_ADMIN_ONLY, ICON_GLOBE, ICON_STANDBY,
)
from utils import escape_markdown, now2ddmmyy, parse_event_date, is_real_admin, GROUP_ANONYMOUS_BOT_ID
from db import track_user, get_connection, get_feature_limit_for_chat
from hub_resolver import resolve_hub_chat_id, register_hub_command
from sheets import (
    get_sheet_for_chat, open_spreadsheet, sync_users_sheet,
)


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

async def _validate_limit_flag(message, chat_id: str, limit_raw: str):
    """
    Validates -limit's gate (event_limit feature) and value (must be a
    positive whole number). Shared by newevent and editevent, which had
    identical validation logic duplicated inline before this extraction.

    Returns the validated int on success. Returns None if a reply was
    already sent to the user (the caller should `return` immediately in
    that case, without proceeding any further).
    """
    if not has_feature(chat_id, "event_limit"):
        await message.reply_text(
            f"{ICON_WARNING} `\\-limit` requires a higher tier\\. Contact the bot owner to upgrade\\.",
            parse_mode="MarkdownV2",
        )
        return None
    if not limit_raw.isdigit() or int(limit_raw) <= 0:
        await message.reply_text(
            "❌ *Invalid `\\-limit` value\\.* Must be a whole number greater than 0\\.",
            parse_mode="MarkdownV2",
        )
        return None
    return int(limit_raw)


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
    -limit <N> [visible|hidden|onlycount] – caps going+guests across the
        whole event (main group + every share); once full, new Going
        clicks join the Waitlist instead. Third word is optional (default
        hidden) and controls Waitlist visibility in the POST itself:
          visible   – shown under Not Going. In the main hub's own post,
                      shows EVERY waiting person across every chat the
                      event was shared to (labeled "from <chat>" for
                      cross-chat entries); in a child chat's own post,
                      shows only that chat's own local entries.
          hidden    – nothing shown in any post; people still queue
                      normally, viewable only via /waitlist.
          onlycount – shows just the total count across every chat
                      combined, no names, in every post.
        /waitlist always returns everyone regardless of this setting -
        it's admin-only, not gated by the post's own visibility.
        Gated behind the event_limit feature (PRO by default).
        Examples:
            -limit 20 visible
            -limit 25 onlycount
            -limit 30 hidden
            -limit 40             (hidden by default)

    Returns: (event_name, going_icon, notgoing_icon, event_date_raw, total_limit_raw, reserve_raw)
    event_date_raw/total_limit_raw/reserve_raw are the raw token(s); validation happens in the caller.
    """
    going_icon    = None
    notgoing_icon = None
    event_date    = None
    total_limit   = None
    reserve_mode  = None

    gi_flags   = {"-gi", "-goingicon"}
    ni_flags   = {"-ni", "-notgoingicon"}
    date_flags = {"-d", "-date"}
    limit_flags = {"-limit"}

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

        elif token in limit_flags and i + 1 < len(tokens):
            total_limit = tokens[i + 1]  # validated by the caller
            i += 2
            # Optional visible|hidden right after the amount - exact match
            # only, so an event whose name happens to start with that word
            # isn't accidentally swallowed (same lookahead technique as
            # -date's optional HH:MM suffix above).
            if i < len(tokens) and tokens[i].strip().lower() in ("visible", "hidden", "onlycount"):
                reserve_mode = tokens[i].strip().lower()
                i += 1

        else:
            clean_tokens.append(token)
            i += 1

    event_name = " ".join(clean_tokens) if clean_tokens else None
    return event_name, going_icon, notgoing_icon, event_date, total_limit, reserve_mode


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
        -limit <N> [visible|hidden]   (gated behind event_limit, PRO by default)
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

    event_name_raw, g_icon, n_icon, date_raw, limit_raw, reserve_raw = parse_event_args(args)
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

    # -limit is a gated feature (event_limit) - validated BEFORE anything
    # else, so an ungated/invalid flag gets a clear message instead of
    # silently being ignored.
    total_limit_value = None
    waitlist_visibility_value = "hidden"
    if limit_raw is not None:
        total_limit_value = await _validate_limit_flag(message, chat_id, limit_raw)
        if total_limit_value is None:
            return
        if reserve_raw is not None:
            waitlist_visibility_value = reserve_raw

    event_id = str(uuid4())[:8]

    verification_enabled = has_feature(chat_id, "verification")
    add_extra_member_enabled = has_feature(chat_id, "add_extra_member")
    feature_snapshot = json.dumps({
        "verification": verification_enabled,
        "add_extra_member": add_extra_member_enabled,
    })

    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO events
                    (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
                     event_status, going_data, notgoing_data, counters_data, event_date, feature_snapshot, total_limit, waitlist_visibility, created_by_user_id)
                VALUES (?, ?, ?, ?, ?, ?, 0, '[]', '[]', '{}', ?, ?, ?, ?, ?)
                """,
                (event_id, chat_id, str(message.message_id),
                 event_name_raw, going_icon, notgoing_icon, event_date, feature_snapshot, total_limit_value, waitlist_visibility_value,
                 str(update.effective_user.id)),
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
    keyboard = create_event_keyboard(
        event_id, 0, going_icon, notgoing_icon, [], {},
        verification_enabled=verification_enabled,
        add_extra_member_enabled=add_extra_member_enabled,
    )

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
            SELECT event_id, name, going_icon, notgoing_icon, event_date, total_limit, waitlist_visibility
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

        event_id, current_name, current_gi, current_ni, current_date, current_limit, current_waitlist_visibility = row
        new_name, new_gi, new_ni, date_raw, limit_raw, reserve_raw = parse_event_args(args)

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

        # -limit is gated behind event_limit, same as newevent. Only if
        # -limit was explicitly supplied.
        updated_limit = current_limit
        updated_waitlist_visibility = current_waitlist_visibility
        promotions_to_announce = []  # [(chat_id, username, user_id), ...] - sent after commit
        if limit_raw is not None:
            validated = await _validate_limit_flag(update.message, chat_id, limit_raw)
            if validated is None:
                return

            # Reject lowering the limit below the event's current combined
            # headcount (main group + every share) - the old limit is kept
            # unchanged rather than silently accepting an inconsistent state.
            cursor.execute("SELECT going_data, counters_data FROM events WHERE event_id = ?", (event_id,))
            going_data_raw, counters_data_raw = cursor.fetchone()
            main_headcount = len(json.loads(going_data_raw)) + sum(json.loads(counters_data_raw).values())
            cursor.execute(
                "SELECT COALESCE(SUM(1 + guests), 0) FROM event_users WHERE event_id = ? AND status = 'going'",
                (event_id,),
            )
            child_headcount = cursor.fetchone()[0]
            current_headcount = main_headcount + child_headcount

            if validated < current_headcount:
                await update.message.reply_text(
                    f"{ICON_WARNING} `\\-limit {validated}` is below the current headcount \\({current_headcount}\\) "
                    f"across the main group and every share combined \\- the limit was left unchanged at {current_limit}\\.",
                    parse_mode="MarkdownV2",
                )
                return

            updated_limit = validated
            if reserve_raw is not None:
                updated_waitlist_visibility = reserve_raw

            # Limit was raised: promote FIFO (oldest first, globally across
            # every chat's waitlist entries) until either the waitlist is
            # empty or headcount reaches the new limit.
            free_slots = updated_limit - current_headcount
            if free_slots > 0:
                cursor.execute("SELECT going_data, counters_data, waitlist_data FROM events WHERE event_id = ?", (event_id,))
                going_raw, counters_raw, waitlist_raw = cursor.fetchone()
                going = json.loads(going_raw)
                counters = json.loads(counters_raw)
                waitlist = json.loads(waitlist_raw or "[]")
                waitlist.sort(key=lambda e: e.get("timestamp", ""))

                to_promote = waitlist[:free_slots]
                remaining_waitlist = waitlist[free_slots:]

                for entry in to_promote:
                    p_chat_id = entry["chat_id"]
                    p_username = entry["username"]
                    p_user_id = entry["user_id"]
                    if str(p_chat_id) == str(chat_id):
                        # Promote into the main hub's own going list
                        going.append(f"{p_username} ({p_user_id})")
                    else:
                        # Promote into a child chat's event_users
                        cursor.execute(
                            "INSERT OR REPLACE INTO event_users (event_id, chat_id, user_id, username, status, guests) VALUES (?, ?, ?, ?, 'going', 0)",
                            (event_id, p_chat_id, p_user_id, p_username),
                        )
                    promotions_to_announce.append((p_chat_id, p_username, p_user_id))

                if to_promote:
                    cursor.execute(
                        "UPDATE events SET going_data = ?, waitlist_data = ? WHERE event_id = ?",
                        (json.dumps(going), json.dumps(remaining_waitlist), event_id),
                    )

        cursor.execute(
            """
            UPDATE events
            SET name = ?, going_icon = ?, notgoing_icon = ?, event_date = ?, total_limit = ?, waitlist_visibility = ?
            WHERE event_id = ?
            """,
            (updated_name, updated_gi, updated_ni, updated_date, updated_limit, updated_waitlist_visibility, event_id),
        )
        conn.commit()

    for p_chat_id, p_username, p_user_id in promotions_to_announce:
        try:
            mention = _mention_link(p_chat_id, p_username, p_user_id)
            await context.bot.send_message(
                chat_id=int(p_chat_id),
                text=f"{ICON_STANDBY} A spot opened up \\- {mention} has been moved from the Waitlist to Going\\!",
                parse_mode="MarkdownV2",
            )
        except Exception as e:
            logger.error(f"editevent limit-raise promotion announcement failed for chat {p_chat_id}: {e}")

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
    usernames = parse_user_args(args[:-1])

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
        cursor.execute("SELECT username, status, user_id FROM main_group_users WHERE chat_id = ?", (chat_id,))
        rows = cursor.fetchall()

    if not rows:
        await update.message.reply_text(
            f"{ICON_STATS} No users tracked for this chat\\.", parse_mode="MarkdownV2"
        )
        return

    lines = [f"• {_mention_link(chat_id, r[0], r[2])} \\(`{escape_markdown(r[1])}`\\)" for r in rows]
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
    see /refreshusersall, which reads exactly these rows per monitor
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
                    "SELECT chat_id FROM sub_chats WHERE chat_name = ? AND is_monitored = 1 "
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
                    if "Participant_id_invalid" in str(e):
                        failed.append(
                            f"{identifier}: not a valid user\\_id for this chat \\(Telegram rejected it\\)\\. "
                            f"Ask them to DM the bot with /userid to get their correct numeric ID\\."
                        )
                    else:
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

    # ── 2b. Surface unresolvable "Add Extra Member" entries ─────────────────
    # These live only in the active event's going_data (added via Verification
    # Mode -> Add Extra Member, resolved against main_group_users at the time),
    # never in main_group_users itself if no user_id could be found for them.
    # They can NEVER be synced to the Users sheet (every row there is keyed
    # by a real numeric USER_ID) - surfaced here so the admin knows to
    # manually resolve them, e.g. by asking the person to message the bot
    # once so a real user_id gets captured, then re-adding them.
    try:
        with get_connection() as fresh_conn:
            fresh_cursor = fresh_conn.cursor()
            fresh_cursor.execute(
                "SELECT going_data FROM events WHERE chat_id = ? AND event_status IN (0, 1) ORDER BY ROWID DESC LIMIT 1",
                (chat_id,),
            )
            active_event_row = fresh_cursor.fetchone()
        if active_event_row:
            going_list = json.loads(active_event_row[0])
            unresolved = [g.split(" (")[0] for g in going_list if g.endswith("(no_id_in_main_group)")]
            if unresolved:
                mentions = ", ".join(f"@{escape_markdown(u)}" for u in unresolved)
                lines.append(
                    f"{ICON_WARNING} Added via Extra Member but never resolved to a real user\\_id "
                    f"\\(can't sync to Sheets without one\\): {mentions}\\. Ask them to message the bot once, then re\\-add\\."
                )
    except Exception as e:
        logger.error(f"refreshusers: unresolved-extra-member check failed: {e}")

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

    if not is_premium(chat_id):
        await update.message.reply_text(
            f"{ICON_WARNING} /refreshusersall is a PRO\\-only feature \\(it syncs monitored groups/channels, "
            f"which are only ever configured via /addmonitor, itself PRO\\-only\\)\\. "
            f"Use /setsub info or contact the bot owner to upgrade\\.",
            parse_mode="MarkdownV2",
        )
        return

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT chat_id, chat_type, chat_name FROM sub_chats WHERE is_monitored = 1 AND (owner_chat_id = ? OR owner_chat_id IS NULL)",
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
            SELECT event_id, name, event_status, going_icon, notgoing_icon, total_limit
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

        event_id, name, event_status, going_icon, notgoing_icon, total_limit = event_row

        if total_limit is not None:
            cursor.execute("SELECT going_data, counters_data FROM events WHERE event_id = ?", (event_id,))
            going_data_raw, counters_data_raw = cursor.fetchone()
            main_headcount = len(json.loads(going_data_raw)) + sum(json.loads(counters_data_raw).values())
            cursor.execute(
                "SELECT COALESCE(SUM(1 + guests), 0) FROM event_users WHERE event_id = ? AND status = 'going'",
                (event_id,),
            )
            child_headcount = cursor.fetchone()[0]
            if main_headcount + child_headcount >= total_limit:
                await context.bot.send_message(
                    chat_id=main_hub_chat_id,
                    text=f"{ICON_WARNING} This event is already at its `\\-limit` capacity \\({total_limit}\\) "
                         f"across the main group and every share combined \\- sharing to a new "
                         f"group/channel is blocked while it's full\\.",
                    parse_mode="MarkdownV2",
                )
                return

        cursor.execute(
            "SELECT chat_id FROM sub_chats WHERE alias = ? AND (owner_chat_id = ? OR owner_chat_id IS NULL)",
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

        share_limit = get_feature_limit_for_chat(main_hub_chat_id, "shareevent")
        if share_limit is not None:
            cursor.execute(
                """
                SELECT COUNT(*) FROM event_shares es
                JOIN events e ON es.event_id = e.event_id
                WHERE e.chat_id = ? AND es.chat_id = ?
                """,
                (str(main_hub_chat_id), str(target_chat_raw)),
            )
            (share_count,) = cursor.fetchone()
            if share_count >= share_limit:
                await context.bot.send_message(
                    chat_id=main_hub_chat_id,
                    text=f"You've reached the /shareevent limit for this target ({share_limit}). "
                         f"Contact the bot owner to raise or remove it.",
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
        target_display_name = target_input if alias_row else (target_chat_obj.title or str(target_chat_api))
        await context.bot.send_message(
            chat_id=main_hub_chat_id,
            text=f"🚀 Event shared successfully to {escape_markdown(target_display_name)}\\.",
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
# Waitlist
# ---------------------------------------------------------------------------

async def waitlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Shows the waitlist for the most recently created event in whichever
    hub this command's own chat belongs to. Scoped by WHERE it's called
    from, same rule as the Waitlist section rendered inside a post:
      - Main hub: every entry, across every chat - each one labeled
        "from <chat_name>" unless it was added in the hub itself.
      - Child group/channel: only that chat's own local entries, never
        another chat's waitlist (even though the underlying data is one
        event-wide list, this command never leaks across chats).
    Not owner-only or admin-only - matches /status's own visibility (any
    member can check it), since seeing your position in line isn't a
    privileged action the way changing tiers/subscriptions is.
    """
    calling_chat_id = str(update.effective_chat.id)

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT owner_chat_id FROM sub_chats WHERE chat_id = ?",
            (calling_chat_id,),
        )
        sub_row = cursor.fetchone()
        is_child_caller = sub_row is not None and sub_row[0] is not None
        hub_chat_id = sub_row[0] if is_child_caller else calling_chat_id

        cursor.execute(
            "SELECT waitlist_data FROM events WHERE chat_id = ? ORDER BY ROWID DESC LIMIT 1",
            (hub_chat_id,),
        )
        event_row = cursor.fetchone()

    if not event_row:
        await update.message.reply_text("❌ No event found for this group\\.", parse_mode="MarkdownV2")
        return

    waitlist = json.loads(event_row[0] or "[]")
    if is_child_caller:
        waitlist = [e for e in waitlist if str(e.get("chat_id")) == calling_chat_id]

    if not waitlist:
        await update.message.reply_text("The waitlist is currently empty\\.", parse_mode="MarkdownV2")
        return

    chat_title_cache = {}

    async def _title(cid):
        if cid not in chat_title_cache:
            try:
                obj = await context.bot.get_chat(int(cid) if str(cid).replace("-", "").isdigit() else cid)
                chat_title_cache[cid] = obj.title or "Group"
            except Exception:
                chat_title_cache[cid] = "Group"
        return chat_title_cache[cid]

    lines = []
    for entry in waitlist:
        mention = _mention_link(entry["chat_id"], entry["username"], entry["user_id"])
        if not is_child_caller and str(entry["chat_id"]) != str(hub_chat_id):
            chat_title = await _title(entry["chat_id"])
            lines.append(f"{mention} from {escape_markdown(chat_title)}")
        else:
            lines.append(mention)

    text = f"*Waitlist* \\({len(waitlist)}\\):\n" + "\n".join(lines)
    await update.message.reply_text(text, parse_mode="MarkdownV2")


# ---------------------------------------------------------------------------
# Global text router
# ---------------------------------------------------------------------------

async def global_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Routes all text messages: extra-player input first, then @everyone tracking."""
    if context.user_data.get("awaiting_extra_player_for"):
        await handle_extra_player_input(update, context)
        return
    await track_everyone_message(update, context)
