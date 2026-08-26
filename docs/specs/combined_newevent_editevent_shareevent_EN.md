# Behavior Specs: /newevent, /editevent, /shareevent

This single file consolidates the command-level specs for `/newevent`, `/editevent`, `/shareevent`, plus the shared flags they use (`-wl`, `-ngl`, `-clc`) and the vocabulary those flags share. Written as an independent source of truth for behavior — not derived from reading the code casually, but verified line-by-line against the actual implementation. Each section ends with Given/When/Then-style test scenarios that double as a manual or automated test checklist.

## Table of contents

- [Commands](#commands)
  - [/newevent and /editevent](#newevent-and-editevent)
  - [/shareevent](#shareevent)
- [Shared flags](#shared-flags)
  - [Shared vocabulary: visible / hidden / onlycount](#shared-vocabulary-visible--hidden--onlycount)
  - [-wl / -waitlist](#-wl---waitlist)
  - [-ngl / -notgoinglist](#-ngl---notgoinglist)
  - [-clc / -clickability](#-clc---clickability)

---

# Commands

## /newevent and /editevent

**File:** `handlers.py`
**Shared parser:** `parse_event_args()` — both commands use the exact same flag set
**Role:** create and edit a Going/Not-Going event in the current chat (hub)

### Syntax

```
/newevent <name> [-d dd.mm.yyyy [HH:MM]] [-gi <emoji>] [-ni <emoji>]
          [-limit N] [-wl visible|hidden|onlycount] [-ngl visible|hidden|onlycount]
          [-clc on|off]

/editevent [name] [-d dd.mm.yyyy [HH:MM]] [-limit N]
           [-wl visible|hidden|onlycount] [-ngl visible|hidden|onlycount] [-clc on|off]
```

For `/editevent`, every parameter is optional except the requirement to provide **at least one**. Any field not explicitly given keeps its current value.

### Flags — where they're covered

| Flag | Shared/unique | Where |
|---|---|---|
| `-d` / `-date` | unique to this command pair | below, this section |
| `-gi` / `-goingicon` | unique to this command pair | below, this section |
| `-ni` / `-notgoingicon` | unique to this command pair | below, this section |
| `-limit` | **different meaning** in `/updatefeature` — do not confuse | below, this section |
| `-wl` / `-waitlist` | shared vocabulary | [see below](#-wl---waitlist) |
| `-ngl` / `-notgoinglist` | shared vocabulary | [see below](#-ngl---notgoinglist) |
| `-clc` / `-clickability` | **also shared with `/shareevent`** | [see below](#-clc---clickability) |

### `-d` / `-date <dd.mm.yyyy> [HH:MM]`

- Time is optional; if omitted, the event has no specific time.
- Invalid format → error `"Invalid date format. Use dd.mm.yyyy or dd.mm.yyyy HH:MM"`, execution stops **before** any DB write.
- In `/editevent`: if the flag isn't passed, the date is left untouched entirely (current value kept).
- Default (for `/newevent`, if omitted): `None` — no date shown in the post.

### `-gi` / `-goingicon <emoji>` and `-ni` / `-notgoingicon <emoji>`

- Simply replace the 👍/❌ icon in the post with any given emoji token.
- No content validation — whatever is passed is used as-is.
- Default: `DEFAULT_GOING_ICON` / `DEFAULT_NOTGOING_ICON` (global config constants).
- In `/editevent`: if not passed, the current icon is kept.

### `-limit <N>`

⚠️ **Do not confuse** with `/updatefeature`'s own `-limit` — that one is a per-feature tier usage limit, this one is the event's own headcount cap. Only the flag string is shared.

- Requires a tier with the `event_limit` feature (PRO by default). Without it, an error is returned and execution stops **before** any DB write.
- Caps the combined total (Going + guests) **across every chat at once** — the main hub plus every share.
- Once the cap is reached, a new Going click joins the Waitlist instead of being added instantly.
- **`/editevent` — lowering the limit below the current headcount is rejected**: if the new `-limit` is less than the current combined total (hub + every child chat), the limit stays **unchanged**, a warning with the current headcount is shown, and nothing else in the same command is applied either.
- **`/editevent` — raising the limit promotes from the queue**: if the new limit is higher than the current headcount, freed slots are filled from the Waitlist **FIFO by global timestamp** (not per-chat), until either the queue is empty or slots run out. Each promoted person gets their own notification in their own chat.
- In `/editevent`, if the flag isn't passed, the limit is left unchanged entirely (and no promotion runs, accordingly).

### Full execution flow

#### `/newevent`
1. Resolve the hub (`resolve_hub_chat_id`) — if it fails (e.g. a DM with no group picked), the command silently stops (resolution itself isn't specific to this command).
2. No arguments at all → error `"Event name is required"`.
3. Parse all flags via `parse_event_args`.
4. Validate `-d`, if given.
5. Validate `-limit` (gating + numeric) — **before** the other gates.
6. Validate `-wl` (gating) — **after** `-limit`.
7. `-ngl` — no gating, applied as-is.
8. Validate `-clc` (gating) — last of the flags.
9. Generate `event_id` (UUID, first 8 characters).
10. Snapshot features (`verification`, `add_extra_member`) — **frozen at creation time**, unaffected by a later `/updatefeature`.
11. Check whether an active event already exists in this chat (status 0 or 1) — if so, the new one is still created, but the user gets a warning that the old post stays clickable for participants while commands like `/waitlist`/`/editevent` now target the new one.
12. `INSERT` into the `events` table.
13. Send the message with its keyboard (Going/Not Going/ADD/Drop/ALL/Verify or Save&Close/Cancel).
14. Write the `message_id` back to the DB.
15. If the hub is premium — sync a row into the Events Google Sheet tab (columns: EVENT_ID, EVENT_NAME, CREATED_AT, CREATED_BY, EVENT_DATE, CLOSED_AT, STATUS, AMOUNT). If premium but no sheet bound — a separate message: `"Please specify google sheet for save"`.

#### `/editevent`
1. Resolve the hub.
2. No arguments → error `"Provide at least one value to update"`.
3. Look up an active event (status 0 or 1) in this chat — if none, error `"No active event found to edit"`.
4. Parse flags via the same `parse_event_args`.
5. For each field — if the flag was given, update it; otherwise keep the current value.
6. `-limit`, if given, triggers separate logic: lowering-rejection check → possible promotion from the queue (see above).
7. A single `UPDATE`, all fields at once, at the end.
8. Any promotion notifications (if triggered) are sent **before** the reply to the user.
9. Reply `"Event updated. Refreshing views."` + an async task to refresh every post (hub + every share).
10. Sync name/date to the Events Google Sheet tab (only if they actually changed; looked up by `EVENT_ID`).

### Test scenarios

**Basic behavior**
- [ ] **N1.** `/newevent` with no arguments → error about the required name, nothing is created.
- [ ] **N2.** `/newevent Party` → created with default icons, no date, `waitlist_visibility='hidden'`, `notgoing_visibility='visible'`, `clickability='on'`, `total_limit=NULL`.
- [ ] **N3.** `/newevent Party -d 01.01.2027` → date saved and shown in the post.
- [ ] **N4.** `/newevent Party -d notadate` → format error, event **not created**.
- [ ] **N5.** `/editevent` with no arguments → error, nothing changes.
- [ ] **N6.** `/editevent` with no active event in the chat → error "No active event found to edit".
- [ ] **N7.** `/editevent NewName` → name changes, everything else (icons, date, limit, etc.) stays as-is.

**Icons**
- [ ] **N8.** `/newevent Party -gi 🎉 -ni 🙅` → both icons replaced with the custom ones in the post and on the Going/Not Going button labels.
- [ ] **N9.** `/editevent -gi 🎉` (no `-ni`) → only the Going icon changes, Not Going stays as-is.

**`-limit`**
- [ ] **N10.** FREE hub, `/newevent Party -limit 10` → error "requires a higher tier", event **not created**.
- [ ] **N11.** PRO hub, `/newevent Party -limit 10` → `total_limit=10` saved.
- [ ] **N12.** Active event, headcount=8 (hub + children), `/editevent -limit 5` → rejected with the current headcount shown, `total_limit` unchanged.
- [ ] **N13.** Active event, headcount=5, waitlist has 3 people (different timestamps), `/editevent -limit 8` → 3 slots free up, all 3 are promoted FIFO by timestamp, each gets a notification, waitlist ends up empty.
- [ ] **N14.** Same setup, but only 1 slot frees up (`-limit 6`) → **only the oldest** by timestamp is promoted, the other 2 stay queued.
- [ ] **N15.** The queue has a guest slot whose owner is **no longer going** (clicked Not Going after being queued) → that entry is **discarded without spending a slot** from the promotion budget, promotion continues with the next entry.

**Existing active event**
- [ ] **N16.** An open event (`status=0`) already exists in the chat, `/newevent NewParty` → the new one is created, an **additional** warning about the old one is shown (doesn't block creation).

**Google Sheets**
- [ ] **N17.** FREE hub, `/newevent Party` → **no** attempt to write to Sheets at all (fully silent).
- [ ] **N18.** PRO hub with no sheet bound, `/newevent Party` → separate message "Please specify google sheet for save", the event is still created in the DB.
- [ ] **N19.** PRO hub with a sheet, `/editevent NewName` → the event's row in the Events tab is updated by `EVENT_ID`, only `EVENT_NAME` (column B) changes, date untouched if `-d` wasn't passed.

**Flag combinations**
- [ ] **N20.** `/newevent Party -limit 20 -wl visible -ngl hidden -clc off` — all 4 gated/ungated flags apply together, argument order doesn't matter.
- [ ] **N21.** FREE hub, `/newevent Party -wl visible -clc off` (no `-limit` at all) → `-wl` still requires the tier (gated by the same `event_limit` feature, regardless of whether `-limit` is present in the same command) → error, event not created.

---

## /shareevent

**Unique flags:** `-mgl`, `-sngl`, `-swl` (covered here)
**Shared flag:** [`-clc`](#-clc---clickability) (here it acts as a **per-share override**, not the whole event's setting)

### Purpose

Forwards a synced sub-view of the active event to a child group/channel. Every error message is routed back to the main hub (even when the actual failure happened while reaching the target chat) — an admin should never have to look inside the target chat to figure out what went wrong.

### Full check order (matters — the very first failure stops everything)

1. **Syntax** — at least one argument (target) is required. Otherwise, an error with the full flag list.
2. **`-clc` gating** — if `-clc`/`-clickability` was passed but the hub lacks the `clickability` feature → error, **the whole command** stops (even if every other flag is valid).
3. **Active event** — must exist (`event_status IN (0, 1)`) in the chat the command was run from. Otherwise, "No active event found for this group".
4. **`-limit` capacity check** — if the event has a `-limit` set, and the current headcount (hub + every existing share) has **already reached or exceeded** it → sharing is blocked entirely; a new group/channel can't be added until a slot frees up.
5. **Target resolution** — first looked up as an alias (set via `/setalias`) for this hub; if not found, used literally as a chat_id.
6. **Can't share to itself** — if the resolved target equals the hub itself → error.
7. **Share-count limit** (`get_feature_limit_for_chat(..., "shareevent")`) — if this (hub → target) pair has already reached the maximum number of shares allowed by the tier → error stating the limit. `None` = unlimited.
8. **Already shared?** — if this event_id already has a row in `event_shares` for this target → error "already been added", a second one is never created (would need a separate removal mechanism first, if one exists).
9. **Target chat validity** — `get_chat()` on the target. Not found / bot not a member → "Chat not found or bot is not a member".
10. **Is the bot an admin in the target?** — if the bot isn't in the chat, or is but isn't an admin → two distinct messages ("add and promote" vs "please promote").
11. **Caller's anonymity** — if the command was run as "Group Anonymous Bot" (anonymous admin mode) → a dedicated error asking to disable anonymity (admin status can't be verified anonymously).
12. **Is the caller an admin in the target?** — checks the **personal** membership and admin status of the actual person who ran the command in the target chat, not just the bot. Not a member / not an admin → corresponding error.
13. If every check passes — a placeholder message is sent to the target, a row is created in `event_shares`, a confirmation is sent to the hub, and a full render is scheduled (`schedule_view_refresh`).

### Unique flags

**`-mgl` / `-maingoinglist visible|hidden|onlycount`**
— Going-list visibility **specifically in this shared post**. Default is `onlycount` (the only flag in this command whose default **isn't** `visible`, unlike `-wl`/`-ngl` on the other commands).

**`-sngl` / `-sharenotgoinglist visible|hidden|onlycount`**
— Override of Not Going visibility **for this specific share**. If omitted, inherits the event's own `-ngl` value.

**`-swl` / `-sharewaitlist visible|hidden|onlycount`**
— Override of Waitlist visibility **for this specific share**. If omitted, inherits the event's own `-wl` value.

**`-clc` / `-clickability on|off`** — see [the shared flag spec](#-clc---clickability). Here it's an override for this specific chat, not the whole event.

### Argument order

Flags and the target **can appear in any order** — the parser first extracts every recognized flag, and the first remaining "free" token becomes the target. Example: `/shareevent -mgl visible @mychannel` behaves identically to `/shareevent @mychannel -mgl visible`.

### Alias vs numeric chat_id

The target can be:
- **An alias** (set up via `/setalias` for this hub) — resolved to a real chat_id before any further checks.
- **A numeric chat_id directly** (`-1001234567890`) — used as-is.
- **`@username`** — passed to `get_chat()` as-is; Telegram resolves it.

### Test scenarios

- [ ] **S1 — Syntax.** `/shareevent` with no arguments → error with the flag hint.
- [ ] **S2 — `-mgl` default.** `/shareevent -200` with no `-mgl` → `share_mode = '-onlycount'`.
- [ ] **S3 — `-clc` gating on FREE.** FREE hub, `/shareevent -200 -clc off` → error, **the entire share is not created** (even if everything else is valid).
- [ ] **S4 — No active event.** Hub with no event → "No active event found for this group".
- [ ] **S5 — Capacity block.** Event with `-limit 10`, headcount already 10 → `/shareevent` to **any** new target is fully blocked.
- [ ] **S6 — Share to self.** `/shareevent` with target = the hub itself → error "Cannot share an event to the same group that owns it".
- [ ] **S7 — Duplicate share.** Same event_id + same target already in `event_shares` → error "already been added", no second row created.
- [ ] **S8 — Share-count limit.** Tier caps 2 shares per target, 2 already exist → the 3rd is rejected with the limit stated.
- [ ] **S9 — Bot not admin in target.** `get_chat_member` for the bot returns `member` (not admin/creator) → "Bot is in the chat but not an admin", no row created.
- [ ] **S10 — Caller in anonymous mode.** `user_id == GROUP_ANONYMOUS_BOT_ID` → dedicated error about disabling anonymity, no further checks run.
- [ ] **S11 — Caller not admin in target.** Bot is an admin, but the caller is a regular member of the target → "You need admin rights in the target chat to share there".
- [ ] **S12 — Alias resolution.** Target given as a configured alias → correctly resolved to the real chat_id **before** every subsequent check (including "share to self").
- [ ] **S13 — Flag order doesn't matter.** `/shareevent -sngl hidden @channel -mgl visible` and `/shareevent @channel -mgl visible -sngl hidden` produce identical results.
- [ ] **S14 — Successful share.** Every check passes → target gets the placeholder message, hub gets a confirmation with the target's name/alias, `event_shares` has a new row with every override field set.
- [ ] **S15 — Override beats inheritance.** Event with `-ngl hidden`, share created with `-sngl visible` → **this** child chat shows Not Going, despite `hidden` on the event overall.

---

# Shared flags

## Shared vocabulary: visible / hidden / onlycount

Used by [`-wl`](#-wl---waitlist) and [`-ngl`](#-ngl---notgoinglist). The general idea is the same across both — three levels of detail for a list in the post. Exact behavior per level is described in each flag's own section (`-wl` has a hub-vs-child-chat nuance, `-ngl` doesn't).

| Value | General meaning |
|---|---|
| `visible` | The list is shown in full — with names |
| `onlycount` | Only the total count is shown, no names |
| `hidden` | The section isn't shown in the post at all |

**Important:** `hidden` ≠ "the feature is off". The underlying mechanic (queuing / tracking Not Going) always keeps working — the flag only controls what's visible **in the post**. Waitlist has a separate way to view it outside the post — the `/waitlist` command (admin-only), which ignores this flag entirely.

**Case:** values are case-insensitive on input (`-wl VISIBLE` works the same as `-wl visible`), but stored lowercase in the DB.

## -wl / -waitlist

**Used in:** [`/newevent`, `/editevent`](#newevent-and-editevent) (`/shareevent` has its own override — `-swl`, see that command's section)
**Syntax:** `-wl visible|hidden|onlycount` (or `-waitlist ...`)
**Tier:** requires PRO (feature `event_limit`) — without it, the flag is rejected with an error
**Default:** `hidden` if the flag isn't given
**Values:** see the [shared vocabulary](#shared-vocabulary-visible--hidden--onlycount) above

### Purpose

Controls whether the Waitlist is shown in the event post. The queue itself always works (if `-limit` is set and capacity is reached) — the flag only affects visibility in the post, not the queuing mechanic itself.

### Detailed behavior per value

1. **`hidden`** (default) — Waitlist isn't shown anywhere in the post. The only way to see it is `/waitlist` (admin-only, ignores this flag).
2. **`onlycount`** — only the total number of waiting people **across every chat at once** is shown. Same number in the hub and in any child chat.
3. **`visible`** — this is where a key hub-vs-child difference kicks in:
   - **In the hub's post:** the list of **every** waiting person across **every** chat the event was shared to. Entries from child chats are labeled `from <chat name>`.
   - **In a child chat's post:** only entries added from that specific chat — no cross-chat entries, no `from` label.
   - Guest slots are grouped by owner — multiple slots held by the same person collapse into a single line with a count.

### Interaction with other flags/features

- **`-limit`**: Waitlist only makes sense once a limit is set, but `-wl` can be changed independently at any time — not necessarily in the same command as `-limit`.
- **[`-clc`](#-clc---clickability)**: separately decides whether names inside the Waitlist list are clickable.
- **`/shareevent -swl`**: per-share override — a specific child chat can have its own `-wl` value, different from the event's. Without an override, it inherits the event's value.

### Test scenarios

- [ ] **W1 — Default.** `/newevent Party` with no `-wl` → `waitlist_visibility = 'hidden'`.
- [ ] **W2 — Explicit choice.** `/newevent Party -limit 10 -wl visible` → `waitlist_visibility = 'visible'`.
- [ ] **W3 — Gating on FREE.** FREE hub, `-wl visible` → error "requires a higher tier", change not applied.
- [ ] **W4 — Gating on PRO.** PRO hub, same call → applies without error.
- [ ] **W5 — `hidden`: render.** 2 people waiting → the post **has no** Waitlist section at all.
- [ ] **W6 — `onlycount`: render.** 3 waiting (2 in hub + 1 in a child) → both hub and child show the same `3`, no names.
- [ ] **W7 — `visible` in the hub: cross-chat.** 1 waiting locally in the hub + 1 in a child chat → hub's post shows both, the child one is labeled `from <chat name>`, the local one isn't.
- [ ] **W8 — `visible` in a child chat: isolation.** Same setup → the child chat's post shows only its own local waiting person.
- [ ] **W9 — Guest grouping.** One person holds 3 guest slots in a row → in `visible` rendering, that's a single line with `3`, not three separate ones.
- [ ] **W10 — `/waitlist` ignores the flag.** `-wl hidden`, people waiting → `/waitlist` still returns the full list.
- [ ] **W11 — Per-share override.** Event `-wl hidden`, a child chat shared with `-swl visible` → that specific child chat shows the Waitlist, despite `hidden` on the event.
- [ ] **W12 — Change on the fly.** Event open with `-wl hidden` and an accumulated queue → `/editevent -wl visible` → the post updates and shows the already-accumulated queue, not just new entries.
- [ ] **W13 — Lowering `-limit` below the current headcount.** If `-limit` is lowered below the current combined headcount (hub + every child) in the same command → the whole change is **rejected** (limit stays unchanged), and `-wl` from the same command is **not applied either**, since the whole command fails before any DB write.

## -ngl / -notgoinglist

**Used in:** [`/newevent`, `/editevent`](#newevent-and-editevent) (`/shareevent` has its own override — `-sngl`, see that command's section)
**Syntax:** `-ngl visible|hidden|onlycount` (or `-notgoinglist ...`)
**Tier:** not gated, available on any tier
**Default:** `visible` if the flag isn't given
**Values:** see the [shared vocabulary](#shared-vocabulary-visible--hidden--onlycount) above

### Purpose

Controls the "Not Going" list's visibility in the event post.

### Detailed behavior per value

1. **`visible`** (default) — the list is shown in full, with names. This is the behavior that existed **before** this flag was introduced — older events with no explicit value behave the same way.
2. **`onlycount`** — only the count is shown, no names.
3. **`hidden`** — the "Not Going" section is entirely absent from the post (not just empty — the header isn't rendered either).

### Difference from `-wl`

Unlike `-wl`, `-ngl` has **no** hub-vs-child distinction — "Not Going" is always local to whichever chat the click happened in (no cross-chat aggregation concept, unlike Waitlist).

### Important nuance — not gated

Unlike `-wl` and `-clc`, this flag is **available on any tier**, including FREE. Reason: the "Not Going" list existed and was visible to everyone before this flag even existed; the flag only added the ability to hide/summarize it, without restricting access to the base functionality itself.

### Interaction with other flags

- **[`-clc`](#-clc---clickability)**: separately decides whether names inside the Not Going list are clickable.
- **`/shareevent -sngl`**: per-share override for a specific child chat.

### Test scenarios

- [ ] **NG1 — Default.** `/newevent Party` with no `-ngl` → `notgoing_visibility = 'visible'`.
- [ ] **NG2 — Explicit choice.** `/newevent Party -ngl hidden` → `notgoing_visibility = 'hidden'`.
- [ ] **NG3 — No gating.** FREE hub, `-ngl hidden` → applies **without** a tier error (unlike `-wl`/`-clc`).
- [ ] **NG4 — `hidden`: render.** 2 people in Not Going → the post has **neither** the section **nor** the "Not Going" header.
- [ ] **NG5 — `onlycount`: render.** 3 people in Not Going → only the number `3` is shown, no names.
- [ ] **NG6 — `visible`: render.** The list is shown in full with names, identical to pre-flag behavior.
- [ ] **NG7 — Locality, no cross-chat.** Event shared to a child chat, someone clicks Not Going there → this is **not** reflected in the hub's Not Going list and vice versa (unlike Waitlist with `-wl visible`, where the hub sees every chat).
- [ ] **NG8 — Per-share override.** Event `-ngl visible`, a child chat shared with `-sngl hidden` → that specific child chat hides Not Going, the hub keeps showing the full list.
- [ ] **NG9 — Change on the fly.** Event open with `-ngl visible` → `/editevent -ngl hidden` → the post updates, the section disappears on the next render.

## -clc / -clickability

**Used in:** [`/newevent`, `/editevent`](#newevent-and-editevent), [`/shareevent`](#shareevent) (there — as a per-share override, see below)
**Syntax:** `-clc on|off` (or `-clickability ...`)
**Tier:** requires PRO (feature `clickability`) — without it, the flag is rejected with an error
**Default:** `on`, if not given at either the event level or a specific share's level

### Purpose

Controls whether names in the post are clickable Telegram mentions (`tg://user?id=...`) or plain text.

### Detailed behavior

1. **`on`** (default) — every name for which a `user_id` could be resolved renders as a clickable mention.
2. **`off`** — every name renders as plain, escaped text, even when the `user_id` is known.

### Important nuance — doesn't guarantee 100% coverage

`-clc on` means **"link wherever it's physically possible"**, not "everyone is guaranteed a link". If a specific person has no resolvable `user_id` on record (e.g. added via Add Extra Member without resolution, or has never interacted with the bot and isn't an admin) — they **always** render as plain text, regardless of the flag's value. This is a Telegram Bot API limitation, not a bug: there's no way to look up an arbitrary user's ID by username without their personal interaction.

### At the `/shareevent` level — per-share override

In `/shareevent`, the `-clc` flag sets an override **only for that specific shared chat**, not the whole event:
- If not given → the chat inherits the event's own `-clc` value.
- If given → **this specific** chat uses its own value, independent of the event's setting (can even be the opposite).

### Interaction with other flags

- Independent of `-wl`/`-ngl` — applies equally to Going, Not Going, and Waitlist lists.
- In the master hub, cross-chat entries ("Going from <chat>") use the clickability setting **of their originating chat**, not the hub the post is displayed in.

### Test scenarios

- [ ] **C1 — Default.** `/newevent Party` with no `-clc` → `clickability = 'on'`.
- [ ] **C2 — Explicit choice.** `/newevent Party -clc off` → `clickability = 'off'`.
- [ ] **C3 — Gating on FREE.** FREE hub, `-clc off` → error "requires a higher tier", change not applied.
- [ ] **C4 — `off` render.** A person with a valid `user_id` is in Going → with `clickability=off`, their name renders **without** a link (plain escaped text).
- [ ] **C5 — `on` render, but no ID.** A person added via Add Extra Member without resolution, `clickability=on` → their name still renders **without** a link (no data to link with — this is expected, not a bug).
- [ ] **C6 — Partial clickability in one post.** Two people in the same Going list: one has a `user_id`, the other doesn't → the first is clickable, the second is plain text, **both** with `clickability=on`. This is correct behavior, not a desync.
- [ ] **C7 — Per-share override, chat=off while event=on.** Event with `clickability=on`, a specific share with `-clc off` → **only that** child chat shows everyone as plain text; everything else (hub and other children) stays clickable as usual.
- [ ] **C8 — No per-share override.** A share with no `-clc` → inherits the event's value.
- [ ] **C9 — Update via `/editevent`.** Event open with `clickability=on` and existing posts → `/editevent -clc off` → every post (hub + every child without its own override) re-renders with plain text instead of links.
