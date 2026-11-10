# Minimal full-coverage tour

> **102 actions** (58 commands, 31 button clicks, 10 manual operations, 3 sheet checks) - about **60 min** for someone who knows the bot and has all four accounts open, about 100 min the first time.

One continuous scenario that touches **every command (all 34), every kind of event button, every role, the FREE / PRO / OWNER difference, Google Sheets, `/lockbot`, a restart and the orphan self-healing** - the smallest run that makes a broken area visible. It is breadth first and follows the happy path: it tells you *something is broken in area X*, not that every edge is right. For edges use the 46-case plan in `docs/manual_testing.md` (section "What it does not cover" says which cases to add).

## Before you start (not counted in the time)

| Role | Account |
|---|---|
| **O** Owner | your account, id in `OWNER_USER_IDS` (also has server access for the restart) |
| **A** Admin | admin of Hub, Child and the FREE hub, **not** an owner |
| **M1**, **M2** | plain members of Hub and Child |

| Chat | State |
|---|---|
| **Hub** | supergroup, bot is admin, **FREE at the start** (step 3 makes it PRO) |
| **Child** | group, bot is admin |
| **Channel** | channel, bot is admin |
| **FREE hub** | another supergroup, bot is admin, never subscribed |
| **Sheets** | a copy of the template shared with the service account as Editor (its URL is used in step 6), and a scratch tab in the Control Sheet (step 10) |

Setup takes about 30-45 minutes the first time. Have the Hub's and Child's ids and the Channel's id ready. Tomorrow's date goes into step "`/newevent`" as `dd.mm.yyyy`.

**How to read the table:** do the steps in order, one per line; compare what you see with *Expected*; tick the last column or write the step number in your notes when something differs. Later steps rely on earlier ones, so on a failure note it, repair if you can, and carry on.


### Phase 1 - Owner and setup (DM) (12 actions)

| # | Who | Where | Action | Expected | ✔ |
|---|---|---|---|---|---|
| 1 | O | DM | `/version` | The deployed version; "Started … (up …)" is recent | |
| 2 | A | DM | `/version` | No reply at all (owner-only commands stay silent) | |
| 3 | O | DM | `/setsub <hub_id> on 30` | PRO activated for the Hub, due date shown | |
| 4 | A | DM | `/start` | Lists the groups A administers | |
| 5 | A | DM | pick **Hub** in that list | Hub is selected for A's DM | |
| 6 | A | DM | `/setsheet <sheet URL>` | Sheet bound to the Hub | |
| 7 | A | DM | `/status` | Names the Hub: PRO, due date, bound sheet | |
| 8 | A | DM | `/chatid` | The **Hub's** id and name - not the DM's own id (the Hub is the group selected in step 5) | |
| 9 | O | DM | `/allgroups` | Hub, Child and the FREE hub are listed | |
| 10 | O | DM | `/allchannels` | Channel is listed | |
| 11 | O | DM | `/showtable all_features <scratch tab>` | The scratch tab of the Control Sheet is filled with the table | |
| 12 | O | DM | `/updatefeature deleteevent -minlevel pro` | Level changed (it is used in phase 5) | |

### Phase 2 - Users and monitoring (12 actions)

| # | Who | Where | Action | Expected | ✔ |
|---|---|---|---|---|---|
| 13 | A | Hub | `/addmonitor <child_id>` | Child added to the monitor list | |
| 14 | A | Hub | `/setalias <child_id> kids` | Alias saved | |
| 15 | A | Hub | `/listaliases` | `kids` is listed | |
| 16 | A | Hub | `/listmonitors` | Child is listed | |
| 17 | A | Hub | `/refreshusersall` | "Synced" line for Hub and Child; the Hub sheet's `Users` tab lists the members as `MEMBER` | |
| 18 | A | Hub | `/adduser <M2 user_id>` | M2 added to the tracked list | |
| 19 | A | Hub | `/updateuser @M2 -p` | M2 is now passive | |
| 20 | A | Hub | `/listusers` | "Tracked Users (N)", active before passive | |
| 21 | A | Hub | `/listusers -p` | "Passive Users (1)" - M2 only | |
| 22 | A | Hub | `/refreshusers` | "Nothing to change"; Users tab synced | |
| 23 | A | Hub | `/userid` | Your Telegram id | |
| 24 | A | Hub | `/chatid` | The Hub's id | |

### Phase 3 - Event lifecycle (42 actions)

| # | Who | Where | Action | Expected | ✔ |
|---|---|---|---|---|---|
| 25 | M1 | Hub | `/newevent Футбол Tour -limit 2 -wl visible -d <tomorrow> 19:00` | Event posted with limit 2 and the date; Events tab gets an `OPEN` row; Going is shown, Not Going and Waitlist start folded, and a list gets its ▾ / ▸ fold button under the post once it has people in it | |
| 26 | A | Hub | `/shareevent kids -swl visible` | Child gets its own post (the alias works) | |
| 27 | A | Hub | `/shareevent <channel_id> -mgl onlycount -clc off` | Channel gets a post; the overrides apply to it only | |
| 28 | A | Hub | `/notify` | Pings tracked people who haven't voted yet | |
| 29 | O | DM | `/lockbot on` | Lock is on | |
| 30 | M2 | Child post | **Going** | Works while locked: 1/2 | |
| 31 | M1 | Hub | `/waitlist` | Explicit lock message (not silence) | |
| 32 | O | Server | `docker compose restart bot`, wait ~10 s | Bot is back up | |
| 33 | M1 | Hub post | **Going** | Works after the restart, still locked; shows 2/2 - M2's vote survived | |
| 34 | M2 | Child | `/waitlist` | Still the lock message - the lock survived the restart | |
| 35 | O | DM | `/lockbot off` | Everything works again | |
| 36 | A | Child post | **Going** | Alert: event is full, added to the Waitlist | |
| 37 | M1 | Hub | `/waitlist` | A is listed, tagged with the chat it came from (Child) | |
| 38 | M2 | Child | `/waitlist` | Only the Child's own entries (A) | |
| 39 | M2 | Child post | **Not Going** | A is promoted automatically, with a notification message | |
| 40 | A | Hub | `/who 3 -ng` | M2 is listed as changed to Not Going, with the time and the Child's alias `kids`; nobody else is (A moved the other way) | |
| 41 | M1 | Hub post | **▸ Not Going (n)** | The Not Going names appear and the button turns ▾; the Child's and the Channel's posts are unchanged (each post folds on its own) | |
| 42 | M2 | Child post | **▾ Going (n)** | The going names fold to their header lines in the Child's post only (the counts and the TOTAL line stay); press it again to unfold | |
| 43 | A | Hub | `/editevent -limit 4 -wl onlycount` | Every post refreshes; only the given fields changed; the Waitlist is now a count only, so its fold button is gone | |
| 44 | M1 | Hub post | **ADD** | 1 guest | |
| 45 | M1 | Hub post | **ADD** | 2 guests | |
| 46 | M1 | Hub post | **DROP** | 1 guest (not 0, not 2) | |
| 47 | M1 | Hub post | **ALL** | 0 guests | |
| 48 | M2 | Hub post | **Verify** | Nothing happens (not an admin, not the creator) | |
| 49 | M1 | Hub post | **Verify** | Verification mode opens (the creator may) | |
| 50 | M1 | Hub post | **Kick** on A | A removed; counts drop in every post | |
| 51 | M1 | Hub post | **Return** on A | A is back | |
| 52 | M1 | Hub post | **Add Extra Member**, then type `@username` | Added - under its own chat's "Going from" block if the person belongs to a monitored chat; plain text plus a notice if the bot never saw them | |
| 53 | M1 | Hub post | **Save & Close Event** | Closed, buttons gone | |
| 54 | — | Hub's Google Sheet | look at `Events` and `EventUsers` | Row `CLOSED` with the right `AMOUNT`; `EventUsers` rows for the going people | |
| 55 | M1 | Hub | `/newevent Second` | Posted with **no** "already an active event" warning | |
| 56 | M1 | Hub post | **Cancel** | Canceled; Events row `CANCELED`, no `EventUsers` rows | |
| 57 | O | Hub | `/reopen on` | "Reopen mode is ON", saying it shows under the 2 newest closed or canceled events; a 🔓 Reopen button appears under **both** `Second` (canceled) and the older closed `Футбол Tour` | |
| 58 | A | Hub post | **🔓 Reopen** (A is not an owner) | An "Owners only" popup; nothing changes | |
| 59 | O | Old `Футбол Tour` post | **🔓 Reopen** | The OLDER event is open again: vote buttons are back in every post, its `Events` row says `OPEN`, its `EventUsers` rows are gone, `Actions` has a `REOPEN` row - and the Reopen button is gone from `Second` too (only one event can be open) | |
| 60 | M2 | Old `Футбол Tour` post | **Going** | M2 is going again (the limit is 4): counts rise in every post - this person is one of the changes step 57 logs | |
| 61 | O | Hub | `/reopen second` | Refused, naming `Футбол Tour` as the event that is open: only one event can be active at a time | |
| 62 | M1 | Old `Футбол Tour` post | **Verify**, **Kick** one person, then **Save & Close Event** | Closed again: `EventUsers` holds exactly the new roster (the kicked person gone, M2 there, nobody twice), the `Events` row is `CLOSED` with the new `AMOUNT`, `Actions` has a `ROSTER_ADDED` row for M2 and a `ROSTER_REMOVED` row for the kicked person (the net change since the first close); the Reopen buttons are back under both posts | |
| 63 | O | Hub | `/reopen off` | "Reopen mode is OFF"; the buttons are gone from both posts | |
| 64 | A | Hub | `/newevent Orphan` | Posted | |
| 65 | A | Hub | delete the Orphan post by hand | The post is gone | |
| 66 | A | Hub | `/newevent Third` | Created with **no** warning; Orphan is silently `CANCELED` | |

### Phase 4 - Statistics (8 actions)

| # | Who | Where | Action | Expected | ✔ |
|---|---|---|---|---|---|
| 67 | A | Hub | `/stats` | Events / closed (% completed) / cancelled / open now / guests % / not going; "Events with a limit …"; "Waitlist: 1 joined, 0 still waiting at close" | |
| 68 | A | Hub | click **Users** | Top-3 lists | |
| 69 | A | Hub | click **Distribution** | Per-chat breakdown (Hub, Child, Channel) | |
| 70 | A | Hub | click **Back** | Back on the Stats screen | |
| 71 | O | Telegram | remove the bot's admin rights in **Child** | - | |
| 72 | O | DM | `/stats -o` | Sections: PRO subscriptions, Growth, Activity, top commands and groups; groups with admin rights = total − 1 | |
| 73 | A | Hub | `/stats -o` | No reply at all | |
| 74 | O | Telegram | make the bot admin in **Child** again | - | |

### Phase 5 - /deleteevent (6 actions)

| # | Who | Where | Action | Expected | ✔ |
|---|---|---|---|---|---|
| 75 | A | Hub | `/deleteevent футбол` | One match → "Delete this event?" screen (case-insensitive, Cyrillic works) | |
| 76 | A | Hub | click **Cancel** | "Cancelled - nothing was deleted" | |
| 77 | A | Hub | `/deleteevent футбол` | The confirmation again | |
| 78 | A | Hub | click **Yes, delete forever** | Result lists DB, Sheet rows and "3 of 3 removed" posts (Hub, Child, Channel) | |
| 79 | O | DM | `/updatefeature deleteevent -minlevel owner` | Level back to owner-only | |
| 80 | A | Hub | `/deleteevent` | No reply at all | |

### Phase 6 - Presence and cleanup (7 actions)

| # | Who | Where | Action | Expected | ✔ |
|---|---|---|---|---|---|
| 81 | M1 | Child | leave the group | - | |
| 82 | M2 | Telegram | change your own first name (add a letter; change it back afterwards) | - | |
| 83 | A | Hub | `/refreshusersall` | "Synced: <Child> (-1)"; in the Hub sheet M1 / Child is `LEFT` and one `UserPresenceLog` row appears; M2's row for the Hub has the new `FIRST_NAME` and the earlier "First Last" in `ARCHIVED_FULL_NAME` (column J - its header appears by itself) | |
| 84 | A | Hub | `/who 3 -l` | M1 is listed under `kids` with the time the refresh noticed | |
| 85 | A | Hub | `/who 3 -l -c main` | "No one left in the last 3 h" - M1 left the Child, not the main group | |
| 86 | A | Hub | `/removemonitor <child_id>` | Removed | |
| 87 | A | Hub | `/removealias kids` | Removed | |

### Phase 7 - Help (5 actions)

| # | Who | Where | Action | Expected | ✔ |
|---|---|---|---|---|---|
| 88 | M1 | Hub | `/help` | Short main screen; no owner commands (no `/deleteevent`, no `/version`) | |
| 89 | M1 | Hub | click **More about Flags** | Flags expand | |
| 90 | M1 | Hub | click **Utility** | Lists `/start`, `/status`, `/stats`, `/setsheet` (the Hub is PRO) | |
| 91 | M1 | Hub | click **Event Lifecycle** | Buttons are marked "admin or event creator" | |
| 92 | O | DM | `/help -o` | Owner screen: DM-only commands (incl. `/version`) apart from "also work inside a group" (`/stats -o`, `/deleteevent`) | |

### Phase 8 - DM group switch and a FREE hub (4 actions)

| # | Who | Where | Action | Expected | ✔ |
|---|---|---|---|---|---|
| 93 | A | DM | `/switchgroup` | Lists the groups A administers | |
| 94 | A | DM | pick the **FREE hub** | Selected | |
| 95 | A | DM | `/status` | "Running commands via DM is a PRO feature …" - FREE ≠ PRO | |
| 96 | A | DM | `/chatid` | The **FREE hub's** id and name - it works although DM commands are PRO: it needs no subscription | |

### Phase 9 - Final audits (2 actions)

| # | Who | Where | Action | Expected | ✔ |
|---|---|---|---|---|---|
| 97 | — | Hub's Google Sheet | look at `Events`, `EventUsers`, `Actions`, `Users`, `UserPresenceLog` | `Second` and `Orphan` are `CANCELED`, `Third` is `OPEN`; the deleted event has no rows anywhere; M1 / Child is `LEFT` with exactly one log row; M2's Hub row has the earlier full name in `ARCHIVED_FULL_NAME` | |
| 98 | — | Control Sheet | look at `GROUPS` and `BOTCONFIG` | Child's `ROLE` is `ADMIN` again; BOTCONFIG's last tier column is `OWNER`; `version` and `deleteevent` are owner-only | |


### Phase 10 - Operator scripts (4 actions)

Run in the container, one hub at a time. None of them is run with `--apply` here: this phase only checks that the scripts and the data agree. Use the Hub's id; the line starts `docker compose exec bot` if that is how you run commands in the container.

| # | Who | Where | Action | Expected | ✔ |
|---|---|---|---|---|---|
| 99 | O | Server | `docker compose exec bot python3 scripts/audit_event_users.py --chat-id <hub_id> --all-time` | "No problems found." - the deleted event left no `EventUsers` or `Events` rows behind (no `SHEET_ORPHAN_EVENT` note) | |
| 100 | O | Server | `docker compose exec bot python3 scripts/check_events_amount.py --chat-id <hub_id>` | "All amounts are right." | |
| 101 | O | Server | `docker compose exec bot python3 scripts/fix_eventusers_ids.py --chat-id <hub_id>` | "Nothing to fix." - nothing in `EventUsers` holds a username instead of an id | |
| 102 | O | Server | `docker compose exec bot python3 scripts/repair_event_users.py --chat-id <hub_id>` | "Nothing to repair." (a dry run) | |


## What the tour proves

Every command ran once; every event button was used (Going, Not Going, ADD, DROP, ALL, Verify, Kick, Return, Add Extra Member, Save & Close, Cancel, 🔓 Reopen); owner, admin, creator and plain member were each exercised; FREE, PRO and OWNER-level behaviour was seen; the Hub's sheet tabs and the Control Sheet were read back; the lock and a waitlist survived a restart; an event whose post was deleted healed itself; `/deleteevent` removed an event from the DB, the sheet and all three chats. Also: `/chatid` in a DM names the group the DM works with (for a FREE group too); an OLDER event was reopened, edited and closed again - its `EventUsers` rewritten with exactly the new roster, its `Events` row corrected and the net change logged in `Actions` - while the one-active-event rule held; a renamed member's earlier full name was kept in `ARCHIVED_FULL_NAME`; the ▾ / ▸ fold buttons folded and unfolded a list in one post without touching the others; and the four operator scripts agreed with the DB and the sheet.

## What it does not cover - add these cases from `docs/manual_testing.md` when the change touches them

| Case | Why it is not in the tour |
|---|---|
| B2, B3, B4 | capacity boundaries, FIFO on a limit raise, refusing a lowered limit |
| A6, A7 | `/editevent` edge cases, the *presence* of the active-event warning |
| A8 | folding edge cases: `hidden` / `onlycount` lists, verification and closed posts, reopen, cleanup on `/deleteevent` |
| C2, C4 | share-check order, clickability consistency |
| D1 | anonymous admin (needs that account) |
| E2, E4 | expired PRO, frozen feature snapshot |
| F2, F3, F4 | no-sheet behaviour, **Sheets outage and recovery** (needs a staging copy) |
| J3 | migration rehearsal on a copy of the production DB - run it **before** deploying, not after |
| H4 (the `/reopen <name or id>` success path) | reopening an event by name would leave `Second` open and break the later steps; the tour only checks that it is refused beside an open event |
| exploratory charters | concurrency, hostile text, stale buttons, client differences |

Also untouched: the `--apply` mode of `repair_event_users.py`, `fix_eventusers_ids.py` and `check_events_amount.py` (they need a deliberately damaged sheet: delete one `EventUsers` row, run `repair_event_users.py --apply`, then the audit - about 3 minutes), the audit's `--telegram` mode, the **Back** button of verification mode, `-gi/-ni`, `-ngl`, `-sngl`, `-wl hidden`, `-clc on`, `/allgroups -pro` and list paging.

## Time estimate

The figure is computed from the steps: a command takes ~20 s including reading the answer (~60 s for the slow ones that talk to Sheets: `/setsheet`, `/showtable`, `/refreshusers`, `/refreshusersall`), a button click ~12 s, a manual Telegram operation ~45 s, a script run in the container ~60 s, the restart ~100 s, reading a sheet ~160 s, plus ~6 s each time you switch account, plus 20 % for notes and screenshots. The first-run figure multiplies that by 1.6. If one person has to log in and out of the four accounts instead of keeping them open side by side, add about 15 minutes.

## Result

Date: ______ Version (`/version`): ______ Tester: ______ Steps that differed: ______
