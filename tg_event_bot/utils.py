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
