"""Tests for the record of what a task uploaded -- the data /copy replays.

Two halves. The unit builders in ``copy_records`` are pinned on their own:
one message becomes one ``single`` unit, one album one ``group`` unit, and
the units an album absorbs are struck rather than left to be copied twice.
The uploader half drives ``_upload_file`` the way
``test_telegram_uploader_album`` does, because the recording points live
next to the ``_uncopied`` bookkeeping: after a successful send, and when an
album retires the messages it carried.

The database half runs against a fake collection: what ``save_copy_record``
writes, what ``find_copy_records`` answers, and that the prune leaves the
newest ``MAX_TASK_RECORDS`` of one user alone.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.core.telegram_manager import TgClient
from bot.helper.ext_utils.copy_records import (
    MAX_RECORD_UNITS,
    MAX_TASK_RECORDS,
    group_unit,
    record,
    single_unit,
    strike,
)
from bot.helper.ext_utils.db_handler import DbManager


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


@pytest.fixture
def uploader_module(monkeypatch):
    """Import the Telegram uploader with its dependencies stubbed out."""
    root = Path(__file__).resolve().parent.parent

    def _passthrough(*_args, **_kwargs):
        return lambda func: func

    tg_client = SimpleNamespace(user=AsyncMock(), bot=AsyncMock())

    aiofiles_os = _stub(
        "aiofiles.os",
        remove=AsyncMock(),
        rename=AsyncMock(),
        path=SimpleNamespace(
            exists=AsyncMock(return_value=False),
            isfile=AsyncMock(return_value=False),
            getsize=AsyncMock(return_value=1),
        ),
    )

    modules = {
        "PIL": _stub("PIL", Image=SimpleNamespace(open=lambda *_a, **_k: None)),
        "natsort": _stub("natsort", natsorted=sorted),
        "aiofiles": _pkg("aiofiles"),
        "aiofiles.os": aiofiles_os,
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
        # DATABASE_URL starts empty so the default is "do not record"; the
        # tests that want recording flip it through `recording` below.
        "bot.core.config_manager": _stub(
            "bot.core.config_manager",
            Config=SimpleNamespace(DATABASE_URL=""),
        ),
        "bot.core.telegram_manager": _stub(
            "bot.core.telegram_manager",
            TgClient=tg_client,
            user_session=lambda: tg_client.user,
        ),
        "bot.helper": _pkg("bot.helper"),
        # Real path: ``copy_records`` imports nothing stubbed.
        "bot.helper.ext_utils": _pkg(
            "bot.helper.ext_utils",
            str(root / "bot" / "helper" / "ext_utils"),
        ),
        "bot.helper.ext_utils.bot_utils": _stub(
            "bot.helper.ext_utils.bot_utils", sync_to_async=AsyncMock()
        ),
        "bot.helper.ext_utils.files_utils": _stub(
            "bot.helper.ext_utils.files_utils",
            is_archive=lambda _p: False,
            get_base_name=lambda p: p,
        ),
        "bot.helper.ext_utils.media_utils": _stub(
            "bot.helper.ext_utils.media_utils",
            get_media_info=AsyncMock(return_value=(10, "artist", "title")),
            get_document_type=AsyncMock(return_value=(False, False, True)),
            get_video_thumbnail=AsyncMock(return_value=None),
            get_audio_thumbnail=AsyncMock(return_value=None),
            get_multiple_frames_thumbnail=AsyncMock(return_value=None),
        ),
        "bot.helper.ext_utils.shutil_helper": _stub(
            "bot.helper.ext_utils.shutil_helper", rmtree=AsyncMock()
        ),
        "bot.helper.telegram_helper": _pkg(
            "bot.helper.telegram_helper",
            str(root / "bot" / "helper" / "telegram_helper"),
        ),
        "bot.helper.telegram_helper.message_utils": _stub(
            "bot.helper.telegram_helper.message_utils",
            chat_of=lambda message: message.chat,
            delete_message=AsyncMock(),
        ),
        "bot.helper.mirror_leech_utils": _pkg("bot.helper.mirror_leech_utils"),
        "bot.helper.mirror_leech_utils.upload_utils": _pkg(
            "bot.helper.mirror_leech_utils.upload_utils",
            str(root / "bot" / "helper" / "mirror_leech_utils" / "upload_utils"),
        ),
    }
    modules["bot"].__path__ = []
    for name, mod in modules.items():
        monkeypatch.setitem(sys.modules, name, mod)

    pkg = "bot.helper.mirror_leech_utils.upload_utils"
    target = f"{pkg}.telegram_uploader"
    siblings = (
        f"{pkg}.flood_pacer",
        f"{pkg}.media_group_batcher",
        "bot.helper.telegram_helper.flood",
    )
    for name in (target, *siblings):
        sys.modules.pop(name, None)
    module = importlib.import_module(target)
    yield module
    for name in (target, *siblings):
        sys.modules.pop(name, None)


class FakeMessage:
    """Minimal stand-in for a pyrogram Message."""

    _next_id = 500

    def __init__(self, kind=None, caption=None, registry=None):
        FakeMessage._next_id += 1
        self.id = FakeMessage._next_id
        self.chat = SimpleNamespace(id=-1001, type=SimpleNamespace(name="CHANNEL"))
        self.caption = caption
        self.reply_to_message_id = None
        self.message_thread_id = None
        self.link = f"https://t.me/c/1001/{self.id}"
        self._registry = registry
        if registry is not None:
            registry[self.id] = self
        self.photo = (
            SimpleNamespace(file_id=f"photo{self.id}") if kind == "photo" else None
        )
        self.video = (
            SimpleNamespace(file_id=f"video{self.id}") if kind == "video" else None
        )
        self.document = (
            SimpleNamespace(file_id=f"doc{self.id}") if kind == "document" else None
        )
        self.audio = (
            SimpleNamespace(file_id=f"audio{self.id}") if kind == "audio" else None
        )


def _make_uploader(uploader_module):
    """Build an uploader whose sends land in a registry the test can read."""
    calls_by_id = {}

    async def send_photo(chat_id, reply_parameters=None, caption=None, **_kwargs):
        return FakeMessage("photo", caption=caption, registry=calls_by_id)

    async def send_document(chat_id, reply_parameters=None, caption=None, **_kwargs):
        return FakeMessage("document", caption=caption, registry=calls_by_id)

    async def send_media_group(chat_id, media, **_kwargs):
        # what kind each member becomes follows what the batcher asked telegram
        # for, so a group of split documents records back as documents
        kinds = [
            {"InputMediaDocument": "document", "InputMediaVideo": "video"}.get(
                type(m).__name__, "photo"
            )
            for m in media
        ]
        sent = [
            FakeMessage(kind, caption=m.caption)
            for kind, m in zip(kinds, media)
        ]
        for msg in sent:
            msg.media_group_id = "group1"
        return sent

    async def get_messages(chat_id, message_ids):
        return calls_by_id[message_ids]

    client = SimpleNamespace(
        send_photo=send_photo,
        send_video=send_photo,
        send_document=send_document,
        send_audio=send_photo,
        send_media_group=send_media_group,
        get_messages=get_messages,
    )
    listener = SimpleNamespace(
        thumb="none",
        user_id=1,
        name="task",
        client=client,
        is_cancelled=False,
        as_doc=False,
        hybrid_leech=False,
        user_transmission=False,
        thumbnail_layout=None,
        screen_shots=None,
        is_super_chat=True,
        up_dest=None,
        clone_dump_chats={},
        copy_preset="",
        copy_units=[],
        user_dict={},
        mid=1,
        message=None,
        on_upload_complete=AsyncMock(),
        on_upload_error=AsyncMock(),
    )
    uploader = uploader_module.TelegramUploader(listener, "/tmp/task")
    uploader._thumb = None
    uploader._sent_msg = FakeMessage(registry=calls_by_id)
    uploader._files_links = False
    uploader._batcher.enabled = True
    return uploader


def _recording(uploader_module, monkeypatch, uploader):
    """Turn recording on the way `_user_settings` would with a database."""
    monkeypatch.setattr(
        sys.modules["bot.core.config_manager"].Config, "DATABASE_URL", "mongodb://test"
    )
    uploader._record_units = True
    return uploader


def _units(uploader):
    """The units recorded so far, summarised as (mode, kind-of-each-media)."""
    units = uploader._listener.copy_units
    return [(u["mode"], tuple(m["kind"] for m in u["media"])) for u in units]


# ── the unit builders ───────────────────────────────────────────────


def test_one_message_becomes_one_single_unit():
    msg = FakeMessage("video", caption="<code>a.mkv</code>")

    unit = single_unit(msg)

    assert unit == {
        "mode": "single",
        "chat": msg.chat.id,
        "msg": msg.id,
        "media": [
            {
                "kind": "video",
                "file_id": msg.video.file_id,
                "caption": "<code>a.mkv</code>",
            }
        ],
    }


def test_a_message_without_media_records_no_unit():
    """There is nothing to copy and no file_id to fall back on."""
    assert single_unit(FakeMessage()) is None
    assert record([], None) is None


def test_one_album_becomes_one_group_unit_anchored_on_its_last_message():
    sent = [FakeMessage("photo", caption="a"), FakeMessage("video", caption="b")]

    unit = group_unit(sent)

    assert unit["mode"] == "group"
    assert unit["chat"] == sent[-1].chat.id
    assert unit["msg"] == sent[-1].id
    assert [m["kind"] for m in unit["media"]] == ["photo", "video"]
    assert [m["caption"] for m in unit["media"]] == ["a", "b"]


def test_strike_removes_only_the_singles_an_album_carried():
    units = [
        {"mode": "single", "chat": 1, "msg": 10, "media": []},
        {"mode": "group", "chat": 1, "msg": 12, "media": []},
        {"mode": "single", "chat": 2, "msg": 30, "media": []},
    ]

    strike(units, {(1, 10), (1, 11)})

    assert units == [
        {"mode": "group", "chat": 1, "msg": 12, "media": []},
        {"mode": "single", "chat": 2, "msg": 30, "media": []},
    ]


def test_units_past_the_cap_are_dropped_and_logged(caplog):
    units = [
        {"mode": "single", "chat": 1, "msg": n, "media": []}
        for n in range(MAX_RECORD_UNITS)
    ]
    one_more = {"mode": "single", "chat": 1, "msg": 999999, "media": []}

    with caplog.at_level("WARNING"):
        record(units, one_more)

    assert len(units) == MAX_RECORD_UNITS
    assert one_more not in units
    assert any("dropping" in line and "999999" in line for line in caplog.messages)


# ── the uploader records while it sends ─────────────────────────────


async def test_one_file_is_recorded_as_a_single_unit(uploader_module, monkeypatch):
    uploader = _recording(uploader_module, monkeypatch, _make_uploader(uploader_module))

    await uploader._upload_file("<code>a.jpg</code>", "a.jpg", "/tmp/a.jpg")

    assert _units(uploader) == [("single", ("photo",))]
    unit = uploader._listener.copy_units[0]
    assert unit["chat"] == uploader._sent_msg.chat.id
    assert unit["msg"] == uploader._sent_msg.id


async def test_an_album_is_recorded_as_one_group_unit(
    uploader_module, monkeypatch
):
    uploader = _recording(uploader_module, monkeypatch, _make_uploader(uploader_module))

    await uploader._upload_file("<code>a.jpg</code>", "a.jpg", "/tmp/a.jpg")
    await uploader._upload_file("<code>b.jpg</code>", "b.jpg", "/tmp/b.jpg")
    await uploader._batcher.send_album()

    # the two singles were struck when the album retired what it carried
    assert _units(uploader) == [("group", ("photo", "photo"))]
    unit = uploader._listener.copy_units[0]
    assert unit["msg"] == uploader._sent_msg.id


async def test_the_odd_file_after_an_album_keeps_its_own_unit(
    uploader_module, monkeypatch
):
    uploader = _recording(uploader_module, monkeypatch, _make_uploader(uploader_module))

    await uploader._upload_file("<code>a.jpg</code>", "a.jpg", "/tmp/a.jpg")
    await uploader._upload_file("<code>b.jpg</code>", "b.jpg", "/tmp/b.jpg")
    await uploader._batcher.send_album()
    await uploader._upload_file("<code>c.jpg</code>", "c.jpg", "/tmp/c.jpg")

    assert _units(uploader) == [("group", ("photo", "photo")), ("single", ("photo",))]


async def test_split_parts_grouped_at_the_end_become_one_group_unit(
    uploader_module, monkeypatch
):
    """Parts of one split file leave as one media group, and are recorded as
    the group they became -- not as the parts they were uploaded as."""
    uploader = _recording(uploader_module, monkeypatch, _make_uploader(uploader_module))
    media_utils = sys.modules["bot.helper.ext_utils.media_utils"]
    media_utils.get_document_type.return_value = (False, False, False)

    await uploader._upload_file("<code>m.rar</code>", "m.rar", "/tmp/m.rar", True)
    await uploader._upload_file(
        "<code>m.part1.rar</code>", "m.part1.rar", "/tmp/m.part1.rar", True
    )
    await uploader._upload_file(
        "<code>m.part2.rar</code>", "m.part2.rar", "/tmp/m.part2.rar", True
    )
    await uploader._batcher.flush("task")

    assert _units(uploader) == [
        ("single", ("document",)),
        ("group", ("document", "document")),
    ]


async def test_recording_runs_without_files_links_and_without_a_preset(
    uploader_module, monkeypatch
):
    """Unlike ``_uncopied``, the record is for every task, not just a task
    with a copy preset -- and it never depended on FILES_LINKS."""
    uploader = _recording(uploader_module, monkeypatch, _make_uploader(uploader_module))
    uploader._files_links = False
    uploader._listener.copy_preset = ""

    await uploader._upload_file("<code>a.jpg</code>", "a.jpg", "/tmp/a.jpg")

    assert _units(uploader) == [("single", ("photo",))]


async def test_nothing_is_recorded_without_a_database(uploader_module):
    uploader = _make_uploader(uploader_module)

    await uploader._upload_file("<code>a.jpg</code>", "a.jpg", "/tmp/a.jpg")

    assert uploader._listener.copy_units == []
    assert uploader._uncopied == []


async def test_units_are_recorded_in_send_order(uploader_module, monkeypatch):
    uploader = _recording(uploader_module, monkeypatch, _make_uploader(uploader_module))
    media_utils = sys.modules["bot.helper.ext_utils.media_utils"]

    await uploader._upload_file("<code>a.jpg</code>", "a.jpg", "/tmp/a.jpg")
    media_utils.get_document_type.return_value = (False, False, False)
    await uploader._upload_file("<code>b.rar</code>", "b.rar", "/tmp/b.rar", True)
    media_utils.get_document_type.return_value = (False, False, True)
    await uploader._upload_file("<code>c.jpg</code>", "c.jpg", "/tmp/c.jpg")

    assert _units(uploader) == [
        ("single", ("photo",)),
        ("single", ("document",)),
        ("single", ("photo",)),
    ]
    ids = [u["msg"] for u in uploader._listener.copy_units]
    assert ids == sorted(ids)


# ── the database round trip ─────────────────────────────────────────


class FakeCursor:
    """The slice of a pymongo cursor the copy-record methods use."""

    def __init__(self, docs):
        self._docs = docs
        self._sort = None
        self._skip = 0

    def sort(self, key, direction):
        self._sort = (key, direction)
        return self

    def skip(self, count):
        self._skip = count
        return self

    async def to_list(self, length=None):
        docs = self._docs
        if self._sort:
            key, direction = self._sort
            docs = sorted(docs, key=lambda d: d[key], reverse=direction == -1)
        return docs[self._skip :]


class FakeCopies:
    """The slice of an AsyncCollection the copy-record methods use."""

    def __init__(self, docs=()):
        self.docs = {doc["_id"]: dict(doc) for doc in docs}

    def find(self, query, projection=None):
        hits = [
            doc
            for doc in self.docs.values()
            if all(doc.get(key) == value for key, value in query.items())
        ]
        return FakeCursor(hits)

    async def update_one(self, query, update, upsert=False):
        _id = query["_id"]
        if _id in self.docs:
            self.docs[_id].update(update["$set"])
        elif upsert:
            self.docs[_id] = {"_id": _id, **update["$set"]}

    async def delete_one(self, query):
        self.docs.pop(query["_id"], None)


def _db(fake):
    """A DbManager pointed at the fake collection, as if connected."""
    dbm = DbManager()
    dbm._return = False
    dbm.db = SimpleNamespace(copies={TgClient.ID: fake})
    return dbm


async def test_a_saved_record_holds_the_task_and_its_units():
    fake = FakeCopies()
    units = [
        {"mode": "single", "chat": -1001, "msg": 7, "media": [{"kind": "photo"}]}
    ]

    await _db(fake).save_copy_record(-1001, 7, 42, "a folder", units)

    doc = fake.docs["-1001:7"]
    assert doc["cid"] == -1001
    assert doc["mid"] == 7
    assert doc["user"] == 42
    assert doc["name"] == "a folder"
    assert isinstance(doc["at"], int)
    assert doc["units"] == units


async def test_saving_again_replaces_the_record_of_the_same_task():
    fake = FakeCopies()
    dbm = _db(fake)

    await dbm.save_copy_record(-1001, 7, 42, "old", [])
    await dbm.save_copy_record(-1001, 7, 42, "new", [])

    assert list(fake.docs) == ["-1001:7"]
    assert fake.docs["-1001:7"]["name"] == "new"


async def test_find_returns_only_the_records_of_that_task_id():
    fake = FakeCopies(
        [
            {"_id": "-1001:7", "mid": 7, "user": 42, "at": 1},
            {"_id": "-1002:7", "mid": 7, "user": 43, "at": 2},
            {"_id": "-1001:8", "mid": 8, "user": 42, "at": 3},
        ]
    )

    found = await _db(fake).find_copy_records(7)

    assert sorted(doc["_id"] for doc in found) == ["-1001:7", "-1002:7"]


async def test_prune_leaves_the_newest_records_of_one_user():
    """Retention is per user: one heavy user never costs another theirs."""
    docs = [
        {"_id": f"-1001:{n}", "mid": n, "user": 42, "at": n}
        for n in range(MAX_TASK_RECORDS + 5)
    ]
    docs.append({"_id": "-1009:1", "mid": 1, "user": 43, "at": 0})
    fake = FakeCopies(docs)

    await _db(fake)._prune_copy_records(42)

    assert len([d for d in fake.docs.values() if d["user"] == 42]) == MAX_TASK_RECORDS
    # the five oldest of user 42 are gone, the newest stay, and user 43 -- whose
    # single record is older than every survivor -- keeps it
    assert "-1001:0" not in fake.docs
    assert f"-1001:{MAX_TASK_RECORDS + 4}" in fake.docs
    assert "-1009:1" in fake.docs


async def test_a_disconnected_db_does_nothing():
    dbm = DbManager()

    await dbm.save_copy_record(-1001, 7, 42, "name", [])
    assert await dbm.find_copy_records(7) == []
    await dbm._prune_copy_records(42)
