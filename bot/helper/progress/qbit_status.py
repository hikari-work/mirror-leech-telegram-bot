from asyncio import sleep, gather

from aioqbt.api.types import TorrentInfo

from ... import LOGGER, qb_torrents, qb_listener_lock
from ...core.torrent_manager import TorrentManager
from ..util.status_utils import (
    MirrorStatus,
    get_readable_file_size,
    get_readable_time,
)


async def get_download(tag, old_info: TorrentInfo | None = None) -> TorrentInfo | None:
    try:
        res = (await TorrentManager.qbittorrent.torrents.info(tag=tag))[0]
        return res or old_info
    except Exception as e:
        LOGGER.error(f"{e}: Qbittorrent, while getting torrent info. Tag: {tag}")
        return old_info


class QbittorrentStatus:
    def __init__(self, listener, seeding=False, queued=False):
        self.queued = queued
        self.seeding = seeding
        self.listener = listener
        # Every accessor below reads this unguarded, so ``update()`` has to have
        # run first -- a precondition the status machinery already satisfies.
        # Annotated to say so rather than to claim the None is unreachable: it
        # is, if the very first ``get_download()`` fails and returns old_info.
        self._info: TorrentInfo = None  # pyrefly: ignore[bad-assignment]
        self.tool = "qbittorrent"

    async def update(self):
        # Keep whatever we last had when the lookup fails. ``get_download``
        # already answers ``old_info`` for that, so the only None this refuses is
        # a first lookup that failed before there was anything to keep -- and
        # then holding the None over is no worse than storing it.
        if info := await get_download(f"{self.listener.mid}", self._info):
            self._info = info

    def progress(self):
        return f"{round(self._info.progress * 100, 2)}%"

    def processed_bytes(self):
        return get_readable_file_size(self._info.downloaded)

    def speed(self):
        return f"{get_readable_file_size(self._info.dlspeed)}/s"

    def name(self):
        if self._info.state in ["metaDL", "checkingResumeData"]:
            return f"[METADATA]{self.listener.name}"
        else:
            return self.listener.name

    def size(self):
        return get_readable_file_size(self._info.size)

    def eta(self):
        return get_readable_time(self._info.eta.total_seconds())

    async def status(self):
        await self.update()
        return self.cached_status()

    def cached_status(self) -> str:
        """The status of the info ``update()`` last fetched.

        Split from ``status()`` so that a caller holding a batch of tasks can
        refresh them all at once and then read every answer off the object it just
        refreshed, instead of paying a round trip per task inside the loop that
        renders them. Reading it before any ``update()`` has run is the same
        mistake calling ``status()`` first used to be.
        """
        state = self._info.state
        if state == "queuedDL" or self.queued:
            return MirrorStatus.STATUS_QUEUEDL
        elif state == "queuedUP":
            return MirrorStatus.STATUS_QUEUEUP
        elif state in ["stoppedDL", "stoppedUP"]:
            return MirrorStatus.STATUS_PAUSED
        elif state in ["checkingUP", "checkingDL"]:
            return MirrorStatus.STATUS_CHECK
        elif state in ["stalledUP", "uploading"] and self.seeding:
            return MirrorStatus.STATUS_SEED
        else:
            return MirrorStatus.STATUS_DOWNLOAD

    def seeders_num(self):
        return self._info.num_seeds

    def leechers_num(self):
        return self._info.num_leechs

    def uploaded_bytes(self):
        return get_readable_file_size(self._info.uploaded)

    def seed_speed(self):
        return f"{get_readable_file_size(self._info.upspeed)}/s"

    def ratio(self):
        return f"{round(self._info.ratio, 3)}"

    def seeding_time(self):
        return get_readable_time(int(self._info.seeding_time.total_seconds()))

    def task(self):
        return self

    def gid(self):
        return self.hash()[:12]

    def known_gid(self) -> str | None:
        """This task's gid, when it can be given without asking qBittorrent.

        A torrent's hash is assigned once and never reassigned, and it is in hand
        from the first successful ``update()``, so a lookup can settle on one by
        comparison alone rather than by a ``torrents.info`` call per task standing
        in its way. ``None`` means there is no info to answer from -- the window
        between being built and that first ``update()`` -- which tells the caller
        to ask rather than to guess.
        """
        return self.gid() if self._info is not None else None

    def hash(self):
        return self._info.hash

    async def cancel_task(self):
        self.listener.is_cancelled = True
        await self.update()
        await TorrentManager.qbittorrent.torrents.stop([self._info.hash])
        if not self.seeding:
            if self.queued:
                LOGGER.info(f"Cancelling QueueDL: {self.name()}")
                msg = "task have been removed from queue/download"
            else:
                LOGGER.info(f"Cancelling Download: {self._info.name}")
                msg = "Stopped by user!"
            await sleep(0.3)
            await gather(
                self.listener.on_download_error(msg),
                TorrentManager.qbittorrent.torrents.delete([self._info.hash], True),
                TorrentManager.qbittorrent.torrents.delete_tags(
                    tags=[self._info.tags[0]]
                ),
            )
            async with qb_listener_lock:
                if self._info.tags[0] in qb_torrents:
                    del qb_torrents[self._info.tags[0]]
