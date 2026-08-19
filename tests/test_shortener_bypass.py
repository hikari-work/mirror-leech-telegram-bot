"""Tests for the gateway-backed shortener bypasses (ouo, Linkvertise)."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

_MODULE = "bot.helper.mirror_leech_utils.download_utils.url_shortener_bypass"


@pytest.fixture
def shortener(monkeypatch):
    """Import ``url_shortener_bypass`` with minimal bot package stubs."""
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

    sys.modules.pop(_MODULE, None)
    module = importlib.import_module(_MODULE)
    # Never actually sleep between retries.
    monkeypatch.setattr(module, "sleep", lambda _seconds: None)
    return module


def _mock_session(*responses):
    """Replace ``requests.Session`` with one that replays ``responses`` in order.

    Each entry is either an exception to raise or a (status, json) pair.
    """
    session = MagicMock()
    calls = []

    def _get(url, **kwargs):
        calls.append((url, kwargs))
        item = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(item, Exception):
            raise item
        status, payload = item
        resp = MagicMock()
        resp.status_code = status
        resp.headers = {}
        if isinstance(payload, Exception):
            resp.json = MagicMock(side_effect=payload)
        else:
            resp.json = MagicMock(return_value=payload)
        return resp

    session.get = MagicMock(side_effect=_get)
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=False)
    return MagicMock(return_value=ctx), session, calls


def test_is_url_shortener_covers_linkvertise_mirrors(shortener):
    for host in (
        "linkvertise.com",
        "www.linkvertise.com",
        "linkvertise.net",
        "link-to.net",
        "direct-link.net",
        "up-to-down.net",
        "file-link.net",
        "link-target.net",
    ):
        assert shortener.is_url_shortener(host), host


def test_is_url_shortener_covers_ouo(shortener):
    assert shortener.is_url_shortener("ouo.io")
    assert shortener.is_url_shortener("ouo.press")
    assert shortener.is_url_shortener("www.ouo.io")


def test_is_url_shortener_rejects_others(shortener):
    # Hosts the gateway answers with 400; routing them here would only turn a
    # working direct-link lookup into a bypass error.
    for host in ("link-hub.net", "link-center.net", "example.com", "", None):
        assert not shortener.is_url_shortener(host), host


def test_linkvertise_success_returns_target(shortener):
    mock_cls, session, calls = _mock_session(
        (200, {"success": True, "url": "https://mega.nz/file/abc#key"})
    )
    with patch.object(shortener, "Session", mock_cls):
        result = shortener.bypass_shortener("https://linkvertise.com/12345/my-slug")

    assert result == "https://mega.nz/file/abc#key"
    url, kwargs = calls[0]
    assert url == "https://api.piyann.me/api/v1/bypass/linkvertise"
    assert kwargs["params"] == {"url": "https://linkvertise.com/12345/my-slug"}
    assert session.get.call_count == 1


def test_linkvertise_honours_configured_gateway_and_token(shortener):
    shortener.Config.GATEWAY_URL = "https://gw.example.com/"
    shortener.Config.GATEWAY_TOKEN = "secret"
    mock_cls, _session, calls = _mock_session(
        (200, {"success": True, "url": "https://example.com/target"})
    )
    with patch.object(shortener, "Session", mock_cls):
        shortener.bypass_shortener("https://link-to.net/12345/my-slug")

    url, kwargs = calls[0]
    assert url == "https://gw.example.com/api/v1/bypass/linkvertise"
    assert kwargs["headers"]["Authorization"] == "Bearer secret"


def test_linkvertise_paste_content_yields_first_url(shortener):
    mock_cls, _session, _calls = _mock_session(
        (200, {"success": True, "url": "password: 123\nhttps://pixeldrain.com/u/xyz\n"})
    )
    with patch.object(shortener, "Session", mock_cls):
        result = shortener.bypass_shortener("https://linkvertise.com/1/paste")

    assert result == "https://pixeldrain.com/u/xyz"


def test_linkvertise_paste_without_url_reports_content(shortener):
    mock_cls, _session, _calls = _mock_session(
        (200, {"success": True, "url": "just some notes"})
    )
    with (
        patch.object(shortener, "Session", mock_cls),
        pytest.raises(Exception) as excinfo,
    ):
        shortener.bypass_shortener("https://linkvertise.com/1/paste")

    assert "just some notes" in str(excinfo.value)


def test_linkvertise_dead_link_is_not_retried(shortener):
    # The gateway reports a removed shortlink as HTTP 500; retrying that would
    # only triple the wait before the same failure.
    mock_cls, session, _calls = _mock_session(
        (
            500,
            {
                "success": False,
                "error": "linkvertise: getContent: Content Not Found.",
            },
        )
    )
    with (
        patch.object(shortener, "Session", mock_cls),
        pytest.raises(Exception) as excinfo,
    ):
        shortener.bypass_shortener("https://linkvertise.com/12345/gone")

    assert session.get.call_count == 1
    assert "Content Not Found" in str(excinfo.value)


def test_linkvertise_rate_limit_is_retried(shortener):
    mock_cls, session, _calls = _mock_session(
        (429, {"success": False, "error": "rate limit exceeded"}),
        (200, {"success": True, "url": "https://example.com/target"}),
    )
    with patch.object(shortener, "Session", mock_cls):
        result = shortener.bypass_shortener("https://linkvertise.com/12345/slug")

    assert result == "https://example.com/target"
    assert session.get.call_count == 2


def test_linkvertise_network_error_retries_then_gives_up(shortener):
    mock_cls, session, _calls = _mock_session(ConnectionError("boom"))
    with (
        patch.object(shortener, "Session", mock_cls),
        pytest.raises(Exception) as excinfo,
    ):
        shortener.bypass_shortener("https://linkvertise.com/12345/slug")

    assert session.get.call_count == shortener._GATEWAY_ATTEMPTS
    assert "ConnectionError" in str(excinfo.value)


def test_linkvertise_non_json_response(shortener):
    mock_cls, _session, _calls = _mock_session((200, ValueError("not json")))
    with (
        patch.object(shortener, "Session", mock_cls),
        pytest.raises(Exception) as excinfo,
    ):
        shortener.bypass_shortener("https://linkvertise.com/12345/slug")

    assert "non-JSON" in str(excinfo.value)


def test_ouo_success_returns_target(shortener):
    mock_cls, session, calls = _mock_session(
        (200, {"success": True, "url": "https://drive.google.com/file/d/abc/view"})
    )
    with patch.object(shortener, "Session", mock_cls):
        result = shortener.bypass_shortener("https://ouo.io/abc123")

    assert result == "https://drive.google.com/file/d/abc/view"
    url, kwargs = calls[0]
    assert url == "https://api.piyann.me/api/v1/bypass/ouo"
    assert kwargs["params"] == {"url": "https://ouo.io/abc123"}
    assert session.get.call_count == 1


def test_ouo_press_link_is_sent_unchanged(shortener):
    # The gateway takes ouo.press itself, so there is no host to rewrite here.
    mock_cls, _session, calls = _mock_session(
        (200, {"success": True, "url": "https://example.com/target"})
    )
    with patch.object(shortener, "Session", mock_cls):
        shortener.bypass_shortener("https://ouo.press/xyz789")

    _url, kwargs = calls[0]
    assert kwargs["params"] == {"url": "https://ouo.press/xyz789"}


def test_ouo_failure_is_named_and_clipped(shortener):
    # ouo errors quote the fetched page back; the chat gets a one-line summary.
    mock_cls, session, _calls = _mock_session(
        (
            500,
            {
                "success": False,
                "error": "ouo: _token not found on initial page (status=200, "
                "body=<!DOCTYPE html>\n<html>\n" + "<div>pad</div>" * 100,
            },
        )
    )
    with (
        patch.object(shortener, "Session", mock_cls),
        pytest.raises(Exception) as excinfo,
    ):
        shortener.bypass_shortener("https://ouo.io/gone")

    message = str(excinfo.value)
    assert message.startswith("ERROR: ouo bypass failed: ouo: _token not found")
    assert "\n" not in message
    assert len(message) < shortener._MAX_REASON + 60
    assert session.get.call_count == 1


def test_ouo_rate_limit_is_retried(shortener):
    mock_cls, session, _calls = _mock_session(
        (429, {"success": False, "error": "rate limit exceeded"}),
        (200, {"success": True, "url": "https://example.com/target"}),
    )
    with patch.object(shortener, "Session", mock_cls):
        result = shortener.bypass_shortener("https://ouo.io/abc123")

    assert result == "https://example.com/target"
    assert session.get.call_count == 2


def test_bypass_shortener_unknown_domain(shortener):
    with pytest.raises(Exception) as excinfo:
        shortener.bypass_shortener("https://example.com/foo")
    assert "No bypasser" in str(excinfo.value)
