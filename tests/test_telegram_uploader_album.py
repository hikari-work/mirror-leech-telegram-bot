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
        "bot.core.config_manager": _stub(
            # DATABASE_URL is read in `_user_settings` to decide whether the
            # task's messages are recorded for /copy; tests that want them
            # recorded flip it on.
            "bot.core.config_manager",
            Config=SimpleNamespace(DATABASE_URL=""),
        ),
        "bot.core.telegram_manager": _stub(
            "bot.core.telegram_manager",
            TgClient=tg_client,
            # The real one stands for "the user session, which exists on this
            # path"; here it always does.
            user_session=lambda: tg_client.user,
        ),
        "bot.helper": _pkg("bot.helper"),
        # Real path: the uploader delegates its copy fan-out to
        # ``storage.copy_records``, which imports nothing stubbed, so it is
        # loaded for real from disk. The individual ``bot.helper.util.*``
        # stubs below still win, because a sys.modules entry beats the path.
        "bot.helper.util": _pkg("bot.helper.util"),
        "bot.helper.storage": _pkg(
            "bot.helper.storage",
            str(root / "bot" / "helper" / "storage"),
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
        # Real path, not a stub: the uploader reads a flood's wait through
        # ``telegram.flood``, which needs nothing but the stubbed
        # ``pyrogram.errors`` to import. ``message_utils`` still resolves to the
        # stub below, because sys.modules wins over the path.
        "bot.helper.telegram": _pkg(
            "bot.helper.telegram",
            str(root / "bot" / "helper" / "telegram"),
        ),
        "bot.helper.telegram.message_utils": _stub(
            "bot.helper.telegram.message_utils",
            chat_of=lambda message: message.chat,
            delete_message=AsyncMock(),
        ),
        # Real path: the uploader, flood pacer and media-group batcher all live
        # in this package and load from disk.
        "bot.helper.upload": _pkg(
            "bot.helper.upload",
            str(root / "bot" / "helper" / "upload"),
        ),
    }
    # bot.__path__ has to allow the stubbed submodules above to resolve.
    modules["bot"].__path__ = []
    for name, mod in modules.items():
        monkeypatch.setitem(sys.modules, name, mod)

    pkg = "bot.helper.upload"
    target = f"{pkg}.telegram_uploader"
    # The siblings are popped too: they bind the stubbed FloodWait and
    # InputMedia classes at import time, so a copy left behind would hand the
    # next test file the wrong ones.
    # ``telegram.flood`` is real but imported under the stubbed errors,
    # so it is dropped with them.
    siblings = (
        f"{pkg}.flood_pacer",
        f"{pkg}.media_group_batcher",
        "bot.helper.telegram.flood",
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

    async def delete(self, revoke=True):
        """What ``Message.delete`` does: one delete through its own client."""
        return await self._client.delete_messages(
            chat_id=self.chat.id, message_ids=self.id, revoke=revoke
        )


class _FakeClient(SimpleNamespace):
    """A namespace that hashes by identity, the way a real client does.

    ``SimpleNamespace`` compares by value and is therefore unhashable, but the
    batched delete groups messages by the client that sent them -- and two real
    clients are only ever the same object, never merely equal.
    """

    __hash__ = object.__hash__
    __eq__ = object.__eq__


def _as_ids(message_ids):
    """The message ids as a list.

    pyrogram takes one id or many: ``Message.delete`` sends the single-id form,
    while a batched delete sends the list.
    """
    return [message_ids] if isinstance(message_ids, int) else list(message_ids)


def _senders(state):
    """What a fake client answers to, keyed by the name pyrogram gives it.

    The client is read off *state* rather than closed over, because it cannot be
    handed to its own methods before it exists -- and a message has to carry the
    client that sent it, since that is what ``Message.delete`` sends through and
    what a batched delete keys on.
    """

    def _sent(kind, caption, reply_parameters):
        msg = FakeMessage(
            kind,
            caption=caption,
            reply_to_message_id=reply_parameters.message_id,
            registry=state.calls_by_id,
        )
        msg._client = state.client
        return msg

    def sender(kind):
        async def send(chat_id, reply_parameters=None, caption=None, **_kwargs):
            return _sent(kind, caption, reply_parameters)

        return send

    async def send_media_group(chat_id, media, **kwargs):
        if state.calls is not None:
            state.calls.append(("send_media_group", list(media)))
        sent = [FakeMessage("photo", caption=m.caption) for m in media]
        for msg in sent:
            msg.media_group_id = "group1"
            msg._client = state.client
        return sent

    async def get_messages(chat_id, message_ids):
        return state.calls_by_id[message_ids]

    async def delete_messages(chat_id, message_ids, revoke=True):
        state.deletes.append((chat_id, _as_ids(message_ids)))
        return len(state.deletes[-1][1])

    return {
        "send_photo": sender("photo"),
        "send_video": sender("video"),
        "send_document": sender("document"),
        "send_audio": sender("audio"),
        "send_media_group": send_media_group,
        "get_messages": get_messages,
        "delete_messages": delete_messages,
    }


def _make_client(calls_by_id, calls):
    """A fake pyrogram client whose files answer with registered messages.

    Every message it sends registers itself by id, so ``get_messages`` can hand
    it back -- which is what the album does when the anchor is not one the album
    can be built from.

    *calls* is where ``send_media_group`` is recorded; the deletes are always
    recorded, on ``client.deletes``, since a test asserting them has to.
    """
    state = SimpleNamespace(
        calls_by_id=calls_by_id, calls=calls, deletes=[], client=None
    )
    client = _FakeClient(**_senders(state))
    client.deletes = state.deletes
    state.client = client
    return client


def _make_uploader(uploader_module, calls):
    """Build an uploader wired to a fake client that records its calls."""
    calls_by_id = {}
    client = _make_client(calls_by_id, calls)
    # The user session is a client of its own and is what carries a file under
    # hybrid leech. Its messages register alongside the others, so the album can
    # read one back -- which is exactly what it has to do on that path.
    sys.modules["bot.core.telegram_manager"].TgClient.user = _make_client(
        calls_by_id, None
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
    media_utils = sys.modules["bot.helper.util.media_utils"]

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
    media_utils = sys.modules["bot.helper.util.media_utils"]

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


# --- a group send that hits a connection drop ------------------------------
#
# An album goes out as one request, and the whole point is lost if that request
# dies to a transient failure while the messages it would absorb stay put. The
# send is retried in place -- bounded, so a dead connection cannot hold the
# upload hostage -- and only then given up on, leaving the messages where they
# already are, one by one, exactly as a refusal would.


def _failing_send_media_group(failures, error):
    """A ``send_media_group`` that drops the first *failures* calls.

    Returns the replacement together with the record of every payload it was
    asked to send, so a test can tell an in-place retry from a re-send.
    """
    attempts = []

    async def send_media_group(chat_id, media, **_kwargs):
        attempts.append(list(media))
        if len(attempts) <= failures:
            raise error
        return [FakeMessage("photo", caption=m.caption) for m in media]

    return send_media_group, attempts


def _count_photos(uploader):
    """Replace the fake ``send_photo`` with one that counts the calls."""
    send_photo = uploader._listener.client.send_photo
    photos = []

    async def counted_send_photo(chat_id, caption=None, **_kwargs):
        photos.append(caption)
        return await send_photo(chat_id, caption=caption, **_kwargs)

    uploader._listener.client.send_photo = counted_send_photo
    return photos


@pytest.mark.asyncio
async def test_an_album_retries_a_connection_drop_in_place(
    uploader_module, monkeypatch
):
    """A group send that hits a connection drop is retried as the same album,
    not by re-sending the file that flushed it."""
    monkeypatch.setattr(uploader_module, "_GROUP_RETRIES", 3, raising=False)
    monkeypatch.setattr(uploader_module, "_GROUP_RETRY_DELAY", 0.0, raising=False)
    uploader, _ = _make_uploader(uploader_module, [])
    photos = _count_photos(uploader)
    uploader._listener.client.send_media_group, attempts = _failing_send_media_group(
        2, TimeoutError("Failed to invoke after 10 retries")
    )

    for i in range(10):
        await uploader._upload_file(f"<code>{i}.jpg</code>", f"{i}.jpg", f"/tmp/{i}.jpg")

    assert len(photos) == 10, "retrying the album must not re-send the files"
    assert len(attempts) == 3, "two drops, then the group itself goes out again"
    assert all(len(group) == 10 for group in attempts)
    assert [m.media for m in attempts[0]] == [m.media for m in attempts[-1]]
    assert uploader._batcher._album_msgs == []


@pytest.mark.asyncio
async def test_a_group_send_that_never_recovers_resends_no_file(
    uploader_module, monkeypatch
):
    """Giving up on an album keeps the file count honest: the tenth photo was
    already sent, so retrying it would double the file."""
    monkeypatch.setattr(uploader_module, "_GROUP_RETRIES", 1, raising=False)
    monkeypatch.setattr(uploader_module, "_GROUP_RETRY_DELAY", 0.0, raising=False)
    uploader, _ = _make_uploader(uploader_module, [])
    photos = _count_photos(uploader)
    uploader._listener.client.send_media_group, attempts = _failing_send_media_group(
        1, TimeoutError("Request timed out")
    )

    for i in range(10):
        await uploader._upload_file(f"<code>{i}.jpg</code>", f"{i}.jpg", f"/tmp/{i}.jpg")

    assert len(photos) == 10
    assert len(attempts) == 1
    assert uploader._batcher._album_msgs == []
    uploader._listener.on_upload_error.assert_not_awaited()


# --- deleting what the album absorbed --------------------------------------


@pytest.mark.asyncio
async def test_an_album_deletes_its_originals_in_one_call(uploader_module):
    """Three messages in one chat are one delete, not three."""
    uploader, _ = _make_uploader(uploader_module, [])
    for name in ("a.jpg", "b.jpg", "c.jpg"):
        await uploader._upload_file(f"<code>{name}</code>", name, f"/tmp/{name}")

    await uploader._batcher.send_album()

    deletes = uploader._listener.client.deletes
    assert len(deletes) == 1
    chat_id, message_ids = deletes[0]
    assert chat_id == -1001
    assert len(message_ids) == 3


@pytest.mark.asyncio
async def test_originals_are_deleted_through_the_client_that_sent_them(
    uploader_module,
):
    """Hybrid leech can put both clients' files in one album.

    ``Message.delete`` sends through the client the message is bound to, so
    batching by chat alone would delete one client's message through the other.
    """
    calls = []
    uploader, _ = _make_uploader(uploader_module, calls)
    uploader._listener.hybrid_leech = True
    bot_client = uploader._listener.client
    user_client = sys.modules["bot.core.telegram_manager"].TgClient.user

    for index, name in enumerate(("a.jpg", "b.jpg")):
        # the first file goes through the user session, the second through the bot
        uploader._user_session = index == 0
        await uploader._upload_file(f"<code>{name}</code>", name, f"/tmp/{name}")

    await uploader._batcher.send_album()

    assert len(bot_client.deletes) == 1
    assert len(user_client.deletes) == 1
    assert len(bot_client.deletes[0][1]) == 1
    assert len(user_client.deletes[0][1]) == 1


# --- which client the album's file_ids belong to ---------------------------


def test_the_anchor_is_reused_when_the_album_client_sent_it(uploader_module):
    """No user session: one client sends the files and the album alike."""
    uploader, _ = _make_uploader(uploader_module, [])

    assert uploader._send_client is uploader._group_client
    assert uploader.anchor_for_group() is uploader.anchor


def test_the_anchor_is_reused_under_a_user_session_without_hybrid(
    uploader_module,
):
    """Both sides move to the user session, so they still agree."""
    uploader, _ = _make_uploader(uploader_module, [])
    # ``_user_session`` is read off the listener once, in __init__
    uploader._user_session = True
    uploader._listener.hybrid_leech = False

    assert uploader._send_client is uploader._group_client
    assert uploader.anchor_for_group() is uploader.anchor


def test_the_anchor_is_fetched_back_when_hybrid_splits_the_two(uploader_module):
    """Hybrid leech sends the file through the user session and the album
    through the bot, so the file_id the file answered with is not one the album
    can be built from -- the message has to be read back instead."""
    uploader, _ = _make_uploader(uploader_module, [])
    uploader._user_session = True
    uploader._listener.hybrid_leech = True

    assert uploader._send_client is not uploader._group_client
    assert uploader.anchor_for_group() is None


@pytest.mark.asyncio
async def test_an_album_of_fetched_messages_still_carries_their_captions(
    uploader_module,
):
    """Reading the messages back has to leave the album as it would have been."""
    calls = []
    uploader, _ = _make_uploader(uploader_module, calls)
    uploader._user_session = True
    uploader._listener.hybrid_leech = True

    for name in ("a.jpg", "b.jpg"):
        await uploader._upload_file(f"<code>{name}</code>", name, f"/tmp/{name}")

    # nothing the album's own client can use, so both have to be read back
    assert uploader._batcher._album_msgs[0][2] is None

    await uploader._batcher.send_album()

    assert [m.caption for m in calls[0][1]] == [
        "<code>a.jpg</code>",
        "<code>b.jpg</code>",
    ]


@pytest.mark.asyncio
async def test_an_album_of_reused_anchors_carries_their_captions(uploader_module):
    """...and the path that sends through one client throughout still matches."""
    calls = []
    uploader, _ = _make_uploader(uploader_module, calls)

    for name in ("a.jpg", "b.jpg"):
        await uploader._upload_file(f"<code>{name}</code>", name, f"/tmp/{name}")

    assert uploader._batcher._album_msgs[0][2] is not None

    await uploader._batcher.send_album()

    assert [m.caption for m in calls[0][1]] == [
        "<code>a.jpg</code>",
        "<code>b.jpg</code>",
    ]
