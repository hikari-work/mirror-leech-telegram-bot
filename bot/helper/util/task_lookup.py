"""Finding the task a command refers to, and who is allowed to touch it.

``/cancel``, ``/select`` and ``/forcestart`` all take either a GID or a reply to
the message that started the task, and all three refuse a task that belongs to
somebody else. Each of them used to spell both out, which is three places to
edit when the wording of "not found" or the definition of "yours" changes -- and
they had already drifted apart in the usage text they print.
"""

from ... import task_dict, task_dict_lock, user_data
from ...core.config_manager import Config
from ..telegram.message_utils import send_message
from .status_utils import get_task_by_gid


async def task_from_command(message, gid="", usage=""):
    """The task *message* asks about, or None once the chat has been told why not.

    *gid* is the one the command carried, when it carried one; without it the
    task is the one whose command message this is a reply to. *usage* is what the
    user sees when the command carried neither -- every command explains its own
    arguments, so it travels in rather than living here.
    """
    if gid:
        task = await get_task_by_gid(gid)
        if task is None:
            await send_message(message, f"GID: <code>{gid}</code> Not Found.")
        return task

    if reply_to_id := message.reply_to_message_id:
        async with task_dict_lock:
            task = task_dict.get(reply_to_id)
        if task is None:
            await send_message(message, "This is not an active task!")
        return task

    await send_message(message, usage)
    return None


async def task_is_yours(message, task, user_id):
    """True when *user_id* may act on *task*, else say so in the chat and False.

    The owner and the sudo users may act on anybody's task; everyone else only
    on their own.
    """
    if (
        Config.OWNER_ID == user_id
        or task.listener.user_id == user_id
        or (user_id in user_data and user_data[user_id].get("SUDO"))
    ):
        return True
    await send_message(message, "This task is not for you!")
    return False
