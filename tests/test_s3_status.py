"""Tests for the bucket upload's status line.

``/status`` reads a task through a fixed set of methods, so this file checks
three things: that ``S3UploadStatus`` answers all of them, that it reports the
upload direction rather than the download one it inherits, and that the status
page knows to ask it for a speed -- a tool missing from that list still renders,
just without the rate, which is the kind of gap nobody reports as a bug.
"""

from __future__ import annotations

from inspect import iscoroutinefunction
from pathlib import Path
from types import SimpleNamespace

from bot.helper.progress.s3_status import S3UploadStatus
from bot.helper.util.status_utils import MirrorStatus, get_readable_file_size

_ROOT = Path(__file__).resolve().parent.parent


class FakeTask:
    """The listener fields ``BaseStatus`` reads."""

    name = "Big.Buck.Bunny"
    size = 1000
    is_cancelled = False


class FakeUploader:
    """``S3Uploader`` exposes both of these as properties, not methods."""

    def __init__(self, processed=250, speed=50):
        self.processed_bytes = processed
        self.speed = speed


def status(processed=250, speed=50):
    return S3UploadStatus(FakeTask(), FakeUploader(processed, speed), "gid")


def _get_download_status():
    """Load the status page's per-task formatter without importing ``bot.modules``.

    Importing the module for real starts the bot's scheduler and opens its log
    file in the pytest process, which the import sweep goes out of its way to
    avoid. The function itself depends on nothing but ``iscoroutinefunction``.
    """
    source = (_ROOT / "bot" / "modules" / "status.py").read_text(encoding="utf-8")
    start = source.find("async def get_download_status(")
    end = source.find("\n\n@", start)
    namespace: dict = {"iscoroutinefunction": iscoroutinefunction}
    exec(compile(source[start:end], "status.py", "exec"), namespace)  # noqa: S102
    return namespace["get_download_status"]


# ── the status object ───────────────────────────────────────────────


def test_a_bucket_upload_is_an_upload():
    assert status().status() == MirrorStatus.STATUS_UPLOAD
    assert status().status() != MirrorStatus.STATUS_DOWNLOAD


def test_it_is_its_own_tool():
    """The listing groups by this, and the speed lookup below keys off it."""
    assert status().tool == "s3"


def test_it_answers_the_methods_the_listing_asks_for():
    live = status()

    assert live.gid() == "gid"
    assert live.name() == "Big.Buck.Bunny"
    assert live.size() == get_readable_file_size(1000)
    assert live.processed_bytes() == get_readable_file_size(250)
    assert live.speed() == f"{get_readable_file_size(50)}/s"
    assert live.progress() == "25.0%"
    assert isinstance(live.eta(), str)


def test_cancel_reaches_the_uploader_that_can_stop_it():
    uploader = FakeUploader()

    assert S3UploadStatus(FakeTask(), uploader, "gid").task() is uploader


def test_an_upload_of_an_unknown_size_does_not_break_the_line():
    """``progress`` divides by the size, which a task may not have yet."""
    live = S3UploadStatus(FakeTask(), FakeUploader(), "gid")
    live.listener.size = 0

    assert live.progress() == "0%"


# ── the status page ─────────────────────────────────────────────────


async def test_an_s3_upload_reports_its_speed_to_the_status_page():
    task = SimpleNamespace(tool="s3", status=lambda: "Upload", speed=lambda: "5 MiB/s")

    assert await _get_download_status()(task) == ("Upload", "5 MiB/s")


async def test_a_tool_without_a_speed_still_renders():
    task = SimpleNamespace(tool="queue", status=lambda: "Queued", speed=None)

    assert await _get_download_status()(task) == ("Queued", "")
