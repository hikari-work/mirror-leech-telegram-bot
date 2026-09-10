from ..util.status_utils import MirrorStatus
from .base import BaseStatus


class S3UploadStatus(BaseStatus):
    """An upload into a bucket: bytes sent against the size at hand-off.

    ``BaseStatus`` is enough here, unlike ``TelegramStatus``. That one snapshots
    ``listener.size`` because a telegram upload can run beside a pipeline that
    still changes it; by the time a bucket upload starts, the pipeline is done
    and nothing touches the size again. The only thing missing is direction,
    which is the whole of what this adds.
    """

    tool = "s3"

    def status(self):
        return MirrorStatus.STATUS_UPLOAD
