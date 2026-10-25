"""
Reopen for ANY closed event, one active at a time, and what a reopen does to the books: the Reopen buttons on the
posts (kept in line with the rule), the EventUsers tab / Events row / Actions after the event is closed again - the
people added and removed in between are logged - and the hooks that keep the buttons right as events come and go.
"""
import json
import sqlite3
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, ".")
import db as db_module
import event_engine
import handlers
from tests.helpers import make_bot, make_chat, make_context, make_update, make_user
from tests.test_eventusers_lifecycle import World, first_close
from tests.test_reopen import HUB, callbacks, seed


# ── the Reopen buttons on the posts ──────────────────────────────────────────

async def refresh(**kwargs):
    bot = make_bot()
    bot.edit_message_reply_markup = AsyncMock()
    with patch("event_engine.REOPEN_REFRESH_PAUSE", 0):
        await event_engine.refresh_reopen_buttons(bot, HUB, **kwargs)
    return {c.kwargs["message_id"]: callbacks(c.kwargs["reply_markup"]) if c.kwargs["reply_markup"] else None
            for c in bot.edit_message_reply_markup.call_args_list}, bot


def seed_with_message(db_path, event_id, status, message_id):
    seed(db_path, event_id, status=status)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE events SET message_id = ? WHERE event_id = ?", (str(message_id), event_id))
    conn.commit()
    conn.close()


class TestKeepingTheButtonsRight:
    async def test_every_recent_closed_or_canceled_post_gets_the_button(self, db_path):
        seed_with_message(db_path, "a", 2, 11); seed_with_message(db_path, "b", -1, 12); seed_with_message(db_path, "c", 2, 13)
        db_module.set_reopen_mode(HUB, True)
        edited, _ = await refresh()
        assert edited == {11: ["reopen_a"], 12: ["reopen_b"], 13: ["reopen_c"]}

    async def test_while_an_event_is_active_every_post_loses_it(self, db_path):
        seed_with_message(db_path, "a", 2, 11); seed_with_message(db_path, "live", 0, 12)
        db_module.set_reopen_mode(HUB, True)
        edited, _ = await refresh()
        assert edited == {11: None}, "the closed post is cleared; the live event's own post is not touched"

    async def test_mode_off_clears_them_all(self, db_path):
        seed_with_message(db_path, "a", 2, 11)
        edited, _ = await refresh()
        assert edited == {11: None}

    async def test_a_post_that_slid_out_of_the_window_loses_its_button_and_much_older_ones_are_left_alone(self, db_path):
        limit = db_module.REOPEN_BUTTON_LIMIT
        for n in range(2 * limit + 4):
            seed_with_message(db_path, f"e{n}", 2, 100 + n)
        db_module.set_reopen_mode(HUB, True)
        edited, _ = await refresh()
        newest = 100 + 2 * limit + 3
        assert edited[newest] is not None and edited[newest - limit + 1] is not None
        assert edited[newest - limit] is None, "just outside the window: cleared"
        assert (100) not in edited and len(edited) == 2 * limit

    async def test_the_post_that_was_just_rendered_whole_is_skipped(self, db_path):
        seed_with_message(db_path, "a", 2, 11); seed_with_message(db_path, "b", 2, 12)
        db_module.set_reopen_mode(HUB, True)
        edited, _ = await refresh(skip_event_id="a")
        assert list(edited) == [12]

    async def test_posts_without_a_message_id_are_skipped(self, db_path):
        seed_with_message(db_path, "a", 2, 11)
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE events SET message_id = NULL WHERE event_id = 'a'")
        conn.commit()
        conn.close()
        db_module.set_reopen_mode(HUB, True)
        assert (await refresh())[0] == {}

    async def test_a_gone_or_unchanged_post_does_not_stop_the_rest(self, db_path):
        seed_with_message(db_path, "a", 2, 11); seed_with_message(db_path, "b", 2, 12)
        db_module.set_reopen_mode(HUB, True)
        bot = make_bot()
        bot.edit_message_reply_markup = AsyncMock(side_effect=[RuntimeError("Message to edit not found"), None])
        with patch("event_engine.REOPEN_REFRESH_PAUSE", 0):
            await event_engine.refresh_reopen_buttons(bot, HUB)
        assert bot.edit_message_reply_markup.await_count == 2

    async def test_other_hubs_posts_are_never_touched(self, db_path):
        seed_with_message(db_path, "mine", 2, 11)
        seed(db_path, "theirs", status=2, hub="-999")
        db_module.set_reopen_mode(HUB, True)
        _, bot = await refresh()
        assert [c.kwargs["chat_id"] for c in bot.edit_message_reply_markup.call_args_list] == [int(HUB)]


class TestWhatTriggersARefresh:
    @pytest.mark.parametrize("status,mode,expected", [(2, True, True), (-1, True, True), (0, True, False), (2, False, False)])
    async def test_rendering_a_finished_event_refreshes_the_other_posts(self, db_path, status, mode, expected):
        seed(db_path, status=status)
        db_module.set_reopen_mode(HUB, mode)
        bot = make_bot()
        bot.get_chat = AsyncMock(return_value=MagicMock(title="News", type="channel"))
        with patch("event_engine.refresh_reopen_buttons", new=AsyncMock()) as buttons:
            await event_engine.update_all_shared_views(make_context(bot=bot), "ev1")
        assert buttons.called is expected
        if expected:
            assert buttons.call_args.args[1] == HUB and buttons.call_args.kwargs == {"skip_event_id": "ev1"}

    async def test_a_new_event_takes_the_buttons_off_the_old_posts(self, db_path):
        db_module.set_reopen_mode("-100123", True)
        ctx = make_context(args=["Party"])
        with patch("handlers.refresh_reopen_buttons", new=AsyncMock()) as buttons, \
             patch("handlers.get_sheet_for_chat", new_callable=AsyncMock, return_value=None), \
             patch("handlers.open_spreadsheet", new_callable=AsyncMock, return_value=None):
            await handlers.newevent(make_update(chat=make_chat(chat_id=-100123)), ctx)
        buttons.assert_called_once()
        assert buttons.call_args.args[1] == "-100123" and buttons.call_args.kwargs["skip_event_id"]

    async def test_but_not_when_the_mode_is_off(self, db_path):
        ctx = make_context(args=["Party"])
        with patch("handlers.refresh_reopen_buttons", new=AsyncMock()) as buttons, \
             patch("handlers.get_sheet_for_chat", new_callable=AsyncMock, return_value=None), \
             patch("handlers.open_spreadsheet", new_callable=AsyncMock, return_value=None):
            await handlers.newevent(make_update(chat=make_chat(chat_id=-100123)), ctx)
        buttons.assert_not_called()

    async def test_deleting_the_active_event_gives_the_old_ones_their_buttons_back(self, db_path):
        seed(db_path, "old", status=2); seed(db_path, "live", status=0)
        db_module.set_reopen_mode(HUB, True)
        with patch("handlers.refresh_reopen_buttons", new=AsyncMock()) as buttons, \
             patch("handlers.delete_event_from_sheet", new=AsyncMock(return_value=None)):
            await handlers._perform_event_deletion(make_context(), HUB, "live")
        buttons.assert_called_once()
        assert buttons.call_args.args[1] == HUB


# ── what a reopen does to the books ──────────────────────────────────────────

def snapshot(db_path, event_id="ev1"):
    conn = sqlite3.connect(db_path)
    raw = conn.execute("SELECT reopened_roster FROM events WHERE event_id = ?", (event_id,)).fetchone()[0]
    conn.close()
    return None if raw is None else json.loads(raw)


def actions(world, *names):
    return [r for r in world.book.tabs["Actions"].rows[1:] if r[1] in names]


class TestTheBooksAfterAReopen:
    async def test_the_roster_it_was_closed_with_is_remembered(self, db_path):
        w = World(db_path)
        await first_close(w)
        await w.reopen()
        assert snapshot(db_path) == {"1": "alice", "3": "carol"}

    async def test_closing_again_logs_who_was_added_and_who_was_removed_and_rewrites_everything(self, db_path):
        w = World(db_path)
        await first_close(w)
        await w.reopen()
        await w.press("close_ev1")
        await w.press("return_ev1:ch-bob")                        # bob is back: added
        await w.add_extra("dave")                                 # dave is new: added
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE event_users SET status = 'notgoing' WHERE event_id = 'ev1' AND user_id = '1'")   # alice drops out
        conn.commit()
        conn.close()
        await w.press("save_ev1")

        assert sorted(w.book.roster()) == ["2", "3", "4"], "EventUsers rewritten with exactly the new roster"
        assert w.book.events_row()[6:] == ["CLOSED", 3], "the Events row follows"
        added = sorted((r[2], r[3]) for r in actions(w, "ROSTER_ADDED"))
        removed = [(r[2], r[3]) for r in actions(w, "ROSTER_REMOVED")]
        assert added == [("bob", "2"), ("dave", "4")] and removed == [("alice", "1")]
        assert all(r[5] == "-100123" and r[4] for r in actions(w, "ROSTER_ADDED", "ROSTER_REMOVED"))
        assert snapshot(db_path) is None, "logged once, then forgotten"

    async def test_nothing_changed_means_no_roster_rows_but_the_usual_ones_are_there(self, db_path):
        w = World(db_path)
        await first_close(w)
        await w.reopen()
        await w.press("close_ev1")
        await w.press("save_ev1")
        assert actions(w, "ROSTER_ADDED", "ROSTER_REMOVED") == []
        assert actions(w, "REOPEN") and sorted(w.book.roster()) == ["1", "3"]

    async def test_a_canceled_event_has_no_roster_to_compare_with(self, db_path):
        w = World(db_path)
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE events SET event_status = -1 WHERE event_id = 'ev1'")
        conn.commit()
        conn.close()
        await w.reopen()
        assert snapshot(db_path) is None
        await w.press("close_ev1")
        await w.press("save_ev1")
        assert actions(w, "ROSTER_ADDED", "ROSTER_REMOVED") == [] and sorted(w.book.roster()) == ["1", "2"]

    async def test_an_event_closed_with_nobody_logs_everyone_who_joined_later(self, db_path):
        w = World(db_path)
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE event_users SET status = 'notgoing' WHERE event_id = 'ev1'")
        conn.execute("UPDATE events SET event_status = 2 WHERE event_id = 'ev1'")
        conn.commit()
        conn.close()
        await w.reopen()
        assert snapshot(db_path) == {}
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE event_users SET status = 'going' WHERE user_id = '1'")
        conn.commit()
        conn.close()
        await w.press("close_ev1")
        await w.press("save_ev1")
        assert [(r[2], r[3]) for r in actions(w, "ROSTER_ADDED")] == [("alice", "1")]

    async def test_if_the_sheet_fails_on_the_second_save_the_snapshot_is_kept_for_the_next_try(self, db_path):
        w = World(db_path)
        await first_close(w)
        await w.reopen()
        await w.press("close_ev1")
        w.book.tabs["EventUsers"].fail_append = True
        await w.press("save_ev1")
        assert snapshot(db_path) == {"1": "alice", "3": "carol"} and actions(w, "ROSTER_ADDED", "ROSTER_REMOVED") == []

    async def test_a_reopen_whose_sheet_cleanup_fails_changes_nothing_and_remembers_nothing(self, db_path):
        w = World(db_path)
        await first_close(w)
        with patch("reopen.reopen_event_in_sheet", new=AsyncMock(side_effect=RuntimeError("quota"))):
            db_module.set_reopen_mode("-100123", True)
            ok, text = await _perform(w)
        assert not ok and "nothing was changed" in text and snapshot(db_path) is None


async def _perform(w):
    import reopen
    ctx = w.context()
    with w.patches():
        with patch("reopen.reopen_event_in_sheet", new=AsyncMock(side_effect=RuntimeError("quota"))):
            return await reopen.perform_reopen(ctx, "-100123", "ev1", make_user(user_id=555, username="boss"), "-100123")


class TestTheDiffHelper:
    async def test_no_snapshot_means_nothing_is_written(self, db_path):
        seed(db_path, status=2)
        ss = MagicMock()
        assert await event_engine._log_reopen_roster_diff(ss, HUB, "ev1", [("1", "going", 0, "alice")]) == 0
        ss.worksheet.assert_not_called()

    async def test_only_going_people_count_and_a_person_without_a_name_is_shown_by_id(self, db_path):
        seed(db_path, status=2)
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE events SET reopened_roster = ? WHERE event_id = 'ev1'", (json.dumps({"1": "alice", "2": "bob"}),))
        conn.commit()
        conn.close()
        ws = MagicMock(append_rows=AsyncMock())
        ss = MagicMock(worksheet=AsyncMock(return_value=ws))
        rows = [("1", "going", 0, "alice"), ("2", "kicked", 0, "bob"), ("3", "going", 1, None)]
        assert await event_engine._log_reopen_roster_diff(ss, HUB, "ev1", rows) == 2
        written = ws.append_rows.call_args.args[0]
        assert [(r[1], r[2], r[3]) for r in written] == [("ROSTER_ADDED", "3", "3"), ("ROSTER_REMOVED", "bob", "2")]

    def test_a_broken_snapshot_is_ignored(self, db_path):
        seed(db_path, status=2)
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE events SET reopened_roster = 'not json' WHERE event_id = 'ev1'")
        conn.commit()
        conn.close()
        assert db_module.get_reopened_roster("ev1") is None
