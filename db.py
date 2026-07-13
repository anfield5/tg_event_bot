import sqlite3
import json

def init_db():
    """
    Initializes the database schema and performs required migrations.
    Creates tables for chat settings, events, users, shares, and aliases.
    """
    conn = sqlite3.connect("database.db")
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
            counters_data TEXT
        )
    """)
    
    # Main registry of known users across chat environments
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_users (
            chat_id TEXT,
            username TEXT,
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
    
    # Database schema migration guard for tracking user status changes
    cursor.execute("PRAGMA table_info(chat_users)")
    columns = [col[1] for col in cursor.fetchall()]
    if "status" not in columns:
        cursor.execute("ALTER TABLE chat_users ADD COLUMN status TEXT DEFAULT 'active'")
        
    conn.commit()
    conn.close()

def track_user(chat_id, username, status="active"):
    """
    Upserts a single user registration record within the localized chat context.
    """
    if not username:
        return
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO chat_users (chat_id, username, status) VALUES (?, ?, ?)
        ON CONFLICT(chat_id, username) DO UPDATE SET status = excluded.status
    """, (str(chat_id), username, status))
    conn.commit()
    conn.close()