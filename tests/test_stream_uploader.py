"""Tests for the shared stream-upload component.

``StreamUploader`` only needs ``LOGGER`` from the package, so the uploader is
handed in through its injection seam and nothing of the bot is imported.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _stub(name, **attrs):
    mod = ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


def _pkg(name, path=None):
    mod = ModuleType(name)
    mod.__path__ = [] if path is None else [path]
    return mod


class _Logger:
    @staticmethod
    def info(msg):
        pass

    error = warning = debug = info


@pytest.fixture
def stream_module(monkeypatch):
    """Import the component with its one package import stubbed out."""
    root = Path(__file__).resolve().parent.parent
    modules = {
        "bot": _stub("bot", LOGGER=_Logger()),
        "bot.helper": _pkg("bot.helper"),
        # Real path, so the module is loaded from disk rather than stubbed.
        "bot.helper.upload": _pkg(
            "bot.helper.upload", str(root / "bot" / "helper" / "upload")
        ),
    }
    # bot.__path__ has to allow the stubbed submodules above to resolve.
    modules["bot"].__path__ = []
    for name, mod in modules.items():
        monkeypatch.setitem(sys.modules, name, mod)

    target = "bot.helper.upload.stream_uploader"
    sys.modules.pop(target, None)
    module = importlib.import_module(target)
    yield module
    sys.modules.pop(target, None)


class FakeListener:
    """The listener attributes a streaming task reads and writes."""

    def __init__(self, mid=1, folder_name="", same_dir=None):
        self.mid = mid
        self.folder_name = folder_name
        self.same_dir = {} if same_dir is None else same_dir
        self.is_cancelled = False
        self.seed = False
        # where the component leaves what it had to drop, for the task message
        self.stream_notices = []
        # the flags a stream cannot honour, all at their unset values
        self.extract = False
        self.compress = False
        self.join = False
        self.sample_video = False
        self.screen_shots = False
        self.convert_audio = ""
        self.convert_video = ""
        self.name_sub = ""
        self.ffmpeg_cmds = frozenset()
        self.split_size = 0

    async def remove_from_same_dir(self):
        group = self.same_dir.get(self.folder_name) if self.folder_name else None
        if group and self.mid in group["tasks"]:
            group["tasks"].discard(self.mid)
            group["total"] -= 1


class FakeUploader:
    """Records what it was asked to send, and can be held open mid-upload."""

    def __init__(self, gate=None, init=True, fail_on=()):
        self.uploaded = []
        self.finalized = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self._gate = gate
        self._init = init
        self._fail_on = set(fail_on)

    async def init_stream(self):
        return self._init

    async def upload_single(self, file_path):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self._gate is not None:
                await self._gate.wait()
            if file_path in self._fail_on:
                raise RuntimeError("upload failed")
            self.uploaded.append(file_path)
        finally:
            self.in_flight -= 1

    async def finalize_stream(self):
        self.finalized += 1


def _stream(module, listener=None, uploader=None, **kwargs):
    return module.StreamUploader(
        listener or FakeListener(),
        "/downloads/1",
        uploader=uploader or FakeUploader(),
        **kwargs,
    )


# ── the consumer ────────────────────────────────────────────────────


async def test_a_started_stream_sends_what_is_submitted(stream_module):
    uploader = FakeUploader()
    stream = _stream(stream_module, uploader=uploader)

    assert await stream.start()
    await stream.submit("/d/a.mkv")
    await stream.submit("/d/b.mkv")
    await stream.finalize()

    assert uploader.uploaded == ["/d/a.mkv", "/d/b.mkv"]
    assert uploader.finalized == 1


async def test_the_next_upload_waits_for_the_previous_one(stream_module):
    """One sender is written to at a time: the uploader's state is not shared."""
    gate = asyncio.Event()
    uploader = FakeUploader(gate=gate)
    stream = _stream(stream_module, uploader=uploader)
    assert await stream.start()

    await stream.submit("/d/a.mkv")
    await asyncio.sleep(0)
    await stream.submit("/d/b.mkv")
    await asyncio.sleep(0.01)

    # the first upload is still in the sender, so the second has not started
    assert uploader.uploaded == []
    assert uploader.in_flight == 1

    gate.set()
    await stream.finalize()

    assert uploader.uploaded == ["/d/a.mkv", "/d/b.mkv"]
    assert uploader.max_in_flight == 1


async def test_a_fast_producer_cannot_run_ahead_of_the_uploader(stream_module):
    """The queue is bounded, so a download stops instead of filling the disk.

    One file is in the sender and one is held by the consumer, so it takes a
    fourth to find the wall -- and the wall is the point.
    """
    gate = asyncio.Event()
    stream = _stream(stream_module, uploader=FakeUploader(gate=gate))
    assert await stream.start()

    await stream.submit("/d/a.mkv")
    await asyncio.sleep(0)  # the consumer picks it up and starts sending
    await stream.submit("/d/b.mkv")
    await asyncio.sleep(0)  # taken off the queue, held while a.mkv sends
    await stream.submit("/d/c.mkv")

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(stream.submit("/d/d.mkv"), 0.05)

    gate.set()
    await stream.drain()


async def test_a_failing_upload_does_not_stop_the_stream(stream_module):
    uploader = FakeUploader(fail_on={"/d/a.mkv"})
    stream = _stream(stream_module, uploader=uploader)

    assert await stream.start()
    await stream.submit("/d/a.mkv")
    await stream.submit("/d/b.mkv")
    await stream.finalize()

    assert uploader.uploaded == ["/d/b.mkv"]


# ── cancellation ────────────────────────────────────────────────────


async def test_a_cancelled_task_keeps_draining_and_reports_nothing(stream_module):
    """A producer waiting for room has to be let go, and no message follows.

    The producer is deliberately left with a file it cannot place: if draining
    stopped at the sentinel instead of emptying the queue, this call would wait
    for an upload that is never coming.
    """
    gate = asyncio.Event()
    listener = FakeListener()
    uploader = FakeUploader(gate=gate)
    stream = _stream(stream_module, listener=listener, uploader=uploader)
    assert await stream.start()

    await stream.submit("/d/a.mkv")
    await asyncio.sleep(0)
    listener.is_cancelled = True
    await stream.submit("/d/b.mkv")
    await asyncio.sleep(0)
    await stream.submit("/d/c.mkv")
    blocked = asyncio.create_task(stream.submit("/d/d.mkv"))

    gate.set()
    await asyncio.wait_for(stream.finalize(), 1)
    await asyncio.wait_for(blocked, 1)

    # what was already in the sender went out; the rest was dropped
    assert uploader.uploaded == ["/d/a.mkv"]
    assert uploader.finalized == 0


async def test_submitting_after_a_drain_is_dropped(stream_module):
    uploader = FakeUploader()
    stream = _stream(stream_module, uploader=uploader)

    assert await stream.start()
    await stream.drain()
    await stream.submit("/d/a.mkv")
    await stream.finalize()

    assert uploader.uploaded == []
    assert uploader.finalized == 1


async def test_a_stream_that_cannot_announce_itself_does_not_start(stream_module):
    uploader = FakeUploader(init=False)
    stream = _stream(stream_module, uploader=uploader)

    assert await stream.start() is False
    await stream.submit("/d/a.mkv")

    assert uploader.uploaded == []


# ── what a stream cannot honour ─────────────────────────────────────


async def test_the_flags_a_stream_skips_are_cleared_and_named(stream_module):
    listener = FakeListener()
    listener.extract = True
    listener.compress = "secret"
    listener.join = True
    listener.screen_shots = True
    listener.split_size = 2000000000
    listener.seed = True
    stream = _stream(stream_module, listener=listener)

    assert await stream.start()

    assert listener.extract is False
    assert listener.compress is False
    assert listener.join is False
    assert listener.screen_shots is False
    assert listener.split_size == 0
    assert listener.seed is False

    assert len(stream.notices) == 3
    assert stream.notices[0].startswith("Note: -e, -z, -j, -ss ignored:")
    assert "post-processing" in stream.notices[0]
    assert stream.notices[1].startswith("Note: -sp ignored:")
    assert stream.notices[2].startswith("Note: -d ignored:")
    await stream.drain()


async def test_only_the_flags_that_were_set_are_named(stream_module):
    """Only what the listener actually carries is named, not the whole table."""
    listener = FakeListener()
    listener.seed = True
    stream = _stream(stream_module, listener=listener)

    assert await stream.start()

    assert listener.seed is False
    assert stream.notices == [
        "Note: -d ignored: a streamed task never seeds, "
        "because the files do not stay on disk."
    ]
    await stream.drain()


async def test_a_plain_stream_task_reports_nothing(stream_module):
    stream = _stream(stream_module)

    assert await stream.start()

    assert stream.notices == []
    await stream.drain()


# ── same-dir groups ─────────────────────────────────────────────────


async def test_a_stream_task_leaves_its_same_dir_group(stream_module):
    group = {"tasks": {1, 2}, "total": 2}
    listener = FakeListener(mid=1, folder_name="album", same_dir={"album": group})
    stream = _stream(stream_module, listener=listener)

    assert await stream.start()

    assert listener.mid not in group["tasks"]
    assert group["total"] == 1
    assert stream.notices == [
        "Note: -m ignored: files are sent one by one, "
        "so they are not merged into one folder."
    ]
    await stream.drain()


async def test_a_stream_task_outside_a_group_is_not_reported_as_merged(stream_module):
    listener = FakeListener(
        mid=9, folder_name="album", same_dir={"album": {"tasks": {1}, "total": 1}}
    )
    stream = _stream(stream_module, listener=listener)

    assert await stream.start()

    assert listener.same_dir["album"]["tasks"] == {1}
    assert stream.notices == []
    await stream.drain()
