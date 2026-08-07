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
from subscription import is_premium
from hub_resolver import _get_known_candidate_chats


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


def _build_main_help_keyboard(chat_id) -> InlineKeyboardMarkup:
    """
    Aliases/Monitoring buttons are shown either as normal (premium hub) or
    marked with ICON_PREMIUM (free hub). Tapping the free version opens an
    upgrade-info message (see upgrade_info_callback_handler) instead of
    doing nothing, so there's an actual next step rather than a dead end.
    """
    premium = is_premium(chat_id)
    alias_btn = InlineKeyboardButton(
        "⚙️ Aliases" if premium else f"⚙️ Aliases {ICON_PREMIUM}",
        callback_data="help_alias" if premium else "upgrade_info",
    )
    monitor_btn = InlineKeyboardButton(
        "🔍 Monitoring" if premium else f"🔍 Monitoring {ICON_PREMIUM}",
        callback_data="help_monitoring" if premium else "upgrade_info",
    )
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗳 Event Lifecycle", callback_data="help_lifecycle"),
         InlineKeyboardButton("📢 Distribution", callback_data="help_distribution")],
        [alias_btn, monitor_btn],
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


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    is_owner_request = bool(context.args) and context.args[0].strip().lower() in ("-a", "--admin", "--owner")
    if is_owner_request and update.effective_user.id in OWNER_USER_IDS:
        owner_help = (
            "🔑 *Owner\\-Only Commands*\n\n"
            "/setsub \\[chat\\_id\\] on \\[days\\] \\- Activate/extend PRO for a group\n"
            "/setsub \\[chat\\_id\\] off \\- Deactivate PRO for a group immediately\n"
            "/allgroups \\[\\-pro\\] \\- List every group the bot is in, 10 at a time\n"
            "/allchannels \\- List every channel the bot is in, 10 at a time\n\n"
            "These are gated on your personal Telegram user\\_id \\(OWNER\\_USER\\_IDS\\), "
            "not on chat admin status \\- posting anonymously \\(as the group/channel itself\\) "
            "can't be verified and will be rejected\\."
        )
        await update.message.reply_text(owner_help, parse_mode="MarkdownV2")
        return

    chat_id_for_help = await _help_target_chat_id(update, context)
    pro = is_premium(chat_id_for_help)

    main_help = (
        "📖 *Main Commands*\n\n"
        "/newevent \\[name\\] \\[\\-date dd\\.mm\\.yyyy \\[HH:MM\\]\\]\\[\\-gi \\<emoji\\>\\]\\[\\-ni \\<emoji\\>\\] \\- Create a new event\n"
        "\\-gi \\| \\-goingicon \\<emoji\\> \\- Custom Going icon\n"
        "\\-ni \\| \\-notgoingicon \\<emoji\\> \\- Custom Not Going icon\n"
        "/editevent \\[name\\] \\[\\-date \\.\\.\\.\\] \\- Edit the active event\n"
        "/notify \\- Ping users who haven't responded\n"
        "/refreshusers \\- Sync user list, Google Sheets, and remove unverifiable users for THIS group\n"
    )
    if pro:
        main_help += "/refreshusersall \\- Same as /refreshusers, but for every monitored group/channel\n"
    main_help += (
        "/listusers \\- Show all tracked users\n"
        "/adduser \\[user\\_id\\|username\\] \\[\\.\\.\\.\\] \\- Manually add users to tracked list\n"
        "/updateuser \\[username\\(s\\)\\] \\-a\\|\\-p \\- Mark tracked users active or passive\n"
    )
    if pro:
        main_help += "/setsheet \\[spreadsheet\\_id\\_or\\_url\\] \\- \\(PRO\\) Bind this group to its own Google Sheet\n"
    main_help += (
        "\n🔧 *Utility Commands*\n"
        "/status \\- Show this group's subscription type\\, due date\\, and bound sheet\n"
        "/switchgroup \\- \\(DM only\\) switch which group your commands target\n"
        "/userid \\- Show your own Telegram user ID\n"
        "/chatid \\- Show this chat's ID\n\n"
        "📚 *More Info*"
    )

    keyboard = _build_main_help_keyboard(chat_id_for_help)
    await update.message.reply_text(main_help, parse_mode="MarkdownV2", reply_markup=keyboard)


async def help_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    # Defensive: these two sections are premium-only. The free-tier keyboard
    # never sends these callback_data values in the first place (it sends
    # "upgrade_info" instead), but re-check here too in case the tier changed
    # between the button being shown and being tapped.
    if query.data in ("help_alias", "help_monitoring") and not is_premium(await _help_target_chat_id(update, context)):
        await query.answer("This section is PRO-only.", show_alert=True)
        return

    await query.answer()

    help_sections = {
        "help_alias": (
            "⚙️ *Alias Subsystem*\n\n"
            "/setalias \\[target\\_id\\] \\[aliasname\\] \\- Bind alias to chat ID\n"
            "/removealias \\[aliasname\\] \\- Remove alias\n"
            "/listaliases \\- Show all aliases\n\n"
            "Aliases let you use memorable names instead of numeric chat IDs when sharing events"
        ),
        "help_distribution": (
            "📢 *Distribution Control*\n\n"
            "/shareevent \\[target\\_alias/id\\] \\[\\-v \\| \\-h \\| \\-oc\\] \\- Share active event\n\n"
            "Modes:\n"
            "  • \\-v \\(visible\\): Show full event in child chat\n"
            "  • \\-h \\(hidden\\): Hide event, only show going/notgoing counts\n"
            "  • \\-oc \\(onlycount\\): Show only total going count\n\n"
            "Defaults to \\-oc if no mode is given"
        ),
        "help_monitoring": (
            "🔍 *Monitoring System*\n\n"
            "/addmonitor \\[chat\\_id\\] \\- Add group/channel to monitor list\n"
            "/removemonitor \\[chat\\_id\\] \\- Remove from monitor list\n"
            "/listmonitors \\- Show all monitored groups/channels\n\n"
            "Monitored chats are tracked for user presence and can be synced with /refreshusersall"
        ),
        "help_lifecycle": (
            f"🗳 *Event Lifecycle Buttons*\n\n"
            f"  • Going / Not Going / ADD / Remove \\- open voting, available to everyone\n"
            f"  • {ICON_VERIFICATION} Verify&Close \\- admin only, locks voting and opens roster review\n"
            f"  • {ICON_CANCEL_EVENT} Cancel Event \\- admin only, cancels immediately \\(CANCELED in Events sheet\\)\n"
            f"  • {ICON_KICK} Kick / {ICON_RETURN} Return \\- admin only, toggle person in/out of going list\n"
            f"  • \\- / \\+ \\- admin only, adjust guest count\n"
            f"  • \\{ICON_ADD} Add Extra Member \\- admin only, add by username\n"
            f"  • {ICON_SAVE} Save & Close Event \\- admin only, finalize and export to EventUsers"
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

    main_help = (
        "📖 *Main Commands*\n\n"
        "/newevent \\[name\\] \\[\\-date dd\\.mm\\.yyyy \\[HH:MM\\]\\] \\- Create a new event\n"
        "/editevent \\[name\\] \\[\\-date \\.\\.\\.\\] \\- Edit the active event\n"
        "/notify \\- Ping users who haven't responded\n"
        "/refreshusers \\- Sync user list, Google Sheets, and remove unverifiable users for THIS group\n"
    )
    if pro:
        main_help += "/refreshusersall \\- Same as /refreshusers, but for every monitored group/channel\n"
    main_help += (
        "/listusers \\- Show all tracked users\n"
        "/adduser \\[user\\_id\\|username\\] \\[\\.\\.\\.\\] \\- Manually add users to tracked list\n"
        "/updateuser \\[username\\(s\\)\\] \\-a\\|\\-p \\- Mark tracked users active or passive\n"
    )
    if pro:
        main_help += "/setsheet \\[spreadsheet\\_id\\_or\\_url\\] \\- \\(PRO\\) Bind this group to its own Google Sheet\n"
    main_help += (
        "\n🔧 *Utility Commands*\n"
        "/status \\- Show this group's subscription type\\, due date\\, and bound sheet\n"
        "/switchgroup \\- \\(DM only\\) switch which group your commands target\n"
        "/userid \\- Show your own Telegram user ID\n"
        "/chatid \\- Show this chat's ID\n\n"
        "📚 *More Info*"
    )

    keyboard = _build_main_help_keyboard(chat_id_for_help)
    await query.edit_message_text(main_help, parse_mode="MarkdownV2", reply_markup=keyboard)


async def upgrade_info_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Shown when a free-tier hub taps the locked Aliases/Monitoring button on
    /help - replaces the old dead-end alert ("This section is PRO-only.")
    with an actual next step: what PRO unlocks, the hub's current tier, a
    button to message the bot owner directly, and a way back to /help
    instead of a conversational dead end.
    """
    query = update.callback_query
    await query.answer()

    chat_id = await _help_target_chat_id(update, context)
    current_tier = "PRO" if is_premium(chat_id) else "FREE"

    text = (
        f"⚡ *Aliases and Monitoring are PRO features*\n\n"
        f"With PRO, this hub gets:\n"
        f"• Custom aliases for child groups/channels\n"
        f"• Group/channel monitoring \\(/addmonitor\\)\n"
        f"• Your own Google Sheet, auto\\-synced with every event\n\n"
        f"Currently: {current_tier}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Message the bot owner", url="https://t.me/anefex")],
        [InlineKeyboardButton("◀️ Back to /help", callback_data="help_back")],
    ])
    await query.edit_message_text(text, parse_mode="MarkdownV2", reply_markup=keyboard)

