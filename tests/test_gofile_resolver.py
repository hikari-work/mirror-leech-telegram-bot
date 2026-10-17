"""Tests for GoFile resolver via gateway API."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def gofile_module(monkeypatch):
    """Load ``hosts/gofile.py`` with its package stubbed out."""
    path = (
        _ROOT
        / "bot"
        / "helper"
        / "download"
        / "direct_link_generators"
        / "hosts"
        / "gofile.py"
    )

    pkg = ModuleType("gofile_stub")
    pkg.__path__ = []
    hosts_pkg = ModuleType("gofile_stub.hosts")
    hosts_pkg.__path__ = []

    class _Logger:
        @staticmethod
        def info(msg):
            pass

        error = warning = debug = info

    class DirectDownloadLinkException(Exception):
        pass

    common = ModuleType("gofile_stub._common")
    common.LOGGER = _Logger()
    common.DirectDownloadLinkException = DirectDownloadLinkException
    common.PASSWORD_ERROR_MESSAGE = "Password required for {0}"
    common.user_agent = "Test-UA"
    common.gateway_url = lambda path="": f"https://gateway.test{path}"
    common.gateway_headers = lambda accept_json=True: {
        "accept": "application/json",
        "Authorization": "Bearer test-token",
    }

    registry = ModuleType("gofile_stub.registry")
    registry.register = lambda *domains, **kwargs: (lambda func: func)

    for name, mod in {
        "gofile_stub": pkg,
        "gofile_stub.hosts": hosts_pkg,
        "gofile_stub._common": common,
        "gofile_stub.registry": registry,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)

    spec = importlib.util.spec_from_file_location("gofile_stub.hosts.gofile", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "gofile_stub.hosts.gofile", module)
    spec.loader.exec_module(module)
    return module


def _mock_session(status=200, payload=None, side_effect=None):
    session = MagicMock()
    if side_effect:
        session.get.side_effect = side_effect
    else:
        resp = MagicMock()
        resp.status_code = status
        resp.json.return_value = payload or {}
        session.get.return_value = resp

    ctx = MagicMock()
    ctx.__enter__.return_value = session
    ctx.__exit__.return_value = False
    return session, ctx


def test_gofile_single_file_success(gofile_module):
    payload = {
        "success": True,
        "title": "single_test",
        "content_id": "VhdEyBg9",
        "files": [
            {
                "name": "video.mp4",
                "path": "/video.mp4",
                "size": 1000,
                "id": "abc-123",
                "download_url": "https://store10.gofile.io/download/web/abc-123/video.mp4",
            }
        ],
        "download": {
            "cookie": "accountToken=token123",
            "user_agent": "Custom-UA",
        },
    }
    session, ctx = _mock_session(200, payload)
    with patch.object(gofile_module, "Session", return_value=ctx):
        result = gofile_module.gofile("https://gofile.io/d/VhdEyBg9")

    assert isinstance(result, tuple)
    url, headers = result
    assert url == "https://store10.gofile.io/download/web/abc-123/video.mp4"
    assert "Cookie: accountToken=token123" in headers
    assert "User-Agent: Custom-UA" in headers

    session.get.assert_called_once()
    call_args, call_kwargs = session.get.call_args
    assert call_args[0] == "https://gateway.test/api/v1/scrape/gofile"
    assert call_kwargs["params"] == {"q": "https://gofile.io/d/VhdEyBg9"}
    assert call_kwargs["headers"]["Authorization"] == "Bearer test-token"


def test_gofile_multi_file_folder_success(gofile_module):
    payload = {
        "success": True,
        "title": "album_folder",
        "content_id": "album1",
        "files": [
            {
                "name": "track1.mp3",
                "path": "/track1.mp3",
                "size": 500,
                "id": "f1",
                "download_url": "https://store1.gofile.io/download/f1/track1.mp3",
            },
            {
                "name": "track2.mp3",
                "path": "/disc2/track2.mp3",
                "size": 700,
                "id": "f2",
                "download_url": "https://store1.gofile.io/download/f2/track2.mp3",
            },
        ],
        "download": {
            "cookie_name": "accountToken",
            "cookie_value": "tok456",
        },
    }
    _session, ctx = _mock_session(200, payload)
    with patch.object(gofile_module, "Session", return_value=ctx):
        result = gofile_module.gofile("https://gofile.io/d/album1")

    assert isinstance(result, dict)
    assert result["title"] == "album_folder"
    assert result["total_size"] == 1200
    assert "Cookie: accountToken=tok456" in str(result["header"])
    contents = result["contents"]
    assert len(contents) == 2
    assert contents[0] == {
        "path": "",
        "filename": "track1.mp3",
        "url": "https://store1.gofile.io/download/f1/track1.mp3",
    }
    assert contents[1] == {
        "path": "disc2",
        "filename": "track2.mp3",
        "url": "https://store1.gofile.io/download/f2/track2.mp3",
    }


def test_gofile_with_password(gofile_module):
    payload = {
        "success": True,
        "title": "secret_file",
        "files": [
            {
                "name": "secret.pdf",
                "path": "/secret.pdf",
                "size": 2048,
                "download_url": "https://store1.gofile.io/download/sec/secret.pdf",
            }
        ],
        "download": {"cookie": "accountToken=pwtoken"},
    }
    session, ctx = _mock_session(200, payload)
    with patch.object(gofile_module, "Session", return_value=ctx):
        result = gofile_module.gofile("https://gofile.io/d/secret123::mypassword")

    url, headers = result
    assert url == "https://store1.gofile.io/download/sec/secret.pdf"
    assert (call_kwargs := session.get.call_args[1])
    assert call_kwargs["params"] == {
        "q": "https://gofile.io/d/secret123",
        "password": "mypassword",
    }


def test_gofile_password_required_error(gofile_module):
    payload = {
        "success": False,
        "error": "error-passwordRequired",
    }
    _session, ctx = _mock_session(200, payload)
    with (
        patch.object(gofile_module, "Session", return_value=ctx),
        pytest.raises(Exception) as excinfo,
    ):
        gofile_module.gofile("https://gofile.io/d/pw_needed")

    assert "Password required for" in str(excinfo.value)


def test_gofile_password_wrong_error(gofile_module):
    payload = {
        "success": False,
        "error": "error-passwordWrong",
    }
    _session, ctx = _mock_session(200, payload)
    with (
        patch.object(gofile_module, "Session", return_value=ctx),
        pytest.raises(Exception) as excinfo,
    ):
        gofile_module.gofile("https://gofile.io/d/pw_wrong::badpw")

    assert "password is wrong" in str(excinfo.value)


def test_gofile_not_found_error(gofile_module):
    payload = {
        "success": False,
        "error": "gofile content is not available (deleted or invalid)",
    }
    _session, ctx = _mock_session(200, payload)
    with (
        patch.object(gofile_module, "Session", return_value=ctx),
        pytest.raises(Exception) as excinfo,
    ):
        gofile_module.gofile("https://gofile.io/d/nonexistent")

    assert "not available" in str(excinfo.value)


def test_gofile_empty_files_raises(gofile_module):
    payload = {
        "success": True,
        "title": "empty",
        "files": [],
    }
    _session, ctx = _mock_session(200, payload)
    with (
        patch.object(gofile_module, "Session", return_value=ctx),
        pytest.raises(Exception) as excinfo,
    ):
        gofile_module.gofile("https://gofile.io/d/empty")

    assert "No files found" in str(excinfo.value)
