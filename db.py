import sqlite3
import json
import asyncio
import functools
from contextlib import contextmanager

DB_PATH = "database.db"


@contextmanager
def get_connection(db_path: str = None):
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

    db_path defaults to None and is resolved to the CURRENT value of the
    module-level DB_PATH at call time, not at function-definition time -
    using `db_path: str = DB_PATH` as the parameter default would bind
    whatever DB_PATH held when this module was first imported, silently
    ignoring any later `db.DB_PATH = ...` / monkeypatch (exactly what every
    pytest fixture in this project does to isolate tests in a temp file).
    """
    if db_path is None:
        db_path = DB_PATH
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

    # Second rename: 'main_chat_settings' -> 'all_groups'. Same reasoning as
    # above - must run before CREATE TABLE IF NOT EXISTS, since 'all_groups'
    # is a brand new name that "IF NOT EXISTS" would happily create empty
    # alongside the still-existing old table.
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='main_chat_settings'")
    has_main_chat_settings = cursor.fetchone() is not None
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='all_groups'")
    has_all_groups = cursor.fetchone() is not None
    if has_main_chat_settings and not has_all_groups:
        cursor.execute("ALTER TABLE main_chat_settings RENAME TO all_groups")

    # Per-hub settings: which Google Sheet this hub writes to (by spreadsheet
    # ID, not name - names aren't guaranteed unique across different Google
    # accounts/files, only the ID is), subscription tier, subscription
    # window, whether the group is public/private, and when the bot was
    # added to it.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS all_groups (
            chat_id TEXT PRIMARY KEY,
            chat_name TEXT DEFAULT NULL,
            type TEXT DEFAULT 'free',
            sheet_id TEXT UNIQUE,
            sheet_name TEXT DEFAULT NULL,
            subs_date_start TEXT DEFAULT NULL,
            subs_date_end TEXT DEFAULT NULL,
            visibility TEXT DEFAULT NULL,
            date_bot_add TEXT DEFAULT NULL
        )
    """)

    # Every channel the bot has ever been added to (separate from
    # all_groups, since channels don't have a subscription/sheet concept
    # the same way hub groups do - this is purely a presence registry).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS all_channels (
            chat_id TEXT PRIMARY KEY,
            chat_name TEXT DEFAULT NULL,
            visibility TEXT DEFAULT NULL,
            date_bot_add TEXT DEFAULT NULL
        )
    """)

    # Historical log of every group/channel the bot has ever been added to
    # and (if applicable) removed from. A row is inserted here - carrying
    # over that chat's date_bot_add - and the corresponding all_groups/
    # all_channels row is deleted, the moment the bot is removed. Kept as
    # its own append-only table (chat_id is NOT unique here - the same chat
    # could add/remove/re-add the bot multiple times over its history).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS all_chats_bot_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            date_bot_add TEXT DEFAULT NULL,
            date_bot_removed TEXT DEFAULT NULL
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
            first_name TEXT DEFAULT NULL,
            last_name TEXT DEFAULT NULL,
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

    # -1. Add any of all_groups' newer columns if still missing (covers an
    # all_groups that already existed under its current name but predates
    # one of these columns).
    cursor.execute("PRAGMA table_info(all_groups)")
    mcs_cols = [col[1] for col in cursor.fetchall()]
    if "type" not in mcs_cols:
        cursor.execute("ALTER TABLE all_groups ADD COLUMN type TEXT DEFAULT 'free'")
    if "subs_date_start" not in mcs_cols:
        cursor.execute("ALTER TABLE all_groups ADD COLUMN subs_date_start TEXT DEFAULT NULL")
    if "subs_date_end" not in mcs_cols:
        cursor.execute("ALTER TABLE all_groups ADD COLUMN subs_date_end TEXT DEFAULT NULL")
    if "chat_name" not in mcs_cols:
        cursor.execute("ALTER TABLE all_groups ADD COLUMN chat_name TEXT DEFAULT NULL")
    if "sheet_name" not in mcs_cols:
        cursor.execute("ALTER TABLE all_groups ADD COLUMN sheet_name TEXT DEFAULT NULL")
    if "visibility" not in mcs_cols:
        cursor.execute("ALTER TABLE all_groups ADD COLUMN visibility TEXT DEFAULT NULL")
    if "date_bot_add" not in mcs_cols:
        cursor.execute("ALTER TABLE all_groups ADD COLUMN date_bot_add TEXT DEFAULT NULL")

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

    # 2b. Add `first_name`/`last_name` columns to main_group_users if missing
    if "first_name" not in main_group_users_cols:
        cursor.execute("ALTER TABLE main_group_users ADD COLUMN first_name TEXT DEFAULT NULL")
    if "last_name" not in main_group_users_cols:
        cursor.execute("ALTER TABLE main_group_users ADD COLUMN last_name TEXT DEFAULT NULL")

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
               user_id: str = None, first_name: str = None, last_name: str = None,
               db_path: str = None):
    """
    Upserts a single user registration record within the localized chat context.
    Optionally stores the Telegram user_id so refreshusers can verify membership,
    plus first_name/last_name so going/notgoing lists can show a clickable
    "First Last" link (tg://user?id=...) instead of relying on @username,
    which may not exist at all.
    """
    if not username:
        return
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    if user_id is not None:
        cursor.execute("""
            INSERT INTO main_group_users (chat_id, username, user_id, status, first_name, last_name)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, username) DO UPDATE
                SET status = excluded.status,
                    user_id = COALESCE(excluded.user_id, main_group_users.user_id),
                    first_name = COALESCE(excluded.first_name, main_group_users.first_name),
                    last_name = COALESCE(excluded.last_name, main_group_users.last_name)
        """, (str(chat_id), username, str(user_id), status, first_name, last_name))
    else:
        cursor.execute("""
            INSERT INTO main_group_users (chat_id, username, status) VALUES (?, ?, ?)
            ON CONFLICT(chat_id, username) DO UPDATE SET status = excluded.status
        """, (str(chat_id), username, status))
    conn.commit()
    conn.close()


def get_display_name(chat_id: str, user_id: str, fallback: str, db_path: str = None) -> str:
    """
    Returns "First Last" for this user_id if we have it stored (from a
    previous track_user() call with first_name/last_name), trimmed of any
    missing part - e.g. just "First" if there's no last_name on file.
    Falls back to `fallback` (typically the @username or "user<id>") if we
    have no name on file at all for this chat_id/user_id yet.
    """
    if not user_id:
        return fallback
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT first_name, last_name FROM main_group_users WHERE chat_id = ? AND user_id = ? "
        "AND first_name IS NOT NULL LIMIT 1",
        (str(chat_id), str(user_id)),
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return fallback
    first, last = row
    full = " ".join(p for p in (first, last) if p)
    return full or fallback


def register_chat_added(chat_id: str, chat_name: str, chat_type: str, visibility: str,
                         date_bot_add: str, db_path: str = None):
    """
    Called the moment the bot is added to a group/channel (see main.py's
    on_my_chat_member_update). Groups go into all_groups (default type
    'free'), channels go into all_channels - both are simple "the bot is
    currently present here" registries, kept separate since groups have a
    subscription/sheet-binding concept that channels don't.

    chat_type: "channel" for channels, anything else (group/supergroup)
    treated as a group.
    visibility: "public" if the chat has a public @username, "private"
    otherwise (see main.py for how this is determined).
    """
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    if chat_type == "channel":
        cursor.execute("""
            INSERT INTO all_channels (chat_id, chat_name, visibility, date_bot_add)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE
                SET chat_name = excluded.chat_name,
                    visibility = excluded.visibility,
                    date_bot_add = excluded.date_bot_add
        """, (str(chat_id), chat_name, visibility, date_bot_add))
    else:
        cursor.execute("""
            INSERT INTO all_groups (chat_id, chat_name, type, visibility, date_bot_add)
            VALUES (?, ?, 'free', ?, ?)
            ON CONFLICT(chat_id) DO UPDATE
                SET chat_name = excluded.chat_name,
                    visibility = excluded.visibility,
                    date_bot_add = excluded.date_bot_add
        """, (str(chat_id), chat_name, visibility, date_bot_add))
    conn.commit()
    conn.close()


def register_chat_removed(chat_id: str, date_bot_removed: str, db_path: str = None):
    """
    Called the moment the bot is removed from (or leaves) a group/channel.
    Moves that chat's row out of all_groups/all_channels (wherever it was)
    and appends a record to all_chats_bot_log with both the original
    date_bot_add and the new date_bot_removed - the presence registries
    only ever reflect chats the bot is CURRENTLY in, the log is the
    historical trail.
    """
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("SELECT date_bot_add FROM all_groups WHERE chat_id = ?", (str(chat_id),))
    row = cursor.fetchone()
    if row is not None:
        cursor.execute(
            "INSERT INTO all_chats_bot_log (chat_id, date_bot_add, date_bot_removed) VALUES (?, ?, ?)",
            (str(chat_id), row[0], date_bot_removed),
        )
        cursor.execute("DELETE FROM all_groups WHERE chat_id = ?", (str(chat_id),))
        conn.commit()
        conn.close()
        return

    cursor.execute("SELECT date_bot_add FROM all_channels WHERE chat_id = ?", (str(chat_id),))
    row = cursor.fetchone()
    if row is not None:
        cursor.execute(
            "INSERT INTO all_chats_bot_log (chat_id, date_bot_add, date_bot_removed) VALUES (?, ?, ?)",
            (str(chat_id), row[0], date_bot_removed),
        )
        cursor.execute("DELETE FROM all_channels WHERE chat_id = ?", (str(chat_id),))
        conn.commit()

    conn.close()
