"""
A safety net under event_engine.update_all_shared_views - the 500-line function that draws every event post.

It renders hundreds of scenarios (event status x the three list modes x the share modes x guests x which lists are
folded x what get_chat answers) and compares a short fingerprint of each post's text AND keyboard with the one recorded in
render_snapshot.json. The recorded fingerprints were taken from the code BEFORE the render was refactored (title cache,
_fold_block), so a pass means a refactor did not change a single character of any post.

If you change what a post looks like ON PURPOSE, regenerate the file and review the diff of the posts, not of the hashes:
    UPDATE_RENDER_SNAPSHOT=1 python3 -m pytest tests/test_render_snapshot.py -q
"""
import hashlib
import itertools
import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, ".")
import db as db_module
import event_engine
from tests.helpers import make_bot, make_context

HUB, CH1, CH2 = "-100", "-210", "-220"
SNAPSHOT = Path(__file__).with_name("render_snapshot.json")
MODES = ["visible", "onlycount", "hidden"]
FOLD_SETS = {
    "default": [],
    "all_toggled": [(HUB, "going"), (HUB, "notgoing"), (HUB, "waitlist"), (CH1, "going"), (CH1, "notgoing"), (CH1, "waitlist"),
                    (CH2, "going"), (CH2, "notgoing"), (CH2, "waitlist")],
    "hub_ng_open": [(HUB, "notgoing")],
}
TITLES = {"get_chat_ok": lambda cid: MagicMock(title=f"Ünï ({cid})_x"), "empty_title": lambda cid: MagicMock(title=""), "fails": None}


def seed(db_path, status, ngl, wl, mode1, mode2, guests):
    c = sqlite3.connect(db_path)
    for t in ("events", "event_users", "event_shares", "list_view_state", "main_group_users"):
        c.execute(f"DELETE FROM {t}")
    wlist = [
        {"chat_id": HUB, "chat_name": None, "username": "wl_hub", "user_id": "81", "timestamp": "t1"},
        {"chat_id": CH1, "chat_name": None, "username": "wl_c1", "user_id": "82", "timestamp": "t2"},
        {"chat_id": CH1, "chat_name": None, "username": "wl_guest", "user_id": "83", "timestamp": "t3", "is_guest": True},
        {"chat_id": CH2, "chat_name": None, "username": "wl_c2", "user_id": "84", "timestamp": "t4"},
    ]
    c.execute("INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon, event_status, going_data, notgoing_data, counters_data, "
              "kicked_data, waitlist_data, waitlist_visibility, notgoing_visibility, clickability, event_date, total_limit) "
              "VALUES ('ev1', ?, '1', 'Big_Match (x)!', '👍', '❌', ?, '[]', '[]', '{}', '[]', ?, ?, ?, 'on', '01.12.2026 18:00', 50)",
              (HUB, status, json.dumps(wlist), wl, ngl))
    rows = [(HUB, "1", "alice", "going", 2 if guests else 0), (HUB, "2", "bob", "going", 0), (HUB, "3", "carol", "notgoing", 0),
            (HUB, "4", "dave", "notgoing", 0), (HUB, "5", "kicky", "kicked", 1 if guests else 0),
            (CH1, "11", "erin", "going", 1 if guests else 0), (CH1, "12", "fran", "notgoing", 0),
            (CH2, "21", "gus", "going", 0), (CH2, "22", "hal", "notgoing", 0), (CH2, "23", "ivy", "notgoing", 0)]
    for chat, uid, name, st, g in rows:
        c.execute("INSERT INTO event_users (event_id, chat_id, user_id, username, status, guests, first_name, last_name) VALUES ('ev1', ?, ?, ?, ?, ?, ?, 'Z')",
                  (chat, uid, name, st, g, name.title()))
        c.execute("INSERT OR IGNORE INTO main_group_users (chat_id, username, user_id, first_name, last_name, status) VALUES (?, ?, ?, ?, 'Z', 'active')",
                  (chat, name, uid, name.title()))
    c.execute("INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type) VALUES ('ev1', ?, '2', ?, 'group')", (CH1, mode1))
    c.execute("INSERT INTO event_shares (event_id, chat_id, message_id, share_mode, chat_type, share_notgoing_visibility, share_waitlist_visibility) "
              "VALUES ('ev1', ?, '3', ?, 'channel', 'visible', 'visible')", (CH2, mode2))
    c.commit()
    c.close()


async def render(title_mode):
    bot = make_bot()
    if title_mode == "fails":
        bot.get_chat = AsyncMock(side_effect=RuntimeError("no access"))
    else:
        maker = TITLES[title_mode]
        bot.get_chat = AsyncMock(side_effect=lambda cid: maker(cid))
    await event_engine.update_all_shared_views(make_context(bot=bot), "ev1")
    out = {}
    for call in bot.edit_message_text.call_args_list:
        kw = call.kwargs
        markup = kw.get("reply_markup")
        buttons = [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard] if markup else None
        out[str(kw["chat_id"])] = {"text": kw["text"], "keyboard": buttons}
    return out


def fingerprint(post_set) -> str:
    return hashlib.sha256(json.dumps(post_set, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


async def test_every_post_is_drawn_exactly_as_before(db_path):
    result = {}
    for status, ngl, wl, (m1, m2), guests, folds, titles in itertools.product(
            [0, 1, 2, -1], MODES, MODES, [("-visible", "-onlycount"), ("-hidden", "-visible")], [False, True], FOLD_SETS, TITLES):
        if titles != "get_chat_ok" and not (status == 0 and ngl == "visible" and wl == "visible" and folds == "default"):
            continue            # the failing / empty title variants only need a few scenarios
        seed(db_path, status, ngl, wl, m1, m2, guests)
        for chat, name in FOLD_SETS[folds]:
            db_module.toggle_list_state("ev1", chat, name)
        event_engine._TITLE_CACHE.clear()
        result[f"{status}|{ngl}|{wl}|{m1}|{m2}|{guests}|{folds}|{titles}"] = fingerprint(await render(titles))

    if os.environ.get("UPDATE_RENDER_SNAPSHOT"):
        SNAPSHOT.write_text(json.dumps(result, sort_keys=True, indent=0))
        return
    expected = json.loads(SNAPSHOT.read_text())
    assert set(expected) == set(result), "the scenarios changed - regenerate the snapshot on purpose, see the module docstring"
    different = sorted(k for k in expected if expected[k] != result[k])
    assert not different, f"{len(different)} of {len(expected)} scenarios render differently, e.g. {different[:3]} (status|ngl|wl|share modes|guests|folds|titles)"
