from asyncio import sleep
from pyrogram.errors import FloodWait, FloodPremiumWait
from re import match as re_match
from time import time
from typing import TYPE_CHECKING

from ... import LOGGER, status_dict, task_dict_lock, intervals, DOWNLOAD_DIR
from ...core.config_manager import Config
from ...core.telegram_manager import TgClient, get_user_client
from ..util.bot_utils import SetInterval
from ..util.exceptions import TgLinkException
from ..util.status_utils import get_readable_message
from .flood import flood_seconds

if TYPE_CHECKING:
    # For ``chat_of``'s signature only; nothing here builds either one.
    from pyrogram.types import Chat, Message

STATUS_RESEND_INTERVAL = 8
"""Seconds before the status message may be re-sent to the bottom of the chat.

Inside the window it is edited in place instead. ``/status`` passes
``force=True`` because a user asking for it expects a fresh message.
"""


def chat_of(message: Message) -> Chat:
    """The chat *message* was sent in.

    pyrogram leaves ``Message.chat`` optional -- the empty placeholder it
    answers with for a message it could not fetch has no chat -- but a command
    the bot is handling and a message the bot just sent both came from one. The
    eight ``message.chat.id`` reads around the bot are not the place to argue
    about that, so the assumption is stated here.
    """
    return message.chat  # pyrefly: ignore[bad-return]


async def send_message(message, text, buttons=None, block=True):
    try:
        return await message.reply(
            text=text,
            disable_notification=True,
            reply_markup=buttons,
        )
    except FloodWait as f:
        LOGGER.warning(str(f))
        if not block:
            return str(f)
        await sleep(flood_seconds(f) * 1.2)
        return await send_message(message, text, buttons, block)
    except Exception as e:
        LOGGER.error(str(e))
        return str(e)


async def edit_message(message, text, buttons=None, block=True):
    try:
        return await message.edit(
            text=text,
            reply_markup=buttons,
        )
    except FloodWait as f:
        LOGGER.warning(str(f))
        if not block:
            return str(f)
        await sleep(flood_seconds(f) * 1.2)
        return await edit_message(message, text, buttons, block)
    except Exception as e:
        LOGGER.error(str(e))
        return str(e)


async def send_file(message, file, caption=""):
    try:
        return await message.reply_document(
            document=file, caption=caption, disable_notification=True
        )
    except FloodWait as f:
        LOGGER.warning(str(f))
        await sleep(flood_seconds(f) * 1.2)
        return await send_file(message, file, caption)
    except Exception as e:
        LOGGER.error(str(e))
        return str(e)


async def send_rss(text, chat_id, thread_id):
    try:
        return await TgClient.bot.send_message(
            chat_id=chat_id,
            text=text,
            message_thread_id=thread_id,
            disable_notification=True,
        )
    except (FloodWait, FloodPremiumWait) as f:
        LOGGER.warning(str(f))
        await sleep(flood_seconds(f) * 1.2)
        return await send_rss(text, chat_id, thread_id)
    except Exception as e:
        LOGGER.error(str(e))
        return str(e)


async def delete_message(message):
    try:
        await message.delete()
    except Exception as e:
        LOGGER.error(str(e))


async def auto_delete_message(cmd_message=None, bot_message=None):
    await sleep(60)
    if cmd_message is not None:
        await delete_message(cmd_message)
    if bot_message is not None:
        await delete_message(bot_message)


async def delete_status():
    async with task_dict_lock:
        for key, data in list(status_dict.items()):
            try:
                await delete_message(data["message"])
                del status_dict[key]
            except Exception as e:
                LOGGER.error(str(e))


async def get_tg_link_message(link, user_id=None):
    message = None
    links = []
    user_client = await get_user_client(user_id) or TgClient.user
    if link.startswith("https://t.me/"):
        private = False
        msg = re_match(
            r"https:\/\/t\.me\/(?:c\/)?([^\/]+)\/(?:\d+\/)*([0-9-]+)",
            link,
        )
    else:
        private = True
        msg = re_match(
            r"tg:\/\/openmessage\?user_id=([0-9]+)&message_id=([0-9-]+)", link
        )
        if not user_client:
            raise TgLinkException("USER_SESSION_STRING required for this private link!")
    if not msg:
        raise TgLinkException("Wrong link format!")
    chat = msg[1]
    msg_id = msg[2]
    if "-" in msg_id:
        start_id, end_id = msg_id.split("-")
        msg_id = start_id = int(start_id)
        end_id = int(end_id)
        btw = end_id - start_id
        if private:
            link = link.split("&message_id=")[0]
            links.append(f"{link}&message_id={start_id}")
            for _ in range(btw):
                start_id += 1
                links.append(f"{link}&message_id={start_id}")
        else:
            link = link.rsplit("/", 1)[0]
            links.append(f"{link}/{start_id}")
            for _ in range(btw):
                start_id += 1
                links.append(f"{link}/{start_id}")
    else:
        msg_id = int(msg_id)

    if chat.isdigit():
        chat = int(chat) if private else int(f"-100{chat}")

    if not private:
        try:
            message = await TgClient.bot.get_messages(chat_id=chat, message_ids=msg_id)
            # No message at all counts as one the bot cannot read, same as the
            # empty placeholder telegram answers with for a deleted one.
            if message is None or message.empty:
                private = True
        except Exception as e:
            private = True
            if not user_client:
                raise e

    if not private:
        return (links, "bot") if links else (message, "bot")
    elif user_client:
        try:
            user_message = await user_client.get_messages(
                chat_id=chat, message_ids=msg_id
            )
        except Exception as e:
            raise TgLinkException(
                f"You don't have access to this chat!. ERROR: {e}"
            ) from e
        if user_message is not None and not user_message.empty:
            return (links, "user") if links else (user_message, "user")
        else:
            raise TgLinkException("Private: Can't get this message!")
    else:
        raise TgLinkException("Private: Can't get this message!")


async def temp_download(msg):
    path = f"{DOWNLOAD_DIR}temp"
    return await msg.download(file_name=f"{path}/")


def cancel_status_interval(sid):
    """Stop the ticker that refreshes the status message of *sid*."""
    if obj := intervals["status"].get(sid):
        obj.cancel()
        del intervals["status"][sid]


def _drop_status(sid):
    """Forget the status message of *sid* and stop refreshing it.

    Every path that gives up on a status message -- nothing left to report,
    Telegram refusing the edit -- has to do both, and dropping the entry while
    leaving the ticker behind means it keeps firing against a message that is no
    longer tracked.
    """
    del status_dict[sid]
    cancel_status_interval(sid)


async def _render_status(sid, is_user):
    """The status text and buttons for *sid*, on the page it was left at."""
    data = status_dict[sid]
    return await get_readable_message(
        sid, is_user, data["page_no"], data["status"], data["page_step"]
    )


async def _send_status(msg, sid, text, buttons):
    """Send a status message as a reply to *msg*, or None if Telegram refused.

    ``.text`` is kept on the returned message because the update path compares
    against it to decide whether an edit is worth a request.
    """
    message = await send_message(msg, text, buttons, block=False)
    if isinstance(message, str):
        LOGGER.error(f"Status with id: {sid} haven't been sent. Error: {message}")
        return None
    message.text = text
    return message


async def update_status_message(sid, force=False):
    if intervals["stopAll"]:
        return
    async with task_dict_lock:
        if not status_dict.get(sid):
            cancel_status_interval(sid)
            return
        if not force and time() - status_dict[sid]["time"] < 3:
            return
        status_dict[sid]["time"] = time()
        text, buttons = await _render_status(sid, status_dict[sid]["is_user"])
        if text is None:
            _drop_status(sid)
            return
        if text != status_dict[sid]["message"].text:
            message = await edit_message(
                status_dict[sid]["message"], text, buttons, block=False
            )
            if isinstance(message, str):
                if message.startswith("Telegram says: [40"):
                    _drop_status(sid)
                else:
                    LOGGER.error(
                        f"Status with id: {sid} haven't been updated. Error: {message}"
                    )
                return
            status_dict[sid]["message"].text = text
            status_dict[sid]["time"] = time()


async def send_status_message(msg, user_id=0, force=False):
    if intervals["stopAll"]:
        return
    sid = user_id or msg.chat.id
    is_user = bool(user_id)
    throttled = False
    async with task_dict_lock:
        if sid in status_dict:
            # every task asks for the status message when it starts, and a bulk
            # starts a hundred of them within a few seconds. Re-sending here
            # costs a send plus a delete each time, which is what earns the
            # FloodWait that then stalls the batch, so outside the window the
            # existing message is edited in place instead of being replaced.
            if (
                not force
                and time() - status_dict[sid].get("sent_at", 0) < STATUS_RESEND_INTERVAL
            ):
                throttled = True
            else:
                text, buttons = await _render_status(sid, is_user)
                if text is None:
                    _drop_status(sid)
                    return
                old_message = status_dict[sid]["message"]
                message = await _send_status(msg, sid, text, buttons)
                if message is None:
                    return
                await delete_message(old_message)
                status_dict[sid].update(
                    {"message": message, "time": time(), "sent_at": time()}
                )
        else:
            text, buttons = await get_readable_message(sid, is_user)
            if text is None:
                return
            message = await _send_status(msg, sid, text, buttons)
            if message is None:
                return
            status_dict[sid] = {
                "message": message,
                "time": time(),
                "sent_at": time(),
                "page_no": 1,
                "page_step": 1,
                "status": "All",
                "is_user": is_user,
            }
        if not intervals["status"].get(sid) and not is_user:
            intervals["status"][sid] = SetInterval(
                Config.STATUS_UPDATE_INTERVAL, update_status_message, sid
            )
    if throttled:
        # locks are not reentrant, so the edit happens after the release
        await update_status_message(sid)
