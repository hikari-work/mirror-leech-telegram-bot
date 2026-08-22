"""What a folder/channel download shares.

A PornHub channel and a Vidara folder are the same shape of task: one entry in
the status list that walks a list of videos and muxes each of them with yt-dlp
in turn. Neither can report a byte total up front -- an HLS ladder does not
declare one -- so both count progress in videos and measure speed from what has
landed so far. That bookkeeping, and the queue slot the task waits for before it
starts, is identical either way; only the per-entry fetch and the status line
differ, and those are what the subclass provides.
"""

from __future__ import annotations

from secrets import token_urlsafe
from time import time

from .... import LOGGER, task_dict, task_dict_lock
from ...ext_utils.task_manager import check_running_tasks
from ...telegram_helper.message_utils import send_status_message
from ..status_utils.queue_status import QueueStatus


class MultiVideoDownloadHelper:
    """One task over a list of videos, counted rather than sized."""

    def __init__(self, listener):
        self._listener = listener
        self._gid = token_urlsafe(10)
        self._processed = 0
        self._current_downloaded = 0
        self._speed = 0.0
        self._last_time = 0.0
        self._last_bytes = 0
        self._done_count = 0
        self._total_count = 0

    @property
    def processed_bytes(self):
        return self._processed + self._current_downloaded

    @property
    def speed(self):
        now = time()
        total = self._processed + self._current_downloaded
        if (elapsed := now - self._last_time) > 0.5:
            self._speed = (total - self._last_bytes) / elapsed
            self._last_time = now
            self._last_bytes = total
        return self._speed

    @property
    def progress_str(self):
        """"x/y" videos -- what stands in for the size nothing declared."""
        return f"{self._done_count}/{self._total_count}"

    @property
    def progress(self):
        return self._done_count / self._total_count * 100 if self._total_count else 0

    def _on_progress(self, d):
        if self._listener.is_cancelled:
            raise SystemExit("Cancelled")
        if d["status"] == "downloading":
            self._current_downloaded = d.get("downloaded_bytes", 0) or 0

    def _make_status(self):
        """The status line this task shows while it runs.

        Left to the subclass so each one imports its own status module, which is
        also where that import stays lazy.
        """
        raise NotImplementedError

    async def _start(self):
        """Announce the task and take a download slot.

        Returns *False* when the task was cancelled while it sat in the queue.
        """
        add_to_queue, event = await check_running_tasks(self._listener)
        if add_to_queue:
            LOGGER.info(f"Added to Queue/Download: {self._listener.name}")
            async with task_dict_lock:
                task_dict[self._listener.mid] = QueueStatus(
                    self._listener, self._gid, "dl",
                )
            await self._listener.on_download_start()
            if self._listener.multi <= 1 and not self._listener.is_rss:
                await send_status_message(self._listener.message)
            await event.wait()
            if self._listener.is_cancelled:
                return False

        async with task_dict_lock:
            task_dict[self._listener.mid] = self._make_status()

        if not add_to_queue:
            await self._listener.on_download_start()
            if self._listener.multi <= 1 and not self._listener.is_rss:
                await send_status_message(self._listener.message)
        return True

    async def cancel_task(self):
        self._listener.is_cancelled = True
        LOGGER.info(f"Cancelling Download: {self._listener.name}")
        await self._listener.on_download_error("Stopped by User!")
