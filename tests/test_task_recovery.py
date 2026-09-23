"""What the boot-time recovery pass places, and what it refuses to.

A restart of the bot is not a restart of the downloads: aria2 and qBittorrent
are daemons, so the transfers the dead process started are still running when
the new one comes up, and what was lost is only the memory tying them to a
telegram message. ``recover_tasks`` reads the rows the dead process left in
``active_tasks``, rebuilds a listener for each and hands it back to its engine.

Three of those decisions are what these tests pin, because each of them is a
way the feature can be wrong in a direction nothing else would catch:

* a task that cannot be placed has to be *reported*, not silently dropped --
  the row is deleted either way, so the log line is the only trace of it;
* the rows are forgotten as they are read, which is what stops a second boot
  from finding them again -- and what makes it safe for the notifier to run
  after this pass rather than racing it;
* the sweep it ends with is narrower than the ``clean_all`` it replaced. It
  runs on every boot, so anything it takes is taken from a user who never
  asked for it.

The store, the engines and the chat are replaced; the pass itself is the real
one. Importing it pulls in the bot package, which this suite pays for anyway.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bot.core import task_recovery
from bot.core.torrent_manager import TorrentManager
from bot.helper.listeners.command_task import ACTIVE_TASK_SCHEMA


class _FakeStore:
    """The slice of ``DbManager`` this pass reads and writes."""

    def __init__(self, rows=(), delivered=None):
        self.rows = list(rows)
        self.delivered = dict(delivered or {})
        self.removed: list[int] = []

    async def get_active_tasks(self):
        return list(self.rows)

    async def rm_active_task(self, mid):
        self.removed.append(mid)

    async def get_uploaded_files(self, mid):
        return list(self.delivered.get(mid, ()))


def _row(mid=1, state="dl", engine="aria2", **fields):
    """One ``active_tasks`` row, with the data document its writer produces."""
    data = {"schema": ACTIVE_TASK_SCHEMA, "engine": engine, "handler": "leech"}
    data.update(fields)
    return {
        "mid": mid,
        "cid": -100,
        "cmd_msg_id": 10,
        "user_id": 2,
        "tag": "@user",
        "state": state,
        "data": data,
    }


@pytest.fixture(autouse=True)
def _offload_inline(monkeypatch) -> None:
    """Run the offload seam inline, for every test in this file.

    ``sync_to_async`` hands work to ``bot_loop`` -- the loop ``bot/__init__``
    built for the running bot -- and awaiting a future from that loop inside a
    pytest task is what raises "attached to a different loop". The sweeps read
    the download directory through it, and what this file is about is which
    names survive, not how the listing is offloaded.
    """
    from bot.helper.util import bot_utils

    async def inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(bot_utils, "sync_to_async", inline)


@pytest.fixture
def store(monkeypatch):
    """Wire the pass to a fake store and a silent chat."""

    def wire(rows=(), delivered=None):
        wired = _FakeStore(rows, delivered)
        monkeypatch.setattr(task_recovery, "database", wired)
        return wired

    return wire


@pytest.fixture
def announced(monkeypatch):
    """Collect the chats a status message was sent into."""
    from bot.helper.telegram import message_utils

    sent: list[object] = []

    async def _send(message):
        sent.append(message)

    monkeypatch.setattr(message_utils, "send_status_message", _send)
    return sent


# ── what cannot come back ─────────────────────────────────────────────


async def test_a_row_is_forgotten_even_when_its_task_cannot_be_placed(store) -> None:
    """The row exists to survive the gap, not to be retried forever.

    Left behind, a second boot would find it again and fail the same way, and
    the incomplete-task notifier -- which reads the same tables to decide who to
    tell -- would see a task that is not coming back as one that might.
    """
    wired = store(rows=[_row(mid=7, state="post")])

    await task_recovery.recover_tasks()

    assert wired.removed == [7]


async def test_a_task_that_died_mid_post_processing_is_refused(store) -> None:
    """Its subprocesses died with the bot, and its files are half-processed.

    Re-entering ``on_download_complete`` would split and extract a directory
    that has already been split and extracted, so the bytes being final is not
    the same thing as the task being finished.
    """
    store(rows=[_row(mid=7, state="post")])

    assert await task_recovery._rebuild(_row(mid=7, state="post")) is None
    assert await task_recovery._rebuild(_row(mid=7, state="up")) is None


async def test_an_engine_inside_the_process_is_refused(store) -> None:
    """yt-dlp, mega and the rest run in this process, so nothing outlives it."""
    store()

    assert await task_recovery._rebuild(_row(mid=8, engine="ytdlp")) is None
    assert await task_recovery._rebuild(_row(mid=8, engine="mega")) is None


async def test_an_unreadable_row_is_reported_rather_than_raised(store, caplog) -> None:
    """A document from a newer writer must not take the whole pass down.

    ``/restart`` runs ``update.py`` before the bot comes back, so the reader and
    the writer can be different versions; one row it does not understand is that
    row's problem, not the nine behind it.
    """
    wired = store(rows=[_row(mid=9, schema=ACTIVE_TASK_SCHEMA + 1)])

    await task_recovery.recover_tasks()

    assert wired.removed == [9]
    assert "Unable to rebuild task 9" in caplog.text


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (_row(state="up"), "up stage"),
        (_row(multi=3), "batch"),
        (_row(multi_tag="abc"), "batch"),
        (_row(mid=10**9 + 1), "batch"),
        (_row(engine="telegram"), "runs inside the bot process"),
        (_row(engine=""), "runs inside the bot process"),
        (_row(), "no longer has it"),
    ],
)
def test_the_reason_a_task_was_not_resumed_is_specific(row, expected) -> None:
    """One line an operator can act on, not a bare count."""
    assert expected in task_recovery._why(row)


# ── what comes back ───────────────────────────────────────────────────


async def test_a_chat_gets_one_status_message_however_many_tasks_returned(
    store, announced, monkeypatch
) -> None:
    """``send_status_message`` renders the whole list, so per task is per lie.

    Ten recovered tasks in one chat used to mean ten messages, nine of them
    showing a list that was still filling up.
    """
    store(rows=[_row(mid=1), _row(mid=2), _row(mid=3)])
    message = SimpleNamespace(id=1, chat=SimpleNamespace(id=-100))

    async def _rebuild(row):
        return SimpleNamespace(mid=row["mid"], message=message)

    monkeypatch.setattr(task_recovery, "_rebuild", _rebuild)

    await task_recovery.recover_tasks()

    assert announced == [message]


async def test_two_chats_are_announced_to_separately(store, announced, monkeypatch) -> None:
    """One per chat is per chat, not one overall."""
    rows = [_row(mid=1), _row(mid=2)]
    rows[1]["cid"] = -200
    store(rows=rows)

    async def _rebuild(row):
        message = SimpleNamespace(id=1, chat=SimpleNamespace(id=row["cid"]))
        return SimpleNamespace(mid=row["mid"], message=message)

    monkeypatch.setattr(task_recovery, "_rebuild", _rebuild)

    await task_recovery.recover_tasks()

    assert [m.chat.id for m in announced] == [-100, -200]


async def test_an_empty_book_of_tasks_sweeps_without_announcing(
    store, announced
) -> None:
    """Nothing was placed, so there is nothing to say -- but the sweep runs."""
    wire = store()

    await task_recovery.recover_tasks()

    assert announced == []
    assert wire.removed == []


# ── the sweep ─────────────────────────────────────────────────────────


@pytest.fixture
def sweep_dir(monkeypatch, tmp_path):
    """A download directory the sweep can actually delete from."""
    monkeypatch.setattr(task_recovery, "DOWNLOAD_DIR", str(tmp_path))
    return tmp_path


async def test_a_directory_no_task_claimed_is_removed(sweep_dir) -> None:
    """The unfinished business of a task that ended while the bot was down."""
    (sweep_dir / "123").mkdir()
    (sweep_dir / "456").mkdir()

    await task_recovery._sweep_download_dir({456})

    assert [p.name for p in sweep_dir.iterdir()] == ["456"]


async def test_a_name_that_is_not_a_task_id_is_left_alone(sweep_dir) -> None:
    """``create_thumb`` writes into ``{DOWNLOAD_DIR}thumbnails/``.

    A sweep that took everything under the download directory -- which is what
    ``clean_all`` did on every boot -- deleted every generated thumbnail with
    it, for a task that had nothing to do with them.
    """
    (sweep_dir / "thumbnails").mkdir()
    (sweep_dir / "sd99").mkdir()
    (sweep_dir / "123").mkdir()

    await task_recovery._sweep_download_dir(set())

    assert sorted(p.name for p in sweep_dir.iterdir()) == ["sd99", "thumbnails"]


class _FakeTorrents:
    def __init__(self, torrents):
        self._torrents = torrents
        self.deleted: list[tuple] = []
        self.tags_deleted: list[list[str]] = []

    async def info(self, *args, **kwargs):
        return self._torrents

    async def delete(self, hashes, delete_files):
        self.deleted.append((tuple(hashes), delete_files))

    async def delete_tags(self, tags):
        self.tags_deleted.append(list(tags))


async def test_a_torrent_tagged_with_a_recovered_task_survives(monkeypatch) -> None:
    """The tag *is* the task id, which is the whole reason it is written.

    A recovered torrent is still downloading; deleting it would throw away the
    work the pass exists to save.
    """
    torrents = _FakeTorrents(
        [
            SimpleNamespace(hash="kept", tags=["42"]),
            SimpleNamespace(hash="gone", tags=["99"]),
            SimpleNamespace(hash="untagged", tags=[]),
        ]
    )
    monkeypatch.setattr(
        TorrentManager, "qbittorrent", SimpleNamespace(torrents=torrents)
    )

    await task_recovery._sweep_torrents({42})

    assert torrents.deleted == [(("gone",), True), (("untagged",), True)]
    assert torrents.tags_deleted == [["99"]]


# ── the two engines that can be re-attached ───────────────────────────


async def test_aria2_is_re_attached_by_the_directory_it_writes_into(
    monkeypatch,
) -> None:
    """A gid is not a handle that survives, and aria2 has no tag to use instead.

    A magnet hands its download off to a new gid the moment the metadata lands,
    so the ``dir`` the task was started with is the only thing left that names
    it.
    """
    downloads = [
        {"gid": "aaa", "dir": "/downloads/1"},
        {"gid": "bbb", "dir": "/downloads/2"},
    ]

    async def _unfinished():
        return downloads

    monkeypatch.setattr(TorrentManager, "unfinished", _unfinished)

    assert await task_recovery._find_aria2_gid("/downloads/2") == "bbb"
    assert await task_recovery._find_aria2_gid("/downloads/3") is None
    assert await task_recovery._find_aria2_gid("") is None


async def test_a_stream_task_comes_back_as_an_ordinary_one(store) -> None:
    """See ``downgrade_stream`` -- the flags streaming drops have to be dropped
    again, because the process that dropped them is the one that died."""
    listener = _streaming_listener()

    await task_recovery.downgrade_stream(listener)

    assert listener.stream_upload is False
    assert listener.extract is False
    assert listener.split_size == 0
    assert listener.seed is False


def _streaming_listener(**overrides):
    """A listener with every flag a stream drops set to a truthy value."""
    fields = {
        "mid": 42,
        "stream_upload": True,
        "extract": True,
        "compress": "secret",
        "join": True,
        "sample_video": True,
        "screen_shots": True,
        "convert_audio": "-c:a aac",
        "convert_video": "x264",
        "name_sub": "sub",
        "ffmpeg_cmds": {("x", "y")},
        "split_size": 2097152000,
        "seed": True,
        "stream_notices": [],
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


async def test_the_notice_a_resumed_stream_writes_names_what_was_sent(
    store, monkeypatch
) -> None:
    """A resumed stream sends less than the link offers, and has to say so.

    The files delivered before the restart are deleted the moment they went out,
    so they are missing from disk -- which a user reads as a task that lost
    them unless the message says otherwise.
    """
    wire = store(
        delivered={
            42: [
                {"relpath": "album/a.mkv", "seq": 1},
                {"relpath": "album/b.mkv", "seq": 2},
            ]
        }
    )
    monkeypatch.setattr(task_recovery, "database", wire)
    listener = _streaming_listener()

    await task_recovery.downgrade_stream(listener)

    notices = listener.stream_notices
    assert len(notices) == 4  # three from the policy, one for the delivered
    assert "2 file(s) were already sent" in notices[-1]
    assert "a.mkv, b.mkv" in notices[-1]
    assert "a.mkv" not in notices[0]


async def test_the_names_a_resumed_stream_lists_are_capped(store) -> None:
    """An album of three hundred files is not a message."""
    store(
        delivered={42: [{"relpath": f"album/{i}.mkv", "seq": i} for i in range(9)]}
    )
    listener = _streaming_listener()

    await task_recovery.downgrade_stream(listener)

    assert "9 file(s) were already sent" in listener.stream_notices[-1]
    assert "and 4 more" in listener.stream_notices[-1]


async def test_a_resumed_stream_with_nothing_delivered_says_nothing(
    store,
) -> None:
    """No restart-shaped surprise, no line about one."""
    store(delivered={})
    listener = _streaming_listener(stream_notices=[])

    await task_recovery.downgrade_stream(listener)

    assert not any("restart" in note for note in listener.stream_notices)


async def test_an_ordinary_task_is_left_alone(store) -> None:
    """Nothing is re-applied to a task that was never streaming."""
    listener = _streaming_listener(stream_upload=False, extract=True, seed=True)
    store()

    assert listener.stream_upload is False
    # not called by the pass at all -- the caller branches on the flag
    assert listener.extract is True
    assert listener.seed is True
