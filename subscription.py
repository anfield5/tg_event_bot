"""
Subscription tiers (free/premium), owner-controlled manual activation, and
the Control Sheet sync that mirrors all_groups for visibility.

Payment itself is confirmed by hand (crypto, checked by the bot owner) -
there is deliberately no payment automation here.
"""

import re
import sqlite3
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import ICON_WARNING, ICON_STATS, OWNER_USER_IDS, logger
from utils import escape_markdown, is_real_admin, GROUP_ANONYMOUS_BOT_ID
from db import get_connection, get_feature_flags, update_feature_flag, _NO_CHANGE as _LIMIT_NO_CHANGE
from hub_resolver import resolve_hub_chat_id, register_hub_command
from sheets import (
    sync_control_sheet_main, sync_control_sheet_botconfig, sync_control_sheet_channels,
    open_spreadsheet, get_service_account_email,
)

SUBS_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"  # ISO-ish, chosen so string comparison
# isn't relied upon anywhere - always parsed via strptime, but kept
# unambiguous/sortable as a matter of hygiene for anyone reading the DB directly.


def is_premium(chat_id: str) -> bool:
    """
    True if this hub currently has an active premium subscription.
    Auto-expires: type='PRO' with a subs_date_end in the past is treated
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

    if not row or row[0] != "PRO" or not row[1]:
        return False
    try:
        return datetime.strptime(row[1], SUBS_DATE_FORMAT) > datetime.now()
    except ValueError:
        return False


def has_feature(chat_id: str, feature_key: str) -> bool:
    """
    General availability check against feature_flags, for anything beyond
    the plain PRO/FREE split that is_premium() covers - e.g. "verification"
    or "add_extra_member", which are independently configurable via
    set_feature_flag() even though they default to FREE.

    Only meaningful for FREE/PRO-tier features. A group's own subscription
    is never "ADMIN" (that tier is gated on OWNER_USER_IDS, the caller's own
    identity - unrelated to any group's subscription), so checking an
    ADMIN-tier feature_key here will always return False; that's correct,
    not a bug - use the OWNER_USER_IDS check directly for those instead.

    Unknown feature_key (typo, or not seeded) defaults to False rather than
    silently allowing everything - fail closed, not open.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT min_tier FROM feature_flags WHERE feature_key = ?", (feature_key,))
        row = cursor.fetchone()

    if not row:
        return False

    tier_order = {"FREE": 0, "PRO": 1, "ADMIN": 2}
    group_tier = "PRO" if is_premium(chat_id) else "FREE"
    return tier_order.get(group_tier, -1) >= tier_order.get(row[0], 99)


# feature_flags (db.get_feature_flags) is now the single source of truth
# for what's available at each tier - see set_feature_flag() below and
# _push_control_sheet_botconfig(), which mirrors it to the Control Sheet's
# "BOTCONFIG" tab every time it's called.


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
        is_anonymous = (
            update.effective_user.id == GROUP_ANONYMOUS_BOT_ID
            or getattr(update.message, "sender_chat", None) is not None
        )
        if is_anonymous:
            await update.message.reply_text(
                "⛔️ Owner\\-only commands can't be verified while posting anonymously \\- "
                "please disable \"Remain anonymous\" and try again\\.",
                parse_mode="MarkdownV2",
            )
        return  # otherwise silent - don't reveal this command exists to non-owners

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
                    "UPDATE all_groups SET type = 'FREE', chat_name = COALESCE(?, chat_name) WHERE chat_id = ?",
                    (chat_name, target_chat_id),
                )
            else:
                cursor.execute(
                    "INSERT INTO all_groups (chat_id, chat_name, type) VALUES (?, ?, 'FREE')",
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
            if existing and existing[0] == "PRO" and existing[1]:
                try:
                    prior_end = datetime.strptime(existing[1], SUBS_DATE_FORMAT)
                    if prior_end > base:
                        base = prior_end
                except ValueError:
                    pass

            new_end = (base + timedelta(days=days)).strftime(SUBS_DATE_FORMAT)
            new_start = existing[1] if existing and existing[0] == "PRO" and existing[1] else datetime.now().strftime(SUBS_DATE_FORMAT)

            if existing:
                cursor.execute(
                    "UPDATE all_groups SET type = 'PRO', subs_date_start = ?, subs_date_end = ?, chat_name = COALESCE(?, chat_name) WHERE chat_id = ?",
                    (new_start, new_end, chat_name, target_chat_id),
                )
            else:
                cursor.execute(
                    "INSERT INTO all_groups (chat_id, chat_name, type, subs_date_start, subs_date_end) VALUES (?, ?, 'PRO', ?, ?)",
                    (target_chat_id, chat_name, new_start, new_end),
                )
            conn.commit()
            reminder = ""
            sa_email = get_service_account_email()
            if sa_email:
                reminder = (
                    f"\n\n💡 To let this group use /setsheet, have them share their Google Sheet "
                    f"with `{escape_markdown(sa_email)}` \\(Editor access\\)\\."
                )
            await update.message.reply_text(
                f"✅ Subscription *on* for `{target_chat_id}` until `{new_end}`\\.{reminder}",
                parse_mode="MarkdownV2",
            )
            await _push_control_sheet_main()
            return

    await update.message.reply_text(
        "❌ *Syntax:* `/setsub <chat_id> on <days>` or `/setsub <chat_id> off`",
        parse_mode="MarkdownV2",
    )


@register_hub_command("setsheet")
async def setsheet(update: Update, context: ContextTypes.DEFAULT_TYPE, override_chat_id: str = None):
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
    chat_id = await resolve_hub_chat_id(update, context, "setsheet", override_chat_id)
    if chat_id is None:
        return

    if not is_premium(chat_id):
        await update.message.reply_text(
            f"{ICON_WARNING} /setsheet is a PRO\\-only feature\\. "
            f"Use /setsub info or contact the bot owner to upgrade\\.",
            parse_mode="MarkdownV2",
        )
        return

    admin_ok = await is_real_admin(
        context.bot, chat_id, update.effective_user, message=update.message
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

    # Opening/reading the title above only confirms VIEW access - a
    # Viewer-only share would ALSO succeed there, then fail on the first
    # real write later (e.g. appending to Events). Round-trip A1 on the
    # first worksheet to actually test Editor access: read it, write the
    # SAME value back. Safe even if something goes wrong mid-way (worst
    # case A1 gets re-written with its own original value).
    edit_access_confirmed = True
    try:
        first_ws = await ss.sheet1
        current_a1 = await first_ws.acell("A1")
        await first_ws.update_acell("A1", current_a1.value or "")
    except Exception as e:
        logger.error(f"setsheet: edit-access probe failed for {sheet_id}: {e}")
        edit_access_confirmed = False

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

    warning = ""
    if not edit_access_confirmed:
        sa_email = get_service_account_email()
        email_note = f" \\(`{escape_markdown(sa_email)}`\\)" if sa_email else ""
        warning = (
            f"\n\n{ICON_WARNING} Could only confirm *view* access, not *edit* \\- writes to this sheet "
            f"will likely fail\\. Make sure the bot's service account{email_note} has *Editor* access, "
            f"not just Viewer\\."
        )

    await update.message.reply_text(
        f"✅ This group is now bound to `{sheet_name}`\\.{warning}",
        parse_mode="MarkdownV2",
    )
    await _push_control_sheet_main()


@register_hub_command("status")
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE, override_chat_id: str = None):
    """
    Shows this hub's current subscription tier, when it expires (if PRO),
    and which Google Sheet it's bound to (if any). Read-only - just a quick
    way to check settings without hunting through other commands or the
    Control Sheet (which only the bot owner can see).
    """
    chat_id = await resolve_hub_chat_id(update, context, "status", override_chat_id)
    if chat_id is None:
        return

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT type, subs_date_end, sheet_id, sheet_name FROM all_groups WHERE chat_id = ?",
            (chat_id,),
        )
        row = cursor.fetchone()

    pro = is_premium(chat_id)
    type_line = "PRO" if pro else "FREE"

    if pro:
        due_date = row[1] if row and row[1] else "unknown"
        due_line = escape_markdown(due_date)
    else:
        due_line = "unlimited"

    lines = [
        f"{ICON_STATS} *Status*",
        "",
        f"Type: {type_line}",
        f"Due Date: {due_line}",
    ]
    if pro:
        sheet_line = escape_markdown(row[3]) if row and row[2] and row[3] else "not bound \\- see /setsheet"
        lines.append(f"Sheet: {sheet_line}")

    await update.message.reply_text("\n".join(lines), parse_mode="MarkdownV2")


# ---------------------------------------------------------------------------
# Owner-only: browse every group/channel the bot is in
# ---------------------------------------------------------------------------

_PAGE_SIZE = 10


def _paginate_groups_text(rows, page: int):
    """
    rows: list of (chat_id, chat_name, type, visibility, sheet_name,
    owner_group_id) tuples, already filtered/sorted by the caller. Returns
    (text, has_prev, has_next) for the given 0-indexed page.
    """
    start = page * _PAGE_SIZE
    page_rows = rows[start:start + _PAGE_SIZE]

    blocks = []
    for chat_id, chat_name, chat_type, visibility, sheet_name, owner_group_id in page_rows:
        vis_line = "visible" if visibility == "public" else "hidden"
        owner_line = escape_markdown(owner_group_id) if owner_group_id else "none"
        sheet_line = escape_markdown(sheet_name) if sheet_name else "none"
        blocks.append(
            f"id\\_group: {escape_markdown(chat_id)}\n"
            f"group name: {escape_markdown(chat_name or 'unknown')}\n"
            f"subscription\\_status: {chat_type}\n"
            f"visibility: {vis_line}\n"
            f"owner\\_group\\_id: {owner_line}\n"
            f"sheetname: {sheet_line}"
        )
    text = "\n\n".join(blocks) if blocks else "No groups found\\."

    has_prev = page > 0
    has_next = start + _PAGE_SIZE < len(rows)
    return text, has_prev, has_next


def _paginate_channels_text(rows, page: int):
    """rows: list of (chat_id, chat_name, visibility, owner_group_id) tuples."""
    start = page * _PAGE_SIZE
    page_rows = rows[start:start + _PAGE_SIZE]

    blocks = []
    for chat_id, chat_name, visibility, owner_group_id in page_rows:
        vis_line = "visible" if visibility == "public" else "hidden"
        owner_line = escape_markdown(owner_group_id) if owner_group_id else "none"
        blocks.append(
            f"id\\_channel: {escape_markdown(chat_id)}\n"
            f"channel name: {escape_markdown(chat_name or 'unknown')}\n"
            f"visibility: {vis_line}\n"
            f"owner\\_group\\_id: {owner_line}"
        )
    text = "\n\n".join(blocks) if blocks else "No channels found\\."

    has_prev = page > 0
    has_next = start + _PAGE_SIZE < len(rows)
    return text, has_prev, has_next


def _pagination_keyboard(has_prev: bool, has_next: bool, callback_prefix: str, page: int):
    if not has_prev and not has_next:
        return None
    row = []
    if has_prev:
        row.append(InlineKeyboardButton("◀️ Prev", callback_data=f"{callback_prefix}_{page - 1}"))
    if has_next:
        row.append(InlineKeyboardButton("Next ▶️", callback_data=f"{callback_prefix}_{page + 1}"))
    return InlineKeyboardMarkup([row])


async def allgroups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Owner-only. Lists every group the bot is currently in (all_groups),
    10 at a time with Prev/Next buttons. /allgroups -pro shows only PRO
    groups.
    """
    if update.effective_user.id not in OWNER_USER_IDS:
        return

    pro_only = bool(context.args) and context.args[0].strip().lower() in ("-pro", "--pro")

    with get_connection() as conn:
        cursor = conn.cursor()
        if pro_only:
            cursor.execute(
                "SELECT g.chat_id, g.chat_name, g.type, g.visibility, g.sheet_name, "
                "(SELECT owner_chat_id FROM sub_chats WHERE chat_id = g.chat_id LIMIT 1) "
                "FROM all_groups g WHERE g.type = 'PRO' ORDER BY g.chat_id"
            )
        else:
            cursor.execute(
                "SELECT g.chat_id, g.chat_name, g.type, g.visibility, g.sheet_name, "
                "(SELECT owner_chat_id FROM sub_chats WHERE chat_id = g.chat_id LIMIT 1) "
                "FROM all_groups g ORDER BY g.chat_id"
            )
        rows = cursor.fetchall()

    prefix = "allgroupspro" if pro_only else "allgroups"
    text, has_prev, has_next = _paginate_groups_text(rows, 0)
    keyboard = _pagination_keyboard(has_prev, has_next, prefix, 0)
    await update.message.reply_text(text, parse_mode="MarkdownV2", reply_markup=keyboard)


async def allgroups_page_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the Prev/Next buttons under /allgroups (and /allgroups -pro)."""
    query = update.callback_query
    await query.answer()

    if update.effective_user.id not in OWNER_USER_IDS:
        return

    prefix, page_str = query.data.rsplit("_", 1)
    page = int(page_str)
    pro_only = prefix == "allgroupspro"

    with get_connection() as conn:
        cursor = conn.cursor()
        if pro_only:
            cursor.execute(
                "SELECT g.chat_id, g.chat_name, g.type, g.visibility, g.sheet_name, "
                "(SELECT owner_chat_id FROM sub_chats WHERE chat_id = g.chat_id LIMIT 1) "
                "FROM all_groups g WHERE g.type = 'PRO' ORDER BY g.chat_id"
            )
        else:
            cursor.execute(
                "SELECT g.chat_id, g.chat_name, g.type, g.visibility, g.sheet_name, "
                "(SELECT owner_chat_id FROM sub_chats WHERE chat_id = g.chat_id LIMIT 1) "
                "FROM all_groups g ORDER BY g.chat_id"
            )
        rows = cursor.fetchall()

    text, has_prev, has_next = _paginate_groups_text(rows, page)
    keyboard = _pagination_keyboard(has_prev, has_next, prefix, page)
    await query.edit_message_text(text, parse_mode="MarkdownV2", reply_markup=keyboard)


async def allchannels_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner-only. Lists every channel the bot is currently in (all_channels), 10 at a time."""
    if update.effective_user.id not in OWNER_USER_IDS:
        return

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT c.chat_id, c.chat_name, c.visibility, "
            "(SELECT owner_chat_id FROM sub_chats WHERE chat_id = c.chat_id LIMIT 1) "
            "FROM all_channels c ORDER BY c.chat_id"
        )
        rows = cursor.fetchall()

    text, has_prev, has_next = _paginate_channels_text(rows, 0)
    keyboard = _pagination_keyboard(has_prev, has_next, "allchannels", 0)
    await update.message.reply_text(text, parse_mode="MarkdownV2", reply_markup=keyboard)


async def allchannels_page_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the Prev/Next buttons under /allchannels."""
    query = update.callback_query
    await query.answer()

    if update.effective_user.id not in OWNER_USER_IDS:
        return

    page = int(query.data.rsplit("_", 1)[1])

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT c.chat_id, c.chat_name, c.visibility, "
            "(SELECT owner_chat_id FROM sub_chats WHERE chat_id = c.chat_id LIMIT 1) "
            "FROM all_channels c ORDER BY c.chat_id"
        )
        rows = cursor.fetchall()

    text, has_prev, has_next = _paginate_channels_text(rows, page)
    keyboard = _pagination_keyboard(has_prev, has_next, "allchannels", page)
    await query.edit_message_text(text, parse_mode="MarkdownV2", reply_markup=keyboard)


async def _push_control_sheet_botconfig() -> bool:
    """Reads all of feature_flags and pushes it to the Control Sheet's 'BOTCONFIG' tab."""
    rows = get_feature_flags()
    return await sync_control_sheet_botconfig(rows)


async def set_feature_flag(feature_key: str, min_tier: str,
                            limit_free=_LIMIT_NO_CHANGE, limit_pro=_LIMIT_NO_CHANGE, limit_admin=_LIMIT_NO_CHANGE) -> bool:
    """
    THE way to change what tier a feature requires (and optionally any of
    its per-tier limits) - writes to feature_flags (db.update_feature_flag)
    and then immediately re-syncs the Control Sheet's BOTCONFIG tab, so it
    can never drift out of date with the table. Powers /updatefeaturelevel.

    min_tier must be one of "FREE", "PRO", "ADMIN". Each limit_* is
    independent: omit to leave that tier's current limit untouched, pass
    None to clear it (unlimited), or an int to set/change it.
    """
    update_feature_flag(feature_key, min_tier, limit_free=limit_free, limit_pro=limit_pro, limit_admin=limit_admin)
    return await _push_control_sheet_botconfig()


def _tier_limit_is_sane(lower_tier_limit, higher_tier_limit) -> bool:
    """
    True if the lower tier's limit is at least as restrictive as the
    higher tier's (the normal, expected relationship - a higher tier
    should never be MORE restricted than a lower one). None means
    unlimited (the least restrictive possible value).

    Soft-check only - never blocks the command, just flags a heads-up in
    the reply. The owner might genuinely want an unusual setup on
    purpose (e.g. a temporary promo), so this never overrides that.
    """
    if lower_tier_limit is None and higher_tier_limit is not None:
        return False  # lower tier unlimited, higher tier capped - inverted
    if lower_tier_limit is not None and higher_tier_limit is not None:
        return lower_tier_limit <= higher_tier_limit
    return True  # both unlimited, or lower capped + higher unlimited - both fine


async def updatefeaturelevel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Owner-only. Changes which tier a feature requires, and optionally any
    of its per-tier usage limits - the live command powering
    set_feature_flag().

    Usage: /updatefeaturelevel <feature_key> <free|pro|admin>
               [-limitfree N] [-limitpro N] [-limitadmin N]
      /updatefeaturelevel shareevent free -limitfree 5   - FREE capped at 5, PRO/ADMIN untouched
      /updatefeaturelevel shareevent free -limitpro 0    - clear PRO's limit (unlimited)
      /updatefeaturelevel aliases admin                  - ADMIN-only, no limits touched

    Each tier's limit is fully independent - e.g. FREE can be capped at 3
    while PRO stays unlimited, or vice versa. 0 clears that specific
    tier's limit; omitting a flag leaves that tier's limit untouched.

    Same OWNER_USER_IDS gating as /setsub, not chat admin status.
    """
    if update.effective_user.id not in OWNER_USER_IDS:
        is_anonymous = (
            update.effective_user.id == GROUP_ANONYMOUS_BOT_ID
            or getattr(update.message, "sender_chat", None) is not None
        )
        if is_anonymous:
            await update.message.reply_text(
                "⛔️ Owner\\-only commands can't be verified while posting anonymously \\- "
                "please disable \"Remain anonymous\" and try again\\.",
                parse_mode="MarkdownV2",
            )
        return  # otherwise silent - don't reveal this command exists to non-owners

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "❌ *Syntax:* `/updatefeaturelevel <feature_key> <free|pro|admin> "
            "[\\-limitfree N] [\\-limitpro N] [\\-limitadmin N]`\n"
            "Each tier's limit is independent\\. `0` clears that tier's limit \\(unlimited\\)\\.",
            parse_mode="MarkdownV2",
        )
        return

    feature_key = args[0]
    level_raw   = args[1].strip().lower()
    level_map   = {"free": "FREE", "pro": "PRO", "admin": "ADMIN"}
    if level_raw not in level_map:
        await update.message.reply_text(
            "❌ Level must be one of `free`, `pro`, `admin`\\.", parse_mode="MarkdownV2"
        )
        return
    min_tier = level_map[level_raw]

    limit_kwargs = {}
    for flag_name, kwarg_name in (("-limitfree", "limit_free"), ("-limitpro", "limit_pro"), ("-limitadmin", "limit_admin")):
        if flag_name in args:
            idx = args.index(flag_name)
            if idx + 1 >= len(args) or not args[idx + 1].lstrip("-").isdigit():
                await update.message.reply_text(
                    f"❌ `{escape_markdown(flag_name)}` must be followed by a number \\(0 clears that tier's limit\\)\\.",
                    parse_mode="MarkdownV2",
                )
                return
            n = int(args[idx + 1])
            limit_kwargs[kwarg_name] = None if n == 0 else n

    existing = get_feature_flags()
    if feature_key not in {row[0] for row in existing}:
        await update.message.reply_text(
            f"🔍 Unknown feature\\_key `{escape_markdown(feature_key)}`\\. "
            f"Check `BOTCONFIG` in the Control Sheet for valid keys\\.",
            parse_mode="MarkdownV2",
        )
        return

    current_row = next(r for r in existing if r[0] == feature_key)
    final_limit_free  = limit_kwargs.get("limit_free", current_row[3])
    final_limit_pro   = limit_kwargs.get("limit_pro", current_row[4])
    final_limit_admin = limit_kwargs.get("limit_admin", current_row[5])

    inversions = []
    if not _tier_limit_is_sane(final_limit_free, final_limit_pro):
        inversions.append("FREE ends up less restricted than PRO")
    if not _tier_limit_is_sane(final_limit_pro, final_limit_admin):
        inversions.append("PRO ends up less restricted than ADMIN")

    ok = await set_feature_flag(feature_key, min_tier, **limit_kwargs)

    limit_notes = []
    for kwarg_name, label in (("limit_free", "free"), ("limit_pro", "pro"), ("limit_admin", "admin")):
        if kwarg_name in limit_kwargs:
            val = limit_kwargs[kwarg_name]
            limit_notes.append(f"{label}={'none' if val is None else val}")
    limit_note = f", limits: {', '.join(limit_notes)}" if limit_notes else ""
    status_icon = "✅" if ok else f"{ICON_WARNING}"
    sync_note = "" if ok else " \\(BOTCONFIG sync failed \\- check CONTROL\\_SHEET\\_ID/permissions\\)"
    inversion_note = ""
    if inversions:
        inversion_note = (
            f"\n\n{ICON_WARNING} Heads up: {escape_markdown(', '.join(inversions))}\\. "
            f"A higher tier is usually meant to be the same or better, not worse \\- double\\-check this is intentional\\."
        )
    await update.message.reply_text(
        f"{status_icon} `{escape_markdown(feature_key)}` set to *{min_tier}*{escape_markdown(limit_note)}{sync_note}\\.{inversion_note}",
        parse_mode="MarkdownV2",
    )
