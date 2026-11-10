"""
What the operator scripts (audit_event_users, repair_event_users, fix_eventusers_ids, check_events_amount) share: which
hubs to go through, how far back to look, and what to do when one hub's Google Sheet cannot be read.
"""
import sqlite3
from datetime import datetime, timedelta


def cutoff_for(days):
    """The oldest creation date an event may have to be looked at, or None for 'every event'."""
    return None if days is None else datetime.now() - timedelta(days=days)


def hub_chat_ids(db_path, target_chat_id=None) -> list:
    """The one hub named on the command line, else every hub that has a Sheet bound."""
    if target_chat_id:
        return [target_chat_id]
    conn = sqlite3.connect(db_path)
    try:
        return [r[0] for r in conn.execute("SELECT chat_id FROM all_groups WHERE sheet_id IS NOT NULL AND sheet_id != ''")]
    finally:
        conn.close()


def describe_hub_error(db_path, chat_id, error) -> str:
    """One line for a hub that could not be read at all: which hub and sheet, what Google said, and what that usually means."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute("SELECT chat_name, sheet_id FROM all_groups WHERE chat_id = ?", (str(chat_id),)).fetchone()
    finally:
        conn.close()
    name = f"\u00ab{row[0]}\u00bb, " if row and row[0] else ""
    sheet = f"sheet {row[1]}: " if row and row[1] else ""
    reason = str(error)
    text = f"{name}{sheet}{type(error).__name__}: {reason[:220]}"
    if "PERMISSION_DENIED" in reason or "403" in reason:
        text += (" - the bot's service account has no access to this spreadsheet: share it with that account as Editor "
                 "(or fix the binding with /setsheet). Saving events in this hub fails the same way.")
    elif "NOT_FOUND" in reason or "404" in reason:
        text += " - the spreadsheet no longer exists or its id is wrong: bind the right one with /setsheet."
    return text


async def run_per_hub(chat_ids, db_path, work, on_error) -> dict:
    """
    {chat_id: result} of `await work(chat_id)` for each hub. One hub's sheet failing (no access, deleted...) must not hide
    all the others: its result is `on_error(<the one-line description>)` instead - each script keeps its own result shape.
    """
    results = {}
    for chat_id in chat_ids:
        try:
            results[chat_id] = await work(chat_id)
        except Exception as e:
            results[chat_id] = on_error(describe_hub_error(db_path, chat_id, e))
    return results
