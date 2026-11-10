"""
Folding the lists of an event post: Going is shown, Not Going and Waitlist start folded to their header, a button under
the post (▾ shown / ▸ folded) flips one, and only an open event folds anything.
"""
from pathlib import Path
import json
import sqlite3
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, ".")
import db as db_module
import event_engine
import handlers
import keyboard
import listview
from tests.helpers import make_bot, make_context

HUB, CHILD = "-100", "-200"
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


async def render():
    bot = make_bot()
    bot.get_chat = AsyncMock(return_value=MagicMock(title="Child Group"))
    await event_engine.update_all_shared_views(make_context(bot=bot), "ev1")
    posts = {c.kwargs["chat_id"]: c.kwargs for c in bot.edit_message_text.call_args_list}
    return posts.get(int(HUB)), posts.get(int(CHILD))


def labels(post):
    return [b.text for row in post["reply_markup"].inline_keyboard for b in row if b.callback_data.startswith("lv_")]


def toggles(post):
    return [b.callback_data for row in post["reply_markup"].inline_keyboard for b in row if b.callback_data.startswith("lv_")]


class TestTheDefaults:
    async def test_going_is_shown_and_not_going_and_waitlist_are_folded_to_their_header(self, db_path):
        event(db_path)
        hub, _ = await render()
        text = hub["text"]
        assert "alice" in text and "bob" in text and "*Going* \\(2\\):" in text
        assert "*Not Going* \\(2\\)" in text and "*Not Going* \\(2\\):" not in text
        assert "carol" not in text and "dave" not in text
        assert "*Waitlist* \\(2\\)" in text and "*Waitlist* \\(2\\):" not in text
        assert "gina" not in text and "hank" not in text

    async def test_each_foldable_list_gets_a_button_with_its_name_amount_and_state(self, db_path):
        event(db_path)
        hub, _ = await render()
        assert labels(hub) == ["▾ Going (3)", "▸ Not Going (2)", "▸ Waitlist (2)"]
        assert toggles(hub) == ["lv_ev1:g", "lv_ev1:n", "lv_ev1:w"]

    async def test_the_buttons_sit_under_the_action_buttons(self, db_path):
        event(db_path)
        hub, _ = await render()
        rows = hub["reply_markup"].inline_keyboard
        assert rows[0][0].callback_data == "going_ev1" and rows[-1][0].callback_data.startswith("lv_")

    def test_the_defaults(self):
        assert db_module.LIST_DEFAULT_COLLAPSED == {"going": False, "notgoing": True, "waitlist": True}


class TestFolding:
    async def test_unfolding_not_going_shows_its_names_and_flips_the_chevron(self, db_path):
        event(db_path)
        assert db_module.toggle_list_state("ev1", HUB, "notgoing") is False        # now NOT collapsed
        hub, _ = await render()
        assert "*Not Going* \\(2\\):" in hub["text"] and "carol" in hub["text"] and "dave" in hub["text"]
        assert "▾ Not Going (2)" in labels(hub)

    async def test_folding_going_hides_every_going_name_in_the_hub_post_including_the_child_block(self, db_path):
        event(db_path)
        db_module.toggle_list_state("ev1", HUB, "going")
        hub, _ = await render()
        text = hub["text"]
        assert "*Going* \\(2\\)" in text and "*Going* \\(2\\):" not in text
        assert "alice" not in text and "bob" not in text and "erin" not in text
        assert "*Going from Child Group* \\(1\\)" in text, "the block's header and count stay"
        assert "▸ Going (3)" in labels(hub)

    async def test_the_total_line_is_never_folded(self, db_path):
        event(db_path)
        db_module.toggle_list_state("ev1", HUB, "going")
        hub, _ = await render()
        assert "TOTAL Going:* 3" in hub["text"]

    async def test_a_second_press_folds_it_again(self, db_path):
        event(db_path)
        db_module.toggle_list_state("ev1", HUB, "notgoing")
        assert db_module.toggle_list_state("ev1", HUB, "notgoing") is True
        hub, _ = await render()
        assert "carol" not in hub["text"]

    async def test_the_waitlist_unfolds_too(self, db_path):
        event(db_path)
        db_module.toggle_list_state("ev1", HUB, "waitlist")
        hub, _ = await render()
        assert "*Waitlist* \\(2\\):" in hub["text"] and "gina" in hub["text"] and "hank" in hub["text"]


class TestWhenThereIsNothingToFold:
    @pytest.mark.parametrize("mode", ["hidden", "onlycount"])
    async def test_a_list_that_is_hidden_or_only_a_count_has_no_button(self, db_path, mode):
        event(db_path, notgoing_viz=mode, waitlist_viz=mode)
        hub, _ = await render()
        assert labels(hub) == ["▾ Going (3)"]
        assert "carol" not in hub["text"]
        if mode == "onlycount":
            assert "*Not Going:* 2" in hub["text"] and "*Waitlist:* 2" in hub["text"]

    async def test_an_empty_list_has_no_button(self, db_path):
        event(db_path)
        sql(db_path, "UPDATE event_users SET status = 'going' WHERE status = 'notgoing' AND chat_id = ?", HUB)
        sql(db_path, "UPDATE events SET waitlist_data = '[]'")
        hub, _ = await render()
        assert not any("Not Going" in l or "Waitlist" in l for l in labels(hub))

    async def test_nobody_going_means_no_going_button(self, db_path):
        event(db_path, shared=False)
        sql(db_path, "UPDATE event_users SET status = 'notgoing' WHERE status = 'going'")
        hub, _ = await render()
        assert not any("Going (" in l and "Not" not in l for l in labels(hub))

    async def test_an_empty_list_is_shown_exactly_as_before(self, db_path):
        event(db_path, shared=False)
        sql(db_path, "UPDATE event_users SET status = 'going' WHERE status = 'notgoing'")
        hub, _ = await render()
        assert "*Not Going* \\(0\\):" in hub["text"]


class TestEachPostFoldsOnItsOwn:
    async def test_the_child_post_has_its_own_defaults_and_buttons(self, db_path):
        event(db_path)
        _, child = await render()
        text = child["text"]
        assert "erin" in text and "*Not Going* \\(1\\)" in text and "fran" not in text
        assert "*Waitlist* \\(1\\)" in text and "hank" not in text
        assert labels(child) == ["▾ Going (3)", "▸ Not Going (1)", "▸ Waitlist (1)"]

    async def test_unfolding_in_the_hub_leaves_the_child_alone_and_the_other_way_round(self, db_path):
        event(db_path)
        db_module.toggle_list_state("ev1", HUB, "notgoing")
        hub, child = await render()
        assert "carol" in hub["text"] and "fran" not in child["text"]
        db_module.toggle_list_state("ev1", CHILD, "notgoing")
        hub, child = await render()
        assert "carol" in hub["text"] and "fran" in child["text"]

    async def test_folding_going_in_the_child_hides_all_its_going_blocks(self, db_path):
        event(db_path)
        db_module.toggle_list_state("ev1", CHILD, "going")
        hub, child = await render()
        text = child["text"]
        assert "alice" not in text and "erin" not in text
        assert "*Going from Child Group*" not in text or "alice" not in text
        assert "*Going here:* \\(1\\)" in text and "alice" in hub["text"], "the hub post is untouched"

    @pytest.mark.parametrize("mode", ["-onlycount", "-hidden"])
    async def test_a_child_that_does_not_show_the_going_lists_has_no_going_button_and_nothing_folded(self, db_path, mode):
        event(db_path, share_mode=mode)
        db_module.toggle_list_state("ev1", CHILD, "going")
        _, child = await render()
        assert not any(l.endswith("Going (3)") and "Not" not in l for l in labels(child))
        assert "erin" in child["text"], "its own going list is shown as it always was"


class TestOnlyAnOpenEventFolds:
    @pytest.mark.parametrize("status", [1, 2, -1])
    async def test_in_verification_or_once_finished_every_list_is_shown_and_there_are_no_buttons(self, db_path, status):
        event(db_path, status=status)
        hub, child = await render()
        for post in (hub, child):
            assert toggles(post) == []
        assert "carol" in hub["text"] and "dave" in hub["text"] and "gina" in hub["text"] and "fran" in child["text"]

    async def test_the_choices_come_back_when_the_event_is_open_again(self, db_path):
        event(db_path)
        db_module.toggle_list_state("ev1", HUB, "notgoing")
        sql(db_path, "UPDATE events SET event_status = 2")
        closed, _ = await render()
        assert "carol" in closed["text"] and toggles(closed) == []
        sql(db_path, "UPDATE events SET event_status = 0")
        reopened, _ = await render()
        assert "carol" in reopened["text"] and "▾ Not Going (2)" in labels(reopened)


class TestTheKeyboardLayout:
    def keyboard(self, specs, status=0):
        return keyboard.create_event_keyboard("ev1", status, "👍", "❌", list_toggles=specs)

    def test_short_labels_share_one_row(self):
        rows = self.keyboard([("g", "▾ Going (5)"), ("n", "▸ Not Going (3)")]).inline_keyboard
        assert [b.callback_data for b in rows[-1]] == ["lv_ev1:g", "lv_ev1:n"]

    def test_long_labels_get_a_row_each(self):
        rows = self.keyboard([("g", "▾ Going (125)"), ("n", "▸ Not Going (123)"), ("w", "▸ Waitlist (120)")]).inline_keyboard
        assert [[b.callback_data for b in row] for row in rows[-3:]] == [["lv_ev1:g"], ["lv_ev1:n"], ["lv_ev1:w"]]

    def test_no_toggles_means_the_keyboard_is_exactly_what_it_was(self):
        assert [r for r in self.keyboard(None).inline_keyboard] == [r for r in keyboard.create_event_keyboard("ev1", 0, "👍", "❌").inline_keyboard]

    @pytest.mark.parametrize("status", [1, 2, -1])
    def test_only_an_open_event_shows_them(self, status):
        rows = self.keyboard([("g", "▾ Going (5)")], status=status).inline_keyboard
        assert not any(b.callback_data.startswith("lv_") for row in rows for b in row)

    def test_the_helper_leaves_out_what_cannot_be_folded_and_closed_events(self):
        specs = [("g", "Going", 5, False, True), ("n", "Not Going", 0, True, False), ("w", "Waitlist", 2, True, True)]
        assert event_engine._build_toggles(0, specs) == [("g", "▾ Going (5)"), ("w", "▸ Waitlist (2)")]
        assert event_engine._build_toggles(2, specs) == []


def press(data="lv_ev1:n", chat_id=int(HUB)):
    query = MagicMock()
    query.data, query.answer = data, AsyncMock()
    query.message.chat_id = chat_id
    update = MagicMock(callback_query=query)
    return update, query


async def run_press(update):
    refresh = AsyncMock()
    with patch("listview.schedule_view_refresh", new=refresh):
        await listview.listview_callback_handler(update, make_context())
    return refresh


class TestThePressItself:
    async def test_it_flips_the_post_answers_once_and_schedules_a_redraw(self, db_path):
        event(db_path)
        update, query = press()
        refresh = await run_press(update)
        assert db_module.get_list_states("ev1")[(HUB, "notgoing")] is False
        query.answer.assert_awaited_once_with()
        refresh.assert_called_once()
        update2, _ = press()
        await run_press(update2)
        assert db_module.get_list_states("ev1")[(HUB, "notgoing")] is True

    async def test_anyone_may_press_it(self, db_path):
        event(db_path)
        update, query = press()
        query.from_user = MagicMock(id=999_999)
        await run_press(update)
        assert db_module.get_list_states("ev1")[(HUB, "notgoing")] is False

    @pytest.mark.parametrize("key,name", [("g", "going"), ("n", "notgoing"), ("w", "waitlist")])
    async def test_each_key_flips_its_own_list(self, db_path, key, name):
        event(db_path)
        await run_press(press(f"lv_ev1:{key}")[0])
        assert list(db_module.get_list_states("ev1")) == [(HUB, name)]

    async def test_a_press_in_a_shared_chat_flips_that_posts_state_only(self, db_path):
        event(db_path)
        await run_press(press(chat_id=int(CHILD))[0])
        assert list(db_module.get_list_states("ev1")) == [(CHILD, "notgoing")]

    async def test_a_press_from_a_chat_that_has_no_post_of_the_event_changes_nothing(self, db_path):
        event(db_path)
        update, query = press(chat_id=-999)
        refresh = await run_press(update)
        assert db_module.get_list_states("ev1") == {} and refresh.call_count == 0
        query.answer.assert_awaited_once()

    @pytest.mark.parametrize("status", [1, 2, -1])
    async def test_a_press_on_an_event_that_is_not_open_changes_nothing_and_says_so(self, db_path, status):
        event(db_path, status=status)
        update, query = press()
        await run_press(update)
        assert db_module.get_list_states("ev1") == {}
        assert "no longer open" in query.answer.call_args.args[0]

    async def test_an_event_that_is_gone_is_said_so(self, db_path):
        update, query = press("lv_nothere:n")
        await run_press(update)
        assert query.answer.call_args.kwargs.get("show_alert") is True

    @pytest.mark.parametrize("data", ["lv_ev1", "lv_ev1:x", "lv_:", "lv_ev1:"])
    async def test_garbage_is_answered_and_ignored(self, db_path, data):
        event(db_path)
        update, query = press(data)
        refresh = await run_press(update)
        assert db_module.get_list_states("ev1") == {} and refresh.call_count == 0
        query.answer.assert_awaited_once()

    def test_an_unknown_list_is_refused_by_the_helper(self, db_path):
        with pytest.raises(ValueError):
            db_module.toggle_list_state("ev1", HUB, "everyone")


class TestCleanUp:
    async def test_deleting_the_event_removes_its_fold_states(self, db_path):
        event(db_path)
        db_module.toggle_list_state("ev1", HUB, "notgoing")
        db_module.toggle_list_state("ev1", CHILD, "going")
        with patch("handlers.delete_event_from_sheet", new=AsyncMock(return_value=None)):
            await handlers._perform_event_deletion(make_context(), HUB, "ev1")
        assert db_module.get_list_states("ev1") == {}

    def test_the_helper_removes_only_that_events_rows(self, db_path):
        db_module.toggle_list_state("ev1", HUB, "notgoing")
        db_module.toggle_list_state("ev2", HUB, "notgoing")
        db_module.delete_list_states("ev1")
        assert list(db_module.get_list_states("ev2")) == [(HUB, "notgoing")] and db_module.get_list_states("ev1") == {}


def test_the_handler_is_registered_before_the_catch_all():
    source = Path("main.py").read_text(encoding="utf-8")
    assert source.index('pattern="^lv_"') < source.index("CallbackQueryHandler(button_handler)")
