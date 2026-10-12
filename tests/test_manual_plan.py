"""docs/manual_testing.md must not rot: its header numbers match its cases, and a new command can't
be added to the bot without the plan at least mentioning it."""
import re

PLAN = open("docs/manual_testing.md", encoding="utf-8").read()
CASES = re.findall(r"^- \[[ x]\] \*\*([A-K]\d+)\*\*( 🔥)?", PLAN, flags=re.M)


def test_case_ids_are_unique_and_the_header_numbers_are_true():
    ids = [c[0] for c in CASES]
    smoke = [c[0] for c in CASES if c[1]]
    assert len(ids) == len(set(ids))
    assert f"{len(ids)} cases in total - {len(smoke)} of them are smoke" in PLAN


def test_every_registered_command_is_mentioned_in_the_plan():
    registered = set(re.findall(r'CommandHandler\("([a-z]+)"', open("main.py", encoding="utf-8").read()))
    mentioned = set(re.findall(r"/([a-z]+)", PLAN))
    assert registered - mentioned == set()


def test_cases_are_spread_over_the_documented_areas():
    areas = {c[0][0] for c in CASES}
    assert areas == set("ABCDEFGHIJK")
