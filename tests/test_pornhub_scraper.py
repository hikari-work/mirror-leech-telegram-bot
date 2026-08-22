"""Tests for the PornHub scraper API integration."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def ph_module(monkeypatch):
    """Import ``pornhub_scraper`` with minimal bot package stubs."""
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
    ext_utils_pkg.__path__ = [
        str(project_root / "bot" / "helper" / "ext_utils")
    ]  # real submodules (gateway) load from disk; the stubs above win in sys.modules
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

    # the real gateway helper binds Config at import time; drop any copy an
    # earlier test left in sys.modules so it binds this fixture's stub
    sys.modules.pop("bot.helper.ext_utils.gateway", None)
    sys.modules.pop(
        "bot.helper.mirror_leech_utils.download_utils.pornhub_scraper", None
    )
    return importlib.import_module(
        "bot.helper.mirror_leech_utils.download_utils.pornhub_scraper"
    )


class TestIsPornhubLink:
    def test_pornhub_url(self, ph_module):
        assert ph_module.is_pornhub_link("https://www.pornhub.com/view_video.php?viewkey=ph5f3b")

    def test_non_pornhub(self, ph_module):
        assert not ph_module.is_pornhub_link("https://youtube.com/watch?v=abc")


class TestParseUrl:
    def test_video_url(self, ph_module):
        result = ph_module.parse_pornhub_url(
            "https://www.pornhub.com/view_video.php?viewkey=ph5f3b1234"
        )
        assert result == ("video", "ph5f3b1234")

    def test_channel_url(self, ph_module):
        result = ph_module.parse_pornhub_url(
            "https://www.pornhub.com/channels/brazzers"
        )
        assert result == ("channel", "brazzers")

    def test_model_url(self, ph_module):
        result = ph_module.parse_pornhub_url(
            "https://www.pornhub.com/model/some-model"
        )
        assert result == ("model", "some-model")

    def test_pornstar_url(self, ph_module):
        result = ph_module.parse_pornhub_url(
            "https://www.pornhub.com/pornstar/mia-khalifa"
        )
        assert result == ("pornstar", "mia-khalifa")

    def test_trailing_slash(self, ph_module):
        result = ph_module.parse_pornhub_url(
            "https://www.pornhub.com/channels/brazzers/"
        )
        assert result == ("channel", "brazzers")

    def test_non_pornhub(self, ph_module):
        assert ph_module.parse_pornhub_url("https://youtube.com/watch?v=abc") is None

    def test_unknown_path(self, ph_module):
        assert ph_module.parse_pornhub_url("https://www.pornhub.com/categories/popular") is None


class TestScrapeVideo:
    def test_success(self, ph_module, monkeypatch):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "success": True,
            "data": {
                "title": "Test Video",
                "video_key": "ph5f3b",
                "downloads": [
                    {"quality": "720p", "format": "hls", "url": "https://cdn/720.m3u8"},
                    {"quality": "1080p", "format": "hls", "url": "https://cdn/1080.m3u8"},
                ],
            },
        }
        monkeypatch.setattr("requests.get", MagicMock(return_value=mock_resp))
        detail = ph_module.scrape_video("ph5f3b")
        assert detail["title"] == "Test Video"
        assert len(detail["downloads"]) == 2

    def test_api_error(self, ph_module, monkeypatch):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"success": False, "error": "Video not found"}
        monkeypatch.setattr("requests.get", MagicMock(return_value=mock_resp))
        with pytest.raises(Exception, match="Video not found"):
            ph_module.scrape_video("badkey")


class TestScrapeList:
    def test_channel_success(self, ph_module, monkeypatch):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "success": True,
            "videos": [
                {"video_key": "ph001", "title": "V1"},
                {"video_key": "ph002", "title": "V2"},
            ],
        }
        mock_get = MagicMock(return_value=mock_resp)
        monkeypatch.setattr("requests.get", mock_get)
        videos = ph_module.scrape_list("channel", "brazzers")
        assert len(videos) == 2
        call_params = mock_get.call_args[1]["params"]
        assert call_params["name"] == "brazzers"
        assert call_params["all"] == "true"

    def test_invalid_type(self, ph_module):
        with pytest.raises(Exception, match="Invalid PornHub list type"):
            ph_module.scrape_list("category", "popular")


class TestResolveSingleVideo:
    def test_hls_returns_pornhub_dict(self, ph_module, monkeypatch):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "success": True,
            "data": {
                "title": "HLS Video",
                "video_key": "ph123",
                "downloads": [
                    {"quality": "480p", "format": "hls", "url": "https://cdn/480.m3u8"},
                    {"quality": "1080p", "format": "hls", "url": "https://cdn/1080.m3u8"},
                    {"quality": "720p", "format": "hls", "url": "https://cdn/720.m3u8"},
                ],
            },
        }
        monkeypatch.setattr("requests.get", MagicMock(return_value=mock_resp))
        result = ph_module.resolve_single_video("ph123")
        assert result["pornhub"] is True
        assert len(result["videos"]) == 1
        assert result["videos"][0]["url"] == "https://cdn/1080.m3u8"
        assert result["videos"][0]["is_hls"] is True
        assert result["videos"][0]["name"] == "HLS Video.mp4"
        assert result["headers"]["Referer"] == "https://www.pornhub.com/"
        assert result["headers"]["Origin"] == "https://www.pornhub.com"

    def test_direct_mp4(self, ph_module, monkeypatch):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "success": True,
            "data": {
                "title": "Direct Video",
                "video_key": "ph456",
                "downloads": [
                    {"quality": "720p", "format": "mp4", "url": "https://cdn/720.mp4"},
                ],
            },
        }
        monkeypatch.setattr("requests.get", MagicMock(return_value=mock_resp))
        result = ph_module.resolve_single_video("ph456")
        assert result["pornhub"] is True
        assert result["videos"][0]["is_hls"] is False
        assert result["videos"][0]["url"] == "https://cdn/720.mp4"

    def test_no_downloads_raises(self, ph_module, monkeypatch):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "success": True,
            "data": {"title": "Empty", "video_key": "ph789", "downloads": []},
        }
        monkeypatch.setattr("requests.get", MagicMock(return_value=mock_resp))
        with pytest.raises(Exception, match="No download URLs"):
            ph_module.resolve_single_video("ph789")

    def test_picks_highest_quality(self, ph_module, monkeypatch):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "success": True,
            "data": {
                "title": "Quality Test",
                "video_key": "phq",
                "downloads": [
                    {"quality": "240p", "format": "hls", "url": "https://cdn/240.m3u8"},
                    {"quality": "1080p", "format": "hls", "url": "https://cdn/1080.m3u8"},
                    {"quality": "480p", "format": "hls", "url": "https://cdn/480.m3u8"},
                    {"quality": "720p", "format": "hls", "url": "https://cdn/720.m3u8"},
                ],
            },
        }
        monkeypatch.setattr("requests.get", MagicMock(return_value=mock_resp))
        result = ph_module.resolve_single_video("phq")
        assert result["videos"][0]["url"] == "https://cdn/1080.m3u8"


class TestResolveListing:
    def test_listing_returns_multi_video_dict(self, ph_module, monkeypatch):
        call_count = {"n": 0}

        def mock_get(*args, **kwargs):
            call_count["n"] += 1
            resp = MagicMock()
            if call_count["n"] == 1:
                resp.json.return_value = {
                    "success": True,
                    "videos": [
                        {"video_key": "v1", "title": "Vid 1"},
                        {"video_key": "v2", "title": "Vid 2"},
                    ],
                }
            elif call_count["n"] == 2:
                resp.json.return_value = {
                    "success": True,
                    "data": {
                        "title": "Video One",
                        "video_key": "v1",
                        "downloads": [{"quality": "720p", "format": "hls", "url": "https://cdn/v1.m3u8"}],
                    },
                }
            else:
                resp.json.return_value = {
                    "success": True,
                    "data": {
                        "title": "Video Two",
                        "video_key": "v2",
                        "downloads": [{"quality": "1080p", "format": "hls", "url": "https://cdn/v2.m3u8"}],
                    },
                }
            return resp

        monkeypatch.setattr("requests.get", mock_get)
        result = ph_module.resolve_listing("channel", "test")
        assert result["pornhub"] is True
        assert result["title"] == "PH_channel_test"
        assert len(result["videos"]) == 2
        assert result["videos"][0]["url"] == "https://cdn/v1.m3u8"
        assert result["videos"][1]["url"] == "https://cdn/v2.m3u8"
