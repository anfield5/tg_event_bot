"""An event whose hub people are still only in the old JSON columns (the post shows them, event_users doesn't) - found by the
audit / amount check and moved by scripts/migrate_legacy_hub_rosters.py."""
import json
import sqlite3
import sys

sys.path.insert(0, ".")
import scripts.audit_event_users as audit
import scripts.check_events_amount as amount
import scripts.migrate_legacy_hub_rosters as migrate
from db import event_headcount

HUB, CHILD = "-100", "-200"


def sql(db_path, statement, *params):
    conn = sqlite3.connect(db_path)
    conn.execute(statement, params)
    conn.commit()
    conn.close()


def legacy_event(db_path, hub_rows=False):
    """Hub in JSON: 2 going, Carol not going (with id), "Andr" not going (no id) with 6 guests; the child has 6 going + 1 guest."""
    sql(db_path,
        "INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon, event_status, going_data, notgoing_data, "
        "counters_data, kicked_data) VALUES ('ev32', ?, '1', 'Football #32', '⚽', '❌', 2, ?, ?, ?, '[]')",
        HUB, json.dumps(["sergiy (1)", "olezhka (2)"]), json.dumps(["carol (3)", "Andr"]), json.dumps({"Andr": 6}))
    for i in range(6):
        sql(db_path, "INSERT INTO event_users (event_id, chat_id, user_id, username, status, guests) VALUES ('ev32', ?, ?, ?, 'going', ?)",
            CHILD, str(100 + i), f"c{i}", 1 if i == 4 else 0)
    if hub_rows:
        sql(db_path, "INSERT INTO event_users (event_id, chat_id, user_id, username, status, guests) VALUES ('ev32', ?, '1', 'sergiy', 'going', 0)", HUB)


def rows(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM event_users").fetchone()[0]
    finally:
        conn.close()


class TestDetection:
    def test_the_hub_json_is_found_only_while_event_users_has_no_hub_row(self, db_path):
        legacy_event(db_path)
        conn = sqlite3.connect(db_path)
        assert audit.hub_json_unmigrated(conn, "ev32", HUB) is not None
        assert audit.hub_json_unmigrated(conn, "ev32", "-999") is not None          # no rows for that chat either
        conn.close()
        sql(db_path, "INSERT INTO event_users (event_id, chat_id, user_id, username, status, guests) VALUES ('ev32', ?, '1', 'sergiy', 'going', 0)", HUB)
        conn = sqlite3.connect(db_path)
        assert audit.hub_json_unmigrated(conn, "ev32", HUB) is None
        conn.close()

    def test_an_event_with_empty_json_has_nothing_to_find(self, db_path):
        sql(db_path, "INSERT INTO events (event_id, chat_id, message_id, name, going_icon, notgoing_icon, event_status, going_data, notgoing_data, counters_data, kicked_data) "
                     "VALUES ('e', ?, '1', 'x', 'a', 'b', 2, '[]', '[]', '{}', '[]')", HUB)
        conn = sqlite3.connect(db_path)
        assert audit.hub_json_unmigrated(conn, "e", HUB) is None
        conn.close()


class TestRoster:
    def test_the_headcount_includes_the_hub_json_and_matches_the_post(self, db_path):
        legacy_event(db_path)
        conn = sqlite3.connect(db_path)
        try:
            without = audit.effective_roster(conn, "ev32", 2, "[]")
            with_hub = audit.effective_roster(conn, "ev32", 2, "[]", HUB)
        finally:
            conn.close()
        assert without["headcount"] == 7                      # the child alone: 6 going + 1 guest
        assert with_hub["headcount"] == 15 and with_hub["hub_unmigrated"]      # + 2 going + Andr's 6 guests

    def test_looking_writes_nothing(self, db_path):
        legacy_event(db_path)
        before = rows(db_path)
        conn = sqlite3.connect(db_path)
        audit.effective_roster(conn, "ev32", 2, "[]", HUB)
        conn.close()
        assert rows(db_path) == before

    def test_the_audit_names_the_problem(self, db_path):
        legacy_event(db_path)
        conn = sqlite3.connect(db_path)
        roster = audit.effective_roster(conn, "ev32", 2, "[]", HUB)
        conn.close()
        codes = [f.code for f in audit.compare_event({"event_id": "ev32", "name": "x", "status": 2}, roster, [], None)]
        assert "HUB_NOT_MIGRATED" in codes

    def test_the_amount_check_expects_the_whole_headcount_not_the_child_alone(self, db_path):
        legacy_event(db_path)
        conn = sqlite3.connect(db_path)
        roster = audit.effective_roster(conn, "ev32", 2, "[]", HUB)
        conn.close()
        roster["breakdown"] = roster.get("breakdown")
        findings, fix = amount.check_event({"event_id": "ev32", "name": "x", "status": 2}, roster, {"AMOUNT": 7, "STATUS": "CLOSED", "_row": 2})
        assert fix == 15 and "AMOUNT_WRONG" in [f.code for f in findings]


class TestScript:
    def test_dry_run_reports_and_writes_nothing(self, db_path):
        legacy_event(db_path)
        before = rows(db_path)
        found = migrate.run(db_path)
        assert [(f["event_id"], f["before"], f["after"], f["migrated"]) for f in found] == [("ev32", 7, 15, False)]
        assert rows(db_path) == before

    def test_apply_moves_the_hub_and_the_post_number_survives(self, db_path):
        legacy_event(db_path)
        found = migrate.run(db_path, apply=True)
        assert found[0]["migrated"] and event_headcount("ev32", db_path)["total"] == 15
        conn = sqlite3.connect(db_path)
        by_name = {r[0]: r[1:] for r in conn.execute("SELECT username, status, guests, user_id FROM event_users WHERE chat_id = ?", (HUB,))}
        conn.close()
        assert by_name["sergiy"][0] == "going" and by_name["carol"][0] == "notgoing"
        assert by_name["Andr"] == ("notgoing", 6, "Andr"), "the name without an id is kept with its guests, keyed by the name"

    def test_running_it_again_finds_nothing(self, db_path):
        legacy_event(db_path)
        migrate.run(db_path, apply=True)
        assert migrate.run(db_path) == []

    def test_an_event_with_hub_rows_is_left_alone(self, db_path):
        legacy_event(db_path, hub_rows=True)
        assert migrate.run(db_path, apply=True) == []


class TestWhollyLegacyEvents:
    def test_an_event_with_no_event_users_rows_at_all_is_not_this_case(self, db_path):
        legacy_event(db_path)
        sql(db_path, "DELETE FROM event_users")
        conn = sqlite3.connect(db_path)
        assert audit.hub_json_unmigrated(conn, "ev32", HUB) is None
        conn.close()
        assert migrate.run(db_path, apply=True) == []
