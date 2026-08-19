"""Tests for album batching in the Telegram uploader."""

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


@pytest.fixture
def uploader_module(monkeypatch):
    """Import the Telegram uploader with its dependencies stubbed out."""
    root = Path(__file__).resolve().parent.parent

    def _passthrough(*_args, **_kwargs):
        return lambda func: func

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
        "aioshutil": _stub("aioshutil", rmtree=AsyncMock()),
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
        "bot.core.config_manager": _stub("bot.core.config_manager", Config=object()),
        "bot.core.telegram_manager": _stub(
            "bot.core.telegram_manager",
            TgClient=SimpleNamespace(user=AsyncMock(), bot=AsyncMock()),
        ),
        "bot.helper": _pkg("bot.helper"),
        "bot.helper.ext_utils": _pkg("bot.helper.ext_utils"),
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
        "bot.helper.telegram_helper": _pkg("bot.helper.telegram_helper"),
        "bot.helper.telegram_helper.message_utils": _stub(
            "bot.helper.telegram_helper.message_utils", delete_message=AsyncMock()
        ),
        "bot.helper.mirror_leech_utils": _pkg("bot.helper.mirror_leech_utils"),
        "bot.helper.mirror_leech_utils.upload_utils": _pkg(
            "bot.helper.mirror_leech_utils.upload_utils",
            str(root / "bot" / "helper" / "mirror_leech_utils" / "upload_utils"),
        ),
    }
    # bot.__path__ has to allow the stubbed submodules above to resolve.
    modules["bot"].__path__ = []
    for name, mod in modules.items():
        monkeypatch.setitem(sys.modules, name, mod)

    target = "bot.helper.mirror_leech_utils.upload_utils.telegram_uploader"
    sys.modules.pop(target, None)
    module = importlib.import_module(target)
    yield module
    sys.modules.pop(target, None)


class FakeMessage:
    """Minimal stand-in for a pyrogram Message."""

    _next_id = 100

    def __init__(self, kind=None, caption=None, reply_to_message_id=None, registry=None):
        FakeMessage._next_id += 1
        self.id = FakeMessage._next_id
        self.chat = SimpleNamespace(id=-1001, type=SimpleNamespace(name="CHANNEL"))
        self.caption = caption
        self.reply_to_message_id = reply_to_message_id
        self.message_thread_id = None
        self.link = f"https://t.me/c/1001/{self.id}"
        self.media_group_id = None
        self._registry = registry
        if registry is not None:
            registry[self.id] = self
        self.photo = SimpleNamespace(file_id=f"photo{self.id}") if kind == "photo" else None
        self.video = SimpleNamespace(file_id=f"video{self.id}") if kind == "video" else None
        self.document = (
            SimpleNamespace(file_id=f"doc{self.id}") if kind == "document" else None
        )
        self.audio = SimpleNamespace(file_id=f"audio{self.id}") if kind == "audio" else None


def _make_uploader(uploader_module, calls):
    """Build an uploader wired to a fake client that records its calls."""
    calls_by_id = {}

    def _sent(kind, caption, reply_parameters):
        return FakeMessage(
            kind,
            caption=caption,
            reply_to_message_id=reply_parameters.message_id,
            registry=calls_by_id,
        )

    async def send_photo(chat_id, reply_parameters=None, caption=None, **_kwargs):
        return _sent("photo", caption, reply_parameters)

    async def send_video(chat_id, reply_parameters=None, caption=None, **_kwargs):
        return _sent("video", caption, reply_parameters)

    async def send_document(chat_id, reply_parameters=None, caption=None, **_kwargs):
        return _sent("document", caption, reply_parameters)

    async def send_audio(chat_id, reply_parameters=None, caption=None, **_kwargs):
        return _sent("audio", caption, reply_parameters)

    async def send_media_group(chat_id, media, **kwargs):
        calls.append(("send_media_group", list(media)))
        sent = [FakeMessage("photo", caption=m.caption) for m in media]
        for msg in sent:
            msg.media_group_id = "group1"
        return sent

    async def get_messages(chat_id, message_ids):
        return calls_by_id[message_ids]

    client = SimpleNamespace(
        send_photo=send_photo,
        send_video=send_video,
        send_document=send_document,
        send_audio=send_audio,
        send_media_group=send_media_group,
        get_messages=get_messages,
    )
    listener = SimpleNamespace(
        thumb="none",
        user_id=1,
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
        user_dict={},
        mid=1,
        message=None,
    )
    uploader = uploader_module.TelegramUploader(listener, "/tmp/task")
    uploader._thumb = None
    uploader._sent_msg = FakeMessage(registry=calls_by_id)
    uploader._files_links = True
    # Album batching is enabled by the MEDIA_GROUP user setting, which is
    # resolved at upload start in `_user_settings`; the tests exercise
    # `_upload_file` directly, so set it here to mirror that resolution.
    uploader._media_group = True
    return uploader, calls_by_id


@pytest.mark.asyncio
async def test_album_is_sent_every_ten_media(uploader_module):
    calls = []
    uploader, _ = _make_uploader(uploader_module, calls)

    for i in range(9):
        await uploader._upload_file(f"<code>{i}.jpg</code>", f"{i}.jpg", f"/tmp/{i}.jpg")
    assert calls == []
    assert len(uploader._album_msgs) == 9

    await uploader._upload_file("<code>9.jpg</code>", "9.jpg", "/tmp/9.jpg")
    assert len(calls) == 1
    assert len(calls[0][1]) == 10
    assert uploader._album_msgs == []


@pytest.mark.asyncio
async def test_photos_and_videos_share_one_album_in_order(uploader_module):
    calls = []
    uploader, _ = _make_uploader(uploader_module, calls)
    types_mod = sys.modules["pyrogram.types"]
    media_utils = sys.modules["bot.helper.ext_utils.media_utils"]

    await uploader._upload_file("<code>a.jpg</code>", "a.jpg", "/tmp/a.jpg")
    media_utils.get_document_type.return_value = (True, False, False)
    await uploader._upload_file("<code>b.mp4</code>", "b.mp4", "/tmp/b.mp4")
    media_utils.get_document_type.return_value = (False, False, True)
    await uploader._upload_file("<code>c.jpg</code>", "c.jpg", "/tmp/c.jpg")

    await uploader._send_album()

    assert len(calls) == 1
    media = calls[0][1]
    assert [type(m) for m in media] == [
        types_mod.InputMediaPhoto,
        types_mod.InputMediaVideo,
        types_mod.InputMediaPhoto,
    ]
    assert [m.caption for m in media] == [
        "<code>a.jpg</code>",
        "<code>b.mp4</code>",
        "<code>c.jpg</code>",
    ]


@pytest.mark.asyncio
async def test_single_pending_media_stays_a_standalone_message(uploader_module):
    calls = []
    uploader, _ = _make_uploader(uploader_module, calls)

    await uploader._upload_file("<code>only.jpg</code>", "only.jpg", "/tmp/only.jpg")
    await uploader._send_album()

    assert calls == []
    assert uploader._album_msgs == []


@pytest.mark.asyncio
async def test_pending_album_is_flushed_before_a_document(uploader_module):
    calls = []
    uploader, _ = _make_uploader(uploader_module, calls)
    media_utils = sys.modules["bot.helper.ext_utils.media_utils"]

    await uploader._upload_file("<code>a.jpg</code>", "a.jpg", "/tmp/a.jpg")
    await uploader._upload_file("<code>b.jpg</code>", "b.jpg", "/tmp/b.jpg")

    media_utils.get_document_type.return_value = (False, False, False)
    await uploader._upload_file("<code>c.rar</code>", "c.rar", "/tmp/c.rar")
    media_utils.get_document_type.return_value = (False, False, True)

    assert len(calls) == 1, "album should go out before the document"
    assert len(calls[0][1]) == 2
    assert uploader._album_msgs == []
    assert uploader._sent_msg.document is not None


@pytest.mark.asyncio
async def test_album_replaces_individual_links_in_msgs_dict(uploader_module):
    calls = []
    uploader, _ = _make_uploader(uploader_module, calls)

    await uploader._upload_file("<code>a.jpg</code>", "a.jpg", "/tmp/a.jpg")
    individual = uploader._sent_msg.link
    uploader._msgs_dict[individual] = "a.jpg"
    await uploader._upload_file("<code>b.jpg</code>", "b.jpg", "/tmp/b.jpg")
    uploader._msgs_dict[uploader._sent_msg.link] = "b.jpg"

    await uploader._send_album()

    assert individual not in uploader._msgs_dict
    assert len(uploader._msgs_dict) == 2
    assert sorted(uploader._msgs_dict.values()) == [
        "<code>a.jpg</code>",
        "<code>b.jpg</code>",
    ]
