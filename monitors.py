"""
Monitoring subsystem: lets a hub group track another group/channel's
membership for cross-group user analytics. Premium-only.
"""

from telegram import Update
from telegram.ext import ContextTypes

from config import ICON_STATS, logger
from utils import escape_markdown
from db import get_connection
from subscription import require_premium


async def addmonitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Adds a group/channel for monitoring.
    Usage: /addmonitor id_channel or /addmonitor id_group
    The bot must be added to the group/channel and the user must be admin in both
    the main group and the monitored group/channel.
    """
    if not await require_premium(update, "Monitoring"):
        return

    args = context.args
    if len(args) < 1:
        await update.message.reply_text(
            "❌ *Syntax error:* `/addmonitor <chat_id>`\\.",
            parse_mode="MarkdownV2",
        )
        return

    target_chat_id = args[0]
    main_chat_id = str(update.effective_chat.id)
    user_id = update.effective_user.id

    try:
        # Check if user is admin in main chat
        try:
            main_admin = await context.bot.get_chat_member(main_chat_id, user_id)
            if main_admin.status not in ["administrator", "creator"]:
                await update.message.reply_text(
                    "❌ You must be an admin in the main group to add monitors\\.",
                    parse_mode="MarkdownV2",
                )
                return
        except Exception:
            await update.message.reply_text(
                "❌ Could not verify admin status in main chat\\.",
                parse_mode="MarkdownV2",
            )
            return

        # Check if bot is in target chat
        try:
            bot_member = await context.bot.get_chat_member(target_chat_id, context.bot.id)
        except Exception:
            await update.message.reply_text(
                "❌ Bot is not a member of the target group/channel\\.",
                parse_mode="MarkdownV2",
            )
            return

        # Check if user is admin in target chat
        try:
            target_admin = await context.bot.get_chat_member(target_chat_id, user_id)
            if target_admin.status not in ["administrator", "creator"]:
                await update.message.reply_text(
                    "❌ You must be an admin in the target group/channel to add it as a monitor\\.",
                    parse_mode="MarkdownV2",
                )
                return
        except Exception:
            await update.message.reply_text(
                "❌ Could not verify admin status in target chat\\.",
                parse_mode="MarkdownV2",
            )
            return

        # Get chat info
        try:
            chat_info = await context.bot.get_chat(target_chat_id)
            chat_name = chat_info.title or "Unknown"
            chat_type = "channel" if chat_info.type == "channel" else "group"
        except Exception:
            await update.message.reply_text(
                "❌ Could not retrieve chat information\\.",
                parse_mode="MarkdownV2",
            )
            return

        # Add to database
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id FROM sub_groups WHERE chat_id = ? AND (owner_chat_id = ? OR owner_chat_id IS NULL)",
                (target_chat_id, main_chat_id),
            )
            existing = cursor.fetchone()

            if existing:
                # A sub_groups row already exists for this (owner, chat_id)
                # pair - possibly an alias-only row from /setalias. Turn on
                # monitoring on that same row instead of inserting a second
                # one, which would violate UNIQUE(owner_chat_id, chat_id).
                cursor.execute(
                    "UPDATE sub_groups SET is_monitored = 1, chat_type = ?, chat_name = ? "
                    "WHERE chat_id = ? AND (owner_chat_id = ? OR owner_chat_id IS NULL)",
                    (chat_type, chat_name, target_chat_id, main_chat_id),
                )
            else:
                cursor.execute(
                    "INSERT INTO sub_groups (chat_id, chat_type, chat_name, owner_chat_id, is_monitored) "
                    "VALUES (?, ?, ?, ?, 1)",
                    (target_chat_id, chat_type, chat_name, main_chat_id),
                )
            conn.commit()

        await update.message.reply_text(
            f"✅ Added monitor: `{escape_markdown(chat_name)}` \\({chat_type}\\)",
            parse_mode="MarkdownV2",
        )

    except Exception as e:
        logger.error(f"Error adding monitor: {e}")
        await update.message.reply_text(
            "❌ Failed to add monitor\\.",
            parse_mode="MarkdownV2",
        )


async def removemonitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Removes a group/channel from monitoring.
    Usage: /removemonitor id_channel or /removemonitor id_group
    """
    if not await require_premium(update, "Monitoring"):
        return

    args = context.args
    if len(args) < 1:
        await update.message.reply_text(
            "❌ *Syntax error:* `/removemonitor <chat_id>`\\.",
            parse_mode="MarkdownV2",
        )
        return

    target_chat_id = args[0]
    hub_chat_id    = str(update.effective_chat.id)

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT chat_name, alias FROM sub_groups WHERE chat_id = ? AND is_monitored = 1 "
            "AND (owner_chat_id = ? OR owner_chat_id IS NULL)",
            (target_chat_id, hub_chat_id),
        )
        row = cursor.fetchone()

        if not row:
            await update.message.reply_text(
                "❌ Monitor not found\\.",
                parse_mode="MarkdownV2",
            )
            return

        chat_name, alias = row
        if alias is not None:
            # This chat is also aliased - only turn off monitoring, keep
            # the row (and its alias) intact.
            cursor.execute(
                "UPDATE sub_groups SET is_monitored = 0 WHERE chat_id = ? "
                "AND (owner_chat_id = ? OR owner_chat_id IS NULL)",
                (target_chat_id, hub_chat_id),
            )
        else:
            cursor.execute(
                "DELETE FROM sub_groups WHERE chat_id = ? AND (owner_chat_id = ? OR owner_chat_id IS NULL)",
                (target_chat_id, hub_chat_id),
            )
        conn.commit()

    await update.message.reply_text(
        f"✅ Removed monitor: `{escape_markdown(chat_name)}`",
        parse_mode="MarkdownV2",
    )


async def listmonitors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Lists all monitored groups/channels belonging to THIS hub group.
    """
    if not await require_premium(update, "Monitoring"):
        return

    hub_chat_id = str(update.effective_chat.id)
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT chat_id, chat_type, chat_name FROM sub_groups WHERE is_monitored = 1 AND (owner_chat_id = ? OR owner_chat_id IS NULL)",
            (hub_chat_id,),
        )
        rows = cursor.fetchall()

    if not rows:
        await update.message.reply_text(
            f"{ICON_STATS} No monitors configured\\.",
            parse_mode="MarkdownV2",
        )
        return

    lines = []
    for chat_id, chat_type, chat_name in rows:
        lines.append(
            f"name: `{escape_markdown(chat_name)}`\n"
            f"type: {escape_markdown(chat_type)}\n"
            f"id: `{escape_markdown(chat_id)}`"
        )

    text = f"{ICON_STATS} *Monitored Groups/Channels:*\n\n" + "\n\n".join(lines)
    await update.message.reply_text(text, parse_mode="MarkdownV2")
