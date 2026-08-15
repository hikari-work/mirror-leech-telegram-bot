"""Differential harness: settings UI before vs after Fase 7.

Loads the pre-refactor `users_settings.py` / `bot_settings.py` out of git and
the working-tree versions under the same stubs, drives both through identical
scenarios, and compares everything observable: the rendered menu text, the
exact button matrix (labels + callback data + layout), the ordered call log
with arguments, and the mutated global state (user_data, Config, aria2/qbit
options, module paging state).

Exit code 0 means every scenario matched. Usage: python tools/_phase7_diff.py
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import ModuleType, SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
USERS = "bot/modules/users_settings.py"
BOTS = "bot/modules/bot_settings.py"

# Deviations we accept on purpose.
#
# The two entries below are the whole observable surface of the `eval()` ->
# `settings.literals.parse_literal` fix. Rejecting non-literals is the point of
# that change, and both scenarios already failed before it — only the wording of
# the error sent back to the user moved, because `literal_eval` would otherwise
# have dumped an AST repr at the user. Nothing else in 306 scenarios shifts.
_LITERAL_HINT = (
    "eval() -> parse_literal: same rejection, error text gains a quoting hint"
)
EXPECTED: dict[str, str] = {
    "user/set_option/FFMPEG_CMDS/{invalid}": _LITERAL_HINT,
    "user/add_one/YT_DLP_OPTIONS/True/{bad}": _LITERAL_HINT,
}


# --------------------------------------------------------------------------
# recording world
# --------------------------------------------------------------------------


class World:
    # The modules do `from .. import user_data` at import time, which binds the
    # container object once. Rebinding these in reset() would leave the modules
    # pointing at a stale dict, so every container is created once here and only
    # ever cleared in place.
    def __init__(self):
        self.calls: list = []
        self.user_data: dict = {}
        self.excluded: list = []
        self.included: list = []
        self.auth_chats: dict = {}
        self.sudo_users: list = []
        self.task_dict: dict = {}
        self.intervals: dict = {"status": {}}
        self.aria2_options: dict = {}
        self.qbit_options: dict = {}
        self.config: dict = {}
        self.existing_paths: set = set()
        self.reset()

    def reset(self):
        self.calls.clear()
        self.user_data.clear()
        self.excluded.clear()
        self.included.clear()
        self.auth_chats.clear()
        self.sudo_users.clear()
        self.task_dict.clear()
        self.intervals.clear()
        self.intervals["status"] = {}
        self.aria2_options.clear()
        self.qbit_options.clear()
        self.config.clear()
        self.existing_paths.clear()
        self.is_premium = False
        self.max_split = 2097152000

    def record(self, name, *args):
        self.calls.append((name, *args))


WORLD = World()


# --------------------------------------------------------------------------
# stub leaves
# --------------------------------------------------------------------------


async def _noop(*a, **k):
    return None


async def _exists(p):
    return p in WORLD.existing_paths


async def _isfile(p):
    return p in WORLD.existing_paths


async def _remove(p):
    WORLD.record("remove_file", p)
    WORLD.existing_paths.discard(p)


async def _rename(a, b):
    WORLD.record("rename", a, b)


async def _create_thumb(message, uid):
    WORLD.record("create_thumb", uid)
    return f"thumbnails/{uid}.jpg"


async def _send_message(message, text, button=None):
    WORLD.record("send_message", text, _ser_menu(button))


async def _edit_message(message, text, button=None):
    WORLD.record("edit_message", text, _ser_menu(button))


async def _send_file(message, f, name=None):
    fname = getattr(f, "name", f)
    payload = None
    if hasattr(f, "getvalue"):
        payload = f.getvalue().decode(errors="replace")
    WORLD.record("send_file", fname, name, payload)


async def _delete_message(message):
    WORLD.record("delete_message", getattr(message, "tag", repr(message)))


async def _update_status_message(cid):
    WORLD.record("update_status_message", cid)


async def _start_from_queued():
    WORLD.record("start_from_queued")


async def _initiate_search_tools():
    WORLD.record("initiate_search_tools")


def _add_job():
    WORLD.record("add_job")


async def _update_qb_options():
    WORLD.record("update_qb_options")


async def _update_variables():
    WORLD.record("update_variables")


def _update_user_ldata(id_, key, value):
    WORLD.user_data.setdefault(id_, {})
    WORLD.user_data[id_][key] = value


def _get_size_bytes(size):
    size = size.lower()
    if "k" in size:
        return int(float(size.split("k")[0]) * 1024)
    if "m" in size:
        return int(float(size.split("m")[0]) * 1048576)
    if "g" in size:
        return int(float(size.split("g")[0]) * 1073741824)
    if "t" in size:
        return int(float(size.split("t")[0]) * 1099511627776)
    return 0


def _new_task(func):
    """Pass-through: the real one spawns a task on bot_loop."""
    return func


class SetInterval:
    def __init__(self, interval, action, *args):
        WORLD.record("SetInterval", interval, *args)
        self.interval = interval

    def cancel(self):
        WORLD.record("SetInterval.cancel", self.interval)


class _Database:
    def __getattr__(self, name):
        async def call(*args, **kwargs):
            WORLD.record(f"db.{name}", *args)

        return call


class _Aria2Manager:
    @staticmethod
    async def change_aria2_option(key, value):
        WORLD.record("aria2.change_option", key, value)


class _QbitApp:
    @staticmethod
    async def set_preferences(prefs):
        WORLD.record("qbit.set_preferences", dict(prefs))


class _TorrentManager:
    qbittorrent = SimpleNamespace(app=_QbitApp())

    @staticmethod
    async def change_aria2_option(key, value):
        WORLD.record("aria2.change_option", key, value)


class _Config:
    """Mimics bot.core.config_manager.Config over WORLD.config."""

    @classmethod
    def get(cls, key):
        return WORLD.config.get(key)

    @classmethod
    def set(cls, key, value):
        WORLD.config[key] = value
        WORLD.record("Config.set", key, value)

    @classmethod
    def get_all(cls):
        return dict(WORLD.config)

    @classmethod
    def load(cls):
        WORLD.record("Config.load")

    def __class_getitem__(cls, key):  # pragma: no cover - unused
        return WORLD.config.get(key)


def _config_getattr(name):
    if name.startswith("__"):
        raise AttributeError(name)
    return WORLD.config.get(name, "")


class _ConfigMeta(type):
    def __getattr__(cls, name):
        return _config_getattr(name)


class Config(_Config, metaclass=_ConfigMeta):
    pass


class _TgClient:
    @classmethod
    def _premium(cls):
        return WORLD.is_premium


class _TgClientMeta(type):
    @property
    def IS_PREMIUM_USER(cls):
        return WORLD.is_premium

    @property
    def MAX_SPLIT_SIZE(cls):
        return WORLD.max_split


class TgClient(metaclass=_TgClientMeta):
    pass


# --- pyrogram surface -----------------------------------------------------


class InlineKeyboardButton:
    def __init__(self, text, callback_data=None, url=None):
        self.text = text
        self.callback_data = callback_data
        self.url = url

    def __repr__(self):
        return f"({self.text!r} -> {self.callback_data or self.url!r})"


class InlineKeyboardMarkup:
    def __init__(self, rows):
        self.rows = rows


def _ser_menu(markup):
    if markup is None:
        return None
    if isinstance(markup, InlineKeyboardMarkup):
        markup = markup.rows
    return [[(b.text, b.callback_data or b.url) for b in row] for row in markup]


class MessageHandler:
    def __init__(self, callback, filters=None):
        self.callback = callback
        self.filters = filters


def _create(func, name=None, **kwargs):
    return ("filter", getattr(func, "__name__", "f"))


class _Client:
    def add_handler(self, handler, group=0):
        WORLD.record("add_handler", type(handler).__name__, group)
        return (handler, group)

    def remove_handler(self, handler, group=0):
        WORLD.record("remove_handler", group)


class _Query:
    def __init__(self, data, user_id=1, chat_id=100):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id, mention=f"@u{user_id}")
        self.message = _Message(chat_id=chat_id)

    async def answer(self, text=None, show_alert=False):
        WORLD.record("query.answer", text, show_alert)


class _Message:
    def __init__(self, chat_id=100, text="", user_id=1, document=None):
        self.chat = SimpleNamespace(id=chat_id)
        self.from_user = SimpleNamespace(id=user_id, mention=f"@u{user_id}")
        self.sender_chat = None
        self.text = text
        self.media = None
        self.document = document
        self.reply_to_message = _Reply()
        self.tag = "message"

    async def download(self, file_name=None):
        WORLD.record("download", file_name)


class _Reply:
    tag = "reply_to_message"


async def _subproc_exec(*args, **kwargs):
    WORLD.record("subprocess_exec", *args)
    return SimpleNamespace(wait=_noop)


async def _subproc_shell(cmd, **kwargs):
    WORLD.record("subprocess_shell", " ".join(cmd.split()))
    return SimpleNamespace(wait=_noop)


async def _gather(*aws, **kwargs):
    for aw in aws:
        await aw


# --------------------------------------------------------------------------
# module graph
# --------------------------------------------------------------------------


def _stub(name, **attrs):
    m = ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    return m


def _pkg(name, path=None):
    m = _stub(name)
    m.__path__ = [str(path)] if path else []
    return m


class _GlobalsProxy(ModuleType):
    """`bot` package: the mutable globals must follow WORLD.reset()."""

    _MAP = {
        "user_data": "user_data",
        "excluded_extensions": "excluded",
        "included_extensions": "included",
        "auth_chats": "auth_chats",
        "sudo_users": "sudo_users",
        "task_dict": "task_dict",
        "intervals": "intervals",
        "aria2_options": "aria2_options",
        "qbit_options": "qbit_options",
    }

    def __getattr__(self, name):
        if name in self._MAP:
            return getattr(WORLD, self._MAP[name])
        raise AttributeError(name)


def install_stubs():
    bot_pkg = _GlobalsProxy("bot")
    bot_pkg.__path__ = [str(ROOT / "bot")]
    bot_pkg.LOGGER = SimpleNamespace(
        info=lambda m: WORLD.record("log", m),
        error=lambda m: WORLD.record("log", m),
        warning=lambda m: WORLD.record("log", m),
    )

    # pyrogram must be stubbed *before* button_build executes, otherwise it
    # binds the real InlineKeyboardMarkup and the menus stop being comparable.
    sys.modules.update({
        "pyrogram": _pkg("pyrogram"),
        "pyrogram.filters": _stub("pyrogram.filters", create=_create),
        "pyrogram.handlers": _stub("pyrogram.handlers", MessageHandler=MessageHandler),
        "pyrogram.types": _stub(
            "pyrogram.types",
            InlineKeyboardButton=InlineKeyboardButton,
            InlineKeyboardMarkup=InlineKeyboardMarkup,
        ),
    })

    button_build = _stub(
        "bot.helper.telegram_helper.button_build",
    )
    src = (ROOT / "bot/helper/telegram_helper/button_build.py").read_text()
    exec(compile(src, "button_build.py", "exec"), button_build.__dict__)

    mods = {
        "aiofiles": _pkg("aiofiles"),
        "aiofiles.os": _stub(
            "aiofiles.os",
            remove=_remove,
            rename=_rename,
            path=SimpleNamespace(exists=_exists, isfile=_isfile),
        ),
        "bot": bot_pkg,
        "bot.modules": _pkg("bot.modules", ROOT / "bot/modules"),
        "bot.modules.rss": _stub("bot.modules.rss", add_job=_add_job),
        "bot.modules.search": _stub(
            "bot.modules.search", initiate_search_tools=_initiate_search_tools
        ),
        "bot.core": _pkg("bot.core"),
        "bot.core.config_manager": _stub("bot.core.config_manager", Config=Config),
        "bot.core.telegram_manager": _stub(
            "bot.core.telegram_manager", TgClient=TgClient
        ),
        "bot.core.torrent_manager": _stub(
            "bot.core.torrent_manager", TorrentManager=_TorrentManager
        ),
        "bot.core.startup": _stub(
            "bot.core.startup",
            update_qb_options=_update_qb_options,
            update_variables=_update_variables,
        ),
        "bot.helper": _pkg("bot.helper", ROOT / "bot/helper"),
        "bot.helper.ext_utils": _pkg("bot.helper.ext_utils"),
        "bot.helper.ext_utils.bot_utils": _stub(
            "bot.helper.ext_utils.bot_utils",
            update_user_ldata=_update_user_ldata,
            new_task=_new_task,
            get_size_bytes=_get_size_bytes,
            SetInterval=SetInterval,
        ),
        "bot.helper.ext_utils.db_handler": _stub(
            "bot.helper.ext_utils.db_handler", database=_Database()
        ),
        "bot.helper.ext_utils.media_utils": _stub(
            "bot.helper.ext_utils.media_utils", create_thumb=_create_thumb
        ),
        "bot.helper.ext_utils.task_manager": _stub(
            "bot.helper.ext_utils.task_manager", start_from_queued=_start_from_queued
        ),
        "bot.helper.ext_utils.help_messages": _stub(
            "bot.helper.ext_utils.help_messages",
            user_settings_text=_USER_SETTINGS_TEXT,
        ),
        "bot.helper.telegram_helper": _pkg("bot.helper.telegram_helper"),
        "bot.helper.telegram_helper.button_build": button_build,
        "bot.helper.telegram_helper.message_utils": _stub(
            "bot.helper.telegram_helper.message_utils",
            send_message=_send_message,
            edit_message=_edit_message,
            send_file=_send_file,
            delete_message=_delete_message,
            update_status_message=_update_status_message,
        ),
    }
    sys.modules.update(mods)


_USER_SETTINGS_TEXT = {
    k: f"<help for {k}>"
    for k in (
        "LEECH_SPLIT_SIZE",
        "LEECH_DUMP_CHAT",
        "LEECH_FILENAME_PREFIX",
        "THUMBNAIL_LAYOUT",
        "EXCLUDED_EXTENSIONS",
        "INCLUDED_EXTENSIONS",
        "NAME_SUBSTITUTE",
        "YT_DLP_OPTIONS",
        "FFMPEG_CMDS",
        "CLONE_DUMP_CHATS",
    )
}


def _load(source: str, modname: str, target: str) -> ModuleType:
    mod = ModuleType(modname)
    mod.__package__ = "bot.modules"
    mod.__file__ = str(ROOT / target)
    sys.modules[modname] = mod
    exec(compile(source, mod.__file__, "exec"), mod.__dict__)
    _patch_runtime(mod)
    return mod


def _patch_runtime(mod):
    """Make the 60s event-handler wait deterministic and instant."""

    async def fast_sleep(_secs):
        for d in _handler_dicts(mod):
            for k in list(d):
                d[k] = False

    if hasattr(mod, "sleep"):
        mod.sleep = fast_sleep
    if hasattr(mod, "create_subprocess_exec"):
        mod.create_subprocess_exec = _subproc_exec
    if hasattr(mod, "create_subprocess_shell"):
        mod.create_subprocess_shell = _subproc_shell
    if hasattr(mod, "gather"):
        mod.gather = _gather


def _handler_dicts(mod):
    out = []
    if hasattr(mod, "handler_dict"):
        out.append(mod.handler_dict)
    for name in dir(mod):
        obj = getattr(mod, name)
        if isinstance(obj, ModuleType) and hasattr(obj, "handler_dict"):
            out.append(obj.handler_dict)
    return out


def load_pair(target: str, tag: str):
    old_src = subprocess.run(
        ["git", "show", f"HEAD:{target}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    old = _load(old_src, f"_old_{tag}", target)
    new = _load((ROOT / target).read_text(), f"_new_{tag}", target)
    return old, new


# --------------------------------------------------------------------------
# scenario plumbing
# --------------------------------------------------------------------------


FROM_USER = SimpleNamespace(id=1, mention="@u1")


def _paging(mod):
    if hasattr(mod, "Paging"):
        return (mod.Paging.start, mod.Paging.state)
    return (getattr(mod, "start", None), getattr(mod, "state", None))


def _set_paging(mod, start=0, state="view"):
    if hasattr(mod, "Paging"):
        mod.Paging.start, mod.Paging.state = start, state
    else:
        mod.start, mod.state = start, state


def _reset_module(mod):
    for d in _handler_dicts(mod):
        d.clear()
    if hasattr(mod, "Paging") or hasattr(mod, "start"):
        _set_paging(mod)


def _snapshot(ret, mod):
    text, menu = (None, None)
    if isinstance(ret, tuple) and len(ret) == 2:
        text, menu = ret[0], _ser_menu(ret[1])
    elif ret is not None:
        text = repr(ret)
    return {
        "text": text,
        "menu": menu,
        "calls": list(WORLD.calls),
        "user_data": repr(WORLD.user_data),
        "config": repr(sorted(WORLD.config.items())),
        "aria2": repr(sorted(WORLD.aria2_options.items())),
        "qbit": repr(sorted(WORLD.qbit_options.items())),
        "excluded": repr(WORLD.excluded),
        "included": repr(WORLD.included),
        "auth_chats": repr(WORLD.auth_chats),
        "sudo_users": repr(WORLD.sudo_users),
        "paging": repr(_paging(mod)),
    }


async def run_once(mod, scenario):
    setup, run = scenario
    WORLD.reset()
    setup(WORLD)
    _reset_module(mod)
    try:
        ret = await run(mod)
    except Exception as exc:  # noqa: BLE001 - the exception is the observation
        snap = _snapshot(None, mod)
        snap["exc"] = f"{type(exc).__name__}: {exc}"
        return snap
    snap = _snapshot(ret, mod)
    snap["exc"] = None
    return snap


def _assign(w, name, value):
    """Fill a WORLD container in place — never rebind (see World.__init__)."""
    current = getattr(w, name, None)
    value = deepcopy(value)
    if isinstance(current, dict) and isinstance(value, dict):
        current.clear()
        current.update(value)
    elif isinstance(current, list) and isinstance(value, list):
        current.clear()
        current.extend(value)
    elif isinstance(current, set) and isinstance(value, (set, frozenset)):
        current.clear()
        current.update(value)
    else:
        setattr(w, name, value)


def setup(**knobs):
    def apply(w):
        for k, v in knobs.items():
            if k == "user_dict":
                w.user_data[1] = deepcopy(v)
            elif hasattr(w, k):
                _assign(w, k, v)
            else:
                w.config[k] = deepcopy(v)

    return apply


def cfg(**values):
    def apply(w):
        w.config.update(deepcopy(values))

    return apply


def combine(*applies):
    def apply(w):
        for a in applies:
            a(w)

    return apply


def user_dict(**values):
    def apply(w):
        w.user_data[1] = deepcopy(values)

    return apply


def world(**knobs):
    """Knob values are deep-copied: old and new runs must not share mutables."""

    def apply(w):
        for k, v in knobs.items():
            _assign(w, k, v)

    return apply


NOTHING = lambda w: None  # noqa: E731


# --------------------------------------------------------------------------
# users_settings scenarios
# --------------------------------------------------------------------------

FULL_USER = dict(
    THUMBNAIL="thumbnails/1.jpg",
    LEECH_SPLIT_SIZE=100,
    LEECH_DUMP_CHAT="-100123",
    LEECH_FILENAME_PREFIX="<b>@ch</b>",
    THUMBNAIL_LAYOUT="2x2",
    CLONE_DUMP_CHATS="-100999",
    AS_DOCUMENT=True,
    EQUAL_SPLITS=True,
    MEDIA_GROUP=True,
    USER_TRANSMISSION=True,
    HYBRID_LEECH=True,
    FILES_LINKS=True,
    EXCLUDED_EXTENSIONS=["mkv"],
    INCLUDED_EXTENSIONS=["mp4"],
    NAME_SUBSTITUTE="a/b",
    YT_DLP_OPTIONS={"format": "best"},
    FFMPEG_CMDS={"conv": ["-i mltb.mkv -c copy mltb {title}"]},
)

FALSY_USER = {
    k: ("" if isinstance(v, str) else type(v)()) for k, v in FULL_USER.items()
}

FULL_CONFIG = dict(
    LEECH_SPLIT_SIZE=2097152000,
    LEECH_DUMP_CHAT="-100c",
    LEECH_FILENAME_PREFIX="cfgprefix",
    THUMBNAIL_LAYOUT="3x3",
    CLONE_DUMP_CHATS="-100cc",
    AS_DOCUMENT=True,
    EQUAL_SPLITS=True,
    MEDIA_GROUP=True,
    USER_TRANSMISSION=True,
    HYBRID_LEECH=True,
    FILES_LINKS=True,
    NAME_SUBSTITUTE="c/d",
    YT_DLP_OPTIONS={"format": "cfg"},
    FFMPEG_CMDS={"cfg": ["-i mltb"]},
)

USER_OPTIONS = [
    "THUMBNAIL",
    "LEECH_SPLIT_SIZE",
    "LEECH_DUMP_CHAT",
    "LEECH_FILENAME_PREFIX",
    "THUMBNAIL_LAYOUT",
    "CLONE_DUMP_CHATS",
    "EXCLUDED_EXTENSIONS",
    "INCLUDED_EXTENSIONS",
    "NAME_SUBSTITUTE",
    "YT_DLP_OPTIONS",
    "FFMPEG_CMDS",
]

USER_CALLBACKS = [
    "userset 2 back",
    "userset 1 setevent",
    "userset 1 leech",
    "userset 1 menu FFMPEG_CMDS",
    "userset 1 menu THUMBNAIL",
    "userset 1 menu LEECH_DUMP_CHAT",
    "userset 1 tog AS_DOCUMENT t",
    "userset 1 tog AS_DOCUMENT f",
    "userset 1 tog HYBRID_LEECH t",
    "userset 1 file THUMBNAIL",
    "userset 1 ffvar",
    "userset 1 ffvar conv",
    "userset 1 ffvar conv title 0",
    "userset 1 ffvar conv ffmpegvarreset",
    "userset 1 ffvar missing",
    "userset 1 set LEECH_SPLIT_SIZE",
    "userset 1 set NAME_SUBSTITUTE",
    "userset 1 addone YT_DLP_OPTIONS",
    "userset 1 rmone FFMPEG_CMDS",
    "userset 1 remove THUMBNAIL",
    "userset 1 remove LEECH_DUMP_CHAT",
    "userset 1 reset LEECH_DUMP_CHAT",
    "userset 1 reset all",
    "userset 1 view THUMBNAIL",
    "userset 1 view FFMPEG_CMDS",
    "userset 1 back",
    "userset 1 close",
]

SET_OPTION_CASES = [
    ("LEECH_SPLIT_SIZE", "12345"),
    ("LEECH_SPLIT_SIZE", "2.5gb"),
    ("LEECH_SPLIT_SIZE", "99999999999"),
    ("LEECH_SPLIT_SIZE", "500mb"),
    ("EXCLUDED_EXTENSIONS", "mkv .MP4  avi"),
    ("INCLUDED_EXTENSIONS", ".mkv MP4"),
    ("FFMPEG_CMDS", "{'a': ['-i mltb']}"),
    ("FFMPEG_CMDS", "not a dict"),
    ("FFMPEG_CMDS", "{invalid}"),
    ("YT_DLP_OPTIONS", "{'format': 'best', 'n': 3}"),
    ("YT_DLP_OPTIONS", "{'nocheckcertificate': True}"),
    ("LEECH_FILENAME_PREFIX", "@chan"),
    ("NAME_SUBSTITUTE", "a/b"),
    ("THUMBNAIL_LAYOUT", "2x2"),
]


def _render_settings(mod, from_user, stype):
    """Call whichever renderer this revision has.

    Pre-refactor the screen builder lived in `users_settings.get_user_settings`;
    post-refactor it is `settings.menu_builder.build_settings`, re-exported by
    `users_settings` under its new name. Both take the same arguments and return
    the same `(text, markup)` pair.
    """
    fn = getattr(mod, "get_user_settings", None) or mod.build_settings
    return fn(from_user, stype)


def _user_render_scenarios():
    for stype in ("main", "leech"):
        for premium in (False, True):
            base = world(is_premium=premium)
            cases = {
                "empty": NOTHING,
                "user-full": user_dict(**FULL_USER),
                "user-falsy": user_dict(**FALSY_USER),
                "config-only": cfg(**FULL_CONFIG),
                "config+falsy-user": combine(
                    cfg(**FULL_CONFIG), user_dict(**FALSY_USER)
                ),
                "thumb-exists": combine(
                    world(existing_paths={"thumbnails/1.jpg"}), user_dict(**FULL_USER)
                ),
                "globals": combine(
                    world(excluded=["aria2", "!qB", "iso"], included=["mp4"]),
                    cfg(**FULL_CONFIG),
                ),
            }
            for cname, capply in cases.items():
                yield (
                    f"user/get_user_settings/{stype}/premium={premium}/{cname}",
                    (
                        combine(base, capply),
                        lambda m, s=stype: _render_settings(m, FROM_USER, s),
                    ),
                )


def _user_menu_scenarios():
    for option in USER_OPTIONS:
        for cname, capply in {
            "absent": NOTHING,
            "present": user_dict(**FULL_USER),
            "falsy": user_dict(**FALSY_USER),
            "config-only": cfg(**FULL_CONFIG),
        }.items():
            yield (
                f"user/get_menu/{option}/{cname}",
                (
                    capply,
                    lambda m, o=option: m.get_menu(o, _Message(), 1),
                ),
            )


def _user_callback_scenarios():
    for data in USER_CALLBACKS:
        for cname, capply in {
            "full": combine(
                user_dict(**FULL_USER),
                world(existing_paths={"thumbnails/1.jpg"}, is_premium=True),
            ),
            "empty": cfg(**FULL_CONFIG),
        }.items():
            yield (
                f"user/edit_user_settings/{data}/{cname}",
                (
                    capply,
                    lambda m, d=data: m.edit_user_settings(_Client(), _Query(d)),
                ),
            )


def _user_set_option_scenarios():
    for option, value in SET_OPTION_CASES:
        yield (
            f"user/set_option/{option}/{value[:18]}",
            (
                NOTHING,
                lambda m, o=option, v=value: m.set_option(None, _Message(text=v), o),
            ),
        )


def _user_add_one_scenarios():
    for option, value, existing in [
        ("FFMPEG_CMDS", "{'new': ['-i mltb']}", True),
        ("FFMPEG_CMDS", "{'new': ['-i mltb']}", False),
        ("YT_DLP_OPTIONS", "{'format': 'x'}", True),
        ("YT_DLP_OPTIONS", "no dict", True),
        ("YT_DLP_OPTIONS", "{bad}", True),
    ]:
        yield (
            f"user/add_one/{option}/{existing}/{value[:12]}",
            (
                user_dict(**FULL_USER) if existing else user_dict(FFMPEG_CMDS={}),
                lambda m, o=option, v=value: m.add_one(None, _Message(text=v), o),
            ),
        )


def _user_remove_one_scenarios():
    for option, value in [
        ("FFMPEG_CMDS", "conv"),
        ("FFMPEG_CMDS", "conv/missing"),
        ("YT_DLP_OPTIONS", "format"),
    ]:
        yield (
            f"user/remove_one/{option}/{value}",
            (
                user_dict(**FULL_USER),
                lambda m, o=option, v=value: m.remove_one(None, _Message(text=v), o),
            ),
        )


def _user_file_and_ffmpeg_scenarios():
    yield (
        "user/add_file",
        (
            NOTHING,
            lambda m: m.add_file(None, _Message(), "THUMBNAIL"),
        ),
    )

    yield (
        "user/set_ffmpeg_variable",
        (
            user_dict(**FULL_USER),
            lambda m: m.set_ffmpeg_variable(
                None, _Message(text="Title!"), "conv", "title", "0"
            ),
        ),
    )

    for key, value, index in [
        (None, None, None),
        ("conv", None, None),
        ("conv", "title", "0"),
        ("missing", None, None),
    ]:
        yield (
            f"user/ffmpeg_variables/{key}/{value}",
            (
                combine(user_dict(**FULL_USER), world(is_premium=True)),
                lambda m, k=key, v=value, i=index: m.ffmpeg_variables(
                    _Client(), _Query("userset 1 ffvar"), _Message(), 1, k, v, i
                ),
            ),
        )


def _user_dump_scenarios():
    for cname, capply in {
        "populated": combine(
            user_dict(**FULL_USER),
            world(auth_chats={-100: [1]}, sudo_users=[7]),
        ),
        "empty": NOTHING,
    }.items():
        yield (
            f"user/get_users_settings/{cname}",
            (capply, lambda m: m.get_users_settings(None, _Message())),
        )

    yield (
        "user/send_user_settings",
        (user_dict(**FULL_USER), lambda m: m.send_user_settings(None, _Message())),
    )

    yield (
        "user/update_user_settings",
        (
            user_dict(**FULL_USER),
            lambda m: m.update_user_settings(_Query("userset 1 back"), "leech"),
        ),
    )


# --------------------------------------------------------------------------
# bot_settings scenarios
# --------------------------------------------------------------------------

BOT_CONFIG = dict(
    BASE_URL="http://x",
    BASE_URL_PORT=80,
    BOT_TOKEN="tok",
    CMD_SUFFIX="1",
    DATABASE_URL="mongo://x",
    DATABASE_NAME="mltb",
    EXCLUDED_EXTENSIONS="",
    INCLUDED_EXTENSIONS="",
    INCOMPLETE_TASK_NOTIFIER=True,
    LEECH_SPLIT_SIZE=2097152000,
    OWNER_ID=7,
    QUEUE_ALL=0,
    RSS_DELAY=600,
    SEARCH_LIMIT=0,
    SEARCH_PLUGINS=[],
    STATUS_UPDATE_INTERVAL=15,
    SUDO_USERS="",
    TELEGRAM_API=1,
    TELEGRAM_HASH="h",
    TORRENT_TIMEOUT=0,
    TG_PROXY={},
    UPSTREAM_BRANCH="master",
    UPSTREAM_REPO="https://x@github.com/a/b",
    USER_SESSION_STRING="",
    AUTHORIZED_CHATS="",
    # Over 200 chars: viewing this sends a .txt file instead of an alert popup.
    NAME_SUBSTITUTE="s" * 250,
)

ARIA_OPTS = {
    "bt-stop-timeout": "0",
    "checksum": "x",
    "index-out": "x",
    "out": "x",
    "pause": "false",
    "select-file": "x",
    "max-connection-per-server": "10",
    "split": "10",
    "https-proxy": "",
    "min-split-size": "10M",
    "seed-time": "0",
    "max-tries": "5",
    "user-agent": "ua",
}

QBIT_OPTS = {
    "max_ratio": 1.0,
    "max_seeding_time": 0,
    "dht": True,
    "pex": True,
    "upload_limit": 0,
    "download_limit": 0,
    "listen_port": 6881,
    "encryption": 0,
    "max_connec": 100,
    "queueing_enabled": True,
    "start_paused_enabled": False,
}


def bot_world(**over):
    def apply(w):
        w.config.update(deepcopy(BOT_CONFIG))
        w.aria2_options.update(deepcopy(ARIA_OPTS))
        w.qbit_options.update(deepcopy(QBIT_OPTS))
        for k, v in over.items():
            if hasattr(w, k):
                _assign(w, k, v)
            else:
                w.config[k] = deepcopy(v)

    return apply


EDIT_VARIABLE_CASES = [
    ("CMD_SUFFIX", "true"),
    ("CMD_SUFFIX", "FALSE"),
    ("INCOMPLETE_TASK_NOTIFIER", "false"),
    ("STATUS_UPDATE_INTERVAL", "20"),
    ("TORRENT_TIMEOUT", "30"),
    ("LEECH_SPLIT_SIZE", "99999999999"),
    ("LEECH_SPLIT_SIZE", "1000"),
    ("BASE_URL_PORT", "8080"),
    ("EXCLUDED_EXTENSIONS", "mkv .MP4 avi"),
    ("INCLUDED_EXTENSIONS", ".mkv MP4"),
    ("AUTHORIZED_CHATS", "-100123|5|6 -100456"),
    ("SUDO_USERS", "11 22"),
    ("SEARCH_LIMIT", "42"),
    ("SEARCH_PLUGINS", "['a', 'b']"),
    ("TG_PROXY", "{'scheme': 'socks5'}"),
    ("UPSTREAM_BRANCH", "dev"),
    ("QUEUE_ALL", "3"),
    ("RSS_DELAY", "900"),
    ("SEARCH_API_LINK", "http://api"),
]

RESETVAR_CASES = [
    "CMD_SUFFIX",
    "LEECH_SPLIT_SIZE",
    "RSS_DELAY",
    "STATUS_UPDATE_INTERVAL",
    "SEARCH_LIMIT",
    "UPSTREAM_BRANCH",
    "EXCLUDED_EXTENSIONS",
    "INCLUDED_EXTENSIONS",
    "TORRENT_TIMEOUT",
    "BASE_URL",
    "BASE_URL_PORT",
    "INCOMPLETE_TASK_NOTIFIER",
    "AUTHORIZED_CHATS",
    "SUDO_USERS",
    "DATABASE_URL",
    "SEARCH_PLUGINS",
    "QUEUE_ALL",
    "TG_PROXY",
    "OWNER_ID",
]

BOT_CALLBACKS = [
    "botset close",
    "botset back",
    "botset var",
    "botset aria",
    "botset qbit",
    "botset syncqbit",
    "botset emptyaria split",
    "botset emptyqbit dht",
    "botset private",
    "botset botvar CMD_SUFFIX",
    "botset botvar USER_SESSION_STRING",
    "botset botvar SEARCH_PLUGINS",
    "botset botvar NAME_SUBSTITUTE",
    "botset ariavar split",
    "botset ariavar newkey",
    "botset qbitvar dht",
    "botset edit var",
    "botset edit aria",
    "botset view var",
    "botset start var 10",
    "botset start var 0",
    "botset push config.py",
    "botset push cookies.txt",
]


def _bot_get_buttons_scenarios():
    for key, edit_type in [
        (None, None),
        ("var", None),
        ("private", None),
        ("aria", None),
        ("qbit", None),
        ("BOT_TOKEN", "botvar"),
        ("CMD_SUFFIX", "botvar"),
        ("LEECH_SPLIT_SIZE", "botvar"),
        ("TG_PROXY", "botvar"),
        ("split", "ariavar"),
        ("newkey", "ariavar"),
        ("dht", "qbitvar"),
    ]:
        for state in ("view", "edit"):
            for start in (0, 10):
                yield (
                    f"bot/get_buttons/{key}/{edit_type}/{state}/{start}",
                    (
                        bot_world(),
                        _run_get_buttons(key, edit_type, state, start),
                    ),
                )


def _bot_edit_variable_scenarios():
    for key, value in EDIT_VARIABLE_CASES:
        yield (
            f"bot/edit_variable/{key}/{value[:16]}",
            (
                bot_world(),
                lambda m, k=key, v=value: m.edit_variable(
                    None, _Message(text=v), _Message(), k
                ),
            ),
        )

    yield (
        "bot/edit_variable/STATUS_UPDATE_INTERVAL/with-tasks",
        (
            bot_world(task_dict={"a": 1}),
            _run_with_interval(
                lambda m: m.edit_variable(
                    None, _Message(text="20"), _Message(), "STATUS_UPDATE_INTERVAL"
                )
            ),
        ),
    )


def _bot_edit_aria_scenarios():
    for key, value in [
        ("newkey", "https-proxy-user:bob"),
        ("split", "true"),
        ("split", "FALSE"),
        ("split", "16"),
    ]:
        yield (
            f"bot/edit_aria/{key}/{value[:14]}",
            (
                bot_world(),
                lambda m, k=key, v=value: m.edit_aria(
                    None, _Message(text=v), _Message(), k
                ),
            ),
        )


def _bot_edit_qbit_scenarios():
    for key, value in [
        ("dht", "true"),
        ("dht", "false"),
        ("max_ratio", "2.5"),
        ("listen_port", "6882"),
        ("encryption", "abc"),
    ]:
        yield (
            f"bot/edit_qbit/{key}/{value[:10]}",
            (
                bot_world(),
                lambda m, k=key, v=value: m.edit_qbit(
                    None, _Message(text=v), _Message(), k
                ),
            ),
        )


def _bot_callback_scenarios():
    for data in BOT_CALLBACKS:
        for state in ("view", "edit"):
            yield (
                f"bot/edit_bot_settings/{data}/{state}",
                (
                    bot_world(existing_paths={"config.py"}),
                    _run_callback(data, state),
                ),
            )


def _bot_resetvar_scenarios():
    for key in RESETVAR_CASES:
        yield (
            f"bot/edit_bot_settings/resetvar {key}",
            (
                bot_world(),
                _run_callback(f"botset resetvar {key}", "view"),
            ),
        )

    yield (
        "bot/edit_bot_settings/resetvar STATUS_UPDATE_INTERVAL/with-tasks",
        (
            bot_world(task_dict={"a": 1}),
            _run_with_interval(
                _run_callback("botset resetvar STATUS_UPDATE_INTERVAL", "view")
            ),
        ),
    )


def _bot_private_file_scenarios():
    for name, message in [
        ("text-filename", _Message(text="cookies.txt")),
        ("text-netrc", _Message(text=".netrc")),
        ("document", _Message(document=SimpleNamespace(file_name="cookies.txt"))),
        ("document-netrc", _Message(document=SimpleNamespace(file_name="netrc"))),
    ]:
        yield (
            f"bot/update_private_file/{name}",
            (
                bot_world(existing_paths={"cookies.txt"}),
                lambda m, msg=message: m.update_private_file(None, msg, _Message()),
            ),
        )


def _bot_root_scenarios():
    yield (
        "bot/send_bot_settings",
        (bot_world(), lambda m: m.send_bot_settings(None, _Message())),
    )



def user_scenarios():
    """Every users_settings scenario, in the order they were written."""
    yield from _user_render_scenarios()
    yield from _user_menu_scenarios()
    yield from _user_callback_scenarios()
    yield from _user_set_option_scenarios()
    yield from _user_add_one_scenarios()
    yield from _user_remove_one_scenarios()
    yield from _user_file_and_ffmpeg_scenarios()
    yield from _user_dump_scenarios()


def _run_get_buttons(key, edit_type, state, start):
    async def run(mod):
        _set_paging(mod, start, state)
        return await mod.get_buttons(key, edit_type)

    return run


def _run_callback(data, state):
    async def run(mod):
        _set_paging(mod, 0, state)
        return await mod.edit_bot_settings(_Client(), _Query(data))

    return run


def bot_scenarios():
    """Every bot_settings scenario, in the order they were written."""
    yield from _bot_get_buttons_scenarios()
    yield from _bot_edit_variable_scenarios()
    yield from _bot_edit_aria_scenarios()
    yield from _bot_edit_qbit_scenarios()
    yield from _bot_callback_scenarios()
    yield from _bot_resetvar_scenarios()
    yield from _bot_private_file_scenarios()
    yield from _bot_root_scenarios()


def _run_with_interval(inner):
    async def run(mod):
        WORLD.intervals["status"] = {5: SetInterval(15, None, 5)}
        WORLD.calls.clear()
        return await inner(mod)

    return run


# --------------------------------------------------------------------------


def diff(a, b):
    out = []
    for key in a:
        if a[key] != b[key]:
            out.append(f"    {key}:\n      old={a[key]!r}\n      new={b[key]!r}")
    return out


async def main():
    install_stubs()
    old_u, new_u = load_pair(USERS, "users_settings")
    old_b, new_b = load_pair(BOTS, "bot_settings")

    mismatches = 0
    total = 0
    suites = (
        (("u", old_u, new_u), user_scenarios),
        (("b", old_b, new_b), bot_scenarios),
    )
    for pair, gen in suites:
        _, old, new = pair
        for name, scenario in gen():
            total += 1
            res_old = await run_once(old, scenario)
            res_new = await run_once(new, scenario)
            d = diff(res_old, res_new)
            if not d:
                continue
            if name in EXPECTED:
                print(f"[expected] {name}: {EXPECTED[name]}")
                continue
            mismatches += 1
            print(f"[MISMATCH] {name}")
            print("\n".join(d))

    print(f"\n{total} scenarios, {mismatches} unexpected mismatch(es)")
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
