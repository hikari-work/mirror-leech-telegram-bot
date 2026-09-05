from asyncio import create_task, sleep, TimeoutError
from aiohttp.client_exceptions import ClientError
from os import path as ospath

from aiofiles.os import path as aiopath

from ... import LOGGER
from ...core.torrent_manager import TorrentManager, aria2_name


class DirectListener:
    def __init__(self, path, listener, a2c_opt, bunkr_lazy=False):
        self.listener = listener
        self._path = path
        self._a2c_opt = a2c_opt
        self._proc_bytes = 0
        self._failed = 0
        self.download_task = None
        self.name = self.listener.name
        self._bunkr_lazy = bunkr_lazy

    @property
    def processed_bytes(self):
        if self.download_task:
            return self._proc_bytes + int(
                self.download_task.get("completedLength", "0")
            )
        return self._proc_bytes

    @property
    def speed(self):
        return (
            int(self.download_task.get("downloadSpeed", "0"))
            if self.download_task
            else 0
        )

    async def _download_one(self, content):
        """Download a single content entry. Returns file path on success, None on failure."""
        if content["path"]:
            self._a2c_opt["dir"] = f"{self._path}/{content['path']}"
        else:
            self._a2c_opt["dir"] = self._path
        filename = content["filename"]
        self._a2c_opt["out"] = filename
        try:
            gid = await TorrentManager.aria2.addUri(
                uris=[content["url"]], options=self._a2c_opt, position=0
            )
        except (TimeoutError, ClientError, Exception) as e:
            self._failed += 1
            LOGGER.error(f"Unable to download {filename} due to: {e}")
            return None
        self.download_task = await TorrentManager.aria2.tellStatus(gid)
        while True:
            if self.listener.is_cancelled:
                if self.download_task:
                    await TorrentManager.aria2_remove(self.download_task)
                return None
            self.download_task = await TorrentManager.aria2.tellStatus(gid)
            if error_message := self.download_task.get("errorMessage"):
                self._failed += 1
                LOGGER.error(
                    f"Unable to download {aria2_name(self.download_task)} due to: {error_message}"
                )
                await TorrentManager.aria2_remove(self.download_task)
                return None
            elif self.download_task.get("status", "") == "complete":
                self._proc_bytes += int(self.download_task.get("totalLength", "0"))
                await TorrentManager.aria2_remove(self.download_task)
                file_path = ospath.join(self._a2c_opt["dir"], filename)
                self.download_task = None
                return file_path
            await sleep(1)

    async def download(self, contents):
        self.is_downloading = True
        if self.listener.stream_upload:
            await self._download_stream(contents)
        else:
            await self._download_batch(contents)

    async def _download_batch(self, contents):
        """Original behavior: download all, then on_download_complete."""
        total = len(contents)
        if self._bunkr_lazy:
            contents = await self._resolve_all_bunkr(contents)
            if not contents:
                await self.listener.on_download_error(
                    "All Bunkr files failed to resolve!"
                )
                return
        for content in contents:
            if self.listener.is_cancelled:
                break
            await self._download_one(content)
        if self.listener.is_cancelled:
            return
        # Counted against the album, not against what survived resolving: the
        # files dropped there are failures too, and comparing with the shrunken
        # list reported a whole album of failures as a completed download.
        if self._failed == total:
            await self.listener.on_download_error("All files are failed to download!")
            return
        await self.listener.on_download_complete()

    async def _download_stream(self, contents):
        """Stream mode: download and upload run in parallel.

        While file N uploads, file N+1 resolves and downloads concurrently.
        Disk usage = max 2 files at a time.  Bunkr URLs are resolved lazily
        (one at a time) to avoid stale signed CDN links.
        """
        from ..upload.telegram_uploader import TelegramUploader

        tg = TelegramUploader(self.listener, self._path)
        if not await tg.init_stream():
            return

        total = len(contents)
        upload_task = None

        for idx, content in enumerate(contents):
            if self.listener.is_cancelled:
                break

            if self._bunkr_lazy:
                content = await self._resolve_one_bunkr(content)
                if content is None:
                    continue

            file_path = await self._download_one(content)
            if file_path is None:
                continue
            if self.listener.is_cancelled:
                break

            if upload_task is not None:
                await upload_task

            if self.listener.is_cancelled:
                break

            LOGGER.info(f"Stream upload [{idx + 1}/{total}]: {ospath.basename(file_path)}")
            upload_task = create_task(tg.upload_single(file_path))

        if upload_task is not None:
            await upload_task

        self.download_task = None
        if self.listener.is_cancelled:
            return
        if self._failed == total:
            await self.listener.on_download_error("All files are failed to download!")
            return
        await tg.finalize_stream()

    async def _resolve_one_bunkr(self, content):
        """Lazily resolve a single bunkr file URL just before download."""
        from ..download.direct_link_generators.hosts.bunkr import (
            bunkr_resolve_download,
        )
        dl_url, filename, file_size = await bunkr_resolve_download(content["url"])
        if dl_url:
            content["url"] = dl_url
            if filename:
                content["filename"] = filename
            return content
        self._failed += 1
        LOGGER.error(f"Bunkr: failed to resolve {content['filename']}")
        return None

    async def _resolve_all_bunkr(self, contents):
        """Resolve all bunkr file URLs, return the ones that resolved.

        The resolves share one connection pool and run a few at a time: an
        album is hundreds of files, and asking for all of them at once is what
        made the gateway answer a large album with errors on every file.
        """
        from ..download.direct_link_generators.hosts.bunkr import (
            bunkr_resolve_many,
        )

        results = await bunkr_resolve_many([c["url"] for c in contents])

        resolved = []
        for content, (dl_url, filename, _size) in zip(contents, results):
            if not dl_url:
                self._failed += 1
                LOGGER.error(f"Bunkr: failed to resolve {content['filename']}")
                continue
            content["url"] = dl_url
            if filename:
                content["filename"] = filename
            resolved.append(content)

        failed = len(contents) - len(resolved)
        if failed:
            LOGGER.warning(f"Bunkr: {failed}/{len(contents)} files failed to resolve")
        return resolved

    async def cancel_task(self):
        self.listener.is_cancelled = True
        LOGGER.info(f"Cancelling Download: {self.listener.name}")
        await self.listener.on_download_error("Download Cancelled by User!")
        if self.download_task:
            await TorrentManager.aria2_remove(self.download_task)
