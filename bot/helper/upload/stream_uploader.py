"""Upload each finished file the moment it lands, outside the upload queue.

``-su`` means a task never waits for an upload slot: the downloader hands a path
over as soon as a file is complete, and this component sends it while the next
file downloads. That is what keeps an album's disk usage at one or two files
instead of its whole size, and what keeps a full ``QUEUE_UPLOAD`` from stalling
a download that has nothing left to fetch.

Two things make this more than a loop around
``TelegramUploader.upload_single``:

* The uploader is not concurrency-safe -- ``_up_path``, ``_error``,
  ``_total_files`` and ``_msgs_dict`` are one set of instance state -- while a
  downloader may finish its files from several coroutines at once (Mega gathers
  them). Every path therefore goes through exactly one consumer coroutine, and
  the queue in front of it is the only way in.
* That queue is bounded, so a producer which outruns Telegram stops downloading
  instead of filling the disk. One file uploads while one waits its turn.

A stream task keeps the download slot it was admitted on for its whole life --
it is still downloading its next file -- and gives it back when the task ends,
in ``TaskListener.on_upload_complete``. It takes no upload slot at all: it
neither waits for one nor counts against one, which is the point of the flag.
"""

from __future__ import annotations

from asyncio import Queue, Task, create_task, gather
from os import path as ospath
from typing import Any

from ... import LOGGER

# (attribute, flag) pairs whose work a stream cannot do, grouped by why. Kept as
# one table so the notice a user reads and the attributes actually cleared
# cannot drift apart.
_POST_PROCESSING: tuple[tuple[str, str, Any], ...] = (
    ("extract", "-e", False),
    ("compress", "-z", False),
    ("join", "-j", False),
    ("sample_video", "-sv", False),
    ("screen_shots", "-ss", False),
    ("convert_audio", "-ca", ""),
    ("convert_video", "-cv", ""),
    ("name_sub", "-ns", ""),
    ("ffmpeg_cmds", "-ff", frozenset()),
)

_DROPPED_BY_STREAM: tuple[tuple[str, tuple[tuple[str, str, Any], ...]], ...] = (
    (
        "post-processing is skipped, because a file is gone as soon as it is sent",
        _POST_PROCESSING,
    ),
    (
        "files are sent as they land, so they are not split first",
        (("split_size", "-sp", 0),),
    ),
    (
        # not a preference: the seed branch of ``on_upload_complete`` deletes an
        # upload directory a stream does not have, and returns before the task
        # is taken off the status list, so leaving it on strands the task
        "a streamed task never seeds, because the files do not stay on disk",
        (("seed", "-d", False),),
    ),
)

_MERGE_DROPPED = "files are sent one by one, so they are not merged into one folder"


class StreamUploader:
    """Drive one ``TelegramUploader`` from a producer that keeps producing files.

    ``start`` has to run before anything else, and a ``False`` from it means the
    uploader could not announce the task -- it has already said so in the chat,
    so the caller only has to stop.
    """

    def __init__(
        self,
        listener: Any,
        path: str,
        *,
        uploader: Any = None,
        queue_size: int = 1,
    ) -> None:
        self._listener = listener
        self._path = path
        self._uploader = uploader
        self._queue: Queue[str | None] = Queue(maxsize=max(1, queue_size))
        self._consumer: Task[None] | None = None
        self._closed = False
        self._sent = 0

    @property
    def notices(self) -> list[str]:
        """What this task dropped, one line per group, for the task's message.

        This is the listener's own list, not a copy: the downloader that builds
        the uploader and the ``on_upload_complete`` that renders the message are
        different places, and one list is what keeps them from disagreeing.
        """
        return self._listener.stream_notices

    async def start(self) -> bool:
        """Settle the task for streaming and set the consumer going.

        The uploader is built here rather than in each downloader so the import
        stays lazy -- it reaches back into this package -- and so tests can hand
        one in.
        """
        self._apply_policy()
        await self._leave_same_dir()
        if self._uploader is None:
            from .telegram_uploader import TelegramUploader

            self._uploader = TelegramUploader(self._listener, self._path)
        if not await self._uploader.init_stream():
            return False
        self._consumer = create_task(self._consume())
        return True

    async def submit(self, file_path: str) -> None:
        """Hand over one finished file, waiting if the uploader is behind.

        Called by the downloader the moment a file is complete. Waiting here is
        the backpressure: the next download does not start until the queue has
        room for this file.
        """
        if self._closed or self._consumer is None:
            LOGGER.info(
                f"Stream upload is not running, dropping {ospath.basename(file_path)}"
            )
            return
        await self._queue.put(file_path)

    async def drain(self) -> None:
        """Let everything already submitted finish, then stop the consumer.

        Idempotent, and safe to call from a ``finally``. It has to empty the
        queue rather than cancel it: a producer waiting in ``submit`` for room
        would otherwise never be let go, and the downloader would hang on an
        upload that is no longer coming.
        """
        if self._closed:
            return
        self._closed = True
        await self._queue.put(None)
        if self._consumer is not None:
            await gather(self._consumer, return_exceptions=True)
            self._consumer = None

    async def finalize(self) -> None:
        """Drain, then let the uploader report the task as complete.

        A cancelled task reports nothing: the cancel has already answered the
        user, and the uploader would add a second, contradictory message.
        """
        await self.drain()
        if self._listener.is_cancelled:
            return
        await self._uploader.finalize_stream()

    # ── what streaming cannot do ────────────────────────────────────

    def _apply_policy(self) -> None:
        """Clear the flags whose work a stream skips, and say which they were.

        Naming them is the point: a user who asked for a zip and did not get one
        has to read why in the task's own message.

        Clearing is unconditional once an attribute is truthy, and it has to be
        -- the seed branch of ``on_upload_complete`` expects files still on disk
        and an upload directory, neither of which a stream has, and it returns
        before the task is taken off the status list. ``-d``, the only way that
        attribute becomes true, is therefore in the table rather than handled
        separately.
        """
        for reason, entries in _DROPPED_BY_STREAM:
            dropped = []
            for attr, flag, cleared in entries:
                if getattr(self._listener, attr, False):
                    dropped.append(flag)
                    setattr(self._listener, attr, cleared)
            if dropped:
                self._note(dropped, reason)

    async def _leave_same_dir(self) -> None:
        """Drop out of a same-dir group before anything is downloaded.

        A group uploads one merged folder once every member has finished, which
        is the opposite of sending each file as it lands -- and a member that
        stayed registered would keep the group's count from ever reaching zero,
        stranding the staging directory with nobody left to upload it.
        """
        listener = self._listener
        group = (
            listener.same_dir.get(listener.folder_name)
            if listener.folder_name
            else None
        )
        if not group or listener.mid not in group["tasks"]:
            return
        await listener.remove_from_same_dir()
        self._note(["-m"], _MERGE_DROPPED)

    def _note(self, flags: list[str], reason: str) -> None:
        text = f"Note: {', '.join(flags)} ignored: {reason}."
        self._listener.stream_notices.append(text)
        LOGGER.warning(text)

    # ── the consumer ────────────────────────────────────────────────

    async def _consume(self) -> None:
        """Send the submitted files one at a time, in the order they arrived.

        The previous upload is awaited before the next one starts, which is what
        keeps the sender's instance state from being written twice at once; the
        path already taken off the queue is what lets a download run during an
        upload.

        ``return_exceptions`` is not redundant with the seal in ``_upload_one``:
        a consumer that dies leaves the producer waiting for room in a queue
        nobody drains, which is a hang rather than a failed task.
        """
        upload_task: Task[None] | None = None
        while True:
            file_path = await self._queue.get()
            if file_path is None:
                break
            if self._listener.is_cancelled:
                # keep draining: the producer may be sitting in ``submit``
                continue
            if upload_task is not None:
                await gather(upload_task, return_exceptions=True)
                upload_task = None
            if self._listener.is_cancelled:
                continue
            upload_task = create_task(self._upload_one(file_path))
        if upload_task is not None:
            await gather(upload_task, return_exceptions=True)

    async def _upload_one(self, file_path: str) -> None:
        """Send one file, sealing the failure at the file's own boundary.

        A file that cannot be sent must cost that file and nothing else: the
        rest of the album is still worth uploading, and the uploader counts the
        failure towards the completion message. Sealing it here is also what
        gives the operator a log line naming the file.
        """
        self._sent += 1
        LOGGER.info(f"Stream upload [{self._sent}]: {ospath.basename(file_path)}")
        try:
            await self._uploader.upload_single(file_path)
        except Exception as e:
            LOGGER.error(f"Stream upload failed for {file_path}. Error: {e}")
