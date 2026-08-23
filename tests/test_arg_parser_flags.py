"""Tests for the ``-ad`` and ``-c`` CLI flags."""

from __future__ import annotations

import importlib
import sys
from types import ModuleType

import pytest


def _stub_bot_package(monkeypatch):
    bot_pkg = ModuleType("bot")
    bot_pkg.LOGGER = type("L", (), {"info": staticmethod(lambda *a, **k: None)})
    helper_pkg = ModuleType("bot.helper")
    ext_utils_pkg = ModuleType("bot.helper.ext_utils")
    monkeypatch.setitem(sys.modules, "bot", bot_pkg)
    monkeypatch.setitem(sys.modules, "bot.helper", helper_pkg)
    monkeypatch.setitem(sys.modules, "bot.helper.ext_utils", ext_utils_pkg)


@pytest.fixture
def arg_parser(monkeypatch):
    """Import only ``arg_parser`` from bot_utils without firing module-level
    side effects elsewhere in the package."""
    _stub_bot_package(monkeypatch)
    sys.modules.pop("bot.helper.ext_utils.bot_utils", None)
    # ``bot_utils`` itself imports several Telegram-only helpers, so we
    # load it from source via execfile-style trick to avoid pulling in
    # the full bot stack.
    from importlib import util
    from pathlib import Path

    file_path = (
        Path(__file__).resolve().parent.parent
        / "bot"
        / "helper"
        / "ext_utils"
        / "bot_utils.py"
    )
    src = file_path.read_text(encoding="utf-8")
    # Strip imports that drag in Telegram + DB dependencies; we only
    # need ``arg_parser`` for these tests.
    namespace: dict[str, object] = {}
    # Provide minimal stubs the function references.
    namespace["loads"] = __import__("ast").literal_eval
    snippet_start = src.find("def arg_parser(")
    snippet_end = src.find("\ndef ", snippet_start + 1)
    if snippet_end == -1:
        snippet_end = len(src)
    snippet = src[snippet_start:snippet_end]
    exec(snippet, namespace)  # noqa: S102 - test-only controlled exec
    return namespace["arg_parser"]


def test_ad_bool_flag_set(arg_parser):
    args = {"-ad": False, "-z": False, "link": ""}
    arg_parser(["http://x", "-ad"], args)
    assert args["-ad"] is True
    assert args["link"] == "http://x"


def test_unknown_flag_left_alone(arg_parser):
    args = {"-ad": False, "link": ""}
    arg_parser(["http://x", "-unknown"], args)
    assert args["-ad"] is False


def test_copy_preset_name_is_read_as_the_flags_value(arg_parser):
    args = {"-c": "", "link": ""}
    arg_parser(["http://x", "-c", "anime"], args)
    assert args["-c"] == "anime"
    assert args["link"] == "http://x"


def test_a_copy_preset_does_not_swallow_the_next_flag(arg_parser):
    """A value-taking flag reads until the next known one, so a preset name
    sitting in front of ``-doc`` must not absorb it."""
    args = {"-c": "", "-doc": False, "link": ""}
    arg_parser(["http://x", "-c", "anime", "-doc"], args)
    assert args["-c"] == "anime"
    assert args["-doc"] is True


def test_a_repeated_copy_preset_flag_keeps_the_last_name(arg_parser):
    """``arg_parser`` used to carry a dead ``-c`` branch that appended a repeated
    flag into the first one's value, giving the nonsense name
    ``anime -c music``. With it gone, ``-c`` behaves like every other
    value-taking flag: the last one wins.
    """
    args = {"-c": "", "link": ""}
    arg_parser(["http://x", "-c", "anime", "-c", "music"], args)
    assert args["-c"] == "music"


def test_a_copy_preset_flag_with_nothing_after_it_stays_empty(arg_parser):
    args = {"-c": "", "link": ""}
    arg_parser(["http://x", "-c"], args)
    assert args["-c"] == ""
