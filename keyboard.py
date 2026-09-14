"""
Inline keyboard builder for event posts.

Pure function, zero I/O, zero dependency on DB/sheets/other handler state -
split out on its own since it's the most self-contained piece of the old
monolithic handlers.py.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from config import (
    ICON_VERIFICATION,
    ICON_KICK, ICON_RETURN, ICON_PERSON, ICON_CHANNEL_PERSON,
    ICON_GUEST_MINUS, ICON_GUEST_PLUS, ICON_ADD, ICON_REMOVE,
    ICON_CANCEL_EVENT, ICON_SAVE,
)

# Each verification-mode participant renders as 2 rows / 5 buttons (name+Kick,
# guest-count+minus+plus). Telegram's own practical limit is ~100 buttons per
# keyboard - real incident: an event with 21+ going participants (105+
# buttons, before even counting Add Extra Member/Save&Close) silently failed
# to re-render on every Kick/Guest/Add-Extra-Member action, since Telegram
# rejects the oversized edit_message_text call. 10 people/page keeps every
# page comfortably under the limit (10*5=50 + ~6 for controls = 56) with
# generous margin, however many total participants there are - Prev/Next
# lets an admin cycle through unlimited pages.
VERIFICATION_PAGE_SIZE = 10


def create_event_keyboard(
    event_id: str,
    event_status: int,
    going_icon: str,
    notgoing_icon: str,
    going_list: list = None,
    counters: dict = None,
    is_child: bool = False,
    child_users_rows: list = None,
    kicked_users: set = None,
    verification_enabled: bool = True,
    add_extra_member_enabled: bool = True,
    is_full: bool = False,
    display_names: dict = None,
    verification_page: int = 0,
) -> InlineKeyboardMarkup:
    """
    Generates dynamic inline keyboards.

    event_status == -1  → empty keyboard (event canceled)
    event_status == 2   → empty keyboard (event closed)
    event_status == 0   → Going / Not Going / Add & Sub guest / Close button
    event_status == 1   → Verification mode:
                    Each participant is rendered over TWO rows:
                        Row A: [👤 name]          [❌ Kick]
                        Row B: [N G.]  [➖]  [➕]
                    NOTE: Telegram's Bot API has no way to set a custom
                    color on button text - the ➕/➖ (Heavy Plus/Minus Sign)
                    glyphs are used bare here because most emoji fonts
                    (including Telegram's own) already render them in an
                    orange/rust tone by default; there's no way to force a
                    different or more saturated orange beyond that.

    display_names: optional {username: "First Last"} map for the
    verification-mode participant rows (event_status == 1) - shows the
    real name instead of the bare username when available. This module
    stays DB-free by design (see the module docstring), so the CALLER
    resolves names (via db.get_display_name) and passes the finished
    map in; a username missing from the map just falls back to itself.

    verification_enabled / add_extra_member_enabled: read from the event's
    OWN stored feature_snapshot (see db.events.feature_snapshot), NOT the
    hub's current live tier - an event keeps whatever rules applied when it
    was created, even if the hub's tier or all_features change later.
    verification_enabled=False changes the OPEN-state button to
    "Save&Close" (callback "directclose_") instead of "Verify"
    (callback "close_") - skipping the review step entirely, since there's
    nothing to gate it into. add_extra_member_enabled=False simply omits
    the "Add Extra Member" button during verification, rather than showing
    it disabled - a tier limit applies to the whole event/group, not to one
    specific clicking user, so hiding it outright is less confusing than an
    inert button (see the admin-only buttons elsewhere, which DO stay
    visible-but-inert since those gate on the clicking individual, not on
    the group's own tier).
    """
    if event_status in (-1, 2):
        return InlineKeyboardMarkup([])

    buttons = []

    if event_status == 0:
        going_label = "Standby" if is_full else "Going"
        buttons.append([
            InlineKeyboardButton(f"{going_icon} {going_label}", callback_data=f"going_{event_id}"),
            InlineKeyboardButton(f"{notgoing_icon} Not Going", callback_data=f"notgoing_{event_id}"),
        ])
        buttons.append([
            InlineKeyboardButton(f"{ICON_ADD} ADD", callback_data=f"add_{event_id}"),
            InlineKeyboardButton(f"{ICON_REMOVE} DROP", callback_data=f"sub_{event_id}"),
            InlineKeyboardButton(f"{ICON_REMOVE} ALL", callback_data=f"dropall_{event_id}"),
        ])
        if not is_child:
            close_button = (
                InlineKeyboardButton(f"{ICON_VERIFICATION} Verify", callback_data=f"close_{event_id}")
                if verification_enabled else
                InlineKeyboardButton(f"{ICON_SAVE} Save&Close", callback_data=f"directclose_{event_id}")
            )
            buttons.append([
                close_button,
                InlineKeyboardButton(f"{ICON_CANCEL_EVENT} Cancel", callback_data=f"cancel_{event_id}"),
            ])

    elif event_status == 1 and not is_child:
        # ── Master participants ────────────────────────────────────────────
        going_list        = going_list or []
        counters          = counters or {}
        child_users_rows  = child_users_rows or []
        kicked_users      = kicked_users or set()
        display_names     = display_names or {}

        going_usernames     = {entry.split(" (")[0] for entry in going_list}
        all_relevant_users  = going_usernames | kicked_users | set(counters.keys())

        # Build ONE unified list of (icon, display, action_target, guest_count,
        # has_membership_row) entries across BOTH master and child chat
        # participants, THEN slice by page - this is what makes pagination
        # actually cap the button count, rather than paginating each source
        # independently (which wouldn't bound the combined total).
        entries = []
        for username in sorted(all_relevant_users):
            guest_count = counters.get(username, 0)
            is_going    = username in going_usernames
            is_kicked   = username in kicked_users
            display     = display_names.get(username, username)
            if not is_going and not is_kicked and guest_count <= 0:
                continue
            entries.append((ICON_PERSON, display, username, guest_count, is_going, is_kicked))

        for ch_username, ch_guests, ch_status in child_users_rows:
            is_going    = ch_status == "going"
            is_kicked   = ch_status == "kicked"
            ch_display  = display_names.get(ch_username, ch_username)
            if not is_going and not is_kicked and ch_guests <= 0:
                continue
            entries.append((ICON_CHANNEL_PERSON, ch_display, f"ch-{ch_username}", ch_guests, is_going, is_kicked))

        total_pages = max(1, (len(entries) + VERIFICATION_PAGE_SIZE - 1) // VERIFICATION_PAGE_SIZE)
        page = max(0, min(verification_page, total_pages - 1))
        page_entries = entries[page * VERIFICATION_PAGE_SIZE:(page + 1) * VERIFICATION_PAGE_SIZE]

        for icon, display, target, guest_count, is_going, is_kicked in page_entries:
            if is_going:
                buttons.append([
                    InlineKeyboardButton(f"{icon} {display}", callback_data="noop"),
                    InlineKeyboardButton(f"{ICON_KICK} Kick",   callback_data=f"kick_{event_id}:{target}"),
                ])
            elif is_kicked:
                buttons.append([
                    InlineKeyboardButton(f"{icon} {display}", callback_data="noop"),
                    InlineKeyboardButton(f"{ICON_RETURN} Return", callback_data=f"return_{event_id}:{target}"),
                ])
            # Row B: guest count + ➖ + ➕ (➖ before ➕; these glyphs render
            # orange by default in most emoji fonts - see docstring above)
            guest_label = f"{guest_count}G: {display}" if guest_count > 0 else "0G"
            buttons.append([
                InlineKeyboardButton(guest_label,      callback_data="noop"),
                InlineKeyboardButton(ICON_GUEST_MINUS, callback_data=f"decgst_{event_id}:{target}"),
                InlineKeyboardButton(ICON_GUEST_PLUS,  callback_data=f"incgst_{event_id}:{target}"),
            ])

        if total_pages > 1:
            nav_row = []
            if page > 0:
                nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"vpage_{event_id}:{page - 1}"))
            nav_row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
            if page < total_pages - 1:
                nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"vpage_{event_id}:{page + 1}"))
            buttons.append(nav_row)

        if add_extra_member_enabled:
            buttons.append([
                InlineKeyboardButton(f"{ICON_ADD} Add Extra Member", callback_data=f"addext_{event_id}"),
            ])
        buttons.append([
            InlineKeyboardButton("🔙 Back", callback_data=f"back_{event_id}"),
            InlineKeyboardButton(f"{ICON_SAVE} Save & Close Event", callback_data=f"save_{event_id}"),
        ])

    return InlineKeyboardMarkup(buttons)
