"""
Guards for /help so it can't drift from the bot again: every registered command is
documented somewhere, nothing outdated is left in the text, hubs only see features they
have, and every text is valid MarkdownV2 (an unescaped '.' makes Telegram reject the whole
message - the user then sees nothing).
"""
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, ".")
import help_system
from tests.helpers import make_chat, make_user, make_message, make_update, make_context

OWNER = 555
KEYS = sorted(set(re.findall(r'"(help_[a-z_]+)"', open("help_system.py", encoding="utf-8").read())))


def _plain(text):
    return text.replace("\\", "")


def _make_pro(db_path, chat_id="-100"):
    end = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO all_groups (chat_id, type, subs_date_start, subs_date_end) VALUES (?, 'PRO', ?, ?)",
                 (chat_id, end, end))
    conn.commit()
    conn.close()


def _ctx(user_id, chat_id, data=None):
    chat = make_chat(chat_id=chat_id, chat_type="supergroup")
    msg = make_message(chat=chat)
    upd = make_update(chat=chat, user=make_user(user_id=user_id), message=msg)
    query = None
    if data is not None:
        query = MagicMock()
        query.data, query.message = data, msg
        query.answer, query.edit_message_text = AsyncMock(), AsyncMock()
        upd.callback_query = query
    return upd, msg, query


async def _gather(user_id, chat_id=-100):
    """Every help text this person can reach from this chat."""
    texts = {}
    with patch("subscription.OWNER_USER_IDS", {OWNER}), patch("help_system.OWNER_USER_IDS", {OWNER}):
        for label, args in (("main", []), ("owner", ["-o"])):
            upd, msg, _ = _ctx(user_id, chat_id)
            await help_system.help_command(upd, make_context(args=args))
            if msg.reply_text.called:
                texts[label] = msg.reply_text.call_args.args[0]
        for key in KEYS:
            upd, _msg, query = _ctx(user_id, chat_id, key)
            try:
                await help_system.help_callback_handler(upd, make_context())
            except Exception:
                continue
            if query.edit_message_text.called:
                texts[key] = query.edit_message_text.call_args.args[0]
        texts["main-expanded"] = help_system._build_main_help_text(True, True, True)
        texts["owner-expanded"] = help_system._build_owner_help_text(expanded=True)
    return texts


_SPECIAL = set("[]()~>#+-=|{}.!")


def markdown_v2_problems(text):
    """Telegram MarkdownV2: these characters must be escaped outside code spans; * _ ` must pair up."""
    problems, counts, in_code, i = [], {"*": 0, "_": 0, "`": 0}, False, 0
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "`":
            in_code = not in_code
            counts["`"] += 1
        elif not in_code:
            if ch in "*_":
                counts[ch] += 1
            elif ch in _SPECIAL:
                problems.append(f"unescaped {ch!r} near {text[max(0, i - 20):i + 20]!r}")
        i += 1
    problems += [f"unbalanced {ch!r}" for ch, n in counts.items() if n % 2]
    return problems


class TestHelpAudit:
    async def test_every_registered_command_is_documented_somewhere(self, db_path):
        _make_pro(db_path)
        blob = " ".join(_plain(t) for t in (await _gather(OWNER)).values())
        mentioned = set(re.findall(r"/([a-z]+)", blob))
        registered = set(re.findall(r'CommandHandler\("([a-z]+)"', open("main.py", encoding="utf-8").read()))
        assert registered - mentioned == set()

    async def test_nothing_outdated_is_left_in_any_text(self, db_path):
        _make_pro(db_path)
        stale = [r"/stats -a\b", r"/help -a\b", r"free\|pro\|admin", r"\bADMIN\b",
                 r"\badmin only\b",   # creators can use the event buttons too
                 r"<hours>"]            # /who's number of hours is optional (default 6), never shown as required
        for who in (1, OWNER):
            for name, text in (await _gather(who)).items():
                for pattern in stale:
                    assert not re.search(pattern, _plain(text)), f"{name} (user {who}): {pattern}"

    async def test_every_text_is_valid_markdown_v2(self, db_path):
        _make_pro(db_path)
        for who, chat in ((1, -100), (OWNER, -100), (1, -200)):     # member / owner / FREE hub
            for name, text in (await _gather(who, chat)).items():
                assert markdown_v2_problems(text) == [], f"{name} (user {who}, chat {chat})"

    async def test_event_buttons_are_documented_as_admin_or_creator(self, db_path):
        text = _plain((await _gather(1))["help_lifecycle"])
        assert text.count("admin or event creator") == 6

    async def test_owner_screen_keeps_dm_only_commands_apart_from_the_rest(self, db_path):
        text = _plain((await _gather(OWNER))["owner"])
        header = text.index("also work inside a group")
        for dm_only in ("/setsub", "/lockbot", "/allgroups", "/allchannels", "/updatefeature", "/showtable", "/version"):
            assert text.index(dm_only) < header, dm_only
        for anywhere in ("/stats", "/deleteevent", "/reopen"):
            assert text.index(anywhere) > header, anywhere

    async def test_utility_documents_start_and_hides_features_the_hub_lacks(self, db_path):
        _make_pro(db_path)
        pro = _plain((await _gather(1))["help_utility"])
        assert "/start" in pro and "/stats" in pro and "/setsheet" in pro
        free = _plain((await _gather(1, chat_id=-200))["help_utility"])
        assert "/start" in free and "/stats" not in free and "/setsheet" not in free

    def test_the_lint_itself_catches_what_it_is_meant_to(self):
        assert markdown_v2_problems("fine \\. text *bold* `code.x`") == []
        assert markdown_v2_problems("unescaped. dot")
        assert markdown_v2_problems("*unbalanced")
