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

    # Migration: rename legacy 'chat_settings' -> 'main_chat_settings' if the
    # old table exists and the new one doesn't. This MUST run before the
    # CREATE TABLE IF NOT EXISTS below - unlike a same-named-table migration
    # (e.g. events, where "IF NOT EXISTS" naturally no-ops against the
    # pre-existing table), main_chat_settings is a NEW name, so "IF NOT
    # EXISTS" would happily create a fresh EMPTY table alongside the still-
    # existing old one, and this rename would then find "the new table
    # already exists" and skip itself, orphaning all the old data.
    #
    # sheet_name (a title, not guaranteed unique across different Google
    # accounts) becomes sheet_id (the spreadsheet's actual ID, which IS
    # globally unique) - existing values get carried over into sheet_id
    # as-is; they'll need updating to real spreadsheet IDs by whoever
    # manages each hub's binding, since a title can't be mechanically
    # converted to an ID.
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='chat_settings'")
    has_legacy_chat_settings = cursor.fetchone() is not None
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='main_chat_settings'")
    has_new_chat_settings = cursor.fetchone() is not None
    if has_legacy_chat_settings and not has_new_chat_settings:
        cursor.execute("ALTER TABLE chat_settings RENAME TO main_chat_settings")
        cursor.execute("ALTER TABLE main_chat_settings RENAME COLUMN sheet_name TO sheet_id")
        cursor.execute("ALTER TABLE main_chat_settings ADD COLUMN type TEXT DEFAULT 'free'")
        cursor.execute("ALTER TABLE main_chat_settings ADD COLUMN subs_date_start TEXT DEFAULT NULL")
        cursor.execute("ALTER TABLE main_chat_settings ADD COLUMN subs_date_end TEXT DEFAULT NULL")

    # Per-hub settings: which Google Sheet this hub writes to (by spreadsheet
    # ID, not name - names aren't guaranteed unique across different Google
    # accounts/files, only the ID is), subscription tier, and subscription
    # window.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS main_chat_settings (
            chat_id TEXT PRIMARY KEY,
            chat_name TEXT DEFAULT NULL,
            type TEXT DEFAULT 'free',
            sheet_id TEXT UNIQUE,
            subs_date_start TEXT DEFAULT NULL,
            subs_date_end TEXT DEFAULT NULL
        )
    """)

    # Storage for active voting events within the system.
    # event_status: -1 canceled / 0 open / 1 verification / 2 closed
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            chat_id TEXT,
            message_id TEXT,
            name TEXT,
            going_icon TEXT,
            notgoing_icon TEXT,
            event_status INTEGER DEFAULT 0,
            going_data TEXT,
            notgoing_data TEXT,
            counters_data TEXT,
            event_date TEXT DEFAULT NULL,
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

    # Migration: merge legacy chat_aliases + monitors into sub_groups, if
    # sub_groups doesn't exist yet and at least one of the two legacy tables
    # does. Same "must run before CREATE TABLE IF NOT EXISTS" reasoning as
    # main_chat_settings above - sub_groups is a brand new table name.
    # A chat present in BOTH legacy tables under the same owner becomes ONE
    # sub_groups row with both alias and is_monitored set, instead of two
    # disconnected facts.
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sub_groups'")
    has_sub_groups = cursor.fetchone() is not None
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='chat_aliases'")
    has_chat_aliases = cursor.fetchone() is not None
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='monitors'")
    has_monitors = cursor.fetchone() is not None

    if not has_sub_groups and (has_chat_aliases or has_monitors):
        cursor.execute("""
            CREATE TABLE sub_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT,
                owner_chat_id TEXT DEFAULT NULL,
                alias TEXT DEFAULT NULL,
                is_monitored INTEGER DEFAULT 0,
                chat_type TEXT DEFAULT NULL,
                chat_name TEXT DEFAULT NULL,
                UNIQUE(owner_chat_id, alias),
                UNIQUE(owner_chat_id, chat_id)
            )
        """)

        if has_chat_aliases:
            cursor.execute("PRAGMA table_info(chat_aliases)")
            alias_has_owner = "owner_chat_id" in [c[1] for c in cursor.fetchall()]
            if alias_has_owner:
                cursor.execute("SELECT chat_id, alias, owner_chat_id FROM chat_aliases")
            else:
                cursor.execute("SELECT chat_id, alias, NULL FROM chat_aliases")
            for chat_id, alias, owner_chat_id in cursor.fetchall():
                cursor.execute(
                    "INSERT INTO sub_groups (chat_id, owner_chat_id, alias) VALUES (?, ?, ?)",
                    (chat_id, owner_chat_id, alias),
                )

        if has_monitors:
            cursor.execute("PRAGMA table_info(monitors)")
            monitors_has_owner = "owner_chat_id" in [c[1] for c in cursor.fetchall()]
            if monitors_has_owner:
                cursor.execute("SELECT chat_id, chat_type, chat_name, owner_chat_id FROM monitors")
            else:
                cursor.execute("SELECT chat_id, chat_type, chat_name, NULL FROM monitors")
            for chat_id, chat_type, chat_name, owner_chat_id in cursor.fetchall():
                # Match NULL-to-NULL correctly (SQL '=' never matches NULL).
                cursor.execute(
                    "UPDATE sub_groups SET is_monitored = 1, chat_type = ?, chat_name = ? "
                    "WHERE chat_id = ? AND (owner_chat_id IS ?)",
                    (chat_type, chat_name, chat_id, owner_chat_id),
                )
                if cursor.rowcount == 0:
                    cursor.execute(
                        "INSERT INTO sub_groups (chat_id, owner_chat_id, is_monitored, chat_type, chat_name) "
                        "VALUES (?, ?, 1, ?, ?)",
                        (chat_id, owner_chat_id, chat_type, chat_name),
                    )

        if has_chat_aliases:
            cursor.execute("DROP TABLE chat_aliases")
        if has_monitors:
            cursor.execute("DROP TABLE monitors")

    # Every OTHER chat a hub has a relationship with: an alias target, a
    # monitored group/channel, or both at once. Replaces the old separate
    # chat_aliases/monitors tables, which described the same underlying
    # relationship ("this hub relates to that chat") in two disconnected
    # places - a chat could be aliased in one table and monitored in the
    # other with no link between the two facts.
    #
    # owner_chat_id = the hub group that ran /setalias or /addmonitor -
    # scoped per-owner, not global: UNIQUE(owner_chat_id, alias) means two
    # different hubs can both use the alias name "downtown" (pointing at two
    # entirely different chats) without colliding. UNIQUE(owner_chat_id,
    # chat_id) means one row per (hub, target chat) pair - a chat can be
    # aliased, monitored, or both, but always through the same single row.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sub_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT,
            owner_chat_id TEXT DEFAULT NULL,
            alias TEXT DEFAULT NULL,
            is_monitored INTEGER DEFAULT 0,
            chat_type TEXT DEFAULT NULL,
            chat_name TEXT DEFAULT NULL,
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

    # ── Migrations ────────────────────────────────────────────────────────────

    # -1. Add any of main_chat_settings' newer columns if still missing
    # (covers a main_chat_settings that already existed under its new name
    # but predates one of these columns).
    cursor.execute("PRAGMA table_info(main_chat_settings)")
    mcs_cols = [col[1] for col in cursor.fetchall()]
    if "type" not in mcs_cols:
        cursor.execute("ALTER TABLE main_chat_settings ADD COLUMN type TEXT DEFAULT 'free'")
    if "subs_date_start" not in mcs_cols:
        cursor.execute("ALTER TABLE main_chat_settings ADD COLUMN subs_date_start TEXT DEFAULT NULL")
    if "subs_date_end" not in mcs_cols:
        cursor.execute("ALTER TABLE main_chat_settings ADD COLUMN subs_date_end TEXT DEFAULT NULL")
    if "chat_name" not in mcs_cols:
        cursor.execute("ALTER TABLE main_chat_settings ADD COLUMN chat_name TEXT DEFAULT NULL")

    # 0b. Add `chat_type`/`chat_name` to sub_groups if it exists from an
    # earlier version of this same migration that predates them.
    cursor.execute("PRAGMA table_info(sub_groups)")
    sg_cols = [c[1] for c in cursor.fetchall()]
    if "chat_type" not in sg_cols:
        cursor.execute("ALTER TABLE sub_groups ADD COLUMN chat_type TEXT DEFAULT NULL")
    if "chat_name" not in sg_cols:
        cursor.execute("ALTER TABLE sub_groups ADD COLUMN chat_name TEXT DEFAULT NULL")

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

    # 3c. Add `kicked_data` column to events if missing (tracks master-hub
    # users who were Kicked during verification, so the Return button still
    # shows even when they have 0 guests left - previously such a person
    # vanished from the keyboard entirely once kicked, since they were
    # neither in the going list nor the guest counters.)
    if "kicked_data" not in events_cols:
        cursor.execute("ALTER TABLE events ADD COLUMN kicked_data TEXT DEFAULT '[]'")

    # 3d. Rebuild events if it still has the old separate is_open/is_cancelled
    # columns instead of the unified event_status - SQLite can't merge two
    # columns into one via ALTER TABLE, so this requires a full table
    # rebuild, translating values as it copies:
    #   is_cancelled=1        -> event_status = -1
    #   is_open=1 (open)      -> event_status = 0
    #   is_open=2 (verify)    -> event_status = 1
    #   is_open=0 (closed)    -> event_status = 2
    if "is_open" in events_cols:
        has_is_cancelled = "is_cancelled" in events_cols
        cursor.execute("ALTER TABLE events RENAME TO events_legacy")
        cursor.execute("""
            CREATE TABLE events (
                event_id TEXT PRIMARY KEY,
                chat_id TEXT,
                message_id TEXT,
                name TEXT,
                going_icon TEXT,
                notgoing_icon TEXT,
                event_status INTEGER DEFAULT 0,
                going_data TEXT,
                notgoing_data TEXT,
                counters_data TEXT,
                event_date TEXT DEFAULT NULL,
                kicked_data TEXT DEFAULT '[]'
            )
        """)
        is_cancelled_expr = "is_cancelled" if has_is_cancelled else "0"
        cursor.execute(f"""
            INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon,
                                 event_status, going_data, notgoing_data, counters_data, event_date, kicked_data)
            SELECT event_id, chat_id, message_id, name, going_icon, notgoing_icon,
                   CASE
                       WHEN {is_cancelled_expr} = 1 THEN -1
                       WHEN is_open = 1 THEN 0
                       WHEN is_open = 2 THEN 1
                       WHEN is_open = 0 THEN 2
                       ELSE 0
                   END,
                   going_data, notgoing_data, counters_data, event_date, kicked_data
            FROM events_legacy
        """)
        cursor.execute("DROP TABLE events_legacy")

    # 4. Rename legacy status value 'frozen' → 'passive'
    cursor.execute("UPDATE main_group_users SET status = 'passive' WHERE status = 'frozen'")

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
