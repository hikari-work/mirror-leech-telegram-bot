"""The ``/live`` task: record a room with ffmpeg, then upload it as video.

A live recording is a task whose download ends when the user says so. Rather
than a second pipeline, this listener reuses ``TaskListener``: recording is the
download, ``_finalize`` leaves exactly one file on disk, and
``on_download_complete`` then runs the existing split/upload/completion path.
That is what gives ``/status``, ``/cancel``, thumbnails, splits and the
completion message for free.

Stopping is deliberately *not* ``is_cancelled``: the recording is the product,
so the task has to survive the stop and reach the uploader. Only a cancel that
arrives while remuxing or uploading is a real cancellation.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from os import path as ospath
from time import time

from aiofiles.os import listdir, makedirs, remove
from aiofiles.os import path as aiopath
from pyrogram import Client
from pyrogram.types import Message, User

from ... import DOWNLOAD_DIR, LOGGER, task_dict, task_dict_lock, user_data
from ..listeners.task_listener import TaskListener
from ..net import matok_live
from ..progress.live_status import LiveRecordStatus
from ..task.settings_resolver import BOT_MAX_SPLIT_SIZE
from ..telegram.message_utils import send_status_message
from ..util.subproc_runner import run_subproc
from .live_support import (
    MAX_RECONNECT_EMPTY,
    MIN_SESSION_SECONDS,
    STREAM_EXPIRY_MARGIN,
    LiveSlots,
    RecordSession,
    build_concat_command,
    build_remux_command,
    concat_parts,
    file_size,
    safe_recording_stem,
    write_concat_list,
)

FALLBACK_WINDOW = 600.0
"""Seconds a session may run when the gateway did not say when the URL expires."""


class LiveRecordTask(TaskListener):
    """One recording, from the first byte to the uploaded file."""

    def __init__(
        self,
        message: Message,
        client: Client,
        user: User,
        anchor_id: str,
        room_name: str,
        initial_url: str,
        initial_expires: int,
        slots: LiveSlots,
        slot: str,
    ) -> None:
        # Read by ``TaskConfig.__init__``: the status message is the task's
        # identity, and a live task is never part of a bulk or a same-dir group.
        self.message = message
        self.client = client
        self.multi_tag = None
        self.bulk = []
        self.same_dir = {}
        super().__init__()

        # The message was sent by the bot on the user's behalf, so the identity
        # TaskConfig derived from it is the bot's. The task belongs to whoever
        # pressed the button, which is what /cancel and the per-user cap read.
        self.user = user
        self.user_id = user.id
        self.user_dict = user_data.get(user.id, {})

        self.anchor_id = anchor_id
        self.room_name = room_name or f"anchor {anchor_id}"
        self.phase = "recording"
        self.progress = False
        self.name = self.room_name
        self.dir = f"{DOWNLOAD_DIR}live_{self.mid}_{anchor_id}"
        self._gid = f"live{self.mid}"
        self._stem = safe_recording_stem(self.room_name, anchor_id, datetime.now())
        self._initial_url = initial_url
        self._initial_expires = initial_expires
        self._slots = slots
        self._slot = slot
        self._stop_event = asyncio.Event()
        self._parts: list[str] = []
        self._active_part: str | None = None
        self._part_no = 1
        self._started_at = time()
        self._terminal = False

    # ── what the status line reads ───────────────────────────────────

    @property
    def gid(self) -> str:
        return self._gid

    def recorded_bytes(self) -> int:
        total = sum(file_size(part) for part in self._parts)
        if self._active_part:
            total += file_size(self._active_part)
        return total

    def elapsed_seconds(self) -> int:
        return int(time() - self._started_at)

    def request_stop(self) -> None:
        """Ask the running session to finish; the recording is then uploaded."""
        self._stop_event.set()

    # ── the task ─────────────────────────────────────────────────────

    async def run(self) -> None:
        try:
            try:
                await self.before_start()
            except Exception as error:
                await self._fail(str(error))
                return
            await self.get_tag([])
            if not self.split_size:
                # ``proceed_split`` divides by this; a config that resolved to
                # "no split" must not become a ZeroDivisionError at upload time.
                self.split_size = self.max_split_size or BOT_MAX_SPLIT_SIZE
            await makedirs(self.dir, exist_ok=True)

            async with task_dict_lock:
                task_dict[self.mid] = LiveRecordStatus(
                    self, self, self._gid, "Recording"
                )
            await self.on_download_start()
            await send_status_message(self.message)

            await self._record_until_stopped()
            if self.is_cancelled:
                return
            await self._finalize()
            if self.is_cancelled:
                return
            await self.on_download_complete()
        except Exception as error:
            LOGGER.error(f"Live recording {self.mid} failed: {error}")
            await self._fail(f"Live recording failed: {error}")
        finally:
            await self._slots.release(self.user_id, self._slot)

    async def _record_until_stopped(self) -> None:
        """Record until the user stops, the URL cannot be refreshed, or it dies."""
        self._started_at = time()
        url, expires_at = self._initial_url, self._initial_expires
        empty_streak = 0
        while not self.is_cancelled and not self._stop_event.is_set():
            part = f"{self.dir}/part-{self._part_no:03d}.ts"
            self._part_no += 1
            self._active_part = part
            written = await RecordSession().run(
                url, part, self._session_window(expires_at), self._stop_event
            )
            self._active_part = None
            if written > 0:
                self._parts.append(part)
                empty_streak = 0
            else:
                empty_streak += 1
                await _remove_if_exists(part)
            if self.is_cancelled or self._stop_event.is_set():
                break
            if empty_streak >= MAX_RECONNECT_EMPTY:
                self._notice("the stream sent no data after several attempts")
                break
            try:
                # The signed URL lives about ten minutes; ask for the next one
                # before the current one lapses rather than after ffmpeg dies.
                fresh = await matok_live.stream(self.anchor_id)
            except matok_live.LiveError as error:
                if self._parts:
                    self._notice(error.friendly)
                    break
                raise
            url, expires_at = fresh.url, fresh.expires_at
            await self._pause(1.0)

    def _session_window(self, expires_at: int) -> float:
        if expires_at <= 0:
            return FALLBACK_WINDOW
        return max(expires_at - time() - STREAM_EXPIRY_MARGIN, MIN_SESSION_SECONDS)

    async def _pause(self, seconds: float) -> None:
        """Sleep, but wake up at once when the user stops the recording."""
        try:
            await asyncio.wait_for(self._stop_event.wait(), seconds)
        except TimeoutError:
            pass

    async def _finalize(self) -> None:
        """Leave exactly one playable file in ``self.dir``.

        The uploader walks the whole directory and ``_resolve_download_path``
        picks a name from it, so intermediates are deleted *before* the
        pipeline is handed control -- otherwise the parts and the concat list
        would be uploaded alongside the recording.
        """
        self.phase = "finalizing"
        parts = [part for part in self._parts if file_size(part) > 0]
        self._parts = parts
        if not parts:
            raise RuntimeError(
                "nothing was recorded -- the stream ended before any data arrived"
            )

        output = await self._produce_output(parts)
        if output is None:
            return
        await self._purge(output)
        self.name = ospath.basename(output)
        self.size = file_size(output)
        self.is_file = True

    async def _produce_output(self, parts: list[str]) -> str | None:
        """MP4 if ffmpeg can make one, the untouched recording if it cannot."""
        mp4 = f"{self.dir}/{self._stem}.mp4"
        source = parts[0]

        if len(parts) > 1:
            listing = f"{self.dir}/concat.txt"
            write_concat_list(parts, listing)
            code, _ = await run_subproc(self, build_concat_command(listing, mp4))
            if self.is_cancelled:
                return None
            if code == 0 and file_size(mp4) > 0:
                return mp4
            # The demuxer refuses recordings whose timestamps it cannot line
            # up; a raw byte join is not pretty but it is still the session.
            joined = f"{self.dir}/{self._stem}.ts"
            LOGGER.info(
                f"Concat demuxer failed for {self.name} (exit {code}); "
                "joining the parts byte by byte"
            )
            if await asyncio.to_thread(concat_parts, parts, joined) > 0:
                source = joined
            else:
                self._notice("joining the recording parts failed")

        if await self._remux(source, mp4):
            return mp4
        if self.is_cancelled:
            return None
        self._notice("remux to MP4 failed, so the raw .ts recording is attached")
        return source

    async def _remux(self, source: str, output: str) -> bool:
        code, _ = await run_subproc(self, build_remux_command(source, output))
        return code == 0 and file_size(output) > 0

    async def _purge(self, keep: str) -> None:
        for entry in await listdir(self.dir):
            path = f"{self.dir}/{entry}"
            if path != keep:
                await _remove_if_exists(path)

    def _notice(self, reason: str) -> None:
        self.stream_notices.append(f"ℹ️ Recording stopped early: {reason}.")

    async def _fail(self, error: str) -> None:
        """Report a failure once, unless an upload path already reported it."""
        if self._terminal:
            return
        self._terminal = True
        await self.on_download_error(error)

    # ``TaskListener``'s upload paths are what make a failure terminal: once
    # one of them has run, its own message is the report and the outer handler
    # must not send a second one.

    async def on_upload_complete(
        self,
        link: str | None,
        files: dict[str, str],
        folders: int,
        mime_type: int,
    ) -> None:
        self._terminal = True
        await super().on_upload_complete(link, files, folders, mime_type)

    async def on_upload_error(self, error: str, silent: bool = False) -> None:
        self._terminal = True
        await super().on_upload_error(error, silent)


async def _remove_if_exists(path: str) -> None:
    try:
        if await aiopath.exists(path):
            await remove(path)
    except OSError as error:
        LOGGER.warning(f"Could not remove {path}: {error}")
