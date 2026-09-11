"""Waiting for a magnet's metadata must not hammer the qBittorrent API.

A magnet link has no file list until qBittorrent has fetched the metadata, and
`/select` cannot be offered before then, so the bot polls. The poll used to have
no delay at all: it called the API again the instant the previous call returned,
for as long as the metadata took -- minutes on a cold swarm. qBittorrent serves
its WebUI from a single thread, so that is not a slow loop, it is the whole API
being held under a stream of requests.

What is pinned here is the gap. The wait is its own function so that it can be
driven directly, without standing up the rest of ``add_qb_torrent``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import bot.helper.download.qbit_download as qbd


class _Info:
    """``torrents.info`` answering a scripted run of states, then one final one.

    ``empty`` makes it report no torrent at all, which is what qBittorrent
    answers with once the tag has been removed or the torrent deleted.
    """

    def __init__(self, states, *, empty=False):
        self._states = list(states)
        self._empty = empty
        self.polls = 0

    async def __call__(self, tag=None):
        self.polls += 1
        if self._empty:
            return []
        state = self._states.pop(0) if self._states else "downloading"
        return [SimpleNamespace(state=state, hash=f"hash-{self.polls}")]


@pytest.fixture
def qbit(monkeypatch):
    """The poll, with the API and the placeholder message replaced."""
    slept: list[float] = []
    deleted: list = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    async def fake_delete(message):
        deleted.append(message)

    monkeypatch.setattr(qbd, "sleep", fake_sleep)
    monkeypatch.setattr(qbd, "delete_message", fake_delete)

    def wire(states, *, empty=False):
        info = _Info(states, empty=empty)
        monkeypatch.setattr(
            qbd,
            "TorrentManager",
            SimpleNamespace(
                qbittorrent=SimpleNamespace(torrents=SimpleNamespace(info=info))
            ),
        )
        return info

    return SimpleNamespace(wire=wire, slept=slept, deleted=deleted)


async def test_the_metadata_poll_waits_between_calls(qbit):
    """Two calls with the metadata still coming means one wait, not none."""
    info = qbit.wire(["metaDL", "metaDL", "downloading"])
    listener = SimpleNamespace(mid=10032)

    torrent = await qbd._wait_for_metadata(listener, "meta")

    assert torrent.hash == "hash-3"
    assert info.polls == 3
    # one gap per pair of consecutive polls
    assert qbit.slept == [1, 1]


async def test_a_torrent_that_never_arrives_stops_polling(qbit):
    """An empty answer is the caller's signal to give up, placeholder and all."""
    info = qbit.wire([], empty=True)
    listener = SimpleNamespace(mid=10032)

    assert await qbd._wait_for_metadata(listener, "meta") is None
    assert info.polls == 1
    assert qbit.deleted == ["meta"]
    # giving up costs no wait -- there is nothing left to poll for
    assert qbit.slept == []
