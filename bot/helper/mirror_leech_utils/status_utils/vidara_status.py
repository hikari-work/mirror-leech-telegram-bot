from ...ext_utils.status_utils import (
    MirrorStatus,
    get_readable_file_size,
)


class VidaraStatus:
    """Status line for a Vidara folder task.

    An HLS ladder does not declare a total size, so until the files are on disk
    there is no byte count to show a percentage against -- the "x/y videos" the
    folder is being worked through stands in for the size instead.
    """

    def __init__(self, listener, obj, gid):
        self.listener = listener
        self._obj = obj
        self._gid = gid
        self.tool = "vidara"

    def processed_bytes(self):
        return get_readable_file_size(self._obj.processed_bytes)

    def size(self):
        return (
            get_readable_file_size(self.listener.size)
            if self.listener.size
            else self._obj.progress_str
        )

    def status(self):
        return MirrorStatus.STATUS_DOWNLOAD

    def name(self):
        return self.listener.name

    def progress(self):
        done = self._obj._done_count
        total = self._obj._total_count
        if total > 0:
            return f"{round(done / total * 100, 2)}%"
        return "0%"

    def speed(self):
        return f"{get_readable_file_size(self._obj.speed)}/s"

    def eta(self):
        return "-"

    def gid(self):
        return self._gid

    def task(self):
        return self._obj
