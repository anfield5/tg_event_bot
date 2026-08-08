# Telegram Event Bot

A Telegram bot that manages RSVP-style events inside groups: create an
event with a Going/Not Going keyboard, track who's coming, share it to
other groups/channels, and export attendance to Google Sheets. Includes a
FREE/PRO subscription model with a per-feature tier and usage-limit system
that an owner can adjust live via `/updatefeaturelevel`, without a
redeploy.

Run `/help` inside the bot for the full command reference - this README
covers setup and architecture, not day-to-day usage.

## Stack

- Python 3.11, [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) 20.3
- SQLite (local file, `database.db`) - all bot state
- Google Sheets via `gspread_asyncio` - optional, per-hub export (PRO) and a
  control sheet (owner-only, always on)
- Docker Compose for deployment

## Running it

```bash
cp .env.example .env   # fill in BOT_TOKEN, GOOGLE_CREDENTIALS_JSON, OWNER_USER_IDS, CONTROL_SHEET_ID
docker compose up -d --build
docker compose logs bot | grep "Bot v"
```

**Persisting the database across rebuilds** - `docker-compose.yml` must
mount the DB file outside the container, or every rebuild starts fresh:

```yaml
services:
  bot:
    volumes:
      - ./database.db:/app/database.db
```

See `tests/README.md` for running the test suite.

## Database schema (SQLite)

```
all_groups            all_channels           feature_flags
─────────────          ─────────────           ──────────────
chat_id (PK)            chat_id (PK)            feature_key (PK)
chat_name               chat_name               feature_label
type (FREE/PRO)         visibility              min_tier (FREE/PRO/ADMIN)
sheet_id (unique)       date_bot_add            limit_free / limit_pro / limit_admin
sheet_name                                      sort_order
subs_date_start/end                             description
visibility
date_bot_add

events                              event_shares
──────────────                       ──────────────
event_id (PK)                        share_id (PK)
chat_id, message_id                  event_id, chat_id, message_id  (UNIQUE per event+chat)
name, going_icon, notgoing_icon      share_mode ('-visible' / '-hidden' / '-onlycount')
event_status (0 open/1 verify/       chat_type ('group' / 'channel')
              2 closed/-1 canceled)
going_data / notgoing_data / counters_data   (JSON)
kicked_data (JSON)
event_date
feature_snapshot (JSON) - which tier-gated behaviors
  (verification, add_extra_member) applied to THIS event,
  frozen at creation time so a later tier change never
  retroactively changes an event already in progress

main_group_users                   event_users                    sub_chats
──────────────────                  ─────────────                  ──────────────
chat_id + username (PK)             event_id + chat_id +            id (PK)
user_id, status                       user_id (PK)                  chat_id, owner_chat_id
first_name, last_name               username, status, guests        alias, is_monitored
                                     - final roster of an event      chat_type, chat_name
                                       at Save & Close, across        - child groups/channels
                                       the main chat and every         linked to a hub, for
                                       chat it was shared to            /shareevent aliases
                                                                        and /addmonitor

command_log                        all_chats_bot_log
──────────────                      ──────────────────
id (PK), chat_id, user_id           id (PK), chat_id
command, command_text               date_bot_add, date_bot_removed
timestamp                           - archive of add/remove events
- every command run, incl. DMs
```

Key relationships:
- `all_groups.sheet_id` is the only link from a hub to its bound Google
  Sheet - `get_sheet_for_chat(chat_id)` is a direct `all_groups` lookup
  (PRO + active subscription + a sheet_id set, otherwise `None`). It's
  always called with the **hub's** chat_id, never a child chat's - every
  event's `events.chat_id` is always the hub it was created in, so
  callers never need to resolve a child chat up to its owner.
- `events.chat_id` is always the **hub** (main group) the event was
  created in; `event_shares` links that same `event_id` to every child
  chat it was shared to.
- `feature_flags` is read live on every gated action (`has_feature()`,
  `get_feature_limit_for_chat()`) - there's no cache, so
  `/updatefeaturelevel` takes effect immediately.

## Google Sheets schema

Two kinds of spreadsheet, both accessed the same way (`open_spreadsheet`,
cached per spreadsheet ID):

**Control Sheet** (`CONTROL_SHEET_ID` in `.env`, one per bot deployment,
owner-only, always kept in sync regardless of tier):

| Tab | Columns | Written by |
|---|---|---|
| `GROUPS` | CHAT_ID, CHAT_NAME, TYPE, SHEET_ID, SHEET_NAME, SUBS_DATE_START, SUBS_DATE_END, VISIBILITY, DATE_BOT_ADD | mirrors `all_groups`, on every `/setsub` |
| `CHANNELS` | CHAT_ID, CHAT_NAME, VISIBILITY, DATE_BOT_ADD | mirrors `all_channels` |
| `BOTCONFIG` | FEATURE_KEY, FEATURE, FREE, PRO, ADMIN, DESCRIPTION | mirrors `feature_flags`, on every `/updatefeaturelevel` |

**Per-hub Sheet** (bound via `/setsheet`, PRO-only - a FREE hub writes
nothing to Sheets at all):

| Tab | Columns | Written by |
|---|---|---|
| `Users` | USER_ID, USER_NAME, PLACE_ID, STATUS, DATE_start, DATE_end, ARCHIVED_USER_NAME, FIRST_NAME, LAST_NAME | `/refreshusers`, `/refreshusersall` - one row per (user, place); STATUS flips MEMBER/LEFT rather than deleting rows |
| `Events` | EVENT_ID, EVENT_NAME, CREATED_DATE, CREATED_BY, EVENT_DATE, CLOSED_AT, STATUS, GOING_COUNT | row appended on `/newevent`, columns F:H updated on Save & Close |
| `Actions` | EVENT_ID, ACTION, USER_NAME, USER_ID, DATE | every button click (going/notgoing/kick/save/...) |
| `EventUsers` | EVENT_ID, USER_ID | final attendee list, written once at Save & Close (main chat + every child chat combined) |
| `UserPresenceLog` | USER_ID, PLACE_ID, DATE_start, DATE_end | logged when someone leaves a monitored/main chat |

## How the DB and Sheets interact

SQLite is the source of truth and the only thing the bot ever *reads* back
- Sheets is a write-mostly export layer, never read to make a decision:

1. Every command works even with `GOOGLE_CREDENTIALS_JSON` unset or Sheets
   unreachable. `get_sheet_for_chat()` is a pure DB lookup (no network
   call) returning `None` for FREE tier or no sheet bound; separately,
   each `sync_*` function wraps its own `open_spreadsheet()`/API calls in
   a try/except that logs and returns on failure instead of raising -
   either way, a Sheets problem never blocks the user-facing action.
2. `all_groups`/`all_channels`/`feature_flags` are pushed to the Control
   Sheet *after* every write to those tables (`_push_control_sheet_*`),
   not read back from it - the Control Sheet is a live mirror for the
   owner to view, never a config source.
3. Per-hub tabs only ever get appended/updated in response to a specific
   SQLite-driven action (a button click, `/refreshusers`, Save & Close) -
   nothing about how the bot behaves is ever decided by what's currently
   in the Sheet.
