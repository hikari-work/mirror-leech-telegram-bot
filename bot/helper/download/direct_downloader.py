from secrets import token_urlsafe

from ... import (
    LOGGER,
    task_dict,
    task_dict_lock,
)
from ..listeners.direct_listener import DirectListener
from ..progress.direct_status import DirectStatus
from ..progress.queue_status import QueueStatus
from ..telegram.message_utils import send_status_message
from ..util.task_manager import check_running_tasks


async def add_direct_download(listener, path):
    details = listener.link
    if not (contents := details.get("contents")):
        await listener.on_download_error("There is nothing to download!")
        return
    listener.size = details["total_size"]

    if not listener.name:
        listener.name = details["title"]
    path = f"{path}/{listener.name}"

    gid = token_urlsafe(10)
    add_to_queue, event = await check_running_tasks(listener)
    if add_to_queue:
        LOGGER.info(f"Added to Queue/Download: {listener.name}")
        async with task_dict_lock:
            task_dict[listener.mid] = QueueStatus(listener, gid, "dl")
        await listener.on_download_start()
        if listener.multi <= 1 and not listener.is_rss:
            await send_status_message(listener.message)
        await event.wait()
        if listener.is_cancelled:
            return

    a2c_opt = {"follow-torrent": "false", "follow-metalink": "false"}
    if header := details.get("header"):
        a2c_opt["header"] = header
    directListener = DirectListener(
        path, listener, a2c_opt, bunkr_lazy=details.get("bunkr_lazy", False)
    )

    async with task_dict_lock:
        task_dict[listener.mid] = DirectStatus(listener, directListener, gid)

    if add_to_queue:
        LOGGER.info(f"Start Queued Download from Direct Download: {listener.name}")
    else:
        LOGGER.info(f"Download from Direct Download: {listener.name}")
        await listener.on_download_start()
        if listener.multi <= 1 and not listener.is_rss:
            await send_status_message(listener.message)

    await directListener.download(contents)
