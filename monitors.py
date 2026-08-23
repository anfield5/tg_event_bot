"""
Monitoring subsystem: lets a hub group track another group/channel's
membership for cross-group user analytics. Premium-only.
"""

from telegram import Update
from telegram.ext import ContextTypes

from config import ICON_STATS, logger
from utils import escape_markdown, is_real_admin, GROUP_ANONYMOUS_BOT_ID
from db import get_connection
from subscription import require_premium
from hub_resolver import resolve_hub_chat_id, register_hub_command


@register_hub_command("addmonitor")
async def addmonitor(update: Update, context: ContextTypes.DEFAULT_TYPE, override_chat_id: str = None):
    """
    Adds a group/channel for monitoring.
    Usage: /addmonitor id_channel or /addmonitor id_group
    The bot must be added to the group/channel and the user must be admin in both
    the main group and the monitored group/channel.
    """
    main_chat_id = await resolve_hub_chat_id(update, context, "addmonitor", override_chat_id)
    if main_chat_id is None:
        return
    if not await require_premium(update, "Monitoring", chat_id=main_chat_id):
        return

    args = context.args
    if len(args) < 1:
        await update.message.reply_text(
            "❌ *Syntax error:* `/addmonitor <chat_id>`\\.",
            parse_mode="MarkdownV2",
        )
        return

    target_chat_id = args[0]
    user_id = update.effective_user.id

    try:
        # Check if user is admin in main chat (same chat the command was
        # sent from - safe to trust Telegram's anonymous-admin substitution)
        if not await is_real_admin(context.bot, main_chat_id, update.effective_user, message=update.message):
            await update.message.reply_text(
                "❌ You must be an admin in the main group to add monitors\\.",
                parse_mode="MarkdownV2",
            )
            return

        # Check if bot is in target chat AND is an admin there - Telegram
        # only delivers chat_member updates about OTHER users (needed for
        # auto-tracking new joiners without a button click) when the bot
        # itself is an admin in that chat. A bot that's only a regular
        # member would silently never see new people join, so this is
        # checked upfront with a clear error instead of monitoring
        # succeeding and quietly failing to notice anyone later.
        try:
            bot_member = await context.bot.get_chat_member(target_chat_id, context.bot.id)
        except Exception:
            await update.message.reply_text(
                "❌ Bot is not a member of the target group/channel\\.",
                parse_mode="MarkdownV2",
            )
            return

        if bot_member.status not in ("administrator", "creator"):
            await update.message.reply_text(
                "❌ Bot must be an *admin* in the target group/channel to monitor it \\- "
                "Telegram only tells the bot about new members joining when it has admin rights there\\. "
                "Please promote the bot, then re\\-run /addmonitor\\.",
                parse_mode="MarkdownV2",
            )
            return

        # Check if user is admin in TARGET chat (a different chat than the
        # one the command was sent from) - if they're posting anonymously,
        # we cannot verify their real identity's admin status in a chat
        # other than the one Telegram already vouched for, so ask them to
        # disable anonymous mode for this specific command rather than
        # either bypassing insecurely or silently rejecting a real admin.
        if user_id == GROUP_ANONYMOUS_BOT_ID:
            await update.message.reply_text(
                "❌ Please disable \"Remain anonymous\" and re\\-run /addmonitor - "
                "your admin status in the target group/channel can't be verified anonymously\\.",
                parse_mode="MarkdownV2",
            )
            return
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
            resolved_chat_id = str(chat_info.id)
        except Exception:
            await update.message.reply_text(
                "❌ Could not retrieve chat information\\.",
                parse_mode="MarkdownV2",
            )
            return

        # Add to database - always the RESOLVED numeric chat_id, never the
        # raw user-typed target_chat_id (which could be a @username,
        # especially common for channels with public usernames). Storing
        # a non-numeric value here would silently break every downstream
        # int(chat_id) call, most notably /refreshusersall's per-monitor
        # loop, which would then quietly skip that chat entirely.
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id FROM sub_chats WHERE chat_id = ? AND (owner_chat_id = ? OR owner_chat_id IS NULL)",
                (resolved_chat_id, main_chat_id),
            )
            existing = cursor.fetchone()

            if existing:
                # A sub_chats row already exists for this (owner, chat_id)
                # pair - possibly an alias-only row from /setalias. Turn on
                # monitoring on that same row instead of inserting a second
                # one, which would violate UNIQUE(owner_chat_id, chat_id).
                cursor.execute(
                    "UPDATE sub_chats SET is_monitored = 1, chat_type = ?, chat_name = ? "
                    "WHERE chat_id = ? AND (owner_chat_id = ? OR owner_chat_id IS NULL)",
                    (chat_type, chat_name, resolved_chat_id, main_chat_id),
                )
            else:
                cursor.execute(
                    "INSERT INTO sub_chats (chat_id, chat_type, chat_name, owner_chat_id, is_monitored) "
                    "VALUES (?, ?, ?, ?, 1)",
                    (resolved_chat_id, chat_type, chat_name, main_chat_id),
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


@register_hub_command("removemonitor")
async def removemonitor(update: Update, context: ContextTypes.DEFAULT_TYPE, override_chat_id: str = None):
    """
    Removes a group/channel from monitoring.
    Usage: /removemonitor id_channel or /removemonitor id_group
    """
    hub_chat_id = await resolve_hub_chat_id(update, context, "removemonitor", override_chat_id)
    if hub_chat_id is None:
        return
    if not await require_premium(update, "Monitoring", chat_id=hub_chat_id):
        return

    args = context.args
    if len(args) < 1:
        await update.message.reply_text(
            "❌ *Syntax error:* `/removemonitor <chat_id>`\\.",
            parse_mode="MarkdownV2",
        )
        return

    target_chat_id = args[0]

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT chat_name, alias FROM sub_chats WHERE chat_id = ? AND is_monitored = 1 "
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
                "UPDATE sub_chats SET is_monitored = 0 WHERE chat_id = ? "
                "AND (owner_chat_id = ? OR owner_chat_id IS NULL)",
                (target_chat_id, hub_chat_id),
            )
        else:
            cursor.execute(
                "DELETE FROM sub_chats WHERE chat_id = ? AND (owner_chat_id = ? OR owner_chat_id IS NULL)",
                (target_chat_id, hub_chat_id),
            )
        conn.commit()

    await update.message.reply_text(
        f"✅ Removed monitor: `{escape_markdown(chat_name)}`",
        parse_mode="MarkdownV2",
    )


@register_hub_command("listmonitors")
async def listmonitors(update: Update, context: ContextTypes.DEFAULT_TYPE, override_chat_id: str = None):
    """
    Lists all monitored groups/channels belonging to THIS hub group.
    """
    hub_chat_id = await resolve_hub_chat_id(update, context, "listmonitors", override_chat_id)
    if hub_chat_id is None:
        return
    if not await require_premium(update, "Monitoring", chat_id=hub_chat_id):
        return

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT chat_id, chat_type, chat_name FROM sub_chats WHERE is_monitored = 1 AND (owner_chat_id = ? OR owner_chat_id IS NULL)",
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
