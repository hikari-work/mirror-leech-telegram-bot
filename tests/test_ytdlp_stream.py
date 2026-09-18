"""Streaming (``-su``) for yt-dlp downloads.

``-su`` uploads each file the moment it is complete, so a twenty-video playlist
no longer has to land in full before the first video goes out. yt-dlp makes that
harder than the other downloaders do: the download runs on a worker thread, and
the only thing that knows where a file ended up is a postprocessing hook called
on that same thread. The hook therefore puts the upload on the event loop and
waits for it, which is at once the hand-over and the backpressure -- the next
video is not fetched while the previous one is still being sent.

The fake below is ``YouTubeDL`` as this module uses it: a context manager that
fires the postprocessing hooks it was configured with, which is the part under
test.
"""

from __future__ import annotations

import asyncio
import importlib
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

import bot.helper.download.yt_dlp_download as ytd

# the fields of an ``outtmpl`` -- ``%(title,fulltitle,alt_title)s`` and friends
_FIELD = re.compile(r"%\([^)]*\)[0-9]*[sd]")


class _FakeYdl:
    """Writes what the template asks for, then reports it the way yt-dlp does.

    ``produces`` off stands for a download that leaves nothing behind, which is
    what an empty playlist looks like from here; ``on_move`` is the container
    yt-dlp picked being another one than the template named.
    """

    written: list[str] = []
    result: dict = {}
    produces = True
    on_move = None

    def __init__(self, params, recorded):
        self.params = params
        self._recorded = recorded

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        self._recorded.append(("extract_info", url, download))
        return _FakeYdl.result

    @staticmethod
    def prepare_filename(info, outtmpl=None):
        return "Some Title.mp4"

    def download(self, url_list):
        self._recorded.append(("download", tuple(url_list)))
        self._produce()

    def process_ie_result(self, ie_result, download=True):
        self._recorded.append(("process_ie_result", download))
        self._produce()

    def _produce(self):
        if not _FakeYdl.produces:
            return
        template = self.params.get("outtmpl")
        if isinstance(template, dict):
            # the thumbnail template names a directory no media lands in
            template = template.get("default", "")
        # the template's fields filled in: what is left is the directory, and
        # the file in it is the one the template's tail would have named
        dest = _FIELD.sub("", template).rstrip("/.")
        if not dest.endswith(".mp4"):
            dest = f"{dest}/clip 1.mp4"
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"x" * 1024)
        _FakeYdl.written.append(dest)

        moved = {dest: _FakeYdl.on_move(dest)} if _FakeYdl.on_move else {dest: dest}
        if moved[dest] != dest:
            Path(dest).rename(moved[dest])
        for hook in self.params.get("postprocessor_hooks") or []:
            hook(
                {
                    "status": "finished",
                    "postprocessor": "MoveFiles",
                    "info_dict": {"filepath": dest, "__files_to_move": moved},
                }
            )


class _RecordingUploader:
    """Stands in for ``TelegramUploader``: it notes what it was sent.

    The sleep is the time an upload takes, and it is what makes "the downloader
    waited for the uploader" observable: nothing is recorded until the send is
    over.
    """

    built: list = []
    delay = 0.02

    def __init__(self, listener, path):
        self.listener = listener
        self.path = path
        self.sent = []
        self.finalized = 0
        _RecordingUploader.built.append(self)

    async def init_stream(self):
        return True

    async def upload_single(self, file_path):
        await asyncio.sleep(self.delay)
        self.sent.append(file_path)

    async def finalize_stream(self):
        self.finalized += 1


class _Listener:
    """The slice of TaskListener this downloader touches."""

    def __init__(self, stream_upload=False):
        self.link = "https://example.test/watch?v=1"
        self.name = ""
        self.size = 0
        self.mid = "mid1"
        self.multi = 0
        self.is_rss = False
        self.is_cancelled = False
        self.thumbnail_layout = ""
        self.message = object()
        self.started = False
        self.completed = False
        self.error = ""
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


@pytest.fixture
async def ytdlp(monkeypatch, tmp_path):
    """The module's ``YoutubeDL``, thread pool and bridge replaced.

    ``async_to_sync`` is the real one's contract against the loop this test runs
    on rather than the bot's own: the download is on a thread, so the call has to
    round-trip through a running loop the way it does in production.
    """
    loop = asyncio.get_running_loop()
    recorded: list[tuple] = []
    _RecordingUploader.built = []
    _FakeYdl.written = []
    _FakeYdl.produces = True
    _FakeYdl.on_move = None
    _FakeYdl.result = {"id": "v1", "title": "Some Title", "ext": "mp4"}

    async def threaded(func, *args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)

    def bridge(func, *args, wait=True, **kwargs):
        future = asyncio.run_coroutine_threadsafe(func(*args, **kwargs), loop)
        return future.result() if wait else future

    async def not_queued(listener):
        return False, None

    async def no_status(message):
        return None

    monkeypatch.setattr(ytd, "YoutubeDL", lambda params: _FakeYdl(params, recorded))
    monkeypatch.setattr(ytd, "sync_to_async", threaded)
    monkeypatch.setattr(ytd, "async_to_sync", bridge)
    monkeypatch.setattr(ytd, "check_running_tasks", not_queued)
    monkeypatch.setattr(ytd, "send_status_message", no_status)
    monkeypatch.setattr(
        importlib.import_module("bot.helper.upload.telegram_uploader"),
        "TelegramUploader",
        _RecordingUploader,
    )

    async def run(listener, playlist=False):
        helper = ytd.YoutubeDLHelper(listener)
        await helper.add_download(str(tmp_path), "bv*+ba/b", playlist, {})
        return helper

    return SimpleNamespace(
        path=str(tmp_path),
        run=run,
        uploads=_RecordingUploader,
        recorded=recorded,
        written=_FakeYdl.written,
    )


async def test_a_streamed_download_sends_the_file_as_it_lands(ytdlp):
    listener = _Listener(stream_upload=True)

    await ytdlp.run(listener)

    uploader = ytdlp.uploads.built[-1]
    assert uploader.sent == [f"{ytdlp.path}/Some Title.mp4"]
    assert uploader.path == ytdlp.path
    assert uploader.finalized == 1
    # the uploader reports the task, so the queued pipeline is never entered
    assert listener.completed is False
    assert not listener.error


async def test_a_download_without_the_flag_reports_at_the_end(ytdlp):
    listener = _Listener(stream_upload=False)

    await ytdlp.run(listener)

    assert ytdlp.uploads.built == []
    assert listener.completed is True
    assert not listener.error


async def test_the_path_the_hook_reports_is_the_one_sent(ytdlp):
    """Not the one the template asked for: yt-dlp picks the container."""
    listener = _Listener(stream_upload=True)
    _FakeYdl.on_move = lambda dest: dest.replace(".mp4", ".mkv")

    await ytdlp.run(listener)

    assert ytdlp.uploads.built[-1].sent == [f"{ytdlp.path}/Some Title.mkv"]


async def test_a_playlist_sends_each_video_while_the_rest_download(ytdlp):
    """The point of the flag: twenty videos do not have to land first."""
    listener = _Listener(stream_upload=True)
    _FakeYdl.result = {"id": "p1", "entries": [{"id": "v1", "ext": "mp4"}]}

    await ytdlp.run(listener, playlist=True)

    uploader = ytdlp.uploads.built[-1]
    assert uploader.sent == [f"{ytdlp.path}/Some Title/clip 1.mp4"]
    assert uploader.path == f"{ytdlp.path}/Some Title"
    assert uploader.finalized == 1
    assert listener.completed is False


async def test_a_playlist_that_downloaded_nothing_is_still_an_error(ytdlp):
    """The empty directory means the same thing streaming or not."""
    listener = _Listener(stream_upload=True)
    _FakeYdl.result = {"id": "p1", "entries": [{"id": "v1", "ext": "mp4"}]}
    _FakeYdl.produces = False

    await ytdlp.run(listener, playlist=True)

    assert listener.error.startswith("No video available")
    assert listener.completed is False


async def test_a_cancelled_stream_still_sends_what_it_was_handed(ytdlp, monkeypatch):
    """A file already handed over is sent, not abandoned in the queue.

    Stopping is what the user asked for; leaving the uploader holding a file
    nobody is going to collect is not, and it would keep the task's slot in the
    thread pool until the bot restarted.
    """
    listener = _Listener(stream_upload=True)
    produce = _FakeYdl._produce

    def produce_then_cancel(self):
        produce(self)
        listener.is_cancelled = True

    monkeypatch.setattr(_FakeYdl, "_produce", produce_then_cancel)

    await ytdlp.run(listener)

    assert ytdlp.uploads.built[-1].sent == [f"{ytdlp.path}/Some Title.mp4"]
    assert ytdlp.uploads.built[-1].finalized == 0
    assert listener.completed is False


async def test_a_cancelled_download_neither_completes_nor_finalizes(ytdlp, monkeypatch):
    """A cancel is answered by whoever asked for it, not by the uploader.

    The download returns with the task already cancelled and nothing to send --
    the file the user stopped was never produced. Reporting the task complete
    there would contradict the cancel, and finalizing the stream would post the
    uploader's own summary about an album that does not exist.
    """
    listener = _Listener(stream_upload=True)
    monkeypatch.setattr(
        _FakeYdl, "_produce", lambda self: setattr(listener, "is_cancelled", True)
    )
    _FakeYdl.produces = False

    await ytdlp.run(listener)

    assert ytdlp.uploads.built[-1].finalized == 0
    assert listener.completed is False
    assert not listener.error
