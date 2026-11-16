"""handlers._send_report: the /refreshusersall report always arrives - split, retried, plain-text fallback."""
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest, RetryAfter, TimedOut

sys.path.insert(0, ".")
import handlers
from utils import escape_markdown


def upd(side_effect=None):
    u = MagicMock()
    u.message.reply_text = AsyncMock(side_effect=side_effect)
    return u


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(handlers.asyncio, "sleep", AsyncMock())


class TestSendReport:
    async def test_a_short_report_is_one_markdown_message(self):
        u = upd()
        await handlers._send_report(u, ["a", "b"])
        u.message.reply_text.assert_awaited_once_with("a\nb", parse_mode="MarkdownV2")

    async def test_a_long_report_is_split_under_the_cap_without_losing_a_line(self):
        lines = [f"line {i} " + "x" * 90 for i in range(100)]
        u = upd()
        await handlers._send_report(u, lines)
        texts = [c.args[0] for c in u.message.reply_text.call_args_list]
        assert len(texts) > 1 and all(len(t) <= 4096 for t in texts)
        assert "\n".join(texts).split("\n") == lines

    async def test_a_timeout_or_flood_wait_is_retried(self):
        u = upd([TimedOut(), RetryAfter(1), None])
        await handlers._send_report(u, ["ok"])
        assert u.message.reply_text.await_count == 3

    async def test_text_telegram_cannot_parse_is_sent_as_plain_text(self):
        u = upd([BadRequest("can't parse entities"), None])
        await handlers._send_report(u, ["  ✅ Synced: `a\\.b` \\(\\+1\\)"])
        assert u.message.reply_text.call_args.args == ("  ✅ Synced: a.b (+1)",)
        assert "parse_mode" not in u.message.reply_text.call_args.kwargs

    async def test_total_failure_is_swallowed_not_raised(self):
        u = upd(TimedOut())
        await handlers._send_report(u, ["x"])          # must not raise: the sync work is already done


def test_a_backslash_in_a_chat_name_is_escaped():
    assert escape_markdown("a\\b") == "a\\\\b"
