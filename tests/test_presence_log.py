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


async def _sync(ss, members, chat_id="-100", owner=None, sheet_of=None):
    sheet_of = sheet_of or (lambda cid: "sheet")
    with patch("sheets.get_sheet_for_chat", new_callable=AsyncMock, side_effect=lambda cid: sheet_of(cid)), \
         patch("sheets.open_spreadsheet", new_callable=AsyncMock, side_effect=lambda sid: ss if sid else None):
        return await sheets.sync_users_sheet(chat_id, members, sheet_owner_chat_id=owner)


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

    async def test_a_changed_first_name_writes_only_that_cell(self):
        users = _Ws([_row()])
        await _sync(_Ss(users, _Ws()), [("1", "u1", "New", "B")])
        assert users.updates == [("B2", [["New"]])]

    async def test_a_name_missing_from_the_sheet_is_written(self):
        users = _Ws([{**_row(), "FIRST_NAME": "", "LAST_NAME": ""}])
        await _sync(_Ss(users, _Ws()), [("1", "u1", "A", "B")])
        assert users.updates == [("B2", [["A"]]), ("C2", [["B"]])]

    async def test_thirty_unchanged_members_cost_zero_writes(self):
        users = _Ws([_row(uid=str(i)) for i in range(1, 31)])
        await _sync(_Ss(users, _Ws()), [(str(i), f"u{i}", "A", "B") for i in range(1, 31)])
        assert users.updates == []
