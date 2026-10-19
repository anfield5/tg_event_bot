"""/who: parsing, scope (main / alias / all), the two reports, formatting and the command itself."""
import sqlite3
import sys
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from gspread.exceptions import WorksheetNotFound

sys.path.insert(0, ".")
import aliases
import db as db_module
import subscription
import who
from tests.helpers import make_bot, make_chat, make_context, make_message, make_update
from tests.test_help_audit import _gather, markdown_v2_problems
from tests.test_stats_extended import _event
from tests.test_version_command import _FakeSpreadsheet

NOW = datetime(2026, 10, 5, 12, 0, 0)
SINCE48 = NOW - timedelta(hours=48)
HUB = "-100"


def ts(hours_ago, now=NOW):
    return (now - timedelta(hours=hours_ago)).strftime("%d.%m.%Y %H:%M:%S.%f")[:-3]


def act(action, uid, name, hours_ago, event="e1", chat=HUB):
    return {"EVENT_ID": event, "ACTION": action, "USER_NAME": name, "USER_ID": uid, "DATE": ts(hours_ago), "CHAT_ID": chat}


def plain(text):
    return text.replace("\\", "")


class TestParseArgs:
    @pytest.mark.parametrize("args,expected", [
        (["48", "-ng"], (48, True, False, None)),
        (["48", "-notgoing"], (48, True, False, None)),
        (["48", "-NG"], (48, True, False, None)),
        (["6", "-l"], (6, False, True, None)),
        (["6", "-LEFT"], (6, False, True, None)),
        (["6", "-l", "-c", "kids"], (6, False, True, "kids")),
        (["6", "-chat", "main", "-left"], (6, False, True, "main")),
        (["6", "-l", "-c", "-1001234567"], (6, False, True, "-1001234567")),
        (["-ng", "-l", "24"], (24, True, True, None)),
        (["-ng"], (6, True, False, None)),
        (["-l"], (6, False, True, None)),
        (["-ng", "-l"], (6, True, True, None)),
        (["-l", "-c", "kids"], (6, False, True, "kids")),
    ])
    def test_valid(self, args, expected):
        parsed, error = who.parse_who_args(args)
        assert error is None
        assert (parsed["hours"], parsed["ng"], parsed["left"], parsed["chat"]) == expected

    @pytest.mark.parametrize("args,fragment", [
        ([], "Add -ng"),
        (["-c", "kids"], "Add -ng"),
        (["abc", "-ng"], "Don't understand"),
        (["0", "-ng"], "between 1 and"),
        (["99999", "-ng"], "between 1 and"),
        (["48"], "Add -ng"),
        (["48", "48", "-ng"], "only once"),
        (["48", "-l", "-c"], "-c needs a value"),
        (["48", "-l", "-c", "-ng"], "-c needs a value"),
        (["48", "-l", "-c", "a", "-c", "b"], "twice"),
        (["48", "-ng", "-x"], "Don't understand"),
        (["4.5", "-ng"], "Don't understand"),
    ])
    def test_invalid(self, args, fragment):
        parsed, error = who.parse_who_args(args)
        assert parsed is None and fragment in error


@pytest.fixture()
def hub_chats(db_path):
    conn = sqlite3.connect(db_path)
    rows = [("-200", HUB, "kids", "Kids Group", 1),      # monitored, aliased
            ("-300", HUB, None, "News Channel", 1),      # monitored, no alias
            ("-400", HUB, "share", "Share Only", 0)]     # alias only - not monitored
    conn.executemany("INSERT INTO sub_chats (chat_id, owner_chat_id, alias, chat_name, is_monitored) VALUES (?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


class TestResolveScope:
    def test_default_is_all_chats_that_are_tracked(self, hub_chats):
        scope, error = who.resolve_scope(HUB, None)
        assert error is None and scope["is_all"]
        assert scope["chats"] == {HUB: "main", "-200": "kids", "-300": "News Channel"}

    def test_all_is_the_same_as_the_default(self, hub_chats):
        assert who.resolve_scope(HUB, "ALL")[0]["chats"] == who.resolve_scope(HUB, None)[0]["chats"]

    @pytest.mark.parametrize("value", ["main", "MAIN", HUB])
    def test_main_is_the_hub_itself(self, hub_chats, value):
        scope, _ = who.resolve_scope(HUB, value)
        assert scope["chats"] == {HUB: "main"} and not scope["is_all"]

    @pytest.mark.parametrize("value", ["kids", "KIDS", "-200"])
    def test_one_chat_by_alias_or_id(self, hub_chats, value):
        scope, _ = who.resolve_scope(HUB, value)
        assert scope["chats"] == {"-200": "kids"} and not scope["unmonitored"]

    def test_a_chat_that_is_only_an_alias_is_flagged_unmonitored(self, hub_chats):
        scope, _ = who.resolve_scope(HUB, "share")
        assert scope["unmonitored"] == {"-400"}

    def test_unknown_names_list_what_is_available(self, hub_chats):
        scope, error = who.resolve_scope(HUB, "nope")
        assert scope is None and "kids" in error and "share" in error

    def test_labels_cover_every_known_chat(self, hub_chats):
        labels = who.resolve_scope(HUB, None)[0]["labels"]
        assert labels["-400"] == "share" and labels[HUB] == "main"

    def test_other_hubs_chats_are_invisible(self, hub_chats, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO sub_chats (chat_id, owner_chat_id, alias, is_monitored) VALUES ('-900', '-999', 'theirs', 1)")
        conn.commit()
        conn.close()
        assert "-900" not in who.resolve_scope(HUB, None)[0]["chats"]
        assert who.resolve_scope(HUB, "theirs")[0] is None


class TestNotGoingChanges:
    def test_a_change_from_going_is_reported(self):
        rows = [act("GOING", 1, "ivan", 30), act("NOTGOING", 1, "ivan", 10)]
        found = who.notgoing_changes(rows, "e1", SINCE48)
        assert [(f["name"], f["when"]) for f in found] == [("ivan", NOW - timedelta(hours=10))]

    def test_a_first_time_not_going_is_not_a_change(self):
        assert who.notgoing_changes([act("NOTGOING", 1, "ivan", 10)], "e1", SINCE48) == []

    def test_changes_before_the_window_are_ignored(self):
        rows = [act("GOING", 1, "ivan", 70), act("NOTGOING", 1, "ivan", 60)]
        assert who.notgoing_changes(rows, "e1", SINCE48) == []

    def test_the_earlier_status_may_predate_the_window(self):
        rows = [act("GOING", 1, "ivan", 200), act("NOTGOING", 1, "ivan", 5)]
        assert len(who.notgoing_changes(rows, "e1", SINCE48)) == 1

    def test_someone_who_went_back_to_going_is_marked(self):
        rows = [act("GOING", 1, "ivan", 30), act("NOTGOING", 1, "ivan", 20), act("GOING", 1, "ivan", 10)]
        found = who.notgoing_changes(rows, "e1", SINCE48)
        assert found[0]["back_to_going"] is True and found[0]["when"] == NOW - timedelta(hours=20)

    def test_the_latest_change_wins_when_there_were_several(self):
        rows = [act("GOING", 1, "ivan", 40), act("NOTGOING", 1, "ivan", 30),
                act("GOING", 1, "ivan", 20), act("NOTGOING", 1, "ivan", 10)]
        found = who.notgoing_changes(rows, "e1", SINCE48)
        assert len(found) == 1 and found[0]["when"] == NOW - timedelta(hours=10) and not found[0]["back_to_going"]

    def test_only_the_asked_event_counts(self):
        rows = [act("GOING", 1, "ivan", 30, event="old"), act("NOTGOING", 1, "ivan", 10, event="old")]
        assert who.notgoing_changes(rows, "e1", SINCE48) == []

    def test_guest_actions_prove_the_person_was_in(self):
        rows = [act("ADD", 1, "ivan", 30), act("NOTGOING", 1, "ivan", 10)]
        assert len(who.notgoing_changes(rows, "e1", SINCE48)) == 1

    def test_admin_clicks_say_nothing_about_the_admins_own_status(self):
        rows = [act("KICK", 7, "admin", 30), act("RETURN", 7, "admin", 29), act("NOTGOING", 7, "admin", 10)]
        assert who.notgoing_changes(rows, "e1", SINCE48) == []

    def test_scope_filters_by_the_chat_the_click_came_from(self):
        rows = [act("GOING", 1, "ivan", 30, chat="-200"), act("NOTGOING", 1, "ivan", 10, chat="-200")]
        assert len(who.notgoing_changes(rows, "e1", SINCE48, {"-200"})) == 1
        assert who.notgoing_changes(rows, "e1", SINCE48, {HUB}) == []
        assert len(who.notgoing_changes(rows, "e1", SINCE48, None)) == 1

    def test_an_unreadable_date_on_the_earlier_row_does_not_hide_the_change(self):
        first = {**act("GOING", 1, "ivan", 30), "DATE": "garbage"}
        assert len(who.notgoing_changes([first, act("NOTGOING", 1, "ivan", 10)], "e1", SINCE48)) == 1

    def test_newest_first(self):
        rows = [act("GOING", 1, "a", 40), act("GOING", 2, "b", 40), act("NOTGOING", 1, "a", 30), act("NOTGOING", 2, "b", 5)]
        assert [f["name"] for f in who.notgoing_changes(rows, "e1", SINCE48)] == ["b", "a"]

    def test_numeric_ids_from_google_sheets_are_matched(self):
        rows = [act("GOING", 5, "ivan", 30), act("NOTGOING", 5, "ivan", 10)]
        assert len(who.notgoing_changes(rows, 1 and "e1", SINCE48)) == 1

    def test_rows_without_a_user_id_are_skipped(self):
        assert who.notgoing_changes([act("GOING", "", "x", 30), act("NOTGOING", "", "x", 10)], "e1", SINCE48) == []


def _log(uid, chat, hours_ago, start="01.01.2026"):
    return {"USER_ID": uid, "CHAT_ID": chat, "DATE_start": start, "DATE_end": ts(hours_ago)}


def _user(uid, chat, first="", last="", username="", status="MEMBER", end=""):
    return {"USER_ID": uid, "CHAT_ID": chat, "FIRST_NAME": first, "LAST_NAME": last, "USER_NAME": username,
            "STATUS": status, "DATE_start": "01.01.2026", "DATE_end": end}


class TestLeftInWindow:
    def test_a_logged_departure_is_reported_with_the_name_from_users(self):
        out = who.left_in_window([_log("1", HUB, 3)], [_user("1", HUB, "Ivan", "Petrenko", "ivan_p")], SINCE48, {HUB})
        assert out[HUB][0]["name"] == "Ivan Petrenko (ivan_p)"

    def test_window_and_scope(self):
        log = [_log("1", HUB, 3), _log("2", HUB, 100), _log("3", "-200", 3)]
        out = who.left_in_window(log, [], SINCE48, {HUB})
        assert [i["name"] for i in out[HUB]] == ["user 1"] and "-200" not in out

    def test_users_rows_marked_left_are_a_safety_net_and_dedupe_against_the_log(self):
        end = ts(3)
        users = [_user("1", HUB, username="a", status="LEFT", end=end), _user("2", HUB, username="b", status="LEFT", end=end)]
        out = who.left_in_window([_log("1", HUB, 3)], users, SINCE48, {HUB})
        assert sorted(i["name"] for i in out[HUB]) == ["a", "b"]

    def test_name_falls_back_to_the_same_person_in_another_chat(self):
        out = who.left_in_window([_log("1", "-200", 3)], [_user("1", HUB, username="ivan")], SINCE48, {"-200"})
        assert out["-200"][0]["name"] == "ivan"

    def test_username_only_and_name_only(self):
        users = [_user("1", HUB, username="only_handle"), _user("2", HUB, first="Olena")]
        out = who.left_in_window([_log("1", HUB, 3), _log("2", HUB, 4)], users, SINCE48, {HUB})
        assert [i["name"] for i in out[HUB]] == ["only_handle", "Olena"]

    def test_newest_first_and_unreadable_dates_ignored(self):
        log = [_log("1", HUB, 10), _log("2", HUB, 2), {**_log("3", HUB, 1), "DATE_end": "garbage"}]
        out = who.left_in_window(log, [], SINCE48, {HUB})
        assert [i["name"] for i in out[HUB]] == ["user 2", "user 1"]


class TestFormatting:
    LABELS = {HUB: "main", "-200": "kids"}

    def test_names_are_plain_so_nobody_is_pinged(self):
        text = who.format_notgoing("Football", 48, [{"name": "ivan_p", "when": NOW, "chat": HUB, "back_to_going": False}], self.LABELS)
        assert "@" not in text and "tg://" not in text

    def test_special_characters_stay_valid_markdown(self):
        item = {"name": "a_b*c[d](e).f!", "when": NOW, "chat": "-200", "back_to_going": True}
        text = who.format_notgoing("Foot_ball (A+B)", 48, [item], self.LABELS)
        assert markdown_v2_problems(text) == []
        left = who.format_left(48, {HUB: [{"name": "x_y*z", "when": NOW}]}, {HUB: "main", "-200": "kids_2"})
        assert markdown_v2_problems(left) == []

    def test_left_report_puts_main_first_and_skips_empty_chats(self):
        text = plain(who.format_left(48, {"-200": [{"name": "k", "when": NOW}], HUB: [{"name": "m", "when": NOW}]},
                                     {"-200": "kids", HUB: "main", "-300": "news"}))
        assert text.index("main") < text.index("kids") and "news" not in text

    def test_empty_results_say_so(self):
        assert "No one changed" in plain(who.format_notgoing("Football", 3, [], self.LABELS))
        assert "No one left" in plain(who.format_left(3, {}, {HUB: "main"}))

    def test_long_lists_are_capped(self):
        items = [{"name": f"u{i}", "when": NOW, "chat": HUB, "back_to_going": False} for i in range(130)]
        text = plain(who.format_notgoing("Football", 48, items, self.LABELS))
        assert "...and 30 more" in text and "u99" in text and "u100" not in text

    def test_split_message_respects_the_limit_and_keeps_every_line(self):
        text = "\n".join(f"line {i} " + "x" * 50 for i in range(200))
        chunks = who.split_message(text, limit=500)
        assert all(len(c) <= 500 for c in chunks) and "\n".join(chunks) == text


@pytest.fixture()
def pro_hub(db_path):
    end = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO all_groups (chat_id, chat_name, type, subs_date_start, subs_date_end) VALUES (?, 'Hub', 'PRO', ?, ?)", (HUB, end, end))
    conn.commit()
    conn.close()


def _log_live(uid, chat, h):
    return {"USER_ID": uid, "CHAT_ID": chat, "DATE_start": "01.01.2026", "DATE_end": hours_ago(h)}


class _Tab:
    def __init__(self, records):
        self.records = records

    async def get_all_records(self):
        return list(self.records)


class _Book:
    def __init__(self, **tabs):
        self.tabs = tabs

    async def worksheet(self, name):
        if name not in self.tabs:
            raise WorksheetNotFound(name)
        return self.tabs[name]


def hours_ago(h):
    return (datetime.now() - timedelta(hours=h)).strftime("%d.%m.%Y %H:%M:%S.%f")[:-3]


def action_row(action, uid, name, h, event="Football", chat=HUB):
    return {"EVENT_ID": event, "ACTION": action, "USER_NAME": name, "USER_ID": uid, "DATE": hours_ago(h), "CHAT_ID": chat}


async def run(args, book=None, *, admin=True, bound=True, open_raises=None):
    bot = make_bot()
    bot.get_chat_member = AsyncMock(return_value=MagicMock(status="administrator" if admin else "member"))
    chat = make_chat(chat_id=int(HUB), chat_type="supergroup")
    msg = make_message(chat=chat)
    opened = AsyncMock(side_effect=open_raises) if open_raises else AsyncMock(return_value=(book if bound else None))
    with patch("who.get_sheet_for_chat", new_callable=AsyncMock, return_value="sheet" if bound else None), \
         patch("who.open_spreadsheet", opened):
        await who.who_command(make_update(chat=chat, message=msg), make_context(bot=bot, args=args))
    return [c.args[0] for c in msg.reply_text.call_args_list], opened


class TestCommand:
    async def test_ng_reports_changes_and_skips_first_time_declines(self, db_path, pro_hub, hub_chats):
        _event(db_path, "Football")
        book = _Book(Actions=_Tab([
            action_row("GOING", 1, "ivan", 30, chat="-200"), action_row("NOTGOING", 1, "ivan", 5, chat="-200"),
            action_row("NOTGOING", 2, "olena", 4),
        ]))
        texts, _ = await run(["48", "-ng"], book)
        out = plain("\n".join(texts))
        assert "ivan" in out and "kids" in out and "Football" in out and "olena" not in out

    @pytest.mark.parametrize("flag", ["-ng", "-notgoing", "-NG"])
    async def test_both_spellings_of_the_flag_do_the_same(self, db_path, pro_hub, flag):
        _event(db_path, "Football")
        book = _Book(Actions=_Tab([action_row("GOING", 1, "ivan", 30), action_row("NOTGOING", 1, "ivan", 5)]))
        texts, _ = await run(["48", flag], book)
        assert "ivan" in plain(texts[0])

    async def test_window_is_respected(self, db_path, pro_hub):
        _event(db_path, "Football")
        book = _Book(Actions=_Tab([action_row("GOING", 1, "ivan", 30), action_row("NOTGOING", 1, "ivan", 5)]))
        texts, _ = await run(["2", "-ng"], book)
        assert "No one changed" in plain(texts[0])

    async def test_it_looks_at_the_latest_event_only(self, db_path, pro_hub):
        _event(db_path, "Old")
        _event(db_path, "Football")
        book = _Book(Actions=_Tab([action_row("GOING", 1, "ivan", 30, event="Old"), action_row("NOTGOING", 1, "ivan", 5, event="Old")]))
        texts, _ = await run(["48", "-ng"], book)
        assert "No one changed" in plain(texts[0]) and "Football" in plain(texts[0])

    async def test_c_narrows_the_vote_report_to_the_chat_the_click_came_from(self, db_path, pro_hub, hub_chats):
        _event(db_path, "Football")
        book = _Book(Actions=_Tab([
            action_row("GOING", 1, "kid", 30, chat="-200"), action_row("NOTGOING", 1, "kid", 5, chat="-200"),
            action_row("GOING", 2, "adult", 30), action_row("NOTGOING", 2, "adult", 5),
        ]))
        kids = plain((await run(["48", "-ng", "-c", "kids"], book))[0][0])
        main = plain((await run(["48", "-ng", "-c", "main"], book))[0][0])
        assert "kid" in kids and "adult" not in kids and "adult" in main and "kid " not in main

    async def test_left_groups_departures_by_chat(self, db_path, pro_hub, hub_chats):
        book = _Book(
            UserPresenceLog=_Tab([_log_live("1", HUB, 3), _log_live("2", "-200", 5), _log_live("3", "-300", 6)]),
            Users=_Tab([_user("1", HUB, "Ivan", "P", "ivan"), _user("2", "-200", username="kid"), _user("3", "-300", first="Sub")]),
        )
        out = plain((await run(["48", "-l"], book))[0][0])
        assert out.index("main") < out.index("kids") and all(n in out for n in ("Ivan P (ivan)", "kid", "Sub", "News Channel"))

    async def test_left_with_c_narrows_to_one_chat(self, db_path, pro_hub, hub_chats):
        book = _Book(UserPresenceLog=_Tab([_log_live("1", HUB, 3), _log_live("2", "-200", 5)]),
                     Users=_Tab([_user("1", HUB, username="mainguy"), _user("2", "-200", username="kidguy")]))
        kids = plain((await run(["48", "-l", "-c", "kids"], book))[0][0])
        main = plain((await run(["48", "-left", "-chat", "main"], book))[0][0])
        assert "kidguy" in kids and "mainguy" not in kids and "mainguy" in main and "kidguy" not in main

    async def test_both_reports_come_in_one_message(self, db_path, pro_hub):
        _event(db_path, "Football")
        book = _Book(Actions=_Tab([action_row("GOING", 1, "ivan", 30), action_row("NOTGOING", 1, "ivan", 5)]),
                     UserPresenceLog=_Tab([_log_live("9", HUB, 3)]), Users=_Tab([_user("9", HUB, username="gone")]))
        texts, _ = await run(["48", "-ng", "-l"], book)
        out = plain(texts[0])
        assert len(texts) == 1 and "Changed to Not Going" in out and "Left" in out and "gone" in out

    async def test_a_missing_presence_tab_still_reports_users_marked_left(self, db_path, pro_hub):
        book = _Book(Users=_Tab([_user("1", HUB, username="gone", status="LEFT", end=hours_ago(3))]))
        assert "gone" in plain((await run(["48", "-l"], book))[0][0])

    async def test_nobody_is_mentioned(self, db_path, pro_hub):
        _event(db_path, "Football")
        book = _Book(Actions=_Tab([action_row("GOING", 1, "ivan_p", 30), action_row("NOTGOING", 1, "ivan_p", 5)]))
        out = (await run(["48", "-ng"], book))[0][0]
        assert "@" not in out and "tg://" not in out

    async def test_a_long_report_is_split_into_valid_messages(self, db_path, pro_hub):
        _event(db_path, "Football")
        rows = []
        for i in range(1, 91):
            rows += [action_row("GOING", i, f"name_{i}", 30), action_row("NOTGOING", i, f"name_{i}", 5)]
        texts, _ = await run(["48", "-ng"], _Book(Actions=_Tab(rows)))
        assert all(len(t) <= 4096 for t in texts) and all(markdown_v2_problems(t) == [] for t in texts)
        assert "...and 0" not in plain("\n".join(texts))

    @pytest.mark.parametrize("args,fragment", [
        ([], "Usage"), (["abc"], "Usage"), (["48"], "Add -ng"), (["0", "-ng"], "between 1 and"),
        (["48", "-l", "-c"], "-c needs a value"),
    ])
    async def test_bad_input_gets_the_usage_and_touches_nothing(self, db_path, pro_hub, args, fragment):
        texts, opened = await run(args, _Book())
        assert fragment in plain(texts[0]) and "```" in texts[0]
        opened.assert_not_called()

    async def test_an_unknown_chat_is_explained(self, db_path, pro_hub, hub_chats):
        texts, opened = await run(["48", "-l", "-c", "nope"], _Book())
        assert "kids" in plain(texts[0]) and "main" in plain(texts[0])
        opened.assert_not_called()

    async def test_an_unmonitored_chat_cannot_be_asked_for_departures(self, db_path, pro_hub, hub_chats):
        texts, opened = await run(["48", "-l", "-c", "share"], _Book())
        assert "/addmonitor" in plain(texts[0])
        opened.assert_not_called()

    async def test_only_admins_may_ask(self, db_path, pro_hub):
        texts, opened = await run(["48", "-ng"], _Book(), admin=False)
        assert "Only admins" in plain(texts[0])
        opened.assert_not_called()

    async def test_a_free_hub_is_told_it_is_a_pro_feature(self, db_path):
        texts, opened = await run(["48", "-ng"], _Book())
        assert texts and "Only admins" not in plain(texts[0])
        opened.assert_not_called()

    async def test_no_bound_sheet_points_at_setsheet(self, db_path, pro_hub):
        texts, _ = await run(["48", "-ng"], bound=False)
        assert "/setsheet" in plain(texts[0])

    async def test_a_sheets_failure_is_reported_not_raised(self, db_path, pro_hub):
        texts, _ = await run(["48", "-l"], open_raises=RuntimeError("invalid_grant"))
        assert "Couldn't read Google Sheets" in plain(texts[0])

    async def test_a_missing_actions_tab_is_reported(self, db_path, pro_hub):
        _event(db_path, "Football")
        texts, _ = await run(["48", "-ng"], _Book())
        assert "Couldn't read Google Sheets" in plain(texts[0])

    async def test_no_events_yet(self, db_path, pro_hub):
        texts, _ = await run(["48", "-ng"], _Book(Actions=_Tab([])))
        assert "No events" in plain(texts[0])


class TestIntegration:
    def test_the_command_is_registered(self):
        assert 'CommandHandler("who", who_command)' in open("main.py", encoding="utf-8").read()

    async def test_help_shows_it_only_on_hubs_that_have_the_feature(self, db_path, pro_hub):
        assert "/who" in plain((await _gather(1))["help_users"])
        assert "/who" not in plain((await _gather(1, chat_id=-200))["help_users"])

    async def test_the_flags_are_explained_in_the_expanded_help(self, db_path, pro_hub):
        expanded = plain((await _gather(1))["help_flags_users_expand"])
        assert all(token in expanded for token in ("-ng | -notgoing", "-l | -left", "-c | -chat", "alias|main|all"))

    async def test_an_alias_cannot_shadow_main_or_all(self, db_path, pro_hub):
        for name in ("main", "ALL"):
            bot = make_bot()
            bot.get_chat = AsyncMock()
            chat = make_chat(chat_id=int(HUB), chat_type="supergroup")
            msg = make_message(chat=chat)
            await aliases.setalias(make_update(chat=chat, message=msg), make_context(bot=bot, args=["-1001", name]))
            assert "reserved" in plain(msg.reply_text.call_args.args[0])
            bot.get_chat.assert_not_called()


class TestWhoIsASeparateProFeature:
    def _tier(self, db_path):
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT min_tier FROM all_features WHERE feature_key = 'who'").fetchone()
        conn.close()
        return row[0] if row else None

    def _set_tier(self, db_path, tier):
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE all_features SET min_tier = ? WHERE feature_key = 'who'", (tier,))
        conn.commit()
        conn.close()

    def test_it_has_its_own_feature_row_at_pro(self, db_path):
        assert self._tier(db_path) == "PRO"

    def test_an_existing_database_gets_the_row_and_keeps_a_tier_the_owner_changed(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM all_features WHERE feature_key = 'who'")
        conn.commit()
        conn.close()
        db_module.init_db(db_path=db_path)
        assert self._tier(db_path) == "PRO"
        self._set_tier(db_path, "OWNER")
        db_module.init_db(db_path=db_path)
        assert self._tier(db_path) == "OWNER", "a restart must not undo /updatefeature"

    async def test_the_control_sheet_lists_it_as_a_pro_feature(self, db_path):
        fake_ss = _FakeSpreadsheet()
        with patch("sheets.CONTROL_SHEET_ID", "fake"), \
             patch("sheets.open_spreadsheet", new_callable=AsyncMock, return_value=fake_ss):
            await subscription._push_control_sheet_botconfig()
        row = next(r for r in fake_ss.worksheets["BOTCONFIG"].updates[0] if r[0] == "who")
        assert row[2:5] == ["no", "yes", "yes"], "FREE no / PRO yes / OWNER yes"

    async def test_lowering_it_to_free_opens_it_to_a_free_hub_and_the_help_follows(self, db_path):
        self._set_tier(db_path, "FREE")
        assert "/who" in plain((await _gather(1, chat_id=-200))["help_users"])

    async def test_raising_it_to_owner_hides_it_from_admins_but_not_from_the_owner(self, db_path, pro_hub):
        self._set_tier(db_path, "OWNER")
        assert "/who" not in plain((await _gather(1))["help_users"])
        assert "/who" in plain((await _gather(555))["help_users"])
        texts, opened = await run(["48", "-ng"], _Book())          # an ordinary admin of a PRO hub
        assert texts == [], "an OWNER-level feature stays silent for everyone else, like /deleteevent"
        opened.assert_not_called()

    async def test_the_aliases_help_names_the_reserved_words(self, db_path, pro_hub):
        text = plain((await _gather(1))["help_alias"])
        assert "reserved" in text and "main" in text and "all" in text

    async def test_the_monitoring_help_points_at_who_while_it_is_available(self, db_path, pro_hub):
        assert "/who" in plain((await _gather(1))["help_monitoring"])
        self._set_tier(db_path, "OWNER")
        assert "/who" not in plain((await _gather(1))["help_monitoring"])


class TestDefaultHours:
    """No number of hours means the last 6."""

    def test_the_default_is_six_hours(self):
        assert who.WHO_DEFAULT_HOURS == 6

    async def test_not_going_without_hours_looks_back_six_hours(self, db_path, pro_hub):
        _event(db_path, "Football")
        book = _Book(Actions=_Tab([
            action_row("GOING", 1, "recent", 20), action_row("NOTGOING", 1, "recent", 5),
            action_row("GOING", 2, "older", 20), action_row("NOTGOING", 2, "older", 7),
        ]))
        out = plain((await run(["-ng"], book))[0][0])
        assert "recent" in out and "older" not in out and "last 6 h" in out

    async def test_left_without_hours_uses_the_same_default(self, db_path, pro_hub):
        book = _Book(UserPresenceLog=_Tab([_log_live("1", HUB, 5), _log_live("2", HUB, 8)]),
                     Users=_Tab([_user("1", HUB, username="five_h"), _user("2", HUB, username="eight_h")]))
        out = plain((await run(["-l"], book))[0][0])
        assert "five_h" in out and "eight_h" not in out and "last 6 h" in out

    async def test_an_explicit_number_still_wins(self, db_path, pro_hub):
        _event(db_path, "Football")
        book = _Book(Actions=_Tab([action_row("GOING", 2, "older", 20), action_row("NOTGOING", 2, "older", 7)]))
        assert "older" in plain((await run(["48", "-ng"], book))[0][0])

    async def test_the_number_may_come_after_the_flags(self, db_path, pro_hub):
        _event(db_path, "Football")
        book = _Book(Actions=_Tab([action_row("GOING", 2, "older", 20), action_row("NOTGOING", 2, "older", 7)]))
        assert "older" in plain((await run(["-ng", "24"], book))[0][0])

    async def test_an_explicit_zero_is_still_an_error_not_the_default(self, db_path, pro_hub):
        texts, opened = await run(["0", "-ng"], _Book())
        assert "between 1 and" in plain(texts[0])
        opened.assert_not_called()

    async def test_the_usage_block_and_the_help_state_the_default(self, db_path, pro_hub):
        usage = plain((await run([], _Book()))[0][0])
        assert "optional" in usage and "6 h" in usage
        assert "default 6" in plain((await _gather(1))["help_users"])
        assert "6 by default" in plain((await _gather(1))["help_flags_users_expand"])
