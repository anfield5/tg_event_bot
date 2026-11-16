"""
Live check of Telegram Rich Messages, the thing /hideability stands on - run it ONCE against a test chat before turning
/hideability on in a real group. It cannot be tested without Telegram, and three things are only visible on a screen.

    BOT_TOKEN=... python3 scripts/try_rich_message.py <chat_id>

It does four things, pausing for Enter between them, and says what to look at:
  1. sends a sample post as a rich message: Going open, Not Going and Waitlist folded, with a keyboard. LOOK: do the
     titles fold and unfold on a tap? do names show as clickable mentions? do the line breaks look right?
  2. edits that same message (a name is added). LOOK: does a list you unfolded STAY unfolded after the edit?
  3. edits it back to ordinary text (what /hideability off does to an old post). LOOK: did Telegram accept it?
  0. (first) sends the same sample in the two layouts the bot can use - A: <p> paragraphs (what the bot sends now),
     B: <br> line breaks - so you can compare the gaps between the lists on your screen.
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


async def main(chat_id: str):
    token = os.getenv("BOT_TOKEN")
    if not token:
        sys.exit("BOT_TOKEN is not set")
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("👍 Going", callback_data="x")]])
    async with Bot(token) as bot:
        import json
        await bot._post("sendRichMessage", {"chat_id": chat_id, "rich_message": json.dumps({"html": "<p>Layout B: &lt;br&gt; line breaks</p>" + richtext.to_rich_html(sample(), paragraphs=False)})})
        await bot._post("sendRichMessage", {"chat_id": chat_id, "rich_message": json.dumps({"html": "<p>Layout A: &lt;p&gt; paragraphs (the bot's)</p>" + richtext.to_rich_html(sample())})})
        print("0. sent the two layouts (A and B) - compare the gaps between the lists.")
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
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1]))
