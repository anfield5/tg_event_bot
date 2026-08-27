# Manual Testing Guide

This is a **testing plan**, not a duplicate of the behavior specs already in `docs/specs/`. Those files (129 checkbox scenarios total, Given/When/Then style) remain the source of truth for exact expected behavior - this document tells you **which ones to run, in what order, and how**, so testing effort goes where bugs have actually happened before, not alphabetically through every file.

**How to mark a scenario as tested:** edit the checkbox directly in its own spec file (`docs/specs/...`), not here - e.g. `- [x] ✅ 27.08 v3.31.0`. Keeping the result next to the scenario it belongs to avoids a second system drifting out of sync with the first.

**Minimum setup to run anything below:** one hub group where you're admin, one child group/channel the bot is also admin in, and your own DM with the bot. Most scenarios here need at least 2 of these 3 vantage points open side by side - you can't verify "child chat sees only its own Waitlist" from the hub alone.

---

## Tier 1 - Smoke test (run before every deploy, ~15 minutes)

The narrow path that's actually broken in production before. Not exhaustive - just "did this deploy not break the basics".

1. **Create + vote.** `/newevent Smoke Test` → click Going in the hub → click Going in the child chat too. Both posts update, both counts agree.
2. **Guest math.** ADD twice, then DROP once → guest count is 1, not 0 or 2 (off-by-one is the classic regression here).
3. **ALL button.** With 2+ guests added, click ALL → guest count is 0, not partially reduced.
4. **Waitlist promotion.** `/editevent -limit 1` with 2 people already going → one gets bumped to Waitlist; have them click Not Going → the waitlist person gets promoted, with a real notification, not silence.
5. **Share to child.** `/shareevent` to your child chat → child gets its own post, hub's own post still works independently.
6. **Verify → Save & Close.** Click Verify → click Save & Close Event on the SAME post → event status is genuinely closed, not stuck in verification (see [`docs/specs/commands/newevent_editevent.md`](specs/commands/newevent_editevent.md) N13-N15 for the promotion nuances specifically).
7. **Cancel from open state.** New event, click Cancel directly (no Verify first) → event is canceled, nothing written to EventUsers.
8. **`/help` main screen.** Opens, is short (not the old 1800-character wall of text), the flags toggle expands/collapses correctly.
9. **DM sticky group.** Run `/status` via DM → the reply names which group it's reporting on (this was silently wrong before - see [item 2's fix](specs/full_index.md)).
10. **Anonymous `/shareevent`.** If your hub has an anonymous-admin-capable member, have them run `/shareevent` anonymously → it succeeds, no "disable anonymous" error.

If any of these 10 fail, stop and file it as a regression before testing anything else - these are the load-bearing paths.

---

## Tier 2 - Full regression pass (after a significant change, or periodically)

Organized by where bugs have actually been found this project's history, most fragile first.

### 2.1 Waitlist + promotion (highest historical bug density)
Full scenarios: [`docs/specs/flags/wl_waitlist.md`](specs/flags/wl_waitlist.md) (13 scenarios, W1-W13)

- Hub-vs-child visibility split (`visible` mode: hub sees everyone with `from <chat>`, child sees only its own - **easy to eyeball wrong**, screenshot-compare both posts side by side)
- Guest-slot grouping in the queue (one person, multiple slots → one line with a count, not N separate lines)
- `-limit` lowering rejection (headcount already above the new limit → change refused, nothing silently truncated)
- `-limit` raising promotes FIFO by **global timestamp**, not per-chat order - queue 3 people across hub + child in a specific order, raise the limit, confirm promotion order matches

### 2.2 Clickability (`-clc`) + name resolution
Full scenarios: [`docs/specs/flags/clc_clickability.md`](specs/flags/clc_clickability.md) (9 scenarios, C1-C9)

- **Known limitation, not a bug:** someone with no resolvable `user_id` on file (added via Add Extra Member, never personally clicked anything) always renders as plain text regardless of `-clc on`. Don't file this as a bug - confirm it's the SAME name showing as plain text in every render of that person, not inconsistent between renders.
- Per-share override (`-clc off` on one specific share) affects only that child, not the hub or other children.
- **If you see the SAME person clickable in one post but plain text in another post of the SAME event** - this is NOT expected (see the investigation in this session's own history) - screenshot both posts and file it, since the current code was verified to render both identically from shared data.

### 2.3 `/newevent` + `/editevent` flags
Full scenarios: [`docs/specs/commands/newevent_editevent.md`](specs/commands/newevent_editevent.md) (21 scenarios, N1-N21)

- Icon overrides (`-gi`/`-ni`) actually change the button labels, not just the post text
- `-d` invalid format rejected before any DB write (event not half-created)
- Existing-active-event warning fires for a genuinely stuck-in-verification event, but should NOT fire for one that completed Save & Close (see Tier 1 #6 - if this warning appears after a real close, that's the verification-not-actually-finished bug, not a false positive)
- Google Sheets: FREE hub → silent, no attempt; PRO hub with no sheet bound → explicit "please specify" message; PRO with sheet → row actually appears

### 2.4 `/shareevent` full check chain
Full scenarios: [`docs/specs/commands/shareevent.md`](specs/commands/shareevent.md) (15 scenarios, S1-S15)

- Order matters here - test the FIRST failing condition actually stops execution (e.g. share-to-self should reject before ever reaching the bot-admin-in-target check)
- Alias resolution happens before the self-share check (share via an alias that happens to point at the hub itself → still correctly rejected as self-share)
- Anonymous caller succeeds (see Tier 1 #10); a genuinely-identifiable non-admin caller is still blocked

### 2.5 `/help` navigation and toggles
- Every command-with-flags section (`/newevent`+`/editevent` on the main screen, `/updateuser` in Users, `/shareevent` in Distribution, `/updatefeature`+`/allgroups` in the owner screen) has its own working expand/collapse.
- **Owner screen accordion specifically:** expand `/updatefeature`'s flags, then expand `/allgroups`'s flags without collapsing the first - confirm `/updatefeature`'s detail disappeared automatically (only one region expanded at a time, ever).
- Tier gating: on a FREE hub, Aliases/Monitoring/DM Access show a locked PRO badge and route to an upgrade prompt instead of the real section.

### 2.6 Data model integrity (spot-check, not exhaustive)
Full table: [`docs/specs/full_index.md`](specs/full_index.md) Data Model section

- Bot removed from a group → row disappears from `all_groups`, appears in `all_chats_bot_log` with both dates set
- `/refreshusers` on a chat with a stale, unresolvable tracked user (never clicked anything, not an admin) → gets removed, doesn't linger forever
- Google Sheets `Users` tab: a MEMBER→LEFT transition sets `DATE_end` and logs to `UserPresenceLog`; re-joining flips back to MEMBER and clears `DATE_end` - row is never deleted, only status-flipped

---

## Tier 3 - Exploratory / edge cases (lower priority, run when time allows)

- Multiple people administering the same hub, each running commands from their own separate DM at the same time (sticky-group selection is per-user, not shared)
- An event at EXACT capacity (headcount == limit) - the boundary condition, not comfortably-under or comfortably-over
- `/updatefeature` changing a feature's tier WHILE an event with that feature's `feature_snapshot` is still open - the running event should keep its frozen-at-creation behavior, not suddenly change mid-event
- Rapid double-clicking the same button (Going, then Going again immediately) - confirm no duplicate entries, no double-promotion

---

## What NOT to bother manually testing

Anything already covered by the 580 automated tests (`pytest tests/`) that doesn't touch REAL Telegram API behavior - pure data transformations, SQL migrations, text formatting. Manual testing time is better spent on the things automated tests structurally can't catch: real button-tap timing, how something actually LOOKS in the Telegram client, and cross-checking two live chat windows against each other.
