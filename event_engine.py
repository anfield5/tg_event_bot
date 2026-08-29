"""
The core event-rendering/interaction engine - split out of handlers.py,
where this and button_handler together made up roughly 900 of that file's
~2400 lines, tightly coupled to each other (shared per-event locks) but
only loosely coupled to the rest of handlers.py's command functions
(newevent, editevent, etc. never call into this module's internals).

Covers:
  - get_event_lock() / _event_locks - one lock per event_id so two
    near-simultaneous button clicks on the same event can't interleave
    their read-modify-write and silently drop one.
  - schedule_view_refresh() / _get_refresh_state() / _refresh_state -
    coalesces multiple rapid state changes into a single re-render pass
    instead of racing to redraw the same message repeatedly.
  - _mention_link() - builds a clickable [Name](tg://user?id=...) mention,
    working even without a stored @username.
  - update_all_shared_views() - re-renders every view of an event (master
    hub + every child chat it's been shared to) after any state change.
  - button_handler() - the main state machine: every inline keyboard click
    across every chat comes through here.
"""

import json
import re
import asyncio
from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes
from telegram.error import BadRequest

from keyboard import create_event_keyboard
from config import (
    ICON_CANCEL_EVENT, ICON_CLOCK, ICON_GUEST, ICON_SHARED, ICON_STATS, ICON_STANDBY,
    ICON_WARNING, ICON_TOTAL, logger,
)
from utils import escape_markdown, now2ddmmyy, is_real_admin
from db import get_connection, get_display_name, track_user, dedupe_waitlist, migrate_event_to_event_users
from sheets import get_sheet_for_chat, open_spreadsheet, sync_event_users_sheet


# One lock per event_id so that two near-simultaneous button clicks on the
# same event can't interleave their read-modify-write and silently drop one.
_event_locks = {}


def get_event_lock(event_id: str) -> asyncio.Lock:
    lock = _event_locks.get(event_id)
    if lock is None:
        lock = asyncio.Lock()
        _event_locks[event_id] = lock
    return lock

# ---------------------------------------------------------------------------
# Shared-view renderer
# ---------------------------------------------------------------------------

_refresh_state = {}


def _get_refresh_state(event_id):
    state = _refresh_state.get(event_id)
    if state is None:
        state = {"lock": asyncio.Lock(), "pending": False}
        _refresh_state[event_id] = state
    return state


async def schedule_view_refresh(context: ContextTypes.DEFAULT_TYPE, event_id: str):
    """
    Coalesces bursts of update_all_shared_views() calls for the same event.

    Every going/notgoing/add/sub/kick/... click schedules a full re-render of
    the master post PLUS every shared child chat/channel. Without coalescing,
    N rapid clicks (very common in a busy public channel with many
    subscribers) each launched their OWN independent, fully sequential
    broadcast concurrently with the others - flooding Telegram's per-chat
    edit-message rate limit. The practical symptom was exactly what got
    reported: "Going" appearing to hang/load forever, and other actions
    looking like they "don't work" (the DB write actually succeeded, but the
    on-screen update got stuck behind a pile-up of redundant duplicate
    broadcasts all competing for the same rate-limited edit calls).

    This makes sure at most ONE broadcast is in flight per event at a time;
    if new changes arrive while one is running, they collapse into a single
    extra pass at the end instead of spawning another full broadcast.
    """
    state = _get_refresh_state(event_id)
    if state["lock"].locked():
        state["pending"] = True
        return
    async with state["lock"]:
        state["pending"] = False
        await update_all_shared_views(context, event_id)
        while state["pending"]:
            state["pending"] = False
            await update_all_shared_views(context, event_id)


async def _edit_message_text_with_retry(context, max_retries: int = 2, retry_delay: float = 0.5, **kwargs):
    """
    Wraps context.bot.edit_message_text with a couple of retries on
    transient failures - found via a real screenshot where the master
    hub's post showed some names as non-clickable while the SAME
    people, in the child chat's own post, showed as clickable, despite
    both being built from identical data in the same render pass.

    The root cause: the master's edit runs sequentially, awaited on its
    own, separately from child edits (which run concurrently
    afterward). If the master's SPECIFIC edit_message_text call hits a
    transient failure (Telegram rate-limiting, a network blip), it was
    being caught, logged, and left there - the post stayed stuck
    showing whatever it rendered on the PREVIOUS successful update
    until some unrelated future click happened to trigger a full
    refresh again. Meanwhile any child chat's edit, which failed
    independently or not at all, could easily reflect newer data in
    the meantime (e.g. a stale-entry healing fix landing on a later
    click). Retrying closes this window instead of leaving one post
    permanently stale after a single bad API call.

    "Message is not modified" is NOT retried - it's an expected,
    harmless BadRequest whenever the new text happens to exactly match
    what's already there.
    """
    last_exception = None
    for attempt in range(max_retries + 1):
        try:
            await context.bot.edit_message_text(**kwargs)
            return
        except BadRequest as e:
            if "Message is not modified" in str(e):
                return
            last_exception = e
        except Exception as e:
            last_exception = e
        if attempt < max_retries:
            await asyncio.sleep(retry_delay)
    raise last_exception


def _mention_link(chat_id: str, username: str, user_id=None, clickable: bool = True, display_name_override: str = None) -> str:
    """
    Builds a clickable MarkdownV2 mention - [First Last](tg://user?id=...) -
    using the stored first_name/last_name for this user_id if we have it on
    file (see db.get_display_name), falling back to the plain @username/
    display name we were given if we don't have a resolvable user_id at all
    (e.g. someone added via /adduser whose name was never captured).

    Unlike Telegram's own automatic @mention linking, this works even for
    users who have no @username set - the whole point of this feature.

    `user_id` can be omitted if the caller only has a plain username on
    hand (e.g. entries in the Not Going list, which - unlike Going - don't
    carry a "(user_id)" suffix) - in that case it's looked up from
    main_group_users by (chat_id, username).

    `clickable=False` (the -clc/-clickability flag, default True) forces
    plain, non-linked text even when a valid user_id IS available - the
    display name is still resolved the same way (First Last if on file),
    just without the tg://user?id=... link wrapper.

    `display_name_override`: if the caller already has a resolved "First
    Last" string on hand (e.g. a waitlist entry, which stores first_name/
    last_name directly at write time), pass it here to skip the
    get_display_name lookup entirely - avoids depending on a separate
    main_group_users row existing/matching, the same fragility class
    fixed for the Not Going list.
    """
    if user_id is None:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT user_id FROM main_group_users WHERE chat_id = ? AND username = ?",
                (str(chat_id), username),
            )
            row = cursor.fetchone()
            user_id = row[0] if row else None

    if not user_id or not str(user_id).lstrip("-").isdigit():
        return escape_markdown(username)

    display = display_name_override if display_name_override else get_display_name(str(chat_id), str(user_id), username)
    if not clickable:
        return escape_markdown(display)
    return f"[{escape_markdown(display)}](tg://user?id={user_id})"


def _promotion_announcement_text(chat_id: str, username: str, user_id, is_guest: bool) -> str:
    """
    Builds the "a spot opened up" announcement shown after a Waitlist
    promotion - previously this exact if/else was copy-pasted in 3 places
    (button_handler's own child-branch return path, button_handler's
    shared master-path return, and editevent's own limit-raise
    promotion in handlers.py). One wording change now only needs to
    happen here.
    """
    mention = _mention_link(chat_id, username, user_id)
    if is_guest:
        return f"{ICON_STANDBY} A spot opened up \\- one more guest for {mention} has been added from the Waitlist\\!"
    return f"{ICON_STANDBY} A spot opened up \\- {mention} has been moved from the Waitlist to Going\\!"


async def _send_promotion_announcements(context, waitlist_promotion, extra_promotions):
    """
    Sends the "a spot opened up" announcement for `waitlist_promotion`
    (a single (chat_id, username, user_id, is_guest) tuple or None) plus
    one more for every entry in `extra_promotions` - the latter only
    ever non-empty for actions that can free MULTIPLE slots at once
    (dropall), since every OTHER promotion trigger (sub/notgoing/limit
    raise) only ever frees one slot per click.

    Previously this exact loop was copy-pasted in button_handler's
    child-chat return path and its master-path return - both call this
    now instead.
    """
    all_promotions = ([waitlist_promotion] if waitlist_promotion else []) + list(extra_promotions)
    for promo_chat_id, promo_username, promo_user_id, promo_is_guest in all_promotions:
        try:
            await context.bot.send_message(
                chat_id=int(promo_chat_id),
                text=_promotion_announcement_text(promo_chat_id, promo_username, promo_user_id, promo_is_guest),
                parse_mode="MarkdownV2",
            )
        except Exception as e:
            logger.error(f"Waitlist promotion announcement failed for chat {promo_chat_id}: {e}")


def _waitlist_display_name_override(entry: dict) -> str:
    """Builds a "First Last" string directly from a waitlist entry's own
    stored fields, avoiding a separate main_group_users lookup. Returns
    None (falls back to _mention_link's own lookup) for old-format
    entries created before first_name/last_name were added here."""
    parts = [p for p in (entry.get("first_name"), entry.get("last_name")) if p]
    return " ".join(parts) if parts else None


def _render_waitlist_local(waitlist: list, chat_id: str, clickable: bool = True) -> tuple:
    """
    Filters an event's waitlist_data down to entries added from THIS
    specific chat_id only, no "from" labels (since every entry shown is
    already local to this chat by construction). Used for:
      - a child chat's own post in 'visible' mode (never shows another
        chat's entries, even though waitlist_data is one event-wide list)
      - /waitlist called from a child chat

    Person entries render as "<Standby icon> <mention>", matching a
    regular Going entry. Guest-slot entries (is_guest=True) are grouped by
    owner - multiple queued slots for the same person collapse into ONE
    line with a count, using the exact same format as the going-list's own
    guest lines (ICON_GUEST, "N, from: <owner>") - not the person-waiting
    format, since a guest slot isn't a separate person waiting.

    `clickable` mirrors the -clc/-clickability flag - False renders every
    name as plain text instead of a clickable mention.

    Returns (count, text_lines).
    """
    entries = [e for e in waitlist if str(e.get("chat_id")) == str(chat_id)]
    person_lines = [
        f"{ICON_STANDBY} {_mention_link(chat_id, e['username'], e['user_id'], clickable, _waitlist_display_name_override(e))}"
        for e in entries if not e.get("is_guest")
    ]
    guest_counts = {}
    guest_name_overrides = {}
    for e in entries:
        if e.get("is_guest"):
            key = (e["user_id"], e["username"])
            guest_counts[key] = guest_counts.get(key, 0) + 1
            guest_name_overrides[key] = _waitlist_display_name_override(e)
    guest_lines = [
        f"{ICON_GUEST} {count}, from: {_mention_link(chat_id, uname, uid, clickable, guest_name_overrides.get((uid, uname)))}"
        for (uid, uname), count in guest_counts.items()
    ]
    lines = person_lines + guest_lines
    return len(entries), "\n".join(lines)


async def _render_waitlist_all(waitlist: list, main_chat_id: str, context: ContextTypes.DEFAULT_TYPE, clickable: bool = True) -> tuple:
    """
    Every entry across every chat the event was shared to, with a "from
    <chat_name>" suffix for anything that ISN'T local to main_chat_id.
    Used for the main hub's OWN post in 'visible' mode - per the design,
    only the hub's own post gets the full cross-chat view; every child
    chat's post still only shows its own local entries (see
    _render_waitlist_local), and /waitlist mirrors this same "hub sees
    everyone, child sees only local" split.

    Person entries render as "<Standby icon> <mention> [from <chat>]".
    Guest-slot entries are grouped by (owner, chat) - matching
    _render_waitlist_local's own grouping - and rendered with the going-
    list's guest-line format instead of the person-waiting format.

    `clickable` mirrors the -clc/-clickability flag - False renders every
    name as plain text instead of a clickable mention.

    Returns (count, text_lines).
    """
    title_cache = {}

    async def _title(cid):
        if cid not in title_cache:
            try:
                obj = await context.bot.get_chat(int(cid) if str(cid).replace("-", "").isdigit() else cid)
                title_cache[cid] = obj.title or "Group"
            except Exception:
                title_cache[cid] = "Group"
        return title_cache[cid]

    person_lines = []
    guest_counts = {}  # (user_id, username, chat_id) -> count
    guest_name_overrides = {}
    for e in waitlist:
        if e.get("is_guest"):
            key = (e["user_id"], e["username"], e["chat_id"])
            guest_counts[key] = guest_counts.get(key, 0) + 1
            guest_name_overrides[key] = _waitlist_display_name_override(e)
            continue
        mention = f"{ICON_STANDBY} {_mention_link(e['chat_id'], e['username'], e['user_id'], clickable, _waitlist_display_name_override(e))}"
        if str(e.get("chat_id")) != str(main_chat_id):
            chat_title = await _title(e["chat_id"])
            mention += f" from {escape_markdown(chat_title)}"
        person_lines.append(mention)

    guest_lines = []
    for (uid, uname, cid), count in guest_counts.items():
        line = f"{ICON_GUEST} {count}, from: {_mention_link(cid, uname, uid, clickable, guest_name_overrides.get((uid, uname, cid)))}"
        if str(cid) != str(main_chat_id):
            chat_title = await _title(cid)
            line += f" \\({escape_markdown(chat_title)}\\)"
        guest_lines.append(line)

    lines = person_lines + guest_lines
    return len(waitlist), "\n".join(lines)


def _render_waitlist_count(waitlist: list) -> int:
    """Total count across every chat combined, for 'onlycount' mode -
    the same global number regardless of which chat's post is asking."""
    return len(waitlist)


async def update_all_shared_views(context: ContextTypes.DEFAULT_TYPE, event_id: str):
    """
    Re-renders EVERY view of one event after its state changed: the master
    post in the hub group, plus every child chat/channel it's been shared
    to (via /shareevent). Called after every going/notgoing/kick/guest
    click, /editevent, Save & Close, and Cancel Event - normally through
    schedule_view_refresh() (see its own docstring for why it's not called
    directly), never straight from a click handler.

    What it does, roughly in order:
      1. Reads the master event row (going/notgoing lists, guest counters,
         kicked list, event_status) - this is the single source of truth
         that everything below gets rendered from.
      2. Reads every event_shares row (one per chat/channel this event has
         been shared to) plus that child chat's own event_users rows
         (their own going/notgoing/guest state, tracked independently of
         the master hub's own going list).
      3. Builds the master hub's message text + keyboard (via
         create_event_keyboard) and edits that message in place.
      4. For each child share: builds that child's own message text +
         keyboard (which only shows THAT child's participants, not the
         whole event) and edits it too - respecting whatever share mode
         (-visible/-onlycount/-hidden) it was shared with.

    All DB reads happen up front in ONE connection, closed before any
    Telegram API calls are made - editing N child chats means N sequential
    network calls, and there's no reason to hold a SQLite connection open
    across all of them.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT chat_id, message_id, name, going_icon, notgoing_icon,
                   event_status, going_data, notgoing_data, counters_data, event_date, kicked_data,
                   feature_snapshot, total_limit, waitlist_data, waitlist_visibility, notgoing_visibility, clickability
            FROM events WHERE event_id = ?
            """,
            (event_id,),
        )
        master = cursor.fetchone()
        if not master:
            return

        (main_chat_id, main_msg_id, name, going_icon, notgoing_icon,
         event_status, going_data, notgoing_data, counters_data, event_date, kicked_data,
         feature_snapshot_raw, total_limit, waitlist_data_raw, waitlist_visibility, notgoing_visibility, clickability) = master

        try:
            feature_snapshot = json.loads(feature_snapshot_raw) if feature_snapshot_raw else {}
        except (TypeError, ValueError):
            feature_snapshot = {}
        verification_enabled = feature_snapshot.get("verification", True)
        add_extra_member_enabled = feature_snapshot.get("add_extra_member", True)

        cursor.execute(
            "SELECT username, status, guests, user_id, first_name, last_name FROM event_users "
            "WHERE event_id = ? AND chat_id = ?",
            (event_id, str(main_chat_id)),
        )
        main_hub_users = cursor.fetchall()

        if not main_hub_users:
            # Not yet migrated to event_users (no click has touched this
            # event since the Variant B deploy) - fall back to rendering
            # straight from the old JSON columns rather than showing an
            # empty event. The actual migration itself only happens in
            # button_handler's own transaction (this function is
            # read-only), so this fallback stays in place until the next
            # real click on the event.
            master_going = json.loads(going_data)
            master_not_going = json.loads(notgoing_data)
            master_counters = json.loads(counters_data)
            master_kicked = set(json.loads(kicked_data or "[]"))
        else:
            master_going = []
            master_not_going = []
            master_counters = {}
            master_kicked = set()
            for m_username, m_status, m_guests, m_user_id, m_first, m_last in main_hub_users:
                has_id = m_user_id and str(m_user_id).lstrip("-").isdigit()
                entry = f"{m_username} ({m_user_id})" if has_id else m_username
                if m_status == "going":
                    master_going.append(entry)
                elif m_status == "notgoing":
                    master_not_going.append(entry)
                elif m_status == "kicked":
                    master_kicked.add(m_username)
                if m_guests and m_guests > 0:
                    master_counters[m_username] = m_guests
        master_waitlist  = dedupe_waitlist(json.loads(waitlist_data_raw or "[]"))

        cursor.execute(
            "SELECT chat_id, message_id, share_mode, share_notgoing_visibility, share_waitlist_visibility, share_clickability FROM event_shares WHERE event_id = ?",
            (event_id,),
        )
        shares = cursor.fetchall()

        # Fetch every share's event_users rows up front, while the
        # connection is open, instead of re-querying inside the loop below -
        # that loop also makes Telegram API calls (get_chat), which
        # shouldn't happen while holding a DB connection open.
        per_share_users = {}
        for s_chat_id, _, _, _, _, _ in shares:
            cursor.execute(
                "SELECT username, status, guests, user_id FROM event_users "
                "WHERE event_id = ? AND chat_id = ?",
                (event_id, str(s_chat_id)),
            )
            per_share_users[str(s_chat_id)] = cursor.fetchall()

    child_data              = {}
    total_child_going       = 0
    child_addons_for_master = []

    for s_chat_id, _, _, _, _, share_clc in shares:
        users      = per_share_users[str(s_chat_id)]
        users_list = []
        notgoing_list = []
        chat_sum   = 0
        effective_clickable = (share_clc if share_clc else clickability) == "on"
        for username, status, guests, u_id in users:
            if status == "going":
                users_list.append(f"{going_icon} {_mention_link(s_chat_id, username, u_id, effective_clickable)}")
                chat_sum += 1
            if guests > 0:
                users_list.append(f"{ICON_GUEST} {guests}, from: {_mention_link(s_chat_id, username, u_id, effective_clickable)}")
                chat_sum += guests
            if status == "notgoing":
                notgoing_list.append(f"{notgoing_icon} {_mention_link(s_chat_id, username, u_id, effective_clickable)}")

        child_data[str(s_chat_id)] = {
            "users_text":      "\n".join(users_list),
            "count":           chat_sum,
            "notgoing_text":   "\n".join(notgoing_list),
            "notgoing_count":  len(notgoing_list),
        }
        total_child_going += chat_sum

        try:
            chat_obj   = await context.bot.get_chat(
                int(s_chat_id) if s_chat_id.replace("-", "").isdigit() else s_chat_id
            )
            chat_title = chat_obj.title or "Child Group"
        except Exception:
            chat_title = "Child Group"

        if chat_sum > 0:
            # FIX: channel/group name without quotes around it
            block = (
                f"\n\n*Going from {escape_markdown(chat_title)}*"
                f" \\({chat_sum}\\):\n" + "\n".join(users_list)
            )
            child_addons_for_master.append(block)

    master_shares_block = "".join(child_addons_for_master)

   
    total_master_guests = sum(master_counters.values())
    total_master_going  = len(master_going) + total_master_guests
    current_post_total  = total_master_going
    global_total        = current_post_total + total_child_going

    master_clickable = clickability == "on"

    going_names_list = [
        f"{going_icon} {_mention_link(main_chat_id, u.split(' (')[0], u.split('(')[-1].rstrip(')') if '(' in u else None, master_clickable)}"
        for u in master_going
    ]

    # Guest lines are now folded directly into the Going list instead of a
    # separate "Guests:" section - one line per contributor, "N, from: Name".
    guest_lines = []
    for entry in master_going:
        u_name = entry.split(" (")[0]
        u_id   = entry.split("(")[-1].rstrip(")") if "(" in entry else None
        if master_counters.get(u_name, 0) > 0:
            guest_lines.append(f"{ICON_GUEST} {master_counters[u_name]}, from: {_mention_link(main_chat_id, u_name, u_id, master_clickable)}")
    # Also include guests from users who are not going (kicked users with guests)
    for k, count in master_counters.items():
        if k not in {u.split(" (")[0] for u in master_going} and count > 0:
            guest_lines.append(f"{ICON_GUEST} {count}, from: {_mention_link(main_chat_id, k, None, master_clickable)}")

    going_list_text = "\n".join(going_names_list + guest_lines)

    if notgoing_visibility == "visible":
        not_going_list_text = (
            "\n".join(
                f"{notgoing_icon} {_mention_link(main_chat_id, u.split(' (')[0], u.split('(')[-1].rstrip(')') if '(' in u else None, master_clickable)}"
                for u in master_not_going
            )
            if master_not_going else ""
        )
        notgoing_section = f"\n\n*Not Going* \\({len(master_not_going)}\\):\n{not_going_list_text}"
    elif notgoing_visibility == "onlycount":
        notgoing_section = f"\n\n*Not Going:* {len(master_not_going)}"
    else:
        notgoing_section = ""

    # Header: changed wording
    header      = f"{ICON_WARNING} *SQUAD VERIFICATION*\n_Review members before save_\n\n" if event_status == 1 else ""
    date_line   = f"{ICON_CLOCK} {escape_markdown(event_date)}\n" if event_date else ""
    title_line  = f"{ICON_CANCEL_EVENT} *CANCELED* ~{escape_markdown(name)}~" if event_status == -1 else f"*{escape_markdown(name)}*"

    if waitlist_visibility == "visible":
        wl_count, wl_text = await _render_waitlist_all(master_waitlist, main_chat_id, context, master_clickable)
        waitlist_section = f"\n\n*Waitlist* \\({wl_count}\\):\n{wl_text}"
    elif waitlist_visibility == "onlycount":
        wl_count = _render_waitlist_count(master_waitlist)
        waitlist_section = f"\n\n*Waitlist:* {wl_count}"
    else:
        waitlist_section = ""

    master_text = (
        f"{header}{title_line}\n\n {date_line}\n"
        f"*Going* \\({total_master_going}\\):\n{going_list_text}"
        f"{notgoing_section}"
        f"{master_shares_block}"
        f"{waitlist_section}\n\n"
        f"{ICON_TOTAL} *TOTAL Going:* {global_total}"
    )

    # Keyboard buttons for master (verification mode needs child rows too)
    with get_connection() as conn2:
        cursor2 = conn2.cursor()
        cursor2.execute(
            "SELECT username, guests, status, user_id, chat_id FROM event_users "
            "WHERE event_id = ? AND (guests > 0 OR status IN ('going', 'kicked'))",
            (event_id,),
        )
        all_child_going_for_buttons_raw = cursor2.fetchall()

    # Verification-mode participant buttons show a resolved "First Last"
    # name (matching the message text's own _mention_link format)
    # instead of the bare username - keyboard.py stays DB-free by design,
    # so this dict is built here and passed in ready-made.
    display_names = {}
    if event_status == 1:
        with get_connection() as conn3:
            cursor3 = conn3.cursor()

            def _resolve(uname, uid_hint=None, resolve_chat_id=None):
                resolve_chat_id = resolve_chat_id or main_chat_id
                uid = uid_hint
                if not uid or not str(uid).lstrip("-").isdigit():
                    cursor3.execute(
                        "SELECT user_id FROM main_group_users WHERE chat_id = ? AND username = ?",
                        (str(resolve_chat_id), uname),
                    )
                    row = cursor3.fetchone()
                    uid = row[0] if row else None
                if uid and str(uid).lstrip("-").isdigit():
                    display_names[uname] = get_display_name(str(resolve_chat_id), str(uid), uname)

            for entry in master_going:
                u_name = entry.split(" (")[0]
                u_id   = entry.split("(")[-1].rstrip(")") if "(" in entry else None
                _resolve(u_name, u_id)
            for u_name in master_kicked:
                _resolve(u_name)
            for u_name in master_counters:
                _resolve(u_name)
            for ch_username, _, _, ch_user_id, ch_chat_id in all_child_going_for_buttons_raw:
                _resolve(ch_username, ch_user_id, resolve_chat_id=ch_chat_id)

    all_child_going_for_buttons = [(u, g, s) for u, g, s, _, _ in all_child_going_for_buttons_raw]

    is_full = total_limit is not None and global_total >= total_limit

    master_keyboard = create_event_keyboard(
        event_id, event_status, going_icon, notgoing_icon,
        master_going, master_counters,
        is_child=False,
        child_users_rows=all_child_going_for_buttons,
        kicked_users=master_kicked,
        verification_enabled=verification_enabled,
        add_extra_member_enabled=add_extra_member_enabled,
        is_full=is_full,
        display_names=display_names,
    )

    try:
        await _edit_message_text_with_retry(
            context,
            chat_id=int(main_chat_id),
            message_id=int(main_msg_id),
            text=master_text,
            reply_markup=master_keyboard,
            parse_mode="MarkdownV2",
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Master view sync failed: {e}")
    except Exception as e:
        logger.error(f"Master view sync failed: {e}")

    # ── Child views ───────────────────────────────────────────────────────
    # Prefetch every chat title exactly once. The old version called
    # get_chat() for the main hub's title inside every iteration, AND for
    # every OTHER share's title inside every iteration's own render pass -
    # an O(N^2) storm of API calls for N shares, all sequential. That alone
    # could make a several-child event take a very long time to refresh.
    title_cache = {}

    async def _get_title(chat_ref):
        key = str(chat_ref)
        if key not in title_cache:
            try:
                obj = await context.bot.get_chat(
                    int(chat_ref) if str(chat_ref).replace("-", "").isdigit() else chat_ref
                )
                title_cache[key] = obj.title or "Group"
            except Exception:
                title_cache[key] = "Group"
        return title_cache[key]

    main_title          = await _get_title(main_chat_id)
    escaped_main_title  = escape_markdown(main_title)
    child_title_name    = (
        f"{ICON_CANCEL_EVENT} *CANCELED* ~{escape_markdown(name)}~"
        if event_status == -1 else f"*{escape_markdown(name)}*"
    )
    for s_chat_id, _, _, _, _, _ in shares:
        await _get_title(s_chat_id)

    async def _render_and_edit_child(s_chat_id, s_msg_id, mode, share_notgoing_viz=None, share_waitlist_viz=None, share_clickability=None):
        c_info = child_data.get(str(s_chat_id), {"users_text": "", "count": 0, "notgoing_text": "", "notgoing_count": 0})
        effective_notgoing_viz = share_notgoing_viz if share_notgoing_viz else notgoing_visibility
        effective_waitlist_viz = share_waitlist_viz if share_waitlist_viz else waitlist_visibility
        effective_clickable = (share_clickability if share_clickability else clickability) == "on"

        if mode == "-visible":
            child_text = (
                f"{ICON_SHARED} {child_title_name}\n"
                f"{date_line} \n"
                f"*Going from {escaped_main_title}* \\({current_post_total}\\):\n{going_list_text}\n\n"
            )
            for other_id, _, _, _, _, _ in shares:
                if str(other_id) != str(s_chat_id):
                    o_title = title_cache.get(str(other_id), "Group")
                    o_info  = child_data.get(str(other_id), {"users_text": "", "count": 0})
                    if o_info["count"] > 0:
                        child_text += (
                            f"*Going from {escape_markdown(o_title)}*"
                            f" \\({o_info['count']}\\):\n{o_info['users_text']}\n\n"
                        )

        elif mode == "-onlycount":
            child_text = (
                f"{ICON_SHARED} {child_title_name}\n"
                f"{date_line} \n"
                f"*Going from {escaped_main_title}:* {current_post_total}\n\n"
            )
            for other_id, _, _, _, _, _ in shares:
                if str(other_id) != str(s_chat_id):
                    o_title = title_cache.get(str(other_id), "Group")
                    o_info  = child_data.get(str(other_id), {"count": 0})
                    child_text += f"*Going from {escape_markdown(o_title)}:* {o_info['count']}\n"
            child_text += "\n"

        else:  # "-hidden"
            child_text = (
                f"{ICON_SHARED} {child_title_name}\n\n_Data hidden by admin\\._\n"
                f"{date_line} \n"
            )

        if effective_notgoing_viz == "visible":
            child_notgoing_section = f"*Not Going* \\({c_info['notgoing_count']}\\):\n{c_info['notgoing_text']}\n\n"
        elif effective_notgoing_viz == "onlycount":
            child_notgoing_section = f"*Not Going:* {c_info['notgoing_count']}\n\n"
        else:
            child_notgoing_section = ""

        if effective_waitlist_viz == "visible":
            wl_count, wl_text = _render_waitlist_local(master_waitlist, s_chat_id, effective_clickable)
            child_waitlist_section = f"*Waitlist* \\({wl_count}\\):\n{wl_text}\n\n"
        elif effective_waitlist_viz == "onlycount":
            wl_count = _render_waitlist_count(master_waitlist)
            child_waitlist_section = f"*Waitlist:* {wl_count}\n\n"
        else:
            child_waitlist_section = ""

        child_text += (
            f"*Going here:* \\({c_info['count']}\\)\n{c_info['users_text']}\n\n"
            f"{child_notgoing_section}"
            f"{child_waitlist_section}"
            f"{ICON_TOTAL} *Total Going \\(all groups\\):* {global_total}\n"
        )

        child_keyboard = create_event_keyboard(
            event_id, event_status, going_icon, notgoing_icon, is_child=True,
            verification_enabled=verification_enabled,
            is_full=is_full,
            add_extra_member_enabled=add_extra_member_enabled,
        )
        try:
            await _edit_message_text_with_retry(
                context,
                chat_id=int(s_chat_id) if str(s_chat_id).replace("-", "").isdigit() else s_chat_id,
                message_id=int(s_msg_id),
                text=child_text,
                reply_markup=child_keyboard,
                parse_mode="MarkdownV2",
            )
        except BadRequest as e:
            if "Message is not modified" not in str(e):
                logger.error(f"Child view update failed for {s_chat_id}: {e}")
        except Exception as e:
            logger.error(f"Child view update failed for {s_chat_id}: {e}")

    if shares:
        # Fire every child chat's update concurrently rather than one at a
        # time - previously a slow/rate-limited edit on ONE chat delayed the
        # update of every other chat queued behind it in the loop.
        # return_exceptions=True keeps one failing/rate-limited chat from
        # aborting the others (each already has its own try/except above,
        # this is just an extra safety net around the gather itself).
        await asyncio.gather(
            *[
                _render_and_edit_child(s_chat_id, s_msg_id, mode, share_ngl, share_wl, share_clc)
                for s_chat_id, s_msg_id, mode, share_ngl, share_wl, share_clc in shares
            ],
            return_exceptions=True,
        )


# ---------------------------------------------------------------------------
# Button handler (main state machine)
# ---------------------------------------------------------------------------

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    The main state machine: every inline keyboard click across every chat
    (master hub or child) comes through here. Broad shape of what happens
    on each click:

      1. Parse callback_data into (action, event_id) - or
         (action, event_id, target_username) for per-person actions like
         kick_<id>:<username> during verification.
      2. Figure out whether this click came from the master hub or a child
         chat (click_chat_id vs the event's own chat_id), and whether the
         clicker is an admin (needed to gate admin-only actions: close,
         cancel, kick/return, save, add-extra-player).
      3. Acquire this event's lock (get_event_lock) so two near-simultaneous
         clicks on the SAME event can't interleave their read-modify-write
         and silently drop one - this is the one place all DB writes for
         an event go through.
      4. Inside the lock: read the current event row, apply exactly ONE
         state change based on (event_status, action, is_child), then
         write it back in a single UPDATE.
      5. Outside the lock (after committing): schedule a re-render of every
         view of this event via schedule_view_refresh(), and best-effort
         log the action to the Google Sheets "Actions" tab.

    event_status branches (see keyboard.py's create_event_keyboard for the
    matching button layout each one shows):
      0  (open)         - going/notgoing/add/sub/close/cancel
      1  (verification)  - kick/return/incgst/decgst/addext/save, master
                            hub only; child-chat clicks during verification
                            only ever touch guest counts/kick-return for
                            THAT child's own participants (event_users table)
      2  (closed) / -1 (canceled) - no buttons are shown at all (see
                            create_event_keyboard), so this function should
                            never actually receive a click for these -
                            treated as a no-op safety net if it somehow does.
    """
    query         = update.callback_query
    callback_data = query.data
    click_chat_id = str(query.message.chat_id)
    user          = query.from_user
    user_id       = user.id

    # FIX: no '(id1234)' suffix when user has no @username
    username_raw = user.username if user.username else (user.first_name or f"user{user_id}")

    try:
        await query.answer()
    except Exception as e:
        logger.error(f"Failed to answer callback: {e}")

    if callback_data == "noop":
        return

    if callback_data.startswith("help_"):
        # These belong to help_callback_handler/help_back_handler - should
        # never reach here if those are registered before button_handler in
        # main.py, but guard anyway rather than silently misparsing
        # "help_alias" as action="help", event_id="alias".
        return

    action = target_username = event_id = None

    try:
        if ":" in callback_data:
            action_prefix, target_username = callback_data.split(":", 1)
            action, event_id = (
                action_prefix.split("_", 1) if "_" in action_prefix else (None, None)
            )
        else:
            action, event_id = (
                callback_data.split("_", 1) if "_" in callback_data else (None, None)
            )
    except Exception as e:
        logger.error(f"Callback parse error: {e}")
        return

    if not action or not event_id:
        return

    is_admin = await is_real_admin(context.bot, query.message.chat.id, user)

    data_changed = False
    event_status = 0

    lock = get_event_lock(event_id)
    async with lock:
        # track_user() opens its OWN separate SQLite connection - calling it
        # from inside the transaction below (before it commits) causes
        # "database is locked", since two connections would be trying to
        # write to the same file at once. Collect what needs tracking here,
        # and only actually call track_user() after the transaction below
        # has committed and closed.
        pending_track_user = []
        try:
            with get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT chat_id, message_id, name, going_icon, notgoing_icon,
                           event_status, going_data, notgoing_data, counters_data, event_date, kicked_data,
                           feature_snapshot, total_limit, waitlist_data, created_by_user_id
                    FROM events WHERE event_id = ?
                    """,
                    (event_id,),
                )
                row = cursor.fetchone()
                if not row:
                    return

                (main_chat_id, main_msg_id, name, going_icon, notgoing_icon,
                 event_status, going_data, notgoing_data, counters_data, event_date, kicked_data,
                 feature_snapshot_raw, total_limit, waitlist_data_raw, created_by_user_id) = row

                # NULL/malformed -> "everything enabled", matching how this
                # event always behaved before feature_snapshot existed.
                try:
                    feature_snapshot = json.loads(feature_snapshot_raw) if feature_snapshot_raw else {}
                except (TypeError, ValueError):
                    feature_snapshot = {}
                verification_enabled = feature_snapshot.get("verification", True)
                add_extra_member_enabled = feature_snapshot.get("add_extra_member", True)

                going     = json.loads(going_data)
                not_going = set(json.loads(notgoing_data))
                counters  = json.loads(counters_data)
                kicked    = json.loads(kicked_data or "[]")

                # ── One-time migration to the unified event_users model ────────
                # See db.migrate_event_to_event_users for the full explanation -
                # shared with handlers.py's /editevent, which also needs an
                # accurate event_users state before button_handler ever runs.
                migrate_event_to_event_users(cursor, event_id, main_chat_id, going, not_going, counters, kicked)
                waitlist  = json.loads(waitlist_data_raw or "[]")

                def _current_headcount():
                    """Reads-only, uses the SAME cursor/connection already
                    open here - never call db.get_event_total_going_headcount()
                    from inside this transaction, it opens its own connection
                    and would deadlock against this one.

                    Single unified query across the WHOLE event_id (no
                    chat_id filter) - Variant B means the master hub's own
                    participants live in event_users too (chat_id=main_chat_id),
                    the same as any child chat, so counting them separately
                    from a child-scoped query would double-count anyone
                    already migrated there.
                    """
                    cursor.execute(
                        "SELECT COALESCE(SUM(1 + guests), 0) FROM event_users WHERE event_id = ? AND status = 'going'",
                        (event_id,),
                    )
                    return cursor.fetchone()[0]

                def _is_at_capacity():
                    return total_limit is not None and _current_headcount() >= total_limit

                def _promote_child(target_chat_id):
                    """Tries to promote ONE waiting entry (person or guest
                    slot) for target_chat_id, skipping stale guest-slot
                    entries whose owner is no longer going (tried the next
                    one instead, at no cost to the caller). Returns
                    (username, user_id, is_guest) on success, or None if
                    there was nothing valid to promote. Mutates `waitlist`
                    and `pending_track_user` via nonlocal/closure."""
                    nonlocal waitlist
                    this_chat_waiting = [e for e in waitlist if str(e.get("chat_id")) == str(target_chat_id)]
                    this_chat_waiting.sort(key=lambda e: e.get("timestamp", ""))
                    for candidate in this_chat_waiting:
                        if candidate.get("is_guest"):
                            cursor.execute(
                                "SELECT status, guests FROM event_users WHERE event_id = ? AND chat_id = ? AND user_id = ?",
                                (event_id, target_chat_id, candidate["user_id"]),
                            )
                            owner_row = cursor.fetchone()
                            if owner_row and owner_row[0] == "going":
                                cursor.execute(
                                    "UPDATE event_users SET guests = ? WHERE event_id = ? AND chat_id = ? AND user_id = ?",
                                    (owner_row[1] + 1, event_id, target_chat_id, candidate["user_id"]),
                                )
                                waitlist = [e for e in waitlist if e is not candidate]
                                return (candidate["username"], candidate["user_id"], True)
                            waitlist = [e for e in waitlist if e is not candidate]
                            continue
                        else:
                            waitlist = [e for e in waitlist if e is not candidate]
                            cursor.execute(
                                "INSERT OR REPLACE INTO event_users (event_id, chat_id, user_id, username, first_name, last_name, status, guests) VALUES (?, ?, ?, ?, ?, ?, 'going', 0)",
                                (event_id, target_chat_id, candidate["user_id"], candidate["username"], candidate.get("first_name"), candidate.get("last_name")),
                            )
                            pending_track_user.append(
                                (target_chat_id, candidate["username"], candidate["user_id"], candidate.get("first_name"), candidate.get("last_name"))
                            )
                            return (candidate["username"], candidate["user_id"], False)
                    return None

                waitlist_promotion = None  # set below if a notgoing/sub click frees a slot
                extra_promotions = []  # additional (chat_id, username, user_id, is_guest) tuples beyond
                                        # waitlist_promotion - for actions like dropall that can free
                                        # MULTIPLE slots at once and need more than one announcement

                if event_status in (2, -1):
                    return

                going_usernames    = {u.split(" (")[0] for u in going}

                # ── Cross-chat protection ─────────────────────────────────────
                # Now purely event_users-based - the master hub's own
                # participants live there too (chat_id=main_chat_id), so a
                # 'going' row for the hub is found the SAME way as any
                # child chat's row, with no separate master-vs-child logic
                # needed here anymore.
                if action in ["going", "add", "sub"]:
                    user_already_registered = False

                    cursor.execute(
                        "SELECT chat_id FROM event_users WHERE event_id = ? AND user_id = ? AND status = 'going'",
                        (event_id, str(user_id)),
                    )
                    for (recorded_chat_id,) in cursor.fetchall():
                        if str(recorded_chat_id) != str(click_chat_id):
                            user_already_registered = True
                            break

                    # Also block if they're already QUEUED in another chat's
                    # Waitlist for this same event - without this, someone
                    # stuck waiting in chat A could click Going in chat B
                    # (also full) and end up double-queued in both, rather
                    # than getting the same "already added elsewhere"
                    # warning that a confirmed 'going' registration gets.
                    # Only applies to fresh "going" attempts - "add"/"sub"
                    # (guest count adjustments) mean they're ALREADY
                    # confirmed going in THIS chat, so being separately
                    # waitlisted elsewhere doesn't create the same conflict.
                    if not user_already_registered and action == "going":
                        for entry in waitlist:
                            if str(entry.get("user_id")) == str(user_id) and str(entry.get("chat_id")) != str(click_chat_id):
                                user_already_registered = True
                                break

                    if user_already_registered:
                        try:
                            await query.answer(
                                text=f"{ICON_WARNING} You are already added to this event in another group/channel",
                                show_alert=True,
                            )
                        except Exception:
                            pass
                        return

                # ── Going/Not Going/Add/Drop/ALL - unified for every chat ──────
                # Variant B: the main hub is no longer special-cased here -
                # it's just another chat_id in event_users, using the exact
                # same code every child chat has always used for these 5
                # actions. Only genuinely master-only actions (Verify,
                # Save & Close, Cancel, Kick/Return/±guest during
                # verification, Add Extra Member) fall through to the
                # admin-only section below instead.
                if action in ["going", "notgoing", "add", "sub", "dropall"]:
                    # Heal a stale unresolvable-entry placeholder (keyed by
                    # username-as-id, from a legacy Add Extra Member
                    # addition) - a real click always carries a genuine,
                    # numeric user_id, so any OTHER row for this same
                    # username with a non-matching, non-numeric id is
                    # superseded. "Rename" it to the real id (preserving
                    # its guests/status) rather than deleting it outright,
                    # unless a row under the real id already exists too -
                    # then that one is authoritative and the placeholder
                    # is simply discarded to avoid a primary-key clash.
                    cursor.execute(
                        "SELECT user_id FROM event_users WHERE event_id = ? AND chat_id = ? AND username = ?",
                        (event_id, click_chat_id, username_raw),
                    )
                    for (existing_uid,) in cursor.fetchall():
                        if str(existing_uid) != str(user_id) and not str(existing_uid).lstrip("-").isdigit():
                            cursor.execute(
                                "SELECT 1 FROM event_users WHERE event_id = ? AND chat_id = ? AND user_id = ?",
                                (event_id, click_chat_id, str(user_id)),
                            )
                            if cursor.fetchone() is None:
                                cursor.execute(
                                    "UPDATE event_users SET user_id = ? WHERE event_id = ? AND chat_id = ? AND user_id = ?",
                                    (str(user_id), event_id, click_chat_id, existing_uid),
                                )
                            else:
                                cursor.execute(
                                    "DELETE FROM event_users WHERE event_id = ? AND chat_id = ? AND user_id = ?",
                                    (event_id, click_chat_id, existing_uid),
                                )

                    cursor.execute(
                        "SELECT status, guests FROM event_users WHERE event_id = ? AND chat_id = ? AND user_id = ?",
                        (event_id, click_chat_id, str(user_id)),
                    )
                    u_row          = cursor.fetchone()
                    current_status = u_row[0] if u_row else "none"
                    current_guests = u_row[1] if u_row else 0

                    if action == "going":
                        already_waiting = any(
                            str(e.get("user_id")) == str(user_id) and str(e.get("chat_id")) == str(click_chat_id)
                            for e in waitlist
                        )
                        if current_status != "going" and _is_at_capacity():
                            if not already_waiting:
                                waitlist.append({
                                    "chat_id": str(click_chat_id),
                                    "user_id": str(user_id),
                                    "first_name": user.first_name,
                                    "last_name": user.last_name,
                                    "username": username_raw,
                                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                })
                                data_changed = True
                            pending_track_user.append(
                                (click_chat_id, username_raw, str(user_id), user.first_name, user.last_name)
                            )
                            try:
                                await query.answer(text=f"{ICON_STANDBY} Event is full - you've been added to the Waitlist", show_alert=True)
                            except Exception:
                                pass
                        else:
                            # In child chats, Going should only set status to 'going', never toggle off
                            cursor.execute(
                                "INSERT OR REPLACE INTO event_users (event_id, chat_id, user_id, username, first_name, last_name, status, guests) VALUES (?, ?, ?, ?, ?, ?, 'going', ?)",
                                (event_id, click_chat_id, str(user_id), username_raw, user.first_name, user.last_name, current_guests),
                            )
                            pending_track_user.append(
                                (click_chat_id, username_raw, str(user_id), user.first_name, user.last_name)
                            )
                            data_changed = True
                    elif action == "notgoing":
                        # Always keep a permanent row - guests=0 no longer
                        # means "delete", since the DB should retain full
                        # information about anyone who ever clicked on the
                        # event at all, independent of their guest count.
                        cursor.execute(
                            "INSERT OR REPLACE INTO event_users (event_id, chat_id, user_id, username, first_name, last_name, status, guests) VALUES (?, ?, ?, ?, ?, ?, 'notgoing', ?)",
                            (event_id, click_chat_id, str(user_id), username_raw, user.first_name, user.last_name, current_guests),
                        )
                        pending_track_user.append(
                            (click_chat_id, username_raw, str(user_id), user.first_name, user.last_name)
                        )
                        data_changed = True
                        if current_status == "going":
                            promoted = _promote_child(click_chat_id)
                            if promoted:
                                p_username, p_user_id, p_is_guest = promoted
                                waitlist_promotion = (click_chat_id, p_username, p_user_id, p_is_guest)
                    elif action == "add":
                        if _is_at_capacity():
                            waitlist.append({
                                "chat_id": str(click_chat_id),
                                "user_id": str(user_id),
                                "first_name": user.first_name,
                                "last_name": user.last_name,
                                "username": username_raw,
                                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                "is_guest": True,
                            })
                            data_changed = True
                            pending_track_user.append(
                                (click_chat_id, username_raw, str(user_id), user.first_name, user.last_name)
                            )
                            try:
                                await query.answer(text=f"{ICON_STANDBY} Event is full - your guest has been added to the Waitlist", show_alert=True)
                            except Exception:
                                pass
                        else:
                            # NOTE: does NOT force status='going' - Add Guest
                            # only ever touches the guest counter, completely
                            # independent of whether the clicker is going/not
                            # going/undeclared. 'notselected' is the explicit
                            # status for someone who's only ever added guests
                            # for themselves without personally declaring
                            # going or not going at all.
                            preserved_status = current_status if current_status != "none" else "notselected"
                            cursor.execute(
                                "INSERT OR REPLACE INTO event_users (event_id, chat_id, user_id, username, first_name, last_name, status, guests) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                (event_id, click_chat_id, str(user_id), username_raw, user.first_name, user.last_name, preserved_status, current_guests + 1),
                            )
                            pending_track_user.append(
                                (click_chat_id, username_raw, str(user_id), user.first_name, user.last_name)
                            )
                            data_changed = True
                    elif action == "sub":
                        # NOTE: In child chats, user must have status (going/notgoing)
                        # Drop only decrements guests, never removes the user
                        if current_guests > 0:
                            new_guests = current_guests - 1
                            cursor.execute(
                                "UPDATE event_users SET guests = ?, first_name = ?, last_name = ? WHERE event_id = ? AND chat_id = ? AND user_id = ?",
                                (new_guests, user.first_name, user.last_name, event_id, click_chat_id, str(user_id)),
                            )
                            pending_track_user.append(
                                (click_chat_id, username_raw, str(user_id), user.first_name, user.last_name)
                            )
                            data_changed = True
                            # Removing a guest frees a capacity slot too - promote
                            # whoever's been waiting longest for this chat, same as
                            # a notgoing click freeing a slot.
                            promoted = _promote_child(click_chat_id)
                            if promoted:
                                p_username, p_user_id, p_is_guest = promoted
                                waitlist_promotion = (click_chat_id, p_username, p_user_id, p_is_guest)
                        else:
                            return
                    elif action == "dropall":
                        # Drops ALL of the clicking user's own guests at once
                        # (sets guests to 0), rather than one at a time via
                        # "sub". Frees as many capacity slots as guests were
                        # dropped - promotes once per freed slot, collecting
                        # every promotion beyond the first into
                        # extra_promotions so the caller can announce all of
                        # them, not just one.
                        if current_guests > 0:
                            cursor.execute(
                                "UPDATE event_users SET guests = 0, first_name = ?, last_name = ? WHERE event_id = ? AND chat_id = ? AND user_id = ?",
                                (user.first_name, user.last_name, event_id, click_chat_id, str(user_id)),
                            )
                            pending_track_user.append(
                                (click_chat_id, username_raw, str(user_id), user.first_name, user.last_name)
                            )
                            data_changed = True
                            for _ in range(current_guests):
                                promoted = _promote_child(click_chat_id)
                                if not promoted:
                                    break
                                p_username, p_user_id, p_is_guest = promoted
                                if waitlist_promotion is None:
                                    waitlist_promotion = (click_chat_id, p_username, p_user_id, p_is_guest)
                                else:
                                    extra_promotions.append((click_chat_id, p_username, p_user_id, p_is_guest))
                        else:
                            return

                    # Persist waitlist_data here too - this branch commits
                    # and returns early, entirely bypassing the shared final
                    # UPDATE further down (which only the master/open-event
                    # path reaches). Without this, every in-memory
                    # waitlist.append()/promotion above in this child branch
                    # would silently vanish on commit.
                    cursor.execute(
                        "UPDATE events SET waitlist_data = ? WHERE event_id = ?",
                        (json.dumps(waitlist), event_id),
                    )
                    conn.commit()

                    for t_chat_id, t_username, t_user_id, t_first_name, t_last_name in pending_track_user:
                        track_user(t_chat_id, t_username, "active", user_id=t_user_id,
                                   first_name=t_first_name, last_name=t_last_name)

                    if data_changed:
                        try:
                            sheet_target = await get_sheet_for_chat(main_chat_id)
                            if sheet_target:
                                ss = await open_spreadsheet(sheet_target)
                                ws = await ss.worksheet("Actions")
                                await ws.append_row([
                                    event_id, action.upper(), username_raw,
                                    str(user_id), now2ddmmyy(), str(click_chat_id),
                                ])
                        except Exception as e:
                            logger.error(f"Sheets child action log failed: {e}")
                        context.application.create_task(schedule_view_refresh(context, event_id))

                    await _send_promotion_announcements(context, waitlist_promotion, extra_promotions)

                    return

                # Everything past this point (Verify, Save & Close, Cancel,
                # Kick/Return/±guest during verification, Add Extra Member)
                # is genuinely master-only - none of it makes sense from a
                # child chat's own post, regardless of the clicker's admin
                # status there.
                if int(click_chat_id) != int(main_chat_id):
                    try:
                        await query.answer(
                            text="⛔️ This action is only available from the main event post.",
                            show_alert=True,
                        )
                    except Exception:
                        pass
                    return

                # ── Admin-only actions guard ──────────────────────────────────
                # close/directclose/save (the Verify&Close flow) also allow
                # the event's OWN creator, not just group admins - /newevent
                # itself has no admin check, so before this a non-admin
                # creator had no way to ever close their own event. The
                # other actions here (kick, cancel, guest adjustments,
                # adding external members) stay strictly group-admin-only -
                # more sensitive moderation actions a random creator
                # shouldn't get unilateral power over.
                is_creator = created_by_user_id is not None and str(created_by_user_id) == str(user_id)
                if action in ["close", "directclose", "save"]:
                    if not (is_admin or is_creator):
                        try:
                            await query.answer(
                                text="⛔️ Only group admins or the event's creator can do this.",
                                show_alert=True,
                            )
                        except Exception:
                            pass
                        return
                elif action in ["kick", "incgst", "decgst", "addext", "cancel"]:
                    if not is_admin:
                        try:
                            await query.answer(text="⛔️ Only group admins can do this.", show_alert=True)
                        except Exception:
                            pass
                        return

                # ── Master open (event_status == 0) ───────────────────────────
                if event_status == 0:
                    if action == "close":
                        if not verification_enabled:
                            return
                        event_status = 1
                        data_changed = True
                    elif action == "directclose":
                        # Skips verification entirely - closes straight from
                        # OPEN, same as clicking "Save & Close Event" would
                        # from VERIFICATION, but with none of the review
                        # steps (no kicks, no manual guest edits, no extra
                        # members) since that state was never entered.
                        event_status = 2
                        data_changed = True
                    elif action == "cancel":
                        event_status = -1
                        data_changed = True

                # ── Master verification (event_status == 1) ───────────────────
                elif event_status == 1:
                    if action == "addext":
                        if not add_extra_member_enabled:
                            return
                        context.user_data["awaiting_extra_player_for"] = event_id
                        await query.message.reply_text(
                            "📝 *Verification Mode:* Type the extra member's username:",
                            parse_mode="MarkdownV2",
                        )
                        return

                    is_target_child  = target_username and target_username.startswith("ch-")
                    clean_target_usr = target_username.replace("ch-", "", 1) if is_target_child else target_username

                    if action == "kick" and target_username:
                        # 'kicked' is distinct from 'notselected' (guest-only,
                        # never declared going) so the keyboard can tell the
                        # two apart and only show Return for genuinely-kicked
                        # people - see create_event_keyboard's docstring.
                        if is_target_child:
                            cursor.execute(
                                "UPDATE event_users SET status = 'kicked' WHERE event_id = ? AND username = ?",
                                (event_id, clean_target_usr),
                            )
                        else:
                            cursor.execute(
                                "UPDATE event_users SET status = 'kicked' WHERE event_id = ? AND chat_id = ? AND username = ?",
                                (event_id, main_chat_id, clean_target_usr),
                            )
                        data_changed = True

                    elif action == "return" and target_username:
                        if is_target_child:
                            cursor.execute(
                                "UPDATE event_users SET status = 'going' WHERE event_id = ? AND username = ?",
                                (event_id, clean_target_usr),
                            )
                        else:
                            cursor.execute(
                                "UPDATE event_users SET status = 'going' WHERE event_id = ? AND chat_id = ? AND username = ?",
                                (event_id, main_chat_id, clean_target_usr),
                            )
                        data_changed = True

                    elif action == "incgst" and target_username:
                        if is_target_child:
                            cursor.execute(
                                "UPDATE event_users SET guests = guests + 1 WHERE event_id = ? AND username = ?",
                                (event_id, clean_target_usr),
                            )
                        else:
                            cursor.execute(
                                "UPDATE event_users SET guests = guests + 1 WHERE event_id = ? AND chat_id = ? AND username = ?",
                                (event_id, main_chat_id, clean_target_usr),
                            )
                        data_changed = True

                    elif action == "decgst" and target_username:
                        if is_target_child:
                            cursor.execute(
                                "SELECT guests FROM event_users WHERE event_id = ? AND username = ?",
                                (event_id, clean_target_usr),
                            )
                            cg_row = cursor.fetchone()
                            if cg_row and cg_row[0] > 0:
                                cursor.execute(
                                    "UPDATE event_users SET guests = guests - 1 WHERE event_id = ? AND username = ?",
                                    (event_id, clean_target_usr),
                                )
                        else:
                            cursor.execute(
                                "SELECT guests FROM event_users WHERE event_id = ? AND chat_id = ? AND username = ?",
                                (event_id, main_chat_id, clean_target_usr),
                            )
                            cg_row = cursor.fetchone()
                            if cg_row and cg_row[0] > 0:
                                cursor.execute(
                                    "UPDATE event_users SET guests = guests - 1 WHERE event_id = ? AND chat_id = ? AND username = ?",
                                    (event_id, main_chat_id, clean_target_usr),
                                )
                        data_changed = True

                    elif action == "save":
                        event_status = 2
                        data_changed = True

                cursor.execute(
                    "UPDATE events SET event_status = ?, waitlist_data = ? WHERE event_id = ?",
                    (event_status, json.dumps(waitlist), event_id),
                )
                conn.commit()

        except Exception as db_err:
            logger.error(f"SQLite transaction failure: {db_err}")
            return

        # Now that the transaction above has committed and its connection
        # is closed, it's safe for track_user() to open its own connection
        # without hitting "database is locked".
        for t_chat_id, t_username, t_user_id, t_first_name, t_last_name in pending_track_user:
            track_user(t_chat_id, t_username, "active", user_id=t_user_id,
                       first_name=t_first_name, last_name=t_last_name)

        await _send_promotion_announcements(context, waitlist_promotion, extra_promotions)

        # Log action to Sheets
        if data_changed:
            try:
                sheet_target = await get_sheet_for_chat(main_chat_id)
                if sheet_target:
                    ss           = await open_spreadsheet(sheet_target)
                    ws           = await ss.worksheet("Actions")
                    if action == "incgst":
                        logged_action = "ADD_editmode"
                    elif action == "decgst":
                        logged_action = "SUB_editmode"
                    else:
                        logged_action = action.upper()
                    await ws.append_row([
                        event_id, logged_action, username_raw,
                        str(user_id), now2ddmmyy(), str(click_chat_id),
                    ])
            except Exception as e:
                logger.error(f"Sheets master action log failed: {e}")

            context.application.create_task(schedule_view_refresh(context, event_id))

        # ── Save & Close Event: write ALL going users to EventUsers sheet ─
        if action in ("save", "directclose"):
            try:
                sheet_target = await get_sheet_for_chat(main_chat_id)
                ss           = await open_spreadsheet(sheet_target)
                if not ss:
                    pass  # free tier / no sheet configured / expired - SQLite-only save, nothing more to do
                else:
                    # 1. Query event_users ONCE for the whole event_id (no
                    #    chat_id filter) - Variant B migration means the
                    #    master hub's own participants live there too
                    #    (chat_id=main_chat_id), the same as any child
                    #    chat's, so this single query already covers
                    #    everyone with a resolvable numeric user_id.
                    with get_connection() as conn_eu:
                        cursor_eu = conn_eu.cursor()
                        cursor_eu.execute(
                            "SELECT user_id, status, guests FROM event_users WHERE event_id = ?",
                            (event_id,),
                        )
                        all_rows = cursor_eu.fetchall()

                    # Migration (which already ran at the start of this
                    # transaction) migrates even unresolvable Add Extra
                    # Member entries into event_users, using their
                    # username as a fallback identifying key - so a
                    # single query here already covers everyone, no
                    # separate scan of the frozen `going` list needed.
                    all_going_ids = [r[0] for r in all_rows if r[1] == "going"]

                    # Compute total for Events sheet - going users plus
                    # every guest (including from non-going/notselected
                    # users), summed once across the whole event_id.
                    total_going = sum(
                        (1 if status == "going" else 0) + (guests or 0)
                        for _uid, status, guests in all_rows
                    )

                    # 4. Update Events sheet row
                    ws      = await ss.worksheet("Events")
                    records = await ws.get_all_records()
                    found   = False
                    for idx, r in enumerate(records, start=2):
                        if str(r.get("EVENT_ID")) == str(event_id):
                            await ws.update(f"F{idx}:H{idx}", [[now2ddmmyy(), "CLOSED", total_going]])
                            found = True
                            break
                    if not found:
                        await ws.append_row([
                            event_id, name, now2ddmmyy(), username_raw,
                            event_date or "", now2ddmmyy(), "CLOSED", total_going,
                        ])

                    # 5. Write all going user_ids to EventUsers sheet
                    context.application.create_task(
                        sync_event_users_sheet(main_chat_id, event_id, all_going_ids)
                    )

            except Exception as e:
                logger.error(f"Sheets save pipeline failed: {e}")

        # ── Cancel Event: mark Events row as Canceled, write NOTHING to EventUsers ─
        if action == "cancel":
            try:
                sheet_target = await get_sheet_for_chat(main_chat_id)
                ss           = await open_spreadsheet(sheet_target)
                if not ss:
                    pass  # free tier / no sheet configured / expired - SQLite-only cancel, nothing more to do
                else:
                    ws      = await ss.worksheet("Events")
                    records = await ws.get_all_records()
                    found   = False
                    for idx, r in enumerate(records, start=2):
                        if str(r.get("EVENT_ID")) == str(event_id):
                            # Update existing row to CANCELED
                            await ws.update(f"F{idx}:H{idx}", [[now2ddmmyy(), "CANCELED", 0]])
                            found = True
                            break
                    if not found:
                        # Only append if row doesn't exist
                        await ws.append_row([
                            event_id, name, now2ddmmyy(), username_raw,
                            event_date or "", now2ddmmyy(), "CANCELED", 0,
                        ])
                    # Intentionally NOT calling sync_event_users_sheet here -
                    # a cancelled event must not write anything to EventUsers.
            except Exception as e:
                logger.error(f"Sheets cancel pipeline failed: {e}")

