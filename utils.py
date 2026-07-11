import re
from datetime import datetime

def escape_markdown(text):
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)

def now2ddmmyy():
    return datetime.now().strftime("%d.%m.%Y %H:%M:%S.%f")[:-3]