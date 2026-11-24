"""
Join form (joinform.py): the hub's one form (/joinform), one-time links to it (/joinlink), and a person answering in a DM and
then getting a one-time invitation. PRO feature "join_form".
"""
import asyncio
import sqlite3
import sys
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, ".")
import joinform
from tests.helpers import make_bot, make_callback_update, make_chat, make_context, make_message, make_update, make_user

HUB, CHILD, ADMIN, M1, M2 = "-100", "-200", 1, 11, 12


@pytest.fixture(autouse=True)
def _no_sheet():
    with patch("joinform.get_sheet_for_chat", new_callable=AsyncMock, return_value=None), \
         patch("joinform._write_sheet_row", new_callable=AsyncMock) as w:
        yield w


def sql(db_path, statement, *params):
    conn = sqlite3.connect(db_path)
    rows = conn.execute(statement, params).fetchall()
    conn.commit()
    conn.close()
    return rows


def make_pro(db_path, chat=HUB):
    end = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    sql(db_path, "INSERT INTO all_groups (chat_id, type, subs_date_start, subs_date_end) VALUES (?, 'PRO', ?, ?)", chat, end, end)


def add_child(db_path, alias="child"):
    sql(db_path, "INSERT INTO sub_chats (chat_id, owner_chat_id, alias, chat_name) VALUES (?, ?, ?, 'Child group')", CHILD, HUB, alias)


def bot_with(invite_right=True, admin_ids=(ADMIN,)):
    bot = make_bot()
    bot.username = "eventbot"

    async def member(chat_id, user_id):
        if user_id == bot.id:
            return MagicMock(status="administrator", can_invite_users=invite_right)
        return MagicMock(status="administrator" if user_id in admin_ids else "member")
    bot.get_chat_member = AsyncMock(side_effect=member)
    bot.create_chat_invite_link = AsyncMock(return_value=MagicMock(invite_link="https://t.me/+ONETIME"))
    return bot


async def admin_says(bot, text, user_id=ADMIN, command=None):
    """Runs /joinform or /joinlink as `user_id` in the hub group -> the list of the replies."""
    command = command or (joinform.joinform_command if text.startswith("/joinform") else joinform.joinlink_command)
    chat = make_chat(chat_id=int(HUB), chat_type="supergroup")
    upd = make_update(chat=chat, user=make_user(user_id=user_id), message=make_message(chat=chat, user=make_user(user_id=user_id), text=text))
    ctx = make_context(bot=bot, args=text.split()[1:])
    await command(upd, ctx)
    return [c.args[0] for c in upd.message.reply_text.call_args_list]


def person(user_id=M1):
    chat = make_chat(chat_id=user_id, chat_type="private")
    return chat, make_user(user_id=user_id, username=f"u{user_id}", first_name="Ann")


async def opens_link(bot, token, user_id=M1):
    chat, user = person(user_id)
    upd = make_update(chat=chat, user=user, message=make_message(chat=chat, user=user, text=f"/start jf_{token}"))
    await joinform.begin_form(upd, make_context(bot=bot, args=[f"jf_{token}"]), token)
    return [c.args[0] for c in upd.message.reply_text.call_args_list]


async def types(bot, text, user_id=M1):
    chat, user = person(user_id)
    upd = make_update(chat=chat, user=user, message=make_message(chat=chat, user=user, text=text))
    assert joinform.has_active_form(upd)
    await joinform.handle_text_answer(upd, make_context(bot=bot))
    return [c.args[0] for c in upd.message.reply_text.call_args_list]


async def taps(bot, token, index, user_id=M1):
    upd = make_callback_update(f"jfc_{token}_{index}", chat_id=user_id, user=make_user(user_id=user_id))
    upd.callback_query.edit_message_text = AsyncMock()
    upd.callback_query.message.text = "Question"
    await joinform.choice_callback(upd, make_context(bot=bot))


def sent_texts(bot):
    return [c.kwargs.get("text", "") for c in bot.send_message.call_args_list]


def only_token(db_path):
    return sql(db_path, "SELECT token FROM join_form_links")[0][0]


@pytest.fixture()
def hub(db_path):
    make_pro(db_path)
    add_child(db_path)
    return db_path


class TestFormCommands:
    async def test_add_list_edit_reorder_remove(self, hub):
        bot = bot_with()
        await admin_says(bot, '/joinform add "Your name?"')
        replies = await admin_says(bot, '/joinform add "Sport?" football;tennis;none')
        assert "1. Your name?" in replies[0] and "2. Sport?" in replies[0] and "football | tennis | none" in replies[0]
        await admin_says(bot, '/joinform edit 1 "Full name?"')
        await admin_says(bot, "/joinform reorder 1 2")
        assert [q["question"] for q in joinform.get_questions(HUB)] == ["Sport?", "Full name?"]
        await admin_says(bot, "/joinform remove 1")
        assert [q["question"] for q in joinform.get_questions(HUB)] == ["Full name?"]
        assert joinform.get_questions(HUB)[0]["position"] == 1

    async def test_the_form_has_at_most_four_questions(self, hub):
        bot = bot_with()
        for i in range(joinform.MAX_QUESTIONS):
            await admin_says(bot, f'/joinform add "Q{i}"')
        replies = await admin_says(bot, '/joinform add "One too many"')
        assert len(joinform.get_questions(HUB)) == 4 and "4 questions already" in replies[0]

    @pytest.mark.parametrize("text", ['/joinform add "Q" onlyone', '/joinform add "Q" a;a', '/joinform add', '/joinform remove 9', '/joinform what'])
    async def test_a_bad_command_changes_nothing_and_says_how(self, hub, text):
        await admin_says(bot_with(), '/joinform add "Keep"')
        replies = await admin_says(bot_with(), text)
        assert [q["question"] for q in joinform.get_questions(HUB)] == ["Keep"] and replies

    async def test_a_non_admin_cannot_and_a_free_hub_is_not_offered_it(self, db_path):
        make_pro(db_path)
        replies = await admin_says(bot_with(), '/joinform add "Q"', user_id=99)
        assert "Only admins" in replies[0] and joinform.get_questions(HUB) == []
        sql(db_path, "UPDATE all_groups SET type = 'FREE' WHERE chat_id = ?", HUB)
        await admin_says(bot_with(), '/joinform add "Q"')
        assert joinform.get_questions(HUB) == []


class TestLinks:
    async def test_a_link_needs_questions_and_a_known_chat_where_the_bot_may_invite(self, hub):
        replies = await admin_says(bot_with(), "/joinlink child")
        assert "no questions" in replies[0] and sql(hub, "SELECT * FROM join_form_links") == []
        await admin_says(bot_with(), '/joinform add "Q"')
        assert "No chat" in (await admin_says(bot_with(), "/joinlink nowhere"))[0]
        assert "Invite users" in (await admin_says(bot_with(invite_right=False), "/joinlink child"))[0]
        assert sql(hub, "SELECT * FROM join_form_links") == []

    async def test_a_link_is_made_for_an_alias_main_or_a_known_id(self, hub):
        await admin_says(bot_with(), '/joinform add "Q"')
        for target, chat in (("child", CHILD), ("main", HUB), (CHILD, CHILD)):
            replies = await admin_says(bot_with(), f"/joinlink {target}")
            assert "https://t.me/eventbot?start=jf_" in replies[0]
        assert sorted(r[0] for r in sql(hub, "SELECT target_chat_id FROM join_form_links")) == sorted([CHILD, HUB, CHILD])

    async def test_list_and_revoke(self, hub):
        bot = bot_with()
        await admin_says(bot, '/joinform add "Q"'); await admin_says(bot, "/joinlink child")
        token = only_token(hub)
        assert token in (await admin_says(bot, "/joinlink list"))[0]
        await admin_says(bot, f"/joinlink revoke {token}")
        assert "doesn't work" in (await opens_link(bot, token))[0]


class TestAPersonFillsTheForm:
    async def setup_form(self, hub, bot, choices=True):
        await admin_says(bot, '/joinform add "Your name?"')
        if choices:
            await admin_says(bot, '/joinform add "Sport?" football;tennis;none')
        await admin_says(bot, "/joinlink child")
        return only_token(hub)

    async def test_the_whole_way_to_a_one_time_invitation(self, hub):
        bot = bot_with()
        token = await self.setup_form(hub, bot)
        await opens_link(bot, token)
        assert "Question 1 of 2" in sent_texts(bot)[-1] and "Your name?" in sent_texts(bot)[-1]
        await types(bot, "Ann Lee")
        assert "Sport?" in sent_texts(bot)[-1] and bot.send_message.call_args.kwargs["reply_markup"] is not None
        await taps(bot, token, 1)
        bot.create_chat_invite_link.assert_awaited_once()
        kwargs = bot.create_chat_invite_link.call_args.kwargs
        assert kwargs["chat_id"] == int(CHILD) and kwargs["member_limit"] == 1 and kwargs["expire_date"] > datetime.now()
        assert "https://t.me/+ONETIME" in sent_texts(bot)[-1]
        assert sql(hub, "SELECT status, user_id FROM join_form_links") == [("done", str(M1))]
        assert sorted(sql(hub, "SELECT question_text, answer FROM join_form_responses")) == [("Sport?", "tennis"), ("Your name?", "Ann Lee")]

    async def test_the_link_is_for_one_person_and_one_use(self, hub):
        bot = bot_with()
        token = await self.setup_form(hub, bot, choices=False)
        await opens_link(bot, token, M1)
        assert "taken by someone else" in (await opens_link(bot, token, M2))[0]
        await types(bot, "Ann")
        assert "used already" in (await opens_link(bot, token, M1))[0]
        assert bot.create_chat_invite_link.await_count == 1

    async def test_opening_the_link_again_continues_where_the_person_stopped(self, hub):
        bot = bot_with()
        token = await self.setup_form(hub, bot)
        await opens_link(bot, token)
        await types(bot, "Ann")
        bot.send_message.reset_mock()
        replies = await opens_link(bot, token)
        assert "continue" in replies[0] and "Question 2 of 2" in sent_texts(bot)[-1]
        assert len(sql(hub, "SELECT * FROM join_form_responses")) == 1

    async def test_a_typed_answer_to_a_buttons_question_must_be_a_choice(self, hub):
        bot = bot_with()
        token = await self.setup_form(hub, bot)
        await opens_link(bot, token); await types(bot, "Ann")
        assert "tap one of the buttons" in (await types(bot, "badminton"))[0]
        assert len(sql(hub, "SELECT * FROM join_form_responses")) == 1
        await types(bot, "Tennis")
        assert ("Sport?", "tennis") in sql(hub, "SELECT question_text, answer FROM join_form_responses")

    async def test_a_too_long_answer_is_refused(self, hub):
        bot = bot_with()
        token = await self.setup_form(hub, bot, choices=False)
        await opens_link(bot, token)
        assert "too long" in (await types(bot, "x" * (joinform.MAX_ANSWER_LEN + 1)))[0]
        assert sql(hub, "SELECT * FROM join_form_responses") == []

    async def test_someone_elses_tap_or_an_old_button_changes_nothing(self, hub):
        bot = bot_with()
        token = await self.setup_form(hub, bot)
        await opens_link(bot, token); await types(bot, "Ann")
        await taps(bot, token, 0, user_id=M2)
        assert len(sql(hub, "SELECT * FROM join_form_responses")) == 1
        await taps(bot, token, 7)
        assert len(sql(hub, "SELECT * FROM join_form_responses")) == 1

    async def test_if_the_invitation_fails_the_answers_stay_and_the_admin_is_told_and_a_retry_works(self, hub):
        bot = bot_with()
        token = await self.setup_form(hub, bot, choices=False)
        bot.create_chat_invite_link = AsyncMock(side_effect=RuntimeError("not enough rights"))
        await opens_link(bot, token); await types(bot, "Ann")
        assert sql(hub, "SELECT status FROM join_form_links") == [("answered",)]
        assert any("couldn't create your invitation" in t for t in sent_texts(bot))
        assert any(c.kwargs.get("chat_id") == ADMIN and "not enough rights" in c.kwargs["text"] for c in bot.send_message.call_args_list)
        bot.create_chat_invite_link = AsyncMock(return_value=MagicMock(invite_link="https://t.me/+RETRY"))
        await opens_link(bot, token)
        assert "https://t.me/+RETRY" in sent_texts(bot)[-1] and sql(hub, "SELECT status FROM join_form_links") == [("done",)]

    async def test_a_lapsed_pro_subscription_stops_the_form(self, hub):
        bot = bot_with()
        token = await self.setup_form(hub, bot, choices=False)
        sql(hub, "UPDATE all_groups SET type = 'FREE' WHERE chat_id = ?", HUB)
        assert "not active" in (await opens_link(bot, token))[0]

    async def test_a_plain_dm_message_is_not_taken_for_an_answer(self, hub):
        chat, user = person(M2)
        assert not joinform.has_active_form(make_update(chat=chat, user=user, message=make_message(chat=chat, user=user, text="hi")))
        group = make_chat(chat_id=int(HUB), chat_type="supergroup")
        assert not joinform.has_active_form(make_update(chat=group, user=make_user(user_id=M1)))

    async def test_the_answers_go_to_the_sheet_once_the_form_is_finished(self, hub, _no_sheet):
        bot = bot_with()
        token = await self.setup_form(hub, bot, choices=False)
        await opens_link(bot, token); await types(bot, "Ann")
        await asyncio.sleep(0)                      # the sheet write is a background task
        _no_sheet.assert_awaited_once()


class _Ws:
    def __init__(self, header=None):
        self.header, self.rows, self.updated = header or [], [], []

    async def row_values(self, n):
        return list(self.header)

    async def update(self, cell, values):
        self.updated.append((cell, values)); self.header = list(values[0])

    async def append_row(self, row):
        self.rows.append(row)


class _Ss:
    def __init__(self, ws=None):
        self.ws = ws

    async def worksheet(self, name):
        from gspread.exceptions import WorksheetNotFound
        if self.ws is None:
            raise WorksheetNotFound(name)
        return self.ws

    async def add_worksheet(self, title, rows, cols):
        self.ws = _Ws()
        return self.ws


class TestTheSheetTab:
    async def write(self, hub, ss, name_answers):
        sql(hub, "INSERT INTO join_form_links (token, hub_chat_id, target_chat_id, created_by, status) VALUES ('tok', ?, ?, '1', 'answered')", HUB, CHILD)
        for i, (q, a) in enumerate(name_answers, start=1):
            sql(hub, "INSERT INTO join_form_responses (token, question_id, question_text, answer) VALUES ('tok', ?, ?, ?)", i, q, a)
        link = joinform._link("tok")
        with patch("joinform.get_sheet_for_chat", new_callable=AsyncMock, return_value="sheet"), \
             patch("joinform.open_spreadsheet", new_callable=AsyncMock, return_value=ss):
            await REAL_WRITE(link, make_user(user_id=M1, username="ann", first_name="Ann", last_name="Lee"))

    async def test_the_tab_is_created_with_the_questions_as_columns(self, hub):
        ss = _Ss()
        await self.write(hub, ss, [("Name?", "Ann"), ("Sport?", "tennis")])
        assert ss.ws.header == joinform.SHEET_BASE + ["Name?", "Sport?"]
        row = ss.ws.rows[0]
        assert row[1:5] == [str(M1), "ann", "Ann Lee", CHILD] and row[5:] == ["Ann", "tennis"]

    async def test_a_new_question_adds_a_column_and_an_old_row_layout_is_kept(self, hub):
        ss = _Ss(_Ws(joinform.SHEET_BASE + ["Name?"]))
        await self.write(hub, ss, [("Name?", "Ann"), ("Sport?", "tennis")])
        assert ss.ws.header == joinform.SHEET_BASE + ["Name?", "Sport?"] and ss.ws.rows[0][5:] == ["Ann", "tennis"]

    async def test_a_failing_sheet_does_not_raise(self, hub):
        ss = MagicMock(); ss.worksheet = AsyncMock(side_effect=RuntimeError("quota"))
        await self.write(hub, ss, [("Name?", "Ann")])


REAL_WRITE = joinform._write_sheet_row


class TestStartLink:
    async def test_start_with_a_form_token_goes_to_the_form_not_the_group_list(self, db_path):
        from hub_resolver import start_command
        chat, user = person()
        upd = make_update(chat=chat, user=user, message=make_message(chat=chat, user=user, text="/start jf_abc"))
        with patch("joinform.begin_form", new_callable=AsyncMock) as begin:
            await start_command(upd, make_context(args=["jf_abc"]))
        begin.assert_awaited_once()
        assert begin.call_args.args[2] == "abc"

    async def test_the_feature_is_seeded_as_pro(self, db_path):
        assert sql(db_path, "SELECT min_tier FROM all_features WHERE feature_key = 'join_form'") == [("PRO",)]
