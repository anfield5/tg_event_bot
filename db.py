import sqlite3
import json

def init_db():
    conn = sqlite3.connect("database.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_settings (
            chat_id TEXT PRIMARY KEY,
            sheet_name TEXT
        )
    """)
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
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chat_users (
            chat_id TEXT,
            username TEXT,
            status TEXT DEFAULT 'active',
            PRIMARY KEY (chat_id, username)
        )
    """)
    
    # Database migration guard for status column
    cursor.execute("PRAGMA table_info(chat_users)")
    columns = [col[1] for col in cursor.fetchall()]
    if "status" not in columns:
        cursor.execute("ALTER TABLE chat_users ADD COLUMN status TEXT DEFAULT 'active'")
        
    conn.commit()
    conn.close()

def track_user(chat_id, username, status="active"):
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