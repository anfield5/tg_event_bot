# Manual Testing Guide

> Written for **4.7.x**. **46 cases in total - 12 of them are smoke** (marked 🔥).
> A full pass is ~3.5-4 hours for one tester; the smoke subset is ~20 minutes.

This is the **prioritised test plan**: which cases to run, in what order, and how. It is risk-based - cases exist where this bot has actually broken before, or where the automated suite structurally cannot look (real Telegram, real Google Sheets, several people at once, a real deploy).

## 0. How this relates to the other documents

| Document | What it is |
|---|---|
| **this file** | the plan: 46 cases, selection rules, exploratory charters |
| `docs/specs/*.md` | exact Given/When/Then per command/flag (`wl_waitlist.md` W1-W13, `clc_clickability.md` C1-C9, `newevent_editevent.md`, `shareevent.md` S1-S15, `ngl_notgoinglist.md`). Cases below point at them - run those scenarios, **tick the result there** |
| `docs/specs/MANUAL_TESTING.md` | per-topic checklists + the **Regression Watch List** (why each check exists) |
| `pytest tests/` (1000+ tests) | all logic. Do not re-test it by hand - see section 6 |

**Marking results:** tick the checkbox in place and add date + version, e.g. `- [x] **A1** ... ✅ 12.10 v4.7.0`. The result then lives next to the case it belongs to.

## 1. Setup (once)

| Role | Who | Used for |
|---|---|---|
| **O** Owner | your account, id in `OWNER_USER_IDS` | owner commands, `/setsub`, `/updatefeature` |
| **A** Admin | second account, admin of the hubs, **not** an owner | admin actions; proves owner commands stay silent |
| **M1** Member | third account, non-admin | voting; creating an event as a non-admin |
| **M2** Member | fourth account (or a friend) | second voter; "someone else's event" |
| **X** Anonymous admin *(optional)* | an admin with "Remain anonymous" | D1 |

| Chat | Setup |
|---|---|
| **Hub-PRO** | supergroup, bot is admin, PRO (`/setsub <chat_id> on 30` from O's DM), own Sheet bound with `/setsheet` and shared with the bot's service account as Editor. Tabs: `Users`, `Events`, `Actions`, `EventUsers`, `UserPresenceLog` |
| **Hub-FREE** | supergroup, bot is admin, never subscribed |
| **Child** | group where the bot is admin; the target of `/shareevent` and `/addmonitor` from Hub-PRO |
| **Channel** | channel where the bot is admin (share target) |
| **DM** | private chat with the bot |

Smoke needs only Hub-PRO, Child, O, A, M1, M2. Use a **staging** bot token, sheet and a **copy of the production DB** for the destructive cases (E2, F3, H2, J3, J4).

## 2. What to run when

| Situation | Run |
|---|---|
| Before **every** deploy | the 12 🔥 cases |
| A change in one area | smoke + the area's cases (table below) |
| Release candidate / after a schema change | all 46 + one exploratory charter |
| Quick full-coverage pass (about an hour) | the guided tour in `docs/manual_tour.md` - every command once, in one continuous scenario |

| Changed | Also run |
|---|---|
| event buttons / lifecycle (`event_engine.py`) | A*, B*, D2, D3, F1 |
| waitlist, `-limit` | B*, I1 |
| sharing, clickability | C*, B5 |
| permissions / roles | D*, E1 |
| tiers, features, `/updatefeature`, seeds | E*, K1 |
| `sheets.py`, credentials, Sheets tabs | F*, H2, G2 |
| users, monitoring, `/refreshusers*` | F5, F6, G1-G3 |
| `/stats`, `/stats -o` | I*, B2 |
| `help_system.py` | K1 |
| `db.py` migrations, startup, deploy | J3, J4, J1 |
| `main.py` chat-member handling | J2 |
| `hub_resolver.py` (DM) | G4 |
| `/deleteevent`, `/newevent` | H*, A7 |

## 3. The 46 cases

### A. Event lifecycle (7)

- [ ] **A1** 🔥 **Create and vote in hub and child.** *Given* Hub-PRO shared to Child. *When* M1 runs `/newevent Smoke Test` in the hub, A runs `/shareevent <child>`, M1 clicks **Going** in the hub and M2 clicks **Going** in the child. *Then* both posts update and show 2 going; the counts agree in both.
- [ ] **A2** 🔥 **Guest math.** *Given* an open event, M1 going. *When* M1 clicks **ADD** twice, **DROP** once, then **ALL**. *Then* after ADD, ADD, DROP the guest count is **1** (not 0 or 2); after ALL it is **0**, not partially reduced.
- [ ] **A3** 🔥 **Verify then Save & Close.** *Given* an open event with 3 going (hub + child). *When* A clicks **Verify**, then **Save & Close Event** on the **same** post. *Then* the event is genuinely closed (buttons gone); Events tab row is `CLOSED` with `GOING_COUNT` = hub + child headcount; `EventUsers` has one row per going person; a following `/newevent` shows **no** "already an active event" warning. (Details: `newevent_editevent.md` N13-N15.)
- [ ] **A4** 🔥 **Cancel from the open state.** *Given* a new event. *When* A clicks **Cancel** directly (no Verify). *Then* the event is canceled (buttons gone), the Events row is `CANCELED`, and **nothing** is written to `EventUsers`.
- [ ] **A5** **Verification-mode edits.** *Given* an event in Verify mode (repeat the Add Extra Member step once as an admin who has "Remain anonymous" on in the hub - the typed name must still be taken, and the `Actions` row must name the admin who pressed the button, not GroupAnonymousBot). *When* A **Kicks** a person, then clicks **Return**, then changes a guest count with **-/+**, then uses **Add Extra Member** with (a) a username of someone who has used the bot, (a2) the username of a member of a **monitored Child that the event is not shared to**, and (b) one who never has. *Then* kick/return/guest changes update every post; (a) is clickable; (a2) is listed under "Going from <Child>" in the hub post, counts in TOTAL and can be kicked / returned there; (b) shows as plain text after a notice that explains why, and a later `/refreshusers` warns "Added via Extra Member but never resolved to a real user_id".
- [ ] **A6** **`/editevent` semantics.** *Given* an open event. *When* A runs `/editevent` changing only the name, then only `-d`, then `-gi/-ni`, then `-d` with an invalid date. *Then* only the given fields change; all posts refresh; the invalid date is rejected **before** anything changes (nothing half-applied). (`newevent_editevent.md` N-series.)
- [ ] **A7** **Active-event warning.** *Given* an open (or stuck-in-verification) event. *When* M1 runs `/newevent` again. *Then* a warning names the old event and says commands now target the new one; after a proper Save & Close the warning does **not** appear.

### B. Capacity and waitlist (5)

- [ ] **B1** 🔥 **Waitlist and promotion.** *Given* an event with `-limit 2` and 2 going. *When* M2 clicks **Going**, then one of the going clicks **Not Going**. *Then* M2 first gets a visible "Event is full - added to the Waitlist"; after the Not Going M2 is **promoted** to going and gets a notification message (not silence).
- [ ] **B2** **Exactly at capacity.** *Given* `-limit 3` with headcount 2. *When* one person clicks **Going** (now 3 = limit), then another clicks **Going**, then a going person clicks **ADD** (guest). *Then* the third fills the event; the next **Going** and the guest **ADD** both go to the waitlist (a guest slot counts against the limit).
- [ ] **B3** **Raising the limit promotes FIFO globally.** *Given* a full event, waitlist queued in the order hub-person, child-person, hub-person. *When* A runs `/editevent -limit` raised by 3. *Then* all three are promoted in **that** order (global timestamp, not per-chat order). (`wl_waitlist.md`)
- [ ] **B4** **Lowering the limit.** *Given* headcount 4. *When* A runs `/editevent -limit 2`. *Then* the change is refused with a message and nothing is silently truncated. (`wl_waitlist.md`)
- [ ] **B5** **Waitlist visibility.** *Given* waitlist entries from hub and child, one person with several guest slots. *When* the event runs with `-wl visible`, then `hidden`, then `onlycount`, and `/waitlist` is run in hub and child. *Then* `visible`: hub shows everyone with "from <chat>", child only its own; `hidden`: no list; `onlycount`: only a count; a multi-slot person is **one line with a count**. Screenshot hub and child side by side.

### C. Sharing and visibility (4)

- [ ] **C1** 🔥 **Share to a child chat.** *Given* an open event in Hub-PRO. *When* A runs `/shareevent <child>`. *Then* Child gets its own post; the hub's post still works independently; clicks in the child are reflected in the hub totals.
- [ ] **C2** **Share check chain order.** *When* A shares (a) to the hub itself, (b) via an alias that points at the hub, (c) to a chat where the bot is not admin, (d) to a valid target. *Then* (a) and (b) are rejected as self-share **before** any bot-admin check; (c) is rejected for the missing admin right; (d) succeeds. (`shareevent.md` S1-S15)
- [ ] **C3** **Channel and per-share overrides.** *When* A shares to the Channel with `-mgl onlycount -sngl visible -swl hidden -clc off`, and to Child with defaults. *Then* the overrides apply **only** to the Channel's post; hub and Child are unaffected; clicking works from the channel.
- [ ] **C4** **Clickability.** *Given* an event shared to Child. *When* `-clc on` then `-clc off` is applied. *Then* a person is rendered the same way in **every** post of that event. A person added through Add Extra Member who never clicked is always plain text - that is a known limitation, but it must be consistent. (`clc_clickability.md` C1-C9)

### D. Permissions (5)

- [ ] **D1** 🔥 **Anonymous admin.** *Given* X (anonymous admin) in Hub-PRO. *When* X runs `/shareevent <child>`. *Then* it succeeds with no "disable anonymous" error; a non-admin member running it is still blocked.
- [ ] **D2** **Non-admin buttons.** *Given* an open event. *When* M2 clicks **Verify**, **Cancel**, **Kick**, **Save & Close**. *Then* nothing changes (alert or ignored); **Going / Not Going / ADD / DROP / ALL** work for M2.
- [ ] **D3** **Creator rights.** *Given* M1 (non-admin) creates event E1 and A creates E2. *When* M1 clicks **Verify**, **Save & Close** and **Cancel** on E1, and the same on E2. *Then* M1 can do all of it on **their own** E1 and none of it on E2; A can on both; M2 can on neither.
- [ ] **D4** **Owner-only commands are silent for others.** *When* A, in DM and in a group, sends `/setsub`, `/lockbot`, `/allgroups`, `/allchannels`, `/updatefeature`, `/showtable`, `/version`, `/stats -o`, `/deleteevent`. *Then* **no reply at all**, and `/help -o` does not reveal the owner screen. *When* O sends them: DM-only commands (`/setsub`, `/lockbot`, `/allgroups`, `/allchannels`, `/updatefeature`, `/showtable`, `/version`) in a **group** answer "only works in a DM"; `/stats -o` and `/deleteevent` work in the group.
- [ ] **D5** **`/lockbot`.** *When* O runs `/lockbot on` in DM. *Then* every chat stops responding to non-owners: commands and Kick/Return/±guest give an explicit lock message with a way to contact the owner, while **Going / Not Going / ADD / DROP / ALL** on an already-posted event still work; a non-owner's own `/lockbot` gets no reply; a bot restart keeps it locked; `/lockbot off` restores everything.

### E. Tiers and gating (4)

- [ ] **E1** **FREE versus PRO.** *Given* Hub-FREE and Hub-PRO. *When* A tries `/setalias`, `/addmonitor`, `/stats`, `/setsheet`, `/newevent -limit`, the Aliases / Monitoring / DM Access sections of `/help`, and DM commands. *Then* on FREE they show the locked badge or a PRO-only / "requires a higher tier" message; on PRO they work (`/setalias`, `/listaliases`, `/removealias` round-trip).
- [ ] **E2** **Expired PRO behaves as FREE.** *Given* (staging) Hub-PRO's `subs_date_end` set to yesterday. *When* A uses a PRO command and O runs `/stats -o`. *Then* the PRO command is denied; Hub-PRO counts under **FREE** and appears under "Expired in the last 30 days"; `/setsub <hub> on 30` restores PRO.
- [ ] **E3** **`/updatefeature` levels.** *When* O (DM) runs `/updatefeature deleteevent -minlevel pro`, then `-minlevel free`, then `-minlevel owner`, then `-minlevel admin`. *Then* `pro`: an admin of a PRO hub can use `/deleteevent` and the `/help` line appears, a FREE hub gets the PRO-only message; `free`: any admin of any hub; `owner`: silent for everyone but O; `admin` is rejected with a message listing `free`, `pro`, `owner`. The Control Sheet **BOTCONFIG** tab mirrors the level (columns FREE / PRO / **OWNER**).
- [ ] **E4** **Frozen feature snapshot.** *Given* an open event created while Verify was available. *When* O lowers the Verify feature for that hub via `/updatefeature` and a **new** event is created. *Then* the running event keeps its Verify button; the new event does not offer it.

### F. Google Sheets (6)

- [ ] **F1** 🔥 **PRO hub end to end.** *Given* Hub-PRO with its sheet. *When* A creates an event, people click, A saves & closes it; then a second event is canceled. *Then* Events tab: row `OPEN` on creation (EVENT_ID, EVENT_NAME, CREATED_DATE, CREATED_BY, EVENT_DATE, CLOSED_AT, STATUS, GOING_COUNT); updated to `CLOSED` with the right `GOING_COUNT` and `EventUsers` rows after Save & Close; `CANCELED` with no `EventUsers` rows for the second; `Actions` gets rows for button actions.
- [ ] **F2** **No sheet.** *When* A creates an event on Hub-FREE, then on a PRO hub with no sheet bound. *Then* FREE: silent, no Sheets attempt; PRO without a sheet: an explicit "Please specify google sheet for save" message.
- [ ] **F3** **Outage is visible, nothing is lost.** *Given* (staging) the service account's access revoked or credentials broken. *When* A runs `/newevent`, clicks, **Save & Close**, **Cancel**, `/refreshusers` and `/refreshusersall`. *Then* the events still work in Telegram; the chat shows "This event was created, but couldn't be saved to Google Sheets (...)" on create, "Event saved, but Google Sheets sync failed" on save, "Event cancelled, but Google Sheets sync failed" on cancel; `/refreshusers` says "Users tab sync incomplete" and `/refreshusersall` marks the chat "Partly synced" instead of success.
- [ ] **F4** **Recovery.** *Given* F3's events exist only in the DB. *When* access is restored and `docker compose exec bot python3 scripts/sync_events_to_sheets.py` is run (optionally `--days 30` / `--chat-id <hub>`). *Then* the missing events appear in Events (plus `EventUsers` for closed ones and approximate `Actions`); a second run adds nothing.
- [ ] **F5** **Users tab and presence log (hub).** *When* `/refreshusers` runs; a member's Telegram name changes; `/refreshusers` runs twice more; a member leaves; the refresh runs again and again; the member rejoins and leaves a second time. *Then* members are `MEMBER` with names; the changed name updates only that cell and unchanged names cause **no** writes on later refreshes; the leaver becomes `LEFT` with `DATE_end` and **exactly one** `UserPresenceLog` row (USER_ID, CHAT_ID, DATE_start, DATE_end) no matter how many refreshes follow; rejoining flips to `MEMBER` and clears `DATE_end` without deleting the log row; the second leave adds a second row.
- [ ] **F6** **Presence of a monitored chat.** *Given* Hub-PRO monitors Child (`/addmonitor`). *When* a member leaves Child and A runs `/refreshusersall`. *Then* their `Users` row (CHAT_ID = Child) becomes `LEFT` and the `UserPresenceLog` row for Child appears **in the hub's sheet**; the reply shows "Synced: <child> (-1)". (This was the bug where monitored chats were never logged.) Also have someone who **never** appeared in `Users` (joined and left between two refreshes) leave: after the refresh they get a `LEFT` row and a log row whose `DATE_start` is one hour before `DATE_end`.

### G. Users and monitoring (4)

- [ ] **G1** **`/refreshusers`.** *Given* a tracked user who left and an admin who isn't tracked. *When* A runs `/refreshusers`. *Then* the reply lists "Removed (left, invalid, or unverifiable)" and "Added (new admins found)"; with nothing to do it says "Nothing to change"; a person who was unresolvable becomes resolved after they become an admin or click something.
- [ ] **G2** **Monitoring.** *When* A runs `/addmonitor <child>` (bot admin there, then not admin), `/listmonitors`, `/removemonitor`, and `/refreshusersall` on Hub-FREE and on Hub-PRO. *Then* `/addmonitor` refuses a chat where the bot is not admin; FREE gets the PRO-only message for `/refreshusersall`; PRO lists every monitored chat with a `Synced (+n/-n)` / `Partly synced` / `Failed - <reason>` line.
- [ ] **G3** **User commands.** *When* A runs `/adduser <id> @name` twice, `/updateuser @name -p`, `/listusers`, `/listusers -a`, `/listusers -p` (and the long forms), `/notify`, and - after one member changed Going to Not Going and another left Child - `/who 24 -ng`, `/who 24 -l`, `/who 24 -l -c main`, `/who 24 -l -c <alias>`. *Then* the second `/adduser` says "Already in group"; `/listusers` shows "Tracked Users (N)" with **active before passive**; `-a` shows only "Active Users (N)", `-p` only "Passive Users (N)"; `/notify` pings only people who haven't responded; `/who -ng` (no hours = the last 6 h) lists only people who changed **from** another status (a first-time Not Going is not listed), `/who -l` lists departures grouped by chat (`main` = the main group), `-c` narrows it to `main` / an alias / `all`, and nobody is mentioned with `@` or a link.
- [ ] **G4** 🔥 **DM sticky group.** *Given* A administers two groups. *When* A sends `/start`, picks a group (or `/switchgroup`), then `/status` and a hub command in the DM. *Then* `/start` lists the groups A administers (auto-selects when there is just one); `/status` names the group it reports on; DM commands behave **identically** to running them in that group; a non-admin gets no access.

### H. `/deleteevent`, orphaned events and reopen (4)

- [ ] **H1** **Find and confirm.** *Given* O and several events (one with a Cyrillic name). *When* O runs `/deleteevent`, `/deleteevent <event_id>` (copy it from the Events tab), `/deleteevent футбол`, a name matching several events, a name matching none, and presses **Cancel**. *Then* the plain command lists the latest 10 as buttons; an id or a unique name goes straight to the "Delete this event?" screen (case-insensitive, Cyrillic works); several matches are listed; none says "No event matching"; **Cancel** says "Cancelled - nothing was deleted".
- [ ] **H2** **Deletion is complete and failure-safe.** *Given* an event shared to Child, plus another event. *When* O confirms deletion. *Then* the event is gone from the DB; its rows disappear from Events / Actions / EventUsers while the **other** event's rows stay; its posts in hub and Child are removed (the reply says "N of M removed" if Telegram refuses some); the other event keeps working. *Repeat with Sheets broken (F3):* the reply says "Nothing was deleted" and the event is fully intact; after repair the deletion succeeds.
- [ ] **H3** **Orphaned event heals itself.** *Given* an open event whose post an admin deleted by hand. *When* M1 runs `/newevent Next`. *Then* **no** "already an active event" warning; the new event is created; the old one is `CANCELED` in the DB and in the Events tab. With the old post **still present** the warning appears as before.
- [ ] **H4** **Reopen mode (`/reopen`).** *Given* O and an event that has just been closed (or canceled), in a group where no other event is open. *When* O runs `/reopen on`; a member presses **🔓 Reopen**; O presses it; the event is closed again with Save & Close; O runs `/reopen off`. Repeat once with a newer event still open, and once from a non-owner's `/reopen`. *Then* `/reopen on` names the event and the button appears under it (everyone can see it, only the hub post has it); the member gets an "Owners only" popup and nothing changes; O's press puts the event back to **open** - vote buttons are back in the hub post and every shared post, the `Events` row says `OPEN` with no `CLOSED_AT`, its `EventUsers` rows are gone, an `Actions` row `REOPEN` names O; after the second Save & Close each person is listed **once** in `EventUsers` (no doubled roster); with a newer event still open the press is refused and names it; an older event never shows the button; `/reopen off` makes the button disappear; a non-owner's `/reopen` gets no reply at all; the mode survives a bot restart.

### I. Statistics (2)

- [ ] **I1** 🔥 **`/stats` on a PRO hub.** *Given* Hub-PRO with several closed events, one canceled, one open, and one with `-limit` that had a waitlist. *When* A runs `/stats`, then `/stats 30d`, then the **Users** and **Distribution** buttons (and Next/Back). *Then* the text shows Events amount, "Events closed (N% completed)", cancelled, open now, total/average members, guest share, not going, and for the limited event "Events with a limit ... avg fill", "Hit the limit", "Waitlist: N joined, M still waiting at close"; `30d` narrows the numbers; the buttons work and return to Stats; the **Users** screen starts with "Unique attendees: N" (different people at >= 1 closed event, narrowed by the period); Hub-FREE gets the PRO-only message.
- [ ] **I2** **`/stats -o` (owner).** *When* O runs `/stats -o` and `/stats -owner` (DM and group), A runs `/stats -o`, and O runs `/stats -a`. *Then* O gets groups/channels totals, admin-rights counts, FREE/PRO (**PRO = active subscription**), "PRO subscriptions" (expiring within 7/30 days, expired in 30), "Growth" (added/removed/net), "Activity" (active/dormant groups, events, people served, top commands, top groups tagged FREE/PRO); adding the bot to a new group moves "Bot added 7d" by +1; A gets silence; `-a` no longer opens the report.

### J. Operations (4)

- [ ] **J1** 🔥 **`/version` after a deploy.** *When* O sends `/version` in DM right after deploying; A sends it; O sends it in a group. *Then* O sees the **deployed** version, a recent "Started ... (up ...)" time (proves the container really restarted) and the Python / python-telegram-bot versions; A gets silence; the group answer says it only works in a DM. `docker compose logs bot | grep "Bot v"` agrees.
- [ ] **J2** **Bot membership lifecycle.** *When* the bot is added to a new group, promoted to admin, demoted, then removed. *Then* the group appears in `/allgroups` and the Control Sheet **GROUPS** tab; the `ROLE` column flips `MEMBER` / `ADMIN` and `/stats -o` admin counts follow; on removal it leaves `/allgroups` and a row with both dates appears in the **chats_log** tab.
- [ ] **J3** **Migration rehearsal.** *Given* a **copy** of the production DB and the release candidate. *When* the new build starts against it, twice. *Then* no errors in the logs; `all_features` has no `min_tier = 'ADMIN'` (renamed `OWNER`); `events.waitlist_joined` and the trigger `events_count_waitlist_joins` exist; BOTCONFIG's header says OWNER; the second start changes nothing. Check with:
  ```
  docker compose exec bot python3 -c "import sqlite3; c=sqlite3.connect('database.db'); print(c.execute(\"select feature_key,min_tier from all_features where min_tier='ADMIN'\").fetchall()); print([r[0] for r in c.execute(\"select name from sqlite_master where type='trigger'\")])"
  ```
- [ ] **J4** **Restart mid-event.** *Given* an open event with votes and a waitlist (and `/lockbot on`). *When* the container is restarted. *Then* buttons keep working, counts and the waitlist are unchanged, no duplicate Sheets rows appear, and the lock is still on.

### K. Help (1)

- [ ] **K1** 🔥 **`/help` as member, admin and owner.** *When* M1, A and O open `/help` on Hub-PRO and Hub-FREE and walk the sections; `/userid` and `/chatid` are run. *Then* the main screen is short and the "More about Flags" toggle round-trips to identical text; Users / Utility / Event Lifecycle / Distribution open; Aliases / Monitoring / DM Access are locked on Hub-FREE; Utility lists `/start` and shows `/stats` and `/setsheet` only on a PRO hub; Users lists `/who` (and its flags under "MORE about Flags") only where the feature is available; Aliases names `main` and `all` as reserved; Event Lifecycle says "admin or event creator"; the `/deleteevent` line appears **only** for O; `/help -o` (O) lists the DM-only commands (including `/version`) apart from "also work inside a group" (`/stats -o|-owner`, `/deleteevent`); a non-owner cannot open the owner screen; the owner screen expands one flags block at a time. (Text validity and command coverage are checked automatically by `tests/test_help_audit.py`.)

## 4. Exploratory charters (15-20 min each, pick one per release)

1. **Two people at once.** Two accounts click Going / ADD / Not Going on a full event at the same moment; double-tap one button. Look for duplicate entries, double promotion, wrong counts.
2. **Hostile text.** Event names, aliases, usernames and chat names containing `_ * [ ] ( ) ~ \` > # + - = | { } . !`, emoji, very long names, Cyrillic. Every command's reply must still render (one unescaped character makes Telegram drop the whole message).
3. **Stale buttons.** Use buttons of an old post after a restart, after the event closed, after `/updatefeature` changed a tier, after a `/deleteevent`.
4. **Client differences.** Open the same event, `/help` and `/stats` on iOS, Android and Desktop; check keyboards and formatting.
5. **Rate limits.** Fire several commands in quick succession and run `/refreshusersall` on a large group; watch for dropped replies.
6. **Time.** Events with `-d` in the past/near future; subscription ending while a command runs; the day boundary in `/stats` periods.

## 5. Known limitations (do not file as bugs)

- A person added via Add Extra Member who the bot has never seen in any chat of this hub always renders as plain text (consistently); the bot says so right after the name is typed.
- "Joined the waitlist" in `/stats` counts only from the release that introduced the counter; older events show just the people left waiting.
- While `/reopen` mode is on, the 🔓 Reopen button is visible to everybody in the hub post (Telegram can't show an inline button to one person only); a non-owner who presses it just gets an "Owners only" popup.
- `/who -l` shows when a refresh *noticed* a departure (there is no automatic refresh), and a departure is only seen at all if Telegram delivered the `chat_member` event while the bot was running.
- `/who -ng` cannot tell a Going click that ended in the waitlist from a real Going, and Kick rows name the admin, not the kicked person - kicks are not reported.
- Commands typed in a DM count toward "top commands" but cannot be attributed to a group in `/stats -o`.
- The first `/refreshusersall` after upgrading may append older, previously missed `UserPresenceLog` rows once.
- Telegram may refuse to delete old messages, so `/deleteevent` can report "N of M removed".

## 6. What NOT to test manually

Anything the 1000+ automated tests already cover and that does not touch the real Telegram or Google APIs: data transformations, SQL and migrations (logic), message formatting and MarkdownV2 validity of every help text, permission checks at function level, stats arithmetic. Spend the time on what only a human can see: real taps and timing, how a message **looks** in the client, two live chat windows against each other, real Sheets, a real deploy.

## 7. Reporting a bug and the run log

Report: **case id**, version (`/version`), account/role, chat, exact steps, expected vs actual, screenshots of every involved chat, and the relevant `docker compose logs bot` lines.

| Date | Version | Tester | Cases run | Failures | Notes |
|---|---|---|---|---|---|
|  |  |  |  |  |  |
