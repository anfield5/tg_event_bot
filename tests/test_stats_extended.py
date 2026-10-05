"""
Extended stats: the per-group status breakdown / guest share / not-going / limit fill /
waitlist numbers, the waitlist_joined trigger that feeds them, and the bot-wide owner
report (subscriptions, growth and churn, activity and top commands/groups).
"""
import sys
import sqlite3
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

sys.path.insert(0, ".")
import db as db_module
import handlers
from tests.helpers import make_chat, make_user, make_message, make_update, make_context, sqlite_has_json1

needs_json1 = pytest.mark.skipif(not sqlite_has_json1(), reason="this SQLite build has no JSON1")

HUB = "-100"
NOW = datetime(2026, 10, 5, 12, 0, 0)


def _stamp(days_ago, now=None):
    """Same shape as now2ddmmyy(): DD.MM.YYYY HH:MM:SS.mmm"""
    return ((now or datetime.now()) - timedelta(days=days_ago)).strftime("%d.%m.%Y %H:%M:%S.%f")[:-3]


def _subs(days_from_now, now=NOW):
    return (now + timedelta(days=days_from_now)).strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "test.db")
    monkeypatch.setattr(db_module, "DB_PATH", path)
    db_module.init_db(db_path=path)
    return path


def _sql(db_path, statement, params=()):
    conn = sqlite3.connect(db_path)
    conn.execute(statement, params)
    conn.commit()
    conn.close()


def _event(db_path, event_id, status=2, days_ago=1, limit=None, chat_id=HUB, waitlist="[]", now=None):
    _sql(db_path,
         "INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon, event_status, "
         "going_data, notgoing_data, counters_data, kicked_data, created_date, total_limit, waitlist_data) "
         "VALUES (?, ?, '1', ?, '👍', '❌', ?, '[]', '[]', '{}', '[]', ?, ?, ?)",
         (event_id, chat_id, event_id, status, _stamp(days_ago, now), limit, waitlist))


def _going(db_path, event_id, n, guests_on_first=0, chat_id=HUB):
    for i in range(n):
        _sql(db_path, "INSERT INTO event_users (event_id, chat_id, user_id, username, status, guests) "
                      "VALUES (?, ?, ?, ?, 'going', ?)",
             (event_id, chat_id, f"{event_id}-u{i}", f"u{i}", guests_on_first if i == 0 else 0))


def _notgoing(db_path, event_id, n, chat_id=HUB):
    for i in range(n):
        _sql(db_path, "INSERT INTO event_users (event_id, chat_id, user_id, username, status, guests) "
                      "VALUES (?, ?, ?, ?, 'notgoing', 0)", (event_id, chat_id, f"{event_id}-n{i}", f"n{i}"))


class TestEventBreakdown:
    def test_status_counts_and_completion_rate(self, db_path):
        for i in range(3):
            _event(db_path, f"c{i}", status=2)
        _event(db_path, "x", status=-1)
        _event(db_path, "o", status=0)
        _event(db_path, "v", status=1)
        b = handlers._compute_event_breakdown(HUB, "all")
        assert (b["created"], b["closed"], b["cancelled"], b["open"]) == (6, 3, 1, 2)
        assert b["completed_pct"] == 50

    def test_period_filter_matches_the_rest_of_stats(self, db_path):
        _event(db_path, "recent", status=2, days_ago=5)
        _event(db_path, "old", status=2, days_ago=40)
        assert handlers._compute_event_breakdown(HUB, "30d")["created"] == 1
        assert handlers._compute_event_breakdown(HUB, "all")["created"] == 2

    def test_guest_share_and_not_going(self, db_path):
        _event(db_path, "e1")
        _going(db_path, "e1", 2, guests_on_first=1)
        _notgoing(db_path, "e1", 3)
        b = handlers._compute_event_breakdown(HUB, "all")
        assert (b["headcount"], b["guests"], b["guests_pct"], b["notgoing"]) == (3, 1, 33, 3)

    def test_no_events_gives_no_percentages(self, db_path):
        b = handlers._compute_event_breakdown(HUB, "all")
        assert b["created"] == 0 and b["completed_pct"] is None and b["guests_pct"] is None and b["limited"] is None

    def _limited_scenario(self, db_path):
        _event(db_path, "full", limit=10); _going(db_path, "full", 10)                       # exactly full
        wl = '[{"user_id": "1"}, {"user_id": "2"}]'
        _event(db_path, "queued", limit=10, waitlist=wl); _going(db_path, "queued", 5)        # people never got in
        _event(db_path, "quiet", limit=10); _going(db_path, "quiet", 8)                       # never full
        _event(db_path, "nolimit"); _going(db_path, "nolimit", 30)                            # must be ignored here

    def test_limit_fill_hits_and_waitlist(self, db_path):
        self._limited_scenario(db_path)
        lim = handlers._compute_event_breakdown(HUB, "all")["limited"]
        assert lim["events"] == 3
        assert lim["avg_fill_pct"] == 77              # (100 + 50 + 80) / 3
        assert lim["avg_headcount"] == 7.7 and lim["avg_limit"] == 10.0
        assert lim["hit_limit"] == 2                  # "full" by headcount, "queued" because it had a waitlist
        assert (lim["waitlist_joined"], lim["waitlist_waiting"]) == (2, 2)

    @needs_json1
    def test_join_counter_can_exceed_the_people_still_waiting(self, db_path):
        _event(db_path, "e1", limit=5); _going(db_path, "e1", 5)
        _sql(db_path, "UPDATE events SET waitlist_data = ? WHERE event_id = 'e1'",
             ('[{"user_id": "1"}, {"user_id": "2"}, {"user_id": "3"}, {"user_id": "4"}]',))
        _sql(db_path, "UPDATE events SET waitlist_data = ? WHERE event_id = 'e1'", ('[{"user_id": "4"}]',))  # 3 promoted
        lim = handlers._compute_event_breakdown(HUB, "all")["limited"]
        assert (lim["waitlist_joined"], lim["waitlist_waiting"]) == (4, 1)

    def test_text_shows_everything(self, db_path):
        self._limited_scenario(db_path)
        _event(db_path, "x", status=-1)
        _event(db_path, "o", status=0)
        text = handlers._build_stats_text(HUB, "all")
        for expected in (
            "Events amount: 6", "Events closed: 4 \\(67% completed\\)", "Events cancelled: 1", "Events open now: 1",
            "Guests: 0% of attendance \\(0\\)", "Not going: 0",
            "Events with a limit: 3 \\(avg fill 77%, 7\\.7 of 10 spots\\)", "Hit the limit: 2 of 3",
            "Waitlist: 2 joined, 2 still waiting at close",
        ):
            assert expected in text, expected

    def test_events_without_a_limit_show_no_limit_block(self, db_path):
        _event(db_path, "e1"); _going(db_path, "e1", 4)
        text = handlers._build_stats_text(HUB, "all")
        assert "Events with a limit" not in text and "Waitlist" not in text


@needs_json1
class TestWaitlistJoinTrigger:
    def _joined(self, db_path):
        conn = sqlite3.connect(db_path)
        v = conn.execute("SELECT waitlist_joined FROM events WHERE event_id = 'e1'").fetchone()[0]
        conn.close()
        return v

    def _set(self, db_path, raw):
        _sql(db_path, "UPDATE events SET waitlist_data = ? WHERE event_id = 'e1'", (raw,))

    def test_counts_growth_only(self, db_path):
        _event(db_path, "e1", status=0)
        entry = lambda n: "[" + ",".join('{"user_id": "%d"}' % i for i in range(n)) + "]"
        self._set(db_path, entry(1)); assert self._joined(db_path) == 1
        self._set(db_path, entry(3)); assert self._joined(db_path) == 3
        self._set(db_path, entry(2)); assert self._joined(db_path) == 3      # a promotion never subtracts
        self._set(db_path, "[]");     assert self._joined(db_path) == 3
        self._set(db_path, entry(1)); assert self._joined(db_path) == 4      # joining again counts again

    def test_malformed_json_neither_fails_the_update_nor_counts(self, db_path):
        _event(db_path, "e1", status=0)
        self._set(db_path, "not json at all")
        assert self._joined(db_path) == 0

    def test_null_previous_value_counts_as_empty(self, db_path):
        _event(db_path, "e1", status=0)
        _sql(db_path, "UPDATE events SET waitlist_data = NULL WHERE event_id = 'e1'")
        self._set(db_path, '[{"user_id": "1"}]')
        assert self._joined(db_path) == 1

    def test_the_projects_own_waitlist_functions_feed_it(self, db_path):
        _event(db_path, "e1", status=0)
        db_module.add_to_waitlist("e1", HUB, "Hub", "alice", "1", db_path=db_path)
        db_module.add_to_waitlist("e1", HUB, "Hub", "bob", "2", db_path=db_path)
        db_module.promote_next_from_waitlist("e1", HUB, db_path=db_path)
        assert self._joined(db_path) == 2

    def test_unrelated_updates_do_not_touch_the_counter(self, db_path):
        _event(db_path, "e1", status=0)
        _sql(db_path, "UPDATE events SET name = 'renamed', event_status = 1 WHERE event_id = 'e1'")
        assert self._joined(db_path) == 0

    def test_trigger_exists_and_init_is_idempotent(self, db_path):
        db_module.init_db(db_path=db_path)
        conn = sqlite3.connect(db_path)
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")]
        conn.close()
        assert names.count("events_count_waitlist_joins") == 1

    def test_existing_database_gets_the_column_and_the_trigger(self, tmp_path, monkeypatch):
        path = str(tmp_path / "old.db")
        monkeypatch.setattr(db_module, "DB_PATH", path)
        db_module.init_db(db_path=path)
        conn = sqlite3.connect(path)
        conn.execute("DROP TRIGGER events_count_waitlist_joins")
        conn.execute("ALTER TABLE events DROP COLUMN waitlist_joined")
        conn.commit(); conn.close()

        db_module.init_db(db_path=path)

        conn = sqlite3.connect(path)
        cols = [c[1] for c in conn.execute("PRAGMA table_info(events)")]
        triggers = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")]
        conn.close()
        assert "waitlist_joined" in cols and "events_count_waitlist_joins" in triggers


def _owner_scenario(db_path):
    """Five groups, two channels, three past stays, a handful of commands and events."""
    g = lambda cid, name, typ, end, added, role="MEMBER": _sql(
        db_path, "INSERT INTO all_groups (chat_id, chat_name, type, subs_date_end, date_bot_add, role) VALUES (?,?,?,?,?,?)",
        (cid, name, typ, end, _stamp(added, NOW) if added is not None else None, role))
    g("-1", "Alpha", "PRO", _subs(3), 2)            # active PRO, expires in 3 days
    g("-2", "Beta", "PRO", _subs(20), 20)           # active PRO, expires in 20 days
    g("-3", "Gamma_(x)", "PRO", _subs(-5), 100)     # lapsed 5 days ago - FREE in practice
    g("-4", "Delta", "PRO", _subs(-60), 100)        # lapsed long ago
    g("-5", "Eps", "FREE", None, 100, role="ADMIN")
    _sql(db_path, "INSERT INTO all_channels (chat_id, chat_name, date_bot_add, role) VALUES ('-10','C1',?,'ADMIN')", (_stamp(3, NOW),))
    _sql(db_path, "INSERT INTO all_channels (chat_id, chat_name, date_bot_add, role) VALUES ('-11','C2',?,'MEMBER')", (_stamp(200, NOW),))
    for add, rem in ((50, 4), (10, 9), (300, 250)):
        _sql(db_path, "INSERT INTO all_chats_bot_log (chat_id, date_bot_add, date_bot_remove) VALUES ('-9',?,?)",
             (_stamp(add, NOW), _stamp(rem, NOW)))
    cmd = lambda chat, command, days: _sql(
        db_path, "INSERT INTO command_log (chat_id, user_id, command, command_text, timestamp) VALUES (?,?,?,?,?)",
        (chat, "7", command, "/" + command, _stamp(days, NOW)))
    cmd("-1", "newevent", 1); cmd("-1", "newevent", 2); cmd("-1", "stats", 2)
    cmd("-3", "newevent", 20)
    cmd("555", "help", 1); cmd("555", "help", 1)    # DM: counts as a command, belongs to no group
    cmd("-5", "newevent", 40)                       # outside 30 days
    cmd("-5", "newevent", 400)                      # previous year
    _event(db_path, "eA", status=2, days_ago=1, chat_id="-1", now=NOW); _going(db_path, "eA", 3, guests_on_first=2, chat_id="-1")
    _event(db_path, "eB", status=2, days_ago=15, chat_id="-2", now=NOW); _going(db_path, "eB", 2, chat_id="-2")
    _event(db_path, "eC", status=2, days_ago=100, chat_id="-4", now=NOW); _going(db_path, "eC", 4, chat_id="-4")
    _event(db_path, "eD", status=0, days_ago=5, chat_id="-2", now=NOW)


class TestOwnerStats:
    def _stats(self, db_path):
        _owner_scenario(db_path)
        return handlers._compute_owner_stats(NOW)

    def test_pro_means_an_active_subscription(self, db_path):
        s = self._stats(db_path)
        assert (s["groups_total"], s["groups_pro"], s["groups_free"]) == (5, 2, 3), "lapsed PRO counts as FREE"

    def test_expiring_and_expired_buckets(self, db_path):
        s = self._stats(db_path)
        assert (s["expiring7"], s["expiring30"], s["expired30"]) == (1, 2, 1)

    def test_growth_and_churn(self, db_path):
        s = self._stats(db_path)
        assert (s["added7"], s["added30"]) == (2, 4)      # Alpha + C1 | + Beta + the 10-day-old stay
        assert (s["removed7"], s["removed30"]) == (1, 2)

    def test_active_and_dormant_groups(self, db_path):
        s = self._stats(db_path)
        assert (s["active7"], s["active30"], s["dormant"]) == (2, 3, 2)

    def test_events_and_people_served(self, db_path):
        s = self._stats(db_path)
        assert (s["events_created"], s["events_closed"]) == (4, 3)
        assert (s["events_created30"], s["events_closed30"]) == (3, 2)
        assert (s["served"], s["served30"]) == (11, 7)

    def test_top_commands_include_dm_commands(self, db_path):
        s = self._stats(db_path)
        assert s["top_commands"] == [("newevent", 3), ("help", 2), ("stats", 1)]

    def test_top_groups_rank_by_commands_plus_events_and_show_the_tier(self, db_path):
        s = self._stats(db_path)
        assert s["top_groups"] == [("Alpha", 3, 1, "PRO"), ("Beta", 0, 2, "PRO"), ("Gamma_(x)", 1, 0, "FREE")]

    def test_admin_rights_counts(self, db_path):
        s = self._stats(db_path)
        assert (s["groups_admin"], s["channels_admin"]) == (1, 1)

    def test_empty_database_reports_zeros_not_errors(self, db_path):
        s = handlers._compute_owner_stats(NOW)
        assert s["groups_total"] == 0 and s["top_commands"] == [] and s["top_groups"] == []
        assert "Top commands \\(30d\\): none" in handlers._build_owner_stats_text(s)

    def test_text_sections_and_markdown_escaping(self, db_path):
        text = handlers._build_owner_stats_text(self._stats(db_path))
        for expected in (
            "💳 *PRO subscriptions*", "Expiring within 7 days: 1", "Expired in the last 30 days: 1",
            "📈 *Growth*", "Net: 7d \\+1 · 30d \\+2", "🔥 *Activity*", "Active groups: 7d 2 · 30d 3 · dormant 2",
            "1\\. /newevent \\- 3", "1\\. Alpha \\- 3 commands, 1 events \\(PRO\\)",
            "3\\. Gamma\\_\\(x\\) \\- 1 commands, 0 events \\(FREE\\)",
        ):
            assert expected in text, expected

    async def test_owner_command_sends_the_extended_report_and_others_get_silence(self, db_path):
        _owner_scenario(db_path)
        for user_id, expect_reply in ((555, True), (1, False)):
            chat = make_chat(chat_id=-999, chat_type="private")
            msg = make_message(chat=chat)
            upd = make_update(chat=chat, user=make_user(user_id=user_id), message=msg)
            with patch("handlers.OWNER_USER_IDS", {555}):
                await handlers.stats_command(upd, make_context(args=["-o"]))
            assert bool(msg.reply_text.call_count) is expect_reply
            if expect_reply:
                text = msg.reply_text.call_args.args[0]
                assert "PRO subscriptions" in text and "Growth" in text and "Top groups" in text
