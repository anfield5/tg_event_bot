"""docs/manual_tour.md must stay true: numbered without gaps, its header states the real number of
actions, and every registered command is touched by at least one step."""
from pathlib import Path
import re

TOUR = Path("docs/manual_tour.md").read_text(encoding="utf-8")
ROWS = [l for l in TOUR.split("\n") if re.match(r"^\| \d+ \|", l)]


def test_steps_are_numbered_without_gaps():
    assert [int(re.match(r"^\| (\d+) \|", r).group(1)) for r in ROWS] == list(range(1, len(ROWS) + 1))


def test_header_states_the_real_number_of_actions():
    assert f"**{len(ROWS)} actions**" in TOUR


def test_every_registered_command_is_touched_by_a_step():
    registered = set(re.findall(r'CommandHandler\("([a-z]+)"', Path("main.py").read_text(encoding="utf-8")))
    touched = set(re.findall(r"/([a-z]+)", "\n".join(ROWS)))
    assert registered - touched == set()
