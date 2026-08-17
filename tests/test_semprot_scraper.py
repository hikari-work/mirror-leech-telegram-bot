"""Tests for the semprot thread scraper API integration."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def semprot_module(monkeypatch):
    """Import ``semprot_scraper`` with minimal bot package stubs."""
    project_root = Path(__file__).resolve().parent.parent

    bot_pkg = ModuleType("bot")
    bot_pkg.__path__ = []
    core_pkg = ModuleType("bot.core")
    core_pkg.__path__ = []
    config_mod = ModuleType("bot.core.config_manager")
    config_mod.Config = SimpleNamespace(GATEWAY_URL="", GATEWAY_TOKEN="")
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
        "bot.core": core_pkg,
        "bot.core.config_manager": config_mod,
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


def _make_mock_session(json_data, status=200):
    """Build a mock that replaces ``aiohttp.ClientSession`` entirely."""
    mock_resp = MagicMock()
    mock_resp.status = status
    if status >= 400:
        mock_resp.raise_for_status = MagicMock(side_effect=Exception(f"HTTP {status}"))
    else:
        mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value=json_data)

    resp_ctx = MagicMock()
    resp_ctx.__aenter__ = AsyncMock(return_value=mock_resp)
    resp_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=resp_ctx)

    session_ctx = MagicMock()
    session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
    session_ctx.__aexit__ = AsyncMock(return_value=False)

    mock_cls = MagicMock(return_value=session_ctx)
    return mock_cls, mock_session


def test_normalize_url(semprot_module):
    norm = semprot_module._normalize_url
    assert norm("https://senang.top/threads/foo.123/") == "https://semprot.com/threads/foo.123/"
    assert norm("https://www.senang.top/threads/bar.456") == "https://www.semprot.com/threads/bar.456"
    assert norm("https://semprot.com/threads/foo.123/") == "https://semprot.com/threads/foo.123/"


@pytest.mark.asyncio
async def test_scrape_pages_success(semprot_module):
    json_data = {
        "success": True,
        "title": "Test Thread",
        "links": ["https://mega.nz/xyz", "https://pixeldrain.com/u/123"],
        "pages_total": 5,
    }
    mock_cls, mock_session = _make_mock_session(json_data)

    with patch.object(semprot_module, "ClientSession", mock_cls):
        title, links, total_pages = await semprot_module.scrape_pages(
            "https://senang.top/threads/test-thread.123/", page_list="2"
        )
    assert links == ["https://mega.nz/xyz", "https://pixeldrain.com/u/123"]
    assert title == "Test Thread"
    assert total_pages == 5
    call_kwargs = mock_session.get.call_args
    assert call_kwargs[1]["params"] == {
        "q": "https://semprot.com/threads/test-thread.123/",
        "pageList": "2",
    }


@pytest.mark.asyncio
async def test_scrape_pages_with_filter(semprot_module):
    json_data = {
        "success": True,
        "links": ["https://vidara.to/abc"],
        "pages_total": 10,
    }
    mock_cls, mock_session = _make_mock_session(json_data)

    with patch.object(semprot_module, "ClientSession", mock_cls):
        title, links, total_pages = await semprot_module.scrape_pages(
            "https://semprot.com/threads/foo.1/", page_list="1-5", filter_host="vidara.to"
        )
    assert links == ["https://vidara.to/abc"]
    assert total_pages == 10
    assert title == "foo.1"  # no "title" in response → falls back to URL slug
    call_kwargs = mock_session.get.call_args
    assert call_kwargs[1]["params"]["filter"] == "vidara.to"
    assert call_kwargs[1]["params"]["pageList"] == "1-5"


@pytest.mark.asyncio
async def test_scrape_pages_api_error(semprot_module):
    json_data = {
        "success": False,
        "error": "Failed to fetch thread page",
    }
    mock_cls, _ = _make_mock_session(json_data)

    with patch.object(semprot_module, "ClientSession", mock_cls):
        with pytest.raises(semprot_module.DirectDownloadLinkException, match="Failed to fetch thread page"):
            await semprot_module.scrape_pages("https://semprot.com/threads/test.1/")


@pytest.mark.asyncio
async def test_scrape_pages_default_total_pages(semprot_module):
    json_data = {
        "success": True,
        "links": ["https://example.com/file.zip"],
    }
    mock_cls, _ = _make_mock_session(json_data)

    with patch.object(semprot_module, "ClientSession", mock_cls):
        _, _, total_pages = await semprot_module.scrape_pages("https://semprot.com/threads/foo.1/")
    assert total_pages == 1
