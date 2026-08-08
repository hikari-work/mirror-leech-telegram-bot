"""Tests for the semprot thread scraper API integration."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def semprot_module(monkeypatch):
    """Import ``semprot_scraper`` with minimal bot package stubs."""
    project_root = Path(__file__).resolve().parent.parent

    bot_pkg = ModuleType("bot")
    bot_pkg.__path__ = []
    helper_pkg = ModuleType("bot.helper")
    helper_pkg.__path__ = []
    ext_utils_pkg = ModuleType("bot.helper.ext_utils")
    ext_utils_pkg.__path__ = []
    exceptions_mod = ModuleType("bot.helper.ext_utils.exceptions")

    class DirectDownloadLinkException(Exception):
        pass

    exceptions_mod.DirectDownloadLinkException = DirectDownloadLinkException

    mlu_pkg = ModuleType("bot.helper.mirror_leech_utils")
    mlu_pkg.__path__ = []
    download_utils_pkg = ModuleType("bot.helper.mirror_leech_utils.download_utils")
    download_utils_pkg.__path__ = [
        str(project_root / "bot" / "helper" / "mirror_leech_utils" / "download_utils")
    ]

    for name, mod in {
        "bot": bot_pkg,
        "bot.helper": helper_pkg,
        "bot.helper.ext_utils": ext_utils_pkg,
        "bot.helper.ext_utils.exceptions": exceptions_mod,
        "bot.helper.mirror_leech_utils": mlu_pkg,
        "bot.helper.mirror_leech_utils.download_utils": download_utils_pkg,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)

    sys.modules.pop(
        "bot.helper.mirror_leech_utils.download_utils.semprot_scraper", None
    )
    return importlib.import_module(
        "bot.helper.mirror_leech_utils.download_utils.semprot_scraper"
    )


def test_normalize_url(semprot_module):
    norm = semprot_module._normalize_url
    assert norm("https://senang.top/threads/foo.123/") == "https://semprot.com/threads/foo.123/"
    assert norm("https://www.senang.top/threads/bar.456") == "https://www.semprot.com/threads/bar.456"
    assert norm("https://semprot.com/threads/foo.123/") == "https://semprot.com/threads/foo.123/"


def test_scrape_thread_success(semprot_module, monkeypatch):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "success": True,
        "links": ["https://mega.nz/xyz", "https://pixeldrain.com/u/123"],
    }
    mock_get = MagicMock(return_value=mock_resp)
    monkeypatch.setattr("requests.get", mock_get)

    title, links = semprot_module.scrape_thread("https://senang.top/threads/test-thread.123/")
    assert links == ["https://mega.nz/xyz", "https://pixeldrain.com/u/123"]
    assert title == "test-thread.123"
    mock_get.assert_called_once_with(
        semprot_module.GATEWAY_API,
        params={"q": "https://semprot.com/threads/test-thread.123/"},
        timeout=60,
    )


def test_scrape_thread_api_error(semprot_module, monkeypatch):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "success": False,
        "error": "Failed to fetch thread page",
    }
    monkeypatch.setattr("requests.get", MagicMock(return_value=mock_resp))

    with pytest.raises(semprot_module.DirectDownloadLinkException, match="Failed to fetch thread page"):
        semprot_module.scrape_thread("https://semprot.com/threads/test.1/")
