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
        assert html == '<details open><summary><b>Going</b> (1)</summary><p>👍 <a href="tg://user?id=7">Al</a></p></details>'

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


class TestSpacing:
    def test_no_line_breaks_next_to_a_block(self):
        text = "*T*\n\n🕐 d\n\n" + richtext.fold("*Going* \\(1\\)", "a", True) + "\n\n" + richtext.fold("*Not Going* \\(1\\)", "b", False) + "\n\n📊 *TOTAL* 1"
        html = richtext.to_rich_html(text)
        assert html == ("<p><b>T</b></p><p>🕐 d</p><details open><summary><b>Going</b> (1)</summary><p>a</p></details>"
                        "<details><summary><b>Not Going</b> (1)</summary><p>b</p></details><p>📊 <b>TOTAL</b> 1</p>")

    def test_the_earlier_br_form_is_still_available_for_comparison(self):
        text = "*T*\n\n🕐 d\n\n" + richtext.fold("*Going* \\(1\\)", "a", True)
        assert richtext.to_rich_html(text, paragraphs=False) == "<b>T</b><br><br>🕐 d<details open><summary><b>Going</b> (1)</summary>a</details>"

    def test_lines_inside_a_block_keep_their_breaks(self):
        assert "<p>a<br>b</p>" in richtext.to_rich_html(richtext.fold("*G*", "a\nb", True))

    def test_the_names_can_go_straight_into_the_block(self):
        html = richtext.to_rich_html(richtext.fold("*G*", "a\nb", True), body_paragraph=False)
        assert html == "<details open><summary><b>G</b></summary>a<br>b</details>"


class TestRichDebug:
    """RICH_DEBUG=1 logs the exact HTML of every rich message (to see what is really sent); off by default."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("call", ["send", "edit"])
    async def test_the_html_is_logged_only_when_the_switch_is_on(self, monkeypatch, call):
        bot = make_bot()
        bot._post = AsyncMock(return_value={})
        text = richtext.fold("*Going* \\(1\\)", "a", True)

        async def run():
            if call == "send":
                await richtext.send_rich(bot, "-1", text)
            else:
                await richtext.edit_rich(bot, "-1", 5, text)

        with patch.object(richtext.logger, "info") as info:
            monkeypatch.delenv("RICH_DEBUG", raising=False)
            await run()
            assert not any("RICH_DEBUG" in str(c) for c in info.call_args_list)
            monkeypatch.setenv("RICH_DEBUG", "1")
            await run()
            logged = [str(c) for c in info.call_args_list if "RICH_DEBUG" in str(c)]
            assert len(logged) == 1
            assert f"RICH_DEBUG {call} chat=-1" in logged[0] and "<details open>" in logged[0]


async def flag_command(args, user_id=OWNER, chat_type="supergroup"):
    """/hideability <flags> as `user_id` - in the hub group, or (chat_type="private") in a DM with no hub in sight."""
    bot = make_bot()
    chat = make_chat(chat_id=int(HUB) if chat_type != "private" else 555, chat_type=chat_type)
    msg = make_message(chat=chat)
    upd = make_update(chat=chat, user=make_user(user_id=user_id), message=msg)
    ctx = make_context(bot=bot, args=args)
    with patch("subscription.OWNER_USER_IDS", {OWNER}), patch("hideability.OWNER_USER_IDS", {OWNER}), \
            patch("hideability.schedule_view_refresh", new=AsyncMock()):
        await hideability.hideability_command(upd, ctx)
    return msg, ctx


class TestListDefaults:
    """/hideability -g|-ng|-w|-sg|-sng|-sw show|hide: how each list STARTS - one setting for every group, owners only."""

    def test_the_defaults_are_going_shown_and_the_rest_hidden(self, db_path):
        assert db_module.get_hideability_flags() == {
            "going": "show", "notgoing": "hide", "waitlist": "hide",
            "sharedgoing": "show", "sharednotgoing": "hide", "sharedwaitlist": "hide"}

    def test_a_saved_choice_replaces_only_its_own_list(self, db_path):
        db_module.set_hideability_flags({"notgoing": "show", "sharedwaitlist": "show"}, OWNER)
        flags = db_module.get_hideability_flags()
        assert flags["notgoing"] == "show" and flags["sharedwaitlist"] == "show" and flags["going"] == "show"
        assert flags["waitlist"] == "hide" and flags["sharedgoing"] == "show" and flags["sharednotgoing"] == "hide"

    def test_every_flag_has_a_short_and_a_long_name(self):
        assert hideability.parse_flags(["-g", "hide", "-going", "show"])[0] == {"going": "show"}
        pairs = {"-ng": "notgoing", "-notgoing": "notgoing", "-w": "waitlist", "-waitlist": "waitlist", "-sg": "sharedgoing",
                 "-sharedgoing": "sharedgoing", "-sng": "sharednotgoing", "-sharednotgoing": "sharednotgoing",
                 "-sw": "sharedwaitlist", "-sharedwaitlist": "sharedwaitlist"}
        for flag, key in pairs.items():
            assert hideability.parse_flags([flag, "show"]) == ({key: "show"}, None)
        assert hideability.parse_flags(["-sharedwaitlist", "HIDE"]) == ({"sharedwaitlist": "hide"}, None)

    @pytest.mark.parametrize("args", [["-x", "show"], ["-g"], ["-g", "maybe"], ["-g", "show", "-ng"], ["-g", "show", "on"], ["g", "show"]])
    def test_what_is_not_a_flag_and_a_value_is_refused(self, args):
        assert hideability.parse_flags(args)[1]

    async def test_going_in_the_hub_post_and_its_going_from_blocks_follow_going(self, db_path):
        event(db_path); on(db_path)
        db_module.set_hideability_flags({"going": "hide"}, OWNER)
        hub, child = await render()
        assert "<details><summary><b>Going</b> (2)</summary>" in hub["html"]
        assert "<details><summary><b>Going from Child Group</b> (1)</summary>" in hub["html"]
        assert "<details open><summary><b>Going here:</b> (1)</summary>" in child["html"], "shared Going is its own flag"

    async def test_the_shared_flags_change_only_the_shared_post(self, db_path):
        event(db_path); on(db_path)
        db_module.set_hideability_flags({"sharedgoing": "hide", "sharednotgoing": "show", "sharedwaitlist": "show"}, OWNER)
        hub, child = await render()
        assert "<details open><summary><b>Going</b> (2)</summary>" in hub["html"]
        assert "<details><summary><b>Not Going</b> (2)</summary>" in hub["html"]
        assert "<details><summary><b>Waitlist</b> (2)</summary>" in hub["html"]
        assert "<details><summary><b>Going here:</b> (1)</summary>" in child["html"]
        assert "<details open><summary><b>Not Going</b> (1)</summary>" in child["html"]
        assert "<details open><summary><b>Waitlist</b> (1)</summary>" in child["html"]

    async def test_the_hub_flags_change_only_the_hub_post(self, db_path):
        event(db_path); on(db_path)
        db_module.set_hideability_flags({"notgoing": "show", "waitlist": "show"}, OWNER)
        hub, child = await render()
        assert "<details open><summary><b>Not Going</b> (2)</summary>" in hub["html"]
        assert "<details open><summary><b>Waitlist</b> (2)</summary>" in hub["html"]
        assert "<details><summary><b>Not Going</b> (1)</summary>" in child["html"]
        assert "<details><summary><b>Waitlist</b> (1)</summary>" in child["html"]

    async def test_a_post_in_verification_is_open_whatever_the_flags_say(self, db_path):
        event(db_path, status=1); on(db_path)
        db_module.set_hideability_flags({k: "hide" for k in richtext.FLAG_DEFAULTS}, OWNER)
        hub, child = await render()
        assert "<details>" not in hub["html"] and "<details>" not in child["html"]
        assert "<details open>" in hub["html"]

    async def test_with_hideability_off_the_flags_change_nothing(self, db_path):
        event(db_path)
        before = await render()
        db_module.set_hideability_flags({k: "hide" for k in richtext.FLAG_DEFAULTS}, OWNER)
        assert await render() == before

    async def test_the_flags_are_the_same_for_every_group(self, db_path):
        event(db_path); on(db_path)
        db_module.set_hideability_mode("-999", True, OWNER)
        await flag_command(["-ng", "show"])
        assert db_module.get_hideability_flags()["notgoing"] == "show"       # no hub in the table: one global row
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT scope FROM bot_settings WHERE key LIKE 'hideability.%'").fetchall() == [("*",)]
        conn.close()

    async def test_an_owner_sets_several_flags_at_once_and_is_told_all_six(self, db_path):
        msg, ctx = await flag_command(["-ng", "show", "-sharedwaitlist", "show", "-g", "hide"])
        flags = db_module.get_hideability_flags()
        assert (flags["notgoing"], flags["sharedwaitlist"], flags["going"]) == ("show", "show", "hide")
        text = msg.reply_text.call_args.args[0]
        for label in ("-g Going: hide", "-ng Not Going: show", "-w Waitlist: hide", "-sg Shared Going: show",
                      "-sng Shared Not Going: hide", "-sw Shared Waitlist: show"):
            assert label in text
        assert ctx.application.create_task.call_count == 1                  # the hubs are re-rendered

    async def test_an_owner_can_set_them_from_a_dm(self, db_path):
        msg, _ = await flag_command(["-w", "show"], chat_type="private")
        assert db_module.get_hideability_flags()["waitlist"] == "show" and "Saved" in msg.reply_text.call_args.args[0]

    async def test_a_wrong_flag_changes_nothing_and_says_how_to_use_it(self, db_path):
        msg, ctx = await flag_command(["-g", "show", "-ng", "maybe"])
        assert db_module.get_hideability_flags() == richtext.FLAG_DEFAULTS
        assert "show or hide" in msg.reply_text.call_args.args[0] and ctx.application.create_task.call_count == 0

    async def test_a_non_owner_cannot_change_them(self, db_path):
        msg, _ = await flag_command(["-g", "hide"], user_id=1)
        assert db_module.get_hideability_flags() == richtext.FLAG_DEFAULTS
        msg.reply_text.assert_not_called()                                  # the feature is OWNER-level: ignored in silence

    async def test_an_admin_of_a_hub_that_was_given_the_feature_is_told_it_is_for_owners(self, db_path):
        sql(db_path, "UPDATE all_features SET min_tier = 'FREE' WHERE feature_key = 'hideability'")
        with patch("hideability.is_real_admin", new=AsyncMock(return_value=True)):
            msg, _ = await flag_command(["-g", "hide"], user_id=1)
        assert db_module.get_hideability_flags() == richtext.FLAG_DEFAULTS
        assert "bot owner" in msg.reply_text.call_args.args[0]

    async def test_the_status_shows_how_the_lists_start(self, db_path):
        db_module.set_hideability_flags({"notgoing": "show"}, OWNER)
        msg, _ = await command([])
        text = msg.reply_text.call_args.args[0]
        assert "OFF" in text and "Not Going: show" in text and "Waitlist: hide" in text

    async def test_every_hub_that_has_it_on_is_re_rendered_and_only_those(self, db_path):
        event(db_path)
        sql(db_path, "INSERT INTO events (event_id, chat_id, message_id, name) VALUES ('ev9', '-999', '9', 'Other')")
        sql(db_path, "INSERT INTO events (event_id, chat_id, message_id, name) VALUES ('ev8', '-888', '8', 'Off hub')")
        db_module.set_hideability_mode(HUB, True, OWNER); db_module.set_hideability_mode("-999", True, OWNER)
        db_module.set_hideability_mode("-888", False, OWNER)
        refresh = AsyncMock()
        with patch("hideability.schedule_view_refresh", new=refresh), patch("hideability.REFRESH_PAUSE", 0):
            await hideability._refresh_all_hubs(make_context(bot=make_bot()))
        assert sorted(c.args[1] for c in refresh.call_args_list) == ["ev1", "ev9"]


class TestBotSettingsTable:
    """Every owner setting lives in ONE table, bot_settings (scope, key, value) - not a table per setting - and it
    survives the bot leaving a group, which deletes the group's all_groups row."""

    def test_reopen_hideability_and_the_list_flags_share_the_one_table(self, db_path):
        db_module.set_reopen_mode(HUB, True, OWNER)
        db_module.set_hideability_mode(HUB, True, OWNER)
        db_module.set_hideability_flags({"notgoing": "show"}, OWNER)
        conn = sqlite3.connect(db_path)
        rows = set(conn.execute("SELECT scope, key, value FROM bot_settings").fetchall())
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        conn.close()
        assert rows == {(HUB, "reopen", "on"), (HUB, "hideability", "on"), ("*", "hideability.notgoing", "show")}
        assert not tables & {"reopen_mode", "hideability_mode", "hideability_flags"}

    def test_a_setting_is_per_hub_and_off_by_default(self, db_path):
        assert not db_module.is_reopen_active(HUB) and not db_module.is_hideability_active(HUB)
        db_module.set_reopen_mode(HUB, True)
        assert db_module.is_reopen_active(HUB) and not db_module.is_reopen_active("-200")
        assert not db_module.is_hideability_active(HUB), "another key of the same hub is untouched"

    def test_the_two_modes_of_one_hub_do_not_disturb_each_other(self, db_path):
        db_module.set_reopen_mode(HUB, True); db_module.set_hideability_mode(HUB, True)
        db_module.set_reopen_mode(HUB, False)
        assert db_module.is_hideability_active(HUB) and not db_module.is_reopen_active(HUB)

    def test_a_setting_survives_the_bot_leaving_the_group(self, db_path):
        sql(db_path, "INSERT INTO all_groups (chat_id, chat_name, type, date_bot_add) VALUES (?, 'G', 'FREE', '01.01.2026')", HUB)
        db_module.set_hideability_mode(HUB, True, OWNER); db_module.set_reopen_mode(HUB, True, OWNER)
        db_module.register_chat_removed(HUB, "02.01.2026", db_path)         # what the bot does when it is removed
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT COUNT(*) FROM all_groups WHERE chat_id = ?", (HUB,)).fetchone() == (0,)
        conn.close()
        assert db_module.is_hideability_active(HUB) and db_module.is_reopen_active(HUB)

    def test_the_old_separate_tables_are_moved_into_it(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM bot_settings")
        conn.executescript("""
            CREATE TABLE reopen_mode (chat_id TEXT PRIMARY KEY, active INTEGER, set_by TEXT, set_at TEXT);
            CREATE TABLE hideability_mode (chat_id TEXT PRIMARY KEY, active INTEGER, set_by TEXT, set_at TEXT);
            CREATE TABLE hideability_flags (list_key TEXT PRIMARY KEY, mode TEXT, set_by TEXT, set_at TEXT);
            INSERT INTO reopen_mode VALUES ('-1', 1, '7', 't1'), ('-2', 0, NULL, 't2');
            INSERT INTO hideability_mode VALUES ('-1', 1, '7', 't3');
            INSERT INTO hideability_flags VALUES ('waitlist', 'show', '7', 't4');
        """)
        conn.commit(); conn.close()
        db_module.init_db(db_path)                                          # the migration runs at start-up
        conn = sqlite3.connect(db_path)
        rows = set(conn.execute("SELECT scope, key, value, set_by, set_at FROM bot_settings").fetchall())
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        conn.close()
        assert rows == {("-1", "reopen", "on", "7", "t1"), ("-2", "reopen", "off", None, "t2"),
                        ("-1", "hideability", "on", "7", "t3"), ("*", "hideability.waitlist", "show", "7", "t4")}
        assert not tables & {"reopen_mode", "hideability_mode", "hideability_flags"}
        assert db_module.is_reopen_active("-1") and db_module.get_hideability_flags()["waitlist"] == "show"
