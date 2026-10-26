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
`/removemonitor`, `/refreshusers`, `/refreshusersall`, `/waitlist`, `/who`, `/reopen`.

`/chatid` in a private DM answers with the id of the DM's **selected group** (and its name), not the DM's own chat id; it skips the PRO DM-access gate (`resolve_hub_chat_id(..., dm_access_required=False)`) because the id of a FREE group is what an owner needs for `/setsub`.

Every dual-callable command is registered with `register_hub_command(<its real command name>)`: that name is what the DM group-picker stores and later replays, and what a FREE group's "DM is a PRO feature" notice tells the user to type. The replay runs on a stand-in update/context (`_ReplayUpdate`/`_ReplayContext`) - the context forwards anything it doesn't carry (`application`, `chat_data`, ...) to the context of the picker button's press.

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
`/updatefeature`, `/setsub`, `/setsheet`, `/showtable`, `/version`. Enforced by
`require_dm_only()` at the top of each — replies with `⛔️ /<command>
only works in a DM with the bot...` and returns, rather than silently
doing nothing (the older convention `/start`/`/switchgroup` used
before this was made explicit). For the 7 owner-only commands here
(`lockbot`, `allgroups`, `allchannels`, `updatefeature`, `setsub`,
`showtable`, `version`), the DM-only check runs **after** the owner check, not
before — a non-owner calling from a group must still get silence (the
command's existence isn't revealed to them); only an owner calling
from the wrong place sees the DM-only error. `/setsheet` keeps its own
`resolve_hub_chat_id` call for
picking which group to bind (a DM alone doesn't know which group), but
the DM-only check runs before that resolution even starts.

# COMMANDS

## /start
No flags. First-touch onboarding — no group context yet, shows a welcome/help pointer.

## /switchgroup
No flags. DM-only. Lets a user pick which group their subsequent DM commands should target (the "sticky group" selection).

## /help
**-o** / **-owner** — shows the owner-only section (`/setsub`, `/allgroups`, `/allchannels`, `/updatefeature`) instead of the normal main menu. Gated on `OWNER_USER_IDS`, not chat admin status.
*(no flag = normal main menu with tier-aware buttons)*

## /userid
No flags. Replies with the caller's own numeric Telegram user ID.

## /chatid
No flags. Replies with the current chat's numeric ID.

## /newevent
**-d** / **-date**, **-gi** / **-goingicon**, **-ni** / **-notgoingicon**, **-limit**, **-wl** / **-waitlist**, **-ngl** / **-notgoinglist**, **-clc** / **-clickability**
Creates a new Going/Not-Going event in the current chat.
→ Full spec: `docs/specs/commands/newevent_editevent.md`

## /deleteevent
`/deleteevent` lists the latest 10 events of the hub as buttons; `/deleteevent <part of name or event id>` finds by exact `event_id` (the EVENT_ID column of the Sheet's Events tab) or by case-insensitive name substring (done in Python - SQLite's `LOWER()`/`LIKE` only fold ASCII, which would miss Cyrillic). One match goes straight to the confirmation screen, several are listed.
Permanently removes ONE event: its `events` / `event_users` / `event_shares` rows, every row of it in the hub Sheet's `Events` / `Actions` / `EventUsers` tabs (one `batchUpdate` request, ranges applied bottom-to-top), and - best effort - its Telegram posts in the hub and every shared chat. Always asks for confirmation; irreversible.
**Gating:** its own feature (`deleteevent` in `all_features`), seeded at `min_tier` **OWNER** = bot owners only (`OWNER_USER_IDS`); anyone else gets total silence and the `/help` line is hidden. The gate (`subscription.require_feature`) follows the CURRENT tier, so opening it up later is just `/updatefeature deleteevent -minlevel pro` (PRO hubs, with the usual upgrade message for FREE) or `-minlevel free` - no code change. Button presses re-check the gate, so a stale confirmation can't outlive a tier change.
Order: Sheet first - if it can't be cleaned, NOTHING is deleted anywhere (no half-deleted event) - then the DB (one transaction, under the event's lock), then the posts. Admins may delete any event of the hub, other members only events they created; re-checked on every button press and scoped to the hub in the callback.

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

## /reopen
`/reopen on | off | <event name or id>` (no argument shows the state) - an **owner-level** feature (`reopen`, OWNER tier; for anyone without it the command is ignored without a word). `on`/`off` switch a per-hub mode, stored in the `reopen_mode` table (who and when are kept), that puts a **🔓 Reopen** button under the group's **recent closed or canceled events** (the newest `db.REOPEN_BUTTON_LIMIT` = 15); pressing one sends that event back to **open** (`event_status` 0, `closed_date` cleared) and re-renders the hub post and every shared post, so voting works again. `/reopen <part of the name | exact event id>` does the same for any event, old ones included, with the mode on or off (an ambiguous name lists the matching ids and reopens nothing).
- **Any** closed or canceled event can be reopened, but only **one event of the group may be active at a time** (`/editevent`, `/waitlist`, `/who`, `/notify` and `/newevent`'s "already active" check all look at the newest one): while an event is open or in verification **no button is shown** (`db.reopen_button_events` returns nothing), and a press or the command is refused naming that event. Only the hub post carries the button (child posts never do).
- The buttons are kept in line with that rule by `event_engine.refresh_reopen_buttons` - only each post's **keyboard** is edited, one post at a time with a 0.35 s pause (Telegram flood limits), the newest 2x15 closed/canceled posts so one that slid out of the window loses its button. It runs on `/reopen on|off`, after a close/cancel (the end of `update_all_shared_views`), when `/newevent` creates an event (the old posts lose the button), after a reopen (`perform_reopen`) and after `/deleteevent` of the active event (the old posts regain it). A gone or already-correct post is not an error.
- The button is visible to everyone while the mode is on (an inline keyboard can't be shown to one person); a non-owner gets an "Owners only" popup. The press is its own callback handler (`^reopen_`), not part of `button_handler`, because `button_handler` answers every callback before knowing what it is and a refusal needs its own popup. Button and command share `reopen.perform_reopen`, which decides everything again under the event's lock.
- Sheet side (`sheets.reopen_event_in_sheet`): the `Events` row goes back to `CLOSED_AT` empty / `OPEN` / `0`, the event's `EventUsers` rows are **removed** (an open event has no final roster; the next Save & Close writes the new one - it REPLACES whatever the event has there, see the `EventUsers` tab below) and an `Actions` row `REOPEN` names the owner. The sheet is cleaned first and the whole reopen abandoned if that fails ("nothing was changed"); redoing it is harmless.
- **What changed while it was open again** is logged by the next Save & Close: `perform_reopen` stores the roster the event was closed with in `events.reopened_roster` (JSON `{user_id: name}`; `NULL` for a canceled event, which never had one), and `event_engine._log_reopen_roster_diff` appends one `Actions` row per person **added** (`ROSTER_ADDED`) or **removed** (`ROSTER_REMOVED`) - the person in `USER_NAME`/`USER_ID`, since these are not clicks - then clears it. It is kept, not cleared, when the Sheets write fails, so the next try still has it.
- The mode stays on after a press (the buttons just aren't shown while an event is open and come back when it is closed or canceled again); `/reopen off` removes them.

## /who
`/who [hours] -ng | -l [-c <alias|main|all>]` - admin-only, PRO-level feature `who` (adjustable with `/updatefeature`), needs the hub's Google Sheet; works from a DM like the other hub commands. `hours` is optional (1-8760; **6 when omitted**, so `/who -ng` means the last 6 hours); `-ng`/`-notgoing` and `-l`/`-left` are synonyms; `-c`/`-chat` narrows the report; at least one of `-ng`, `-l` is required and both may be combined.
- **`-ng`** - people whose status in the **latest event** of the hub (newest by creation, any status) changed to Not Going at or after `now - hours`, read from the `Actions` tab. Only a real *change* counts: the person must have an earlier Going-type action (`GOING`, `ADD`, `SUB`, `DROPALL`) in that same event - a first-time Not Going is skipped. Admin rows (`KICK`, `RETURN`, ...) name the admin, not the person acted upon, so they say nothing about anybody's own status (kicks are therefore not reported). Someone who went back to Going afterwards is marked "(now Going again)". Known limit: a Going click that ended in the waitlist is logged as `GOING` too.
- **`-l`** - every stay that ended in the window, from `UserPresenceLog` (names from `Users`; `Users` rows marked `LEFT` are a safety net), grouped by chat. The time shown is when a refresh *noticed* the departure.
- **`-c`** - omitted or `all`: the main group + every monitored chat (for `-ng`: no filter at all); `main`: the main group; otherwise an alias from `/listaliases` or a chat id this hub knows (for `-ng` it filters by the chat the click came from; for `-l` the chat must be monitored). `main` and `all` are reserved, `/setalias` refuses them. The main group is deliberately **not** a monitor/alias: `/refreshusersall` already processes it first (adding it would process it twice), and an alias pointing at the hub is rejected by `/shareevent` as a self-share.

Names are printed without `@` or links so that nobody listed gets a notification; a long report is split into messages under 4,000 characters.

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
No flags. Works from a private DM too: the group is the DM's selected hub (like `/stats`, `/listusers`; DM access is a PRO feature), and the full hub-style list is shown. Admin-only: shows the full Waitlist, ignoring the event's own `-wl` visibility setting.

## /setalias
Alias names are stored lower-case; `main` and `all` are reserved (they mean "the main group" and "every tracked chat" in `/who -c`) and are refused.
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
`on`/`off` argument, no flags. *(owner-only)* Global emergency switch — `on` makes the bot stop everyone not in `OWNER_USER_IDS`, across every chat at once (not scoped to one group), with one deliberate exception: Going/Not Going/ADD/Drop/ALL clicks on an already-posted event still work for everyone, so a lockdown doesn't strand people mid-RSVP. Enforced by a dedicated gate handler (`main.py`'s `lock_gate`) that runs before every other handler. A blocked command gets a reply with a button to message the bot owner (`utils.get_admin_contact()` — the same contact info shown in the premium-upgrade flow); a blocked button click gets a `show_alert` popup with the same contact URL as plain text. `off` restores normal availability.

## /version
No arguments. *(owner-only, DM-only)* Shows which build is actually RUNNING: the version (`BOT_VERSION`), when the process started and its uptime (so "did my deploy really restart it?" has an answer), and the Python / python-telegram-bot versions. Non-owners get silence, so the command's existence isn't revealed. Listed in BOTCONFIG as feature `version` at level OWNER ("no / no / yes" under FREE / PRO / OWNER) - informational: enforcement stays on `OWNER_USER_IDS`, like the `/setsub` and `/allgroups` rows, so lowering the level there does not open the command.

## /updatefeature
**-minlevel**, **-limit** — *(owner-only)* changes a feature's own tier requirement and/or per-tier usage cap. **Note:** unrelated to `/newevent`'s `-limit` (event headcount) despite the identical flag string.

## /showtable
`table_name` and `sheet_name` arguments, no flags. *(owner-only)* Runs `SELECT * FROM table_name` against the bot's own SQLite database and writes the results into the named tab of the Control Sheet (`EventBot_Config`), overwriting whatever was there before. `table_name` is checked against `sqlite_master`'s real table list before ever being interpolated into a raw SQL string — table/column names can't be passed as `?` placeholder parameters in SQLite, so this whitelist check IS the actual SQL-injection safety boundary here. `sheet_name` must already exist as a tab — this only writes into an existing tab, never creates one; an unknown name gets an explicit warning.

## /setsheet
No flags. Binds this group to its own Google Sheet.

## /status
No flags. Shows subscription type, expiry, bound sheet.

## /stats
No flags. Events created/closed + total/average headcount across closed events.
Beyond created/closed/headcount, the per-group report shows how events ended (`cancelled`, `open now`, `% completed` = closed ÷ created), the guest share of attendance, "not going" clicks and - for closed events with a `-limit` - the average fill (e.g. 19.4 of 21 spots), how many events hit the limit, and the waitlist (`joined` / `still waiting at close`). "Hit the limit" = final headcount reached it OR anyone ever joined the waitlist. People who got IN from the waitlist can't be read back from `waitlist_data` (promotion deletes the entry), so `events.waitlist_joined` counts joins, fed by a SQLite trigger on `waitlist_data` (counted from the version that introduced it; "joined" is never shown lower than the people still waiting).
The **👥 Users** screen opens with `Unique attendees: N` - how many DIFFERENT people (by user id) attended at least one closed event in the chosen period (all time by default; `/stats 30d` etc. narrows it). It is built from the same rows as the top lists: `going` people of the period's closed events in the hub and in every shared chat; guests, not-going and kicked people don't count, and someone who came to ten events counts once.
`/stats -o | -owner` (owner-only) is the bot-wide report: group/channel counts, admin rights, FREE/PRO split (**PRO = active subscription**, same rule as `is_premium`: an expired `type='PRO'` row counts as FREE and shows up under "expired"), PRO expiring within 7/30 days and expired in the last 30, bot added/removed in 7/30 days (current chats' `date_bot_add` + `all_chats_bot_log`), active vs dormant groups (a command in `command_log` or an event created/closed), events created/closed and people served, and the top-5 commands and top-5 groups of the last 30 days with their tier. Commands typed in a DM can't be tied to a hub, so they count toward top commands only.

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
- **Verify** *(shown when `verification` feature is on)* — admin-or-creator, locks voting, enters review mode.
- **Save&Close** *(shown when `verification` is off)* — admin-or-creator, closes directly.
- **Cancel** — admin-or-creator, cancels immediately.

## Event post — verification mode (main hub only)
- **[Participant name]** — non-clickable display row.
- **Kick** — admin-or-creator, removes this person from going.
- **Return** — admin-or-creator, restores a kicked person.
- **[guest counter]** — non-clickable display.
- **−** / **+** — admin-or-creator, adjusts this person's guest count by one.
- **Add Extra Member** — admin-or-creator, prompts for a username (or numeric id) to add manually. The person is looked up, case-insensitively, in `main_group_users` of the hub, then of every chat the event is shared to, then of every chat the hub **monitors** (first match wins, hub first) and recorded under **that** chat, so they appear in its "Going from <chat>" block - for a monitored chat the event was never shared to the block exists in the hub post only (there is no child post), and they count in TOTAL. Someone already in the event keeps their row (no second one). Nobody knows them → plain text under the hub, plus a notice saying why. **Anonymous admins** ("Remain anonymous") work too: the button press is a callback query and carries the real admin, but the typed username arrives from `GroupAnonymousBot`, whose `user_data` is not the one the press filled - so the prompt is also kept in `chat_data` for 10 minutes, readable only by an anonymous-admin post (an ordinary member's message can never consume it), and the `Actions` row names the admin who pressed the button.
- **🔙 Back** — admin-or-creator, reverts to open (`event_status` 0) for an accidentally-clicked Verify, without going through Save & Close or Cancel. Doesn't touch any kick/guest/extra-member data already changed while in verification, and doesn't set `closed_date`.
- **Save & Close Event** — admin-or-creator, finalizes and exports the roster.
- **◀️ Prev / N/M / Next ▶️** — pagination controls, shown only when there's more than one page (see below).

**Pagination (real bug fixed):** each participant renders as 2 rows / 5
buttons (name+Kick/Return, guest-count+minus+plus). With enough people this
exceeds Telegram's practical ~100-button-per-keyboard limit — reported
incident: an event with 21 going participants (105+ buttons) got
permanently stuck in verification mode, since Telegram silently rejects
every re-render attempt (Kick/Guest/Add Extra Member/Save & Close all stop
visibly working, though the underlying DB state is untouched — the fix has
nothing to do with the data itself). Fixed via `keyboard.VERIFICATION_PAGE_SIZE`
(10 people/page, keeping every page comfortably under the limit no matter
the total roster size) — master hub and every shared/monitored child chat's
participants are combined into ONE list before paginating, so the button
count is genuinely capped regardless of how many different chats
contributed people. The current page is stored in
`event_engine._verification_pages[event_id]` (a plain module-level dict,
matching this same module's existing `_event_locks`/`_refresh_state`
pattern) via a new `vpage_<event_id>:<page>` callback action — this
preserves the admin's page across Kick/Guest/Add Extra Member clicks
rather than resetting to page 0 every time, and an out-of-range page (e.g.
after kicks shrink the roster) clamps to the last valid page rather than
showing empty or crashing. **Real bug found in an earlier version of this
fix**: the page was originally stored in `context.application.chat_data`,
which PTB v20+ made read-only at the top level (`.setdefault()`/item
assignment on it raises — only mutating an *already-existing* inner
per-chat dict is sanctioned). This silently broke Prev/Next in actual
production while still passing in tests, since the test mocks used a
plain, unrestricted dict instead of PTB's real restricted mapping — a
reminder that mocking a third-party object's *shape* isn't the same as
mocking its *behavior/constraints*. Not persisted across a bot restart
(an acceptable, minor UX tradeoff for something this transient).

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
**Columns:** `chat_id` (PK), `chat_name`, `type` (FREE/PRO), `sheet_id` (UNIQUE), `sheet_name`, `subs_date_start`, `subs_date_end`, `visibility`, `date_bot_add`, `role` (the bot's OWN status in this chat — MEMBER/ADMIN)

Only holds chats the bot is **currently** in as a group/supergroup — a hub. One row per chat, upserted by `chat_id`.

| Trigger | Operation |
|---|---|
| Bot added to a group (`my_chat_member` update) | INSERT (or UPDATE on conflict — `chat_name`/`visibility`/`date_bot_add`/`role` refreshed) via `register_chat_added()`, `role` set from the add's own `new_chat_member.status` (ADMIN if added straight in as administrator) |
| Bot promoted/demoted in a group without leaving (`my_chat_member`, `member` ↔ `administrator`) | UPDATE `role` only, via `update_chat_role()` — previously silently ignored entirely (real bug fixed: the handler's own `else: return` branch covered this exact transition) |
| Bot removed from a group | Row moved out: INSERT into `all_chats_bot_log`, then **DELETE** from `all_groups`, via `register_chat_removed()` |
| `/setsub <chat_id> off` | UPDATE `type='FREE'` if the row exists, else INSERT a fresh FREE row |
| `/setsub <chat_id> on <days>` | UPDATE `type='PRO'`, `subs_date_start`/`subs_date_end` recalculated (extending an active sub stacks onto its current end date, doesn't reset it), else INSERT a fresh PRO row |
| `/setsheet` | UPDATE `sheet_id`/`sheet_name` only |
| Legacy value normalization (startup migration) | UPDATE `type='FREE'`/`'PRO'` where the old lowercase `'free'`/`'pro'` values are found |

### `all_channels`
**Columns:** `chat_id` (PK), `chat_name`, `visibility`, `date_bot_add`, `role` (the bot's OWN status in this channel — MEMBER/ADMIN)

Same presence-registry idea as `all_groups`, but channels have no subscription/sheet concept.

| Trigger | Operation |
|---|---|
| Bot added to a channel | INSERT (or UPDATE on conflict, including `role`) via `register_chat_added()` |
| Bot promoted/demoted in a channel without leaving | UPDATE `role` only, via `update_chat_role()` — same fix as `all_groups`' own entry above |
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
**Columns:** `feature_key` (PK), `feature_label`, `min_tier` (FREE/PRO/OWNER), `limit_count`, `sort_order`, `description`

The single source of truth for what's available at each tier. Seeded at startup from a hardcoded list. *(Renamed from `feature_flags` — old DBs get a one-time `ALTER TABLE ... RENAME TO`, data preserved.)*

| Trigger | Operation |
|---|---|
| Startup / any `init_db()` run | INSERT OR IGNORE for every feature in the current seed list (doesn't overwrite an existing row's `min_tier`/`limit_count` — those are admin-configurable state) |
| Startup / any `init_db()` run | UPDATE `feature_label`/`sort_order`/`description` for every seeded feature (these ARE always refreshed to match the current code, unlike tier/limit) |
| Startup / any `init_db()` run | **DELETE** any row whose `feature_key` is no longer in the current seed list (removed/renamed features get pruned) |
| `/updatefeature -minlevel ...` and/or `-limit ...` | UPDATE `min_tier` and/or `limit_count` for one specific `feature_key` |
| One-time legacy migration (old per-tier limit columns) | UPDATE `limit_count` from whichever of the 3 old columns matched the row's current tier, then those 3 old columns are dropped |

### `events`
**Columns:** `event_id` (PK), `chat_id` (NOT NULL), `message_id`, `name`, `going_icon`, `notgoing_icon`, `event_status` (-1 canceled / 0 open / 1 verification / 2 closed), `going_data` (JSON, **frozen/vestigial** — see below), `notgoing_data` (JSON, **frozen/vestigial**), `counters_data` (JSON, **frozen/vestigial**), `event_date`, `kicked_data` (JSON, **frozen/vestigial**), `feature_snapshot` (JSON, frozen at creation), `total_limit`, `waitlist_data` (JSON — still live, see below), `waitlist_joined`, `waitlist_visibility`, `notgoing_visibility`, `clickability`, `created_by_user_id`, `created_date`, `closed_date`

`created_date`/`closed_date` are real, persisted columns (not just written to the bound Google Sheet) — set via `now2ddmmyy()` at `/newevent` creation time and at Save&Close/Cancel/Directclose respectively. Both follow a "DB first, then Sheets reuses that exact value" pattern: the timestamp is computed once, written to the `events` row (via a plain `INSERT`/`UPDATE`), and the SAME in-memory value is reused for the Google Sheets Events tab export (if the hub is PRO with a bound sheet) — never recomputed a second time for the Sheet, which could otherwise drift from what was actually persisted. `closed_date` uses `COALESCE(?, closed_date)` in its own `UPDATE` so actions that don't close the event (kick, guest adjustments, `back`, pagination) never overwrite an already-set value with `NULL`.

`reopened_roster` (TEXT, JSON `{user_id: name}`, normally `NULL`) is set by `/reopen` to the final roster the event had when it was closed and read-and-cleared by the next Save & Close, which logs the difference to the `Actions` tab (`ROSTER_ADDED` / `ROSTER_REMOVED`).

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
**Columns:** `chat_id`, `username`, `user_id` (NOT NULL), `status` (active/passive), `first_name`, `last_name` — composite PK `(chat_id, user_id)`

The tracked-roster table used by `/listusers`, `/notify`, `/refreshusers`, and every mention-resolution lookup.

**Real bug fixed:** this table used to be keyed by `(chat_id, username)`, with `user_id` optional. But a Telegram `@username` is **not** a permanent identity — it can be changed, and once abandoned, can be picked up by a completely different person. This caused a real incident: a stale row for a reused `@username` still pointed at its *former* holder's real (but no-longer-relevant) `user_id`. `/refreshusers` correctly queried that old id, Telegram correctly reported it as `left`, and the row was deleted — silently removing the *current*, still-present holder of that username from tracking. Keying by `user_id` (the only permanent Telegram identity) makes this collision structurally impossible: each real person always gets their own row, no matter how many usernames they've cycled through, and no matter who held their current username before them. `user_id` is required (`NOT NULL`) — a call to `track_user()` with no resolvable numeric id is now a silent no-op, rather than the old behavior of creating a permanently-unresolvable stub row that lingered forever.

`track_user()` also refuses to ever store `GROUP_ANONYMOUS_BOT_ID` (`1087968824`, Telegram's shared pseudo-account for "Remain Anonymous" senders) as anyone's personal tracking id — a plain text message from an anonymous sender never reveals their real identity, only a genuine button click does, so accepting this pseudo-id here would create a bogus row that doesn't correspond to any real, individually-trackable person.

A handful of call sites still do a **fallback lookup by username** specifically when no `user_id` is already known (e.g. `/updateuser` checking if a name has already been resolved, `_mention_link`'s own fallback for old-format Not Going entries) — this is an inherent, unavoidable property of resolving an identity from a name alone, not a bug: if two different real people have both, at different times, held the same now-current username, and neither has been freshly tracked yet, the lookup could theoretically return either. The core incident above (incorrect *removal* of a still-present person) is fixed regardless, since removal is now keyed by `user_id`, never by username.

| Trigger | Operation |
|---|---|
| Anyone clicking Going/Not Going/any event button, joining/being added to a chat, `/adduser`, `/updateuser`, `@everyone` mentions | INSERT ... ON CONFLICT(chat_id, user_id) DO UPDATE (upsert) via `track_user()` — updates `username`/`status` always (so a rename is reflected in place, not left stale); `first_name`/`last_name` only if a non-null value is actually being passed (never overwrites a good value with a blank one). No-op entirely if `user_id` is missing or is `GROUP_ANONYMOUS_BOT_ID` |
| `/refreshusers` finding someone no longer in the chat | **DELETE** by `(chat_id, user_id)` — never by username, to avoid ever deleting a *different*, currently-present person who happens to hold that same username string right now |
| `/refreshusersall`, same logic, per monitored chat | **DELETE** by `(chat_id, user_id)`, same reasoning. Also fixed a real gap here: unlike `/refreshusers`, this loop previously had no protection against transient API errors — any exception was treated as confirmed departure and removed; now matches the safer "keep on error" behavior |

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
**Columns:** CHAT_ID, CHAT_NAME, TYPE, SHEET_ID, SHEET_NAME, SUBS_DATE_START, SUBS_DATE_END, VISIBILITY, DATE_BOT_ADD, ROLE

Full overwrite-and-trim mirror of the entire `all_groups` SQLite table (not incremental row edits).

| Trigger | Operation |
|---|---|
| Bot startup | Full re-push |
| Bot added to or removed from any group | Full re-push |
| `/setsub` (on or off) | Full re-push |

### Control Sheet — `CHANNELS` tab
**Columns:** CHAT_ID, CHAT_NAME, VISIBILITY, DATE_BOT_ADD, ROLE

Same full-mirror pattern as `GROUPS`, sourced from `all_channels`.

| Trigger | Operation |
|---|---|
| Bot startup | Full re-push |
| Bot added to or removed from any channel | Full re-push |

### Control Sheet — `BOTCONFIG` tab
**Columns:** FEATURE_KEY, FEATURE, FREE, PRO, OWNER, DESCRIPTION

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
**Columns:** EVENT_ID, EVENT_NAME, CREATED_AT, CREATED_BY, EVENT_DATE, CLOSED_AT, STATUS, AMOUNT (A..H - the bot writes a row with `append_row` and F:H as a block, and the scripts read it by POSITION; the template's header names vary: AMOUNT / GOING_COUNT, CREATED_AT / CREATED_DATE)

**AMOUNT** is the event's headcount at Save & Close - `db.headcount_from_rows`: every person who is `going` PLUS every guest, from every chat of the event; 0 while the event is open or canceled. It is a snapshot written to the Sheet only: the database keeps no copy, `/stats` and the audit scripts recompute it from `event_users`. The post's "TOTAL Going" is the same number. Guests are added independently of the host's own status (Not Going and "only ever added guests" rows keep them), so guests of people who are not going are in it. The capacity check behind `-limit` (`event_engine._current_headcount`) is narrower: going people and **their** guests only - the two agree unless a not-going / kicked host has guests.


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
| Add Extra Member | `append_row`, `ACTION='ADD_EXTRA_PLAYER'`, `USER_NAME`/`USER_ID` = the **admin or creator who clicked**, not the person being added |
| `/reopen` (button or command) | `append_row`, `ACTION='REOPEN'`, `USER_NAME`/`USER_ID` = the owner who reopened it |
| Save & Close of an event that was **reopened** | one `append_rows` batch: `ACTION='ROSTER_ADDED'` / `'ROSTER_REMOVED'` for each person added to / removed from the final roster since it was last closed - `USER_NAME`/`USER_ID` = **the person**, not a clicker |

### Per-hub sheet — `EventUsers` tab
**Columns:** EVENT_ID, USER_ID

The final attendee roster — **replaced** at every Save & Close, so the tab always holds exactly the event's final roster, never a doubled or stale one.

| Trigger | Operation |
|---|---|
| Save & Close, successfully | the event's existing rows here are removed first (one `batchUpdate`, only if there are any), then `append_rows` — every `going` `user_id`, the master hub **and** every other chat that holds participants of the event (shared or just monitored) combined, **one row per person** (kicked, not-going, waitlisted people and guests are not listed). People typed in with Add Extra Member and never resolved are written by their username |
| `/reopen` | the event's rows are removed at once (it is open again and has no final roster yet); a later Save & Close writes the new one |
| Cancel | **Nothing is written here at all** — intentionally, since a canceled event has no final attendee list |

The write is part of the Save & Close pipeline, not a background task: if it fails the chat gets "Event saved, but Google Sheets sync failed" (it used to be only a log line, leaving the tab empty without a word).

### Per-hub sheet — `UserPresenceLog` tab
**Columns:** USER_ID, CHAT_ID, DATE_start, DATE_end

| Trigger | Operation |
|---|---|
| A tracked person found no longer present during `/refreshusers`/`/refreshusersall` (the `Users` tab MEMBER→LEFT transition) | `append_row` — but only if no existing row already has the exact same `(USER_ID, CHAT_ID, DATE_start)` combination, to avoid duplicate log entries |

Never updated or deleted — a pure log.
Every stay that ends gets a row, however the `Users` row turned `LEFT` (`sheets.sync_users_sheet`): the log is written FIRST - one read + one append - and only then are statuses flipped, so a failed write leaves the person `MEMBER` and the next `/refreshusers` / `/refreshusersall` retries (rows are de-duplicated by user + chat + `DATE_start`, falling back to `DATE_end` when there is no start). It is written to the sheet of the HUB, which is what makes monitored chats log too. Rows that are already `LEFT` but missing from the log (an earlier failed write, or the previous stay of someone who just rejoined) are backfilled before a rejoin overwrites their dates. If the log can't be written the refresh says so (`⚠️ ... incomplete`) instead of reporting success.
People a refresh confirms as gone (`left`/`kicked` from `getChatMember`) who have **no** `Users` row yet - they joined and left between two refreshes, or never interacted with the bot - get one too: a `LEFT` row and a log row, with `DATE_start` one hour before `DATE_end` because their join time is unknown. `/refreshusers` deletes them from `main_group_users` immediately, so until the sheet has them they wait in the `left_pending` table (the first detection time is kept, so a retry writes identical dates and can't double a log row); the entry is dropped once the sync succeeds, or when the hub has no sheet at all.
