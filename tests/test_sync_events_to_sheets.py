"""
Tests for scripts/sync_events_to_sheets.py - backfills events (and
their EventUsers/Actions rows) that exist in the DB but are missing
from Google Sheets, e.g. from a period where Sheets writes were
silently failing. Only considers events within a recent day-window.
"""
import sys
import sqlite3
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, ".")
import db as db_module
import scripts.sync_events_to_sheets as sync_script


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "test.db")
    monkeypatch.setattr(db_module, "DB_PATH", path)
    db_module.init_db(db_path=path)
    return path


def _recent(days_ago=1):
    return (datetime.now() - timedelta(days=days_ago)).strftime(sync_script.DATE_FMT)


def _insert_pro_hub(db_path, chat_id="-100", sheet_id="sheet123"):
    conn = sqlite3.connect(db_path)
    future = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO all_groups (chat_id, type, sheet_id, subs_date_end) VALUES (?, 'PRO', ?, ?)",
        (chat_id, sheet_id, future),
    )
    conn.commit()
    conn.close()


def _insert_event(db_path, event_id, chat_id="-100", status=0, name="Party",
                   created_date=None, closed_date=None, created_by="42"):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon, "
        "event_status, going_data, notgoing_data, counters_data, kicked_data, "
        "created_date, closed_date, created_by_user_id) "
        "VALUES (?, ?, '1', ?, '👍', '❌', ?, '[]', '[]', '{}', '[]', ?, ?, ?)",
        (event_id, chat_id, name, status, created_date or _recent(), closed_date, created_by),
    )
    conn.commit()
    conn.close()


def _mock_ss(events_records, eventusers_records=None, actions_records=None):
    ws_events = MagicMock()
    ws_events.get_all_records = AsyncMock(return_value=events_records)
    ws_events.append_row = AsyncMock()
    ws_eu = MagicMock()
    ws_eu.get_all_records = AsyncMock(return_value=eventusers_records or [])
    ws_eu.append_rows = AsyncMock()
    ws_actions = MagicMock()
    ws_actions.get_all_records = AsyncMock(return_value=actions_records or [])
    ws_actions.append_rows = AsyncMock()

    ss = MagicMock()
    async def _worksheet(name):
        return {"Events": ws_events, "EventUsers": ws_eu, "Actions": ws_actions}[name]
    ss.worksheet = AsyncMock(side_effect=_worksheet)
    return ss, ws_events, ws_eu, ws_actions


class TestSyncEventsToSheets:
    async def test_missing_open_event_gets_added(self, db_path):
        _insert_pro_hub(db_path)
        _insert_event(db_path, "ev1", status=0)
        ss, ws_events, ws_eu, ws_actions = _mock_ss([])

        with patch("scripts.sync_events_to_sheets.get_sheet_for_chat", new_callable=AsyncMock, return_value="sheet123"), \
             patch("scripts.sync_events_to_sheets.open_spreadsheet", new_callable=AsyncMock, return_value=ss):
            await sync_script._run(db_path=db_path)

        ws_events.append_row.assert_called_once()
        row = ws_events.append_row.call_args.args[0]
        assert row[0] == "ev1"
        assert row[6] == "OPEN"

    async def test_already_present_event_is_never_touched(self, db_path):
        _insert_pro_hub(db_path)
        _insert_event(db_path, "ev1", status=0)
        ss, ws_events, ws_eu, ws_actions = _mock_ss([{"EVENT_ID": "ev1"}], actions_records=[{"EVENT_ID": "ev1"}])

        with patch("scripts.sync_events_to_sheets.get_sheet_for_chat", new_callable=AsyncMock, return_value="sheet123"), \
             patch("scripts.sync_events_to_sheets.open_spreadsheet", new_callable=AsyncMock, return_value=ss):
            await sync_script._run(db_path=db_path)

        ws_events.append_row.assert_not_called()

    async def test_missing_closed_event_backfills_event_users_and_going_count(self, db_path):
        _insert_pro_hub(db_path)
        _insert_event(db_path, "ev2", status=2, closed_date=_recent())
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO event_users (event_id, chat_id, user_id, username, status, guests) "
                     "VALUES ('ev2','-100','1','alice','going',1)")
        conn.execute("INSERT INTO event_users (event_id, chat_id, user_id, username, status, guests) "
                     "VALUES ('ev2','-100','2','bob','going',0)")
        conn.commit()
        conn.close()

        ss, ws_events, ws_eu, ws_actions = _mock_ss([])

        with patch("scripts.sync_events_to_sheets.get_sheet_for_chat", new_callable=AsyncMock, return_value="sheet123"), \
             patch("scripts.sync_events_to_sheets.open_spreadsheet", new_callable=AsyncMock, return_value=ss):
            await sync_script._run(db_path=db_path)

        row = ws_events.append_row.call_args.args[0]
        assert row[6] == "CLOSED"
        assert row[7] == 3, "going_count must be 1(alice going)+1(alice guest)+1(bob going)+0(bob guest) = 3"

        eu_rows = ws_eu.append_rows.call_args.args[0]
        assert sorted(eu_rows) == [["ev2", "1"], ["ev2", "2"]]

    async def test_closed_event_already_in_event_users_is_not_double_backfilled(self, db_path):
        _insert_pro_hub(db_path)
        _insert_event(db_path, "ev2", status=2)
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO event_users (event_id, chat_id, user_id, username, status, guests) "
                     "VALUES ('ev2','-100','1','alice','going',0)")
        conn.commit()
        conn.close()

        ss, ws_events, ws_eu, ws_actions = _mock_ss([], eventusers_records=[{"EVENT_ID": "ev2", "USER_ID": "1"}])

        with patch("scripts.sync_events_to_sheets.get_sheet_for_chat", new_callable=AsyncMock, return_value="sheet123"), \
             patch("scripts.sync_events_to_sheets.open_spreadsheet", new_callable=AsyncMock, return_value=ss):
            await sync_script._run(db_path=db_path)

        ws_eu.append_rows.assert_not_called()

    async def test_skips_non_pro_hubs(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO all_groups (chat_id, type) VALUES ('-200', 'FREE')")
        conn.commit()
        conn.close()
        _insert_event(db_path, "ev1", chat_id="-200")

        get_sheet_mock = AsyncMock(return_value=None)
        with patch("scripts.sync_events_to_sheets.get_sheet_for_chat", get_sheet_mock):
            await sync_script._run(db_path=db_path)  # must not raise

    async def test_target_single_chat_ignores_other_hubs(self, db_path):
        _insert_pro_hub(db_path, chat_id="-100", sheet_id="sheet100")
        _insert_pro_hub(db_path, chat_id="-200", sheet_id="sheet200")
        _insert_event(db_path, "ev1", chat_id="-100")
        _insert_event(db_path, "ev2", chat_id="-200")

        ss, ws_events, ws_eu, ws_actions = _mock_ss([])

        async def fake_get_sheet(chat_id):
            return "sheet100" if chat_id == "-100" else "sheet200"

        with patch("scripts.sync_events_to_sheets.get_sheet_for_chat", side_effect=fake_get_sheet), \
             patch("scripts.sync_events_to_sheets.open_spreadsheet", new_callable=AsyncMock, return_value=ss):
            await sync_script._run(target_chat_id="-100", db_path=db_path)

        assert ws_events.append_row.call_count == 1
        assert ws_events.append_row.call_args.args[0][0] == "ev1"


class TestDaysWindowFilter:
    """Requested: only process events from the last N days (default 21)."""

    async def test_event_within_window_is_processed(self, db_path):
        _insert_pro_hub(db_path)
        _insert_event(db_path, "ev_recent", created_date=_recent(days_ago=10))
        ss, ws_events, ws_eu, ws_actions = _mock_ss([])

        with patch("scripts.sync_events_to_sheets.get_sheet_for_chat", new_callable=AsyncMock, return_value="sheet123"), \
             patch("scripts.sync_events_to_sheets.open_spreadsheet", new_callable=AsyncMock, return_value=ss):
            await sync_script._run(db_path=db_path, days=21)

        ws_events.append_row.assert_called_once()

    async def test_event_outside_default_window_is_ignored(self, db_path):
        _insert_pro_hub(db_path)
        _insert_event(db_path, "ev_old", created_date=_recent(days_ago=40))
        ss, ws_events, ws_eu, ws_actions = _mock_ss([])

        with patch("scripts.sync_events_to_sheets.get_sheet_for_chat", new_callable=AsyncMock, return_value="sheet123"), \
             patch("scripts.sync_events_to_sheets.open_spreadsheet", new_callable=AsyncMock, return_value=ss):
            await sync_script._run(db_path=db_path, days=21)

        ws_events.append_row.assert_not_called()

    async def test_custom_days_value_widens_the_window(self, db_path):
        _insert_pro_hub(db_path)
        _insert_event(db_path, "ev_old", created_date=_recent(days_ago=40))
        ss, ws_events, ws_eu, ws_actions = _mock_ss([])

        with patch("scripts.sync_events_to_sheets.get_sheet_for_chat", new_callable=AsyncMock, return_value="sheet123"), \
             patch("scripts.sync_events_to_sheets.open_spreadsheet", new_callable=AsyncMock, return_value=ss):
            await sync_script._run(db_path=db_path, days=60)

        ws_events.append_row.assert_called_once()

    async def test_event_with_no_created_date_is_excluded_by_default(self, db_path):
        """An event with NULL created_date has no way to know its age -
        treated as outside the window rather than assumed recent."""
        _insert_pro_hub(db_path)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon, "
            "event_status, going_data, notgoing_data, counters_data, kicked_data) "
            "VALUES ('ev_nodate','-100','1','P','👍','❌',0,'[]','[]','{}','[]')"
        )
        conn.commit()
        conn.close()
        ss, ws_events, ws_eu, ws_actions = _mock_ss([])

        with patch("scripts.sync_events_to_sheets.get_sheet_for_chat", new_callable=AsyncMock, return_value="sheet123"), \
             patch("scripts.sync_events_to_sheets.open_spreadsheet", new_callable=AsyncMock, return_value=ss):
            await sync_script._run(db_path=db_path)

        ws_events.append_row.assert_not_called()


class TestActionsBackfill:
    """Requested: also backfill the Actions tab, not just Events."""

    async def test_missing_actions_backfilled_with_final_statuses(self, db_path):
        _insert_pro_hub(db_path)
        _insert_event(db_path, "ev1", status=2, closed_date=_recent())
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO event_users (event_id, chat_id, user_id, username, status, guests) "
                     "VALUES ('ev1','-100','1','alice','going',0)")
        conn.execute("INSERT INTO event_users (event_id, chat_id, user_id, username, status, guests) "
                     "VALUES ('ev1','-100','2','bob','notgoing',0)")
        conn.commit()
        conn.close()

        ss, ws_events, ws_eu, ws_actions = _mock_ss([])

        with patch("scripts.sync_events_to_sheets.get_sheet_for_chat", new_callable=AsyncMock, return_value="sheet123"), \
             patch("scripts.sync_events_to_sheets.open_spreadsheet", new_callable=AsyncMock, return_value=ss):
            await sync_script._run(db_path=db_path)

        rows = ws_actions.append_rows.call_args.args[0]
        actions = {r[1] for r in rows}
        assert "GOING" in actions
        assert "NOTGOING" in actions
        assert "SAVE" in actions, "the event's own close action must also be synthesized"

    async def test_event_with_existing_actions_is_left_alone(self, db_path):
        """If even one genuine Actions row already exists for this
        event, don't mix in synthetic rows alongside it."""
        _insert_pro_hub(db_path)
        _insert_event(db_path, "ev1", status=2)
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO event_users (event_id, chat_id, user_id, username, status, guests) "
                     "VALUES ('ev1','-100','1','alice','going',0)")
        conn.commit()
        conn.close()

        ss, ws_events, ws_eu, ws_actions = _mock_ss([], actions_records=[{"EVENT_ID": "ev1"}])

        with patch("scripts.sync_events_to_sheets.get_sheet_for_chat", new_callable=AsyncMock, return_value="sheet123"), \
             patch("scripts.sync_events_to_sheets.open_spreadsheet", new_callable=AsyncMock, return_value=ss):
            await sync_script._run(db_path=db_path)

        ws_actions.append_rows.assert_not_called()

    async def test_open_event_gets_no_close_action_row(self, db_path):
        _insert_pro_hub(db_path)
        _insert_event(db_path, "ev1", status=0)
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO event_users (event_id, chat_id, user_id, username, status, guests) "
                     "VALUES ('ev1','-100','1','alice','going',0)")
        conn.commit()
        conn.close()

        ss, ws_events, ws_eu, ws_actions = _mock_ss([])

        with patch("scripts.sync_events_to_sheets.get_sheet_for_chat", new_callable=AsyncMock, return_value="sheet123"), \
             patch("scripts.sync_events_to_sheets.open_spreadsheet", new_callable=AsyncMock, return_value=ss):
            await sync_script._run(db_path=db_path)

        rows = ws_actions.append_rows.call_args.args[0]
        actions = {r[1] for r in rows}
        assert "SAVE" not in actions
        assert "CANCEL" not in actions

    async def test_cancelled_event_gets_cancel_action_row(self, db_path):
        _insert_pro_hub(db_path)
        _insert_event(db_path, "ev1", status=-1, closed_date=_recent())
        ss, ws_events, ws_eu, ws_actions = _mock_ss([])

        with patch("scripts.sync_events_to_sheets.get_sheet_for_chat", new_callable=AsyncMock, return_value="sheet123"), \
             patch("scripts.sync_events_to_sheets.open_spreadsheet", new_callable=AsyncMock, return_value=ss):
            await sync_script._run(db_path=db_path)

        rows = ws_actions.append_rows.call_args.args[0]
        actions = {r[1] for r in rows}
        assert "CANCEL" in actions

    async def test_actions_rows_include_chat_id_as_sixth_column(self, db_path):
        _insert_pro_hub(db_path, chat_id="-100")
        _insert_event(db_path, "ev1", chat_id="-100", status=2)
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO event_users (event_id, chat_id, user_id, username, status, guests) "
                     "VALUES ('ev1','-100','1','alice','going',0)")
        conn.commit()
        conn.close()

        ss, ws_events, ws_eu, ws_actions = _mock_ss([])

        with patch("scripts.sync_events_to_sheets.get_sheet_for_chat", new_callable=AsyncMock, return_value="sheet123"), \
             patch("scripts.sync_events_to_sheets.open_spreadsheet", new_callable=AsyncMock, return_value=ss):
            await sync_script._run(db_path=db_path)

        rows = ws_actions.append_rows.call_args.args[0]
        for r in rows:
            assert r[5] == "-100", "6th column must be chat_id, matching the real Actions write format"
