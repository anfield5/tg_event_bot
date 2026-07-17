import sqlite3
import json
import asyncio
import functools

DB_PATH = "database.db"


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
            event_date TEXT DEFAULT NULL
        )
    """)

    # Main registry of known users across chat environments
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_users (
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

    # High-performance unique index table for child chat routing aliases
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_aliases (
            chat_id TEXT PRIMARY KEY,
            alias TEXT UNIQUE
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

    # 1. Add `status` column to chat_users if missing (old schema)
    cursor.execute("PRAGMA table_info(chat_users)")
    chat_users_cols = [col[1] for col in cursor.fetchall()]
    if "status" not in chat_users_cols:
        cursor.execute("ALTER TABLE chat_users ADD COLUMN status TEXT DEFAULT 'active'")

    # 2. Add `user_id` column to chat_users if missing
    if "user_id" not in chat_users_cols:
        cursor.execute("ALTER TABLE chat_users ADD COLUMN user_id TEXT DEFAULT NULL")

    # 3. Add `event_date` column to events if missing
    cursor.execute("PRAGMA table_info(events)")
    events_cols = [col[1] for col in cursor.fetchall()]
    if "event_date" not in events_cols:
        cursor.execute("ALTER TABLE events ADD COLUMN event_date TEXT DEFAULT NULL")

    # 4. Rename legacy status value 'frozen' → 'passive'
    cursor.execute("UPDATE chat_users SET status = 'passive' WHERE status = 'frozen'")

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
            INSERT INTO chat_users (chat_id, username, user_id, status) VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id, username) DO UPDATE
                SET status = excluded.status,
                    user_id = COALESCE(excluded.user_id, chat_users.user_id)
        """, (str(chat_id), username, str(user_id), status))
    else:
        cursor.execute("""
            INSERT INTO chat_users (chat_id, username, status) VALUES (?, ?, ?)
            ON CONFLICT(chat_id, username) DO UPDATE SET status = excluded.status
        """, (str(chat_id), username, status))
    conn.commit()
    conn.close()
