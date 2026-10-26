"""
UserPresenceLog: one row per stay that has ended - ALWAYS, whichever way the
person's STATUS turned to LEFT (hub or monitored chat, first attempt or retry,
or a stay a failed write left unlogged before).
"""
import sqlite3
import sys
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from gspread.exceptions import WorksheetNotFound

sys.path.insert(0, ".")
import handlers
import sheets
from tests.helpers import make_chat, make_user, make_message, make_update, make_context, make_bot


class _Ws:
    def __init__(self, records=None):
        self.records = list(records or [])
        self.updates, self.appended = [], []
        self.append_calls = self.reads = 0
        self.fail_append = None

    async def get_all_records(self):
        self.reads += 1
        return list(self.records)

    async def update(self, cell_range, values):
        self.updates.append((cell_range, values))

    async def append_row(self, row):
        self.appended.append(row)

    async def append_rows(self, rows):
        self.append_calls += 1
        if self.fail_append:
            raise self.fail_append
        self.appended.extend(rows)


class _Ss:
    def __init__(self, users, log=None):
        self.users, self.log = users, log

    async def worksheet(self, name):
        if name == "Users":
            return self.users
        if name == "UserPresenceLog":
            if self.log is None:
                raise WorksheetNotFound(name)
            return self.log
        raise AssertionError(name)


def _row(uid="1", chat="-100", status="MEMBER", start="01.01.2026", end=""):
    return {"USER_ID": uid, "FIRST_NAME": "A", "LAST_NAME": "B", "USER_NAME": f"u{uid}", "CHAT_ID": chat,
            "STATUS": status, "DATE_start": start, "DATE_end": end, "ARCHIVED_USER_NAME": ""}


def _log_row(uid="1", chat="-100", start="01.01.2026", end="02.01.2026"):
    return {"USER_ID": uid, "CHAT_ID": chat, "DATE_start": start, "DATE_end": end}


async def _sync(ss, members, chat_id="-100", owner=None, sheet_of=None, left=None):
    sheet_of = sheet_of or (lambda cid: "sheet")
    with patch("sheets.get_sheet_for_chat", new_callable=AsyncMock, side_effect=lambda cid: sheet_of(cid)), \
         patch("sheets.open_spreadsheet", new_callable=AsyncMock, side_effect=lambda sid: ss if sid else None):
        return await sheets.sync_users_sheet(chat_id, members, sheet_owner_chat_id=owner, left_members=left)


class TestPresenceLogOnLeave:
    async def test_leaving_member_gets_a_log_row_with_the_same_timestamp_as_the_users_tab(self):
        users, log = _Ws([_row()]), _Ws()
        assert await _sync(_Ss(users, log), []) is True
        assert len(log.appended) == 1
        uid, chat, start, end = log.appended[0]
        assert (uid, chat, start) == ("1", "-100", "01.01.2026") and end
        assert ("F2", [["LEFT"]]) in users.updates
        assert ("H2", [[end]]) in users.updates

    async def test_monitored_chat_is_logged_into_the_hubs_sheet(self):
        """The reported bug: /refreshusersall syncs a monitored chat with
        sheet_owner_chat_id=<hub>. The log used to look the sheet up under the
        CHILD's own id (no sheet of its own) and silently write nothing."""
        users, log = _Ws([_row(chat="-200")]), _Ws()
        hub_only = lambda cid: "hub_sheet" if cid == "-100" else None
        assert await _sync(_Ss(users, log), [], chat_id="-200", owner="-100", sheet_of=hub_only) is True
        assert [r[:2] for r in log.appended] == [["1", "-200"]]
        assert ("F2", [["LEFT"]]) in users.updates

    async def test_left_row_missing_from_the_log_is_backfilled(self):
        users, log = _Ws([_row(status="LEFT", end="05.01.2026")]), _Ws()
        await _sync(_Ss(users, log), [])
        assert log.appended == [["1", "-100", "01.01.2026", "05.01.2026"]]
        assert users.updates == [], "an already-LEFT row needs no status change"

    async def test_left_row_already_logged_is_not_duplicated(self):
        users, log = _Ws([_row(status="LEFT", end="02.01.2026")]), _Ws([_log_row()])
        await _sync(_Ss(users, log), [])
        assert log.appended == [] and log.append_calls == 0

    async def test_stay_already_in_the_log_is_not_written_twice_but_status_still_flips(self):
        users, log = _Ws([_row()]), _Ws([_log_row(end="03.01.2026")])
        await _sync(_Ss(users, log), [])
        assert log.appended == []
        assert ("F2", [["LEFT"]]) in users.updates

    async def test_failed_log_write_flips_nobody_and_the_retry_succeeds(self):
        users, log = _Ws([_row()]), _Ws()
        log.fail_append = RuntimeError("quota exceeded")
        assert await _sync(_Ss(users, log), []) is False
        assert not any(cell in ("F2", "H2") for cell, _ in users.updates), "still MEMBER, so it is retried"

        log.fail_append = None
        assert await _sync(_Ss(users, log), []) is True
        assert len(log.appended) == 1 and ("F2", [["LEFT"]]) in users.updates

    async def test_spreadsheet_without_the_tab_still_flips_statuses(self):
        users = _Ws([_row()])
        assert await _sync(_Ss(users, None), []) is True
        assert ("F2", [["LEFT"]]) in users.updates

    async def test_rejoin_logs_the_previous_stay_before_it_is_overwritten(self):
        users, log = _Ws([_row(status="LEFT", end="05.01.2026")]), _Ws()
        await _sync(_Ss(users, log), [("1", "u1")])
        assert log.appended == [["1", "-100", "01.01.2026", "05.01.2026"]]
        assert ("F2", [["MEMBER"]]) in users.updates

    async def test_stays_without_a_start_date_are_told_apart_by_their_end_date(self):
        users = _Ws([_row(status="LEFT", start="", end="02.01.2026")])
        log = _Ws([_log_row(start="", end="01.01.2026")])
        await _sync(_Ss(users, log), [])
        assert log.appended == [["1", "-100", "", "02.01.2026"]]

        same_end = _Ws([_log_row(start="", end="02.01.2026")])
        await _sync(_Ss(users, same_end), [])
        assert same_end.appended == []

    async def test_many_leavers_cost_one_read_and_one_append(self):
        users, log = _Ws([_row(uid=str(i)) for i in range(1, 4)]), _Ws()
        await _sync(_Ss(users, log), [])
        assert (log.reads, log.append_calls, len(log.appended)) == (1, 1, 3)

    async def test_other_chats_rows_are_left_alone(self):
        users, log = _Ws([_row(chat="-999")]), _Ws()
        await _sync(_Ss(users, log), [])
        assert log.appended == [] and users.updates == []

    async def test_hub_without_a_sheet_does_nothing_and_returns_none(self):
        users = _Ws([_row()])
        assert await _sync(_Ss(users, _Ws()), [], sheet_of=lambda cid: None) is None
        assert users.updates == []

    async def test_present_members_log_nothing(self):
        users, log = _Ws([_row()]), _Ws()
        assert await _sync(_Ss(users, log), [("1", "u1")]) is True
        assert log.appended == [] and log.reads == 0

    async def test_users_tab_failure_is_reported_not_raised(self):
        class Boom(_Ws):
            async def get_all_records(self):
                raise RuntimeError("boom")
        assert await _sync(_Ss(Boom(), _Ws()), []) is False

    async def test_numeric_cells_from_google_sheets_are_matched(self):
        users = _Ws([{**_row(), "USER_ID": 1, "CHAT_ID": -100}])
        log = _Ws([{"USER_ID": 1, "CHAT_ID": -100, "DATE_start": "01.01.2026", "DATE_end": "02.01.2026"}])
        await _sync(_Ss(users, log), [])
        assert log.appended == []
        assert ("F2", [["LEFT"]]) in users.updates


def _bot():
    bot = make_bot()
    bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator"))
    bot.get_chat = AsyncMock(return_value=MagicMock(title="Hub"))
    bot.get_chat_administrators = AsyncMock(return_value=[])
    return bot


class TestRefreshCommandsShowAnIncompleteSync:
    """A swallowed sync failure used to look exactly like success."""

    async def _refreshusers(self, synced):
        chat = make_chat(chat_id=-100123)
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, message=msg)
        with patch("handlers.sync_users_sheet", new_callable=AsyncMock, return_value=synced):
            await handlers.refreshusers(upd, make_context(bot=_bot(), args=[]))
        return msg.reply_text.call_args.args[0]

    async def test_refreshusers_warns_when_the_sync_was_incomplete(self, db_path):
        assert "incomplete" in await self._refreshusers(False)

    async def test_refreshusers_says_synced_when_it_was(self, db_path):
        text = await self._refreshusers(True)
        assert "synced" in text and "incomplete" not in text

    async def _refreshusersall(self, db_path, synced):
        end = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO all_groups (chat_id, type, subs_date_start, subs_date_end) VALUES ('-100123','PRO',?,?)", (end, end))
        conn.execute("INSERT INTO sub_chats (chat_id, owner_chat_id, chat_type, chat_name, is_monitored) "
                     "VALUES ('-200', '-100123', 'group', 'Monitored Group', 1)")
        conn.commit()
        conn.close()
        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        msg = make_message(chat=chat)
        upd = make_update(chat=chat, user=make_user(user_id=1), message=msg)
        ctx = make_context(bot=_bot(), args=[])
        with patch("handlers.sync_users_sheet", new_callable=AsyncMock, return_value=synced):
            await handlers.refreshusersall(upd, ctx)
        final = msg.reply_text.call_args_list[-1]
        text = final.args[0] if final.args else final.kwargs.get("text", "")
        if not text and ctx.bot.edit_message_text.called:
            text = ctx.bot.edit_message_text.call_args.kwargs.get("text", "")
        return text

    async def test_refreshusersall_marks_an_incomplete_chat(self, db_path):
        text = await self._refreshusersall(db_path, False)
        assert "Partly synced" in text and "Monitored Group" in text

    async def test_refreshusersall_marks_a_complete_chat_as_synced(self, db_path):
        text = await self._refreshusersall(db_path, True)
        assert "Synced" in text and "Partly" not in text


class TestUsersTabWritesOnlyWhatChanged:
    """Names used to be rewritten for every present member on every refresh (2 throttled API
    calls per person) even when the sheet already held exactly those values."""

    async def test_unchanged_names_cause_no_writes(self):
        users = _Ws([_row()])
        assert await _sync(_Ss(users, _Ws()), [("1", "u1", "A", "B")]) is True
        assert users.updates == []

    async def test_a_changed_first_name_writes_that_cell_and_keeps_the_old_name(self):
        """Nothing else is touched: the new name, and the earlier full name archived (header added once, cell J2)."""
        users = _Ws([_row()])
        await _sync(_Ss(users, _Ws()), [("1", "u1", "New", "B")])
        assert users.updates == [("B2", [["New"]]), ("J1", [["ARCHIVED_FULL_NAME"]]), ("J2", [["A B"]])]

    async def test_a_name_missing_from_the_sheet_is_written(self):
        users = _Ws([{**_row(), "FIRST_NAME": "", "LAST_NAME": ""}])
        await _sync(_Ss(users, _Ws()), [("1", "u1", "A", "B")])
        assert users.updates == [("B2", [["A"]]), ("C2", [["B"]])]

    async def test_thirty_unchanged_members_cost_zero_writes(self):
        users = _Ws([_row(uid=str(i)) for i in range(1, 31)])
        await _sync(_Ss(users, _Ws()), [(str(i), f"u{i}", "A", "B") for i in range(1, 31)])
        assert users.updates == []


import db as db_module

LEFT_AT = "05.10.2026 12:00:00.000"
START = "05.10.2026 11:00:00.000"


def _ghost(uid="9", left_at=LEFT_AT):
    return (uid, "ghost", "G", "H", left_at)


class TestPeopleTheSheetNeverHadAreRecordedWhenTheyLeave:
    """Someone with no Users row has no MEMBER -> LEFT transition to notice. A refresh that confirmed
    they're gone must still leave a LEFT row and a UserPresenceLog row behind."""

    async def test_a_leaver_without_a_row_gets_a_left_row_and_a_log_row(self):
        users, log = _Ws(), _Ws()
        assert await _sync(_Ss(users, log), [], left=[_ghost()]) is True
        assert users.appended == [["9", "G", "H", "ghost", "-100", "LEFT", START, LEFT_AT, ""]]
        assert log.appended == [["9", "-100", START, LEFT_AT]]

    async def test_join_time_is_one_hour_before_leaving_even_across_midnight(self):
        users, log = _Ws(), _Ws()
        await _sync(_Ss(users, log), [], left=[_ghost(left_at="05.10.2026 00:30:00.000")])
        assert users.appended[0][6:8] == ["04.10.2026 23:30:00.000", "05.10.2026 00:30:00.000"]

    async def test_monitored_chat_row_and_log_go_to_the_hubs_sheet(self):
        users, log = _Ws(), _Ws()
        hub_only = lambda cid: "hub_sheet" if cid == "-100" else None
        assert await _sync(_Ss(users, log), [], chat_id="-200", owner="-100", sheet_of=hub_only,
                           left=[_ghost()]) is True
        assert users.appended[0][4] == "-200" and log.appended[0][:2] == ["9", "-200"]

    async def test_someone_who_is_actually_present_is_ignored(self):
        users, log = _Ws(), _Ws()
        await _sync(_Ss(users, log), [("9", "ghost")], left=[_ghost()])
        assert log.appended == []
        assert all(row[5] != "LEFT" for row in users.appended)

    async def test_someone_with_a_member_row_goes_through_the_normal_path_once(self):
        users, log = _Ws([_row(uid="9")]), _Ws()
        await _sync(_Ss(users, log), [], left=[_ghost()])
        assert users.appended == [], "no second row for a person who already has one"
        assert log.appended == [["9", "-100", "01.01.2026", log.appended[0][3]]], "real join date, logged once"
        assert ("F2", [["LEFT"]]) in users.updates

    async def test_someone_who_already_has_a_left_row_is_not_added_again(self):
        users, log = _Ws([_row(uid="9", status="LEFT", end="02.01.2026")]), _Ws([_log_row(uid="9")])
        await _sync(_Ss(users, log), [], left=[_ghost()])
        assert users.appended == [] and log.appended == []

    async def test_failed_log_write_creates_no_row_and_the_retry_is_identical(self):
        users, log = _Ws(), _Ws()
        log.fail_append = RuntimeError("quota")
        assert await _sync(_Ss(users, log), [], left=[_ghost()]) is False
        assert users.appended == []
        log.fail_append = None
        assert await _sync(_Ss(users, log), [], left=[_ghost()]) is True
        assert log.appended == [["9", "-100", START, LEFT_AT]] and len(users.appended) == 1

    async def test_failed_row_write_after_a_good_log_write_does_not_duplicate_the_log_on_retry(self):
        users, log = _Ws(), _Ws()
        users.fail_append = RuntimeError("boom")
        assert await _sync(_Ss(users, log), [], left=[_ghost()]) is False
        assert log.appended == [["9", "-100", START, LEFT_AT]]

        users.fail_append = None
        log.records = [_log_row(uid="9", start=START, end=LEFT_AT)]      # what the sheet now holds
        assert await _sync(_Ss(users, log), [], left=[_ghost()]) is True
        assert len(log.appended) == 1, "same dates on the retry -> recognised, not written twice"
        assert len(users.appended) == 1

    async def test_unreadable_leave_time_is_kept_and_the_start_left_blank(self):
        users, log = _Ws(), _Ws()
        await _sync(_Ss(users, log), [], left=[_ghost(left_at="garbage")])
        assert users.appended[0][6:8] == ["", "garbage"] and log.appended == [["9", "-100", "", "garbage"]]

    async def test_missing_leave_time_falls_back_to_now(self):
        users, log = _Ws(), _Ws()
        await _sync(_Ss(users, log), [], left=[("9", "ghost", "G", "H", None)])
        start, end = users.appended[0][6:8]
        assert start and end and start != end

    async def test_missing_names_become_empty_cells(self):
        users, log = _Ws(), _Ws()
        await _sync(_Ss(users, log), [], left=[("9", None, None, None, LEFT_AT)])
        assert users.appended[0][:5] == ["9", "", "", "", "-100"]

    async def test_several_leavers_cost_one_log_append_and_one_row_append(self):
        users, log = _Ws(), _Ws()
        await _sync(_Ss(users, log), [], left=[_ghost("9"), _ghost("10"), _ghost("11")])
        assert (log.append_calls, users.append_calls) == (1, 1)
        assert len(log.appended) == len(users.appended) == 3


class TestLeftPendingQueue:
    def test_queue_read_and_clear(self, db_path):
        db_module.queue_left_members("-100", [("9", "ghost", "G", "H", LEFT_AT)])
        assert db_module.get_pending_left("-100") == [("9", "ghost", "G", "H", LEFT_AT)]
        db_module.clear_pending_left("-100", ["9"])
        assert db_module.get_pending_left("-100") == []

    def test_the_first_detection_time_is_kept(self, db_path):
        db_module.queue_left_members("-100", [("9", "ghost", "G", "H", LEFT_AT)])
        db_module.queue_left_members("-100", [("9", "ghost", "G", "H", "06.10.2026 09:00:00.000")])
        assert db_module.get_pending_left("-100")[0][4] == LEFT_AT

    def test_queues_are_per_chat(self, db_path):
        db_module.queue_left_members("-100", [("9", "a", None, None, LEFT_AT)])
        db_module.queue_left_members("-200", [("9", "a", None, None, LEFT_AT)])
        db_module.clear_pending_left("-100", ["9"])
        assert db_module.get_pending_left("-100") == [] and len(db_module.get_pending_left("-200")) == 1


def _track(db_path, chat_id, username, user_id, status="passive"):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO main_group_users (chat_id, username, user_id, status) VALUES (?, ?, ?, ?)",
                 (chat_id, username, user_id, status))
    conn.commit()
    conn.close()


def _bot_where_777_left():
    bot = _bot()

    def member(chat_id, user_id):
        if int(user_id) == 777:
            return MagicMock(status="left", user=MagicMock(username="ghost", first_name="G", last_name="H"))
        return MagicMock(status="administrator")

    bot.get_chat_member = AsyncMock(side_effect=member)
    return bot


class TestRefreshCommandsRecordPeopleWhoLeft:
    async def _refreshusers(self, db_path, synced):
        _track(db_path, "-100123", "ghost", "777")
        chat = make_chat(chat_id=-100123)
        msg = make_message(chat=chat)
        sync = AsyncMock(return_value=synced)
        with patch("handlers.sync_users_sheet", sync):
            await handlers.refreshusers(make_update(chat=chat, message=msg), make_context(bot=_bot_where_777_left(), args=[]))
        return sync

    async def test_refreshusers_hands_the_confirmed_leaver_to_the_sheet_sync(self, db_path):
        sync = await self._refreshusers(db_path, True)
        left = sync.call_args.kwargs["left_members"]
        assert [l[:4] for l in left] == [("777", "ghost", "G", "H")] and left[0][4]

    async def test_a_good_sync_clears_the_queue(self, db_path):
        await self._refreshusers(db_path, True)
        assert db_module.get_pending_left("-100123") == []

    async def test_a_failed_sync_keeps_them_queued_for_the_next_refresh(self, db_path):
        await self._refreshusers(db_path, False)
        assert [p[0] for p in db_module.get_pending_left("-100123")] == ["777"]

    async def test_a_leaver_from_an_earlier_failed_refresh_is_retried(self, db_path):
        await self._refreshusers(db_path, False)                      # nothing is tracked any more after this
        chat = make_chat(chat_id=-100123)
        sync = AsyncMock(return_value=True)
        with patch("handlers.sync_users_sheet", sync):
            await handlers.refreshusers(make_update(chat=chat, message=make_message(chat=chat)),
                                        make_context(bot=_bot_where_777_left(), args=[]))
        assert [l[0] for l in sync.call_args.kwargs["left_members"]] == ["777"]
        assert db_module.get_pending_left("-100123") == []

    async def test_refreshusersall_does_the_same_for_a_monitored_chat(self, db_path):
        end = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO all_groups (chat_id, type, subs_date_start, subs_date_end) VALUES ('-100123','PRO',?,?)", (end, end))
        conn.execute("INSERT INTO sub_chats (chat_id, owner_chat_id, chat_type, chat_name, is_monitored) "
                     "VALUES ('-200', '-100123', 'group', 'Monitored Group', 1)")
        conn.commit()
        conn.close()
        _track(db_path, "-200", "ghost", "777")

        chat = make_chat(chat_id=-100123, chat_type="supergroup")
        sync = AsyncMock(return_value=True)
        with patch("handlers.sync_users_sheet", sync):
            await handlers.refreshusersall(make_update(chat=chat, user=make_user(user_id=1), message=make_message(chat=chat)),
                                           make_context(bot=_bot_where_777_left(), args=[]))
        by_chat = {c.args[0]: c.kwargs for c in sync.call_args_list}
        assert [l[0] for l in by_chat["-200"]["left_members"]] == ["777"]
        assert by_chat["-200"]["sheet_owner_chat_id"] == "-100123"
        assert by_chat["-100123"]["left_members"] == []
        assert db_module.get_pending_left("-200") == []
