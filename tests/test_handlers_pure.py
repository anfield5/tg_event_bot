"""
Tests for pure (non-async, no-I/O) functions in handlers.py
─────────────────────────────────────────────────────────────
Covered:
  * parse_event_args  — flag parsing for /newevent and /editevent
  * parse_user_args   — @-stripping and comma/space splitting
  * create_event_keyboard — inline keyboard shape for every event_status state

No mocking needed here; these functions have zero side-effects.
"""

import pytest
from handlers import parse_event_args, parse_user_args, create_event_keyboard
from telegram import InlineKeyboardMarkup


# ---------------------------------------------------------------------------
# parse_event_args
# ---------------------------------------------------------------------------

class TestParseEventArgs:
    """Exercises every flag combination for parse_event_args()."""

    # ── Basic name parsing ────────────────────────────────────────────────

    def test_plain_name_no_flags(self):
        name, gi, ni, date = parse_event_args(["World", "Cup", "Final"])
        assert name == "World Cup Final"
        assert gi is None
        assert ni is None
        assert date is None

    def test_single_word_name(self):
        name, *_ = parse_event_args(["Match"])
        assert name == "Match"

    def test_empty_args_returns_none_name(self):
        name, gi, ni, date = parse_event_args([])
        assert name is None

    # ── Going icon flag ───────────────────────────────────────────────────

    def test_gi_flag_short(self):
        _, gi, _, _ = parse_event_args(["-gi", "⚽", "Party"])
        assert gi == "⚽"

    def test_goingicon_flag_long(self):
        _, gi, _, _ = parse_event_args(["-goingicon", "⚽", "Party"])
        assert gi == "⚽"

    def test_gi_flag_strips_from_name(self):
        # '-gi ⚽' must NOT appear in the event name
        name, gi, _, _ = parse_event_args(["Party", "-gi", "⚽"])
        assert name == "Party"
        assert gi == "⚽"

    # ── Not-going icon flag ───────────────────────────────────────────────

    def test_ni_flag_short(self):
        _, _, ni, _ = parse_event_args(["-ni", "❎", "Party"])
        assert ni == "❎"

    def test_notgoingicon_flag_long(self):
        _, _, ni, _ = parse_event_args(["-notgoingicon", "❎", "Party"])
        assert ni == "❎"

    # ── Date flag ─────────────────────────────────────────────────────────

    def test_date_flag_date_only(self):
        # -date 14.07.2026  → single token consumed
        _, _, _, date = parse_event_args(["Party", "-date", "14.07.2026"])
        assert date == "14.07.2026"

    def test_date_flag_with_time(self):
        # -date 14.07.2026 19:00  → two tokens consumed (time matches HH:MM)
        _, _, _, date = parse_event_args(["Party", "-date", "14.07.2026", "19:00"])
        assert date == "14.07.2026 19:00"

    def test_d_flag_short(self):
        _, _, _, date = parse_event_args(["-d", "01.01.2025", "Event"])
        assert date == "01.01.2025"

    def test_date_with_time_not_consumed_if_no_time(self):
        # Next token is NOT a time → only one token consumed
        name, _, _, date = parse_event_args(["Party", "-date", "14.07.2026", "stuff"])
        assert date == "14.07.2026"
        assert name == "Party stuff"

    def test_date_absent_returns_none(self):
        _, _, _, date = parse_event_args(["Party"])
        assert date is None

    # ── All flags combined ────────────────────────────────────────────────

    def test_all_flags_combined(self):
        args = ["Big", "Event", "-gi", "⚽", "-ni", "❎", "-date", "14.07.2026", "19:00"]
        name, gi, ni, date = parse_event_args(args)
        assert name == "Big Event"
        assert gi   == "⚽"
        assert ni   == "❎"
        assert date == "14.07.2026 19:00"

    def test_flags_before_name(self):
        # Flags can appear anywhere in the token list
        name, gi, ni, date = parse_event_args(["-gi", "⚽", "Party", "Night"])
        assert name == "Party Night"
        assert gi   == "⚽"


# ---------------------------------------------------------------------------
# parse_user_args
# ---------------------------------------------------------------------------

class TestParseUserArgs:
    """Exercises @-stripping, comma splitting, and edge cases."""

    def test_simple_username(self):
        assert parse_user_args(["alice"]) == ["alice"]

    def test_at_prefix_stripped(self):
        assert parse_user_args(["@alice"]) == ["alice"]

    def test_multiple_usernames_spaces(self):
        result = parse_user_args(["alice", "bob"])
        assert result == ["alice", "bob"]

    def test_comma_separated(self):
        result = parse_user_args(["alice,bob,carol"])
        assert result == ["alice", "bob", "carol"]

    def test_mixed_separators(self):
        result = parse_user_args(["alice,", "bob", "carol"])
        assert "alice" in result
        assert "bob"   in result
        assert "carol" in result

    def test_multiple_at_prefixes(self):
        result = parse_user_args(["@alice", "@bob"])
        assert result == ["alice", "bob"]

    def test_empty_args(self):
        assert parse_user_args([]) == []

    def test_whitespace_only_ignored(self):
        result = parse_user_args(["  ", "alice"])
        assert result == ["alice"]

    def test_strips_extra_spaces(self):
        result = parse_user_args(["  alice  "])
        assert "alice" in result


# ---------------------------------------------------------------------------
# create_event_keyboard
# ---------------------------------------------------------------------------

class TestCreateEventKeyboard:
    """Verifies inline keyboard structure for every event_status value."""

    EVENT_ID      = "abc12345"
    GOING_ICON    = "✅"
    NOT_GOING_ICN = "❌"

    # ── event_status == 2 (closed) ────────────────────────────────────────

    def test_closed_event_returns_empty_keyboard(self):
        kb = create_event_keyboard(
            self.EVENT_ID, 2, self.GOING_ICON, self.NOT_GOING_ICN
        )
        assert isinstance(kb, InlineKeyboardMarkup)
        # Different ptb versions return [] or () for an empty keyboard
        assert len(kb.inline_keyboard) == 0

    # ── event_status == -1 (canceled) ─────────────────────────────────────

    def test_canceled_event_returns_empty_keyboard(self):
        """A canceled event must show no buttons at all, same as a closed one."""
        kb = create_event_keyboard(
            self.EVENT_ID, -1, self.GOING_ICON, self.NOT_GOING_ICN
        )
        assert isinstance(kb, InlineKeyboardMarkup)
        assert len(kb.inline_keyboard) == 0

    # ── event_status == 0 (open voting) ───────────────────────────────────

    def test_open_event_has_going_notgoing_row(self):
        kb = create_event_keyboard(
            self.EVENT_ID, 0, self.GOING_ICON, self.NOT_GOING_ICN
        )
        rows = kb.inline_keyboard
        # First row: Going + Not Going
        assert any("Going" in btn.text for row in rows for btn in row)
        assert any("Not Going" in btn.text for row in rows for btn in row)

    def test_open_event_has_add_remove_guest_row(self):
        """Buttons are labeled plain 'ADD'/'Remove', not 'Add Guest'/'Sub Guest'."""
        kb   = create_event_keyboard(self.EVENT_ID, 0, self.GOING_ICON, self.NOT_GOING_ICN)
        flat = [btn for row in kb.inline_keyboard for btn in row]
        texts = [b.text for b in flat]
        assert any("ADD" in t for t in texts)
        assert any("Remove" in t for t in texts)

    def test_open_event_master_has_verification_button(self):
        # Non-child view must show a "Verify&Close" button
        kb    = create_event_keyboard(self.EVENT_ID, 0, self.GOING_ICON, self.NOT_GOING_ICN, is_child=False)
        flat  = [btn for row in kb.inline_keyboard for btn in row]
        assert any("Verify&Close" in b.text for b in flat)

    def test_open_event_master_has_cancel_event_button(self):
        kb    = create_event_keyboard(self.EVENT_ID, 0, self.GOING_ICON, self.NOT_GOING_ICN, is_child=False)
        flat  = [btn for row in kb.inline_keyboard for btn in row]
        assert any(b.callback_data == f"cancel_{self.EVENT_ID}" for b in flat)

    def test_verify_and_cancel_share_a_row_verify_first(self):
        # Verify&Close and Cancel Event sit on the SAME row - Verify&Close
        # on the left, Cancel Event on the right.
        kb   = create_event_keyboard(self.EVENT_ID, 0, self.GOING_ICON, self.NOT_GOING_ICN, is_child=False)
        row  = next(r for r in kb.inline_keyboard if any("Verify&Close" in b.text for b in r))
        assert len(row) == 2
        assert "Verify&Close" in row[0].text
        assert row[1].callback_data == f"cancel_{self.EVENT_ID}"

    def test_open_event_child_has_no_verification_button(self):
        # Child views must NOT show the close/verification button
        kb   = create_event_keyboard(self.EVENT_ID, 0, self.GOING_ICON, self.NOT_GOING_ICN, is_child=True)
        flat = [btn for row in kb.inline_keyboard for btn in row]
        assert not any("Verify&Close" in b.text for b in flat)

    def test_open_event_child_has_no_cancel_event_button(self):
        kb   = create_event_keyboard(self.EVENT_ID, 0, self.GOING_ICON, self.NOT_GOING_ICN, is_child=True)
        flat = [btn for row in kb.inline_keyboard for btn in row]
        assert not any(b.callback_data == f"cancel_{self.EVENT_ID}" for b in flat)

    def test_going_callback_format(self):
        kb   = create_event_keyboard(self.EVENT_ID, 0, self.GOING_ICON, self.NOT_GOING_ICN)
        flat = [btn for row in kb.inline_keyboard for btn in row]
        assert any(b.callback_data == f"going_{self.EVENT_ID}" for b in flat)

    def test_notgoing_callback_format(self):
        kb   = create_event_keyboard(self.EVENT_ID, 0, self.GOING_ICON, self.NOT_GOING_ICN)
        flat = [btn for row in kb.inline_keyboard for btn in row]
        assert any(b.callback_data == f"notgoing_{self.EVENT_ID}" for b in flat)

    # ── event_status == 1 (verification mode) ─────────────────────────────

    def test_verification_has_save_close_event_button(self):
        kb   = create_event_keyboard(self.EVENT_ID, 1, self.GOING_ICON, self.NOT_GOING_ICN)
        flat = [btn for row in kb.inline_keyboard for btn in row]
        assert any("Save & Close Event" in b.text for b in flat)

    def test_verification_has_add_extra_player_button(self):
        kb   = create_event_keyboard(self.EVENT_ID, 1, self.GOING_ICON, self.NOT_GOING_ICN)
        flat = [btn for row in kb.inline_keyboard for btn in row]
        assert any("Add Extra Member" in b.text for b in flat)

    def test_verification_participant_uses_two_rows(self):
        """
        Each master participant must appear over exactly TWO consecutive rows:
          Row A: [👤 name]   [❌ Kick]
          Row B: [NG: name]  [−]  [+]
        """
        going_list = ["alice (111)"]
        counters   = {"alice": 2}
        kb         = create_event_keyboard(
            self.EVENT_ID, 1, self.GOING_ICON, self.NOT_GOING_ICN,
            going_list=going_list, counters=counters,
        )
        rows = kb.inline_keyboard

        # Find the row that contains 'alice'
        alice_row_idx = None
        for i, row in enumerate(rows):
            if any("alice" in btn.text for btn in row):
                alice_row_idx = i
                break
        assert alice_row_idx is not None, "alice must appear in a keyboard row"

        # Row A: name + Kick
        row_a = rows[alice_row_idx]
        assert any("alice" in b.text for b in row_a)
        assert any("Kick"  in b.text for b in row_a)
        assert len(row_a) == 2, "Row A must have exactly 2 buttons (name + kick)"

        # Row B: guest count + − + +  (immediately follows row A)
        row_b = rows[alice_row_idx + 1]
        assert len(row_b) == 3, "Row B must have exactly 3 buttons (count / minus / plus)"
        assert any("alice" in b.text for b in row_b), "guest label must mention who the guests belong to"
        # minus appears before plus (reversed order requested)
        texts = [b.text for b in row_b]
        minus_idx = next((i for i, t in enumerate(texts) if "-" in t), None)
        plus_idx  = next((i for i, t in enumerate(texts) if "+" in t), None)
        assert minus_idx is not None, "minus button missing from row B"
        assert plus_idx  is not None, "plus button missing from row B"
        assert minus_idx < plus_idx,  "minus must appear before plus (reversed order)"

    def test_verification_inc_dec_buttons_have_no_stray_icons(self):
        """
        The guest +/- buttons are plain ' - ' / ' + ' text (no emoji, no
        colored-dot prefix) - Telegram's Bot API has no way to recolor
        button text, so there's nothing further to force here.
        """
        going_list = ["alice (111)"]
        kb         = create_event_keyboard(
            self.EVENT_ID, 1, self.GOING_ICON, self.NOT_GOING_ICN,
            going_list=going_list, counters={},
        )
        flat = [btn for row in kb.inline_keyboard for btn in row]
        assert not any("🟠" in b.text for b in flat), "orange-dot prefix must be gone"
        assert not any("➖" in b.text or "➕" in b.text for b in flat), "must not use the emoji +/- glyphs"
        minus_btns = [b for b in flat if b.text == " - "]
        plus_btns  = [b for b in flat if b.text == " + "]
        assert minus_btns, "the ' - ' button must exist"
        assert plus_btns,  "the ' + ' button must exist"

    def test_verification_kick_callback_format(self):
        going_list = ["alice (111)"]
        kb         = create_event_keyboard(
            self.EVENT_ID, 1, self.GOING_ICON, self.NOT_GOING_ICN,
            going_list=going_list, counters={},
        )
        flat = [btn for row in kb.inline_keyboard for btn in row]
        assert any(b.callback_data == f"kick_{self.EVENT_ID}:alice" for b in flat)

    def test_verification_return_button_for_kicked_user(self):
        """A kicked (but not currently going) user gets a Return button, not Kick."""
        kb   = create_event_keyboard(
            self.EVENT_ID, 1, self.GOING_ICON, self.NOT_GOING_ICN,
            going_list=[], counters={}, kicked_users={"alice"},
        )
        flat = [btn for row in kb.inline_keyboard for btn in row]
        assert any(b.callback_data == f"return_{self.EVENT_ID}:alice" for b in flat)
        assert not any(b.callback_data == f"kick_{self.EVENT_ID}:alice" for b in flat)

    def test_verification_incgst_callback_format(self):
        going_list = ["alice (111)"]
        kb         = create_event_keyboard(
            self.EVENT_ID, 1, self.GOING_ICON, self.NOT_GOING_ICN,
            going_list=going_list, counters={},
        )
        flat = [btn for row in kb.inline_keyboard for btn in row]
        assert any(b.callback_data == f"incgst_{self.EVENT_ID}:alice" for b in flat)

    def test_verification_decgst_callback_format(self):
        going_list = ["alice (111)"]
        kb         = create_event_keyboard(
            self.EVENT_ID, 1, self.GOING_ICON, self.NOT_GOING_ICN,
            going_list=going_list, counters={},
        )
        flat = [btn for row in kb.inline_keyboard for btn in row]
        assert any(b.callback_data == f"decgst_{self.EVENT_ID}:alice" for b in flat)

    def test_verification_child_participants_use_ch_prefix(self):
        """
        Child-chat participants must use 'ch-<username>' callbacks.
        child_users_rows is a list of (username, guests, status) 3-tuples -
        status='going' here so this renders a Kick (not Return) button.
        """
        child_rows = [("anreon", 3, "going")]
        kb         = create_event_keyboard(
            self.EVENT_ID, 1, self.GOING_ICON, self.NOT_GOING_ICN,
            going_list=[], counters={}, child_users_rows=child_rows,
        )
        flat = [btn for row in kb.inline_keyboard for btn in row]
        assert any(b.callback_data == f"kick_{self.EVENT_ID}:ch-anreon"   for b in flat)
        assert any(b.callback_data == f"incgst_{self.EVENT_ID}:ch-anreon" for b in flat)
        assert any(b.callback_data == f"decgst_{self.EVENT_ID}:ch-anreon" for b in flat)

    def test_verification_child_kicked_status_uses_return(self):
        """status='kicked' on a child row must render Return, not Kick."""
        child_rows = [("anreon", 3, "kicked")]
        kb         = create_event_keyboard(
            self.EVENT_ID, 1, self.GOING_ICON, self.NOT_GOING_ICN,
            going_list=[], counters={}, child_users_rows=child_rows,
        )
        flat = [btn for row in kb.inline_keyboard for btn in row]
        assert any(b.callback_data == f"return_{self.EVENT_ID}:ch-anreon" for b in flat)
        assert not any(b.callback_data == f"kick_{self.EVENT_ID}:ch-anreon" for b in flat)

    def test_verification_child_also_uses_two_rows(self):
        """Child participants must also get the two-row layout."""
        child_rows = [("anreon", 3, "going")]
        kb         = create_event_keyboard(
            self.EVENT_ID, 1, self.GOING_ICON, self.NOT_GOING_ICN,
            going_list=[], counters={}, child_users_rows=child_rows,
        )
        rows = kb.inline_keyboard
        # Find child-participant row
        child_row_idx = None
        for i, row in enumerate(rows):
            if any("anreon" in btn.text for btn in row):
                child_row_idx = i
                break
        assert child_row_idx is not None
        # Row A: 2 buttons, Row B: 3 buttons
        assert len(rows[child_row_idx])     == 2
        assert len(rows[child_row_idx + 1]) == 3

    def test_verification_is_child_returns_empty(self):
        # Child views in event_status==1 get an empty keyboard (no verification UI)
        kb = create_event_keyboard(
            self.EVENT_ID, 1, self.GOING_ICON, self.NOT_GOING_ICN, is_child=True
        )
        assert len(kb.inline_keyboard) == 0

    def test_save_button_says_save_and_close_event(self):
        """Exact wording check — must NOT say 'Save & Lock Roster' anymore."""
        kb   = create_event_keyboard(self.EVENT_ID, 1, self.GOING_ICON, self.NOT_GOING_ICN)
        flat = [btn for row in kb.inline_keyboard for btn in row]
        save_btns = [b for b in flat if b.callback_data == f"save_{self.EVENT_ID}"]
        assert save_btns, "save button must exist"
        assert save_btns[0].text == "💾 Save & Close Event"
        # Old text must be absent
        assert not any("Lock Roster" in b.text for b in flat)

    def test_guest_only_contributor_gets_no_name_row(self):
        """
        Someone who only ever clicked Add Guest (never Going, never Kicked)
        must NOT get a name/Kick row - only the guest count row shows, since
        there's no "membership" for an admin to act on.
        """
        kb   = create_event_keyboard(
            self.EVENT_ID, 1, self.GOING_ICON, self.NOT_GOING_ICN,
            going_list=[], counters={"bob": 2}, kicked_users=set(),
        )
        rows = kb.inline_keyboard
        assert not any("👤 bob" in btn.text for row in rows for btn in row)
        assert any("bob" in btn.text for row in rows for btn in row), "the guest count row must still show"


class TestCreateEventKeyboardFeatureSnapshot:
    """
    verification_enabled/add_extra_member_enabled - per-event feature
    gating, driven by the event's own stored feature_snapshot rather than
    the hub's live tier (see subscription.has_feature and the
    feature_snapshot column on events).
    """

    EVENT_ID = "ev1"
    GOING_ICON = "👍"
    NOT_GOING_ICN = "❌"

    def test_verification_disabled_shows_save_and_close_not_verify(self):
        kb = create_event_keyboard(
            self.EVENT_ID, 0, self.GOING_ICON, self.NOT_GOING_ICN,
            is_child=False, verification_enabled=False,
        )
        flat = [btn for row in kb.inline_keyboard for btn in row]
        assert not any("Verify&Close" in b.text for b in flat)
        assert any("Save&Close" in b.text for b in flat)

    def test_verification_disabled_uses_directclose_callback(self):
        kb = create_event_keyboard(
            self.EVENT_ID, 0, self.GOING_ICON, self.NOT_GOING_ICN,
            is_child=False, verification_enabled=False,
        )
        flat = [btn for row in kb.inline_keyboard for btn in row]
        save_btn = next(b for b in flat if "Save&Close" in b.text)
        assert save_btn.callback_data == f"directclose_{self.EVENT_ID}"

    def test_verification_enabled_keeps_current_behavior(self):
        kb = create_event_keyboard(
            self.EVENT_ID, 0, self.GOING_ICON, self.NOT_GOING_ICN,
            is_child=False, verification_enabled=True,
        )
        flat = [btn for row in kb.inline_keyboard for btn in row]
        close_btn = next(b for b in flat if "Verify&Close" in b.text)
        assert close_btn.callback_data == f"close_{self.EVENT_ID}"

    def test_add_extra_member_disabled_hides_the_button(self):
        kb = create_event_keyboard(
            self.EVENT_ID, 1, self.GOING_ICON, self.NOT_GOING_ICN,
            going_list=[], counters={}, kicked_users=set(),
            add_extra_member_enabled=False,
        )
        flat = [btn for row in kb.inline_keyboard for btn in row]
        assert not any("Add Extra Member" in b.text for b in flat)
        # Save & Close Event must still be there - only the one button is gated
        assert any("Save & Close Event" in b.text for b in flat)

    def test_add_extra_member_enabled_keeps_current_behavior(self):
        kb = create_event_keyboard(
            self.EVENT_ID, 1, self.GOING_ICON, self.NOT_GOING_ICN,
            going_list=[], counters={}, kicked_users=set(),
            add_extra_member_enabled=True,
        )
        flat = [btn for row in kb.inline_keyboard for btn in row]
        assert any("Add Extra Member" in b.text for b in flat)
