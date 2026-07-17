"""
Tests for utils.py
──────────────────
All functions here are pure (no I/O, no Telegram, no DB), so no mocking
or fixtures are needed — just call and assert.
"""

import re
import pytest
from utils import escape_markdown, now2ddmmyy, parse_event_date, DATE_FORMATS


# ---------------------------------------------------------------------------
# escape_markdown
# ---------------------------------------------------------------------------

class TestEscapeMarkdown:
    """MarkdownV2 requires escaping: _ * [ ] ( ) ~ ` > # + - = | { } . !"""

    SPECIAL_CHARS = list(r'_*[]()~`>#+-=|{}.!')

    def test_plain_text_unchanged(self):
        # Text with no special chars must come back identical
        assert escape_markdown("hello world") == "hello world"

    def test_single_special_chars_are_escaped(self):
        # Every special char gets a backslash prepended
        for ch in self.SPECIAL_CHARS:
            result = escape_markdown(ch)
            assert result == f"\\{ch}", f"Expected \\{ch!r}, got {result!r}"

    def test_multiple_special_chars(self):
        # A string that mixes plain and special characters
        result = escape_markdown("hello_world!")
        assert result == r"hello\_world\!"

    def test_parentheses_escaped(self):
        # Parentheses appear frequently in bot messages
        assert escape_markdown("(+3 g.)") == r"\(\+3 g\.\)"

    def test_numbers_and_letters_unchanged(self):
        assert escape_markdown("abc 123") == "abc 123"

    def test_empty_string(self):
        assert escape_markdown("") == ""

    def test_already_escaped_text(self):
        # If text already has backslashes they are NOT double-escaped
        # (backslash itself is not in the escape set, so it passes through)
        result = escape_markdown(r"\already")
        assert "\\" in result  # backslash preserved

    def test_non_string_input_coerced(self):
        # Function calls str() on input, so integers should work
        assert escape_markdown(42) == "42"

    def test_channel_name_with_quotes(self):
        # Real-world case: chat title containing quotes or underscores
        result = escape_markdown('My_Channel "name"')
        assert r"\_" in result          # underscore escaped
        # double-quote is not in the special set, so it passes through
        assert '"name"' in result


# ---------------------------------------------------------------------------
# now2ddmmyy
# ---------------------------------------------------------------------------

class TestNow2ddmmyy:
    FORMAT_RE = re.compile(
        r'^\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2}\.\d{3}$'
    )

    def test_returns_string(self):
        assert isinstance(now2ddmmyy(), str)

    def test_format_matches_pattern(self):
        # Expected: "DD.MM.YYYY HH:MM:SS.mmm"
        result = now2ddmmyy()
        assert self.FORMAT_RE.match(result), f"Unexpected format: {result!r}"

    def test_two_calls_are_close_in_time(self):
        # Both calls happen within the same second most of the time
        a = now2ddmmyy()
        b = now2ddmmyy()
        # They share the same date portion
        assert a[:10] == b[:10]


# ---------------------------------------------------------------------------
# parse_event_date
# ---------------------------------------------------------------------------

class TestParseEventDate:
    """Validates date parsing for the -date flag in /newevent and /editevent."""

    def test_date_only_valid(self):
        # dd.mm.yyyy format
        result = parse_event_date("14.07.2026")
        assert result == "14.07.2026"

    def test_date_and_time_valid(self):
        # dd.mm.yyyy HH:MM format
        result = parse_event_date("14.07.2026 19:00")
        assert result == "14.07.2026 19:00"

    def test_none_input_returns_none(self):
        assert parse_event_date(None) is None

    def test_empty_string_returns_none(self):
        assert parse_event_date("") is None

    def test_wrong_separator_returns_none(self):
        # Slash-separated dates are not supported
        assert parse_event_date("14/07/2026") is None

    def test_wrong_order_returns_none(self):
        # yyyy.mm.dd is not in DATE_FORMATS
        assert parse_event_date("2026.07.14") is None

    def test_invalid_day_returns_none(self):
        # Day 32 doesn't exist
        assert parse_event_date("32.07.2026") is None

    def test_invalid_month_returns_none(self):
        assert parse_event_date("14.13.2026") is None

    def test_normalises_whitespace(self):
        # Leading/trailing spaces should be stripped
        result = parse_event_date("  14.07.2026  ")
        assert result == "14.07.2026"

    def test_time_priority_over_date_only(self):
        # When both date+time are given, the full format is returned
        result = parse_event_date("01.01.2025 00:00")
        assert result == "01.01.2025 00:00"

    def test_all_supported_formats_covered(self):
        # DATE_FORMATS must contain exactly 2 entries
        assert len(DATE_FORMATS) == 2
