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
    # bot.__path__ has to allow the stubbed submodules above to resolve.
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
        user_dict={},
        mid=1,
        message=None,
        on_upload_complete=AsyncMock(),
        on_upload_error=AsyncMock(),
    )
    uploader = uploader_module.TelegramUploader(listener, "/tmp/task")
    uploader._thumb = None
    uploader._sent_msg = FakeMessage(registry=calls_by_id)
    uploader._files_links = True
    # Album batching is enabled by the MEDIA_GROUP user setting, which is
    # resolved at upload start in `_user_settings`; the tests exercise
    # `_upload_file` directly, so set it on the batcher here to mirror that.
    uploader._batcher.enabled = True
    return uploader, calls_by_id


@pytest.mark.asyncio
async def test_album_is_sent_every_ten_media(uploader_module):
    calls = []
    uploader, _ = _make_uploader(uploader_module, calls)

    for i in range(9):
        await uploader._upload_file(f"<code>{i}.jpg</code>", f"{i}.jpg", f"/tmp/{i}.jpg")
    assert calls == []
    assert len(uploader._batcher._album_msgs) == 9

    await uploader._upload_file("<code>9.jpg</code>", "9.jpg", "/tmp/9.jpg")
    assert len(calls) == 1
    assert len(calls[0][1]) == 10
    assert uploader._batcher._album_msgs == []


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

    await uploader._batcher.send_album()

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
    await uploader._batcher.send_album()

    assert calls == []
    assert uploader._batcher._album_msgs == []


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
    assert uploader._batcher._album_msgs == []
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

    await uploader._batcher.send_album()

    assert individual not in uploader._msgs_dict
    assert len(uploader._msgs_dict) == 2
    assert sorted(uploader._msgs_dict.values()) == [
        "<code>a.jpg</code>",
        "<code>b.jpg</code>",
    ]


# --- copying to a preset's chats -------------------------------------------
#
# A copy preset points `clone_dump_chats` at the chats it names and copies the
# albums there. What needs pinning is the seam: an album is copied once, the
# files it carried are not copied again, and a file that never joined an album is
# not left behind.


DUMPS = ((-2001, 12), (-2001, 34), (-2002, None))


def _record_copies(uploader, dumps=DUMPS, preset="anime"):
    """Point the uploader at *dumps* and record what the bot is asked to copy.

    Returns the log of `(kind, chat_id, thread_id, reply_to)` tuples, where kind
    is "group" for a whole album and "one" for a single message. Set on the
    stubbed `TgClient.bot` because that is the session the copies go out on,
    whichever one carried the upload.
    """
    copied = []

    async def copy_media_group(
        chat_id, message_thread_id=None, reply_to_message_id=None, **_kwargs
    ):
        copied.append(("group", chat_id, message_thread_id, reply_to_message_id))
        return [FakeMessage("photo"), FakeMessage("photo")]

    async def copy_message(
        chat_id, message_thread_id=None, reply_to_message_id=None, **_kwargs
    ):
        copied.append(("one", chat_id, message_thread_id, reply_to_message_id))
        return FakeMessage("photo")

    sys.modules["bot.core.telegram_manager"].TgClient.bot = SimpleNamespace(
        copy_media_group=copy_media_group, copy_message=copy_message
    )
    uploader._listener.copy_preset = preset
    uploader._listener.clone_dump_chats = {
        key: {"last_sent_msg": None} for key in dumps
    }
    return copied


async def _finish(uploader):
    """End the task the way the uploader does, past the "no files" guard."""
    uploader._total_files = 1
    await uploader._finish()


@pytest.mark.asyncio
async def test_a_copy_preset_forces_media_group_on(uploader_module):
    """With grouping off there would be no album to copy, which is the one thing
    a preset promises."""
    uploader, _ = _make_uploader(uploader_module, [])
    uploader._batcher.enabled = False
    uploader._listener.copy_preset = "anime"
    uploader._listener.user_dict = {
        "MEDIA_GROUP": False,
        "LEECH_FILENAME_PREFIX": "",
        "FILES_LINKS": False,
    }

    await uploader._user_settings()

    assert uploader._batcher.enabled is True


@pytest.mark.asyncio
async def test_an_album_is_copied_to_every_destination(uploader_module):
    uploader, _ = _make_uploader(uploader_module, [])
    copied = _record_copies(uploader)

    await uploader._upload_file("<code>a.jpg</code>", "a.jpg", "/tmp/a.jpg")
    await uploader._upload_file("<code>b.jpg</code>", "b.jpg", "/tmp/b.jpg")
    await uploader._batcher.send_album()

    assert [(kind, chat, thread) for kind, chat, thread, _ in copied] == [
        ("group", -2001, 12),
        ("group", -2001, 34),
        ("group", -2002, None),
    ]


@pytest.mark.asyncio
async def test_two_topics_of_one_group_each_get_their_own_copy(uploader_module):
    """The headline case: addressed by thread, not by replying into one."""
    uploader, _ = _make_uploader(uploader_module, [])
    copied = _record_copies(uploader, dumps=((-2001, 12), (-2001, 34)))

    await uploader._upload_file("<code>a.jpg</code>", "a.jpg", "/tmp/a.jpg")
    await uploader._upload_file("<code>b.jpg</code>", "b.jpg", "/tmp/b.jpg")
    await uploader._batcher.send_album()

    assert [thread for _, _, thread, _ in copied] == [12, 34]


@pytest.mark.asyncio
async def test_files_an_album_carried_are_not_copied_again(uploader_module):
    """The album is the copy; copying its files individually would double them."""
    uploader, _ = _make_uploader(uploader_module, [])
    copied = _record_copies(uploader)

    await uploader._upload_file("<code>a.jpg</code>", "a.jpg", "/tmp/a.jpg")
    await uploader._upload_file("<code>b.jpg</code>", "b.jpg", "/tmp/b.jpg")
    await uploader._batcher.send_album()
    await _finish(uploader)

    assert [kind for kind, *_ in copied] == ["group"] * 3


@pytest.mark.asyncio
async def test_a_single_file_task_still_reaches_the_destinations(uploader_module):
    """One file never becomes an album, and would otherwise be copied nowhere."""
    uploader, _ = _make_uploader(uploader_module, [])
    copied = _record_copies(uploader)

    await uploader._upload_file("<code>only.jpg</code>", "only.jpg", "/tmp/only.jpg")
    await _finish(uploader)

    assert [(kind, chat, thread) for kind, chat, thread, _ in copied] == [
        ("one", -2001, 12),
        ("one", -2001, 34),
        ("one", -2002, None),
    ]


@pytest.mark.asyncio
async def test_the_odd_file_left_after_an_album_is_copied_on_its_own(uploader_module):
    """An album goes out at ten, so a task of eleven files leaves one over."""
    uploader, _ = _make_uploader(uploader_module, [])
    copied = _record_copies(uploader, dumps=((-2002, None),))

    await uploader._upload_file("<code>a.jpg</code>", "a.jpg", "/tmp/a.jpg")
    await uploader._upload_file("<code>b.jpg</code>", "b.jpg", "/tmp/b.jpg")
    await uploader._batcher.send_album()
    await uploader._upload_file("<code>c.jpg</code>", "c.jpg", "/tmp/c.jpg")
    await _finish(uploader)

    assert [kind for kind, *_ in copied] == ["group", "one"]


@pytest.mark.asyncio
async def test_each_destination_keeps_its_own_reply_chain(uploader_module):
    """Threading is per destination: the second copy answers the first copy in
    that chat, not the one in whichever chat was copied to last."""
    uploader, _ = _make_uploader(uploader_module, [])
    copied = _record_copies(uploader, dumps=((-2001, None), (-2002, None)))

    for name in ("a", "b"):
        await uploader._upload_file(f"<code>{name}.jpg</code>", f"{name}.jpg", f"/{name}")
    await uploader._batcher.send_album()
    first = {chat: reply for _, chat, _, reply in copied}
    copied.clear()
    for name in ("c", "d"):
        await uploader._upload_file(f"<code>{name}.jpg</code>", f"{name}.jpg", f"/{name}")
    await uploader._batcher.send_album()

    assert set(first.values()) == {None}, "nothing to reply to on the first album"
    assert len({reply for _, _, _, reply in copied}) == 2, (
        "each chat should answer its own last message"
    )


@pytest.mark.asyncio
async def test_without_a_preset_a_lone_file_is_not_copied(uploader_module):
    """Plain `CLONE_DUMP_CHATS` copies albums and nothing else, exactly as
    before -- only `-c` opts into the individual copies."""
    uploader, _ = _make_uploader(uploader_module, [])
    copied = _record_copies(uploader, preset="")

    await uploader._upload_file("<code>only.jpg</code>", "only.jpg", "/tmp/only.jpg")
    await _finish(uploader)

    assert copied == []
    assert uploader._uncopied == []


@pytest.mark.asyncio
async def test_one_unreachable_destination_does_not_cost_the_others(uploader_module):
    """A `return` here used to skip every destination after the first failure."""
    uploader, _ = _make_uploader(uploader_module, [])
    copied = _record_copies(uploader)
    good = sys.modules["bot.core.telegram_manager"].TgClient.bot.copy_media_group

    async def copy_media_group(chat_id, **kwargs):
        if chat_id == -2001 and kwargs.get("message_thread_id") == 12:
            raise _Err("chat not found")
        return await good(chat_id, **kwargs)

    sys.modules["bot.core.telegram_manager"].TgClient.bot.copy_media_group = (
        copy_media_group
    )

    await uploader._upload_file("<code>a.jpg</code>", "a.jpg", "/tmp/a.jpg")
    await uploader._upload_file("<code>b.jpg</code>", "b.jpg", "/tmp/b.jpg")
    await uploader._batcher.send_album()

    assert [(chat, thread) for _, chat, thread, _ in copied] == [
        (-2001, 34),
        (-2002, None),
    ]
