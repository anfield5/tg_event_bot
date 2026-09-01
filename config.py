import logging
import os
from dotenv import load_dotenv

load_dotenv()

# Bumped manually on each meaningful release; also used as the git tag.
BOT_VERSION = "4.2.1"

# Logger configuration
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Default UI icons with safe fallback configurations
DEFAULT_GOING_ICON = "👍"
DEFAULT_NOTGOING_ICON = "❌"

TELEGRAM_TOKEN = os.getenv("BOT_TOKEN")
# Optional: set only if api.telegram.org is blocked/throttled on this network
# (common cause of httpx.ReadTimeout/TimedOut on the very first getMe() call,
# before any bot logic even runs). Examples:
#   TELEGRAM_PROXY=socks5://127.0.0.1:1080
#   TELEGRAM_PROXY=http://user:pass@host:port
# Leave unset if Telegram is directly reachable - most deployments don't need this.
TELEGRAM_PROXY = os.getenv("TELEGRAM_PROXY") or None

# One or more Telegram user_id's (plain integers, e.g. 123456789 - NOT chat
# ID's) allowed to run owner-only commands like /setsub. Comma-separated in
# .env, e.g. OWNER_USER_IDS=123456789,987654321
# Restricts owner-only commands: subscription control must NOT be gated by
# "is admin in this chat", since a group's own admin could just promote
# themselves and grant themselves a free subscription otherwise. Find your
# own user_id via @userinfobot on Telegram.
OWNER_USER_IDS = {
    int(uid) for uid in os.getenv("OWNER_USER_IDS", "").split(",") if uid.strip().isdigit()
}

# The single "Control" spreadsheet (separate from any hub's own event sheet)
# where the bot mirrors all_groups ("GROUPS" tab, one row per hub -
# for you to see every group using the bot and its subscription status at a
# glance) and the free/premium feature matrix ("BOTCONFIG" tab, static
# reference data). This sheet must be shared with the SAME service account
# (GOOGLE_CREDENTIALS_JSON) as every other sheet the bot writes to.
CONTROL_SHEET_ID = os.getenv("CONTROL_SHEET_ID") or None

# The service account credentials JSON (as a single-line string, not a file
# path) used to authenticate with the Google Sheets/Drive APIs. Required for
# any Sheets feature to work at all (Control Sheet, per-hub /setsheet export).
# Share every Sheet the bot should write to with this service account's
# client_email, with Editor access (see /setsub's reminder and /setsheet's
# access check).
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

# ---------------------------------------------------------------------------
# Static UI icons
# ---------------------------------------------------------------------------
# These are fixed (not env-configurable, unlike DEFAULT_GOING_ICON/
# DEFAULT_NOTGOING_ICON/DEFAULT_CLOSE_ICON above, which are per-event and
# meant to be customized). Centralized here purely so every place in the
# code that needs "the Kick icon" or "the warning icon" reads from one
# source instead of a bare emoji literal repeated at each call site.

ICON_GUEST = "⊕"   # marks "N guests, from: <username>" lines
ICON_STANDBY = "💤"   # marks a person waiting in the Waitlist/Standby queue
CLOSE_ICON = "🔴"
ICON_VERIFICATION = "🔄"   # used on the "Verify" button (open -> verification mode)

# Verification-mode roster actions
ICON_KICK          = "❌"
ICON_RETURN        = "↩️"
ICON_PERSON         = "👤"   # master-hub participant row
ICON_CHANNEL_PERSON = "📢"   # child-chat/channel participant row
ICON_GUEST_MINUS    = " - "
ICON_GUEST_PLUS     = " + "

# Event lifecycle buttons
ICON_ADD            = "+"   # also used for "Add Extra Member"
ICON_REMOVE         = "-"
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
ICON_GLOBE          = "🌍"   # /refreshusersall global sync section
ICON_PREMIUM        = "⚡"   # marks premium-only features (e.g. Aliases/Monitoring on /help)
ICON_TOTAL          = "🌐"   # marks the "Total Going" line in event posts
