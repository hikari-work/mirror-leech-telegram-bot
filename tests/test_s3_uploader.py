"""Tests for the bucket uploader.

Two things this module wraps are worth faking and are faked here. The object
store: a stub ``botocore`` records the client options it is handed, so the
checksum settings -- which are load-bearing against R2 rather than tuning --
can be asserted without a network. And the thread pool: ``sync_to_async`` is
replaced by a direct call, because the real one posts to the bot's event loop.

The filesystem is *not* faked. Walking a download directory is a thing this
module actually does, and a temporary tree is the cheapest honest way to check
which files come out of it.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest

import bot.helper.upload.s3_uploader as s3u
from bot.core.config_manager import Config

DEFAULTS = {
    "S3_ENDPOINT_URL": "https://acct.r2.cloudflarestorage.com",
    "S3_ACCESS_KEY_ID": "key",
    "S3_SECRET_ACCESS_KEY": "secret",
    "S3_BUCKET": "bucketmirror",
    "S3_REGION": "auto",
    "S3_KEY_PREFIX": "",
    "S3_BROWSER_URL": "http://127.0.0.1:8080",
    "S3_MULTIPART_CONCURRENCY": 4,
}
"""Everything ``s3_config_error`` insists on, plus the keys that have a usable
default. Filled in per test so a test only has to say which one it empties."""


# ── fakes ───────────────────────────────────────────────────────────


class FakeListener:
    """The slice of a task the uploader reads and reports back to."""

    def __init__(self, mid=10032, name="Big.Buck.Bunny"):
        self.mid = mid
        self.name = name
        self.is_cancelled = False
        self.completed: tuple | None = None
        self.errors: list[str] = []

    async def on_upload_complete(self, link, files, folders, mime_type):
        self.completed = (link, files, folders, mime_type)

    async def on_upload_error(self, error, *args, **kwargs):
        self.errors.append(error)


class FakeClient:
    """Records what was asked of the bucket, and can misbehave on demand."""

    def __init__(self, listener, *, fail_on=(), cancel_on=None):
        self.listener = listener
        self.calls: list[tuple[str, str, str]] = []
        self.fail_on = set(fail_on)
        self.cancel_on = cancel_on

    def upload_file(self, path, bucket, key, Callback=None, Config=None):
        self.calls.append((path, bucket, key))
        if key == self.cancel_on:
            # ``/cancel`` sets this from another coroutine; the callback below
            # is what turns it into a stopped transfer.
            self.listener.is_cancelled = True
        if Callback is not None:
            Callback(7)
        if key in self.fail_on:
            raise OSError("simulated transfer failure")


@pytest.fixture
def s3(monkeypatch):
    """Configure the module for a working bucket and let a test wire a client."""
    for name, value in DEFAULTS.items():
        monkeypatch.setattr(Config, name, value)

    async def immediate(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(s3u, "sync_to_async", immediate)

    def wire(listener, **kwargs):
        client = FakeClient(listener, **kwargs)
        monkeypatch.setattr(s3u, "_make_client", lambda: client)
        return client

    return SimpleNamespace(wire=wire)


def tree(root, *relative_paths):
    """Create *relative_paths* under *root*, the last name being a real file."""
    for rel in relative_paths:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(rel.encode())
    return root


# ── one task, one folder ────────────────────────────────────────────


async def test_one_task_lands_in_one_folder_named_after_its_id(s3, tmp_path):
    """The payload folder is peeled off, so 10032/ holds the album's files."""
    up_dir = tmp_path / "10032"
    tree(up_dir, "Big.Buck.Bunny/a.mkv", "Big.Buck.Bunny/b.mkv")
    listener = FakeListener(name="Big.Buck.Bunny")
    client = s3.wire(listener)

    await s3u.S3Uploader(listener, str(up_dir)).upload()

    assert [key for _, _, key in client.calls] == ["10032/a.mkv", "10032/b.mkv"]


async def test_sub_directories_inside_the_payload_are_kept(s3, tmp_path):
    up_dir = tmp_path / "10032"
    tree(up_dir, "Album/subs/ep1.srt", "Album/movie.mkv")
    listener = FakeListener(name="Album")
    client = s3.wire(listener)

    await s3u.S3Uploader(listener, str(up_dir)).upload()

    assert sorted(key for _, _, key in client.calls) == [
        "10032/movie.mkv",
        "10032/subs/ep1.srt",
    ]


async def test_a_lone_file_is_not_looked_for_inside_a_folder_of_its_name(s3, tmp_path):
    """When the payload is a bare file there is no folder to peel off."""
    up_dir = tmp_path / "10032"
    tree(up_dir, "movie.mkv")
    listener = FakeListener(name="movie.mkv")
    client = s3.wire(listener)

    await s3u.S3Uploader(listener, str(up_dir)).upload()

    assert [key for _, _, key in client.calls] == ["10032/movie.mkv"]


async def test_a_configured_key_prefix_wraps_the_task_id(s3, tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "S3_KEY_PREFIX", "/mirror/")
    up_dir = tmp_path / "10032"
    tree(up_dir, "Album/movie.mkv")
    listener = FakeListener(name="Album")
    client = s3.wire(listener)

    await s3u.S3Uploader(listener, str(up_dir)).upload()

    assert [key for _, _, key in client.calls] == ["mirror/10032/movie.mkv"]


# ── what is deliberately not stored ─────────────────────────────────


@pytest.mark.parametrize("dirname", ["yt-dlp-thumb", "_mltbss"])
async def test_a_telegram_scratch_folder_never_reaches_the_bucket(
    s3, tmp_path, dirname
):
    up_dir = tmp_path / "10032"
    tree(up_dir, "Album/movie.mkv", f"Album/{dirname}/scratch.jpg")
    listener = FakeListener(name="Album")
    client = s3.wire(listener)

    await s3u.S3Uploader(listener, str(up_dir)).upload()

    assert [key for _, _, key in client.calls] == ["10032/movie.mkv"]


async def test_a_zero_byte_file_is_counted_and_not_sent(s3, tmp_path):
    up_dir = tmp_path / "10032"
    tree(up_dir, "Album/movie.mkv")
    (up_dir / "Album" / "empty.mkv").write_bytes(b"")
    listener = FakeListener(name="Album")
    client = s3.wire(listener)

    await s3u.S3Uploader(listener, str(up_dir)).upload()

    assert [key for _, _, key in client.calls] == ["10032/movie.mkv"]
    assert listener.completed is not None
    assert listener.completed[3] == 1


async def test_one_unreadable_file_does_not_lose_the_others(s3, tmp_path):
    up_dir = tmp_path / "10032"
    tree(up_dir, "Album/a.mkv", "Album/b.mkv")
    listener = FakeListener(name="Album")
    client = s3.wire(listener, fail_on={"10032/b.mkv"})

    await s3u.S3Uploader(listener, str(up_dir)).upload()

    assert [key for _, _, key in client.calls] == ["10032/a.mkv", "10032/b.mkv"]
    assert listener.completed is not None
    assert listener.completed[3] == 1


# ── reporting ───────────────────────────────────────────────────────


async def test_the_message_carries_exactly_one_link_to_the_folder(s3, tmp_path):
    up_dir = tmp_path / "10032"
    tree(up_dir, "Album/a.mkv", "Album/b.mkv", "Album/c.mkv")
    listener = FakeListener(name="Album")
    s3.wire(listener)

    await s3u.S3Uploader(listener, str(up_dir)).upload()

    link, files, folders, corrupted = listener.completed
    assert link == "http://127.0.0.1:8080/?bucket=bucketmirror&prefix=10032/"
    assert files == {link: "10032/"}
    assert folders == 3
    assert corrupted == 0


async def test_an_empty_download_is_an_error_not_an_empty_folder(s3, tmp_path):
    up_dir = tmp_path / "10032"
    up_dir.mkdir()
    listener = FakeListener(name="Album")
    s3.wire(listener)

    await s3u.S3Uploader(listener, str(up_dir)).upload()

    assert listener.completed is None
    assert listener.errors == [s3u.NO_FILES_ERROR]


async def test_nothing_uploadable_at_all_reports_the_failure(s3, tmp_path):
    up_dir = tmp_path / "10032"
    tree(up_dir, "Album/a.mkv")
    listener = FakeListener(name="Album")
    s3.wire(listener, fail_on={"10032/a.mkv"})

    await s3u.S3Uploader(listener, str(up_dir)).upload()

    assert listener.completed is None
    assert "simulated transfer failure" in listener.errors[0]


async def test_a_missing_setting_fails_before_the_files_are_walked(
    s3, tmp_path, monkeypatch
):
    monkeypatch.setattr(Config, "S3_BUCKET", "")
    up_dir = tmp_path / "10032"
    tree(up_dir, "Album/a.mkv")
    listener = FakeListener(name="Album")
    client = s3.wire(listener)

    await s3u.S3Uploader(listener, str(up_dir)).upload()

    assert client.calls == []
    assert "S3_BUCKET" in listener.errors[0]


# ── stopping ────────────────────────────────────────────────────────


async def test_a_cancelled_task_uploads_nothing(s3, tmp_path):
    up_dir = tmp_path / "10032"
    tree(up_dir, "Album/a.mkv")
    listener = FakeListener(name="Album")
    listener.is_cancelled = True
    client = s3.wire(listener)

    await s3u.S3Uploader(listener, str(up_dir)).upload()

    assert client.calls == []
    assert listener.completed is None


async def test_cancelling_mid_transfer_aborts_the_upload_in_flight(s3, tmp_path):
    """A running future cannot be recalled, so the callback raises instead."""
    up_dir = tmp_path / "10032"
    tree(up_dir, "Album/a.mkv", "Album/b.mkv")
    listener = FakeListener(name="Album")
    client = s3.wire(listener, cancel_on="10032/b.mkv")

    await s3u.S3Uploader(listener, str(up_dir)).upload()

    assert [key for _, _, key in client.calls] == ["10032/a.mkv", "10032/b.mkv"]
    assert listener.completed is None


# ── progress ────────────────────────────────────────────────────────


def test_the_callback_accumulates_what_has_been_sent(s3):
    listener = FakeListener()
    uploader = s3u.S3Uploader(listener, "/nowhere")
    callback = s3u._ProgressCallback(uploader)

    callback(100)
    callback(50)

    assert uploader.processed_bytes == 150


def test_speed_is_the_bytes_sent_over_the_seconds_elapsed(s3, monkeypatch):
    clock = [1_000.0]
    monkeypatch.setattr(s3u, "time", lambda: clock[0])
    uploader = s3u.S3Uploader(FakeListener(), "/nowhere")
    clock[0] += 4

    s3u._ProgressCallback(uploader)(400)

    assert uploader.speed == 100


def test_cancellation_is_the_listeners_to_decide(s3):
    listener = FakeListener()
    uploader = s3u.S3Uploader(listener, "/nowhere")

    assert uploader.is_cancelled is False
    listener.is_cancelled = True
    assert uploader.is_cancelled is True


# ── the work that must not happen on the event loop ─────────────────


class _OffloadRecorder:
    """A ``sync_to_async`` that really does hop threads, and says so.

    The ``s3`` fixture's stand-in runs the function inline, which is what makes
    every other test here a plain call -- and also what makes it blind to
    whether the module offloads anything at all. This one keeps the hop, so a
    test can ask *where* a function ran rather than only that it ran.
    """

    def __init__(self):
        self.functions: list = []
        self.threads: list[threading.Thread] = []

    async def __call__(self, func, *args, **kwargs):
        self.functions.append(func)

        def record(*inner_args, **inner_kwargs):
            self.threads.append(threading.current_thread())
            return func(*inner_args, **inner_kwargs)

        return await asyncio.to_thread(record, *args, **kwargs)


@pytest.fixture
def offloading(s3, monkeypatch):
    """The configured bucket, with the offload seam observed instead of faked."""
    recorder = _OffloadRecorder()
    monkeypatch.setattr(s3u, "sync_to_async", recorder)
    return recorder


async def test_the_client_is_built_off_the_event_loop(
    offloading, tmp_path, monkeypatch
):
    """Building it imports boto3 and has it read credentials off disk.

    That is filesystem and import work, and it used to run in the middle of the
    upload coroutine -- on the one event loop every other task in the bot shares.
    """
    up_dir = tmp_path / "10032"
    tree(up_dir, "Album/a.mkv")
    listener = FakeListener(name="Album")
    built: list[threading.Thread] = []

    def make_client():
        built.append(threading.current_thread())
        return FakeClient(listener)

    monkeypatch.setattr(s3u, "_make_client", make_client)

    await s3u.S3Uploader(listener, str(up_dir)).upload()

    assert make_client in offloading.functions
    assert built and built[0] is not threading.main_thread()


async def test_the_transfer_config_is_built_once_for_the_whole_task(
    s3, monkeypatch, tmp_path
):
    """It only describes how one file is split, and that never changes."""
    up_dir = tmp_path / "10032"
    tree(up_dir, "Album/a.mkv", "Album/b.mkv", "Album/c.mkv")
    listener = FakeListener(name="Album")
    s3.wire(listener)

    built = []
    real = s3u._transfer_config

    def counted():
        built.append(real())
        return built[-1]

    monkeypatch.setattr(s3u, "_transfer_config", counted)

    await s3u.S3Uploader(listener, str(up_dir)).upload()

    assert len(built) == 1


async def test_the_client_is_closed_when_the_task_is_done(s3, tmp_path):
    """Nothing else closes it, so its connections outlived the task."""
    up_dir = tmp_path / "10032"
    tree(up_dir, "Album/a.mkv")
    listener = FakeListener(name="Album")
    client = s3.wire(listener)
    closed = []
    client.close = lambda: closed.append(True)

    await s3u.S3Uploader(listener, str(up_dir)).upload()

    assert closed == [True]


# ── the link, and the client that makes it possible ─────────────────


def test_the_link_follows_the_browser_apps_own_shape(s3):
    assert s3u.browse_url(FakeListener()) == (
        "http://127.0.0.1:8080/?bucket=bucketmirror&prefix=10032/"
    )


@pytest.mark.parametrize(
    "configured", ["http://127.0.0.1:8080", "http://127.0.0.1:8080/"]
)
def test_a_trailing_slash_on_the_browser_url_does_not_double_up(
    s3, monkeypatch, configured
):
    monkeypatch.setattr(Config, "S3_BROWSER_URL", configured)

    assert s3u.browse_url(FakeListener()) == (
        "http://127.0.0.1:8080/?bucket=bucketmirror&prefix=10032/"
    )


def test_the_folder_separator_in_the_link_is_not_escaped(s3, monkeypatch):
    """``10032%2F`` is a folder name to nothing; the app reads this as a path."""
    monkeypatch.setattr(Config, "S3_KEY_PREFIX", "mirror")

    assert s3u.browse_url(FakeListener()).endswith("&prefix=mirror/10032/")


def test_a_bucket_name_that_needs_escaping_is_escaped(s3, monkeypatch):
    monkeypatch.setattr(Config, "S3_BUCKET", "a bucket")

    assert "bucket=a%20bucket" in s3u.browse_url(FakeListener())


def test_every_missing_setting_is_named(s3, monkeypatch):
    monkeypatch.setattr(Config, "S3_BUCKET", "")
    monkeypatch.setattr(Config, "S3_BROWSER_URL", "")

    error = s3u.s3_config_error()

    assert "S3_BUCKET" in error
    assert "S3_BROWSER_URL" in error
    assert "S3_ENDPOINT_URL" not in error


def test_a_configured_bucket_has_nothing_to_report(s3):
    assert s3u.s3_config_error() == ""


def test_the_client_is_built_in_the_dialect_r2_accepts(s3, monkeypatch):
    """boto3 1.36 sends a checksum R2 refuses unless both options say otherwise.

    Without these two the first upload fails with "x-amz-content-sha256 must be
    UNSIGNED-PAYLOAD" or "You can only specify one non-default checksum at a
    time" -- so they are asserted rather than left to the service to discover.
    """
    recorded = {}

    class BotoConfig:
        def __init__(self, **kwargs):
            recorded.update(kwargs)

    def client(service, **kwargs):
        recorded["service"] = service
        recorded["client"] = kwargs
        return "a client"

    session = SimpleNamespace(Session=lambda: SimpleNamespace(client=client))
    fake_boto3 = SimpleNamespace(session=session)
    fake_botocore = ModuleType("botocore")
    fake_config = ModuleType("botocore.config")
    fake_config.Config = BotoConfig
    monkeypatch.setitem(sys.modules, "boto3", fake_boto3)
    monkeypatch.setitem(sys.modules, "botocore", fake_botocore)
    monkeypatch.setitem(sys.modules, "botocore.config", fake_config)

    s3u._make_client()

    assert recorded["request_checksum_calculation"] == "when_required"
    assert recorded["response_checksum_validation"] == "when_required"
    assert recorded["client"]["endpoint_url"] == DEFAULTS["S3_ENDPOINT_URL"]
    assert recorded["client"]["region_name"] == "auto"
    # folded into endpoint_url it would repeat the bucket at the front of every
    # key under virtual-hosted addressing
    assert "bucketmirror" not in recorded["client"]["endpoint_url"]
