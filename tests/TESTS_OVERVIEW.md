# Test Suite Reference — what every test checks and why

**Total: 203 tests across 5 files.** Each test checks ONE small, specific
behavior rather than a whole command end-to-end — this is intentional: when
something breaks, the test name tells you exactly what broke, instead of a
vague "something in /newevent is wrong."

Rough breakdown: ~15 commands × ~10-15 scenarios each (success path, error
path, free vs premium, main chat vs child chat, edge cases) = a lot of small
tests, not a few giant ones.

---

## `test_utils.py` (23 tests) — pure helper functions, no DB/Telegram at all

### `TestEscapeMarkdown` (9 tests)
Checks `escape_markdown()` correctly backslash-escapes every MarkdownV2
reserved character (`_ * [ ] ( ) ~ \` > # + - = | { } . !`), leaves plain
text/numbers untouched, handles already-escaped text, non-string input, and
quotes.

### `TestNow2ddmmyy` (3 tests)
Checks the timestamp helper returns a string, matches the expected
`dd.mm.yyyy HH:MM:SS.mmm` pattern, and two calls a moment apart are close in
time (not wildly different).

### `TestParseEventDate` (11 tests)
Checks the `-date` flag parser: valid date-only, date+time, `None`/empty
input, wrong separator, wrong field order, invalid day/month, whitespace
normalization, and that every documented date format actually parses.

---

## `test_db.py` (30 tests) — schema creation and every migration path

### `TestInitDb` (13 tests)
Checks a fresh `init_db()` call creates every required table, that legacy
table names are gone, that `events` has the right columns (`event_date`,
`event_status`, `kicked_data`) and does NOT have the old `is_open`/
`is_cancelled` columns, that `main_group_users`/`main_chat_settings`/
`sub_groups` have their expected columns, that calling `init_db()` twice or
three times doesn't fail or duplicate data, and that a legacy `'frozen'`
status gets migrated to `'passive'`.

### `TestMigrationChatUsersRename` (1 test)
The very old `chat_users` table must rename to `main_group_users`,
preserving existing rows.

### `TestMigrationChatSettingsRename` (2 tests)
The old `chat_settings` (with `sheet_name`) must become `main_chat_settings`
(with `sheet_id` + subscription fields), and running the migration twice
must not break or duplicate anything.

### `TestMigrationSubGroupsMerge` (4 tests)
The old separate `chat_aliases` + `monitors` tables must merge into one
`sub_groups` table — checks an alias-only row, a monitor-only row, and
(critically) a chat that was **both** aliased and monitored merging into
ONE row instead of two, plus idempotency.

### `TestMigrationEventStatusRebuild` (3 tests)
The old `is_open`/`is_cancelled` pair must translate correctly into the new
single `event_status` column for every legacy combination (open,
verification, closed, canceled), while preserving all other event fields,
and idempotently.

### `TestTrackUser` (7 tests)
Checks `track_user()` inserts new users, updates existing status, ignores
empty usernames, stores/preserves `user_id`, handles the same username
across different chats independently, and defaults to `'active'` status.

---

## `test_handlers_pure.py` (48 tests) — pure functions, no mocking needed

### `TestParseEventArgs` (15 tests)
Every flag combination for `/newevent`/`/editevent`'s argument parser:
plain name, no args, `-gi`/`-goingicon` short and long forms (and that they
get stripped out of the event name), same for `-ni`/`-notgoingicon`,
`-date`/`-d` with and without time, flags before vs after the name, and all
flags combined at once.

### `TestParseUserArgs` (9 tests)
`@`-prefix stripping, comma-separated lists, space-separated lists, mixed
separators, multiple `@` prefixes, empty/whitespace-only input.

### `TestCreateEventKeyboard` (24 tests)
The single biggest pure-function test class — checks the exact button
layout for every `event_status` value:
- Closed/canceled → empty keyboard
- Open → Going/Not Going, ADD/Remove, Verification Mode, Cancel Event
  buttons (master only, not on child views)
- Verification mode → the two-row-per-person layout (name+Kick, then
  guest-count+minus+plus), Kick vs Return depending on status, child-chat
  rows using the `ch-` callback prefix, guest-only contributors getting
  just the count row (no name/Kick row), and the exact button wording
  ("Save & Close Event", not old wording)

---

## `test_markdown_safety.py` (1 test)

A static scan of `handlers.py` that fails if it finds ANY
`parse_mode="MarkdownV2"` message containing an unescaped `.` or `!`
outside a code span. This is the single test that has caught the most real
production crashes during development (`Can't parse entities`) — always
keep this one green.

---

## `test_handlers_async.py` (98 tests) — everything that touches Telegram + DB together

### `TestNewevent` (8 tests)
Event gets created in the DB, message gets sent to the right chat, the
keyboard sent is the OPEN state (not verification — this caught a real
regression once), missing name / invalid date reply with an error, and the
event_date is stored (or `NULL`) correctly.

### `TestEditevent` (5 tests)
Updates name, updates date only when the flag is given, leaves date alone
otherwise, replies with an error if there's no active event or an invalid
date.

### `TestNotify` (3 tests)
Pings only users who haven't responded yet, skips those who have, replies
with an error if there's no active event.

### `TestAdduser` (5 tests)
Admin gate, a numeric user_id only gets added if `getChatMember` confirms
they're *currently* in the chat (a regression test specifically covers
`status=left/kicked` - a valid, non-exception API response that must be
checked explicitly, not just "did the call succeed"), and a `@username` can
only be resolved via the chat's administrator list (the Bot API has no
username-lookup for `getChatMember` at all - a previous version tried
passing `username=` directly to it, which silently never worked).

### `TestUpdateuser` (7 tests)
The `-a`/`-active`/`-p`/`-passive` flag syntax, unknown flag error, missing
args error, `@`-prefix stripping.

### `TestListusers` (2 tests)
Shows all tracked users, replies "no users" for an empty chat.

### `TestAliasCommands` (4 tests)
Alias removal (existing/unknown/no-args), listing when empty.

### `TestShareevent` (5 tests)
Defaults to `-onlycount` when no mode is given, explicit `-v` overrides the
default, free tier blocks after 3 shares to the same target, premium has no
limit, and the limit is per-target (blocking target A doesn't block a
different target B).

### `TestPremiumGating` (8 tests)
Every alias/monitoring command (`/setalias`, `/removealias`, `/listalias`,
`/addmonitor`, `/removemonitor`, `/listmonitors`) is blocked on free tier
and allowed on premium.

### `TestIsPremium` (5 tests)
Every edge case for the `is_premium()` check: no row, `type='free'`, future
end date, past end date, `premium` type but a `NULL` end date.

### `TestSetsub` (4 tests)
Non-owner is silently ignored, owner can turn on/off, extending an active
subscription stacks on top instead of resetting.

### `TestHelpTierAwareKeyboard` (4 tests)
Free tier shows locked Aliases/Monitoring buttons, premium shows them
active, the row layout (Lifecycle+Distribution first, Aliases+Monitoring
second), and that Distribution/Lifecycle stay active for everyone
regardless of tier.

### `TestButtonHandlerUsername` (3 tests)
Username fallback logic: no `@username` → first name only, has username →
use it, has neither → fall back to the numeric user_id.

### `TestGoingFromLabel` (2 tests)
The "Going from" channel label has no stray quotes, and the verification
header says "SQUAD VERIFICATION."

### `TestRefreshusers` (15 tests)
The biggest command-level test class: non-admin rejection, a departed user
gets fully removed (not just marked passive — a real regression this
caught), a kicked user is also removed, a still-present user isn't touched,
users without an ID are skipped rather than removed, a new chat admin gets
auto-added, an already-tracked admin isn't duplicated, an empty chat still
gets a reply, and the whole `-r`/`-root` flag's interaction with the Google
Sheets "Users" tab (new member appended, username change archived with a
comma-joined history, departed user marked "Left," and — critically — rows
belonging to a *different* PLACE_ID are never touched).

### `TestButtonHandlerMasterHub` (3 tests)
Clicking Going adds the user, clicking Not Going doesn't reset their guest
count, Add Guest increments the counter — all in the main hub chat.

### `TestButtonHandlerCrossChatProtection` (3 tests)
Two different users who happen to share a display name are never confused
with each other, the same real user_id going in the master hub can't also
register from a child chat, and Not Going in a child chat preserves guests.

### `TestButtonHandlerChildGuestLogicMatchesMasterHub` (5 tests)
Child-chat guest logic must mirror the master hub exactly: Add Guest
doesn't mark the clicker as going, Not Going after Add Guest keeps the
guest, Sub Guest never touches going/not-going status, subtracting from
zero guests is a no-op, and a guest-only registration still shows in the
child view without counting as "going."

### `TestButtonHandlerSaveCloseEvent` (4 tests)
The Save & Close flow: a new Events sheet row gets the date in the right
column, an existing row's Closed-At/Status/Amount get updated in the right
columns, going users get exported from the main hub AND every child chat,
and a name added via "Add Extra Player" is included in that export.

### `TestButtonHandlerActionsLogNaming` (3 tests)
Guest +/- clicks during verification log as "ADD_EDITMODE"/"SUB_EDITMODE"
(not the raw button name), other actions log their plain uppercase name.

### `TestSharedLabelAndIcon` (1 test)
The child-chat broadcast message says "SHARED:" with the ↪️ icon.

### `TestGuestsFoldedIntoGoingList` (2 tests)
There's no separate "Guests:" section in the master view (folded into the
going list), but the child view still shows a guest line.

### `TestScheduleViewRefreshCoalescing` (2 tests)
The click-burst coalescer (batches rapid clicks into one broadcast instead
of one per click) actually runs the broadcast, and concurrent refreshes for
the same event collapse into one extra pass instead of stacking up.

---

## How to use this reference

Run `pytest -v` to see every test name print as it runs. If one fails, find
its name in this document to understand what behavior it's protecting —
that tells you whether the failure is a real regression in that specific
behavior, or (as happened once already) a sign you're testing an outdated
local copy of the code.
