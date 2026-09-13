"""Looking a task up by gid must not cost a round trip per task in the way.

``task_dict`` is keyed by ``listener.mid``, but every listener callback and every
button that names a task names it by gid, so ``get_task_by_gid`` walks the dict.
It used to refresh every torrent it walked past before comparing gids -- one
``torrents.info`` or ``tellStatus`` call per torrent in the dict, to find the one
the lookup was actually about. The qBittorrent listener makes such a lookup once
per event it sees, and the dict is global, so every torrent of every user was
asked about to find the one the event concerned.

What is pinned here is which lookups still have to ask. A qBittorrent task's gid
is its hash, which its server assigns once and never reassigns, so comparing the
hash settles it. An aria2 task's gid can move -- ``update()`` follows a
``followedBy`` to the gid the download was handed off to -- so instead of being
answered from a value that may be stale, that one is asked, exactly as every task
used to be. The last two tests pin that faithful-to-before behaviour, including
the gid such a task leaves behind.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.helper.util import status_utils as su
from bot.helper.util.status_utils import MirrorStatus


class _PlainTask:
    """A status that holds its gid from construction and is never asked."""

    def __init__(self, gid: str, mid: int, user_id: int = 1) -> None:
        self._gid = gid
        self.listener = SimpleNamespace(mid=mid, user_id=user_id)

    def gid(self) -> str:
        return self._gid

    def status(self) -> str:
        return MirrorStatus.STATUS_DOWNLOAD


class _QbitTask:
    """A qBittorrent status: its hash, settled once it has any info at all.

    ``refreshed=False`` is the window the real object spends between being put in
    ``task_dict`` and its first successful ``update()``, where there is no hash to
    compare yet -- the one case a qBittorrent lookup still has to ask about.

    ``seeding`` is the real constructor's attribute, and the fakes carry it
    because the implementation this replaced picked the tasks it refreshed out
    with ``hasattr(tk, "seeding")``. A fake without it would be invisible to that
    code, and the comparison these tests exist for would prove nothing.
    """

    def __init__(self, gid: str, mid: int, *, refreshed: bool = False) -> None:
        self._gid = gid
        self._info = True if refreshed else None
        self.seeding = False
        self.listener = SimpleNamespace(mid=mid, user_id=1)
        self.updates = 0

    async def update(self) -> None:
        self.updates += 1
        self._info = True

    def gid(self) -> str:
        return self._gid

    def known_gid(self):
        return self._gid if self._info is not None else None

    def cached_status(self) -> str:
        return MirrorStatus.STATUS_DOWNLOAD

    async def status(self) -> str:
        await self.update()
        return self.cached_status()


class _Aria2Task:
    """An aria2 status: its gid can be handed off, so it is never settled.

    ``followed_by`` is that hand-off. It is only visible after a refresh, which
    is the whole reason this kind of task cannot be matched from the value it
    happens to be holding.
    """

    def __init__(self, gid: str, mid: int, followed_by: str | None = None) -> None:
        self._gid = gid
        self._followed_by = followed_by
        self.seeding = False
        self.listener = SimpleNamespace(mid=mid, user_id=1)
        self.updates = 0

    async def update(self) -> None:
        self.updates += 1
        if self._followed_by:
            self._gid, self._followed_by = self._followed_by, None

    def gid(self) -> str:
        return self._gid

    def known_gid(self):
        return None

    def cached_status(self) -> str:
        return MirrorStatus.STATUS_DOWNLOAD

    async def status(self) -> str:
        await self.update()
        return self.cached_status()


@pytest.fixture
def tasks(monkeypatch):
    """Point the lookup at a dict built from this test's tasks, in the order given."""

    def wire(*entries):
        monkeypatch.setattr(
            su, "task_dict", {entry.listener.mid: entry for entry in entries}
        )
        return entries

    return wire


async def test_a_settled_gid_costs_no_round_trip(tasks):
    """The listener's common case: the hash is already in hand, so nothing is asked.

    Two torrents, the second wanted. Finding it used to be a ``torrents.info``
    call for the first one as well.
    """
    first = _QbitTask("aaaa", 1, refreshed=True)
    wanted = _QbitTask("bbbb", 2, refreshed=True)
    tasks(first, wanted)

    assert await su.get_task_by_gid("bbbb") is wanted
    assert (first.updates, wanted.updates) == (0, 0)


async def test_a_torrent_with_no_info_yet_is_asked_once(tasks):
    """The other side of the same rule: nothing to compare means asking."""
    task = _QbitTask("aaaa", 1)
    tasks(task)

    assert await su.get_task_by_gid("aaaa") is task
    assert task.updates == 1


async def test_a_plain_task_is_matched_without_asking_the_torrents(tasks):
    """Every other tool holds its gid outright, and the torrents are not touched."""
    plain = _PlainTask("gid-1", 1)
    torrent = _QbitTask("aaaa", 2, refreshed=True)
    tasks(plain, torrent)

    assert await su.get_task_by_gid("gid-1") is plain
    assert torrent.updates == 0


async def test_a_task_that_must_be_asked_is_found_behind_a_settled_one(tasks):
    """Both passes keep the dict's order, so position decides nothing."""
    settled = _QbitTask("aaaa", 1, refreshed=True)
    asked = _Aria2Task("g1", 2)
    tasks(settled, asked)

    assert await su.get_task_by_gid("g1") is asked
    assert (settled.updates, asked.updates) == (0, 1)


async def test_the_gid_an_aria2_task_moved_to_is_found(tasks):
    """The hand-off, as it behaved before: only the refresh reveals the new gid."""
    task = _Aria2Task("g1", 1, followed_by="g2")
    tasks(task)

    assert await su.get_task_by_gid("g2") is task
    assert task.updates == 1
    assert task.gid() == "g2"


async def test_the_gid_an_aria2_task_left_behind_stops_matching(tasks):
    """The hazard this design exists to avoid.

    The task holds ``g1`` and aria2 has moved it to ``g2``. Answering from the
    value in hand would hand back a task the old implementation refused, so a
    task whose gid can move is never settled by comparison -- it is refreshed,
    and then it answers ``g2``, which is what it did before.
    """
    task = _Aria2Task("g1", 1, followed_by="g2")
    tasks(task)

    assert await su.get_task_by_gid("g1") is None
    assert task.updates == 1


async def test_an_unknown_gid_asks_only_what_has_to_be_asked(tasks):
    """A miss cannot grow the work past the tasks that never settle."""
    first = _Aria2Task("g1", 1)
    second = _Aria2Task("g2", 2)
    settled = _QbitTask("aaaa", 3, refreshed=True)
    tasks(first, second, settled)

    assert await su.get_task_by_gid("nope") is None
    assert (first.updates, second.updates) == (1, 1)
    # its gid is settled, so there is nothing a refresh could tell us
    assert settled.updates == 0
