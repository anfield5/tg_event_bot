"""
Add Extra Member must find a person who belongs to a MONITORED child chat - even when the event was never shared
to that chat - and record them under THAT chat's list. The button itself stays hub-only.
"""
import sqlite3
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, ".")
import handlers
from tests.helpers import make_bot, make_chat, make_context, make_message, make_update, make_user

HUB, CHILD = "-100", "-200"


def seed(db_path, *, shared=False, monitored=True, tracked=((CHILD, "Andr", "555"),), voted=None, owner=HUB):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon, event_status, "
        "going_data, notgoing_data, counters_data, kicked_data) VALUES ('ev1','-100','1','Party','👍','❌',1,'[]','[]','{}','[]')"
    )
    if shared:
        conn.execute("INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type) "
                     "VALUES ('ev1','-200','5','-visible','channel')")
    conn.execute("INSERT INTO sub_chats (chat_id, owner_chat_id, chat_type, chat_name, is_monitored) "
                 "VALUES ('-200', ?, 'channel', 'News', ?)", (owner, 1 if monitored else 0))
    for chat, username, uid in tracked:
        conn.execute("INSERT INTO main_group_users (chat_id, username, user_id, first_name, last_name) VALUES (?, ?, ?, 'F', 'L')",
                     (chat, username, uid))
    if voted:
        conn.execute("INSERT INTO event_users (event_id, chat_id, user_id, username, status, guests) "
                     "VALUES ('ev1', ?, ?, ?, ?, 0)", voted)
    conn.commit()
    conn.close()


async def type_name(db_path, text):
    bot = make_bot()
    bot.send_message = AsyncMock()
    chat = make_chat(chat_id=int(HUB), chat_type="supergroup")        # the admin types in the HUB
    msg = make_message(chat=chat)
    msg.text, msg.delete = text, AsyncMock()
    upd = make_update(chat=chat, user=make_user(user_id=1, username="admin"), message=msg)
    ctx = make_context(bot=bot)
    ctx.user_data["awaiting_extra_player_for"] = "ev1"
    with patch("handlers.is_real_admin", new_callable=AsyncMock, return_value=True), \
         patch("handlers.get_sheet_for_chat", new_callable=AsyncMock, return_value=None), \
         patch("handlers.schedule_view_refresh", new_callable=AsyncMock):
        await handlers.handle_extra_player_input(upd, ctx)
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT chat_id, user_id, username, status FROM event_users WHERE event_id = 'ev1' ORDER BY rowid").fetchall()
    conn.close()
    return rows, bot


class TestFindingPeopleOfMonitoredChats:
    async def test_a_person_of_a_monitored_chat_the_event_is_not_shared_to_is_found(self, db_path):
        seed(db_path, shared=False)
        rows, bot = await type_name(db_path, "Andr")
        assert rows == [(CHILD, "555", "Andr", "going")]
        bot.send_message.assert_not_called()

    async def test_the_same_person_is_found_when_the_chat_is_shared_too(self, db_path):
        seed(db_path, shared=True)
        assert (await type_name(db_path, "Andr"))[0] == [(CHILD, "555", "Andr", "going")]

    async def test_a_chat_that_is_neither_shared_nor_monitored_is_not_searched(self, db_path):
        seed(db_path, monitored=False)
        rows, bot = await type_name(db_path, "Andr")
        assert rows == [(HUB, "Andr", "Andr", "going")], "falls back to plain text under the hub"
        bot.send_message.assert_awaited_once()

    async def test_another_hubs_monitored_chat_is_not_searched(self, db_path):
        seed(db_path, owner="-999")
        assert (await type_name(db_path, "Andr"))[0][0][0] == HUB

    async def test_the_hub_still_wins_when_the_person_is_in_both(self, db_path):
        seed(db_path, tracked=((CHILD, "Andr", "222"), (HUB, "Andr", "111")))
        assert (await type_name(db_path, "Andr"))[0] == [(HUB, "111", "Andr", "going")]

    async def test_the_name_is_matched_ignoring_case_and_stored_in_its_real_spelling(self, db_path):
        seed(db_path)
        assert (await type_name(db_path, "andr"))[0] == [(CHILD, "555", "Andr", "going")]

    async def test_cyrillic_names_match_ignoring_case_too(self, db_path):
        seed(db_path, tracked=((CHILD, "Андрій", "777"),))
        assert (await type_name(db_path, "андрій"))[0] == [(CHILD, "777", "Андрій", "going")]

    async def test_a_numeric_user_id_works_as_well(self, db_path):
        seed(db_path)
        assert (await type_name(db_path, "555"))[0] == [(CHILD, "555", "Andr", "going")]

    async def test_a_leading_at_sign_is_ignored(self, db_path):
        seed(db_path)
        assert (await type_name(db_path, "@Andr"))[0] == [(CHILD, "555", "Andr", "going")]


class TestNoDoubleCounting:
    async def test_someone_who_already_voted_in_the_child_keeps_that_row(self, db_path):
        seed(db_path, tracked=((CHILD, "Andr", "555"), (HUB, "Andr", "555")), voted=(CHILD, "555", "Andr", "going"))
        rows, _ = await type_name(db_path, "Andr")
        assert rows == [(CHILD, "555", "Andr", "going")], "no second row in the hub for the same person"

    async def test_a_not_going_vote_from_the_child_is_turned_into_going_in_the_same_place(self, db_path):
        seed(db_path, tracked=((CHILD, "Andr", "555"), (HUB, "Andr", "555")), voted=(CHILD, "555", "Andr", "notgoing"))
        assert (await type_name(db_path, "Andr"))[0] == [(CHILD, "555", "Andr", "going")]

    async def test_adding_the_same_person_twice_changes_nothing(self, db_path):
        seed(db_path)
        await type_name(db_path, "Andr")
        assert len((await type_name(db_path, "Andr"))[0]) == 1


class TestTheNoticeWhenNobodyKnowsThePerson:
    async def test_it_says_why_the_person_landed_as_plain_text(self, db_path):
        seed(db_path, tracked=())
        rows, bot = await type_name(db_path, "ghost")
        assert rows == [(HUB, "ghost", "ghost", "going")]
        text = bot.send_message.call_args.kwargs["text"].replace("\\", "")
        assert "ghost" in text and "plain text" in text and "monitor" in text

    async def test_a_person_who_was_found_gets_no_notice(self, db_path):
        seed(db_path)
        assert (await type_name(db_path, "Andr"))[1].send_message.await_count == 0

    async def test_a_failing_notice_does_not_break_the_add(self, db_path):
        seed(db_path, tracked=())
        with patch("handlers.logger"):
            bot = make_bot()
            bot.send_message = AsyncMock(side_effect=RuntimeError("flood"))
            chat = make_chat(chat_id=int(HUB), chat_type="supergroup")
            msg = make_message(chat=chat)
            msg.text, msg.delete = "ghost", AsyncMock()
            ctx = make_context(bot=bot)
            ctx.user_data["awaiting_extra_player_for"] = "ev1"
            with patch("handlers.is_real_admin", new_callable=AsyncMock, return_value=True), \
                 patch("handlers.get_sheet_for_chat", new_callable=AsyncMock, return_value=None), \
                 patch("handlers.schedule_view_refresh", new_callable=AsyncMock):
                await handlers.handle_extra_player_input(make_update(chat=chat, message=msg), ctx)
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT COUNT(*) FROM event_users").fetchone()[0] == 1


async def master_text(db_path):
    bot = make_bot()
    bot.get_chat = AsyncMock(return_value=MagicMock(title="News", type="channel"))
    await handlers.update_all_shared_views(make_context(bot=bot), "ev1")
    return bot.edit_message_text.call_args_list[0].kwargs["text"].replace("\\", "")


def add_row(db_path, chat, uid, username, status="going", guests=0):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO event_users (event_id, chat_id, user_id, username, status, guests) VALUES ('ev1', ?, ?, ?, ?, ?)",
                 (chat, uid, username, status, guests))
    conn.commit()
    conn.close()


class TestThePostShowsThemUnderTheirOwnChat:
    async def test_a_person_of_a_chat_that_is_not_a_share_gets_a_block_and_counts_in_total(self, db_path):
        seed(db_path, shared=False)
        add_row(db_path, CHILD, "555", "Andr", guests=2)
        text = await master_text(db_path)
        assert "Going from News* (3)" in text and "user?id=555" in text, "listed under its own chat, clickable"
        assert "TOTAL Going:* 3" in text

    async def test_a_shared_chat_is_still_shown_exactly_once(self, db_path):
        seed(db_path, shared=True)
        add_row(db_path, CHILD, "555", "Andr", guests=2)
        text = await master_text(db_path)
        assert text.count("Going from News") == 1 and "TOTAL Going:* 3" in text

    async def test_hub_people_and_the_other_chat_add_up(self, db_path):
        seed(db_path)
        add_row(db_path, HUB, "1", "hubguy")
        add_row(db_path, CHILD, "555", "Andr")
        assert "TOTAL Going:* 2" in await master_text(db_path)

    async def test_people_who_are_not_going_in_that_chat_add_no_block(self, db_path):
        seed(db_path)
        add_row(db_path, CHILD, "555", "Andr", status="notgoing")
        text = await master_text(db_path)
        assert "Going from News" not in text and "TOTAL Going:* 0" in text

    async def test_end_to_end_type_the_name_then_see_it_in_the_post(self, db_path):
        seed(db_path, shared=False)
        await type_name(db_path, "andr")
        text = await master_text(db_path)
        assert "Going from News* (1)" in text and "user?id=555" in text and "TOTAL Going:* 1" in text

    async def test_the_person_can_be_kicked_from_the_verification_view(self, db_path):
        seed(db_path, shared=False)
        await type_name(db_path, "Andr")
        conn = sqlite3.connect(db_path)
        # the same statement the kick button runs for a child-chat target ("ch-" prefix): by username, any chat
        conn.execute("UPDATE event_users SET status = 'kicked' WHERE event_id = 'ev1' AND username = 'Andr'")
        conn.commit()
        conn.close()
        assert "Going from News" not in await master_text(db_path)
