from ..util.status_utils import (
    MirrorStatus,
    get_readable_file_size,
    get_readable_time,
)
from .base import SubprocStatus


class FFmpegStatus(SubprocStatus):
    tool = "ffmpeg"

    def speed(self):
        return f"{get_readable_file_size(self._obj.speed_raw)}/s"

    def progress(self):
        return f"{round(self._obj.progress_raw, 2)}%"

    def eta(self):
        return get_readable_time(self._obj.eta_raw) if self._obj.eta_raw else "-"

    def status(self):
        if self._cstatus == "Convert":
            return MirrorStatus.STATUS_CONVERT
        elif self._cstatus == "Split":
            return MirrorStatus.STATUS_SPLIT
        elif self._cstatus == "Sample Video":
            return MirrorStatus.STATUS_SAMVID
        else:
            return MirrorStatus.STATUS_FFMPEG
