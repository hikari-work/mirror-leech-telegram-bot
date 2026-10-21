from .. import (
    queued_up,
    queued_dl,
    queue_dict_lock,
)
from ..helper.util.bot_utils import new_task
from ..helper.util.status_utils import get_task_by_gid
from ..helper.util.task_lookup import task_from_command, task_is_yours
from ..helper.telegram.bot_commands import BotCommands
from ..helper.telegram.filters import CustomFilters
from ..helper.telegram.message_utils import send_message
from ..helper.util.task_manager import start_dl_from_queued, start_up_from_queued


@new_task
async def remove_from_queue(_, message):
    user_id = message.from_user.id if message.from_user else message.sender_chat.id
    msg = message.text.split()
    status = msg[1] if len(msg) > 1 and msg[1] in ["fd", "fu"] else ""
    has_gid = (status and len(msg) > 2) or (not status and len(msg) > 1)
    task = await task_from_command(
        message,
        (msg[2] if status else msg[1]) if has_gid else "",
        f"""Reply to an active Command message which was used to start the download/upload.
<code>/{BotCommands.ForceStartCommand[0]}</code> fd (to remove it from download queue) or fu (to remove it from upload queue) or nothing to start remove it from both download and upload queue.
Also send <code>/{BotCommands.ForceStartCommand[0]} GID</code> fu|fd or obly gid to force start by removing the task rom queue!
Examples:
<code>/{BotCommands.ForceStartCommand[1]}</code> GID fu (force upload)
<code>/{BotCommands.ForceStartCommand[1]}</code> GID (force download and upload)
By reply to task cmd:
<code>/{BotCommands.ForceStartCommand[1]}</code> (force download and upload)
<code>/{BotCommands.ForceStartCommand[1]}</code> fd (force download)
""",
    )
    if task is None or not await task_is_yours(message, task, user_id):
        return
    listener = task.listener
    msg = ""
    async with queue_dict_lock:
        if status == "fu":
            listener.force_upload = True
            if listener.mid in queued_up:
                await start_up_from_queued(listener.mid)
                msg = "Task have been force started to upload!"
            else:
                msg = "Force upload enabled for this task!"
        elif status == "fd":
            listener.force_download = True
            if listener.mid in queued_dl:
                await start_dl_from_queued(listener.mid)
                msg = "Task have been force started to download only!"
            else:
                msg = "This task not in download queue!"
        else:
            listener.force_download = True
            listener.force_upload = True
            if listener.mid in queued_up:
                await start_up_from_queued(listener.mid)
                msg = "Task have been force started to upload!"
            elif listener.mid in queued_dl:
                await start_dl_from_queued(listener.mid)
                msg = "Task have been force started to download and upload will start once download finish!"
            else:
                msg = "This task not in queue!"
    if msg:
        await send_message(message, msg)


@new_task
async def remove_from_queue_callback(_, query):
    """Force-start a task from the buttons inside its status line.

    The command form above needs a message to reply to, which the status view
    does not have. The callback carries the gid instead, so the same queue
    transitions are reachable without leaving the message that reports them.
    """
    data = query.data.split()
    user_id = query.from_user.id
    task = await get_task_by_gid(data[2])
    if task is None:
        await query.answer("This task has been cancelled!", show_alert=True)
        return
    if user_id != task.listener.user_id and not await CustomFilters.is_sudo(query):
        await query.answer("This task is not for you!", show_alert=True)
        return
    listener = task.listener
    msg = ""
    async with queue_dict_lock:
        if data[1] == "fu":
            listener.force_upload = True
            if listener.mid in queued_up:
                await start_up_from_queued(listener.mid)
                msg = "Task have been force started to upload!"
            else:
                msg = "Force upload enabled for this task!"
        elif data[1] == "fd":
            listener.force_download = True
            if listener.mid in queued_dl:
                await start_dl_from_queued(listener.mid)
                msg = "Task have been force started to download only!"
            else:
                msg = "This task not in download queue!"
        else:
            listener.force_download = True
            listener.force_upload = True
            if listener.mid in queued_up:
                await start_up_from_queued(listener.mid)
                msg = "Task have been force started to upload!"
            elif listener.mid in queued_dl:
                await start_dl_from_queued(listener.mid)
                msg = (
                    "Task have been force started to download and upload will "
                    "start once download finish!"
                )
            else:
                msg = "This task not in queue!"
    if msg:
        await query.answer(msg, show_alert=True)

