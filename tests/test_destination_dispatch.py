"""Tests for the table that decides what an upload destination *is*.

``_start_upload`` and ``_run_post_processing`` used to branch on the destination
directly. They now read one row out of ``_DESTINATIONS``, so this file pins the
row contents and, more importantly, the one difference that changes what lands
on disk: files are split into parts because Telegram caps message size, and a
bucket does not.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import bot.helper.listeners.task_listener as tl
from bot.helper.progress.s3_status import S3UploadStatus
from bot.helper.progress.telegram_status import TelegramStatus
from bot.helper.upload.s3_uploader import S3Uploader
from bot.helper.upload.telegram_uploader import TelegramUploader
from bot.helper.util.status_utils import MirrorStatus


class FakeTask:
    """A task with no post-processing enabled, so only the split is left."""

    _run_post_processing = tl.TaskListener._run_post_processing

    def __init__(self, destination):
        self.destination = destination
        self.name = "Big.Buck.Bunny"
        self.size = 0
        self.is_cancelled = False
        self.cleared = False
        self.splits_requested: list[str] = []
        for stage in tl._STAGES:
            for attr in stage.guard:
                setattr(self, attr, None)

    async def proceed_split(self, up_path, gid):
        self.splits_requested.append(up_path)

    def clear(self):
        self.cleared = True


@pytest.fixture(autouse=True)
def no_disk(monkeypatch):
    async def get_path_size(path):
        return 0

    monkeypatch.setattr(tl, "get_path_size", get_path_size)


# ── the rows ────────────────────────────────────────────────────────


def test_there_is_a_row_per_destination():
    assert set(tl._DESTINATIONS) == {"tg", "s3"}


def test_each_row_names_the_object_that_does_the_work():
    assert tl._DESTINATIONS["tg"].uploader is TelegramUploader
    assert tl._DESTINATIONS["s3"].uploader is S3Uploader


def test_only_the_message_size_ceiling_makes_splitting_necessary():
    assert tl._DESTINATIONS["tg"].splits is True
    assert tl._DESTINATIONS["s3"].splits is False


@pytest.mark.parametrize(
    "destination, expected",
    [("tg", TelegramStatus), ("s3", S3UploadStatus)],
)
def test_each_row_builds_the_status_line_for_its_own_uploader(destination, expected):
    listener = SimpleNamespace(name="x", size=10)
    obj = SimpleNamespace(processed_bytes=1, speed=1)

    status = tl._DESTINATIONS[destination].status(listener, obj, "gid")

    assert isinstance(status, expected)
    # both are uploads: TelegramStatus serves downloads too, and is built with
    # the direction spelled out for exactly that reason
    assert status.status() == MirrorStatus.STATUS_UPLOAD


# ── the pipeline reads them ─────────────────────────────────────────


async def test_a_telegram_task_splits_its_files():
    task = FakeTask("tg")

    await task._run_post_processing("/dir/payload", "/dir", "gid")

    assert task.splits_requested == ["/dir/payload"]
    assert task.cleared is True


async def test_a_bucket_task_splits_nothing():
    task = FakeTask("s3")

    await task._run_post_processing("/dir/payload", "/dir", "gid")

    assert task.splits_requested == []
