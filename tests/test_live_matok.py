"""The matok live gateway client, driven through ``httpx.MockTransport``.

The module is loaded straight from disk: importing ``bot.helper.net`` would pull
in ``bot``, whose ``__init__`` installs uvloop and opens ``log.txt``. The client
itself only needs stdlib and httpx, which is why it takes its HTTP client, base
URL and headers as arguments instead of importing the gateway helpers.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

_ROOT = Path(__file__).resolve().parent.parent
_MODULE = _ROOT / "bot" / "helper" / "net" / "matok_live.py"


@pytest.fixture(scope="module")
def matok():
    spec = importlib.util.spec_from_file_location("matok_live_under_test", _MODULE)
    module = importlib.util.module_from_spec(spec)
    # Python 3.14's dataclasses resolve annotations through sys.modules, so a
    # spec-loaded module has to be registered before its body runs.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_rooms_parses_a_page_and_sends_the_bearer(matok):
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json={
                "success": True,
                "total": 279,
                "page": 2,
                "page_size": 10,
                "pages": 28,
                "rooms": [
                    {
                        "anchor_id": "1807111564595621889",
                        "nickname": "cua",
                        "game": "ML",
                        "online_count": 5,
                    },
                    {"anchor_id": "", "nickname": "broken"},
                ],
            },
        )

    async with _client(handler) as http:
        page = await matok.list_rooms(
            http, "https://gw.test/", {"Authorization": "Bearer t"}, page=2
        )

    assert seen["path"] == "/api/v1/live/matok/rooms"
    assert seen["params"] == {"page": "2", "page_size": "10"}
    assert seen["auth"] == "Bearer t"
    assert (page.page, page.pages, page.total) == (2, 28, 279)
    assert [room.nickname for room in page.rooms] == ["cua"]
    assert page.rooms[0].anchor_id == "1807111564595621889"


async def test_stream_parses_the_signed_url(matok):
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "success": True,
                "anchor_id": "1807111564595621889",
                "nickname": "cua",
                "game": "ML",
                "url": "rtmp://cdn.test/live/abc?sign=1",
                "expires_at": 1792497756,
                "quality": "GQ",
                "secret": False,
                "pull_sdk": 0,
                "buy": False,
            },
        )

    async with _client(handler) as http:
        stream = await matok.fetch_stream(
            http, "https://gw.test", {}, "1807111564595621889"
        )

    assert seen["path"] == "/api/v1/live/matok/stream"
    assert seen["params"] == {"anchor_id": "1807111564595621889"}
    assert stream.url == "rtmp://cdn.test/live/abc?sign=1"
    assert stream.nickname == "cua"
    assert stream.expires_at == 1792497756


async def test_a_soft_failure_carries_its_code_and_status(matok):
    def handler(request):
        return httpx.Response(
            404,
            json={
                "success": False,
                "code": "ROOM_OFFLINE",
                "error": "room is offline",
            },
        )

    async with _client(handler) as http:
        with pytest.raises(matok.LiveError) as caught:
            await matok.fetch_stream(http, "https://gw.test", {}, "123")

    assert caught.value.code == "ROOM_OFFLINE"
    assert caught.value.status == 404
    assert "offline" in caught.value.friendly


async def test_success_false_on_a_200_is_still_a_failure(matok):
    def handler(request):
        return httpx.Response(
            200,
            json={"success": False, "code": "TOKEN_INVALID", "error": "nope"},
        )

    async with _client(handler) as http:
        with pytest.raises(matok.LiveError) as caught:
            await matok.list_rooms(http, "https://gw.test", {})

    assert caught.value.code == "TOKEN_INVALID"


async def test_a_non_json_body_is_a_gateway_error(matok):
    def handler(request):
        return httpx.Response(502, text="<html>bad gateway</html>")

    async with _client(handler) as http:
        with pytest.raises(matok.LiveError) as caught:
            await matok.list_rooms(http, "https://gw.test", {})

    assert caught.value.code == "GATEWAY_ERROR"
    assert caught.value.status == 502


async def test_a_transport_error_is_a_gateway_error(matok):
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    async with _client(handler) as http:
        with pytest.raises(matok.LiveError) as caught:
            await matok.fetch_stream(http, "https://gw.test", {}, "123")

    assert caught.value.code == "GATEWAY_ERROR"


async def test_an_unusable_stream_url_is_refused(matok):
    def handler(request):
        return httpx.Response(
            200,
            json={
                "success": True,
                "anchor_id": "123",
                "url": "file:///etc/passwd",
                "expires_at": 1,
            },
        )

    async with _client(handler) as http:
        with pytest.raises(matok.LiveError) as caught:
            await matok.fetch_stream(http, "https://gw.test", {}, "123")

    assert caught.value.code == "URL_UNAVAILABLE"


def test_only_live_stream_schemes_are_accepted(matok):
    assert matok.valid_stream_url("rtmp://cdn.test/live/x?sign=1")
    assert matok.valid_stream_url("https://cdn.test/live/x.m3u8")
    assert not matok.valid_stream_url("file:///tmp/x")
    # With a host, so the scheme -- not the missing netloc -- is what refuses it.
    assert not matok.valid_stream_url("file://host/etc/passwd")
    assert not matok.valid_stream_url("ftp://cdn.test/live/x")
    assert not matok.valid_stream_url("concat:http://a|http://b")
    assert not matok.valid_stream_url("rtmp://cdn.test/li\nve")
    assert not matok.valid_stream_url("")
