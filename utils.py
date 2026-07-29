import re
from datetime import datetime

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
