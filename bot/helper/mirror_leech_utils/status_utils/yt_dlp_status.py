from ...ext_utils.status_utils import get_readable_file_size, get_readable_time
from .base import BaseStatus


class YtDlpStatus(BaseStatus):
    """yt-dlp keeps its own totals, so every count here comes off the helper."""

    tool = "yt-dlp"

    def processed_bytes(self):
        return get_readable_file_size(self._obj.downloaded_bytes)

    def size(self):
        return get_readable_file_size(self._obj.size)

    def progress(self):
        return f"{round(self._obj.progress, 2)}%"

    def speed(self):
        return f"{get_readable_file_size(self._obj.download_speed)}/s"

    def eta(self):
        if self._obj.eta != "-":
            return get_readable_time(self._obj.eta)
        try:
            seconds = (
                self._obj.size - self._obj.downloaded_bytes
            ) / self._obj.download_speed
            return get_readable_time(seconds)
        except Exception:
            return "-"
