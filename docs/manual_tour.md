# Minimal full-coverage tour

> **91 actions** (55 commands, 27 button clicks, 6 manual operations, 3 sheet checks) - about **55 min** for someone who knows the bot and has all four accounts open, about 85 min the first time.

One continuous scenario that touches **every command (all 32), every kind of event button, every role, the FREE / PRO / OWNER difference, Google Sheets, `/lockbot`, a restart and the orphan self-healing** - the smallest run that makes a broken area visible. It is breadth first and follows the happy path: it tells you *something is broken in area X*, not that every edge is right. For edges use the 46-case plan in `docs/manual_testing.md` (section "What it does not cover" says which cases to add).

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


### Phase 1 - Owner and setup (DM) (11 actions)

| # | Who | Where | Action | Expected | ✔ |
|---|---|---|---|---|---|
| 1 | O | DM | `/version` | The deployed version; "Started … (up …)" is recent | |
| 2 | A | DM | `/version` | No reply at all (owner-only commands stay silent) | |
| 3 | O | DM | `/setsub <hub_id> on 30` | PRO activated for the Hub, due date shown | |
| 4 | A | DM | `/start` | Lists the groups A administers | |
| 5 | A | DM | pick **Hub** in that list | Hub is selected for A's DM | |
| 6 | A | DM | `/setsheet <sheet URL>` | Sheet bound to the Hub | |
| 7 | A | DM | `/status` | Names the Hub: PRO, due date, bound sheet | |
| 8 | O | DM | `/allgroups` | Hub, Child and the FREE hub are listed | |
| 9 | O | DM | `/allchannels` | Channel is listed | |
| 10 | O | DM | `/showtable all_features <scratch tab>` | The scratch tab of the Control Sheet is filled with the table | |
| 11 | O | DM | `/updatefeature deleteevent -minlevel pro` | Level changed (it is used in phase 5) | |

### Phase 2 - Users and monitoring (12 actions)

| # | Who | Where | Action | Expected | ✔ |
|---|---|---|---|---|---|
| 12 | A | Hub | `/addmonitor <child_id>` | Child added to the monitor list | |
| 13 | A | Hub | `/setalias <child_id> kids` | Alias saved | |
| 14 | A | Hub | `/listaliases` | `kids` is listed | |
| 15 | A | Hub | `/listmonitors` | Child is listed | |
| 16 | A | Hub | `/refreshusersall` | "Synced" line for Hub and Child; the Hub sheet's `Users` tab lists the members as `MEMBER` | |
| 17 | A | Hub | `/adduser <M2 user_id>` | M2 added to the tracked list | |
| 18 | A | Hub | `/updateuser @M2 -p` | M2 is now passive | |
| 19 | A | Hub | `/listusers` | "Tracked Users (N)", active before passive | |
| 20 | A | Hub | `/listusers -p` | "Passive Users (1)" - M2 only | |
| 21 | A | Hub | `/refreshusers` | "Nothing to change"; Users tab synced | |
| 22 | A | Hub | `/userid` | Your Telegram id | |
| 23 | A | Hub | `/chatid` | The Hub's id | |

### Phase 3 - Event lifecycle (38 actions)

| # | Who | Where | Action | Expected | ✔ |
|---|---|---|---|---|---|
| 24 | M1 | Hub | `/newevent Футбол Tour -limit 2 -wl visible -d <tomorrow> 19:00` | Event posted with limit 2 and the date; Events tab gets an `OPEN` row | |
| 25 | A | Hub | `/shareevent kids -swl visible` | Child gets its own post (the alias works) | |
| 26 | A | Hub | `/shareevent <channel_id> -mgl onlycount -clc off` | Channel gets a post; the overrides apply to it only | |
| 27 | A | Hub | `/notify` | Pings tracked people who haven't voted yet | |
| 28 | O | DM | `/lockbot on` | Lock is on | |
| 29 | M2 | Child post | **Going** | Works while locked: 1/2 | |
| 30 | M1 | Hub | `/waitlist` | Explicit lock message (not silence) | |
| 31 | O | Server | `docker compose restart bot`, wait ~10 s | Bot is back up | |
| 32 | M1 | Hub post | **Going** | Works after the restart, still locked; shows 2/2 - M2's vote survived | |
| 33 | M2 | Child | `/waitlist` | Still the lock message - the lock survived the restart | |
| 34 | O | DM | `/lockbot off` | Everything works again | |
| 35 | A | Child post | **Going** | Alert: event is full, added to the Waitlist | |
| 36 | M1 | Hub | `/waitlist` | A is listed, tagged with the chat it came from (Child) | |
| 37 | M2 | Child | `/waitlist` | Only the Child's own entries (A) | |
| 38 | M2 | Child post | **Not Going** | A is promoted automatically, with a notification message | |
| 39 | A | Hub | `/who 3 -ng` | M2 is listed as changed to Not Going, with the time and the Child's alias `kids`; nobody else is (A moved the other way) | |
| 40 | A | Hub | `/editevent -limit 4 -wl onlycount` | Every post refreshes; only the given fields changed | |
| 41 | M1 | Hub post | **ADD** | 1 guest | |
| 42 | M1 | Hub post | **ADD** | 2 guests | |
| 43 | M1 | Hub post | **DROP** | 1 guest (not 0, not 2) | |
| 44 | M1 | Hub post | **ALL** | 0 guests | |
| 45 | M2 | Hub post | **Verify** | Nothing happens (not an admin, not the creator) | |
| 46 | M1 | Hub post | **Verify** | Verification mode opens (the creator may) | |
| 47 | M1 | Hub post | **Kick** on A | A removed; counts drop in every post | |
| 48 | M1 | Hub post | **Return** on A | A is back | |
| 49 | M1 | Hub post | **Add Extra Member**, then type `@username` | Added - under its own chat's "Going from" block if the person belongs to a monitored chat; plain text plus a notice if the bot never saw them | |
| 50 | M1 | Hub post | **Save & Close Event** | Closed, buttons gone | |
| 51 | — | Hub's Google Sheet | look at `Events` and `EventUsers` | Row `CLOSED` with the right `GOING_COUNT`; `EventUsers` rows for the going people | |
| 52 | M1 | Hub | `/newevent Second` | Posted with **no** "already an active event" warning | |
| 53 | M1 | Hub post | **Cancel** | Canceled; Events row `CANCELED`, no `EventUsers` rows | |
| 54 | O | Hub | `/reopen on` | "Reopen mode is ON", saying it shows under the 2 newest closed or canceled events; a 🔓 Reopen button appears under **both** `Second` (canceled) and the older closed `Футбол Tour` | |
| 55 | A | Hub post | **🔓 Reopen** (A is not an owner) | An "Owners only" popup; nothing changes | |
| 56 | O | Old `Футбол Tour` post | **🔓 Reopen** | The OLDER event is open again: vote buttons are back in every post, its `Events` row says `OPEN`, its `EventUsers` rows are gone, `Actions` has a `REOPEN` row - and the Reopen button is gone from `Second` too (only one event can be open) | |
| 57 | M1 | Old `Футбол Tour` post | **Verify**, **Kick** one person, then **Save & Close Event** | Closed again: `EventUsers` holds exactly the new roster (the kicked person gone, nobody twice), the `Events` row is `CLOSED` with the new count, `Actions` has a `ROSTER_REMOVED` row for the kicked person; the Reopen buttons are back under both posts | |
| 58 | O | Hub | `/reopen off` | "Reopen mode is OFF"; the buttons are gone from both posts | |
| 59 | A | Hub | `/newevent Orphan` | Posted | |
| 60 | A | Hub | delete the Orphan post by hand | The post is gone | |
| 61 | A | Hub | `/newevent Third` | Created with **no** warning; Orphan is silently `CANCELED` | |

### Phase 4 - Statistics (8 actions)

| # | Who | Where | Action | Expected | ✔ |
|---|---|---|---|---|---|
| 62 | A | Hub | `/stats` | Events / closed (% completed) / cancelled / open now / guests % / not going; "Events with a limit …"; "Waitlist: 1 joined, 0 still waiting at close" | |
| 63 | A | Hub | click **Users** | Top-3 lists | |
| 64 | A | Hub | click **Distribution** | Per-chat breakdown (Hub, Child, Channel) | |
| 65 | A | Hub | click **Back** | Back on the Stats screen | |
| 66 | O | Telegram | remove the bot's admin rights in **Child** | - | |
| 67 | O | DM | `/stats -o` | Sections: PRO subscriptions, Growth, Activity, top commands and groups; groups with admin rights = total − 1 | |
| 68 | A | Hub | `/stats -o` | No reply at all | |
| 69 | O | Telegram | make the bot admin in **Child** again | - | |

### Phase 5 - /deleteevent (6 actions)

| # | Who | Where | Action | Expected | ✔ |
|---|---|---|---|---|---|
| 70 | A | Hub | `/deleteevent футбол` | One match → "Delete this event?" screen (case-insensitive, Cyrillic works) | |
| 71 | A | Hub | click **Cancel** | "Cancelled - nothing was deleted" | |
| 72 | A | Hub | `/deleteevent футбол` | The confirmation again | |
| 73 | A | Hub | click **Yes, delete forever** | Result lists DB, Sheet rows and "3 of 3 removed" posts (Hub, Child, Channel) | |
| 74 | O | DM | `/updatefeature deleteevent -minlevel owner` | Level back to owner-only | |
| 75 | A | Hub | `/deleteevent` | No reply at all | |

### Phase 6 - Presence and cleanup (6 actions)

| # | Who | Where | Action | Expected | ✔ |
|---|---|---|---|---|---|
| 76 | M1 | Child | leave the group | - | |
| 77 | A | Hub | `/refreshusersall` | "Synced: <Child> (-1)"; in the Hub sheet M1 / Child is `LEFT` and one `UserPresenceLog` row appears | |
| 78 | A | Hub | `/who 3 -l` | M1 is listed under `kids` with the time the refresh noticed | |
| 79 | A | Hub | `/who 3 -l -c main` | "No one left in the last 3 h" - M1 left the Child, not the main group | |
| 80 | A | Hub | `/removemonitor <child_id>` | Removed | |
| 81 | A | Hub | `/removealias kids` | Removed | |

### Phase 7 - Help (5 actions)

| # | Who | Where | Action | Expected | ✔ |
|---|---|---|---|---|---|
| 82 | M1 | Hub | `/help` | Short main screen; no owner commands (no `/deleteevent`, no `/version`) | |
| 83 | M1 | Hub | click **More about Flags** | Flags expand | |
| 84 | M1 | Hub | click **Utility** | Lists `/start`, `/status`, `/stats`, `/setsheet` (the Hub is PRO) | |
| 85 | M1 | Hub | click **Event Lifecycle** | Buttons are marked "admin or event creator" | |
| 86 | O | DM | `/help -o` | Owner screen: DM-only commands (incl. `/version`) apart from "also work inside a group" (`/stats -o`, `/deleteevent`) | |

### Phase 8 - DM group switch and a FREE hub (3 actions)

| # | Who | Where | Action | Expected | ✔ |
|---|---|---|---|---|---|
| 87 | A | DM | `/switchgroup` | Lists the groups A administers | |
| 88 | A | DM | pick the **FREE hub** | Selected | |
| 89 | A | DM | `/status` | "Running commands via DM is a PRO feature …" - FREE ≠ PRO | |

### Phase 9 - Final audits (2 actions)

| # | Who | Where | Action | Expected | ✔ |
|---|---|---|---|---|---|
| 90 | — | Hub's Google Sheet | look at `Events`, `EventUsers`, `Actions`, `Users`, `UserPresenceLog` | `Second` and `Orphan` are `CANCELED`, `Third` is `OPEN`; the deleted event has no rows anywhere; M1 / Child is `LEFT` with exactly one log row | |
| 91 | — | Control Sheet | look at `GROUPS` and `BOTCONFIG` | Child's `ROLE` is `ADMIN` again; BOTCONFIG's last tier column is `OWNER`; `version` and `deleteevent` are owner-only | |


## What the tour proves

Every command ran once; every event button was used (Going, Not Going, ADD, DROP, ALL, Verify, Kick, Return, Add Extra Member, Save & Close, Cancel, 🔓 Reopen); owner, admin, creator and plain member were each exercised; FREE, PRO and OWNER-level behaviour was seen; the Hub's sheet tabs and the Control Sheet were read back; the lock and a waitlist survived a restart; an event whose post was deleted healed itself; `/deleteevent` removed an event from the DB, the sheet and all three chats.

## What it does not cover - add these cases from `docs/manual_testing.md` when the change touches them

| Case | Why it is not in the tour |
|---|---|
| B2, B3, B4 | capacity boundaries, FIFO on a limit raise, refusing a lowered limit |
| A6, A7 | `/editevent` edge cases, the *presence* of the active-event warning |
| C2, C4 | share-check order, clickability consistency |
| D1 | anonymous admin (needs that account) |
| E2, E4 | expired PRO, frozen feature snapshot |
| F2, F3, F4 | no-sheet behaviour, **Sheets outage and recovery** (needs a staging copy) |
| J3 | migration rehearsal on a copy of the production DB - run it **before** deploying, not after |
| exploratory charters | concurrency, hostile text, stale buttons, client differences |

Also untouched: the **Back** button of verification mode, `-gi/-ni`, `-ngl`, `-sngl`, `-wl hidden`, `-clc on`, `/allgroups -pro` and list paging.

## Time estimate

The figure is computed from the steps: a command takes ~20 s including reading the answer (~60 s for the slow ones that talk to Sheets), a button click ~12 s, a manual Telegram operation ~45 s, the restart ~100 s, reading a sheet ~160 s, plus ~6 s each time you switch account, plus 20 % for notes and screenshots. The first-run figure multiplies that by 1.6. If one person has to log in and out of the four accounts instead of keeping them open side by side, add about 15 minutes.

## Result

Date: ______ Version (`/version`): ______ Tester: ______ Steps that differed: ______
