"""
Hideability - owner-level switch for collapsible lists in a group's event posts.

  /hideability on     the Going, Not Going, Waitlist and "Going from ..." lists of the group's event posts (and of the
                      posts shared from it) become native collapsible blocks: tap a list's title to fold or unfold it,
                      on your own screen only. Going starts unfolded, Not Going and Waitlist folded.
  /hideability off    plain posts again, nothing to fold - exactly as before the feature
  /hideability        shows whether it is on

How it works: richtext.py (Telegram "Rich Messages"). A list shown only as a count, hidden, or empty is never
collapsible. The switch is per MAIN group (hub) and applies to every event of it, shared posts included; the posts are
re-rendered right away. It is an OWNER-tier feature (all_features row "hideability"); an owner can open it to other tiers
with /updatefeature hideability -minlevel pro, after which group admins of that tier may switch it.
If Telegram ever refuses a rich message the post is edited as ordinary text instead - the lists just are not collapsible.
"""
from telegram import Update
from telegram.ext import ContextTypes

from config import ICON_ADMIN_ONLY, logger
from db import get_connection, is_hideability_active, set_hideability_mode
from event_engine import schedule_view_refresh
from hub_resolver import resolve_hub_chat_id, register_hub_command
from subscription import require_feature, feature_min_tier
from utils import is_real_admin


def _posts_to_refresh(hub_chat_id: str) -> list:
    """Every event of the hub whose posts are worth re-rendering: the open ones and the recent finished ones - an old
    closed post is the record and is re-rendered the next time anything touches it."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT event_id FROM events WHERE chat_id = ? ORDER BY ROWID DESC LIMIT 15", (str(hub_chat_id),)
        ).fetchall()
    return [str(r[0]) for r in rows]


@register_hub_command("hideability")
async def hideability_command(update: Update, context: ContextTypes.DEFAULT_TYPE, override_chat_id: str = None):
    """/hideability on | off | (nothing = show the state). Ignored - no reply at all - for anyone without the feature."""
    hub_chat_id = await resolve_hub_chat_id(update, context, "hideability", override_chat_id)
    if hub_chat_id is None:
        return
    if not await require_feature(update, "hideability", "/hideability", hub_chat_id):
        return
    if feature_min_tier("hideability") != "OWNER" and not await is_real_admin(
        context.bot, hub_chat_id, update.effective_user, message=update.message
    ):
        await update.message.reply_text(f"{ICON_ADMIN_ONLY} Only admins can use /hideability\\.", parse_mode="MarkdownV2")
        return

    args = context.args or []
    arg = args[0].lower() if args else ""
    if arg in ("", "status"):
        state = "ON" if is_hideability_active(hub_chat_id) else "OFF"
        await update.message.reply_text(
            f"🗂 Hideability is *{state}* for this group\\. `/hideability on` makes the lists of the event posts collapsible, "
            f"`/hideability off` brings back plain posts\\.",
            parse_mode="MarkdownV2",
        )
        return

    if arg not in ("on", "off"):
        await update.message.reply_text("Use /hideability on or /hideability off.")
        return

    set_hideability_mode(hub_chat_id, arg == "on", update.effective_user.id)
    for event_id in _posts_to_refresh(hub_chat_id):
        context.application.create_task(schedule_view_refresh(context, event_id))
    if arg == "on":
        text = (
            "🗂 Hideability is *ON*\\. In the event posts of this group, tap the title of a list \\(Going, Not Going, Waitlist\\) "
            "to fold or unfold it \\- on your own screen only\\. Going starts open, Not Going and Waitlist folded\\. "
            "Turn it off with `/hideability off`\\."
        )
    else:
        text = "🗂 Hideability is *OFF*\\. The event posts are plain again, with nothing to fold\\."
    await update.message.reply_text(text, parse_mode="MarkdownV2")
