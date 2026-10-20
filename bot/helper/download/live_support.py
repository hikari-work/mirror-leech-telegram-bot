"""Pure helpers for `/live`: the menu, ffmpeg commands, the slot cap, a session.

Nothing here imports ``bot``: the tests load this file straight from disk. The
heavy listener in ``live_record`` imports *from* here, not the other way
around, so this module can be exercised without uvloop, pyrogram or a bot
token.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

MAX_ROOMS_PER_PAGE = 10
MAX_RECONNECT_EMPTY = 5
STREAM_EXPIRY_MARGIN = 45
MIN_SESSION_SECONDS = 30
STOP_GRACE_SECONDS = 15
BUTTON_LABEL_MAX = 32

STREAM_PROTOCOLS = "rtmp,rtmps,http,https,tcp,tls"
READ_TIMEOUT_US = 15_000_000

_TAIL_LINES = 20
_CHUNK = 1024 * 1024
_INVALID_FILENAME = re.compile(r"[^A-Za-z0-9._-]+")

_FFMPEG = (
    "ffmpeg",
    "-nostdin",
    "-hide_banner",
    "-loglevel",
    "warning",
    "-y",
)
_STREAM_MAP = ("-map", "0:v:0?", "-map", "0:a:0?")
_CONCAT_FLAGS = (
    "-avoid_negative_ts",
    "make_zero",
    "-movflags",
    "+faststart",
)


class RoomLike(Protocol):
    """What the menu needs from a room; read-only so a frozen dataclass fits."""

    @property
    def anchor_id(self) -> str: ...

    @property
    def nickname(self) -> str: ...


@dataclass(frozen=True)
class MenuButton:
    label: str
    data: str


@dataclass(frozen=True)
class RoomMenu:
    rows: tuple[MenuButton, ...]
    footer: tuple[MenuButton, ...]


def _label(room: RoomLike) -> str:
    label = (room.nickname or "").strip() or f"anchor {room.anchor_id}"
    if len(label) > BUTTON_LABEL_MAX:
        label = label[: BUTTON_LABEL_MAX - 1].rstrip() + "…"
    return label


def room_menu(
    rooms: Iterable[RoomLike], page: int, pages: int, user_id: int
) -> RoomMenu:
    """One button per room (Telegram caps a keyboard at 100 buttons).

    The page footer repeats the owner's id in every callback so the handler can
    tell a stranger's press from the owner's without keeping server state.
    """
    rows = tuple(
        MenuButton(
            label=_label(room),
            data=f"live room {user_id} {room.anchor_id}",
        )
        for room in list(rooms)[:MAX_ROOMS_PER_PAGE]
    )
    pages = max(pages, 1)
    page = min(max(page, 1), pages)
    footer = (
        MenuButton(label="‹", data=f"live page {user_id} {max(page - 1, 1)}"),
        MenuButton(label=f"{page}/{pages}", data=f"live noop {user_id}"),
        MenuButton(label="›", data=f"live page {user_id} {min(page + 1, pages)}"),
        MenuButton(label="Close", data=f"live close {user_id}"),
    )
    return RoomMenu(rows=rows, footer=footer)


def menu_text(total: int, page: int, pages: int) -> str:
    return (
        "<b>Live rooms</b>\n\n"
        f"{total} room(s) are live right now.\n"
        f"Page {page}/{max(pages, 1)}\n\n"
        "Pick a nickname to start recording."
    )


def safe_recording_stem(nickname: str, anchor_id: str, when: datetime) -> str:
    """A filename stem that cannot leave the download directory.

    `nickname` is chosen by the broadcaster, so it is not trusted with path
    separators or leading dots; when nothing usable survives, the numeric
    anchor id is used instead.
    """
    stem = _INVALID_FILENAME.sub("_", nickname or "").strip("._-")
    stem = stem[:60].rstrip("._-")
    if not stem:
        stem = _INVALID_FILENAME.sub("_", anchor_id).strip("._-") or "live"
    return f"{stem}_{when:%Y%m%d-%H%M%S}"


def build_record_command(url: str, output_ts: str) -> list[str]:
    """Record *url* to MPEG-TS without re-encoding.

    MPEG-TS survives an interrupted write (a killed MP4 would not), and the
    remux to MP4 happens once, after the user stops. ``-rw_timeout`` keeps a
    stalled CDN read from hanging the process forever.
    """
    return [
        *_FFMPEG,
        "-rw_timeout",
        str(READ_TIMEOUT_US),
        "-protocol_whitelist",
        STREAM_PROTOCOLS,
        "-i",
        url,
        *_STREAM_MAP,
        "-c",
        "copy",
        "-f",
        "mpegts",
        output_ts,
    ]


def build_remux_command(source: str, output_mp4: str) -> list[str]:
    return [
        *_FFMPEG,
        "-i",
        source,
        *_STREAM_MAP,
        "-c",
        "copy",
        *_CONCAT_FLAGS,
        output_mp4,
    ]


def build_concat_command(listing: str, output_mp4: str) -> list[str]:
    """Join reconnected parts in one pass; the demuxer reads ``listing``."""
    return [
        *_FFMPEG,
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        listing,
        *_STREAM_MAP,
        "-c",
        "copy",
        *_CONCAT_FLAGS,
        output_mp4,
    ]


def _concat_escape(path: str) -> str:
    return path.replace("'", r"'\''")


def write_concat_list(parts: Iterable[str], listing: str) -> None:
    with open(listing, "w", encoding="utf-8") as handle:
        for part in parts:
            handle.write(f"file '{_concat_escape(part)}'\n")


def concat_parts(parts: Iterable[str], output: str) -> int:
    """Append the parts byte by byte (blocking; call it in a thread).

    The demuxer path is tried first because it rewrites timestamps; this is the
    fallback for recordings whose timestamps it refuses, where a raw TS join is
    still watchable. Returns the total number of bytes written.
    """
    total = 0
    with open(output, "wb") as target:
        for part in parts:
            try:
                source = open(part, "rb")
            except OSError:
                continue
            with source:
                while chunk := source.read(_CHUNK):
                    target.write(chunk)
                    total += len(chunk)
    return total


class LiveSlots:
    """The concurrent-recording cap, counted per user.

    A token is claimed before ffmpeg starts and released only after the upload
    finishes (the listener releases it in its ``finally``), so a user cannot
    start a sixth recording while five are still uploading. A limit of 0 means
    unlimited, which is the documented meaning of ``MAX_LIVE_RECORDS = 0``.
    """

    def __init__(self, limit_getter: Callable[[], int] | None = None) -> None:
        self._limit = limit_getter or _no_limit
        self._lock = asyncio.Lock()
        self._active: dict[int, set[str]] = {}
        self._counter = 0

    async def claim(self, user_id: int) -> str | None:
        async with self._lock:
            held = self._active.setdefault(user_id, set())
            limit = self._limit()
            if limit > 0 and len(held) >= limit:
                return None
            # Monotonic, not size-derived: releasing a middle token must not
            # make the next claim reuse a token still in the set.
            self._counter += 1
            token = f"{user_id}:{self._counter}"
            held.add(token)
            return token

    async def release(self, user_id: int, token: str) -> None:
        async with self._lock:
            held = self._active.get(user_id)
            if not held:
                return
            held.discard(token)
            if not held:
                self._active.pop(user_id, None)

    def count(self, user_id: int) -> int:
        return len(self._active.get(user_id, ()))


def _no_limit() -> int:
    return 0


class RecordSession:
    """One ffmpeg process: start it, and stop it without losing the file.

    Stopping sends SIGINT rather than killing: ffmpeg flushes the MPEG-TS tail
    on SIGINT, then exits within the grace period. If it does not, SIGKILL is
    the only remaining option and the bytes already on disk are still valid TS.
    """

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._tail: deque[str] = deque(maxlen=_TAIL_LINES)
        self.bytes_written = 0

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def run(
        self,
        url: str,
        output: str,
        max_seconds: float,
        stop_event: asyncio.Event,
    ) -> int:
        process = await asyncio.create_subprocess_exec(
            *build_record_command(url, output),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._process = process
        drain = asyncio.create_task(self._drain(process.stderr))
        exited = asyncio.create_task(process.wait())
        stopped = asyncio.create_task(stop_event.wait())
        try:
            await asyncio.wait(
                {exited, stopped},
                timeout=max(float(max_seconds), float(MIN_SESSION_SECONDS)),
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if process.returncode is None:
                await self._terminate(process)
            for task in (exited, stopped, drain):
                task.cancel()
            await asyncio.gather(exited, stopped, drain, return_exceptions=True)
            self._process = None
        self.bytes_written = file_size(output)
        return self.bytes_written

    async def _drain(self, stream: asyncio.StreamReader | None) -> None:
        """Keep the pipe moving; a full stderr buffer would stall ffmpeg."""
        if stream is None:
            return
        try:
            while line := await stream.readline():
                self._tail.append(line.decode("utf-8", "replace").strip())
        except (OSError, ValueError):
            # The pipe is closed while we tear the process down; nothing to do.
            return

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        try:
            process.send_signal(signal.SIGINT)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), STOP_GRACE_SECONDS)
        except TimeoutError:
            process.kill()
            await process.wait()


def file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0
