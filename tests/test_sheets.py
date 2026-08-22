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
