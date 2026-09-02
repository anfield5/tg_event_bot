import re
from datetime import datetime

# ---------------------------------------------------------------------------
# Command destination classification
# ---------------------------------------------------------------------------
# Every command falls into exactly one of these 3 categories, describing
# WHERE its result lands relative to WHERE it was typed (group directly,
# or a DM with the bot) - a separate concern from whether a command is
# admin/owner-gated.
#
#   1 - dual-callable: result goes wherever the command was typed (a
#       group call replies in the group, a DM call replies in the DM).
#       This is the default for the vast majority of commands - not
#       every key needs to be listed explicitly, only 2 and 3.
#   2 - dual-callable: the SUBSTANTIVE result always lands in the hub
#       group regardless of where the command was typed (e.g. the
#       /newevent post itself, or /notify's actual ping - pinging
#       people from inside a DM wouldn't reach them where they need to
#       respond). A DM caller may still get a brief separate
#       confirmation in their own DM, but the main output goes to the
#       group either way.
#   3 - DM only: calling this from a group is rejected with an
#       explicit error (see require_dm_only below), not silently
#       ignored.
COMMAND_DESTINATION_TYPE = {
    # Type 2 - result always in the group
    "newevent":  2,
    "editevent": 2,
    "shareevent": 2,
    "notify":    2,
    # Type 3 - DM only
    "switchgroup":    3,
    "start":          3,
    "lockbot":        3,
    "allgroups":      3,
    "allchannels":    3,
    "updatefeature":  3,
    "setsub":         3,
    "setsheet":       3,
    "showtable":      3,
    # Everything else not listed here is Type 1 (the default) - e.g.
    # adduser, updateuser, listusers, refreshusers, refreshusersall,
    # help, waitlist, userid, chatid, setalias, removealias,
    # listaliases, addmonitor, removemonitor, listmonitors, status,
    # stats.
}


def get_admin_contact() -> tuple:
    """
    Returns (button_label, url) for messaging the bot owner directly -
    the single source of this contact info. Used both when a hub taps
    a locked /help section (help_system.py's upgrade_info flow) and
    when the bot is locked via /lockbot and a non-owner wants to know
    more (main.py's lock_gate) - one place to update if this ever
    changes, instead of two copies quietly drifting apart.
    """
    return ("💬 Message the bot owner", "https://t.me/anefex")


async def require_owner(update, owner_ids) -> bool:
    """
    Owner-only command gating: returns True if the caller is in
    owner_ids. If not, shows an explicit message when the sender is
    genuinely anonymous (their identity can't be verified either way,
    so "please disable Remain anonymous and try again" is the only
    actionable thing to tell them) - anyone else who isn't an owner
    gets total silence, so the command's existence isn't revealed to
    non-owners. Pass OWNER_USER_IDS as owner_ids (not imported directly
    here to avoid a config.py <-> utils.py import cycle).
    """
    if update.effective_user.id in owner_ids:
        return True
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
    return False


async def require_dm_only(update, command_name: str) -> bool:
    """
    Type 3 enforcement: call at the very start of a DM-only command.
    Returns True if the caller may proceed (already in a DM), False if
    an explicit error was shown because they called from a group -
    unlike the pre-existing convention elsewhere in this codebase of
    silently ignoring a command in the wrong context, DM-only commands
    ARE expected to explain why nothing happened.
    """
    if update.effective_chat.type == "private":
        return True
    await update.message.reply_text(
        f"⛔️ /{command_name} only works in a DM with the bot, not inside a group\\. "
        f"Message the bot directly instead\\.",
        parse_mode="MarkdownV2",
    )
    return False


def escape_markdown(text):
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', str(text))

def now2ddmmyy():
    return datetime.now().strftime("%d.%m.%Y %H:%M:%S.%f")[:-3]

# Supported date input formats for -date flag in /newevent and /editevent.
# Users may type: 14.07.2026  or  14.07.2026 19:00
DATE_FORMATS = ["%d.%m.%Y %H:%M", "%d.%m.%Y"]

def parse_event_date(date_str: str):
    """
    Tries to parse a user-supplied date string using DATE_FORMATS.
    Returns the normalized string (same format as input) or None if invalid.
    """
    if not date_str:
        return None
    for fmt in DATE_FORMATS:
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            return dt.strftime(fmt)   # normalise (e.g. trim extra spaces)
        except ValueError:
            continue
    return None


# Telegram's special pseudo-account used as the message sender whenever an
# admin posts with "Remain anonymous" enabled - a per-group admin setting,
# not something specific to this bot. This ID is a Telegram platform
# constant, the same across every bot and every group.
GROUP_ANONYMOUS_BOT_ID = 1087968824


async def is_real_admin(bot, chat_id, user, message=None) -> bool:
    """
    True if `user` is an administrator/creator of `chat_id`.

    Handles the anonymous-admin case: when a group admin has "Remain
    anonymous" turned on, Telegram substitutes the message's sender with
    the GROUP_ANONYMOUS_BOT_ID pseudo-account instead of the admin's real
    user_id. A plain get_chat_member(chat_id, user.id) call would then
    return status=LEFT for that pseudo-account (it isn't a real member),
    incorrectly rejecting a genuine admin. Since Telegram itself only lets
    admins/creators post anonymously in the first place, seeing that
    pseudo-account (or a message with `sender_chat` set) is trusted as
    "yes, an admin sent this" without needing to query get_chat_member at all.
    """
    if user and user.id == GROUP_ANONYMOUS_BOT_ID:
        return True
    if message is not None and getattr(message, "sender_chat", None) is not None:
        return True
    try:
        member = await bot.get_chat_member(chat_id, user.id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False
