"""
Alias routing subsystem: lets a hub group bind a short, memorable name to a
target chat_id for use with /shareevent. Premium-only.
"""

import sqlite3

from telegram import Update
from telegram.ext import ContextTypes
from telegram.error import BadRequest

from config import ICON_WARNING
from utils import escape_markdown, GROUP_ANONYMOUS_BOT_ID
from db import get_connection
from subscription import require_premium
from hub_resolver import resolve_hub_chat_id, register_hub_command


@register_hub_command("setalias")
async def setalias(update: Update, context: ContextTypes.DEFAULT_TYPE, override_chat_id: str = None):
    """Binds a custom alias to a Telegram Chat ID."""
    hub_chat_id = await resolve_hub_chat_id(update, context, "setalias", override_chat_id)
    if hub_chat_id is None:
        return
    if not await require_premium(update, "Aliases", chat_id=hub_chat_id):
        return

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
            "Add @EventPlanCheckBot to target group/channel as admin\\.", parse_mode="MarkdownV2"
        )
        return

    try:
        bot_member = await context.bot.get_chat_member(chat_id=target_chat_id, user_id=context.bot.id)
        if bot_member.status not in ["administrator", "creator"]:
            await update.message.reply_text(
                "Add @EventPlanCheckBot to target group/channel as admin\\.", parse_mode="MarkdownV2"
            )
            return
    except Exception:
        await update.message.reply_text(
            "Add @EventPlanCheckBot to target group/channel as admin\\.", parse_mode="MarkdownV2"
        )
        return

    if user_id == GROUP_ANONYMOUS_BOT_ID:
        await update.message.reply_text(
            "Please disable \"Remain anonymous\" and re\\-run /setalias \\- your admin status in the "
            "target group/channel can't be verified anonymously",
            parse_mode="MarkdownV2",
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

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT chat_id FROM sub_groups WHERE alias = ? AND (owner_chat_id = ? OR owner_chat_id IS NULL)",
            (alias_name, hub_chat_id),
        )
        if cursor.fetchone():
            await update.message.reply_text("Alias already exist", parse_mode="MarkdownV2")
            return

        cursor.execute(
            "SELECT alias FROM sub_groups WHERE chat_id = ? AND (owner_chat_id = ? OR owner_chat_id IS NULL)",
            (str(target_chat_id), hub_chat_id),
        )
        existing_row = cursor.fetchone()
        if existing_row and existing_row[0] is not None:
            await update.message.reply_text(
                f"{ICON_WARNING} This group or channel has already been added\\. Please check its existing alias\\.",
                parse_mode="MarkdownV2",
            )
            return

        try:
            if existing_row is not None:
                # A sub_groups row already exists for this (owner, chat_id)
                # pair - it just came from /addmonitor (is_monitored=1,
                # alias still NULL). Set the alias on that same row instead
                # of inserting a second one, which would violate
                # UNIQUE(owner_chat_id, chat_id).
                cursor.execute(
                    "UPDATE sub_groups SET alias = ? WHERE chat_id = ? AND (owner_chat_id = ? OR owner_chat_id IS NULL)",
                    (alias_name, str(target_chat_id), hub_chat_id),
                )
            else:
                cursor.execute(
                    "INSERT INTO sub_groups (chat_id, alias, owner_chat_id) VALUES (?, ?, ?)",
                    (str(target_chat_id), alias_name, hub_chat_id),
                )
            conn.commit()
        except sqlite3.IntegrityError:
            # Uniqueness is scoped per-owner - UNIQUE(owner_chat_id, alias)
            # and UNIQUE(owner_chat_id, chat_id) - so two different hubs can
            # freely reuse the same alias name for different targets. The
            # SELECT checks above already cover the common case; this is
            # just a safety net against a race (two concurrent /setalias
            # calls from the SAME hub for the same name/target).
            await update.message.reply_text(
                f"{ICON_WARNING} That alias name or target is already in use for this group\\. Pick a different name\\.",
                parse_mode="MarkdownV2",
            )
            return

    await update.message.reply_text(
        rf"✅ Alias `__{escape_markdown(alias_name)}__` mapped to node ID `{target_chat_id}`\\.",
        parse_mode="MarkdownV2",
    )


@register_hub_command("removealias")
async def removealias(update: Update, context: ContextTypes.DEFAULT_TYPE, override_chat_id: str = None):
    """Removes an alias from the routing table."""
    hub_chat_id = await resolve_hub_chat_id(update, context, "removealias", override_chat_id)
    if hub_chat_id is None:
        return
    if not await require_premium(update, "Aliases", chat_id=hub_chat_id):
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "❌ *Syntax error:* Usage: `/removealias [aliasname]`", parse_mode="MarkdownV2"
        )
        return

    alias_name  = args[0].strip().lower()
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT is_monitored FROM sub_groups WHERE alias = ? AND (owner_chat_id = ? OR owner_chat_id IS NULL)",
            (alias_name, hub_chat_id),
        )
        row = cursor.fetchone()
        if not row:
            await update.message.reply_text("🔍 Alias not found\\.", parse_mode="MarkdownV2")
            return

        if row[0]:
            # This chat is also monitored - only clear the alias, keep the
            # row (and its monitoring) intact.
            cursor.execute(
                "UPDATE sub_groups SET alias = NULL WHERE alias = ? AND (owner_chat_id = ? OR owner_chat_id IS NULL)",
                (alias_name, hub_chat_id),
            )
        else:
            cursor.execute(
                "DELETE FROM sub_groups WHERE alias = ? AND (owner_chat_id = ? OR owner_chat_id IS NULL)",
                (alias_name, hub_chat_id),
            )
        conn.commit()
    await update.message.reply_text(
        f"🗑️ Alias `__{escape_markdown(alias_name)}__` removed\\.", parse_mode="MarkdownV2"
    )


@register_hub_command("listalias")
async def listalias(update: Update, context: ContextTypes.DEFAULT_TYPE, override_chat_id: str = None):
    """Shows all active routing aliases belonging to THIS hub group."""
    hub_chat_id = await resolve_hub_chat_id(update, context, "listalias", override_chat_id)
    if hub_chat_id is None:
        return
    if not await require_premium(update, "Aliases", chat_id=hub_chat_id):
        return

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT alias, chat_id FROM sub_groups WHERE alias IS NOT NULL "
            "AND (owner_chat_id = ? OR owner_chat_id IS NULL)",
            (hub_chat_id,),
        )
        rows = cursor.fetchall()

    if not rows:
        await update.message.reply_text("📋 No aliases configured\\.", parse_mode="MarkdownV2")
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
