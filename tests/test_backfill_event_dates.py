"""
Tests for scripts/backfill_event_dates.py - the one-time backfill that
copies created_date/closed_date from each PRO hub's Events sheet back
into the DB for events where the DB value is currently NULL.
"""
import sys
import sqlite3
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, ".")
import db as db_module
import scripts.backfill_event_dates as backfill


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "test.db")
    monkeypatch.setattr(db_module, "DB_PATH", path)
    db_module.init_db(db_path=path)
    return path


def _insert_pro_hub(db_path, chat_id="-100", sheet_id="sheet123"):
    conn = sqlite3.connect(db_path)
    future = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO all_groups (chat_id, type, sheet_id, subs_date_end) VALUES (?, 'PRO', ?, ?)",
        (chat_id, sheet_id, future),
    )
    conn.commit()
    conn.close()


def _insert_event(db_path, event_id, chat_id="-100", created_date=None, closed_date=None):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon, "
        "event_status, going_data, notgoing_data, counters_data, kicked_data, created_date, closed_date) "
        "VALUES (?, ?, '1', 'Party', '👍', '❌', 2, '[]', '[]', '{}', '[]', ?, ?)",
        (event_id, chat_id, created_date, closed_date),
    )
    conn.commit()
    conn.close()


def _mock_sheet(records):
    ws = MagicMock()
    ws.get_all_records = AsyncMock(return_value=records)
    ss = MagicMock()
    ss.worksheet = AsyncMock(return_value=ws)
    return ss


class TestBackfillEventDates:
    async def test_fills_null_created_and_closed_date(self, db_path):
        _insert_pro_hub(db_path)
        _insert_event(db_path, "ev1")
        records = [{
            "EVENT_ID": "ev1", "CREATED_DATE": "10.05.2025 14:30:00.000",
            "CLOSED_AT": "12.05.2025 09:00:00.000",
        }]
        with patch("scripts.backfill_event_dates.get_sheet_for_chat", new_callable=AsyncMock, return_value="sheet123"), \
             patch("scripts.backfill_event_dates.open_spreadsheet", new_callable=AsyncMock, return_value=_mock_sheet(records)):
            await backfill._run(db_path=db_path)

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT created_date, closed_date FROM events WHERE event_id='ev1'").fetchone()
        assert row == ("10.05.2025 14:30:00.000", "12.05.2025 09:00:00.000")

    async def test_handles_created_at_header_instead_of_created_date(self, db_path):
        """Real bug fixed: the real Sheet header (row 1, set manually
        when the template was created, never written by this bot's own
        code) may say "CREATED_AT" rather than "CREATED_DATE" -
        handlers.py's own long-standing comment about this column uses
        "CREATED_AT". Must not silently skip backfilling just because
        of this header-name discrepancy."""
        _insert_pro_hub(db_path)
        _insert_event(db_path, "ev1")
        records = [{
            "EVENT_ID": "ev1", "CREATED_AT": "10.05.2025 14:30:00.000",
            "CLOSED_AT": "12.05.2025 09:00:00.000",
        }]
        with patch("scripts.backfill_event_dates.get_sheet_for_chat", new_callable=AsyncMock, return_value="sheet123"), \
             patch("scripts.backfill_event_dates.open_spreadsheet", new_callable=AsyncMock, return_value=_mock_sheet(records)):
            await backfill._run(db_path=db_path)

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT created_date, closed_date FROM events WHERE event_id='ev1'").fetchone()
        assert row == ("10.05.2025 14:30:00.000", "12.05.2025 09:00:00.000")

    async def test_never_overwrites_an_existing_value(self, db_path):
        _insert_pro_hub(db_path)
        _insert_event(db_path, "ev1", created_date="01.01.2026 00:00:00.000")
        records = [{
            "EVENT_ID": "ev1", "CREATED_DATE": "99.99.9999 00:00:00.000",
            "CLOSED_AT": "05.09.2026 10:00:00.000",
        }]
        with patch("scripts.backfill_event_dates.get_sheet_for_chat", new_callable=AsyncMock, return_value="sheet123"), \
             patch("scripts.backfill_event_dates.open_spreadsheet", new_callable=AsyncMock, return_value=_mock_sheet(records)):
            await backfill._run(db_path=db_path)

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT created_date, closed_date FROM events WHERE event_id='ev1'").fetchone()
        assert row[0] == "01.01.2026 00:00:00.000", "existing created_date must not be touched"
        assert row[1] == "05.09.2026 10:00:00.000", "NULL closed_date should still get backfilled"

    async def test_skips_non_pro_hubs(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO all_groups (chat_id, type) VALUES ('-200', 'FREE')")
        conn.commit()
        conn.close()
        _insert_event(db_path, "ev1", chat_id="-200")

        get_sheet_mock = AsyncMock(return_value=None)
        with patch("scripts.backfill_event_dates.get_sheet_for_chat", get_sheet_mock):
            await backfill._run(db_path=db_path)

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT created_date FROM events WHERE event_id='ev1'").fetchone()
        assert row == (None,), "a free hub's event must stay untouched"

    async def test_sheet_row_with_no_matching_db_row_is_skipped(self, db_path):
        """A Sheet row for an event that's since been deleted from the
        DB (or never existed there) must not raise or create anything."""
        _insert_pro_hub(db_path)
        records = [{"EVENT_ID": "ghost_event", "CREATED_DATE": "10.05.2025 14:30:00.000", "CLOSED_AT": ""}]
        with patch("scripts.backfill_event_dates.get_sheet_for_chat", new_callable=AsyncMock, return_value="sheet123"), \
             patch("scripts.backfill_event_dates.open_spreadsheet", new_callable=AsyncMock, return_value=_mock_sheet(records)):
            await backfill._run(db_path=db_path)  # must not raise

        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        assert count == 0

    async def test_empty_sheet_value_does_not_backfill(self, db_path):
        """An empty CLOSED_AT cell in the Sheet (event never closed)
        must not overwrite NULL with an empty string."""
        _insert_pro_hub(db_path)
        _insert_event(db_path, "ev1")
        records = [{"EVENT_ID": "ev1", "CREATED_DATE": "10.05.2025 14:30:00.000", "CLOSED_AT": ""}]
        with patch("scripts.backfill_event_dates.get_sheet_for_chat", new_callable=AsyncMock, return_value="sheet123"), \
             patch("scripts.backfill_event_dates.open_spreadsheet", new_callable=AsyncMock, return_value=_mock_sheet(records)):
            await backfill._run(db_path=db_path)

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT created_date, closed_date FROM events WHERE event_id='ev1'").fetchone()
        assert row == ("10.05.2025 14:30:00.000", None)

    async def test_target_single_chat_id_ignores_other_hubs(self, db_path):
        _insert_pro_hub(db_path, chat_id="-100", sheet_id="sheet123")
        _insert_pro_hub(db_path, chat_id="-200", sheet_id="sheet456")
        _insert_event(db_path, "ev1", chat_id="-100")
        _insert_event(db_path, "ev2", chat_id="-200")

        records_100 = [{"EVENT_ID": "ev1", "CREATED_DATE": "10.05.2025 00:00:00.000", "CLOSED_AT": ""}]

        async def fake_get_sheet(chat_id):
            return "sheet123" if chat_id == "-100" else "sheet456"

        with patch("scripts.backfill_event_dates.get_sheet_for_chat", side_effect=fake_get_sheet), \
             patch("scripts.backfill_event_dates.open_spreadsheet", new_callable=AsyncMock, return_value=_mock_sheet(records_100)):
            await backfill._run(target_chat_id="-100", db_path=db_path)

        conn = sqlite3.connect(db_path)
        ev1 = conn.execute("SELECT created_date FROM events WHERE event_id='ev1'").fetchone()
        ev2 = conn.execute("SELECT created_date FROM events WHERE event_id='ev2'").fetchone()
        assert ev1 == ("10.05.2025 00:00:00.000",)
        assert ev2 == (None,), "the untargeted hub must be left completely untouched"
