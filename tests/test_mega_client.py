"""Tests for Mega's client-side crypto.

This is the part that fails silently: a wrong key does not raise, it produces
bytes, and the download completes with a file that is simply not the file. So
the primitives are pinned against vectors built here with an independent
encryptor, and the key-recovery oracle is checked to reject a wrong key rather
than accept it.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


@pytest.fixture(scope="module")
def mc():
    """Load mega_client directly - it imports nothing from the bot package, so
    it needs none of the stubbing the other suites do."""
    path = (
        Path(__file__).resolve().parent.parent
        / "bot"
        / "helper"
        / "ext_utils"
        / "mega_client.py"
    )
    spec = importlib.util.spec_from_file_location("mega_client", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _encrypt_cbc(key, data):
    enc = Cipher(algorithms.AES(key), modes.CBC(b"\0" * 16)).encryptor()
    return enc.update(data) + enc.finalize()


def _encrypt_ecb(key, data):
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return enc.update(data) + enc.finalize()


def _attr_blob(mc, attr_key, name):
    plain = b"MEGA" + json.dumps({"n": name}).encode()
    plain += b"\0" * (-len(plain) % 16)  # Mega null-pads, it does not use PKCS#7
    return mc.b64url_encode(_encrypt_cbc(attr_key, plain))


def test_b64url_round_trip(mc):
    """Mega omits base64 padding, and bytes that produce '+' or '/' in
    standard base64 must come back through the url-safe alphabet."""
    raw = bytes([0xFF, 0xFE, 0x01, 0x00, 0xAB])
    encoded = mc.b64url_encode(raw)
    assert "=" not in encoded and "+" not in encoded and "/" not in encoded
    assert mc.b64url_decode(encoded) == raw


def test_b64url_decodes_padded_input(mc):
    """Some links carry padding anyway; it must not become part of the key."""
    assert mc.b64url_decode("__4B") == mc.b64url_decode("__4B=")


def test_unpack_file_key_folds_halves(mc):
    """The AES key is the two halves XORed; the nonce and MAC are slices of
    the second half. Getting this wrong yields a plausible-looking key that
    decrypts to garbage."""
    k32 = bytes(range(32))
    key, nonce, meta_mac = mc.unpack_file_key(k32)
    assert key == bytes(a ^ b for a, b in zip(k32[:16], k32[16:]))
    assert nonce == k32[16:24]
    assert meta_mac == k32[24:32]


def test_unpack_file_key_rejects_short_key(mc):
    with pytest.raises(ValueError):
        mc.unpack_file_key(bytes(16))


def test_counter_at_tracks_block_index(mc):
    """The counter is the nonce plus a big-endian block index, which is what
    lets any byte range be decrypted without reading what came before it."""
    nonce = bytes(range(8))
    assert mc.counter_at(nonce, 0) == nonce + b"\0" * 8
    assert mc.counter_at(nonce, 16) == nonce + (1).to_bytes(8, "big")
    assert mc.counter_at(nonce, 1048576) == nonce + (65536).to_bytes(8, "big")


def test_counter_at_rounds_down_to_block(mc):
    """An offset mid-block maps to that block's counter; the caller drops the
    leading bytes it did not want."""
    nonce = bytes(range(8))
    assert mc.counter_at(nonce, 20) == mc.counter_at(nonce, 16)


def test_ctr_decrypts_any_offset_independently(mc):
    """The property the whole parallel/resume design rests on: a slice
    decrypted from its own counter equals that slice of a whole-file decrypt."""
    key, nonce = bytes(range(16)), bytes(range(8))
    plain = bytes(range(256)) * 8

    whole = mc.ctr_decrypt(key, mc.counter_at(nonce, 0), plain)
    tail = mc.ctr_decrypt(key, mc.counter_at(nonce, 1024), plain[1024:])
    assert tail == whole[1024:]


def test_decrypt_attr_reads_name(mc):
    attr_key = mc.unpack_file_key(bytes(range(32, 64)))[0]
    blob = _attr_blob(mc, attr_key, "holiday video.mp4")
    assert mc.decrypt_attr(attr_key, blob) == {"n": "holiday video.mp4"}


def test_decrypt_attr_rejects_wrong_key(mc):
    """This is also the oracle recover_node_key relies on, so a wrong key must
    come back None rather than as some other name."""
    attr_key = mc.unpack_file_key(bytes(range(32, 64)))[0]
    blob = _attr_blob(mc, attr_key, "holiday video.mp4")
    assert mc.decrypt_attr(bytes(16), blob) is None


def test_decrypt_attr_rejects_misaligned_blob(mc):
    assert mc.decrypt_attr(bytes(16), mc.b64url_encode(b"short")) is None


def _node(mc, share_key, handle, kind, parent, name, node_key, size=0):
    """Build a listing node the way Mega serves one: the node key wrapped
    under the share key, and the attributes encrypted under the node key."""
    attr_key = mc.unpack_file_key(node_key)[0] if kind == 0 else node_key[:16]
    return {
        "h": handle,
        "t": kind,
        "p": parent,
        "s": size,
        "k": f"{handle}:{mc.b64url_encode(_encrypt_ecb(share_key, node_key))}",
        "a": _attr_blob(mc, attr_key, name),
    }


def test_recover_node_key_returns_key_and_attributes(mc):
    share_key, file_key = bytes(range(16)), bytes(range(32, 64))
    node = _node(mc, share_key, "FiLe0001", 0, "ROOT0001", "movie.mkv", file_key)

    raw, attr = mc.recover_node_key(share_key, node)
    assert raw[:32] == file_key
    assert attr["n"] == "movie.mkv"


def test_recover_node_key_rejects_foreign_share_key(mc):
    """A node reachable through several shares carries one wrapped key per
    share, and nothing labels which is which - so a key that does not belong
    must fail rather than yield a wrong name."""
    share_key, file_key = bytes(range(16)), bytes(range(32, 64))
    node = _node(mc, share_key, "FiLe0001", 0, "ROOT0001", "movie.mkv", file_key)
    assert mc.recover_node_key(bytes(16), node) is None


def test_recover_node_key_skips_unusable_pairs(mc):
    """An RSA-wrapped key for someone else's account sits in the same field;
    it is skipped, and the pair that does belong is still found."""
    share_key, file_key = bytes(range(16)), bytes(range(32, 64))
    node = _node(mc, share_key, "FiLe0001", 0, "ROOT0001", "movie.mkv", file_key)
    node["k"] = f"OtHeR999:{mc.b64url_encode(bytes(43))}/{node['k']}"

    raw, attr = mc.recover_node_key(share_key, node)
    assert raw[:32] == file_key and attr["n"] == "movie.mkv"


async def _no_sleep(seconds):
    """Retries back off for seconds at a time; nothing here needs to wait."""


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status = 200

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Stands in for aiohttp: records what was asked and replays canned
    responses, so the listing logic is tested without a network."""

    def __init__(self, *payloads):
        self._payloads = list(payloads)
        self.calls = []

    def post(self, url, params=None, json=None):
        self.calls.append({"url": url, "params": params or {}, "json": json})
        return _FakeResponse(self._payloads.pop(0))


def _share(mc):
    """A share shaped like:  Film/ (root)  movie.mkv, Vids/clip.mp4"""
    share_key = bytes(range(16))
    file_key = bytes(range(32, 64))
    return share_key, file_key, [
        _node(mc, share_key, "ROOT0001", 1, "OUTSIDE0", "Film", bytes(range(16))),
        _node(mc, share_key, "VIDS0001", 1, "ROOT0001", "Vids", bytes(range(16))),
        _node(mc, share_key, "FiLe0001", 0, "ROOT0001", "movie.mkv", file_key, 100),
        _node(mc, share_key, "FiLe0002", 0, "VIDS0001", "clip.mp4", file_key, 50),
    ]


async def test_list_folder_flattens_the_tree(mc):
    share_key, _, nodes = _share(mc)
    session = _FakeSession([{"f": nodes}])

    listing = await mc.list_folder(session, "ShArE001", mc.b64url_encode(share_key))

    assert listing["name"] == "Film"
    by_name = {f["name"]: f for f in listing["files"]}
    # Paths are relative to the share root, which is not itself part of them.
    assert by_name["movie.mkv"]["path"] == ""
    assert by_name["clip.mp4"]["path"] == "Vids"
    assert by_name["movie.mkv"]["size"] == 100


async def test_list_folder_asks_for_the_whole_subtree(mc):
    """r:1 is what makes Mega return descendants in one flat array; without it
    every folder would need its own round trip."""
    share_key, _, nodes = _share(mc)
    session = _FakeSession([{"f": nodes}])

    await mc.list_folder(session, "ShArE001", mc.b64url_encode(share_key))

    assert session.calls[0]["json"] == [{"a": "f", "c": 1, "r": 1, "ca": 1}]
    assert session.calls[0]["params"]["n"] == "ShArE001"


async def test_list_folder_rejects_a_bad_key_length(mc):
    session = _FakeSession([{"f": []}])
    with pytest.raises(ValueError):
        await mc.list_folder(session, "ShArE001", mc.b64url_encode(bytes(8)))


async def test_list_folder_raises_when_nothing_decrypts(mc):
    """A wrong key decrypts no node at all, which is worth saying plainly
    rather than reporting an empty folder."""
    _, _, nodes = _share(mc)
    session = _FakeSession([{"f": nodes}])
    with pytest.raises(ValueError):
        await mc.list_folder(session, "ShArE001", mc.b64url_encode(bytes(16)))


def test_error_classification(mc):
    """These three drive the caller's behaviour: quota means change IP,
    transient means try again, anything else means stop."""
    assert mc.MegaApiError(-17).is_quota  # EOVERQUOTA
    assert mc.MegaApiError(-4).is_quota  # ERATELIMIT
    assert mc.MegaApiError(-3).is_transient  # EAGAIN
    assert not mc.MegaApiError(-9).is_quota  # ENOENT
    assert not mc.MegaApiError(-9).is_transient


def test_error_message_is_readable(mc):
    """The text ends up in a Telegram message, so it says what happened
    rather than printing a bare number."""
    assert "no longer exists" in str(mc.MegaApiError(-9))
    assert "quota" in str(mc.MegaApiError(-17))


async def test_api_request_raises_quota_without_retrying(mc):
    """Retrying a quota error just burns time - only a new IP clears it."""
    session = _FakeSession(-17, -17, -17, -17)
    with pytest.raises(mc.MegaApiError) as excinfo:
        await mc.api_request(session, {"a": "f"}, context="listing")
    assert excinfo.value.is_quota
    assert len(session.calls) == 1


async def test_api_request_retries_transient_errors(mc, monkeypatch):
    """-3 means "ask again", so it is the one code worth retrying here."""
    monkeypatch.setattr(mc, "sleep", _no_sleep)
    session = _FakeSession(-3, -3, [{"f": []}])

    result = await mc.api_request(session, {"a": "f"})

    assert result == {"f": []}
    assert len(session.calls) == 3


async def test_api_request_unwraps_the_response_array(mc):
    """Mega answers a one-command batch with a one-element array."""
    session = _FakeSession([{"g": "https://cdn.example/file", "s": 42}])
    assert (await mc.api_request(session, {"a": "g"}))["s"] == 42


async def test_api_request_sends_the_command_as_a_batch(mc):
    """The command must go out wrapped in an array. A bare object is not
    rejected - Mega answers it with an empty array, which used to unwrap into
    a list and only failed later at .get(), looking like an empty folder."""
    session = _FakeSession([{"f": []}])
    await mc.api_request(session, {"a": "f", "c": 1})
    assert session.calls[0]["json"] == [{"a": "f", "c": 1}]


async def test_api_request_rejects_an_empty_batch(mc):
    """An empty array carries no result, so it must fail loudly rather than
    being handed on as if it were the response object."""
    session = _FakeSession([])
    with pytest.raises(ConnectionError):
        await mc.api_request(session, {"a": "f"}, context="listing the folder")


async def test_api_request_varies_the_sequence_id(mc, monkeypatch):
    """'id' is a cache-buster Mega expects to differ per request."""
    monkeypatch.setattr(mc, "sleep", _no_sleep)
    session = _FakeSession(-3, [{"f": []}])
    await mc.api_request(session, {"a": "f"})
    assert session.calls[0]["params"]["id"] != session.calls[1]["params"]["id"]
