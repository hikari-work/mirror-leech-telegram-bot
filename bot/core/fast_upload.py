"""Sending a file's parts over more than one connection.

kurigram pushes every part of every upload down a single media session.
``Client.get_session`` caches one session per datacenter and hands that same
one to every caller, ``save_file`` asks for it, and then feeds it from an
``asyncio.Queue(1)`` -- a queue that holds a single part -- with four worker
coroutines. Parts are 512 KiB, which is telegram's maximum, so the only levers
left are how many parts are in flight and how many connections carry them.
Neither was enough: on a link measured at 247 Mbit/s the bot uploaded at under
15, and ``ss`` inside the container showed exactly one connection to the media
datacenter.

So the whole-file upload of a big file is taken over here. A pool of extra
media sessions is opened once per client and datacenter, parts are read off the
disk in a worker thread rather than on the event loop, and enough of them are
kept moving to cover the round trip instead of one.

Everything else is handed straight back to kurigram: a small file is one or two
round trips either way, and the single-part repair that ``send_*`` performs
after ``FilePartMissing`` has fiddly semantics -- it returns None rather than an
``InputFile`` -- that are not worth restating for no gain.

Set ``MLTB_FAST_UPLOAD=0`` to leave kurigram's own path in place.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import math
import os
from logging import getLogger
from pathlib import PurePath
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

from pyrogram import Client, StopTransmission, raw

if TYPE_CHECKING:
    from collections.abc import Callable

    from pyrogram.session import Session

LOGGER = getLogger(__name__)

# Telegram rejects a larger part, so the only way to push harder is to keep
# more of them moving at once.
_PART_SIZE = 512 * 1024

# What kurigram itself calls a big file, and the line below which this module
# hands the upload back. Telegram also switches RPC at exactly this size --
# under it a file goes up with ``SaveFilePart`` and carries an md5 -- so
# following the same boundary keeps both paths on the RPC they were written for.
_BIG_FILE = 10 * 1024 * 1024

_DEFAULT_SESSIONS = 4
# Extra connections past this stop buying throughput and start looking like a
# reason for telegram to rate limit the account.
_MAX_SESSIONS = 8

# Parts in flight per session. Four 512 KiB parts is 2 MiB per connection,
# which keeps the pipe full across a round trip of well over a second.
_PARTS_PER_SESSION = 4

# kurigram's own ``save_file``, kept so the paths this module declines -- and
# any upload whose extra sessions could not be opened -- still work.
_original_save_file: Callable[..., Any] | None = None

# Extra media sessions per client, then per datacenter. Keyed weakly so a
# per-user client that is dropped does not keep its sessions alive; the
# sessions themselves are closed by ``discard``, which startup wires into the
# points where a client stops or restarts.
_pools: WeakKeyDictionary[Client, dict[int, list[Session]]] = WeakKeyDictionary()
_pools_guard = asyncio.Lock()


def _session_count() -> int:
    """How many media sessions to open, from the environment or the default."""
    try:
        count = int(os.environ.get("MLTB_FAST_UPLOAD_SESSIONS", ""))
    except ValueError:
        return _DEFAULT_SESSIONS
    return max(1, min(count, _MAX_SESSIONS))


async def _media_sessions(client: Client, dc_id: int, size: int) -> list[Session]:
    """This client's extra media sessions, opening them the first time.

    ``temporary=True`` is what makes these independent: it reuses the auth key
    of the client's main session, so no authorization has to be exported, but
    skips kurigram's per-datacenter cache and hands back a session of our own.

    Returns as many as could be opened, which may be none -- a caller that gets
    an empty list has nothing to gain here and falls back to kurigram.
    """
    async with _pools_guard:
        by_dc = _pools.setdefault(client, {})
        sessions = by_dc.setdefault(dc_id, [])
        while len(sessions) < size:
            try:
                sessions.append(
                    await client.get_session(dc_id, is_media=True, temporary=True)
                )
            except Exception as e:
                # One connection short is worth uploading over; the pool is
                # whatever did open, and the next file tries for the rest.
                LOGGER.warning(f"Could not open a media session to DC {dc_id}: {e}")
                break
        return list(sessions)


async def discard(client: Client) -> None:
    """Close this client's extra sessions and forget them.

    Called when an upload fails, because the likeliest reason is a connection
    that died and a dead session would fail every later upload the same way,
    and when a client stops or restarts, because these sessions are not in
    kurigram's caches and nothing else would ever close them.
    """
    async with _pools_guard:
        by_dc = _pools.pop(client, None)
    for sessions in (by_dc or {}).values():
        for session in sessions:
            try:
                await session.stop()
            except Exception as e:
                LOGGER.warning(f"While closing a media session: {e}")


class _Progress:
    """Reports acknowledged parts to the caller's callback, in order.

    Parts finish out of order, so this counts the ones that telegram has
    acknowledged rather than the offset of whichever just landed -- the number
    only ever climbs. It also means progress reflects what is actually on
    telegram, where kurigram reported parts as it queued them.

    The lock is what makes that true of the calls and not just the count.
    Several parts land at once, and a synchronous callback is handed to a
    thread pool, which is free to run two of them in either order -- so without
    it a report of part 7 can arrive after part 8 and walk the displayed
    progress backwards. Serialising costs nothing here: it is one counter
    update every 512 KiB.
    """

    def __init__(
        self,
        client: Client,
        file_size: int,
        progress: Callable[..., Any] | None,
        progress_args: tuple,
    ):
        self._client = client
        self._file_size = file_size
        self._progress = progress
        self._args = progress_args
        self._awaitable = inspect.iscoroutinefunction(progress)
        self._lock = asyncio.Lock()
        self._done = 0

    async def part_done(self) -> None:
        async with self._lock:
            self._done += 1
            if self._progress is None:
                return
            func = functools.partial(
                self._progress,
                min(self._done * _PART_SIZE, self._file_size),
                self._file_size,
                *self._args,
            )
            if self._awaitable:
                await func()
            else:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(self._client.executor, func)


class _PartSender:
    """Pushes one file's parts to telegram over a pool of media sessions.

    Backpressure is the semaphore, acquired by the reader before a part is read
    and released by the send that part started: at most one permit's worth of
    data is ever in memory, so a 40 GiB file costs the same as a 40 MiB one.
    Parts are dealt to the sessions round robin.

    The first failure -- including the ``StopTransmission`` a progress callback
    raises to cancel -- is kept and re-raised once the sends still running have
    finished, so nothing is left writing to a session after ``run`` returns.
    """

    def __init__(self, sessions: list[Session], reporter: _Progress):
        self._sessions = sessions
        self._reporter = reporter
        self._in_flight = asyncio.Semaphore(len(sessions) * _PARTS_PER_SESSION)
        self._tasks: set[asyncio.Task] = set()
        self._failure: BaseException | None = None

    async def _send(self, session: Session, rpc: Any) -> None:
        try:
            await session.invoke(rpc)
            await self._reporter.part_done()
        except BaseException as e:
            # Kept rather than raised: this runs in its own task, so the only
            # way out is the attribute the reader and ``run`` both read.
            if self._failure is None:
                self._failure = e
        finally:
            self._in_flight.release()

    def _start(self, part: int, total_parts: int, file_id: int, chunk: bytes) -> None:
        task = asyncio.get_running_loop().create_task(
            self._send(
                self._sessions[part % len(self._sessions)],
                raw.functions.upload.SaveBigFilePart(
                    file_id=file_id,
                    file_part=part,
                    file_total_parts=total_parts,
                    bytes=chunk,
                ),
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _next_chunk(self, fp: Any) -> bytes:
        """The next part, once there is room in flight for it; b"" to stop.

        The read is handed to a thread: it is a blocking 512 KiB off a disk
        that is usually also being written to by a download, and on the event
        loop it would stall every other transfer the bot has open.
        """
        await self._in_flight.acquire()
        if self._failure is not None:
            self._in_flight.release()
            return b""
        loop = asyncio.get_running_loop()
        chunk = await loop.run_in_executor(None, fp.read, _PART_SIZE)
        if not chunk:
            self._in_flight.release()
        return chunk

    async def run(self, fp: Any, file_size: int, file_id: int) -> int:
        total_parts = math.ceil(file_size / _PART_SIZE)
        part = 0
        while chunk := await self._next_chunk(fp):
            self._start(part, total_parts, file_id, chunk)
            part += 1
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._failure is not None:
            raise self._failure
        return total_parts


async def _upload_parts(
    client: Client,
    fp: Any,
    file_size: int,
    file_id: int,
    sessions: list[Session],
    progress: Callable[..., Any] | None,
    progress_args: tuple,
) -> int:
    """Put every part of ``fp`` on telegram, several at a time, and count them."""
    reporter = _Progress(client, file_size, progress, progress_args)
    return await _PartSender(sessions, reporter).run(fp, file_size, file_id)


async def _save_file(
    self: Client,
    path: Any,
    file_id: int | None = None,
    file_part: int = 0,
    progress: Callable[..., Any] | None = None,
    progress_args: tuple = (),
) -> Any:
    """``Client.save_file``, over several connections for a big local file.

    Anything else -- an in-memory file object, a small file, the single-part
    repair after ``FilePartMissing`` -- is kurigram's to handle, unchanged.
    """
    fallback = _original_save_file
    assert fallback is not None  # set by install(), which is what patches this in
    if path is None:
        return None
    if (
        file_id is not None
        or file_part
        or not isinstance(path, (str, PurePath))
        or not os.path.isfile(path)
    ):
        return await fallback(self, path, file_id, file_part, progress, progress_args)

    file_size = os.path.getsize(path)
    if file_size <= _BIG_FILE:
        return await fallback(self, path, file_id, file_part, progress, progress_args)

    async with self.save_file_semaphore:
        limit_mib = 4000 if self.me and self.me.is_premium else 2000
        if file_size > limit_mib * 1024 * 1024:
            raise ValueError(f"Can't upload files bigger than {limit_mib} MiB")

        dc_id = await self.storage.dc_id()
        sessions = await _media_sessions(self, dc_id, _session_count())
        if not sessions:
            return await fallback(self, path, None, 0, progress, progress_args)

        new_file_id = self.rnd_id()
        fp = open(path, "rb")
        # Read before the upload, because ``fp`` is closed by the time the
        # result is built. kurigram sends the path it opened, so this does too.
        file_name = getattr(fp, "name", "file.jpg")
        try:
            parts = await _upload_parts(
                self, fp, file_size, new_file_id, sessions, progress, progress_args
            )
        except StopTransmission:
            # The upload was cancelled on purpose; the sessions are fine.
            raise
        except Exception:
            # A part failed, and a session that lost its connection would fail
            # every later upload too. Drop the pool so the next one reconnects.
            await discard(self)
            raise
        finally:
            fp.close()

        return raw.types.InputFileBig(
            id=new_file_id,
            parts=parts,
            name=file_name,
        )


def install() -> bool:
    """Patch ``Client.save_file``. Returns whether the fast path is in place.

    Patched on the class rather than per client so every upload gets it: the
    bot, the USER_SESSION_STRING client, and the per-user clients started on
    demand. Idempotent, because a restart re-runs startup.
    """
    global _original_save_file
    if _original_save_file is not None:
        return True
    if os.environ.get("MLTB_FAST_UPLOAD", "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        LOGGER.info("Fast upload is off; uploads use kurigram's own save_file")
        return False
    _original_save_file = Client.save_file
    Client.save_file = _save_file  # pyrefly: ignore[bad-assignment]
    LOGGER.info(
        f"Fast upload installed: up to {_session_count()} media sessions per DC, "
        f"{_PARTS_PER_SESSION} parts in flight each"
    )
    return True
