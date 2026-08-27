# Manual Testing Guide

This is a guide for testing the **live bot in real Telegram groups** —
not the automated `pytest` suite (that already covers 583+ scenarios on
every commit). Use this when verifying a deploy, hunting a
reported bug, or sanity-checking a big refactor before it goes out.

**How to mark results:** check items off in place, right in this file —
`- [ ] T1` → `- [x] T1 ✅ 27.08 v3.31.0`. Keeping the result next to the
scenario (instead of a separate tracker) means the test plan and its
last-known status never drift apart.

**Setup needed for a full pass:** one hub group (you as admin), one
child group/channel the event gets shared to, and a second Telegram
account (or a friend) to click things as a non-admin/other person —
most of the interesting bugs this session (waitlist promotion,
cross-chat clickability, DM group resolution) only show up once there
are genuinely 2+ people and 2+ chats involved, not just you clicking
alone in one group.

---

## Part 1 — Smoke Checklist (run before every deploy)

10–15 minutes, covers the highest-risk paths based on what's actually
broken in this project's history. Do this EVERY time, even for a
"small" change — several of the bugs found this session were in code
nobody thought the change touched.

- [ ] **S1.** `/newevent Test -limit 3` on a PRO hub → event posts with
  the right icons/limit, no errors.
- [ ] **S2.** Click **Going** as 3 different people → all 3 show up,
  headcount matches.
- [ ] **S3.** Click **Going** as a 4th person → they go to the
  **Waitlist** instead (event is full), with a visible confirmation.
- [ ] **S4.** Click **Not Going** as one of the original 3 → the 4th
  person (from the waitlist) gets **auto-promoted**, with a
  notification in their own chat.
- [ ] **S5.** Click **ALL** (drop all guests) for someone with 2+
  guests, while the waitlist has 2+ people queued → **all** freed
  slots get promoted, not just one.
- [ ] **S6.** `/shareevent <child_chat_alias>` → child chat gets the
  post, admin in hub gets a confirmation.
- [ ] **S7.** Click **Going** in the **child chat** → child's own post
  updates, **and** the hub's post updates too (cross-reference count).
- [ ] **S8.** Everyone from S6/S7 who's clickable in the child chat's
  own post is **also** clickable in the hub's "Going from `<chat>`"
  cross-reference section — same names, same linked/unlinked state in
  both places. *(See item 3 fix, v3.31.0 — this is exactly what broke
  before the edit-retry fix.)*
- [ ] **S9.** Click **Verify** → event enters review mode (Kick/Return/
  Add Extra Member/Save & Close buttons appear).
- [ ] **S10.** Click **Save & Close Event** → event closes, Google
  Sheets Events/EventUsers rows update (if a sheet is bound).
- [ ] **S11.** Run `/newevent` again in the same chat **immediately
  after S10** → **no** "there's already an active event" warning
  (previous one is genuinely closed, not stuck in verification).
- [ ] **S12.** `/status` and `/stats` run **directly in the hub group**
  → no group-name prefix (redundant there).
- [ ] **S13.** `/status` run from a **DM** (with a group already
  selected via `/switchgroup`) → reply is prefixed with `for <Group
  Name>` so it's obvious which group is being reported on.
- [ ] **S14.** `/help` → main screen is short (bare command syntax +
  "More about Flags"), tapping it expands the full `-newevent`/
  `-editevent` flag breakdown in place, tapping "Hide Flags" collapses
  it back to the exact original text.

---

## Part 2 — Full Checklist by Topic

Each topic below points to the detailed scenario file where the FULL
Given/When/Then breakdown lives (edge cases, exact expected values,
etc.) — this section is just an index + a place to log **when** each
area was last fully verified end-to-end, since re-reading and manually
executing all ~130 detailed scenarios every time isn't realistic.

### /newevent, /editevent
→ Full scenarios: `docs/specs/commands/newevent_editevent.md` (21 scenarios: N1–N21)
- [ ] Last full pass: _____________ (date, version, tester)
- Priority sub-areas if short on time: `-limit` lowering/raising +
  promotion (N12–N15), existing-active-event warning (N16), Google
  Sheets sync (N17–N19).

### /shareevent
→ Full scenarios: `docs/specs/commands/shareevent.md` (15 scenarios: S1–S15)
- [ ] Last full pass: _____________ (date, version, tester)
- Priority sub-areas: the 13-step check order (S1–S12) — these rarely
  get exercised end-to-end by a person just clicking around normally,
  since most days everything just works and nobody deliberately tries
  "share to a chat the bot isn't admin in" etc.
- **Also test:** anonymous admin ("Remain Anonymous" mode) can
  successfully `/shareevent` (v3.31.0 fix) — a regular, identifiable
  non-admin should still be blocked.

### -wl / -waitlist
→ Full scenarios: `docs/specs/flags/wl_waitlist.md` (13 scenarios: W1–W13)
- [ ] Last full pass: _____________ (date, version, tester)
- Priority: the hub-vs-child visibility split (W7–W8) — easy to miss
  since it only shows a difference once there's a genuine multi-chat
  setup with `visible` mode.

### -ngl / -notgoinglist
→ Full scenarios: `docs/specs/flags/ngl_notgoinglist.md` (9 scenarios: NG1–NG9)
- [ ] Last full pass: _____________ (date, version, tester)

### -clc / -clickability
→ Full scenarios: `docs/specs/flags/clc_clickability.md` (9 scenarios: C1–C9)
- [ ] Last full pass: _____________ (date, version, tester)
- **Known non-bug to remember:** a person with no resolvable
  Telegram user_id (never interacted with the bot, not an admin) will
  ALWAYS render as plain text regardless of `-clc on` — this is a
  Telegram platform limitation, not something `-clc` itself controls.
  Don't mistake this for the S8 cross-chat consistency bug above -
  they look similar but have different root causes.

### /help navigation and toggles
- [ ] Main screen's "More about Flags" / "Hide Flags" toggle round-trips
  to byte-identical text on collapse.
- [ ] Same toggle pattern works in **Users** section (`/updateuser`
  flags) and **Distribution** section (`/shareevent` flags).
- [ ] Owner screen (`/help -a`) accordion: expanding `/updatefeature`'s
  flags collapses `/allgroups`' flags if it was open, and vice versa -
  only one expanded at a time, ever.
- [ ] Non-owner tapping an owner-screen toggle gets a "this section is
  owner-only" alert, not a crash or silent no-op.

### DM / sticky-group behavior
- [ ] `/switchgroup` in a DM correctly lists every group you're an
  admin of, lets you pick one.
- [ ] Any hub command run from a DM (with a group selected) behaves
  **identically** to running it directly in that group - same tier,
  same data, same permissions (v3.31.0 added the group-name prefix to
  `/status`/`/stats` specifically to make this verifiable; check other
  commands too if you suspect a mismatch).

### Data model / Google Sheets
→ Full breakdown: `docs/specs/full_index.md` (Data Model section)
- [ ] Save & Close writes the right `AMOUNT` to the Events tab
  (combined hub + every child chat's headcount).
- [ ] Cancel writes `STATUS=CANCELED` and does **not** touch
  EventUsers at all.
- [ ] `/refreshusers` heals a previously-unresolvable user (e.g. one
  added via Add Extra Member) once they become an admin or click
  something themselves.

---

## Part 3 — Regression Watch List

Specific things that broke once already this session — worth an extra
double-check any time code near them changes, even if the smoke
checklist above already covers the general area.

| Area | What broke | Where the fix lives |
|---|---|---|
| Users sheet columns | Column order mismatch caused duplicate rows on every refresh | `sheets.py::sync_users_sheet` |
| `/shareevent` from a DM | Decorator was on the wrong function, broke the DM group-picker replay | `handlers.py::shareevent` |
| `/refreshusersall` on channels | `@username` stored raw instead of resolved numeric ID | `monitors.py::addmonitor` |
| `/help`'s PRO-lock check | Used a blunt tier check instead of the same per-feature check the button itself used | `help_system.py::help_callback_handler` |
| Stale `going_data` entries | A real click's valid user_id was discarded if the person already had an unresolvable placeholder entry | `event_engine.py` (going-click handler) |
| Master vs child post clickability | Master's `edit_message_text` could fail transiently and get permanently stuck stale while children updated fine | `event_engine.py::_edit_message_text_with_retry` |
