"""The status each torrent task reports, state by state.

Both classes answer out of the info their own ``update()`` fetched, and both had
that answer sitting inside ``status()`` -- a method a caller cannot use without
also paying for the fetch. It moved into ``cached_status()`` so that a view
holding a batch of tasks can fetch them together and then read the answers while
it renders. The tables below are what makes the move checkable: a branch dropped
or reordered on the way across would otherwise show up only as a wrong word in a
status message.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import bot.helper.progress.aria2_status as a2
import bot.helper.progress.qbit_status as qb
from bot.helper.util.status_utils import MirrorStatus


class _QbitApi:
    """The slice of ``TorrentManager`` a qBittorrent status reads, with a count.

    ``polls`` is what tells the fetch apart from the read: ``update()`` is the
    only thing that may move it.
    """

    def __init__(self) -> None:
        self.state = "downloading"
        self.polls = 0

    async def info(self, tag=None):
        self.polls += 1
        return [SimpleNamespace(state=self.state, hash="abcdef123456")]

    def build(self, *, seeding: bool = False, queued: bool = False):
        return qb.QbittorrentStatus(
            SimpleNamespace(mid=1, name="a torrent"), seeding, queued
        )


class _Aria2Api:
    """The same for aria2, which answers with a plain dict."""

    def __init__(self) -> None:
        self.download: dict = {}
        self.polls = 0

    async def tell_status(self, gid):
        self.polls += 1
        return dict(self.download)

    def build(self, *, seeding: bool = False, queued: bool = False):
        return a2.Aria2Status(
            SimpleNamespace(mid=1, name="a download"), "g1", seeding, queued
        )


@pytest.fixture
def qbit(monkeypatch):
    api = _QbitApi()
    monkeypatch.setattr(
        qb,
        "TorrentManager",
        SimpleNamespace(
            qbittorrent=SimpleNamespace(torrents=SimpleNamespace(info=api.info))
        ),
    )
    return api


@pytest.fixture
def aria2(monkeypatch):
    api = _Aria2Api()
    monkeypatch.setattr(
        a2, "TorrentManager", SimpleNamespace(aria2_status=api.tell_status)
    )
    return api


@pytest.mark.parametrize(
    ("state", "seeding", "queued", "expected"),
    [
        ("downloading", False, False, MirrorStatus.STATUS_DOWNLOAD),
        ("queuedDL", False, False, MirrorStatus.STATUS_QUEUEDL),
        ("downloading", False, True, MirrorStatus.STATUS_QUEUEDL),
        ("queuedUP", False, False, MirrorStatus.STATUS_QUEUEUP),
        ("stoppedDL", False, False, MirrorStatus.STATUS_PAUSED),
        ("stoppedUP", False, False, MirrorStatus.STATUS_PAUSED),
        ("checkingUP", False, False, MirrorStatus.STATUS_CHECK),
        ("checkingDL", False, False, MirrorStatus.STATUS_CHECK),
        ("uploading", True, False, MirrorStatus.STATUS_SEED),
        ("stalledUP", True, False, MirrorStatus.STATUS_SEED),
        # seeding is what makes an upload a seed, not the state alone
        ("uploading", False, False, MirrorStatus.STATUS_DOWNLOAD),
        ("stalledUP", False, False, MirrorStatus.STATUS_DOWNLOAD),
        # and the queue flag outranks every state but nothing else
        ("checkingDL", False, True, MirrorStatus.STATUS_QUEUEDL),
    ],
)
async def test_the_qbit_status_table(qbit, state, seeding, queued, expected):
    qbit.state = state
    status = qbit.build(seeding=seeding, queued=queued)

    await status.update()

    assert status.cached_status() == expected


@pytest.mark.parametrize(
    ("download", "seeding", "queued", "expected"),
    [
        ({}, False, False, MirrorStatus.STATUS_DOWNLOAD),
        ({"status": "active"}, False, False, MirrorStatus.STATUS_DOWNLOAD),
        ({"status": "waiting"}, False, False, MirrorStatus.STATUS_QUEUEDL),
        ({"status": "waiting"}, True, False, MirrorStatus.STATUS_QUEUEUP),
        ({"status": "paused"}, False, False, MirrorStatus.STATUS_PAUSED),
        ({"seeder": "true"}, True, False, MirrorStatus.STATUS_SEED),
        ({"seeder": "true"}, False, False, MirrorStatus.STATUS_DOWNLOAD),
        ({"status": "active"}, False, True, MirrorStatus.STATUS_QUEUEDL),
        ({"status": "active"}, True, True, MirrorStatus.STATUS_QUEUEUP),
        # a waiting seeder is queued for upload, not for download
        ({"status": "waiting"}, True, False, MirrorStatus.STATUS_QUEUEUP),
    ],
)
async def test_the_aria2_status_table(aria2, download, seeding, queued, expected):
    aria2.download = download
    status = aria2.build(seeding=seeding, queued=queued)

    await status.update()

    assert status.cached_status() == expected


async def test_reading_the_cached_status_asks_nothing(qbit):
    """The read costs nothing, which is what makes fetching a batch possible."""
    status = qbit.build()
    await status.update()
    assert qbit.polls == 1

    assert status.cached_status() == MirrorStatus.STATUS_DOWNLOAD
    assert qbit.polls == 1


async def test_the_async_form_still_fetches_before_it_answers(qbit):
    """``status()`` keeps the contract every existing caller has with it.

    It is the read *and* the fetch, so a caller that has refreshed nothing still
    gets an answer that is true now -- rather than one from whenever the object
    happened to be built.
    """
    qbit.state = "stalledUP"

    assert await qbit.build(seeding=True).status() == MirrorStatus.STATUS_SEED
    assert qbit.polls == 1
