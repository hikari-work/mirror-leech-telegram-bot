from ...ext_utils.status_utils import (
    MirrorStatus,
    get_readable_file_size,
    get_readable_time,
)


class MegaStatus:
    def __init__(self, listener, obj, gid):
        self.listener = listener
        self._obj = obj
        self._gid = gid
        self.tool = "mega"

    def processed_bytes(self):
        return get_readable_file_size(self._obj.processed_bytes)

    def size(self):
        return get_readable_file_size(self.listener.size)

    def status(self):
        return MirrorStatus.STATUS_DOWNLOAD

    def name(self):
        return self.listener.name

    def progress(self):
        try:
            return f"{round(self._obj.processed_bytes / self.listener.size * 100, 2)}%"
        except Exception:
            return "0%"

    def speed(self):
        return f"{get_readable_file_size(self._obj.speed)}/s"

    def eta(self):
        try:
            seconds = (
                self.listener.size - self._obj.processed_bytes
            ) / self._obj.speed
            return get_readable_time(seconds)
        except Exception:
            return "-"

    def gid(self):
        return self._gid

    def task(self):
        return self._obj
