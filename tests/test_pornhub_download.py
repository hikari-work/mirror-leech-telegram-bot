"""Tests for the PornHub channel downloader.

A channel is one task that walks its listing and muxes each video in turn -- the
same shape as a Vidara folder, which is why the two share a base class, a stub
kit and now a streaming path. The kit itself lives with the Vidara tests, and
this file borrows it rather than keeping a second copy of the same twelve stubs
in step.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from test_vidara_download import (
    _check_running_tasks,
    _FakeYoutubeDL,
    _Listener,
    _load,
    _Lock,
    _Logger,
    _module,
    _RecordingUploader,
    _send_status_message,
    _sync_to_async,
)


def _stub_modules():
    """The slice of the bot package ``pornhub_download`` imports."""
    return {
        "bot": _module(
            "bot",
            package=True,
            LOGGER=_Logger(),
            task_dict={},
            task_dict_lock=_Lock(),
        ),
        "bot.helper.util.bot_utils": _module(
            "bot.helper.util.bot_utils", sync_to_async=_sync_to_async
        ),
        "bot.helper.util.task_manager": _module(
            "bot.helper.util.task_manager",
            check_running_tasks=_check_running_tasks,
        ),
        "bot.helper.telegram.message_utils": _module(
            "bot.helper.telegram.message_utils",
            send_status_message=_send_status_message,
        ),
        "bot.helper.progress.queue_status": _module(
            "bot.helper.progress.queue_status",
            QueueStatus=lambda *args, **kwargs: object(),
        ),
        "bot.helper.progress.pornhub_status": _module(
            "bot.helper.progress.pornhub_status",
            PornHubStatus=lambda *args, **kwargs: object(),
        ),
        # the stream component is loaded for real from disk, so the uploader it
        # builds -- and only that -- is replaced
        "bot.helper.upload": _module("bot.helper.upload", package=True),
        "bot.helper.upload.telegram_uploader": _module(
            "bot.helper.upload.telegram_uploader",
            TelegramUploader=_RecordingUploader,
        ),
    }


@pytest.fixture
def pornhub_dl(monkeypatch, tmp_path):
    """Load ``pornhub_download.py`` with the bot package stubbed to what it uses."""
    _FakeYoutubeDL.calls = []
    _FakeYoutubeDL.on_download = None
    _FakeYoutubeDL.on_move = None
    _FakeYoutubeDL.silent = False
    _RecordingUploader.built = []

    for name, mod in _stub_modules().items():
        monkeypatch.setitem(sys.modules, name, mod)
    for name in (
        "bot.helper",
        "bot.helper.util",
        "bot.helper.telegram",
        "bot.helper.progress",
        "bot.helper.download",
    ):
        monkeypatch.setitem(sys.modules, name, _module(name, package=True))

    # the queue slot, the counters and the cancel path come from the shared base,
    # and the file paths a stream hands over come from the hook
    _load(monkeypatch, "multi_video_download")
    _load(monkeypatch, "yt_dlp_hooks")
    _load(monkeypatch, "stream_uploader", where="upload")
    module = _load(monkeypatch, "pornhub_download")
    monkeypatch.setattr(module, "YoutubeDL", _FakeYoutubeDL)

    return SimpleNamespace(
        module=module, path=str(tmp_path), uploads=_RecordingUploader
    )


def _channel(*names, title="Chan"):
    return {
        "pornhub": True,
        "title": title,
        "videos": [
            {"name": name, "url": f"https://www.pornhub.com/view_video.php?viewkey={name}"}
            for name in names
        ],
    }


async def _run(harness, listener):
    await harness.module.add_pornhub_download(listener, harness.path)


async def test_a_channel_without_the_flag_uploads_at_the_end(pornhub_dl):
    listener = _Listener(_channel("a.mp4", "b.mp4"))

    await _run(pornhub_dl, listener)

    assert listener.completed is True
    assert pornhub_dl.uploads.built == []
    assert not listener.error


async def test_a_streamed_channel_sends_each_video_as_it_lands(pornhub_dl):
    listener = _Listener(_channel("a.mp4", "b.mp4"), stream_upload=True)
    # the container is yt-dlp's choice, so where the file ends up is not what
    # the download asked for
    _FakeYoutubeDL.on_move = lambda dest: dest.replace(".mp4", ".mkv")

    await _run(pornhub_dl, listener)

    # a channel of more than one video keeps its own directory, as it always has
    base = f"{pornhub_dl.path}/Chan"
    uploader = pornhub_dl.uploads.built[-1]
    assert uploader.sent == [f"{base}/a.mkv", f"{base}/b.mkv"]
    assert uploader.path == base
    assert uploader.finalized == 1
    # the queued pipeline is never entered: that is the switch -su replaces
    assert listener.completed is False
    assert not listener.error


async def test_a_video_yt_dlp_says_nothing_about_is_still_sent(pornhub_dl):
    """The hook is the usual answer, not the only one: a download the downloader
    reports nothing about still has a file where it was asked for."""
    listener = _Listener(_channel("a.mp4", "b.mp4"), stream_upload=True)
    _FakeYoutubeDL.silent = True

    await _run(pornhub_dl, listener)

    base = f"{pornhub_dl.path}/Chan"
    assert pornhub_dl.uploads.built[-1].sent == [f"{base}/a.mp4", f"{base}/b.mp4"]


async def test_a_streamed_single_video_goes_into_the_task_directory(pornhub_dl):
    listener = _Listener(_channel("only.mp4"), stream_upload=True)

    await _run(pornhub_dl, listener)

    uploader = pornhub_dl.uploads.built[-1]
    assert uploader.sent == [f"{pornhub_dl.path}/only.mp4"]
    assert uploader.finalized == 1


async def test_a_video_that_failed_is_not_sent(pornhub_dl):
    listener = _Listener(_channel("dead.mp4", "b.mp4"), stream_upload=True)

    def _fail_first(ydl, url):
        if "dead" in url:
            raise OSError("connection reset")

    _FakeYoutubeDL.on_download = _fail_first

    await _run(pornhub_dl, listener)

    uploader = pornhub_dl.uploads.built[-1]
    assert uploader.sent == [f"{pornhub_dl.path}/Chan/b.mp4"]
    assert uploader.finalized == 1
    assert listener.completed is False


async def test_every_video_failing_is_still_an_error(pornhub_dl):
    listener = _Listener(_channel("a.mp4", "b.mp4"), stream_upload=True)

    def _fail(ydl, url):
        raise OSError("connection reset")

    _FakeYoutubeDL.on_download = _fail

    await _run(pornhub_dl, listener)

    assert listener.completed is False
    assert listener.error == "All videos failed to download!"
    assert pornhub_dl.uploads.built[-1].sent == []
    assert pornhub_dl.uploads.built[-1].finalized == 0
