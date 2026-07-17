"""
Tests for db.py
───────────────
Uses an isolated temp SQLite file (via the `db_path` fixture from conftest.py)
so no test ever touches the real `database.db`.

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


# ---------------------------------------------------------------------------
# init_db
# ---------------------------------------------------------------------------

class TestInitDb:
    """init_db() must create all required tables and add migration columns."""

    EXPECTED_TABLES = {
        "chat_settings",
        "events",
        "chat_users",
        "event_shares",
        "chat_aliases",
        "event_users",
    }

    def test_all_tables_created(self, tmp_path):
        # Fresh database — every table must be present after init
        path = str(tmp_path / "fresh.db")
        init_db(db_path=path)
        assert self.EXPECTED_TABLES.issubset(get_tables(path))

    def test_events_has_event_date_column(self, tmp_path):
        # Migration: event_date column must exist on `events`
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        assert "event_date" in get_columns(path, "events")

    def test_chat_users_has_user_id_column(self, tmp_path):
        # Migration: user_id column must exist on `chat_users`
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        assert "user_id" in get_columns(path, "chat_users")

    def test_chat_users_has_status_column(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        assert "status" in get_columns(path, "chat_users")

    def test_idempotent_second_call(self, tmp_path):
        # Calling init_db twice must not raise (IF NOT EXISTS guards)
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        init_db(db_path=path)   # second call — must not fail
        assert self.EXPECTED_TABLES.issubset(get_tables(path))

    def test_frozen_status_migrated_to_passive(self, tmp_path):
        # Legacy records with status='frozen' must be updated to 'passive'
        path = str(tmp_path / "t.db")
        # Insert a 'frozen' user before running init_db (simulate old schema)
        conn   = sqlite3.connect(path)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS chat_users (
                chat_id TEXT, username TEXT, status TEXT DEFAULT 'active',
                PRIMARY KEY (chat_id, username)
            )
        """)
        cursor.execute(
            "INSERT INTO chat_users (chat_id, username, status) VALUES ('c1','alice','frozen')"
        )
        conn.commit()
        conn.close()

        init_db(db_path=path)

        rows = fetch_all(path, "SELECT status FROM chat_users WHERE username = 'alice'")
        assert rows[0][0] == "passive", "Legacy 'frozen' status should be migrated to 'passive'"


# ---------------------------------------------------------------------------
# track_user
# ---------------------------------------------------------------------------

class TestTrackUser:
    """track_user() upserts a user record into chat_users."""

    def test_inserts_new_user(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        track_user("chat1", "alice", "active", db_path=path)
        rows = fetch_all(path, "SELECT username, status FROM chat_users WHERE chat_id='chat1'")
        assert len(rows) == 1
        assert rows[0] == ("alice", "active")

    def test_updates_existing_status(self, tmp_path):
        # Insert active first, then update to passive
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        track_user("chat1", "alice", "active",  db_path=path)
        track_user("chat1", "alice", "passive", db_path=path)
        rows = fetch_all(path, "SELECT status FROM chat_users WHERE chat_id='chat1' AND username='alice'")
        assert rows[0][0] == "passive"

    def test_empty_username_is_ignored(self, tmp_path):
        # track_user must silently return without inserting anything
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        track_user("chat1", "", "active", db_path=path)
        track_user("chat1", None, "active", db_path=path)
        rows = fetch_all(path, "SELECT * FROM chat_users")
        assert len(rows) == 0

    def test_stores_user_id(self, tmp_path):
        # When user_id is provided it must be stored in the column
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        track_user("chat1", "bob", "active", user_id="99988", db_path=path)
        rows = fetch_all(path, "SELECT user_id FROM chat_users WHERE username='bob'")
        assert rows[0][0] == "99988"

    def test_user_id_preserved_on_status_update(self, tmp_path):
        # If we update status without passing user_id, the stored user_id must remain
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        track_user("chat1", "bob", "active",  user_id="99988", db_path=path)
        track_user("chat1", "bob", "passive",                   db_path=path)  # no user_id
        rows = fetch_all(path, "SELECT user_id FROM chat_users WHERE username='bob'")
        assert rows[0][0] == "99988", "user_id must be preserved when not explicitly passed"

    def test_multiple_users_different_chats(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        track_user("chat1", "alice", "active",  db_path=path)
        track_user("chat2", "alice", "passive", db_path=path)
        rows = fetch_all(path, "SELECT chat_id, status FROM chat_users WHERE username='alice' ORDER BY chat_id")
        assert rows == [("chat1", "active"), ("chat2", "passive")]

    def test_default_status_is_active(self, tmp_path):
        path = str(tmp_path / "t.db")
        init_db(db_path=path)
        track_user("chat1", "carol", db_path=path)  # no explicit status
        rows = fetch_all(path, "SELECT status FROM chat_users WHERE username='carol'")
        assert rows[0][0] == "active"
