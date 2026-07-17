import logging
import os
import codecs
import json
from dotenv import load_dotenv

load_dotenv()

# Logger configuration
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Default UI icons with safe fallback configurations
DEFAULT_GOING_ICON = codecs.decode(os.getenv("DEFAULT_GOING_ICON", "✅"), "unicode_escape")
DEFAULT_NOTGOING_ICON = codecs.decode(os.getenv("DEFAULT_NOTGOING_ICON", "❌"), "unicode_escape")
DEFAULT_CLOSE_ICON = codecs.decode(os.getenv("DEFAULT_CLOSE_ICON", "🔴"), "unicode_escape")

TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
GLOBAL_DEFAULT_SHEET = os.getenv("GOOGLE_SHEET_NAME")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")
