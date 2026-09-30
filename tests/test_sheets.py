"""
Tests for sheets.py - previously had zero dedicated test coverage at all
(every handlers.py-level test just mocks sync_users_sheet away entirely,
never exercising its real body).

Covers the Users sheet's column order:
USER_ID, FIRST_NAME, LAST_NAME, USER_NAME, CHAT_ID, STATUS, DATE_start,
DATE_end, ARCHIVED_USER_NAME - reordered from the previous
USER_ID, USER_NAME, CHAT_ID, STATUS, DATE_start, DATE_end,
ARCHIVED_USER_NAME, FIRST_NAME, LAST_NAME.
"""
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import sheets


class TestSyncUsersSheetColumnOrder:
    async def test_new_user_append_row_column_order(self):
        ws = MagicMock()
        ws.get_all_records = AsyncMock(return_value=[])
        ws.append_row = AsyncMock()
        ws.update = AsyncMock()
        ss = MagicMock()
        ss.worksheet = AsyncMock(return_value=ws)

        with patch("sheets.get_sheet_for_chat", new_callable=AsyncMock, return_value="fake_id"), \
             patch("sheets.open_spreadsheet", new_callable=AsyncMock, return_value=ss):
            await sheets.sync_users_sheet("-100", [("2", "newuser", "New", "User")])

        row = ws.append_row.call_args.args[0]
        # USER_ID, FIRST_NAME, LAST_NAME, USER_NAME, CHAT_ID, STATUS, DATE_start, DATE_end, ARCHIVED_USER_NAME
        assert row[0] == "2"
        assert row[1] == "New"
        assert row[2] == "User"
        assert row[3] == "newuser"
        assert row[4] == "-100"
        assert row[5] == "MEMBER"
        assert row[6]  # DATE_start populated
        assert row[7] == ""  # DATE_end blank
        assert row[8] == ""  # ARCHIVED_USER_NAME blank

    async def test_2tuple_member_still_appends_blank_names(self):
        """Backward-compat: the old (user_id, username) 2-tuple form
        must still work, just with blank FIRST_NAME/LAST_NAME."""
        ws = MagicMock()
        ws.get_all_records = AsyncMock(return_value=[])
        ws.append_row = AsyncMock()
        ss = MagicMock()
        ss.worksheet = AsyncMock(return_value=ws)

        with patch("sheets.get_sheet_for_chat", new_callable=AsyncMock, return_value="fake_id"), \
             patch("sheets.open_spreadsheet", new_callable=AsyncMock, return_value=ss):
            await sheets.sync_users_sheet("-100", [("2", "newuser")])

        row = ws.append_row.call_args.args[0]
        assert row == ["2", "", "", "newuser", "-100", "MEMBER", row[6], "", ""]

    async def test_username_change_updates_correct_cells(self):
        ws = MagicMock()
        ws.get_all_records = AsyncMock(return_value=[
            {"USER_ID": "1", "FIRST_NAME": "Old", "LAST_NAME": "Name", "USER_NAME": "olduser",
             "CHAT_ID": "-100", "STATUS": "MEMBER", "DATE_start": "01.01.2026", "DATE_end": "",
             "ARCHIVED_USER_NAME": ""},
        ])
        ws.update = AsyncMock()
        ss = MagicMock()
        ss.worksheet = AsyncMock(return_value=ws)

        with patch("sheets.get_sheet_for_chat", new_callable=AsyncMock, return_value="fake_id"), \
             patch("sheets.open_spreadsheet", new_callable=AsyncMock, return_value=ss):
            await sheets.sync_users_sheet("-100", [("1", "newusername", "Updated", "Name")])

        calls = {c.args[0]: c.args[1][0][0] for c in ws.update.call_args_list}
        assert calls.get("D2") == "newusername"       # USER_NAME
        assert calls.get("I2") == "olduser"            # ARCHIVED_USER_NAME
        assert calls.get("B2") == "Updated"            # FIRST_NAME
        assert calls.get("C2") == "Name"               # LAST_NAME

    async def test_left_to_member_transition_updates_correct_cells(self):
        ws = MagicMock()
        ws.get_all_records = AsyncMock(return_value=[
            {"USER_ID": "1", "FIRST_NAME": "A", "LAST_NAME": "B", "USER_NAME": "u1",
             "CHAT_ID": "-100", "STATUS": "LEFT", "DATE_start": "01.01.2026", "DATE_end": "05.01.2026",
             "ARCHIVED_USER_NAME": ""},
        ])
        ws.update = AsyncMock()
        ss = MagicMock()
        ss.worksheet = AsyncMock(return_value=ws)

        with patch("sheets.get_sheet_for_chat", new_callable=AsyncMock, return_value="fake_id"), \
             patch("sheets.open_spreadsheet", new_callable=AsyncMock, return_value=ss):
            await sheets.sync_users_sheet("-100", [("1", "u1")])

        calls = {c.args[0]: c.args[1][0][0] for c in ws.update.call_args_list}
        assert calls.get("F2") == "MEMBER"    # STATUS
        assert "G2" in calls                  # DATE_start refreshed
        assert calls.get("H2") == ""          # DATE_end cleared

    async def test_member_to_left_transition_updates_correct_cells(self):
        ws = MagicMock()
        ws.get_all_records = AsyncMock(return_value=[
            {"USER_ID": "1", "FIRST_NAME": "A", "LAST_NAME": "B", "USER_NAME": "u1",
             "CHAT_ID": "-100", "STATUS": "MEMBER", "DATE_start": "01.01.2026", "DATE_end": "",
             "ARCHIVED_USER_NAME": ""},
        ])
        ws.update = AsyncMock()
        ss = MagicMock()
        ss.worksheet = AsyncMock(return_value=ws)

        with patch("sheets.get_sheet_for_chat", new_callable=AsyncMock, return_value="fake_id"), \
             patch("sheets.open_spreadsheet", new_callable=AsyncMock, return_value=ss), \
             patch("sheets.log_user_presence_if_not_exists", new_callable=AsyncMock):
            await sheets.sync_users_sheet("-100", [])  # nobody currently there -> this user "left"

        calls = {c.args[0]: c.args[1][0][0] for c in ws.update.call_args_list}
        assert calls.get("F2") == "LEFT"      # STATUS
        assert "H2" in calls                  # DATE_end set

    async def test_already_member_status_unchanged_no_status_update(self):
        """Same status, same name - no STATUS/name cell writes should
        happen at all, only a no-op pass-through."""
        ws = MagicMock()
        ws.get_all_records = AsyncMock(return_value=[
            {"USER_ID": "1", "FIRST_NAME": "A", "LAST_NAME": "B", "USER_NAME": "u1",
             "CHAT_ID": "-100", "STATUS": "MEMBER", "DATE_start": "01.01.2026", "DATE_end": "",
             "ARCHIVED_USER_NAME": ""},
        ])
        ws.update = AsyncMock()
        ss = MagicMock()
        ss.worksheet = AsyncMock(return_value=ws)

        with patch("sheets.get_sheet_for_chat", new_callable=AsyncMock, return_value="fake_id"), \
             patch("sheets.open_spreadsheet", new_callable=AsyncMock, return_value=ss):
            await sheets.sync_users_sheet("-100", [("1", "u1")])

        calls = {c.args[0] for c in ws.update.call_args_list}
        assert "F2" not in calls  # STATUS untouched - already MEMBER
        assert "D2" not in calls  # USER_NAME untouched - unchanged


class TestGetSheetForChat:
    """Direct unit tests for get_sheet_for_chat() - previously had ZERO
    direct coverage at all, only ever exercised indirectly through
    handlers.py-level tests that mock it away entirely. Also
    previously untestable this way at all: the function connected via
    a hardcoded "database.db" path instead of db.DB_PATH, ignoring the
    project's standard isolated tmp_path fixture - fixed alongside
    adding this coverage."""

    async def test_unregistered_chat_returns_none(self, db_path):
        result = await sheets.get_sheet_for_chat("-999")
        assert result is None

    async def test_free_tier_returns_none(self, db_path):
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO all_groups (chat_id, type) VALUES ('-100', 'FREE')")
        conn.commit()
        conn.close()

        result = await sheets.get_sheet_for_chat("-100")
        assert result is None

    async def test_pro_tier_no_sheet_id_returns_none(self, db_path):
        import sqlite3
        from datetime import datetime, timedelta
        conn = sqlite3.connect(db_path)
        future = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO all_groups (chat_id, type, subs_date_end) VALUES ('-100', 'PRO', ?)",
            (future,),
        )
        conn.commit()
        conn.close()

        result = await sheets.get_sheet_for_chat("-100")
        assert result is None

    async def test_pro_tier_with_sheet_id_and_active_subscription_returns_it(self, db_path):
        import sqlite3
        from datetime import datetime, timedelta
        conn = sqlite3.connect(db_path)
        future = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO all_groups (chat_id, type, sheet_id, subs_date_end) VALUES ('-100', 'PRO', 'sheet123', ?)",
            (future,),
        )
        conn.commit()
        conn.close()

        result = await sheets.get_sheet_for_chat("-100")
        assert result == "sheet123"

    async def test_pro_tier_with_expired_subscription_returns_none(self, db_path):
        """Even with a sheet_id configured, an expired PRO subscription
        must be treated as free - no more Sheets writes."""
        import sqlite3
        from datetime import datetime, timedelta
        conn = sqlite3.connect(db_path)
        past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO all_groups (chat_id, type, sheet_id, subs_date_end) VALUES ('-100', 'PRO', 'sheet123', ?)",
            (past,),
        )
        conn.commit()
        conn.close()

        result = await sheets.get_sheet_for_chat("-100")
        assert result is None

    async def test_pro_tier_with_no_subs_date_end_returns_none(self, db_path):
        """A PRO row with no subs_date_end at all (never actually
        activated) must not be treated as an active subscription."""
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO all_groups (chat_id, type, sheet_id) VALUES ('-100', 'PRO', 'sheet123')"
        )
        conn.commit()
        conn.close()

        result = await sheets.get_sheet_for_chat("-100")
        assert result is None

    async def test_malformed_subs_date_end_returns_none(self, db_path):
        """A corrupted/unparseable date string must fail safe (no
        Sheets writes), not raise an unhandled exception."""
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO all_groups (chat_id, type, sheet_id, subs_date_end) VALUES ('-100', 'PRO', 'sheet123', 'not-a-date')"
        )
        conn.commit()
        conn.close()

        result = await sheets.get_sheet_for_chat("-100")
        assert result is None

    async def test_uses_db_path_not_a_hardcoded_file(self, db_path):
        """Regression test for the real bug fixed alongside this
        coverage: get_sheet_for_chat used to hardcode "database.db"
        instead of db.DB_PATH, meaning it would silently connect to
        the WRONG file if the bot's working directory ever differed
        from where the real database.db lives. Confirmed by inserting
        data via the db_path fixture's own isolated file and getting a
        correct, non-None result back - if the hardcoded path were
        still in place, this row would live in a completely different
        (real) database.db file this test never touches, and the
        result would incorrectly be None."""
        import sqlite3
        from datetime import datetime, timedelta
        conn = sqlite3.connect(db_path)
        future = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO all_groups (chat_id, type, sheet_id, subs_date_end) VALUES ('-777', 'PRO', 'isolated_sheet', ?)",
            (future,),
        )
        conn.commit()
        conn.close()

        result = await sheets.get_sheet_for_chat("-777")
        assert result == "isolated_sheet"


class TestControlSheetRoleColumn:
    """Real gap fixed: ROLE (the bot's own MEMBER/ADMIN status) was
    only added to the CHANNELS tab export, GROUPS was missed entirely."""

    async def test_groups_tab_header_includes_role(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        ws = MagicMock()
        ws.update = AsyncMock()
        ws.get_all_values = AsyncMock(return_value=[])
        ws.batch_clear = AsyncMock()
        ss = MagicMock()
        ss.worksheet = AsyncMock(return_value=ws)

        with patch("sheets.CONTROL_SHEET_ID", "fake_id"), \
             patch("sheets.open_spreadsheet", new_callable=AsyncMock, return_value=ss):
            await sheets.sync_control_sheet_main([
                ("-100", "G1", "FREE", None, None, None, None, "public", "01.01.2026", "ADMIN"),
            ])

        grid = ws.update.call_args.args[1]
        assert "ROLE" in grid[0]

    async def test_channels_tab_header_includes_role(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        ws = MagicMock()
        ws.update = AsyncMock()
        ws.get_all_values = AsyncMock(return_value=[])
        ws.batch_clear = AsyncMock()
        ss = MagicMock()
        ss.worksheet = AsyncMock(return_value=ws)

        with patch("sheets.CONTROL_SHEET_ID", "fake_id"), \
             patch("sheets.open_spreadsheet", new_callable=AsyncMock, return_value=ss):
            await sheets.sync_control_sheet_channels([
                ("-200", "C1", "public", "01.01.2026", "MEMBER"),
            ])

        grid = ws.update.call_args.args[1]
        assert "ROLE" in grid[0]


class TestOpenSpreadsheetRetry:
    """Real gap fixed: open_spreadsheet's OAuth-dependent calls
    (agcm.authorize + gc.open_by_key) were the only unprotected Google
    API call in sheets.py - any exception there propagated straight
    up through refreshusersall's outer catch with zero retry, even
    for "invalid_grant: Invalid grant: account not found", a
    documented, sometimes-transient Google error (server clock skew
    or a flaky token refresh, not always a genuinely revoked key)."""

    async def test_transient_invalid_grant_recovers_on_retry(self):
        sheets._spreadsheet_cache.pop("sheet_retry_test", None)
        call_count = {"n": 0}

        async def flaky_authorize():
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise Exception("invalid_grant: Invalid grant: account not found")
            gc = MagicMock()
            gc.open_by_key = AsyncMock(return_value="FAKE_SPREADSHEET")
            return gc

        with patch("sheets.agcm") as mock_agcm:
            mock_agcm.authorize = flaky_authorize
            result = await sheets.open_spreadsheet("sheet_retry_test", _retry_delay=0.01)

        assert result == "FAKE_SPREADSHEET"
        assert call_count["n"] == 2

    async def test_persistent_invalid_grant_raises_after_retries_exhausted(self):
        sheets._spreadsheet_cache.pop("sheet_persistent_fail", None)

        async def always_fails():
            raise Exception("invalid_grant: Invalid grant: account not found")

        with patch("sheets.agcm") as mock_agcm:
            mock_agcm.authorize = always_fails
            with pytest.raises(Exception, match="invalid_grant"):
                await sheets.open_spreadsheet("sheet_persistent_fail", _retries=2, _retry_delay=0.01)

    async def test_non_invalid_grant_error_is_not_retried(self):
        """A different kind of error must fail immediately, not burn
        through retries meant specifically for the OAuth hiccup case."""
        sheets._spreadsheet_cache.pop("sheet_other_error", None)
        call_count = {"n": 0}

        async def different_error():
            call_count["n"] += 1
            raise Exception("some other unrelated error")

        with patch("sheets.agcm") as mock_agcm:
            mock_agcm.authorize = different_error
            with pytest.raises(Exception, match="unrelated error"):
                await sheets.open_spreadsheet("sheet_other_error", _retry_delay=0.01)

        assert call_count["n"] == 1, "must not retry a non-invalid_grant error"

    async def test_cached_sheet_never_calls_authorize_at_all(self):
        sheets._spreadsheet_cache["sheet_cached_unique"] = "ALREADY_CACHED"
        with patch("sheets.agcm") as mock_agcm:
            mock_agcm.authorize = AsyncMock(side_effect=AssertionError("should not be called"))
            result = await sheets.open_spreadsheet("sheet_cached_unique")
        assert result == "ALREADY_CACHED"
        sheets._spreadsheet_cache.pop("sheet_cached_unique", None)

    async def test_default_retry_count_is_four(self):
        """Strengthened after a real report where the original single
        retry (2 total attempts) still exhausted every attempt with
        server clock sync and the fix's own deployment both confirmed
        fine - now defaults to 4 retries (5 total attempts)."""
        sheets._spreadsheet_cache.pop("sheet_default_retries", None)
        call_count = {"n": 0}

        async def always_fails():
            call_count["n"] += 1
            raise Exception("invalid_grant: Invalid grant: account not found")

        with patch("sheets.agcm") as mock_agcm:
            mock_agcm.authorize = always_fails
            with pytest.raises(Exception, match="invalid_grant"):
                await sheets.open_spreadsheet("sheet_default_retries", _retry_delay=0.01)

        assert call_count["n"] == 5, "default must be 4 retries = 5 total attempts"

    async def test_backoff_delay_doubles_each_attempt(self):
        sheets._spreadsheet_cache.pop("sheet_backoff_test", None)
        sleep_calls = []

        async def always_fails():
            raise Exception("invalid_grant: Invalid grant: account not found")

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        with patch("sheets.agcm") as mock_agcm, patch("sheets.asyncio.sleep", fake_sleep):
            mock_agcm.authorize = always_fails
            with pytest.raises(Exception, match="invalid_grant"):
                await sheets.open_spreadsheet("sheet_backoff_test", _retries=3, _retry_delay=2.0)

        assert sleep_calls == [2.0, 4.0, 8.0], "delay must double each attempt (exponential backoff)"
