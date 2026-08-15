from asyncio import gather, sleep
from dataclasses import dataclass
from html import escape

from aiofiles.os import listdir, remove
from aiofiles.os import path as aiopath

from ... import (
    DOWNLOAD_DIR,
    LOGGER,
    intervals,
    non_queued_dl,
    non_queued_up,
    upload_chat_of,
    queue_dict_lock,
    queued_dl,
    queued_up,
    same_directory_lock,
    task_dict,
    task_dict_lock,
)
from ...core.config_manager import Config
from ...core.torrent_manager import TorrentManager
from ..common import TaskConfig
from ..ext_utils.db_handler import database
from ..ext_utils.files_utils import (
    clean_download,
    clean_target,
    create_recursive_symlink,
    get_path_size,
    join_files,
    move_and_merge,
    remove_excluded_files,
    remove_non_included_files,
)
from ..ext_utils.status_utils import get_readable_file_size
from ..ext_utils.task_manager import check_running_tasks, start_from_queued
from ..mirror_leech_utils.status_utils.queue_status import QueueStatus
from ..mirror_leech_utils.status_utils.telegram_status import TelegramStatus
from ..mirror_leech_utils.upload_utils.telegram_uploader import TelegramUploader
from ..telegram_helper.message_utils import (
    delete_message,
    delete_status,
    send_message,
    update_status_message,
)

NO_FILES_ERROR = (
    "No files to upload. In case you have filled EXCLUDED/INCLUDED EXTENSIONS, "
    "then check if all files have those extensions or not."
)


@dataclass(frozen=True)
class _Stage:
    """One post-processing step applied to a finished download.

    The seven steps look alike but each refreshes a different subset of the
    listener state afterwards, so the differences live here as data rather
    than as seven near-identical blocks.
    """

    guard: tuple[str, ...]
    """Listener attributes; the step runs when any of them is truthy."""

    step: str
    """Name of the coroutine on the listener that does the work."""

    pass_gid: bool = True
    set_name: bool = True
    refresh_size: bool = True
    do_clear: bool = True

    stat_before_cancel: bool = False
    """Refresh ``is_file`` before the cancel check instead of after it."""

    log: str = ""
    """Logged with the path before the step runs; empty means no log line."""

    refilter: bool = False
    """Re-apply the extension filter once the step is done."""


_STAGES = (
    _Stage(("extract",), "proceed_extract", refilter=True),
    _Stage(("ffmpeg_cmds",), "proceed_ffmpeg"),
    _Stage(
        ("name_sub",),
        "substitute",
        pass_gid=False,
        refresh_size=False,
        do_clear=False,
        log="Start Name Substitution",
    ),
    _Stage(("screen_shots",), "generate_screenshots", pass_gid=False, do_clear=False),
    _Stage(("convert_audio", "convert_video"), "convert_media"),
    _Stage(("sample_video",), "generate_sample_video"),
    _Stage(
        ("compress",),
        "proceed_compress",
        set_name=False,
        refresh_size=False,
        stat_before_cancel=True,
    ),
)


class TaskListener(TaskConfig):
    def __init__(self):
        super().__init__()

    async def clean(self):
        try:
            if st := intervals["status"]:
                for intvl in list(st.values()):
                    intvl.cancel()
            intervals["status"].clear()
            await gather(TorrentManager.aria2.purgeDownloadResult(), delete_status())
        except Exception:
            pass

    def clear(self):
        self.subname = ""
        self.subsize = 0
        self.files_to_proceed = []
        self.proceed_count = 0
        self.progress = True

    async def remove_from_same_dir(self):
        async with task_dict_lock:
            if (
                self.folder_name
                and self.same_dir
                and self.mid in self.same_dir[self.folder_name]["tasks"]
            ):
                self.same_dir[self.folder_name]["tasks"].remove(self.mid)
                self.same_dir[self.folder_name]["total"] -= 1

    async def on_download_start(self):
        if (
            self.is_super_chat
            and Config.INCOMPLETE_TASK_NOTIFIER
            and Config.DATABASE_URL
        ):
            await database.add_incomplete_task(
                self.message.chat.id, self.message.link, self.tag
            )

    async def on_download_complete(self):
        await sleep(2)
        if self.is_cancelled:
            return

        multi_links = await self._await_same_dir_merge()
        if multi_links is None:
            return

        async with task_dict_lock:
            if self.is_cancelled or self.mid not in task_dict:
                return
            download = task_dict[self.mid]
            self.name = download.name()
            gid = download.gid()
        LOGGER.info(f"Download completed: {self.name}")

        if multi_links:
            await self._finish_merged_task()
            return
        # a task that merged into a sibling, or shares a folder with one,
        # stops seeding — same three outcomes as the old nested branches
        if not (self.is_torrent or self.is_qbit) or self.same_dir:
            self.seed = False

        dl_path = await self._resolve_download_path()
        if dl_path is None:
            return
        up_dir, up_path = await self._prepare_upload_dir(dl_path)

        await self._filter_extensions(self.up_dir or self.dir)
        if not await aiopath.exists(up_path):
            await self.on_upload_error(NO_FILES_ERROR)
            return

        await self._release_download_slot()
        if self.join and not self.is_file:
            await join_files(up_path)

        up_path, cancelled = await self._run_post_processing(up_path, up_dir, gid)
        if cancelled:
            return

        self.subproc = None
        await self._start_upload(up_dir, gid)

    async def _await_same_dir_merge(self):
        """Wait until this task is the last one left in its same-dir group.

        Returns True when this task's files were merged into a sibling task,
        False when it carries on by itself, and None when it was dropped from
        the group while waiting — in which case the caller must return.
        """
        if not (
            self.folder_name
            and self.same_dir
            and self.mid in self.same_dir[self.folder_name]["tasks"]
        ):
            return False

        async with same_directory_lock:
            while True:
                async with task_dict_lock:
                    group = self.same_dir[self.folder_name]
                    if self.mid not in group["tasks"]:
                        return None
                    if group["total"] <= 1 or len(group["tasks"]) > 1:
                        if group["total"] <= 1:
                            return False
                        group["tasks"].remove(self.mid)
                        group["total"] -= 1
                        spath = f"{self.dir}{self.folder_name}"
                        des_id = list(group["tasks"])[0]
                        des_path = f"{DOWNLOAD_DIR}{des_id}{self.folder_name}"
                        LOGGER.info(f"Moving files from {self.mid} to {des_id}")
                        await move_and_merge(spath, des_path, self.mid)
                        return True
                # lock released before sleeping so siblings can make progress
                await sleep(1)

    async def _finish_merged_task(self):
        """Report a task whose files were merged into a sibling, then stop."""
        self.seed = False
        await self.on_upload_error(
            f"{self.name} Downloaded!\n\nWaiting for other tasks to finish...",
            silent=True,
        )
        batch = self._batch()
        if not batch:
            return
        batch["done"] += 1
        await self.update_batch_progress()
        if self.message != batch["anchor"]:
            try:
                await delete_message(self.message)
            except Exception:
                pass
        await self.finalize_batch()

    async def _resolve_download_path(self):
        """Settle ``self.name`` on what actually landed on disk, then stat it.

        Returns the download path, or None if the directory could not be read
        and the caller must return.
        """
        if self.folder_name:
            self.name = self.folder_name.strip("/").split("/", 1)[0]

        if not await aiopath.exists(f"{self.dir}/{self.name}"):
            try:
                files = await listdir(self.dir)
                self.name = files[-1]
                if self.name == "yt-dlp-thumb":
                    self.name = files[0]
            except Exception as e:
                await self.on_upload_error(str(e))
                return None

        dl_path = f"{self.dir}/{self.name}"
        self.size = await get_path_size(dl_path)
        self.is_file = await aiopath.isfile(dl_path)
        return dl_path

    async def _prepare_upload_dir(self, dl_path):
        """Pick the directory the upload reads from.

        A seeding task uploads through a symlink tree so the torrent's own
        files stay where the client left them.
        """
        if not self.seed:
            return self.dir, dl_path
        up_dir = self.up_dir = f"{self.dir}10000"
        up_path = f"{up_dir}/{self.name}"
        await create_recursive_symlink(self.dir, up_dir)
        LOGGER.info(f"Shortcut created: {dl_path} -> {up_path}")
        return up_dir, up_path

    async def _filter_extensions(self, directory):
        """Drop files the user excluded, or everything they did not include."""
        if not self.included_extensions:
            await remove_excluded_files(directory, self.excluded_extensions)
        else:
            await remove_non_included_files(directory, self.included_extensions)

    async def _release_download_slot(self):
        """Give the download slot back to the queue unless QUEUE_ALL holds it."""
        if Config.QUEUE_ALL:
            return
        async with queue_dict_lock:
            if self.mid in non_queued_dl:
                non_queued_dl.remove(self.mid)
        await start_from_queued()

    async def _run_stage(self, stage, up_path, up_dir, gid):
        """Run one post-processing stage.

        Returns ``(up_path, cancelled)``; when cancelled is True the caller
        must return without touching the rest of the pipeline.
        """
        if stage.log:
            LOGGER.info(f"{stage.log} {up_path}")

        step = getattr(self, stage.step)
        up_path = await (step(up_path, gid) if stage.pass_gid else step(up_path))

        if stage.stat_before_cancel:
            self.is_file = await aiopath.isfile(up_path)
        if self.is_cancelled:
            return up_path, True
        if not stage.stat_before_cancel:
            self.is_file = await aiopath.isfile(up_path)

        if stage.set_name:
            self.name = up_path.replace(f"{up_dir}/", "").split("/", 1)[0]
        if stage.refresh_size:
            self.size = await get_path_size(up_dir)
        if stage.do_clear:
            self.clear()
        if stage.refilter:
            await self._filter_extensions(up_dir)
        return up_path, False

    async def _run_post_processing(self, up_path, up_dir, gid):
        """Run every enabled stage, then split what is left.

        Returns ``(up_path, cancelled)`` like :meth:`_run_stage`.
        """
        for stage in _STAGES:
            if not any(getattr(self, attr) for attr in stage.guard):
                continue
            up_path, cancelled = await self._run_stage(stage, up_path, up_dir, gid)
            if cancelled:
                return up_path, True

        self.name = up_path.replace(f"{up_dir}/", "").split("/", 1)[0]
        self.size = await get_path_size(up_dir)

        if not self.compress:
            await self.proceed_split(up_path, gid)
            if self.is_cancelled:
                return up_path, True
            self.clear()
        return up_path, False

    async def _start_upload(self, up_dir, gid):
        """Queue the upload if needed, then hand the files to Telegram."""
        add_to_queue, event = await check_running_tasks(self, "up")
        await start_from_queued()
        if add_to_queue:
            LOGGER.info(f"Added to Queue/Upload: {self.name}")
            async with task_dict_lock:
                task_dict[self.mid] = QueueStatus(self, gid, "Up")
            await event.wait()
            if self.is_cancelled:
                return
            LOGGER.info(f"Start from Queued/Upload: {self.name}")

        self.size = await get_path_size(up_dir)

        LOGGER.info(f"Leech Name: {self.name}")
        tg = TelegramUploader(self, up_dir)
        async with task_dict_lock:
            task_dict[self.mid] = TelegramStatus(self, tg, gid, "up")
        await gather(
            update_status_message(self.message.chat.id),
            tg.upload(),
        )
        del tg

    async def on_upload_complete(self, link, files, folders, mime_type):
        if (
            self.is_super_chat
            and Config.INCOMPLETE_TASK_NOTIFIER
            and Config.DATABASE_URL
        ):
            await database.rm_complete_task(self.message.link)

        LOGGER.info(f"Task Done: {self.name}")

        batch = self._batch()
        if batch:
            batch["results"].append({
                "name": self.name,
                "size": self.size,
                "folders": folders,
                "corrupted": mime_type,
                "files": files,
                "link": link,
                "mime_type": "",
            })
            batch["done"] += 1

            if self.message != batch["anchor"]:
                try:
                    await delete_message(self.message)
                except OSError:
                    pass

            await self.update_batch_progress()
            await self.finalize_batch()
        else:
            msg = f"<b>Name: </b><code>{escape(self.name)}</code>\n\n<b>Size: </b>{get_readable_file_size(self.size)}"
            msg += f"\n<b>Total Files: </b>{folders}"
            if mime_type != 0:
                msg += f"\n<b>Corrupted Files: </b>{mime_type}"
            msg += f"\n<b>cc: </b>{self.tag}\n\n"
            if not files:
                await send_message(self.message, msg)
            else:
                fmsg = ""
                for index, (link, name) in enumerate(files.items(), start=1):
                    fmsg += f"{index}. <a href='{link}'>{name}</a>\n"
                    if len(fmsg.encode() + msg.encode()) > 4000:
                        await send_message(self.message, msg + fmsg)
                        await sleep(1)
                        fmsg = ""
                if fmsg != "":
                    await send_message(self.message, msg + fmsg)

        if self.seed:
            await clean_target(self.up_dir)
            async with queue_dict_lock:
                if self.mid in non_queued_up:
                    non_queued_up.remove(self.mid)
                upload_chat_of.pop(self.mid, None)
            await start_from_queued()
            return
        await clean_download(self.dir)
        async with task_dict_lock:
            if self.mid in task_dict:
                del task_dict[self.mid]
            count = len(task_dict)
        if count == 0:
            await self.clean()
        else:
            await update_status_message(self.message.chat.id)

        async with queue_dict_lock:
            if self.mid in non_queued_up:
                non_queued_up.remove(self.mid)
            upload_chat_of.pop(self.mid, None)

        await start_from_queued()

    async def on_download_error(self, error, button=None):
        async with task_dict_lock:
            if self.mid in task_dict:
                del task_dict[self.mid]
            count = len(task_dict)
        await self.remove_from_same_dir()
        if magnet_id := getattr(self, "_alldebrid_magnet_id", 0) or 0:
            try:
                from ..mirror_leech_utils.download_utils.alldebrid_resolver import (
                    delete_magnet,
                )

                await delete_magnet(magnet_id)
            except Exception:
                pass
            self._alldebrid_magnet_id = 0

        torbox_torrent_id = getattr(self, "_torbox_torrent_id", 0) or 0
        torbox_web_id = getattr(self, "_torbox_web_id", 0) or 0

        if torbox_torrent_id or torbox_web_id:
            try:
                from ..mirror_leech_utils.download_utils.torbox_resolver import (
                    delete_torrent,
                    delete_web_download,
                )

                if torbox_torrent_id:
                    await delete_torrent(torbox_torrent_id)

                if torbox_web_id:
                    await delete_web_download(torbox_web_id)

            except Exception:
                pass

        self._torbox_torrent_id = 0
        self._torbox_web_id = 0
        msg = f"{self.tag} Download: {escape(str(error))}"
        await send_message(self.message, msg, button)

        batch = self._batch()
        if batch:
            batch["errors"].append({"name": getattr(self, 'name', 'Unknown'), "error": str(error)})
            await self.update_batch_progress()
            await self.finalize_batch()

        if count == 0:
            await self.clean()
        else:
            await update_status_message(self.message.chat.id)

        if (
            self.is_super_chat
            and Config.INCOMPLETE_TASK_NOTIFIER
            and Config.DATABASE_URL
        ):
            await database.rm_complete_task(self.message.link)

        async with queue_dict_lock:
            if self.mid in queued_dl:
                queued_dl[self.mid].set()
                del queued_dl[self.mid]
            if self.mid in queued_up:
                queued_up[self.mid].set()
                del queued_up[self.mid]
            if self.mid in non_queued_dl:
                non_queued_dl.remove(self.mid)
            if self.mid in non_queued_up:
                non_queued_up.remove(self.mid)
            upload_chat_of.pop(self.mid, None)

        await start_from_queued()
        await sleep(3)
        await clean_download(self.dir)
        if self.up_dir:
            await clean_download(self.up_dir)
        if self.thumb and await aiopath.exists(self.thumb):
            await remove(self.thumb)

    async def on_upload_error(self, error, silent=False):
        async with task_dict_lock:
            if self.mid in task_dict:
                del task_dict[self.mid]
            count = len(task_dict)

        if not silent:
            await send_message(self.message, f"{self.tag} {escape(str(error))}")
            batch = self._batch()
            if batch:
                batch["errors"].append({"name": self.name, "error": str(error)})
                await self.update_batch_progress()
                await self.finalize_batch()

        if count == 0:
            await self.clean()
        else:
            await update_status_message(self.message.chat.id)

        if (
            self.is_super_chat
            and Config.INCOMPLETE_TASK_NOTIFIER
            and Config.DATABASE_URL
        ):
            await database.rm_complete_task(self.message.link)

        async with queue_dict_lock:
            if self.mid in queued_dl:
                queued_dl[self.mid].set()
                del queued_dl[self.mid]
            if self.mid in queued_up:
                queued_up[self.mid].set()
                del queued_up[self.mid]
            if self.mid in non_queued_dl:
                non_queued_dl.remove(self.mid)
            if self.mid in non_queued_up:
                non_queued_up.remove(self.mid)
            upload_chat_of.pop(self.mid, None)

        await start_from_queued()
        await sleep(3)
        await clean_download(self.dir)
        if self.up_dir:
            await clean_download(self.up_dir)
        if self.thumb and await aiopath.exists(self.thumb):
            await remove(self.thumb)
