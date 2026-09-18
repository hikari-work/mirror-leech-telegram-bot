"""Streaming a Mega folder: each file leaves as it lands, not at the end.

Mega is the port where the component's one-consumer rule is load-bearing: a
folder is fetched through several connections at once, so several coroutines
finish their file at the same moment and only one of them may drive the
uploader, whose state is one file wide.

Everything here runs the real ``MegaDownloadHelper._run`` and the real
``StreamUploader``; what is replaced is the CDN, the HTTP session, and the
uploader the component builds.
"""

from __future__ import annotations

import sys
from asyncio import Event, wait_for
from types import ModuleType

import pytest

import bot.helper.download.mega_download as md

# The file's own helper, and the seam the component builds its uploader at.
SENT_WITHIN = 5


class FakeMegaTask:
    """A leech task with the handful of things ``_run`` reaches for."""

    def __init__(self, *, stream=True):
        self.name = "album"
        self.mid = 101
        self.size = 0
        self.is_cancelled = False
        self.stream_upload = stream
        self.stream_notices = []
        self.same_dir = {}
        self.folder_name = ""
        self.completed = 0
        self.errors = []

    async def on_download_complete(self):
        self.completed += 1

    async def on_download_error(self, error, button=None):
        self.errors.append(error)


class RecordingUploader:
    """Stands in for ``TelegramUploader`` at the one place it is built."""

    built: list = []
    # set by the fixture, and set again by the first file that goes out: the
    # test that proves uploads overlap downloads hangs its second download on it
    first_send: Event

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
        type(self).first_send.set()

    async def finalize_stream(self):
        self.finalized += 1


class _FakeSession:
    """The download's HTTP session. Nothing here reaches a CDN."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def recorded(monkeypatch):
    """Replace the uploader the component builds, at its point of import.

    The ``sys.modules`` entry is what gets replaced rather than an imported
    reference: the component imports the uploader lazily, so a name patched
    anywhere else would not be the one it resolves.
    """
    RecordingUploader.built = []
    RecordingUploader.first_send = Event()
    fake = ModuleType("bot.helper.upload.telegram_uploader")
    fake.TelegramUploader = RecordingUploader
    monkeypatch.setitem(sys.modules, "bot.helper.upload.telegram_uploader", fake)
    return RecordingUploader


def _helper(monkeypatch, listener):
    """A helper whose proxy pool is fixed and whose session never connects."""
    monkeypatch.setattr(md, "_get_proxy_list", lambda: ["w1", "w2"])
    monkeypatch.setattr(md.MegaDownloadHelper, "_session", lambda self: _FakeSession())
    return md.MegaDownloadHelper(listener)


def _files(*names):
    return [{"name": name, "path": "", "size": 1} for name in names]


async def test_a_file_is_sent_while_its_sibling_still_downloads(
    monkeypatch, recorded, tmp_path
):
    """The second connection is still fetching when the first file goes out.

    Its download is released by the first upload and by nothing else, so this
    cannot pass by fetching everything first and uploading afterwards.
    """
    listener = FakeMegaTask()
    helper = _helper(monkeypatch, listener)
    downloaded = []

    async def download_file(self, session, item, folder_handle, dest, idx):
        if item["name"] == "b.mkv":
            await wait_for(recorded.first_send.wait(), SENT_WITHIN)
        downloaded.append(item["name"])
        return True

    monkeypatch.setattr(md.MegaDownloadHelper, "_download_file", download_file)

    await helper._run(str(tmp_path), None, _files("a.mkv", "b.mkv"), False)

    uploader = recorded.built[-1]
    assert downloaded == ["a.mkv", "b.mkv"]
    assert uploader.sent == [
        str(tmp_path / "album" / "a.mkv"),
        str(tmp_path / "album" / "b.mkv"),
    ]
    assert uploader.finalized == 1
    # the queued pipeline is never entered: that is the switch -su replaces
    assert listener.completed == 0
    assert listener.errors == []


async def test_a_task_without_the_flag_still_uploads_at_the_end(
    monkeypatch, recorded, tmp_path
):
    listener = FakeMegaTask(stream=False)
    helper = _helper(monkeypatch, listener)

    async def download_file(self, session, item, folder_handle, dest, idx):
        return True

    monkeypatch.setattr(md.MegaDownloadHelper, "_download_file", download_file)

    await helper._run(str(tmp_path), None, _files("a.mkv", "b.mkv"), False)

    assert listener.completed == 1
    assert recorded.built == []
    assert listener.errors == []


async def test_a_file_that_failed_is_not_sent(monkeypatch, recorded, tmp_path):
    """A transfer that raised leaves nothing to send, and the album goes on."""
    listener = FakeMegaTask()
    helper = _helper(monkeypatch, listener)
    downloaded = []

    async def download_file(self, session, item, folder_handle, dest, idx):
        downloaded.append(item["name"])
        if item["name"] == "bad.mkv":
            raise ConnectionError("CDN answered HTTP 503")
        helper._processed += 10
        return True

    monkeypatch.setattr(md.MegaDownloadHelper, "_download_file", download_file)

    await helper._run(str(tmp_path), None, _files("bad.mkv", "a.mkv"), False)

    assert downloaded == ["bad.mkv", "a.mkv"]
    assert recorded.built[-1].sent == [str(tmp_path / "album" / "a.mkv")]
    # what did land is what the task is measured by
    assert listener.size == 10
    assert listener.completed == 0
    assert listener.errors == []


async def test_every_file_failing_is_still_an_error(monkeypatch, recorded, tmp_path):
    listener = FakeMegaTask()
    helper = _helper(monkeypatch, listener)

    async def download_file(self, session, item, folder_handle, dest, idx):
        raise ConnectionError("CDN answered HTTP 503")

    monkeypatch.setattr(md.MegaDownloadHelper, "_download_file", download_file)

    await helper._run(str(tmp_path), None, _files("a.mkv", "b.mkv"), False)

    assert len(listener.errors) == 1
    assert "every file failed" in listener.errors[0]
    assert recorded.built[-1].sent == []
    assert recorded.built[-1].finalized == 0
