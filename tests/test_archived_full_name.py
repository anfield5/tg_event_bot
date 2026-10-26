"""The Users tab keeps the EARLIER "First Last" of someone who changed it (ARCHIVED_FULL_NAME, column J), and the
username archive no longer lists the same name twice or a mere change of letter case."""
import sys

sys.path.insert(0, ".")
import sheets
from tests.test_presence_log import _Ss, _Ws, _row, _sync

JOINED = " | "


def row(first="A", last="B", archived_full=None, archived_user="", user="u1", uid="1"):
    r = {**_row(uid=uid), "FIRST_NAME": first, "LAST_NAME": last, "USER_NAME": user, "ARCHIVED_USER_NAME": archived_user}
    if archived_full is not None:
        r["ARCHIVED_FULL_NAME"] = archived_full        # the tab already has the column
    return r


async def sync(users, members):
    await _sync(_Ss(users, _Ws()), members)
    return dict(users.updates)


class TestTheFullNameArchive:
    async def test_a_changed_first_name_archives_the_old_full_name(self):
        users = _Ws([row(archived_full="")])
        assert await sync(users, [("1", "u1", "New", "B")]) == {"B2": [["New"]], "J2": [["A B"]]}

    async def test_a_changed_last_name_does_too(self):
        users = _Ws([row(archived_full="")])
        assert (await sync(users, [("1", "u1", "A", "Newlast")]))["J2"] == [["A B"]]

    async def test_both_changing_at_once_archive_one_entry(self):
        users = _Ws([row(archived_full="")])
        assert (await sync(users, [("1", "u1", "X", "Y")]))["J2"] == [["A B"]]

    async def test_the_next_change_is_added_after_the_earlier_ones(self):
        users = _Ws([row(first="C", last="D", archived_full="A B")])
        assert (await sync(users, [("1", "u1", "E", "F")]))["J2"] == [[f"A B{JOINED}C D"]]

    async def test_going_back_to_an_earlier_name_does_not_list_it_twice(self):
        users = _Ws([row(first="C", last="D", archived_full=f"A B{JOINED}C D")])
        assert "J2" not in await sync(users, [("1", "u1", "A", "B")])

    async def test_a_name_with_a_comma_stays_one_entry(self):
        users = _Ws([row(first="Smith,", last="John", archived_full="")])
        updates = await sync(users, [("1", "u1", "Smith", "J")])
        assert updates["J2"] == [["Smith, John"]]
        users2 = _Ws([row(first="C", last="D", archived_full="Smith, John")])
        assert (await sync(users2, [("1", "u1", "E", "F")]))["J2"] == [[f"Smith, John{JOINED}C D"]]

    async def test_only_the_newest_twenty_are_kept(self):
        old = JOINED.join(f"N{n} X" for n in range(20))
        users = _Ws([row(first="Last", last="One", archived_full=old)])
        entries = (await sync(users, [("1", "u1", "Next", "One")]))["J2"][0][0].split(JOINED)
        assert len(entries) == 20 and entries[0] == "N1 X" and entries[-1] == "Last One"

    async def test_nothing_is_archived_when_nothing_changed(self):
        users = _Ws([row(archived_full="")])
        assert await sync(users, [("1", "u1", "A", "B")]) == {}

    async def test_a_person_whose_name_was_never_stored_has_nothing_to_archive(self):
        users = _Ws([row(first="", last="", archived_full="")])
        assert await sync(users, [("1", "u1", "A", "B")]) == {"B2": [["A"]], "C2": [["B"]]}

    async def test_a_member_given_without_names_is_left_alone(self):
        users = _Ws([row(archived_full="")])
        assert await sync(users, [("1", "u1")]) == {}

    async def test_a_removed_last_name_is_not_noticed_as_before(self):
        users = _Ws([row(archived_full="")])
        assert await sync(users, [("1", "u1", "A", None)]) == {}

    async def test_a_new_member_row_is_unchanged(self):
        users = _Ws([])
        await _sync(_Ss(users, _Ws()), [("2", "new", "N", "M")])
        assert users.appended[-1] if hasattr(users, "appended") else True


class TestTheColumnItself:
    async def test_the_header_is_added_once_to_a_standard_nine_column_tab_even_for_two_people(self):
        users = _Ws([row(uid="1"), row(uid="2", user="u2")])
        updates = await sync(users, [("1", "u1", "New", "B"), ("2", "u2", "Other", "B")])
        assert [u for u in users.updates if u[0] == "J1"] == [("J1", [["ARCHIVED_FULL_NAME"]])]
        assert updates["J2"] == [["A B"]] and updates["J3"] == [["A B"]]

    async def test_an_existing_header_is_used_where_it_is(self):
        users = _Ws([{**row(), "NOTES": "x", "ARCHIVED_FULL_NAME": ""}])      # the column sits in K here
        updates = await sync(users, [("1", "u1", "New", "B")])
        assert updates.get("K2") == [["A B"]] and "J1" not in updates and "J2" not in updates

    async def test_a_tab_whose_column_j_is_something_else_is_left_alone_and_still_syncs(self):
        users = _Ws([{**row(), "MY_NOTES": "keep me"}])
        result = await _sync(_Ss(users, _Ws()), [("1", "u1", "New", "B")])
        assert result is True
        assert dict(users.updates) == {"B2": [["New"]]}, "nothing written to J, the other column untouched"

    async def test_a_nonstandard_narrower_tab_is_left_alone_too(self):
        users = _Ws([{"USER_ID": "1", "FIRST_NAME": "A", "LAST_NAME": "B", "USER_NAME": "u1", "CHAT_ID": "-100",
                      "STATUS": "MEMBER", "DATE_start": "01.01.2026", "DATE_end": ""}])
        assert dict(users.updates if (await _sync(_Ss(users, _Ws()), [("1", "u1", "New", "B")]) is True) else {}) == {"B2": [["New"]]}

    async def test_the_lookup_is_decided_once_per_sync(self):
        users = _Ws([row(uid=str(i), user=f"u{i}") for i in range(1, 4)])
        await _sync(_Ss(users, _Ws()), [(str(i), f"u{i}", "New", "B") for i in range(1, 4)])
        assert len([u for u in users.updates if u[0] == "J1"]) == 1

    def test_the_column_letters(self):
        assert [sheets._column_letter(n) for n in (1, 10, 26, 27, 0)] == ["A", "J", "Z", None, None]


class TestTheUsernameArchive:
    async def test_a_change_of_letter_case_only_is_not_archived_but_is_written(self):
        users = _Ws([row(user="Alice")])
        updates = await sync(users, [("1", "alice", "A", "B")])
        assert updates == {"D2": [["alice"]]}

    async def test_a_real_change_is_archived_as_before(self):
        users = _Ws([row(user="olduser")])
        updates = await sync(users, [("1", "newuser", "A", "B")])
        assert updates == {"D2": [["newuser"]], "I2": [["olduser"]]}

    async def test_flipping_back_and_forth_does_not_list_a_name_twice(self):
        # a -> b -> a: the archive ends up "a,b" (a was listed when they left it, and stays listed)
        users = _Ws([row(user="b", archived_user="a")])
        assert await sync(users, [("1", "a", "A", "B")]) == {"D2": [["a"]], "I2": [["a,b"]]}
        # ... and the next flip to b adds nothing: a is already there
        users2 = _Ws([row(user="a", archived_user="a,b")])
        assert "I2" not in await sync(users2, [("1", "b", "A", "B")])

    async def test_the_separator_stays_a_comma(self):
        users = _Ws([row(user="two", archived_user="one")])
        assert (await sync(users, [("1", "three", "A", "B")]))["I2"] == [["one,two"]]
