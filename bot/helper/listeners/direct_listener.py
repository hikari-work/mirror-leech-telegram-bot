from asyncio import gather, sleep, TimeoutError
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
        if self._failed == len(contents):
            await self.listener.on_download_error("All files are failed to download!")
            return
        await self.listener.on_download_complete()

    async def _download_stream(self, contents):
        """Stream mode: download one file -> upload -> delete -> next.

        Bunkr URLs are resolved lazily (one at a time) to avoid stale
        signed CDN links expiring while earlier files upload.
        """
        from ..mirror_leech_utils.upload_utils.telegram_uploader import TelegramUploader

        tg = TelegramUploader(self.listener, self._path)
        if not await tg.init_stream():
            return

        total = len(contents)
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

            LOGGER.info(f"Stream upload [{idx + 1}/{total}]: {ospath.basename(file_path)}")
            await tg.upload_single(file_path)

            if self.listener.is_cancelled:
                break

        self.download_task = None
        if self.listener.is_cancelled:
            return
        if self._failed == total:
            await self.listener.on_download_error("All files are failed to download!")
            return
        await tg.finalize_stream()

    async def _resolve_one_bunkr(self, content):
        """Lazily resolve a single bunkr file URL just before download."""
        from ..mirror_leech_utils.download_utils.direct_link_generators.hosts.bunkr import (
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
        """Resolve all bunkr file URLs concurrently, return resolved contents."""
        from ..mirror_leech_utils.download_utils.direct_link_generators.hosts.bunkr import (
            bunkr_resolve_download,
        )

        async def _resolve_one(content):
            dl_url, filename, file_size = await bunkr_resolve_download(content["url"])
            if dl_url:
                content["url"] = dl_url
                if filename:
                    content["filename"] = filename
                return content
            LOGGER.error(f"Bunkr: failed to resolve {content['filename']}")
            return None

        results = await gather(*[_resolve_one(c) for c in contents])
        resolved = [r for r in results if r is not None]
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
