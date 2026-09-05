"""Differential harness: the uploader's media grouping before vs after Fase 11b.

Loads the pre-refactor `telegram_uploader.py` out of git alongside the working
tree version, drives both through the same scripted uploads and flushes, and
compares four things:

1. **Telegram calls** — which client method was called, with which arguments, in
   which order, including every `delete_message`. Whether a bucket went out as
   one album, and which messages it replaced, is visible here and nowhere else.
2. **Log lines** — the skipped-album notice and the swallowed flush errors are
   the only trace those two paths leave.
3. **State** — the pending buckets, the pending album, the hold flag, the links
   dict, the reply anchor, and the clone-dump bookkeeping.
4. **Outcome** — the return value or the exception each operation ended with.
   `where=""` raising while a named `where` swallows is a difference here.

The batching state moved from the uploader onto a collaborator, so both versions
are read through the small bridge below rather than by attribute name -- that
relocation is the point of the phase, and what the state holds must be identical
either way.

Run from the repo root: `python tools/_phase11b_diff.py`
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TARGET = "bot/helper/mirror_leech_utils/upload_utils/telegram_uploader.py"
PKG = "bot.helper.mirror_leech_utils.upload_utils"
NEW_NAME = f"{PKG}.telegram_uploader"
BATCHER_NAME = f"{PKG}.media_group_batcher"
PACER_NAME = f"{PKG}.flood_pacer"
OLD_NAME = f"{PKG}._old_telegram_uploader"


# --------------------------------------------------------------------------
# the cage both versions are imported into
# --------------------------------------------------------------------------


class _Err(Exception):
    pass


class Flood(_Err):
    """Base of both flood errors, as in pyrogram (they are siblings there)."""

    def __init__(self, value=1):
        super().__init__(f"A wait of {value} seconds is required")
        self.value = value


class FloodWait(Flood):
    pass


class FloodPremiumWait(Flood):
    pass


class RPCError(_Err):
    pass


class BadRequest(_Err):
    pass


class _InputMedia:
    def __init__(self, media=None, caption=None, **kwargs):
        self.media = media
        self.caption = caption

    def __repr__(self):
        return f"{type(self).__name__}({self.media},{self.caption})"


class _ReplyParameters:
    def __init__(self, message_id=None, **kwargs):
        self.message_id = message_id

    def __repr__(self):
        return f"ReplyParameters({self.message_id})"


def _stub(name, **attrs):
    mod = ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


def _pkg(name, path=None):
    mod = ModuleType(name)
    mod.__path__ = [] if path is None else [path]
    return mod


async def _false(*_a, **_k):
    return False


async def _none(*_a, **_k):
    return None


async def _media_info(*_a, **_k):
    return (10, "artist", "title")


CONFIG = SimpleNamespace(MEDIA_GROUP=False, LEECH_FILENAME_PREFIX="", FILES_LINKS=False)


def _install_stubs():
    """Put both versions behind the same fakes, so only their code differs."""
    up = ROOT / "bot" / "helper" / "mirror_leech_utils" / "upload_utils"
    aiofiles_os = _stub(
        "aiofiles.os",
        remove=_none,
        rename=_none,
        path=SimpleNamespace(exists=_false, isfile=_false, getsize=_none),
    )

    def _passthrough(*_a, **_k):
        return lambda func: func

    modules = {
        "PIL": _stub("PIL", Image=SimpleNamespace(open=lambda *_a, **_k: None)),
        "aioshutil": _stub("aioshutil", rmtree=_none),
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
            InputMediaVideo=type("InputMediaVideo", (_InputMedia,), {}),
            InputMediaDocument=type("InputMediaDocument", (_InputMedia,), {}),
            InputMediaPhoto=type("InputMediaPhoto", (_InputMedia,), {}),
            ReplyParameters=_ReplyParameters,
        ),
        "bot": _stub("bot", intervals={"stopAll": False}),
        "bot.core": _pkg("bot.core"),
        "bot.core.config_manager": _stub("bot.core.config_manager", Config=CONFIG),
        "bot.core.telegram_manager": _stub(
            "bot.core.telegram_manager", TgClient=SimpleNamespace()
        ),
        "bot.helper": _pkg("bot.helper"),
        "bot.helper.ext_utils": _pkg("bot.helper.ext_utils"),
        "bot.helper.ext_utils.bot_utils": _stub(
            "bot.helper.ext_utils.bot_utils", sync_to_async=_none
        ),
        "bot.helper.ext_utils.files_utils": _stub(
            "bot.helper.ext_utils.files_utils",
            is_archive=lambda _p: False,
            get_base_name=lambda p: p,
        ),
        "bot.helper.ext_utils.media_utils": _stub(
            "bot.helper.ext_utils.media_utils",
            get_media_info=_media_info,
            get_document_type=_none,
            get_video_thumbnail=_none,
            get_audio_thumbnail=_none,
            get_multiple_frames_thumbnail=_none,
        ),
        "bot.helper.telegram_helper": _pkg("bot.helper.telegram_helper"),
        "bot.helper.telegram_helper.message_utils": _stub(
            "bot.helper.telegram_helper.message_utils", delete_message=_none
        ),
        "bot.helper.mirror_leech_utils": _pkg("bot.helper.mirror_leech_utils"),
        PKG: _pkg(PKG, str(up)),
    }
    modules["bot"].__path__ = []
    sys.modules.update(modules)


def _load_old():
    """The pre-refactor uploader, imported as a sibling inside the package."""
    src = subprocess.run(
        ["git", "show", f"HEAD:{TARGET}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    spec = importlib.util.spec_from_loader(OLD_NAME, loader=None, origin=TARGET)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = str(ROOT / TARGET)
    mod.__package__ = PKG
    sys.modules[OLD_NAME] = mod
    exec(compile(src, TARGET, "exec"), mod.__dict__)
    return mod


_install_stubs()
new_mod = importlib.import_module(NEW_NAME)
old_mod = _load_old()
# The batcher only exists on the new side; before the extraction there is
# nothing to patch, so this stays None and every loop over it is a no-op.
batcher_mod = sys.modules.get(BATCHER_NAME)
SIDE_MODULES = [m for m in (sys.modules.get(PACER_NAME), batcher_mod) if m]


# --------------------------------------------------------------------------
# the world one scenario runs in
# --------------------------------------------------------------------------

KINDS = {
    "video": (True, False, False),
    "audio": (False, True, False),
    "photo": (False, False, True),
    "document": (False, False, False),
}


class FakeMessage:
    _next_id = 500

    def __init__(self, kind=None, caption=None, registry=None, reply_to=None):
        FakeMessage._next_id += 1
        self.id = FakeMessage._next_id
        self.chat = SimpleNamespace(
            id=-1001, type=SimpleNamespace(name="CHANNEL"), title="dest"
        )
        self.caption = caption
        self.message_thread_id = None
        self.reply_to_message_id = reply_to
        self.link = f"https://t.me/c/1001/{self.id}"
        self.media_group_id = None
        self.photo = SimpleNamespace(file_id=f"p{self.id}") if kind == "photo" else None
        self.video = SimpleNamespace(file_id=f"v{self.id}") if kind == "video" else None
        self.document = (
            SimpleNamespace(file_id=f"d{self.id}") if kind == "document" else None
        )
        self.audio = SimpleNamespace(file_id=f"a{self.id}") if kind == "audio" else None
        if registry is not None:
            registry[self.id] = self

    def __repr__(self):
        return f"Msg({self.id})"

    # Both versions build their own messages, so the logs are only comparable
    # if a message counts as the one the other version made in its place. The
    # ids come from a counter reset before each run, which means an extra or
    # missing send still shows up as a mismatch.
    def __eq__(self, other):
        return isinstance(other, FakeMessage) and other.id == self.id

    def __hash__(self):
        return hash(self.id)


def _render(value):
    """A repr that does not give away which of the two versions produced it.

    `progress=self._upload_progress` is a bound method, so its repr carries the
    defining module and the object address -- both differ between the two
    uploaders by construction. The name is the part that matters.
    """
    if callable(value) and not isinstance(value, type):
        return f"<callable {getattr(value, '__name__', type(value).__name__)}>"
    return repr(value)


def _sorted(kwargs):
    """kwargs as a stable tuple, with unhashable values rendered."""
    return tuple(sorted((k, _render(v)) for k, v in kwargs.items()))


class FakeClient:
    """A telegram client that logs its calls and hands back real-ish messages.

    *script* maps a method name to answers consumed in order: a `BaseException`
    is raised, `None` is returned as-is -- which is how the "group did not go
    through" branch is reached without cancelling -- and anything else falls
    through to the normal behaviour.
    """

    def __init__(self, log, registry, label, script, resolve_as, sent_as):
        self._log = log
        self._registry = registry
        self._label = label
        self._script = script
        self._resolve_as = resolve_as
        self._sent_as = sent_as

    def _record(self, name, kwargs):
        self._log.append((f"{self._label}.{name}", (), _sorted(kwargs)))

    def _scripted(self, name):
        """(handled, answer) for the next scripted answer to *name*."""
        queue = self._script.get(name)
        if not queue:
            return False, None
        answer = queue.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        if answer is None:
            return True, None
        return False, None

    def _send(self, kind, name, caption, reply_parameters, kwargs):
        self._record(name, kwargs)
        handled, answer = self._scripted(name)
        if handled:
            return answer
        if self._sent_as:
            # telegram sniffing the file and deciding it is something other than
            # what it was sent as. This is how the probe's guess and the bucket
            # end up disagreeing, and it is not hypothetical: anything ffprobe
            # cannot read is sent as a document and may come back a video.
            kind = self._sent_as.pop(0) or kind
        return FakeMessage(
            kind,
            caption=caption,
            registry=self._registry,
            reply_to=getattr(reply_parameters, "message_id", None),
        )

    async def send_photo(self, reply_parameters=None, caption=None, **kwargs):
        return self._send("photo", "send_photo", caption, reply_parameters, kwargs)

    async def send_video(self, reply_parameters=None, caption=None, **kwargs):
        return self._send("video", "send_video", caption, reply_parameters, kwargs)

    async def send_document(self, reply_parameters=None, caption=None, **kwargs):
        return self._send(
            "document", "send_document", caption, reply_parameters, kwargs
        )

    async def send_audio(self, reply_parameters=None, caption=None, **kwargs):
        return self._send("audio", "send_audio", caption, reply_parameters, kwargs)

    async def send_message(self, text=None, **kwargs):
        self._record("send_message", {"text": text, **kwargs})
        handled, answer = self._scripted("send_message")
        if handled:
            return answer
        return FakeMessage(registry=self._registry)

    async def send_media_group(self, media=None, **kwargs):
        self._record("send_media_group", {"media": list(media or []), **kwargs})
        handled, answer = self._scripted("send_media_group")
        if handled:
            return answer
        sent = [
            FakeMessage("photo", caption=m.caption, registry=self._registry)
            for m in media
        ]
        for msg in sent:
            msg.media_group_id = "album"
        return sent

    async def get_messages(self, chat_id=None, message_ids=None, **kwargs):
        self._record("get_messages", {"chat_id": chat_id, "message_ids": message_ids})
        handled, answer = self._scripted("get_messages")
        if handled:
            return answer
        msg = self._registry.get(message_ids)
        if self._resolve_as:
            kind = self._resolve_as.pop(0)
            if kind != "same":
                # telegram handing back a different media type than went out
                return FakeMessage(
                    kind,
                    caption=None if msg is None else msg.caption,
                    registry=self._registry,
                )
        return msg

    async def copy_media_group(self, **kwargs):
        self._record("copy_media_group", kwargs)
        handled, answer = self._scripted("copy_media_group")
        if handled:
            return answer
        return [FakeMessage(registry=self._registry)]


def _listener(scenario, log, registry, bot_client):
    """The listener the uploader reads its task-wide settings off."""
    listener = SimpleNamespace(
        thumb="none",
        user_id=7,
        client=bot_client,
        is_cancelled=False,
        as_doc=scenario.get("as_doc", False),
        hybrid_leech=scenario.get("hybrid_leech", False),
        user_transmission=scenario.get("user_session", False),
        thumbnail_layout=None,
        screen_shots=None,
        is_super_chat=scenario.get("is_super_chat", True),
        up_dest=scenario.get("up_dest"),
        chat_thread_id=None,
        cmd_text="/leech link",
        name="task",
        # Deep, not shallow: the dumps bookkeeping writes `last_sent_msg` back
        # into these inner dicts, so a shared copy would hand the second
        # version the first one's leftovers.
        clone_dump_chats={
            chat: dict(cfg)
            for chat, cfg in scenario.get("clone_dump_chats", {}).items()
        },
        user_dict=dict(scenario.get("user_dict", {})),
        mid=1,
        message=FakeMessage(registry=registry),
    )

    async def on_upload_error(err):
        log.append(("on_upload_error", (err,), ()))

    async def on_upload_complete(link, msgs, total, corrupted):
        log.append(("on_upload_complete", (total, corrupted), ()))

    listener.on_upload_error = on_upload_error
    listener.on_upload_complete = on_upload_complete
    return listener


def _build(mod, scenario, log, sleeps):
    """One uploader from *mod*, wired to the scenario's fake clients."""
    registry = {}
    script = {name: list(q) for name, q in scenario.get("script", {}).items()}
    resolve_as = list(scenario.get("resolve_as", []))
    sent_as = list(scenario.get("sent_as", []))
    bot_client = FakeClient(log, registry, "bot", script, resolve_as, sent_as)
    user_client = FakeClient(log, registry, "user", script, resolve_as, sent_as)
    mod.TgClient.user = user_client
    mod.TgClient.bot = bot_client

    CONFIG.MEDIA_GROUP = scenario.get("config_media_group", False)
    CONFIG.FILES_LINKS = scenario.get("config_files_links", False)
    CONFIG.LEECH_FILENAME_PREFIX = ""

    listener = _listener(scenario, log, registry, bot_client)
    uploader = mod.TelegramUploader(listener, "/tmp/task")
    uploader._thumb = None
    uploader._sent_msg = FakeMessage(registry=registry)
    uploader._files_links = scenario.get("files_links", True)
    _set_enabled(uploader, scenario.get("media_group", True))
    if scenario.get("base_msg"):
        uploader._base_msg = FakeMessage(registry=registry)

    # The probe answer changes per upload, so it reads a box the op writes.
    kind_box = {"kind": "photo"}

    async def get_document_type(_path):
        return KINDS[kind_box["kind"]]

    async def record_sleep(duration):
        sleeps.append(round(duration, 6))

    async def delete_message(msg):
        log.append(("delete_message", (msg,), ()))

    for target in (mod, *SIDE_MODULES):
        if hasattr(target, "sleep"):
            target.sleep = record_sleep
        if hasattr(target, "delete_message"):
            target.delete_message = delete_message
        if hasattr(target, "get_document_type"):
            target.get_document_type = get_document_type
    return uploader, kind_box, registry


# --------------------------------------------------------------------------
# the bridge: the same batching state, wherever this version keeps it
# --------------------------------------------------------------------------


def _batcher(uploader):
    return getattr(uploader, "_batcher", None)


def _set_enabled(uploader, value):
    batcher = _batcher(uploader)
    if batcher is None:
        uploader._media_group = value
    else:
        batcher.enabled = value


def _enabled(uploader):
    batcher = _batcher(uploader)
    return uploader._media_group if batcher is None else batcher.enabled


def _holding(uploader):
    batcher = _batcher(uploader)
    return uploader._last_msg_in_group if batcher is None else batcher._holding


def _buckets(uploader):
    holder = _batcher(uploader) or uploader
    return holder._media_dict


def _album(uploader):
    holder = _batcher(uploader) or uploader
    return holder._album_msgs


async def _send_album(uploader):
    batcher = _batcher(uploader)
    if batcher is None:
        return await uploader._send_album()
    return await batcher.send_album()


async def _flush(uploader, where=""):
    batcher = _batcher(uploader)
    if batcher is None:
        return await uploader._flush_media_groups(where)
    return await batcher.flush(where)


async def _track(uploader, o_path, attempt):
    """The effective bucket key after filing the message that was just sent.

    The old version stamped `attempt.key` itself; the new one splits the pure
    decision out of the filing, and `_send_one` stamps from that. Both are read
    the same way here, so a change in *which* key comes out still fails the
    gate -- and because the new stamp happens before the filing, just as the old
    one did, a group send that fails still leaves the same key behind.
    """
    batcher = _batcher(uploader)
    if batcher is None:
        await uploader._track_media_group(o_path, attempt)
        return attempt.key
    attempt.key = batcher.classify(o_path) or attempt.key
    await batcher.track(o_path)
    return attempt.key


async def _queue(uploader, key, pname):
    batcher = _batcher(uploader)
    if batcher is None:
        return await uploader._queue_in_group(key, pname)
    return await batcher._queue(key, pname)


# --------------------------------------------------------------------------
# the operations a scenario can script
# --------------------------------------------------------------------------


async def _op_upload(uploader, op, log):
    """One file all the way through: send, then file it into group or album."""
    kind, name = op[1], op[2]
    force_document = op[3] if len(op) > 3 else False
    uploader._kind_box["kind"] = kind
    uploader._up_path = f"/tmp/task/{name}"
    result = await uploader._upload_file(
        f"<code>{name}</code>", name, f"/tmp/task/{name}", force_document
    )
    log.append(("upload_file", (result, repr(uploader._sent_msg)), ()))


async def _op_upload_one(uploader, op, log):
    """The per-file preamble, which is where a stale hold is resolved.

    `_upload_file` and `_prepare_file` are replaced: neither is part of this
    phase, and both would drag the whole send path into a scenario that is
    only about whether the pending group went out before this file. They are
    put back afterwards so a scenario can go on uploading for real.
    """
    f_path = op[1]

    async def prepare(file_, dirpath):
        return f"<code>{file_}</code>"

    async def upload_file(cap, file_, o_path, force_document=False):
        log.append(("upload_file_called", (cap, o_path), ()))

    uploader._prepare_file = prepare
    uploader._upload_file = upload_file
    uploader._up_path = f_path
    try:
        await uploader._upload_one(f_path.rsplit("/", 1)[-1], "/tmp/task", f_path)
    finally:
        del uploader._prepare_file
        del uploader._upload_file
    log.append(("holding_after", (_holding(uploader),), ()))


async def _op_send_album(uploader, op, log):
    log.append(("send_album", (await _send_album(uploader),), ()))


async def _op_flush(uploader, op, log):
    where = op[1] if len(op) > 1 else ""
    log.append(("flush", (await _flush(uploader, where),), ()))


async def _op_track(uploader, op, log):
    """`_track_media_group` on its own, with the anchor set to *kind*."""
    o_path, kind = op[1], op[2]
    preset = op[3] if len(op) > 3 else None
    uploader._sent_msg = FakeMessage(kind, caption=o_path, registry=uploader._registry)
    attempt = SimpleNamespace(key=preset, thumb=None, is_video=False, aborted=False)
    log.append(("track_key", (await _track(uploader, o_path, attempt),), ()))


async def _op_queue(uploader, op, log):
    key, pname, kind = op[1], op[2], op[3]
    uploader._sent_msg = FakeMessage(kind, caption=pname, registry=uploader._registry)
    await _queue(uploader, key, pname)


async def _op_send_one(uploader, op, log):
    """`_send_one` alone, so the bucket key it settles on is visible.

    That key is what a failed send is judged on, and it is settled in two
    places -- `_pick_key` from the probe, then whatever the batcher filed the
    message under. Driving the whole of `_upload_file` hides it.
    """
    kind, name = op[1], op[2]
    force_document = op[3] if len(op) > 3 else False
    uploader._kind_box["kind"] = kind
    uploader._up_path = f"/tmp/task/{name}"
    attempt = uploader._attempt_cls(None)
    sent = await uploader._send_one(
        f"<code>{name}</code>", name, f"/tmp/task/{name}", force_document, attempt
    )
    log.append(("send_one", (sent, attempt.key), ()))


async def _op_cancel(uploader, op, log):
    uploader._listener.is_cancelled = True


async def _op_user_settings(uploader, op, log):
    await uploader._user_settings()
    log.append(("media_group", (_enabled(uploader),), ()))


async def _op_finish(uploader, op, log):
    await uploader._finish(stream=op[1] if len(op) > 1 else False)


async def _op_note_link(uploader, op, log):
    """Book the standalone link of the last send, as `_upload_one` does."""
    uploader._msgs_dict[uploader._sent_msg.link] = op[1]


_OPS = {
    "upload": _op_upload,
    "upload_one": _op_upload_one,
    "send_album": _op_send_album,
    "flush": _op_flush,
    "track": _op_track,
    "queue": _op_queue,
    "send_one": _op_send_one,
    "cancel": _op_cancel,
    "user_settings": _op_user_settings,
    "finish": _op_finish,
    "note_link": _op_note_link,
}


async def _run_op(uploader, op, log):
    handler = _OPS.get(op[0])
    if handler is None:
        raise AssertionError(f"unknown op {op[0]!r}")
    await handler(uploader, op, log)


def run(mod, scenario):
    """Everything one scenario observes about one version."""
    log, sleeps, logged = [], [], []

    for target in (mod, *SIDE_MODULES):
        if not hasattr(target, "LOGGER"):
            continue
        logger = target.LOGGER
        for level in ("warning", "error", "info"):
            setattr(
                logger,
                level,
                lambda m, _lvl=level, *a, **k: logged.append((_lvl, str(m))),
            )

    uploader, kind_box, registry = _build(mod, scenario, log, sleeps)
    uploader._kind_box = kind_box
    uploader._registry = registry
    uploader._attempt_cls = mod._Attempt
    if scenario.get("cancelled_before"):
        uploader._listener.is_cancelled = True

    async def drive():
        for op in scenario["ops"]:
            await _run_op(uploader, op, log)

    try:
        asyncio.run(drive())
        outcome = ("ok", None)
    except Exception as e:  # noqa: BLE001 - comparing failure modes is the point
        outcome = (type(e).__name__, str(e))

    state = (
        repr(_buckets(uploader)),
        repr(_album(uploader)),
        _holding(uploader),
        _enabled(uploader),
        dict(uploader._msgs_dict),
        repr(uploader._sent_msg),
        uploader._base_msg is not None,
        uploader._total_files,
        uploader._corrupted,
        uploader._error,
        {c: dict(d) for c, d in uploader._listener.clone_dump_chats.items()},
    )
    return outcome, tuple(log), tuple(sleeps), tuple(logged), state


# --------------------------------------------------------------------------
# scenarios
# --------------------------------------------------------------------------


def scenarios():
    yield from _album_scenarios()
    yield from _split_group_scenarios()
    yield from _asymmetry_scenarios()
    yield from _flush_scenarios()
    yield from _bookkeeping_scenarios()
    yield from _hold_scenarios()
    yield from _key_scenarios()
    yield from _settings_scenarios()


def _photos(count, prefix="p"):
    return [("upload", "photo", f"{prefix}{i}.jpg") for i in range(count)]


def _album_scenarios():
    """The running album: when it fills, when it waits, what order it keeps."""
    yield "album_nine_photos_wait", {"ops": _photos(9)}
    yield "album_ten_photos_go_out", {"ops": _photos(10)}
    yield "album_twelve_photos", {"ops": _photos(12)}
    yield "album_twenty_photos_two_albums", {"ops": _photos(20)}
    yield (
        "album_photos_and_videos_share_order",
        {
            "ops": [
                ("upload", "photo", "a.jpg"),
                ("upload", "video", "b.mp4"),
                ("upload", "photo", "c.jpg"),
                ("send_album",),
            ]
        },
    )
    yield (
        "album_single_pending_stays_standalone",
        {"ops": [("upload", "photo", "only.jpg"), ("send_album",)]},
    )
    yield "album_empty_send_is_a_noop", {"ops": [("send_album",)]}
    yield (
        "album_flushed_before_a_document",
        {
            "ops": [
                ("upload", "photo", "a.jpg"),
                ("upload", "photo", "b.jpg"),
                ("upload", "document", "c.rar"),
            ]
        },
    )
    yield (
        "album_flushed_before_audio",
        {
            "ops": [
                ("upload", "photo", "a.jpg"),
                ("upload", "photo", "b.jpg"),
                ("upload", "audio", "c.mp3"),
            ]
        },
    )
    yield (
        "album_flushed_before_a_forced_document",
        {
            "ops": [
                ("upload", "photo", "a.jpg"),
                ("upload", "photo", "b.jpg"),
                ("upload", "video", "c.mp4", True),
            ]
        },
    )
    yield (
        "album_not_flushed_before_a_video",
        {
            "ops": [
                ("upload", "photo", "a.jpg"),
                ("upload", "video", "b.mp4"),
            ]
        },
    )
    # as_doc sends everything through send_document, so every file flushes the
    # album it can never join
    yield (
        "album_never_forms_when_as_doc",
        {"as_doc": True, "ops": _photos(4)},
    )


def _split_group_scenarios():
    """Split parts of one file, keyed by the stem they share."""

    def parts(count, stem="movie.mkv", kind="video"):
        return [("upload", kind, f"{stem}.{i:03d}") for i in range(1, count + 1)]

    yield "group_three_video_parts_wait", {"ops": parts(3)}
    yield "group_ten_video_parts_go_out", {"ops": parts(10)}
    yield "group_twelve_video_parts", {"ops": parts(12)}
    yield (
        "group_two_stems_stay_apart",
        {"ops": parts(3, "one.mkv") + parts(3, "two.mkv")},
    )
    # part-style names: `arch.part1.rar` shares the stem `arch`
    yield (
        "group_document_parts",
        {
            "ops": [
                ("upload", "document", f"arch.part{i}.rar") for i in range(1, 4)
            ]
        },
    )
    yield (
        "group_ten_document_parts_go_out",
        {
            "ops": [
                ("upload", "document", f"arch.part{i}.rar") for i in range(1, 11)
            ]
        },
    )
    yield (
        "group_reaches_ten_then_two_more_reopen_it",
        {"ops": parts(12)[:10] + parts(12)[10:]},
    )
    yield (
        "group_and_album_interleaved",
        {
            "ops": [
                ("upload", "video", "movie.mkv.001"),
                ("upload", "photo", "shot.jpg"),
                ("upload", "video", "movie.mkv.002"),
                ("upload", "photo", "shot2.jpg"),
                ("send_album",),
                ("flush", "task"),
            ]
        },
    )
    yield (
        "queue_directly_to_ten",
        {"ops": [("queue", "videos", "stem", "video")] * 10},
    )
    yield (
        "queue_documents_directly",
        {"ops": [("queue", "documents", "stem", "document")] * 10},
    )


def _asymmetry_scenarios():
    """The deliberate lopsidedness of the rules, pinned one by one."""
    # a split-named *photo* does not form a group: the split branch demands a
    # video, so it falls through to the album
    yield (
        "split_named_photos_fall_to_the_album",
        {"ops": [("upload", "photo", f"shot.jpg.{i:03d}") for i in range(1, 4)]},
    )
    yield (
        "split_named_photos_fill_an_album",
        {"ops": [("upload", "photo", f"shot.jpg.{i:03d}") for i in range(1, 11)]},
    )
    # a document never joins the album -- only a group, and only when its name
    # matches the split pattern
    yield (
        "plain_documents_are_never_tracked",
        {"ops": [("upload", "document", f"file{i}.rar") for i in range(4)]},
    )
    yield (
        "plain_audio_is_never_tracked",
        {"ops": [("upload", "audio", f"song{i}.mp3") for i in range(4)]},
    )
    yield (
        "split_named_audio_is_never_tracked",
        {"ops": [("upload", "audio", f"song.mp3.{i:03d}") for i in range(1, 4)]},
    )
    # with grouping off nothing is tracked at all
    yield "grouping_off_photos", {"media_group": False, "ops": _photos(12)}
    yield (
        "grouping_off_split_videos",
        {
            "media_group": False,
            "ops": [("upload", "video", f"m.mkv.{i:03d}") for i in range(1, 12)],
        },
    )
    # `_media_dict` only ever grows a videos and a documents bucket
    yield (
        "track_every_kind_directly",
        {
            "ops": [
                ("track", "m.mkv.001", "video"),
                ("track", "m.mkv.001", "photo"),
                ("track", "m.mkv.001", "document"),
                ("track", "m.mkv.001", "audio"),
                ("track", "plain.mkv", "video"),
                ("track", "plain.jpg", "photo"),
                ("track", "plain.rar", "document"),
                ("track", "plain.mp3", "audio"),
            ]
        },
    )
    yield (
        "track_keeps_the_preset_key_when_it_files_nothing",
        {
            "ops": [
                ("track", "plain.rar", "document", "documents"),
                ("track", "plain.mp3", "audio", "audios"),
                ("track", "plain.jpg", "photo", "photos"),
            ]
        },
    )
    yield (
        "track_does_nothing_once_cancelled",
        {
            "ops": [
                ("cancel",),
                ("track", "m.mkv.001", "video", "videos"),
                ("track", "plain.jpg", "photo", "photos"),
            ]
        },
    )
    # a bucket of one is never sent and never dropped: it outlives the task
    yield (
        "bucket_of_one_survives_every_flush",
        {
            "ops": [
                ("upload", "video", "movie.mkv.001"),
                ("flush",),
                ("flush", "task"),
                ("finish",),
            ]
        },
    )


def _flush_scenarios():
    """Sending what is still buffered, and who gets to see the failure."""
    two_parts = [
        ("upload", "video", "movie.mkv.001"),
        ("upload", "video", "movie.mkv.002"),
    ]
    yield "flush_two_parts", {"ops": two_parts + [("flush",)]}
    yield "flush_two_parts_named", {"ops": two_parts + [("flush", "task")]}
    yield "flush_nothing_pending", {"ops": [("flush",), ("flush", "task")]}
    yield (
        "flush_both_buckets",
        {
            "ops": two_parts
            + [("upload", "document", f"arch.part{i}.rar") for i in range(1, 3)]
            + [("flush", "task")]
        },
    )
    # `where=""` lets the error reach the per-file handler; a named `where`
    # swallows it and logs instead
    yield (
        "flush_error_propagates_when_unnamed",
        {"script": {"send_media_group": [RPCError("no rights")]}, "ops": two_parts
         + [("flush",)]},
    )
    yield (
        "flush_error_swallowed_when_named",
        {"script": {"send_media_group": [RPCError("no rights")]}, "ops": two_parts
         + [("flush", "task")]},
    )
    yield (
        "flush_error_swallowed_per_bucket",
        {
            "script": {"send_media_group": [RPCError("a"), RPCError("b")]},
            "ops": two_parts
            + [("upload", "document", f"arch.part{i}.rar") for i in range(1, 3)]
            + [("flush", "task")],
        },
    )
    # a group that did not go through keeps its bucket
    yield (
        "unsent_group_keeps_its_bucket",
        {"script": {"send_media_group": [None]}, "ops": two_parts + [("flush",)]},
    )
    yield (
        "unsent_group_keeps_its_bucket_then_retries",
        {
            "script": {"send_media_group": [None]},
            "ops": two_parts + [("flush",), ("flush", "task")],
        },
    )
    # cancelled mid-task: the guarded send answers None without calling out
    yield (
        "flush_after_cancel_sends_nothing",
        {"ops": two_parts + [("cancel",), ("flush", "task")]},
    )
    yield (
        "album_after_cancel_sends_nothing",
        {"ops": _photos(3) + [("cancel",), ("send_album",)]},
    )
    yield (
        "flush_floods_then_succeeds",
        {
            "script": {"send_media_group": [FloodWait(2)]},
            "ops": two_parts + [("flush",)],
        },
    )
    # the album is cleared before the send can fail, so a rejected album is
    # simply lost from the bookkeeping
    yield (
        "album_cleared_before_a_failing_send",
        {
            "script": {"send_media_group": [RPCError("too big")]},
            "ops": _photos(3) + [("send_album",)],
        },
    )
    yield (
        "album_skipped_when_telegram_reclassifies",
        {"resolve_as": ["same", "document"], "ops": _photos(2) + [("send_album",)]},
    )
    yield (
        "album_skipped_when_a_message_vanishes",
        {"resolve_as": ["same", "audio"], "ops": _photos(3) + [("send_album",)]},
    )
    yield (
        "finish_flushes_album_then_buckets",
        {"ops": _photos(2) + two_parts + [("finish",)]},
    )
    yield (
        "finish_stream_labels_differently",
        {"ops": _photos(2) + two_parts + [("finish", True)]},
    )
    yield (
        "finish_swallows_a_failing_album",
        {
            "script": {"send_media_group": [RPCError("nope")]},
            "ops": _photos(2) + [("finish",)],
        },
    )
    yield (
        "finish_swallows_a_failing_bucket",
        {
            "script": {"send_media_group": [RPCError("nope")]},
            "ops": two_parts + [("finish",)],
        },
    )
    # a bucket of one is skipped, but that must not stop the buckets after it
    yield (
        "flush_skips_a_lone_bucket_and_sends_the_next",
        {
            "ops": [
                ("upload", "video", "one.mkv.001"),
                ("upload", "video", "two.mkv.001"),
                ("upload", "video", "two.mkv.002"),
                ("flush", "task"),
            ]
        },
    )
    # a full group whose send never landed stays at ten, and the eleventh part
    # does not re-trigger the send: the trigger is `== 10`, not `>= 10`
    yield (
        "group_of_ten_unsent_then_an_eleventh_part",
        {
            "script": {"send_media_group": [None]},
            "ops": [
                ("upload", "video", f"movie.mkv.{i:03d}") for i in range(1, 12)
            ],
        },
    )


def _bookkeeping_scenarios():
    """What a sent group does to the links, the anchor and the dumps."""
    two = _photos(2)
    yield (
        "album_replaces_the_individual_links",
        {
            "ops": [
                ("upload", "photo", "a.jpg"),
                ("note_link", "a.jpg"),
                ("upload", "photo", "b.jpg"),
                ("note_link", "b.jpg"),
                ("send_album",),
            ]
        },
    )
    yield (
        "album_writes_no_links_when_files_links_is_off",
        {
            "files_links": False,
            "ops": [
                ("upload", "photo", "a.jpg"),
                ("note_link", "a.jpg"),
                ("upload", "photo", "b.jpg"),
                ("send_album",),
            ],
        },
    )
    yield (
        "album_writes_no_links_outside_a_channel",
        {"is_super_chat": False, "ops": two + [("send_album",)]},
    )
    yield (
        "album_writes_links_when_up_dest_is_set",
        {"is_super_chat": False, "up_dest": -1009, "ops": two + [("send_album",)]},
    )
    yield (
        "album_reanchors_the_reply_chain",
        {"ops": two + [("send_album",), ("upload", "photo", "next.jpg")]},
    )
    yield (
        "group_reanchors_the_reply_chain",
        {
            "ops": [
                ("upload", "video", "m.mkv.001"),
                ("upload", "video", "m.mkv.002"),
                ("flush",),
                ("upload", "photo", "after.jpg"),
            ]
        },
    )
    dumps = {
        -100: {"last_sent_msg": None, "thread_id": 5},
        -200: {"last_sent_msg": 3, "thread_id": None},
    }
    yield (
        "album_is_copied_to_the_clone_dumps",
        {"clone_dump_chats": dumps, "ops": two + [("send_album",)]},
    )
    yield (
        "clone_dump_failure_is_logged_not_raised",
        {
            "clone_dump_chats": dumps,
            "script": {"copy_media_group": [RPCError("banned")]},
            "ops": two + [("send_album",)],
        },
    )
    yield (
        "base_msg_is_dropped_once_the_album_goes_out",
        {"base_msg": True, "ops": two + [("send_album",)]},
    )
    # `_upload_file` keeps the base message alive while anything is pending
    yield (
        "base_msg_survives_a_pending_album",
        {"base_msg": True, "ops": _photos(1)},
    )
    yield (
        "base_msg_survives_a_held_split_group",
        {"base_msg": True, "ops": [("upload", "video", "m.mkv.001")]},
    )
    yield (
        "base_msg_dropped_when_nothing_is_pending",
        {"base_msg": True, "media_group": False, "ops": _photos(1)},
    )
    yield (
        "base_msg_dropped_after_a_plain_document",
        {"base_msg": True, "ops": [("upload", "document", "file.rar")]},
    )
    # the group client differs from the send client under hybrid leech
    yield (
        "group_client_under_user_session",
        {"user_session": True, "ops": two + [("send_album",)]},
    )
    yield (
        "group_client_under_hybrid_leech",
        {
            "user_session": True,
            "hybrid_leech": True,
            "ops": two + [("send_album",)],
        },
    )
    # a group send that raises BadRequest after the key was stamped: the retry
    # decision reads that key, so where it is stamped is observable
    yield (
        "group_send_bad_request_retries_as_document",
        {
            "script": {"send_media_group": [BadRequest("media invalid")]},
            "ops": [("upload", "video", f"m.mkv.{i:03d}") for i in range(1, 11)],
        },
    )
    yield (
        "album_send_bad_request_retries_as_document",
        {
            "script": {"send_media_group": [BadRequest("media invalid")]},
            "ops": _photos(10),
        },
    )


def _hold_scenarios():
    """`_last_msg_in_group`: the stale hold the next file has to resolve."""
    held = [("upload", "video", "movie.mkv.001")]
    # the next file continues the same split group, so nothing is flushed
    yield (
        "hold_continues_into_the_same_stem",
        {"ops": held + [("upload_one", "/tmp/task/movie.mkv.002")]},
    )
    # ... an unrelated file forces the group out first
    yield (
        "hold_broken_by_an_unrelated_split_name",
        {"ops": held + [("upload_one", "/tmp/task/other.mkv.001")]},
    )
    yield (
        "hold_broken_by_a_plain_name",
        {"ops": held + [("upload_one", "/tmp/task/plain.jpg")]},
    )
    # two parts held, then broken: the group is big enough to go out
    yield (
        "hold_of_two_broken_sends_the_group",
        {
            "ops": [
                ("upload", "video", "movie.mkv.001"),
                ("upload", "video", "movie.mkv.002"),
                ("upload_one", "/tmp/task/plain.jpg"),
            ]
        },
    )
    yield (
        "no_hold_means_no_flush",
        {"ops": [("upload_one", "/tmp/task/plain.jpg")]},
    )
    yield (
        "hold_cleared_even_when_nothing_is_flushed",
        {"ops": held + [("upload_one", "/tmp/task/movie.mkv.002")] * 2},
    )
    # a pending album is not what the hold is about, so it is left alone
    yield (
        "hold_ignores_a_pending_album",
        {"ops": _photos(2) + [("upload_one", "/tmp/task/plain.jpg")]},
    )
    yield (
        "hold_broken_while_a_document_bucket_waits",
        {
            "ops": [
                ("upload", "document", "arch.part1.rar"),
                ("upload", "document", "arch.part2.rar"),
                ("upload_one", "/tmp/task/plain.jpg"),
            ]
        },
    )
    # a held document group is continued by its own stem too, so the stems the
    # hold is checked against come from every bucket, not just the videos
    yield (
        "hold_continues_into_the_same_document_stem",
        {
            "ops": [
                ("upload", "document", "arch.part1.rar"),
                ("upload", "document", "arch.part2.rar"),
                ("upload_one", "/tmp/task/arch.part3.rar"),
            ]
        },
    )
    # a group that fills up leaves no hold behind, even though the part before
    # it did -- the hold in between is settled by an unrelated file
    yield (
        "a_filled_group_leaves_no_hold",
        {
            "ops": [("upload", "video", f"movie.mkv.{i:03d}") for i in range(1, 10)]
            + [
                ("upload_one", "/tmp/task/movie.mkv.010"),
                ("upload", "video", "movie.mkv.010"),
            ]
        },
    )
    yield (
        "hold_flush_error_reaches_the_per_file_handler",
        {
            "script": {"send_media_group": [RPCError("no rights")]},
            "ops": [
                ("upload", "video", "movie.mkv.001"),
                ("upload", "video", "movie.mkv.002"),
                ("upload_one", "/tmp/task/plain.jpg"),
            ],
        },
    )
    yield (
        "hold_after_cancel",
        {"ops": held + [("cancel",), ("upload_one", "/tmp/task/plain.jpg")]},
    )
    # once the hold is cleared the bucket it left behind is nobody's business:
    # a later unrelated file must not flush it, because only a hold does that
    yield (
        "cleared_hold_leaves_the_bucket_alone",
        {
            "ops": [
                ("upload", "video", "movie.mkv.001"),
                ("upload", "video", "movie.mkv.002"),
                ("upload_one", "/tmp/task/movie.mkv.003"),
                ("upload_one", "/tmp/task/plain.jpg"),
            ]
        },
    )


def _key_scenarios():
    """The bucket key `_send_one` settles on, which a failed send is judged by.

    It is written twice -- once from the probe, once from wherever the batcher
    filed the message -- and only the second is this phase's business.
    """
    for kind in ("video", "photo", "document", "audio"):
        yield (
            f"send_one_key_for_a_plain_{kind}",
            {"ops": [("send_one", kind, f"plain.{kind}")]},
        )
        yield (
            f"send_one_key_for_a_split_{kind}",
            {"ops": [("send_one", kind, f"plain.{kind}.001")]},
        )
    yield (
        "send_one_key_when_forced_to_document",
        {"ops": [("send_one", "document", "movie.mkv.001", True)]},
    )
    yield (
        "send_one_key_with_grouping_off",
        {
            "media_group": False,
            "ops": [
                ("send_one", "video", "movie.mkv.001"),
                ("send_one", "document", "arch.part1.rar"),
            ],
        },
    )
    yield (
        "send_one_key_once_cancelled",
        {"ops": [("cancel",), ("send_one", "video", "movie.mkv.001")]},
    )
    # the probe cannot read the file, so it goes out as a document -- and comes
    # back a video, which is the bucket the retry has to be judged on
    yield (
        "send_one_key_when_telegram_reclassifies_a_document",
        {
            "sent_as": ["video"],
            "ops": [("send_one", "document", "movie.mkv.001")],
        },
    )
    yield (
        "send_one_key_when_a_reclassified_document_is_not_a_part",
        {"sent_as": ["video"], "ops": [("send_one", "document", "movie.mkv")]},
    )
    # ... and the key has to be settled *before* the group is sent, because the
    # group send is what fails and the handler reads the key
    yield (
        "reclassified_document_key_survives_a_failing_group_send",
        {
            "sent_as": ["video"] * 24,
            "script": {"send_media_group": [BadRequest("group too big")]},
            "ops": [
                ("upload", "document", f"movie.mkv.{i:03d}") for i in range(1, 11)
            ],
        },
    )


def _settings_scenarios():
    """Where the grouping switch comes from."""
    yield (
        "media_group_from_the_user_dict",
        {"media_group": False, "user_dict": {"MEDIA_GROUP": True}, "ops":
         [("user_settings",)]},
    )
    yield (
        "media_group_off_in_the_user_dict_beats_the_config",
        {
            "media_group": True,
            "config_media_group": True,
            "user_dict": {"MEDIA_GROUP": False},
            "ops": [("user_settings",)],
        },
    )
    yield (
        "media_group_from_the_config",
        {"media_group": False, "config_media_group": True, "ops":
         [("user_settings",)]},
    )
    yield (
        "media_group_off_everywhere",
        {"media_group": True, "ops": [("user_settings",)]},
    )
    yield (
        "settings_then_upload",
        {
            "media_group": False,
            "config_media_group": True,
            "ops": [("user_settings",)] + _photos(3),
        },
    )


# --------------------------------------------------------------------------
# where the batching is called from
# --------------------------------------------------------------------------

# The scenarios reach every batching path except the forced album at the
# screenshot directory boundary, which lives in `upload()` -- a method that
# walks a real directory tree. Comparing the call sites in the source closes
# that gap: a batching call that moved to another method, or vanished, fails the
# gate even though no scenario reaches it.
_OLD_CALLS = {
    "_send_album": "album",
    "_flush_media_groups": "flush",
    "_track_media_group": "track",
    "_send_media_group": "bucket",
    "_send_group": "ship",
    "_queue_in_group": "queue",
    "_get_input_media": "input_media",
}
_NEW_CALLS = {
    "send_album": "album",
    "release_unless_continued": "flush",
    "flush": "flush",
    "track": "track",
}

# The bodies that moved to `media_group_batcher.py`. Their internal calls to
# each other went with them, so the check looks at the uploader's own callers
# only -- everything else in the mapping must match exactly.
_MOVED_OUT = frozenset(_OLD_CALLS)


def _batching_call(node):
    """Which batching operation an AST call node performs, if any."""
    if not isinstance(node.func, ast.Attribute):
        return None
    owner = node.func.value
    if isinstance(owner, ast.Name) and owner.id == "self":
        return _OLD_CALLS.get(node.func.attr)
    if (
        isinstance(owner, ast.Attribute)
        and owner.attr == "_batcher"
        and isinstance(owner.value, ast.Name)
        and owner.value.id == "self"
    ):
        return _NEW_CALLS.get(node.func.attr)
    return None


def _call_sites(src, label):
    """`{method name: sorted batching calls}` for the uploader class in *src*."""
    tree = ast.parse(src, label)
    sites = {}
    for cls in ast.walk(tree):
        if not isinstance(cls, ast.ClassDef) or cls.name != "TelegramUploader":
            continue
        for func in cls.body:
            if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if func.name in _MOVED_OUT:
                continue
            found = [
                kind
                for node in ast.walk(func)
                if isinstance(node, ast.Call) and (kind := _batching_call(node))
            ]
            if found:
                sites[func.name] = sorted(found)
    return sites


def check_call_sites():
    """True when every batching call is still made from the same method."""
    old_src = subprocess.run(
        ["git", "show", f"HEAD:{TARGET}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    old = _call_sites(old_src, "HEAD")
    new = _call_sites((ROOT / TARGET).read_text(), "worktree")
    if old == new:
        total = sum(len(v) for v in old.values())
        print(f"call sites: {total} batching calls across {len(old)} methods, unmoved")
        return True
    print("\n✗ batching calls moved between methods")
    for name in sorted(set(old) | set(new)):
        if old.get(name) != new.get(name):
            print(f"    {name}:")
            print(f"        old={old.get(name)}")
            print(f"        new={new.get(name)}")
    return False


# --------------------------------------------------------------------------


LABELS = ("outcome", "calls", "sleeps", "logs", "state")


def main():
    passed = failed = 0
    for name, scenario in scenarios():
        FakeMessage._next_id = 500
        old = run(old_mod, scenario)
        FakeMessage._next_id = 500
        new = run(new_mod, scenario)
        if old == new:
            passed += 1
            continue
        failed += 1
        print(f"\n✗ {name}")
        for label, o, n in zip(LABELS, old, new, strict=True):
            if o != n:
                print(f"    {label}:\n        old={o!r}\n        new={n!r}")

    print(f"\n{passed} identical, {failed} divergent, {passed + failed} scenarios")
    return 1 if failed or not check_call_sites() else 0


if __name__ == "__main__":
    raise SystemExit(main())
