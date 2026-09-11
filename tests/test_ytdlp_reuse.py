"""yt-dlp must resolve a link once, not once to probe and once to download.

``add_download`` probes the link to learn the name -- and ``outtmpl`` is built
from that name -- and then ``_download`` handed the same link to a second
``YoutubeDL``, which extracted it all over again. For a video behind an HLS
master playlist that is a second round of requests before a single byte moves.

The result of the probe is now kept and handed to the download. What makes that
safe is that yt-dlp evaluates ``outtmpl`` when it prepares the filename
(``YoutubeDL._prepare_filename``), not out of the info dict -- so the template
``add_download`` sets *after* probing still decides where the file lands, which
is the whole reason the probe happens first.

The fake below stands in for ``YoutubeDL`` and records which of the two entry
points was used; nothing here reaches the network.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from yt_dlp.utils import ExtractorError

import bot.helper.download.yt_dlp_download as ytd


class _FakeYdl:
    """``YoutubeDL`` as the module uses it: a context manager over the calls."""

    def __init__(self, params, script, recorded):
        self.params = params
        self._script = script
        self._recorded = recorded

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        self._recorded.append(("extract_info", url, download))
        return self._script["result"]

    @staticmethod
    def prepare_filename(info, outtmpl=None):
        return "/downloads/Some Title.mp4"

    def download(self, url_list):
        self._recorded.append(("download", tuple(url_list)))
        if failure := self._script.get("download_raises"):
            raise failure

    def process_ie_result(self, ie_result, download=True):
        self._recorded.append(("process_ie_result", ie_result, download))
        if failure := self._script.get("process_raises"):
            raise failure


@pytest.fixture
def ytdlp(monkeypatch):
    """The module's ``YoutubeDL`` replaced, and what reaches the listener noted.

    ``script["result"]`` is what the probe extracts. ``script["process_raises"]``
    and ``script["download_raises"]`` make the matching entry point fail, which
    is how the two error paths are told apart.
    """
    recorded: list[tuple] = []
    events: list[str] = []
    script: dict = {"result": None}

    def build(params):
        return _FakeYdl(params, script, recorded)

    def fake_async_to_sync(func, *args, **kwargs):
        # the two calls the helper makes here are the listener's own callbacks
        events.append(getattr(func, "__name__", "call"))
        return None

    monkeypatch.setattr(ytd, "YoutubeDL", build)
    monkeypatch.setattr(ytd, "async_to_sync", fake_async_to_sync)

    def helper():
        # named rather than lambdas: ``events`` records the callback by name
        def on_download_complete():
            pass

        def on_download_error(error):
            pass

        return ytd.YoutubeDLHelper(
            SimpleNamespace(
                link="https://example.test/watch?v=1",
                name="",
                size=0,
                is_cancelled=False,
                on_download_complete=on_download_complete,
                on_download_error=on_download_error,
            )
        )

    return SimpleNamespace(
        recorded=recorded,
        events=events,
        script=script,
        helper=helper,
        calls=lambda kind: [call for call in recorded if call[0] == kind],
    )


def test_a_single_video_is_extracted_only_once(ytdlp):
    """The probe's result is the download's result -- one extraction, not two."""
    result = {"id": "v1", "title": "Some Title", "ext": "mp4"}
    ytdlp.script["result"] = result
    helper = ytdlp.helper()

    helper._extract_meta_data()
    helper._download("/downloads/task")

    assert len(ytdlp.calls("extract_info")) == 1
    assert ytdlp.calls("process_ie_result") == [("process_ie_result", result, True)]
    # the link was not handed to a second extracting download
    assert ytdlp.calls("download") == []
    assert ytdlp.events == ["on_download_complete"]


def test_a_playlist_still_downloads_by_link(ytdlp):
    """A playlist is not reused: its entries are resolved per item on the way."""
    ytdlp.script["result"] = {
        "id": "p1",
        "entries": [{"id": "v1", "title": "One", "ext": "mp4"}],
    }
    helper = ytdlp.helper()
    helper.is_playlist = True

    helper._extract_meta_data()
    helper._download("/downloads/task")

    assert helper._ie_result is None
    assert len(ytdlp.calls("extract_info")) == 1
    assert ytdlp.calls("process_ie_result") == []
    assert ytdlp.calls("download") == [
        ("download", ("https://example.test/watch?v=1",))
    ]


def test_a_probe_that_failed_leaves_the_download_on_the_old_path(ytdlp):
    """No result to reuse means falling back, not passing ``None`` to yt-dlp."""
    ytdlp.script["result"] = None
    helper = ytdlp.helper()

    helper._extract_meta_data()
    assert helper._ie_result is None

    helper._download("/downloads/task")

    assert ytdlp.calls("process_ie_result") == []
    assert len(ytdlp.calls("download")) == 1


def test_a_stale_result_is_dropped_before_the_next_probe(ytdlp):
    """``_ie_result`` describes this probe's link, so it is cleared first."""
    ytdlp.script["result"] = {"id": "v1", "title": "One", "ext": "mp4"}
    helper = ytdlp.helper()
    helper._extract_meta_data()
    assert helper._ie_result is not None

    # the next probe is a playlist, which must not reuse the video above
    ytdlp.script["result"] = {"id": "p1", "entries": [{"id": "v2", "ext": "mp4"}]}
    helper.is_playlist = True
    helper._extract_meta_data()

    assert helper._ie_result is None


def test_a_reused_result_reports_a_failure_the_same_way(ytdlp):
    """``process_ie_result`` raises a *sibling* of what ``download`` raises.

    For the same unavailable format ``ydl.download`` surfaces ``DownloadError``
    while ``process_ie_result`` surfaces ``ExtractorError``, and neither is the
    other's parent. Catching only ``DownloadError`` -- as this did while both
    paths were one path -- would let the reuse path fail silently.
    """
    ytdlp.script["result"] = {"id": "v1", "title": "Some Title", "ext": "mp4"}
    ytdlp.script["process_raises"] = ExtractorError("Requested format is not available")
    helper = ytdlp.helper()
    helper._extract_meta_data()

    helper._download("/downloads/task")

    # reported, and reported as a failure rather than as a completion
    assert ytdlp.events == ["on_download_error"]
    assert helper._listener.is_cancelled


def test_a_failed_link_download_reports_the_same_way(ytdlp):
    """The playlist path keeps reporting through the same handler."""
    from yt_dlp.utils import DownloadError

    ytdlp.script["result"] = {"id": "p1", "entries": [{"id": "v2", "ext": "mp4"}]}
    ytdlp.script["download_raises"] = DownloadError("ERROR: unavailable")
    helper = ytdlp.helper()
    helper.is_playlist = True
    helper._extract_meta_data()

    helper._download("/downloads/task")

    assert ytdlp.events == ["on_download_error"]
    assert helper._listener.is_cancelled
