from ...ext_utils.status_utils import (
    MirrorStatus,
    get_readable_file_size,
    get_readable_time,
)
from .base import BaseStatus


class TelegramStatus(BaseStatus):
    tool = "telegram"

    def __init__(self, listener, obj, gid, status):
        super().__init__(listener, obj, gid)
        # the size at hand-off: an upload is measured against what it was given,
        # not against a listener size the pipeline may still change
        self._size = self.listener.size
        self._status = status

    def size(self):
        return get_readable_file_size(self._size)

    def status(self):
        if self._status == "up":
            return MirrorStatus.STATUS_UPLOAD
        return MirrorStatus.STATUS_DOWNLOAD

    def progress(self):
        try:
            progress_raw = self._obj.processed_bytes / self._size * 100
        except Exception:
            progress_raw = 0
        return f"{round(progress_raw, 2)}%"

    def eta(self):
        try:
            seconds = (self._size - self._obj.processed_bytes) / self._obj.speed
            return get_readable_time(seconds)
        except Exception:
            return "-"
