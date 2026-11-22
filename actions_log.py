"""
The Actions tab of the Google Sheet: one row per click (GOING, NOTGOING, ADD, SUB, ...).

Writing a row costs two Google calls (open the tab, append) and gspread_asyncio spaces every call of the whole bot at least
1.1 seconds apart - so doing it inside the click handler made each click wait 2-3 seconds, and the bot handles updates one
after another, so every other click in every chat waited behind it. Now the handler only puts the row into the SQLite table
`actions_outbox` (a millisecond) and goes on; this module's background task writes the waiting rows of a hub to its sheet in
ONE append, a couple of seconds later.

Nothing is lost: a row stays in the outbox until the sheet has it - a failed write (Google down, rate limit) is retried, and a
restart of the bot picks up what was waiting. The rows keep the click's own date and their order. A row that still cannot be
written after MAX_ATTEMPTS tries (e.g. the tab was deleted) is dropped with an error in the log, so it cannot block the rows
behind it; a hub that is no longer on a premium sheet has its rows dropped, as before.
"""
import asyncio
import json

from config import logger
from db import get_connection
from sheets import get_sheet_for_chat, open_spreadsheet
from utils import now2ddmmyy

FLUSH_DELAY = 2.0        # seconds the clicks of a burst are collected before they are written together
RETRY_EVERY = 30.0       # seconds between tries while rows are left over (a failed write)
BATCH = 200              # rows taken from the outbox at a time
MAX_ATTEMPTS = 20

_wake = None


def _event() -> asyncio.Event:
    global _wake
    if _wake is None:
        _wake = asyncio.Event()
    return _wake


def queue_action(hub_chat_id, row: list) -> None:
    """Puts one Actions row in the outbox (it is written to the sheet soon, see the module docstring). Never raises."""
    try:
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO actions_outbox (hub_chat_id, row_json, attempts, created_at) VALUES (?, ?, 0, ?)",
                (str(hub_chat_id), json.dumps(row), now2ddmmyy()),
            )
            conn.commit()
    except Exception as e:
        logger.error(f"Could not queue the Actions row {row}: {e}")
        return
    _event().set()


def pending_count() -> int:
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM actions_outbox").fetchone()[0]


async def flush_once() -> int:
    """Writes up to BATCH waiting rows (one append per hub). -> how many rows left the outbox because the sheet has them."""
    with get_connection() as conn:
        waiting = conn.execute(
            "SELECT id, hub_chat_id, row_json, attempts FROM actions_outbox ORDER BY id LIMIT ?", (BATCH,)
        ).fetchall()
    by_hub = {}
    for row_id, hub, row_json, attempts in waiting:
        by_hub.setdefault(hub, []).append((row_id, json.loads(row_json), attempts))

    written = 0
    for hub, items in by_hub.items():
        ids = [i for i, _r, _a in items]
        try:
            sheet_target = await get_sheet_for_chat(hub)
            ss = await open_spreadsheet(sheet_target) if sheet_target else None
            if ss is None:
                _remove(ids)                      # no premium sheet (any more): nothing to write to
                continue
            ws = await ss.worksheet("Actions")
            await ws.append_rows([r for _i, r, _a in items])
            _remove(ids)
            written += len(ids)
        except Exception as e:
            logger.error(f"Sheets Actions write failed for {hub} ({len(ids)} row(s)), will retry: {e}")
            _failed(items)
    return written


def _remove(ids) -> None:
    with get_connection() as conn:
        conn.executemany("DELETE FROM actions_outbox WHERE id = ?", [(i,) for i in ids])
        conn.commit()


def _failed(items) -> None:
    """Counts a failed try against each row; a row out of tries is dropped (and logged) so it cannot block the ones behind."""
    with get_connection() as conn:
        for row_id, row, attempts in items:
            if attempts + 1 >= MAX_ATTEMPTS:
                logger.error(f"Giving up on the Actions row {row} after {MAX_ATTEMPTS} tries")
                conn.execute("DELETE FROM actions_outbox WHERE id = ?", (row_id,))
            else:
                conn.execute("UPDATE actions_outbox SET attempts = attempts + 1 WHERE id = ?", (row_id,))
        conn.commit()


async def run_flusher() -> None:
    """The background task (started with the bot): waits for rows, lets a burst of clicks gather, writes them."""
    while True:
        try:
            await asyncio.wait_for(_event().wait(), timeout=RETRY_EVERY)
        except asyncio.TimeoutError:
            pass
        _event().clear()
        await asyncio.sleep(FLUSH_DELAY)
        try:
            while await flush_once():
                pass
        except Exception as e:
            logger.error(f"Actions flusher: {e}")
            await asyncio.sleep(RETRY_EVERY)
