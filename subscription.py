"""
Subscription tiers (free/premium), owner-controlled manual activation, and
the Control Sheet sync that mirrors all_groups for visibility.

Payment itself is confirmed by hand (crypto, checked by the bot owner) -
there is deliberately no payment automation here.
"""

import re
import sqlite3
from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import ContextTypes

from config import ICON_WARNING, OWNER_USER_IDS, logger
from utils import escape_markdown, is_real_admin
from db import get_connection
from sheets import sync_control_sheet_main, sync_control_sheet_subconfig, sync_control_sheet_channels, open_spreadsheet

SUBS_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"  # ISO-ish, chosen so string comparison
# isn't relied upon anywhere - always parsed via strptime, but kept
# unambiguous/sortable as a matter of hygiene for anyone reading the DB directly.


def is_premium(chat_id: str) -> bool:
    """
    True if this hub currently has an active premium subscription.
    Auto-expires: type='pro' with a subs_date_end in the past is treated
    as NOT premium, without needing any background job to flip the flag back -
    the flag only ever matters at the moment a premium-gated command runs.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT type, subs_date_end FROM all_groups WHERE chat_id = ?",
            (str(chat_id),),
        )
        row = cursor.fetchone()

    if not row or row[0] != "pro" or not row[1]:
        return False
    try:
        return datetime.strptime(row[1], SUBS_DATE_FORMAT) > datetime.now()
    except ValueError:
        return False


# Source of truth for both /help's wording and the Control Sheet's
# "sub_config" tab (sheets.sync_control_sheet_subconfig mirrors this list
# verbatim) - keeping ONE list means the sheet can never silently drift from
# what the bot actually enforces.
FEATURE_MATRIX = [
    # (feature label,                 free,       premium)
    ("/newevent",                     "available", "available"),
    ("/editevent",                    "available", "available"),
    ("/listusers",                    "available", "available"),
    ("/notify",                       "available", "available"),
    ("/shareevent (per target group/channel)", "limited (3)", "available"),
    ("Aliases (/setalias etc.)",      "not available", "available"),
    ("Monitoring (/addmonitor etc.)", "not available", "available"),
]


async def require_premium(update: Update, feature_label: str, chat_id: str = None) -> bool:
    """
    Call at the top of a premium-only command. Returns True if the
    invoking chat's hub is premium (caller proceeds normally); otherwise
    sends the upgrade message and returns False (caller should just return).

    `chat_id` can be passed explicitly when the caller has already resolved
    the real hub to act on (e.g. via hub_resolver.resolve_hub_chat_id for a
    command run from a DM) - otherwise it defaults to
    update.effective_chat.id, which is only correct when the command was
    run directly inside the group itself.
    """
    if chat_id is None:
        chat_id = str(update.effective_chat.id)
    if is_premium(chat_id):
        return True
    await update.message.reply_text(
        f"{ICON_WARNING} *{escape_markdown(feature_label)}* is a PRO\\-only feature\\. "
        f"Use /setsub info or contact the bot owner to upgrade\\.",
        parse_mode="MarkdownV2",
    )
    return False


async def _push_control_sheet_main() -> bool:
    """Reads all of all_groups and pushes it to the Control Sheet's 'GROUPS' tab."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT chat_id, chat_name, type, sheet_id, sheet_name, subs_date_start, subs_date_end, "
            "visibility, date_bot_add FROM all_groups"
        )
        rows = cursor.fetchall()
    return await sync_control_sheet_main(rows)


async def _push_control_sheet_channels() -> bool:
    """Reads all of all_channels and pushes it to the Control Sheet's 'CHANNELS' tab."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT chat_id, chat_name, visibility, date_bot_add FROM all_channels")
        rows = cursor.fetchall()
    return await sync_control_sheet_channels(rows)


async def setsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Owner-only. The simplest possible manual subscription control - no
    payment automation at all, since payment is confirmed by hand (crypto,
    checked by the bot owner) anyway:

      /setsub <chat_id> on <days>   - activate/extend premium by <days>
      /setsub <chat_id> off         - deactivate premium immediately

    Deliberately gated on the bot owner(s)' own Telegram user_id
    (OWNER_USER_IDS), NOT on "is admin in this chat" - a hub's own admin
    could otherwise just grant themselves a free subscription.

    After every change, pushes the updated all_groups to the Control
    Sheet's "MAIN" tab, so you can see every group's status there without
    needing to run any command.
    """
    if update.effective_user.id not in OWNER_USER_IDS:
        return  # silent - don't reveal this command exists to non-owners

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "❌ *Syntax:* `/setsub <chat_id> on <days>` or `/setsub <chat_id> off`",
            parse_mode="MarkdownV2",
        )
        return

    target_chat_id = args[0]
    mode           = args[1].lower()

    # Best-effort: snapshot the group's display name for the Control Sheet,
    # so it shows something more useful than a bare numeric chat_id. Not
    # fatal if this fails (e.g. bot isn't in that chat) - falls back to None.
    chat_name = None
    try:
        chat_obj  = await context.bot.get_chat(int(target_chat_id))
        chat_name = chat_obj.title or chat_obj.username
    except Exception as e:
        logger.error(f"setsub: could not fetch chat name for {target_chat_id}: {e}")

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT type, subs_date_end FROM all_groups WHERE chat_id = ?",
            (target_chat_id,),
        )
        existing = cursor.fetchone()

        if mode == "off":
            if existing:
                cursor.execute(
                    "UPDATE all_groups SET type = 'free', chat_name = COALESCE(?, chat_name) WHERE chat_id = ?",
                    (chat_name, target_chat_id),
                )
            else:
                cursor.execute(
                    "INSERT INTO all_groups (chat_id, chat_name, type) VALUES (?, ?, 'free')",
                    (target_chat_id, chat_name),
                )
            conn.commit()
            await update.message.reply_text(
                f"✅ Subscription turned *off* for `{target_chat_id}`\\.",
                parse_mode="MarkdownV2",
            )
            await _push_control_sheet_main()
            return

        if mode == "on":
            if len(args) < 3 or not args[2].isdigit():
                await update.message.reply_text(
                    "❌ *Syntax:* `/setsub <chat_id> on <days>`", parse_mode="MarkdownV2"
                )
                return
            days = int(args[2])

            # Extending an still-active subscription adds to its CURRENT end
            # date, not to "now" - otherwise renewing a few days early would
            # throw away the remaining days instead of stacking on top.
            base = datetime.now()
            if existing and existing[0] == "pro" and existing[1]:
                try:
                    prior_end = datetime.strptime(existing[1], SUBS_DATE_FORMAT)
                    if prior_end > base:
                        base = prior_end
                except ValueError:
                    pass

            new_end = (base + timedelta(days=days)).strftime(SUBS_DATE_FORMAT)
            new_start = existing[1] if existing and existing[0] == "pro" and existing[1] else datetime.now().strftime(SUBS_DATE_FORMAT)

            if existing:
                cursor.execute(
                    "UPDATE all_groups SET type = 'pro', subs_date_start = ?, subs_date_end = ?, chat_name = COALESCE(?, chat_name) WHERE chat_id = ?",
                    (new_start, new_end, chat_name, target_chat_id),
                )
            else:
                cursor.execute(
                    "INSERT INTO all_groups (chat_id, chat_name, type, subs_date_start, subs_date_end) VALUES (?, ?, 'pro', ?, ?)",
                    (target_chat_id, chat_name, new_start, new_end),
                )
            conn.commit()
            await update.message.reply_text(
                f"✅ Subscription *on* for `{target_chat_id}` until `{new_end}`\\.",
                parse_mode="MarkdownV2",
            )
            await _push_control_sheet_main()
            return

    await update.message.reply_text(
        "❌ *Syntax:* `/setsub <chat_id> on <days>` or `/setsub <chat_id> off`",
        parse_mode="MarkdownV2",
    )


async def setsheet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Binds this hub group to its own Google Sheet, by spreadsheet ID or full
    URL (the ID is extracted automatically from a pasted URL). Requires:
      1. This hub to be premium (free tier never writes to Sheets at all).
      2. The caller to be an admin of this chat.

    The sheet must already be shared with the bot's service account
    (GOOGLE_CREDENTIALS_JSON's client_email) with Editor access - this is
    verified immediately by actually opening it and reading its title,
    rather than trusting the ID blindly.
    """
    chat_id = str(update.effective_chat.id)

    if not is_premium(chat_id):
        await update.message.reply_text(
            f"{ICON_WARNING} /setsheet is a PRO\\-only feature\\. "
            f"Use /setsub info or contact the bot owner to upgrade\\.",
            parse_mode="MarkdownV2",
        )
        return

    admin_ok = await is_real_admin(
        context.bot, update.effective_chat.id, update.effective_user, message=update.message
    )
    if not admin_ok:
        await update.message.reply_text("⛔️ Only admins can use /setsheet\\.", parse_mode="MarkdownV2")
        return

    args = context.args
    if not args:
        await update.message.reply_text(
            "❌ *Syntax:* `/setsheet <spreadsheet_id_or_url>`", parse_mode="MarkdownV2"
        )
        return

    raw = args[0]
    m = re.search(r"/d/([a-zA-Z0-9-_]+)", raw)
    sheet_id = m.group(1) if m else raw

    try:
        ss = await open_spreadsheet(sheet_id)
        if not ss:
            raise ValueError("open_spreadsheet returned nothing")
        sheet_name = ss.title
    except Exception as e:
        logger.error(f"setsheet: could not open sheet {sheet_id}: {e}")
        await update.message.reply_text(
            f"❌ Could not open that spreadsheet\\. Make sure the ID/URL is correct and the sheet is "
            f"shared with the bot's service account \\(Editor access\\)\\.",
            parse_mode="MarkdownV2",
        )
        return

    with get_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                "UPDATE all_groups SET sheet_id = ?, sheet_name = ? WHERE chat_id = ?",
                (sheet_id, sheet_name, chat_id),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            await update.message.reply_text(
                f"❌ That spreadsheet is already bound to a different group\\.",
                parse_mode="MarkdownV2",
            )
            return

    await update.message.reply_text(
        f"✅ This group is now bound to `{sheet_name}`\\.",
        parse_mode="MarkdownV2",
    )
    await _push_control_sheet_main()


async def syncgroups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Owner-only. Manually pushes the current all_groups, all_channels, and
    the free/PRO feature matrix to the Control Sheet, without needing to
    change anyone's subscription first (e.g. right after setting up
    CONTROL_SHEET_ID for the first time).
    """
    if update.effective_user.id not in OWNER_USER_IDS:
        return

    groups_ok     = await _push_control_sheet_main()
    channels_ok   = await _push_control_sheet_channels()
    subconfig_ok  = await sync_control_sheet_subconfig(FEATURE_MATRIX)

    if groups_ok and channels_ok and subconfig_ok:
        await update.message.reply_text(
            "✅ Control Sheet synced \\(GROUPS \\+ CHANNELS \\+ SUB\\_CONFIG\\)\\.", parse_mode="MarkdownV2"
        )
    else:
        failed = []
        if not groups_ok:
            failed.append("GROUPS")
        if not channels_ok:
            failed.append("CHANNELS")
        if not subconfig_ok:
            failed.append("SUB_CONFIG")
        await update.message.reply_text(
            f"❌ Sync failed for: {escape_markdown(', '.join(failed))}\\. Check CONTROL_SHEET_ID is set, the sheet is "
            f"shared with the bot's service account \\(Editor access\\), and all three tabs exist with the "
            f"exact names `GROUPS`, `CHANNELS`, and `SUB_CONFIG`\\. See server logs for the specific error\\.",
            parse_mode="MarkdownV2",
        )
