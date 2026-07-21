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
DEFAULT_GOING_ICON = "✅"
DEFAULT_NOTGOING_ICON = "❌"
DEFAULT_CLOSE_ICON = "🔴"

TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
GLOBAL_DEFAULT_SHEET = os.getenv("GOOGLE_SHEET_NAME")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

# ---------------------------------------------------------------------------
# Static UI icons
# ---------------------------------------------------------------------------
# These are fixed (not env-configurable, unlike DEFAULT_GOING_ICON/
# DEFAULT_NOTGOING_ICON/DEFAULT_CLOSE_ICON above, which are per-event and
# meant to be customized). Centralized here purely so every place in the
# code that needs "the Kick icon" or "the warning icon" reads from one
# source instead of a bare emoji literal repeated at each call site.

# Verification-mode roster actions
ICON_KICK          = "❌"
ICON_RETURN        = "↩️"
ICON_PERSON         = "👤"   # master-hub participant row
ICON_CHANNEL_PERSON = "📢"   # child-chat/channel participant row
ICON_GUEST_MINUS    = " − "
ICON_GUEST_PLUS     = " + "

# Event lifecycle buttons
ICON_ADD            = "➕"   # also used for "Add Extra Player"
ICON_REMOVE         = "➖"
ICON_CANCEL_EVENT   = "🚫"
ICON_SAVE           = "💾"

# Message / status icons
ICON_SHARED         = "↪️"
ICON_STATS          = "📊"
ICON_WARNING        = "⚠️"
ICON_ERROR          = "❌"
ICON_CLOCK          = "🕐"   # event date/time display
ICON_NOTIFY         = "🔔"
ICON_CLEAN          = "🧹"   # /refreshusers removal summary
ICON_ADMIN_ONLY     = "⛔️"
ICON_GLOBE          = "🌍"   # /refreshusers -g global sync section
