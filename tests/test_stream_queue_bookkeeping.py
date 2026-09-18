"""What a streamed (``-su``) task leaves behind in the queues, and in its message.

Two defects lived here and neither is visible from the download path alone: a
streamed task that finished successfully kept its ``non_queued_dl`` entry
forever, because every remover of that set sits behind ``_start_upload``; and
``-d -su`` took the seed branch of ``on_upload_complete``, which answers with a
``return`` before the task is taken off the status list.

These tests run the real ``on_upload_complete`` and the real queue helpers, with
only Telegram, the filesystem and the database replaced.
"""

from __future__ import annotations

import sys
from asyncio import Event
from types import ModuleType, SimpleNamespace

import pytest

import bot.helper.listeners.task_listener as tl
import bot.helper.task.batch_tracker as bt
import bot.helper.util.task_manager as tm
from bot.core.config_manager import Config
from bot.helper.listeners.direct_listener import DirectListener
from bot.helper.upload.stream_uploader import StreamUploader


@pytest.fixture(autouse=True)
def queues(monkeypatch):
    """Give each test its own view of the bot-wide registries."""
    fresh = {
        "non_queued_dl": set(),
        "non_queued_up": set(),
        "queued_dl": {},
        "queued_up": {},
        "upload_chat_of": {},
        "task_dict": {},
    }
    for name, value in fresh.items():
        monkeypatch.setattr(tl, name, value)
    # the queue helpers read the same registries, bound in their own module
    for name in (
        "non_queued_dl",
        "non_queued_up",
        "queued_dl",
        "queued_up",
        "upload_chat_of",
    ):
        monkeypatch.setattr(tm, name, fresh[name])
    return SimpleNamespace(**fresh)


@pytest.fixture
def tg(monkeypatch):
    """The three Telegram/filesystem calls the completion path makes."""
    calls = SimpleNamespace(messages=[], cleaned=[], targets=[], status=0)

    async def send_message(_message, text, **_kwargs):
        calls.messages.append(text)

    async def clean_download(path):
        calls.cleaned.append(path)

    async def clean_target(path):
        calls.targets.append(path)

    async def update_status_message(_chat_id):
        calls.status += 1

    monkeypatch.setattr(tl, "send_message", send_message)
    monkeypatch.setattr(tl, "clean_download", clean_download)
    monkeypatch.setattr(tl, "clean_target", clean_target)
    monkeypatch.setattr(tl, "update_status_message", update_status_message)
    return calls


class FakeTask:
    """A listener with only what ``on_upload_complete`` touches, real method."""

    on_upload_complete = tl.TaskListener.on_upload_complete
    _batch = bt.BatchTrackerMixin._batch

    def __init__(self, mid=1, *, seed=False, folder_name="", same_dir=None):
        self.mid = mid
        self.name = "album"
        self.size = 2048
        self.tag = "someone"
        self.dir = f"{tl.DOWNLOAD_DIR}{mid}"
        self.up_dir = f"{self.dir}10000"
        self.message = SimpleNamespace(chat=SimpleNamespace(id=-100))
        self.is_super_chat = False
        self.copy_units = []
        self.stream_notices = []
        self.multi_tag = ""
        self.folder_name = folder_name
        self.same_dir = {} if same_dir is None else same_dir
        self.seed = seed
        self.stream_upload = True
        self.is_cancelled = False
        self.same_dir_jobs = 0

    async def remove_from_same_dir(self):
        self.same_dir_jobs += 1


class FakeUploader:
    """An uploader that reports the task as done, as the real one does."""

    def __init__(self, listener):
        self._listener = listener
        self.sent = []

    async def init_stream(self):
        return True

    async def upload_single(self, file_path):
        self.sent.append(file_path)

    async def finalize_stream(self):
        await self._listener.on_upload_complete(None, {}, 1, 0)


def _stream(listener, *files):
    """Run one streamed task from start to the completion message."""
    uploader = FakeUploader(listener)
    stream = StreamUploader(listener, f"{listener.dir}/", uploader=uploader)

    async def run():
        assert await stream.start()
        for file_path in files:
            await stream.submit(file_path)
        await stream.finalize()

    return stream, run


# ── the download slot ───────────────────────────────────────────────


@pytest.mark.parametrize("queue_all", [0, 3])
async def test_a_finished_stream_task_gives_its_download_slot_back(
    queues, tg, monkeypatch, queue_all
):
    """``QUEUE_ALL`` is included because it is what holds the slot for others."""
    monkeypatch.setattr(Config, "QUEUE_ALL", queue_all)
    listener = FakeTask(101)
    queues.non_queued_dl.add(101)
    queues.task_dict[101] = object()
    queues.task_dict[999] = object()  # another task, so the bot stays awake

    _, run = _stream(listener, "/d/a.mkv")
    await run()

    assert 101 not in queues.non_queued_dl
    assert 101 not in queues.task_dict


async def test_the_freed_slot_is_handed_to_the_next_task_in_line(
    queues, tg, monkeypatch
):
    """A released slot is only real if the queue actually reuses it."""
    monkeypatch.setattr(Config, "QUEUE_ALL", 0)
    monkeypatch.setattr(Config, "QUEUE_DOWNLOAD", 2)
    listener = FakeTask(101)
    queues.non_queued_dl.add(101)
    queues.task_dict[101] = object()
    queues.task_dict[999] = object()  # another task, so the bot stays awake
    queues.queued_dl[202] = Event()

    _, run = _stream(listener, "/d/a.mkv")
    await run()

    assert queues.queued_dl == {}
    assert 202 in queues.non_queued_dl


async def test_a_task_that_already_left_the_queue_releases_nothing(
    queues, tg, monkeypatch
):
    """The common path: ``_start_upload`` took this task's slot long ago.

    The only entry left in ``non_queued_dl`` belongs to somebody else, and the
    download limit is exactly filled by it -- so a release that this task is not
    owed would show up as the waiting download being let in.
    """
    monkeypatch.setattr(Config, "QUEUE_ALL", 0)
    monkeypatch.setattr(Config, "QUEUE_DOWNLOAD", 1)
    listener = FakeTask(101)
    queues.task_dict[101] = object()
    queues.task_dict[999] = object()  # another task, so the bot stays awake
    queues.non_queued_dl.add(555)
    pending = Event()
    queues.queued_dl[202] = pending

    _, run = _stream(listener, "/d/a.mkv")
    await run()

    assert queues.non_queued_dl == {555}
    assert queues.queued_dl == {202: pending}


async def test_a_completion_message_that_fails_still_frees_the_queue(
    queues, tg, monkeypatch
):
    """The slot is released early because the last thing this method does can fail.

    Sending the completion message is a network call: when it raises, the
    ``start_from_queued`` at the end of the method never runs, and a queue woken
    only from there would keep every waiting download parked behind a task that
    is already over.
    """
    monkeypatch.setattr(Config, "QUEUE_ALL", 0)
    monkeypatch.setattr(Config, "QUEUE_DOWNLOAD", 1)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("telegram is unhappy")

    monkeypatch.setattr(tl, "send_message", boom)
    listener = FakeTask(101)
    queues.non_queued_dl.add(101)
    queues.task_dict[101] = object()
    queues.task_dict[999] = object()  # another task, so the bot stays awake
    queues.queued_dl[202] = Event()

    _, run = _stream(listener, "/d/a.mkv")
    with pytest.raises(RuntimeError):
        await run()

    assert 202 in queues.non_queued_dl


# ── the seed branch ─────────────────────────────────────────────────


async def test_a_stream_task_never_takes_the_seed_branch(queues, tg, monkeypatch):
    """``-d -su``: the files are gone, so the seed branch would strand the task."""
    monkeypatch.setattr(Config, "QUEUE_ALL", 0)
    listener = FakeTask(101, seed=True)
    queues.task_dict[101] = object()
    queues.task_dict[999] = object()

    _, run = _stream(listener, "/d/a.mkv")
    await run()

    assert listener.seed is False
    assert tg.cleaned == [listener.dir]
    assert tg.targets == []
    assert 101 not in queues.task_dict


async def test_a_seeding_task_keeps_its_own_path(queues, tg, monkeypatch):
    """A torrent is not touched: it seeds, and its files stay where they are."""
    monkeypatch.setattr(Config, "QUEUE_ALL", 0)
    listener = FakeTask(101, seed=True)
    listener.stream_upload = False
    queues.task_dict[101] = object()
    queues.non_queued_up.add(101)
    queues.upload_chat_of[101] = "-100"

    await listener.on_upload_complete(None, {}, 1, 0)

    assert tg.targets == [listener.up_dir]
    assert tg.cleaned == []
    assert 101 not in queues.non_queued_up
    assert 101 not in queues.upload_chat_of


# ── what the user is told ───────────────────────────────────────────


async def test_the_dropped_flags_are_named_in_the_task_message(queues, tg, monkeypatch):
    monkeypatch.setattr(Config, "QUEUE_ALL", 0)
    listener = FakeTask(101, seed=True)
    listener.compress = "secret"
    listener.split_size = 2000000000
    queues.task_dict[101] = object()
    queues.task_dict[999] = object()

    _, run = _stream(listener, "/d/a.mkv")
    await run()

    message = tg.messages[-1]
    assert "Note: -z ignored:" in message
    assert "Note: -sp ignored:" in message
    assert "Note: -d ignored:" in message
    # and the task's own numbers are still there
    assert "<b>Task ID: </b><code>101</code>" in message


# ── the direct downloader, which is the port that already streamed ───


class RecordingUploader:
    """Stands in for ``TelegramUploader`` at the one place it is built."""

    built: list = []

    def __init__(self, listener, path):
        self.listener = listener
        self.path = path
        self.sent = []
        self.finalized = 0
        RecordingUploader.built.append(self)

    async def init_stream(self):
        return True

    async def upload_single(self, file_path):
        self.sent.append(file_path)

    async def finalize_stream(self):
        self.finalized += 1
        await self.listener.on_upload_complete(None, {}, len(self.sent), 0)


class ScriptedDirect(DirectListener):
    """A direct download where the network is a list of outcomes."""

    def __init__(self, listener, path, outcomes):
        super().__init__(path, listener, {})
        self._outcomes = list(outcomes)

    async def _download_one(self, content):
        outcome = self._outcomes.pop(0)
        if outcome is None:
            self._failed += 1
        return outcome


@pytest.fixture
def recorded(monkeypatch):
    """Replace the uploader the component builds, at its point of import.

    The entry in ``sys.modules`` is what gets replaced rather than an imported
    reference: other test files load ``telegram_uploader`` from disk under a
    stubbed package and pop the entry afterwards, so a reference patched here
    would be a different object from the one the lazy import resolves to.
    """
    RecordingUploader.built = []
    fake = ModuleType("bot.helper.upload.telegram_uploader")
    fake.TelegramUploader = RecordingUploader
    monkeypatch.setitem(sys.modules, "bot.helper.upload.telegram_uploader", fake)
    return RecordingUploader


async def test_a_direct_download_sends_each_file_as_it_lands(
    queues, tg, recorded, monkeypatch
):
    """And never calls ``on_download_complete``: that is the queued pipeline."""
    monkeypatch.setattr(Config, "QUEUE_ALL", 0)
    listener = FakeTask(101)
    queues.task_dict[101] = object()
    queues.task_dict[999] = object()  # another task, so the bot stays awake

    async def boom(*_args, **_kwargs):
        raise AssertionError("a streamed task must not queue an upload")

    monkeypatch.setattr(listener, "on_download_complete", boom, raising=False)
    contents = [{"path": "", "filename": name} for name in ("a.mkv", "b.mkv", "c.mkv")]
    download = ScriptedDirect(
        listener, "/d/101/", ["/d/101/a.mkv", None, "/d/101/b.mkv"]
    )

    await download._download_stream(contents)

    uploader = recorded.built[-1]
    assert uploader.sent == ["/d/101/a.mkv", "/d/101/b.mkv"]
    assert uploader.finalized == 1
    assert 101 not in queues.task_dict


async def test_every_file_failing_is_still_an_error(queues, tg, recorded, monkeypatch):
    monkeypatch.setattr(Config, "QUEUE_ALL", 0)
    listener = FakeTask(101)
    errors = []

    async def on_download_error(error, *_args):
        errors.append(error)

    monkeypatch.setattr(listener, "on_download_error", on_download_error, raising=False)
    download = ScriptedDirect(listener, "/d/101/", [None, None])

    await download._download_stream(
        [{"path": "", "filename": n} for n in ("a.mkv", "b.mkv")]
    )

    assert errors == ["All files are failed to download!"]
    assert recorded.built[-1].finalized == 0
