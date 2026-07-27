"""
Subscription tiers (free/premium), owner-controlled manual activation, and
the Control Sheet sync that mirrors main_chat_settings for visibility.

Payment itself is confirmed by hand (crypto, checked by the bot owner) -
there is deliberately no payment automation here.
"""

from datetime import datetime, timedelta

from telegram import Update
from telegram.ext import ContextTypes

from config import ICON_WARNING, OWNER_USER_IDS, logger
from utils import escape_markdown
from db import get_connection
from sheets import sync_control_sheet_main, sync_control_sheet_subconfig

SUBS_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"  # ISO-ish, chosen so string comparison
# isn't relied upon anywhere - always parsed via strptime, but kept
# unambiguous/sortable as a matter of hygiene for anyone reading the DB directly.


def is_premium(chat_id: str) -> bool:
    """
    True if this hub currently has an active premium subscription.
    Auto-expires: type='premium' with a subs_date_end in the past is treated
    as NOT premium, without needing any background job to flip the flag back -
    the flag only ever matters at the moment a premium-gated command runs.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT type, subs_date_end FROM main_chat_settings WHERE chat_id = ?",
            (str(chat_id),),
        )
        row = cursor.fetchone()

    if not row or row[0] != "premium" or not row[1]:
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


async def require_premium(update: Update, feature_label: str) -> bool:
    """
    Call at the top of a premium-only command. Returns True if the
    invoking chat's hub is premium (caller proceeds normally); otherwise
    sends the upgrade message and returns False (caller should just return).
    """
    chat_id = str(update.effective_chat.id)
    if is_premium(chat_id):
        return True
    await update.message.reply_text(
        f"{ICON_WARNING} *{escape_markdown(feature_label)}* is a premium\\-only feature\\. "
        f"Use /setsub info or contact the bot owner to upgrade\\.",
        parse_mode="MarkdownV2",
    )
    return False


async def _push_control_sheet_main():
    """Reads all of main_chat_settings and pushes it to the Control Sheet's 'Main' tab."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT chat_id, chat_name, type, sheet_id, subs_date_start, subs_date_end FROM main_chat_settings"
        )
        rows = cursor.fetchall()
    await sync_control_sheet_main(rows)


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

    After every change, pushes the updated main_chat_settings to the Control
    Sheet's "Main" tab, so you can see every group's status there without
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
            "SELECT type, subs_date_end FROM main_chat_settings WHERE chat_id = ?",
            (target_chat_id,),
        )
        existing = cursor.fetchone()

        if mode == "off":
            if existing:
                cursor.execute(
                    "UPDATE main_chat_settings SET type = 'free', chat_name = COALESCE(?, chat_name) WHERE chat_id = ?",
                    (chat_name, target_chat_id),
                )
            else:
                cursor.execute(
                    "INSERT INTO main_chat_settings (chat_id, chat_name, type) VALUES (?, ?, 'free')",
                    (target_chat_id, chat_name),
                )
            conn.commit()
            await update.message.reply_text(
                f"✅ Subscription turned *off* for `{escape_markdown(target_chat_id)}`\\.",
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
            if existing and existing[0] == "premium" and existing[1]:
                try:
                    prior_end = datetime.strptime(existing[1], SUBS_DATE_FORMAT)
                    if prior_end > base:
                        base = prior_end
                except ValueError:
                    pass

            new_end = (base + timedelta(days=days)).strftime(SUBS_DATE_FORMAT)
            new_start = existing[1] if existing and existing[0] == "premium" and existing[1] else datetime.now().strftime(SUBS_DATE_FORMAT)

            if existing:
                cursor.execute(
                    "UPDATE main_chat_settings SET type = 'premium', subs_date_start = ?, subs_date_end = ?, chat_name = COALESCE(?, chat_name) WHERE chat_id = ?",
                    (new_start, new_end, chat_name, target_chat_id),
                )
            else:
                cursor.execute(
                    "INSERT INTO main_chat_settings (chat_id, chat_name, type, subs_date_start, subs_date_end) VALUES (?, ?, 'premium', ?, ?)",
                    (target_chat_id, chat_name, new_start, new_end),
                )
            conn.commit()
            await update.message.reply_text(
                f"✅ Subscription *on* for `{escape_markdown(target_chat_id)}` until `{escape_markdown(new_end)}`\\.",
                parse_mode="MarkdownV2",
            )
            await _push_control_sheet_main()
            return

    await update.message.reply_text(
        "❌ *Syntax:* `/setsub <chat_id> on <days>` or `/setsub <chat_id> off`",
        parse_mode="MarkdownV2",
    )


async def syncgroups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Owner-only. Manually pushes the current main_chat_settings and the
    free/premium feature matrix to the Control Sheet, without needing to
    change anyone's subscription first (e.g. right after setting up
    CONTROL_SHEET_ID for the first time).
    """
    if update.effective_user.id not in OWNER_USER_IDS:
        return

    await _push_control_sheet_main()
    await sync_control_sheet_subconfig(FEATURE_MATRIX)
    await update.message.reply_text(
        "✅ Control Sheet synced \\(Main \\+ sub\\_config\\)\\.", parse_mode="MarkdownV2"
    )
