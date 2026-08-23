"""
Tests for main.py's on_my_chat_member_update - tracks the BOT'S OWN
membership changes (added to / removed from a group or channel) and keeps
all_groups/all_channels/all_chats_bot_log plus the Control Sheet's
GROUPS/CHANNELS tabs in sync immediately, without waiting for the next
command or bot restart.

Previously had ZERO test coverage at all - this file covers the full
add/remove lifecycle for both groups and channels, verifying:
  - Groups go into all_groups with type=FREE; channels go into all_channels
  - Both get pushed to the Control Sheet immediately (not just on request)
  - Removal deletes the row from all_groups/all_channels AND correctly
    clears it from the Control Sheet (full overwrite + stale-row trim, not
    append-only, so a removed chat's row doesn't linger forever)
  - Removal logs to all_chats_bot_log with BOTH date_bot_add (copied from
    the original row) and date_bot_remove (the moment of removal)
"""
import sys
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import main


def _my_chat_member_update(chat_id, chat_type, chat_title, old_status, new_status):
    upd = MagicMock()
    chat = MagicMock()
    chat.id = chat_id
    chat.type = chat_type
    chat.title = chat_title
    chat.username = None
    old_m = MagicMock(status=old_status)
    new_m = MagicMock(status=new_status)
    upd.my_chat_member = MagicMock(chat=chat, old_chat_member=old_m, new_chat_member=new_m)
    return upd


class TestBotAddedToGroup:
    async def test_group_added_gets_free_type_and_immediate_sheet_push(self, db_path):
        main.CONTROL_SHEET_ID = "fake_sheet_id"
        with patch("subscription.sync_control_sheet_main", new_callable=AsyncMock) as sync_main, \
             patch("subscription.sync_control_sheet_channels", new_callable=AsyncMock) as sync_channels:
            sync_main.return_value = True
            sync_channels.return_value = True
            upd = _my_chat_member_update(-100, "supergroup", "My Group", "left", "member")
            await main.on_my_chat_member_update(upd, MagicMock())

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT chat_id, chat_name, type FROM all_groups WHERE chat_id='-100'").fetchone()
        assert row == ("-100", "My Group", "FREE")

        # Pushed immediately - not waiting for the next command
        sync_main.assert_called_once()
        pushed_rows = sync_main.call_args.args[0]
        assert any(r[0] == "-100" for r in pushed_rows)

    async def test_creator_status_also_counts_as_added(self, db_path):
        main.CONTROL_SHEET_ID = "fake_sheet_id"
        with patch("subscription.sync_control_sheet_main", new_callable=AsyncMock, return_value=True), \
             patch("subscription.sync_control_sheet_channels", new_callable=AsyncMock, return_value=True):
            upd = _my_chat_member_update(-100, "group", "My Group", "left", "creator")
            await main.on_my_chat_member_update(upd, MagicMock())

        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT chat_id FROM all_groups WHERE chat_id='-100'").fetchone() is not None


class TestBotAddedToChannel:
    async def test_channel_added_goes_to_all_channels_not_all_groups(self, db_path):
        main.CONTROL_SHEET_ID = "fake_sheet_id"
        with patch("subscription.sync_control_sheet_main", new_callable=AsyncMock, return_value=True), \
             patch("subscription.sync_control_sheet_channels", new_callable=AsyncMock) as sync_channels:
            sync_channels.return_value = True
            upd = _my_chat_member_update(-200, "channel", "My Channel", "left", "administrator")
            await main.on_my_chat_member_update(upd, MagicMock())

        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT chat_id FROM all_channels WHERE chat_id='-200'").fetchone() is not None
        assert conn.execute("SELECT chat_id FROM all_groups WHERE chat_id='-200'").fetchone() is None

        sync_channels.assert_called_once()
        pushed_rows = sync_channels.call_args.args[0]
        assert any(r[0] == "-200" for r in pushed_rows)


class TestBotRemovedFromGroup:
    async def test_removal_deletes_row_logs_both_dates_clears_sheet(self, db_path):
        main.CONTROL_SHEET_ID = "fake_sheet_id"
        with patch("subscription.sync_control_sheet_main", new_callable=AsyncMock, return_value=True), \
             patch("subscription.sync_control_sheet_channels", new_callable=AsyncMock, return_value=True):
            await main.on_my_chat_member_update(
                _my_chat_member_update(-100, "supergroup", "My Group", "left", "member"), MagicMock()
            )

        with patch("subscription.sync_control_sheet_main", new_callable=AsyncMock) as sync_main, \
             patch("subscription.sync_control_sheet_channels", new_callable=AsyncMock, return_value=True):
            sync_main.return_value = True
            await main.on_my_chat_member_update(
                _my_chat_member_update(-100, "supergroup", "My Group", "member", "left"), MagicMock()
            )

        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT chat_id FROM all_groups WHERE chat_id='-100'").fetchone() is None

        log_row = conn.execute(
            "SELECT chat_id, date_bot_add, date_bot_remove FROM all_chats_bot_log WHERE chat_id='-100'"
        ).fetchone()
        assert log_row is not None
        assert log_row[1] is not None  # date_bot_add carried over from the original row
        assert log_row[2] is not None  # date_bot_remove set to the moment of removal

        # The sheet push after removal must reflect the removal (chat no
        # longer present), not just append - proves full-overwrite sync.
        pushed_rows = sync_main.call_args.args[0]
        assert not any(r[0] == "-100" for r in pushed_rows)

    async def test_kicked_status_also_counts_as_removed(self, db_path):
        main.CONTROL_SHEET_ID = "fake_sheet_id"
        with patch("subscription.sync_control_sheet_main", new_callable=AsyncMock, return_value=True), \
             patch("subscription.sync_control_sheet_channels", new_callable=AsyncMock, return_value=True):
            await main.on_my_chat_member_update(
                _my_chat_member_update(-100, "supergroup", "My Group", "left", "member"), MagicMock()
            )
            await main.on_my_chat_member_update(
                _my_chat_member_update(-100, "supergroup", "My Group", "member", "kicked"), MagicMock()
            )

        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT chat_id FROM all_groups WHERE chat_id='-100'").fetchone() is None
        assert conn.execute("SELECT chat_id FROM all_chats_bot_log WHERE chat_id='-100'").fetchone() is not None


class TestBotRemovedFromChannel:
    async def test_removal_deletes_row_logs_both_dates_clears_sheet(self, db_path):
        main.CONTROL_SHEET_ID = "fake_sheet_id"
        with patch("subscription.sync_control_sheet_main", new_callable=AsyncMock, return_value=True), \
             patch("subscription.sync_control_sheet_channels", new_callable=AsyncMock, return_value=True):
            await main.on_my_chat_member_update(
                _my_chat_member_update(-200, "channel", "My Channel", "left", "administrator"), MagicMock()
            )

        with patch("subscription.sync_control_sheet_main", new_callable=AsyncMock, return_value=True), \
             patch("subscription.sync_control_sheet_channels", new_callable=AsyncMock) as sync_channels:
            sync_channels.return_value = True
            await main.on_my_chat_member_update(
                _my_chat_member_update(-200, "channel", "My Channel", "administrator", "left"), MagicMock()
            )

        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT chat_id FROM all_channels WHERE chat_id='-200'").fetchone() is None

        log_row = conn.execute(
            "SELECT chat_id, date_bot_add, date_bot_remove FROM all_chats_bot_log WHERE chat_id='-200'"
        ).fetchone()
        assert log_row is not None
        assert log_row[1] is not None
        assert log_row[2] is not None

        pushed_rows = sync_channels.call_args.args[0]
        assert not any(r[0] == "-200" for r in pushed_rows)


class TestNonMembershipChangesAreIgnored:
    async def test_restricted_to_member_transition_does_not_trigger_add_or_remove(self, db_path):
        """Neither status was a 'not present' state and neither is a
        genuine add/remove - e.g. admin rights being granted/revoked
        while the bot stays a member throughout. Must not log spurious
        add/remove events."""
        main.CONTROL_SHEET_ID = "fake_sheet_id"
        with patch("subscription.sync_control_sheet_main", new_callable=AsyncMock) as sync_main, \
             patch("subscription.sync_control_sheet_channels", new_callable=AsyncMock) as sync_channels:
            await main.on_my_chat_member_update(
                _my_chat_member_update(-100, "supergroup", "My Group", "member", "administrator"), MagicMock()
            )

        sync_main.assert_not_called()
        sync_channels.assert_not_called()
        conn = sqlite3.connect(db_path)
        assert conn.execute("SELECT chat_id FROM all_groups WHERE chat_id='-100'").fetchone() is None


class TestChatsLogSyncedToControlSheet:
    """New: all_chats_bot_log (the historical add/remove trail) is now
    synced to the Control Sheet's 'chats_log' tab, alongside GROUPS/
    CHANNELS, immediately whenever the bot is removed from a chat."""

    async def test_removal_pushes_chats_log_with_both_dates(self, db_path):
        main.CONTROL_SHEET_ID = "fake_sheet_id"
        with patch("subscription.sync_control_sheet_main", new_callable=AsyncMock, return_value=True), \
             patch("subscription.sync_control_sheet_channels", new_callable=AsyncMock, return_value=True), \
             patch("subscription.sync_control_sheet_chats_log", new_callable=AsyncMock, return_value=True):
            await main.on_my_chat_member_update(
                _my_chat_member_update(-100, "supergroup", "My Group", "left", "member"), MagicMock()
            )

        with patch("subscription.sync_control_sheet_main", new_callable=AsyncMock, return_value=True), \
             patch("subscription.sync_control_sheet_channels", new_callable=AsyncMock, return_value=True), \
             patch("subscription.sync_control_sheet_chats_log", new_callable=AsyncMock) as sync_chats_log:
            sync_chats_log.return_value = True
            await main.on_my_chat_member_update(
                _my_chat_member_update(-100, "supergroup", "My Group", "member", "left"), MagicMock()
            )

        sync_chats_log.assert_called_once()
        pushed_rows = sync_chats_log.call_args.args[0]
        assert len(pushed_rows) == 1
        assert pushed_rows[0][0] == "-100"
        assert pushed_rows[0][1] is not None
        assert pushed_rows[0][2] is not None


class TestStatsCommandRegistration:
    """/stats was decorated with @register_hub_command in handlers.py, but
    that alone isn't enough - python-telegram-bot needs its own explicit
    CommandHandler registration in main()'s setup to actually route
    incoming /stats messages at all. Verified via static source
    inspection since main() blocks on run_polling(), making direct
    invocation in a test unsafe."""

    def test_stats_command_handler_registered_in_main(self):
        source = open("main.py", encoding="utf-8").read()
        assert 'CommandHandler("stats", stats_command)' in source

    def test_stats_command_imported_from_handlers(self):
        source = open("main.py", encoding="utf-8").read()
        import_block = source[source.index("from handlers import"):source.index("from subscription import")]
        assert "stats_command" in import_block


class TestOnChatMemberUpdateCapturesRealName:
    """Real gap found: on_chat_member_update (auto-tracking someone
    joining/leaving) passed a real user_id but never first_name/last_name
    to track_user(), even though the user object always has this data
    readily available - meaning newly-joined members would show as their
    plain username instead of 'First Last' everywhere they're mentioned,
    until they happened to trigger some OTHER, more complete track_user()
    call."""

    async def test_join_captures_first_and_last_name(self, db_path):
        result = MagicMock()
        result.chat.id = -1
        new_member = MagicMock()
        new_member.status = "member"
        user = MagicMock()
        user.id = 777
        user.username = "newperson"
        user.first_name = "New"
        user.last_name = "Person"
        new_member.user = user
        result.new_chat_member = new_member
        upd = MagicMock()
        upd.chat_member = result

        await main.on_chat_member_update(upd, MagicMock())

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT user_id, first_name, last_name FROM main_group_users WHERE chat_id='-1'").fetchone()
        assert row == ("777", "New", "Person")

    async def test_leave_also_captures_name(self, db_path):
        result = MagicMock()
        result.chat.id = -1
        new_member = MagicMock()
        new_member.status = "left"
        user = MagicMock()
        user.id = 888
        user.username = "leaverperson"
        user.first_name = "Leaver"
        user.last_name = "Person"
        new_member.user = user
        result.new_chat_member = new_member
        upd = MagicMock()
        upd.chat_member = result

        await main.on_chat_member_update(upd, MagicMock())

        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT user_id, first_name, last_name, status FROM main_group_users WHERE chat_id='-1'").fetchone()
        assert row == ("888", "Leaver", "Person", "passive")
