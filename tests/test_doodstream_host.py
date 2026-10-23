"""DoodStream host: gateway read, retry classification, and the aria2 descriptor.

The endpoint answers one MP4 with the header set it must be fetched with, so the
registered handler has to hand the direct downloader a ``contents`` entry and
the ``Referer`` that keeps the CDN from answering 302. These tests pin that
shape, the mirror fallback, and which answers are worth another attempt -- the
same classification vidara uses, because a bulk resolves in lockstep and a rate
limit that is mistaken for a dead link counts a healthy file as failed.
"""

from __future__ import annotations

from urllib.parse import urlparse

import pytest


class _Resp:
    def __init__(self, status, payload, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


class _Session:
    """Replays canned responses; the last one repeats once the queue runs dry."""

    def __init__(self, *responses):
        self._q = list(responses)
        self.calls = 0
        self.urls: list[str] = []
        self.params: list[dict] = []

    def get(self, url, **kwargs):
        self.calls += 1
        self.urls.append(url)
        self.params.append(kwargs.get("params") or {})
        item = self._q.pop(0) if len(self._q) > 1 else self._q[0]
        if isinstance(item, Exception):
            raise item
        return item

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ok_body(direct_url="https://cdn.test/v/abc123?token=t&expiry=1"):
    return {
        "success": True,
        "file_code": "abc123",
        "host": "playmogo.com",
        "page_url": "https://playmogo.com/e/abc123",
        "title": "clip",
        "filename": "clip.mp4",
        "direct_url": direct_url,
        "verified": True,
        "content_type": "video/mp4",
        "size_bytes": 1234,
        "download": {
            "headers": {
                "Referer": "https://playmogo.com/e/abc123",
                "User-Agent": "UA2",
            },
            "referer": "https://playmogo.com/e/abc123",
            "user_agent": "UA2",
        },
    }


# ── matching ─────────────────────────────────────────────────────────


def test_matches_every_mirror_and_a_subdomain(doodstream):
    for domain in doodstream.DOODSTREAM_DOMAINS:
        assert doodstream.is_doodstream_link(f"https://{domain}/e/abc123")
        assert doodstream.is_doodstream_link(f"https://www.{domain}/e/abc123")


def test_lookalike_is_not_doodstream(doodstream):
    assert not doodstream.is_doodstream_link("https://notplaymogo.com/e/abc123")
    assert not doodstream.is_doodstream_link("https://playmogo.com.evil.test/e/x")
    assert not doodstream.is_doodstream_link("")


# ── the descriptor the direct downloader consumes ────────────────────


def test_handler_returns_the_mp4_with_the_cdn_headers(doodstream, monkeypatch):
    session = _Session(_Resp(200, _ok_body()))
    monkeypatch.setattr(doodstream, "Session", lambda: session)

    resolved = doodstream.doodstream("https://dood.to/e/abc123")

    assert resolved["contents"] == [
        {
            "path": "",
            "filename": "clip.mp4",
            "url": "https://cdn.test/v/abc123?token=t&expiry=1",
        }
    ]
    assert resolved["title"] == "clip"
    assert resolved["total_size"] == 1234
    assert "Referer: https://playmogo.com/e/abc123" in resolved["header"]
    assert "User-Agent: UA2" in resolved["header"]
    # the gateway is asked to range-probe, which is where the size comes from
    assert session.params[0]["verify"] == "true"
    assert session.params[0]["q"] == "https://dood.to/e/abc123"


def test_handler_tries_the_canonical_host_after_a_retired_mirror(
    doodstream, monkeypatch
):
    session = _Session(
        _Resp(404, {"success": False, "error": "video not found"}),
        _Resp(200, _ok_body()),
    )
    monkeypatch.setattr(doodstream, "Session", lambda: session)

    resolved = doodstream.doodstream("https://dood.watch/e/abc123")

    assert resolved["contents"][0]["url"].startswith("https://cdn.test/")
    assert len(session.params) == 2
    assert "dood.watch" in session.params[0]["q"]
    assert session.params[1]["q"] == "https://playmogo.com/e/abc123"


def test_the_canonical_host_is_not_fetched_twice(doodstream):
    assert doodstream.doodstream_targets("https://playmogo.com/e/abc") == [
        "https://playmogo.com/e/abc"
    ]


def test_filename_never_becomes_a_path(doodstream):
    body = _ok_body()
    body["filename"] = "../../evil.mp4"

    assert doodstream.doodstream_filename(body) == "evil.mp4"


def test_filename_falls_back_to_the_file_code_with_an_mp4_suffix(doodstream):
    body = {"file_code": "abc123", "title": ""}

    assert doodstream.doodstream_filename(body) == "abc123.mp4"


def test_headers_fill_in_what_the_gateway_left_out(doodstream):
    headers = doodstream.doodstream_headers({})

    assert headers["User-Agent"]
    assert headers["Referer"] == "https://playmogo.com/"
    assert headers["Accept-Encoding"] == "identity"


# ── which answers are retryable ──────────────────────────────────────


def test_rate_limit_is_retryable(doodstream):
    session = _Session(
        _Resp(429, {"success": False, "error": "slow down"}, {"Retry-After": "4"})
    )

    response, _, retryable, retry_after = doodstream.doodstream_read(
        session, "https://dood.to/e/abc"
    )

    assert response is None
    assert retryable is True
    assert retry_after == "4"


def test_server_error_is_retryable(doodstream):
    session = _Session(_Resp(503, None))

    _, reason, retryable, _ = doodstream.doodstream_read(
        session, "https://dood.to/e/abc"
    )

    assert retryable is True
    assert "503" in reason


def test_rate_limit_wrapped_in_a_200_is_retryable(doodstream):
    """The gateway also reports throttling as HTTP 200 + success: false."""
    session = _Session(_Resp(200, {"success": False, "error": "Rate limit hit"}))

    _, reason, retryable, _ = doodstream.doodstream_read(
        session, "https://dood.to/e/abc"
    )

    assert retryable is True
    assert reason == "Rate limit hit"


def test_removed_video_is_final(doodstream):
    session = _Session(_Resp(404, {"success": False, "error": "video not found"}))

    _, reason, retryable, _ = doodstream.doodstream_read(
        session, "https://dood.to/e/abc"
    )

    assert retryable is False
    assert reason == "video not found"


def test_connection_error_is_retryable(doodstream):
    session = _Session(OSError("connection reset"))

    _, reason, retryable, _ = doodstream.doodstream_read(
        session, "https://dood.to/e/abc"
    )

    assert retryable is True
    assert reason == "OSError"


# ── backoff ──────────────────────────────────────────────────────────


def test_retry_delay_backs_off_and_stays_capped(doodstream):
    assert 2 <= doodstream.doodstream_retry_delay(1) <= 3.5
    assert 4 <= doodstream.doodstream_retry_delay(2) <= 5.5
    assert (
        doodstream.doodstream_retry_delay(20)
        <= doodstream.DOODSTREAM_MAX_RETRY_DELAY + 1.5
    )


def test_retry_delay_honours_retry_after(doodstream):
    assert 6 <= doodstream.doodstream_retry_delay(1, "6") <= 7.5
    # a header the gateway sent as a date, or as nonsense, must not raise
    assert (
        2
        <= doodstream.doodstream_retry_delay(1, "Wed, 21 Oct 2015 07:28:00 GMT")
        <= 3.5
    )


def test_retry_delay_is_jittered(doodstream):
    delays = {round(doodstream.doodstream_retry_delay(1), 6) for _ in range(20)}
    assert len(delays) > 1


# ── the retry loop ───────────────────────────────────────────────────


def test_handler_retries_a_rate_limit_then_succeeds(doodstream, monkeypatch):
    session = _Session(
        _Resp(429, {"success": False, "error": "slow down"}),
        _Resp(200, _ok_body()),
    )
    monkeypatch.setattr(doodstream, "Session", lambda: session)
    monkeypatch.setattr(doodstream, "sleep", lambda seconds: None)

    resolved = doodstream.doodstream("https://playmogo.com/e/abc123")

    assert resolved["contents"][0]["filename"] == "clip.mp4"
    assert session.calls == 2


def test_handler_gives_up_after_its_attempts(doodstream, monkeypatch):
    session = _Session(_Resp(429, {"success": False, "error": "slow down"}))
    monkeypatch.setattr(doodstream, "Session", lambda: session)
    monkeypatch.setattr(doodstream, "sleep", lambda seconds: None)

    with pytest.raises(doodstream.DirectDownloadLinkException, match="slow down"):
        doodstream.doodstream("https://playmogo.com/e/abc123")

    assert session.calls == doodstream.DOODSTREAM_ATTEMPTS


def test_handler_does_not_retry_a_dead_link(doodstream, monkeypatch):
    session = _Session(_Resp(404, {"success": False, "error": "video not found"}))
    monkeypatch.setattr(doodstream, "Session", lambda: session)
    monkeypatch.setattr(doodstream, "sleep", lambda seconds: None)

    with pytest.raises(doodstream.DirectDownloadLinkException, match="video not found"):
        doodstream.doodstream("https://playmogo.com/e/abc123")

    # the canonical host is the only target for a playmogo link, so one read
    assert session.calls == 1


def test_only_a_retry_bypasses_the_gateway_cache(doodstream, monkeypatch):
    """A stored failure outlives this module's retry window.

    Without ``nocache`` on the retries, every attempt is served the same stored
    failure and a one-off hiccup reads as a dead page.
    """
    session = _Session(
        _Resp(429, {"success": False, "error": "slow down"}),
        _Resp(200, _ok_body()),
    )
    monkeypatch.setattr(doodstream, "Session", lambda: session)
    monkeypatch.setattr(doodstream, "sleep", lambda seconds: None)

    doodstream.doodstream("https://playmogo.com/e/abc123")

    assert "nocache" not in session.params[0]
    assert session.params[1]["nocache"] == "1"


def test_gateway_endpoint_is_the_doodstream_one(doodstream, monkeypatch):
    session = _Session(_Resp(200, _ok_body()))
    monkeypatch.setattr(doodstream, "Session", lambda: session)

    doodstream.doodstream("https://playmogo.com/e/abc123")

    assert urlparse(session.urls[0]).path == "/api/v1/scrape/doodstream"
    assert session.params[0]["q"] == "https://playmogo.com/e/abc123"
