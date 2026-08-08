"""Tests for mega_client: CTR crypto primitives and gateway response parsing.

The gateway is mocked so no network is needed. Tests that covered the old
Mega API (api_request, ecb/cbc, share-key recovery) are replaced by tests
against the new gateway helpers (resolve_link, list_folder, file_cdn).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


@pytest.fixture(scope="module")
def mc():
    """Load mega_client directly — imports only cryptography, no bot package."""
    path = (
        Path(__file__).resolve().parent.parent
        / "bot" / "helper" / "ext_utils" / "mega_client.py"
    )
    spec = importlib.util.spec_from_file_location("mega_client", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# base64url helpers
# ---------------------------------------------------------------------------

def test_b64url_round_trip(mc):
    """Unpadded base64url: '+' and '/' replaced, no '=' padding."""
    raw = bytes([0xFF, 0xFE, 0x01, 0x00, 0xAB])
    encoded = mc.b64url_encode(raw)
    assert "=" not in encoded and "+" not in encoded and "/" not in encoded
    assert mc.b64url_decode(encoded) == raw


def test_b64url_decodes_padded_input(mc):
    assert mc.b64url_decode("__4B") == mc.b64url_decode("__4B=")


# ---------------------------------------------------------------------------
# CTR crypto
# ---------------------------------------------------------------------------

def test_counter_at_tracks_block_index(mc):
    nonce = bytes(range(8))
    assert mc.counter_at(nonce, 0) == nonce + b"\0" * 8
    assert mc.counter_at(nonce, 16) == nonce + (1).to_bytes(8, "big")
    assert mc.counter_at(nonce, 1048576) == nonce + (65536).to_bytes(8, "big")


def test_counter_at_rounds_down_to_block(mc):
    nonce = bytes(range(8))
    assert mc.counter_at(nonce, 20) == mc.counter_at(nonce, 16)


def test_ctr_decrypts_any_offset_independently(mc):
    """Any slice can be decrypted from its own counter without the prefix."""
    key, nonce = bytes(range(16)), bytes(range(8))
    plain = bytes(range(256)) * 8

    whole = mc.ctr_decrypt(key, mc.counter_at(nonce, 0), plain)
    tail = mc.ctr_decrypt(key, mc.counter_at(nonce, 1024), plain[1024:])
    assert tail == whole[1024:]


def test_ctr_stream_matches_ctr_decrypt(mc):
    """ctr_stream fed incrementally must agree with ctr_decrypt on the whole buffer."""
    key, nonce = bytes(range(16, 32)), bytes(range(8))
    data = bytes(range(256)) * 16
    counter = mc.counter_at(nonce, 0)

    expected = mc.ctr_decrypt(key, counter, data)
    stream = mc.ctr_stream(key, counter)
    got = stream.update(data[:512]) + stream.update(data[512:])
    assert got == expected


# ---------------------------------------------------------------------------
# key_from_node
# ---------------------------------------------------------------------------

def test_key_from_node_folds_correctly(mc):
    """32-byte node key: bytes 0-15 XOR 16-31 = aes_key; bytes 16-23 = nonce."""
    raw = bytes(range(32, 64))
    node = {"key_b64": mc.b64url_encode(raw)}
    aes_key, nonce = mc.key_from_node(node)
    assert aes_key == bytes(a ^ b for a, b in zip(raw[:16], raw[16:32]))
    assert nonce == raw[16:24]


def test_key_from_node_rejects_short_key(mc):
    node = {"key_b64": mc.b64url_encode(bytes(16))}
    with pytest.raises(ValueError):
        mc.key_from_node(node)


# ---------------------------------------------------------------------------
# _reconstruct_url
# ---------------------------------------------------------------------------

def test_reconstruct_url_encodes_hash(mc):
    """Gateway needs the '#' encoded so URL parsers don't strip the fragment."""
    url = mc._reconstruct_url("folder", "AbCd1234", "someKey_-")
    assert "#" not in url
    assert "%23" in url
    assert "AbCd1234" in url


# ---------------------------------------------------------------------------
# Gateway helpers (mocked aiohttp session)
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    """Records GET calls and replays canned responses."""

    def __init__(self, *payloads):
        self._q = list(payloads)
        self.calls = []

    def get(self, url, params=None, **kwargs):
        # url may be a yarl.URL or string
        query = dict(url.query) if hasattr(url, "query") else (params or {})
        self.calls.append({"url": str(url), "params": query})
        status, payload = 200, self._q.pop(0)
        if isinstance(payload, tuple):
            status, payload = payload
        return _Resp(payload, status)


def _file_entry(handle="FH1", name="movie.mkv", path="", size=100,
                aes_key=None, nonce=None, cdn_url=""):
    aes_key = aes_key or "AAAAAAAAAAAAAAAAAAAAAA"  # 16B b64url
    nonce = nonce or "AAAAAAAAAAA"  # 8B b64url
    return {
        "handle": handle, "name": name, "path": path, "size": size,
        "aes_key": aes_key, "nonce": nonce, "meta_mac": "AAAAAAAA",
        "url": cdn_url,
    }


async def _no_sleep(s):
    pass


# resolve_link — file

async def test_resolve_link_file(mc, monkeypatch):
    monkeypatch.setattr(mc, "sleep", _no_sleep)
    f = _file_entry(cdn_url="https://cdn.mega.co.nz/dl/abc")
    body = {
        "success": True, "kind": "file", "name": "movie.mkv",
        "handle": "FH1", "file": f, "files": [], "folders": [],
        "raw_node_count": 1, "error": "", "timestamp": 0,
    }
    session = _Session(body)
    result = await mc.resolve_link(session, "file", "FH1", "someKey")

    assert result["kind"] == "file"
    assert result["files"][0]["cdn_url"] == "https://cdn.mega.co.nz/dl/abc"
    assert len(result["files"]) == 1
    assert "mega.nz/file/FH1" in session.calls[0]["params"]["url"]


# resolve_link — folder

async def test_resolve_link_folder(mc, monkeypatch):
    monkeypatch.setattr(mc, "sleep", _no_sleep)
    files = [
        _file_entry("FH1", "a.mp4", ""), _file_entry("FH2", "b.mp4", "Sub"),
    ]
    body = {
        "success": True, "kind": "folder", "name": "Films",
        "handle": "FOLDER1", "file": None, "files": files, "folders": [],
        "raw_node_count": 3, "error": "", "timestamp": 0,
    }
    session = _Session(body)
    result = await mc.resolve_link(session, "folder", "FOLDER1", "fKey")

    assert result["kind"] == "folder"
    assert result["name"] == "Films"
    assert len(result["files"]) == 2
    names = {f["name"] for f in result["files"]}
    assert names == {"a.mp4", "b.mp4"}


# list_folder

async def test_list_folder_returns_files(mc, monkeypatch):
    monkeypatch.setattr(mc, "sleep", _no_sleep)
    nodes = [
        {"h": "N1", "name": "clip.mp4", "is_folder": False, "t": 0,
         "s": 500, "parent": "ROOT", "path": "", "key_b64": "A" * 43},
        {"h": "D1", "name": "SubDir", "is_folder": True, "t": 1,
         "s": 0, "parent": "ROOT", "path": "", "key_b64": ""},
    ]
    body = {"success": True, "name": "MyShare", "files": nodes,
            "folders": [], "raw_node_count": 2, "error": "", "timestamp": 0}
    session = _Session(body)
    result = await mc.list_folder(session, "FOLDER1", "fKey")

    assert result["name"] == "MyShare"
    assert len(result["files"]) == 1
    assert result["files"][0]["name"] == "clip.mp4"
    assert result["files"][0]["size"] == 500
    # Verify params
    p = session.calls[0]["params"]
    assert p["folder"] == "FOLDER1"
    assert p["key"] == "fKey"


async def test_list_folder_raises_when_no_files(mc, monkeypatch):
    monkeypatch.setattr(mc, "sleep", _no_sleep)
    body = {"success": True, "name": "Empty", "files": [],
            "folders": [], "raw_node_count": 0, "error": "", "timestamp": 0}
    session = _Session(body)
    with pytest.raises(ValueError, match="no downloadable"):
        await mc.list_folder(session, "F1", "k")


# file_cdn

async def test_file_cdn_returns_url_and_size(mc, monkeypatch):
    monkeypatch.setattr(mc, "sleep", _no_sleep)
    body = {"success": True, "cdn_url": "https://g.mega.co.nz/dl/xyz",
            "file_name": "f.mkv", "size": 123456,
            "error": "", "timestamp": 0}
    session = _Session(body)
    url, size = await mc.file_cdn(session, "FOLDER1", "FILE1")

    assert url == "https://g.mega.co.nz/dl/xyz"
    assert size == 123456
    p = session.calls[0]["params"]
    assert p["folder"] == "FOLDER1"
    assert p["file"] == "FILE1"


async def test_file_cdn_raises_on_missing_url(mc, monkeypatch):
    monkeypatch.setattr(mc, "sleep", _no_sleep)
    body = {"success": True, "cdn_url": "", "file_name": "f", "size": 0,
            "error": "", "timestamp": 0}
    session = _Session(body)
    with pytest.raises(mc.MegaApiError):
        await mc.file_cdn(session, "F", "FF")


# Gateway error handling

async def test_gateway_raises_on_success_false(mc, monkeypatch):
    monkeypatch.setattr(mc, "sleep", _no_sleep)
    body = {"success": False, "error": "link expired"}
    session = _Session(body)
    with pytest.raises(mc.MegaApiError, match="link expired"):
        await mc.resolve_link(session, "file", "H", "k")


async def test_gateway_raises_on_http_error(mc, monkeypatch):
    monkeypatch.setattr(mc, "sleep", _no_sleep)

    class _BadResp:
        status = 500
        async def json(self, content_type=None):
            return {}
        async def __aenter__(self): return self
        async def __aexit__(self, *exc): return False

    class _BadSession:
        def get(self, url, params=None, **kwargs):
            return _BadResp()

    with pytest.raises(mc.MegaApiError, match="HTTP 500"):
        await mc.resolve_link(_BadSession(), "file", "H", "k")
