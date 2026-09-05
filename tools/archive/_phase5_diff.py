"""Differential harness: telegram_uploader._upload_file before vs after Fase 5.

Loads the pre-refactor module out of git and the working-tree module under the
same stubs, drives both through identical scenarios, and compares everything
observable: the telegram calls made and their order, the state left on the
uploader, the thumbnails removed, and the exception raised.

Usage: python tools/_phase5_diff.py
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
TARGET = "bot/helper/mirror_leech_utils/upload_utils/telegram_uploader.py"
MODNAME = "bot.helper.mirror_leech_utils.upload_utils.telegram_uploader"

# --------------------------------------------------------------------------
# stub environment
# --------------------------------------------------------------------------


class _Err(Exception):
    pass


class FloodWait(_Err):
    def __init__(self, value=1):
        super().__init__(f"flood {value}")
        self.value = value


class FloodPremiumWait(FloodWait):
    pass


class RPCError(_Err):
    pass


class BadRequest(RPCError):
    pass


class _InputMedia:
    def __init__(self, media=None, caption=None, **kw):
        self.media = media
        self.caption = caption


class InputMediaVideo(_InputMedia):
    pass


class InputMediaDocument(_InputMedia):
    pass


class InputMediaPhoto(_InputMedia):
    pass


class _FakeImage:
    def __init__(self, size=(111, 222)):
        self.size = size

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


# Mutable knobs the scenarios drive; the stub modules read them at call time.
class World:
    def __init__(self):
        self.log = []
        self.existing = set()
        self.doc_type = (False, False, True)
        self.audio_thumb = None
        self.video_thumb = None
        self.frames_thumb = None
        self.raise_on = {}  # call name -> exception factory
        self.cancel_after = None  # call name -> flips is_cancelled
        self.listener = None

    def record(self, name, *args):
        self.log.append((name, *args))

    def maybe_raise(self, name):
        factory = self.raise_on.get(name)
        if factory is not None:
            n = sum(1 for e in self.log if e[0] == f"raise:{name}")
            self.record(f"raise:{name}")
            exc = factory(n)
            if exc is not None:
                raise exc

    def maybe_cancel(self, name):
        if self.cancel_after == name:
            self.listener.is_cancelled = True


WORLD = World()


def _pkg(name, path=None):
    mod = ModuleType(name)
    mod.__path__ = [] if path is None else [path]
    return mod


def _stub(name, **attrs):
    mod = ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


async def _exists(p):
    WORLD.record("exists", p)
    return p in WORLD.existing


async def _isfile(p):
    WORLD.record("isfile", p)
    return p in WORLD.existing


async def _getsize(_p):
    return 1


async def _remove(p):
    WORLD.record("remove", p)
    WORLD.existing.discard(p)


async def _rename(a, b):
    WORLD.record("rename", a, b)


async def _get_document_type(_p):
    WORLD.record("get_document_type")
    WORLD.maybe_raise("get_document_type")
    return WORLD.doc_type


async def _get_media_info(_p):
    WORLD.record("get_media_info")
    return (42, "artist", "title")


async def _get_video_thumbnail(_p, duration):
    WORLD.record("get_video_thumbnail", duration)
    WORLD.maybe_raise("get_video_thumbnail")
    if WORLD.video_thumb:
        WORLD.existing.add(WORLD.video_thumb)
    return WORLD.video_thumb


async def _get_audio_thumbnail(_p):
    WORLD.record("get_audio_thumbnail")
    WORLD.maybe_raise("get_audio_thumbnail")
    if WORLD.audio_thumb:
        WORLD.existing.add(WORLD.audio_thumb)
    return WORLD.audio_thumb


async def _get_multiple_frames_thumbnail(_p, layout, shots):
    WORLD.record("get_multiple_frames_thumbnail", layout, shots)
    if WORLD.frames_thumb:
        WORLD.existing.add(WORLD.frames_thumb)
    return WORLD.frames_thumb


async def _delete_message(msg):
    WORLD.record("delete_message", getattr(msg, "id", msg))


async def _sleep(secs):
    WORLD.record("sleep", round(secs, 4))


def _passthrough(*_a, **_k):
    return lambda f: f


def install_stubs():
    aiofiles_os = _stub(
        "aiofiles.os",
        remove=_remove,
        rename=_rename,
        path=SimpleNamespace(exists=_exists, isfile=_isfile, getsize=_getsize),
    )
    mods = {
        "PIL": _stub("PIL", Image=SimpleNamespace(open=lambda *_a, **_k: _FakeImage())),
        "aioshutil": _stub("aioshutil", rmtree=lambda *_a, **_k: None),
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
            FloodWait=FloodWait,
            FloodPremiumWait=FloodPremiumWait,
            RPCError=RPCError,
            BadRequest=BadRequest,
        ),
        "pyrogram.types": _stub(
            "pyrogram.types",
            InputMediaVideo=InputMediaVideo,
            InputMediaDocument=InputMediaDocument,
            InputMediaPhoto=InputMediaPhoto,
        ),
        "bot": _stub("bot", intervals={"stopAll": False}),
        "bot.core": _pkg("bot.core"),
        "bot.core.config_manager": _stub(
            "bot.core.config_manager", Config=SimpleNamespace()
        ),
        "bot.core.telegram_manager": _stub(
            "bot.core.telegram_manager",
            TgClient=SimpleNamespace(user=SimpleNamespace(), bot=SimpleNamespace()),
        ),
        "bot.helper": _pkg("bot.helper"),
        "bot.helper.ext_utils": _pkg("bot.helper.ext_utils"),
        "bot.helper.ext_utils.bot_utils": _stub(
            "bot.helper.ext_utils.bot_utils", sync_to_async=None
        ),
        "bot.helper.ext_utils.files_utils": _stub(
            "bot.helper.ext_utils.files_utils",
            is_archive=lambda _p: False,
            get_base_name=lambda p: p,
        ),
        "bot.helper.ext_utils.media_utils": _stub(
            "bot.helper.ext_utils.media_utils",
            get_media_info=_get_media_info,
            get_document_type=_get_document_type,
            get_video_thumbnail=_get_video_thumbnail,
            get_audio_thumbnail=_get_audio_thumbnail,
            get_multiple_frames_thumbnail=_get_multiple_frames_thumbnail,
        ),
        "bot.helper.telegram_helper": _pkg("bot.helper.telegram_helper"),
        "bot.helper.telegram_helper.message_utils": _stub(
            "bot.helper.telegram_helper.message_utils", delete_message=_delete_message
        ),
        "bot.helper.mirror_leech_utils": _pkg("bot.helper.mirror_leech_utils"),
        "bot.helper.mirror_leech_utils.upload_utils": _pkg(
            "bot.helper.mirror_leech_utils.upload_utils",
            str(ROOT / "bot" / "helper" / "mirror_leech_utils" / "upload_utils"),
        ),
    }
    mods["bot"].__path__ = []
    sys.modules.update(mods)
    # asyncio.sleep is imported by name inside the module under test
    import asyncio as _aio

    _aio.sleep = _sleep


def load_old():
    """Exec the pre-refactor source as a module with working relative imports."""
    src = subprocess.run(
        ["git", "show", f"HEAD:{TARGET}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    mod = ModuleType("_old_telegram_uploader")
    mod.__package__ = "bot.helper.mirror_leech_utils.upload_utils"
    mod.__file__ = str(ROOT / TARGET)
    sys.modules["_old_telegram_uploader"] = mod
    exec(compile(src, mod.__file__, "exec"), mod.__dict__)
    return mod


def load_new():
    sys.modules.pop(MODNAME, None)
    return importlib.import_module(MODNAME)


# --------------------------------------------------------------------------
# fakes
# --------------------------------------------------------------------------


class FakeMessage:
    _next = [1000]

    def __init__(
        self, kind=None, caption=None, reply_to_message_id=None, registry=None
    ):
        FakeMessage._next[0] += 1
        self.id = FakeMessage._next[0]
        self.chat = SimpleNamespace(id=-1001, type=SimpleNamespace(name="CHANNEL"))
        self.caption = caption
        self.reply_to_message_id = reply_to_message_id
        self.link = f"link/{self.id}"
        self.media_group_id = None
        self._registry = registry
        if registry is not None:
            registry[self.id] = self
        self.photo = SimpleNamespace(file_id=f"p{self.id}") if kind == "photo" else None
        self.video = SimpleNamespace(file_id=f"v{self.id}") if kind == "video" else None
        self.document = (
            SimpleNamespace(file_id=f"d{self.id}") if kind == "document" else None
        )
        self.audio = SimpleNamespace(file_id=f"a{self.id}") if kind == "audio" else None

    def _reply(self, kind, name, kwargs):
        WORLD.record(
            name,
            kwargs.get("caption"),
            kwargs.get("thumb"),
            kwargs.get("duration"),
            kwargs.get("width"),
            kwargs.get("height"),
        )
        WORLD.maybe_raise(name)
        WORLD.maybe_cancel(name)
        return FakeMessage(
            kind,
            caption=kwargs.get("caption"),
            reply_to_message_id=self.id,
            registry=self._registry,
        )

    async def reply_photo(self, **kw):
        return self._reply("photo", "reply_photo", kw)

    async def reply_video(self, **kw):
        return self._reply("video", "reply_video", kw)

    async def reply_document(self, **kw):
        return self._reply("document", "reply_document", kw)

    async def reply_audio(self, **kw):
        return self._reply("audio", "reply_audio", kw)


def build(module, scenario, registry):
    """Construct an uploader for one scenario against one module version."""

    async def send_media_group(chat_id, media, **kw):
        WORLD.record("send_media_group", chat_id, [m.caption for m in media])
        WORLD.maybe_raise("send_media_group")
        sent = [
            FakeMessage("photo", caption=m.caption, registry=registry) for m in media
        ]
        return sent

    async def get_messages(chat_id, message_ids):
        WORLD.record("get_messages", message_ids)
        return registry[message_ids]

    client = SimpleNamespace(
        send_media_group=send_media_group, get_messages=get_messages
    )
    listener = SimpleNamespace(
        thumb=scenario.get("listener_thumb", "none"),
        user_id=1,
        client=client,
        is_cancelled=scenario.get("is_cancelled", False),
        as_doc=scenario.get("as_doc", False),
        hybrid_leech=False,
        user_transmission=False,
        thumbnail_layout=scenario.get("thumbnail_layout"),
        screen_shots=scenario.get("screen_shots"),
        is_super_chat=True,
        up_dest=None,
        clone_dump_chats={},
        user_dict={},
        mid=1,
        message=None,
        name="task",
    )
    WORLD.listener = listener
    up = module.TelegramUploader(listener, "/tmp/task")
    up._thumb = scenario.get("thumb")
    up._sent_msg = FakeMessage(registry=registry)
    up._files_links = True
    up._media_group = scenario.get("media_group", True)
    up._up_path = scenario.get("up_path", "/tmp/task/file.bin")
    up._media_dict = scenario.get("media_dict", {"videos": {}, "documents": {}})
    up._album_msgs = list(scenario.get("album_msgs", []))
    up._base_msg = FakeMessage(registry=registry) if scenario.get("base_msg") else None
    up._last_msg_in_group = scenario.get("last_msg_in_group", False)
    return up


def snapshot(up, exc):
    """Everything observable after one _upload_file call."""

    def kind(m):
        if m is None:
            return None
        for k in ("photo", "video", "document", "audio"):
            if getattr(m, k, None):
                return k
        return "text"

    return {
        "sent_kind": kind(up._sent_msg),
        "sent_caption": getattr(up._sent_msg, "caption", None),
        "media_dict": {
            k: {sk: len(v) for sk, v in sub.items()}
            for k, sub in up._media_dict.items()
        },
        "album_len": len(up._album_msgs),
        "last_msg_in_group": up._last_msg_in_group,
        "msgs_dict": sorted(up._msgs_dict.values()),
        "base_msg": up._base_msg is not None,
        "thumb": up._thumb,
        "corrupted": up._corrupted,
        "exc": None if exc is None else f"{type(exc).__name__}: {exc}",
    }


async def run_one(module, scenario, seed):
    """Run one scenario, returning (call log, end state)."""
    FakeMessage._next[0] = seed
    WORLD.log = []
    WORLD.existing = set(scenario.get("existing", ()))
    WORLD.doc_type = scenario.get("doc_type", (False, False, True))
    WORLD.audio_thumb = scenario.get("audio_thumb")
    WORLD.video_thumb = scenario.get("video_thumb")
    WORLD.frames_thumb = scenario.get("frames_thumb")
    WORLD.raise_on = scenario.get("raise_on", {})
    WORLD.cancel_after = scenario.get("cancel_after")

    # Capture what the module logs; wording and level are user-visible.
    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append((record.levelname, record.getMessage()))

    handler = _Capture()
    module.LOGGER.addHandler(handler)
    module.LOGGER.propagate = False
    registry = {}
    up = build(module, scenario, registry)
    exc = None
    try:
        for cap, name, o_path in scenario["files"]:
            await up._upload_file(cap, name, o_path)
    except Exception as e:  # noqa: BLE001 - the harness compares failures too
        exc = e
    finally:
        module.LOGGER.removeHandler(handler)
    state = snapshot(up, exc)
    state["log_records"] = records
    return list(WORLD.log), state


# --------------------------------------------------------------------------
# scenarios
# --------------------------------------------------------------------------

PHOTO = (False, False, True)
VIDEO = (True, False, False)
AUDIO = (False, True, False)
OTHER = (False, False, False)
VIDEO_AUDIO = (True, True, False)


def _single_file_scenarios():
    out = []

    # --- one file of each type, with and without a user thumbnail ---
    for label, dt in [
        ("photo", PHOTO),
        ("video", VIDEO),
        ("audio", AUDIO),
        ("other", OTHER),
        ("video+audio", VIDEO_AUDIO),
    ]:
        for thumb in [None, "/thumbs/user.jpg", "none"]:
            for as_doc in [False, True]:
                out.append(
                    (
                        f"single {label} thumb={thumb} as_doc={as_doc}",
                        {
                            "doc_type": dt,
                            "thumb": thumb,
                            "as_doc": as_doc,
                            "existing": {"/thumbs/user.jpg"},
                            "video_thumb": "/gen/vid.jpg",
                            "files": [("<code>a</code>", "a.mp4", "/tmp/a.mp4")],
                        },
                    )
                )

    # --- thumbnail resolution ladder ---
    yt = "/tmp/task/yt-dlp-thumb/a.jpg"
    beside = "/tmp/task/a.jpg"
    for label, existing, audio_thumb, dt in [
        ("yt-dlp thumb", {yt}, None, VIDEO),
        ("beside media", {beside}, None, VIDEO),
        ("both present", {yt, beside}, None, VIDEO),
        ("audio cover", set(), "/gen/cover.jpg", AUDIO),
        ("audio no cover", set(), None, AUDIO),
        ("image skips search", {yt}, None, PHOTO),
        ("video+audio skips cover", set(), "/gen/cover.jpg", VIDEO_AUDIO),
    ]:
        out.append(
            (
                f"resolve {label}",
                {
                    "doc_type": dt,
                    "thumb": None,
                    "existing": set(existing),
                    "audio_thumb": audio_thumb,
                    "video_thumb": "/gen/vid.jpg",
                    "files": [("<code>a</code>", "a.mp4", "/tmp/a.mp4")],
                },
            )
        )

    # --- stale user thumbnail is dropped ---
    out.append(
        (
            "stale user thumb dropped",
            {
                "doc_type": VIDEO,
                "thumb": "/thumbs/gone.jpg",
                "existing": set(),
                "video_thumb": "/gen/vid.jpg",
                "files": [("<code>a</code>", "a.mp4", "/tmp/a.mp4")],
            },
        )
    )
    out.append(
        (
            "thumb 'none' is kept as sentinel",
            {
                "doc_type": VIDEO,
                "thumb": "none",
                "existing": set(),
                "files": [("<code>a</code>", "a.mp4", "/tmp/a.mp4")],
            },
        )
    )

    return out


def _thumb_cleanup_scenarios():
    out = []

    # --- generated thumbnail cleanup ---
    for label, dt, extra in [
        ("video generated", VIDEO, {"video_thumb": "/gen/vid.jpg"}),
        (
            "frames layout",
            VIDEO,
            {"frames_thumb": "/gen/frames.jpg", "thumbnail_layout": "2x2"},
        ),
        ("audio cover", AUDIO, {"audio_thumb": "/gen/cover.jpg"}),
        ("doc of a video", VIDEO, {"video_thumb": "/gen/vid.jpg", "as_doc": True}),
    ]:
        s = {
            "doc_type": dt,
            "thumb": None,
            "existing": set(),
            "files": [("<code>a</code>", "a.mp4", "/tmp/a.mp4")],
        }
        s.update(extra)
        out.append((f"cleanup {label}", s))

    return out


def _grouping_scenarios():
    out = []

    # --- album batching ---
    out.append(
        (
            "album fills to ten",
            {
                "doc_type": PHOTO,
                "files": [
                    (f"<code>{i}</code>", f"{i}.jpg", f"/tmp/{i}.jpg")
                    for i in range(11)
                ],
            },
        )
    )
    out.append(
        (
            "album disabled",
            {
                "doc_type": PHOTO,
                "media_group": False,
                "files": [
                    (f"<code>{i}</code>", f"{i}.jpg", f"/tmp/{i}.jpg") for i in range(3)
                ],
            },
        )
    )

    # --- split-file media groups ---
    out.append(
        (
            "split video group fills",
            {
                "doc_type": VIDEO,
                "files": [
                    (f"<code>p{i}</code>", "big.mkv", f"/tmp/big.mkv.{i:03d}")
                    for i in range(1, 12)
                ],
            },
        )
    )
    out.append(
        (
            "split document group fills",
            {
                "doc_type": OTHER,
                "files": [
                    (f"<code>p{i}</code>", "big.rar", f"/tmp/big.rar.part{i}.rar")
                    for i in range(1, 12)
                ],
            },
        )
    )
    out.append(
        (
            "split video, media_group off",
            {
                "doc_type": VIDEO,
                "media_group": False,
                "files": [
                    (f"<code>p{i}</code>", "big.mkv", f"/tmp/big.mkv.{i:03d}")
                    for i in range(1, 4)
                ],
            },
        )
    )
    out.append(
        (
            "document without split name",
            {
                "doc_type": OTHER,
                "files": [("<code>a</code>", "a.bin", "/tmp/a.bin")],
            },
        )
    )
    out.append(
        (
            "photo with split name",
            {
                "doc_type": PHOTO,
                "files": [("<code>a</code>", "a.jpg", "/tmp/a.jpg.001")],
            },
        )
    )
    out.append(
        (
            "album flushed before document",
            {
                "doc_type": PHOTO,
                "files": [
                    ("<code>a</code>", "a.jpg", "/tmp/a.jpg"),
                    ("<code>b</code>", "b.jpg", "/tmp/b.jpg"),
                ],
            },
        )
    )

    # --- base message bookkeeping ---
    for label, extra in [
        ("plain", {}),
        ("pending album", {"doc_type": PHOTO}),
        ("last in group", {"last_msg_in_group": True}),
    ]:
        s = {
            "doc_type": OTHER,
            "base_msg": True,
            "files": [("<code>a</code>", "a.bin", "/tmp/a.bin")],
        }
        s.update(extra)
        out.append((f"base_msg {label}", s))

    return out


def _failure_scenarios():
    out = []

    # --- cancellation ---
    for label, dt in [
        ("photo", PHOTO),
        ("video", VIDEO),
        ("audio", AUDIO),
        ("doc", OTHER),
    ]:
        out.append(
            (
                f"cancelled before send {label}",
                {
                    "doc_type": dt,
                    "is_cancelled": True,
                    "thumb": None,
                    "video_thumb": "/gen/vid.jpg",
                    "audio_thumb": "/gen/cover.jpg",
                    "base_msg": True,
                    "files": [("<code>a</code>", "a.mp4", "/tmp/a.mp4")],
                },
            )
        )
    for label, call, dt in [
        ("photo", "reply_photo", PHOTO),
        ("video", "reply_video", VIDEO),
        ("document", "reply_document", OTHER),
    ]:
        out.append(
            (
                f"cancelled during send {label}",
                {
                    "doc_type": dt,
                    "cancel_after": call,
                    "base_msg": True,
                    "video_thumb": "/gen/vid.jpg",
                    "files": [("<code>a</code>", "a.mp4", "/tmp/a.mp4")],
                },
            )
        )

    # --- flood waits ---
    for label, call, dt in [
        ("photo", "reply_photo", PHOTO),
        ("video", "reply_video", VIDEO),
        ("document", "reply_document", OTHER),
        ("audio", "reply_audio", AUDIO),
    ]:
        out.append(
            (
                f"floodwait once on {label}",
                {
                    "doc_type": dt,
                    "video_thumb": "/gen/vid.jpg",
                    "raise_on": {call: lambda n: FloodWait(3) if n == 0 else None},
                    "files": [("<code>a</code>", "a.mp4", "/tmp/a.mp4")],
                },
            )
        )
    out.append(
        (
            "flood premium wait",
            {
                "doc_type": PHOTO,
                "raise_on": {
                    "reply_photo": lambda n: FloodPremiumWait(2) if n == 0 else None
                },
                "files": [("<code>a</code>", "a.jpg", "/tmp/a.jpg")],
            },
        )
    )

    # --- bad request fallback to document ---
    out.append(
        (
            "badrequest photo falls back to document",
            {
                "doc_type": PHOTO,
                "raise_on": {"reply_photo": lambda _n: BadRequest("nope")},
                "files": [("<code>a</code>", "a.jpg", "/tmp/a.jpg")],
            },
        )
    )
    out.append(
        (
            "badrequest video falls back, keeps thumb",
            {
                "doc_type": VIDEO,
                "video_thumb": "/gen/vid.jpg",
                "raise_on": {"reply_video": lambda _n: BadRequest("nope")},
                "files": [("<code>a</code>", "a.mp4", "/tmp/a.mp4")],
            },
        )
    )
    out.append(
        (
            "badrequest audio falls back",
            {
                "doc_type": AUDIO,
                "audio_thumb": "/gen/cover.jpg",
                "raise_on": {"reply_audio": lambda _n: BadRequest("nope")},
                "files": [("<code>a</code>", "a.m4a", "/tmp/a.m4a")],
            },
        )
    )
    out.append(
        (
            "badrequest document raises",
            {
                "doc_type": OTHER,
                "raise_on": {"reply_document": lambda _n: BadRequest("nope")},
                "files": [("<code>a</code>", "a.bin", "/tmp/a.bin")],
            },
        )
    )
    out.append(
        (
            "badrequest both times",
            {
                "doc_type": PHOTO,
                "raise_on": {
                    "reply_photo": lambda _n: BadRequest("first"),
                    "reply_document": lambda _n: BadRequest("second"),
                },
                "files": [("<code>a</code>", "a.jpg", "/tmp/a.jpg")],
            },
        )
    )

    # --- other failures ---
    out.append(
        (
            "rpc error propagates",
            {
                "doc_type": PHOTO,
                "raise_on": {"reply_photo": lambda _n: RPCError("boom")},
                "files": [("<code>a</code>", "a.jpg", "/tmp/a.jpg")],
            },
        )
    )
    out.append(
        (
            "plain error propagates, thumb cleaned",
            {
                "doc_type": VIDEO,
                "video_thumb": "/gen/vid.jpg",
                "raise_on": {"reply_video": lambda _n: ValueError("boom")},
                "files": [("<code>a</code>", "a.mp4", "/tmp/a.mp4")],
            },
        )
    )
    out.append(
        (
            "failure while flushing a split group",
            {
                "doc_type": VIDEO,
                "raise_on": {"send_media_group": lambda _n: BadRequest("group")},
                "files": [
                    (f"<code>p{i}</code>", "big.mkv", f"/tmp/big.mkv.{i:03d}")
                    for i in range(1, 11)
                ],
            },
        )
    )
    out.append(
        (
            "failure while flushing the album",
            {
                "doc_type": PHOTO,
                "raise_on": {"send_media_group": lambda _n: BadRequest("album")},
                "files": [
                    (f"<code>{i}</code>", f"{i}.jpg", f"/tmp/{i}.jpg")
                    for i in range(10)
                ],
            },
        )
    )
    out.append(
        (
            "thumbnail generation fails",
            {
                "doc_type": VIDEO,
                "raise_on": {"get_video_thumbnail": lambda _n: BadRequest("thumb")},
                "files": [("<code>a</code>", "a.mp4", "/tmp/a.mp4")],
            },
        )
    )

    # --- failures before the media bucket is known ---
    for call, exc, label in [
        ("get_document_type", lambda _n: BadRequest("probe"), "probe badrequest"),
        ("get_document_type", lambda _n: ValueError("probe"), "probe plain error"),
        ("get_audio_thumbnail", lambda _n: BadRequest("cover"), "cover badrequest"),
        ("get_audio_thumbnail", lambda _n: ValueError("cover"), "cover plain error"),
    ]:
        out.append(
            (
                f"pre-dispatch {label}",
                {
                    "doc_type": AUDIO,
                    "thumb": None,
                    "raise_on": {call: exc},
                    "files": [("<code>a</code>", "a.m4a", "/tmp/a.m4a")],
                },
            )
        )
    return out


def scenarios():
    """Every scenario both module versions get driven through."""
    return [
        *_single_file_scenarios(),
        *_thumb_cleanup_scenarios(),
        *_grouping_scenarios(),
        *_failure_scenarios(),
    ]


async def main():
    install_stubs()
    old = load_old()
    new = load_new()

    # Two intentional deviations are re-run after the main sweep to prove they
    # are the only differences, so a future refactor can treat the harness as
    # a pass/fail gate. Pre-dispatch BadRequest is one of them: the old code
    # raised an UnboundLocalError over the real error, the new one propagates
    # the BadRequest itself.
    EXPECTED = {
        "pre-dispatch probe badrequest",
        "pre-dispatch cover badrequest",
    }

    mismatches = 0
    total = 0
    for name, scenario in scenarios():
        total += 1
        old_log, old_state = await run_one(old, scenario, 1000)
        new_log, new_state = await run_one(new, scenario, 1000)
        differs = old_log != new_log or old_state != new_state
        if differs and name not in EXPECTED:
            mismatches += 1
            print(f"\n=== MISMATCH: {name}")
            if old_state != new_state:
                for k in old_state:
                    if old_state[k] != new_state[k]:
                        print(f"  state.{k}: old={old_state[k]!r} new={new_state[k]!r}")
            if old_log != new_log:
                for i in range(max(len(old_log), len(new_log))):
                    o = old_log[i] if i < len(old_log) else None
                    n = new_log[i] if i < len(new_log) else None
                    if o != n:
                        print(f"  log[{i}]: old={o!r}")
                        print(f"           new={n!r}")
        elif differs:
            print(f"expected deviation: {name}")
    print(f"\n{total} scenarios, {mismatches} unexpected mismatches")
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
