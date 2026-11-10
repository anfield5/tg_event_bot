"""
Fold / unfold the lists of an event post.

An open event's post carries, under its action buttons, one button per list that can be folded:

    ▾ Going (5)    ▸ Not Going (3)    ▸ Waitlist (2)

The chevron shows the state: ▾ the list is shown, ▸ it is folded to its header line ("Not Going (3)" with no names).
Pressing the button flips it and the post is re-rendered.

Why buttons under the post and not an icon inside the text: Telegram has no pressable element inside a message's text -
only inline-keyboard buttons can be pressed. (Its own "expandable quote" folds on the viewer's device, but always to its
first three lines and always folded to begin with, so it can neither be fully folded nor leave the Going list open.)

What this means in practice
  * The state belongs to the POST, not to the person who pressed: everybody looking at that post sees the change.
    The hub's post and each shared post fold independently (db.list_view_state is keyed by event AND chat).
  * Defaults: Going shown, Not Going and Waitlist folded (db.LIST_DEFAULT_COLLAPSED).
  * A list that is hidden, shown only as a count, or empty has nothing to fold and gets no button.
  * Only an OPEN event folds: in verification, and once closed or canceled, every list is shown in full and the buttons
    are gone.
  * In a shared post "Going" folds every going block of that post, and exists only in -visible mode.
"""
from telegram import Update
from telegram.ext import ContextTypes

from db import get_connection, toggle_list_state
from event_engine import schedule_view_refresh

CALLBACK_PREFIX = "lv_"                                   # lv_<event_id>:<g|n|w>
KEYS = {"g": "going", "n": "notgoing", "w": "waitlist"}


async def listview_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    The fold / unfold button. Its own handler - not part of button_handler - for the same reason as reopen's: no
    action in there is needed, and it must answer the press exactly once. Anyone may press it: it changes how the post
    is displayed, not what it says.
    """
    query = update.callback_query
    try:
        event_id, key = query.data[len(CALLBACK_PREFIX):].rsplit(":", 1)
    except ValueError:
        await query.answer()
        return
    list_name = KEYS.get(key)
    if list_name is None:
        await query.answer()
        return

    chat_id = str(query.message.chat_id)
    with get_connection() as conn:
        event = conn.execute("SELECT chat_id, event_status FROM events WHERE event_id = ?", (event_id,)).fetchone()
        shared_here = conn.execute(
            "SELECT 1 FROM event_shares WHERE event_id = ? AND chat_id = ?", (event_id, chat_id)
        ).fetchone() if event else None

    if event is None:
        await query.answer("This event no longer exists.", show_alert=True)
        return
    if str(event[0]) != chat_id and not shared_here:
        await query.answer()                      # a post of some other event's chat - not ours to change
        return
    if event[1] != 0:
        await query.answer("This event is no longer open - the lists are shown in full.")
        return

    toggle_list_state(event_id, chat_id, list_name)
    await query.answer()
    context.application.create_task(schedule_view_refresh(context, event_id))
