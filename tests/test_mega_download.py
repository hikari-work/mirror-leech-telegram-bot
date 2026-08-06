"""Tests for the Mega downloader's segmenting and reassembly.

Decryption bugs here do not raise - they write a file of the right length full
of the wrong bytes, which survives the download, the upload, and is only
noticed by whoever opens it. So these tests do the one check that catches that
class of bug: encrypt a known payload, run it through the real download path,
and compare the file on disk byte for byte.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


@pytest.fixture
def mega_dl(monkeypatch, tmp_path):
    """Import mega_download with the bot package stubbed to the few names it
    actually touches."""
    root = Path(__file__).resolve().parent.parent

    bot_pkg = ModuleType("bot")
    bot_pkg.__path__ = []

    class _Logger:
        @staticmethod
        def info(msg):
            pass

        error = warning = debug = info

    bot_pkg.LOGGER = _Logger()
    bot_pkg.task_dict = {}

    class _Lock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    bot_pkg.task_dict_lock = _Lock()

    core_pkg = ModuleType("bot.core")
    core_pkg.__path__ = []
    config_manager = ModuleType("bot.core.config_manager")

    class Config:
        WARP_ENABLED = False
        WARP_PROXY_PORT = 40000
        MEGA_PROXY_URL = ""
        MEGA_CONNECTIONS = 4
        MEGA_MAX_RESTARTS = 3

    config_manager.Config = Config

    helper_pkg = ModuleType("bot.helper")
    helper_pkg.__path__ = []
    ext_utils_pkg = ModuleType("bot.helper.ext_utils")
    ext_utils_pkg.__path__ = [str(root / "bot" / "helper" / "ext_utils")]

    task_manager = ModuleType("bot.helper.ext_utils.task_manager")

    async def _no_duplicate(listener):
        return False, None

    async def _no_queue(listener):
        return False, None

    task_manager.stop_duplicate_check = _no_duplicate
    task_manager.check_running_tasks = _no_queue

    warp_mod = ModuleType("bot.helper.ext_utils.warp_utils")

    async def _ensure():
        return False

    async def _restart(force=False):
        return False

    warp_mod.ensure_proxy_mode = _ensure
    warp_mod.restart_warp = _restart
    warp_mod.warp_proxy_url = lambda: ""

    bot_utils_mod = ModuleType("bot.helper.ext_utils.bot_utils")

    async def cmd_exec(cmd, shell=False):
        return "", "", 0

    bot_utils_mod.cmd_exec = cmd_exec

    mlu_pkg = ModuleType("bot.helper.mirror_leech_utils")
    mlu_pkg.__path__ = []
    du_pkg = ModuleType("bot.helper.mirror_leech_utils.download_utils")
    du_pkg.__path__ = [
        str(root / "bot" / "helper" / "mirror_leech_utils" / "download_utils")
    ]
    su_pkg = ModuleType("bot.helper.mirror_leech_utils.status_utils")
    su_pkg.__path__ = [
        str(root / "bot" / "helper" / "mirror_leech_utils" / "status_utils")
    ]

    status_utils = ModuleType("bot.helper.ext_utils.status_utils")
    status_utils.MirrorStatus = SimpleNamespace(STATUS_DOWNLOAD="Download")
    status_utils.get_readable_file_size = lambda n: str(n)
    status_utils.get_readable_time = lambda n: str(n)

    queue_status = ModuleType("bot.helper.mirror_leech_utils.status_utils.queue_status")
    queue_status.QueueStatus = object

    tg_pkg = ModuleType("bot.helper.telegram_helper")
    tg_pkg.__path__ = []
    message_utils = ModuleType("bot.helper.telegram_helper.message_utils")

    async def send_status_message(message):
        return None

    message_utils.send_status_message = send_status_message

    for name, mod in {
        "bot": bot_pkg,
        "bot.core": core_pkg,
        "bot.core.config_manager": config_manager,
        "bot.helper": helper_pkg,
        "bot.helper.ext_utils": ext_utils_pkg,
        "bot.helper.ext_utils.bot_utils": bot_utils_mod,
        "bot.helper.ext_utils.status_utils": status_utils,
        "bot.helper.ext_utils.task_manager": task_manager,
        "bot.helper.ext_utils.warp_utils": warp_mod,
        "bot.helper.mirror_leech_utils": mlu_pkg,
        "bot.helper.mirror_leech_utils.download_utils": du_pkg,
        "bot.helper.mirror_leech_utils.status_utils": su_pkg,
        "bot.helper.mirror_leech_utils.status_utils.queue_status": queue_status,
        "bot.helper.telegram_helper": tg_pkg,
        "bot.helper.telegram_helper.message_utils": message_utils,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)

    for name in (
        "bot.helper.mirror_leech_utils.download_utils.mega_download",
        "bot.helper.ext_utils.mega_client",
    ):
        sys.modules.pop(name, None)

    module = importlib.import_module(
        "bot.helper.mirror_leech_utils.download_utils.mega_download"
    )
    module.Config = Config
    return module


class _Listener:
    """The parts of the real listener the downloader touches."""

    def __init__(self, tmp_path, link):
        self.link = link
        self.mid = 1
        self.multi = 1
        self.is_rss = False
        self.is_cancelled = False
        self.name = ""
        self.size = 0
        self.message = None
        self.errors = []
        self.completed = False
        self.dir = str(tmp_path)

    async def on_download_start(self):
        pass

    async def on_download_complete(self):
        self.completed = True

    async def on_download_error(self, message, button=None):
        self.errors.append(message)


def _ciphertext(mc, key32, plain):
    """Encrypt the way Mega does: AES-CTR from a zero block index. CTR is
    symmetric, so the module's own decrypt doubles as the encryptor."""
    aes_key, nonce, _ = mc.unpack_file_key(key32)
    return mc.ctr_decrypt(aes_key, mc.counter_at(nonce, 0), plain)


class _Response:
    """Serves a byte range of the ciphertext, honouring the Range header the
    way a CDN does - which is what the resume logic depends on."""

    def __init__(self, body, headers, status=200, stall=None):
        self.status = status
        self._stall = stall
        start, end = 0, len(body) - 1
        if (rng := headers.get("Range", "")).startswith("bytes="):
            first, _, last = rng[6:].partition("-")
            start = int(first)
            if last:
                # Honouring the end bound is what makes the parallel test real:
                # without it every connection receives the whole tail and a
                # segment writing to the wrong offset still lands correct bytes.
                end = min(end, int(last))
        self._body = body[start : end + 1]
        if status in (200, 206) and start:
            self.status = 206

    @property
    def content(self):
        return self

    async def iter_chunked(self, size):
        sent = 0
        for at in range(0, len(self._body), size):
            if self._stall is not None and sent >= self._stall:
                return  # connection dies mid-transfer
            chunk = self._body[at : at + size]
            sent += len(chunk)
            yield chunk

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _CDN:
    """Stands in for aiohttp: hands out ranges of one encrypted body, and can
    be told to fail the first N requests with a quota status."""

    def __init__(self, body, quota_first=0, stall=None):
        self.body = body
        self.quota_first = quota_first
        self.stall = stall
        self.requests = []

    def get(self, url, headers=None, proxy=None):
        headers = headers or {}
        self.requests.append(headers.get("Range", ""))
        if self.quota_first > 0:
            self.quota_first -= 1
            return _Response(self.body, headers, status=509)
        stall = self.stall
        if stall is not None:
            self.stall = None  # only the first attempt is cut short
        return _Response(self.body, headers, stall=stall)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def mc():
    """The crypto module, loaded standalone to build the test vectors."""
    import importlib.util

    path = (
        Path(__file__).resolve().parent.parent
        / "bot" / "helper" / "ext_utils" / "mega_client.py"
    )
    spec = importlib.util.spec_from_file_location("mega_client_vectors", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


KEY32 = bytes(range(32, 64))


def _helper(mega_dl, tmp_path, cdn, size, monkeypatch, restarts=None):
    """Wire a helper up to the fake CDN and a fake resolve_file."""
    listener = _Listener(tmp_path, {"mega": {"kind": "file"}})
    helper = mega_dl.MegaDownloadHelper(listener)
    helper._proxy = "socks5://127.0.0.1:40000"  # rotation only makes sense with one

    async def resolve_file(session, handle, folder=None):
        return "https://cdn.example/dl", size

    async def restart_warp(force=False):
        if restarts is not None:
            restarts.append(force)
        return True

    monkeypatch.setattr(mega_dl, "resolve_file", resolve_file)
    monkeypatch.setattr(mega_dl, "restart_warp", restart_warp)
    monkeypatch.setattr(mega_dl, "sleep", _no_sleep)
    return helper, listener


async def _no_sleep(seconds):
    """Rotation waits a few seconds for the tunnel; nothing here needs to."""


async def test_single_span_file_decrypts_byte_for_byte(mega_dl, mc, tmp_path, monkeypatch):
    """The check that matters: a decryption bug produces a file of exactly the
    right length full of the wrong bytes, and nothing else would notice."""
    plain = bytes(range(256)) * 40  # 10 KiB, below MIN_SPLIT so one connection
    cdn = _CDN(_ciphertext(mc, KEY32, plain))
    helper, _ = _helper(mega_dl, tmp_path, cdn, len(plain), monkeypatch)

    dest = str(tmp_path / "out.bin")
    item = {"handle": "H", "name": "out.bin", "key": KEY32, "size": len(plain)}
    assert await helper._download_file(cdn, item, None, dest) is True

    assert Path(dest).read_bytes() == plain
    assert helper.processed_bytes == len(plain)


async def test_parallel_spans_reassemble_in_order(mega_dl, mc, tmp_path, monkeypatch):
    """Each connection derives its own counter from its start offset and seeks
    to its own place. A wrong counter or a wrong seek shows up as a file that
    is the right size but scrambled at the seams."""
    plain = bytes((i * 7 + 3) % 256 for i in range(40 * 1024 * 1024))
    cdn = _CDN(_ciphertext(mc, KEY32, plain))
    helper, _ = _helper(mega_dl, tmp_path, cdn, len(plain), monkeypatch)

    dest = str(tmp_path / "big.bin")
    item = {"handle": "H", "name": "big.bin", "key": KEY32, "size": len(plain)}
    await helper._download_file(cdn, item, None, dest)

    assert len(cdn.requests) == 4  # MEGA_CONNECTIONS
    assert Path(dest).read_bytes() == plain


async def test_quota_rotates_warp_and_resumes(mega_dl, mc, tmp_path, monkeypatch):
    """A quota response must rotate the egress IP and pick up where it left
    off - re-downloading from zero is what the resume design exists to avoid."""
    plain = bytes(range(251)) * 4096
    cdn = _CDN(_ciphertext(mc, KEY32, plain), quota_first=1)
    restarts = []
    helper, _ = _helper(mega_dl, tmp_path, cdn, len(plain), monkeypatch, restarts)

    dest = str(tmp_path / "q.bin")
    item = {"handle": "H", "name": "q.bin", "key": KEY32, "size": len(plain)}
    await helper._download_file(cdn, item, None, dest)

    assert len(restarts) == 1
    assert Path(dest).read_bytes() == plain


async def test_quota_without_a_proxy_does_not_touch_the_tunnel(
    mega_dl, mc, tmp_path, monkeypatch
):
    """Rotating the egress IP only means anything when the request goes through
    the proxy. Downloading directly, a restart cannot change the address Mega
    saw, so it is 50 wasted seconds per file and the error should surface."""
    plain = bytes(range(251)) * 4096
    cdn = _CDN(_ciphertext(mc, KEY32, plain), quota_first=1)
    restarts = []
    helper, _ = _helper(mega_dl, tmp_path, cdn, len(plain), monkeypatch, restarts)
    helper._proxy = ""  # direct download, the fallback path

    item = {"handle": "H", "name": "q.bin", "key": KEY32, "size": len(plain)}
    with pytest.raises(Exception):
        await helper._download_file(cdn, item, None, str(tmp_path / "q.bin"))

    assert restarts == []


async def test_interrupted_transfer_resumes_from_its_own_offset(
    mega_dl, mc, tmp_path, monkeypatch
):
    """The resume path is where an off-by-one silently corrupts the join: the
    second request starts mid-file, backs up to a block boundary and drops the
    replayed bytes."""
    plain = bytes((i * 31 + 11) % 256 for i in range(3 * 1024 * 1024 + 12345))
    cdn = _CDN(_ciphertext(mc, KEY32, plain), stall=1024 * 1024)
    helper, listener = _helper(mega_dl, tmp_path, cdn, len(plain), monkeypatch)

    dest = Path(tmp_path / "r.bin")
    dest.write_bytes(b"\0" * len(plain))
    aes_key, nonce, _ = mc.unpack_file_key(KEY32)
    done = [0]
    end = len(plain) - 1

    # First attempt is cut short partway through.
    with pytest.raises(ConnectionError):
        await helper._segment(
            cdn, "u", str(dest), aes_key, nonce, 0, end, done
        )
    assert 0 < done[0] < len(plain)

    # Second attempt continues from done[0] rather than from zero.
    await helper._segment(cdn, "u", str(dest), aes_key, nonce, 0, end, done)

    assert cdn.requests[1] != "bytes=0-%d" % end
    assert dest.read_bytes() == plain


async def test_resume_from_an_unaligned_offset_drops_the_replayed_bytes(
    mega_dl, mc, tmp_path, monkeypatch
):
    """CTR can only start on a 16-byte boundary, so an odd resume point is
    backed up and the overlap discarded. Getting that wrong shifts the rest of
    the file by a few bytes - still the right length, still unreadable."""
    plain = bytes((i * 13 + 5) % 256 for i in range(200_000))
    cdn = _CDN(_ciphertext(mc, KEY32, plain))
    helper, _ = _helper(mega_dl, tmp_path, cdn, len(plain), monkeypatch)

    dest = Path(tmp_path / "u.bin")
    dest.write_bytes(b"\0" * len(plain))
    aes_key, nonce, _ = mc.unpack_file_key(KEY32)

    # 7 bytes into a block: the request must start at 12345-7 and drop 7 bytes.
    done = [12345]
    await helper._segment(
        cdn, "u", str(dest), aes_key, nonce, 0, len(plain) - 1, done
    )

    assert cdn.requests[0] == "bytes=12336-199999"
    assert dest.read_bytes()[12345:] == plain[12345:]


def test_spans_cover_the_file_without_gaps_or_overlap(mega_dl, monkeypatch):
    """Any gap is a hole of zero bytes in the delivered file, and any overlap
    is two connections writing the same offsets."""
    for size in (1, 4095, 8 * 1024 * 1024, 40 * 1024 * 1024 + 7, 1 << 30):
        spans = mega_dl.MegaDownloadHelper(None)._spans(size)
        assert spans[0][0] == 0
        assert spans[-1][1] == size - 1
        for (_, prev_end), (next_start, _) in zip(spans, spans[1:]):
            assert next_start == prev_end + 1
            assert next_start % 16 == 0


def test_small_files_are_not_split(mega_dl):
    """Four connections against a 2 MiB file would cost four round trips to
    save nothing."""
    assert mega_dl.MegaDownloadHelper(None)._spans(2 * 1024 * 1024) == [
        (0, 2 * 1024 * 1024 - 1)
    ]


async def test_cancellation_stops_writing(mega_dl, mc, tmp_path, monkeypatch):
    """Cancel has to take effect mid-transfer, not after the file finishes."""
    plain = bytes(range(256)) * 8192
    cdn = _CDN(_ciphertext(mc, KEY32, plain))
    helper, listener = _helper(mega_dl, tmp_path, cdn, len(plain), monkeypatch)
    listener.is_cancelled = True

    dest = Path(tmp_path / "c.bin")
    dest.write_bytes(b"\0" * len(plain))
    aes_key, nonce, _ = mc.unpack_file_key(KEY32)

    await helper._segment(
        cdn, "u", str(dest), aes_key, nonce, 0, len(plain) - 1, [0]
    )

    assert helper.processed_bytes == 0
