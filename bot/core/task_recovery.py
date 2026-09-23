"""Pick up the tasks a bot restart left behind.

A restart of the bot process is not a restart of the downloads. ``aria2c`` and
``qbittorrent-nox`` run as daemons, started before the bot and outliving every
``os.execl`` it does, so the transfers the dead process had started are still
running when the new one comes up. What the restart destroys is the *memory*:
``task_dict``, the queue registries, the listener holding the parsed command.
Nothing in either engine names the telegram message that asked for a download,
so without the ``active_tasks`` row there is no way back to it.

The shape of this module follows from one decision: a task is **adopted, never
replayed**. Nothing here re-runs ``ask_options`` (which would ask the user to
choose again), ``run_multi`` (which would chain the next task of a batch a
second time) or ``_resolve_links`` (a network round trip that may now fail for
reasons that have nothing to do with the download). The row's arguments are
re-applied to a fresh listener and the engine is asked what it still has.

What that rules out is as important as what it does. A task whose bytes are no
longer in the engine's hands -- one that had finished downloading and was in
the middle of splitting, extracting or uploading -- cannot be put back: the
subprocesses doing that work died with the bot, and re-entering
``on_download_complete`` would run the whole pipeline over files that were
already half-processed. Those are reported instead, by the incomplete-task
notifier, which is left to see the rows this pass could not place.
"""

from __future__ import annotations

from os import listdir
from os import path as ospath

from .. import DOWNLOAD_DIR, LOGGER, task_dict, task_dict_lock
from ..helper.storage.db_handler import database
from ..helper.util.task_args import load_args

# Engines whose transfers survive the restart, and so can be adopted. The rest
# -- yt-dlp, mega, telegram, the debrid links -- do their work in threads and
# subprocesses of the bot process itself, which is why nothing is left of them
# to pick up.
ADOPTABLE = frozenset({"aria2", "qbit", "direct"})

# The only ``state`` worth coming back to. A task in ``post`` or ``up`` is past
# the download: its engine job is either finished or gone, and what it was
# doing when the process died is not something the engine knows about.
STATE_DOWNLOADING = "dl"

# Where a bulk child's synthetic id starts. Bulk tasks are left to the notifier
# -- see ``_why``.
BULK_MID_FLOOR = 10**9


async def recover_tasks() -> None:
    """Rebuild every task the previous process left an engine holding.

    Runs once, at boot, in place of the ``clean_all`` that used to wipe the
    download directory and both engines on every start. Called before the
    incomplete-task notifier, which has to see the rows this pass was able to
    place *removed* -- otherwise it tells a user to send again a task that is
    already back.
    """
    rows = await database.get_active_tasks()
    if not rows:
        await _sweep(set())
        return

    recovered: set[int] = set()
    unplaced: list[tuple[dict, str]] = []
    # One representative message per chat, for the status message at the end.
    chats: dict[int, object] = {}
    for row in rows:
        mid = row["mid"]
        try:
            listener = await _rebuild(row)
        except Exception as e:
            LOGGER.error(f"Unable to rebuild task {mid}: {e}")
            listener = None
        if listener is None:
            unplaced.append((row, _why(row)))
        else:
            recovered.add(mid)
            chats.setdefault(row["cid"], listener.message)
        # Either way the row has done its job. It exists to survive the gap
        # between the crash and this pass; a second boot must not find it
        # again, and a task that could not be placed is the notifier's to
        # report rather than this pass's to retry forever.
        await database.rm_active_task(mid)

    await _sweep(recovered)
    for row, reason in unplaced:
        LOGGER.warning(f"Task {row['mid']} in chat {row['cid']} not resumed: {reason}")
    # One status message per chat, and only after every task is back: it renders
    # the whole task list, so sending it per task would show each chat a partial
    # list and pay for the privilege.
    for message in chats.values():
        await _announce(message)


async def _rebuild(row: dict):
    """Build a listener for one ``active_tasks`` row and hand it to its engine.

    ``None`` means the task could not be placed and the operator should see why;
    an exception means the row itself was unusable, which is logged where the
    caller catches it. The difference matters because a task whose command
    message has been deleted has nothing to say to anyone -- there is no chat
    left to report it into.
    """
    from ..helper.listeners.command_task import ACTIVE_TASK_SCHEMA

    data = row["data"] or {}
    if data.get("schema") != ACTIVE_TASK_SCHEMA:
        raise ValueError(f"unknown task schema {data.get('schema')!r}")
    if row["state"] != STATE_DOWNLOADING:
        return None

    engine = data.get("engine", "")
    if engine not in ADOPTABLE:
        LOGGER.info(f"Task {row['mid']}: {engine or 'unknown'} is not resumable")
        return None

    listener = await _make_listener(row, data)
    if listener is None:
        return None
    if listener.stream_upload:
        await downgrade_stream(listener)
    if engine == "qbit":
        return await _adopt_qbit(listener, row, data)
    if engine == "direct":
        return await _adopt_direct(listener, row, data)
    return await _adopt_aria2(listener, row, data)


async def _make_listener(row: dict, data: dict):
    """Rebuild the task object from its row, arguments and all.

    ``before_start`` rather than ``_apply_args`` alone: the arguments carry what
    the user typed, and ``before_start`` is what turns those into the resolved
    settings every later stage reads -- the upload destination, the split size,
    the ffmpeg commands. It is called exactly once and on a freshly built
    object, because a second call on the same one is not a no-op.

    Anything it raises -- a chat the bot is no longer in, a copy preset deleted
    in the meantime -- is a task that cannot be placed, and the caller is told
    so rather than being handed a listener with half its settings.
    """
    from ..core.telegram_manager import TgClient
    from ..modules.leech import Leech
    from ..modules.ytdlp import YtDlp

    handler = data.get("handler")
    if handler == "leech":
        cls = Leech
    elif handler == "ytdl":
        cls = YtDlp
    else:
        LOGGER.error(f"Task {row['mid']}: unknown command {handler!r}")
        return None

    message = await TgClient.bot.get_messages(row["cid"], row["cmd_msg_id"])
    if message is None:
        LOGGER.info(f"Task {row['mid']}: command message {row['cmd_msg_id']} is gone")
        return None

    listener = cls(
        TgClient.bot,
        message,
        is_qbit=data.get("engine") == "qbit",
        mid=row["mid"],
        cmd_text=data.get("cmd_text") or "",
    )
    listener._apply_args(load_args(data["args"]))
    await listener.before_start()
    return listener


async def downgrade_stream(listener) -> None:
    """Bring a recovered ``-su`` task back as an ordinary one.

    Streaming uploads each file the moment it lands and deletes it right after,
    so a restart in the middle leaves the task with some files delivered, some
    still to come, and no downloader left to hand the rest over -- the loop that
    did that died with the bot. Picking the stream back up is therefore not the
    question; what it resumes *into* is.

    Not as it stands, though. The flags a stream drops -- extract, compress,
    split, seed -- were dropped in the memory of the process that died, and a
    task rebuilt with them on would run its post-processing over a directory
    holding only the files that had not been sent yet: an archive cut from the
    tail of an album, a zip of a partial set. So the same table the live stream
    reads is applied here, and the task re-enters the ordinary pipeline, where
    the files still on disk upload and the checkpoints carry the ones delivered
    before the restart back into the report.

    The line naming those files is not decoration: they are the reason the
    resumed task sends less than the link offers, and a user who is not told
    reads that as a task that lost files.
    """
    from ..helper.upload.stream_uploader import apply_stream_policy

    listener.stream_upload = False
    apply_stream_policy(listener)

    sent = await database.get_uploaded_files(listener.mid)
    if not sent:
        return
    names = [ospath.basename(row["relpath"]) for row in sent]
    shown = ", ".join(names[:5])
    more = f" and {len(names) - 5} more" if len(names) > 5 else ""
    listener.stream_notices.append(
        f"Note: this stream task was cut short by a restart. "
        f"{len(names)} file(s) were already sent: {shown}{more}."
    )


async def _adopt_qbit(listener, row: dict, data: dict):
    """Re-attach to a torrent qBittorrent is still downloading or seeding."""
    from ..core.torrent_manager import TorrentManager
    from ..helper.download.qbit_download import qbit_tag
    from ..helper.listeners.qbit_listener import on_download_start
    from ..helper.progress.qbit_status import QbittorrentStatus

    tag = data.get("engine_tag") or qbit_tag(row["mid"])
    found = await TorrentManager.qbittorrent.torrents.info(tag=tag)
    if not found:
        return None
    torrent = found[0]
    listener.name = torrent.name
    listener.is_torrent = True
    # Nothing is started or stopped here. ``queued`` is the bot's own admission
    # queue, which is empty at boot, and the torrent's own state is read off its
    # info by the status object -- so a torrent the dead process had paused for
    # the user's file selection must not be started out from under them.
    async with task_dict_lock:
        task_dict[row["mid"]] = QbittorrentStatus(listener)
    # Registers the tag in ``qb_torrents`` and, if nothing else is watching,
    # starts the listener loop -- without which no torrent event would find this
    # task again.
    await on_download_start(tag)
    LOGGER.info(f"Resumed qBittorrent task {row['mid']}: {torrent.name}")
    return listener


async def _adopt_aria2(listener, row: dict, data: dict):
    """Re-attach to the aria2 download whose ``dir`` is this task's."""
    from ..helper.progress.aria2_status import Aria2Status

    gid = await _find_aria2_gid(data.get("engine_dir") or "")
    if gid is None:
        return None
    listener.is_torrent = await _aria2_is_torrent(gid)
    async with task_dict_lock:
        task_dict[row["mid"]] = Aria2Status(listener, gid)
    LOGGER.info(f"Resumed aria2 task {row['mid']} on gid {gid}")
    return listener


async def _find_aria2_gid(target: str) -> str | None:
    """The gid of the live download writing into *target*, if there is one.

    Matched on ``dir`` because a gid is not a handle that survives a restart: a
    magnet hands its download off to a new one the moment the metadata arrives,
    and the bot only learns the new id by asking. The directory a task was
    started with does not change, which is why it is what the row records.
    """
    from ..core.torrent_manager import TorrentManager

    if not target:
        return None
    for download in await TorrentManager.unfinished():
        if download.get("dir", "") == target:
            return download.get("gid", "") or None
    return None


async def _aria2_is_torrent(gid: str) -> bool:
    from ..core.torrent_manager import TorrentManager

    try:
        return "bittorrent" in await TorrentManager.aria2_status(gid)
    except Exception:
        return False


async def _adopt_direct(listener, row: dict, data: dict):
    """Restart the loop that walks a direct link's file list.

    A direct download is one aria2 job per file, added by a loop that does not
    outlive the bot -- so this rebuilds the loop and has it pick up the files
    the engine is still holding, and the ones already on disk, instead of
    fetching either a second time.

    That loop runs for the length of the transfer, so it is scheduled rather
    than awaited: doing it inline would hold up every task behind this one in
    the recovery pass.
    """
    from .. import bot_loop
    from ..helper.download.direct_downloader import add_direct_download

    if not isinstance(listener.link, dict) or not listener.link.get("contents"):
        return None
    target = data.get("engine_dir") or ""
    bot_loop.create_task(_run_direct(listener, target))
    LOGGER.info(f"Resumed direct download {row['mid']}: {listener.name}")
    return listener


async def _run_direct(listener, target: str) -> None:
    """``add_direct_download`` in resume mode, with its failures contained."""
    from ..helper.download.direct_downloader import add_direct_download

    try:
        await add_direct_download(listener, target, resume=True)
    except Exception as e:
        LOGGER.error(f"Resumed direct download for {listener.mid} failed: {e}")
        await listener.on_download_error(str(e))


async def _announce(message) -> None:
    """Put one chat's status message back, once, for every task recovered.

    Called after the whole pass rather than per task: ``send_status_message``
    renders the entire task list, so a chat with ten recovered tasks would show
    nine partial lists and pay for ten round trips to do it. The throttle inside
    it is what makes one call per chat enough.
    """
    from ..helper.telegram.message_utils import send_status_message

    try:
        await send_status_message(message)
    except Exception as e:
        LOGGER.error(f"Unable to send the resumed status message: {e}")


def _why(row: dict) -> str:
    """Why a task could not be placed, in the operator's words."""
    state = row.get("state")
    if state != STATE_DOWNLOADING:
        return f"it died in the {state} stage, which runs inside the bot process"
    data = row.get("data") or {}
    if data.get("multi") or data.get("multi_tag") or row["mid"] >= BULK_MID_FLOOR:
        return "it is part of a batch, whose merging state lived in the dead process"
    if data.get("engine") not in ADOPTABLE:
        return f"the {data.get('engine') or 'unknown'} engine runs inside the bot process"
    return "its engine no longer has it"


async def _sweep(keep: set[int]) -> None:
    """Clear out what the dead process left and nothing is coming back for.

    Deliberately narrower than the ``clean_all`` it replaces, which removed
    every file under ``DOWNLOAD_DIR`` and every download in both engines. A
    resumed task's directory is exactly what must not be touched, and neither
    must anything whose name is not a task id -- ``create_thumb`` writes into
    ``{DOWNLOAD_DIR}thumbnails/``, so a sweep that took the whole directory
    would delete every generated thumbnail on every boot.
    """
    from ..core.torrent_manager import TorrentManager

    await _sweep_download_dir(keep)
    try:
        # ``purgeDownloadResult`` rather than ``remove_all``: it forgets the
        # finished and failed downloads -- the ones no task is waiting on any
        # more -- and leaves the running ones alone.
        await TorrentManager.aria2.purgeDownloadResult()
    except Exception as e:
        LOGGER.error(f"Unable to purge aria2 results: {e}")
    await _sweep_torrents(keep)


async def _sweep_download_dir(keep: set[int]) -> None:
    """Remove the directories of tasks that are not being resumed.

    A name that is not a number is not a task id, so it stays where it is. A
    number no row claimed belongs to a task that finished, failed or was
    cancelled while the bot was down.
    """
    from ..helper.util.shutil_helper import rmtree

    for name in await _listdir(DOWNLOAD_DIR):
        if not name.isdigit() or int(name) in keep:
            continue
        try:
            await rmtree(ospath.join(DOWNLOAD_DIR, name), ignore_errors=True)
        except Exception as e:
            LOGGER.error(f"Unable to remove {name} from the download directory: {e}")


async def _sweep_torrents(keep: set[int]) -> None:
    """Delete the torrents of tasks no row came back for.

    The rows are what recovery reads, so a torrent whose tag is not one of them
    belongs to a task that ended -- normally, or while the bot was down. Its
    files are in the download directory that was just swept, which is why this
    runs after it rather than before.
    """
    from ..core.torrent_manager import TorrentManager

    try:
        torrents = await TorrentManager.qbittorrent.torrents.info()
    except Exception as e:
        LOGGER.error(f"Unable to list torrents while sweeping: {e}")
        return
    for torrent in torrents:
        tag = torrent.tags[0] if torrent.tags else ""
        if tag.isdigit() and int(tag) in keep:
            continue
        try:
            await TorrentManager.qbittorrent.torrents.delete([torrent.hash], True)
            if tag:
                await TorrentManager.qbittorrent.torrents.delete_tags([tag])
        except Exception as e:
            LOGGER.error(f"Unable to remove torrent {torrent.hash}: {e}")


async def _listdir(path: str) -> list[str]:
    """What is in *path*, or nothing if it is not there yet."""
    from ..helper.util.bot_utils import sync_to_async

    if not ospath.isdir(path):
        return []
    return await sync_to_async(listdir, path)
