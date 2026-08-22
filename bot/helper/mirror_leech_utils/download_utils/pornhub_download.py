"""PornHub multi-video downloader.

Downloads all videos in a single task, using yt-dlp for HLS streams
and aria2 for direct MP4 files.  Modelled after ``mega_download.py``.
"""

from __future__ import annotations

from os import path as ospath

from aiofiles.os import makedirs
from aiofiles.os import path as aiopath
from yt_dlp import YoutubeDL

from .... import LOGGER
from ...ext_utils.bot_utils import sync_to_async
from .multi_video_download import MultiVideoDownloadHelper


class PornHubDownloadHelper(MultiVideoDownloadHelper):
    """Downloads all videos from a resolved PornHub dict in one task."""

    def _make_status(self):
        from ..status_utils.pornhub_status import PornHubStatus

        return PornHubStatus(self._listener, self, self._gid)

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

        if not await self._start():
            return

        LOGGER.info(
            f"PornHub download: {self._listener.name} "
            f"({self._total_count} video{'s' if self._total_count != 1 else ''})"
        )

        if single:
            base = path
        else:
            base = f"{path}/{self._listener.name}"

        failed = 0
        for entry in videos:
            if self._listener.is_cancelled:
                return

            url = entry["url"]
            name = entry["name"]

            if not await aiopath.exists(base):
                await makedirs(base, exist_ok=True)

            dest = ospath.join(base, name)

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


async def add_pornhub_download(listener, path):
    await PornHubDownloadHelper(listener).add_download(path)
