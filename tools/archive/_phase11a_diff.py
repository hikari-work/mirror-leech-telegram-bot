"""Differential harness: the uploader's flood pacing before vs after Fase 11a.

Loads the pre-refactor `telegram_uploader.py` out of git alongside the working
tree version, drives both through the same scripted sequences of pacing
operations and real uploader entry points, and compares four things:

1. **Sleeps** — the ordered list of durations waited. This is the whole
   observable behaviour of the pacing policy, so a gap that grows or decays
   differently shows up here and nowhere else.
2. **Telegram calls** — which client method was called, with which arguments,
   in which order. A retry that stops retrying, or one that retries too often,
   is a difference in this log.
3. **Log lines** — the `LOGGER.warning` calls, in order. The rate-limit
   notices are the only trace a waited-out flood leaves behind.
4. **State** — the pace and calm counters, plus the anchor bookkeeping that
   `_msg_to_reply` sets, and the return value or exception of the operation.

The pacing state moved from the uploader onto a collaborator, so the two
versions are read through `_snapshot_pace` rather than by attribute name --
that relocation is the point of the phase, and the numbers it holds must be
identical either way.

Run from the repo root: `python tools/_phase11a_diff.py`
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
PACER_NAME = f"{PKG}.flood_pacer"
OLD_NAME = f"{PKG}._old_telegram_uploader"


# --------------------------------------------------------------------------
# the cage both versions are imported into
# --------------------------------------------------------------------------


class _Err(Exception):
    pass


class Flood(_Err):
    """Base of both flood errors, as in pyrogram.

    `FloodPremiumWait` is a *sibling* of `FloodWait` there, not a subclass, so
    catching only one of them really does miss the other. Modelling them as
    parent and child would quietly make that mistake untestable.
    """

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
        "bot.core.config_manager": _stub(
            "bot.core.config_manager",
            Config=SimpleNamespace(
                MEDIA_GROUP=False, LEECH_FILENAME_PREFIX="", FILES_LINKS=False
            ),
        ),
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
            get_media_info=_none,
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
    """The pre-refactor uploader, imported as a sibling inside the package.

    Loading it under the real package name is what makes its relative imports
    resolve exactly like the working-tree version's.
    """
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
pacer_mod = sys.modules[PACER_NAME]
old_mod = _load_old()


# --------------------------------------------------------------------------
# the world one scenario runs in
# --------------------------------------------------------------------------


class FakeMessage:
    _next_id = 500

    def __init__(self, kind=None, caption=None, chat_type="CHANNEL"):
        FakeMessage._next_id += 1
        self.id = FakeMessage._next_id
        self.chat = SimpleNamespace(
            id=-1001, type=SimpleNamespace(name=chat_type), title="dest"
        )
        self.caption = caption
        self.message_thread_id = None
        self.reply_to_message_id = None
        self.link = f"https://t.me/c/1001/{self.id}"
        self.photo = SimpleNamespace(file_id="p") if kind == "photo" else None
        self.video = SimpleNamespace(file_id="v") if kind == "video" else None
        self.document = SimpleNamespace(file_id="d") if kind == "document" else None
        self.audio = SimpleNamespace(file_id="a") if kind == "audio" else None

    def __repr__(self):
        return f"Msg({self.id})"

    # Both versions build their own messages, so the logs are only comparable
    # if a message counts as the one the other version made in its place. The
    # ids are handed out from a counter reset before each run, which means an
    # extra or missing send still shows up as a mismatch.
    def __eq__(self, other):
        return isinstance(other, FakeMessage) and other.id == self.id

    def __hash__(self):
        return hash(self.id)


class Recorder:
    """A telegram client that logs every call and answers from a script.

    Answers are consumed in order; a `BaseException` in the script is raised
    instead of returned, which is how a flood is injected at a chosen attempt.
    """

    def __init__(self, log, answers, label):
        self._log = log
        self._answers = answers
        self._label = label

    def __getattr__(self, name):
        async def call(*args, **kwargs):
            self._log.append((f"{self._label}.{name}", args, _sorted(kwargs)))
            queue = self._answers.get(name)
            answer = queue.pop(0) if queue else None
            if isinstance(answer, BaseException):
                raise answer
            if answer == "MSG":
                return FakeMessage()
            return answer

        return call


def _sorted(kwargs):
    """kwargs as a stable tuple, with unhashable values rendered."""
    return tuple(sorted((k, repr(v)) for k, v in kwargs.items()))


def _build(mod, scenario, log, sleeps):
    """One uploader from *mod*, wired to the scenario's scripted answers."""
    answers = {
        name: list(queue) for name, queue in scenario.get("answers", {}).items()
    }
    bot_client = Recorder(log, answers, "bot")
    user_client = Recorder(log, answers, "user")
    mod.TgClient.user = user_client
    mod.TgClient.bot = bot_client

    listener = SimpleNamespace(
        thumb="none",
        user_id=7,
        client=bot_client,
        is_cancelled=False,
        as_doc=False,
        hybrid_leech=False,
        user_transmission=scenario.get("user_session", False),
        thumbnail_layout=None,
        screen_shots=None,
        is_super_chat=scenario.get("is_super_chat", True),
        up_dest=scenario.get("up_dest"),
        chat_thread_id=scenario.get("chat_thread_id"),
        cmd_text="/leech link",
        cmd_msg_id=99,
        # Deep, not shallow: the dumps bookkeeping writes `last_sent_msg` back
        # into these inner dicts, so a shared copy would hand the second
        # version the first one's leftovers.
        clone_dump_chats={
            chat: dict(cfg)
            for chat, cfg in scenario.get("clone_dump_chats", {}).items()
        },
        user_dict={},
        mid=1,
        message=FakeMessage(),
    )

    async def on_upload_error(err):
        log.append(("on_upload_error", (err,), ()))

    listener.on_upload_error = on_upload_error

    uploader = mod.TelegramUploader(listener, "/tmp/task")
    uploader._thumb = None
    uploader._sent_msg = FakeMessage()

    # Every module that can sleep reports into the same list, so the ordered
    # sequence is comparable no matter which of them owns the wait.
    async def record(duration):
        sleeps.append(round(duration, 6))

    for target in (mod, pacer_mod):
        if hasattr(target, "sleep"):
            target.sleep = record
    return uploader


def _snapshot_pace(uploader):
    """The pace counters, wherever this version keeps them."""
    holder = getattr(uploader, "_pacer", uploader)
    return (
        round(holder._pace, 6),
        holder._calm,
        holder._MAX_PACE,
        holder._CALM_FILES,
    )


# --------------------------------------------------------------------------
# the operations a scenario can script
# --------------------------------------------------------------------------


def _note_flood(uploader):
    holder = getattr(uploader, "_pacer", None)
    return holder.note_flood() if holder else uploader._note_flood()


async def _pace(uploader):
    holder = getattr(uploader, "_pacer", None)
    if holder:
        return await holder.pace()
    return await uploader._pace_next_file()


async def _guard(uploader, func, *args, **kwargs):
    holder = getattr(uploader, "_pacer", None)
    if holder:
        return await holder.guard(func, *args, **kwargs)
    return await uploader._wait_flood(func, *args, **kwargs)


def _flaky(name, floods, result="ok", then=None, exc=FloodWait):
    """A coroutine that floods *floods* times before answering."""
    calls = []

    async def func(*args, **kwargs):
        calls.append(1)
        if len(calls) <= floods:
            if then is not None:
                then()
            raise exc(2)
        return result

    func.__name__ = name
    return func, calls


async def _op_flood(uploader, op, log):
    _note_flood(uploader)


async def _op_pace(uploader, op, log):
    await _pace(uploader)


async def _op_cancel(uploader, op, log):
    uploader._listener.is_cancelled = True


async def _op_guard(uploader, op, log):
    name, floods = op[1], op[2]
    exc = op[3] if len(op) > 3 else FloodWait
    func, calls = _flaky(name, floods, exc=exc)
    result = await _guard(uploader, func, "pos", kw=1)
    log.append(("guard_result", (result, len(calls)), ()))


async def _op_guard_cancel_midway(uploader, op, log):
    """Cancelled by the flood itself, so the retry is where it stops."""

    def cancel():
        uploader._listener.is_cancelled = True

    func, calls = _flaky("cancel_midway", 3, then=cancel)
    result = await _guard(uploader, func, kw=2)
    log.append(("guard_result", (result, len(calls)), ()))


async def _op_guard_raises(uploader, op, log):
    async def broken():
        raise ValueError("not a flood")

    broken.__name__ = "broken"
    try:
        await _guard(uploader, broken)
    except Exception as e:  # noqa: BLE001 - the failure mode is what is compared
        log.append(("guard_raised", (type(e).__name__, str(e)), ()))


async def _op_msg_to_reply(uploader, op, log):
    log.append(("msg_to_reply", (await uploader._msg_to_reply(),), ()))


async def _op_get_message(uploader, op, log):
    log.append(("get_message", (await uploader._get_message(-1001, 42),), ()))


async def _op_copy_dumps(uploader, op, log):
    await uploader._copy_group_to_clone_dumps(-1001, 42)


async def _op_send_group(uploader, op, log):
    msgs = [FakeMessage(kind="video"), FakeMessage(kind="video")]
    log.append(("send_group", (await uploader._send_group(msgs, []),), ()))


async def _op_upload_file_flood(uploader, op, log):
    """The per-file sender path, which deliberately does not retry in place.

    The flood is re-raised so `_upload_file` re-enters and the thumbnail
    outlives the sleep. This op is what proves that path did not get unified
    with the guarded one.
    """
    floods = op[1]
    exc = op[2] if len(op) > 2 else FloodWait
    calls = []

    async def send_one(*_a, **_k):
        calls.append(1)
        if len(calls) <= floods:
            raise exc(3)
        return True

    uploader._send_one = send_one
    try:
        await uploader._upload_file("cap", "file", "/tmp/task/file")
    except Exception as e:  # noqa: BLE001 - the failure mode is what is compared
        log.append(("upload_file_raised", (type(e).__name__,), ()))
    log.append(("upload_file_sends", (len(calls),), ()))


_OPS = {
    "flood": _op_flood,
    "pace": _op_pace,
    "cancel": _op_cancel,
    "guard": _op_guard,
    "guard_cancel_midway": _op_guard_cancel_midway,
    "guard_raises": _op_guard_raises,
    "msg_to_reply": _op_msg_to_reply,
    "get_message": _op_get_message,
    "copy_dumps": _op_copy_dumps,
    "send_group": _op_send_group,
    "upload_file_flood": _op_upload_file_flood,
}


async def _run_op(uploader, op, log):
    """Perform one scripted operation and log what it answered."""
    handler = _OPS.get(op[0])
    if handler is None:
        raise AssertionError(f"unknown op {op[0]!r}")
    await handler(uploader, op, log)


def run(mod, scenario):
    """Everything one scenario observes about one version."""
    log, sleeps, logged = [], [], []

    for target in (mod, pacer_mod):
        if not hasattr(target, "LOGGER"):
            continue
        logger = target.LOGGER
        for level in ("warning", "error", "info"):
            setattr(
                logger,
                level,
                lambda m, _lvl=level, *a, **k: logged.append((_lvl, str(m))),
            )

    uploader = _build(mod, scenario, log, sleeps)
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
        _snapshot_pace(uploader),
        getattr(uploader._sent_msg, "id", None) is not None,
        uploader._is_private,
        uploader._base_msg is not None,
        dict(uploader._listener.clone_dump_chats),
    )
    return outcome, tuple(log), tuple(sleeps), tuple(logged), state


# --------------------------------------------------------------------------
# scenarios
# --------------------------------------------------------------------------


def scenarios():
    yield from _pace_scenarios()
    yield from _guard_scenarios()
    yield from _real_call_site_scenarios()
    yield from _per_file_sender_scenarios()


def _pace_scenarios():
    """The gap between files: how it appears, grows, caps and decays."""
    yield "pace_never_floods", {"ops": [("pace",)] * 6}
    yield "pace_one_flood", {"ops": [("flood",), ("pace",)]}
    yield "pace_two_floods", {"ops": [("flood",), ("pace",)] * 2}
    yield (
        "pace_decay_to_zero",
        {"ops": [("flood",)] + [("pace",)] * 8},
    )
    yield (
        "pace_cap",
        {"ops": [("flood",)] * 20 + [("pace",)] * 3},
    )
    # a flood right after the gap has decayed away starts over at 0.5
    yield (
        "pace_flood_after_decay",
        {"ops": [("flood",)] + [("pace",)] * 6 + [("flood",), ("pace",)]},
    )
    # a long calm run crossing several decay steps
    yield (
        "pace_long_calm",
        {"ops": [("flood",)] * 4 + [("pace",)] * 30},
    )
    # interleaved: every other file floods, so the gap should keep widening
    yield (
        "pace_alternating",
        {"ops": [("flood",), ("pace",), ("pace",)] * 5},
    )


def _guard_scenarios():
    """Waiting out a flood limit on a call that retries in place."""
    for floods in (0, 1, 2, 5):
        yield f"guard_{floods}_floods", {"ops": [("guard", "send_message", floods)]}
    yield (
        "guard_then_pace",
        {"ops": [("guard", "send_message", 1), ("pace",)]},
    )
    yield (
        "guard_cancelled_before",
        {"cancelled_before": True, "ops": [("guard", "send_message", 0)]},
    )
    yield "guard_cancel_midway", {"ops": [("guard_cancel_midway",)]}
    yield "guard_non_flood_error", {"ops": [("guard_raises",)]}
    yield (
        "guard_after_cancel_op",
        {"ops": [("guard", "a", 1), ("cancel",), ("guard", "b", 0)]},
    )
    # a premium account's flood is a sibling class, not a subclass: catching
    # only the ordinary one would miss it entirely
    yield (
        "guard_premium_flood",
        {"ops": [("guard", "send_message", 2, FloodPremiumWait)]},
    )
    yield (
        "guard_premium_then_pace",
        {"ops": [("guard", "send_message", 1, FloodPremiumWait), ("pace",)]},
    )


def _real_call_site_scenarios():
    """The uploader's own guarded calls, driven through its real methods."""
    # the four _msg_to_reply paths
    yield (
        "reply_dest_bot",
        {
            "up_dest": -1002,
            "answers": {"send_message": ["MSG"]},
            "ops": [("msg_to_reply",)],
        },
    )
    yield (
        "reply_dest_bot_private",
        {
            "up_dest": 7,
            "answers": {"send_message": [FakeMessage(chat_type="PRIVATE")]},
            "ops": [("msg_to_reply",)],
        },
    )
    yield (
        "reply_dest_user_session",
        {
            "up_dest": -1002,
            "user_session": True,
            "answers": {"send_message": ["MSG"]},
            "ops": [("msg_to_reply",)],
        },
    )
    yield (
        "reply_dest_floods_twice",
        {
            "up_dest": -1002,
            "answers": {"send_message": [FloodWait(2), FloodWait(4), "MSG"]},
            "ops": [("msg_to_reply",)],
        },
    )
    yield (
        "reply_dest_errors",
        {
            "up_dest": -1002,
            "answers": {"send_message": [RPCError("chat closed")]},
            "ops": [("msg_to_reply",)],
        },
    )
    yield (
        "reply_dest_cancelled",
        {
            "up_dest": -1002,
            "cancelled_before": True,
            "answers": {"send_message": ["MSG"]},
            "ops": [("msg_to_reply",)],
        },
    )
    yield (
        "reply_dest_not_super",
        {
            "up_dest": -1002,
            "is_super_chat": False,
            "answers": {"send_message": ["MSG"]},
            "ops": [("msg_to_reply",)],
        },
    )
    yield (
        "reply_cmd_msg_found",
        {
            "user_session": True,
            "answers": {"get_messages": ["MSG"]},
            "ops": [("msg_to_reply",)],
        },
    )
    yield (
        "reply_cmd_msg_deleted",
        {
            "user_session": True,
            "answers": {"get_messages": [None], "send_message": ["MSG"]},
            "ops": [("msg_to_reply",)],
        },
    )
    yield (
        "reply_cmd_msg_floods",
        {
            "user_session": True,
            "answers": {"get_messages": [FloodWait(1), "MSG"]},
            "ops": [("msg_to_reply",)],
        },
    )
    yield "reply_plain_message", {"ops": [("msg_to_reply",)]}

    # _get_message, the guarded lookup the album rebuilds itself from
    yield (
        "get_message_ok",
        {"answers": {"get_messages": ["MSG"]}, "ops": [("get_message",)]},
    )
    yield (
        "get_message_floods",
        {
            "answers": {"get_messages": [FloodWait(2), FloodWait(2), "MSG"]},
            "ops": [("get_message",)],
        },
    )
    yield (
        "get_message_cancelled",
        {"cancelled_before": True, "ops": [("get_message",)]},
    )

    # the clone dumps, where a guarded call runs once per configured chat
    dumps = {
        -100: {"last_sent_msg": None, "thread_id": 5},
        -200: {"last_sent_msg": 3, "thread_id": None},
    }
    yield (
        "copy_dumps_ok",
        {
            "clone_dump_chats": dumps,
            "answers": {"copy_media_group": [[FakeMessage()], [FakeMessage()]]},
            "ops": [("copy_dumps",)],
        },
    )
    yield (
        "copy_dumps_floods",
        {
            "clone_dump_chats": dumps,
            "answers": {
                "copy_media_group": [FloodWait(3), [FakeMessage()], [FakeMessage()]]
            },
            "ops": [("copy_dumps",)],
        },
    )
    yield (
        "copy_dumps_cancelled_stops_early",
        {
            "clone_dump_chats": dumps,
            "cancelled_before": True,
            "ops": [("copy_dumps",)],
        },
    )
    yield (
        "copy_dumps_error_is_logged",
        {
            "clone_dump_chats": dumps,
            "answers": {"copy_media_group": [RPCError("no rights"), [FakeMessage()]]},
            "ops": [("copy_dumps",)],
        },
    )

    # _send_group, whose guarded send decides whether the group counts as sent
    yield (
        "send_group_ok",
        {
            "answers": {"send_media_group": [[FakeMessage(), FakeMessage()]]},
            "ops": [("send_group",)],
        },
    )
    yield (
        "send_group_floods",
        {
            "answers": {
                "send_media_group": [FloodWait(2), [FakeMessage(), FakeMessage()]]
            },
            "ops": [("send_group",)],
        },
    )
    yield (
        "send_group_cancelled",
        {"cancelled_before": True, "ops": [("send_group",)]},
    )


def _per_file_sender_scenarios():
    """The path that deliberately does not retry in place."""
    for floods in (0, 1, 2):
        yield f"upload_file_{floods}_floods", {"ops": [("upload_file_flood", floods)]}
    # a flood on a file, then the next file's gap: proves the two paths share
    # the widened pace and nothing else
    yield (
        "upload_file_then_pace",
        {"ops": [("upload_file_flood", 1), ("pace",)]},
    )
    yield (
        "upload_file_flood_then_guard",
        {"ops": [("upload_file_flood", 2), ("guard", "send_message", 0), ("pace",)]},
    )
    yield (
        "upload_file_premium_flood",
        {"ops": [("upload_file_flood", 1, FloodPremiumWait), ("pace",)]},
    )


# --------------------------------------------------------------------------
# where the pacing is called from
# --------------------------------------------------------------------------

# The behavioural scenarios can prove the policy is unchanged, but not that
# every call to it stayed in the method it was in: `_upload_one` walks a
# directory and sends files, so it is not drivable here, and it owns the only
# `pace()` call there is. Comparing the call sites in the source closes that
# gap -- a moved or dropped call fails the gate even though no scenario reaches
# it.
_OLD_CALLS = {"_wait_flood": "guard", "_pace_next_file": "pace", "_note_flood": "flood"}
_NEW_CALLS = {"guard": "guard", "pace": "pace", "note_flood": "flood"}

# The one difference the check is allowed to see: the three pacing methods used
# to be defined on the uploader, and `_wait_flood`'s own body called
# `_note_flood`. Their bodies are what moved to `flood_pacer.py`, so the check
# looks at callers only -- everything else in the mapping must match exactly.
_MOVED_OUT = frozenset(_OLD_CALLS)


def _pacing_call(node):
    """Which pacing operation an AST call node performs, if any."""
    if not isinstance(node.func, ast.Attribute):
        return None
    owner = node.func.value
    if isinstance(owner, ast.Name) and owner.id == "self":
        return _OLD_CALLS.get(node.func.attr)
    if (
        isinstance(owner, ast.Attribute)
        and owner.attr == "_pacer"
        and isinstance(owner.value, ast.Name)
        and owner.value.id == "self"
    ):
        return _NEW_CALLS.get(node.func.attr)
    return None


def _call_sites(src, label):
    """`{method name: sorted pacing calls}` for the uploader class in *src*."""
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
                if isinstance(node, ast.Call) and (kind := _pacing_call(node))
            ]
            if found:
                sites[func.name] = sorted(found)
    return sites


def check_call_sites():
    """True when every pacing call is still made from the same method."""
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
        print(f"call sites: {total} pacing calls across {len(old)} methods, unmoved")
        return True
    print("\n✗ pacing calls moved between methods")
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
