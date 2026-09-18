"""Tests for the Telegram downloader.

A replied file is one file, downloaded by pyrogram and then uploaded -- the
queued way at the end of the task, or, with ``-su``, sent the moment it lands
instead of waiting for an upload slot. The two endings also both have to let go
of the file's id: it is held from the moment the download starts to keep a
second task off the same file, and a task that streamed and never released it
would make that file undownloadable until the bot restarted.

The module is loaded under a stubbed bot package -- it reaches into the client
manager at import time, and none of that is needed to drive a download -- with
pyrogram itself real, since the flood-wait retry is part of what is under test.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parent.parent


class _Logger:
    @staticmethod
    def info(msg):
        pass

    error = warning = debug = info


class _Lock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Listener:
    """The slice of TaskListener the downloader touches."""

    def __init__(self, stream_upload=False):
        self.name = ""
        self.size = 0
        self.mid = "mid1"
        self.multi = 0
        self.user_id = 7
        self.is_cancelled = False
        self.user_transmission = False
        self.is_super_chat = False
        self.message = object()
        self.started = False
        self.completed = False
        self.error = ""
        self.client = _Client()
        # what streaming reads and writes
        self.stream_upload = stream_upload
        self.stream_notices = []
        self.same_dir = {}
        self.folder_name = ""

    async def on_download_start(self):
        self.started = True

    async def on_download_complete(self):
        self.completed = True

    async def on_download_error(self, error):
        self.error = str(error)


class _Client:
    def stop_transmission(self):
        pass


class _RecordingUploader:
    """Stands in for ``TelegramUploader``, which the stream component builds."""

    built: list = []

    def __init__(self, listener, path):
        self.listener = listener
        self.path = path
        self.sent = []
        self.finalized = 0
        _RecordingUploader.built.append(self)

    async def init_stream(self):
        return True

    async def upload_single(self, file_path):
        self.sent.append(file_path)

    async def finalize_stream(self):
        self.finalized += 1


class _Media:
    file_unique_id = "uid1"
    file_size = 1024
    file_name = "movie.mkv"


class _Message:
    """A replied message whose ``download`` writes the file and reports it."""

    document = _Media()

    def __init__(self, dest, failures=0, on_download=None):
        self._dest = dest
        self._failures = failures
        self._on_download = on_download
        self.attempts = 0

    async def download(self, file_name=None, progress=None):
        self.attempts += 1
        if self._failures >= self.attempts:
            raise _Flood()
        if progress is not None:
            await progress(512, 1024)
        if self._on_download is not None:
            self._on_download()
        Path(self._dest).write_bytes(b"x" * 1024)
        return self._dest


class _Flood(Exception):
    """Stands in for ``FloodWait``, which the module is loaded with."""


async def _check_running_tasks(listener):
    return False, None


async def _send_status_message(message):
    return None


def _module(name, package=False, **attrs):
    mod = ModuleType(name)
    if package:
        mod.__path__ = []
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


def _stub_modules():
    """The slice of the bot package ``telegram_download`` imports."""
    modules = {
        "bot": _module(
            "bot", package=True, LOGGER=_Logger(), task_dict={}, task_dict_lock=_Lock()
        ),
        "bot.core": _module("bot.core", package=True),
        "bot.core.telegram_manager": _module(
            "bot.core.telegram_manager",
            TgClient=SimpleNamespace(bot=None),
            get_user_client=lambda *args: None,
            user_session=lambda: None,
        ),
        "bot.helper.util.task_manager": _module(
            "bot.helper.util.task_manager", check_running_tasks=_check_running_tasks
        ),
        "bot.helper.telegram.message_utils": _module(
            "bot.helper.telegram.message_utils",
            send_status_message=_send_status_message,
        ),
        # a flood wait is not slept through in a test: the retry is what matters
        "bot.helper.telegram.flood": _module(
            "bot.helper.telegram.flood", flood_seconds=lambda f: 0
        ),
        "bot.helper.progress.queue_status": _module(
            "bot.helper.progress.queue_status",
            QueueStatus=lambda *args, **kwargs: object(),
        ),
        "bot.helper.progress.telegram_status": _module(
            "bot.helper.progress.telegram_status",
            TelegramStatus=lambda *args, **kwargs: object(),
        ),
        # the stream component is loaded for real from disk, so the uploader it
        # builds -- and only that -- is replaced
        "bot.helper.upload": _module("bot.helper.upload", package=True),
        "bot.helper.upload.telegram_uploader": _module(
            "bot.helper.upload.telegram_uploader",
            TelegramUploader=_RecordingUploader,
        ),
    }
    for name in (
        "bot.helper",
        "bot.helper.util",
        "bot.helper.telegram",
        "bot.helper.progress",
        "bot.helper.download",
    ):
        modules[name] = _module(name, package=True)
    return modules


def _load(monkeypatch, name, where="download"):
    """Load one real module under the stubbed package tree."""
    path = _ROOT / "bot" / "helper" / where / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"bot.helper.{where}.{name}", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def telegram_dl(monkeypatch, tmp_path):
    """Load ``telegram_download.py`` with the bot package stubbed to what it uses."""
    _RecordingUploader.built = []

    for name, mod in _stub_modules().items():
        monkeypatch.setitem(sys.modules, name, mod)

    _load(monkeypatch, "stream_uploader", where="upload")
    module = _load(monkeypatch, "telegram_download")
    # the real flood exceptions are pyrogram's; the retry is driven with one of
    # these instead, which is the same shape and does not need a fake server
    monkeypatch.setattr(module, "FloodWait", _Flood)
    monkeypatch.setattr(module, "FloodPremiumWait", _Flood)

    async def run(listener, message):
        await module.TelegramDownloadHelper(listener).add_download(
            message, f"{tmp_path}/", "bot"
        )

    return SimpleNamespace(
        module=module,
        path=str(tmp_path),
        run=run,
        uploads=_RecordingUploader,
        message=lambda **kwargs: _Message(f"{tmp_path}/movie.mkv", **kwargs),
    )


async def test_a_streamed_download_is_sent_as_it_lands(telegram_dl):
    listener = _Listener(stream_upload=True)

    await telegram_dl.run(listener, telegram_dl.message())

    uploader = telegram_dl.uploads.built[-1]
    assert uploader.sent == [f"{telegram_dl.path}/movie.mkv"]
    assert uploader.path == telegram_dl.path
    assert uploader.finalized == 1
    # the uploader reports the task, so the queued pipeline is never entered
    assert listener.completed is False
    assert not listener.error


async def test_a_download_without_the_flag_uploads_at_the_end(telegram_dl):
    listener = _Listener(stream_upload=False)

    await telegram_dl.run(listener, telegram_dl.message())

    assert telegram_dl.uploads.built == []
    assert listener.completed is True
    assert not listener.error


async def test_a_flood_wait_retry_builds_one_uploader(telegram_dl):
    """The retry runs the download again, and it must not announce the task twice."""
    listener = _Listener(stream_upload=True)
    message = telegram_dl.message(failures=1)

    await telegram_dl.run(listener, message)

    assert message.attempts == 2
    assert len(telegram_dl.uploads.built) == 1
    uploader = telegram_dl.uploads.built[-1]
    assert uploader.sent == [f"{telegram_dl.path}/movie.mkv"]
    assert uploader.finalized == 1


async def test_a_streamed_file_is_not_left_as_being_downloaded(telegram_dl):
    """The id guards against a second task taking the same file."""
    listener = _Listener(stream_upload=True)

    await telegram_dl.run(listener, telegram_dl.message())

    assert telegram_dl.module.GLOBAL_GID == set()


async def test_a_cancelled_download_is_not_sent(telegram_dl):
    listener = _Listener(stream_upload=True)
    message = telegram_dl.message(
        on_download=lambda: setattr(listener, "is_cancelled", True)
    )

    await telegram_dl.run(listener, message)

    assert telegram_dl.uploads.built == []
    assert listener.completed is False
    assert not listener.error
