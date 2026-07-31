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
    """
    if event_status in (-1, 2):
        return InlineKeyboardMarkup([])

    buttons = []

    if event_status == 0:
        buttons.append([
            InlineKeyboardButton(f"{going_icon} Going",        callback_data=f"going_{event_id}"),
            InlineKeyboardButton(f"{notgoing_icon} Not Going", callback_data=f"notgoing_{event_id}"),
        ])
        buttons.append([
            InlineKeyboardButton(f"{ICON_ADD} ADD", callback_data=f"add_{event_id}"),
            InlineKeyboardButton(f"{ICON_REMOVE} Remove", callback_data=f"sub_{event_id}"),
        ])
        if not is_child:
            buttons.append([
                InlineKeyboardButton(f"{ICON_VERIFICATION} Verify&Close", callback_data=f"close_{event_id}"),
                InlineKeyboardButton(f"{ICON_CANCEL_EVENT} Cancel Event", callback_data=f"cancel_{event_id}"),
            ])

    elif event_status == 1 and not is_child:
        # ── Master participants ────────────────────────────────────────────
        going_list        = going_list or []
        counters          = counters or {}
        child_users_rows  = child_users_rows or []
        kicked_users      = kicked_users or set()

        going_usernames     = {entry.split(" (")[0] for entry in going_list}
        all_relevant_users  = going_usernames | kicked_users | set(counters.keys())

        for username in sorted(all_relevant_users):
            guest_count = counters.get(username, 0)
            is_going    = username in going_usernames
            is_kicked   = username in kicked_users

            if is_going:
                buttons.append([
                    InlineKeyboardButton(f"{ICON_PERSON} {username}", callback_data="noop"),
                    InlineKeyboardButton(f"{ICON_KICK} Kick",          callback_data=f"kick_{event_id}:{username}"),
                ])
            elif is_kicked:
                buttons.append([
                    InlineKeyboardButton(f"{ICON_PERSON} {username}", callback_data="noop"),
                    InlineKeyboardButton(f"{ICON_RETURN} Return",      callback_data=f"return_{event_id}:{username}"),
                ])
            else:
                # Guest-only contributor - never declared Going and was
                # never Kicked either, so there's no "membership" here for
                # an admin to Kick/Return - only the guest count row shows.
                if guest_count <= 0:
                    continue

            # Row B: guest count + ➖ + ➕ (➖ before ➕; these glyphs render
            # orange by default in most emoji fonts - see docstring above)
            # Show "2G from username" format for clarity
            guest_label = f"{guest_count}G: {username}" if guest_count > 0 else "0G"
            buttons.append([
                InlineKeyboardButton(guest_label,  callback_data="noop"),
                InlineKeyboardButton(ICON_GUEST_MINUS, callback_data=f"decgst_{event_id}:{username}"),
                InlineKeyboardButton(ICON_GUEST_PLUS,  callback_data=f"incgst_{event_id}:{username}"),
            ])

        # ── Child-chat participants ────────────────────────────────────────
        for ch_username, ch_guests, ch_status in child_users_rows:
            is_going  = ch_status == "going"
            is_kicked = ch_status == "kicked"

            if is_going:
                buttons.append([
                    InlineKeyboardButton(f"{ICON_CHANNEL_PERSON} {ch_username}", callback_data="noop"),
                    InlineKeyboardButton(f"{ICON_KICK} Kick",                     callback_data=f"kick_{event_id}:ch-{ch_username}"),
                ])
            elif is_kicked:
                buttons.append([
                    InlineKeyboardButton(f"{ICON_CHANNEL_PERSON} {ch_username}", callback_data="noop"),
                    InlineKeyboardButton(f"{ICON_RETURN} Return",                 callback_data=f"return_{event_id}:ch-{ch_username}"),
                ])
            else:
                if ch_guests <= 0:
                    continue

            guest_label = f"{ch_guests}G: {ch_username}" if ch_guests > 0 else "0G"
            buttons.append([
                InlineKeyboardButton(guest_label,  callback_data="noop"),
                InlineKeyboardButton(ICON_GUEST_MINUS, callback_data=f"decgst_{event_id}:ch-{ch_username}"),
                InlineKeyboardButton(ICON_GUEST_PLUS,  callback_data=f"incgst_{event_id}:ch-{ch_username}"),
            ])

        buttons.append([
            InlineKeyboardButton(f"{ICON_ADD} Add Extra Member", callback_data=f"addext_{event_id}"),
        ])
        buttons.append([
            InlineKeyboardButton(f"{ICON_SAVE} Save & Close Event", callback_data=f"save_{event_id}"),
        ])

    return InlineKeyboardMarkup(buttons)
