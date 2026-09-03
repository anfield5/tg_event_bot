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
from db import get_all_features, get_shareevent_remaining_for_chat
from utils import escape_markdown, get_admin_contact
import flag_registry


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


# Which all_features key(s) gate each /help section button. A button is
# shown locked if ANY of its features aren't accessible to the calling
# chat - "Utility" maps to nothing since status/switchgroup/userid/chatid
# are never tier-gated (see the earlier decision to keep them out of
# all_features entirely, matching /userid/chatid/help/start).
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


def _newevent_flags_detail_text() -> str:
    """
    The -d/-gi/-ni/-limit/-wl/-ngl/-clc flag breakdown for /newevent and
    /editevent - shared by the inline "More" toggle on the main /help
    screen and the Event Lifecycle section, so the two can't drift apart.

    Per-flag lines are generated from flag_registry.py (the single
    source of truth for each flag's spelling/default/gating/description);
    only the header + shared-vocabulary + defaults summary around them
    stay hand-written here, since those are bespoke prose, not per-flag
    data the registry can meaningfully hold.
    """
    return (
        "*Flags* \\(on /newevent and /editevent\\)\n"
        + flag_registry.render_flags_detail("newevent") +
        "\n"
        "*visible\\|hidden\\|onlycount* \\(shared by \\-wl and \\-ngl\\):\n"
        "    visible \\- the full list is shown \\(for \\-wl: hub's post shows everyone across every chat; a child chat's post shows only its own\\)\n"
        "    onlycount \\- shows just the total count, no names\n"
        "    hidden \\- section not shown in the post at all \\(for \\-wl: admin\\-only via /waitlist\\)\n\n"
        "*on\\|off* \\(\\-clc only\\):\n"
        "    on \\- every name is a clickable mention\n"
        "    off \\- every name is plain, non\\-linked text\n\n"
        "*Defaults:* \\-wl hidden, \\-ngl visible, \\-clc on\n"
    )


def _shareevent_flags_detail_text() -> str:
    """The -mgl/-sngl/-swl/-clc flag breakdown for /shareevent, from flag_registry.py."""
    return flag_registry.render_flags_detail("shareevent")


def _updateuser_flags_detail_text() -> str:
    """The -a/-p flag breakdown for /updateuser, from flag_registry.py."""
    return flag_registry.render_flags_detail("updateuser")


def _updatefeature_flags_detail_text() -> str:
    """The -minlevel/-limit flag breakdown for /updatefeature (owner-only), from flag_registry.py."""
    return (
        flag_registry.render_flags_detail("updatefeature") +
        "At least one of the two flags is required\\. If \\-limit is omitted: the existing limit is "
        "kept if the tier didn't change, or reset to unlimited if it did\\.\n"
    )


def _build_main_help_keyboard(chat_id, expanded: bool = False) -> InlineKeyboardMarkup:
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

    toggle_button = (
        InlineKeyboardButton("🔼 Hide Flags", callback_data="help_collapse_newevent")
        if expanded else
        InlineKeyboardButton("🔽 MORE about Flags", callback_data="help_expand_newevent")
    )

    return InlineKeyboardMarkup([
        [toggle_button],
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


def _build_main_help_text(has_event_limit: bool = False, expanded: bool = False) -> str:
    """
    The one and only source of the main /help text - both help_command
    (direct /help) and help_back_handler (the "Back" button from a detail
    section) call this, so the two can never drift apart again the way
    they already have twice (missing -d/-date, then missing -gi/-ni).

    has_event_limit controls whether /waitlist is shown - it lives right
    after -limit's own description (not in Distribution, where a group
    that can't even set -limit would see a dead-end command).

    expanded controls whether the full /newevent+/editevent flag
    breakdown is shown inline (via the "🔽 More" toggle button) instead
    of just a pointer to the Event Lifecycle section - same underlying
    text either way (see _newevent_flags_detail_text), just shown in a
    different place depending on what the person tapped.
    """
    waitlist_line = "/waitlist \\- Show the Waitlist for the latest event \\(hub sees everyone with `from <chat>`, a child chat sees only its own\\)\n" if has_event_limit else ""
    tail = _newevent_flags_detail_text() + "\n" if expanded else "See 🗳 *Event Lifecycle* below for what each flag does and its default\\.\n"
    text = (
        "📖 *Main Commands*\n\n"
        "/newevent \\[name\\] \\[\\-d dd\\.mm\\.yyyy \\[HH:MM\\]\\]\\[\\-gi \\<emoji\\>\\]\\[\\-ni \\<emoji\\>\\]"
        "\\[\\-limit N\\]\\[\\-wl visible\\|hidden\\|onlycount\\]\\[\\-ngl visible\\|hidden\\|onlycount\\]\\[\\-clc on\\|off\\] \\- Create a new event\n"
        f"{waitlist_line}"
        "/editevent \\[name\\] \\[\\-d dd\\.mm\\.yyyy \\[HH:MM\\]\\]\\[\\-limit N\\]\\[\\-wl visible\\|hidden\\|onlycount\\]"
        "\\[\\-ngl visible\\|hidden\\|onlycount\\]\\[\\-clc on\\|off\\] \\- Edit the active event \\(same flags as /newevent, only what's given is changed\\)\n\n"
        f"{tail}"
    )
    text += "\n📚 *More Info*"
    return text


def _build_owner_help_text(expanded: bool = False) -> str:
    """
    Single toggle for the whole screen (not a per-command accordion) -
    expanding shows BOTH updatefeature's and allgroups' flag details at
    once, matching every other /help screen's "always exactly one
    toggle, shows every flagged command's details together" behavior.
    """
    allgroups_detail = "\\-pro \\- filters the list to PRO\\-tier groups only\n" if expanded else ""
    updatefeature_detail = _updatefeature_flags_detail_text() if expanded else ""
    return (
        "🔑 *Owner\\-Only Commands*\n"
        "Only work from a DM with the bot \\- running any of them inside a group is rejected\\.\n\n"
        "/setsub \\[chat\\_id\\] on \\[days\\] \\- Activate/extend PRO for a group\n"
        "/setsub \\[chat\\_id\\] off \\- Deactivate PRO for a group immediately\n"
        "/lockbot on\\|off \\- Global emergency switch \\- `on` makes the bot ignore "
        "everyone except owners, across every chat at once\n"
        "/allgroups \\[\\-pro\\] \\- List every group the bot is in, 10 at a time\n"
        f"{allgroups_detail}"
        "/allchannels \\- List every channel the bot is in, 10 at a time\n"
        "/updatefeature \\[feature\\_key\\] \\[\\-minlevel free\\|pro\\|admin\\] \\[\\-limit N\\] "
        "\\- Change a feature's tier and/or its usage limit\\. At least one of the two flags is required\\.\n"
        f"{updatefeature_detail}{chr(10) if expanded else ''}"
        "/showtable \\[table\\_name\\] \\[sheet\\_name\\] \\- Dumps `SELECT * FROM table_name` into the "
        "named tab of EventBot\\_Config \\(must already exist there\\)\\."
    )


def _build_owner_help_keyboard(expanded: bool = False) -> InlineKeyboardMarkup:
    """One toggle for the whole screen, always the topmost button."""
    toggle_button = InlineKeyboardButton(
        "🔼 Hide Flags" if expanded else "🔽 MORE about Flags",
        callback_data="help_owner_collapse" if expanded else "help_owner_expand",
    )
    return InlineKeyboardMarkup([[toggle_button]])


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_owner_request = bool(context.args) and context.args[0].strip().lower() in ("-a", "-admin")
    if is_owner_request and update.effective_user.id in OWNER_USER_IDS:
        await update.message.reply_text(
            _build_owner_help_text(),
            parse_mode="MarkdownV2",
            reply_markup=_build_owner_help_keyboard(),
        )
        return

    chat_id_for_help = await _help_target_chat_id(update, context)
    has_event_limit = has_feature(chat_id_for_help, "event_limit")
    main_help = _build_main_help_text(has_event_limit)

    keyboard = _build_main_help_keyboard(chat_id_for_help)
    await update.message.reply_text(main_help, parse_mode="MarkdownV2", reply_markup=keyboard)


async def help_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    # Defensive: re-check the SAME per-feature gate the button itself was
    # rendered with (see _make_button/_BUTTON_FEATURE_MAP), in case the
    # tier changed between the button being shown and being tapped -
    # covers both a subscription expiring AND an admin changing a
    # specific feature's own min_tier via /updatefeature (e.g. lowering
    # "aliases" to FREE) independently of the hub's overall PRO status.
    # Deliberately NOT a blunt is_premium() check - that would ignore any
    # per-feature override and could block access the hub genuinely has.
    button_key_for_section = {
        "help_alias": "aliases", "help_monitoring": "monitoring", "help_dm_access": "dm_access",
    }.get(query.data)
    if button_key_for_section:
        chat_id_for_gate = await _help_target_chat_id(update, context)
        section_features = _BUTTON_FEATURE_MAP.get(button_key_for_section, [])
        if any(not has_feature(chat_id_for_gate, fk) for fk in section_features):
            await query.answer("This section is PRO-only.", show_alert=True)
            return

    await query.answer()

    if query.data in ("help_expand_newevent", "help_collapse_newevent"):
        chat_id_for_toggle = await _help_target_chat_id(update, context)
        has_event_limit = has_feature(chat_id_for_toggle, "event_limit")
        expanded = query.data == "help_expand_newevent"
        new_text = _build_main_help_text(has_event_limit, expanded=expanded)
        new_keyboard = _build_main_help_keyboard(chat_id_for_toggle, expanded=expanded)
        await query.edit_message_text(new_text, parse_mode="MarkdownV2", reply_markup=new_keyboard)
        return

    if query.data in ("help_owner_expand", "help_owner_collapse"):
        if update.effective_user.id not in OWNER_USER_IDS:
            await query.answer("This section is owner-only.", show_alert=True)
            return
        expanded = query.data == "help_owner_expand"
        await query.edit_message_text(
            _build_owner_help_text(expanded),
            parse_mode="MarkdownV2",
            reply_markup=_build_owner_help_keyboard(expanded),
        )
        return

    # Toggle clicks for other flagged commands (users/distribution) map
    # back to their real section key here, carrying along whether THIS
    # section's one flag-block should render expanded. Each of these
    # sections only has a single toggleable command today, so there's
    # nothing else on the same screen that could need collapsing - true
    # multi-region accordion behavior only applies to the owner screen
    # (updatefeature + allgroups), handled separately below.
    users_expanded = query.data == "help_flags_users_expand"
    if query.data in ("help_users", "help_flags_users_expand", "help_flags_users_collapse"):
        effective_query_data = "help_users"
    else:
        effective_query_data = query.data

    distribution_expanded = query.data == "help_flags_distribution_expand"
    if query.data in ("help_distribution", "help_flags_distribution_expand", "help_flags_distribution_collapse"):
        effective_query_data = "help_distribution"

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
            + (_updateuser_flags_detail_text() if users_expanded else "")
            + "/notify \\- Ping users who haven't responded\n"
            "/refreshusers \\- Sync user list, Google Sheets, and remove unverifiable users for THIS group"
        ),
        "help_utility": (
            "🔧 *Utility Commands*\n\n"
            "/status \\- Show this group's subscription type\\, due date\\, and bound sheet\n"
            "/switchgroup \\- \\(DM only\\) switch which group your commands target\n"
            "/userid \\- Show your own Telegram user ID\n"
            "/chatid \\- Show this chat's ID"
            + (
                "\n/setsheet \\(DM only\\) \\[sheetid\\|sheeturl\\] \\- Bind this group to its own Google Sheet "
                "\\(Users/Events/Actions/EventUsers/UserPresenceLog tabs\\)\n"
                "sheetid\\|sheeturl \\- either the raw spreadsheet ID, or a full Google Sheets URL "
                "\\(the ID is extracted automatically\\)"
                if has_feature(hub_chat_id_for_limits, "custom_sheet") else ""
            )
            + (
                "\n/stats \\- Event activity stats for this group \\(events created, closed, "
                "total/average headcount\\)"
                if has_feature(hub_chat_id_for_limits, "stats") else ""
            )
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
            "\\[\\-sngl visible\\|hidden\\|onlycount\\] \\[\\-swl visible\\|hidden\\|onlycount\\] \\[\\-clc on\\|off\\] \\- Share active event\n\n"
            + (_shareevent_flags_detail_text() + "\n" if distribution_expanded else "")
            + f"{shareevent_limit_line}"
        ),
        "help_monitoring": (
            "🔍 *Monitoring*\n\n"
            "/addmonitor \\[chat\\_id\\] \\- Add group/channel to monitor list\n"
            "/removemonitor \\[chat\\_id\\] \\- Remove from monitor list\n"
            "/listmonitors \\- Show all monitored groups/channels\n"
            "/refreshusersall \\- Sync user list for THIS group PLUS every monitored child under it in one go "
            "\\(heavier/slower than plain /refreshusers, since it touches every monitored chat\\)\n\n"
            "Monitored chats are tracked for user presence and can be synced with /refreshusersall"
        ),
        "help_dm_access": (
            "💬 *DM Access*\n\n"
            "Run commands in a private DM with the bot, not just inside the group itself\\. "
            "The DM sticks to whichever group you last picked until you switch: "
            "/switchgroup \\- \\(DM only\\) change which group your DM commands target\\."
        ),
        "help_lifecycle": (
            f"🗳 *Event Lifecycle Buttons*\n\n"
            f"  • Going / Not Going \\- vote, available to everyone\n"
            f"  • ADD / DROP \\- adjust your own guest count one at a time, available to everyone\n"
            f"  • ALL \\- drop ALL of your own guests at once, available to everyone\n"
            f"  • {ICON_VERIFICATION} Verify \\- admin only, locks voting and opens roster review\n"
            f"  • {ICON_CANCEL_EVENT} Cancel \\- admin only, cancels immediately \\(CANCELED in Events sheet\\)\n"
            f"  • {ICON_KICK} Kick / {ICON_RETURN} Return \\- admin only, toggle person in/out of going list\n"
            f"  • \\- / \\+ \\- admin only, adjust guest count\n"
            f"  • \\{ICON_ADD} Add Extra Member \\- admin only, add by username\n"
            f"  • {ICON_SAVE} Save & Close Event \\- admin only, finalize and export to EventUsers\n\n"
            f"Verify and Add Extra Member are both locked in per\\-event at "
            f"creation time \\- changing either via /updatefeature never affects an "
            f"event already running\\. If Verify is disabled for a hub, the "
            f"OPEN\\-state button closes the event directly instead of entering review\\.\n\n"
            f"See /newevent's own \\🔽 More button \\(main /help screen\\) for its flags\\."
        ),
    }
    
    section_text = help_sections.get(effective_query_data, "Unknown section")
    
    toggle_row = []
    if effective_query_data == "help_users":
        toggle_row = [InlineKeyboardButton(
            "🔼 Hide Flags" if users_expanded else "🔽 MORE about Flags",
            callback_data="help_flags_users_collapse" if users_expanded else "help_flags_users_expand",
        )]
    elif effective_query_data == "help_distribution":
        toggle_row = [InlineKeyboardButton(
            "🔼 Hide Flags" if distribution_expanded else "🔽 MORE about Flags",
            callback_data="help_flags_distribution_collapse" if distribution_expanded else "help_flags_distribution_expand",
        )]

    keyboard_rows = ([toggle_row] if toggle_row else []) + [
        [InlineKeyboardButton("🔙 Back", callback_data="help_back")],
    ]
    keyboard = InlineKeyboardMarkup(keyboard_rows)
    
    await query.edit_message_text(section_text, parse_mode="MarkdownV2", reply_markup=keyboard)


async def help_back_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id_for_help = await _help_target_chat_id(update, context)
    has_event_limit = has_feature(chat_id_for_help, "event_limit")
    main_help = _build_main_help_text(has_event_limit)

    keyboard = _build_main_help_keyboard(chat_id_for_help)
    await query.edit_message_text(main_help, parse_mode="MarkdownV2", reply_markup=keyboard)


async def upgrade_info_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Shown when a hub taps a locked /help section button - replaces the old
    dead-end alert ("This section is PRO-only.") with an actual next step:
    what unlocking it gets you (pulled live from all_features.description,
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

    all_flags = {row[0]: row for row in get_all_features()}
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
    contact_label, contact_url = get_admin_contact()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(contact_label, url=contact_url)],
        [InlineKeyboardButton("◀️ Back to /help", callback_data="help_back")],
    ])
    await query.edit_message_text(text, parse_mode="MarkdownV2", reply_markup=keyboard)

