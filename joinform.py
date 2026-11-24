"""
Join form (PRO feature "join_form"): a person answers a few questions with the bot BEFORE being let into a group / channel.

The flow (a bot cannot add anybody to a chat and cannot write to a person first - so the person opens a link, and the bot
answers with an invitation):
  1. An admin writes the hub's ONE form: /joinform add "Question" [choice1;choice2;...]  (list / remove / edit / reorder / preview).
  2. /joinlink <main | alias | chat id> makes a ONE-TIME link  t.me/<bot>?start=jf_<token>  for that chat. The admin sends it.
  3. The person opens it (Start). The first person to open it owns it; nobody else can use it. The bot asks the questions one by one
     (free text, or buttons for a question with choices). Progress is in the database, so a restart or a pause loses nothing and
     opening the link again continues where the person stopped.
  4. After the last answer the bot creates a one-time (member_limit 1) invitation to the chosen chat and sends it. The link is then
     used up. If the invitation cannot be made (the bot lost its rights there), the answers are kept, the admin is told, and opening
     the link again retries the invitation.
Answers: SQLite is the record (join_form_responses, one row per answer); with a bound sheet a row also goes to the JoinForm tab
(columns = the question texts, new questions add columns). A failed sheet write is only logged - the answers stay in SQLite.
"""
import asyncio
import json
import re
import secrets
from datetime import datetime, timedelta

from gspread.exceptions import WorksheetNotFound
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import logger, ICON_ADMIN_ONLY, ICON_WARNING
from db import get_connection
from hub_resolver import resolve_hub_chat_id, register_hub_command
from sheets import get_sheet_for_chat, open_spreadsheet
from subscription import require_feature, feature_available
from utils import is_real_admin, now2ddmmyy

FEATURE = "join_form"
MAX_QUESTIONS = 4
MAX_QUESTION_LEN = 200
MAX_CHOICES = 8
MAX_CHOICE_LEN = 40
MAX_ANSWER_LEN = 300
INVITE_TTL_HOURS = 72
SHEET_TAB = "JoinForm"
SHEET_BASE = ["TIMESTAMP", "USER_ID", "USERNAME", "NAME", "JOINED_TO"]
START_PREFIX = "jf_"
CALLBACK_PREFIX = "jfc_"

FORM_USAGE = (
    "/joinform list\n"
    "/joinform add \"Question\"                      - a question with a free-text answer\n"
    "/joinform add \"Question\" yes;no;maybe         - a question answered with buttons (2-8 choices, separated by ;)\n"
    "/joinform edit <n> \"New text\"                 - change the text of question n\n"
    "/joinform remove <n>\n"
    "/joinform reorder <n> <m>                     - swap questions n and m\n"
    "/joinform preview                             - the form as a person sees it"
)
LINK_USAGE = (
    "/joinlink <main | alias | chat id>   - a one-time link to the form; after it the person gets an invitation to that chat\n"
    "/joinlink list                       - the latest links and their state\n"
    "/joinlink revoke <token>             - make an unused link stop working"
)


# ---------------------------------------------------------------------------
# The form (database)
# ---------------------------------------------------------------------------
def get_questions(hub_chat_id) -> list:
    """[{id, position, question, choices (list or None)}], in order."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, position, question, choices FROM join_form_questions WHERE hub_chat_id = ? ORDER BY position, id",
            (str(hub_chat_id),),
        ).fetchall()
    return [{"id": r[0], "position": r[1], "question": r[2], "choices": json.loads(r[3]) if r[3] else None} for r in rows]


def _renumber(conn, hub_chat_id) -> None:
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM join_form_questions WHERE hub_chat_id = ? ORDER BY position, id", (str(hub_chat_id),)).fetchall()]
    for pos, qid in enumerate(ids, start=1):
        conn.execute("UPDATE join_form_questions SET position = ? WHERE id = ?", (pos, qid))


def add_question(hub_chat_id, text: str, choices=None) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO join_form_questions (hub_chat_id, position, question, choices) VALUES (?, 999999, ?, ?)",
            (str(hub_chat_id), text, json.dumps(choices, ensure_ascii=False) if choices else None),
        )
        _renumber(conn, hub_chat_id)
        conn.commit()


def remove_question(hub_chat_id, n: int) -> bool:
    questions = get_questions(hub_chat_id)
    if not 1 <= n <= len(questions):
        return False
    with get_connection() as conn:
        conn.execute("DELETE FROM join_form_questions WHERE id = ?", (questions[n - 1]["id"],))
        _renumber(conn, hub_chat_id)
        conn.commit()
    return True


def edit_question(hub_chat_id, n: int, text: str, choices=None) -> bool:
    questions = get_questions(hub_chat_id)
    if not 1 <= n <= len(questions):
        return False
    with get_connection() as conn:
        conn.execute(
            "UPDATE join_form_questions SET question = ?, choices = ? WHERE id = ?",
            (text, json.dumps(choices, ensure_ascii=False) if choices else None, questions[n - 1]["id"]),
        )
        conn.commit()
    return True


def swap_questions(hub_chat_id, n: int, m: int) -> bool:
    questions = get_questions(hub_chat_id)
    if not (1 <= n <= len(questions) and 1 <= m <= len(questions)):
        return False
    a, b = questions[n - 1], questions[m - 1]
    with get_connection() as conn:
        conn.execute("UPDATE join_form_questions SET position = ? WHERE id = ?", (b["position"], a["id"]))
        conn.execute("UPDATE join_form_questions SET position = ? WHERE id = ?", (a["position"], b["id"]))
        conn.commit()
    return True


_QUOTED = re.compile(r'^\s*["“„«](.+?)["”“»]\s*(.*)$', re.S)


def parse_question(rest: str):
    """`"Text" a;b;c` or just `Text` -> (text, choices or None, None) or (None, None, error)."""
    rest = (rest or "").strip()
    if not rest:
        return None, None, "Write the question after the command."
    m = _QUOTED.match(rest)
    text, tail = (m.group(1).strip(), m.group(2).strip()) if m else (rest, "")
    if not text:
        return None, None, "The question is empty."
    if len(text) > MAX_QUESTION_LEN:
        return None, None, f"A question can be up to {MAX_QUESTION_LEN} characters."
    choices = None
    if tail:
        choices = [c.strip() for c in tail.split(";") if c.strip()]
        if not 2 <= len(choices) <= MAX_CHOICES:
            return None, None, f"A question with buttons needs 2-{MAX_CHOICES} choices, separated by ;"
        if any(len(c) > MAX_CHOICE_LEN for c in choices):
            return None, None, f"A choice can be up to {MAX_CHOICE_LEN} characters."
        if len({c.lower() for c in choices}) != len(choices):
            return None, None, "The choices must differ from each other."
    return text, choices, None


def _describe(questions) -> str:
    if not questions:
        return "The form has no questions yet. Add one with /joinform add \"Question\"."
    lines = []
    for i, q in enumerate(questions, start=1):
        lines.append(f"{i}. {q['question']}" + (f"\n   buttons: {' | '.join(q['choices'])}" if q["choices"] else "   (free text)"))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Links (database)
# ---------------------------------------------------------------------------
def _link(token: str):
    with get_connection() as conn:
        r = conn.execute(
            "SELECT token, hub_chat_id, target_chat_id, created_by, created_at, status, user_id FROM join_form_links WHERE token = ?",
            (token,),
        ).fetchone()
    if not r:
        return None
    return dict(zip(("token", "hub", "target", "created_by", "created_at", "status", "user_id"), r))


def _set_link(token: str, **fields) -> None:
    with get_connection() as conn:
        conn.execute(
            f"UPDATE join_form_links SET {', '.join(k + ' = ?' for k in fields)} WHERE token = ?", (*fields.values(), token))
        conn.commit()


def _answers(token: str) -> dict:
    with get_connection() as conn:
        rows = conn.execute("SELECT question_id, question_text, answer FROM join_form_responses WHERE token = ? ORDER BY id", (token,)).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def _next_question(link):
    """The first question of the hub's form that this link has no answer to yet, or None."""
    done = _answers(link["token"])
    for q in get_questions(link["hub"]):
        if q["id"] not in done:
            return q
    return None


def _resolve_target(hub: str, value: str):
    """main / an alias / a chat id this hub knows -> (chat_id, label) or (None, error)."""
    low = value.strip().lower()
    if low == "main" or value.strip() == hub:
        return hub, "main"
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT chat_id, alias, chat_name FROM sub_chats WHERE owner_chat_id = ? OR owner_chat_id IS NULL", (hub,)).fetchall()
    for cid, alias, name in rows:
        if (alias and alias.lower() == low) or str(cid) == value.strip():
            return str(cid), alias or name or str(cid)
    aliases = ", ".join(sorted(a.lower() for _c, a, _n in rows if a)) or "none yet"
    return None, f"No chat '{value}' here. Use main, an alias from /listaliases or a chat id this group knows (aliases: {aliases})."


async def _can_invite(bot, chat_id):
    """(True, title) when the bot may make invitation links in that chat, else (False, why)."""
    try:
        chat = await bot.get_chat(int(chat_id))
        member = await bot.get_chat_member(int(chat_id), bot.id)
    except Exception as e:
        return False, f"I can't reach that chat: {e}"
    title = getattr(chat, "title", None) or str(chat_id)
    if member.status == "creator" or (member.status == "administrator" and getattr(member, "can_invite_users", False)):
        return True, title
    return False, f"I need to be an admin of {title} with the 'Invite users' right to make invitations."


# ---------------------------------------------------------------------------
# Admin commands
# ---------------------------------------------------------------------------
async def _admin_gate(update, context, command, override_chat_id):
    hub = await resolve_hub_chat_id(update, context, command, override_chat_id)
    if hub is None:
        return None
    if not await is_real_admin(context.bot, hub, update.effective_user, message=update.message):
        await update.message.reply_text(f"{ICON_ADMIN_ONLY} Only admins can use /{command}.")
        return None
    if not await require_feature(update, FEATURE, f"/{command}", hub):
        return None
    return str(hub)


@register_hub_command("joinform")
async def joinform_command(update: Update, context: ContextTypes.DEFAULT_TYPE, override_chat_id: str = None):
    """/joinform list | add | edit | remove | reorder | preview - the hub's one form. See the module docstring."""
    hub = await _admin_gate(update, context, "joinform", override_chat_id)
    if hub is None:
        return
    reply = update.message.reply_text
    parts = (update.message.text or "").split(None, 2)
    sub = parts[1].lower() if len(parts) > 1 else ""
    rest = parts[2] if len(parts) > 2 else ""
    questions = get_questions(hub)

    if sub in ("list", "preview"):
        head = "The form a person sees:" if sub == "preview" else "The join form:"
        await reply(f"{head}\n\n{_describe(questions)}")
    elif sub == "add":
        if len(questions) >= MAX_QUESTIONS:
            await reply(f"{ICON_WARNING} The form has {MAX_QUESTIONS} questions already - remove one first.")
            return
        text, choices, error = parse_question(rest)
        if error:
            await reply(f"{ICON_WARNING} {error}\n\n{FORM_USAGE}")
            return
        add_question(hub, text, choices)
        await reply(f"✅ Added as question {len(questions) + 1}.\n\n{_describe(get_questions(hub))}")
    elif sub in ("remove", "edit", "reorder"):
        nums = rest.split(None, 1)
        if not nums or not nums[0].isdigit():
            await reply(f"{ICON_WARNING} Give the question number.\n\n{FORM_USAGE}")
            return
        n = int(nums[0])
        if sub == "remove":
            ok = remove_question(hub, n)
        elif sub == "reorder":
            m = nums[1].strip() if len(nums) > 1 else ""
            if not m.isdigit():
                await reply(f"{ICON_WARNING} Give the two question numbers.\n\n{FORM_USAGE}")
                return
            ok = swap_questions(hub, n, int(m))
        else:
            text, choices, error = parse_question(nums[1] if len(nums) > 1 else "")
            if error:
                await reply(f"{ICON_WARNING} {error}\n\n{FORM_USAGE}")
                return
            ok = edit_question(hub, n, text, choices)
        if not ok:
            await reply(f"{ICON_WARNING} There is no such question (the form has {len(questions)}).")
            return
        await reply(f"✅ Done.\n\n{_describe(get_questions(hub))}")
    else:
        await reply(f"{ICON_WARNING} /joinform what?\n\n{FORM_USAGE}")


@register_hub_command("joinlink")
async def joinlink_command(update: Update, context: ContextTypes.DEFAULT_TYPE, override_chat_id: str = None):
    """/joinlink <target> | list | revoke <token> - one-time links to the form. See the module docstring."""
    hub = await _admin_gate(update, context, "joinlink", override_chat_id)
    if hub is None:
        return
    reply = update.message.reply_text
    args = context.args or []
    if not args:
        await reply(f"{ICON_WARNING} Where should the person be invited after the form?\n\n{LINK_USAGE}")
        return

    if args[0].lower() == "list":
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT token, target_chat_id, status, user_id, created_at FROM join_form_links WHERE hub_chat_id = ? ORDER BY rowid DESC LIMIT 10",
                (hub,)).fetchall()
        if not rows:
            await reply("No links yet.")
            return
        await reply("The latest links:\n\n" + "\n".join(
            f"{t}  ->  {target}  [{status}]" + (f"  user {uid}" if uid else "") + f"  ({created})"
            for t, target, status, uid, created in rows))
        return

    if args[0].lower() == "revoke":
        link = _link(args[1]) if len(args) > 1 else None
        if not link or link["hub"] != hub:
            await reply(f"{ICON_WARNING} No such link here.")
        elif link["status"] == "done":
            await reply(f"{ICON_WARNING} That link has been used already.")
        else:
            _set_link(link["token"], status="revoked")
            await reply("✅ The link no longer works.")
        return

    if not get_questions(hub):
        await reply(f"{ICON_WARNING} The form has no questions yet - add some with /joinform add first.")
        return
    target, label = _resolve_target(hub, args[0])
    if target is None:
        await reply(f"{ICON_WARNING} {label}")
        return
    ok, info = await _can_invite(context.bot, target)
    if not ok:
        await reply(f"{ICON_WARNING} {info}")
        return
    token = secrets.token_urlsafe(8)
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO join_form_links (token, hub_chat_id, target_chat_id, created_by, created_at, status) VALUES (?, ?, ?, ?, ?, 'new')",
            (token, hub, target, str(update.effective_user.id), now2ddmmyy()),
        )
        conn.commit()
    await reply(
        f"✅ One-time link to the form; after it the person is invited to {info}:\n\n"
        f"https://t.me/{context.bot.username}?start={START_PREFIX}{token}\n\n"
        f"It works for the first person who opens it and nobody else."
    )


# ---------------------------------------------------------------------------
# The person's side
# ---------------------------------------------------------------------------
def _choice_keyboard(token: str, q: dict):
    return InlineKeyboardMarkup([[InlineKeyboardButton(c, callback_data=f"{CALLBACK_PREFIX}{token}_{i}")] for i, c in enumerate(q["choices"])])


async def _ask(bot, chat_id, link, q: dict) -> None:
    questions = get_questions(link["hub"])
    number = next((i for i, x in enumerate(questions, start=1) if x["id"] == q["id"]), 1)
    text = f"Question {number} of {len(questions)}\n\n{q['question']}"
    if q["choices"]:
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=_choice_keyboard(link["token"], q))
    else:
        await bot.send_message(chat_id=chat_id, text=text + "\n\n(type your answer)")


async def begin_form(update: Update, context: ContextTypes.DEFAULT_TYPE, token: str) -> None:
    """/start jf_<token>: the person opened a link."""
    reply = update.message.reply_text
    user = update.effective_user
    link = _link(token)
    if not link or link["status"] == "revoked":
        await reply("This link doesn't work (any more). Ask the admin for a new one.")
        return
    if link["user_id"] and link["user_id"] != str(user.id):
        await reply("This link has been taken by someone else. Ask the admin for your own link.")
        return
    if link["status"] == "done":
        await reply("This link has been used already. Ask the admin for a new one if you need it.")
        return
    if not feature_available(link["hub"], int(link["created_by"] or 0), FEATURE):
        await reply("This form is not active right now. Please contact the admin.")
        return
    if not get_questions(link["hub"]):
        await reply("This form has no questions at the moment. Please contact the admin.")
        return

    if link["status"] == "new":
        _set_link(token, status="filling", user_id=str(user.id))
        link["status"], link["user_id"] = "filling", str(user.id)
        await reply("👋 Hi! Please answer a few short questions - then I'll send you the invitation.")
    elif link["status"] == "filling":
        await reply("Let's continue where you stopped.")

    q = _next_question(link)
    if q is None:                      # everything answered (a retry of the invitation)
        await _finish(context.bot, update.effective_chat.id, link, user)
    else:
        await _ask(context.bot, update.effective_chat.id, link, q)


def _active_link_of(user_id):
    """The link this person is filling in right now, or None."""
    with get_connection() as conn:
        r = conn.execute(
            "SELECT token FROM join_form_links WHERE user_id = ? AND status = 'filling' ORDER BY rowid DESC LIMIT 1", (str(user_id),)).fetchone()
    return _link(r[0]) if r else None


def has_active_form(update: Update) -> bool:
    """Is this text message (in a private chat) an answer to a form? Cheap: one indexed-free lookup on private chats only."""
    chat = update.effective_chat
    if chat is None or chat.type != "private" or update.effective_user is None:
        return False
    return _active_link_of(update.effective_user.id) is not None


async def handle_text_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    link = _active_link_of(update.effective_user.id)
    if link is None:
        return
    q = _next_question(link)
    if q is None:
        await _finish(context.bot, update.effective_chat.id, link, update.effective_user)
        return
    text = (update.message.text or "").strip()
    if q["choices"]:
        match = next((c for c in q["choices"] if c.lower() == text.lower()), None)
        if match is None:
            await update.message.reply_text("Please tap one of the buttons:")
            await _ask(context.bot, update.effective_chat.id, link, q)
            return
        text = match
    if not text:
        await update.message.reply_text("Please type your answer.")
        return
    if len(text) > MAX_ANSWER_LEN:
        await update.message.reply_text(f"That is too long - please keep it under {MAX_ANSWER_LEN} characters.")
        return
    await _record(context.bot, update.effective_chat.id, link, q, text, update.effective_user)


async def choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A tap on one of a question's buttons: jfc_<token>_<index>."""
    query = update.callback_query
    await query.answer()
    m = re.match(rf"^{CALLBACK_PREFIX}([A-Za-z0-9_-]+)_(\d+)$", query.data or "")
    link = _link(m.group(1)) if m else None
    if not link or link["status"] != "filling" or link["user_id"] != str(query.from_user.id):
        return
    q = _next_question(link)
    idx = int(m.group(2))
    if q is None or not q["choices"] or idx >= len(q["choices"]):
        return                                            # an old button of a question that is already answered
    answer = q["choices"][idx]
    try:
        await query.edit_message_text(f"{query.message.text}\n\n✅ {answer}")
    except Exception:
        pass
    await _record(context.bot, query.message.chat_id, link, q, answer, query.from_user)


async def _record(bot, chat_id, link, q: dict, answer: str, user) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO join_form_responses (token, question_id, question_text, answer, submitted_at) VALUES (?, ?, ?, ?, ?)",
            (link["token"], q["id"], q["question"], answer, now2ddmmyy()),
        )
        conn.commit()
    nxt = _next_question(link)
    if nxt is None:
        await _finish(bot, chat_id, link, user)
    else:
        await _ask(bot, chat_id, link, nxt)


async def _finish(bot, chat_id, link, user) -> None:
    """All answered: keep them, then send the one-time invitation (retried by opening the link again if it fails)."""
    first_time = link["status"] == "filling"
    if first_time:
        _set_link(link["token"], status="answered", answered_at=now2ddmmyy())
        asyncio.ensure_future(_write_sheet_row(link, user))
    try:
        invite = await bot.create_chat_invite_link(
            chat_id=int(link["target"]), member_limit=1,
            expire_date=datetime.now() + timedelta(hours=INVITE_TTL_HOURS), name=f"form {link['token'][:8]}")
    except Exception as e:
        logger.error(f"Join form {link['token']}: could not create the invitation to {link['target']}: {e}")
        await bot.send_message(chat_id=chat_id, text="Thank you, your answers are saved ✅\n\nI couldn't create your invitation right now - I told the admin. "
                                                     "Open the same link again a bit later and I'll try once more.")
        if link["created_by"]:
            try:
                await bot.send_message(chat_id=int(link["created_by"]), text=(
                    f"{ICON_WARNING} Someone finished the join form but I could not create the invitation to {link['target']}: {e}\n"
                    f"Check that I am an admin there with the 'Invite users' right. The person can open their link again to retry."))
            except Exception:
                pass
        return
    _set_link(link["token"], status="done")
    await bot.send_message(chat_id=chat_id, text=(
        f"Thank you, your answers are saved ✅\n\nHere is your invitation (one person, valid for {INVITE_TTL_HOURS // 24} days):\n{invite.invite_link}"))


async def _write_sheet_row(link, user) -> None:
    """The JoinForm tab of the hub's sheet (best effort - the answers are in SQLite either way)."""
    try:
        sheet_id = await get_sheet_for_chat(link["hub"])
        ss = await open_spreadsheet(sheet_id) if sheet_id else None
        if ss is None:
            return
        try:
            ws = await ss.worksheet(SHEET_TAB)
        except WorksheetNotFound:
            ws = await ss.add_worksheet(title=SHEET_TAB, rows=1000, cols=12)
        answers = {text: ans for text, ans in _answers(link["token"]).values()}
        header = list(await ws.row_values(1))
        wanted = SHEET_BASE + header[len(SHEET_BASE):] if header[:len(SHEET_BASE)] == SHEET_BASE else list(SHEET_BASE)
        wanted += [t for t in answers if t not in wanted]
        if wanted != header:
            await ws.update("A1", [wanted])
        name = " ".join(x for x in (getattr(user, "first_name", None), getattr(user, "last_name", None)) if x)
        row = [now2ddmmyy(), str(user.id), getattr(user, "username", None) or "", name, link["target"]]
        await ws.append_row(row + [answers.get(h, "") for h in wanted[len(SHEET_BASE):]])
    except Exception as e:
        logger.error(f"Join form {link['token']}: the JoinForm tab was not written (the answers are in the database): {e}")
