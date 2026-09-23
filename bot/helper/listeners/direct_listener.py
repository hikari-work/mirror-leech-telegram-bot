from asyncio import sleep, TimeoutError
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
        # Path -> gid for every download aria2 is still running, filled only in
        # resume mode -- see ``adopt_running``.
        self._live: dict[str, str] = {}
        # Relative paths a previous run of this task already delivered, filled
        # only in resume mode -- see ``adopt_running``.
        self._delivered: set[str] = set()
        # Whether this run is picking a previous one back up. It widens what
        # ``_download_one`` is allowed to skip: a complete file on disk is
        # something to leave alone when resuming, and something to fetch when
        # the same album is asked for a second time.
        self._resume = False

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

    async def adopt_running(self) -> None:
        """Pick up the aria2 downloads a restart left behind.

        The engine is a daemon and never stopped, so the files this task was
        pulling are still coming down; what died with the bot was only the
        process that knew which task they belonged to. Matching on the path each
        download is writing is what reconnects the two -- the gid is not a
        handle that survives, and aria2 has no tag to hang a task id on.

        One ``tellActive``/``tellWaiting`` pair covers the whole album, which is
        what makes this affordable for a task of several hundred files.
        """
        from ..storage.db_handler import database

        self._resume = True
        for download in await TorrentManager.unfinished():
            gid = download.get("gid", "")
            for entry in download.get("files", []):
                if path := entry.get("path", ""):
                    self._live[path] = gid
        # And what the previous run had already *sent*. A streamed file is
        # deleted the moment it goes out, so it is missing from disk for a
        # reason that has nothing to do with a failed download, and fetching it
        # again would send the user a second copy of something they have. The
        # checkpoints are keyed by the path the file was fetched to, which is
        # the coordinate this loop works in.
        self._delivered = {
            row["relpath"]
            for row in await database.get_uploaded_files(self.listener.mid)
        }

    async def _download_one(self, content):
        """Download a single content entry. Returns file path on success, None on failure."""
        if content["path"]:
            self._a2c_opt["dir"] = f"{self._path}/{content['path']}"
        else:
            self._a2c_opt["dir"] = self._path
        filename = content["filename"]
        self._a2c_opt["out"] = filename
        target = ospath.join(self._a2c_opt["dir"], filename)
        if gid := self._live.pop(target, ""):
            # Already coming down: poll the download that is already running
            # rather than adding a second one for the same file.
            LOGGER.info(f"Resuming aria2 download {filename} on gid {gid}")
            return await self._await_download(gid, target)
        if self._resume and ospath.relpath(target, self._path) in self._delivered:
            # Sent by the run this one continues, and deleted straight after --
            # which is what a stream does with every file it delivers, and is
            # why the path is missing rather than merely incomplete. Its message
            # is already in the report, read back from the same checkpoints.
            LOGGER.info(f"Already sent, skipping: {target}")
            return target
        if self._resume and await self._is_complete(target):
            # Finished by the run this one is resuming, and still on disk
            # because nothing swept it. Adding it again would only make aria2
            # rename the good copy out of the way -- ``--allow-overwrite`` and
            # ``--auto-file-renaming`` between them.
            LOGGER.info(f"Already downloaded, skipping: {target}")
            return target
        try:
            gid = await TorrentManager.aria2.addUri(
                uris=[content["url"]], options=self._a2c_opt, position=0
            )
        except (TimeoutError, ClientError, Exception) as e:
            self._failed += 1
            LOGGER.error(f"Unable to download {filename} due to: {e}")
            return None
        return await self._await_download(gid, target)

    @staticmethod
    async def _is_complete(target: str) -> bool:
        """Whether *target* is a finished file rather than one still arriving.

        aria2 leaves a ``.aria2`` control file beside anything it has not
        finished, and with ``--continue=true`` it picks the bytes up from there.
        A file without one is what the last run left behind complete.
        """
        return await aiopath.exists(target) and not await aiopath.exists(
            f"{target}.aria2"
        )

    async def _await_download(self, gid, target):
        """Wait out one aria2 download and answer the path it produced."""
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
                self.download_task = None
                return target
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
        """Stream mode: each file is uploaded while the next one downloads.

        Disk usage stays at the file being sent plus the one being fetched.
        Bunkr URLs are resolved lazily (one at a time) to avoid stale signed
        CDN links. The uploader itself lives in ``StreamUploader``, which also
        clears the flags streaming cannot honour and reports them in the task's
        message -- and which is why this task never calls
        ``on_download_complete``: that is the switch back into the queued,
        upload-at-the-end pipeline.
        """
        from ..upload.stream_uploader import StreamUploader

        stream = StreamUploader(self.listener, self._path)
        if not await stream.start():
            return

        total = len(contents)
        try:
            for content in contents:
                if self.listener.is_cancelled:
                    break

                if self._bunkr_lazy:
                    content = await self._resolve_one_bunkr(content)
                    if content is None:
                        continue

                file_path = await self._download_one(content)
                if file_path is None:
                    continue
                await stream.submit(file_path)
        finally:
            self.download_task = None
            # in a finally so a cancellation, or the SystemExit a progress hook
            # can raise, still hands the uploader back what it was sent
            await stream.drain()

        if self.listener.is_cancelled:
            return
        if self._failed == total:
            await self.listener.on_download_error("All files are failed to download!")
            return
        await stream.finalize()

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
