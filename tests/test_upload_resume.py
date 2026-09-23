"""What a restart has already delivered, and what reading it back saves.

Telegram keeps no record of what a task sent, so the only evidence that a file
went out is the row written next to the send, in ``uploaded_files``. Two readers
use it, and they save different things:

* the uploader, which must not send a file the user already has -- and must not
  report "No files to upload" for a task whose every file went out before the
  restart, which is what a file total of zero reads as;
* the direct downloader, for a *stream* task: ``-su`` deletes each file the
  moment it is sent, so a file that is missing from disk is not a failed
  download and must not be fetched a second time.

The uploader is loaded for real with its dependencies stubbed, the way
``test_telegram_uploader_helpers.py`` does it, and the store is replaced with
the slice of ``DbManager`` the checkpoints use.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _stub(name, **attrs):
    mod = ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


def _pkg(name, path=None):
    mod = ModuleType(name)
    mod.__path__ = [] if path is None else [path]
    return mod


class _InputMedia:
    def __init__(self, media=None, caption=None, **kwargs):
        self.media = media
        self.caption = caption


class _ReplyParameters:
    def __init__(self, message_id=None, **kwargs):
        self.message_id = message_id


class _Err(Exception):
    pass


class _Store:
    """The two checkpoint methods, recording what they were handed."""

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.written: list[tuple] = []
        self.albums: list[tuple] = []
        self.fail = False

    async def get_uploaded_files(self, mid):
        return list(self.rows)

    async def add_uploaded_file(self, mid, relpath, seq, cid, msg_id, link, label, unit):
        if self.fail:
            raise RuntimeError("the database is down")
        self.written.append((mid, relpath, seq, cid, msg_id, link, label, unit))

    async def rewrite_uploaded_album(self, mid, carried, cid, msg_id, link, unit):
        if self.fail:
            raise RuntimeError("the database is down")
        self.albums.append((mid, set(carried), cid, msg_id, link, unit))


@pytest.fixture
def store():
    return _Store()


@pytest.fixture
def uploader_module(monkeypatch, store):
    """Import the real uploader with its dependencies stubbed out.

    The store is stubbed in place of ``bot.helper.storage.db_handler``, so the
    module's ``from ..storage.db_handler import database`` binds the fake rather
    than a manager that is not connected.
    """
    root = Path(__file__).resolve().parent.parent

    def _passthrough(*_args, **_kwargs):
        return lambda func: func

    tg_client = SimpleNamespace(user=AsyncMock(), bot=AsyncMock())

    modules = {
        "PIL": _stub("PIL", Image=SimpleNamespace(open=lambda *_a, **_k: None)),
        "natsort": _stub("natsort", natsorted=sorted),
        "aiofiles": _pkg("aiofiles"),
        "aiofiles.os": _stub(
            "aiofiles.os",
            remove=AsyncMock(),
            rename=AsyncMock(),
            path=SimpleNamespace(
                exists=AsyncMock(return_value=False),
                isfile=AsyncMock(return_value=False),
                getsize=AsyncMock(return_value=1),
            ),
        ),
        "tenacity": _stub(
            "tenacity",
            retry=_passthrough,
            wait_exponential=_passthrough,
            stop_after_attempt=_passthrough,
            retry_if_exception_type=_passthrough,
            RetryError=type("RetryError", (Exception,), {}),
        ),
        "pyrogram": _pkg("pyrogram"),
        "pyrogram.errors": _stub(
            "pyrogram.errors",
            FloodWait=type("FloodWait", (_Err,), {}),
            FloodPremiumWait=type("FloodPremiumWait", (_Err,), {}),
            RPCError=type("RPCError", (_Err,), {}),
            BadRequest=type("BadRequest", (_Err,), {}),
        ),
        "pyrogram.types": _stub(
            "pyrogram.types",
            InputMediaVideo=type("InputMediaVideo", (_InputMedia,), {}),
            InputMediaDocument=type("InputMediaDocument", (_InputMedia,), {}),
            InputMediaPhoto=type("InputMediaPhoto", (_InputMedia,), {}),
            ReplyParameters=_ReplyParameters,
        ),
        "bot": _stub("bot", intervals={"stopAll": False}),
        "bot.core": _pkg("bot.core"),
        "bot.core.config_manager": _stub(
            "bot.core.config_manager",
            Config=SimpleNamespace(
                DATABASE_URL="",
                MEDIA_GROUP=False,
                LEECH_FILENAME_PREFIX="",
                FILES_LINKS=False,
            ),
        ),
        "bot.core.telegram_manager": _stub(
            "bot.core.telegram_manager",
            TgClient=tg_client,
            user_session=lambda: tg_client.user,
        ),
        "bot.helper": _pkg("bot.helper"),
        "bot.helper.util": _pkg("bot.helper.util"),
        # Real path, like the stubs above it: ``copy_records`` is loaded from
        # disk -- it imports nothing stubbed -- while ``db_handler`` beside it
        # resolves to the stub, because a sys.modules entry beats the path.
        "bot.helper.storage": _pkg(
            "bot.helper.storage", str(root / "bot" / "helper" / "storage")
        ),
        # In place of the manager: what the checkpoints reach for.
        "bot.helper.storage.db_handler": _stub(
            "bot.helper.storage.db_handler", database=store
        ),
        "bot.helper.util.bot_utils": _stub(
            "bot.helper.util.bot_utils", sync_to_async=AsyncMock()
        ),
        "bot.helper.util.files_utils": _stub(
            "bot.helper.util.files_utils",
            is_archive=lambda _p: False,
            get_base_name=lambda p: p,
        ),
        "bot.helper.util.media_utils": _stub(
            "bot.helper.util.media_utils",
            get_media_info=AsyncMock(return_value=(10, "artist", "title")),
            get_document_type=AsyncMock(return_value=(False, False, True)),
            get_video_thumbnail=AsyncMock(return_value=None),
            get_audio_thumbnail=AsyncMock(return_value=None),
            get_multiple_frames_thumbnail=AsyncMock(return_value=None),
        ),
        "bot.helper.util.shutil_helper": _stub(
            "bot.helper.util.shutil_helper", rmtree=AsyncMock()
        ),
        "bot.helper.util.task_manager": _stub(
            "bot.helper.util.task_manager", check_running_tasks=AsyncMock()
        ),
        "bot.helper.telegram": _pkg(
            "bot.helper.telegram", str(root / "bot" / "helper" / "telegram")
        ),
        "bot.helper.telegram.message_utils": _stub(
            "bot.helper.telegram.message_utils",
            chat_of=lambda message: message.chat,
            delete_message=AsyncMock(),
        ),
        "bot.helper.upload": _pkg(
            "bot.helper.upload", str(root / "bot" / "helper" / "upload")
        ),
    }
    modules["bot"].__path__ = []
    for name, mod in modules.items():
        monkeypatch.setitem(sys.modules, name, mod)

    pkg = "bot.helper.upload"
    target = f"{pkg}.telegram_uploader"
    # The uploader is loaded from disk, so it has to come out of sys.modules
    # first: a copy left behind by another test file would still hold the
    # ``database`` it bound then.
    sys.modules.pop(target, None)
    module = importlib.import_module(target)
    yield module
    sys.modules.pop(target, None)


def _listener(**overrides):
    fields = {
        "thumb": "none",
        "user_id": 1,
        "client": SimpleNamespace(),
        "is_cancelled": False,
        "as_doc": False,
        "hybrid_leech": False,
        "user_transmission": False,
        "thumbnail_layout": None,
        "screen_shots": None,
        "is_super_chat": True,
        "up_dest": None,
        "clone_dump_chats": {},
        "copy_preset": "",
        "copy_units": [],
        "user_dict": {},
        "mid": 42,
        "name": "a",
        "message": None,
        "on_upload_complete": AsyncMock(),
        "on_upload_error": AsyncMock(),
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _uploader(module, path="/tmp/task", **overrides):
    listener = _listener(**overrides)
    uploader = module.TelegramUploader(listener, path)
    uploader._thumb = None
    uploader._sent_msg = SimpleNamespace(id=7, chat=SimpleNamespace(id=-1001), link="")
    uploader._user_settings = AsyncMock()
    uploader._msg_to_reply = AsyncMock(return_value=True)
    uploader._upload_one = AsyncMock()
    uploader._finish = AsyncMock()
    return uploader


# ── the file total ────────────────────────────────────────────────────


async def test_a_task_whose_every_file_was_sent_is_not_reported_as_empty(
    uploader_module, store
) -> None:
    """The trap this pins: a total of zero means "No files to upload".

    A resumed task whose files all went out before the restart walks its
    directory and finds nothing, so without the rows counted back in it fails
    with a message saying it had nothing to do -- for a task that succeeded.
    """
    store.rows = [
        {"relpath": "a.mkv", "seq": 1, "link": "", "label": "a.mkv", "unit": None},
        {"relpath": "b.mkv", "seq": 2, "link": "", "label": "b.mkv", "unit": None},
    ]
    uploader = _uploader(uploader_module)

    await uploader._seed_from_checkpoint()

    assert uploader._total_files == 2
    assert uploader._sent_paths == {"a.mkv", "b.mkv"}
    # and the sequence carries on where the previous run stopped
    assert uploader._seq == 2


async def test_a_task_with_nothing_recorded_starts_where_it_did(
    uploader_module, store
) -> None:
    """The common case, which must not gain a row's worth of state."""
    uploader = _uploader(uploader_module)

    await uploader._seed_from_checkpoint()

    assert uploader._total_files == 0
    assert uploader._sent_paths == set()
    assert uploader._seq == 0


async def test_the_links_of_what_went_out_are_put_back_in_the_report(
    uploader_module, store
) -> None:
    """The completion message is built from the message map, and telegram is
    the only other place those messages exist -- so a task that comes back
    without them reports a different set of files than it sent."""
    store.rows = [
        {
            "relpath": "a.mkv",
            "seq": 1,
            "link": "https://t.me/c/1/10",
            "label": "a.mkv",
            "unit": None,
        }
    ]
    uploader = _uploader(uploader_module)
    uploader._files_links = True

    await uploader._seed_from_checkpoint()

    assert uploader._msgs_dict == {"https://t.me/c/1/10": "a.mkv"}


async def test_units_are_rebuilt_in_the_order_they_were_recorded(
    uploader_module, store
) -> None:
    """A copy preset reads the units back; an album that is still to come has to
    land after the ones already there."""
    store.rows = [
        {"relpath": "a.mkv", "seq": 1, "link": "", "label": "a", "unit": {"m": 1}},
        {"relpath": "b.mkv", "seq": 2, "link": "", "label": "b", "unit": {"m": 2}},
    ]
    uploader = _uploader(uploader_module)
    uploader._record_units = True

    await uploader._seed_from_checkpoint()

    assert uploader._listener.copy_units == [{"m": 1}, {"m": 2}]


# ── skipping what was already sent ────────────────────────────────────


@pytest.fixture
def tree(tmp_path, monkeypatch, uploader_module):
    """A real directory to walk, and a real ``exists`` to walk it with.

    ``sync_to_async`` normally hands the walk to ``bot_loop``, and the stubbed
    ``aiofiles.os`` answers "no" to everything -- either would leave ``upload``
    with nothing to skip or to send, which is the one thing these tests are
    about.
    """
    import os

    def _write(*names):
        for name in names:
            path = tmp_path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
        return str(tmp_path)

    async def inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    async def exists(path):
        return os.path.exists(path)

    monkeypatch.setattr(uploader_module, "sync_to_async", inline)
    monkeypatch.setattr(uploader_module.aiopath, "exists", exists)
    return _write


async def test_a_file_already_sent_is_not_sent_again(
    uploader_module, store, tree
) -> None:
    """The point of the whole checkpoint: the user gets one copy, not two."""
    store.rows = [
        {"relpath": "a.mkv", "seq": 1, "link": "", "label": "a.mkv", "unit": None}
    ]
    path = tree("a.mkv", "b.mkv")
    uploader = _uploader(uploader_module, path)

    await uploader.upload()

    sent = [call.args[0] for call in uploader._upload_one.call_args_list]
    assert sent == ["b.mkv"]


async def test_a_file_recorded_whose_deletion_failed_is_not_sent_again(
    uploader_module, store, tree
) -> None:
    """The removal after a send is cleanup, not part of it.

    A file that could not be removed is recorded as sent all the same, and the
    next run walks past it on disk -- which is exactly the case the relative
    path has to match in, so it is worth pinning that it does.
    """
    store.rows = [
        {"relpath": "album/a.mkv", "seq": 1, "link": "", "label": "a", "unit": None}
    ]
    path = tree("album/a.mkv", "album/b.mkv")
    uploader = _uploader(uploader_module, path)

    await uploader.upload()

    sent = [call.args[0] for call in uploader._upload_one.call_args_list]
    assert sent == ["b.mkv"]


async def test_a_fresh_task_sends_everything_it_finds(
    uploader_module, store, tree
) -> None:
    """Nothing recorded means nothing skipped -- the map is not a filter."""
    path = tree("a.mkv", "album/b.mkv")
    uploader = _uploader(uploader_module, path)

    await uploader.upload()

    sent = sorted(call.args[0] for call in uploader._upload_one.call_args_list)
    assert sent == ["a.mkv", "b.mkv"]


# ── writing the checkpoints ───────────────────────────────────────────


async def test_a_checkpoint_names_the_file_by_its_path_inside_the_task(
    uploader_module, store
) -> None:
    """The coordinate both readers work in: relative to the uploader's own
    directory, which is the one thing a restart rebuilds identically."""
    uploader = _uploader(uploader_module, "/d/42")
    uploader._listener.mid = 42

    await uploader._checkpoint_sent("/d/42/album/a.mkv", "a.mkv", None)

    assert store.written == [(42, "album/a.mkv", 1, -1001, 7, "", "a.mkv", None)]


async def test_a_second_checkpoint_continues_the_sequence(
    uploader_module, store
) -> None:
    """Order is what makes the report come out in the order it was sent."""
    uploader = _uploader(uploader_module, "/d/42")

    await uploader._checkpoint_sent("/d/42/a.mkv", "a", None)
    await uploader._checkpoint_sent("/d/42/b.mkv", "b", None)

    assert [row[2] for row in store.written] == [1, 2]


async def test_a_checkpoint_that_cannot_be_written_does_not_fail_the_send(
    uploader_module, store
) -> None:
    """A database that is down costs the task its resumability, not its upload."""
    store.fail = True
    uploader = _uploader(uploader_module, "/d/42")

    await uploader._checkpoint_sent("/d/42/a.mkv", "a", None)
    await uploader._checkpoint_album({(-1001, 10)}, SimpleNamespace(), None)

    assert store.written == []
    assert store.albums == []


async def test_an_album_re_points_the_files_it_absorbed(uploader_module, store) -> None:
    """The album deletes the messages it carried, so a link to one of them is a
    link to nothing -- the rows have to name the album instead."""
    uploader = _uploader(uploader_module, "/d/42")
    anchor = SimpleNamespace(id=99, chat=SimpleNamespace(id=-1001), link="")

    await uploader._checkpoint_album({(-1001, 10), (-1001, 11)}, anchor, {"m": 1})

    assert store.albums == [(42, {(-1001, 10), (-1001, 11)}, -1001, 99, "", {"m": 1})]


# ── the other reader: the direct downloader resuming a stream ─────────


class _Engine:
    """The slice of the aria2 client a resumed direct download talks to."""

    def __init__(self, live=()):
        self.live = list(live)
        self.added: list[tuple] = []
        self.gid = 0

    async def addUri(self, uris, options, position=0):
        self.gid += 1
        self.added.append((tuple(uris), dict(options)))
        return f"new{self.gid}"

    async def tellStatus(self, gid):
        return {"gid": gid, "status": "complete", "totalLength": "5"}


@pytest.fixture
def engine(monkeypatch, store):
    """Stand the engine in for the real one and hand the listener the store.

    The store is patched at ``db_handler`` rather than on the listener, because
    ``adopt_running`` reaches for it through a local import -- the module
    attribute is what that import reads.
    """
    from bot.core.torrent_manager import TorrentManager
    from bot.helper.storage import db_handler

    wired = _Engine()
    monkeypatch.setattr(TorrentManager, "aria2", wired)
    monkeypatch.setattr(db_handler, "database", store)

    async def _unfinished():
        return wired.live

    async def _remove(_download):
        return None

    monkeypatch.setattr(TorrentManager, "unfinished", _unfinished)
    monkeypatch.setattr(TorrentManager, "aria2_remove", _remove)
    return wired


def _direct_listener(path, listener):
    """The real listener, imported here rather than at module scope.

    The other half of this file loads the uploader against a stubbed ``bot``,
    and an import at collection time would bind whichever of the two happened
    to come first.
    """
    from bot.helper.listeners.direct_listener import DirectListener

    return DirectListener(path, listener, {"follow-torrent": "false"})


def _task(**overrides):
    fields = {"mid": 42, "is_cancelled": False, "stream_upload": True, "name": "n"}
    fields.update(overrides)
    return SimpleNamespace(**fields)


async def test_a_download_the_engine_is_still_running_is_not_added_again(
    engine, tmp_path
) -> None:
    """The transfer never stopped -- only the loop that was waiting on it did.

    Adding the same file a second time is what makes aria2 rename the good copy
    out of the way, so the path each live download is writing is what the
    rebuild matches on.
    """
    target = str(tmp_path / "a.mkv")
    engine.live = [{"gid": "live1", "files": [{"path": target}]}]
    listener = _direct_listener(str(tmp_path), _task())

    await listener.adopt_running()
    path = await listener._download_one(
        {"url": "http://x/a", "path": "", "filename": "a.mkv"}
    )

    assert path == target
    assert engine.added == []


async def test_a_file_already_sent_by_a_stream_is_not_fetched_again(
    engine, tmp_path, store
) -> None:
    """``-su`` deletes each file the moment it is sent.

    So a path that is missing from disk is a file the user already has, not a
    failed download -- and fetching it again sends a second copy of it.
    """
    store.rows = [
        {"relpath": "a.mkv", "seq": 1, "link": "", "label": "a", "unit": None}
    ]
    listener = _direct_listener(str(tmp_path), _task())

    await listener.adopt_running()
    path = await listener._download_one(
        {"url": "http://x/a", "path": "", "filename": "a.mkv"}
    )

    assert path == str(tmp_path / "a.mkv")
    assert engine.added == []


async def test_a_finished_file_still_on_disk_is_left_alone(engine, tmp_path) -> None:
    """The case that existed before the checkpoints, pinned against them.

    A file with no ``.aria2`` control file beside it was finished by the run
    being resumed; only one still arriving has the marker.
    """
    (tmp_path / "a.mkv").write_bytes(b"whole")
    listener = _direct_listener(str(tmp_path), _task())

    await listener.adopt_running()
    path = await listener._download_one(
        {"url": "http://x/a", "path": "", "filename": "a.mkv"}
    )

    assert path == str(tmp_path / "a.mkv")
    assert engine.added == []


async def test_a_fresh_run_fetches_over_what_is_on_disk(engine, tmp_path) -> None:
    """The skips belong to resume mode only.

    Asking for the same album twice is a request for the files, not for a
    directory listing, so a fresh run has to fetch what it finds there.
    """
    (tmp_path / "a.mkv").write_bytes(b"stale")
    listener = _direct_listener(str(tmp_path), _task())

    path = await listener._download_one(
        {"url": "http://x/a", "path": "", "filename": "a.mkv"}
    )

    assert path == str(tmp_path / "a.mkv")
    assert [added[0] for added in engine.added] == [("http://x/a",)]
