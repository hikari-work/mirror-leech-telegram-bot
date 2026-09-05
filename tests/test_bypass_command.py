"""Tests for the /bypass command's shortlink branch and its dispatcher predicate.

/bypass used to answer a Linkvertise link with "No bypass scraper for
linkvertise.com": the command only knew about thread scrapers, while the
shortener resolvers were reachable from /mirror and /leech alone. These pin the
two behaviours the command now has to keep apart — resolve a shortlink and stop,
versus resolve it and carry on into the page prompt when the target is a thread.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_DU = "bot.helper.download"
_DISPATCHER = f"{_DU}.bypass_dispatcher"


class _DirectDownloadLinkException(Exception):
    pass


@pytest.fixture
def dispatcher(monkeypatch):
    """Import ``bypass_dispatcher`` without the side-effectful ``bot`` package."""
    pkgs = {}
    for name in (
        "bot",
        "bot.helper",
        "bot.helper.util",
        _DU,
    ):
        mod = ModuleType(name)
        mod.__path__ = []
        pkgs[name] = mod
    pkgs[_DU].__path__ = [
        str(_ROOT / "bot" / "helper" / "download")
    ]

    exceptions_mod = ModuleType("bot.helper.util.exceptions")
    exceptions_mod.DirectDownloadLinkException = _DirectDownloadLinkException
    pkgs["bot.helper.util.exceptions"] = exceptions_mod

    semprot_mod = ModuleType(f"{_DU}.semprot_scraper")
    semprot_mod.scrape_pages = AsyncMock(return_value=("Title", [], 1))
    pkgs[f"{_DU}.semprot_scraper"] = semprot_mod

    for name, mod in pkgs.items():
        monkeypatch.setitem(sys.modules, name, mod)

    sys.modules.pop(_DISPATCHER, None)
    return importlib.import_module(_DISPATCHER)


@pytest.fixture
def bypass_cmd(monkeypatch):
    """Import ``bot.modules.bypass`` with its bot-side dependencies stubbed.

    Returns (module, stubs) where ``stubs`` exposes the doubles the command
    talks to, so a test can assert on what the user was actually shown.
    """
    stubs = SimpleNamespace(
        is_url_shortener=MagicMock(return_value=False),
        bypass_shortener=MagicMock(return_value=""),
        is_scrape_target=MagicMock(return_value=False),
        bypass_scrape=AsyncMock(return_value=("Thread", [], 3)),
        send_message=AsyncMock(),
        edit_message=AsyncMock(),
        send_file=AsyncMock(),
        delete_message=AsyncMock(),
    )

    bot_pkg = ModuleType("bot")
    bot_pkg.__path__ = [str(_ROOT / "bot")]
    bot_pkg.bot_loop = MagicMock()

    def _pkg(name):
        mod = ModuleType(name)
        mod.__path__ = []
        return mod

    helper_pkg = _pkg("bot.helper")
    util_pkg = _pkg("bot.helper.util")
    du_pkg = _pkg(_DU)
    tg_pkg = _pkg("bot.helper.telegram")
    # the real `conversation` helper is loaded from here -- it only needs
    # bot_loop and the message_utils double below, both of which are stubbed
    tg_pkg.__path__ = [str(_ROOT / "bot" / "helper" / "telegram")]
    modules_pkg = _pkg("bot.modules")
    modules_pkg.__path__ = [str(_ROOT / "bot" / "modules")]

    bot_utils = ModuleType("bot.helper.util.bot_utils")
    # The real decorator hands the coroutine to bot_loop.create_task; the test
    # awaits the command directly instead.
    bot_utils.new_task = lambda func: func

    async def _sync_to_async(func, *args, **kwargs):
        return func(*args, **kwargs)

    bot_utils.sync_to_async = _sync_to_async

    exceptions_mod = ModuleType("bot.helper.util.exceptions")
    exceptions_mod.DirectDownloadLinkException = _DirectDownloadLinkException

    dispatcher_mod = ModuleType(_DISPATCHER)
    dispatcher_mod.bypass_scrape = stubs.bypass_scrape
    dispatcher_mod.is_scrape_target = stubs.is_scrape_target

    shortener_mod = ModuleType(f"{_DU}.url_shortener_bypass")
    shortener_mod.bypass_shortener = stubs.bypass_shortener
    shortener_mod.is_url_shortener = stubs.is_url_shortener

    message_utils = ModuleType("bot.helper.telegram.message_utils")
    message_utils.send_message = stubs.send_message
    message_utils.edit_message = stubs.edit_message
    message_utils.send_file = stubs.send_file
    message_utils.delete_message = stubs.delete_message

    shortener_name = f"{_DU}.url_shortener_bypass"
    for name, mod in {
        "bot": bot_pkg,
        "bot.helper": helper_pkg,
        "bot.helper.util": util_pkg,
        "bot.helper.util.bot_utils": bot_utils,
        "bot.helper.util.exceptions": exceptions_mod,
        _DU: du_pkg,
        _DISPATCHER: dispatcher_mod,
        shortener_name: shortener_mod,
        "bot.helper.telegram": tg_pkg,
        "bot.helper.telegram.message_utils": message_utils,
        "bot.modules": modules_pkg,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)

    sys.modules.pop("bot.modules.bypass", None)
    # `conversation` binds bot_loop and delete_message at import; drop any copy an
    # earlier test left behind so it binds this fixture's doubles
    sys.modules.pop("bot.helper.telegram.conversation", None)
    return importlib.import_module("bot.modules.bypass"), stubs


def _message(text):
    return SimpleNamespace(
        text=text,
        reply_to_message=None,
        from_user=SimpleNamespace(id=7),
        chat=SimpleNamespace(id=-100),
    )


def _shown(mock):
    """Every message body passed to a send/edit double."""
    return [call.args[-1] for call in mock.await_args_list]


def test_is_scrape_target(dispatcher):
    assert dispatcher.is_scrape_target("https://semprot.com/threads/foo.1/")
    assert dispatcher.is_scrape_target("https://senang.top/threads/foo.1/")
    assert not dispatcher.is_scrape_target("https://linkvertise.com/1/slug")
    assert not dispatcher.is_scrape_target("not a url")


async def test_shortlink_answered_with_target(bypass_cmd):
    module, stubs = bypass_cmd
    stubs.is_url_shortener.return_value = True
    stubs.bypass_shortener.return_value = "https://mega.nz/file/abc#key"

    await module.bypass_scrape_cmd(
        MagicMock(), _message("/bypass https://linkvertise.com/12345/slug")
    )

    stubs.bypass_shortener.assert_called_once_with(
        "https://linkvertise.com/12345/slug"
    )
    assert "https://mega.nz/file/abc#key" in _shown(stubs.edit_message)[-1]
    # No page prompt, and the thread scraper is never consulted.
    stubs.bypass_scrape.assert_not_awaited()


async def test_shortlink_target_is_html_escaped(bypass_cmd):
    module, stubs = bypass_cmd
    stubs.is_url_shortener.return_value = True
    stubs.bypass_shortener.return_value = "https://host/f?a=1&b=2"

    await module.bypass_scrape_cmd(MagicMock(), _message("/bypass https://ouo.io/x"))

    body = _shown(stubs.edit_message)[-1]
    assert "a=1&amp;b=2" in body


async def test_shortlink_onto_thread_continues_into_page_prompt(bypass_cmd):
    module, stubs = bypass_cmd
    stubs.is_url_shortener.return_value = True
    stubs.bypass_shortener.return_value = "https://semprot.com/threads/foo.1/"
    stubs.is_scrape_target.return_value = True

    # The page prompt waits on the user's next message; time it out at once.
    async def _no_reply(*_args, **_kwargs):
        return None

    module._ask_reply = _no_reply

    await module.bypass_scrape_cmd(
        MagicMock(), _message("/bypass https://linkvertise.com/12345/slug")
    )

    # Probed with the resolved thread URL, not the shortlink.
    stubs.bypass_scrape.assert_awaited_once_with(
        "https://semprot.com/threads/foo.1/", "1", ""
    )
    assert any("Total pages: 3" in body for body in _shown(stubs.edit_message))


async def test_shortlink_failure_is_reported(bypass_cmd):
    module, stubs = bypass_cmd
    stubs.is_url_shortener.return_value = True
    stubs.bypass_shortener.side_effect = _DirectDownloadLinkException(
        "ERROR: linkvertise bypass failed: Content Not Found."
    )

    await module.bypass_scrape_cmd(
        MagicMock(), _message("/bypass https://linkvertise.com/12345/gone")
    )

    assert "Content Not Found" in _shown(stubs.edit_message)[-1]
    stubs.bypass_scrape.assert_not_awaited()


async def test_plain_thread_url_skips_the_shortener(bypass_cmd):
    module, stubs = bypass_cmd

    async def _no_reply(*_args, **_kwargs):
        return None

    module._ask_reply = _no_reply

    await module.bypass_scrape_cmd(
        MagicMock(), _message("/bypass https://semprot.com/threads/foo.1/ vidara.to")
    )

    stubs.bypass_shortener.assert_not_called()
    stubs.bypass_scrape.assert_awaited_once_with(
        "https://semprot.com/threads/foo.1/", "1", "vidara.to"
    )
