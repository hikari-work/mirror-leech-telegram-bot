"""Unit tests for the per-file helpers extracted out of _upload_file.

Covers thumbnail resolution and cleanup, sender dispatch, and the split-file
media group bookkeeping. The album itself is exercised by
test_telegram_uploader_album.py.
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


@pytest.fixture
def uploader_module(monkeypatch):
    """Import the Telegram uploader with its dependencies stubbed out."""
    root = Path(__file__).resolve().parent.parent

    def _passthrough(*_args, **_kwargs):
        return lambda func: func

    # Kept in a local so the module stub and ``user_session`` below hand out the
    # same object; a test that swaps a client on it swaps it for both.
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
        "bot.core.config_manager": _stub("bot.core.config_manager", Config=object()),
        "bot.core.telegram_manager": _stub(
            "bot.core.telegram_manager",
            TgClient=tg_client,
            # The real one stands for "the user session, which exists on this
            # path"; here it always does.
            user_session=lambda: tg_client.user,
        ),
        "bot.helper": _pkg("bot.helper"),
        # Real path: the uploader delegates its copy fan-out to
        # ``ext_utils.copy_records``, which imports nothing stubbed.
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
        # Real path, not a stub: the uploader reads a flood's wait through
        # ``telegram_helper.flood``, which needs nothing but the stubbed
        # ``pyrogram.errors`` to import. ``message_utils`` still resolves to the
        # stub below, because sys.modules wins over the path.
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
    # The siblings are popped too: they bind the stubbed FloodWait and
    # InputMedia classes at import time, so a copy left behind would hand the
    # next test file the wrong ones.
    # ``telegram_helper.flood`` is real but imported under the stubbed errors,
    # so it is dropped with them.
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
    _next_id = 200

    def __init__(self, kind=None, caption=None):
        FakeMessage._next_id += 1
        self.id = FakeMessage._next_id
        self.chat = SimpleNamespace(id=-1001, type=SimpleNamespace(name="CHANNEL"))
        self.caption = caption
        self.message_thread_id = None
        self.link = f"https://t.me/c/1001/{self.id}"
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
    """Minimal uploader for exercising the per-file helpers directly."""
    listener = SimpleNamespace(
        thumb="none",
        user_id=1,
        client=SimpleNamespace(),
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
        user_dict={},
        mid=1,
        message=None,
    )
    uploader = uploader_module.TelegramUploader(listener, "/tmp/task")
    uploader._thumb = None
    uploader._sent_msg = FakeMessage()
    uploader._batcher.enabled = True
    return uploader


# --- _resolve_thumb --------------------------------------------------------


@pytest.mark.asyncio
async def test_thumbnail_prefers_the_yt_dlp_sidecar(uploader_module):
    uploader = _make_uploader(uploader_module)
    uploader_module.aiopath.isfile.side_effect = lambda p: (
        p == "/tmp/task/yt-dlp-thumb/a.jpg"
    )
    thumb = await uploader._resolve_thumb("a.mp4", None, True, False, False)
    assert thumb == "/tmp/task/yt-dlp-thumb/a.jpg"


@pytest.mark.asyncio
async def test_thumbnail_looks_next_to_the_media_file(uploader_module):
    uploader = _make_uploader(uploader_module)
    uploader_module.aiopath.isfile.side_effect = lambda p: p == "/tmp/task/a.jpg"
    thumb = await uploader._resolve_thumb("a.mp4", None, True, False, False)
    assert thumb == "/tmp/task/a.jpg"


@pytest.mark.asyncio
async def test_existing_thumb_wins_over_any_lookup(uploader_module):
    uploader = _make_uploader(uploader_module)
    uploader_module.aiopath.isfile.side_effect = lambda _p: True
    thumb = await uploader._resolve_thumb(
        "a.mp4", "/thumbs/user.jpg", True, False, False
    )
    assert thumb == "/thumbs/user.jpg"


@pytest.mark.asyncio
async def test_thumbnail_skips_search_for_images(uploader_module, monkeypatch):
    uploader = _make_uploader(uploader_module)
    cover = AsyncMock(return_value="/gen/cover.jpg")
    # The module binds the helper at import time, so patch it there.
    monkeypatch.setattr(uploader_module, "get_audio_thumbnail", cover)

    thumb = await uploader._resolve_thumb("a.jpg", None, False, True, True)
    assert thumb is None
    cover.assert_not_awaited()


@pytest.mark.asyncio
async def test_thumbnail_uses_embedded_cover_for_audio(uploader_module, monkeypatch):
    uploader = _make_uploader(uploader_module)
    monkeypatch.setattr(
        uploader_module, "get_audio_thumbnail", AsyncMock(return_value="/gen/cover.jpg")
    )

    thumb = await uploader._resolve_thumb("a.m4a", None, False, True, False)
    assert thumb == "/gen/cover.jpg"


@pytest.mark.asyncio
async def test_video_sent_as_document_keeps_no_cover_lookup(
    uploader_module, monkeypatch
):
    uploader = _make_uploader(uploader_module)
    cover = AsyncMock(return_value="/gen/cover.jpg")
    monkeypatch.setattr(uploader_module, "get_audio_thumbnail", cover)

    thumb = await uploader._resolve_thumb("a.mkv", None, True, True, False)
    assert thumb is None
    cover.assert_not_awaited()


# --- _temp_thumb -----------------------------------------------------------


@pytest.mark.asyncio
async def test_temp_thumb_removes_only_generated_thumbs(uploader_module):
    uploader = _make_uploader(uploader_module)
    uploader_module.aiopath.exists.return_value = True
    attempt = uploader_module._Attempt("/gen/vid.jpg")
    async with uploader._temp_thumb(attempt):
        pass
    uploader_module.remove.assert_awaited_once_with("/gen/vid.jpg")


@pytest.mark.asyncio
async def test_temp_thumb_keeps_user_thumb(uploader_module):
    uploader = _make_uploader(uploader_module)
    uploader._thumb = "/thumbs/user.jpg"
    uploader_module.aiopath.exists.return_value = True
    attempt = uploader_module._Attempt("/thumbs/user.jpg")
    async with uploader._temp_thumb(attempt):
        pass
    uploader_module.remove.assert_not_awaited()


@pytest.mark.asyncio
async def test_temp_thumb_cleans_up_when_send_raises(uploader_module):
    uploader = _make_uploader(uploader_module)
    uploader_module.aiopath.exists.return_value = True
    attempt = uploader_module._Attempt("/gen/vid.jpg")
    with pytest.raises(RuntimeError):
        async with uploader._temp_thumb(attempt):
            raise RuntimeError("boom")
    uploader_module.remove.assert_awaited_once_with("/gen/vid.jpg")


@pytest.mark.asyncio
async def test_temp_thumb_keeps_thumb_of_aborted_send(uploader_module):
    """A send that cancellation stopped keeps its thumbnail, matching the early
    return the original code took before any cleanup ran."""
    uploader = _make_uploader(uploader_module)
    uploader_module.aiopath.exists.return_value = True
    attempt = uploader_module._Attempt("/gen/vid.jpg")
    attempt.aborted = True
    async with uploader._temp_thumb(attempt):
        pass
    uploader_module.remove.assert_not_awaited()


# --- sender dispatch -------------------------------------------------------


def _sender_names(uploader_module):
    return sorted(
        uploader_module.TelegramUploader._SENDERS[k].__name__
        for k in ("documents", "videos", "audios", "photos")
    )


def test_sender_table_covers_all_four_kinds(uploader_module):
    assert _sender_names(uploader_module) == [
        "_send_as_audio",
        "_send_as_document",
        "_send_as_photo",
        "_send_as_video",
    ]


def test_pick_key_prefers_document_for_force(uploader_module):
    uploader = _make_uploader(uploader_module)
    uploader._listener.as_doc = False
    assert uploader._pick_key(True, True, False, False) == "documents"


def test_pick_key_obeys_as_doc_setting(uploader_module):
    uploader = _make_uploader(uploader_module)
    uploader._listener.as_doc = True
    assert uploader._pick_key(False, True, False, False) == "documents"


def test_pick_key_buckets_by_media_type(uploader_module):
    uploader = _make_uploader(uploader_module)
    assert uploader._pick_key(False, True, False, False) == "videos"
    assert uploader._pick_key(False, False, True, False) == "audios"
    assert uploader._pick_key(False, False, False, True) == "photos"
    assert uploader._pick_key(False, False, False, False) == "documents"


# --- reply target and client routing ---------------------------------------


def test_send_client_follows_the_hybrid_size_switch(uploader_module):
    """Hybrid leech flips client per file, which is why the anchor is addressed
    by id instead of being re-fetched through whichever client needs it next."""
    uploader = _make_uploader(uploader_module)
    tg_client = sys.modules["bot.core.telegram_manager"].TgClient

    uploader._user_session = False
    assert uploader._send_client is uploader._listener.client
    uploader._user_session = True
    assert uploader._send_client is tg_client.user


def test_reply_args_target_the_anchor_chat_and_topic(uploader_module):
    uploader = _make_uploader(uploader_module)
    uploader._sent_msg.message_thread_id = 77

    args = uploader._reply_args()

    assert args["chat_id"] == uploader._sent_msg.chat.id
    assert args["message_thread_id"] == 77
    assert args["reply_parameters"].message_id == uploader._sent_msg.id

