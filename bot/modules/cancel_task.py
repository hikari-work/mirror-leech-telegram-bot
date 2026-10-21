from asyncio import sleep

from .. import task_dict, task_dict_lock, multi_tags
from ..helper.util.bot_utils import new_task
from ..helper.util.status_utils import (
    get_all_tasks,
    get_task_by_gid,
    MirrorStatus,
)
from ..helper.util.task_lookup import task_from_command, task_is_yours
from ..helper.telegram import button_build
from ..helper.telegram.bot_commands import BotCommands
from ..helper.telegram.button_build import ButtonMaker
from ..helper.telegram.filters import CustomFilters
from ..helper.telegram.message_utils import (
    send_message,
    auto_delete_message,
    delete_message,
    edit_message,
    message_text,
)


@new_task
async def cancel(_, message):
    user_id = message.from_user.id if message.from_user else message.sender_chat.id
    msg = message_text(message).split()
    gid = msg[1] if len(msg) > 1 else ""
    if gid in multi_tags:
        # a bulk's tag, not a task's gid; only its owner (or a sudo) may stop it
        if multi_tags[gid] != user_id and not await CustomFilters.is_sudo(message):
            await send_message(message, "Not Yours!")
            return
        del multi_tags[gid]
        return
    task = await task_from_command(
        message,
        gid,
        "Reply to an active Command message which was used to start the download"
        f" or send <code>/{BotCommands.CancelTaskCommand[0]} GID</code> to cancel it!",
    )
    if task is None or not await task_is_yours(message, task, user_id):
        return
    obj = task.task()
    await obj.cancel_task()


@new_task
async def cancel_updates(_, query):
    data = query.data.split()
    user_id = query.from_user.id
    if data[1] == "close":
        await query.answer()
        await delete_message(query.message)
        return
    if data[1] == "multi":
        tag = data[2]
        if tag not in multi_tags:
            await query.answer("Already Stopped/Finished!", show_alert=True)
            return
        if multi_tags[tag] != user_id and not await CustomFilters.is_sudo(query):
            await query.answer("Not Yours!", show_alert=True)
            return
        del multi_tags[tag]
        await query.answer("Stopped!", show_alert=True)
        return

    gid = data[2]
    task = await get_task_by_gid(gid)
    if task is None:
        await query.answer("Task already cancelled or finished!", show_alert=True)
        return
    if user_id != task.listener.user_id and not await CustomFilters.is_sudo(query):
        await query.answer("Not Yours!", show_alert=True)
        return
    if data[1] == "canconf":
        await query.answer()
        button = ButtonMaker()
        button.data_button("Yes", f"cancel conf {gid}", style="green")
        button.data_button("No", "cancel close", style="red")
        res = await send_message(
            query.message,
            "Are you sure you want to cancel this task?",
            buttons=button.build_menu(2),
        )
        await auto_delete_message(res)
        return
    await query.answer()
    await delete_message(query.message)
    obj = task.task()
    await obj.cancel_task()


async def cancel_all(status, user_id):
    matches = await get_all_tasks(status.strip(), user_id)
    if not matches:
        return False
    for task in matches:
        obj = task.task()
        await obj.cancel_task()
        await sleep(2)
    return True


def create_cancel_buttons(is_sudo, user_id=""):
    buttons = button_build.ButtonMaker()
    buttons.data_button(
        "Downloading", f"canall ms {MirrorStatus.STATUS_DOWNLOAD} {user_id}"
    )
    buttons.data_button(
        "Uploading", f"canall ms {MirrorStatus.STATUS_UPLOAD} {user_id}"
    )
    buttons.data_button("Seeding", f"canall ms {MirrorStatus.STATUS_SEED} {user_id}")
    buttons.data_button("Splitting", f"canall ms {MirrorStatus.STATUS_SPLIT} {user_id}")
    buttons.data_button(
        "Extracting", f"canall ms {MirrorStatus.STATUS_EXTRACT} {user_id}"
    )
    buttons.data_button(
        "Archiving", f"canall ms {MirrorStatus.STATUS_ARCHIVE} {user_id}"
    )
    buttons.data_button(
        "QueuedDl", f"canall ms {MirrorStatus.STATUS_QUEUEDL} {user_id}"
    )
    buttons.data_button(
        "QueuedUp", f"canall ms {MirrorStatus.STATUS_QUEUEUP} {user_id}"
    )
    buttons.data_button(
        "SampleVideo", f"canall ms {MirrorStatus.STATUS_SAMVID} {user_id}"
    )
    buttons.data_button(
        "ConvertMedia", f"canall ms {MirrorStatus.STATUS_CONVERT} {user_id}"
    )
    buttons.data_button("FFmpeg", f"canall ms {MirrorStatus.STATUS_FFMPEG} {user_id}")
    buttons.data_button("Paused", f"canall ms {MirrorStatus.STATUS_PAUSED} {user_id}")
    buttons.data_button("All", f"canall ms All {user_id}")
    if is_sudo:
        if user_id:
            buttons.data_button("All Added Tasks", f"canall bot ms {user_id}")
        else:
            buttons.data_button("My Tasks", f"canall user ms {user_id}")
    buttons.data_button("Close", f"canall close ms {user_id}")
    return buttons.build_menu(2)


@new_task
async def cancel_all_buttons(_, message):
    async with task_dict_lock:
        count = len(task_dict)
    if count == 0:
        await send_message(message, "No active tasks!")
        return
    is_sudo = await CustomFilters.is_sudo(message)
    button = create_cancel_buttons(is_sudo, message.from_user.id)
    can_msg = await send_message(message, "Choose tasks to cancel!", button)
    await auto_delete_message(message, can_msg)


@new_task
async def cancel_all_update(_, query):
    data = query.data.split()
    message = query.message
    reply_to = message.reply_to_message
    user_id = int(data[3]) if len(data) > 3 else ""
    is_sudo = await CustomFilters.is_sudo(query)
    if not is_sudo and user_id and user_id != query.from_user.id:
        await query.answer("Not Yours!", show_alert=True)
    else:
        await query.answer()
    if data[1] == "close":
        await delete_message(reply_to)
        await delete_message(message)
    elif data[1] == "back":
        button = create_cancel_buttons(is_sudo, user_id)
        await edit_message(message, "Choose tasks to cancel!", button)
    elif data[1] == "bot":
        button = create_cancel_buttons(is_sudo, "")
        await edit_message(message, "Choose tasks to cancel!", button)
    elif data[1] == "user":
        button = create_cancel_buttons(is_sudo, query.from_user.id)
        await edit_message(message, "Choose tasks to cancel!", button)
    elif data[1] == "ms":
        buttons = button_build.ButtonMaker()
        buttons.data_button("Yes!", f"canall {data[2]} confirm {user_id}")
        buttons.data_button("Back", f"canall back confirm {user_id}")
        buttons.data_button("Close", f"canall close confirm {user_id}")
        button = buttons.build_menu(2)
        await edit_message(
            message, f"Are you sure you want to cancel all {data[2]} tasks", button
        )
    else:
        button = create_cancel_buttons(is_sudo, user_id)
        await edit_message(message, "Choose tasks to cancel.", button)
        res = await cancel_all(data[1], user_id)
        if not res:
            await send_message(reply_to, f"No matching tasks for {data[1]}!")
