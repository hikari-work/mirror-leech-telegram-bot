from aiofiles.os import remove, path as aiopath
from asyncio import iscoroutinefunction

from .. import LOGGER
from ..core.config_manager import Config
from ..core.torrent_manager import TorrentManager
from ..helper.util.bot_utils import (
    bt_selection_buttons,
    new_task,
)
from ..helper.util.status_utils import get_task_by_gid, MirrorStatus
from ..helper.util.task_lookup import task_from_command, task_is_yours
from ..helper.telegram.message_utils import (
    send_message,
    send_status_message,
    delete_message,
)


@new_task
async def select(_, message):
    if not Config.BASE_URL:
        await send_message(message, "Base URL not defined!")
        return
    user_id = message.from_user.id
    msg = message.text.split()
    task = await task_from_command(
        message,
        msg[1] if len(msg) > 1 else "",
        "Reply to an active /cmd which was used to start the download or add gid along with cmd\n\n"
        + "This command mainly for selection incase you decided to select files from already added torrent. "
        + "But you can always use /cmd with arg `s` to select files before download start.",
    )
    if task is None or not await task_is_yours(message, task, user_id):
        return
    if not iscoroutinefunction(task.status):
        await send_message(message, "The task have finished the download stage!")
        return
    if await task.status() not in [
        MirrorStatus.STATUS_DOWNLOAD,
        MirrorStatus.STATUS_PAUSED,
        MirrorStatus.STATUS_QUEUEDL,
    ]:
        await send_message(
            message,
            "Task should be in download or pause (incase message deleted by wrong) or queued status (incase you have used torrent file)!",
        )
        return
    if task.name().startswith("[METADATA]") or task.name().startswith("Trying"):
        await send_message(message, "Try after downloading metadata finished!")
        return

    try:
        await task.update()
        id_ = task.hash() if task.listener.is_qbit else task.gid()
        # A task still sitting in the bot's own queue has nothing to pause; it
        # only needs the id, for the selection buttons below.
        if not task.queued:
            if task.listener.is_qbit:
                await TorrentManager.qbittorrent.torrents.stop([id_])
            else:
                try:
                    await TorrentManager.aria2.forcePause(id_)
                except Exception as e:
                    LOGGER.error(
                        f"{e} Error in pause, this mostly happens after abuse aria2"
                    )
        task.listener.select = True
    except Exception:
        await send_message(message, "This is not a bittorrent task!")
        return

    SBUTTONS = bt_selection_buttons(id_)
    msg = "Your download paused. Choose files then press Done Selecting button to resume downloading."
    await send_message(message, msg, SBUTTONS)


@new_task
async def confirm_selection(_, query):
    user_id = query.from_user.id
    data = query.data.split()
    message = query.message
    task = await get_task_by_gid(data[2])
    if task is None:
        await query.answer("This task has been cancelled!", show_alert=True)
        await delete_message(message)
        return
    if user_id != task.listener.user_id:
        await query.answer("This task is not for you!", show_alert=True)
    elif data[1] == "pin":
        await query.answer(data[3], show_alert=True)
    elif data[1] == "done":
        await query.answer()
        id_ = data[3]
        if hasattr(task, "seeding"):
            if task.listener.is_qbit:
                tor_info = (
                    await TorrentManager.qbittorrent.torrents.info(hashes=[id_])
                )[0]
                path = tor_info.content_path.rsplit("/", 1)[0]
                res = await TorrentManager.qbittorrent.torrents.files(id_)
                for f in res:
                    if f.priority == 0:
                        f_paths = [f"{path}/{f.name}", f"{path}/{f.name}.!qB"]
                        for f_path in f_paths:
                            if await aiopath.exists(f_path):
                                try:
                                    await remove(f_path)
                                except OSError:
                                    pass
                if not task.queued:
                    await TorrentManager.qbittorrent.torrents.start([id_])
            else:
                res = await TorrentManager.aria2_files(id_)
                for f in res:
                    if f["selected"] == "false" and await aiopath.exists(f["path"]):
                        try:
                            await remove(f["path"])
                        except OSError:
                            pass
                if not task.queued:
                    try:
                        await TorrentManager.aria2.unpause(id_)
                    except Exception as e:
                        LOGGER.error(
                            f"{e} Error in resume, this mostly happens after abuse aria2. Try to use select cmd again!"
                        )
        await send_status_message(message)
        await delete_message(message)
    else:
        await delete_message(message)
        await task.cancel_task()
