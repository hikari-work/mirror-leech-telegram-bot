from time import time

from ...ext_utils.status_utils import (
    MirrorStatus,
    get_readable_file_size,
    get_readable_time,
)
from .base import SubprocStatus


class SevenZStatus(SubprocStatus):
    tool = "7z"

    def __init__(self, listener, obj, gid, status=""):
        super().__init__(listener, obj, gid, status)
        # 7z reports no rate of its own, so it is averaged over the run
        self._start_time = time()

    def _speed_raw(self):
        return self._obj.processed_bytes / (time() - self._start_time)

    def progress(self):
        return self._obj.progress

    def speed(self):
        return f"{get_readable_file_size(self._speed_raw())}/s"

    def eta(self):
        try:
            seconds = (
                self.listener.subsize - self._obj.processed_bytes
            ) / self._speed_raw()
            return get_readable_time(seconds)
        except Exception:
            return "-"

    def status(self):
        if self._cstatus == "Extract":
            return MirrorStatus.STATUS_EXTRACT
        else:
            return MirrorStatus.STATUS_ARCHIVE
