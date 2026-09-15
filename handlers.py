import json
import re
from uuid import uuid4
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram.error import BadRequest

from keyboard import create_event_keyboard
from subscription import is_premium, has_feature, require_premium
from aliases import setalias, removealias, listalias
from monitors import addmonitor, removemonitor, listmonitors
from help_system import (
    userid, chatid, help_command, help_callback_handler, help_back_handler,
    upgrade_info_callback_handler,
)
from event_engine import (
    get_event_lock, schedule_view_refresh, update_all_shared_views, button_handler, _mention_link,
    _render_waitlist_local, _render_waitlist_all, _promotion_announcement_text,
)

from config import (
    DEFAULT_GOING_ICON, DEFAULT_NOTGOING_ICON, logger,
    ICON_SHARED, ICON_STATS, ICON_WARNING,
    ICON_CLOCK, ICON_NOTIFY, ICON_CLEAN, ICON_ADMIN_ONLY, ICON_GLOBE, ICON_STANDBY,
)
from utils import escape_markdown, now2ddmmyy, parse_event_date, is_real_admin, GROUP_ANONYMOUS_BOT_ID
from db import track_user, get_connection, get_feature_limit_for_chat, dedupe_waitlist, ensure_event_migrated
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


async def _validate_waitlist_visibility_flag(message, chat_id: str, waitlist_viz_raw: str):
    """
    Validates -wl/-waitlist's gate (same event_limit feature as -limit,
    since it's the same underlying waitlist mechanic - can now be set
    independently of -limit in the same command). Shared by newevent and
    editevent, which had identical validation logic duplicated inline
    before this extraction.

    Returns the raw value unchanged on success (already validated to be
    one of visible/hidden/onlycount by parse_event_args' own lookahead
    matching, so no further parsing needed here). Returns None if a
    reply was already sent to the user (the caller should `return`
    immediately in that case, without proceeding any further).
    """
    if not has_feature(chat_id, "event_limit"):
        await message.reply_text(
            f"{ICON_WARNING} `\\-wl`/`\\-waitlist` requires a higher tier\\. Contact the bot owner to upgrade\\.",
            parse_mode="MarkdownV2",
        )
        return None
    return waitlist_viz_raw


async def _validate_clickability_flag(message, chat_id: str, clickability_raw: str):
    """
    Validates -clc/-clickability's gate (the "clickability" feature).
    Shared by newevent and editevent, which had identical validation
    logic duplicated inline before this extraction (introduced when
    -clc was made a gated feature, item 3).

    Returns the raw value unchanged on success. Returns None if a reply
    was already sent to the user (the caller should `return` immediately
    in that case).
    """
    if not has_feature(chat_id, "clickability"):
        await message.reply_text(
            f"{ICON_WARNING} `\\-clc`/`\\-clickability` requires a higher tier\\. Contact the bot owner to upgrade\\.",
            parse_mode="MarkdownV2",
        )
        return None
    return clickability_raw


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
    -limit <N> – caps going+guests across the whole event (main group +
        every share); once full, new Going clicks join the Waitlist
        instead. Gated behind the event_limit feature (PRO by default).
        Visibility of the Waitlist itself is now a SEPARATE flag, not a
        trailing word after the number:

    -wl / -waitlist visible|hidden|onlycount – controls Waitlist
        visibility in the POST itself:
          visible   – shown under Not Going. In the main hub's own post,
                      shows EVERY waiting person across every chat the
                      event was shared to (labeled "from <chat>" for
                      cross-chat entries); in a child chat's own post,
                      shows only that chat's own local entries.
          hidden    – nothing shown in any post (default); people still
                      queue normally, viewable only via /waitlist.
          onlycount – shows just the total count across every chat
                      combined, no names, in every post.
        /waitlist always returns everyone regardless of this setting -
        it's admin-only, not gated by the post's own visibility.
        Examples:
            -limit 20 -wl visible
            -limit 25 -waitlist onlycount
            -limit 30              (waitlist stays hidden by default)

    -ngl / -notgoinglist visible|hidden|onlycount – controls Not Going
        list visibility in the post, same visible/hidden/onlycount
        vocabulary as -waitlist:
          visible   – shown as before (default - matches every event's
                      behavior prior to this flag existing).
          hidden    – the Not Going section doesn't appear in the post
                      at all.
          onlycount – shows just the total count, no names.
        Examples:
            -ngl hidden
            -notgoinglist onlycount

    -clc / -clickability on|off – whether names in the post are
        clickable mentions (tg://user?id=...) or plain, non-linked text:
          on  (default) – every name in the post is a clickable mention.
          off – every name in the post is plain text, not clickable.
        Examples:
            -clc off
            -clickability on

    Returns: (event_name, going_icon, notgoing_icon, event_date_raw, total_limit_raw,
              waitlist_visibility_raw, notgoing_visibility_raw, clickability_raw)
    event_date_raw/total_limit_raw are the raw token(s); validation happens in the caller.
    waitlist_visibility_raw/notgoing_visibility_raw/clickability_raw are None if the flag wasn't given.
    """
    going_icon    = None
    notgoing_icon = None
    event_date    = None
    total_limit   = None
    waitlist_visibility_raw = None
    notgoing_visibility_raw = None
    clickability_raw = None

    gi_flags   = {"-gi", "-goingicon"}
    ni_flags   = {"-ni", "-notgoingicon"}
    date_flags = {"-d", "-date"}
    limit_flags = {"-limit"}
    waitlist_flags = {"-wl", "-waitlist"}
    notgoinglist_flags = {"-ngl", "-notgoinglist"}
    clickability_flags = {"-clc", "-clickability"}
    visibility_words = ("visible", "hidden", "onlycount")
    clickability_words = ("on", "off")

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

        elif token in waitlist_flags and i + 1 < len(tokens) and tokens[i + 1].strip().lower() in visibility_words:
            waitlist_visibility_raw = tokens[i + 1].strip().lower()
            i += 2

        elif token in notgoinglist_flags and i + 1 < len(tokens) and tokens[i + 1].strip().lower() in visibility_words:
            notgoing_visibility_raw = tokens[i + 1].strip().lower()
            i += 2

        elif token in clickability_flags and i + 1 < len(tokens) and tokens[i + 1].strip().lower() in clickability_words:
            clickability_raw = tokens[i + 1].strip().lower()
            i += 2

        else:
            clean_tokens.append(token)
            i += 1

    event_name = " ".join(clean_tokens) if clean_tokens else None
    return event_name, going_icon, notgoing_icon, event_date, total_limit, waitlist_visibility_raw, notgoing_visibility_raw, clickability_raw


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

    event_name_raw, g_icon, n_icon, date_raw, limit_raw, waitlist_viz_raw, notgoing_viz_raw, clickability_raw = parse_event_args(args)
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
    if limit_raw is not None:
        total_limit_value = await _validate_limit_flag(message, chat_id, limit_raw)
        if total_limit_value is None:
            return

    # -w/-waitlist is now independent of -limit being given in the SAME
    # command (previously it was a trailing word right after -limit's
    # number) - still gated on the same event_limit feature, since it's
    # the same underlying waitlist mechanic, just usable on its own
    # (e.g. set now, raise the actual -limit later via /editevent).
    waitlist_visibility_value = "hidden"
    if waitlist_viz_raw is not None:
        validated_viz = await _validate_waitlist_visibility_flag(message, chat_id, waitlist_viz_raw)
        if validated_viz is None:
            return
        waitlist_visibility_value = validated_viz

    # -ngl/-notgoinglist is ungated - the Not Going list has always been
    # visible to everyone with no tier restriction, this flag just adds
    # the ability to hide/summarize it, available at every tier.
    notgoing_visibility_value = notgoing_viz_raw if notgoing_viz_raw is not None else "visible"

    # -clc/-clickability is now a gated feature (item 3) - was ungated
    # before, matching -ngl's own reasoning, but is now tier-restricted
    # like -limit/-wl.
    clickability_value = "on"
    if clickability_raw is not None:
        validated_clc = await _validate_clickability_flag(message, chat_id, clickability_raw)
        if validated_clc is None:
            return
        clickability_value = validated_clc

    event_id = str(uuid4())[:8]

    verification_enabled = has_feature(chat_id, "verification")
    add_extra_member_enabled = has_feature(chat_id, "add_extra_member")
    feature_snapshot = json.dumps({
        "verification": verification_enabled,
        "add_extra_member": add_extra_member_enabled,
    })

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT message_id, name FROM events WHERE chat_id = ? AND event_status IN (0, 1) ORDER BY ROWID DESC LIMIT 1",
            (chat_id,),
        )
        existing_active = cursor.fetchone()

    try:
        created_date = now2ddmmyy()
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO events
                    (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
                     event_status, going_data, notgoing_data, counters_data, event_date, feature_snapshot, total_limit, waitlist_visibility, notgoing_visibility, clickability, created_by_user_id, created_date)
                VALUES (?, ?, ?, ?, ?, ?, 0, '[]', '[]', '{}', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (event_id, chat_id, str(message.message_id),
                 event_name_raw, going_icon, notgoing_icon, event_date, feature_snapshot, total_limit_value, waitlist_visibility_value, notgoing_visibility_value, clickability_value,
                 str(update.effective_user.id), created_date),
            )
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to save new event: {e}")
        await message.reply_text("❌ Database error: could not create event\\.", parse_mode="MarkdownV2")
        return

    if existing_active:
        await message.reply_text(
            f"{ICON_WARNING} There's already an active event \\(`{escape_markdown(existing_active[1])}`\\) in this chat\\. "
            f"Its post is still clickable for participants, but commands like /waitlist and /editevent now target "
            f"this NEW event instead\\. Consider closing the old one first next time\\.",
            parse_mode="MarkdownV2",
        )

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
                    event_id, event_name_raw, created_date, str(update.effective_user.id), event_date or "", "", "OPEN", 0,
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
            SELECT event_id, name, going_icon, notgoing_icon, event_date, total_limit, waitlist_visibility, notgoing_visibility, clickability
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

        event_id, current_name, current_gi, current_ni, current_date, current_limit, current_waitlist_visibility, current_notgoing_visibility, current_clickability = row
        new_name, new_gi, new_ni, date_raw, limit_raw, waitlist_viz_raw, notgoing_viz_raw, clickability_raw = parse_event_args(args)

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
        updated_notgoing_visibility = current_notgoing_visibility
        updated_clickability = current_clickability
        promotions_to_announce = []  # [(chat_id, username, user_id, is_guest), ...] - sent after commit

        # -w/-waitlist can now be set independently of -limit in the SAME
        # command (previously it was only readable as a trailing word
        # right after -limit's number) - still requires event_limit,
        # since it's the same underlying waitlist mechanic.
        if waitlist_viz_raw is not None:
            validated_viz = await _validate_waitlist_visibility_flag(update.message, chat_id, waitlist_viz_raw)
            if validated_viz is None:
                return
            updated_waitlist_visibility = validated_viz

        # -ngl/-notgoinglist is ungated, same as newevent.
        if notgoing_viz_raw is not None:
            updated_notgoing_visibility = notgoing_viz_raw

        # -clc/-clickability is now a gated feature (item 3).
        if clickability_raw is not None:
            validated_clc = await _validate_clickability_flag(update.message, chat_id, clickability_raw)
            if validated_clc is None:
                return
            updated_clickability = validated_clc

        if limit_raw is not None:
            validated = await _validate_limit_flag(update.message, chat_id, limit_raw)
            if validated is None:
                return

            # Ensure this event's state is migrated to event_users before
            # computing headcount below - an event nobody has clicked on
            # since Variant B was deployed wouldn't have any event_users
            # rows yet otherwise, undercounting its real headcount.
            ensure_event_migrated(cursor, event_id, chat_id)

            # Reject lowering the limit below the event's current combined
            # headcount (main group + every share) - the old limit is kept
            # unchanged rather than silently accepting an inconsistent state.
            # Single unified query across the whole event_id - Variant B
            # means the master hub's own participants live in event_users
            # too (chat_id=main_chat_id), so this one query already covers
            # everyone with no separate going_data/counters_data reads.
            cursor.execute(
                "SELECT COALESCE(SUM(1 + guests), 0) FROM event_users WHERE event_id = ? AND status = 'going'",
                (event_id,),
            )
            current_headcount = cursor.fetchone()[0]

            if validated < current_headcount:
                await update.message.reply_text(
                    f"{ICON_WARNING} `\\-limit {validated}` is below the current headcount \\({current_headcount}\\) "
                    f"across the main group and every share combined \\- the limit was left unchanged at {current_limit}\\.",
                    parse_mode="MarkdownV2",
                )
                return

            updated_limit = validated

            # Limit was raised: promote FIFO (oldest first, globally across
            # every chat's waitlist entries) until either the waitlist is
            # empty or headcount reaches the new limit.
            free_slots = updated_limit - current_headcount
            if free_slots > 0:
                cursor.execute("SELECT waitlist_data FROM events WHERE event_id = ?", (event_id,))
                (waitlist_raw,) = cursor.fetchone()
                waitlist = json.loads(waitlist_raw or "[]")
                waitlist.sort(key=lambda e: e.get("timestamp", ""))

                slots_left = free_slots
                remaining_waitlist = list(waitlist)
                for entry in waitlist:
                    if slots_left <= 0:
                        break
                    p_chat_id = entry["chat_id"]
                    p_username = entry["username"]
                    p_user_id = entry["user_id"]

                    if entry.get("is_guest"):
                        cursor.execute(
                            "SELECT status, guests FROM event_users WHERE event_id = ? AND chat_id = ? AND user_id = ?",
                            (event_id, p_chat_id, p_user_id),
                        )
                        owner_row = cursor.fetchone()
                        still_going = bool(owner_row and owner_row[0] == "going")
                        if still_going:
                            cursor.execute(
                                "UPDATE event_users SET guests = ? WHERE event_id = ? AND chat_id = ? AND user_id = ?",
                                (owner_row[1] + 1, event_id, p_chat_id, p_user_id),
                            )
                        else:
                            # Owner is no longer going - discard this stale
                            # guest-slot entry WITHOUT spending a slot from
                            # the budget, and move on to the next candidate.
                            remaining_waitlist.remove(entry)
                            continue
                    else:
                        cursor.execute(
                            "INSERT OR REPLACE INTO event_users (event_id, chat_id, user_id, username, first_name, last_name, status, guests) VALUES (?, ?, ?, ?, ?, ?, 'going', 0)",
                            (event_id, p_chat_id, p_user_id, p_username, entry.get("first_name"), entry.get("last_name")),
                        )

                    remaining_waitlist.remove(entry)
                    promotions_to_announce.append((p_chat_id, p_username, p_user_id, entry.get("is_guest", False)))
                    slots_left -= 1

                cursor.execute(
                    "UPDATE events SET waitlist_data = ? WHERE event_id = ?",
                    (json.dumps(remaining_waitlist), event_id),
                )

        cursor.execute(
            """
            UPDATE events
            SET name = ?, going_icon = ?, notgoing_icon = ?, event_date = ?, total_limit = ?, waitlist_visibility = ?, notgoing_visibility = ?, clickability = ?
            WHERE event_id = ?
            """,
            (updated_name, updated_gi, updated_ni, updated_date, updated_limit, updated_waitlist_visibility, updated_notgoing_visibility, updated_clickability, event_id),
        )
        conn.commit()

    for p_chat_id, p_username, p_user_id, p_is_guest in promotions_to_announce:
        try:
            await context.bot.send_message(
                chat_id=int(p_chat_id),
                text=_promotion_announcement_text(p_chat_id, p_username, p_user_id, p_is_guest),
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
            SELECT event_id, name
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

        event_id, event_name = event_row
        ensure_event_migrated(cursor, event_id, chat_id)
        conn.commit()

        cursor.execute(
            "SELECT username FROM event_users WHERE event_id = ? AND chat_id = ? AND status IN ('going', 'notgoing')",
            (event_id, chat_id),
        )
        decided_users = {r[0] for r in cursor.fetchall()}

        cursor.execute(
            "SELECT username, user_id FROM main_group_users WHERE chat_id = ? AND status = 'active'", (chat_id,)
        )
        all_active = cursor.fetchall()

    if not all_active:
        await message.reply_text(
            f"{ICON_STATS} No active users tracked in this chat\\.", parse_mode="MarkdownV2"
        )
        return

    pending = []
    for uname, uid in all_active:
        if uname and uname not in decided_users:
            pending.append(f"• {_mention_link(chat_id, uname, uid)}")

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
    await context.bot.send_message(chat_id=chat_id, text=header + users_list, parse_mode="MarkdownV2")

    # If called from a DM, the ping above went to the group, not here -
    # give the caller SOME confirmation in their own DM, otherwise
    # they'd see nothing happen at all from their end.
    if update.effective_chat.type == "private":
        await message.reply_text(
            f"✅ Pinged {len(pending)} pending user\\(s\\) in the group\\.", parse_mode="MarkdownV2"
        )


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
    unresolved = []
    with get_connection() as conn:
        cursor = conn.cursor()
        admins_cache = None
        for username in usernames:
            cursor.execute(
                "SELECT user_id FROM main_group_users WHERE chat_id = ? AND username = ?",
                (chat_id, username),
            )
            existing = cursor.fetchone()
            already_has_id = existing and existing[0] and str(existing[0]).lstrip("-").isdigit()

            if already_has_id:
                track_user(chat_id, username, status, user_id=existing[0])
                updated.append(username)
            else:
                # First time this exact username is being tracked (or it
                # was tracked before without ever resolving a real
                # user_id) - try to resolve one now via the admin list,
                # same pattern /adduser uses, so /listusers can show a
                # clickable mention instead of permanently falling back
                # to plain text.
                if admins_cache is None:
                    try:
                        admins_cache = await context.bot.get_chat_administrators(chat_id)
                    except Exception:
                        admins_cache = []
                target_username = username.lstrip("@")
                match = next(
                    (a.user for a in admins_cache if a.user.username and a.user.username.lower() == target_username.lower()),
                    None,
                )
                if match:
                    track_user(
                        chat_id, username, status, user_id=str(match.id),
                        first_name=match.first_name, last_name=match.last_name,
                    )
                    updated.append(username)
                else:
                    # No resolvable user_id at all - nothing is persisted
                    # (track_user is a no-op without one), so this person
                    # is deliberately NOT added to `updated` below, or the
                    # success message would falsely claim their status was
                    # changed when nothing was actually written.
                    unresolved.append(username)

    if not updated and unresolved:
        mentions = ", ".join(f"@{escape_markdown(u)}" for u in unresolved)
        await update.message.reply_text(
            f"{ICON_WARNING} Could not resolve a user\\_id for: {mentions} \\- nothing was updated\\. "
            f"They need to be an admin of this chat, or have already interacted with the bot at least once\\.",
            parse_mode="MarkdownV2",
        )
        return

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
    if updated and unresolved:
        mentions = ", ".join(f"@{escape_markdown(u)}" for u in unresolved)
        await update.message.reply_text(
            f"{ICON_WARNING} Could not resolve a user\\_id for: {mentions} \\- their status was NOT changed\\.",
            parse_mode="MarkdownV2",
        )

    if unresolved:
        unresolved_list = ", ".join(f"@{escape_markdown(u)}" for u in unresolved)
        await update.message.reply_text(
            f"⚠️ Could not resolve a real user\\_id for {unresolved_list} \\(not an admin here, and hasn't "
            f"interacted with the bot yet\\) \\- they'll show as plain text \\(not clickable\\) in /listusers "
            f"until they do\\.",
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
            # Strip a trailing comma - "/adduser 123, 456" (comma glued to
            # the first number, no space before it) would otherwise leave
            # "123," as the literal identifier, which fails the numeric
            # check entirely (a comma isn't a digit) and gets silently
            # misclassified as a username instead of the number it is.
            user_identifiers.append(args[i].rstrip(","))
            i += 1

    added = []
    already_tracked = []
    failed = []

    for identifier in user_identifiers:
        try:
            # Check if identifier is a numeric user_id
            if identifier.lstrip("-").isdigit():
                if identifier.startswith("-"):
                    # Telegram user ids are NEVER negative - only chat ids
                    # (groups/channels) are. A negative "user_id" here is
                    # almost always a copy-paste mix-up with a chat_id, and
                    # silently passing it to Telegram's API just produces a
                    # confusing generic rejection instead of this specific,
                    # actionable explanation.
                    failed.append(
                        f"{identifier}: Telegram user IDs are never negative "
                        f"\\(only group/channel chat IDs are\\) \\- did you mean `{identifier.lstrip('-')}`\\?"
                    )
                    continue
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
                        with get_connection() as conn:
                            already = conn.execute(
                                "SELECT 1 FROM main_group_users WHERE chat_id = ? AND user_id = ?",
                                (target_chat_id, target_user_id),
                            ).fetchone() is not None
                        track_user(
                            target_chat_id, username, "active", user_id=target_user_id,
                            first_name=member.user.first_name, last_name=member.user.last_name,
                        )
                        if already:
                            already_tracked.append(f"@{escape_markdown(username)} \\({target_user_id}\\)")
                        else:
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
                        with get_connection() as conn:
                            already = conn.execute(
                                "SELECT 1 FROM main_group_users WHERE chat_id = ? AND user_id = ?",
                                (target_chat_id, resolved_user_id),
                            ).fetchone() is not None
                        track_user(
                            target_chat_id, resolved_username, "active", user_id=resolved_user_id,
                            first_name=match.first_name, last_name=match.last_name,
                        )
                        if already:
                            already_tracked.append(f"@{escape_markdown(resolved_username)} \\({resolved_user_id}\\)")
                        else:
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
    if already_tracked:
        lines.append(f"ℹ️ Already in group: {', '.join(already_tracked)}")
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

        # Fetched once up front - reused both for resolving unresolved
        # entries below (giving a stale username-only row a real chance
        # at healing instead of unconditional removal) and for "add
        # missing admins" further down, avoiding a duplicate API call.
        try:
            admins = await context.bot.get_chat_administrators(chat_id)
        except Exception as e:
            logger.error(f"refreshusers: could not fetch admin list: {e}")
            admins = []

        # ── 1. Remove confirmed-departed/invalid/unverifiable users ──────────
        removed        = []  # user_ids confirmed departed - NOT usernames, see DELETE below
        still_present  = []  # (user_id, LIVE username straight from Telegram) - verified currently in the chat
        current_admin_ids = {str(a.user.id) for a in admins}

        for username, user_id, status in rows:
            try:
                m = await context.bot.get_chat_member(
                    chat_id=int(chat_id), user_id=int(user_id)
                )
                if m.status in ["left", "kicked"] and str(user_id) not in current_admin_ids:
                    removed.append(user_id)
                elif m.status in ["left", "kicked"]:
                    # get_chat_member disagrees with the admin list we just
                    # fetched - this happens for anonymous admins (Remain
                    # Anonymous), where get_chat_member can incorrectly
                    # report "left" even though they're still a genuine,
                    # current admin. get_chat_administrators is the more
                    # reliable source here, so it wins - keep them.
                    still_present.append((user_id, username, None, None))
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
            # Deletes by user_id, NOT username - a username can be reused by a
            # completely different, still-present person within the same
            # refresh cycle (they just haven't been tracked under it yet), so
            # deleting by the string alone could hit the wrong row entirely.
            cursor.executemany(
                "DELETE FROM main_group_users WHERE chat_id = ? AND user_id = ?",
                [(chat_id, uid) for uid in removed],
            )
            conn.commit()

        # ── 2. Add missing chat administrators as 'active' ──────────────────────
        added = []
        try:
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
            logger.error(f"refreshusers: error while processing admin list: {e}")

    # Dedupe still_present by user_id (an admin who was already tracked
    # would otherwise appear twice - once from step 1, once from step 2).
    # Also filter out entries without valid user_id for Google Sheets sync
    still_present = list({
        str(uid): (uid, uname, first_name, last_name)
        for uid, uname, first_name, last_name in still_present if uid and uid != uname
    }.values())

    lines = []
    if removed:
        username_by_id = {uid: uname for uname, uid, _status in rows}
        mentions = ", ".join(f"@{escape_markdown(username_by_id.get(uid, uid))}" for uid in removed)
        lines.append(f"{ICON_CLEAN} Removed \\(left, invalid, or unverifiable\\): {mentions}")
    if added:
        mentions = ", ".join(f"@{escape_markdown(u)}" for u in added)
        lines.append(f"➕ Added \\(new admins found\\): {mentions}")
    if not lines:
        lines.append("✅ Nothing to change \\- list already matches the group\\.")

    # ── 2b. Surface unresolvable "Add Extra Member" entries ─────────────────
    # Someone added via Verification Mode -> Add Extra Member with no
    # resolvable real Telegram user_id gets their username used as a
    # fallback identifying key instead (see handle_extra_player_input) -
    # they can NEVER be synced to the Users sheet (every row there is keyed
    # by a real numeric USER_ID) - surfaced here so the admin knows to
    # manually resolve them, e.g. by asking the person to message the bot
    # once so a real user_id gets captured, then re-adding them.
    try:
        with get_connection() as fresh_conn:
            fresh_cursor = fresh_conn.cursor()
            fresh_cursor.execute(
                "SELECT event_id FROM events WHERE chat_id = ? AND event_status IN (0, 1) ORDER BY ROWID DESC LIMIT 1",
                (chat_id,),
            )
            active_event_row = fresh_cursor.fetchone()
            unresolved = []
            if active_event_row:
                ev_id = active_event_row[0]
                ensure_event_migrated(fresh_cursor, ev_id, chat_id)
                fresh_conn.commit()
                fresh_cursor.execute(
                    "SELECT username FROM event_users WHERE event_id = ? AND chat_id = ? AND status = 'going' AND user_id = username",
                    (ev_id, chat_id),
                )
                unresolved = [r[0] for r in fresh_cursor.fetchall()]
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
    Covers the group/channel this command was run in PLUS every monitored
    child under it (see /addmonitor) - the hub's own members aren't a
    separate step, this replaces needing a plain /refreshusers call for
    the hub itself. This is a heavier, potentially slow bulk operation -
    it makes live Telegram API calls for every tracked user AND every
    admin in EVERY chat it touches, one after another - so it's kept as
    its own explicit command rather than a flag on the lightweight,
    everyday /refreshusers.
    """
    chat_id = await resolve_hub_chat_id(update, context, "refreshusersall", override_chat_id)
    if chat_id is None:
        return

    if not await is_real_admin(context.bot, chat_id, update.effective_user, message=update.message):
        await update.message.reply_text(f"{ICON_ADMIN_ONLY} Only admins can use /refreshusersall\\.", parse_mode="MarkdownV2")
        return

    if not await require_premium(update, "Monitoring sync (/refreshusersall, tied to /addmonitor)", chat_id=chat_id):
        return

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT chat_id, chat_type, chat_name FROM sub_chats WHERE is_monitored = 1 AND (owner_chat_id = ? OR owner_chat_id IS NULL)",
            (chat_id,),
        )
        monitors = cursor.fetchall()

    # The hub itself is always included too - refreshusersall covers the
    # group the command was run in PLUS every monitored child under it,
    # not just the children. Resolved via a live API call so this works
    # the same whether called directly from the hub or via a DM override.
    # The middle tuple field (chat_type) is never actually read anywhere
    # in the loop below - it's only there so this entry has the same
    # shape as the sub_chats query result above it gets prepended to.
    try:
        hub_chat_obj = await context.bot.get_chat(int(chat_id) if str(chat_id).lstrip("-").isdigit() else chat_id)
        hub_chat_name = hub_chat_obj.title or chat_id
    except Exception:
        hub_chat_name = chat_id
    monitors = [(chat_id, None, hub_chat_name)] + list(monitors)

    total_chats = len(monitors)
    progress_msg = None
    try:
        progress_msg = await update.message.reply_text(
            f"{ICON_GLOBE} Refreshing 0/{total_chats} chats\\.\\.\\.", parse_mode="MarkdownV2"
        )
    except Exception as e:
        logger.error(f"refreshusersall: could not send progress message: {e}")

    lines = [f"{ICON_GLOBE} *Processing monitored groups/channels:*"]
    for processed_count, (monitor_chat_id, chat_type, chat_name) in enumerate(monitors, start=1):
        try:
            with get_connection() as conn_mon:
                cursor_mon = conn_mon.cursor()

                # Fetched once up front - reused both for cross-checking a
                # "left"/"kicked" get_chat_member result below (Telegram's
                # own get_chat_member can incorrectly report an anonymous
                # admin as departed - the admin list is the more reliable
                # source for them) and for "add missing admins" further
                # down, avoiding a duplicate API call.
                try:
                    monitor_admins = await context.bot.get_chat_administrators(int(monitor_chat_id))
                except Exception as e:
                    logger.error(f"refreshusersall: could not fetch admins for {chat_name}: {e}")
                    monitor_admins = []
                current_monitor_admin_ids = {str(a.user.id) for a in monitor_admins}

                # Local sync for monitored group (remove departed, add admins)
                cursor_mon.execute(
                    "SELECT username, user_id, status FROM main_group_users WHERE chat_id = ?",
                    (monitor_chat_id,),
                )
                monitor_rows = cursor_mon.fetchall()

                monitor_removed = []  # user_ids confirmed departed - NOT usernames
                monitor_present = []

                for username, user_id, status in monitor_rows:
                    try:
                        m = await context.bot.get_chat_member(
                            chat_id=int(monitor_chat_id), user_id=int(user_id)
                        )
                        if m.status in ["left", "kicked"] and str(user_id) not in current_monitor_admin_ids:
                            monitor_removed.append(user_id)
                        elif m.status in ["left", "kicked"]:
                            # Disagrees with the admin list just fetched -
                            # trust that over get_chat_member for the same
                            # anonymous-admin reason as /refreshusers.
                            monitor_present.append((user_id, username, None, None))
                        else:
                            live_username = getattr(m.user, "username", None) or getattr(m.user, "first_name", None) or username
                            monitor_present.append((user_id, live_username, m.user.first_name, m.user.last_name))
                    except BadRequest as e:
                        # Same protective handling as /refreshusers' own loop -
                        # a transient API error is NOT confirmation of
                        # departure, keep them rather than risk a false removal.
                        logger.error(f"refreshusersall: BadRequest for user {username} (user_id={user_id}) in {monitor_chat_id}: {e}")
                        monitor_present.append((user_id, username, None, None))
                    except Exception as e:
                        logger.error(f"refreshusersall: Exception for user {username} (user_id={user_id}) in {monitor_chat_id}: {e}")
                        monitor_present.append((user_id, username, None, None))

                if monitor_removed:
                    # Deletes by user_id, NOT username - see /refreshusers'
                    # own fix for the full reused-username reasoning.
                    cursor_mon.executemany(
                        "DELETE FROM main_group_users WHERE chat_id = ? AND user_id = ?",
                        [(monitor_chat_id, uid) for uid in monitor_removed],
                    )
                    conn_mon.commit()

                # Add missing admins for monitored group - reuses
                # monitor_admins fetched above, no second API call.
                monitor_added = []
                try:
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
                    logger.error(f"refreshusersall: could not process admins for {chat_name}: {e}")

            # Dedupe monitor_present
            monitor_present = list({
                str(uid): (uid, uname, first_name, last_name)
                for uid, uname, first_name, last_name in monitor_present
            }.values())

            # Sync to sheets with chat_id (each monitor gets its own chat_id)
            await sync_users_sheet(monitor_chat_id, monitor_present, sheet_owner_chat_id=chat_id)

            status_line = f"  ✅ Synced: `{escape_markdown(chat_name)}`"
            if monitor_removed:
                status_line += f" \\(-{len(monitor_removed)}\\)"
            if monitor_added:
                status_line += f" \\(+{len(monitor_added)}\\)"
            lines.append(status_line)
        except Exception as e:
            logger.error(f"refreshusersall failed for {chat_name}: {e}")
            lines.append(f"  ❌ Failed: `{escape_markdown(chat_name)}`")

        if progress_msg is not None:
            try:
                await progress_msg.edit_text(
                    f"{ICON_GLOBE} Refreshing {processed_count}/{total_chats} chats\\.\\.\\.",
                    parse_mode="MarkdownV2",
                )
            except Exception:
                # Rate limit, message deleted by the admin, or the text
                # happened to be identical to the last edit ("message is
                # not modified") - none of these should ever interrupt
                # the actual sync work still in progress.
                pass

    if progress_msg is not None:
        try:
            await progress_msg.delete()
        except Exception:
            pass

    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


# ---------------------------------------------------------------------------
# Event sharing
# ---------------------------------------------------------------------------

def parse_shareevent_args(args: list):
    """
    Parses arguments for /shareevent.

    Supported flags
    ───────────────
    -mgl / -maingoinglist visible|hidden|onlycount – Going list visibility
        for this specific share (defaults to onlycount, matching the
        pre-existing default before this flag was introduced).
    -sngl / -sharenotgoinglist visible|hidden|onlycount – Not Going list
        visibility override for this share; None if omitted (inherits
        the event's own -ngl setting).
    -swl / -sharewaitlist visible|hidden|onlycount – Waitlist visibility
        override for this share; None if omitted (inherits the event's
        own -wl setting).
    -clc / -clickability on|off – whether names are clickable mentions
        in this specific share's post; None if omitted (inherits the
        event's own -clc setting).

    The first token that isn't consumed by one of the flags above is
    the target (alias or chat_id) - order-independent, so flags can
    appear before or after it.

    Returns: (target_input, mode, share_notgoing_viz, share_waitlist_viz, share_clickability)
    target_input is None if no target token was found at all (the
    caller's job to reject that as a syntax error). mode is always one
    of "-visible"/"-hidden"/"-onlycount" (never None).
    """
    mgl_flags = {"-mgl", "-maingoinglist"}
    sngl_flags = {"-sngl", "-sharenotgoinglist"}
    swl_flags = {"-swl", "-sharewaitlist"}
    clc_flags = {"-clc", "-clickability"}
    visibility_words = ("visible", "hidden", "onlycount")
    clickability_words = ("on", "off")

    target_input = None
    mode = "-onlycount"
    share_notgoing_viz = None
    share_waitlist_viz = None
    share_clickability = None

    tokens = args[:]
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in mgl_flags and i + 1 < len(tokens) and tokens[i + 1].strip().lower() in visibility_words:
            mode = f"-{tokens[i + 1].strip().lower()}"
            i += 2
        elif token in sngl_flags and i + 1 < len(tokens) and tokens[i + 1].strip().lower() in visibility_words:
            share_notgoing_viz = tokens[i + 1].strip().lower()
            i += 2
        elif token in swl_flags and i + 1 < len(tokens) and tokens[i + 1].strip().lower() in visibility_words:
            share_waitlist_viz = tokens[i + 1].strip().lower()
            i += 2
        elif token in clc_flags and i + 1 < len(tokens) and tokens[i + 1].strip().lower() in clickability_words:
            share_clickability = tokens[i + 1].strip().lower()
            i += 2
        elif target_input is None:
            target_input = token.strip()
            i += 1
        else:
            i += 1

    return target_input, mode, share_notgoing_viz, share_waitlist_viz, share_clickability


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
            text="❌ *Syntax error:* `/shareevent [target_alias/id] [-mgl visible|hidden|onlycount] "
                 "[-sngl visible|hidden|onlycount] [-swl visible|hidden|onlycount] [-clc on|off]`",
            parse_mode="MarkdownV2",
        )
        return

    target_input, mode, share_notgoing_viz, share_waitlist_viz, share_clickability = parse_shareevent_args(args)

    if target_input is None:
        await context.bot.send_message(
            chat_id=main_hub_chat_id,
            text="❌ *Syntax error:* `/shareevent [target_alias/id] [-mgl visible|hidden|onlycount] "
                 "[-sngl visible|hidden|onlycount] [-swl visible|hidden|onlycount] [-clc on|off]`",
            parse_mode="MarkdownV2",
        )
        return

    # -clc/-clickability is now a gated feature (item 3).
    if share_clickability is not None and not has_feature(main_hub_chat_id, "clickability"):
        await context.bot.send_message(
            chat_id=main_hub_chat_id,
            text=f"{ICON_WARNING} `\\-clc`/`\\-clickability` requires a higher tier\\. Contact the bot owner to upgrade\\.",
            parse_mode="MarkdownV2",
        )
        return

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
            ensure_event_migrated(cursor, event_id, str(main_hub_chat_id))
            conn.commit()
            cursor.execute(
                "SELECT COALESCE(SUM(1 + guests), 0) FROM event_users WHERE event_id = ? AND status = 'going'",
                (event_id,),
            )
            current_headcount = cursor.fetchone()[0]
            if current_headcount >= total_limit:
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
        # Telegram genuinely never reveals who's behind an anonymous post,
        # so we can't run the SAME per-user "are you an admin of the
        # TARGET chat" check below for them - but "Remain Anonymous" is
        # itself an admin-only toggle (set in the group's own Admin
        # Rights panel; a regular member has no way to enable it,
        # spoofed or otherwise), so an anonymous caller is, by Telegram's
        # own enforcement, guaranteed to already be a real admin of THIS
        # hub. Trust that and skip straight to sharing - the bot's own
        # admin status in the target chat (already checked above,
        # unconditionally) remains the one hard requirement either way.
        pass
    else:
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
                    (event_id, chat_id, message_id, share_mode, chat_type, share_notgoing_visibility, share_waitlist_visibility, share_clickability)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (event_id, str(target_chat_api), str(sent.message_id), mode, chat_type_flag, share_notgoing_viz, share_waitlist_viz, share_clickability),
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
                cursor.execute("SELECT chat_id FROM events WHERE event_id = ?", (event_id,))
                event_row = cursor.fetchone()
                if event_row is None:
                    return
                hub_chat_id = event_row[0]

                ensure_event_migrated(cursor, event_id, hub_chat_id)

                # Search every chat relevant to this event (the hub itself,
                # plus every child chat it's been shared to) for a real,
                # resolved user_id for this username - not just wherever the
                # admin happens to be typing from. The target person is very
                # often a member of a monitored/shared CHILD chat only,
                # never the hub itself - using the admin's own chat_id
                # blindly here previously caused a fake, duplicate
                # event_users row (keyed under the wrong chat_id) instead of
                # correctly attaching the guest to the person's real,
                # existing membership.
                cursor.execute("SELECT chat_id FROM event_shares WHERE event_id = ?", (event_id,))
                candidate_chat_ids = [hub_chat_id] + [r[0] for r in cursor.fetchall()]

                resolved_chat_id = None
                user_id = None
                for candidate_chat_id in candidate_chat_ids:
                    cursor.execute(
                        "SELECT user_id FROM main_group_users WHERE chat_id = ? AND username = ?",
                        (candidate_chat_id, target_username),
                    )
                    row = cursor.fetchone()
                    if row and row[0]:
                        resolved_chat_id = candidate_chat_id
                        user_id = row[0]
                        break

                if resolved_chat_id is None:
                    # Not tracked in any chat relevant to this event - fall
                    # back to the admin's own chat_id (previous behavior),
                    # using the raw username as a fallback identifying key
                    # since there's no real id to attach this to anywhere.
                    resolved_chat_id = chat_id
                    user_id = target_username

                cursor.execute(
                    "SELECT status, guests FROM event_users WHERE event_id = ? AND chat_id = ? AND username = ?",
                    (event_id, resolved_chat_id, target_username),
                )
                existing = cursor.fetchone()
                if not existing or existing[0] != "going":
                    existing_guests = existing[1] if existing else 0
                    cursor.execute(
                        "INSERT OR REPLACE INTO event_users (event_id, chat_id, user_id, username, status, guests) VALUES (?, ?, ?, ?, 'going', ?)",
                        (event_id, resolved_chat_id, user_id, target_username, existing_guests if existing else 0),
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
            "SELECT event_id, waitlist_data FROM events WHERE chat_id = ? ORDER BY ROWID DESC LIMIT 1",
            (hub_chat_id,),
        )
        event_row = cursor.fetchone()

    if not event_row:
        await update.message.reply_text("❌ No event found for this group\\.", parse_mode="MarkdownV2")
        return

    raw_waitlist = json.loads(event_row[1] or "[]")
    waitlist = dedupe_waitlist(raw_waitlist)
    if len(waitlist) != len(raw_waitlist):
        # Stale duplicate person-entries found (likely left over from
        # before click-time dedup existed) - persist the cleanup so they
        # don't keep resurfacing on every /waitlist call.
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE events SET waitlist_data = ? WHERE event_id = ?",
                (json.dumps(waitlist), event_row[0]),
            )
            conn.commit()

    if is_child_caller:
        count, text_lines = _render_waitlist_local(waitlist, calling_chat_id)
    else:
        count, text_lines = await _render_waitlist_all(waitlist, hub_chat_id, context)

    if count == 0:
        await update.message.reply_text("The waitlist is currently empty\\.", parse_mode="MarkdownV2")
        return

    text = f"*Waitlist* \\({count}\\):\n" + text_lines
    await update.message.reply_text(text, parse_mode="MarkdownV2")


# Variant 4 (text argument, as requested): "<number><unit>" - d(ays)
# 1-99999, w(eeks) 1-9999, m(onths) 1-999, y(ears) 1-99 - or "all" for
# no filter. The 3 keyboard buttons below use "all"/"1y"/"6m", which
# this same format already covers natively (1 year, 6 months), so one
# parser/validator serves both the text argument and the buttons.
STATS_PERIOD_RANGES = {
    "d": (1, 99999),
    "w": (1, 9999),
    "m": (1, 999),
    "y": (1, 99),
}
STATS_UNIT_NAMES = {"d": "day", "w": "week", "m": "month", "y": "year"}


def _parse_stats_period(text: str):
    """
    Returns (period_key, error_message) - period_key is "all" or a
    normalized "<number><unit>" string on success (error_message is
    None); on failure period_key is None and error_message explains
    why, ready to send straight to the user.
    """
    if not text:
        return "all", None
    text = text.strip().lower()
    if text == "all":
        return "all", None

    m = re.fullmatch(r"(\d+)([dwmy])", text)
    if not m:
        return None, (
            f"{ICON_WARNING} Invalid period `{escape_markdown(text)}`\\. "
            f"Use a number \\+ `d`/`w`/`m`/`y` \\(e\\.g\\. `30d`, `4w`, `6m`, `2y`\\), or `all`\\."
        )

    amount, unit = int(m.group(1)), m.group(2)
    low, high = STATS_PERIOD_RANGES[unit]
    if not (low <= amount <= high):
        return None, (
            f"{ICON_WARNING} `{amount}{unit}` is out of range\\. "
            f"Valid `{unit}` range is {low}\\-{high}\\."
        )
    return f"{amount}{unit}", None


def _stats_period_label(period: str) -> str:
    if period == "all":
        return "All Time"
    amount, unit = int(period[:-1]), period[-1]
    name = STATS_UNIT_NAMES[unit]
    if amount == 1:
        return f"Last {name.capitalize()}"
    return f"Last {amount} {name}s"


def _stats_period_cutoff(period: str):
    if period == "all":
        return None
    amount, unit = int(period[:-1]), period[-1]
    if unit == "d":
        return datetime.now() - timedelta(days=amount)
    if unit == "w":
        return datetime.now() - timedelta(weeks=amount)
    if unit == "m":
        # Simple, dependency-free approximation (30 days/month) -
        # exact calendar-month arithmetic isn't worth the complexity
        # for a usage-stats cutoff.
        return datetime.now() - timedelta(days=amount * 30)
    if unit == "y":
        return datetime.now() - timedelta(days=amount * 365)


def _closed_event_ids_for_period(chat_id: str, period: str):
    """
    Shared by _compute_stats and _compute_top_users. Returns
    (events_amount, closed_event_ids) for this hub within the given
    period - see _compute_stats' own docstring for the exact
    created_date filtering rules (Python-side parsing, events with no
    created_date excluded from any specific period but included in
    "all").
    """
    cutoff = _stats_period_cutoff(period)

    def _within_period(created_date_raw):
        if cutoff is None:
            return True
        if not created_date_raw:
            return False
        try:
            return datetime.strptime(created_date_raw, "%d.%m.%Y %H:%M:%S.%f") >= cutoff
        except ValueError:
            return False

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT event_id, event_status, created_date FROM events WHERE chat_id = ?", (chat_id,))
        all_rows = [r for r in cursor.fetchall() if _within_period(r[2])]

    events_amount = len(all_rows)
    closed_event_ids = [r[0] for r in all_rows if r[1] == 2]
    return events_amount, closed_event_ids


def _compute_stats(chat_id: str, period: str = "all"):
    """
    Shared by /stats and its Users-view callback. Returns
    (events_amount, events_closed, total_members, average_members).

    Period filtering uses events.created_date (a real DB column since
    v4.3.8, DD.MM.YYYY HH:MM:SS.fff via now2ddmmyy() - NOT a format
    that sorts correctly as a raw SQL string, so filtering happens in
    Python after parsing each row's own created_date). An event with
    no created_date at all (created before that column existed) is
    excluded from any specific period - there's no way to know when
    it actually happened - but always included in "all".
    """
    events_amount, closed_event_ids = _closed_event_ids_for_period(chat_id, period)
    events_closed = len(closed_event_ids)

    with get_connection() as conn:
        cursor = conn.cursor()
        # Single unified query per event_id (no chat_id filter within
        # event_users) - a closed event has already gone through Save &
        # Close, which migrates the master hub's own contribution into
        # event_users too (chat_id=main_chat_id), the same as any child
        # chat's. Summing a separate master-only count on top of this
        # would double-count anyone already represented there.
        total_members = 0
        for event_id in closed_event_ids:
            cursor.execute(
                "SELECT status, guests FROM event_users WHERE event_id = ?",
                (event_id,),
            )
            rows = cursor.fetchall()
            if rows:
                going_count = sum(1 for status, guests in rows if status == "going")
                guests_total = sum(guests for status, guests in rows)
                total_members += going_count + guests_total
            else:
                # Historical event closed before Save & Close wrote into
                # event_users at all - fall back to its own frozen
                # going_data/counters_data for just this one event.
                cursor.execute(
                    "SELECT going_data, counters_data FROM events WHERE event_id = ?",
                    (event_id,),
                )
                going_raw, counters_raw = cursor.fetchone()
                total_members += len(json.loads(going_raw or "[]")) + sum(json.loads(counters_raw or "{}").values())

    average_members = round(total_members / events_closed, 1) if events_closed > 0 else 0
    return events_amount, events_closed, total_members, average_members


def _compute_top_users(chat_id: str, period: str = "all", top_n: int = 3):
    """
    Returns (top_by_attendance, top_by_total, top_by_average) - each a
    list of up to top_n (display_name, value) tuples, sorted
    descending. top_by_attendance ranks by raw count of closed events
    attended (going status) in the period, regardless of guests.
    top_by_total ranks by SUM of guests invited across those events;
    top_by_average ranks by that same sum divided by events attended,
    so someone who came to 1 event with 5 guests doesn't automatically
    outrank someone who consistently brings 3 guests to 10 events on
    the "average" list, while still surfacing on the "total" list.
    """
    _, closed_event_ids = _closed_event_ids_for_period(chat_id, period)

    # user_id -> [guests_total, events_attended, username, first_name, last_name]
    per_user = {}
    with get_connection() as conn:
        cursor = conn.cursor()
        for event_id in closed_event_ids:
            cursor.execute(
                "SELECT user_id, username, first_name, last_name, guests FROM event_users "
                "WHERE event_id = ? AND status = 'going'",
                (event_id,),
            )
            for user_id, username, first_name, last_name, guests in cursor.fetchall():
                if not user_id:
                    continue
                entry = per_user.setdefault(user_id, [0, 0, username, first_name, last_name])
                entry[0] += guests or 0
                entry[1] += 1
                # Prefer a row that actually has a name over an earlier
                # one that didn't (data quality varies row to row).
                if not entry[2] and username:
                    entry[2] = username
                if not entry[3] and first_name:
                    entry[3] = first_name
                if not entry[4] and last_name:
                    entry[4] = last_name

    def _display(user_id, entry):
        _, _, username, first_name, last_name = entry
        if first_name:
            name = f"{first_name} {last_name}".strip() if last_name else first_name
        else:
            name = username or f"user{user_id}"
        return _mention_link(chat_id, username or name, user_id, display_name_override=name if first_name else None)

    by_attendance = sorted(per_user.items(), key=lambda kv: kv[1][1], reverse=True)[:top_n]
    top_by_attendance = [(_display(uid, e), e[1]) for uid, e in by_attendance if e[1] > 0]

    by_total = sorted(per_user.items(), key=lambda kv: kv[1][0], reverse=True)[:top_n]
    top_by_total = [(_display(uid, e), e[0]) for uid, e in by_total if e[0] > 0]

    by_avg = sorted(per_user.items(), key=lambda kv: kv[1][0] / kv[1][1], reverse=True)[:top_n]
    top_by_average = [(_display(uid, e), round(e[0] / e[1], 1)) for uid, e in by_avg if e[0] > 0]

    return top_by_attendance, top_by_total, top_by_average


def _build_top_users_text(chat_id: str, period: str, group_name: str = None) -> str:
    top_by_attendance, top_by_total, top_by_average = _compute_top_users(chat_id, period)

    header = f"{ICON_STATS} *Top Users for {escape_markdown(group_name)}*" if group_name else f"{ICON_STATS} *Top Users*"
    period_label = escape_markdown(_stats_period_label(period))

    def _section(title, rows, unit_label):
        if not rows:
            return f"*{title}*\n_No data for this period_"
        lines = [f"*{title}*"]
        for i, (name, value) in enumerate(rows, start=1):
            value_text = str(value).replace(".", "\\.")
            label = unit_label
            if value == 1 and unit_label.endswith("s") and "/" not in unit_label:
                label = unit_label[:-1]
            lines.append(f"{i}\\. {name} \\- {value_text} {label}")
        return "\n".join(lines)

    attendance_section = _section("Most events attended", top_by_attendance, "events")
    total_section = _section("Most guests invited \\(total\\)", top_by_total, "guests")
    average_section = _section("Most guests invited \\(average per event\\)", top_by_average, "guests/event")

    return (
        f"{header}\n"
        f"_{period_label}_\n\n"
        f"{attendance_section}\n\n"
        f"{total_section}\n\n"
        f"{average_section}"
    )


STATS_DISTRIBUTION_PAGE_SIZE = 5


def _stats_keyboard(chat_id: str, period: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("👥 Users", callback_data=f"statsusers_{chat_id}:{period}"),
        InlineKeyboardButton("📊 Distribution", callback_data=f"statsdist_{chat_id}:{period}:0"),
    ]])


def _top_users_keyboard(chat_id: str, period: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔙 Back to Stats", callback_data=f"statsback_{chat_id}:{period}"),
    ]])


def _compute_distribution(hub_chat_id: str, period: str):
    """
    Returns a list of dicts, one per chat_id (the hub itself plus every
    child chat an event was shared to) that had at least one total
    member across every closed event in the period:
        {"chat_id", "chat_name", "events_shared", "total", "average", "max", "min"}
    Sorted with the hub itself always first, then by events_shared
    descending. "Events shared" counts every closed event in the
    period that was posted to this chat_id (hub or event_shares),
    regardless of whether anyone from there actually attended -
    "total/average/max/min" are computed only from event_users rows
    that exist for that (event, chat_id) pair.
    """
    _, closed_event_ids = _closed_event_ids_for_period(hub_chat_id, period)

    # chat_id -> {"events_shared": int, "member_counts": [int, ...]}
    per_chat = {}
    with get_connection() as conn:
        cursor = conn.cursor()
        for event_id in closed_event_ids:
            cursor.execute("SELECT chat_id FROM event_shares WHERE event_id = ?", (event_id,))
            share_chat_ids = [hub_chat_id] + [r[0] for r in cursor.fetchall()]

            for share_chat_id in share_chat_ids:
                entry = per_chat.setdefault(share_chat_id, {"events_shared": 0, "member_counts": []})
                entry["events_shared"] += 1

                cursor.execute(
                    "SELECT status, guests FROM event_users WHERE event_id = ? AND chat_id = ?",
                    (event_id, share_chat_id),
                )
                rows = cursor.fetchall()
                going_count = sum(1 for status, guests in rows if status == "going")
                guests_total = sum(guests or 0 for _, guests in rows)
                entry["member_counts"].append(going_count + guests_total)

        results = []
        for share_chat_id, entry in per_chat.items():
            total = sum(entry["member_counts"])
            if total <= 0:
                continue  # "хоть 1" - skip chats with zero total participation
            if share_chat_id == hub_chat_id:
                cursor.execute("SELECT chat_name FROM all_groups WHERE chat_id = ?", (hub_chat_id,))
            else:
                cursor.execute("SELECT chat_name FROM sub_chats WHERE chat_id = ?", (share_chat_id,))
            row = cursor.fetchone()
            chat_name = row[0] if row and row[0] else share_chat_id

            results.append({
                "chat_id": share_chat_id,
                "chat_name": chat_name,
                "events_shared": entry["events_shared"],
                "total": total,
                "average": round(total / len(entry["member_counts"]), 1),
                "max": max(entry["member_counts"]),
                "min": min(entry["member_counts"]),
            })

    results.sort(key=lambda r: (r["chat_id"] != hub_chat_id, -r["events_shared"]))
    return results


def _build_distribution_text(hub_chat_id: str, period: str, page: int, group_name: str = None):
    """Returns (text, total_pages, clamped_page)."""
    rows = _compute_distribution(hub_chat_id, period)

    header = f"{ICON_STATS} *Distribution for {escape_markdown(group_name)}*" if group_name else f"{ICON_STATS} *Distribution*"
    period_label = escape_markdown(_stats_period_label(period))

    if not rows:
        text = f"{header}\n_{period_label}_\n\n_No data for this period_"
        return text, 1, 0

    total_pages = max(1, (len(rows) + STATS_DISTRIBUTION_PAGE_SIZE - 1) // STATS_DISTRIBUTION_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    page_rows = rows[page * STATS_DISTRIBUTION_PAGE_SIZE:(page + 1) * STATS_DISTRIBUTION_PAGE_SIZE]

    blocks = []
    for r in page_rows:
        average_text = str(r["average"]).replace(".", "\\.")
        blocks.append(
            f"*{escape_markdown(r['chat_name'])}*\n"
            f"Events shared: {r['events_shared']}\n"
            f"Total event members amount: {r['total']}\n"
            f"Average event members amount: {average_text}\n"
            f"Max event members amount: {r['max']}\n"
            f"Min event members amount: {r['min']}"
        )

    text = f"{header}\n_{period_label}_\n\n" + "\n\n".join(blocks)
    return text, total_pages, page


def _distribution_keyboard(chat_id: str, period: str, page: int, total_pages: int) -> InlineKeyboardMarkup:
    rows = []
    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"statsdist_{chat_id}:{period}:{page - 1}"))
        nav_row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"statsdist_{chat_id}:{period}:{page + 1}"))
        rows.append(nav_row)
    rows.append([InlineKeyboardButton("🔙 Back to Stats", callback_data=f"statsback_{chat_id}:{period}")])
    return InlineKeyboardMarkup(rows)


def _build_stats_text(chat_id: str, period: str, group_name: str = None) -> str:
    events_amount, events_closed, total_members, average_members = _compute_stats(chat_id, period)
    average_members_text = str(average_members).replace(".", "\\.")

    header = f"{ICON_STATS} *Event Stats for {escape_markdown(group_name)}*" if group_name else f"{ICON_STATS} *Event Stats*"
    period_label = escape_markdown(_stats_period_label(period))

    return (
        f"{header}\n"
        f"_{period_label}_\n\n"
        f"Events amount: {events_amount}\n"
        f"Events closed: {events_closed}\n"
        f"Total members amount: {total_members}\n"
        f"Average members amount: {average_members_text}"
    )


@register_hub_command("stats")
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE, override_chat_id: str = None):
    """
    Shows event activity stats for THIS hub group: how many events have
    ever been created, how many were closed (Save & Close Event), and the
    total/average headcount (going + guests) across every closed event.
    PRO-gated (the "stats" feature) - a quick usage snapshot, not tied to
    any single event.

    Period: /stats [period] where period is "<number><unit>" - d(ays)
    1-99999, w(eeks) 1-9999, m(onths) 1-999, y(ears) 1-99 - or omitted/
    "all" for no filter (e.g. /stats 30d, /stats 2y). A "Users" button
    under the reply shows the same period's top-3 guest-inviters (see
    stats_users_callback_handler) without re-running the command.
    """
    chat_id = await resolve_hub_chat_id(update, context, "stats", override_chat_id)
    if chat_id is None:
        return

    if not await require_premium(update, "Event stats", chat_id=chat_id):
        return

    period_arg = context.args[0] if context.args else None
    period, error = _parse_stats_period(period_arg)
    if error:
        await update.message.reply_text(error, parse_mode="MarkdownV2")
        return

    is_dm = update.effective_chat.type == "private"
    group_name = None
    if is_dm:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT chat_name FROM all_groups WHERE chat_id = ?", (chat_id,))
            row = cursor.fetchone()
            group_name = row[0] if row and row[0] else chat_id

    text = _build_stats_text(chat_id, period, group_name)
    await update.message.reply_text(
        text, parse_mode="MarkdownV2", reply_markup=_stats_keyboard(chat_id, period)
    )


async def _stats_group_name(chat_id: str, is_dm: bool):
    if not is_dm:
        return None
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT chat_name FROM all_groups WHERE chat_id = ?", (chat_id,))
        row = cursor.fetchone()
        return row[0] if row and row[0] else chat_id


async def stats_users_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the "👥 Users" button under /stats (statsusers_<chat_id>:<period>) -
    switches the SAME message to show the period's top-3 guest-inviters."""
    query = update.callback_query
    await query.answer()

    chat_id, period = query.data.split("_", 1)[1].split(":", 1)
    period, error = _parse_stats_period(period)
    if error:
        return

    if not await is_real_admin(context.bot, chat_id, update.effective_user, message=query.message):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    if not await require_premium(update, "Event stats", chat_id=chat_id):
        return

    group_name = await _stats_group_name(chat_id, query.message.chat.type == "private")
    text = _build_top_users_text(chat_id, period, group_name)
    await query.edit_message_text(
        text, parse_mode="MarkdownV2", reply_markup=_top_users_keyboard(chat_id, period)
    )


async def stats_distribution_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the "📊 Distribution" button and its Prev/Next pagination
    (statsdist_<chat_id>:<period>:<page>) - per-chat breakdown of event
    members for every chat_id (hub or shared child) with at least one
    total member in the period."""
    query = update.callback_query
    await query.answer()

    chat_id, period, page_raw = query.data.split("_", 1)[1].split(":")
    period, error = _parse_stats_period(period)
    if error:
        return
    try:
        page = int(page_raw)
    except ValueError:
        page = 0

    if not await is_real_admin(context.bot, chat_id, update.effective_user, message=query.message):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    if not await require_premium(update, "Event stats", chat_id=chat_id):
        return

    group_name = await _stats_group_name(chat_id, query.message.chat.type == "private")
    text, total_pages, clamped_page = _build_distribution_text(chat_id, period, page, group_name)
    await query.edit_message_text(
        text, parse_mode="MarkdownV2",
        reply_markup=_distribution_keyboard(chat_id, period, clamped_page, total_pages),
    )


async def stats_back_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the "🔙 Back to Stats" button under the Users view (statsback_<chat_id>:<period>)."""
    query = update.callback_query
    await query.answer()

    chat_id, period = query.data.split("_", 1)[1].split(":", 1)
    period, error = _parse_stats_period(period)
    if error:
        return

    if not await is_real_admin(context.bot, chat_id, update.effective_user, message=query.message):
        await query.answer("⛔ Admins only.", show_alert=True)
        return
    if not await require_premium(update, "Event stats", chat_id=chat_id):
        return

    group_name = await _stats_group_name(chat_id, query.message.chat.type == "private")
    text = _build_stats_text(chat_id, period, group_name)
    await query.edit_message_text(
        text, parse_mode="MarkdownV2", reply_markup=_stats_keyboard(chat_id, period)
    )


# ---------------------------------------------------------------------------
# Global text router
# ---------------------------------------------------------------------------

async def global_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Routes all text messages: extra-player input first, then @everyone tracking."""
    if context.user_data.get("awaiting_extra_player_for"):
        await handle_extra_player_input(update, context)
        return
    await track_everyone_message(update, context)
