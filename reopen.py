"""
Reopen mode - an owner-level way to take an event back from CLOSED or CANCELED to OPEN.

  /reopen on      a "🔓 Reopen" button appears under the group's newest closed or canceled event
  /reopen off     the button disappears
  /reopen         shows whether the mode is on

Pressing the button puts the event back to OPEN (voting works again in every post it was shared to); the usual
Verify -> Save & Close flow can then close it again.

Why a switch instead of a permanent button: an inline keyboard can't be shown to one person only, so while the
mode is on EVERYONE sees the button (a non-owner who presses it just gets a "owners only" popup). The owner turns it
on, uses it, turns it off.

Rules - each one protects something:
  * only the group's NEWEST event can be reopened: /editevent, /waitlist, /who, /notify and the "already an active
    event" check of /newevent all work on the newest one, so reopening an older event would leave them looking at
    the wrong event;
  * refused while another event of the group is still open or in verification - two live events at once confuses
    those same commands;
  * the Google Sheet is cleaned FIRST and the whole press is abandoned if that fails: Save & Close only APPENDS
    rows to EventUsers, so reopening without removing the old ones would list everybody twice after the next save.
"""
from telegram import Update
from telegram.ext import ContextTypes

from config import logger, ICON_ADMIN_ONLY
from db import (
    get_connection, is_reopen_active, set_reopen_mode, latest_event_of_hub, other_active_event,
)
from event_engine import get_event_lock, schedule_view_refresh
from hub_resolver import resolve_hub_chat_id, register_hub_command
from sheets import reopen_event_in_sheet
from subscription import require_feature, feature_available, feature_min_tier
from utils import escape_markdown, is_real_admin, now2ddmmyy

BUTTON_PREFIX = "reopen_"


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


@register_hub_command("reopen")
async def reopen_command(update: Update, context: ContextTypes.DEFAULT_TYPE, override_chat_id: str = None):
    """/reopen on | off | (nothing = show the state). Ignored - no reply at all - for anyone without the feature."""
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

    arg = context.args[0].lower() if context.args else ""
    if arg in ("", "status"):
        state = "ON" if is_reopen_active(hub_chat_id) else "OFF"
        await update.message.reply_text(
            f"🔓 Reopen mode is *{state}* for this group\\. Use `/reopen on` or `/reopen off`\\.",
            parse_mode="MarkdownV2",
        )
        return
    if arg not in ("on", "off"):
        await update.message.reply_text("Usage: `/reopen on` \\| `/reopen off` \\| `/reopen`", parse_mode="MarkdownV2")
        return

    set_reopen_mode(hub_chat_id, arg == "on", update.effective_user.id)

    # Show / hide the button right away on the post it belongs to.
    latest = latest_event_of_hub(hub_chat_id)
    event_waiting = latest is not None and latest[1] in (2, -1)
    if event_waiting:
        context.application.create_task(schedule_view_refresh(context, latest[0]))

    if arg == "on":
        where = (
            f"under the newest event, *{escape_markdown(latest[2])}*" if event_waiting
            else "under the next event that is closed or canceled"
        )
        text = (
            f"🔓 Reopen mode is *ON*\\. A *🔓 Reopen* button now shows {where} \\- everyone can see it, "
            f"only owners can use it\\. Turn it off with `/reopen off`\\."
        )
    else:
        text = "🔓 Reopen mode is *OFF*\\. The button is gone\\."
    await update.message.reply_text(text, parse_mode="MarkdownV2")


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

    async with get_event_lock(event_id):
        # Everything is decided again under the lock: two owners pressing at the same moment must not both win.
        with get_connection() as conn:
            event_status = conn.execute("SELECT event_status FROM events WHERE event_id = ?", (event_id,)).fetchone()[0]
        if event_status not in (2, -1):
            await refuse("This event is already open.")
            return
        latest = latest_event_of_hub(hub_chat_id)
        if latest is None or str(latest[0]) != event_id:
            await refuse("Only the newest event of this group can be reopened.")
            return
        blocking = other_active_event(hub_chat_id, event_id)
        if blocking:
            await refuse(f"“{blocking[:60]}” is still open. Close or cancel it first, then reopen this one.")
            return

        username_raw = user.username if user.username else (user.first_name or f"user{user.id}")
        try:
            await reopen_event_in_sheet(
                hub_chat_id, event_id,
                actions_row=[event_id, "REOPEN", username_raw, str(user.id), now2ddmmyy(), clicked_in],
            )
        except Exception as e:
            logger.error(f"Reopen: Google Sheets cleanup failed for {event_id}: {repr(e)}")
            await refuse("Google Sheets couldn't be updated, so nothing was changed. Try again in a minute.")
            return

        with get_connection() as conn:
            conn.execute("UPDATE events SET event_status = 0, closed_date = NULL WHERE event_id = ?", (event_id,))
            conn.commit()

    await query.answer("🔓 Event reopened - voting is open again.")
    context.application.create_task(schedule_view_refresh(context, event_id))
