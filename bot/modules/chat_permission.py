from functools import wraps

from .. import user_data
from ..helper.util.bot_utils import update_user_ldata, new_task
from ..helper.storage.db_handler import database
from ..helper.telegram.message_utils import send_message


def _reports(action):
    """Send back whatever *action* has to say, including how it went wrong.

    All four commands ended the same two ways -- turn an unexpected error into a
    line of text, then put that line in the chat -- so they only have to decide
    what to change and what to call it.
    """

    @wraps(action)
    async def handler(client, message):
        try:
            msg = await action(client, message)
        except Exception as e:
            msg = f"Error: {e}"
        await send_message(message, msg)

    return handler


def _target_chat(message):
    """The chat, and the topic within it, that an /auth or /unauth is aimed at.

    In order: the argument (``chat_id`` or ``chat_id|thread_id``), the sender of
    the replied-to message, or the chat the command itself was sent in -- and if
    that was inside a topic, only that topic.
    """
    thread_id = None
    msg = message.text.split()
    if len(msg) > 1:
        if "|" in msg:
            chat_id, thread_id = list(map(int, msg[1].split("|")))
        else:
            chat_id = int(msg[1].strip())
    elif (
        reply_to := message.reply_to_message
    ) and reply_to.id != message.message_thread_id:
        chat_id = (
            reply_to.from_user.id if reply_to.from_user else reply_to.sender_chat.id
        )
    else:
        if message.topic_message:
            thread_id = message.message_thread_id
        chat_id = message.chat.id
    return chat_id, thread_id


def _target_user(message):
    """The user an /addsudo or /rmsudo is aimed at, or "" when none was named."""
    msg = message.text.split()
    if len(msg) > 1:
        return int(msg[1].strip())
    if reply_to := message.reply_to_message:
        return reply_to.from_user.id if reply_to.from_user else reply_to.sender_chat.id
    return ""


@new_task
@_reports
async def authorize(_, message):
    chat_id, thread_id = _target_chat(message)
    if chat_id in user_data and user_data[chat_id].get("AUTH"):
        if (
            thread_id is not None
            and thread_id in user_data[chat_id].get("thread_ids", [])
            or thread_id is None
        ):
            return "Already Authorized!"
        if "thread_ids" in user_data[chat_id]:
            user_data[chat_id]["thread_ids"].append(thread_id)
        else:
            user_data[chat_id]["thread_ids"] = [thread_id]
        return "Authorized"

    update_user_ldata(chat_id, "AUTH", True)
    if thread_id is not None:
        update_user_ldata(chat_id, "thread_ids", [thread_id])
    await database.update_user_data(chat_id)
    return "Authorized"


@new_task
@_reports
async def unauthorize(_, message):
    chat_id, thread_id = _target_chat(message)
    if chat_id not in user_data or not user_data[chat_id].get("AUTH"):
        return (
            "Already Unauthorized! Authorized Chats added from config must be"
            " removed from config."
        )

    if thread_id is not None and thread_id in user_data[chat_id].get("thread_ids", []):
        user_data[chat_id]["thread_ids"].remove(thread_id)
    else:
        update_user_ldata(chat_id, "AUTH", False)
    await database.update_user_data(chat_id)
    return "Unauthorized"


@new_task
@_reports
async def add_sudo(_, message):
    id_ = _target_user(message)
    if not id_:
        return "Give ID or Reply To message of whom you want to Promote."
    if id_ in user_data and user_data[id_].get("SUDO"):
        return "Already Sudo!"

    update_user_ldata(id_, "SUDO", True)
    await database.update_user_data(id_)
    return "Promoted as Sudo"


@new_task
@_reports
async def remove_sudo(_, message):
    id_ = _target_user(message)
    if not id_:
        return "Give ID or Reply To message of whom you want to remove from Sudo"
    if id_ not in user_data or not user_data[id_].get("SUDO"):
        return (
            "Already Not Sudo! Sudo users added from config must be removed"
            " from config."
        )

    update_user_ldata(id_, "SUDO", False)
    await database.update_user_data(id_)
    return "Demoted"
