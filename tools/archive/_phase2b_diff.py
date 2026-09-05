"""Differential harness: `before_start()` before vs after the Fase 2 sub-phase.

Loads the pre-refactor `settings_resolver.py` out of git alongside the working
tree version, drives both through a matrix of task settings, and compares three
things per scenario:

1. **Final state** — every attribute `before_start()` assigns.
2. **Failure mode** — the type and message of any exception raised, so a
   "Chat not found!" cannot silently become a "could not ask" or vice versa.
3. **Warnings** — the `LOGGER.warning` calls, in order, because several of the
   destination downgrades are only observable as a log line.

Run from the repo root: `python tools/_phase2b_diff.py`
"""

from __future__ import annotations

import asyncio
import importlib.util
import itertools
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bot.helper.task_config.settings_resolver as new_mod  # noqa: E402

TARGET = "bot/helper/task_config/settings_resolver.py"


def _load_old():
    """The pre-refactor module, imported as a sibling inside the real package.

    Loading it under `bot.helper.task_config.*` is what makes its relative
    imports resolve exactly like the working-tree version's.
    """
    src = subprocess.run(
        ["git", "show", f"HEAD:{TARGET}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    name = "bot.helper.task_config._old_settings_resolver"
    spec = importlib.util.spec_from_loader(name, loader=None, origin=TARGET)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = str(ROOT / TARGET)
    mod.__package__ = "bot.helper.task_config"
    sys.modules[name] = mod
    exec(compile(src, TARGET, "exec"), mod.__dict__)
    return mod


old_mod = _load_old()

DEST = -1001234567890
USER = 42

TRACKED = (
    "name_sub up_dest chat_thread_id user_transmission hybrid_leech split_size "
    "max_split_size equal_splits as_doc thumbnail_layout clone_dump_chats "
    "excluded_extensions included_extensions ffmpeg_cmds thumb"
).split()


def chat(kind="CHANNEL", is_admin=True):
    return SimpleNamespace(id=DEST, type=SimpleNamespace(name=kind), is_admin=is_admin)


def member(can_manage=True, can_delete=True):
    return SimpleNamespace(
        privileges=SimpleNamespace(
            can_manage_chat=can_manage, can_delete_messages=can_delete
        )
    )


class Listener:
    def __init__(self, **kw):
        self.user_id = USER
        self.user_dict = {}
        self.client = SimpleNamespace(me=SimpleNamespace(id=1))
        self.up_dest = ""
        self.chat_thread_id = None
        self.name_sub = ""
        self.thumbnail_layout = ""
        self.thumb = None
        self.split_size = 0
        self.max_split_size = 0
        self.clone_dump_chats = {}
        self.excluded_extensions = []
        self.included_extensions = []
        self.ffmpeg_cmds = None
        self.equal_splits = False
        self.user_transmission = False
        self.hybrid_leech = False
        self.as_doc = False
        self.as_med = False
        self.bot_trans = False
        self.user_trans = False
        self.is_super_chat = True
        self.__dict__.update(kw)


def patch(mod, scenario, monkey):
    """Point one module's lookups at the scripted answers for this scenario."""
    chat_answers = list(scenario.get("chat", [chat()]))
    member_answers = list(scenario.get("member", [member()]))
    reach_answers = list(scenario.get("reach", [True]))

    async def get_dest_chat(client, chat_id):
        await asyncio.sleep(0)
        a = chat_answers.pop(0) if chat_answers else chat()
        if isinstance(a, BaseException):
            raise a
        return a

    async def get_dest_member(client, chat_id, user_id):
        await asyncio.sleep(0)
        a = member_answers.pop(0) if member_answers else member()
        if isinstance(a, BaseException):
            raise a
        return a

    async def can_reach_dest(client, chat_id):
        await asyncio.sleep(0)
        a = reach_answers.pop(0) if reach_answers else True
        if isinstance(a, BaseException):
            raise a
        return a

    monkey.append((mod, "get_dest_chat", mod.get_dest_chat))
    monkey.append((mod, "get_dest_member", mod.get_dest_member))
    monkey.append((mod, "can_reach_dest", mod.can_reach_dest))
    mod.get_dest_chat = get_dest_chat
    mod.get_dest_member = get_dest_member
    mod.can_reach_dest = can_reach_dest


def run(mod, scenario):
    """Final state (or the exception) after that module's before_start."""
    undo = []
    patch(mod, scenario, undo)
    premium = scenario.get("premium", True)
    mod.TgClient.IS_PREMIUM_USER = premium
    mod.TgClient.user = SimpleNamespace(me=SimpleNamespace(id=2))
    mod.TgClient.MAX_SPLIT_SIZE = 4194304000
    for key, value in scenario.get("config", {}).items():
        setattr(mod.Config, key, value)

    listener = Listener(**scenario.get("attrs", {}))
    listener.user_dict = dict(scenario.get("user_dict", {}))
    Bound = type("Bound", (mod.SettingsResolverMixin,), {})
    listener.__class__ = Bound

    logged = []
    real_warn = mod.LOGGER.warning
    mod.LOGGER.warning = lambda m, *a, **k: logged.append(str(m))
    try:
        asyncio.run(listener.before_start())
        outcome = ("ok", None)
    except Exception as e:  # noqa: BLE001 - comparing failure modes is the point
        outcome = (type(e).__name__, str(e))
    finally:
        mod.LOGGER.warning = real_warn
        for target, name, original in undo:
            setattr(target, name, original)

    state = {k: getattr(listener, k, "<unset>") for k in TRACKED}
    return outcome, state, logged


def scenarios():
    """Every scenario the two implementations are compared on."""
    yield from _destination_scenarios()
    yield from _lookup_scenarios()
    yield from _setting_scenarios()
    yield from _split_and_ffmpeg_scenarios()
    yield from _dump_and_thumb_scenarios()


def _destination_scenarios():
    """The destination string, and who was asked to upload to it."""
    yield "bare", {}
    yield "no_dest_not_super", {"attrs": {"is_super_chat": False, "user_trans": True}}

    # every prefix, with and without premium
    for prefix, premium in itertools.product(["", "b:", "u:", "h:"], [True, False]):
        yield (
            f"prefix_{prefix or 'none'}_premium_{premium}",
            {"attrs": {"up_dest": f"{prefix}{DEST}"}, "premium": premium},
        )

    # destination shapes
    for typed in [str(DEST), f"{DEST}|9", "pm", "@named", DEST]:
        yield f"dest_{typed}", {"attrs": {"up_dest": typed}}

    # transmission request flags
    for bot_trans, user_trans in itertools.product([False, True], repeat=2):
        yield (
            f"flags_bot{bot_trans}_user{user_trans}",
            {
                "attrs": {
                    "up_dest": DEST,
                    "bot_trans": bot_trans,
                    "user_trans": user_trans,
                },
                "user_dict": {"USER_TRANSMISSION": True, "HYBRID_LEECH": True},
            },
        )

def _lookup_scenarios():
    """What telegram answered, including the answers that are not verdicts."""
    # lookup outcomes, for a bot-only task and for a user-session task
    outcomes = {
        "found": [chat()],
        "missing": [None],
        "flood": [new_mod.ChatLookupError("rate limited")],
        "private": [chat(kind="PRIVATE")],
        "group_not_admin": [chat(is_admin=False)],
    }
    for label, answers in outcomes.items():
        for ut in [False, True]:
            yield (
                f"chat_{label}_ut{ut}",
                {
                    "attrs": {"up_dest": DEST, "user_trans": ut},
                    "user_dict": {"HYBRID_LEECH": True} if ut else {},
                    "chat": answers * 2,
                },
            )

    # privilege outcomes
    privs = {
        "full": [member()],
        "no_manage": [member(can_manage=False)],
        "no_delete": [member(can_delete=False)],
        "flood": [new_mod.ChatLookupError("rate limited")],
    }
    for label, answers in privs.items():
        for ut in [False, True]:
            yield (
                f"member_{label}_ut{ut}",
                {
                    "attrs": {"up_dest": DEST, "user_trans": ut},
                    "user_dict": {"HYBRID_LEECH": True} if ut else {},
                    "member": answers * 2,
                },
            )

    # reachability of a private destination
    for label, answers in {
        "yes": [True],
        "no": [False],
        "flood": [new_mod.ChatLookupError("rate limited")],
    }.items():
        for ut in [False, True]:
            yield (
                f"reach_{label}_ut{ut}",
                {
                    "attrs": {"up_dest": USER, "user_trans": ut},
                    "chat": [chat(kind="PRIVATE")] * 2,
                    "reach": answers * 2,
                },
            )

def _setting_scenarios():
    """The three-tier fallback, on both string and boolean settings."""
    # three-tier settings: absent / set / emptied
    for key, cfg, val in [
        ("NAME_SUBSTITUTE", "g/global", "u/user"),
        ("THUMBNAIL_LAYOUT", "2x2", "3x3"),
        ("EXCLUDED_EXTENSIONS", ["cfg"], ["usr"]),
        ("INCLUDED_EXTENSIONS", ["cfg"], ["usr"]),
    ]:
        for variant, ud in [
            ("absent", {}),
            ("set", {key: val}),
            ("emptied", {key: type(val)()}),
        ]:
            yield (
                f"setting_{key}_{variant}",
                {"user_dict": ud, "config": {key: cfg}},
            )

    # boolean settings
    for key in ["USER_TRANSMISSION", "HYBRID_LEECH", "EQUAL_SPLITS", "AS_DOCUMENT"]:
        for cfg, variant, ud in [
            (True, "absent", {}),
            (True, "off", {key: False}),
            (True, "on", {key: True}),
            (False, "absent", {}),
            (False, "on", {key: True}),
        ]:
            yield (
                f"flag_{key}_{variant}_cfg{cfg}",
                {"attrs": {"up_dest": DEST}, "user_dict": ud, "config": {key: cfg}},
            )

    # as_doc / as_med interaction
    for as_doc, as_med in itertools.product([False, True], repeat=2):
        yield (
            f"format_doc{as_doc}_med{as_med}",
            {"attrs": {"as_doc": as_doc, "as_med": as_med}},
        )

def _split_and_ffmpeg_scenarios():
    """Split ceilings, and the preset expansion that can drop a command."""
    # split sizes
    for size, premium, ut in itertools.product(
        ["", "5000", "1GB", "8GB"], [True, False], [False, True]
    ):
        yield (
            f"split_{size or 'none'}_p{premium}_ut{ut}",
            {
                "attrs": {"up_dest": DEST, "split_size": size, "user_trans": ut},
                "premium": premium,
            },
        )

    # ffmpeg presets
    ff_cases = [
        ("literal", [("-i", "a.mkv")], {}),
        ("named", ["mine"], {"FFMPEG_CMDS": {"mine": ["-c copy"]}}),
        (
            "filled",
            ["mine"],
            {
                "FFMPEG_CMDS": {"mine": ["-crf {q}"]},
                "FFMPEG_VARIABLES": {"mine": {"0": {"q": "18"}}},
            },
        ),
        (
            "half_filled",
            ["mine"],
            {
                "FFMPEG_CMDS": {"mine": ["-crf {q} -vf {s}"]},
                "FFMPEG_VARIABLES": {"mine": {"0": {"q": "18"}}},
            },
        ),
        ("unknown_key", ["absent"], {"FFMPEG_CMDS": {"mine": ["-c copy"]}}),
        ("no_presets_at_all", ["mine"], {}),
        ("empty_user_presets", ["mine"], {"FFMPEG_CMDS": {}}),
        ("mixed", [("-x",), "mine"], {"FFMPEG_CMDS": {"mine": ["-c copy"]}}),
        ("none_requested", None, {"FFMPEG_CMDS": {"mine": ["-c copy"]}}),
    ]
    for label, cmds, ud in ff_cases:
        for cfg in [{}, {"mine": ["-cfg preset"]}]:
            yield (
                f"ffmpeg_{label}_cfg{bool(cfg)}",
                {
                    "attrs": {"ffmpeg_cmds": cmds},
                    "user_dict": ud,
                    "config": {"FFMPEG_CMDS": cfg},
                },
            )

def _dump_and_thumb_scenarios():
    """Every shape a clone dump chat can be configured in, plus the thumbnail."""
    # clone dump chats
    dumps = [
        DEST,
        str(DEST),
        f"{DEST}|7",
        "pm",
        "@named",
        f"[{DEST}, 'pm']",
        f"['{DEST}|7', '@named']",
        [DEST, "pm"],
        "",
        {},
    ]
    for i, configured in enumerate(dumps):
        for where in ["user", "config"]:
            yield (
                f"dump_{i}_{where}",
                {
                    "user_dict": {"CLONE_DUMP_CHATS": configured}
                    if where == "user"
                    else {},
                    "config": {
                        "CLONE_DUMP_CHATS": configured if where == "config" else {}
                    },
                },
            )

    # thumb: "none" and a plain path must both be left alone
    for thumb in [None, "none", "/tmp/thumb.jpg"]:
        yield f"thumb_{thumb}", {"attrs": {"thumb": thumb}}


def main():
    passed = failed = 0
    for name, scenario in scenarios():
        old = run(old_mod, scenario)
        new = run(new_mod, scenario)
        if old == new:
            passed += 1
            continue
        failed += 1
        print(f"\n✗ {name}")
        o_out, o_state, o_log = old
        n_out, n_state, n_log = new
        if o_out != n_out:
            print(f"    outcome: old={o_out!r}  new={n_out!r}")
        for key in TRACKED:
            if o_state[key] != n_state[key]:
                print(f"    {key}: old={o_state[key]!r}  new={n_state[key]!r}")
        if o_log != n_log:
            print(f"    logs: old={o_log!r}\n          new={n_log!r}")

    print(f"\n{passed} identical, {failed} divergent, {passed + failed} scenarios")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
