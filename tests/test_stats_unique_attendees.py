"""/stats -> Users shows how many DIFFERENT people came to at least one closed event in the period."""
import sqlite3
import sys

sys.path.insert(0, ".")
import handlers
from tests.test_stats_extended import _event, HUB

CHILD = "-200"


def attend(db_path, event_id, user_id, username="u", *, status="going", guests=0, chat_id=HUB):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO event_users (event_id, chat_id, user_id, username, status, guests) VALUES (?, ?, ?, ?, ?, ?)",
                 (event_id, chat_id, str(user_id), username, status, guests))
    conn.commit()
    conn.close()


class TestCount:
    def test_nobody_yet(self, db_path):
        assert handlers._count_unique_attendees(HUB, "all") == 0

    def test_someone_at_many_events_counts_once(self, db_path):
        for ev in ("a", "b", "c"):
            _event(db_path, ev)
            attend(db_path, ev, 1, "ivan")
        attend(db_path, "a", 2, "olena")
        assert handlers._count_unique_attendees(HUB, "all") == 2

    def test_only_people_who_actually_attended_count(self, db_path):
        _event(db_path, "a")
        attend(db_path, "a", 1, "came")
        attend(db_path, "a", 2, "declined", status="notgoing")
        attend(db_path, "a", 3, "removed", status="kicked", guests=2)
        assert handlers._count_unique_attendees(HUB, "all") == 1

    def test_guests_are_not_people(self, db_path):
        _event(db_path, "a")
        attend(db_path, "a", 1, "host", guests=5)
        assert handlers._count_unique_attendees(HUB, "all") == 1

    def test_only_closed_events_are_visited_events(self, db_path):
        _event(db_path, "closed", status=2); attend(db_path, "closed", 1)
        _event(db_path, "open", status=0); attend(db_path, "open", 2)
        _event(db_path, "verify", status=1); attend(db_path, "verify", 3)
        _event(db_path, "cancelled", status=-1); attend(db_path, "cancelled", 4)
        assert handlers._count_unique_attendees(HUB, "all") == 1

    def test_people_of_shared_chats_count_and_are_not_doubled_across_chats(self, db_path):
        _event(db_path, "a")
        attend(db_path, "a", 1, "hubber", chat_id=HUB)
        attend(db_path, "a", 2, "kid", chat_id=CHILD)
        _event(db_path, "b")
        attend(db_path, "b", 2, "kid", chat_id=HUB)           # same person, other event, other chat
        assert handlers._count_unique_attendees(HUB, "all") == 2

    def test_the_period_limits_which_events_count(self, db_path):
        _event(db_path, "recent", days_ago=5); attend(db_path, "recent", 1)
        _event(db_path, "old", days_ago=60); attend(db_path, "old", 2); attend(db_path, "old", 1)
        assert handlers._count_unique_attendees(HUB, "30d") == 1
        assert handlers._count_unique_attendees(HUB, "all") == 2

    def test_placeholder_ids_of_unresolved_people_count_and_empty_ones_do_not(self, db_path):
        _event(db_path, "a")
        attend(db_path, "a", "typedname", "typedname")
        attend(db_path, "a", "", "noid")
        assert handlers._count_unique_attendees(HUB, "all") == 1

    def test_other_hubs_events_are_not_mixed_in(self, db_path):
        _event(db_path, "mine"); attend(db_path, "mine", 1)
        _event(db_path, "theirs", chat_id="-999"); attend(db_path, "theirs", 2, chat_id="-999")
        assert handlers._count_unique_attendees(HUB, "all") == 1

    def test_more_events_than_one_sql_query_can_take(self, db_path):
        conn = sqlite3.connect(db_path)
        for i in range(1200):
            conn.execute(
                "INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon, event_status, going_data, "
                "notgoing_data, counters_data, kicked_data, created_date) VALUES (?, ?, '1', 'x', 'g', 'n', 2, '[]', '[]', '{}', '[]', ?)",
                (f"e{i}", HUB, "01.10.2026 12:00:00.000"))
            conn.execute("INSERT INTO event_users (event_id, chat_id, user_id, username, status, guests) VALUES (?, ?, ?, 'u', 'going', 0)",
                         (f"e{i}", HUB, f"p{i % 1000}"))                 # 1000 distinct people over 1200 events
        conn.commit()
        conn.close()
        assert handlers._count_unique_attendees(HUB, "all") == 1000


class TestOnTheUsersScreen:
    def _plain(self, text):
        return text.replace("\\", "")

    def test_the_line_is_there_with_the_period(self, db_path):
        _event(db_path, "a"); attend(db_path, "a", 1); attend(db_path, "a", 2)
        text = self._plain(handlers._build_top_users_text(HUB, "all", "Football"))
        assert "Unique attendees:* 2" in text
        assert text.index("Unique attendees") < text.index("Most events attended")

    def test_it_follows_the_requested_period(self, db_path):
        _event(db_path, "recent", days_ago=5); attend(db_path, "recent", 1)
        _event(db_path, "old", days_ago=60); attend(db_path, "old", 2)
        assert "Unique attendees:* 1" in self._plain(handlers._build_top_users_text(HUB, "30d", "G"))
        assert "Unique attendees:* 2" in self._plain(handlers._build_top_users_text(HUB, "all", "G"))

    def test_with_no_data_it_says_zero_and_the_rest_still_says_no_data(self, db_path):
        text = self._plain(handlers._build_top_users_text(HUB, "all", "G"))
        assert "Unique attendees:* 0" in text and "No data for this period" in text

    def test_the_message_stays_valid_markdown(self, db_path):
        import re
        from tests.test_help_audit import markdown_v2_problems
        _event(db_path, "a"); attend(db_path, "a", 1, "a_b*c")
        text = handlers._build_top_users_text(HUB, "all", "Foot_ball (A+B)")
        text = re.sub(r"\[[^\]]*\]\(tg://[^)]*\)", "name", text)          # mention links are legitimate MarkdownV2
        assert markdown_v2_problems(text) == []


class TestHelpMentionsIt:
    async def test_the_utility_help_says_the_users_screen_shows_unique_attendees(self, db_path):
        from tests.test_help_audit import _gather, _make_pro
        _make_pro(db_path)
        text = (await _gather(1))["help_utility"].replace("\\", "")
        assert "unique attendees" in text and "top guest-inviters" not in text
