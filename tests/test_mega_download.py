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
        "bot.helper.ext_utils.status_utils": status_utils,
        "bot.helper.ext_utils.task_manager": task_manager,
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


@pytest.fixture(autouse=True)
def _clean_proxy_pool(mega_dl):
    """The proxy pool lives in a module global shared across tests.

    mega_dl re-imports mega_download each test but proxy_pool stays cached, so
    point it at this test's stub Config and clear the cache both sides of the
    test — otherwise a gateway fetch in one test leaks into another's fallback
    assertions.
    """
    proxy_pool = sys.modules["bot.helper.ext_utils.proxy_pool"]
    proxy_pool.Config = mega_dl.Config
    proxy_pool.reset_proxy_pool()
    yield
    proxy_pool.reset_proxy_pool()


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


# A 16-byte AES key and 8-byte nonce, as the gateway hands them over.
AES_KEY = bytes(range(16))
NONCE = bytes(range(32, 40))


def _ciphertext(mc, plain):
    """Encrypt the way Mega does: AES-CTR from a zero block index. CTR is
    symmetric, so the module's own decrypt doubles as the encryptor."""
    return mc.ctr_decrypt(AES_KEY, mc.counter_at(NONCE, 0), plain)


def _item(name="out.bin", path="", size=0, cdn="https://cdn.example/dl"):
    """A resolve_link-shaped item carrying pre-decoded key material."""
    return {
        "handle": "H", "name": name, "path": path, "size": size,
        "aes_key": AES_KEY, "nonce": NONCE, "cdn_url": cdn,
    }


class _Response:
    """Serves a byte range of the ciphertext, honouring the Range header."""

    def __init__(self, body, headers, status=200, stall=None, aligned=False):
        self.status = status
        self._stall = stall
        self._aligned = aligned
        start, end = 0, len(body) - 1
        if (rng := headers.get("Range", "")).startswith("bytes="):
            first, _, last = rng[6:].partition("-")
            start = int(first)
            if last:
                end = min(end, int(last))
        self._body = body[start : end + 1]
        if status in (200, 206) and start:
            self.status = 206

    @property
    def content(self):
        return self

    async def iter_chunked(self, size):
        """aiohttp yields *at most* `size` bytes; ragged sizes are the point."""
        sent = 0
        at = 0
        step = 0
        while at < len(self._body):
            if self._stall is not None and sent >= self._stall:
                return  # connection dies mid-transfer
            take = size if self._aligned else self._RAGGED[step % len(self._RAGGED)]
            chunk = self._body[at : at + min(take, size)]
            at += len(chunk)
            sent += len(chunk)
            step += 1
            yield chunk

    _RAGGED = (1000, 4321, 16, 11_911, 7, 65_536, 333)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _CDN:
    """Stands in for aiohttp: hands out ranges of one encrypted body, and can
    be told to fail the first N requests with a quota status.

    Records the proxy index seen in each request URL so proxy rotation on
    quota can be asserted.
    """

    def __init__(self, body, quota_first=0, stall=None, aligned=False):
        self.body = body
        self.quota_first = quota_first
        self.stall = stall
        self.aligned = aligned
        self.requests = []
        self.proxies = []

    def get(self, url, headers=None):
        headers = headers or {}
        self.requests.append(headers.get("Range", ""))
        # Extract proxy-N from the worker URL.
        if "proxy-" in url:
            n = url.split("proxy-", 1)[1][0]
            self.proxies.append(int(n))
        if self.quota_first > 0:
            self.quota_first -= 1
            return _Response(self.body, headers, status=509)
        stall = self.stall
        if stall is not None:
            self.stall = None  # only the first attempt is cut short
        return _Response(self.body, headers, stall=stall, aligned=self.aligned)

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


async def _no_sleep(seconds):
    """Rotation waits a few seconds; nothing here needs to."""


def _helper(mega_dl, tmp_path, monkeypatch, link=None):
    """Wire a helper up, patching sleep so rotation does not stall."""
    listener = _Listener(tmp_path, link or {"mega": {"kind": "file"}})
    helper = mega_dl.MegaDownloadHelper(listener)
    monkeypatch.setattr(mega_dl, "sleep", _no_sleep)
    return helper, listener


async def test_single_span_file_decrypts_byte_for_byte(mega_dl, mc, tmp_path, monkeypatch):
    """A decryption bug produces a file of exactly the right length full of the
    wrong bytes, and nothing else would notice."""
    plain = bytes(range(256)) * 40  # 10 KiB, below MIN_SPLIT so one connection
    cdn = _CDN(_ciphertext(mc, plain))
    helper, _ = _helper(mega_dl, tmp_path, monkeypatch)

    dest = str(tmp_path / "out.bin")
    item = _item(name="out.bin", size=len(plain))
    assert await helper._download_file(cdn, item, None, dest) is True

    assert Path(dest).read_bytes() == plain
    assert helper.processed_bytes == len(plain)


async def test_parallel_spans_reassemble_in_order(mega_dl, mc, tmp_path, monkeypatch):
    """Each connection derives its own counter from its start offset and seeks
    to its own place."""
    plain = bytes((i * 7 + 3) % 256 for i in range(40 * 1024 * 1024))
    cdn = _CDN(_ciphertext(mc, plain))
    helper, _ = _helper(mega_dl, tmp_path, monkeypatch)

    dest = str(tmp_path / "big.bin")
    item = _item(name="big.bin", size=len(plain))
    await helper._download_file(cdn, item, None, dest)

    assert len(cdn.requests) == 4  # MEGA_CONNECTIONS
    assert Path(dest).read_bytes() == plain


async def test_quota_rotates_proxy_and_resumes(mega_dl, mc, tmp_path, monkeypatch):
    """A quota response must retry and pick up where it left off."""
    plain = bytes(range(251)) * 4096
    cdn = _CDN(_ciphertext(mc, plain), quota_first=1)
    helper, _ = _helper(mega_dl, tmp_path, monkeypatch)

    dest = str(tmp_path / "q.bin")
    item = _item(name="q.bin", size=len(plain))
    await helper._download_file(cdn, item, None, dest)

    assert Path(dest).read_bytes() == plain


async def test_quota_rotation_auto_scales_with_proxy_list(
    mega_dl, mc, tmp_path, monkeypatch
):
    """Rotation budget automatically scales to len(proxies) if higher than MEGA_MAX_RESTARTS."""
    monkeypatch.setattr(mega_dl.Config, "MEGA_PROXY_URL", "http://p1 http://p2 http://p3 http://p4 http://p5", raising=False)
    monkeypatch.setattr(mega_dl.Config, "MEGA_MAX_RESTARTS", 2)
    plain = bytes(range(251)) * 512
    cdn = _CDN(_ciphertext(mc, plain), quota_first=4)
    helper, _ = _helper(mega_dl, tmp_path, monkeypatch)

    dest = str(tmp_path / "q_scale.bin")
    item = _item(name="q_scale.bin", size=len(plain))
    await helper._download_file(cdn, item, None, dest)
    assert Path(dest).read_bytes() == plain


async def test_quota_rotation_wraps_and_gives_up_at_budget(
    mega_dl, mc, tmp_path, monkeypatch
):
    """MEGA_MAX_RESTARTS bounds rotation per file; exceeding it raises."""
    plain = bytes(range(251)) * 512  # < 8 MiB → single span
    cdn = _CDN(_ciphertext(mc, plain), quota_first=999)
    helper, _ = _helper(mega_dl, tmp_path, monkeypatch)

    item = _item(name="q.bin", size=len(plain))
    with pytest.raises(Exception):
        await helper._download_file(cdn, item, None, str(tmp_path / "q.bin"))


async def test_file_cdn_called_when_no_cdn_url(mega_dl, mc, tmp_path, monkeypatch):
    """A folder item has no cdn_url; file_cdn must be called to fetch one."""
    plain = bytes(range(256)) * 40
    cdn = _CDN(_ciphertext(mc, plain))
    helper, _ = _helper(mega_dl, tmp_path, monkeypatch)

    calls = []

    async def fake_file_cdn(session, folder, handle):
        calls.append((folder, handle))
        return "https://cdn.example/resolved", len(plain)

    monkeypatch.setattr(mega_dl, "file_cdn", fake_file_cdn)

    dest = str(tmp_path / "f.bin")
    item = _item(name="f.bin", size=len(plain), cdn="")  # no cdn_url
    await helper._download_file(cdn, item, "FOLDER1", dest)

    assert calls == [("FOLDER1", "H")]
    assert Path(dest).read_bytes() == plain


async def test_interrupted_transfer_resumes_from_its_own_offset(
    mega_dl, mc, tmp_path, monkeypatch
):
    """The resume path is where an off-by-one silently corrupts the join."""
    plain = bytes((i * 31 + 11) % 256 for i in range(3 * 1024 * 1024 + 12345))
    cdn = _CDN(_ciphertext(mc, plain), stall=1024 * 1024)
    helper, listener = _helper(mega_dl, tmp_path, monkeypatch)

    dest = Path(tmp_path / "r.bin")
    dest.write_bytes(b"\0" * len(plain))
    done = [0]
    end = len(plain) - 1

    with pytest.raises(ConnectionError):
        await helper._segment(cdn, "u", str(dest), AES_KEY, NONCE, 0, end, done)
    assert 0 < done[0] < len(plain)

    await helper._segment(cdn, "u", str(dest), AES_KEY, NONCE, 0, end, done)

    assert cdn.requests[1] != "bytes=0-%d" % end
    assert dest.read_bytes() == plain


async def test_resume_from_an_unaligned_offset_drops_the_replayed_bytes(
    mega_dl, mc, tmp_path, monkeypatch
):
    """CTR can only start on a 16-byte boundary, so an odd resume point is
    backed up and the overlap discarded."""
    plain = bytes((i * 13 + 5) % 256 for i in range(200_000))
    cdn = _CDN(_ciphertext(mc, plain))
    helper, _ = _helper(mega_dl, tmp_path, monkeypatch)

    dest = Path(tmp_path / "u.bin")
    dest.write_bytes(b"\0" * len(plain))

    done = [12345]  # 7 bytes into a block: request starts at 12336, drops 9
    await helper._segment(cdn, "u", str(dest), AES_KEY, NONCE, 0, len(plain) - 1, done)

    assert cdn.requests[0] == "bytes=12336-199999"
    assert dest.read_bytes()[12345:] == plain[12345:]


async def test_ragged_chunk_boundaries_decrypt_correctly(
    mega_dl, mc, tmp_path, monkeypatch
):
    """The counter must not be rebuilt per chunk from a floored offset. Both
    splits of the same body must produce the same plaintext."""
    plain = bytes((i * 11 + 5) % 256 for i in range(300_000))
    body = _ciphertext(mc, plain)
    item = _item(name="x.bin", size=len(plain))

    ragged_cdn = _CDN(body)
    helper, _ = _helper(mega_dl, tmp_path, monkeypatch)
    ragged_dest = str(tmp_path / "ragged.bin")
    await helper._download_file(ragged_cdn, item, None, ragged_dest)

    aligned_cdn = _CDN(body, aligned=True)
    helper, _ = _helper(mega_dl, tmp_path, monkeypatch)
    aligned_dest = str(tmp_path / "aligned.bin")
    await helper._download_file(aligned_cdn, item, None, aligned_dest)

    assert Path(ragged_dest).read_bytes() == plain
    assert Path(aligned_dest).read_bytes() == plain


def test_spans_cover_the_file_without_gaps_or_overlap(mega_dl, monkeypatch):
    """Any gap is a hole of zero bytes; any overlap is two connections writing
    the same offsets."""
    for size in (1, 4095, 8 * 1024 * 1024, 40 * 1024 * 1024 + 7, 1 << 30):
        spans = mega_dl.MegaDownloadHelper(None)._spans(size)
        assert spans[0][0] == 0
        assert spans[-1][1] == size - 1
        for (_, prev_end), (next_start, _) in zip(spans, spans[1:]):
            assert next_start == prev_end + 1
            assert next_start % 16 == 0


async def test_zero_byte_file_is_created_without_http(mega_dl, tmp_path, monkeypatch):
    """A zero-byte file must be created on disk without issuing range requests."""
    helper, _ = _helper(mega_dl, tmp_path, monkeypatch)
    dest = str(tmp_path / "empty.txt")
    item = _item(name="empty.txt", size=0)
    assert await helper._download_file(None, item, None, dest) is True
    assert Path(dest).read_bytes() == b""


def test_small_files_are_not_split(mega_dl):
    assert mega_dl.MegaDownloadHelper(None)._spans(2 * 1024 * 1024) == [
        (0, 2 * 1024 * 1024 - 1)
    ]


def test_proxied_url_format(mega_dl):
    """CDN URL passed as ?url= query param, testing default and dynamic Config values."""
    cdn = "https://cdn.mega.co.nz/dl/aB-cD?x=1"

    # 1. Default fallback (proxy 1-5)
    mega_dl.Config.MEGA_PROXY_URL = ""
    url = mega_dl._proxied_url(cdn, 0)
    assert url.startswith("https://proxy-1.vianstefani754.workers.dev/?url=")
    assert "%3A%2F%2F" in url
    url_2 = mega_dl._proxied_url(cdn, 1)
    assert url_2.startswith("https://proxy-2.vianstefani754.workers.dev/?url=")

    # 2. Multi-proxy string in Config
    mega_dl.Config.MEGA_PROXY_URL = "https://p1.com, https://p2.com"
    assert mega_dl._proxied_url(cdn, 0).startswith("https://p1.com/?url=")
    assert mega_dl._proxied_url(cdn, 1).startswith("https://p2.com/?url=")

    # Reset
    mega_dl.Config.MEGA_PROXY_URL = ""


def test_proxy_pool_fetches_from_gateway(mega_dl, monkeypatch):
    """The pool is fetched from the gateway (de-duplicated); a gateway failure
    falls back to Config.MEGA_PROXY_URL, then to the hardcoded defaults."""
    proxy_pool = sys.modules["bot.helper.ext_utils.proxy_pool"]

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "success": True,
                "data": [
                    "https://g1.workers.dev",
                    "https://g2.workers.dev",
                    "https://g1.workers.dev",  # duplicate -> dropped
                ],
            }

    monkeypatch.setattr(proxy_pool, "_requests_get", lambda *a, **k: _Resp())
    fetched = proxy_pool.refresh_proxy_pool_sync(force=True)
    assert fetched == ["https://g1.workers.dev", "https://g2.workers.dev"]
    # Hot path serves the cached pool without touching the network again.
    assert proxy_pool.get_proxy_pool() == fetched

    # Gateway down -> fall back to Config.MEGA_PROXY_URL.
    def _boom(*a, **k):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(proxy_pool, "_requests_get", _boom)
    proxy_pool.reset_proxy_pool()
    proxy_pool.Config.MEGA_PROXY_URL = "https://cfg1.dev https://cfg2.dev"
    assert proxy_pool.refresh_proxy_pool_sync(force=True) == [
        "https://cfg1.dev",
        "https://cfg2.dev",
    ]

    # No config either -> hardcoded defaults.
    proxy_pool.reset_proxy_pool()
    proxy_pool.Config.MEGA_PROXY_URL = ""
    assert proxy_pool.refresh_proxy_pool_sync(force=True) == proxy_pool._DEFAULT_PROXIES


async def test_cancellation_stops_writing(mega_dl, mc, tmp_path, monkeypatch):
    """Cancel has to take effect mid-transfer, not after the file finishes."""
    plain = bytes(range(256)) * 8192
    cdn = _CDN(_ciphertext(mc, plain))
    helper, listener = _helper(mega_dl, tmp_path, monkeypatch)
    listener.is_cancelled = True

    dest = Path(tmp_path / "c.bin")
    dest.write_bytes(b"\0" * len(plain))

    await helper._segment(cdn, "u", str(dest), AES_KEY, NONCE, 0, len(plain) - 1, [0])

    assert helper.processed_bytes == 0
