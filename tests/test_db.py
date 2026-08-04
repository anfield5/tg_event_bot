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
from db import init_db, track_user


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
        "sub_groups",
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

    def test_sub_groups_columns(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        cols = get_columns(path, "sub_groups")
        for expected in ("chat_id", "owner_chat_id", "alias", "is_monitored", "chat_type", "chat_name"):
            assert expected in cols, f"sub_groups missing '{expected}'"

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
    """Legacy chat_aliases + monitors must merge into a single sub_groups table."""

    def test_merges_alias_only_row(self, tmp_path):
        path = str(tmp_path / "t.db")
        run_sql(path, "CREATE TABLE chat_aliases (chat_id TEXT PRIMARY KEY, alias TEXT UNIQUE, owner_chat_id TEXT)")
        run_sql(path, "INSERT INTO chat_aliases (chat_id, alias, owner_chat_id) VALUES ('-200','downtown','-100')")

        init_db(db_path=path)

        assert "sub_groups" in get_tables(path)
        assert "chat_aliases" not in get_tables(path)
        rows = fetch_all(path, "SELECT chat_id, alias, is_monitored, owner_chat_id FROM sub_groups")
        assert rows == [("-200", "downtown", 0, "-100")]

    def test_merges_monitor_only_row(self, tmp_path):
        path = str(tmp_path / "t.db")
        run_sql(path, "CREATE TABLE monitors (chat_id TEXT PRIMARY KEY, chat_type TEXT, chat_name TEXT, owner_chat_id TEXT)")
        run_sql(path, "INSERT INTO monitors VALUES ('-300','group','Other Group','-100')")

        init_db(db_path=path)

        assert "monitors" not in get_tables(path)
        rows = fetch_all(path, "SELECT chat_id, alias, is_monitored, chat_type, chat_name FROM sub_groups")
        assert rows == [("-300", None, 1, "group", "Other Group")]

    def test_merges_chat_present_in_both_legacy_tables_into_one_row(self, tmp_path):
        """
        A chat that was BOTH aliased and monitored under the same owner must
        become a single sub_groups row with both facts set, not two rows.
        """
        path = str(tmp_path / "t.db")
        run_sql(path, "CREATE TABLE chat_aliases (chat_id TEXT PRIMARY KEY, alias TEXT UNIQUE, owner_chat_id TEXT)")
        run_sql(path, "CREATE TABLE monitors (chat_id TEXT PRIMARY KEY, chat_type TEXT, chat_name TEXT, owner_chat_id TEXT)")
        run_sql(path, "INSERT INTO chat_aliases (chat_id, alias, owner_chat_id) VALUES ('-200','downtown','-100')")
        run_sql(path, "INSERT INTO monitors VALUES ('-200','channel','Downtown Channel','-100')")

        init_db(db_path=path)

        rows = fetch_all(path, "SELECT chat_id, alias, is_monitored, chat_type, chat_name FROM sub_groups")
        assert rows == [("-200", "downtown", 1, "channel", "Downtown Channel")]

    def test_idempotent_after_merge(self, tmp_path):
        path = str(tmp_path / "t.db")
        run_sql(path, "CREATE TABLE chat_aliases (chat_id TEXT PRIMARY KEY, alias TEXT UNIQUE, owner_chat_id TEXT)")
        run_sql(path, "INSERT INTO chat_aliases (chat_id, alias, owner_chat_id) VALUES ('-200','downtown','-100')")
        init_db(db_path=path)
        init_db(db_path=path)  # must not raise or duplicate
        rows = fetch_all(path, "SELECT chat_id, alias FROM sub_groups")
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
