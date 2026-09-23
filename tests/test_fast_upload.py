"""Unit tests for the multi-session upload path.

These build no client and open no socket: ``_save_file`` only ever asks its
``self`` for a semaphore, a datacenter id, a random id and some sessions, so a
handful of fakes is enough to drive every branch. What is actually being
checked is that the bytes of a file land on telegram exactly once, in the right
parts, spread over every session, without the reader ever running further ahead
than the semaphore allows.
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import PurePath

import pytest
from pyrogram import StopTransmission

from bot.core import fast_upload


@pytest.fixture(autouse=True)
def _clean_pools():
    """Keep the module-level session pool from leaking between tests."""
    fast_upload._pools.clear()
    yield
    fast_upload._pools.clear()


class _Session:
    """A media session that records the parts it was given.

    ``delay`` lets a test hold parts in flight long enough to observe how many
    the reader is willing to have outstanding at once.
    """

    def __init__(self, delay: float = 0.0, fail_on: int | None = None):
        self.parts: list[tuple[int, bytes]] = []
        self.delay = delay
        self.fail_on = fail_on
        self.stopped = False
        self.in_flight = 0

    async def invoke(self, rpc):
        self.in_flight += 1
        _Session.peak = max(getattr(_Session, "peak", 0), _total_in_flight())
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.fail_on is not None and rpc.file_part == self.fail_on:
                raise OSError("connection reset")
            self.parts.append((rpc.file_part, rpc.bytes))
        finally:
            self.in_flight -= 1

    async def stop(self):
        self.stopped = True


_ALL_SESSIONS: list[_Session] = []


def _total_in_flight() -> int:
    return sum(s.in_flight for s in _ALL_SESSIONS)


class _Storage:
    def __init__(self, dc_id: int = 2):
        self._dc_id = dc_id

    async def dc_id(self):
        return self._dc_id


class _Me:
    def __init__(self, is_premium: bool = False):
        self.is_premium = is_premium


class _Client:
    """Just the surface ``_save_file`` touches."""

    def __init__(self, sessions=None, is_premium=False, session_error=None):
        self.save_file_semaphore = asyncio.Semaphore(10)
        self.me = _Me(is_premium)
        self.storage = _Storage()
        self.executor = None
        self._sessions = sessions if sessions is not None else []
        self._session_error = session_error
        self.handed_out: list[_Session] = []
        self._next_id = 1000

    def rnd_id(self):
        self._next_id += 1
        return self._next_id

    async def get_session(self, dc_id, is_media=False, temporary=False):
        assert is_media and temporary, "the pool must not touch kurigram's cache"
        if self._session_error:
            raise self._session_error
        if not self._sessions:
            raise RuntimeError("no more sessions")
        session = self._sessions.pop(0)
        self.handed_out.append(session)
        return session


def _make_sessions(count, **kwargs):
    global _ALL_SESSIONS
    sessions = [_Session(**kwargs) for _ in range(count)]
    _ALL_SESSIONS = sessions
    _Session.peak = 0
    return sessions


@pytest.fixture
def fallback(monkeypatch):
    """Stand in for kurigram's ``save_file`` and record that it was reached."""
    calls = []

    async def _fallback(self, path, file_id, file_part, progress, progress_args):
        calls.append((path, file_id, file_part))
        return "fell-back"

    monkeypatch.setattr(fast_upload, "_original_save_file", _fallback)
    return calls


@pytest.fixture
def tiny(monkeypatch):
    """Shrink a "part" and a "big file" to a few bytes.

    The real numbers are 512 KiB parts and a 10 MiB threshold, so exercising
    the multi-part path honestly would mean writing tens of megabytes per test
    to cover exactly the same branches. Both are read from the module at call
    time, so moving them here changes nothing else.

    The threshold is put at one part rather than the real twenty so that a file
    of "a couple of parts" -- which is what these tests write -- is on the same
    side of it as a real big file. What the branch actually distinguishes is a
    file worth spreading over several connections from one that is a round trip
    either way, and one part against more than one says that just as well.
    """
    monkeypatch.setattr(fast_upload, "_PART_SIZE", 1024)
    monkeypatch.setattr(fast_upload, "_BIG_FILE", 1024)


def _write(tmp_path, size, name="big.bin"):
    path = tmp_path / name
    # Not all one byte: a part that got duplicated or reordered has to be
    # visible in the reassembled file, and zeros would hide both.
    path.write_bytes(bytes((i * 7 + 13) % 251 for i in range(size)))
    return path


# --- what this module declines to handle ---


async def test_small_file_falls_back(tmp_path, fallback):
    path = _write(tmp_path, 1024)
    client = _Client()
    assert await fast_upload._save_file(client, str(path)) == "fell-back"
    assert fallback == [(str(path), None, 0)]


async def test_file_exactly_at_the_threshold_falls_back(tmp_path, fallback, tiny):
    """The boundary is kurigram's: at 10 MiB a file still carries an md5."""
    path = _write(tmp_path, fast_upload._BIG_FILE)
    client = _Client()
    assert await fast_upload._save_file(client, str(path)) == "fell-back"
    assert fallback


async def test_part_repair_falls_back(tmp_path, fallback, tiny):
    """``FilePartMissing`` re-sends one part and returns None, not an InputFile."""
    path = _write(tmp_path, fast_upload._BIG_FILE + 1)
    client = _Client()
    result = await fast_upload._save_file(client, str(path), file_id=77, file_part=3)
    assert result == "fell-back"
    assert fallback == [(str(path), 77, 3)]


async def test_file_object_falls_back(fallback):
    """An in-memory upload has no path to re-read; kurigram streams it."""
    import io

    handle = io.BytesIO(b"x" * 32)
    client = _Client()
    assert await fast_upload._save_file(client, handle) == "fell-back"
    assert fallback


async def test_none_path_returns_none(fallback):
    client = _Client()
    assert await fast_upload._save_file(client, None) is None
    assert not fallback


async def test_falls_back_when_no_session_opens(tmp_path, fallback, tiny):
    """No extra connection means nothing to gain here."""
    path = _write(tmp_path, fast_upload._BIG_FILE + 1)
    client = _Client(session_error=RuntimeError("dc unreachable"))
    assert await fast_upload._save_file(client, str(path)) == "fell-back"
    assert fallback == [(str(path), None, 0)]


# --- the fast path ---


async def test_every_byte_goes_up_once_in_order(tmp_path, fallback, tiny):
    # Five full parts and a short one, expressed as a fraction of a part so the
    # count stays six whatever the part size is.
    size = fast_upload._PART_SIZE * 5 + fast_upload._PART_SIZE // 3
    path = _write(tmp_path, size)
    sessions = _make_sessions(4)
    client = _Client(sessions=list(sessions))

    result = await fast_upload._save_file(client, str(path))

    collected = sorted(
        (part, data) for s in sessions for part, data in s.parts
    )
    assert [p for p, _ in collected] == list(range(6)), "parts must be 0..n once each"
    assert b"".join(data for _, data in collected) == path.read_bytes()
    assert result.parts == 6
    assert result.id == 1001
    assert result.name == str(path)
    assert not fallback, "the big-file path must not reach kurigram"


async def test_parts_are_spread_over_every_session(tmp_path, fallback, tiny):
    path = _write(tmp_path, fast_upload._PART_SIZE * 12)
    sessions = _make_sessions(4)
    client = _Client(sessions=list(sessions))

    await fast_upload._save_file(client, str(path))

    assert all(s.parts for s in sessions), "an idle session buys no throughput"
    assert [len(s.parts) for s in sessions] == [3, 3, 3, 3]


async def test_reader_does_not_run_past_the_semaphore(tmp_path, fallback, tiny):
    """Backpressure is what keeps a 40 GiB file from being read into memory."""
    path = _write(tmp_path, fast_upload._PART_SIZE * 40)
    sessions = _make_sessions(2, delay=0.001)
    client = _Client(sessions=list(sessions))

    await fast_upload._save_file(client, str(path))

    assert _Session.peak <= 2 * fast_upload._PARTS_PER_SESSION
    assert _Session.peak > 1, "the whole point is more than one part at a time"


async def test_progress_climbs_to_the_file_size(tmp_path, fallback, tiny):
    size = fast_upload._PART_SIZE * 5 + 99
    path = _write(tmp_path, size)
    sessions = _make_sessions(3, delay=0.001)
    client = _Client(sessions=list(sessions))
    seen = []

    async def progress(current, total, tag):
        seen.append((current, total, tag))

    await fast_upload._save_file(
        client, str(path), progress=progress, progress_args=("t",)
    )

    assert [c for c, _, _ in seen] == sorted(c for c, _, _ in seen), "must not go back"
    assert seen[-1] == (size, size, "t")
    assert all(total == size for _, total, _ in seen)


class _InvertingPool:
    """A thread pool that hands later work a head start.

    A real pool reorders two concurrent calls now and then, at the scheduler's
    whim, which made the test for the progress lock fail eight runs in ten when
    the lock was removed -- and pass the other two. Making the inversion
    deliberate turns that into every run: under the lock only one call is ever
    outstanding, so nothing is there to overtake it and the delays cancel out,
    while without it the calls in flight come back in the wrong order by
    construction.

    Only ``submit`` is needed; that is all ``run_in_executor`` calls.
    """

    def __init__(self):
        self._pool = ThreadPoolExecutor(max_workers=8)
        self._n = 0

    def submit(self, func, *args, **kwargs):
        self._n += 1
        # Sawtooth rather than a plain countdown: it has to keep inverting for
        # as many parts as the test cares to upload, not just the first few.
        delay = 0.02 * (7 - self._n % 8) / 8
        return self._pool.submit(self._after, delay, func, *args, **kwargs)

    @staticmethod
    def _after(delay, func, *args, **kwargs):
        time.sleep(delay)
        return func(*args, **kwargs)

    def shutdown(self, wait=True):
        self._pool.shutdown(wait=wait)


async def test_sync_progress_callback_is_supported(tmp_path, fallback, tiny):
    """A plain function goes to a thread pool, and must still arrive in order."""
    size = fast_upload._PART_SIZE * 24
    path = _write(tmp_path, size)
    sessions = _make_sessions(4)
    client = _Client(sessions=list(sessions))
    client.executor = _InvertingPool()
    seen = []

    def progress(current, total):
        seen.append(current)

    try:
        await fast_upload._save_file(client, str(path), progress=progress)
    finally:
        client.executor.shutdown()

    assert seen == sorted(seen), "a report out of order rewinds the status bar"
    assert seen[-1] == size


async def test_stop_transmission_propagates_and_keeps_the_pool(
    tmp_path, fallback, tiny
):
    """Cancelling is deliberate, so the sessions are still good."""
    path = _write(tmp_path, fast_upload._PART_SIZE * 20)
    sessions = _make_sessions(2, delay=0.001)
    client = _Client(sessions=list(sessions))

    async def progress(current, total):
        if current > fast_upload._PART_SIZE * 2:
            raise StopTransmission

    with pytest.raises(StopTransmission):
        await fast_upload._save_file(client, str(path), progress=progress)

    assert client in fast_upload._pools, "a cancel must not tear down the pool"
    assert not any(s.stopped for s in sessions)


async def test_a_failed_part_raises_and_drops_the_pool(tmp_path, fallback, tiny):
    """A dead connection would fail every later upload the same way."""
    path = _write(tmp_path, fast_upload._PART_SIZE * 8)
    sessions = _make_sessions(2)
    sessions[1].fail_on = 3
    client = _Client(sessions=list(sessions))

    with pytest.raises(OSError, match="connection reset"):
        await fast_upload._save_file(client, str(path))

    assert client not in fast_upload._pools
    assert all(s.stopped for s in sessions), "the pool must actually be closed"


async def test_oversize_file_is_refused(tmp_path, fallback, monkeypatch):
    monkeypatch.setattr(fast_upload, "_BIG_FILE", 16)
    path = _write(tmp_path, 64)
    client = _Client(sessions=_make_sessions(1))
    # 2000 MiB for a plain account; pretend the file is past it.
    monkeypatch.setattr(
        fast_upload.os.path, "getsize", lambda _p: 2001 * 1024 * 1024
    )
    with pytest.raises(ValueError, match="bigger than 2000 MiB"):
        await fast_upload._save_file(client, str(path))


async def test_premium_gets_the_larger_limit(tmp_path, fallback, monkeypatch):
    monkeypatch.setattr(fast_upload, "_BIG_FILE", 16)
    path = _write(tmp_path, 64)
    client = _Client(sessions=_make_sessions(1), is_premium=True)
    monkeypatch.setattr(
        fast_upload.os.path, "getsize", lambda _p: 2001 * 1024 * 1024
    )
    # Reading stops at the real end of the file, so this uploads and returns.
    result = await fast_upload._save_file(client, str(path))
    assert result.parts == 4002


# --- the session pool ---


async def test_pool_is_opened_once_and_reused(tmp_path, fallback, tiny):
    path = _write(tmp_path, fast_upload._PART_SIZE * 3)
    sessions = _make_sessions(4)
    client = _Client(sessions=list(sessions))

    await fast_upload._save_file(client, str(path))
    await fast_upload._save_file(client, str(path))

    assert len(client.handed_out) == 4, "the second upload must reuse the first's"


async def test_pool_settles_for_what_it_can_open(tmp_path, fallback, tiny):
    """Two connections beat one; a third that refuses is not fatal."""
    path = _write(tmp_path, fast_upload._PART_SIZE * 4)
    sessions = _make_sessions(2)
    client = _Client(sessions=list(sessions))

    result = await fast_upload._save_file(client, str(path))

    assert result.parts == 4
    assert len(client.handed_out) == 2


async def test_discard_closes_and_forgets(tmp_path, fallback, tiny):
    path = _write(tmp_path, fast_upload._PART_SIZE * 2)
    sessions = _make_sessions(2)
    client = _Client(sessions=list(sessions))
    await fast_upload._save_file(client, str(path))

    await fast_upload.discard(client)

    assert all(s.stopped for s in sessions)
    assert client not in fast_upload._pools
    await fast_upload.discard(client)  # must be safe twice


async def test_purepath_is_accepted(tmp_path, fallback, tiny):
    path = _write(tmp_path, fast_upload._PART_SIZE * 2)
    client = _Client(sessions=_make_sessions(2))
    result = await fast_upload._save_file(client, PurePath(str(path)))
    assert result.parts == 2
    assert not fallback


# --- installation ---


def test_install_is_idempotent_and_reversible(monkeypatch):
    from pyrogram import Client as RealClient

    original = RealClient.save_file
    monkeypatch.setattr(fast_upload, "_original_save_file", None)
    monkeypatch.delenv("MLTB_FAST_UPLOAD", raising=False)
    try:
        assert fast_upload.install() is True
        assert RealClient.save_file is fast_upload._save_file
        assert fast_upload.install() is True, "a restart re-runs startup"
    finally:
        RealClient.save_file = original


def test_install_can_be_switched_off(monkeypatch):
    from pyrogram import Client as RealClient

    original = RealClient.save_file
    monkeypatch.setattr(fast_upload, "_original_save_file", None)
    monkeypatch.setenv("MLTB_FAST_UPLOAD", "0")
    try:
        assert fast_upload.install() is False
        assert RealClient.save_file is original
    finally:
        RealClient.save_file = original


@pytest.mark.parametrize(
    "value,expected",
    [("", 4), ("1", 1), ("6", 6), ("99", 8), ("0", 1), ("-3", 1), ("junk", 4)],
)
def test_session_count_is_clamped(monkeypatch, value, expected):
    monkeypatch.setenv("MLTB_FAST_UPLOAD_SESSIONS", value)
    assert fast_upload._session_count() == expected
