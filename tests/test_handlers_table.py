"""What `add_handlers()` registers, pinned against the pre-refactor listing.

Fase 8 replaced 246 lines of `TgClient.bot.add_handler(...)` calls with the two
tables in `bot/core/handlers.py`. The tables decide who may run every command in
the bot, so `EXPECTED_COMMANDS` / `EXPECTED_CALLBACKS` below are transcribed by
hand from the old listing (commit d92de95) rather than generated from the new
tables — an expectation derived from the code under test would agree with any
reordering, including a wrong one.

Order matters and is asserted: pyrogram puts every handler in group 0 and its
dispatcher stops at the first handler that matches, so two rows claiming the
same command means the second one is dead code.

The registration itself is collected in a subprocess. Importing the real `bot`
package installs uvloop's event loop policy, builds an event loop and starts a
scheduler; every other test file in this suite avoids that by stubbing or
exec'ing from source, and this one keeps the pytest process equally clean.

`CMD_SUFFIX` is empty in the subprocess (no config is loaded), so the command
words below are the bare ones.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_MARKER = "<<<HANDLERS-JSON>>>"

# Access filters, by the name of the function `filters.create()` was given.
_ACCESS = {
    "CustomFilters.owner_filter": "owner",
    "CustomFilters.sudo_user": "sudo",
    "CustomFilters.authorized_user": "auth",
    None: None,
}

# (callback, sorted command words, who may run it) — in registration order.
EXPECTED_COMMANDS = [
    ("authorize", ["auth"], "sudo"),
    ("unauthorize", ["unauth"], "sudo"),
    ("add_sudo", ["addsudo"], "owner"),
    ("remove_sudo", ["rmsudo"], "owner"),
    ("send_bot_settings", ["bs", "bsetting"], "sudo"),
    ("cancel", ["c", "cancel"], "auth"),
    ("cancel_all_buttons", ["cancelall"], "auth"),
    ("aioexecute", ["aexec"], "owner"),
    ("execute", ["exec"], "owner"),
    ("clear", ["clearlocals"], "owner"),
    ("select", ["sel"], "auth"),
    ("remove_from_queue", ["forcestart", "fs"], "auth"),
    ("bypass_scrape_cmd", ["bp", "bypass"], "auth"),
    ("leech", ["l", "leech"], "auth"),
    ("qb_leech", ["qbleech", "ql"], "auth"),
    ("get_rss_menu", ["rss"], "auth"),
    ("run_shell", ["shell"], "owner"),
    ("start", ["start"], None),
    ("log", ["log"], "sudo"),
    ("restart_bot", ["restart"], "sudo"),
    ("ping", ["ping"], "auth"),
    ("bot_help", ["help"], "auth"),
    ("bot_stats", ["stats"], "auth"),
    ("task_status", ["status"], "auth"),
    ("torrent_search", ["search"], "auth"),
    ("get_users_settings", ["users"], "sudo"),
    ("send_user_settings", ["us", "usetting"], "auth"),
    ("user_login", ["login"], "auth"),
    ("user_logout", ["logout"], "auth"),
    ("copy_task", ["copy"], "auth"),
    ("ytdl_leech", ["yl", "ytdlleech"], "auth"),
]

# (callback, callback-data pattern, who may press it) — in registration order.
EXPECTED_CALLBACKS = [
    ("edit_bot_settings", "^botset", "sudo"),
    ("cancel_all_update", "^canall", None),
    ("cancel_multi", "^stopm", None),
    ("confirm_selection", "^sel", None),
    ("arg_usage", "^help", None),
    ("rss_listener", "^rss", None),
    ("confirm_restart", "^botrestart", "sudo"),
    ("status_pages", "^status", None),
    ("torrent_search_update", "^torser", None),
    ("edit_user_settings", "^userset", None),
    ("copy_choice", "^copyt", None),
]

# Runs inside the subprocess. Filter trees are only ever a match filter or
# `match & access`; anything else is reported as "unexpected" so a new shape
# fails the tests instead of being quietly flattened.
_DUMP = f"""
import json
from types import SimpleNamespace

from pyrogram.filters import AndFilter

from bot.core import handlers
from bot.core.telegram_manager import TgClient
from bot.helper.telegram.bot_commands import BotCommands

recorded = []
TgClient.bot = SimpleNamespace(add_handler=recorded.append)
handlers.add_handlers()


def match(flt):
    name = type(flt).__name__
    if name == "CommandFilter":
        return {{
            "type": "command",
            "words": sorted(flt.commands),
            "prefixes": sorted(flt.prefixes),
            "case_sensitive": flt.case_sensitive,
        }}
    if name == "RegexFilter":
        return {{"type": "regex", "pattern": flt.p.pattern}}
    return {{"type": "unexpected", "name": name}}


rows = []
for handler in recorded:
    flt = handler.filters
    if isinstance(flt, AndFilter):
        described, access = match(flt.base), type(flt.other).__call__.__qualname__
    else:
        described, access = match(flt), None
    rows.append({{
        "kind": type(handler).__name__,
        "callback": handler.callback.__qualname__,
        "module": handler.callback.__module__,
        "match": described,
        "access": access,
    }})

commands = {{
    name: getattr(BotCommands, name)
    for name in dir(BotCommands)
    if not name.startswith("_")
}}
print("{_MARKER}" + json.dumps({{"rows": rows, "bot_commands": commands}}))
"""


@pytest.fixture(scope="module")
def registered(tmp_path_factory):
    """Every handler `add_handlers()` registered, in order."""
    proc = subprocess.run(
        [sys.executable, "-c", _DUMP],
        cwd=tmp_path_factory.mktemp("handlers"),  # keep log.txt out of the repo
        env={"PYTHONPATH": str(_ROOT), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    payload = next(
        line[len(_MARKER) :]
        for line in proc.stdout.splitlines()
        if line.startswith(_MARKER)
    )
    return json.loads(payload)


def _of_kind(registered, kind):
    return [row for row in registered["rows"] if row["kind"] == kind]


def _summarise(row):
    match = row["match"]
    key = match["words"] if match["type"] == "command" else match["pattern"]
    assert row["access"] in _ACCESS, f"unknown access filter {row['access']}"
    return (row["callback"], key, _ACCESS[row["access"]])


def test_commands_match_the_pre_refactor_listing(registered):
    rows = _of_kind(registered, "MessageHandler")
    assert [_summarise(row) for row in rows] == EXPECTED_COMMANDS


def test_callbacks_match_the_pre_refactor_listing(registered):
    rows = _of_kind(registered, "CallbackQueryHandler")
    assert [_summarise(row) for row in rows] == EXPECTED_CALLBACKS


def test_shell_is_the_only_command_that_also_fires_on_edit(registered):
    edited = _of_kind(registered, "EditedMessageHandler")
    assert [_summarise(row) for row in edited] == [("run_shell", ["shell"], "owner")]

    # The edited twin must be gated exactly like the plain command: an edit is
    # still a way to run `/shell`.
    plain = next(
        row
        for row in _of_kind(registered, "MessageHandler")
        if row["callback"] == "run_shell"
    )
    assert edited[0]["match"] == plain["match"]
    assert edited[0]["access"] == plain["access"]


def test_start_is_the_only_command_open_to_everyone(registered):
    open_commands = [
        row["callback"]
        for row in _of_kind(registered, "MessageHandler")
        if row["access"] is None
    ]
    assert open_commands == ["start"]


def test_every_command_is_case_sensitive_and_slash_prefixed(registered):
    for row in registered["rows"]:
        if row["match"]["type"] != "command":
            continue
        assert row["match"]["case_sensitive"] is True, row["callback"]
        assert row["match"]["prefixes"] == ["/"], row["callback"]


def test_no_command_word_is_claimed_twice(registered):
    words = [
        word
        for row in _of_kind(registered, "MessageHandler")
        for word in row["match"]["words"]
    ]
    duplicates = sorted({word for word in words if words.count(word) > 1})
    assert not duplicates, f"first handler registered wins, rest are dead: {duplicates}"


def test_every_declared_bot_command_is_wired(registered):
    """A `BotCommands` entry nobody registered is a command that does nothing."""
    wired = {
        word
        for row in _of_kind(registered, "MessageHandler")
        for word in row["match"]["words"]
    }
    declared = {
        name: value if isinstance(value, list) else [value]
        for name, value in registered["bot_commands"].items()
    }
    missing = {
        name: words for name, words in declared.items() if not set(words) <= wired
    }
    assert not missing


def test_no_callback_pattern_shadows_a_later_one(registered):
    """`^sel` registered before `^select` would swallow every `select…` press."""
    patterns = [
        row["match"]["pattern"]
        for row in _of_kind(registered, "CallbackQueryHandler")
    ]
    shadowed = [
        (earlier, later)
        for i, earlier in enumerate(patterns)
        for later in patterns[i + 1 :]
        if later.lstrip("^").startswith(earlier.lstrip("^"))
    ]
    assert not shadowed


def test_no_filter_shape_is_unexpected(registered):
    unexpected = [
        row for row in registered["rows"] if row["match"]["type"] == "unexpected"
    ]
    assert unexpected == []
