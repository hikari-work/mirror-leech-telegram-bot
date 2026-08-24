"""Tests for Bunkr's per-file resolve: retry classification and pooling.

An album is resolved one file at a time, and the resolver used to ask for every
file in the album at once -- a fresh session per file, all opened in the same
tick. A 458-file album answered that burst with 5xx and HTML error pages, and
the container ran out of sockets before most of the requests left it, which is
the "ClientConnectorError on every file" a large album reported.

These tests pin the two halves of the fix: which answers are worth another
attempt (a gateway shedding load reports it as HTTP 200 + ``success: false``
with no reason at all, which used to be treated as final), and that a bulk
resolve stays under its concurrency cap while keeping results in order.
"""

from __future__ import annotations

import asyncio

import pytest


class _Resp:
    """One canned gateway answer, usable as an async context manager."""

    def __init__(self, status, payload, headers=None, bad_json=False):
        self.status = status
        self._payload = payload
        self.headers = headers or {}
        self._bad_json = bad_json

    async def json(self, content_type=None):
        if self._bad_json:
            raise ValueError("Attempt to decode JSON with unexpected mimetype")
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    """Replays canned answers; the last one repeats once the queue runs dry.

    Also records how many resolves were ever in flight at once, which is what
    the concurrency cap is asserted against.
    """

    def __init__(self, *responses):
        self._q = list(responses)
        self.calls = 0
        self.in_flight = 0
        self.peak = 0
        self.urls = []

    def get(self, url, params=None, headers=None, **kwargs):
        self.calls += 1
        self.urls.append((params or {}).get("q"))
        item = self._q.pop(0) if len(self._q) > 1 else self._q[0]
        if isinstance(item, Exception):
            raise item
        return _Tracked(self, item)


class _Tracked:
    """Wraps a response so entering/leaving it moves the in-flight counter."""

    def __init__(self, session, resp):
        self._session = session
        self._resp = resp

    async def __aenter__(self):
        self._session.in_flight += 1
        self._session.peak = max(self._session.peak, self._session.in_flight)
        # Yield so sibling resolves get a chance to overlap; without this the
        # cap would look respected simply because nothing ever interleaved.
        await asyncio.sleep(0)
        return self._resp

    async def __aexit__(self, *exc):
        self._session.in_flight -= 1
        return False


def _ok(url="https://cdn.test/file.mp4?token=abc", name="file.mp4", size=1234):
    return {
        "success": True,
        "download_url": url,
        "filename": name,
        "file_size": size,
    }


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch, bunkr):
    """Backoff is asserted separately; keep the retry tests instant."""
    recorded = []

    async def fake_sleep(delay):
        recorded.append(delay)

    monkeypatch.setattr(bunkr, "asleep", fake_sleep)
    return recorded


# ── retry classification ────────────────────────────────────────────


async def test_success_returns_triple(bunkr):
    session = _Session(_Resp(200, _ok()))
    result = await bunkr.bunkr_resolve_download("https://bunkr.cr/v/x", session)
    assert result == ("https://cdn.test/file.mp4?token=abc", "file.mp4", 1234)
    assert session.calls == 1


async def test_server_error_is_retried_then_succeeds(bunkr):
    session = _Session(_Resp(503, {"success": False}), _Resp(200, _ok()))
    result = await bunkr.bunkr_resolve_download("https://bunkr.cr/v/x", session)
    assert result[0] == "https://cdn.test/file.mp4?token=abc"
    assert session.calls == 2


async def test_html_error_page_is_retried(bunkr):
    """Under load the gateway answers 500 with an HTML body, not JSON.

    ``resp.json()`` raising used to be the only thing that got retried; it now
    has to survive being read with ``content_type=None`` as well.
    """
    session = _Session(_Resp(500, None, bad_json=True), _Resp(200, _ok()))
    result = await bunkr.bunkr_resolve_download("https://bunkr.cr/v/x", session)
    assert result[0] == "https://cdn.test/file.mp4?token=abc"
    assert session.calls == 2


async def test_reasonless_failure_is_retried(bunkr):
    """HTTP 200 + ``success: false`` + no error is the gateway shedding load.

    This is the one the old classifier called final: it broke out of the loop
    for anything under HTTP 500, so a file that would have resolved on the
    next attempt was reported as unresolvable.
    """
    session = _Session(_Resp(200, {"success": False}), _Resp(200, _ok()))
    result = await bunkr.bunkr_resolve_download("https://bunkr.cr/v/x", session)
    assert result[0] == "https://cdn.test/file.mp4?token=abc"
    assert session.calls == 2


async def test_rate_limit_at_http_200_is_retried(bunkr):
    session = _Session(
        _Resp(200, {"success": False, "error": "Rate limit exceeded"}),
        _Resp(200, _ok()),
    )
    result = await bunkr.bunkr_resolve_download("https://bunkr.cr/v/x", session)
    assert result[0] == "https://cdn.test/file.mp4?token=abc"
    assert session.calls == 2


async def test_named_error_is_final(bunkr):
    """A deleted file is the gateway's answer, not a hiccup: do not retry it."""
    session = _Session(_Resp(404, {"success": False, "error": "File not found"}))
    result = await bunkr.bunkr_resolve_download("https://bunkr.cr/v/x", session)
    assert result == (None, "", 0)
    assert session.calls == 1


async def test_connection_error_is_retried_and_gives_up(bunkr):
    session = _Session(OSError("Cannot connect to host"))
    result = await bunkr.bunkr_resolve_download("https://bunkr.cr/v/x", session)
    assert result == (None, "", 0)
    assert session.calls == bunkr.BUNKR_ATTEMPTS


async def test_non_http_download_url_is_final(bunkr):
    session = _Session(_Resp(200, {"success": True, "download_url": "/relative"}))
    result = await bunkr.bunkr_resolve_download("https://bunkr.cr/v/x", session)
    assert result == (None, "", 0)
    assert session.calls == 1


async def test_attempts_are_capped(bunkr):
    session = _Session(_Resp(503, {"success": False}))
    result = await bunkr.bunkr_resolve_download("https://bunkr.cr/v/x", session)
    assert result == (None, "", 0)
    assert session.calls == bunkr.BUNKR_ATTEMPTS


# ── backoff ─────────────────────────────────────────────────────────


def test_backoff_grows_and_carries_jitter(bunkr):
    """An album resolves in lockstep, so a fixed sleep reschedules the burst."""
    first = {bunkr.bunkr_retry_delay(1) for _ in range(20)}
    second = {bunkr.bunkr_retry_delay(2) for _ in range(20)}
    assert len(first) > 1, "delay must be jittered, not fixed"
    assert all(2 <= d <= 3.5 for d in first)
    assert all(4 <= d <= 5.5 for d in second)
    assert min(second) > min(first)


def test_backoff_honours_retry_after_and_its_cap(bunkr):
    assert all(5 <= bunkr.bunkr_retry_delay(1, "5") <= 6.5 for _ in range(10))
    capped = bunkr.bunkr_retry_delay(1, "9999")
    assert bunkr.BUNKR_MAX_RETRY_DELAY <= capped <= bunkr.BUNKR_MAX_RETRY_DELAY + 1.5


def test_garbage_retry_after_falls_back_to_backoff(bunkr):
    assert 2 <= bunkr.bunkr_retry_delay(1, "soon") <= 3.5


# ── bulk resolve ────────────────────────────────────────────────────


@pytest.fixture
def pooled(monkeypatch, bunkr):
    """Give ``bunkr_resolve_many`` a recording session instead of a real one."""

    def install(*responses):
        session = _Session(*responses)

        class _FakeAioSession:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return session

            async def __aexit__(self, *exc):
                return False

        monkeypatch.setattr(bunkr, "AioSession", _FakeAioSession)
        monkeypatch.setattr(bunkr, "TCPConnector", lambda **kwargs: None)
        return session

    return install


async def test_bulk_resolve_stays_under_the_cap(bunkr, pooled):
    session = pooled(_Resp(200, _ok()))
    urls = [f"https://bunkr.cr/v/{i}" for i in range(200)]

    results = await bunkr.bunkr_resolve_many(urls)

    assert len(results) == 200
    assert all(r[0] for r in results)
    assert session.peak <= bunkr.BUNKR_MAX_CONCURRENCY, (
        f"opened {session.peak} at once, cap is {bunkr.BUNKR_MAX_CONCURRENCY}"
    )


async def test_bulk_resolve_keeps_order_and_marks_failures(bunkr, pooled):
    # Second answer is a deleted file, so it fails without retrying and the
    # queue stays aligned with the urls.
    session = pooled(
        _Resp(200, _ok("https://cdn.test/a?t=1", "a.mp4", 1)),
        _Resp(404, {"success": False, "error": "File not found"}),
        _Resp(200, _ok("https://cdn.test/c?t=3", "c.mp4", 3)),
    )
    urls = ["https://bunkr.cr/v/a", "https://bunkr.cr/v/b", "https://bunkr.cr/v/c"]

    results = await bunkr.bunkr_resolve_many(urls, limit=1)

    assert results[0] == ("https://cdn.test/a?t=1", "a.mp4", 1)
    assert results[1] == (None, "", 0)
    assert results[2] == ("https://cdn.test/c?t=3", "c.mp4", 3)
    assert session.urls == urls


async def test_bulk_resolve_of_nothing_asks_nothing(bunkr, pooled):
    session = pooled(_Resp(200, _ok()))
    assert await bunkr.bunkr_resolve_many([]) == []
    assert session.calls == 0
