import sqlite3
import json
import asyncio
import functools
from datetime import datetime
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


def _seed_feature_flags(cursor):
    """
    Populates feature_flags with the current, actually-audited access tier
    for every gated feature in the codebase (checked against every
    is_premium()/require_premium() and OWNER_USER_IDS check as of when this
    was written - see subscription.py, aliases.py, monitors.py, handlers.py).

    INSERT OR IGNORE: only fills in rows that don't already exist, so
    re-running init_db() never clobbers a flag that's since been changed
    by hand or by a future admin command.
    """
    seed_rows = [
        ("newevent", "/newevent (create a new event)", "FREE", None,
         "Creates a new event with optional custom Going/Not Going icons and a date."),
        ("editevent", "/editevent (edit the active event)", "FREE", None,
         "Edits the name/date/icons of the currently active event."),
        ("event_limit", "-limit on /newevent and /editevent (Waitlist capacity)", "PRO", None,
         "Whether a hub can cap an event's total headcount and configure Waitlist visibility "
         "with -limit N [visible|hidden|onlycount]. When gated off, the flag is rejected with "
         "a clear error instead of being silently ignored."),
        ("user_management", "User Management (/adduser, /listusers, /updateuser, /notify, /refreshusers)", "FREE", None,
         "Tracking, notifying, and syncing the roster of users for THIS group - available to every group regardless of tier."),
        ("shareevent", "/shareevent (per target group/channel)", "FREE", 3,
         "Sharing an event to a child group/channel. limit_count caps how many distinct events can be "
         "shared to the same target - only applies while min_tier is FREE (a PRO/ADMIN-gated hub is always "
         "unlimited). Change either via /updatefeature."),
        ("verification", "Verification step before closing an event", "FREE", None,
         "The review step (kick/return, +/- guest, Add Extra Member) between OPEN and CLOSED. "
         "If disabled, the OPEN-state button closes the event directly instead of entering review. "
         "Locked in per-event at creation time - changing this later never affects an event already running."),
        ("add_extra_member", "Add Extra Member button (during verification)", "FREE", None,
         "Lets an admin add someone by username during the verification step, without them having clicked Going themselves. "
         "Locked in per-event at creation time, same as verification."),
        ("dm_access", "Run commands via DM with the bot", "PRO", None,
         "Whether commands can be run in a private DM with the bot at all (sticky group selection, "
         "/switchgroup, etc.) - FREE hubs must run commands inside the actual group chat instead. "
         "/start, /help, and /switchgroup itself are never gated by this - only the actual work commands are."),
        ("setsub", "/setsub (manage subscriptions)", "ADMIN", None,
         "Activate, extend, or deactivate PRO for any group. Gated on OWNER_USER_IDS, not chat admin status."),
        ("custom_sheet", "Custom Google Sheet (/setsheet)", "PRO", None,
         "Binds the hub to its own Google Sheet (Users/Events/Actions/EventUsers/UserPresenceLog tabs)."),
        ("monitoring", "Monitoring (/addmonitor, /removemonitor, /listmonitors)", "PRO", None,
         "Marks a child group/channel as included in /refreshusersall."),
        ("refreshusersall", "/refreshusersall (sync every monitored group/channel)", "PRO", None,
         "Requires monitored chats, which can only ever be configured via /addmonitor (itself PRO-only) - so this is functionally inert on FREE regardless."),
        ("aliases", "Aliases (/setalias, /removealias, /listalias)", "PRO", None,
         "Custom short names for child groups/channels, used with /shareevent."),
        ("owner_overview", "/allgroups, /allchannels (view everything the bot is in)", "ADMIN", None,
         "Lists every group/channel the bot is in, paginated, with an optional -pro filter on /allgroups."),
    ]
    seed_rows = [row[:4] + (i,) + row[4:] for i, row in enumerate(seed_rows)]  # insert sort_order before description
    cursor.executemany(
        "INSERT OR IGNORE INTO feature_flags "
        "(feature_key, feature_label, min_tier, limit_count, sort_order, description) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        seed_rows,
    )
    # feature_label/description are developer-owned reference text, not
    # user-configurable (unlike min_tier/limit_count, which set_feature_flag()
    # may have since changed and must never be overwritten here) - always
    # refresh them so a pre-existing database seeded before a wording/scope
    # change (e.g. refreshusersall moving out of event_lifecycle's bundle)
    # picks up the correction too, not just brand-new databases.
    cursor.executemany(
        "UPDATE feature_flags SET feature_label = ?, sort_order = ?, description = ? WHERE feature_key = ?",
        [(label, sort_order, desc, key) for key, label, _tier, _limit, sort_order, desc in seed_rows],
    )
    # Retire feature_keys that no longer exist in the current seed list
    # (e.g. event_lifecycle + updateuser were decomposed into
    # newevent/editevent/user_management) - without this, a pre-existing
    # database would keep the old rows around forever, orphaned and stale.
    current_keys = [row[0] for row in seed_rows]
    placeholders = ",".join("?" * len(current_keys))
    cursor.execute(
        f"DELETE FROM feature_flags WHERE feature_key NOT IN ({placeholders})",
        current_keys,
    )


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
        cursor.execute("ALTER TABLE main_chat_settings ADD COLUMN type TEXT DEFAULT 'FREE'")
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
            type TEXT DEFAULT 'FREE',
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

    # Every command invocation, across every chat - foundation for usage
    # statistics (which groups are actually active, which commands get
    # used). Deliberately NOT tied to any specific command's own logic -
    # see main.py's generic command-logging handler, which populates this
    # for every registered command without needing per-command changes.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS command_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL,
            user_id TEXT DEFAULT NULL,
            command TEXT NOT NULL,
            command_text TEXT DEFAULT NULL,
            timestamp TEXT NOT NULL
        )
    """)

    # The single source of truth for what's available at each subscription
    # tier - FREE / PRO / ADMIN, in that ascending order (ADMIN can do
    # everything PRO can, PRO can do everything FREE can). Mirrored to the
    # Control Sheet's "BOTCONFIG" tab (see sheets.sync_control_sheet_botconfig)
    # every time this table changes - see db.set_feature_flag().
    #
    # limit_count applies ONLY to the feature's own min_tier - any tier
    # ABOVE min_tier is automatically unlimited. E.g. min_tier=FREE,
    # limit_count=3 means FREE is capped at 3, PRO/ADMIN are unlimited by
    # construction (they're above FREE). There is no way to configure an
    # "inversion" (a higher tier ending up more restricted than a lower
    # one) - the model doesn't allow it, so no separate limit exists per
    # tier and no runtime warning is needed for that case anymore.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS feature_flags (
            feature_key TEXT PRIMARY KEY,
            feature_label TEXT NOT NULL,
            min_tier TEXT NOT NULL DEFAULT 'FREE',
            limit_count INTEGER DEFAULT NULL,
            sort_order INTEGER DEFAULT 999,
            description TEXT DEFAULT NULL
        )
    """)
    cursor.execute("PRAGMA table_info(feature_flags)")
    feature_flags_cols = [col[1] for col in cursor.fetchall()]
    had_per_tier_limits = "limit_free" in feature_flags_cols
    if "limit_count" not in feature_flags_cols:
        cursor.execute("ALTER TABLE feature_flags ADD COLUMN limit_count INTEGER DEFAULT NULL")
    if "sort_order" not in feature_flags_cols:
        cursor.execute("ALTER TABLE feature_flags ADD COLUMN sort_order INTEGER DEFAULT 999")
    if had_per_tier_limits:
        # One-time migration from the old per-tier model (limit_free/
        # limit_pro/limit_admin, one owner-settable cap per tier) to the
        # new single limit_count (applies only at min_tier). Only the
        # column matching a row's CURRENT min_tier was ever the
        # functionally active one under the old model too, so carry that
        # specific value over; the other two tiers' old limits are
        # discarded (they were only ever reachable by first changing
        # min_tier to match them, at which point they'd have applied).
        for tier, col in (("FREE", "limit_free"), ("PRO", "limit_pro"), ("ADMIN", "limit_admin")):
            cursor.execute(
                f"UPDATE feature_flags SET limit_count = {col} "
                f"WHERE min_tier = ? AND limit_count IS NULL",
                (tier,),
            )
        try:
            cursor.execute("ALTER TABLE feature_flags DROP COLUMN limit_free")
            cursor.execute("ALTER TABLE feature_flags DROP COLUMN limit_pro")
            cursor.execute("ALTER TABLE feature_flags DROP COLUMN limit_admin")
        except sqlite3.OperationalError:
            pass  # older SQLite without DROP COLUMN support - harmless leftover columns
    _seed_feature_flags(cursor)

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
            kicked_data TEXT DEFAULT '[]',
            feature_snapshot TEXT DEFAULT NULL,
            total_limit INTEGER DEFAULT NULL,
            waitlist_data TEXT DEFAULT '[]',
            waitlist_open INTEGER DEFAULT 0,
            waitlist_visibility TEXT DEFAULT 'hidden',
            created_by_user_id TEXT DEFAULT NULL
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
    # Rename: 'sub_groups' -> 'sub_chats' - the old name implied "only
    # groups", but this table has always covered both groups and channels
    # (see chat_type). Must run before CREATE TABLE IF NOT EXISTS, since
    # 'sub_chats' is a brand new name that "IF NOT EXISTS" would happily
    # create empty alongside the still-existing old table.
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sub_groups'")
    has_sub_groups = cursor.fetchone() is not None
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sub_chats'")
    has_sub_chats = cursor.fetchone() is not None
    if has_sub_groups and not has_sub_chats:
        cursor.execute("ALTER TABLE sub_groups RENAME TO sub_chats")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sub_chats (
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
        cursor.execute("ALTER TABLE all_groups ADD COLUMN type TEXT DEFAULT 'FREE'")
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

    # 0a2. Normalize any pre-existing lowercase 'free'/'pro' type values to
    # 'FREE'/'PRO' - covers rows written before this uppercase convention.
    cursor.execute("UPDATE all_groups SET type = 'FREE' WHERE type = 'free'")
    cursor.execute("UPDATE all_groups SET type = 'PRO' WHERE type = 'pro'")

    # 0a3. Add `command_text` to command_log if it exists from an earlier
    # version that predates it.
    cursor.execute("PRAGMA table_info(command_log)")
    command_log_cols = [col[1] for col in cursor.fetchall()]
    if "command_text" not in command_log_cols:
        cursor.execute("ALTER TABLE command_log ADD COLUMN command_text TEXT DEFAULT NULL")

    # 0a4. Add `feature_snapshot` to events if it exists from an earlier
    # version that predates it. NULL for every pre-existing event means
    # "created before this mechanism existed" - handled as "everything
    # enabled" wherever the snapshot is read, so old events keep working
    # exactly as they always did.
    cursor.execute("PRAGMA table_info(events)")
    events_cols = [col[1] for col in cursor.fetchall()]
    if "feature_snapshot" not in events_cols:
        cursor.execute("ALTER TABLE events ADD COLUMN feature_snapshot TEXT DEFAULT NULL")

    # 0a5. Add `total_limit`/`waitlist_data` to events if missing. total_limit
    # is NULL for "no cap" (matches every pre-existing event, which never had
    # a limit). waitlist_data is a JSON list of entries each carrying their
    # own chat_id/chat_name, since the waitlist is a SINGLE event-wide list
    # but must render filtered-per-chat in each post and unfiltered (with
    # "from <chat_name>") in /waitlist - see event_engine.py's rendering.
    if "total_limit" not in events_cols:
        cursor.execute("ALTER TABLE events ADD COLUMN total_limit INTEGER DEFAULT NULL")
    if "waitlist_data" not in events_cols:
        cursor.execute("ALTER TABLE events ADD COLUMN waitlist_data TEXT DEFAULT '[]'")
    if "waitlist_open" not in events_cols:
        cursor.execute("ALTER TABLE events ADD COLUMN waitlist_open INTEGER DEFAULT 0")

    # 0a6. Add `waitlist_visibility` (visible/hidden/onlycount) alongside
    # the older boolean waitlist_open, which stays in the schema for
    # backward-compat inertia but is no longer read by any code path.
    # One-time migration: for pre-existing rows, carry the old boolean
    # over (1 -> visible, 0 -> hidden) so nobody's setting silently
    # resets - only genuinely new rows default to 'hidden' going forward.
    if "waitlist_visibility" not in events_cols:
        cursor.execute("ALTER TABLE events ADD COLUMN waitlist_visibility TEXT DEFAULT 'hidden'")
        cursor.execute("UPDATE events SET waitlist_visibility = 'visible' WHERE waitlist_open = 1")

    # 0a7. Add `created_by_user_id` - lets the event's own creator close it,
    # not just group admins (previously ANY member could create an event
    # via /newevent with zero admin check, but only a group admin could
    # ever close one - a non-admin creator had no way to close their own
    # event). Pre-existing events simply get NULL (unknown creator), which
    # is safe: NULL never matches a real user_id, so old events keep the
    # exact same admin-only close behavior they always had.
    if "created_by_user_id" not in events_cols:
        cursor.execute("ALTER TABLE events ADD COLUMN created_by_user_id TEXT DEFAULT NULL")

    # 0b. Add `chat_type`/`chat_name` to sub_chats if it exists from an
    # earlier version of this same migration that predates them.
    cursor.execute("PRAGMA table_info(sub_chats)")
    sg_cols = [c[1] for c in cursor.fetchall()]
    if "chat_type" not in sg_cols:
        cursor.execute("ALTER TABLE sub_chats ADD COLUMN chat_type TEXT DEFAULT NULL")
    if "chat_name" not in sg_cols:
        cursor.execute("ALTER TABLE sub_chats ADD COLUMN chat_name TEXT DEFAULT NULL")

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
    'FREE'), channels go into all_channels - both are simple "the bot is
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
            VALUES (?, ?, 'FREE', ?, ?)
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


def log_command_usage(chat_id: str, user_id, command: str, command_text: str, timestamp: str, db_path: str = None):
    """
    Records one command invocation, including the full raw text as typed
    (command_text) - not just the parsed command name. Called generically
    for every command (see main.py's log_command_usage_handler), not from
    inside each command's own logic - so adding a new command automatically
    gets logged without needing to remember to add a line for it.
    """
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO command_log (chat_id, user_id, command, command_text, timestamp) VALUES (?, ?, ?, ?, ?)",
        (str(chat_id), str(user_id) if user_id is not None else None, command, command_text, timestamp),
    )
    conn.commit()
    conn.close()


def get_feature_flags(db_path: str = None):
    """
    Returns every row of feature_flags as (feature_key, feature_label,
    min_tier, limit_count, description) tuples, ordered by sort_order (an
    explicit, developer-defined display order - deliberately not
    tier-then-alphabetical, since the intended order mixes tiers, e.g.
    setsub/ADMIN listed before custom_sheet/PRO) - this is what
    sync_control_sheet_botconfig() mirrors to the Control Sheet's
    "BOTCONFIG" tab. limit_count is None for unlimited, or an integer cap -
    it only ever applies while a chat is AT min_tier exactly; any tier
    above min_tier is unlimited by construction (see
    get_feature_limit_for_chat()).
    """
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT feature_key, feature_label, min_tier, limit_count, description "
        "FROM feature_flags "
        "ORDER BY sort_order, feature_key"
    )
    rows = cursor.fetchall()
    conn.close()
    return rows


_NO_CHANGE = object()  # sentinel distinct from None, since None is the legitimate "clear the limit" value


def update_feature_flag(feature_key: str, min_tier: str, limit_count=_NO_CHANGE, db_path: str = None):
    """
    Changes which tier a feature requires and/or its limit_count (the cap
    that applies only while a chat is AT min_tier exactly - see
    get_feature_limit_for_chat()). Plain, synchronous DB write - see
    subscription.set_feature_flag() for the async wrapper that also
    re-syncs the Control Sheet's BOTCONFIG tab immediately after, which is
    the actual guarantee that BOTCONFIG never drifts out of date with this
    table. Call THROUGH that wrapper, not this function directly, unless
    you're deliberately skipping the sync (e.g. inside a bulk migration
    that syncs once at the end).

    limit_count: pass an int to set it, None to clear it (unlimited), or
    omit it entirely (the default) to leave the current value untouched -
    the caller (subscription.updatefeature) decides whether a tier change
    should reset it, not this function.
    """
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    sets = ["min_tier = ?"]
    params = [min_tier]
    if limit_count is not _NO_CHANGE:
        sets.append("limit_count = ?")
        params.append(limit_count)
    params.append(feature_key)

    cursor.execute(f"UPDATE feature_flags SET {', '.join(sets)} WHERE feature_key = ?", params)
    conn.commit()
    conn.close()


def get_feature_limit_for_chat(chat_id: str, feature_key: str, db_path: str = None):
    """
    Returns the limit that applies to THIS specific chat, given its own
    current tier (None = unlimited, whether because limit_count is unset
    or because the chat's tier is ABOVE the feature's min_tier - a limit
    only ever applies while a chat is exactly AT min_tier). Used by
    enforcement code that needs a live limit check (e.g. /shareevent's
    per-target cap) separately from the tier-ACCESS check that
    has_feature() already covers.

    Inlines the same PRO-detection logic as subscription.is_premium()
    rather than importing it, to avoid a circular import (subscription.py
    already imports from db.py at module level).
    """
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cursor.execute("SELECT type, subs_date_end FROM all_groups WHERE chat_id = ?", (str(chat_id),))
    group_row = cursor.fetchone()
    is_pro = False
    if group_row and group_row[0] == "PRO" and group_row[1]:
        try:
            is_pro = datetime.strptime(group_row[1], "%Y-%m-%d %H:%M:%S") > datetime.now()
        except ValueError:
            is_pro = False

    group_tier = "PRO" if is_pro else "FREE"
    cursor.execute("SELECT min_tier, limit_count FROM feature_flags WHERE feature_key = ?", (feature_key,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    min_tier, limit_count = row
    # Limit only applies at the feature's own min_tier - anything above is
    # unlimited by construction, and anything below wouldn't have access
    # at all (that's has_feature()'s job, not this function's).
    return limit_count if group_tier == min_tier else None


def get_event_total_going_headcount(event_id: str, db_path: str = None) -> int:
    """
    Total headcount currently counted against an event's total_limit: every
    person marked going in the main hub (plus their guests, via
    counters_data) PLUS every person marked going in any child chat's
    event_users row (plus their own guests column). This is a person-based
    cap, not a row-count - someone with 3 guests fills 4 spots, matching
    what -limit is meant to represent (physical capacity), not just a cap
    on distinct "Going" clicks.

    Used by the capacity check on every "going" click (both master-hub and
    child-chat) before it's allowed to actually add someone - see
    event_engine.button_handler.
    """
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT going_data, counters_data FROM events WHERE event_id = ?", (event_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return 0
    going_data, counters_data = row
    going_list = json.loads(going_data) if going_data else []
    counters = json.loads(counters_data) if counters_data else {}
    main_headcount = len(going_list) + sum(counters.values())

    cursor.execute(
        "SELECT COALESCE(SUM(1 + guests), 0) FROM event_users WHERE event_id = ? AND status = 'going'",
        (event_id,),
    )
    child_headcount = cursor.fetchone()[0]
    conn.close()
    return main_headcount + child_headcount


def add_to_waitlist(event_id: str, chat_id: str, chat_name: str, username: str, user_id: str, db_path: str = None):
    """
    Appends one entry to an event's waitlist_data (a single event-wide JSON
    list; each entry carries its own chat_id/chat_name so rendering can
    filter to "this chat only" in a post, or show everyone with "from
    <chat_name>" in /waitlist - see event_engine.py's rendering and
    handlers.waitlist_command).

    Does nothing (silently) if this exact user is already waiting in this
    exact chat, so a double-click can't queue someone twice.

    NOT called by event_engine.button_handler, which inlines this exact
    logic instead using its own already-open cursor - this function opens
    its own standalone connection, so calling it from WITHIN
    button_handler's transaction would risk a nested-connection deadlock.
    Safe to use standalone (e.g. a future admin command) outside any
    already-open transaction.
    """
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT waitlist_data FROM events WHERE event_id = ?", (event_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return
    waitlist = json.loads(row[0]) if row[0] else []
    already_waiting = any(
        str(e.get("user_id")) == str(user_id) and str(e.get("chat_id")) == str(chat_id)
        for e in waitlist
    )
    if not already_waiting:
        waitlist.append({
            "chat_id": str(chat_id),
            "chat_name": chat_name,
            "username": username,
            "user_id": str(user_id),
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        })
        cursor.execute(
            "UPDATE events SET waitlist_data = ? WHERE event_id = ?",
            (json.dumps(waitlist), event_id),
        )
        conn.commit()
    conn.close()


def promote_next_from_waitlist(event_id: str, chat_id: str, db_path: str = None):
    """
    Removes and returns the OLDEST waitlist entry for THIS SPECIFIC chat_id
    (FIFO - whoever's been waiting longest in that chat goes first), or
    None if that chat's waitlist is empty. Only removes the entry from
    waitlist_data - the caller is responsible for actually adding the
    promoted person into going_data/event_users, since that differs
    between the main hub and a child chat.

    NOT called by event_engine.button_handler, which inlines this exact
    logic instead using its own already-open cursor - see add_to_waitlist's
    docstring above for why (nested-connection deadlock risk). Safe to use
    standalone (e.g. a future admin command) outside any already-open
    transaction.
    """
    if db_path is None:
        db_path = DB_PATH
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT waitlist_data FROM events WHERE event_id = ?", (event_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None
    waitlist = json.loads(row[0]) if row[0] else []
    this_chat_entries = [e for e in waitlist if str(e.get("chat_id")) == str(chat_id)]
    if not this_chat_entries:
        conn.close()
        return None
    this_chat_entries.sort(key=lambda e: e.get("timestamp", ""))
    promoted = this_chat_entries[0]
    waitlist = [e for e in waitlist if e is not promoted]
    cursor.execute(
        "UPDATE events SET waitlist_data = ? WHERE event_id = ?",
        (json.dumps(waitlist), event_id),
    )
    conn.commit()
    conn.close()
    return promoted
