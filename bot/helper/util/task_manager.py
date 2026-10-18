from asyncio import Event

from ... import (
    DOWNLOAD_DIR,
    LOGGER,
    task_dict,
    queued_dl,
    queued_up,
    non_queued_up,
    non_queued_dl,
    upload_chat_of,
    task_dict_lock,
    queue_dict_lock,
)
from ...core.config_manager import Config
from .bot_utils import get_size_bytes
from .files_utils import get_path_size
from .status_utils import get_readable_file_size


def upload_chat_id(listener):
    """Chat that an upload counts against for the per-chat upload limit."""
    return str(listener.up_dest or listener.message.chat.id)


def _chat_up_count(chat_id):
    return sum(1 for mid in non_queued_up if upload_chat_of.get(mid) == chat_id)


async def check_running_tasks(listener, state="dl") -> tuple[bool, Event]:
    """Whether *listener* has to queue, and the event that will release it.

    The two answers come together: there is an event exactly when the first
    element is true, and every caller waits on it under that flag. Saying so in
    the signature keeps the eight ``await event.wait()`` sites from asking about
    a None none of them can reach.
    """
    all_limit = Config.QUEUE_ALL
    state_limit = Config.QUEUE_DOWNLOAD if state == "dl" else Config.QUEUE_UPLOAD
    event = None
    is_over_limit = False
    chat_id = upload_chat_id(listener) if state == "up" else None
    async with queue_dict_lock:
        if state == "up" and listener.mid in non_queued_dl:
            non_queued_dl.remove(listener.mid)
        if (
            (all_limit or state_limit)
            and not listener.force_run
            and not (listener.force_upload and state == "up")
            and not (listener.force_download and state == "dl")
        ):
            dl_count = len(non_queued_dl)
            up_count = len(non_queued_up)
            # Upload limit is enforced per destination chat, not globally.
            t_count = dl_count if state == "dl" else _chat_up_count(chat_id)
            is_over_limit = (
                all_limit
                and dl_count + up_count >= all_limit
                and (not state_limit or t_count >= state_limit)
            ) or (state_limit and t_count >= state_limit)
            if is_over_limit:
                event = Event()
                if state == "dl":
                    queued_dl[listener.mid] = event
                else:
                    queued_up[listener.mid] = event
                    upload_chat_of[listener.mid] = chat_id
        if not is_over_limit:
            if state == "up":
                upload_chat_of[listener.mid] = chat_id
                non_queued_up.add(listener.mid)
            else:
                non_queued_dl.add(listener.mid)

    return is_over_limit, event  # pyrefly: ignore[bad-return]


async def start_dl_from_queued(mid: int):
    queued_dl[mid].set()
    del queued_dl[mid]
    non_queued_dl.add(mid)


async def start_up_from_queued(mid: int):
    queued_up[mid].set()
    del queued_up[mid]
    non_queued_up.add(mid)


async def start_from_queued():
    all_limit = Config.QUEUE_ALL
    up_limit = Config.QUEUE_UPLOAD
    dl_limit = Config.QUEUE_DOWNLOAD
    async with queue_dict_lock:
        # Uploads: limit is per destination chat, not global.
        if queued_up:
            for mid in list(queued_up.keys()):
                if all_limit and len(non_queued_dl) + len(non_queued_up) >= all_limit:
                    break
                chat_id = upload_chat_of.get(mid)
                if up_limit and _chat_up_count(chat_id) >= up_limit:
                    continue
                await start_up_from_queued(mid)

        # Downloads: unchanged global limit behaviour.
        if queued_dl:
            for mid in list(queued_dl.keys()):
                if all_limit and len(non_queued_dl) + len(non_queued_up) >= all_limit:
                    break
                if dl_limit and len(non_queued_dl) >= dl_limit:
                    break
                await start_dl_from_queued(mid)


async def _user_tasks(user_id: int) -> list:
    """The listeners of every task *user_id* holds.

    Read from ``task_dict``, where downloading, uploading and queued tasks all
    live, so one pass covers the whole lifetime of a task without a second
    ledger. Taken under the lock and returned whole: the callers below go on to
    measure these against the disk, which is no time to be holding it.
    """
    async with task_dict_lock:
        return [
            status.listener
            for status in task_dict.values()
            if status.listener.user_id == user_id
        ]


async def _tasks_size(listeners) -> int:
    """What *listeners* add up to in bytes.

    A task's bytes are ``listener.size`` where the downloader has already
    settled them -- a link's metadata, or a downloader's own running count.
    Where that is still 0 its own directory is measured instead, because aria2
    and qbittorrent leave ``listener.size`` at 0 until the download ends:
    without that fallback a user holding nothing but torrents would measure as
    0 and no size cap would ever see them.
    """
    total = 0
    for listener in listeners:
        total += listener.size or await get_path_size(f"{DOWNLOAD_DIR}{listener.mid}")
    return total


def _size_cap_bytes() -> int:
    """``USER_QUEUE_SIZE_MAX`` in bytes, or 0 when there is no usable cap.

    The setting is written the way a person would say it -- "100GB", "1TB",
    "500MB" -- or as a plain byte count, for which 0 is the setting's own "off".
    A value the bot cannot make sense of counts as no cap too, and is said out
    loud in the log rather than taken quietly: a typo that lifts the size cap is
    not something anyone notices until the disk does.
    """
    configured = Config.USER_QUEUE_SIZE_MAX
    raw = str(configured or "").strip().lower()
    if not raw:
        return 0
    if raw.isdigit():
        return int(raw)

    try:
        size = get_size_bytes(raw)
    except (ValueError, TypeError):
        size = 0
    if not size:
        LOGGER.warning(
            f"USER_QUEUE_SIZE_MAX is {configured!r}, which is not a size this"
            ' bot understands (try "100GB"); the per-user size cap is off.'
        )
    return size


async def user_limit_refusal(user_id: int) -> str | None:
    """Why *user_id* may not start another task, or None when they may.

    Two bounds, and a user has to be under both to keep adding:
    ``USER_QUEUE_MAX`` tasks, or ``USER_QUEUE_SIZE_MAX`` bytes between them.
    Either one reaching its bound turns the next task away, so the count keeps
    one person from taking the queue's slots and the size keeps them from
    taking its disk. A user may add up to the count as long as what they hold
    stays under the size, and no further once either bound is met.

    ``USER_QUEUE_MAX`` of 0 turns the whole check off, and is also the owner's
    standing exemption; an empty ``USER_QUEUE_SIZE_MAX`` drops the size side,
    which leaves the task count bounding on its own. Both are read fresh each
    call, so a change through ``/botset`` takes effect on the next task.

    The task count is answered without a size measurement when there is no
    size side, and the sizes themselves are read from the listeners where the
    downloaders already settled them, so the disk walk stays the fallback.
    """
    task_limit = Config.USER_QUEUE_MAX
    if not task_limit or user_id == Config.OWNER_ID:
        return None

    listeners = await _user_tasks(user_id)
    size_limit = _size_cap_bytes()
    size = await _tasks_size(listeners) if size_limit else 0
    if len(listeners) < task_limit and (not size_limit or size < size_limit):
        return None

    held = f"{len(listeners)} tasks"
    if size_limit:
        held += f" totalling {get_readable_file_size(size)}"
    allowed = f"{task_limit} tasks"
    if size_limit:
        allowed += f" and {get_readable_file_size(size_limit)}"
    return (
        f"You already hold {held}. One user may hold {allowed} at once --"
        " wait for one of them to finish before adding another."
    )
