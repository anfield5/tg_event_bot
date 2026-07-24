import sqlite3
import json
import asyncio
import functools
from contextlib import contextmanager

DB_PATH = "database.db"


@contextmanager
def get_connection(db_path: str = DB_PATH):
    """
    Small context-manager wrapper around sqlite3.connect() so call sites can
    write:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(...)
            conn.commit()
    instead of the open/commit/close boilerplate repeated ~28 times across
    handlers.py. Also guarantees the connection is closed even if an
    exception is raised mid-query, which several existing call sites don't
    (a raised exception between `sqlite3.connect()` and `conn.close()`
    currently leaks the connection).
    """
    conn = sqlite3.connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


async def run_db(func, *args, **kwargs):
    """
    Runs a blocking sqlite3 function in a worker thread so it doesn't
    block the asyncio event loop that python-telegram-bot relies on.
    Usage: rows = await run_db(some_sync_function, arg1, arg2)
    """
    loop = asyncio.get_running_loop()
    call = functools.partial(func, *args, **kwargs)
    return await loop.run_in_executor(None, call)


def init_db(db_path: str = DB_PATH):
    """
    Initializes the database schema and performs required migrations.
    Creates tables for chat settings, events, users, shares, and aliases.
    Accepts an optional db_path so tests can use an isolated temp file.
    """
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Storage for mapping chats to specific Google Sheets
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_settings (
            chat_id TEXT PRIMARY KEY,
            sheet_name TEXT
        )
    """)

    # Storage for active voting events within the system
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            chat_id TEXT,
            message_id TEXT,
            name TEXT,
            going_icon TEXT,
            notgoing_icon TEXT,
            is_open INTEGER,
            going_data TEXT,
            notgoing_data TEXT,
            counters_data TEXT,
            event_date TEXT DEFAULT NULL,
            is_cancelled INTEGER DEFAULT 0,
            kicked_data TEXT DEFAULT '[]'
        )
    """)

    # Migration: rename legacy 'chat_users' table to 'main_group_users' if a
    # bot upgrading from before this rename still has the old table (and the
    # new one doesn't exist yet) - this must run BEFORE the CREATE TABLE IF
    # NOT EXISTS below, otherwise that would create an empty main_group_users
    # and silently orphan all the old data.
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='chat_users'")
    has_legacy_table = cursor.fetchone() is not None
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='main_group_users'")
    has_new_table = cursor.fetchone() is not None
    if has_legacy_table and not has_new_table:
        cursor.execute("ALTER TABLE chat_users RENAME TO main_group_users")

    # Main registry of known users across chat environments
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS main_group_users (
            chat_id TEXT,
            username TEXT,
            user_id TEXT DEFAULT NULL,
            status TEXT DEFAULT 'active',
            PRIMARY KEY (chat_id, username)
        )
    """)

    # Distribution index tracking where events were shared
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS event_shares (
            share_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT,
            chat_id TEXT,
            message_id TEXT,
            share_mode TEXT,      -- '-visible', '-onlycount', or '-hidden'
            chat_type TEXT,       -- 'group' or 'channel'
            UNIQUE(event_id, chat_id)
        )
    """)

    # High-performance unique index table for child chat routing aliases.
    # owner_chat_id = the hub group that ran /setalias - aliases are scoped
    # to the hub that created them, so /listalias and /shareevent's alias
    # lookup only ever see aliases belonging to the group they're invoked
    # from, not every hub's aliases across the whole deployment.
    #
    # Uniqueness is per-owner, not global: UNIQUE(owner_chat_id, alias) means
    # two different hubs can both use the alias name "downtown" (pointing at
    # two entirely different chats) without colliding. UNIQUE(owner_chat_id,
    # chat_id) preserves the original "one alias per target per owner" rule.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
            alias TEXT,
            owner_chat_id TEXT DEFAULT NULL,
            UNIQUE(owner_chat_id, alias),
            UNIQUE(owner_chat_id, chat_id)
        )
    """)

    # Per-child-chat participation records (going/notgoing + guest counts)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS event_users (
            event_id TEXT,
            chat_id TEXT,
            user_id TEXT,
            username TEXT,
            status TEXT,
            guests INTEGER DEFAULT 0,
            PRIMARY KEY (event_id, chat_id, user_id)
        )
    """)

    # Monitored groups/channels for global user tracking.
    # owner_chat_id = the hub group that ran /addmonitor - same scoping
    # rationale as chat_aliases.owner_chat_id above.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS monitors (
            chat_id TEXT PRIMARY KEY,
            chat_type TEXT,
            chat_name TEXT,
            owner_chat_id TEXT DEFAULT NULL
        )
    """)

    # ── Migrations ────────────────────────────────────────────────────────────

    # 0. Add `owner_chat_id` to chat_aliases/monitors if missing (pre-existing
    # rows have no recorded owner - see the "OR owner_chat_id IS NULL"
    # transitional clause used in every scoped query in handlers.py, which
    # keeps such legacy rows visible everywhere until they're naturally
    # re-created/re-claimed under the new per-hub scoping).
    #
    # 0b. Rebuild chat_aliases if it still has the OLD globally-unique
    # 'alias' column (single-column UNIQUE, and 'chat_id' as PRIMARY KEY) -
    # SQLite has no ALTER TABLE support for changing/dropping a UNIQUE
    # constraint, so this requires a full table rebuild. Detects the legacy
    # shape by checking for chat_id being the table's primary key (only true
    # in the old schema; the new schema uses an autoincrement 'id' instead).
    cursor.execute("PRAGMA table_info(chat_aliases)")
    alias_table_cols = cursor.fetchall()
    chat_id_is_pk = any(col[1] == "chat_id" and col[5] == 1 for col in alias_table_cols)
    if chat_id_is_pk:
        cursor.execute("ALTER TABLE chat_aliases RENAME TO chat_aliases_legacy")
        cursor.execute("""
            CREATE TABLE chat_aliases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT,
                alias TEXT,
                owner_chat_id TEXT DEFAULT NULL,
                UNIQUE(owner_chat_id, alias),
                UNIQUE(owner_chat_id, chat_id)
            )
        """)
        cursor.execute("PRAGMA table_info(chat_aliases_legacy)")
        legacy_had_owner_col = "owner_chat_id" in [col[1] for col in cursor.fetchall()]
        if legacy_had_owner_col:
            cursor.execute("""
                INSERT INTO chat_aliases (chat_id, alias, owner_chat_id)
                SELECT chat_id, alias, owner_chat_id FROM chat_aliases_legacy
            """)
        else:
            # Truly original schema, from before owner_chat_id existed at all -
            # every row becomes an unowned (NULL) legacy row, same as the
            # transitional handling everywhere else in this migration.
            cursor.execute("""
                INSERT INTO chat_aliases (chat_id, alias, owner_chat_id)
                SELECT chat_id, alias, NULL FROM chat_aliases_legacy
            """)
        cursor.execute("DROP TABLE chat_aliases_legacy")

    cursor.execute("PRAGMA table_info(chat_aliases)")
    if "owner_chat_id" not in [col[1] for col in cursor.fetchall()]:
        cursor.execute("ALTER TABLE chat_aliases ADD COLUMN owner_chat_id TEXT DEFAULT NULL")

    cursor.execute("PRAGMA table_info(monitors)")
    if "owner_chat_id" not in [col[1] for col in cursor.fetchall()]:
        cursor.execute("ALTER TABLE monitors ADD COLUMN owner_chat_id TEXT DEFAULT NULL")

    # 1. Add `status` column to main_group_users if missing (old schema)
    cursor.execute("PRAGMA table_info(main_group_users)")
    main_group_users_cols = [col[1] for col in cursor.fetchall()]
    if "status" not in main_group_users_cols:
        cursor.execute("ALTER TABLE main_group_users ADD COLUMN status TEXT DEFAULT 'active'")

    # 2. Add `user_id` column to main_group_users if missing
    if "user_id" not in main_group_users_cols:
        cursor.execute("ALTER TABLE main_group_users ADD COLUMN user_id TEXT DEFAULT NULL")

    # 3. Add `event_date` column to events if missing
    cursor.execute("PRAGMA table_info(events)")
    events_cols = [col[1] for col in cursor.fetchall()]
    if "event_date" not in events_cols:
        cursor.execute("ALTER TABLE events ADD COLUMN event_date TEXT DEFAULT NULL")

    # 3b. Add `is_cancelled` column to events if missing
    if "is_cancelled" not in events_cols:
        cursor.execute("ALTER TABLE events ADD COLUMN is_cancelled INTEGER DEFAULT 0")

    # 3c. Add `kicked_data` column to events if missing (task #2: tracks
    # master-hub users who were Kicked during verification, so the Return
    # button still shows even when they have 0 guests left - previously
    # such a person vanished from the keyboard entirely once kicked, since
    # they were neither in the going list nor the guest counters.)
    if "kicked_data" not in events_cols:
        cursor.execute("ALTER TABLE events ADD COLUMN kicked_data TEXT DEFAULT '[]'")

    # 4. Rename legacy status value 'frozen' → 'passive'
    cursor.execute("UPDATE main_group_users SET status = 'passive' WHERE status = 'frozen'")

    conn.commit()
    conn.close()


def delete_tracked_user(chat_id: str, user_id: str = None, username: str = None,
                         db_path: str = None):
    """
    Removes a user's row from main_group_users entirely (as opposed to
    track_user(), which only upserts/changes status) - used when
    ChatMemberHandler confirms someone has left/been kicked, so they
    immediately disappear from /listusers instead of lingering as 'passive'.

    Prefers matching by user_id (stable across username changes); falls
    back to matching by username if no user_id is available. Does nothing
    if neither is provided.
    """
    if db_path is None:
        db_path = DB_PATH
    if user_id is None and username is None:
        return
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    if user_id is not None:
        cursor.execute(
            "DELETE FROM main_group_users WHERE chat_id = ? AND user_id = ?",
            (str(chat_id), str(user_id)),
        )
    else:
        cursor.execute(
            "DELETE FROM main_group_users WHERE chat_id = ? AND username = ?",
            (str(chat_id), username),
        )
    conn.commit()
    conn.close()


def track_user(chat_id: str, username: str, status: str = "active",
               user_id: str = None, db_path: str = None):
    """
    Upserts a single user registration record within the localized chat context.
    Optionally stores the Telegram user_id so refreshusers can verify membership.
    """
    if not username:
        return
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    if user_id is not None:
        cursor.execute("""
            INSERT INTO main_group_users (chat_id, username, user_id, status) VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id, username) DO UPDATE
                SET status = excluded.status,
                    user_id = COALESCE(excluded.user_id, main_group_users.user_id)
        """, (str(chat_id), username, str(user_id), status))
    else:
        cursor.execute("""
            INSERT INTO main_group_users (chat_id, username, status) VALUES (?, ?, ?)
            ON CONFLICT(chat_id, username) DO UPDATE SET status = excluded.status
        """, (str(chat_id), username, status))
    conn.commit()
    conn.close()
