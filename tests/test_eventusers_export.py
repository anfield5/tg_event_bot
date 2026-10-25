"""Save & Close writes the EventUsers tab: every person who is going, from the hub AND from every other chat
that holds participants of the event - once each."""
import json
import sys
from unittest.mock import AsyncMock, patch

sys.path.insert(0, ".")
import handlers
from tests.helpers import make_bot, make_callback_update, make_context, make_user
from tests.test_handlers_async import FakeSpreadsheet, MAIN_CHAT, insert_event, insert_event_user


async def save_and_close(db_path):
    bot, ctx = make_bot(), None
    ctx = make_context(bot=bot)
    upd = make_callback_update("save_ev1", chat_id=int(MAIN_CHAT), user=make_user(user_id=9, username="admin"))
    sync = AsyncMock()
    with patch("event_engine.get_sheet_for_chat", new_callable=AsyncMock), \
         patch("event_engine.open_spreadsheet", new_callable=AsyncMock, return_value=FakeSpreadsheet()), \
         patch("event_engine.sync_event_users_sheet", sync):
        await handlers.button_handler(upd, ctx)
    sync.assert_called_once()
    return sync.call_args.args[2]


def event(db_path):
    insert_event(db_path, event_id="ev1", chat_id=MAIN_CHAT, event_status=1, going=json.dumps(["alice (1)"]))


class TestWhoIsExported:
    async def test_the_hub_every_shared_chat_and_a_chat_that_is_not_a_share(self, db_path):
        event(db_path)
        insert_event_user(db_path, event_id="ev1", chat_id="-200", user_id="2", username="bob", status="going")
        insert_event_user(db_path, event_id="ev1", chat_id="-300", user_id="3", username="carol", status="going")
        insert_event_user(db_path, event_id="ev1", chat_id="-400", user_id="5", username="eve", status="going")   # a monitored chat, never shared
        assert sorted(await save_and_close(db_path)) == ["1", "2", "3", "5"]

    async def test_only_people_who_are_going(self, db_path):
        event(db_path)
        insert_event_user(db_path, event_id="ev1", chat_id="-200", user_id="2", username="bob", status="notgoing")
        insert_event_user(db_path, event_id="ev1", chat_id="-200", user_id="6", username="kicked_guy", status="kicked")
        assert await save_and_close(db_path) == ["1"]

    async def test_a_person_going_in_two_chats_is_written_once(self, db_path):
        event(db_path)
        insert_event_user(db_path, event_id="ev1", chat_id="-200", user_id="1", username="alice", status="going")
        ids = await save_and_close(db_path)
        assert ids == ["1"]

    async def test_someone_typed_in_by_name_is_written_by_that_name(self, db_path):
        insert_event(db_path, event_id="ev1", chat_id=MAIN_CHAT, event_status=1)          # already migrated: everyone is in event_users
        insert_event_user(db_path, event_id="ev1", chat_id=MAIN_CHAT, user_id="1", username="alice", status="going")
        insert_event_user(db_path, event_id="ev1", chat_id=MAIN_CHAT, user_id="typedname", username="typedname", status="going")
        assert sorted(await save_and_close(db_path)) == ["1", "typedname"]
