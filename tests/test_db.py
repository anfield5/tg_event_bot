"""
Tests for db.py
───────────────
Uses an isolated temp SQLite file (via tmp_path) so no test ever touches the
real `database.db`.

We import `init_db` and `track_user` directly and pass `db_path` explicitly,
since those functions accept an optional path argument.
"""

import sqlite3
import pytest
from datetime import datetime, timedelta
from db import (
    init_db, track_user, get_all_features, update_feature_flag, log_command_usage,
    get_event_total_going_headcount, add_to_waitlist, promote_next_from_waitlist,
    register_chat_added, register_chat_removed, get_feature_limit_for_chat, get_display_name,
    dedupe_waitlist, get_shareevent_remaining_for_chat, migrate_event_to_event_users,
    is_bot_locked, set_bot_locked, ensure_event_migrated,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_tables(db_path: str) -> set:
    """Returns the set of table names in the given SQLite file."""
    conn   = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in cursor.fetchall()}
    conn.close()
    return tables


def get_columns(db_path: str, table: str) -> list:
    """Returns column names for a given table."""
    conn   = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(f"PRAGMA table_info({table})")
    cols = [row[1] for row in cursor.fetchall()]
    conn.close()
    return cols


def fetch_all(db_path: str, query: str, params=()):
    conn   = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    return rows


def run_sql(db_path: str, query: str, params=()):
    conn = sqlite3.connect(db_path)
    conn.execute(query, params)
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# init_db — fresh schema
# ---------------------------------------------------------------------------

class TestInitDb:
    """init_db() must create all required tables and add migration columns."""

    EXPECTED_TABLES = {
        "all_groups",
        "all_channels",
        "all_chats_bot_log",
        "events",
        "main_group_users",
        "event_shares",
        "sub_chats",
        "event_users",
    }

    def test_all_tables_created(self, tmp_path):
        # Fresh database — every table must be present after init
        path = str(tmp_path / "fresh.db")
        init_db(db_path=path)
        assert self.EXPECTED_TABLES.issubset(get_tables(path))

    def test_legacy_table_names_are_gone(self, tmp_path):
        # A fresh DB must never contain the old pre-rename table names
        path = str(tmp_path / "fresh.db")
        init_db(db_path=path)
        tables = get_tables(path)
        assert "chat_settings" not in tables
        assert "chat_users" not in tables
        assert "chat_aliases" not in tables
        assert "monitors" not in tables

    def test_events_has_event_date_column(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        assert "event_date" in get_columns(path, "events")

    def test_events_has_event_status_column(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        assert "event_status" in get_columns(path, "events")

    def test_events_has_no_legacy_is_open_columns(self, tmp_path):
        # is_open/is_cancelled must not exist anywhere - event_status replaces both
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        cols = get_columns(path, "events")
        assert "is_open" not in cols
        assert "is_cancelled" not in cols

    def test_events_has_kicked_data_column(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        assert "kicked_data" in get_columns(path, "events")

    def test_main_group_users_has_user_id_column(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        assert "user_id" in get_columns(path, "main_group_users")

    def test_main_group_users_has_status_column(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        assert "status" in get_columns(path, "main_group_users")

    def test_main_group_users_has_name_columns(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        cols = get_columns(path, "main_group_users")
        assert "first_name" in cols
        assert "last_name" in cols

    def test_all_groups_columns(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        cols = get_columns(path, "all_groups")
        for expected in ("chat_id", "type", "sheet_id", "sheet_name", "subs_date_start",
                         "subs_date_end", "visibility", "date_bot_add"):
            assert expected in cols, f"all_groups missing '{expected}'"

    def test_sub_chats_columns(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        cols = get_columns(path, "sub_chats")
        for expected in ("chat_id", "owner_chat_id", "alias", "is_monitored", "chat_type", "chat_name"):
            assert expected in cols, f"sub_chats missing '{expected}'"

    def test_idempotent_second_call(self, tmp_path):
        # Calling init_db twice must not raise (IF NOT EXISTS guards)
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        init_db(db_path=path)   # second call — must not fail
        assert self.EXPECTED_TABLES.issubset(get_tables(path))

    def test_idempotent_third_call_does_not_duplicate_data(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        track_user("chat1", "alice", "active", db_path=path)
        init_db(db_path=path)
        init_db(db_path=path)
        rows = fetch_all(path, "SELECT COUNT(*) FROM main_group_users WHERE chat_id='chat1' AND username='alice'")
        assert rows[0][0] == 1

    def test_frozen_status_migrated_to_passive(self, tmp_path):
        # Legacy records with status='frozen' must be updated to 'passive'
        path = str(tmp_path / "t.db")
        run_sql(path, """
            CREATE TABLE IF NOT EXISTS main_group_users (
                chat_id TEXT, username TEXT, status TEXT DEFAULT 'active',
                PRIMARY KEY (chat_id, username)
            )
        """)
        run_sql(path, "INSERT INTO main_group_users (chat_id, username, status) VALUES ('c1','alice','frozen')")

        init_db(db_path=path)

        rows = fetch_all(path, "SELECT status FROM main_group_users WHERE username = 'alice'")
        assert rows[0][0] == "passive", "Legacy 'frozen' status should be migrated to 'passive'"


# ---------------------------------------------------------------------------
# init_db — migrations from pre-rename schemas
# ---------------------------------------------------------------------------

class TestMigrationChatUsersRename:
    """Legacy 'chat_users' table must be renamed to 'main_group_users', preserving data."""

    def test_renames_and_preserves_data(self, tmp_path):
        path = str(tmp_path / "t.db")
        run_sql(path, """
            CREATE TABLE chat_users (
                chat_id TEXT, username TEXT, status TEXT DEFAULT 'active',
                PRIMARY KEY (chat_id, username)
            )
        """)
        run_sql(path, "INSERT INTO chat_users (chat_id, username, status) VALUES ('c1','alice','active')")

        init_db(db_path=path)

        tables = get_tables(path)
        assert "main_group_users" in tables
        assert "chat_users" not in tables
        rows = fetch_all(path, "SELECT username, status FROM main_group_users WHERE chat_id='c1'")
        assert rows == [("alice", "active")]


class TestMigrationAllFeaturesRename:
    """'feature_flags' table must be renamed to 'all_features', preserving
    any admin-customized data (e.g. a tier changed via /updatefeature)."""

    def test_renames_and_preserves_data(self, tmp_path):
        path = str(tmp_path / "t.db")
        run_sql(path, """
            CREATE TABLE feature_flags (
                feature_key TEXT PRIMARY KEY,
                feature_label TEXT NOT NULL,
                min_tier TEXT NOT NULL DEFAULT 'FREE',
                limit_count INTEGER DEFAULT NULL,
                sort_order INTEGER DEFAULT 999,
                description TEXT DEFAULT NULL
            )
        """)
        # A real feature_key with an admin-customized tier, to confirm the
        # rename preserves data rather than resetting it via re-seeding.
        run_sql(path, "INSERT INTO feature_flags (feature_key, feature_label, min_tier) VALUES ('aliases','Aliases','ADMIN')")

        init_db(db_path=path)

        tables = get_tables(path)
        assert "all_features" in tables
        assert "feature_flags" not in tables
        row = fetch_all(path, "SELECT feature_key, min_tier FROM all_features WHERE feature_key='aliases'")
        assert row == [("aliases", "ADMIN")]


class TestMigrationDateBotRemovedRename:
    """all_chats_bot_log's 'date_bot_removed' column must be renamed to
    'date_bot_remove', preserving data."""

    def test_renames_and_preserves_data(self, tmp_path):
        path = str(tmp_path / "t.db")
        run_sql(path, """
            CREATE TABLE all_chats_bot_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                date_bot_add TEXT DEFAULT NULL,
                date_bot_removed TEXT DEFAULT NULL
            )
        """)
        run_sql(path, "INSERT INTO all_chats_bot_log (chat_id, date_bot_add, date_bot_removed) VALUES ('-1','01.01.2026','05.01.2026')")

        init_db(db_path=path)

        cols = [r[1] for r in fetch_all(path, "PRAGMA table_info(all_chats_bot_log)")]
        assert "date_bot_remove" in cols
        assert "date_bot_removed" not in cols
        rows = fetch_all(path, "SELECT chat_id, date_bot_add, date_bot_remove FROM all_chats_bot_log")
        assert rows == [("-1", "01.01.2026", "05.01.2026")]


class TestMigrationChatSettingsRename:
    """
    Legacy 'chat_settings' (sheet_name) must become 'all_groups' (sheet_id +
    subscription fields) - a single init_db() call runs the FULL migration
    chain (chat_settings -> main_chat_settings -> all_groups), so a
    database starting from the oldest legacy name ends up fully migrated
    to the current one in one pass.
    """

    def test_renames_column_and_preserves_data(self, tmp_path):
        path = str(tmp_path / "t.db")
        run_sql(path, "CREATE TABLE chat_settings (chat_id TEXT PRIMARY KEY, sheet_name TEXT)")
        run_sql(path, "INSERT INTO chat_settings (chat_id, sheet_name) VALUES ('-1001234567890','TestEventsSheet')")

        init_db(db_path=path)

        tables = get_tables(path)
        assert "all_groups" in tables
        assert "chat_settings" not in tables
        assert "main_chat_settings" not in tables
        rows = fetch_all(path, "SELECT chat_id, sheet_id, type FROM all_groups WHERE chat_id='-1001234567890'")
        assert rows == [("-1001234567890", "TestEventsSheet", "FREE")]

    def test_idempotent_after_rename(self, tmp_path):
        path = str(tmp_path / "t.db")
        run_sql(path, "CREATE TABLE chat_settings (chat_id TEXT PRIMARY KEY, sheet_name TEXT)")
        run_sql(path, "INSERT INTO chat_settings (chat_id, sheet_name) VALUES ('100','SheetA')")
        init_db(db_path=path)
        init_db(db_path=path)  # must not raise or duplicate
        rows = fetch_all(path, "SELECT chat_id, sheet_id FROM all_groups")
        assert rows == [("100", "SheetA")]


class TestMigrationMainChatSettingsRename:
    """
    'main_chat_settings' (the intermediate name) must become 'all_groups' -
    covers a database that already migrated to main_chat_settings under an
    older bot version, and is now catching up to the current name.
    """

    def test_renames_and_preserves_data(self, tmp_path):
        path = str(tmp_path / "t.db")
        run_sql(path, """
            CREATE TABLE main_chat_settings (
                chat_id TEXT PRIMARY KEY, chat_name TEXT, type TEXT DEFAULT 'free',
                sheet_id TEXT UNIQUE, sheet_name TEXT, subs_date_start TEXT, subs_date_end TEXT
            )
        """)
        run_sql(path, "INSERT INTO main_chat_settings (chat_id, type, sheet_id) VALUES ('-200', 'pro', 'SheetB')")

        init_db(db_path=path)

        tables = get_tables(path)
        assert "all_groups" in tables
        assert "main_chat_settings" not in tables
        rows = fetch_all(path, "SELECT chat_id, type, sheet_id FROM all_groups WHERE chat_id='-200'")
        assert rows == [("-200", "PRO", "SheetB")]

    def test_idempotent_after_rename(self, tmp_path):
        path = str(tmp_path / "t.db")
        run_sql(path, """
            CREATE TABLE main_chat_settings (
                chat_id TEXT PRIMARY KEY, chat_name TEXT, type TEXT DEFAULT 'free',
                sheet_id TEXT UNIQUE, sheet_name TEXT, subs_date_start TEXT, subs_date_end TEXT
            )
        """)
        run_sql(path, "INSERT INTO main_chat_settings (chat_id, sheet_id) VALUES ('300', 'SheetC')")
        init_db(db_path=path)
        init_db(db_path=path)
        rows = fetch_all(path, "SELECT chat_id, sheet_id FROM all_groups")
        assert rows == [("300", "SheetC")]


class TestMigrationSubGroupsMerge:
    """Legacy chat_aliases + monitors must merge into a single sub_chats table."""

    def test_merges_alias_only_row(self, tmp_path):
        path = str(tmp_path / "t.db")
        run_sql(path, "CREATE TABLE chat_aliases (chat_id TEXT PRIMARY KEY, alias TEXT UNIQUE, owner_chat_id TEXT)")
        run_sql(path, "INSERT INTO chat_aliases (chat_id, alias, owner_chat_id) VALUES ('-200','downtown','-100')")

        init_db(db_path=path)

        assert "sub_chats" in get_tables(path)
        assert "chat_aliases" not in get_tables(path)
        rows = fetch_all(path, "SELECT chat_id, alias, is_monitored, owner_chat_id FROM sub_chats")
        assert rows == [("-200", "downtown", 0, "-100")]

    def test_merges_monitor_only_row(self, tmp_path):
        path = str(tmp_path / "t.db")
        run_sql(path, "CREATE TABLE monitors (chat_id TEXT PRIMARY KEY, chat_type TEXT, chat_name TEXT, owner_chat_id TEXT)")
        run_sql(path, "INSERT INTO monitors VALUES ('-300','group','Other Group','-100')")

        init_db(db_path=path)

        assert "monitors" not in get_tables(path)
        rows = fetch_all(path, "SELECT chat_id, alias, is_monitored, chat_type, chat_name FROM sub_chats")
        assert rows == [("-300", None, 1, "group", "Other Group")]

    def test_merges_chat_present_in_both_legacy_tables_into_one_row(self, tmp_path):
        """
        A chat that was BOTH aliased and monitored under the same owner must
        become a single sub_chats row with both facts set, not two rows.
        """
        path = str(tmp_path / "t.db")
        run_sql(path, "CREATE TABLE chat_aliases (chat_id TEXT PRIMARY KEY, alias TEXT UNIQUE, owner_chat_id TEXT)")
        run_sql(path, "CREATE TABLE monitors (chat_id TEXT PRIMARY KEY, chat_type TEXT, chat_name TEXT, owner_chat_id TEXT)")
        run_sql(path, "INSERT INTO chat_aliases (chat_id, alias, owner_chat_id) VALUES ('-200','downtown','-100')")
        run_sql(path, "INSERT INTO monitors VALUES ('-200','channel','Downtown Channel','-100')")

        init_db(db_path=path)

        rows = fetch_all(path, "SELECT chat_id, alias, is_monitored, chat_type, chat_name FROM sub_chats")
        assert rows == [("-200", "downtown", 1, "channel", "Downtown Channel")]

    def test_idempotent_after_merge(self, tmp_path):
        path = str(tmp_path / "t.db")
        run_sql(path, "CREATE TABLE chat_aliases (chat_id TEXT PRIMARY KEY, alias TEXT UNIQUE, owner_chat_id TEXT)")
        run_sql(path, "INSERT INTO chat_aliases (chat_id, alias, owner_chat_id) VALUES ('-200','downtown','-100')")
        init_db(db_path=path)
        init_db(db_path=path)  # must not raise or duplicate
        rows = fetch_all(path, "SELECT chat_id, alias FROM sub_chats")
        assert rows == [("-200", "downtown")]


class TestMigrationEventStatusRebuild:
    """Legacy events.is_open/is_cancelled must be translated into a single event_status column."""

    LEGACY_SCHEMA = """
        CREATE TABLE events (
            event_id TEXT PRIMARY KEY, chat_id TEXT, message_id TEXT, name TEXT,
            going_icon TEXT, notgoing_icon TEXT, is_open INTEGER, going_data TEXT,
            notgoing_data TEXT, counters_data TEXT, event_date TEXT DEFAULT NULL,
            is_cancelled INTEGER DEFAULT 0, kicked_data TEXT DEFAULT '[]'
        )
    """

    def _insert_legacy_event(self, path, event_id, is_open, is_cancelled):
        run_sql(
            path,
            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, "100", "1", "Test", "✅", "❌", is_open,
             "[]", "[]", "{}", None, is_cancelled, "[]"),
        )

    @pytest.mark.parametrize("is_open,is_cancelled,expected_status", [
        (1, 0, 0),   # open -> 0
        (2, 0, 1),   # verification -> 1
        (0, 0, 2),   # closed -> 2
        (0, 1, -1),  # canceled -> -1 (is_cancelled overrides is_open)
    ])
    def test_translates_each_legacy_combination(self, tmp_path, is_open, is_cancelled, expected_status):
        path = str(tmp_path / "t.db")
        run_sql(path, self.LEGACY_SCHEMA)
        self._insert_legacy_event(path, "ev1", is_open, is_cancelled)

        init_db(db_path=path)

        rows = fetch_all(path, "SELECT event_status FROM events WHERE event_id='ev1'")
        assert rows == [(expected_status,)]

    def test_preserves_other_event_fields(self, tmp_path):
        path = str(tmp_path / "t.db")
        run_sql(path, self.LEGACY_SCHEMA)
        run_sql(
            path,
            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("ev1", "100", "1", "Party", "✅", "❌", 1,
             '["alice (1)"]', "[]", '{"alice": 2}', "25.12.2026", 0, '["bob"]'),
        )

        init_db(db_path=path)

        row = fetch_all(
            path,
            "SELECT name, going_data, counters_data, event_date, kicked_data FROM events WHERE event_id='ev1'",
        )[0]
        assert row == ("Party", '["alice (1)"]', '{"alice": 2}', "25.12.2026", '["bob"]')

    def test_idempotent_after_rebuild(self, tmp_path):
        path = str(tmp_path / "t.db")
        run_sql(path, self.LEGACY_SCHEMA)
        self._insert_legacy_event(path, "ev1", 1, 0)
        init_db(db_path=path)
        init_db(db_path=path)  # must not raise or re-translate
        rows = fetch_all(path, "SELECT event_status FROM events WHERE event_id='ev1'")
        assert rows == [(0,)]


# ---------------------------------------------------------------------------
# track_user
# ---------------------------------------------------------------------------

class TestTrackUser:
    """track_user() upserts a user record into main_group_users."""

    def test_inserts_new_user(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        track_user("chat1", "alice", "active", db_path=path)
        rows = fetch_all(path, "SELECT username, status FROM main_group_users WHERE chat_id='chat1'")
        assert len(rows) == 1
        assert rows[0] == ("alice", "active")

    def test_updates_existing_status(self, tmp_path):
        # Insert active first, then update to passive
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        track_user("chat1", "alice", "active",  db_path=path)
        track_user("chat1", "alice", "passive", db_path=path)
        rows = fetch_all(path, "SELECT status FROM main_group_users WHERE chat_id='chat1' AND username='alice'")
        assert rows[0][0] == "passive"

    def test_empty_username_is_ignored(self, tmp_path):
        # track_user must silently return without inserting anything
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        track_user("chat1", "", "active", db_path=path)
        track_user("chat1", None, "active", db_path=path)
        rows = fetch_all(path, "SELECT * FROM main_group_users")
        assert len(rows) == 0

    def test_stores_user_id(self, tmp_path):
        # When user_id is provided it must be stored in the column
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        track_user("chat1", "bob", "active", user_id="99988", db_path=path)
        rows = fetch_all(path, "SELECT user_id FROM main_group_users WHERE username='bob'")
        assert rows[0][0] == "99988"

    def test_user_id_preserved_on_status_update(self, tmp_path):
        # If we update status without passing user_id, the stored user_id must remain
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        track_user("chat1", "bob", "active",  user_id="99988", db_path=path)
        track_user("chat1", "bob", "passive",                   db_path=path)  # no user_id
        rows = fetch_all(path, "SELECT user_id FROM main_group_users WHERE username='bob'")
        assert rows[0][0] == "99988", "user_id must be preserved when not explicitly passed"

    def test_multiple_users_different_chats(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        track_user("chat1", "alice", "active",  db_path=path)
        track_user("chat2", "alice", "passive", db_path=path)
        rows = fetch_all(path, "SELECT chat_id, status FROM main_group_users WHERE username='alice' ORDER BY chat_id")
        assert rows == [("chat1", "active"), ("chat2", "passive")]

    def test_default_status_is_active(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        track_user("chat1", "carol", db_path=path)  # no explicit status
        rows = fetch_all(path, "SELECT status FROM main_group_users WHERE username='carol'")
        assert rows[0][0] == "active"


class TestTypeCaseNormalization:
    """
    all_groups.type values must always be uppercase ('FREE'/'PRO'), even
    for rows already stored lowercase by an older bot version - init_db()
    normalizes them on every run.
    """

    def test_normalizes_existing_lowercase_values(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        run_sql(path, "INSERT INTO all_groups (chat_id, type) VALUES ('-1', 'free')")
        run_sql(path, "INSERT INTO all_groups (chat_id, type) VALUES ('-2', 'pro')")

        init_db(db_path=path)  # re-run triggers the normalization step

        rows = fetch_all(path, "SELECT chat_id, type FROM all_groups ORDER BY chat_id")
        assert rows == [("-1", "FREE"), ("-2", "PRO")]

    def test_already_uppercase_values_are_left_alone(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        run_sql(path, "INSERT INTO all_groups (chat_id, type) VALUES ('-3', 'PRO')")

        init_db(db_path=path)

        rows = fetch_all(path, "SELECT chat_id, type FROM all_groups WHERE chat_id='-3'")
        assert rows == [("-3", "PRO")]


class TestCommandLog:
    """command_log records every command invocation, including the full raw text typed."""

    def test_has_command_text_column(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        assert "command_text" in get_columns(path, "command_log")

    def test_log_command_usage_stores_full_text(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        log_command_usage("-100123", "42", "newevent", "/newevent Friday Football -date 25.12.2026",
                           "01.01.2026 12:00:00", db_path=path)
        rows = fetch_all(path, "SELECT chat_id, user_id, command, command_text FROM command_log")
        assert rows == [("-100123", "42", "newevent", "/newevent Friday Football -date 25.12.2026")]

    def test_command_text_column_added_to_existing_table(self, tmp_path):
        """A command_log table from before command_text existed must get the column added."""
        path = str(tmp_path / "t.db")
        run_sql(path, """
            CREATE TABLE command_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                user_id TEXT DEFAULT NULL,
                command TEXT NOT NULL,
                timestamp TEXT NOT NULL
            )
        """)
        init_db(db_path=path)
        assert "command_text" in get_columns(path, "command_log")


class TestFeatureFlags:
    """
    all_features is the single source of truth for what's available at
    each tier (FREE/PRO/ADMIN) - seeded automatically on init_db(), never
    overwritten by a second init_db() call.
    """

    def test_seeds_sixteen_flags_on_fresh_db(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        rows = get_all_features(db_path=path)
        assert len(rows) == 16

    def test_free_pro_admin_tiers_all_present(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        rows = get_all_features(db_path=path)
        tiers = {r[2] for r in rows}
        assert tiers == {"FREE", "PRO", "ADMIN"}

    def test_known_features_have_the_expected_tier(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        by_key = {r[0]: r[2] for r in get_all_features(db_path=path)}
        assert by_key["newevent"] == "FREE"
        assert by_key["editevent"] == "FREE"
        assert by_key["user_management"] == "FREE"
        assert by_key["aliases"] == "PRO"
        assert by_key["monitoring"] == "PRO"
        assert by_key["custom_sheet"] == "PRO"
        assert by_key["setsub"] == "ADMIN"
        assert by_key["owner_overview"] == "ADMIN"
        assert by_key["refreshusersall"] == "PRO"
        assert by_key["verification"] == "FREE"
        assert by_key["add_extra_member"] == "FREE"
        assert by_key["dm_access"] == "PRO"

    def test_shareevent_has_a_default_limit(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        by_key = {r[0]: (r[2], r[3]) for r in get_all_features(db_path=path)}
        assert by_key["shareevent"] == ("FREE", 3)

    def test_reseeding_does_not_overwrite_a_manually_changed_flag(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        update_feature_flag("aliases", "ADMIN", db_path=path)

        init_db(db_path=path)  # re-run must NOT reset aliases back to PRO

        by_key = {r[0]: r[2] for r in get_all_features(db_path=path)}
        assert by_key["aliases"] == "ADMIN"

    def test_reseeding_refreshes_stale_label_text_but_not_min_tier(self, tmp_path):
        """
        feature_label/description are developer-owned reference text, not
        user-configurable - a pre-existing database seeded before a wording
        change must pick up the correction on the next init_db(), while a
        manually-changed min_tier (which IS user-configurable) must survive.
        """
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        run_sql(path, "UPDATE all_features SET feature_label = 'stale old label' WHERE feature_key = 'aliases'")
        update_feature_flag("aliases", "ADMIN", db_path=path)

        init_db(db_path=path)

        rows = fetch_all(path, "SELECT feature_label, min_tier FROM all_features WHERE feature_key = 'aliases'")
        assert rows[0][0] != "stale old label"
        assert rows[0][1] == "ADMIN"

    def test_retires_feature_keys_no_longer_in_the_seed_list(self, tmp_path):
        """
        event_lifecycle + updateuser were decomposed into
        newevent/editevent/user_management - a pre-existing database must
        drop the old, now-orphaned rows on the next init_db(), not keep
        them around forever alongside the new ones.
        """
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        run_sql(path, "DELETE FROM all_features WHERE feature_key IN ('newevent','editevent','user_management')")
        run_sql(path, "INSERT INTO all_features (feature_key, feature_label, min_tier, description) "
                       "VALUES ('event_lifecycle','old bundle','FREE','old desc')")
        run_sql(path, "INSERT INTO all_features (feature_key, feature_label, min_tier, description) "
                       "VALUES ('updateuser','old updateuser','FREE','old desc')")

        init_db(db_path=path)

        by_key = {r[0]: r[2] for r in get_all_features(db_path=path)}
        assert "event_lifecycle" not in by_key
        assert "updateuser" not in by_key
        assert by_key["newevent"] == "FREE"
        assert by_key["editevent"] == "FREE"
        assert by_key["user_management"] == "FREE"

    def test_update_feature_flag_changes_only_the_target_row(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        update_feature_flag("shareevent", "PRO", db_path=path)

        by_key = {r[0]: r[2] for r in get_all_features(db_path=path)}
        assert by_key["shareevent"] == "PRO"
        assert by_key["newevent"] == "FREE"  # untouched


class TestMigrationSubGroupsRename:
    """'sub_groups' (the old name) must become 'sub_chats' - the old name
    implied "only groups", but this table has always covered channels too."""

    def test_renames_and_preserves_data(self, tmp_path):
        path = str(tmp_path / "t.db")
        run_sql(path, """
            CREATE TABLE sub_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT, owner_chat_id TEXT, alias TEXT,
                is_monitored INTEGER DEFAULT 0, chat_type TEXT, chat_name TEXT
            )
        """)
        run_sql(path, "INSERT INTO sub_groups (chat_id, owner_chat_id, alias, is_monitored) VALUES ('-999','-100111','downtown',1)")

        init_db(db_path=path)

        tables = get_tables(path)
        assert "sub_chats" in tables
        assert "sub_groups" not in tables
        rows = fetch_all(path, "SELECT chat_id, owner_chat_id, alias, is_monitored FROM sub_chats")
        assert rows == [("-999", "-100111", "downtown", 1)]

    def test_idempotent_after_rename(self, tmp_path):
        path = str(tmp_path / "t.db")
        run_sql(path, """
            CREATE TABLE sub_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT, owner_chat_id TEXT, alias TEXT,
                is_monitored INTEGER DEFAULT 0, chat_type TEXT, chat_name TEXT
            )
        """)
        run_sql(path, "INSERT INTO sub_groups (chat_id, alias) VALUES ('-1','test')")
        init_db(db_path=path)
        init_db(db_path=path)  # must not raise or duplicate
        rows = fetch_all(path, "SELECT chat_id, alias FROM sub_chats")
        assert rows == [("-1", "test")]


# ---------------------------------------------------------------------------
# Waitlist helpers (v3.24.0): get_event_total_going_headcount, add_to_waitlist,
# promote_next_from_waitlist. These are the pure DB-layer building blocks the
# capacity check and Standby/promotion mechanics in event_engine.button_handler
# are built on - see tests/test_handlers_async.py for the full click-through
# behavior.
# ---------------------------------------------------------------------------

import json


def _insert_event_for_waitlist(path, going=None, counters=None, total_limit=None, waitlist=None):
    conn = sqlite3.connect(path)
    conn.execute(
        """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
           event_status, going_data, notgoing_data, counters_data, kicked_data, total_limit, waitlist_data)
           VALUES ('ev1','-100','1','Test','👍','❌',0,?,'[]',?,'[]',?,?)""",
        (
            json.dumps(going or []),
            json.dumps(counters or {}),
            total_limit,
            json.dumps(waitlist or []),
        ),
    )
    conn.commit()
    conn.close()


class TestGetEventTotalGoingHeadcount:
    """Counts people, not rows - a person with guests fills multiple spots."""

    def test_counts_main_group_going_plus_guests(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        _insert_event_for_waitlist(path, going=["alice (1)", "bob (2)"], counters={"alice": 2})
        # alice + her 2 guests + bob = 4
        assert get_event_total_going_headcount("ev1", db_path=path) == 4

    def test_includes_child_chat_going_and_guests(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        _insert_event_for_waitlist(path, going=["alice (1)"])
        conn = sqlite3.connect(path)
        conn.execute(
            "INSERT INTO event_users (event_id, chat_id, user_id, username, status, guests) VALUES ('ev1','-200','2','carol','going',1)"
        )
        conn.commit()
        conn.close()
        # alice (main) + carol + her 1 guest = 3
        assert get_event_total_going_headcount("ev1", db_path=path) == 3

    def test_notgoing_child_rows_are_not_counted(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        _insert_event_for_waitlist(path, going=[])
        conn = sqlite3.connect(path)
        conn.execute(
            "INSERT INTO event_users (event_id, chat_id, user_id, username, status, guests) VALUES ('ev1','-200','2','carol','notgoing',3)"
        )
        conn.commit()
        conn.close()
        assert get_event_total_going_headcount("ev1", db_path=path) == 0

    def test_unknown_event_returns_zero(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        assert get_event_total_going_headcount("no-such-event", db_path=path) == 0


class TestAddToWaitlist:
    def test_appends_an_entry(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        _insert_event_for_waitlist(path)
        add_to_waitlist("ev1", "-100", "Main Hub", "dave", "4", db_path=path)
        conn = sqlite3.connect(path)
        wl = json.loads(conn.execute("SELECT waitlist_data FROM events WHERE event_id='ev1'").fetchone()[0])
        assert len(wl) == 1
        assert wl[0]["username"] == "dave"
        assert wl[0]["chat_id"] == "-100"

    def test_duplicate_add_same_user_same_chat_is_a_no_op(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        _insert_event_for_waitlist(path)
        add_to_waitlist("ev1", "-100", "Main Hub", "dave", "4", db_path=path)
        add_to_waitlist("ev1", "-100", "Main Hub", "dave", "4", db_path=path)
        conn = sqlite3.connect(path)
        wl = json.loads(conn.execute("SELECT waitlist_data FROM events WHERE event_id='ev1'").fetchone()[0])
        assert len(wl) == 1

    def test_same_user_different_chats_are_independent_entries(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        _insert_event_for_waitlist(path)
        add_to_waitlist("ev1", "-100", "Main Hub", "dave", "4", db_path=path)
        add_to_waitlist("ev1", "-200", "Child Group", "dave", "4", db_path=path)
        conn = sqlite3.connect(path)
        wl = json.loads(conn.execute("SELECT waitlist_data FROM events WHERE event_id='ev1'").fetchone()[0])
        assert len(wl) == 2


class TestPromoteNextFromWaitlist:
    def test_promotes_the_oldest_entry_for_that_chat(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        _insert_event_for_waitlist(path, waitlist=[
            {"chat_id": "-100", "chat_name": None, "username": "dave", "user_id": "4", "timestamp": "2026-01-01 00:00:00"},
            {"chat_id": "-100", "chat_name": None, "username": "erin", "user_id": "5", "timestamp": "2026-01-01 00:00:01"},
        ])
        promoted = promote_next_from_waitlist("ev1", "-100", db_path=path)
        assert promoted["username"] == "dave"
        conn = sqlite3.connect(path)
        wl = json.loads(conn.execute("SELECT waitlist_data FROM events WHERE event_id='ev1'").fetchone()[0])
        assert [e["username"] for e in wl] == ["erin"]

    def test_only_considers_entries_for_the_given_chat(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        _insert_event_for_waitlist(path, waitlist=[
            {"chat_id": "-200", "chat_name": None, "username": "dave", "user_id": "4", "timestamp": "2026-01-01 00:00:00"},
        ])
        promoted = promote_next_from_waitlist("ev1", "-100", db_path=path)
        assert promoted is None

    def test_empty_waitlist_returns_none(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        _insert_event_for_waitlist(path)
        assert promote_next_from_waitlist("ev1", "-100", db_path=path) is None


# ---------------------------------------------------------------------------
# register_chat_added / register_chat_removed - bot presence lifecycle,
# called from main.py's MY_CHAT_MEMBER handler. Previously had zero direct
# test coverage despite being real, meaningful business logic (upsert
# behavior on re-add, historical log trail on removal).
# ---------------------------------------------------------------------------

class TestRegisterChatAdded:
    def test_group_added_defaults_to_free(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        register_chat_added("-1", "My Group", "supergroup", "public", "2026-01-01 00:00:00", db_path=path)
        conn = sqlite3.connect(path)
        row = conn.execute("SELECT chat_id, chat_name, type, visibility FROM all_groups WHERE chat_id='-1'").fetchone()
        assert row == ("-1", "My Group", "FREE", "public")

    def test_channel_added_goes_to_all_channels_not_all_groups(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        register_chat_added("-2", "My Channel", "channel", "private", "2026-01-01 00:00:00", db_path=path)
        conn = sqlite3.connect(path)
        row = conn.execute("SELECT chat_id, chat_name, visibility FROM all_channels WHERE chat_id='-2'").fetchone()
        assert row == ("-2", "My Channel", "private")
        assert conn.execute("SELECT COUNT(*) FROM all_groups WHERE chat_id='-2'").fetchone()[0] == 0

    def test_re_adding_an_existing_group_upserts_not_duplicates(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        register_chat_added("-1", "My Group", "supergroup", "public", "2026-01-01 00:00:00", db_path=path)
        register_chat_added("-1", "Renamed Group", "supergroup", "private", "2026-01-02 00:00:00", db_path=path)
        conn = sqlite3.connect(path)
        count = conn.execute("SELECT COUNT(*) FROM all_groups WHERE chat_id='-1'").fetchone()[0]
        row = conn.execute("SELECT chat_name, visibility FROM all_groups WHERE chat_id='-1'").fetchone()
        assert count == 1
        assert row == ("Renamed Group", "private")


class TestRegisterChatRemoved:
    def test_removed_group_moves_to_log_and_leaves_all_groups(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        register_chat_added("-1", "My Group", "supergroup", "public", "2026-01-01 00:00:00", db_path=path)
        register_chat_removed("-1", "2026-01-03 00:00:00", db_path=path)
        conn = sqlite3.connect(path)
        assert conn.execute("SELECT COUNT(*) FROM all_groups WHERE chat_id='-1'").fetchone()[0] == 0
        log_row = conn.execute(
            "SELECT chat_id, date_bot_add, date_bot_remove FROM all_chats_bot_log WHERE chat_id='-1'"
        ).fetchone()
        assert log_row == ("-1", "2026-01-01 00:00:00", "2026-01-03 00:00:00")

    def test_removing_a_chat_that_was_never_added_is_a_safe_no_op(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        register_chat_removed("-999", "2026-01-01 00:00:00", db_path=path)  # must not raise
        conn = sqlite3.connect(path)
        assert conn.execute("SELECT COUNT(*) FROM all_chats_bot_log WHERE chat_id='-999'").fetchone()[0] == 0


# ---------------------------------------------------------------------------
# get_feature_limit_for_chat - core per-tier usage-limit lookup, used by
# /shareevent's own limit check. Previously had zero direct test coverage.
# ---------------------------------------------------------------------------

class TestGetFeatureLimitForChat:
    def test_limit_applies_exactly_at_min_tier(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO all_groups (chat_id, type) VALUES ('-1','FREE')")
        conn.commit()
        # shareevent's seeded defaults: min_tier=FREE, limit_count=3
        assert get_feature_limit_for_chat("-1", "shareevent", db_path=path) == 3

    def test_unlimited_above_min_tier(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        from datetime import datetime, timedelta
        end = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO all_groups (chat_id, type, subs_date_end) VALUES ('-2','PRO',?)", (end,))
        conn.commit()
        # PRO is above shareevent's FREE min_tier -> unlimited
        assert get_feature_limit_for_chat("-2", "shareevent", db_path=path) is None

    def test_unknown_feature_key_returns_none(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO all_groups (chat_id, type) VALUES ('-1','FREE')")
        conn.commit()
        assert get_feature_limit_for_chat("-1", "notarealfeature", db_path=path) is None


# ---------------------------------------------------------------------------
# get_display_name - resolves "First Last" for mention links (_mention_link
# in event_engine.py calls this for every rendered post). Previously had
# zero direct test coverage despite affecting every post's display.
# ---------------------------------------------------------------------------

class TestGetDisplayName:
    def _insert_user(self, path, user_id, first_name, last_name):
        conn = sqlite3.connect(path)
        conn.execute(
            "INSERT INTO main_group_users (chat_id, user_id, username, first_name, last_name) VALUES ('-1', ?, 'u', ?, ?)",
            (user_id, first_name, last_name),
        )
        conn.commit()
        conn.close()

    def test_full_name_when_both_first_and_last_on_file(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        self._insert_user(path, "100", "Alice", "Smith")
        assert get_display_name("-1", "100", "fallback", db_path=path) == "Alice Smith"

    def test_first_name_only_when_no_last_name(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        self._insert_user(path, "200", "Bob", None)
        assert get_display_name("-1", "200", "fallback", db_path=path) == "Bob"

    def test_falls_back_when_user_unknown(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        assert get_display_name("-1", "999", "fallback_name", db_path=path) == "fallback_name"

    def test_falls_back_when_user_id_is_none(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        assert get_display_name("-1", None, "fallback_name", db_path=path) == "fallback_name"

    def test_falls_back_when_user_id_is_empty_string(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        assert get_display_name("-1", "", "fallback_name", db_path=path) == "fallback_name"


# ---------------------------------------------------------------------------
# dedupe_waitlist - defensive cleanup for stale duplicate waitlist entries
# (e.g. from before click-time dedup existed, or any other data corruption
# source). PERSON entries (same user_id+chat_id) are deduped to the
# earliest one; GUEST-slot entries are NEVER deduped (multiple queued
# guest slots for the same person are legitimate).
# ---------------------------------------------------------------------------

class TestDedupeWaitlist:
    def test_exact_duplicate_persons_kept_once_earliest(self):
        wl = [
            {"chat_id": "-100", "user_id": "1", "username": "andr", "timestamp": "2026-01-01 00:00:03"},
            {"chat_id": "-100", "user_id": "1", "username": "andr", "timestamp": "2026-01-01 00:00:01"},
            {"chat_id": "-100", "user_id": "1", "username": "andr", "timestamp": "2026-01-01 00:00:02"},
        ]
        result = dedupe_waitlist(wl)
        assert len(result) == 1
        assert result[0]["timestamp"] == "2026-01-01 00:00:01"

    def test_same_person_different_chats_both_kept(self):
        wl = [
            {"chat_id": "-100", "user_id": "1", "username": "andr", "timestamp": "t1"},
            {"chat_id": "-200", "user_id": "1", "username": "andr", "timestamp": "t2"},
        ]
        result = dedupe_waitlist(wl)
        assert len(result) == 2

    def test_multiple_guest_slots_never_deduped(self):
        wl = [
            {"chat_id": "-100", "user_id": "1", "username": "andr", "timestamp": "t1", "is_guest": True},
            {"chat_id": "-100", "user_id": "1", "username": "andr", "timestamp": "t2", "is_guest": True},
            {"chat_id": "-100", "user_id": "1", "username": "andr", "timestamp": "t3", "is_guest": True},
        ]
        result = dedupe_waitlist(wl)
        assert len(result) == 3

    def test_mixed_duplicate_persons_and_legit_guest_slots(self):
        wl = [
            {"chat_id": "-100", "user_id": "1", "username": "andr", "timestamp": "t1"},
            {"chat_id": "-100", "user_id": "1", "username": "andr", "timestamp": "t2"},
            {"chat_id": "-100", "user_id": "1", "username": "andr", "timestamp": "t3", "is_guest": True},
            {"chat_id": "-100", "user_id": "1", "username": "andr", "timestamp": "t4", "is_guest": True},
        ]
        result = dedupe_waitlist(wl)
        assert len(result) == 3  # 1 person + 2 guest slots

    def test_empty_waitlist(self):
        assert dedupe_waitlist([]) == []

    def test_no_duplicates_returns_all_unchanged(self):
        wl = [
            {"chat_id": "-100", "user_id": "1", "username": "andr", "timestamp": "t1"},
            {"chat_id": "-100", "user_id": "2", "username": "bob", "timestamp": "t2"},
        ]
        result = dedupe_waitlist(wl)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# get_shareevent_remaining_for_chat - shareevent's limit is enforced PER
# (hub, target) PAIR, counted across the hub's entire event history (not
# just the active event, since event_shares has UNIQUE(event_id, chat_id)
# so the same event can never re-share to the same target). "remaining"
# is the hub's most-constrained target right now.
# ---------------------------------------------------------------------------

class TestGetShareeventRemainingForChat:
    def test_no_events_ever_returns_full_limit(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        limit, remaining = get_shareevent_remaining_for_chat("-100", db_path=path)
        assert (limit, remaining) == (3, 3)

    def test_active_event_never_shared_returns_full_limit(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        conn = sqlite3.connect(path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-100','1','Party','👍','❌',0,'[]','[]','{}','[]')"""
        )
        conn.commit()
        limit, remaining = get_shareevent_remaining_for_chat("-100", db_path=path)
        assert (limit, remaining) == (3, 3)

    def test_most_constrained_target_drives_the_number(self, tmp_path):
        """Multiple DIFFERENT events (across the hub's history) sharing
        to the same target - childA hits the exact limit, childB has
        more room. Remaining should reflect the tightest one."""
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        conn = sqlite3.connect(path)
        for i in range(1, 4):
            conn.execute(
                f"""INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
                   event_status, going_data, notgoing_data, counters_data, kicked_data)
                   VALUES ('ev{i}','-100','{i}','P{i}','👍','❌',2,'[]','[]','{{}}','[]')"""
            )
            conn.execute(
                "INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type) VALUES (?, '-200', ?, '-oc', 'group')",
                (f"ev{i}", str(10 + i)),
            )
        conn.execute(
            "INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type) VALUES ('ev1', '-300', '20', '-oc', 'group')"
        )
        conn.commit()
        limit, remaining = get_shareevent_remaining_for_chat("-100", db_path=path)
        assert (limit, remaining) == (3, 0)

    def test_pro_tier_returns_none_none_unlimited(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        end = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO all_groups (chat_id, type, subs_date_end) VALUES ('-999','PRO',?)", (end,))
        conn.commit()
        limit, remaining = get_shareevent_remaining_for_chat("-999", db_path=path)
        assert (limit, remaining) == (None, None)

    def test_cleared_limit_zero_returns_none_none_unlimited(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        update_feature_flag("shareevent", "FREE", limit_count=0, db_path=path)
        limit, remaining = get_shareevent_remaining_for_chat("-100", db_path=path)
        assert (limit, remaining) == (None, None)

    def test_remaining_never_goes_negative(self, tmp_path):
        """If usage somehow exceeds the limit (e.g. limit was lowered
        after shares already happened), remaining floors at 0."""
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        conn = sqlite3.connect(path)
        for i in range(1, 4):
            conn.execute(
                f"""INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
                   event_status, going_data, notgoing_data, counters_data, kicked_data)
                   VALUES ('ev{i}','-100','{i}','P{i}','👍','❌',2,'[]','[]','{{}}','[]')"""
            )
            conn.execute(
                "INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type) VALUES (?, '-200', ?, '-oc', 'group')",
                (f"ev{i}", str(10 + i)),
            )
        conn.commit()
        conn.close()
        update_feature_flag("shareevent", "FREE", limit_count=1, db_path=path)
        limit, remaining = get_shareevent_remaining_for_chat("-100", db_path=path)
        assert limit == 1
        assert remaining == 0


class TestMigrateEventToEventUsers:
    """Direct, isolated unit tests for the Variant B migration function -
    previously only covered indirectly through button_handler/editevent/
    notify/refreshusers, each exercising it as a side effect of their own
    logic rather than testing the function's own behavior directly."""

    def _setup_event(self, path, going="[]", not_going="[]", counters="{}", kicked="[]"):
        conn = sqlite3.connect(path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-100','1','Party','👍','❌',0,?,?,?,?)""",
            (going, not_going, counters, kicked),
        )
        conn.commit()
        return conn

    def test_migrates_going_notgoing_and_guests(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        conn = self._setup_event(
            path,
            going='["alice (1)", "bob (2)"]',
            not_going='["carol (3)"]',
            counters='{"alice": 2}',
        )
        cursor = conn.cursor()
        migrate_event_to_event_users(cursor, "ev1", "-100", ["alice (1)", "bob (2)"], ["carol (3)"], {"alice": 2}, [])
        conn.commit()

        rows = {r[0]: r for r in conn.execute(
            "SELECT username, status, guests, user_id FROM event_users WHERE event_id='ev1' AND chat_id='-100'"
        ).fetchall()}
        assert rows["alice"][1] == "going" and rows["alice"][2] == 2
        assert rows["bob"][1] == "going" and rows["bob"][2] == 0
        assert rows["carol"][1] == "notgoing"
        conn.close()

    def test_kicked_status_takes_priority_over_going(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        conn = self._setup_event(path, going='["dave (4)"]')
        cursor = conn.cursor()
        migrate_event_to_event_users(cursor, "ev1", "-100", ["dave (4)"], [], {}, ["dave"])
        conn.commit()

        row = conn.execute("SELECT status FROM event_users WHERE event_id='ev1' AND username='dave'").fetchone()
        assert row == ("kicked",)
        conn.close()

    def test_guest_only_entry_gets_notselected_status(self, tmp_path):
        """Someone with a counters entry but no going/notgoing status of
        their own - only migratable if resolvable via main_group_users."""
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        conn = self._setup_event(path, counters='{"erin": 3}')
        conn.execute(
            "INSERT INTO main_group_users (chat_id, username, user_id, status) VALUES ('-100', 'erin', '5', 'active')"
        )
        conn.commit()
        cursor = conn.cursor()
        migrate_event_to_event_users(cursor, "ev1", "-100", [], [], {"erin": 3}, [])
        conn.commit()

        row = conn.execute(
            "SELECT status, guests, user_id FROM event_users WHERE event_id='ev1' AND username='erin'"
        ).fetchone()
        assert row == ("notselected", 3, "5")
        conn.close()

    def test_unresolvable_guest_only_entry_is_skipped(self, tmp_path):
        """No main_group_users row at all - genuinely can't be migrated
        (no fallback id makes sense for a PURE guest-count entry, unlike
        a going/notgoing entry which at least has a username to fall
        back on)."""
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        conn = self._setup_event(path, counters='{"frank": 1}')
        cursor = conn.cursor()
        migrate_event_to_event_users(cursor, "ev1", "-100", [], [], {"frank": 1}, [])
        conn.commit()

        row = conn.execute("SELECT * FROM event_users WHERE event_id='ev1' AND username='frank'").fetchone()
        assert row is None
        conn.close()

    def test_legacy_unresolvable_going_entry_uses_username_as_fallback_id(self, tmp_path):
        """The old '(no_id_in_main_group)' marker (or any bare entry with
        no parens at all) migrates using the username itself as a
        fallback identifying key - stays visible/surfaceable rather than
        silently vanishing."""
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        conn = self._setup_event(path, going='["ghost (no_id_in_main_group)"]')
        cursor = conn.cursor()
        migrate_event_to_event_users(cursor, "ev1", "-100", ["ghost (no_id_in_main_group)"], [], {}, [])
        conn.commit()

        row = conn.execute(
            "SELECT status, user_id FROM event_users WHERE event_id='ev1' AND username='ghost'"
        ).fetchone()
        assert row == ("going", "ghost")
        conn.close()

    def test_already_migrated_event_is_a_safe_noop(self, tmp_path):
        """Calling migration twice (e.g. two clicks in a row) must not
        duplicate or corrupt already-migrated data."""
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        conn = self._setup_event(path, going='["alice (1)"]')
        cursor = conn.cursor()
        migrate_event_to_event_users(cursor, "ev1", "-100", ["alice (1)"], [], {}, [])
        conn.commit()

        # Simulate real-world drift: after migration, going_data is frozen
        # and stops reflecting reality, but calling migrate again with the
        # SAME (now-stale) input must not touch the already-migrated row.
        conn.execute("UPDATE event_users SET guests = 99 WHERE event_id='ev1' AND username='alice'")
        conn.commit()
        migrate_event_to_event_users(cursor, "ev1", "-100", ["alice (1)"], [], {}, [])
        conn.commit()

        row = conn.execute("SELECT guests FROM event_users WHERE event_id='ev1' AND username='alice'").fetchone()
        assert row == (99,), "second call must be a no-op, not overwrite real event_users state"
        conn.close()

    def test_empty_event_migrates_to_nothing(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        conn = self._setup_event(path)
        cursor = conn.cursor()
        migrate_event_to_event_users(cursor, "ev1", "-100", [], [], {}, [])
        conn.commit()

        rows = conn.execute("SELECT * FROM event_users WHERE event_id='ev1'").fetchall()
        assert rows == []
        conn.close()


class TestBotLockState:
    """Direct unit tests for the /lockbot feature's persistence layer."""

    def test_defaults_to_unlocked(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        import db
        db.DB_PATH = path
        assert is_bot_locked() is False

    def test_lock_and_unlock_round_trip(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        import db
        db.DB_PATH = path

        set_bot_locked(True)
        assert is_bot_locked() is True

        set_bot_locked(False)
        assert is_bot_locked() is False

    def test_lock_state_survives_reinit(self, tmp_path):
        """init_db() (e.g. on bot restart) must not reset an already-set
        lock state - 'INSERT OR IGNORE' should leave an existing row alone."""
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        import db
        db.DB_PATH = path
        set_bot_locked(True)

        init_db(db_path=path)  # simulates a restart
        assert is_bot_locked() is True


class TestEnsureEventMigrated:
    """Direct unit tests for the new ensure_event_migrated wrapper -
    extracted from a "SELECT the 4 frozen columns, json.loads each,
    call migrate_event_to_event_users" sequence that was repeated
    identically across 5 call sites in handlers.py."""

    def _setup_event(self, path, going="[]", not_going="[]", counters="{}", kicked="[]"):
        conn = sqlite3.connect(path)
        conn.execute(
            """INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
               event_status, going_data, notgoing_data, counters_data, kicked_data)
               VALUES ('ev1','-100','1','Party','👍','❌',0,?,?,?,?)""",
            (going, not_going, counters, kicked),
        )
        conn.commit()
        return conn

    def test_migrates_correctly_from_frozen_columns(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        conn = self._setup_event(path, going='["alice (1)"]', counters='{"alice": 2}')
        cursor = conn.cursor()

        ensure_event_migrated(cursor, "ev1", "-100")
        conn.commit()

        row = conn.execute(
            "SELECT status, guests FROM event_users WHERE event_id='ev1' AND username='alice'"
        ).fetchone()
        assert row == ("going", 2)
        conn.close()

    def test_nonexistent_event_is_a_safe_noop(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        conn = sqlite3.connect(path)
        cursor = conn.cursor()

        # Must not raise for an event_id that doesn't exist at all
        ensure_event_migrated(cursor, "nonexistent_ev", "-100")

        rows = conn.execute("SELECT * FROM event_users WHERE event_id='nonexistent_ev'").fetchall()
        assert rows == []
        conn.close()

    def test_already_migrated_event_is_a_noop(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        conn = self._setup_event(path, going='["alice (1)"]')
        cursor = conn.cursor()
        ensure_event_migrated(cursor, "ev1", "-100")
        conn.commit()

        conn.execute("UPDATE event_users SET guests = 99 WHERE event_id='ev1' AND username='alice'")
        conn.commit()
        ensure_event_migrated(cursor, "ev1", "-100")
        conn.commit()

        row = conn.execute("SELECT guests FROM event_users WHERE event_id='ev1' AND username='alice'").fetchone()
        assert row == (99,), "second call must not overwrite already-migrated state"
        conn.close()
