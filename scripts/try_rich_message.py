"""
Live check of Telegram Rich Messages, the thing /hideability stands on - run it ONCE against a test chat before turning
/hideability on in a real group. It cannot be tested without Telegram, and three things are only visible on a screen.

    BOT_TOKEN=... python3 scripts/try_rich_message.py <chat_id>
    docker compose exec -T bot python3 scripts/try_rich_message.py <chat_id> --once      # in the container: BOT_TOKEN is already there

--once only sends the three layouts below and stops (no questions, nothing to type).

It does four things, pausing for Enter between them, and says what to look at:
  1. sends a sample post as a rich message: Going open, Not Going and Waitlist folded, with a keyboard. LOOK: do the
     titles fold and unfold on a tap? do names show as clickable mentions? do the line breaks look right?
  2. edits that same message (a name is added). LOOK: does a list you unfolded STAY unfolded after the edit?
  3. edits it back to ordinary text (what /hideability off does to an old post). LOOK: did Telegram accept it?
  0. (first) sends the same sample in the two layouts the bot can use - A: <p> paragraphs (what the bot sends now),
     B: <br> line breaks, C: <p> outside the blocks and the names straight in them - so you can compare the gaps
     between the lists on your screen.
Nothing is written to the database. The messages are left in the chat; delete them by hand.
"""
import asyncio
import os
import sys

sys.path.insert(0, ".")
import richtext  # noqa: E402
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup  # noqa: E402


def sample(extra: str = "") -> str:
    return (
        "*Test event*\n\n"
        + richtext.fold("*Going* \\(2\\)", "👍 alice\n👍 [bob](tg://user?id=1)", True)
        + "\n\n" + richtext.fold("*Not Going* \\(1\\)", "❌ carol" + extra, False)
        + "\n\n" + richtext.fold("*Waitlist* \\(1\\)", "💤 dave", False)
        + "\n\n📊 *TOTAL Going:* 2"
    )


async def main(chat_id: str, once: bool = False):
    token = os.getenv("BOT_TOKEN")
    if not token:
        sys.exit("BOT_TOKEN is not set")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("👍 Going", callback_data="x")]])
    async with Bot(token) as bot:
        import json
        await bot._post("sendRichMessage", {"chat_id": chat_id, "rich_message": json.dumps({"html": "<p>Layout B: &lt;br&gt; line breaks</p>" + richtext.to_rich_html(sample(), paragraphs=False)})})
        await bot._post("sendRichMessage", {"chat_id": chat_id, "rich_message": json.dumps({"html": "<p>Layout C: &lt;p&gt; outside, names straight in the block</p>" + richtext.to_rich_html(sample(), body_paragraph=False)})})
        await bot._post("sendRichMessage", {"chat_id": chat_id, "rich_message": json.dumps({"html": "<p>Layout A: &lt;p&gt; paragraphs (the bot's)</p>" + richtext.to_rich_html(sample())})})
        print("0. sent three layouts (A, B, C) - compare the gaps between the lists and tell me which is tightest.")
        if once:
            return
        input("   Enter to continue... ")
        sent = await richtext.send_rich(bot, chat_id, sample(), keyboard)
        print(f"1. sent a rich message (id {sent.message_id}). Unfold Not Going and Waitlist, check mentions and line breaks.")
        input("   Enter to edit it... ")
        await richtext.edit_rich(bot, chat_id, sent.message_id, sample("\n❌ erin"), keyboard)
        print("2. edited with a new name. Is Not Going still unfolded on your screen?")
        input("   Enter to edit it back to plain text... ")
        await bot.edit_message_text(chat_id=chat_id, message_id=sent.message_id, text=richtext.unfold(sample()), parse_mode="MarkdownV2", reply_markup=keyboard)
        print("3. edited as ordinary text: accepted. (If this raised an error, /hideability off falls back to an unfolded rich message.)")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--once"]
    if len(args) != 1:
        sys.exit(__doc__)
    asyncio.run(main(args[0], once="--once" in sys.argv))
