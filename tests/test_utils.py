"""
Tests for utils.py
──────────────────
All functions here are pure (no I/O, no Telegram, no DB), so no mocking
or fixtures are needed — just call and assert.
"""

import re
import pytest
from utils import escape_markdown, now2ddmmyy, parse_event_date, DATE_FORMATS, require_dm_only, COMMAND_DESTINATION_TYPE, get_admin_contact


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


class TestCommandDestinationClassification:
    """Direct unit tests for the command destination-type classification
    (COMMAND_DESTINATION_TYPE) and its Type 3 enforcement helper
    (require_dm_only), previously only covered indirectly through each
    individual command's own tests."""

    def test_type_2_commands_are_exactly_these_four(self):
        type_2 = {k for k, v in COMMAND_DESTINATION_TYPE.items() if v == 2}
        assert type_2 == {"newevent", "editevent", "shareevent", "notify"}

    def test_type_3_commands_are_exactly_these_eight(self):
        type_3 = {k for k, v in COMMAND_DESTINATION_TYPE.items() if v == 3}
        assert type_3 == {
            "switchgroup", "start", "lockbot", "allgroups",
            "allchannels", "updatefeature", "setsub", "setsheet",
        }

    def test_no_command_is_both_type_2_and_type_3(self):
        type_2 = {k for k, v in COMMAND_DESTINATION_TYPE.items() if v == 2}
        type_3 = {k for k, v in COMMAND_DESTINATION_TYPE.items() if v == 3}
        assert type_2.isdisjoint(type_3)

    @pytest.mark.asyncio
    async def test_require_dm_only_allows_private_chat(self):
        from unittest.mock import MagicMock, AsyncMock
        update = MagicMock()
        update.effective_chat.type = "private"
        update.message.reply_text = AsyncMock()

        result = await require_dm_only(update, "somecommand")

        assert result is True
        assert not update.message.reply_text.called

    @pytest.mark.asyncio
    async def test_require_dm_only_rejects_group_with_explicit_error(self):
        from unittest.mock import MagicMock, AsyncMock
        update = MagicMock()
        update.effective_chat.type = "supergroup"
        update.message.reply_text = AsyncMock()

        result = await require_dm_only(update, "somecommand")

        assert result is False
        update.message.reply_text.assert_awaited_once()
        text = update.message.reply_text.call_args.args[0]
        assert "/somecommand" in text
        assert "only works in a DM" in text

    @pytest.mark.asyncio
    async def test_require_dm_only_rejects_channel_too(self):
        """A channel post isn't a DM either - must be rejected the same
        way as a group."""
        from unittest.mock import MagicMock, AsyncMock
        update = MagicMock()
        update.effective_chat.type = "channel"
        update.message.reply_text = AsyncMock()

        result = await require_dm_only(update, "somecommand")

        assert result is False


class TestCommandDestinationTypeMatchesRealCode:
    """Drift-detection test, matching the pattern already used for
    flag_registry.py's gating verification (test_flag_registry.py):
    parses each Type 3 command's actual source function and confirms
    it genuinely calls require_dm_only() somewhere - catches a future
    command being added to COMMAND_DESTINATION_TYPE without the
    enforcement actually being wired up, or the reverse (enforcement
    removed from code without updating the dict)."""

    # (function name, source file, source file's own dispatcher name for the command key)
    TYPE_3_FUNCTIONS = {
        "switchgroup": ("switchgroup_command", "hub_resolver.py"),
        "start": ("start_command", "hub_resolver.py"),
        "lockbot": ("lockbot", "subscription.py"),
        "allgroups": ("allgroups_command", "subscription.py"),
        "allchannels": ("allchannels_command", "subscription.py"),
        "updatefeature": ("updatefeature", "subscription.py"),
        "setsub": ("setsub", "subscription.py"),
        "setsheet": ("setsheet", "subscription.py"),
    }

    def _get_function_source(self, filename, function_name):
        import re
        with open(filename) as f:
            content = f.read()
        # Match from "async def <name>(" to the next top-level "async def "
        # (a function starting at column 0), or end of file.
        pattern = rf"async def {function_name}\(.*?(?=\nasync def |\Z)"
        match = re.search(pattern, content, re.DOTALL)
        assert match, f"Could not find function {function_name} in {filename}"
        return match.group(0)

    def test_every_type_3_command_in_the_dict_has_a_known_function(self):
        type_3 = {k for k, v in COMMAND_DESTINATION_TYPE.items() if v == 3}
        assert type_3 == set(self.TYPE_3_FUNCTIONS.keys()), \
            "TYPE_3_FUNCTIONS in this test must be kept in sync with COMMAND_DESTINATION_TYPE"

    @pytest.mark.parametrize("command_key", [
        "switchgroup", "start", "lockbot", "allgroups",
        "allchannels", "updatefeature", "setsub", "setsheet",
    ])
    def test_type_3_command_actually_calls_require_dm_only(self, command_key):
        function_name, filename = self.TYPE_3_FUNCTIONS[command_key]
        source = self._get_function_source(filename, function_name)
        assert "require_dm_only(" in source, (
            f"/{command_key} is listed as Type 3 (DM-only) in COMMAND_DESTINATION_TYPE, "
            f"but its function {function_name}() in {filename} never calls require_dm_only()"
        )


class TestGetAdminContact:
    """The single shared source of the bot owner's contact info, used
    by both the premium-upgrade flow and /lockbot's non-owner
    notification."""

    def test_returns_a_label_and_url_tuple(self):
        result = get_admin_contact()
        assert isinstance(result, tuple)
        assert len(result) == 2
        label, url = result
        assert isinstance(label, str) and isinstance(url, str)
        assert url.startswith("https://t.me/")
