"""Vidara folder downloader.

A ``/f/<code>`` page is a listing, and every video in it is an HLS ladder rather
than a file, so the folder is downloaded as one task that muxes each video with
yt-dlp in turn -- the same shape ``pornhub_download.py`` uses for a channel.

Each entry is resolved to its stream when its turn comes, not when the folder was
listed: the URLs the gateway mints are IP-bound and expire, and the tail of a
long folder would be stale by the time the download got there. That resolve is
the only network call that goes through ``resolve_gate``, which is what keeps a
bulk of folders from hitting the gateway once per video all at once.
"""

from __future__ import annotations

from os import path as ospath
from typing import Any

from aiofiles.os import makedirs
from aiofiles.os import path as aiopath
from yt_dlp import YoutubeDL

from .... import LOGGER
from ...ext_utils.bot_utils import sync_to_async
from ...ext_utils.resolve_gate import resolve_gate
from .multi_video_download import MultiVideoDownloadHelper


class VidaraDownloadHelper(MultiVideoDownloadHelper):
    """Downloads every video of a resolved Vidara folder in one task."""

    def _make_status(self):
        from ..status_utils.vidara_status import VidaraStatus

        return VidaraStatus(self._listener, self, self._gid)

    def _download_one(self, url: str, stem: str, headers: dict):
        """Mux one HLS stream to *stem* with yt-dlp (blocking).

        The extension comes from yt-dlp rather than from the title: it decides
        what container the ladder was muxed into, and a name handed over whole
        lands as "clip.mp4.mp4". A ``%`` in a title is escaped -- ``outtmpl`` is
        a template, and "100%(real)" would otherwise expand to something else.
        """
        opts: dict[str, Any] = {
            "format": "bv*+ba/b",
            "merge_output_format": "mp4",
            "outtmpl": f"{stem.replace('%', '%%')}.%(ext)s",
            "noprogress": True,
            "overwrites": True,
            "fragment_retries": 10,
            "retries": 10,
            "concurrent_fragment_downloads": 4,
            "progress_hooks": [self._on_progress],
            "quiet": True,
            "no_warnings": True,
        }
        if headers:
            opts["http_headers"] = headers
        # yt-dlp declares ``params`` as a TypedDict of every option it knows;
        # this one is built per download, headers included.
        with YoutubeDL(opts) as ydl:  # pyrefly: ignore[bad-argument-type]
            ydl.download([url])

    async def _resolve_entry(self, entry):
        """The entry's HLS master playlist and the headers the CDN wants.

        Held behind ``resolve_gate`` and nothing else: the mux that follows takes
        minutes, and a resolve slot held through it is a slot the rest of the
        batch waits for.
        """
        from .direct_link_generators import vidara_resolve

        async with resolve_gate():
            return await sync_to_async(vidara_resolve, entry["url"], entry["name"])

    async def _fetch_entry(self, entry, base):
        """Resolve one entry and mux it into *base*.

        Returns the reason it could not be fetched, or None when the file
        landed. One dead video does not fail the folder: it is counted, named in
        the log, and the rest of the listing carries on.
        """
        dest_dir = f"{base}/{entry['subpath']}" if entry["subpath"] else base
        if not await aiopath.exists(dest_dir):
            await makedirs(dest_dir, exist_ok=True)

        try:
            name, link, headers = await self._resolve_entry(entry)
        except Exception as exc:
            LOGGER.error(f"Vidara: can't resolve {entry['url']}: {exc}")
            return str(exc)

        self._current_downloaded = 0
        try:
            await sync_to_async(
                self._download_one,
                link,
                ospath.join(dest_dir, entry["name"] or name),
                headers,
            )
        except Exception as exc:
            LOGGER.error(f"Vidara: failed {entry['name']}: {exc}")
            return str(exc)
        finally:
            self._processed += self._current_downloaded
            self._current_downloaded = 0
        return None

    async def add_download(self, path: str):
        details = self._listener.link
        videos = details["videos"]
        title = details.get("title") or "Vidara"

        self._total_count = len(videos)
        self._listener.size = 0

        if not self._listener.name:
            self._listener.name = title

        if not await self._start():
            return

        LOGGER.info(
            f"Vidara folder download: {self._listener.name} "
            f"({self._total_count} video{'s' if self._total_count != 1 else ''})"
        )

        # the folder stays a folder: a bulk merges every task into one directory,
        # and loose files from a dozen folders would be indistinguishable there
        base = f"{path}/{self._listener.name}"
        failures = []

        for entry in videos:
            if self._listener.is_cancelled:
                return
            try:
                failure = await self._fetch_entry(entry, base)
            except SystemExit:
                # the progress hook raises this when the user cancels mid-mux
                return
            if failure:
                failures.append(failure)
                continue
            self._done_count += 1
            LOGGER.info(
                f"Vidara: [{self._done_count}/{self._total_count}] {entry['name']}"
            )

        if self._listener.is_cancelled:
            return

        if len(failures) == self._total_count:
            await self._listener.on_download_error(
                f"No video in this Vidara folder could be downloaded "
                f"({failures[0]})"
            )
            return

        if failures:
            # the upload is about to look complete, so say what is missing from it
            LOGGER.error(
                f"Vidara: {self._listener.name} finished with "
                f"{len(failures)}/{self._total_count} video(s) missing"
            )

        await self._listener.on_download_complete()


async def add_vidara_download(listener, path):
    await VidaraDownloadHelper(listener).add_download(path)
