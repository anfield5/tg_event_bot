# Running the tests

## 1. Install dependencies

```bash
pip install pytest pytest-asyncio python-telegram-bot --break-system-packages
```

(drop `--break-system-packages` if you're using a virtualenv, which is
recommended)

## 2. Run the whole suite

From the project root:

```bash
pytest
```

`pytest.ini` already points pytest at the `tests/` folder and enables
`asyncio_mode = auto`, so `async def test_...` functions work without any
extra decorator.

## 3. Run a single file / class / test

```bash
# one file
pytest tests/test_handlers_async.py

# one class
pytest tests/test_handlers_async.py::TestPremiumGating

# one specific test
pytest tests/test_handlers_async.py::TestPremiumGating::test_setalias_blocked_on_free

# by keyword (matches test/class names containing the string)
pytest -k "premium"
```

## 4. Useful flags

```bash
pytest -v              # verbose - print every test name and its result
pytest -x              # stop at the first failure instead of running everything
pytest --lf            # re-run only the tests that failed last time
pytest -x -v -k alias  # combine flags as needed
```

## 5. What's covered where

| File | Covers |
|---|---|
| `test_utils.py` | `escape_markdown`, `now2ddmmyy`, `parse_event_date` |
| `test_db.py` | Schema creation, every migration path (legacy table renames, the chat_aliases+monitors merge into sub_groups, the is_open/is_cancelled→event_status rebuild), `track_user()` |
| `test_handlers_pure.py` | `create_event_keyboard` - every `event_status` value (open/verification/closed/canceled), button labels, callback_data formats |
| `test_handlers_async.py` | Everything that touches Telegram/DB together: commands (`/newevent`, `/editevent`, `/notify`, `/refreshusers`, `/shareevent`, `/setalias`, `/addmonitor`, `/setsub`...), the `button_handler` click-handling engine, premium gating, `/help`'s tier-aware keyboard |
| `test_markdown_safety.py` | Static scan of `handlers.py` for unescaped MarkdownV2 characters (`.`/`!`) outside code spans - this is what caught several real crashes during development, keep it passing |

## 6. Test isolation - how it works

- `conftest.py`'s `db_path` fixture gives every test its **own temporary
  SQLite file** (via `tmp_path`), so tests never touch your real
  `database.db` and never interfere with each other.
- An `autouse` fixture clears the module-level `_event_locks` and
  `_refresh_state` dicts in `handlers.py` between tests, so a lock acquired
  in one test can't leak into the next one.
- `helpers.py` provides `make_chat`, `make_user`, `make_message`,
  `make_update`, `make_bot`, `make_context`, `make_callback_update` - all
  return `MagicMock`/`AsyncMock` objects standing in for real
  `python-telegram-bot` objects, so no network call ever happens.
  **Important**: `make_message()` explicitly sets `sender_chat = None`.
  A bare `MagicMock()` auto-vivifies *any* unset attribute as a truthy mock
  object instead of `None` - without this, every admin-gated command's
  test silently short-circuited through `is_real_admin()`'s "was this
  posted anonymously as the channel/chat itself" check as if it were
  always true, meaning the *real* `get_chat_member`-based admin check
  never actually ran in any test using a plain message mock. Don't remove
  this line.
- `insert_event()` and `insert_premium()` (in `test_handlers_async.py`) are
  shared helpers for seeding a temp DB with an event or an active
  subscription before exercising a handler against it.

## 7. Before pushing a change

Run at minimum:

```bash
pytest -v
```

and make sure every test passes. If you touched `handlers.py`,
`subscription.py`, `aliases.py`, or `monitors.py` and added a new
`reply_text(..., parse_mode="MarkdownV2")` call, it's worth double-checking
`test_markdown_safety.py` still passes - it's the one thing that reliably
catches "unescaped `.` in a MarkdownV2 message" before it reaches
production and crashes with `Can't parse entities`.

## 8. Known gap (documented on purpose, not hidden)

There is currently no dedicated test file for `sheets.py`'s Google Sheets
API calls (`sync_users_sheet`, `sync_event_users_sheet`,
`sync_control_sheet_main`, etc.) - these are exercised indirectly (via
mocking `open_spreadsheet`/`sync_*` functions) inside
`test_handlers_async.py`, but there's no `test_sheets.py` testing
`sheets.py`'s own internal logic in isolation. Worth adding if that file
grows more complex.
