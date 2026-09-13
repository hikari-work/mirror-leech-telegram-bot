from collections.abc import Iterable
from html import escape
from psutil import virtual_memory, cpu_percent, disk_usage
from time import time
from typing import Any
from asyncio import gather

from ... import task_dict, task_dict_lock, bot_start_time, status_dict, DOWNLOAD_DIR
from ...core.config_manager import Config
from ..telegram.button_build import ButtonMaker

SIZE_UNITS = ["B", "KB", "MB", "GB", "TB", "PB"]


class MirrorStatus:
    STATUS_UPLOAD = "Upload"
    STATUS_DOWNLOAD = "Download"
    STATUS_QUEUEDL = "QueueDl"
    STATUS_QUEUEUP = "QueueUp"
    STATUS_PAUSED = "Pause"
    STATUS_ARCHIVE = "Archive"
    STATUS_EXTRACT = "Extract"
    STATUS_SPLIT = "Split"
    STATUS_CHECK = "CheckUp"
    STATUS_SEED = "Seed"
    STATUS_SAMVID = "SamVid"
    STATUS_CONVERT = "Convert"
    STATUS_FFMPEG = "FFmpeg"


STATUSES = {
    "ALL": "All",
    "DL": MirrorStatus.STATUS_DOWNLOAD,
    "UP": MirrorStatus.STATUS_UPLOAD,
    "QD": MirrorStatus.STATUS_QUEUEDL,
    "QU": MirrorStatus.STATUS_QUEUEUP,
    "AR": MirrorStatus.STATUS_ARCHIVE,
    "EX": MirrorStatus.STATUS_EXTRACT,
    "SD": MirrorStatus.STATUS_SEED,
    "CM": MirrorStatus.STATUS_CONVERT,
    "SP": MirrorStatus.STATUS_SPLIT,
    "SV": MirrorStatus.STATUS_SAMVID,
    "FF": MirrorStatus.STATUS_FFMPEG,
    "PA": MirrorStatus.STATUS_PAUSED,
    "CK": MirrorStatus.STATUS_CHECK,
}


def _task_status(tk: Any) -> str:
    """What *tk* last reported, without asking its tool for anything newer.

    A torrent status answers out of the info its own ``update()`` fetched; every
    other tool holds its status in process. Either way this is a read, so a caller
    that has refreshed the torrent tasks can classify and render a whole page of
    them without a round trip in between.
    """
    cached = getattr(tk, "cached_status", None)
    return cached() if cached is not None else tk.status()


async def _refresh_torrent_statuses(tasks: Iterable[Any]) -> None:
    """Ask every torrent task in *tasks* for its info, all of them at once.

    ``update()`` is the only member of the status protocol that talks to a
    server -- ``torrents.info`` for a qBittorrent task, ``tellStatus`` for an
    aria2 one -- so this is where the waiting in a status view lives, and it is
    one wait rather than one per task. Every other tool has no ``update`` at all:
    it knows its status without being asked.
    """
    await gather(*(tk.update() for tk in tasks if hasattr(tk, "update")))


async def get_task_by_gid(gid: str):
    """The task *gid* names, or None.

    Two passes, both under the lock. The first settles every task whose gid can
    be read off the object itself: all but the torrents hold theirs from
    construction, and a qBittorrent task holds a hash its server never
    reassigns. A lookup that lands there costs nothing, which is the common case
    for the qBittorrent listener -- it finds one task per torrent event, and used
    to pay a ``torrents.info`` call for every *other* torrent in the dict to do
    it.

    Whatever is left has to be asked, because its gid is what the answer moves:
    aria2 hands a followed download a new one, and the status object only learns
    it by looking. Those are refreshed in place, in the order the dict holds them
    and stopping at the first match -- which is exactly what this function did
    before the first pass existed, so a lookup that gets this far answers what it
    always did. Two passes cannot disagree about *which* task that is: a gid
    names one download, so at most one task here can match it.
    """
    async with task_dict_lock:
        unsettled = []
        for tk in task_dict.values():
            if hasattr(tk, "known_gid"):
                known = tk.known_gid()
                if known is None:
                    unsettled.append(tk)
                elif known == gid:
                    return tk
            elif tk.gid() == gid:
                return tk
        for tk in unsettled:
            await tk.update()
            if tk.gid() == gid:
                return tk
        return None


async def get_specific_tasks(status, user_id):
    if status == "All":
        if user_id:
            return [tk for tk in task_dict.values() if tk.listener.user_id == user_id]
        else:
            return list(task_dict.values())
    tasks_to_check = (
        [tk for tk in task_dict.values() if tk.listener.user_id == user_id]
        if user_id
        else list(task_dict.values())
    )
    # One round trip per torrent task, all of them in flight together, and then
    # every answer read back off the object that just fetched it. The statuses
    # used to be gathered into a list of results that the loop below matched tasks
    # against by searching that list, which was a scan per task to find something
    # it already knew.
    await _refresh_torrent_statuses(tasks_to_check)
    result = []
    for tk in tasks_to_check:
        st = _task_status(tk)
        if (st == status) or (
            status == MirrorStatus.STATUS_DOWNLOAD and st not in STATUSES.values()
        ):
            result.append(tk)
    return result


async def get_all_tasks(req_status: str, user_id):
    async with task_dict_lock:
        return await get_specific_tasks(req_status, user_id)


def get_readable_file_size(size_in_bytes):
    if not size_in_bytes:
        return "0B"

    index = 0
    while size_in_bytes >= 1024 and index < len(SIZE_UNITS) - 1:
        size_in_bytes /= 1024
        index += 1

    return f"{size_in_bytes:.2f}{SIZE_UNITS[index]}"


def get_readable_time(seconds: float):
    """ "1d2h3m4s" for *seconds*, dropping the units that would read as zero.

    Floats are welcome: most callers hand over a ``time()`` difference, and the
    parts are rounded on the way into the text anyway.
    """
    periods = [("d", 86400), ("h", 3600), ("m", 60), ("s", 1)]
    result = ""
    for period_name, period_seconds in periods:
        if seconds >= period_seconds:
            period_value, seconds = divmod(seconds, period_seconds)
            result += f"{int(period_value)}{period_name}"
    return result


def time_to_seconds(time_duration):
    try:
        parts = time_duration.split(":")
        if len(parts) == 3:
            hours, minutes, seconds = map(float, parts)
        elif len(parts) == 2:
            hours = 0
            minutes, seconds = map(float, parts)
        elif len(parts) == 1:
            hours = 0
            minutes = 0
            seconds = float(parts[0])
        else:
            return 0
        return hours * 3600 + minutes * 60 + seconds
    except Exception:
        return 0


def speed_string_to_bytes(size_text: str) -> int:
    """The byte count in a size or speed as written -- "1.5GiB", "300 kb/s".

    Whole bytes: every caller either adds it to a byte total or formats it as
    one, and two of those totals are counted in whole bytes, so the fraction
    this used to carry had nowhere to go. Text naming no unit at all answers 0.
    """
    size = 0.0
    size_text = size_text.lower()
    if "k" in size_text:
        size += float(size_text.split("k")[0]) * 1024
    elif "m" in size_text:
        size += float(size_text.split("m")[0]) * 1048576
    elif "g" in size_text:
        size += float(size_text.split("g")[0]) * 1073741824
    elif "t" in size_text:
        size += float(size_text.split("t")[0]) * 1099511627776
    elif "b" in size_text:
        size += float(size_text.split("b")[0])
    return int(size)


def get_progress_bar_string(pct):
    pct = float(pct.strip("%"))
    p = min(max(pct, 0), 100)
    cFull = int(p // 8)
    p_str = "■" * cFull
    p_str += "□" * (12 - cFull)
    return f"[{p_str}]"


async def get_readable_message(sid, is_user, page_no=1, status="All", page_step=1):
    msg = ""
    button = None

    tasks = await get_specific_tasks(status, sid if is_user else None)

    STATUS_LIMIT = Config.STATUS_LIMIT
    tasks_no = len(tasks)
    pages = (max(tasks_no, 1) + STATUS_LIMIT - 1) // STATUS_LIMIT
    if page_no > pages:
        page_no = (page_no - 1) % pages + 1
        status_dict[sid]["page_no"] = page_no
    elif page_no < 1:
        page_no = pages - (abs(page_no) % pages)
        status_dict[sid]["page_no"] = page_no
    start_position = (page_no - 1) * STATUS_LIMIT
    page = tasks[start_position : STATUS_LIMIT + start_position]

    if status == "All":
        # The "All" filter hands the dict over without reading a status off
        # anything, so the page about to be rendered is the only part of it that
        # has to be refreshed -- and it refreshes in flight, where asking each
        # task for its status inside the loop below was one round trip after
        # another. Every other status was refreshed by the filter itself.
        await _refresh_torrent_statuses(page)

    for index, task in enumerate(page, start=1):
        if status != "All":
            tstatus = status
        else:
            tstatus = _task_status(task)
        if task.listener.is_super_chat:
            msg += f"<b>{index + start_position}.<a href='{task.listener.message.link}'>{tstatus}</a>: </b>"
        else:
            msg += f"<b>{index + start_position}.{tstatus}: </b>"
        msg += f"<code>{escape(f'{task.name()}')}</code>"
        if task.listener.subname:
            msg += f"\n<i>{task.listener.subname}</i>"
        if (
            tstatus not in [MirrorStatus.STATUS_SEED, MirrorStatus.STATUS_QUEUEUP]
            and task.listener.progress
        ):
            progress = task.progress()
            msg += f"\n{get_progress_bar_string(progress)} {progress}"
            if task.listener.subname:
                subsize = f"/{get_readable_file_size(task.listener.subsize)}"
                ac = len(task.listener.files_to_proceed)
                count = f"{task.listener.proceed_count}/{ac or '?'}"
            else:
                subsize = ""
                count = ""
            msg += f"\n<b>Processed:</b> {task.processed_bytes()}{subsize}"
            if count:
                msg += f"\n<b>Count:</b> {count}"
            msg += f"\n<b>Size:</b> {task.size()}"
            msg += f"\n<b>Speed:</b> {task.speed()}"
            msg += f"\n<b>ETA:</b> {task.eta()}"
            if (
                tstatus == MirrorStatus.STATUS_DOWNLOAD
                and task.listener.is_torrent
                or task.listener.is_qbit
            ):
                try:
                    msg += f"\n<b>Seeders:</b> {task.seeders_num()} | <b>Leechers:</b> {task.leechers_num()}"
                except Exception:
                    pass
        elif tstatus == MirrorStatus.STATUS_SEED:
            msg += f"\n<b>Size: </b>{task.size()}"
            msg += f"\n<b>Speed: </b>{task.seed_speed()}"
            msg += f"\n<b>Uploaded: </b>{task.uploaded_bytes()}"
            msg += f"\n<b>Ratio: </b>{task.ratio()}"
            msg += f" | <b>Time: </b>{task.seeding_time()}"
        else:
            msg += f"\n<b>Size: </b>{task.size()}"
        msg += f"\n<b>Gid: </b><code>{task.gid()}</code>\n\n"

    if len(msg) == 0:
        if status == "All":
            return None, None
        else:
            msg = f"No Active {status} Tasks!\n\n"
    buttons = ButtonMaker()
    if not is_user:
        buttons.data_button("📜", f"status {sid} ov", position="header")
    if len(tasks) > STATUS_LIMIT:
        msg += f"<b>Page:</b> {page_no}/{pages} | <b>Tasks:</b> {tasks_no}\n"
        buttons.data_button("<<", f"status {sid} pre", position="header")
        buttons.data_button(">>", f"status {sid} nex", position="header")
    buttons.data_button("♻️", f"status {sid} ref", position="header")
    button = buttons.build_menu(8)
    msg += f"<b>CPU:</b> {cpu_percent()}% | <b>FREE:</b> {get_readable_file_size(disk_usage(DOWNLOAD_DIR).free)}"
    msg += f"\n<b>RAM:</b> {virtual_memory().percent}% | <b>UPTIME:</b> {get_readable_time(time() - bot_start_time)}"
    return msg, button
