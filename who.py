"""
/who - the two questions an admin otherwise answers by scrolling the Google Sheet:

  /who [hours] -ng            who changed to Not Going in the LAST event, within the last [hours]
  /who [hours] -l             who left the main group and every monitored chat, within the last [hours]
  /who [hours] -l -c <where>  the same, for one chat: an alias from /listaliases, "main" or "all"

`hours` is optional: without it the report covers the last 6 hours (utils.WHO_DEFAULT_HOURS).

Both read the hub's own Google Sheet - votes from the Actions tab, departures from UserPresenceLog (names
from Users) - so the command needs a bound sheet and the PRO-level "who" feature, and only admins may use it.

Names are printed WITHOUT "@" or links on purpose: the people listed are usually still in the chat, and a
mention would notify every one of them.
"""
from datetime import datetime, timedelta

from gspread.exceptions import WorksheetNotFound
from telegram import Update
from telegram.ext import ContextTypes

from config import logger, ICON_ADMIN_ONLY, ICON_WARNING
from db import get_connection
from hub_resolver import resolve_hub_chat_id, register_hub_command
from sheets import get_sheet_for_chat, open_spreadsheet
from subscription import require_feature
from utils import escape_markdown, is_real_admin, parse_stored_date, WHO_DEFAULT_HOURS

WHO_MAX_HOURS = 24 * 365
MAX_ROWS = 100          # per list - the rest is summarised as "...and N more"
CHUNK = 3900            # Telegram's limit is 4096 characters per message
MAIN, ALL = "main", "all"

_NG = {"-ng", "-notgoing"}
_LEFT = {"-l", "-left"}
_CHAT = {"-c", "-chat"}

# Actions that prove the clicker themself was in the event. KICK / RETURN / ADDEXT / CLOSE / SAVE / CANCEL /
# ADD_editmode ... are admin clicks - their USER_NAME is the ADMIN, not the person acted upon - so they say
# nothing about anyone's own status and are ignored.
GOING_LIKE = frozenset({"GOING", "ADD", "SUB", "DROPALL"})

USAGE = (
    "Usage: /who [hours] -ng | -l [-c <alias|main|all>]\n"
    f"  hours is optional - the last {WHO_DEFAULT_HOURS} h when left out\n"
    "  /who 48 -ng          who changed to Not Going in the last event (last 48 h)\n"
    f"  /who -l              who left the main group and every monitored chat (last {WHO_DEFAULT_HOURS} h)\n"
    "  /who 48 -l -c kids   only the chat with alias 'kids' (see /listaliases); main = the main group"
)


# ---------------------------------------------------------------------------
# Arguments and scope
# ---------------------------------------------------------------------------

def parse_who_args(args):
    """-> ({"hours", "ng", "left", "chat"}, None) or (None, error text). No number given = WHO_DEFAULT_HOURS."""
    hours, ng, left, chat = None, False, False, None
    args = [str(a) for a in (args or [])]
    i = 0
    while i < len(args):
        token, low = args[i], args[i].lower()
        if low in _NG:
            ng = True
        elif low in _LEFT:
            left = True
        elif low in _CHAT:
            if chat is not None:
                return None, "-c was given twice."
            if i + 1 >= len(args) or args[i + 1].lower() in (_NG | _LEFT | _CHAT):
                return None, "-c needs a value: an alias, main or all."
            chat = args[i + 1]
            i += 1
        elif token.isascii() and token.isdigit():
            if hours is not None:
                return None, "Give the number of hours only once."
            hours = int(token)
        else:
            return None, f"Don't understand '{token}'."
        i += 1
    if hours is None:
        hours = WHO_DEFAULT_HOURS
    if not 1 <= hours <= WHO_MAX_HOURS:
        return None, f"Hours must be between 1 and {WHO_MAX_HOURS}."
    if not (ng or left):
        return None, "Add -ng (changed to Not Going) and/or -l (left)."
    return {"hours": hours, "ng": ng, "left": left, "chat": chat}, None


def resolve_scope(hub_chat_id, chat_value):
    """
    Which chats a report covers. The main group is NOT an alias or a monitor (it is the hub itself, and
    /refreshusersall already handles it first), so it has the reserved name "main"; "all" is the main group
    plus every monitored chat. Anything else is an alias from /listaliases or a chat id known to this hub.

    -> (scope, None) or (None, error text); scope = {"is_all", "chats": {chat_id: label},
       "unmonitored": {chat_id}, "labels": {chat_id: label for every chat this hub knows}}.
    """
    hub = str(hub_chat_id)
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT chat_id, alias, chat_name, is_monitored FROM sub_chats WHERE owner_chat_id = ? OR owner_chat_id IS NULL",
            (hub,),
        ).fetchall()
    known, by_alias = {}, {}
    for cid, alias, name, monitored in rows:
        cid = str(cid)
        known[cid] = (alias or name or cid, bool(monitored))
        if alias:
            by_alias[alias.lower()] = cid
    labels = {hub: MAIN, **{cid: lab for cid, (lab, _m) in known.items() if cid != hub}}
    monitored_chats = {cid: lab for cid, (lab, mon) in known.items() if mon and cid != hub}

    value = (chat_value or ALL).strip()
    low = value.lower()
    if low == ALL:
        return {"is_all": True, "chats": {hub: MAIN, **monitored_chats}, "unmonitored": set(), "labels": labels}, None
    cid = hub if (low == MAIN or value == hub) else (by_alias.get(low) or (value if value in known else None))
    if cid is None:
        aliases = ", ".join(sorted(by_alias)) or "none yet"
        return None, f"No chat '{value}' here. Use main, all, or an alias from /listaliases (aliases: {aliases})."
    if cid == hub:
        return {"is_all": False, "chats": {hub: MAIN}, "unmonitored": set(), "labels": labels}, None
    label, monitored = known[cid]
    return {"is_all": False, "chats": {cid: label}, "unmonitored": set() if monitored else {cid}, "labels": labels}, None


# ---------------------------------------------------------------------------
# The two reports (pure - rows in, rows out)
# ---------------------------------------------------------------------------

def notgoing_changes(records, event_id, since, scope_chats=None):
    """
    People of ONE event whose status CHANGED to Not Going at or after `since`. A change means they had a
    Going-type action earlier in that same event (at any time) - someone whose very first action was Not
    Going just declined and is not reported. `scope_chats` limits it to clicks made in those chats
    (None = every chat). Rows are read in sheet order, which is chronological (the Actions tab is append-only).
    Newest first; `back_to_going` marks people who went back to Going afterwards.
    """
    status, found = {}, {}
    for r in records:
        if str(r.get("EVENT_ID", "")).strip() != str(event_id):
            continue
        uid = str(r.get("USER_ID", "")).strip()
        action = str(r.get("ACTION", "")).strip().upper()
        if not uid:
            continue
        if action in GOING_LIKE:
            status[uid] = "going"
        elif action == "NOTGOING":
            when = parse_stored_date(r.get("DATE"))
            chat = str(r.get("CHAT_ID", "")).strip()
            if (status.get(uid) == "going" and when is not None and when >= since
                    and (scope_chats is None or chat in scope_chats)):
                found[uid] = {"name": str(r.get("USER_NAME", "")).strip() or uid, "when": when, "chat": chat}
            status[uid] = "notgoing"
    for uid, item in found.items():
        item["back_to_going"] = status.get(uid) == "going"
    return sorted(found.values(), key=lambda item: item["when"], reverse=True)


def left_in_window(log_records, users_records, since, scope_chats):
    """
    {chat_id: [{"name", "when"}, ...]} newest first - every stay that ended at or after `since` in the given
    chats. UserPresenceLog is the primary source (one row per ended stay); Users rows currently marked LEFT
    are added as a safety net and de-duplicated against it. The time is when a refresh NOTICED the departure.
    """
    names, names_by_uid = {}, {}
    for r in users_records:
        uid, chat = str(r.get("USER_ID", "")).strip(), str(r.get("CHAT_ID", "")).strip()
        full = " ".join(p for p in (str(r.get("FIRST_NAME", "")).strip(), str(r.get("LAST_NAME", "")).strip()) if p)
        username = str(r.get("USER_NAME", "")).strip()
        name = f"{full} ({username})" if full and username and full != username else (full or username)
        if uid and name:
            names[(uid, chat)] = name
            names_by_uid.setdefault(uid, name)

    stays = {}

    def _add(uid, chat, raw_end):
        end = parse_stored_date(raw_end)
        if uid and end is not None and end >= since and chat in scope_chats:
            stays[(uid, chat, str(raw_end).strip())] = end

    for r in log_records:
        _add(str(r.get("USER_ID", "")).strip(), str(r.get("CHAT_ID", "")).strip(), r.get("DATE_end"))
    for r in users_records:
        if str(r.get("STATUS", "")).strip().upper() == "LEFT":
            _add(str(r.get("USER_ID", "")).strip(), str(r.get("CHAT_ID", "")).strip(), r.get("DATE_end"))

    by_chat = {}
    for (uid, chat, _raw), end in stays.items():
        by_chat.setdefault(chat, []).append(
            {"name": names.get((uid, chat)) or names_by_uid.get(uid) or f"user {uid}", "when": end})
    for items in by_chat.values():
        items.sort(key=lambda item: item["when"], reverse=True)
    return by_chat


# ---------------------------------------------------------------------------
# Formatting (MarkdownV2)
# ---------------------------------------------------------------------------

def _when(dt):
    return dt.strftime("%d.%m %H:%M")


def _capped(lines):
    E = escape_markdown
    if len(lines) <= MAX_ROWS:
        return lines
    return lines[:MAX_ROWS] + [E(f"...and {len(lines) - MAX_ROWS} more")]


def format_notgoing(event_name, hours, items, labels):
    E = escape_markdown
    title = "«" + event_name + "»"
    if not items:
        return E(f"No one changed to Not Going in {title} in the last {hours} h.")
    lines = [f"👥 *Changed to Not Going* \\- {E(title)}, last {hours} h \\({len(items)}\\)"]
    rows = []
    for item in items:
        label = labels.get(item["chat"], item["chat"])
        again = " (now Going again)" if item["back_to_going"] else ""
        rows.append(E(f"• {item['name']} - {_when(item['when'])} · {label}{again}"))
    return "\n".join(lines + _capped(rows))


def format_left(hours, by_chat, scope_chats):
    """scope_chats: {chat_id: label} - the main group first, the rest by label."""
    E = escape_markdown
    order = sorted(scope_chats, key=lambda cid: (scope_chats[cid] != MAIN, scope_chats[cid].lower()))
    total = sum(len(by_chat.get(cid, [])) for cid in order)
    note = "_" + E("Departures are recorded when /refreshusers or /refreshusersall runs.") + "_"
    if not total:
        return E(f"No one left in the last {hours} h.") + "\n" + note
    out = [f"🚪 *Left* \\- last {hours} h \\({total}\\)"]
    for cid in order:
        items = by_chat.get(cid, [])
        if items:
            out.append(f"*{E(scope_chats[cid])}*")
            out += _capped([E(f"• {i['name']} - {_when(i['when'])}") for i in items])
    out.append(note)
    return "\n".join(out)


def split_message(text, limit=CHUNK):
    """Splits on line boundaries so no MarkdownV2 entity is ever cut in half."""
    chunks, current = [], ""
    for line in text.split("\n"):
        if current and len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------

async def _records(ss, tab, required=True):
    try:
        ws = await ss.worksheet(tab)
    except WorksheetNotFound:
        if required:
            raise
        return []
    return await ws.get_all_records()


@register_hub_command("who")
async def who_command(update: Update, context: ContextTypes.DEFAULT_TYPE, override_chat_id: str = None):
    """See the module docstring. Admin-only, PRO-level feature "who", needs the hub's Google Sheet."""
    E = escape_markdown
    hub = await resolve_hub_chat_id(update, context, "who", override_chat_id)
    if hub is None:
        return
    if not await is_real_admin(context.bot, hub, update.effective_user, message=update.message):
        await update.message.reply_text(f"{ICON_ADMIN_ONLY} Only admins can use /who\\.", parse_mode="MarkdownV2")
        return
    if not await require_feature(update, "who", "/who", hub):
        return

    async def _warn(text, usage=False):
        body = f"{ICON_WARNING} {E(text)}" + (f"\n\n```\n{USAGE}\n```" if usage else "")
        await update.message.reply_text(body, parse_mode="MarkdownV2")

    parsed, error = parse_who_args(context.args)
    if error:
        await _warn(error, usage=True)
        return
    scope, error = resolve_scope(hub, parsed["chat"])
    if error:
        await _warn(error)
        return
    if parsed["left"] and scope["unmonitored"]:
        await _warn("That chat isn't monitored, so nobody's departures are tracked there. Add it with /addmonitor first.")
        return

    since = datetime.now() - timedelta(hours=parsed["hours"])
    sections = []
    try:
        ss = await open_spreadsheet(await get_sheet_for_chat(hub))
        if ss is None:
            await _warn("This group has no Google Sheet bound - /who reads the Actions and UserPresenceLog tabs. Bind one with /setsheet.")
            return
        if parsed["ng"]:
            with get_connection() as conn:
                event = conn.execute(
                    "SELECT event_id, name FROM events WHERE chat_id = ? ORDER BY ROWID DESC LIMIT 1", (hub,)
                ).fetchone()
            if event is None:
                sections.append(E("No events in this group yet - nothing to report."))
            else:
                records = await _records(ss, "Actions")
                items = notgoing_changes(records, event[0], since, None if scope["is_all"] else set(scope["chats"]))
                sections.append(format_notgoing(event[1], parsed["hours"], items, scope["labels"]))
        if parsed["left"]:
            log_records = await _records(ss, "UserPresenceLog", required=False)
            users_records = await _records(ss, "Users", required=False)
            by_chat = left_in_window(log_records, users_records, since, set(scope["chats"]))
            sections.append(format_left(parsed["hours"], by_chat, scope["chats"]))
    except Exception as e:
        logger.error(f"/who could not read Google Sheets: {repr(e)}")
        await _warn(f"Couldn't read Google Sheets right now ({str(e)[:80] or type(e).__name__}). Try again in a minute.")
        return

    for chunk in split_message("\n\n".join(sections)):
        await update.message.reply_text(chunk, parse_mode="MarkdownV2")
