"""
Reopen mode - an owner-level way to take an event back from CLOSED or CANCELED to OPEN.

  /reopen on            a "🔓 Reopen" button appears under the group's newest closed / canceled events
  /reopen off           the buttons disappear
  /reopen               shows whether the mode is on
  /reopen <name or id>  reopens that event right away - for one too old to carry a button

Pressing a button (or using the command) puts the event back to OPEN (voting works again in every post it was shared
to); the usual Verify -> Save & Close flow can then close it again.

Why a switch instead of a permanent button: an inline keyboard can't be shown to one person only, so while the
mode is on EVERYONE sees the buttons (a non-owner who presses one just gets an "owners only" popup). The owner turns
it on, uses it, turns it off.

Rules - each one protects something:
  * ANY closed or canceled event can be reopened, but only ONE event of the group may be active at a time: /editevent,
    /waitlist, /who, /notify and the "already an active event" check of /newevent all work on the newest event, so
    two live events at once would leave them looking at the wrong one. So the buttons are not shown at all while an
    event is open or in verification, and a press (or the command) is refused if one is;
  * the buttons sit on the 15 newest closed / canceled events (db.REOPEN_BUTTON_LIMIT) - every button is a post that
    has to be edited whenever the picture changes - and the command reaches the rest;
  * the Google Sheet is cleaned FIRST and the whole reopen is abandoned if that fails: the event is open again and
    has no final roster, so its EventUsers rows go (and the Events row says OPEN again);
  * the roster the event had when it was closed is remembered, so the next Save & Close can log in Actions who was
    added and who was removed in between (ROSTER_ADDED / ROSTER_REMOVED).
"""
import json

from telegram import Update
from telegram.ext import ContextTypes

from config import logger, ICON_ADMIN_ONLY
from db import (
    get_connection, is_reopen_active, set_reopen_mode, other_active_event, reopen_button_events,
)
from event_engine import get_event_lock, schedule_view_refresh, refresh_reopen_buttons
from hub_resolver import resolve_hub_chat_id, register_hub_command
from sheets import reopen_event_in_sheet
from subscription import require_feature, feature_available, feature_min_tier
from utils import is_real_admin, now2ddmmyy

BUTTON_PREFIX = "reopen_"
MAX_LISTED = 5


async def _may_use(bot, hub_chat_id, user, message=None) -> bool:
    """
    The feature must be available to this person (an OWNER-level feature means a bot owner); and if an owner has
    opened it up to ordinary tiers with /updatefeature, group admins only - a plain member never.
    """
    if not feature_available(hub_chat_id, user.id, "reopen"):
        return False
    if feature_min_tier("reopen") == "OWNER":
        return True
    return await is_real_admin(bot, hub_chat_id, user, message=message)


def _find_closed_events(hub_chat_id: str, text: str) -> list:
    """[(event_id, name, status)] of the hub's closed / canceled events matching `text`: an exact event id, else a
    case-insensitive part of the name. Newest first."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT event_id, name, event_status FROM events WHERE chat_id = ? AND event_status IN (2, -1) ORDER BY ROWID DESC",
            (str(hub_chat_id),),
        ).fetchall()
    needle = text.strip().casefold()
    by_id = [r for r in rows if str(r[0]).casefold() == needle]
    return by_id or [r for r in rows if needle in (r[1] or "").casefold()]


async def perform_reopen(context, hub_chat_id: str, event_id: str, user, clicked_in: str):
    """
    Takes one closed / canceled event back to OPEN. -> (ok, text): the line to tell the person, success or the reason
    nothing happened. Everything is decided under the event's lock, so two owners acting at the same moment cannot
    both win.
    """
    async with get_event_lock(event_id):
        with get_connection() as conn:
            row = conn.execute("SELECT event_status FROM events WHERE event_id = ? AND chat_id = ?", (event_id, hub_chat_id)).fetchone()
        if row is None:
            return False, "This event no longer exists."
        event_status = row[0]
        if event_status not in (2, -1):
            return False, "This event is already open."
        blocking = other_active_event(hub_chat_id, event_id)
        if blocking:
            return False, f"“{blocking[:60]}” is still open. Close or cancel it first, then reopen this one."

        # The roster it was closed with, to tell the next Save & Close what changed. A canceled event never had one.
        snapshot = None
        if event_status == 2:
            with get_connection() as conn:
                people = conn.execute(
                    "SELECT user_id, username FROM event_users WHERE event_id = ? AND status = 'going'", (event_id,)
                ).fetchall()
            snapshot = {str(uid): (name or str(uid)) for uid, name in people if uid}

        username_raw = user.username if user.username else (user.first_name or f"user{user.id}")
        try:
            await reopen_event_in_sheet(
                hub_chat_id, event_id,
                actions_row=[event_id, "REOPEN", username_raw, str(user.id), now2ddmmyy(), clicked_in],
            )
        except Exception as e:
            logger.error(f"Reopen: Google Sheets cleanup failed for {event_id}: {repr(e)}")
            return False, "Google Sheets couldn't be updated, so nothing was changed. Try again in a minute."

        with get_connection() as conn:
            conn.execute(
                "UPDATE events SET event_status = 0, closed_date = NULL, reopened_roster = ? WHERE event_id = ?",
                (json.dumps(snapshot, ensure_ascii=False) if snapshot is not None else None, event_id),
            )
            conn.commit()

    # The event's own posts are re-rendered as an open event; every OTHER post loses its button (one event at a time).
    context.application.create_task(schedule_view_refresh(context, event_id))
    context.application.create_task(refresh_reopen_buttons(context.bot, hub_chat_id, skip_event_id=event_id))
    return True, "🔓 Event reopened - voting is open again."


@register_hub_command("reopen")
async def reopen_command(update: Update, context: ContextTypes.DEFAULT_TYPE, override_chat_id: str = None):
    """/reopen on | off | <event name or id> | (nothing = show the state). Ignored - no reply at all - for anyone without the feature."""
    hub_chat_id = await resolve_hub_chat_id(update, context, "reopen", override_chat_id)
    if hub_chat_id is None:
        return
    if not await require_feature(update, "reopen", "/reopen", hub_chat_id):
        return
    if feature_min_tier("reopen") != "OWNER" and not await is_real_admin(
        context.bot, hub_chat_id, update.effective_user, message=update.message
    ):
        await update.message.reply_text(f"{ICON_ADMIN_ONLY} Only admins can use /reopen\\.", parse_mode="MarkdownV2")
        return

    args = context.args or []
    arg = args[0].lower() if args else ""
    if arg in ("", "status"):
        state = "ON" if is_reopen_active(hub_chat_id) else "OFF"
        await update.message.reply_text(
            f"🔓 Reopen mode is *{state}* for this group\\. Use `/reopen on`, `/reopen off`, "
            f"or `/reopen <event name or id>` to reopen one event right away\\.",
            parse_mode="MarkdownV2",
        )
        return

    if arg in ("on", "off"):
        set_reopen_mode(hub_chat_id, arg == "on", update.effective_user.id)
        context.application.create_task(refresh_reopen_buttons(context.bot, hub_chat_id))
        if arg == "off":
            text = "🔓 Reopen mode is *OFF*\\. The buttons are gone\\."
        else:
            shown = len(reopen_button_events(hub_chat_id))
            if other_active_event(hub_chat_id, ""):
                where = ("An event is open right now, so no button is shown yet \\- they appear under the closed and "
                         "canceled events as soon as it is closed or canceled\\.")
            elif shown:
                where = f"A *🔓 Reopen* button now shows under the {shown} newest closed or canceled event\\(s\\)\\."
            else:
                where = "There is no closed or canceled event yet \\- the button appears under the next one\\."
            text = (
                f"🔓 Reopen mode is *ON*\\. {where} Everyone can see them, only owners can use them\\. "
                f"An older event: `/reopen <name or id>`\\. Turn the mode off with `/reopen off`\\."
            )
        await update.message.reply_text(text, parse_mode="MarkdownV2")
        return

    # Anything else is an event to reopen right away.
    wanted = " ".join(args)
    matches = _find_closed_events(hub_chat_id, wanted)
    if not matches:
        await update.message.reply_text(f"No closed or canceled event of this group matches “{wanted[:60]}”.")
        return
    if len(matches) > 1:
        listed = "\n".join(f"• {name} — {event_id}" for event_id, name, _ in matches[:MAX_LISTED])
        more = f"\n… and {len(matches) - MAX_LISTED} more" if len(matches) > MAX_LISTED else ""
        await update.message.reply_text(f"Several events match “{wanted[:60]}” - repeat the command with the id of the one you mean:\n{listed}{more}")
        return
    event_id, name, _status = matches[0]
    ok, text = await perform_reopen(context, hub_chat_id, event_id, update.effective_user, hub_chat_id)
    await update.message.reply_text(f"{name[:60]}: {text}")


async def reopen_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    The 🔓 Reopen button (reopen_<event_id>). It has its own handler - not button_handler - because that one
    answers every callback before it knows what it is, and every refusal here needs a popup of its own
    (Telegram accepts one answer per press).
    """
    query = update.callback_query
    event_id = query.data.split("_", 1)[1]
    user = query.from_user
    clicked_in = str(query.message.chat_id)

    async def refuse(text):
        await query.answer(text, show_alert=True)

    with get_connection() as conn:
        row = conn.execute("SELECT chat_id FROM events WHERE event_id = ?", (event_id,)).fetchone()
    if row is None:
        await refuse("This event no longer exists.")
        return
    hub_chat_id = str(row[0])

    if clicked_in != hub_chat_id:
        await refuse("Reopen only works in the group where the event was created.")
        return
    # No `message=` here: query.message is the BOT's post, while the clicker is always a real person (a press carries
    # the real user even when they post anonymously), so their membership has to be checked for real.
    if not await _may_use(context.bot, hub_chat_id, user):
        await refuse("⛔ Owners only.")
        return
    if not is_reopen_active(hub_chat_id):
        await refuse("Reopen mode is off now. Turn it on with /reopen on.")
        return

    ok, text = await perform_reopen(context, hub_chat_id, event_id, user, clicked_in)
    if ok:
        await query.answer(text)
    else:
        await refuse(text)
