"""
The /help system and related diagnostic commands - split out of handlers.py
since this is self-contained (only depends on config/utils/subscription/
hub_resolver, never touches the event-rendering engine) and was one of the
largest coherent chunks of that file.

Covers: /userid, /chatid, /help (including the owner-only "-a" variant),
the help_* callback handlers (section drill-down, back button), and the
upgrade-info screen shown when a free-tier hub taps a locked PRO button.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import (
    ICON_PREMIUM, OWNER_USER_IDS, ICON_ADD, ICON_CANCEL_EVENT,
    ICON_KICK, ICON_RETURN, ICON_SAVE, ICON_VERIFICATION,
)
from subscription import is_premium, has_feature
from hub_resolver import _get_known_candidate_chats
from db import get_feature_flags, get_shareevent_remaining_for_chat
from utils import escape_markdown


async def _help_target_chat_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Which chat's premium status should gate the Aliases/Monitoring help
    buttons. In a group, that's the group itself. In a DM:
      - Use whichever group is currently "stuck" for this conversation
        (see hub_resolver.py) if one has already been selected.
      - Otherwise, look up admin groups the same way other DM commands do.
        If there's exactly one, use it AND remember it as the selection
        (so /help behaves consistently whether or not it's the first
        command run in the conversation). With zero or multiple matches,
        fall back to the DM's own id (never premium) rather than forcing
        a picker here - a help menu shouldn't demand a group choice.
    """
    chat = update.effective_chat
    if chat.type != "private":
        return chat.id

    selected = context.user_data.get("selected_hub_chat_id")
    if selected is not None:
        return selected

    admin_of = await _get_known_candidate_chats(context, update.effective_user.id)
    if len(admin_of) == 1:
        context.user_data["selected_hub_chat_id"] = admin_of[0][0]
        return admin_of[0][0]

    return chat.id


# Which feature_flags key(s) gate each /help section button. A button is
# shown locked if ANY of its features aren't accessible to the calling
# chat - "Utility" maps to nothing since status/switchgroup/userid/chatid
# are never tier-gated (see the earlier decision to keep them out of
# feature_flags entirely, matching /userid/chatid/help/start).
_BUTTON_FEATURE_MAP = {
    "lifecycle":     ["newevent", "editevent", "verification", "add_extra_member", "event_limit"],
    "distribution":  ["shareevent"],
    "users":         ["user_management"],
    "utility":       [],
    "aliases":       ["aliases"],
    "monitoring":    ["monitoring", "refreshusersall"],
    "dm_access":     ["dm_access"],
}

_BUTTON_LABELS = {
    "users":        ("👥", "Users", "help_users"),
    "utility":      ("🔧", "Utility", "help_utility"),
    "lifecycle":    ("🗳", "Event Lifecycle", "help_lifecycle"),
    "distribution": ("📢", "Distribution", "help_distribution"),
    "aliases":      ("⚙️", "Aliases", "help_alias"),
    "monitoring":   ("🔍", "Monitoring", "help_monitoring"),
    "dm_access":    ("💬", "DM Access", "help_dm_access"),
}


def _build_main_help_keyboard(chat_id) -> InlineKeyboardMarkup:
    """
    Every /help section button is tier-aware now, not just Aliases/
    Monitoring - if ANY feature backing a button (_BUTTON_FEATURE_MAP)
    isn't accessible to this chat, the button shows ICON_PREMIUM and
    routes to upgrade_info_<button_key> (see upgrade_info_callback_handler)
    instead of its normal detail section - an actual next step rather
    than a dead end.
    """
    def _make_button(button_key: str) -> InlineKeyboardButton:
        icon, label, normal_callback = _BUTTON_LABELS[button_key]
        features = _BUTTON_FEATURE_MAP[button_key]
        locked = any(not has_feature(chat_id, fk) for fk in features)
        text = f"{icon} {label} {ICON_PREMIUM}" if locked else f"{icon} {label}"
        callback = f"upgrade_info_{button_key}" if locked else normal_callback
        return InlineKeyboardButton(text, callback_data=callback)

    return InlineKeyboardMarkup([
        [_make_button("users"), _make_button("utility")],
        [_make_button("lifecycle"), _make_button("distribution")],
        [_make_button("aliases"), _make_button("monitoring")],
        [_make_button("dm_access")],
    ])


async def userid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Replies with the caller's own numeric Telegram user_id - the exact
    value to put in OWNER_USER_IDS (.env) to grant owner-only command
    access. A user_id isn't sensitive/secret, so this is safe for anyone
    to run.
    """
    await update.message.reply_text(
        f'Your user id: "{update.effective_user.id}"',
    )


async def chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Replies with the CURRENT chat's numeric ID - the exact value to pass to
    /setsub, /addmonitor, etc. A chat_id isn't sensitive/secret, so this is
    safe for anyone in the group/channel to run.
    """
    await update.message.reply_text(
        f"This chat's ID: {update.effective_chat.id}",
    )


def _build_main_help_text(pro: bool, has_event_limit: bool = False) -> str:
    """
    The one and only source of the main /help text - both help_command
    (direct /help) and help_back_handler (the "Back" button from a detail
    section) call this, so the two can never drift apart again the way
    they already have twice (missing -d/-date, then missing -gi/-ni).

    has_event_limit controls whether /waitlist is shown - it lives right
    after -limit's own description (not in Distribution, where a group
    that can't even set -limit would see a dead-end command).
    """
    waitlist_line = "/waitlist \\- Show the Waitlist for the latest event \\(hub sees everyone with `from <chat>`, a child chat sees only its own\\)\n" if has_event_limit else ""
    text = (
        "📖 *Main Commands*\n\n"
        "/newevent \\[name\\] \\[\\-d dd\\.mm\\.yyyy \\[HH:MM\\]\\]\\[\\-gi \\<emoji\\>\\]\\[\\-ni \\<emoji\\>\\]"
        "\\[\\-limit N\\]\\[\\-wl visible\\|hidden\\|onlycount\\]\\[\\-ngl visible\\|hidden\\|onlycount\\] \\- Create a new event\n"
        "\\-d \\| \\-date dd\\.mm\\.yyyy \\[HH:MM\\] \\- Event date \\(and optional time\\)\n"
        "\\-gi \\| \\-goingicon \\<emoji\\> \\- Custom Going icon\n"
        "\\-ni \\| \\-notgoingicon \\<emoji\\> \\- Custom Not Going icon\n"
        "\\-limit N \\- caps going\\+guests across the whole event; once full, new Going clicks join the Waitlist instead\\. Requires a higher tier\\.\n"
        "\\-wl \\| \\-waitlist visible\\|hidden\\|onlycount \\- Waitlist visibility in the post \\(independent of \\-limit, can be set/changed on its own\\)\n"
        "    visible \\- hub's post shows everyone across every chat; a child chat's post shows only its own\n"
        "    hidden \\(default\\) \\- nothing shown in the post, admin\\-only via /waitlist\n"
        "    onlycount \\- shows just the total count, no names\n"
        "\\-ngl \\| \\-notgoinglist visible\\|hidden\\|onlycount \\- Not Going list visibility in the post \\(same 3 modes, ungated at every tier\\)\n"
        "    visible \\(default\\) \\- shown as before\n"
        "    hidden \\- section removed from the post entirely\n"
        "    onlycount \\- shows just the total count, no names\n"
        f"{waitlist_line}"
        "/editevent \\[name\\] \\[\\-d dd\\.mm\\.yyyy \\[HH:MM\\]\\]\\[\\-limit N\\]\\[\\-wl visible\\|hidden\\|onlycount\\]"
        "\\[\\-ngl visible\\|hidden\\|onlycount\\] \\- Edit the active event \\(same flags as /newevent, only what's given is changed\\)\n"
    )
    if pro:
        text += "/setsheet \\[sheetid\\|sheeturl\\] \\- Bind this group to its own Google Sheet \\(Users/Events/Actions/EventUsers/UserPresenceLog tabs\\)\n"
        text += "sheetid\\|sheeturl \\- either the raw spreadsheet ID, or a full Google Sheets URL \\(the ID is extracted automatically\\)\n"
        text += "/stats \\- Event activity stats for this group \\(events created, closed, total/average headcount\\)\n"
    text += "\n📚 *More Info*"
    return text


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_owner_request = bool(context.args) and context.args[0].strip().lower() in ("-a", "--admin", "--owner")
    if is_owner_request and update.effective_user.id in OWNER_USER_IDS:
        owner_help = (
            "🔑 *Owner\\-Only Commands*\n\n"
            "/setsub \\[chat\\_id\\] on \\[days\\] \\- Activate/extend PRO for a group\n"
            "/setsub \\[chat\\_id\\] off \\- Deactivate PRO for a group immediately\n"
            "/allgroups \\[\\-pro\\] \\- List every group the bot is in, 10 at a time\n"
            "/allchannels \\- List every channel the bot is in, 10 at a time\n"
            "/updatefeature \\[feature\\_key\\] \\[\\-minlevel free\\|pro\\|admin\\] \\[\\-limit N\\] "
            "\\- Change a feature's tier and/or its usage limit\\. At least one of the two flags is required\\.\n"
            "\\-limit N \\- the cap that applies only while a group is exactly at that feature's tier "
            "\\(any tier above is always unlimited\\)\\. `0` clears it \\(unlimited\\)\\.\n"
            "If \\-limit is omitted: the existing limit is kept if the tier didn't change, or reset to "
            "unlimited if it did\\.\n\n"
            "These are gated on your personal Telegram user\\_id \\(OWNER\\_USER\\_IDS\\), "
            "not on chat admin status \\- posting anonymously \\(as the group/channel itself\\) "
            "can't be verified and will be rejected\\."
        )
        await update.message.reply_text(owner_help, parse_mode="MarkdownV2")
        return

    chat_id_for_help = await _help_target_chat_id(update, context)
    pro = is_premium(chat_id_for_help)
    has_event_limit = has_feature(chat_id_for_help, "event_limit")
    main_help = _build_main_help_text(pro, has_event_limit)

    keyboard = _build_main_help_keyboard(chat_id_for_help)
    await update.message.reply_text(main_help, parse_mode="MarkdownV2", reply_markup=keyboard)


async def help_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    # Defensive: these two sections are premium-only. The free-tier keyboard
    # never sends these callback_data values in the first place (it sends
    # "upgrade_info" instead), but re-check here too in case the tier changed
    # between the button being shown and being tapped.
    if query.data in ("help_alias", "help_monitoring", "help_dm_access") and not is_premium(await _help_target_chat_id(update, context)):
        await query.answer("This section is PRO-only.", show_alert=True)
        return

    await query.answer()

    hub_chat_id_for_limits = await _help_target_chat_id(update, context)
    shareevent_limit, shareevent_remaining = get_shareevent_remaining_for_chat(hub_chat_id_for_limits)
    if shareevent_limit is not None:
        shareevent_limit_line = (
            f"FREE hubs can share to the same target before being blocked "
            f"\\(limit {shareevent_limit}, remaining {shareevent_remaining}\\) \\- a PRO/ADMIN\\-gated "
            f"hub is always unlimited\\. The limit is adjustable via /updatefeature\\."
        )
    else:
        shareevent_limit_line = "Currently unlimited for every tier \\(adjustable via /updatefeature\\)\\."

    help_sections = {
        "help_users": (
            "👥 *User Management*\n\n"
            "/adduser \\[user\\_id\\|username\\] \\[\\.\\.\\.\\] \\- Manually add users to tracked list\n"
            "username only resolves if they're an admin of this chat or have already interacted with the bot\n"
            "/listusers \\- Show all tracked users\n"
            "/updateuser \\[username\\(s\\)\\] \\-a\\|\\-p \\- Mark tracked users active or passive\n"
            "\\-a \\| \\-active \\- Mark as active\n"
            "\\-p \\| \\-passive \\- Mark as passive\n"
            "/notify \\- Ping users who haven't responded\n"
            "/refreshusers \\- Sync user list, Google Sheets, and remove unverifiable users for THIS group"
        ),
        "help_utility": (
            "🔧 *Utility Commands*\n\n"
            "/status \\- Show this group's subscription type\\, due date\\, and bound sheet\n"
            "/switchgroup \\- \\(DM only\\) switch which group your commands target\n"
            "/userid \\- Show your own Telegram user ID\n"
            "/chatid \\- Show this chat's ID"
        ),
        "help_alias": (
            "⚙️ *Aliases*\n\n"
            "/setalias \\[target\\_id\\] \\[aliasname\\] \\- Bind alias to chat ID\n"
            "/removealias \\[aliasname\\] \\- Remove alias\n"
            "/listaliases \\- Show all aliases\n\n"
            "Aliases let you use memorable names instead of numeric chat IDs when sharing events"
        ),
        "help_distribution": (
            "📢 *Distribution Control*\n\n"
            "/shareevent \\[target\\_alias/chatid\\] \\[\\-mgl visible\\|hidden\\|onlycount\\] "
            "\\[\\-sngl visible\\|hidden\\|onlycount\\] \\[\\-swl visible\\|hidden\\|onlycount\\] \\- Share active event\n\n"
            "\\-mgl \\| \\-maingoinglist \\- Going list visibility in this specific share \\(defaults to onlycount\\):\n"
            "  • visible: Show full going list in child chat\n"
            "  • hidden: Hide event, only show going/notgoing counts\n"
            "  • onlycount \\(default\\): Show only total going count\n\n"
            "\\-sngl \\| \\-sharenotgoing \\- Not Going list visibility override for this share \\(if omitted, inherits the event's own \\-ngl setting\\)\n"
            "\\-swl \\| \\-sharewaitlist \\- Waitlist visibility override for this share \\(if omitted, inherits the event's own \\-wl setting\\)\n\n"
            f"{shareevent_limit_line}"
        ),
        "help_monitoring": (
            "🔍 *Monitoring*\n\n"
            "/addmonitor \\[chat\\_id\\] \\- Add group/channel to monitor list\n"
            "/removemonitor \\[chat\\_id\\] \\- Remove from monitor list\n"
            "/listmonitors \\- Show all monitored groups/channels\n\n"
            "Monitored chats are tracked for user presence and can be synced with /refreshusersall"
        ),
        "help_dm_access": (
            "💬 *DM Access*\n\n"
            "Whether commands can be run in a private DM with the bot at all\\.\n\n"
            "FREE hubs must run commands directly inside the group chat itself \\- "
            "commands typed in a DM with the bot are rejected\\.\n\n"
            "PRO hubs can run commands from a DM too\\. The DM \"sticks\" to whichever "
            "group you last picked \\(sticky group selection\\) until you switch:\n"
            "/switchgroup \\- \\(DM only\\) change which group your DM commands target\n\n"
            "/start, /help, and /switchgroup itself are never gated by this \\- only the "
            "actual work commands \\(/newevent, /adduser, etc\\.\\) require PRO to run from a DM\\."
        ),
        "help_lifecycle": (
            f"🗳 *Event Lifecycle Buttons*\n\n"
            f"  • Going / Not Going \\- vote, available to everyone\n"
            f"  • ADD / Remove \\- adjust your own guest count, available to everyone\n"
            f"  • {ICON_VERIFICATION} Verify&Close \\- admin only, locks voting and opens roster review\n"
            f"  • {ICON_CANCEL_EVENT} Cancel Event \\- admin only, cancels immediately \\(CANCELED in Events sheet\\)\n"
            f"  • {ICON_KICK} Kick / {ICON_RETURN} Return \\- admin only, toggle person in/out of going list\n"
            f"  • \\- / \\+ \\- admin only, adjust guest count\n"
            f"  • \\{ICON_ADD} Add Extra Member \\- admin only, add by username\n"
            f"  • {ICON_SAVE} Save & Close Event \\- admin only, finalize and export to EventUsers\n\n"
            f"Verify&Close and Add Extra Member are both locked in per\\-event at "
            f"creation time \\- changing either via /updatefeature never affects an "
            f"event already running\\. If Verify&Close is disabled for a hub, the "
            f"OPEN\\-state button closes the event directly instead of entering review\\."
        ),
    }
    
    section_text = help_sections.get(query.data, "Unknown section")
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Back", callback_data="help_back")],
    ])
    
    await query.edit_message_text(section_text, parse_mode="MarkdownV2", reply_markup=keyboard)


async def help_back_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id_for_help = await _help_target_chat_id(update, context)
    pro = is_premium(chat_id_for_help)
    has_event_limit = has_feature(chat_id_for_help, "event_limit")
    main_help = _build_main_help_text(pro, has_event_limit)

    keyboard = _build_main_help_keyboard(chat_id_for_help)
    await query.edit_message_text(main_help, parse_mode="MarkdownV2", reply_markup=keyboard)


async def upgrade_info_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Shown when a hub taps a locked /help section button - replaces the old
    dead-end alert ("This section is PRO-only.") with an actual next step:
    what unlocking it gets you (pulled live from feature_flags.description,
    not a hardcoded message - see task 8), the hub's current tier, a
    button to message the bot owner directly, and a way back to /help.

    callback_data is "upgrade_info_<button_key>" (e.g. "upgrade_info_aliases")
    - button_key indexes _BUTTON_FEATURE_MAP to find which feature(s) are
    actually gating that button, so the message is specific to what was
    tapped, not a one-size-fits-all Aliases/Monitoring blurb.
    """
    query = update.callback_query
    await query.answer()

    button_key = query.data[len("upgrade_info_"):]
    button_icon, button_label, _ = _BUTTON_LABELS.get(button_key, ("⚡", "This section", None))
    feature_keys = _BUTTON_FEATURE_MAP.get(button_key, [])

    all_flags = {row[0]: row for row in get_feature_flags()}
    lines = []
    for fk in feature_keys:
        row = all_flags.get(fk)
        if row and row[4]:  # row[4] = description
            lines.append(f"• {row[4]}")
    features_text = "\n".join(lines) if lines else "Unlocks additional capabilities for this section."

    chat_id = await _help_target_chat_id(update, context)
    current_tier = "PRO" if is_premium(chat_id) else "FREE"

    text = (
        f"{button_icon} *{escape_markdown(button_label)} requires a higher tier*\n\n"
        f"{escape_markdown(features_text)}\n\n"
        f"Currently: {current_tier}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Message the bot owner", url="https://t.me/anefex")],
        [InlineKeyboardButton("◀️ Back to /help", callback_data="help_back")],
    ])
    await query.edit_message_text(text, parse_mode="MarkdownV2", reply_markup=keyboard)

