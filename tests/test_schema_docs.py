"""The schema documentation must match db.py: every table and column of a freshly initialised database appears in the diagram
(docs/db_sheets_schema.svg) and every table is described in README.md and docs/specs/full_index.md - found out the hard way when
the diagram still showed an old table name and lacked columns added later. Also: the indexes init_db promises exist."""
import re
import sqlite3
from pathlib import Path

import pytest

import db

SVG = Path("docs/db_sheets_schema.svg").read_text(encoding="utf-8")
README = Path("README.md").read_text(encoding="utf-8")
INDEX = Path("docs/specs/full_index.md").read_text(encoding="utf-8")


@pytest.fixture()
def conn(tmp_path):
    path = str(tmp_path / "t.db")
    db.init_db(db_path=path)
    c = sqlite3.connect(path)
    yield c
    c.close()


def tables(conn):
    return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]


def test_every_table_is_in_the_diagram_the_readme_and_the_index(conn):
    for t in tables(conn):
        assert f">{t}</text>" in SVG, f"{t}: not drawn in docs/db_sheets_schema.svg"
        assert t in README, f"{t}: not in README.md"
        assert t in INDEX, f"{t}: not in docs/specs/full_index.md"


def test_every_column_is_in_the_diagram(conn):
    missing = [f"{t}.{c}" for t in tables(conn) for c in [r[1] for r in conn.execute(f"PRAGMA table_info({t})")] if c not in SVG]
    assert missing == []


def test_the_diagram_draws_no_table_that_does_not_exist(conn):
    drawn_gray = set(re.findall(r'fill="#ffffff" dominant-baseline="central">([a-z_]+)</text>', SVG))
    # the white-on-gray headers are SQLite tables, white-on-green/blue are sheet tabs (UPPER or Capitalised names)
    assert {n for n in drawn_gray if n.islower()} - set(tables(conn)) == set()


def test_the_promised_indexes_exist(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_%'")}
    assert {"idx_events_chat", "idx_event_shares_chat", "idx_join_links_user"} <= names


def test_the_diagram_draws_the_links_between_the_new_tables_and_the_sheet_tabs():
    """actions_outbox -> Actions, join_form_responses -> JoinForm, links.token = responses.token, questions.id = responses.question_id:
    the diagram once showed these tables as islands."""
    assert ">JoinForm</text>" in SVG and ">Actions</text>" in SVG
    assert "row_json = the Actions row" in SVG and "ACTION, USER_NAME, USER_ID, DATE, CHAT_ID" in SVG
    assert ">token</text>" in SVG and "question_id = id" in SVG
    assert SVG.count("marker-end=\"url(#arrow)\"") >= 20      # 17 arrows before + the 4 added for these tables, minus none
