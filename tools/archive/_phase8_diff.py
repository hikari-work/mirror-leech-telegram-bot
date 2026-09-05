"""Differential harness: handler registration before vs after Fase 8.

Loads the pre-refactor `bot/core/handlers.py` out of git and the working-tree
version, runs `add_handlers()` on both against a recording `TgClient.bot`, and
compares two things:

1. **Structure** — for every registered handler: its class, its callback, and a
   recursive description of its filter tree (command set + prefixes +
   case-sensitivity, regex pattern + flags, and the identity of every custom
   filter function). Compared as an ordered list *per handler kind*, because
   that is the order pyrogram's dispatcher actually walks.

2. **Dispatch** — a replay of `pyrogram/dispatcher.py::handler_worker` over
   ~2300 fake updates (every command and alias, every callback-data prefix, four
   caller identities, plus case variants, prefix collisions and truncations).
   For each update both registrations must elect the *same* callback, or no
   callback at all. This is the check that matters: it exercises the filters
   instead of just describing them.

Interleaving between kinds is expected to differ — the old code registered
message and callback handlers alternately, the tables register all commands then
all callbacks. That reordering cannot change dispatch: `MessageHandler`,
`EditedMessageHandler` and `CallbackQueryHandler` each subclass `Handler`
directly, so the dispatcher's `isinstance(handler, handler_type)` gate selects
exactly one kind per update. The dispatch replay above is what proves it.

Exit code 0 means every check matched. Usage: python tools/_phase8_diff.py
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType, SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
TARGET = "bot/core/handlers.py"
OLD_REF = os.environ.get("PHASE8_OLD_REF", "HEAD")

sys.path.insert(0, str(ROOT))

from bot.helper.telegram_helper.bot_commands import BotCommands  # noqa: E402
from pyrogram.filters import AndFilter, InvertFilter, OrFilter  # noqa: E402
from pyrogram.handlers import (  # noqa: E402
    CallbackQueryHandler,
    EditedMessageHandler,
    MessageHandler,
)
from pyrogram.types import CallbackQuery, Message  # noqa: E402

import bot  # noqa: E402
from bot.core.config_manager import Config  # noqa: E402
from bot.core.telegram_manager import TgClient  # noqa: E402

# --------------------------------------------------------------------------
# loading both versions
# --------------------------------------------------------------------------


def _load(source: str, modname: str) -> ModuleType:
    mod = ModuleType(modname)
    mod.__package__ = "bot.core"
    mod.__file__ = str(ROOT / TARGET)
    sys.modules[modname] = mod
    exec(compile(source, mod.__file__, "exec"), mod.__dict__)
    return mod


def load_pair() -> tuple[ModuleType, ModuleType]:
    old_src = subprocess.run(
        ["git", "show", f"{OLD_REF}:{TARGET}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return _load(old_src, "_handlers_old"), _load(
        (ROOT / TARGET).read_text(), "_handlers_new"
    )


class Recorder:
    """Stands in for `TgClient.bot` and keeps every registration in order."""

    def __init__(self):
        self.calls: list[tuple[object, int]] = []

    def add_handler(self, handler, group: int = 0):
        self.calls.append((handler, group))


def register(mod: ModuleType) -> list[tuple[object, int]]:
    recorder = Recorder()
    TgClient.bot = recorder
    mod.add_handlers()
    TgClient.bot = None
    return recorder.calls


# --------------------------------------------------------------------------
# structural description
# --------------------------------------------------------------------------


def describe_filter(flt) -> tuple:
    if flt is None:
        return ("none",)
    if isinstance(flt, AndFilter):
        return ("and", describe_filter(flt.base), describe_filter(flt.other))
    if isinstance(flt, OrFilter):
        return ("or", describe_filter(flt.base), describe_filter(flt.other))
    if isinstance(flt, InvertFilter):
        return ("not", describe_filter(flt.base))

    name = type(flt).__name__
    if name == "CommandFilter":
        return (
            "command",
            tuple(sorted(flt.commands)),
            tuple(sorted(flt.prefixes)),
            flt.case_sensitive,
        )
    if name == "RegexFilter":
        return ("regex", flt.p.pattern, flt.p.flags)
    # Custom filter built by `filters.create`: pin the underlying function, not
    # just the generated class name.
    call = type(flt).__call__
    return ("custom", name, call.__module__, call.__qualname__)


def describe(handler, group: int) -> tuple:
    return (
        type(handler).__name__,
        handler.callback.__module__,
        handler.callback.__qualname__,
        group,
        describe_filter(handler.filters),
    )


# --------------------------------------------------------------------------
# fake world for the dispatch replay
# --------------------------------------------------------------------------

OWNER_ID, SUDO_ID, AUTH_ID, STRANGER_ID = 1, 2, 3, 4
CHAT_ID = 500

CLIENT = SimpleNamespace(me=SimpleNamespace(username="testbot"))


def install_world() -> None:
    Config.OWNER_ID = OWNER_ID
    bot.user_data.clear()
    bot.user_data[SUDO_ID] = {"SUDO": True}
    bot.user_data[AUTH_ID] = {"AUTH": True}
    bot.sudo_users.clear()
    bot.auth_chats.clear()


def make_message(text: str, uid: int) -> Message:
    msg = Message.__new__(Message)
    msg.text = text
    msg.caption = None
    msg.command = None
    msg.matches = None
    msg.from_user = SimpleNamespace(id=uid)
    msg.sender_chat = None
    msg.chat = SimpleNamespace(id=CHAT_ID)
    msg.topic_message = False
    msg.message_thread_id = None
    return msg


def make_query(data: str, uid: int) -> CallbackQuery:
    query = CallbackQuery.__new__(CallbackQuery)
    query.data = data
    query.matches = None
    query.from_user = SimpleNamespace(id=uid)
    query.sender_chat = None
    return query


async def dispatch(calls, update, handler_type) -> str | None:
    """Replay `dispatcher.handler_worker`: first matching handler wins."""
    for handler, _group in calls:
        if not isinstance(handler, handler_type):
            continue
        if await handler.check(CLIENT, update):
            return handler.callback.__qualname__
    return None


# --------------------------------------------------------------------------
# scenarios
# --------------------------------------------------------------------------

USERS = (
    ("owner", OWNER_ID),
    ("sudo", SUDO_ID),
    ("auth", AUTH_ID),
    ("stranger", STRANGER_ID),
)


def command_words() -> list[str]:
    words: list[str] = []
    for name in dir(BotCommands):
        if name.startswith("_"):
            continue
        value = getattr(BotCommands, name)
        words.extend(value if isinstance(value, list) else [value])
    return words


def near_misses(word: str) -> list[str]:
    """Truncations and a last-character flip.

    Without these, narrowing a pattern is invisible to the replay: `^rss` and
    `^rs` both match every scenario that starts with "rss". The truncations are
    the only inputs that tell the two apart.
    """
    out = {word[:i] for i in range(1, len(word))}
    out.add(word[:-1] + ("x" if word[-1:] != "x" else "y"))
    return sorted(out - {word})


def message_scenarios() -> list[tuple[str, Message, type]]:
    out = []
    for word in sorted(set(command_words())):
        for label, uid in USERS:
            for text, tag in (
                (f"/{word}", "bare"),
                (f"/{word} https://example.com/x", "arg"),
                (f"/{word.upper()}", "upper"),
                (f"/{word}@testbot", "mention"),
                (f"/{word}xyz", "suffix"),
                (f"!{word}", "badprefix"),
                (f"/{word}", "edited"),
            ):
                kind = EditedMessageHandler if tag == "edited" else MessageHandler
                out.append(
                    (f"msg/{tag}/{word}/{label}", make_message(text, uid), kind)
                )
            for miss in near_misses(word):
                out.append(
                    (
                        f"msg/near/{word}->{miss}/{label}",
                        make_message(f"/{miss}", uid),
                        MessageHandler,
                    )
                )
    for label, uid in USERS:
        for text in ("/unknowncmd", "no command at all", "/", "/leech\n/exec"):
            out.append(
                (f"msg/other/{text!r}/{label}", make_message(text, uid), MessageHandler)
            )
    return out


def query_scenarios() -> list[tuple[str, CallbackQuery, type]]:
    prefixes = (
        "botset",
        "botrestart",
        "canall",
        "stopm",
        "sel",
        "help",
        "rss",
        "status",
        "torser",
        "userset",
        "unknown",
        "sta",
        "botsetx",
    )
    out = []
    for prefix in prefixes:
        for label, uid in USERS:
            variants = [prefix, f"{prefix} 1 2", f"x{prefix}", *near_misses(prefix)]
            for data in variants:
                out.append(
                    (
                        f"cb/{data}/{label}",
                        make_query(data, uid),
                        CallbackQueryHandler,
                    )
                )
    return out


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def compare_structure(old_desc: list[tuple], new_desc: list[tuple]) -> list[str]:
    failures: list[str] = []

    if Counter(old_desc) != Counter(new_desc):
        for item in Counter(old_desc) - Counter(new_desc):
            failures.append(f"lost:  {item}")
        for item in Counter(new_desc) - Counter(old_desc):
            failures.append(f"added: {item}")

    for kind in ("MessageHandler", "EditedMessageHandler", "CallbackQueryHandler"):
        old_kind = [d for d in old_desc if d[0] == kind]
        new_kind = [d for d in new_desc if d[0] == kind]
        status = "same order" if old_kind == new_kind else "ORDER CHANGED"
        print(f"  {kind:<22} {len(old_kind):>2} handlers, {status}")
        for i, (a, b) in enumerate(zip(old_kind, new_kind, strict=False)):
            if a != b:
                failures.append(f"{kind}[{i}] {a[2]} -> {b[2]}")

    print(
        "  cross-kind interleaving: "
        + ("differs (expected)" if old_desc != new_desc else "identical")
    )
    return failures


async def compare_dispatch(old_calls, new_calls) -> list[str]:
    failures: list[str] = []
    scenarios = message_scenarios() + query_scenarios()

    for name, update, handler_type in scenarios:
        old_winner = await dispatch(old_calls, update, handler_type)
        # Filters mutate the update (`message.command`, `update.matches`), so the
        # second replay gets a freshly built one.
        fresh = (
            make_message(update.text, update.from_user.id)
            if isinstance(update, Message)
            else make_query(update.data, update.from_user.id)
        )
        new_winner = await dispatch(new_calls, fresh, handler_type)
        if old_winner != new_winner:
            failures.append(f"dispatch {name}: {old_winner} -> {new_winner}")

    print(f"dispatch replay: {len(scenarios)} scenarios, {len(failures)} mismatch")
    return failures


async def run() -> int:
    old_mod, new_mod = load_pair()
    old_calls, new_calls = register(old_mod), register(new_mod)
    install_world()

    print(f"registrations: old={len(old_calls)} new={len(new_calls)}")
    failures: list[str] = []
    if len(old_calls) != len(new_calls):
        failures.append(f"handler count {len(old_calls)} -> {len(new_calls)}")

    failures += compare_structure(
        [describe(h, g) for h, g in old_calls],
        [describe(h, g) for h, g in new_calls],
    )
    failures += await compare_dispatch(old_calls, new_calls)

    if failures:
        print("\nFAILURES")
        for line in failures:
            print(f"  {line}")
        return 1

    print("\nOK — registration and dispatch identical")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
