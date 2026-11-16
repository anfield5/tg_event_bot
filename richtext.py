"""
Native collapsible lists - Telegram "Rich Messages" (Bot API 10.x), used while /hideability is ON for a hub.

A post is still BUILT as MarkdownV2 text by event_engine, exactly as always. While the hub has /hideability on, each
foldable list is wrapped by `fold()` in invisible marker characters; this module then

  * turns the whole text into the HTML a rich message takes - each marked list becomes a
    <details [open]><summary>header</summary>names</details> block, the viewer folds / unfolds it with a tap, on
    their own device (the bot is not told, and no button is involved);
  * sends / edits it with sendRichMessage / editMessageText(rich_message=...), which python-telegram-bot 20.3 has no
    method for, so the raw Bot API call (`bot._post`, the same path every other call takes - retries, error mapping)
    is used;
  * can take the markers out again (`unfold`) so the same text goes out as an ordinary MarkdownV2 message - the
    fallback whenever Telegram refuses a rich message (an old server, a chat type that cannot have one, a post over
    the limits). A failed rich message never costs a post: it is posted unfolded instead.

With /hideability OFF none of this is used: no markers, a plain MarkdownV2 post, byte for byte as before.
"""
import html
import json
import re

from config import logger

# Private-use characters: they never occur in a name or in the text the bot writes.
_OPEN, _CLOSED, _SEP, _BODY, _END = "", "", "", "", ""
_MARKERS = re.compile("[-]")
_FOLD = re.compile(f"([{_OPEN}{_CLOSED}])(.*?){_SEP}(.*?){_BODY}(.*?){_END}", re.DOTALL)

# Rich message limits (Bot API): 32768 characters of source, 500 blocks counting nested ones.
MAX_SOURCE_CHARS = 32768
MAX_NEWLINES = 450       # a line break is at most one block; stay clear of the 500 limit

# Which lists start unfolded for the viewer: Going shown, Not Going and Waitlist folded.
DEFAULT_OPEN = {"going": True, "notgoing": False, "waitlist": False}


def fold(header: str, body: str, is_open: bool, sep: str = ":\n") -> str:
    """One foldable list: `header` always shown, `body` (the names) revealed on a tap. `sep` is what joins them when the
    markers are taken out again (unfold)."""
    return f"{_OPEN if is_open else _CLOSED}{header}{_SEP}{sep}{_BODY}{body}{_END}"


def has_folds(text: str) -> bool:
    text = text or ""
    return _OPEN in text or _CLOSED in text


def unfold(text: str) -> str:
    """The text as the ordinary MarkdownV2 post: every list in full, header then names - what OFF renders."""
    return _FOLD.sub(lambda m: f"{m.group(2)}{m.group(3)}{m.group(4)}", text)


def _escape(chunk: str) -> str:
    return html.escape(chunk, quote=False)


def md2_to_html(text: str) -> str:
    """
    The subset of MarkdownV2 the bot's posts use -> rich-message HTML: *bold*, _italic_, __underline__, ~strike~,
    `code`, [text](url), \\-escaped characters, line breaks. Everything else is plain text.
    """
    out, stack, i, n = [], [], 0, len(text)
    tags = {"*": "b", "_": "i", "~": "s", "`": "code"}

    def toggle(tag):
        if stack and stack[-1] == tag:
            out.append(f"</{stack.pop()}>")
        else:
            stack.append(tag)
            out.append(f"<{tag}>")

    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            out.append(_escape(text[i + 1]))
            i += 2
        elif c == "_" and text.startswith("__", i):
            toggle("u")
            i += 2
        elif c in tags:
            toggle(tags[c])
            i += 1
        elif c == "[":
            close = text.find("](", i)
            end = text.find(")", close + 2) if close != -1 else -1
            if close == -1 or end == -1:
                out.append(_escape(c))
                i += 1
            else:
                label = md2_to_html(text[i + 1:close])
                url = html.escape(text[close + 2:end], quote=True)
                out.append(f'<a href="{url}">{label}</a>')
                i = end + 1
        elif c == "\n":
            out.append("<br>")
            i += 1
        else:
            out.append(_escape(c))
            i += 1
    out.extend(f"</{tag}>" for tag in reversed(stack))      # a stray marker never leaves a tag open
    return "".join(out)


def to_rich_html(text: str) -> str:
    """The marked text -> the HTML of the rich message (see the module docstring)."""
    parts, last = [], 0
    for m in _FOLD.finditer(text):
        parts.append(md2_to_html(text[last:m.start()]))
        opened = " open" if m.group(1) == _OPEN else ""
        parts.append(f"<details{opened}><summary>{md2_to_html(m.group(2))}</summary>{md2_to_html(m.group(4))}</details>")
        last = m.end()
    parts.append(md2_to_html(_MARKERS.sub("", text[last:])))
    return "".join(parts)


def fits(text: str) -> bool:
    """Can this post go out as one rich message at all (its limits)?"""
    return len(text) < MAX_SOURCE_CHARS // 2 and text.count("\n") < MAX_NEWLINES


def _markup(reply_markup):
    return reply_markup.to_dict() if hasattr(reply_markup, "to_dict") else reply_markup


async def send_rich(bot, chat_id, text: str, reply_markup=None, **extra):
    """sendRichMessage -> the sent Message (a telegram.Message). Raises what the API raises."""
    from telegram import Message
    data = {"chat_id": chat_id, "rich_message": json.dumps({"html": to_rich_html(text)}), **extra}
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(_markup(reply_markup))
    result = await bot._post("sendRichMessage", data)
    return Message.de_json(result, bot) if isinstance(result, dict) else result


async def edit_rich(bot, chat_id, message_id, text: str, reply_markup=None):
    """editMessageText with a rich message instead of text. Raises what the API raises."""
    data = {"chat_id": chat_id, "message_id": message_id, "rich_message": json.dumps({"html": to_rich_html(text)})}
    if reply_markup is not None:
        data["reply_markup"] = json.dumps(_markup(reply_markup))
    return await bot._post("editMessageText", data)


async def send_post(bot, chat_id, text: str, native: bool, reply_markup=None, parse_mode="MarkdownV2", **extra):
    """
    Sends a post: as a rich message when `native`, else (or when Telegram refuses one) as ordinary MarkdownV2 text.
    -> the sent Message.
    """
    if native and fits(text):
        try:
            return await send_rich(bot, chat_id, text, reply_markup, **extra)
        except Exception as e:
            logger.warning(f"sendRichMessage failed for {chat_id}, posting plain text instead: {e}")
    return await bot.send_message(chat_id=chat_id, text=unfold(text), parse_mode=parse_mode, reply_markup=reply_markup, **extra)
