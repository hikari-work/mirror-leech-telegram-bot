"""Tests for the Vidara folder downloader.

A folder is one task that walks its listing: each video is resolved to an HLS
ladder when its turn comes -- not when the folder was listed, because the URLs
the gateway mints are IP-bound and expire -- and muxed into a directory named
after the folder. These tests pin that shape, and the two failure modes that
matter in a bulk: one dead video must not cost the other eight, and a folder
where everything failed must not report success with an empty directory.

yt-dlp is replaced by a fake that writes the file it was asked for, so the
naming, the per-entry headers and the cancel path are exercised without network.
"""

from __future__ import annotations

import importlib.util
import sys
from contextlib import asynccontextmanager
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

    def __init__(self, link, name=""):
        self.link = link
        self.name = name
        self.size = 0
        self.mid = 1
        self.multi = 0
        self.is_rss = False
        self.is_cancelled = False
        self.message = object()
        self.started = False
        self.completed = False
        self.error = ""

    async def on_download_start(self):
        self.started = True

    async def on_download_complete(self):
        self.completed = True

    async def on_download_error(self, error):
        self.error = str(error)


class _FakeYoutubeDL:
    """Writes the file ``outtmpl`` asks for and reports its bytes.

    Registered per test through ``calls``, which records what each download was
    handed -- the URL, the headers and the path that came out of the template.
    """

    calls: list[dict] = []
    on_download = None

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def download(self, urls):
        # yt-dlp decides the container; the template is what carries the stem
        dest = self.opts["outtmpl"].replace("%(ext)s", "mp4").replace("%%", "%")
        _FakeYoutubeDL.calls.append(
            {
                "url": urls[0],
                "headers": self.opts.get("http_headers"),
                "dest": dest,
            }
        )
        if _FakeYoutubeDL.on_download:
            _FakeYoutubeDL.on_download(self, urls[0])
        Path(dest).write_bytes(b"x" * 1024)
        for hook in self.opts.get("progress_hooks") or []:
            hook({"status": "downloading", "downloaded_bytes": 1024})


async def _sync_to_async(func, *args, **kwargs):
    # inline rather than threaded: the fake yt-dlp does not block, and a
    # SystemExit from the progress hook has to reach the caller the way a
    # thread's would
    return func(*args, **kwargs)


@asynccontextmanager
async def _resolve_gate():
    yield


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


def _stub_modules(resolved):
    """The slice of the bot package ``vidara_download`` imports.

    ``resolved`` maps a page URL to what ``vidara_resolve`` should hand back --
    a (name, link, headers) triple, or an exception to raise for a dead video.
    """

    def _vidara_resolve(url, name=""):
        entry = resolved[url]
        if isinstance(entry, Exception):
            raise entry
        return entry

    modules = {
        "bot": _module(
            "bot",
            package=True,
            LOGGER=_Logger(),
            task_dict={},
            task_dict_lock=_Lock(),
        ),
        "bot.helper.ext_utils.bot_utils": _module(
            "bot.helper.ext_utils.bot_utils", sync_to_async=_sync_to_async
        ),
        "bot.helper.ext_utils.resolve_gate": _module(
            "bot.helper.ext_utils.resolve_gate", resolve_gate=_resolve_gate
        ),
        "bot.helper.ext_utils.task_manager": _module(
            "bot.helper.ext_utils.task_manager",
            check_running_tasks=_check_running_tasks,
        ),
        "bot.helper.telegram_helper.message_utils": _module(
            "bot.helper.telegram_helper.message_utils",
            send_status_message=_send_status_message,
        ),
        "bot.helper.mirror_leech_utils.status_utils.queue_status": _module(
            "bot.helper.mirror_leech_utils.status_utils.queue_status",
            QueueStatus=lambda *args, **kwargs: object(),
        ),
        "bot.helper.mirror_leech_utils.status_utils.vidara_status": _module(
            "bot.helper.mirror_leech_utils.status_utils.vidara_status",
            VidaraStatus=lambda *args, **kwargs: object(),
        ),
        "bot.helper.mirror_leech_utils.download_utils.direct_link_generators": _module(
            "bot.helper.mirror_leech_utils.download_utils.direct_link_generators",
            vidara_resolve=_vidara_resolve,
        ),
    }
    for name in (
        "bot.helper",
        "bot.helper.ext_utils",
        "bot.helper.telegram_helper",
        "bot.helper.mirror_leech_utils",
        "bot.helper.mirror_leech_utils.status_utils",
        "bot.helper.mirror_leech_utils.download_utils",
    ):
        modules[name] = _module(name, package=True)
    return modules


def _load(monkeypatch, name):
    """Load one real ``download_utils`` module under the stubbed package tree."""
    path = (
        _ROOT / "bot" / "helper" / "mirror_leech_utils" / "download_utils"
        / f"{name}.py"
    )
    spec = importlib.util.spec_from_file_location(
        f"bot.helper.mirror_leech_utils.download_utils.{name}", path
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def vidara_dl(monkeypatch, tmp_path):
    """Load ``vidara_download.py`` with the bot package stubbed to what it uses."""
    _FakeYoutubeDL.calls = []
    _FakeYoutubeDL.on_download = None

    resolved = {}
    for name, mod in _stub_modules(resolved).items():
        monkeypatch.setitem(sys.modules, name, mod)

    # the queue slot, the counters and the cancel path come from the shared base
    _load(monkeypatch, "multi_video_download")
    module = _load(monkeypatch, "vidara_download")
    monkeypatch.setattr(module, "YoutubeDL", _FakeYoutubeDL)

    return SimpleNamespace(module=module, resolved=resolved, path=str(tmp_path))


def _entry(name, code="aaa", subpath=""):
    return {
        "name": name,
        "url": f"https://vidara.to/v/{code}",
        "code": code,
        "subpath": subpath,
        "duration": 42,
    }


def _stream(code="aaa", name="clip"):
    return (
        name,
        f"https://cdn.test/{code}/master.m3u8",
        {"Referer": "https://vidara.to/"},
    )


def _folder(*entries, title="ZILVIAZU"):
    return {
        "vidara": True,
        "title": title,
        "folder_url": "https://vidara.to/f/ROOT",
        "videos": list(entries),
    }


async def _run(harness, listener):
    await harness.module.add_vidara_download(listener, harness.path)


# ── the happy path ───────────────────────────────────────────────────


async def test_a_folder_lands_in_one_directory_named_after_it(vidara_dl):
    listener = _Listener(_folder(_entry("clip 1", "aaa"), _entry("clip 2", "bbb")))
    vidara_dl.resolved.update(
        {
            "https://vidara.to/v/aaa": _stream("aaa"),
            "https://vidara.to/v/bbb": _stream("bbb"),
        }
    )

    await _run(vidara_dl, listener)

    folder = Path(vidara_dl.path) / "ZILVIAZU"
    assert sorted(p.name for p in folder.iterdir()) == ["clip 1.mp4", "clip 2.mp4"]
    assert listener.completed is True
    assert listener.started is True
    assert not listener.error


async def test_subfolder_entries_keep_their_own_directory(vidara_dl):
    listener = _Listener(
        _folder(_entry("ep1", "aaa", subpath="Season 1"), _entry("trailer", "bbb"))
    )
    vidara_dl.resolved.update(
        {
            "https://vidara.to/v/aaa": _stream("aaa"),
            "https://vidara.to/v/bbb": _stream("bbb"),
        }
    )

    await _run(vidara_dl, listener)

    root = Path(vidara_dl.path) / "ZILVIAZU"
    assert (root / "Season 1" / "ep1.mp4").is_file()
    assert (root / "trailer.mp4").is_file()


async def test_the_name_flag_wins_over_the_folder_title(vidara_dl):
    listener = _Listener(_folder(_entry("clip", "aaa")), name="my folder")
    vidara_dl.resolved["https://vidara.to/v/aaa"] = _stream("aaa")

    await _run(vidara_dl, listener)

    assert (Path(vidara_dl.path) / "my folder" / "clip.mp4").is_file()


async def test_a_percent_in_a_title_is_not_a_template(vidara_dl):
    listener = _Listener(_folder(_entry("100%(real)s clip", "aaa")))
    vidara_dl.resolved["https://vidara.to/v/aaa"] = _stream("aaa")

    await _run(vidara_dl, listener)

    assert (
        Path(vidara_dl.path) / "ZILVIAZU" / "100%(real)s clip.mp4"
    ).is_file()


async def test_the_headers_ride_with_each_stream(vidara_dl):
    """The CDN checks Referer, so a stream fetched without its headers 403s."""
    listener = _Listener(_folder(_entry("clip", "aaa")))
    vidara_dl.resolved["https://vidara.to/v/aaa"] = _stream("aaa")

    await _run(vidara_dl, listener)

    call = _FakeYoutubeDL.calls[0]
    assert call["url"] == "https://cdn.test/aaa/master.m3u8"
    assert call["headers"] == {"Referer": "https://vidara.to/"}


async def test_each_video_is_resolved_when_its_turn_comes(vidara_dl):
    """Resolving the whole folder up front mints URLs that expire before the
    tail of the listing is reached."""
    order = []
    listener = _Listener(_folder(_entry("clip 1", "aaa"), _entry("clip 2", "bbb")))

    def _resolve(url, name=""):
        order.append(("resolve", url))
        return _stream(url.rsplit("/", 1)[-1])

    # the downloader imports vidara_resolve lazily, so patching the stub module
    # after load is what it will pick up
    sys.modules[
        "bot.helper.mirror_leech_utils.download_utils.direct_link_generators"
    ].vidara_resolve = _resolve
    _FakeYoutubeDL.on_download = lambda ydl, url: order.append(("download", url))

    await _run(vidara_dl, listener)

    assert [step for step, _ in order] == [
        "resolve",
        "download",
        "resolve",
        "download",
    ]


# ── failure modes ────────────────────────────────────────────────────


async def test_one_dead_video_does_not_cost_the_others(vidara_dl):
    listener = _Listener(_folder(_entry("gone", "aaa"), _entry("clip", "bbb")))
    vidara_dl.resolved.update(
        {
            "https://vidara.to/v/aaa": RuntimeError("ERROR: video not found"),
            "https://vidara.to/v/bbb": _stream("bbb"),
        }
    )

    await _run(vidara_dl, listener)

    folder = Path(vidara_dl.path) / "ZILVIAZU"
    assert [p.name for p in folder.iterdir()] == ["clip.mp4"]
    assert listener.completed is True
    assert not listener.error


async def test_a_folder_where_everything_failed_is_an_error(vidara_dl):
    listener = _Listener(_folder(_entry("gone", "aaa"), _entry("also gone", "bbb")))
    vidara_dl.resolved.update(
        {
            "https://vidara.to/v/aaa": RuntimeError("ERROR: video not found"),
            "https://vidara.to/v/bbb": RuntimeError("ERROR: video removed"),
        }
    )

    await _run(vidara_dl, listener)

    assert listener.completed is False
    assert "video not found" in listener.error


async def test_a_download_that_fails_is_counted_not_raised(vidara_dl):
    listener = _Listener(_folder(_entry("clip 1", "aaa"), _entry("clip 2", "bbb")))
    vidara_dl.resolved.update(
        {
            "https://vidara.to/v/aaa": _stream("aaa"),
            "https://vidara.to/v/bbb": _stream("bbb"),
        }
    )

    def _fail_first(ydl, url):
        if "aaa" in url:
            raise OSError("connection reset")

    _FakeYoutubeDL.on_download = _fail_first

    await _run(vidara_dl, listener)

    folder = Path(vidara_dl.path) / "ZILVIAZU"
    assert [p.name for p in folder.iterdir()] == ["clip 2.mp4"]
    assert listener.completed is True


async def test_a_cancel_mid_mux_stops_the_folder(vidara_dl):
    listener = _Listener(_folder(_entry("clip 1", "aaa"), _entry("clip 2", "bbb")))
    vidara_dl.resolved.update(
        {
            "https://vidara.to/v/aaa": _stream("aaa"),
            "https://vidara.to/v/bbb": _stream("bbb"),
        }
    )

    def _cancel(ydl, url):
        listener.is_cancelled = True

    _FakeYoutubeDL.on_download = _cancel

    await _run(vidara_dl, listener)

    # the progress hook raises out of the first video; nothing after it runs
    assert len(_FakeYoutubeDL.calls) == 1
    assert listener.completed is False
    assert not listener.error


async def test_a_cancel_before_the_first_video_downloads_nothing(vidara_dl):
    listener = _Listener(_folder(_entry("clip", "aaa")))
    vidara_dl.resolved["https://vidara.to/v/aaa"] = _stream("aaa")
    listener.is_cancelled = True

    await _run(vidara_dl, listener)

    assert _FakeYoutubeDL.calls == []
    assert listener.completed is False
