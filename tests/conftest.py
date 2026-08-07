"""
Shared pytest fixtures used across all test modules.

Key design decisions
────────────────────
* Every test that touches the database receives an isolated temp file via the
  `db_path` fixture.  We monkey-patch `db.DB_PATH` and `handlers.DB_PATH` so
  that all production code writes to the temp file instead of `database.db`.
* Async tests use `pytest-asyncio` in "auto" mode (configured in pytest.ini /
  pyproject.toml) to avoid decorating every single test function.
* Telegram objects (Update, Message, CallbackQuery, Chat, User, Bot, Context)
  are created with `unittest.mock.MagicMock` / `AsyncMock` so no real network
  call is ever made.
"""

import sys
import os
import sqlite3
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Make the project root importable without installing the package
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db as db_module
import event_engine as event_engine_module
from db import init_db
from tests.helpers import (          # re-export so conftest consumers can use them
    make_user, make_chat, make_message, make_bot, make_update, make_context
)


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    """
    Creates an isolated SQLite database in a temp directory and patches
    DB_PATH in `db` so all production code (via db.get_connection(), the
    only DB access path used anywhere in this project) uses it. Returns
    the path string so tests can open direct connections if needed.
    """
    path = str(tmp_path / "test.db")
    monkeypatch.setattr(db_module, "DB_PATH", path)
    # Initialise schema (migrations included)
    init_db(db_path=path)
    return path


@pytest.fixture()
def db_conn(db_path):
    """
    Yields an open sqlite3 connection to the test database.
    Closes it automatically after the test.
    """
    conn = sqlite3.connect(db_path)
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def _reset_module_level_state():
    """
    event_engine.py keeps two module-level dicts across the whole process
    lifetime: `_event_locks` (per-event asyncio.Lock for the button_handler
    critical section) and `_refresh_state` (per-event coalescing state for
    schedule_view_refresh). Tests freely reuse the same event_id strings
    (e.g. "ev1") across completely unrelated test functions, so without
    resetting these dicts between tests, a lock left in a stuck/locked state
    by one test (e.g. a cancelled task) could cause an unrelated later test
    using the same event_id to hang waiting on it. Clearing both dicts before
    every test keeps tests fully isolated from each other.
    """
    event_engine_module._event_locks.clear()
    event_engine_module._refresh_state.clear()
    yield
    event_engine_module._event_locks.clear()
    event_engine_module._refresh_state.clear()


# ---------------------------------------------------------------------------
# Export helpers so tests can import them directly from conftest
# ---------------------------------------------------------------------------
__all__ = [
    "make_user", "make_chat", "make_message", "make_bot",
    "make_update", "make_context",
]
