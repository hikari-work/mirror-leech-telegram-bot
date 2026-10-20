"""Status line for a recording started by ``/live``."""

from ... import LOGGER
from ..util.status_utils import (
    MirrorStatus,
    get_readable_file_size,
    get_readable_time,
)
from .base import SubprocStatus


class LiveRecordStatus(SubprocStatus):
    """A recording has no declared size, so there is no percentage to report.

    The task sets ``listener.progress = False``, which makes the renderer print
    only ``Size`` for it; that slot carries what has landed on disk and how
    long the recording has run, which is the whole story until the user stops
    it. After the stop the task is replaced by the upload status and this
    object goes away.
    """

    tool = "live"

    def status(self) -> str:
        return MirrorStatus.STATUS_FFMPEG

    def size(self) -> str:
        elapsed = max(self.listener.elapsed_seconds(), 1)
        return (
            f"{get_readable_file_size(self.listener.recorded_bytes())}"
            f" ({get_readable_time(elapsed)} recorded)"
        )

    async def cancel_task(self) -> None:
        """Stop recording, or cancel the remux/upload if that is what runs.

        Stopping is not a cancellation: the recording is the product, so the
        task carries on and uploads it. Only the finalizing phase -- and the
        upload after it -- can be cancelled in the ordinary sense.
        """
        if self.listener.phase == "recording":
            LOGGER.info(f"Stopping live recording: {self.listener.name}")
            self.listener.request_stop()
            return
        await super().cancel_task()
