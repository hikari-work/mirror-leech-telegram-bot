"""The shape every status line shares.

``get_readable_message`` reads a task through a fixed set of methods -- gid,
name, size, status, processed_bytes, progress, speed, eta, task -- so each
downloader used to spell all nine out again, and eight of the twelve files here
carried byte-for-byte identical bodies for most of them. What is left in each
file is what actually differs per tool: where the byte counts come from, and
which ``MirrorStatus`` the task reports.
"""

from ... import LOGGER
from ..util.status_utils import (
    MirrorStatus,
    get_readable_file_size,
    get_readable_time,
)


class BaseStatus:
    """A download whose progress is a byte count against ``listener.size``."""

    #: which engine is doing the work; ``/status`` groups tasks by it
    tool = ""

    def __init__(self, listener, obj, gid):
        self.listener = listener
        self._obj = obj
        self._gid = gid

    def gid(self):
        return self._gid

    def name(self):
        return self.listener.name

    def size(self):
        return get_readable_file_size(self.listener.size)

    def status(self):
        return MirrorStatus.STATUS_DOWNLOAD

    def processed_bytes(self):
        return get_readable_file_size(self._obj.processed_bytes)

    def speed(self):
        return f"{get_readable_file_size(self._obj.speed)}/s"

    def progress(self):
        try:
            return f"{round(self._obj.processed_bytes / self.listener.size * 100, 2)}%"
        except Exception:
            return "0%"

    def eta(self):
        try:
            seconds = (self.listener.size - self._obj.processed_bytes) / self._obj.speed
            return get_readable_time(seconds)
        except Exception:
            return "-"

    def task(self):
        return self._obj


class CountedStatus(BaseStatus):
    """A task counted in files rather than bytes.

    An HLS ladder does not declare a total size, so until the files are on disk
    there is no byte count to show a percentage against -- the "x/y videos" the
    listing is being worked through stands in for the size, and an ETA cannot be
    guessed from it at all.
    """

    def size(self):
        return (
            get_readable_file_size(self.listener.size)
            if self.listener.size
            else self._obj.progress_str
        )

    def progress(self):
        return f"{round(self._obj.progress, 2)}%"

    def eta(self):
        return "-"


class SubprocStatus(BaseStatus):
    """Work this bot runs itself in a subprocess -- ffmpeg, 7z.

    There is no download object to cancel through, so the status line *is* the
    task: cancelling means killing the process the listener holds.
    """

    def __init__(self, listener, obj, gid, status=""):
        super().__init__(listener, obj, gid)
        self._cstatus = status

    def task(self):
        return self

    async def cancel_task(self):
        LOGGER.info(f"Cancelling {self._cstatus}: {self.listener.name}")
        self.listener.is_cancelled = True
        if (
            self.listener.subproc is not None
            and self.listener.subproc.returncode is None
        ):
            try:
                self.listener.subproc.kill()
            except Exception:
                pass
        await self.listener.on_upload_error(f"{self._cstatus} stopped by user!")
