"""``/live``: browse the gateway's live rooms and record one with ffmpeg.

The catalogue is rendered as one nickname per row plus a page/close footer.
Every callback carries the chat owner's id because the callback handlers are
not gated -- ``CustomFilters.authorized_user`` reads ``update.chat``, which a
``CallbackQuery`` does not have -- so the owner (or a sudo user) is checked
here instead.
"""

from __future__ import annotations

from html import escape

from pyrogram import Client
from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, Message, User

from .. import LOGGER, bot_loop, task_dict, task_dict_lock
from ..core.config_manager import Config
from ..helper.download.live_record import LiveRecordTask
from ..helper.download.live_support import LiveSlots, menu_text, room_menu
from ..helper.net import matok_live
from ..helper.telegram import button_build
from ..helper.telegram.filters import CustomFilters
from ..helper.telegram.message_utils import delete_message, edit_message, send_message
from ..helper.util.bot_utils import new_task

SLOTS = LiveSlots(lambda: int(Config.MAX_LIVE_RECORDS or 0))
"""The per-user cap, shared by every recording in this process."""


def _owner_id(message: Message) -> int:
    sender = message.from_user or message.sender_chat
    if sender is None or sender.id is None:
        return 0
    return sender.id


def _menu_markup(page: matok_live.LivePage, owner: int) -> InlineKeyboardMarkup:
    buttons = button_build.ButtonMaker()
    menu = room_menu(page.rooms, page.page, page.pages, owner)
    for room in menu.rows:
        buttons.data_button(room.label, room.data)
    for footer in menu.footer:
        buttons.data_button(footer.label, footer.data, position="footer")
    return buttons.build_menu(1, f_cols=4)


def _stop_markup(owner: int, mid: int) -> InlineKeyboardMarkup:
    buttons = button_build.ButtonMaker()
    buttons.data_button("⏹ Stop", f"live stop {owner} {mid}")
    return buttons.build_menu(1)


def _empty_markup() -> InlineKeyboardMarkup:
    """An inline keyboard with no rows; Telegram removes the keyboard for one.

    ``edit_message(..., buttons=None)`` would leave the old Stop button in
    place, and a press after the recording is over would land on whatever
    status the mid has by then -- the upload.
    """
    return button_build.ButtonMaker().build_menu(1)


def _int_arg(data: list[str], index: int) -> int | None:
    try:
        return int(data[index])
    except (IndexError, ValueError):
        return None


def _recording_text(room: str, anchor_id: str) -> str:
    return (
        "<b>🔴 Recording</b>\n\n"
        f"<b>Room:</b> {escape(room)}\n"
        f"<b>Anchor:</b> <code>{escape(anchor_id)}</code>\n\n"
        "Press <b>Stop</b> to finish and upload, or reply "
        "<code>/cancel</code> to this message."
    )


@new_task
async def live_menu(_client: Client, message: Message) -> None:
    try:
        page = await matok_live.rooms(1)
    except matok_live.LiveError as error:
        await send_message(message, f"Live rooms are unavailable. {error.friendly}")
        return
    if not page.rooms:
        await send_message(message, "No live rooms are online right now.")
        return
    await send_message(
        message,
        menu_text(page.total, page.page, page.pages),
        _menu_markup(page, _owner_id(message)),
    )


@new_task
async def live_callback(client: Client, query: CallbackQuery) -> None:
    raw = query.data
    if isinstance(raw, bytes):
        raw = raw.decode()
    data = raw.split()
    pressed = query.from_user
    if pressed is None or len(data) < 3:
        await query.answer()
        return
    try:
        owner = int(data[2])
    except ValueError:
        await query.answer()
        return
    if pressed.id != owner and not await CustomFilters.is_sudo(query):
        await query.answer("Not yours!", show_alert=True)
        return
    await _dispatch(client, query, pressed, owner, data)


async def _dispatch(
    client: Client,
    query: CallbackQuery,
    pressed: User,
    owner: int,
    data: list[str],
) -> None:
    action = data[1]
    if action == "page":
        page_no = _int_arg(data, 3)
        if page_no is not None:
            await _show_page(query, owner, page_no)
        else:
            await query.answer()
    elif action == "room":
        await _start_recording(client, query, pressed, data[3])
    elif action == "stop":
        mid = _int_arg(data, 3)
        if mid is not None:
            await _stop_recording(query, mid)
        else:
            await query.answer()
    elif action == "close":
        await query.answer()
        await delete_message(query.message)
    else:
        await query.answer()


async def _show_page(query: CallbackQuery, owner: int, page_no: int) -> None:
    try:
        page = await matok_live.rooms(page_no)
    except matok_live.LiveError as error:
        await query.answer(error.friendly, show_alert=True)
        return
    await query.answer()
    if not page.rooms:
        return
    await edit_message(
        query.message,
        menu_text(page.total, page.page, page.pages),
        _menu_markup(page, owner),
    )


async def _start_recording(
    client: Client,
    query: CallbackQuery,
    user: User,
    anchor_id: str,
) -> None:
    # Charged to whoever pressed, not to the menu's owner: a sudo user can
    # start a recording from somebody else's menu, and the listener releases
    # the token under its own user_id (which is the presser).
    token = await SLOTS.claim(user.id)
    if token is None:
        limit = Config.MAX_LIVE_RECORDS
        await query.answer(
            f"You already have {limit} live recording(s) running.",
            show_alert=True,
        )
        return

    try:
        stream = await matok_live.stream(anchor_id)
    except matok_live.LiveError as error:
        await SLOTS.release(user.id, token)
        await query.answer(error.friendly, show_alert=True)
        return

    room = stream.nickname or f"anchor {anchor_id}"
    text = _recording_text(room, anchor_id)
    control = await send_message(query.message, text)
    if isinstance(control, str):
        await SLOTS.release(user.id, token)
        await query.answer("The recording controls could not be sent.", show_alert=True)
        return

    listener = LiveRecordTask(
        message=control,
        client=client,
        user=user,
        anchor_id=anchor_id,
        room_name=room,
        initial_url=stream.url,
        initial_expires=stream.expires_at,
        slots=SLOTS,
        slot=token,
    )
    markup = _stop_markup(user.id, listener.mid)
    edited = await edit_message(control, text, markup)
    if isinstance(edited, str):
        # The recording can still be stopped with /cancel live<mid>.
        LOGGER.warning(f"Could not attach the stop button to {listener.mid}: {edited}")

    bot_loop.create_task(listener.run())
    await query.answer(f"Recording {room}…")


async def _stop_recording(query: CallbackQuery, mid: int) -> None:
    async with task_dict_lock:
        status = task_dict.get(mid)
    if status is None:
        await query.answer("This recording has already finished.", show_alert=True)
        return
    await query.answer("Stopping…")
    await edit_message(
        query.message,
        "⏹ <b>Recording stopped.</b> Finalizing and uploading…",
        _empty_markup(),
    )
    await status.cancel_task()
