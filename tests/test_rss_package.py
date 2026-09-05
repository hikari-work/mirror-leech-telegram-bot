"""Characterization tests for the rss package (Fase 9).

Tests the pure-logic functions that don't touch the network or the DB.
The ``bot`` package is side-effectful on import, so the modules under test
are loaded with lightweight stubs — same strategy as test_direct_link_registry.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _rss_stubs(monkeypatch):
    """Stub out the bot package so rss.feed and rss.store import cleanly."""
    bot_pkg = ModuleType("bot")
    bot_pkg.__path__ = [str(_ROOT / "bot")]
    bot_pkg.LOGGER = type("L", (), {"info": staticmethod(lambda m: None),
                                     "warning": staticmethod(lambda m: None),
                                     "error": staticmethod(lambda m: None)})()
    bot_pkg.rss_dict = {}
    bot_pkg.scheduler = type("S", (), {"running": False, "state": 0,
                                       "add_job": lambda *a, **kw: None,
                                       "start": lambda *a: None})()

    core = ModuleType("bot.core")
    core.__path__ = []
    config_manager = ModuleType("bot.core.config_manager")
    config_manager.Config = type("Config", (), {"RSS_DELAY": 600,
                                                 "RSS_CHAT": "",
                                                 "RSS_SIZE_LIMIT": 0,
                                                 "CMD_SUFFIX": ""})()

    helper = ModuleType("bot.helper")
    helper.__path__ = []
    ext = ModuleType("bot.helper.util")
    ext.__path__ = []

    # bot_utils — only get_size_bytes is needed by feed.item_size
    bot_utils = ModuleType("bot.helper.util.bot_utils")
    def get_size_bytes(size):
        size = size.lower()
        if "k" in size:
            return int(float(size.split("k")[0]) * 1024)
        if "m" in size:
            return int(float(size.split("m")[0]) * 1048576)
        if "g" in size:
            return int(float(size.split("g")[0]) * 1073741824)
        return 0
    bot_utils.get_size_bytes = get_size_bytes
    bot_utils.new_task = lambda fn: fn
    bot_utils.arg_parser = lambda *a, **kw: None

    db_handler = ModuleType("bot.helper.storage.db_handler")
    db_handler.database = type("DB", (), {
        "rss_update": staticmethod(lambda *a: None),
        "rss_update_all": staticmethod(lambda *a: None),
        "rss_delete": staticmethod(lambda *a: None),
        "trunc_table": staticmethod(lambda *a: None),
    })()

    exceptions = ModuleType("bot.helper.util.exceptions")
    exceptions.RssShutdownException = type("RssShutdownException", (Exception,), {})

    status_utils = ModuleType("bot.helper.util.status_utils")
    status_utils.get_readable_file_size = lambda s: str(s)

    help_messages = ModuleType("bot.helper.util.help_messages")
    help_messages.RSS_HELP_MESSAGE = "help"

    tg_helper = ModuleType("bot.helper.telegram")
    tg_helper.__path__ = []
    msg_utils = ModuleType("bot.helper.telegram.message_utils")
    msg_utils.send_rss = lambda *a, **kw: None
    msg_utils.send_message = lambda *a, **kw: None
    msg_utils.edit_message = lambda *a, **kw: None
    msg_utils.delete_message = lambda *a, **kw: None
    msg_utils.send_file = lambda *a, **kw: None
    btn_build = ModuleType("bot.helper.telegram.button_build")
    btn_build.ButtonMaker = type("ButtonMaker", (), {
        "data_button": lambda *a, **kw: None,
        "build_menu": lambda *a, **kw: None,
    })
    filters_mod = ModuleType("bot.helper.telegram.filters")
    filters_mod.CustomFilters = type("CF", (), {
        "sudo": staticmethod(lambda *a, **kw: False),
    })()

    telegram_manager = ModuleType("bot.core.telegram_manager")
    telegram_manager.TgClient = type("TgClient", (), {"bot": None})()

    du = ModuleType("bot.helper.download")
    du.__path__ = []

    modules = ModuleType("bot.modules")
    modules.__path__ = [str(_ROOT / "bot" / "modules")]

    rss_pkg = ModuleType("bot.modules.rss")
    rss_pkg.__path__ = [str(_ROOT / "bot" / "modules" / "rss")]

    stubs = {
        "bot": bot_pkg,
        "bot.core": core,
        "bot.core.config_manager": config_manager,
        "bot.core.telegram_manager": telegram_manager,
        "bot.helper": helper,
        "bot.helper.util": ext,
        "bot.helper.util.bot_utils": bot_utils,
        "bot.helper.storage.db_handler": db_handler,
        "bot.helper.util.exceptions": exceptions,
        "bot.helper.util.status_utils": status_utils,
        "bot.helper.util.help_messages": help_messages,
        "bot.helper.telegram": tg_helper,
        "bot.helper.telegram.message_utils": msg_utils,
        "bot.helper.telegram.button_build": btn_build,
        "bot.helper.telegram.filters": filters_mod,
        "bot.helper.download": du,
        "bot.modules": modules,
        "bot.modules.rss": rss_pkg,
    }
    for name, mod in stubs.items():
        monkeypatch.setitem(sys.modules, name, mod)

    # Clear any previously cached rss submodules so they reimport with stubs
    for name in [m for m in list(sys.modules) if m.startswith("bot.modules.rss.")]:
        monkeypatch.delitem(sys.modules, name, raising=False)

    yield


def _feed():
    return importlib.import_module("bot.modules.rss.feed")


def _store():
    return importlib.import_module("bot.modules.rss.store")


# ── item_blocked ──────────────────────────────────────────────────────────

class TestItemBlocked:
    def test_no_filters(self):
        assert not _feed().item_blocked("Anything Goes",
                                        {"inf": [], "exf": [], "sensitive": False})

    def test_inf_match(self):
        data = {"inf": [["1080", "720"]], "exf": [], "sensitive": False}
        assert not _feed().item_blocked("Movie.1080p.BluRay", data)

    def test_inf_no_match(self):
        data = {"inf": [["1080", "720"]], "exf": [], "sensitive": False}
        assert _feed().item_blocked("Movie.480p.BluRay", data)

    def test_exf_match(self):
        data = {"inf": [], "exf": [["CAM"]], "sensitive": False}
        assert _feed().item_blocked("Movie.CAM.Rip", data)

    def test_exf_no_match(self):
        data = {"inf": [], "exf": [["CAM"]], "sensitive": False}
        assert not _feed().item_blocked("Movie.BluRay", data)

    def test_sensitive_true_lowercases(self):
        data = {"inf": [["bluray"]], "exf": [], "sensitive": True}
        assert not _feed().item_blocked("Movie.BluRay.1080p", data)

    def test_sensitive_false_exact(self):
        data = {"inf": [["bluray"]], "exf": [], "sensitive": False}
        assert _feed().item_blocked("Movie.BluRay.1080p", data)

    def test_multiple_inf_groups(self):
        f = _feed()
        data = {"inf": [["1080"], ["x264", "x265"]], "exf": [], "sensitive": False}
        assert not f.item_blocked("Movie.1080p.x265", data)
        assert f.item_blocked("Movie.1080p.HEVC", data)

    def test_multiple_exf_groups(self):
        f = _feed()
        data = {"inf": [], "exf": [["CAM"], ["TS"]], "sensitive": False}
        assert f.item_blocked("Movie.TS.Rip", data)
        assert not f.item_blocked("Movie.BluRay", data)


# ── item_url / latest_url ────────────────────────────────────────────────

class TestItemUrl:
    def test_enclosure_link(self):
        entry = {"links": [{"href": "page"}, {"href": "torrent"}], "link": "page"}
        assert _feed().item_url(entry) == "torrent"

    def test_single_link_fallback(self):
        entry = {"links": [{"href": "page"}], "link": "direct"}
        assert _feed().item_url(entry) == "direct"

    def test_latest_url_two_links(self):
        entry = {"links": [{"href": "page"}, {"href": "enc"}]}
        assert _feed().latest_url(entry) == "enc"

    def test_latest_url_one_link(self):
        entry = {"links": [{"href": "only"}]}
        assert _feed().latest_url(entry) == "only"

    def test_latest_url_no_links(self):
        assert _feed().latest_url({}) is None

    def test_latest_url_fallback_link_attr(self):
        assert _feed().latest_url({"link": "fallback"}) == "fallback"


# ── item_size ─────────────────────────────────────────────────────────────

class TestItemSize:
    def test_explicit_size(self):
        assert _feed().item_size({"size": "1024"}) == 1024

    def test_summary_size(self):
        assert _feed().item_size({"summary": "Some torrent 1.5 GB stuff"}) > 0

    def test_no_size_info(self):
        assert _feed().item_size({}) == 0


# ── parse_chat_target ─────────────────────────────────────────────────────

class TestParseChatTarget:
    def test_int(self):
        assert _store().parse_chat_target(-100123) == (-100123, None)

    def test_str_int(self):
        assert _store().parse_chat_target("-100123") == (-100123, None)

    def test_str_with_topic(self):
        assert _store().parse_chat_target("-100123|45") == (-100123, 45)

    def test_empty(self):
        assert _store().parse_chat_target("") == (None, None)

    def test_none(self):
        assert _store().parse_chat_target(None) == (None, None)

    def test_channel_name(self):
        assert _store().parse_chat_target("@channel") == (None, None)
