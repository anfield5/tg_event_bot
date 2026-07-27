"""
One-time helper to bind a Telegram group to its own Google Sheet in
main_chat_settings, until a proper /setsheet command exists.

Run once, from the project root, against your real database.db:
    python3 scripts/bind_sheet.py 7180695982 <spreadsheet_id>

Pass the spreadsheet ID (the long string in the sheet's URL,
https://docs.google.com/spreadsheets/d/<THIS PART>/edit), not the sheet's
title - open_spreadsheet() now opens by ID (gc.open_by_key), since titles
aren't guaranteed unique across different Google accounts/files.

The UNIQUE constraint on main_chat_settings.sheet_id (see db.py) guarantees
this binding is exclusive in both directions: this group can't be pointed
at a second sheet, and this sheet can't be claimed by a second group -
the INSERT below will fail loudly with sqlite3.IntegrityError if either
is already taken by someone else, instead of silently overwriting it.
"""
import sys
import sqlite3

sys.path.insert(0, ".")
from db import init_db, DB_PATH  # noqa: E402


def bind(chat_id: str, sheet_id: str, db_path: str = DB_PATH):
    init_db(db_path=db_path)  # make sure the UNIQUE-constrained schema exists
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO main_chat_settings (chat_id, sheet_id) VALUES (?, ?)",
            (str(chat_id), sheet_id),
        )
        conn.commit()
        print(f"OK: chat_id={chat_id} is now exclusively bound to spreadsheet '{sheet_id}'")
    except sqlite3.IntegrityError as e:
        print(f"REJECTED: {e}")
        print("Either this chat_id already has a different sheet bound, "
              "or this sheet_id is already bound to a different chat_id.")
        print("Check the current binding with:")
        print(f"  SELECT * FROM main_chat_settings WHERE chat_id = '{chat_id}' OR sheet_id = '{sheet_id}';")
    finally:
        conn.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python3 scripts/bind_sheet.py <chat_id> <spreadsheet_id>")
        sys.exit(1)
    bind(sys.argv[1], sys.argv[2])
