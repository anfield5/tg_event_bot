# Bot Specification — Commands, Buttons, and Data Model

Navigation index + full data model. Commands with rich, multi-step behavior (`/newevent`, `/editevent`, `/shareevent`) have detailed test-scenario specs in `docs/specs/commands/` and `docs/specs/flags/` — this document links to those instead of repeating them. The Data Model section below is new: every SQLite table and every Google Sheets tab, with their exact columns and exactly when a write/update/delete happens, verified against the current code (not the README, which had 2 stale claims — noted where found).

---

# COMMANDS

## Where results go: DM vs group

Every command falls into exactly one of 3 categories, tracked explicitly
in `utils.COMMAND_DESTINATION_TYPE` (a plain dict, not a DB table — this
is a fixed architectural classification, not something that needs
runtime reconfiguration) and enforced for Type 3 via `utils.require_dm_only()`.

**Type 1 — dual-callable, result goes wherever it was called from**
(the default — not listed explicitly in the dict, everything absent
from it is Type 1): `/help`, `/userid`, `/chatid`, `/status`, `/stats`,
`/listusers`, `/listaliases`, `/listmonitors`, `/updateuser`,
`/adduser`, `/setalias`, `/removealias`, `/addmonitor`,
`/removemonitor`, `/refreshusers`, `/refreshusersall`, `/waitlist`.

**Type 2 — dual-callable, but the substantive result always lands in
the group**, regardless of where the command was typed:
`/newevent`/`/editevent` (the event post itself is always
`send_message`d to the hub; only errors/confirmations reply to the
caller), `/shareevent` (every message, including errors, goes to the
hub — a DM caller gets nothing back in their own DM at all), `/notify`
(the actual ping always goes to the group via `send_message`, since
pinging people from inside a DM wouldn't reach them where they need to
respond — a DM caller gets a brief separate confirmation in their own
DM instead, so they're not left wondering if anything happened).

**Type 3 — DM only, explicit error if called from a group**:
`/switchgroup`, `/start`, `/lockbot`, `/allgroups`, `/allchannels`,
`/updatefeature`, `/setsub`, `/setsheet`. Enforced by `require_dm_only()`
at the top of each — replies with `⛔️ /<command> only works in a DM
with the bot...` and returns, rather than silently doing nothing (the
older convention `/start`/`/switchgroup` used before this was made
explicit). For the 5 owner-only commands here (`lockbot`, `allgroups`,
`allchannels`, `updatefeature`, `setsub`), the DM-only check runs
**after** the owner check, not before — a non-owner calling from a
group must still get silence (the command's existence isn't revealed
to them); only an owner calling from the wrong place sees the DM-only
error. `/setsheet` keeps its own `resolve_hub_chat_id` call for
picking which group to bind (a DM alone doesn't know which group), but
the DM-only check runs before that resolution even starts.

# COMMANDS

## /start
No flags. First-touch onboarding — no group context yet, shows a welcome/help pointer.

## /switchgroup
No flags. DM-only. Lets a user pick which group their subsequent DM commands should target (the "sticky group" selection).

## /help
**-a** / **-admin** — shows the owner-only section (`/setsub`, `/allgroups`, `/allchannels`, `/updatefeature`) instead of the normal main menu. Gated on `OWNER_USER_IDS`, not chat admin status.
*(no flag = normal main menu with tier-aware buttons)*

## /userid
No flags. Replies with the caller's own numeric Telegram user ID.

## /chatid
No flags. Replies with the current chat's numeric ID.

## /newevent
**-d** / **-date**, **-gi** / **-goingicon**, **-ni** / **-notgoingicon**, **-limit**, **-wl** / **-waitlist**, **-ngl** / **-notgoinglist**, **-clc** / **-clickability**
Creates a new Going/Not-Going event in the current chat.
→ Full spec: `docs/specs/commands/newevent_editevent.md`

## /editevent
*(identical flags to `/newevent` — see there; only "unset flag keeps current value" is `/editevent`-specific)*
Edits the active event's name/icons/date/limit/visibility settings.
→ Full spec: `docs/specs/commands/newevent_editevent.md`

## /notify
No flags. Pings everyone tracked as active in this chat who hasn't voted yet on the active event.

## /updateuser
**-a** / **-active**, **-p** / **-passive** — marks a manually-listed user active/passive in this chat's tracked roster.

## /listusers
No flags. Shows every user tracked for this chat, with status.

## /refreshusers
No flags. Re-syncs the tracked user list against Telegram's admin list and (if premium) the Users sheet tab; retries resolving previously-unresolvable entries before removing them.

## /refreshusersall
No flags. Same as `/refreshusers`, but also covers every monitored child chat.

## /adduser
No flags. Manually adds a username to this chat's tracked roster.

## /shareevent
**-mgl** / **-maingoinglist**, **-sngl** / **-sharenotgoinglist**, **-swl** / **-sharewaitlist**, **-clc** / **-clickability** *(per-share override here)*
Forwards the active event to a child group/channel.
→ Full spec: `docs/specs/commands/shareevent.md`

## /waitlist
No flags. Admin-only: shows the full Waitlist, ignoring the event's own `-wl` visibility setting.

## /setalias
No flags. Registers a short name for a chat_id.

## /removealias
No flags. Deletes a registered alias.

## /listaliases
No flags. Lists every alias for this hub.

## /addmonitor
No flags. Adds a chat to this hub's monitored-chats list.

## /removemonitor
No flags. Removes a chat from the monitored list.

## /listmonitors
No flags. Lists monitored chats.

## /setsub
No flags. *(owner-only)* Sets/changes a group/channel's subscription tier and expiry.

## /lockbot
`on`/`off` argument, no flags. *(owner-only)* Global emergency switch — `on` makes the bot ignore every command and button click from anyone not in `OWNER_USER_IDS`, across every chat at once (not scoped to one group). `off` restores normal availability. Enforced by a dedicated gate handler that runs before every other handler, silently stopping all further processing for non-owners while locked — no reply, same as being offline.

## /updatefeature
**-minlevel**, **-limit** — *(owner-only)* changes a feature's own tier requirement and/or per-tier usage cap. **Note:** unrelated to `/newevent`'s `-limit` (event headcount) despite the identical flag string.

## /setsheet
No flags. Binds this group to its own Google Sheet.

## /status
No flags. Shows subscription type, expiry, bound sheet.

## /stats
No flags. Events created/closed + total/average headcount across closed events.

## /allgroups
**-pro** *(owner-only, filters to PRO-tier groups only)*

## /allchannels
No flags. *(owner-only)* Lists every channel the bot is in.

---

# BUTTONS

## Event post — open state
- **Going** *("Standby" label when at `-limit` capacity)* — marks going, or joins Waitlist if full.
- **Not Going** — marks not going, clears guest counters if previously going.
- **ADD** — adds one guest to the clicker's own count.
- **Drop** — removes one guest from the clicker's own count.
- **ALL** — drops all of the clicker's own guests at once; promotes one waiter per freed slot.
- **Verify** *(shown when `verification` feature is on)* — admin-only, locks voting, enters review mode.
- **Save&Close** *(shown when `verification` is off)* — admin-only, closes directly.
- **Cancel** — admin-only, cancels immediately.

## Event post — verification mode (main hub only)
- **[Participant name]** — non-clickable display row.
- **Kick** — admin-only, removes this person from going.
- **Return** — admin-only, restores a kicked person.
- **[guest counter]** — non-clickable display.
- **−** / **+** — admin-only, adjusts this person's guest count by one.
- **Add Extra Member** — admin-only, prompts for a username to add manually.
- **Save & Close Event** — admin-only, finalizes and exports the roster.

## /help main menu
**Users**, **Utility**, **Event Lifecycle**, **Distribution**, **Aliases**, **Monitoring**, **DM Access** — each opens its detail section; locked ones show a PRO badge and route to an upgrade prompt.

## /help navigation
- **🔙 Back** / **◀️ Back to /help** — returns to the main menu.
- **💬 Message the bot owner** — opens a DM link to the owner (shown on the owner-only screen).

## Group/hub picker (DM context)
- **[Group name]** — one per available group; used by `/switchgroup` and the "which group?" prompt for hub commands run from a DM.

## Pagination (`/allgroups`, `/allchannels`)
- **◀️ Prev** / **Next ▶️**

---

# DATA MODEL

## Part A — SQLite tables

### `all_groups`
**Columns:** `chat_id` (PK), `chat_name`, `type` (FREE/PRO), `sheet_id` (UNIQUE), `sheet_name`, `subs_date_start`, `subs_date_end`, `visibility`, `date_bot_add`

Only holds chats the bot is **currently** in as a group/supergroup — a hub. One row per chat, upserted by `chat_id`.

| Trigger | Operation |
|---|---|
| Bot added to a group (`my_chat_member` update) | INSERT (or UPDATE on conflict — `chat_name`/`visibility`/`date_bot_add` refreshed) via `register_chat_added()` |
| Bot removed from a group | Row moved out: INSERT into `all_chats_bot_log`, then **DELETE** from `all_groups`, via `register_chat_removed()` |
| `/setsub <chat_id> off` | UPDATE `type='FREE'` if the row exists, else INSERT a fresh FREE row |
| `/setsub <chat_id> on <days>` | UPDATE `type='PRO'`, `subs_date_start`/`subs_date_end` recalculated (extending an active sub stacks onto its current end date, doesn't reset it), else INSERT a fresh PRO row |
| `/setsheet` | UPDATE `sheet_id`/`sheet_name` only |
| Legacy value normalization (startup migration) | UPDATE `type='FREE'`/`'PRO'` where the old lowercase `'free'`/`'pro'` values are found |

### `all_channels`
**Columns:** `chat_id` (PK), `chat_name`, `visibility`, `date_bot_add`

Same presence-registry idea as `all_groups`, but channels have no subscription/sheet concept.

| Trigger | Operation |
|---|---|
| Bot added to a channel | INSERT (or UPDATE on conflict) via `register_chat_added()` |
| Bot removed from a channel | INSERT into `all_chats_bot_log`, then **DELETE** from `all_channels`, via `register_chat_removed()` |

### `all_chats_bot_log`
**Columns:** `id` (PK, autoincrement), `chat_id` (not unique — same chat can appear multiple times), `date_bot_add`, `date_bot_remove`

Append-only historical trail. Never updated or deleted — only ever grows.

| Trigger | Operation |
|---|---|
| Bot removed from any group or channel | INSERT (carries over the `date_bot_add` from the row being removed out of `all_groups`/`all_channels`) |

### `command_log`
**Columns:** `id` (PK, autoincrement), `chat_id`, `user_id`, `command`, `command_text` (full raw text as typed), `timestamp`

| Trigger | Operation |
|---|---|
| Every single command invocation, for every registered command, in every chat | INSERT — via a generic handler in `main.py`, not per-command logic |

Never updated or deleted by the bot itself (a pure usage log).

### `bot_lock`
**Columns:** `id` (PK, fixed to `1` via CHECK constraint — always exactly one row), `locked` (0/1)

Backs the `/lockbot` global emergency switch. A single-row table by design — there's only ever one lock state for the whole bot, not per-chat.

| Trigger | Operation |
|---|---|
| Startup / any `init_db()` run | INSERT OR IGNORE the single row with `locked=0` if it doesn't exist yet — never resets an already-set lock state on restart |
| `/lockbot on` | UPDATE `locked=1` |
| `/lockbot off` | UPDATE `locked=0` |

Read on every single incoming command and callback query (`main.py`'s `lock_gate` handler, registered before every other handler) — deliberately a fast, index-free single-row lookup.

### `all_features`
**Columns:** `feature_key` (PK), `feature_label`, `min_tier` (FREE/PRO/ADMIN), `limit_count`, `sort_order`, `description`

The single source of truth for what's available at each tier. Seeded at startup from a hardcoded list. *(Renamed from `feature_flags` — old DBs get a one-time `ALTER TABLE ... RENAME TO`, data preserved.)*

| Trigger | Operation |
|---|---|
| Startup / any `init_db()` run | INSERT OR IGNORE for every feature in the current seed list (doesn't overwrite an existing row's `min_tier`/`limit_count` — those are admin-configurable state) |
| Startup / any `init_db()` run | UPDATE `feature_label`/`sort_order`/`description` for every seeded feature (these ARE always refreshed to match the current code, unlike tier/limit) |
| Startup / any `init_db()` run | **DELETE** any row whose `feature_key` is no longer in the current seed list (removed/renamed features get pruned) |
| `/updatefeature -minlevel ...` and/or `-limit ...` | UPDATE `min_tier` and/or `limit_count` for one specific `feature_key` |
| One-time legacy migration (old per-tier limit columns) | UPDATE `limit_count` from whichever of the 3 old columns matched the row's current tier, then those 3 old columns are dropped |

### `events`
**Columns:** `event_id` (PK), `chat_id`, `message_id`, `name`, `going_icon`, `notgoing_icon`, `event_status` (-1 canceled / 0 open / 1 verification / 2 closed), `going_data` (JSON, **frozen/vestigial** — see below), `notgoing_data` (JSON, **frozen/vestigial**), `counters_data` (JSON, **frozen/vestigial**), `event_date`, `kicked_data` (JSON, **frozen/vestigial**), `feature_snapshot` (JSON, frozen at creation), `total_limit`, `waitlist_data` (JSON — still live, see below), `waitlist_open`, `waitlist_visibility`, `notgoing_visibility`, `clickability`, `created_by_user_id`

**Variant B note:** `going_data`/`notgoing_data`/`counters_data`/`kicked_data` were the master hub's own source of truth *before* Variant B unified the master hub and every child chat under the single `event_users` table (see that table's own section below). These four columns are no longer written to by any button click or command — they're read exactly once per event, the very first time ANY code path touches that event after the Variant B deploy, to migrate their contents into `event_users` (see `db.migrate_event_to_event_users()`). After that one-time migration, they sit frozen, kept only so that an event nobody has touched yet doesn't render as empty (a fallback path in `update_all_shared_views` reads them directly if `event_users` has no rows yet for that event).

| Trigger | Operation |
|---|---|
| `/newevent` | INSERT (all fields set from flags/defaults; the four frozen columns start as empty JSON) |
| `/newevent`, right after the message is sent | UPDATE `message_id` only (the real Telegram message ID wasn't known at INSERT time) |
| `/editevent` | UPDATE `name`/`going_icon`/`notgoing_icon`/`event_date`/`total_limit`/`waitlist_visibility`/`notgoing_visibility`/`clickability` — one combined UPDATE at the end, only for fields whose flag was actually given |
| `/editevent -limit` raising the cap and promoting from the Waitlist | UPDATE `waitlist_data` only (the promoted person's own going/guest state goes into `event_users`, not this table) |
| Going/Not Going/ADD/Drop/ALL/Kick/Return/±guest/Save/Cancel button clicks | UPDATE `event_status`, `waitlist_data` only, once per click, only if the click actually changed something (`data_changed`) — the participant's own state goes into `event_users` |
| Add Extra Member (verification mode) | No write to this table at all — goes directly into `event_users` |
| A person joining the Waitlist (event at capacity) | UPDATE `waitlist_data` only |
| A person being promoted from the Waitlist (slot freed by Drop/ALL/Not Going/limit raise) | UPDATE `waitlist_data` only |
| `/waitlist` finding stale duplicate entries in the queue on read | UPDATE `waitlist_data` only (persists a one-time dedup cleanup) |
| Startup migrations (older DBs missing newer columns) | UPDATE `waitlist_visibility`/`notgoing_visibility`/`clickability` to their default value where `NULL` |

No DELETE ever happens on this table — a canceled or closed event's row stays forever (its `event_status` just changes).

### `main_group_users`
**Columns:** `chat_id`, `username`, `user_id`, `status` (active/passive), `first_name`, `last_name` — composite PK `(chat_id, username)`

The tracked-roster table used by `/listusers`, `/notify`, `/refreshusers`, and every mention-resolution lookup.

| Trigger | Operation |
|---|---|
| Anyone clicking Going/Not Going/any event button, joining/being added to a chat, `/adduser`, `/updateuser`, `@everyone` mentions | INSERT ... ON CONFLICT DO UPDATE (upsert) via `track_user()` — updates `status` always; `user_id`/`first_name`/`last_name` only if a non-null value is actually being passed (never overwrites a good value with a blank one) |
| `/refreshusers` finding someone no longer in the chat (and unresolvable via the admin list) | **DELETE** by `(chat_id, username)` |
| `/refreshusersall`, same logic, per monitored chat | **DELETE** by `(chat_id, username)` |

### `event_shares`
**Columns:** `share_id` (PK, autoincrement), `event_id`, `chat_id`, `message_id`, `share_mode` (-visible/-onlycount/-hidden — the `-mgl` value), `chat_type`, `share_notgoing_visibility`, `share_waitlist_visibility`, `share_clickability` — UNIQUE `(event_id, chat_id)`

| Trigger | Operation |
|---|---|
| `/shareevent` succeeding | INSERT OR REPLACE (the UNIQUE constraint means a retry after a prior failure replaces rather than duplicates) |

Never updated after creation (change a share's settings by removing and re-sharing — no dedicated "edit share" command exists) and never explicitly deleted by any current command.

### `sub_chats`
**Columns:** `id` (PK, autoincrement), `chat_id`, `owner_chat_id`, `alias`, `is_monitored` (0/1), `chat_type`, `chat_name` — UNIQUE `(owner_chat_id, alias)`, UNIQUE `(owner_chat_id, chat_id)`

One row per (hub, related chat) relationship — a chat can be an alias target, monitored, or both, through the same row.

| Trigger | Operation |
|---|---|
| `/setalias` on a chat with no existing row for this hub | INSERT with `alias` set |
| `/setalias` on a chat that already has a row (e.g. already monitored) | UPDATE `alias` only, on the existing row |
| `/removealias` | UPDATE `alias = NULL` (row is **kept** if it's still monitored) **or** DELETE the row entirely if it was alias-only |
| `/addmonitor` on a chat with no existing row | INSERT with `is_monitored=1` |
| `/addmonitor` on a chat that already has a row (e.g. already aliased) | UPDATE `is_monitored=1`/`chat_type`/`chat_name`, on the existing row |
| `/removemonitor` | UPDATE `is_monitored=0` (row **kept** if it still has an alias) **or** DELETE the row entirely if it was monitor-only |

### `event_users` — the unified participant table (Variant B)
**Columns:** `event_id`, `chat_id`, `user_id`, `username`, `first_name`, `last_name`, `status` (`going`/`notgoing`/`kicked`/`notselected`), `guests` — composite PK `(event_id, chat_id, user_id)`

**The single source of truth for who's participating in an event, from ANY chat it exists in** — the master hub included. Before Variant B, the master hub's own going/notgoing/guest state lived in the `events` table's own JSON columns (see above), completely separate from child chats, which always used this table. Variant B unified both under this one table: the master hub is simply `chat_id = <the hub's own chat_id>`, same as any child share.

`first_name`/`last_name` are stored directly on the row (not resolved via a separate `main_group_users` lookup at render time) — this is deliberate: a display name that travels with the row can never go stale or fail to resolve if `main_group_users` doesn't have a matching entry for any reason.

`status = 'notselected'` is for someone who's only ever adjusted their own guest count (via ADD) without personally clicking Going or Not Going at all — distinct from `'kicked'` so the verification-mode keyboard can tell the two apart (only a genuinely-kicked person gets a Return button).

An unresolvable person (e.g. added via Add Extra Member with no real Telegram user_id available) uses their **username itself** as the `user_id` value — a deliberate fallback, not a bug: `_mention_link()` already renders a non-numeric `user_id` as plain text, so this displays correctly without ever needing a real Telegram id. If that same person later performs a genuine click (which always carries a real numeric id), the stale placeholder row is **renamed** (its `user_id` updated in place, preserving `guests`/`status`) rather than left to duplicate alongside a fresh row.

| Trigger | Operation |
|---|---|
| The FIRST click/command that touches an event since the Variant B deploy | One-time migration (`db.migrate_event_to_event_users()`) converts the event's old `going_data`/`notgoing_data`/`counters_data`/`kicked_data` into rows here for `chat_id = <the hub's own chat_id>`. Detected by "does this event have ANY row here yet" — safe to call repeatedly, a no-op once migrated |
| Going click (master hub or any child chat — identical code path) | INSERT OR REPLACE with `status='going'` |
| Going click while the event is at capacity | No row written here — the person goes to the event-wide Waitlist instead (`events.waitlist_data`) |
| Not Going click | INSERT OR REPLACE with `status='notgoing'` — **never deleted**, regardless of guest count (a permanent record of anyone who ever clicked, independent of whether they currently have guests) |
| ADD (guest) click, person has no prior status | INSERT with `status='notselected'`, `guests=1` |
| ADD (guest) click, person already has a status | UPDATE `guests` only — their own going/notgoing/kicked status is untouched |
| Drop (guest) click | UPDATE `guests = guests - 1` |
| ALL (drop all guests) click | UPDATE `guests = 0` |
| Kick (verification mode) | UPDATE `status = 'kicked'` |
| Return (verification mode) | UPDATE `status = 'going'` |
| ±guest buttons (verification mode) | UPDATE `guests = guests + 1` / `guests - 1` |
| Add Extra Member (verification mode) | INSERT OR REPLACE with `status='going'` — a real `user_id` if resolvable via `main_group_users`, otherwise the username itself as a fallback |
| A genuine click by someone who has a stale unresolvable placeholder row (username-as-id, from a legacy Add Extra Member) | UPDATE, renaming that row's `user_id` to the real one (preserving `guests`/`status`) — or DELETE it if a row under the real id already exists too |
| `/editevent -limit` raise, promoting someone from the Waitlist | INSERT OR REPLACE with `status='going'`, `guests=0` (or `guests + 1` for a promoted guest-slot entry) — works identically whether the target is the master hub or a child chat |

---

## Part B — Google Sheets tabs

Two categories: the **Control Sheet** (one, shared across the whole bot, tracked via `CONTROL_SHEET_ID`) and **per-hub sheets** (one Google Sheet per premium hub, bound via `/setsheet`). Every per-hub write is silently skipped entirely on FREE tier or if no sheet is bound — by design, not an error.

### Control Sheet — `GROUPS` tab
**Columns:** CHAT_ID, CHAT_NAME, TYPE, SHEET_ID, SHEET_NAME, SUBS_DATE_START, SUBS_DATE_END, VISIBILITY, DATE_BOT_ADD

Full overwrite-and-trim mirror of the entire `all_groups` SQLite table (not incremental row edits).

| Trigger | Operation |
|---|---|
| Bot startup | Full re-push |
| Bot added to or removed from any group | Full re-push |
| `/setsub` (on or off) | Full re-push |

### Control Sheet — `CHANNELS` tab
**Columns:** CHAT_ID, CHAT_NAME, VISIBILITY, DATE_BOT_ADD

Same full-mirror pattern as `GROUPS`, sourced from `all_channels`.

| Trigger | Operation |
|---|---|
| Bot startup | Full re-push |
| Bot added to or removed from any channel | Full re-push |

### Control Sheet — `BOTCONFIG` tab
**Columns:** FEATURE_KEY, FEATURE, FREE, PRO, ADMIN, DESCRIPTION

Full mirror of `all_features`.

| Trigger | Operation |
|---|---|
| Bot startup | Full re-push |
| `/updatefeature` | Full re-push |

### Control Sheet — `chats_log` tab
**Columns:** CHAT_ID, DATE_BOT_ADD, DATE_BOT_REMOVE

Full mirror of `all_chats_bot_log`.

| Trigger | Operation |
|---|---|
| Bot startup | Full re-push |
| Bot removed from any group or channel | Full re-push |

### Per-hub sheet — `Users` tab
**Columns:** USER_ID, FIRST_NAME, LAST_NAME, USER_NAME, CHAT_ID, STATUS, DATE_start, DATE_end, ARCHIVED_USER_NAME

One row per (person, chat) they were ever tracked in — never deleted, only status-flipped.

| Trigger | Operation |
|---|---|
| `/refreshusers`/`/refreshusersall` finding someone new | `append_row` — new row, `STATUS='MEMBER'`, `DATE_start` set, `DATE_end` blank |
| Same commands, person already tracked, username changed | UPDATE `USER_NAME` cell + append the old name to `ARCHIVED_USER_NAME` |
| Same commands, person already tracked, name/first/last changed | UPDATE the relevant cell(s) directly (no archiving for name fields, only for username) |
| Same commands, person previously `LEFT` and now present again | UPDATE `STATUS='MEMBER'`, refresh `DATE_start`, clear `DATE_end` |
| Same commands, a previously-`MEMBER` person no longer present | UPDATE `STATUS='LEFT'`, set `DATE_end`; also triggers a `UserPresenceLog` append (see below) |

No row is ever deleted from this tab — only the `STATUS`/date columns change.

### Per-hub sheet — `Events` tab
**Columns:** EVENT_ID, EVENT_NAME, CREATED_AT, CREATED_BY, EVENT_DATE, CLOSED_AT, STATUS, AMOUNT

*(Note: the project README lists these last two as `CREATED_DATE`/`GOING_COUNT` — that's stale; the actual code comment and every write site use `CREATED_AT`/`AMOUNT`.)*

| Trigger | Operation |
|---|---|
| `/newevent` | `append_row` — `STATUS='OPEN'`, `AMOUNT=0`, `CLOSED_AT` blank |
| `/editevent` changing the name and/or date | UPDATE cell B (`EVENT_NAME`) and/or cell E (`EVENT_DATE`) only, row found by matching `EVENT_ID` |
| Save & Close (event found in the sheet) | UPDATE cells F:H together (`CLOSED_AT`, `STATUS='CLOSED'`, `AMOUNT`=final combined headcount) |
| Save & Close (event's row somehow missing from the sheet) | `append_row` — a full new row is written instead, already in the CLOSED state |
| Cancel (event found in the sheet) | UPDATE cells F:H together (`CLOSED_AT`, `STATUS='CANCELED'`, `AMOUNT=0`) |
| Cancel (row missing) | `append_row`, already in the CANCELED state |

### Per-hub sheet — `Actions` tab
**Columns:** EVENT_ID, ACTION, USER_NAME, USER_ID, DATE, CHAT_ID

*(Note: the README lists only 5 columns, omitting `CHAT_ID` — stale; every one of the 3 write sites appends all 6.)*

Pure append-only action log — one row per state-changing button click or Add Extra Member use. Never updated or deleted.

| Trigger | Operation |
|---|---|
| Any state-changing click in the master hub or a child chat (going/notgoing/add/sub/dropall/kick/return/save/cancel) | `append_row`, `ACTION` = the raw action name, uppercased |
| ± guest buttons specifically, in verification mode | `append_row`, `ACTION` logged as the friendlier `ADD_editmode`/`SUB_editmode` instead of the raw `incgst`/`decgst` |
| Add Extra Member | `append_row`, `ACTION='ADD_EXTRA_PLAYER'`, `USER_NAME`/`USER_ID` = the **admin who clicked**, not the person being added |

### Per-hub sheet — `EventUsers` tab
**Columns:** EVENT_ID, USER_ID

The final attendee roster — written exactly once per event, at Save & Close.

| Trigger | Operation |
|---|---|
| Save & Close, successfully | `append_rows` (batch) — every going `user_id`, master hub **and** every child chat combined, one row each |
| Cancel | **Nothing is written here at all** — intentionally, since a canceled event has no final attendee list |

### Per-hub sheet — `UserPresenceLog` tab
**Columns:** USER_ID, CHAT_ID, DATE_start, DATE_end

| Trigger | Operation |
|---|---|
| A tracked person found no longer present during `/refreshusers`/`/refreshusersall` (the `Users` tab MEMBER→LEFT transition) | `append_row` — but only if no existing row already has the exact same `(USER_ID, CHAT_ID, DATE_start)` combination, to avoid duplicate log entries |

Never updated or deleted — a pure log.
