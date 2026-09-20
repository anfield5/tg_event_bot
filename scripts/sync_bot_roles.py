"""
One-time (and safe-to-rerun) sync: all_groups/all_channels gained a `role`
column (v4.3.13) tracking the bot's OWN MEMBER/ADMIN status in each chat,
kept live going forward via on_my_chat_member_update. But that only fires
on an ACTUAL status change - a chat the bot was added to (or was already
an admin in) BEFORE this feature existed, whose role has never changed
SINCE deploying it either, still has the column's default ('MEMBER'),
even if the bot is genuinely an admin there right now.

This script asks Telegram directly (getChatMember) for the bot's real,
current status in every chat_id already in all_groups/all_channels, and
writes the correct role - safe to run repeatedly, always reflects
whatever Telegram reports at the moment it runs.

Run once, from the project root, against your real database.db:
    python3 scripts/sync_bot_roles.py

Or target a single chat instead of every registered one:
    python3 scripts/sync_bot_roles.py <chat_id>

Needs BOT_TOKEN in the environment (same one the running bot uses) to
call Telegram's API directly - this makes real network requests, unlike
the DB-only backfill_event_dates.py script.
"""
import sys
import asyncio
import sqlite3

sys.path.insert(0, ".")
from telegram import Bot  # noqa: E402
from telegram.error import TelegramError  # noqa: E402
from config import TELEGRAM_TOKEN  # noqa: E402
from db import init_db, DB_PATH, update_chat_role  # noqa: E402


async def _sync_one(bot: Bot, chat_id: str, chat_name: str) -> str:
    """Returns the resolved role, or "ERROR"/"SKIPPED" (bot no longer
    in that chat - left untouched, register_chat_removed() via a real
    my_chat_member update is the correct way to clean that up, not this
    script silently deleting rows)."""
    try:
        member = await bot.get_chat_member(int(chat_id), bot.id)
    except TelegramError as e:
        print(f"  chat_id={chat_id} ({chat_name}): could not check ({e}) - skipped")
        return "ERROR"

    if member.status not in ("member", "administrator", "creator", "restricted"):
        print(f"  chat_id={chat_id} ({chat_name}): bot is no longer present (status={member.status}) - "
              f"left untouched, use a real chat action to trigger removal")
        return "SKIPPED"

    role = "ADMIN" if member.status == "administrator" else "MEMBER"
    update_chat_role(chat_id, role)
    print(f"  chat_id={chat_id} ({chat_name}): role={role}")
    return role


async def _run(target_chat_id: str = None, db_path: str = DB_PATH):
    init_db(db_path=db_path)  # make sure the role column exists
    if not TELEGRAM_TOKEN:
        print("BOT_TOKEN is not set - cannot query Telegram.")
        return

    conn = sqlite3.connect(db_path)
    if target_chat_id:
        rows = conn.execute(
            "SELECT chat_id, chat_name, 'group' FROM all_groups WHERE chat_id = ? "
            "UNION ALL "
            "SELECT chat_id, chat_name, 'channel' FROM all_channels WHERE chat_id = ?",
            (target_chat_id, target_chat_id),
        ).fetchall()
    else:
        rows = conn.execute("SELECT chat_id, chat_name, 'group' FROM all_groups").fetchall()
        rows += conn.execute("SELECT chat_id, chat_name, 'channel' FROM all_channels").fetchall()
    conn.close()

    if not rows:
        print("No registered groups/channels found - nothing to sync.")
        return

    bot = Bot(token=TELEGRAM_TOKEN)
    async with bot:
        print(f"Syncing {len(rows)} chat(s)...")
        admin_count = 0
        for chat_id, chat_name, _ in rows:
            role = await _sync_one(bot, chat_id, chat_name)
            if role == "ADMIN":
                admin_count += 1

    print(f"\nDone. {admin_count} chat(s) confirmed with the bot as ADMIN.")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else None
    asyncio.run(_run(target))
