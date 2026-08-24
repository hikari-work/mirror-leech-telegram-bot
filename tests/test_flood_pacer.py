"""Unit tests for the inter-file pacing extracted out of TelegramUploader.

The gap between two files starts at nothing, is widened by every flood telegram
answers with, and decays once telegram stops complaining. None of that needs an
upload in progress, so none of these tests build one.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType
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


class _Err(Exception):
    pass


TARGET = "bot.helper.mirror_leech_utils.upload_utils.flood_pacer"


@pytest.fixture
def pacer_module(monkeypatch):
    """Import the pacer with pyrogram and the bot package chain stubbed out."""
    root = Path(__file__).resolve().parent.parent
    modules = {
        "pyrogram": _pkg("pyrogram"),
        "pyrogram.errors": _stub(
            "pyrogram.errors",
            FloodWait=type("FloodWait", (_Err,), {}),
            FloodPremiumWait=type("FloodPremiumWait", (_Err,), {}),
        ),
        "bot": _pkg("bot"),
        "bot.helper": _pkg("bot.helper"),
        # Real path, not a stub: the pacer reads a flood's wait through
        # ``telegram_helper.flood``, and these tests assert the number it comes
        # back with, so the module under the assertions has to be the real one.
        # It needs nothing but the stubbed ``pyrogram.errors`` to import.
        "bot.helper.telegram_helper": _pkg(
            "bot.helper.telegram_helper",
            str(root / "bot" / "helper" / "telegram_helper"),
        ),
        "bot.helper.mirror_leech_utils": _pkg("bot.helper.mirror_leech_utils"),
        "bot.helper.mirror_leech_utils.upload_utils": _pkg(
            "bot.helper.mirror_leech_utils.upload_utils",
            str(root / "bot" / "helper" / "mirror_leech_utils" / "upload_utils"),
        ),
    }
    for name, mod in modules.items():
        monkeypatch.setitem(sys.modules, name, mod)

    sys.modules.pop(TARGET, None)
    module = importlib.import_module(TARGET)
    yield module
    sys.modules.pop(TARGET, None)
    # The real flood module imported above binds the stubbed error classes, so
    # it is dropped with the pacer rather than handed to the next test file.
    sys.modules.pop("bot.helper.telegram_helper.flood", None)


def _pacer(pacer_module, monkeypatch, is_cancelled=lambda: False):
    """A pacer whose sleeps are recorded instead of waited out."""
    slept = []
    monkeypatch.setattr(
        pacer_module, "sleep", AsyncMock(side_effect=lambda d: slept.append(d))
    )
    return pacer_module.FloodPacer(is_cancelled), slept


# --- the gap between files -------------------------------------------------


@pytest.mark.asyncio
async def test_files_follow_each_other_with_no_gap_by_default(
    pacer_module, monkeypatch
):
    pacer, slept = _pacer(pacer_module, monkeypatch)

    for _ in range(3):
        await pacer.pace()

    assert slept == []


@pytest.mark.asyncio
async def test_each_flood_widens_the_gap(pacer_module, monkeypatch):
    pacer, slept = _pacer(pacer_module, monkeypatch)

    pacer.note_flood()
    await pacer.pace()
    pacer.note_flood()
    await pacer.pace()

    assert slept == [0.5, 1.0]


def test_the_gap_never_grows_past_the_cap(pacer_module, monkeypatch):
    pacer, _ = _pacer(pacer_module, monkeypatch)

    for _ in range(20):
        pacer.note_flood()

    assert pacer._pace == pacer._MAX_PACE


@pytest.mark.asyncio
async def test_the_gap_decays_once_the_floods_stop(pacer_module, monkeypatch):
    pacer, slept = _pacer(pacer_module, monkeypatch)
    pacer.note_flood()

    for _ in range(pacer._CALM_FILES):
        await pacer.pace()
    assert pacer._pace == 0
    assert slept == [0.5] * pacer._CALM_FILES

    await pacer.pace()
    assert len(slept) == pacer._CALM_FILES, "gap should be gone, not slept again"


# --- waiting out a flood limit ---------------------------------------------


@pytest.mark.asyncio
async def test_a_flood_on_any_call_widens_the_gap(pacer_module, monkeypatch):
    pacer, slept = _pacer(pacer_module, monkeypatch)
    flood = pacer_module.FloodWait()
    flood.value = 2
    attempts = []

    async def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise flood
        return "ok"

    assert await pacer.guard(flaky) == "ok"
    assert pacer._pace == 0.5
    assert slept == [2 * pacer_module.FLOOD_SLACK], "telegram's wait, plus a margin"


@pytest.mark.asyncio
async def test_a_flood_is_waited_out_as_many_times_as_it_takes(
    pacer_module, monkeypatch
):
    pacer, slept = _pacer(pacer_module, monkeypatch)
    attempts = []

    async def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            flood = pacer_module.FloodPremiumWait()
            flood.value = 1
            raise flood
        return "ok"

    assert await pacer.guard(flaky) == "ok"
    assert len(attempts) == 3
    assert len(slept) == 2


@pytest.mark.asyncio
async def test_a_guarded_call_passes_its_arguments_through(pacer_module, monkeypatch):
    pacer, _ = _pacer(pacer_module, monkeypatch)
    seen = {}

    async def send(chat_id, text=None):
        seen.update(chat_id=chat_id, text=text)
        return "sent"

    assert await pacer.guard(send, -1001, text="hi") == "sent"
    assert seen == {"chat_id": -1001, "text": "hi"}


@pytest.mark.asyncio
async def test_a_canceled_task_never_reaches_telegram(pacer_module, monkeypatch):
    pacer, _ = _pacer(pacer_module, monkeypatch, is_cancelled=lambda: True)
    calls = []

    async def send():
        calls.append(1)

    assert await pacer.guard(send) is None
    assert calls == []


@pytest.mark.asyncio
async def test_a_cancel_during_the_wait_ends_the_retry_loop(pacer_module, monkeypatch):
    """The cancel is read at the top of every attempt, so the retry after a
    flood is where a task cancelled mid-sleep stops."""
    cancelled = []
    pacer, _ = _pacer(
        pacer_module, monkeypatch, is_cancelled=lambda: bool(cancelled)
    )
    attempts = []

    async def flaky():
        attempts.append(1)
        cancelled.append(1)
        flood = pacer_module.FloodWait()
        flood.value = 1
        raise flood

    assert await pacer.guard(flaky) is None
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_errors_other_than_a_flood_are_not_retried(pacer_module, monkeypatch):
    pacer, _ = _pacer(pacer_module, monkeypatch)
    attempts = []

    async def broken():
        attempts.append(1)
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        await pacer.guard(broken)
    assert len(attempts) == 1
    assert pacer._pace == 0.0, "a non-flood error says nothing about the pace"
