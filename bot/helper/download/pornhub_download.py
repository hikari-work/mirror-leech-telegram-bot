"""PornHub multi-video downloader.

Downloads all videos in a single task, using yt-dlp for HLS streams
and aria2 for direct MP4 files.  Modelled after ``mega_download.py``.
"""

from __future__ import annotations

from os import path as ospath
from typing import Any

from aiofiles.os import makedirs
from aiofiles.os import path as aiopath
from yt_dlp import YoutubeDL

from ... import LOGGER
from ..util.bot_utils import sync_to_async
from .multi_video_download import MultiVideoDownloadHelper
from .yt_dlp_hooks import final_paths_hook


class PornHubDownloadHelper(MultiVideoDownloadHelper):
    """Downloads all videos from a resolved PornHub dict in one task."""

    def _make_status(self):
        from ..progress.pornhub_status import PornHubStatus

        return PornHubStatus(self._listener, self, self._gid)

    def _download_one(self, url: str, dest: str, headers: dict, finished: list[str]):
        """Download a single video with yt-dlp (blocking).

        *finished* collects where the file landed, which is what an upload
        started before the task ends has to be handed.
        """
        opts: dict[str, Any] = {
            "format": "best",
            "outtmpl": dest,
            "noprogress": True,
            "overwrites": True,
            "fragment_retries": 10,
            "retries": 10,
            "progress_hooks": [self._on_progress],
            "postprocessor_hooks": [final_paths_hook(finished.append)],
            "quiet": True,
            "no_warnings": True,
        }
        if headers:
            opts["http_headers"] = headers
        # yt-dlp declares ``params`` as a TypedDict of every option it knows;
        # this one is built per download, headers included.
        with YoutubeDL(opts) as ydl:  # pyrefly: ignore[bad-argument-type]
            ydl.download([url])

    async def _fetch_video(self, entry, base, headers, stream):
        """Download one video and hand it over. Answers whether it landed."""
        url = entry["url"]
        name = entry["name"]

        if not await aiopath.exists(base):
            await makedirs(base, exist_ok=True)

        dest = ospath.join(base, name)
        # filled by the download itself, on the worker thread it runs on; read
        # here once that call has returned
        finished: list[str] = []
        self._current_downloaded = 0
        try:
            await sync_to_async(self._download_one, url, dest, headers, finished)
        except Exception as exc:
            self._current_downloaded = 0
            LOGGER.error(f"PornHub: failed {name}: {exc}")
            return False

        self._processed += self._current_downloaded
        self._current_downloaded = 0
        self._done_count += 1
        LOGGER.info(f"PornHub: [{self._done_count}/{self._total_count}] {name}")

        if stream is not None:
            # *dest* is the fallback and not the usual answer: yt-dlp reports
            # the file it moved into place, and says nothing about one it did
            # not have to move
            for path in finished or [dest]:
                await stream.submit(path)
        return True

    async def _walk_videos(self, videos, base, headers, stream):
        """Download every video in turn, and answer how many did not land."""
        failed = 0
        for entry in videos:
            if self._listener.is_cancelled:
                return failed
            try:
                landed = await self._fetch_video(entry, base, headers, stream)
            except SystemExit:
                # the progress hook raises this when the user cancels mid-mux
                return failed
            if not landed:
                failed += 1
        return failed

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

        stream = None
        if self._listener.stream_upload:
            # imported here: this is the download side of the package the
            # component uploads through
            from ..upload.stream_uploader import StreamUploader

            stream = StreamUploader(self._listener, base)
            if not await stream.start():
                return

        try:
            failed = await self._walk_videos(videos, base, headers, stream)
        finally:
            # in a finally so a cancelled channel still gives the uploader back
            # what it was sent, before the outcome below ends the task
            if stream is not None:
                await stream.drain()

        if self._listener.is_cancelled:
            return

        if failed == self._total_count:
            await self._listener.on_download_error("All videos failed to download!")
            return

        if stream is not None:
            await stream.finalize()
            return
        await self._listener.on_download_complete()


async def add_pornhub_download(listener, path):
    await PornHubDownloadHelper(listener).add_download(path)
