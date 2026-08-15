"""PornHub multi-video downloader.

Downloads all videos in a single task, using yt-dlp for HLS streams
and aria2 for direct MP4 files.  Modelled after ``mega_download.py``.
"""

from __future__ import annotations

from os import path as ospath
from secrets import token_urlsafe
from time import time

from aiofiles.os import makedirs
from aiofiles.os import path as aiopath
from yt_dlp import YoutubeDL

from .... import LOGGER, task_dict, task_dict_lock
from ...ext_utils.bot_utils import sync_to_async
from ...ext_utils.task_manager import check_running_tasks
from ...telegram_helper.message_utils import send_status_message
from ..status_utils.queue_status import QueueStatus


class PornHubDownloadHelper:
    """Downloads all videos from a resolved PornHub dict in one task."""

    def __init__(self, listener):
        self._listener = listener
        self._gid = token_urlsafe(10)
        self._processed = 0
        self._current_downloaded = 0
        self._speed = 0.0
        self._last_time = 0.0
        self._last_bytes = 0
        self._done_count = 0
        self._total_count = 0

    @property
    def processed_bytes(self):
        return self._processed + self._current_downloaded

    @property
    def speed(self):
        now = time()
        total = self._processed + self._current_downloaded
        if (elapsed := now - self._last_time) > 0.5:
            self._speed = (total - self._last_bytes) / elapsed
            self._last_time = now
            self._last_bytes = total
        return self._speed

    @property
    def progress_str(self):
        return f"{self._done_count}/{self._total_count}"

    def _on_progress(self, d):
        if self._listener.is_cancelled:
            raise SystemExit("Cancelled")
        if d["status"] == "downloading":
            self._current_downloaded = d.get("downloaded_bytes", 0) or 0

    def _download_one(self, url: str, dest: str, headers: dict):
        """Download a single video with yt-dlp (blocking)."""
        opts = {
            "format": "best",
            "outtmpl": dest,
            "noprogress": True,
            "overwrites": True,
            "fragment_retries": 10,
            "retries": 10,
            "progress_hooks": [self._on_progress],
            "quiet": True,
            "no_warnings": True,
        }
        if headers:
            opts["http_headers"] = headers
        with YoutubeDL(opts) as ydl:
            ydl.download([url])

    async def add_download(self, path: str):
        details = self._listener.link
        videos = details["videos"]
        headers = details.get("headers") or {}
        title = details.get("title") or "PornHub"

        self._total_count = len(videos)
        self._listener.size = 0

        if not self._listener.name:
            self._listener.name = title

        single = len(videos) == 1

        add_to_queue, event = await check_running_tasks(self._listener)
        if add_to_queue:
            LOGGER.info(f"Added to Queue/Download: {self._listener.name}")
            async with task_dict_lock:
                task_dict[self._listener.mid] = QueueStatus(
                    self._listener, self._gid, "dl",
                )
            await self._listener.on_download_start()
            if self._listener.multi <= 1 and not self._listener.is_rss:
                await send_status_message(self._listener.message)
            await event.wait()
            if self._listener.is_cancelled:
                return

        from ..status_utils.pornhub_status import PornHubStatus

        async with task_dict_lock:
            task_dict[self._listener.mid] = PornHubStatus(
                self._listener, self, self._gid,
            )

        if not add_to_queue:
            await self._listener.on_download_start()
            if self._listener.multi <= 1 and not self._listener.is_rss:
                await send_status_message(self._listener.message)

        LOGGER.info(
            f"PornHub download: {self._listener.name} "
            f"({self._total_count} video{'s' if self._total_count != 1 else ''})"
        )

        if single:
            base = path
        else:
            base = f"{path}/{self._listener.name}"

        failed = 0
        for idx, entry in enumerate(videos):
            if self._listener.is_cancelled:
                return

            url = entry["url"]
            name = entry["name"]
            dest_dir = base if single else base

            if not await aiopath.exists(dest_dir):
                await makedirs(dest_dir, exist_ok=True)

            dest = ospath.join(dest_dir, name)

            self._current_downloaded = 0
            try:
                await sync_to_async(self._download_one, url, dest, headers)
                self._processed += self._current_downloaded
                self._current_downloaded = 0
                self._done_count += 1
                LOGGER.info(f"PornHub: [{self._done_count}/{self._total_count}] {name}")
            except SystemExit:
                return
            except Exception as exc:
                failed += 1
                LOGGER.error(f"PornHub: failed {name}: {exc}")

        if self._listener.is_cancelled:
            return

        if failed == self._total_count:
            await self._listener.on_download_error("All videos failed to download!")
            return

        await self._listener.on_download_complete()

    async def cancel_task(self):
        self._listener.is_cancelled = True
        LOGGER.info(f"Cancelling Download: {self._listener.name}")
        await self._listener.on_download_error("Stopped by User!")


async def add_pornhub_download(listener, path):
    await PornHubDownloadHelper(listener).add_download(path)
