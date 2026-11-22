"""
/refreshusersall -d | -detailed: every chat on its own line as "Chatname: joined N, left M" (+ a total), where the numbers
are what the Users tab really got in this run (sheets.sync_users_sheet's `stats`); without the option the report is the
usual "Synced: ..." one. Unknown options are refused before anything is synced.
"""
import sqlite3
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, ".")
import handlers
from tests.helpers import make_bot, make_chat, make_context, make_message, make_update, make_user
from tests.test_handlers_async import insert_premium
from tests.test_presence_log import _Ss, _Ws, _log_row, _row, _sync


class TestSyncStats:
    async def test_a_new_member_is_a_join(self):
        stats = {}
        users = _Ws([_row("1")])
        with patch("sheets.get_sheet_for_chat", new_callable=AsyncMock, return_value="s"), \
             patch("sheets.open_spreadsheet", new_callable=AsyncMock, return_value=_Ss(users, _Ws())):
            import sheets
            await sheets.sync_users_sheet("-100", [("1", "u1", "A", "B"), ("2", "u2", "C", "D")], stats=stats)
        assert stats == {"joined": 1, "left": 0}

    async def test_someone_who_came_back_is_a_join(self):
        stats = {}
        users = _Ws([_row("1", status="LEFT", end="02.01.2026")])
        log = _Ws([_log_row("1")])
        with patch("sheets.get_sheet_for_chat", new_callable=AsyncMock, return_value="s"), \
             patch("sheets.open_spreadsheet", new_callable=AsyncMock, return_value=_Ss(users, log)):
            import sheets
            await sheets.sync_users_sheet("-100", [("1", "u1", "A", "B")], stats=stats)
        assert stats == {"joined": 1, "left": 0}

    async def test_members_who_are_gone_and_leavers_the_sheet_never_knew_are_lefts(self):
        stats = {}
        users = _Ws([_row("1"), _row("2")])
        with patch("sheets.get_sheet_for_chat", new_callable=AsyncMock, return_value="s"), \
             patch("sheets.open_spreadsheet", new_callable=AsyncMock, return_value=_Ss(users, _Ws())):
            import sheets
            await sheets.sync_users_sheet("-100", [("2", "u2", "A", "B")],
                                          left_members=[("9", "ghost", "G", "H", "05.01.2026 10:00:00.000")], stats=stats)
        assert stats == {"joined": 0, "left": 2}             # user 1 flipped to LEFT + the never-synced leaver 9

    async def test_present_members_are_neither(self):
        stats = {}
        users = _Ws([_row("1")])
        with patch("sheets.get_sheet_for_chat", new_callable=AsyncMock, return_value="s"), \
             patch("sheets.open_spreadsheet", new_callable=AsyncMock, return_value=_Ss(users, _Ws())):
            import sheets
            await sheets.sync_users_sheet("-100", [("1", "u1", "A", "B")], stats=stats)
        assert stats == {"joined": 0, "left": 0}

    async def test_nobody_is_counted_as_left_when_the_log_could_not_be_written(self):
        stats = {}
        users, log = _Ws([_row("1")]), _Ws()
        log.fail_append = RuntimeError("quota")
        with patch("sheets.get_sheet_for_chat", new_callable=AsyncMock, return_value="s"), \
             patch("sheets.open_spreadsheet", new_callable=AsyncMock, return_value=_Ss(users, log)):
            import sheets
            await sheets.sync_users_sheet("-100", [], stats=stats)
        assert stats["left"] == 0

    async def test_the_old_call_without_stats_still_works(self):
        assert await _sync(_Ss(_Ws([_row("1")]), _Ws()), [("1", "u1", "A", "B")]) is True


def _world(db_path):
    conn = sqlite3.connect(db_path)
    insert_premium(db_path, chat_id="-100")
    conn.execute("INSERT INTO main_group_users (chat_id, user_id, username, first_name, last_name, status) VALUES ('-100','55','hubperson','Hub','Person','active')")
    conn.execute("INSERT INTO sub_chats (chat_id, owner_chat_id, alias, is_monitored, chat_name) VALUES ('-200','-100','child',1,'Kids_Chat')")
    conn.execute("INSERT INTO main_group_users (chat_id, user_id, username, first_name, last_name, status) VALUES ('-200','7','kid','Kid','One','active')")
    conn.commit(); conn.close()


async def run(args, sync_result=True, fill=None):
    bot = make_bot()
    bot.get_chat = AsyncMock(return_value=MagicMock(title="Real Hub"))
    bot.get_chat_administrators = AsyncMock(return_value=[])

    async def member(chat_id, user_id):
        if str(user_id) == "1":
            return MagicMock(status="administrator")
        m = MagicMock(status="left" if (str(user_id) == "7") else "member")     # the kid has left Kids_Chat
        m.user.username, m.user.first_name, m.user.last_name = "x", "X", "Y"
        return m
    bot.get_chat_member = AsyncMock(side_effect=member)
    chat = make_chat(chat_id=-100, chat_type="supergroup")
    msg = make_message(chat=chat)
    upd = make_update(chat=chat, user=make_user(user_id=1), message=msg)
    calls = []

    async def fake_sync(cid, members, sheet_owner_chat_id=None, left_members=None, stats=None):
        calls.append((cid, stats is not None))
        if stats is not None and fill and cid in fill:
            stats.update(fill[cid])
        return sync_result

    with patch("handlers.sync_users_sheet", side_effect=fake_sync):
        await handlers.refreshusersall(upd, make_context(bot=bot, args=args))
    texts = [c.args[0] for c in msg.reply_text.call_args_list]
    return texts, calls


class TestDetailedReport:
    async def test_without_the_option_the_report_is_the_usual_one(self, db_path):
        _world(db_path)
        texts, calls = await run([])
        report = texts[-1]
        assert "Synced" in report and "joined" not in report and "Total" not in report
        assert all(passed is False for _cid, passed in calls), "no stats are collected unless asked for"

    async def test_each_chat_gets_a_joined_and_left_line_and_there_is_a_total(self, db_path):
        _world(db_path)
        texts, _ = await run(["-d"], fill={"-100": {"joined": 3, "left": 1}, "-200": {"joined": 0, "left": 2}})
        report = texts[-1]
        assert "Real Hub: joined 3, left 1" in report
        assert "Kids\\_Chat: joined 0, left 2" in report
        assert "Total:* joined 3, left 3" in report

    async def test_the_long_name_works_and_any_case(self, db_path):
        _world(db_path)
        texts, _ = await run(["-Detailed"], fill={"-100": {"joined": 1, "left": 0}, "-200": {"joined": 0, "left": 0}})
        assert "Real Hub: joined 1, left 0" in texts[-1]

    async def test_a_hub_with_no_sheet_shows_what_this_refresh_itself_found(self, db_path):
        _world(db_path)
        texts, _ = await run(["-d"], sync_result=None)       # no sheet: nothing counted by the sync
        assert "Kids\\_Chat: joined 0, left 1" in texts[-1]     # the kid was found gone by the refresh itself

    async def test_an_incomplete_sync_is_still_marked(self, db_path):
        _world(db_path)
        texts, _ = await run(["-d"], sync_result=False, fill={"-100": {"joined": 1, "left": 0}, "-200": {"joined": 0, "left": 0}})
        assert "will retry" in texts[-1] and "Real Hub: joined 1, left 0" in texts[-1]

    async def test_an_unknown_option_is_refused_and_nothing_is_synced(self, db_path):
        _world(db_path)
        texts, calls = await run(["-x"])
        assert "Unknown option" in texts[-1] and "-d" in texts[-1] and calls == []
