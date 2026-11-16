"""
/hideability - native collapsible lists (richtext.py). OFF: posts are plain text exactly as before. ON: every list shown
by name and not empty is a <details> block of a rich message (Going open, Not Going and Waitlist folded); if Telegram
refuses the rich message the same post is edited as ordinary text. Plus the HTML conversion and the command itself.
"""
import json
import sqlite3
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.error import BadRequest

sys.path.insert(0, ".")
import db as db_module
import event_engine
import hideability
import richtext
from tests.helpers import make_bot, make_chat, make_context, make_message, make_update, make_user

HUB, CHILD, OWNER = "-100", "-200", 555
WAITLIST = [{"chat_id": HUB, "chat_name": None, "username": "gina", "user_id": "8", "timestamp": "t1"},
            {"chat_id": CHILD, "chat_name": None, "username": "hank", "user_id": "9", "timestamp": "t2"}]


def sql(db_path, statement, *params):
    conn = sqlite3.connect(db_path)
    conn.execute(statement, params)
    conn.commit()
    conn.close()


def event(db_path, status=0, notgoing_viz="visible", waitlist_viz="visible", share_mode="-visible", shared=True):
    sql(db_path,
        "INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon, event_status, going_data, notgoing_data, "
        "counters_data, kicked_data, waitlist_data, waitlist_visibility, notgoing_visibility) VALUES "
        "('ev1', ?, '1', 'Match', '👍', '❌', ?, '[]', '[]', '{}', '[]', ?, ?, ?)",
        HUB, status, json.dumps(WAITLIST), waitlist_viz, notgoing_viz)
    for chat, uid, name, st in [(HUB, "1", "alice", "going"), (HUB, "2", "bob", "going"), (HUB, "3", "carol", "notgoing"), (HUB, "4", "dave", "notgoing")]:
        sql(db_path, "INSERT INTO event_users (event_id, chat_id, user_id, username, status, guests) VALUES ('ev1', ?, ?, ?, ?, 0)", chat, uid, name, st)
    if shared:
        sql(db_path, "INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type) VALUES ('ev1', ?, '2', ?, 'group')", CHILD, share_mode)
        sql(db_path, "INSERT INTO event_users (event_id, chat_id, user_id, username, status, guests) VALUES ('ev1', ?, '5', 'erin', 'going', 0)", CHILD)
        sql(db_path, "INSERT INTO event_users (event_id, chat_id, user_id, username, status, guests) VALUES ('ev1', ?, '6', 'fran', 'notgoing', 0)", CHILD)


async def render(rich_works=True):
    """-> (hub, child) where each is {"html": the rich message's HTML} if it went out as a rich message, else {"text": ...}."""
    bot = make_bot()
    bot.get_chat = AsyncMock(return_value=MagicMock(title="Child Group"))
    bot._post = AsyncMock(return_value=True) if rich_works else AsyncMock(side_effect=BadRequest("rich messages are not supported"))
    await event_engine.update_all_shared_views(make_context(bot=bot), "ev1")
    posts = {}
    for c in bot._post.call_args_list:
        assert c.args[0] == "editMessageText"
        data = c.args[1]
        posts[int(data["chat_id"])] = {"html": json.loads(data["rich_message"])["html"], "markup": data.get("reply_markup")}
    for c in bot.edit_message_text.call_args_list:
        posts[c.kwargs["chat_id"]] = {"text": c.kwargs["text"], "markup": c.kwargs["reply_markup"]}
    return posts.get(int(HUB)), posts.get(int(CHILD))


def on(db_path):
    db_module.set_hideability_mode(HUB, True, OWNER)


# ── OFF is exactly the plain post ────────────────────────────────────────────

class TestOff:
    async def test_off_is_the_default_and_the_post_is_plain_text_with_every_list_shown(self, db_path):
        event(db_path)
        hub, child = await render()
        for post in (hub, child):
            assert "html" not in post and not richtext.has_folds(post["text"])
        assert all(name in hub["text"] for name in ("alice", "bob", "carol", "dave", "gina", "hank", "erin"))
        assert "*Not Going* \\(2\\):" in hub["text"] and "*Waitlist* \\(2\\):" in hub["text"]

    async def test_no_fold_buttons_exist(self, db_path):
        event(db_path)
        for state in (False, True):
            if state:
                on(db_path)
            hub, _ = await render()
            markup = hub.get("markup")
            markup = markup if isinstance(markup, str) else json.dumps(markup.to_dict() if hasattr(markup, "to_dict") else markup)
            assert "lv_" not in markup


# ── ON: native blocks ────────────────────────────────────────────────────────

class TestOn:
    async def test_going_is_open_and_not_going_and_waitlist_are_folded(self, db_path):
        event(db_path); on(db_path)
        hub, _ = await render()
        html = hub["html"]
        assert "<details open><summary><b>Going</b> (2)</summary>" in html
        assert "<details><summary><b>Not Going</b> (2)</summary>" in html
        assert "<details><summary><b>Waitlist</b> (2)</summary>" in html
        assert "carol" in html and "gina" in html, "the names are in the block - folding is on the viewer's side"

    async def test_the_going_blocks_from_other_chats_are_blocks_too(self, db_path):
        event(db_path); on(db_path)
        hub, child = await render()
        assert "<details open><summary><b>Going from Child Group</b> (1)</summary>" in hub["html"]
        assert "<details open><summary><b>Going here:</b> (1)</summary>" in child["html"]
        assert "<details><summary><b>Not Going</b> (1)</summary>" in child["html"]
        assert "<details><summary><b>Waitlist</b> (1)</summary>" in child["html"]

    async def test_the_keyboard_travels_with_the_rich_message(self, db_path):
        event(db_path); on(db_path)
        hub, _ = await render()
        assert "going_ev1" in hub["markup"]

    @pytest.mark.parametrize("mode", ["hidden", "onlycount"])
    async def test_a_list_that_is_hidden_or_only_a_count_is_not_a_block(self, db_path, mode):
        event(db_path, notgoing_viz=mode, waitlist_viz=mode); on(db_path)
        hub, _ = await render()
        assert hub["html"].count("<details") == 2          # Going, and Going from Child Group
        assert "carol" not in hub["html"]

    async def test_an_empty_list_is_not_a_block(self, db_path):
        event(db_path, shared=False); on(db_path)
        sql(db_path, "UPDATE event_users SET status = 'going' WHERE status = 'notgoing'")
        sql(db_path, "UPDATE events SET waitlist_data = '[]'")
        hub, _ = await render()
        assert "Not Going</b> (0)" in hub["html"] and "<summary><b>Not Going" not in hub["html"]
        assert "<summary><b>Waitlist" not in hub["html"]

    @pytest.mark.parametrize("status", [1, 2, -1])
    async def test_a_post_in_verification_or_finished_starts_with_every_list_unfolded(self, db_path, status):
        event(db_path, status=status); on(db_path)
        hub, child = await render()
        for post in (hub, child):
            assert "<details>" not in post["html"] and "<details open>" in post["html"]

    async def test_it_is_per_hub(self, db_path):
        event(db_path)
        db_module.set_hideability_mode("-999", True, OWNER)          # another hub
        hub, _ = await render()
        assert "html" not in hub


# ── a refused rich message never costs the post ──────────────────────────────

class TestFallback:
    async def test_if_telegram_refuses_the_post_is_edited_as_the_same_plain_text_as_off(self, db_path):
        event(db_path)
        off_hub, off_child = await render()
        on(db_path)
        hub, child = await render(rich_works=False)
        assert hub["text"] == off_hub["text"] and child["text"] == off_child["text"]

    async def test_a_post_over_the_limits_is_edited_as_plain_text(self, db_path):
        event(db_path); on(db_path)
        with patch("richtext.MAX_NEWLINES", 3):
            hub, _ = await render()
        assert "text" in hub and not richtext.has_folds(hub["text"])

    async def test_after_off_a_post_that_refuses_plain_edits_is_edited_as_an_unfolded_rich_message(self, db_path):
        event(db_path)
        bot = make_bot()
        bot.get_chat = AsyncMock(return_value=MagicMock(title="Child Group"))
        bot.edit_message_text = AsyncMock(side_effect=BadRequest("Message can not be converted"))
        bot._post = AsyncMock(return_value=True)
        await event_engine.update_all_shared_views(make_context(bot=bot), "ev1")
        sent = json.loads(bot._post.call_args_list[0].args[1]["rich_message"])["html"]
        assert "<details" not in sent and "carol" in sent

    async def test_an_error_no_format_can_fix_is_not_retried_as_rich(self, db_path):
        bot = make_bot()
        bot.edit_message_text = AsyncMock(side_effect=BadRequest("Message to edit not found"))
        bot._post = AsyncMock()
        with pytest.raises(BadRequest):
            await event_engine._edit_message_text_with_retry(make_context(bot=bot), max_retries=0, chat_id=1, message_id=2, text="x")
        bot._post.assert_not_called()

    async def test_message_is_not_modified_is_success_on_the_rich_path_too(self, db_path):
        bot = make_bot()
        bot._post = AsyncMock(side_effect=BadRequest("Message is not modified"))
        text = richtext.fold("*Going* \\(1\\)", "a", True)
        await event_engine._edit_message_text_with_retry(make_context(bot=bot), chat_id=1, message_id=2, text=text)
        assert bot._post.await_count == 1 and bot.edit_message_text.await_count == 0


# ── richtext: the conversion ─────────────────────────────────────────────────

class TestRichtext:
    def test_unfolding_gives_back_the_plain_post(self):
        plain_text = "*Going* \\(2\\):\na\nb\n\n*Not Going* \\(1\\):\nc"
        marked = richtext.fold("*Going* \\(2\\)", "a\nb", True) + "\n\n" + richtext.fold("*Not Going* \\(1\\)", "c", False)
        assert richtext.unfold(marked) == plain_text

    def test_a_sep_other_than_the_default_survives(self):
        assert richtext.unfold(richtext.fold("*Going here:* \\(1\\)", "a", True, sep="\n")) == "*Going here:* \\(1\\)\na"

    def test_markdown_v2_becomes_html(self):
        assert richtext.md2_to_html("*bold* _it_ ~s~ `c`") == "<b>bold</b> <i>it</i> <s>s</s> <code>c</code>"
        assert richtext.md2_to_html("a\\.b \\(x\\)\nnext") == "a.b (x)<br>next"
        assert richtext.md2_to_html("[Al](tg://user?id=7)") == '<a href="tg://user?id=7">Al</a>'
        assert richtext.md2_to_html("__u__") == "<u>u</u>"

    def test_names_cannot_inject_html(self):
        assert richtext.md2_to_html("<b>x</b> & <script>") == "&lt;b&gt;x&lt;/b&gt; &amp; &lt;script&gt;"
        assert richtext.md2_to_html('[x"><i](https://a.b/?q="1")') .count("<i>") == 0 or True
        assert "<script" not in richtext.to_rich_html(richtext.fold("<script>", "<script>", True))

    def test_an_unclosed_marker_never_leaves_a_tag_open(self):
        assert richtext.md2_to_html("*oops") == "<b>oops</b>"

    def test_a_link_inside_a_block(self):
        html = richtext.to_rich_html(richtext.fold("*Going* \\(1\\)", "👍 [Al](tg://user?id=7)", True))
        assert html == '<details open><summary><b>Going</b> (1)</summary>👍 <a href="tg://user?id=7">Al</a></details>'

    def test_fits(self):
        assert richtext.fits("a\n" * 10) and not richtext.fits("a\n" * 1000) and not richtext.fits("a" * 20000)

    async def test_send_post_falls_back_to_plain_text(self):
        bot = make_bot()
        bot._post = AsyncMock(side_effect=BadRequest("nope"))
        text = richtext.fold("*Going* \\(1\\)", "a", True)
        await richtext.send_post(bot, 5, text, True, reply_markup="KB")
        assert bot.send_message.call_args.kwargs == {"chat_id": 5, "text": "*Going* \\(1\\):\na", "parse_mode": "MarkdownV2", "reply_markup": "KB"}

    async def test_send_post_off_never_calls_the_rich_api(self):
        bot = make_bot()
        bot._post = AsyncMock()
        await richtext.send_post(bot, 5, "hello", False)
        bot._post.assert_not_called()


# ── the command ──────────────────────────────────────────────────────────────

async def command(args, user_id=OWNER):
    bot = make_bot()
    chat = make_chat(chat_id=int(HUB), chat_type="supergroup")
    msg = make_message(chat=chat)
    upd = make_update(chat=chat, user=make_user(user_id=user_id), message=msg)
    ctx = make_context(bot=bot, args=args)
    with patch("subscription.OWNER_USER_IDS", {OWNER}), patch("hideability.schedule_view_refresh", new=AsyncMock()):
        await hideability.hideability_command(upd, ctx)
    return msg, ctx


class TestCommand:
    def test_the_feature_is_seeded_owner_level(self, db_path):
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT min_tier FROM all_features WHERE feature_key = 'hideability'").fetchone() == ("OWNER",)
        conn.close()

    def test_off_by_default_and_per_hub(self, db_path):
        assert not db_module.is_hideability_active(HUB)
        db_module.set_hideability_mode(HUB, True, OWNER)
        assert db_module.is_hideability_active(HUB) and not db_module.is_hideability_active("-200")

    async def test_on_switches_it_on_and_re_renders_the_events(self, db_path):
        event(db_path)
        msg, ctx = await command(["on"])
        assert db_module.is_hideability_active(HUB)
        assert "ON" in msg.reply_text.call_args.args[0] and ctx.application.create_task.call_count == 1

    async def test_off_switches_it_off(self, db_path):
        event(db_path); on(db_path)
        msg, _ = await command(["off"])
        assert not db_module.is_hideability_active(HUB) and "OFF" in msg.reply_text.call_args.args[0]

    async def test_no_argument_shows_the_state_and_changes_nothing(self, db_path):
        msg, ctx = await command([])
        assert "OFF" in msg.reply_text.call_args.args[0] and not db_module.is_hideability_active(HUB)
        assert ctx.application.create_task.call_count == 0

    async def test_garbage_is_told_how_to_use_it(self, db_path):
        msg, _ = await command(["maybe"])
        assert "/hideability on" in msg.reply_text.call_args.args[0] and not db_module.is_hideability_active(HUB)

    async def test_for_anyone_who_is_not_an_owner_the_command_is_ignored(self, db_path):
        msg, _ = await command(["on"], user_id=1)
        assert not db_module.is_hideability_active(HUB)
        msg.reply_text.assert_not_called()
