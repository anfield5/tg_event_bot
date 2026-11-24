"""
Hideability - owner-level switch for collapsible lists in a group's event posts.

  /hideability on     the Going, Not Going, Waitlist and "Going from ..." lists of the group's event posts (and of the
                      posts shared from it) become native collapsible blocks: tap a list's title to fold or unfold it,
                      on your own screen only. By default Going starts unfolded, Not Going and Waitlist folded (the flags above change that).
  /hideability off    plain posts again, nothing to fold - exactly as before the feature
  /hideability        shows whether it is on, and how each list starts
  /hideability -g show -ng hide ...   (OWNERS only, one setting for every group) how each list STARTS on the viewer's
                      screen, show = unfolded, hide = folded. -g/-going (default show), -ng/-notgoing (hide),
                      -w/-waitlist (hide) are the lists of the group's own post, the "Going from <chat>" blocks
                      follow -g; -sg/-sharedgoing (show), -sng/-sharednotgoing (hide), -sw/-sharedwaitlist (hide) are the
                      same lists in the posts shared to other chats. Several flags can go in one command.

How it works: richtext.py (Telegram "Rich Messages"). A list shown only as a count, hidden, or empty is never
collapsible. The switch is per MAIN group (hub) and applies to every event of it, shared posts included; the posts are
re-rendered right away. It is an OWNER-tier feature (all_features row "hideability"); an owner can open it to other tiers
with /updatefeature hideability -minlevel pro, after which group admins of that tier may switch it.
If Telegram ever refuses a rich message the post is edited as ordinary text instead - the lists just are not collapsible.
"""
from telegram import Update
from telegram.ext import ContextTypes

import asyncio

import richtext
from config import ICON_ADMIN_ONLY, OWNER_USER_IDS
from db import (active_hideability_hubs, get_connection, get_hideability_flags, is_hideability_active,
                set_hideability_flags, set_hideability_mode)
from event_engine import schedule_view_refresh
from hub_resolver import resolve_hub_chat_id, register_hub_command
from subscription import require_feature, feature_min_tier
from utils import escape_markdown, is_real_admin


def _posts_to_refresh(hub_chat_id: str) -> list:
    """Every event of the hub whose posts are worth re-rendering: the open ones and the recent finished ones - an old
    closed post is the record and is re-rendered the next time anything touches it."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT event_id FROM events WHERE chat_id = ? ORDER BY ROWID DESC LIMIT 15", (str(hub_chat_id),)
        ).fetchall()
    return [str(r[0]) for r in rows]


REFRESH_PAUSE = 0.35     # seconds between two hubs' re-renders - a burst of edits draws Telegram flood limits


def parse_flags(args):
    """['-g', 'hide', '-sng', 'show'] -> ({"going": "hide", "sharednotgoing": "show"}, None), or ({}, the error text).
    Only flag/value pairs; the flag may be written -ng or -notgoing (and without the dash), the value is show or hide."""
    changes, i = {}, 0
    while i < len(args):
        name = args[i].lstrip("-").lower()
        if not args[i].startswith("-") or name not in richtext.FLAG_ALIASES:
            return {}, f"Unknown option: {args[i]}"
        if i + 1 >= len(args) or args[i + 1].lower() not in ("show", "hide"):
            return {}, f"{args[i]} needs show or hide, e.g. {args[i]} show"
        changes[richtext.FLAG_ALIASES[name]] = args[i + 1].lower()
        i += 2
    return changes, None


def flags_text(flags: dict) -> str:
    """The six lists and how each starts, one per line (plain text)."""
    return "\n".join(
        f"{richtext.FLAG_LABELS[k][0]} {richtext.FLAG_LABELS[k][1]}: {flags[k]}" for k in richtext.FLAG_DEFAULTS
    )


async def _refresh_all_hubs(context):
    """The list defaults are one setting for every group: re-render the recent events of every hub that has it on."""
    for hub in active_hideability_hubs():
        for event_id in _posts_to_refresh(hub):
            await schedule_view_refresh(context, event_id)
        await asyncio.sleep(REFRESH_PAUSE)


@register_hub_command("hideability")
async def hideability_command(update: Update, context: ContextTypes.DEFAULT_TYPE, override_chat_id: str = None):
    """/hideability on | off | (nothing = show the state). Ignored - no reply at all - for anyone without the feature."""
    args = context.args or []
    changes, flag_error = parse_flags(args) if args and args[0].startswith("-") else ({}, None)
    if args and args[0].startswith("-") and update.effective_user.id in OWNER_USER_IDS:
        # The list defaults belong to no group: an owner sets them from anywhere, DM included, and they apply to all.
        if flag_error:
            await update.message.reply_text(
                f"{flag_error}\nUse /hideability -g show|hide, -ng, -w, -sg, -sng, -sw (several at once are fine).")
            return
        set_hideability_flags(changes, update.effective_user.id)
        context.application.create_task(_refresh_all_hubs(context))
        await update.message.reply_text("🗂 Saved - the same for every group:\n" + flags_text(get_hideability_flags()))
        return

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

    arg = args[0].lower() if args else ""
    if arg.startswith("-"):
        await update.message.reply_text("Only the bot owner can change how the lists start.")
        return
    if arg in ("", "status"):
        state = "ON" if is_hideability_active(hub_chat_id) else "OFF"
        await update.message.reply_text(
            f"🗂 Hideability is *{state}* for this group\\. `/hideability on` makes the lists of the event posts collapsible, "
            f"`/hideability off` brings back plain posts\\.\n\nHow the lists start \\(the same for every group\\):\n"
            + escape_markdown(flags_text(get_hideability_flags())),
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
            "to fold or unfold it \\- on your own screen only\\. Which lists start open is set by the bot owner \\(by default Going\\)\\. "
            "Turn it off with `/hideability off`\\."
        )
    else:
        text = "🗂 Hideability is *OFF*\\. The event posts are plain again, with nothing to fold\\."
    await update.message.reply_text(text, parse_mode="MarkdownV2")
