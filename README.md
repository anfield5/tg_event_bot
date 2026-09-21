# Telegram Event Bot

A Telegram bot that manages RSVP-style events inside groups: create an
event with a Going/Not Going keyboard, track who's coming, share it to
other groups/channels, and export attendance to Google Sheets. Includes a
FREE/PRO subscription model with a per-feature tier and usage-limit system
that an owner can adjust live via `/updatefeature`, without a
redeploy.

Run `/help` inside the bot for the full command reference - this README
covers setup and architecture, not day-to-day usage.

## Stack

- Python 3.9+, [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) 20.3
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

## Database and Google Sheets schema

SQLite tables and Google Sheets tabs shown together, grouped into three
non-overlapping zones, with relationship lines showing *which DB table's
data ends up in which Sheet tab*. Every table/tab shows its full column
list; bold fields are primary/unique keys. Table positions within each
zone are chosen to minimize crossing lines.

![Database and Google Sheets schema](docs/db_sheets_schema.svg)

> **Recent schema changes not yet reflected in the diagram above** (the
> SVG is a static, hand-produced artifact with no generation script in
> this repo, so it can't be mechanically regenerated - `db.py`'s own
> `init_db()` is the definitive, always-current source of truth):
> - `main_group_users`' primary key is now `(chat_id, user_id)`, not
>   `(chat_id, username)` - a Telegram `@username` isn't a permanent
>   identity (it can change, and a since-abandoned one can be reused by
>   a completely different person), which caused a real incident where
>   a still-present admin was incorrectly removed by `/refreshusers`.
>   `user_id` is `NOT NULL` and must be numeric (both enforced by
>   `track_user()` and by the migration that re-keys an older table).
> - `events` gained two new columns: `created_date` and `closed_date`
>   (both `TEXT`, `now2ddmmyy()`-formatted). Previously these values
>   were only ever written to the bound Google Sheet, never persisted
>   in the DB itself. Set once at `/newevent` (`created_date`) and once
>   at Save&Close/Cancel/Directclose (`closed_date`, via
>   `COALESCE(?, closed_date)` so unrelated actions like a kick or a
>   guest-count edit never clear an already-set value). The Sheets
>   Events tab export reuses these exact persisted values rather than
>   computing a fresh timestamp at write time. `created_date` is also
>   how `/stats [period]` filters events into a time window (parsed in
>   Python, since `now2ddmmyy()`'s `DD.MM.YYYY` format doesn't sort
>   correctly as a raw SQL string comparison) - an event with no
>   `created_date` at all is excluded from any specific period but
>   still counted under "all time". Events closed before this
>   change has NULL `created_date`/`closed_date` in the DB (though the
>   same values already sit in the Sheet) - `scripts/backfill_event_dates.py`
>   is a one-time, safe-to-rerun script that copies them back from each
>   PRO hub's Events tab into the DB, only ever filling a `NULL`, never
>   overwriting an existing value.
> - `bot_lock` - a single-row table backing the `/lockbot` global
>   emergency switch (see [Global lock](#global-lock-and-command-destination-classification)
>   below) - was added but isn't shown in the diagram at all.
> - `all_groups`/`all_channels` gained a `role` column - the bot's OWN
>   status in that chat (`MEMBER`/`ADMIN`), tracked live via
>   `on_my_chat_member_update`'s `member ↔ administrator` transition
>   (previously silently ignored - the bot's own promotion/demotion
>   without leaving a chat was never recorded). Powers `/stats -a`'s
>   admin-rights counts and is now exported as both `GROUPS`' and
>   `CHANNELS`' `ROLE` column on the Control Sheet (initially only
>   `CHANNELS` got it - a real gap, fixed). Chats added (or already
>   admin-status) before this feature existed, whose role hasn't
>   changed SINCE deploying it, still have the column's default until a
>   resync - `scripts/sync_bot_roles.py` queries Telegram's live
>   `getChatMember` for every ALREADY-registered chat and backfills the
>   real current role; safe to run repeatedly. **Platform limitation,
>   not fixable in code:** Telegram's Bot API has no "list every chat
>   I'm in" method at all - a chat the bot was added to before
>   `on_my_chat_member_update` ever ran (i.e. before this whole
>   presence-tracking feature existed in any form) has literally no row
>   in `all_groups`/`all_channels` at all, and the sync script can't
>   discover it either, since it only iterates chat_ids ALREADY in
>   those tables. The only fix is removing and re-adding the bot to
>   that specific chat, which triggers a fresh `my_chat_member` event.

- **Gray** = SQLite table (single database, source of truth)
- **Blue** = Control Sheet - **one** spreadsheet, shared across the whole bot
- **Green** = Per-hub Sheet - **one separate spreadsheet per hub**, bound via `/setsheet` (shown as a stacked block to indicate multiple instances)

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
- `all_features` is read live on every gated action (`has_feature()`,
  `get_feature_limit_for_chat()`) - there's no cache, so
  `/updatefeature` takes effect immediately. `limit_count` is a single
  value that only ever caps usage while a chat's tier is exactly AT the
  feature's `min_tier` - any tier above is unlimited by construction,
  so there's no way to misconfigure a higher tier as more restricted
  than a lower one.
- `command_log` (every command run, incl. DMs) is DB-only - it has no
  Sheets counterpart, so it's omitted from the diagram above.
  `all_chats_bot_log` (add/remove history) IS mirrored to the Control
  Sheet's `chats_log` tab - see the column reference below.

**Full column reference:**

**Control Sheet** (`CONTROL_SHEET_ID` in `.env`, one per bot deployment,
owner-only, always kept in sync regardless of tier):

| Tab | Columns | Written by |
|---|---|---|
| `GROUPS` | CHAT_ID, CHAT_NAME, TYPE, SHEET_ID, SHEET_NAME, SUBS_DATE_START, SUBS_DATE_END, VISIBILITY, DATE_BOT_ADD | mirrors `all_groups`, on every `/setsub` |
| `CHANNELS` | CHAT_ID, CHAT_NAME, VISIBILITY, DATE_BOT_ADD | mirrors `all_channels` |
| `BOTCONFIG` | FEATURE_KEY, FEATURE, FREE, PRO, ADMIN, DESCRIPTION | mirrors `all_features`, on every `/updatefeature` |
| `chats_log` | CHAT_ID, DATE_BOT_ADD, DATE_BOT_REMOVE | mirrors `all_chats_bot_log` - the historical add/remove trail, pushed immediately whenever the bot is removed from a group/channel |

**Per-hub Sheet** (bound via `/setsheet`, PRO-only - a FREE hub writes
nothing to Sheets at all):

| Tab | Columns | Written by |
|---|---|---|
| `Users` | USER_ID, FIRST_NAME, LAST_NAME, USER_NAME, CHAT_ID, STATUS, DATE_start, DATE_end, ARCHIVED_USER_NAME | `/refreshusers`, `/refreshusersall` - one row per (user, chat); STATUS flips MEMBER/LEFT rather than deleting rows |
| `Events` | EVENT_ID, EVENT_NAME, CREATED_DATE, CREATED_BY, EVENT_DATE, CLOSED_AT, STATUS, GOING_COUNT | row appended on `/newevent`, columns F:H updated on Save & Close - CREATED_DATE/CLOSED_AT reuse `events.created_date`/`closed_date` (real DB columns, see the schema note above), not a timestamp computed fresh at Sheets-write time |
| `Actions` | EVENT_ID, ACTION, USER_NAME, USER_ID, DATE | every button click (going/notgoing/kick/save/...) |
| `EventUsers` | EVENT_ID, USER_ID | final attendee list, written once at Save & Close (main chat + every child chat combined) |
| `UserPresenceLog` | USER_ID, CHAT_ID, DATE_start, DATE_end | logged when someone leaves a monitored/main chat |

## How the DB and Sheets interact

SQLite is the source of truth and the only thing the bot ever *reads* back
- Sheets is a write-mostly export layer, never read to make a decision:

1. Every command works even with `GOOGLE_CREDENTIALS_JSON` unset or Sheets
   unreachable. `get_sheet_for_chat()` is a pure DB lookup (no network
   call) returning `None` for FREE tier or no sheet bound; separately,
   each `sync_*` function wraps its own `open_spreadsheet()`/API calls in
   a try/except that logs and returns on failure instead of raising -
   either way, a Sheets problem never blocks the user-facing action.
2. `all_groups`/`all_channels`/`all_features` are pushed to the Control
   Sheet *after* every write to those tables (`_push_control_sheet_*`),
   not read back from it - the Control Sheet is a live mirror for the
   owner to view, never a config source.
3. Per-hub tabs only ever get appended/updated in response to a specific
   SQLite-driven action (a button click, `/refreshusers`, Save & Close) -
   nothing about how the bot behaves is ever decided by what's currently
   in the Sheet.

## Global lock and command destination classification

`/lockbot on` (owner-only) is a single-row switch (`bot_lock` table) checked
by a dedicated handler (`main.py`'s `lock_gate`) registered *before every
other handler in the application* - when active, every command and button
click from anyone outside `OWNER_USER_IDS` is stopped immediately, with one
deliberate exception: Going/Not Going/ADD/Drop/ALL clicks on an
already-posted event still work for everyone, so a lockdown doesn't strand
people mid-RSVP on an event an owner already put up. Every other action
(admin verification buttons, Add Extra Member, all commands) is blocked.

Separately, every command falls into one of three categories describing
*where its result lands* relative to *where it was typed* (a group directly,
or a DM with the bot) - tracked explicitly in `utils.COMMAND_DESTINATION_TYPE`
and enforced for the DM-only category via `utils.require_dm_only()`:
1. **Dual-callable, result follows the caller** (the default - most commands).
2. **Dual-callable, but the result always lands in the group** (`/newevent`,
   `/editevent`, `/shareevent`, `/notify`) - e.g. `/notify`'s ping always
   goes to the group via `send_message`, since pinging people from inside a
   DM wouldn't reach them where they need to respond.
3. **DM only** (`/switchgroup`, `/start`, and every owner-only command) -
   calling one of these from a group gets an explicit rejection, not silence.
