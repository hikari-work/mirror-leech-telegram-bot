"""A status view must ask each task once, and ask the page in flight.

Two views of the same tasks: ``get_specific_tasks``, which is what a filtered
``/status`` or ``/cancel`` matches against, and ``get_readable_message``, which
renders one page of them. Both read every task's status through the coroutine
that fetches it, and the "All" view did it inside the loop that renders each line
-- one round trip after another, on a page the user is waiting for.

The refresh and the read are two halves of one protocol now: ``update()`` asks a
tool for its info, ``cached_status()`` answers from what that fetch left behind.
That is what lets a caller ask a batch together and then read the answers while
it renders. What these tests count is the asking.
"""

from __future__ import annotations

from asyncio import sleep
from types import SimpleNamespace

import pytest

from bot.helper.util import status_utils as su
from bot.helper.util.status_utils import MirrorStatus

SID = 1234


class _Flight:
    """How many fetches were ever in flight at once.

    The difference between a batch asked together and the same batch asked one
    at a time. Each fetch yields to the loop before it finishes, so a gather lets
    every one of them enter first and a sequential loop cannot.
    """

    def __init__(self) -> None:
        self.in_flight = 0
        self.peak = 0

    async def hop(self) -> None:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        await sleep(0)
        self.in_flight -= 1


class _Task:
    """A torrent status whose fetch and answer are counted apart.

    ``update()`` is the round trip and ``cached_status()`` the answer it leaves
    behind; ``status()`` is both at once, as it is on the real classes, so a
    caller that falls back to it shows up as a second fetch rather than as a
    silent extra wait. The listener is inert enough to render -- ``progress`` is
    off so the line is a name, a size and a gid.
    """

    def __init__(
        self, gid: str, status: str, mid: int, user_id: int = 1, flight=None
    ) -> None:
        self._gid = gid
        self._status = status
        self._flight = flight
        self.seeding = False
        self.listener = SimpleNamespace(
            mid=mid,
            user_id=user_id,
            is_super_chat=False,
            subname=None,
            progress=False,
        )
        self.updates = 0

    async def update(self) -> None:
        self.updates += 1
        if self._flight is not None:
            await self._flight.hop()

    def cached_status(self) -> str:
        return self._status

    async def status(self) -> str:
        await self.update()
        return self.cached_status()

    def gid(self) -> str:
        return self._gid

    def name(self) -> str:
        return f"name-{self._gid}"

    def size(self) -> str:
        return "1.00KB"


class _PlainTask:
    """A status with nothing to fetch: it knows what it is without being asked."""

    def __init__(self, gid: str, status: str, mid: int, user_id: int = 1) -> None:
        self._gid = gid
        self._status = status
        self.listener = SimpleNamespace(
            mid=mid,
            user_id=user_id,
            is_super_chat=False,
            subname=None,
            progress=False,
        )

    def gid(self) -> str:
        return self._gid

    def status(self) -> str:
        return self._status

    def name(self) -> str:
        return f"name-{self._gid}"

    def size(self) -> str:
        return "1.00KB"


@pytest.fixture
def flight() -> _Flight:
    return _Flight()


@pytest.fixture
def view(monkeypatch, tmp_path):
    """Point the view at this test's tasks and at a download dir that exists.

    ``get_readable_message`` reports free space on ``DOWNLOAD_DIR``, which is the
    container's ``/app/downloads`` by default and is not here.
    """
    monkeypatch.setattr(su, "DOWNLOAD_DIR", str(tmp_path))

    def wire(*entries):
        monkeypatch.setattr(
            su, "task_dict", {entry.listener.mid: entry for entry in entries}
        )
        return entries

    return wire


async def test_classifying_asks_every_task_once_altogether(view, flight):
    """One fetch each, and the batch does not wait for itself."""
    download = _Task("a", MirrorStatus.STATUS_DOWNLOAD, 1, flight=flight)
    upload = _Task("b", MirrorStatus.STATUS_UPLOAD, 2, flight=flight)
    view(download, upload)

    assert await su.get_specific_tasks(MirrorStatus.STATUS_DOWNLOAD, None) == [download]
    assert (download.updates, upload.updates) == (1, 1)
    assert flight.peak == 2


async def test_only_the_users_tasks_are_asked(view):
    """The filter is applied before the fetching, not after it."""
    mine = _Task("a", MirrorStatus.STATUS_DOWNLOAD, 1, user_id=7)
    theirs = _Task("b", MirrorStatus.STATUS_DOWNLOAD, 2, user_id=8)
    view(mine, theirs)

    assert await su.get_specific_tasks(MirrorStatus.STATUS_DOWNLOAD, 7) == [mine]
    assert theirs.updates == 0


async def test_a_task_with_nothing_to_fetch_is_read_directly(view):
    """No ``update`` means no round trip, and the filter still sees it."""
    plain = _PlainTask("a", MirrorStatus.STATUS_PAUSED, 1)
    torrent = _Task("b", MirrorStatus.STATUS_PAUSED, 2)
    view(plain, torrent)

    assert await su.get_specific_tasks(MirrorStatus.STATUS_PAUSED, None) == [
        plain,
        torrent,
    ]
    assert torrent.updates == 1


async def test_an_unrecognised_status_is_still_a_download(view):
    """The catch-all the loop carries: anything off the list counts as one."""
    unknown = _PlainTask("a", "SomeNewState", 1)
    view(unknown)

    assert await su.get_specific_tasks(MirrorStatus.STATUS_DOWNLOAD, None) == [unknown]


async def test_rendering_all_fetches_the_page_in_flight(view, flight):
    """The "All" page used to be fetched one task at a time, in the render loop.

    Both tasks are on the page, so both have to be fetched -- the change is that
    neither waits for the other's round trip to come back first.
    """
    first = _Task("a", MirrorStatus.STATUS_UPLOAD, 1, flight=flight)
    second = _Task("b", MirrorStatus.STATUS_DOWNLOAD, 2, flight=flight)
    view(first, second)

    msg, _ = await su.get_readable_message(SID, False, status="All")

    assert (first.updates, second.updates) == (1, 1)
    assert flight.peak == 2
    assert "1.Upload" in msg
    assert "2.Download" in msg


async def test_rendering_all_reads_the_status_the_fetch_left_behind(view):
    """One fetch per task, not one to classify and another to print."""
    task = _Task("a", MirrorStatus.STATUS_CHECK, 1)
    view(task)

    msg, _ = await su.get_readable_message(SID, False, status="All")

    assert task.updates == 1
    assert "1.CheckUp" in msg


async def test_rendering_a_filtered_page_asks_nothing_extra(view, flight):
    """The filter already fetched these; the render prints what it fetched."""
    showed = _Task("a", MirrorStatus.STATUS_DOWNLOAD, 1, flight=flight)
    hidden = _Task("b", MirrorStatus.STATUS_UPLOAD, 2, flight=flight)
    view(showed, hidden)

    msg, _ = await su.get_readable_message(
        SID, False, status=MirrorStatus.STATUS_DOWNLOAD
    )

    assert (showed.updates, hidden.updates) == (1, 1)
    assert "1.Download" in msg
    assert "name-b" not in msg


async def test_an_empty_all_view_asks_nobody(view, flight):
    """No tasks, no fetches, and the caller is told there is nothing to show."""
    view()

    assert await su.get_readable_message(SID, False, status="All") == (None, None)
    assert flight.peak == 0
