from ..util.status_utils import MirrorStatus
from .base import BaseStatus


class DirectStatus(BaseStatus):
    tool = "aria2"

    def status(self):
        if (
            self._obj.download_task
            and self._obj.download_task.get("status", "") == "waiting"
        ):
            return MirrorStatus.STATUS_QUEUEDL
        return MirrorStatus.STATUS_DOWNLOAD
