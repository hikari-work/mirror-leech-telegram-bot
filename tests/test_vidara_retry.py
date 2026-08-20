"""Tests for Vidara's retry classification.

The resolver used to call anything short of HTTP 5xx final, so the 429 a bulk
provokes was reported as "rejected by the API" -- indistinguishable from a video
that was actually removed, and counted as a failed link. These tests pin which
answers are worth another attempt, and that the backoff a retry waits out grows
and carries jitter (a batch resolves in lockstep, so a fixed sleep only
reschedules the same burst).
"""

from __future__ import annotations

import pytest

# The ``vidara`` fixture lives in conftest.py: test_vidara_folder.py loads the
# same module the same way.


class _Resp:
    def __init__(self, status, payload, headers=None, text="", url=""):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = text
        self.url = url

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Session:
    """Replays canned responses; the last one repeats once the queue runs dry."""

    def __init__(self, *responses):
        self._q = list(responses)
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        item = self._q.pop(0) if len(self._q) > 1 else self._q[0]
        if isinstance(item, Exception):
            raise item
        return item

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ok_body(master="https://cdn.test/master.m3u8"):
    return {"success": True, "master_url": master, "title": "clip.mp4"}


def _hls_resp(url="https://cdn.test/master.m3u8"):
    """What the HLS probe in the registered handler expects to fetch."""
    return _Resp(
        200,
        None,
        headers={"Content-Type": "application/vnd.apple.mpegurl"},
        text="#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\n",
        url=url,
    )


# ── which answers are retryable ──────────────────────────────────────


def test_rate_limit_is_retryable(vidara):
    session = _Session(_Resp(429, {"success": False, "error": "slow down"},
                             {"Retry-After": "4"}))

    response, reason, retryable, retry_after = vidara.vidara_scrape(
        session, "https://vidara.to/abc"
    )

    assert response is None
    assert retryable is True
    assert retry_after == "4"


def test_server_error_is_retryable(vidara):
    session = _Session(_Resp(503, None))

    _, reason, retryable, _ = vidara.vidara_scrape(session, "https://vidara.to/abc")

    assert retryable is True
    assert "503" in reason


def test_rate_limit_wrapped_in_a_200_is_retryable(vidara):
    """The gateway also reports throttling as HTTP 200 + success: false."""
    session = _Session(_Resp(200, {"success": False, "error": "Rate limit hit"}))

    _, reason, retryable, _ = vidara.vidara_scrape(session, "https://vidara.to/abc")

    assert retryable is True
    assert reason == "Rate limit hit"


def test_removed_video_is_final(vidara):
    session = _Session(_Resp(404, {"success": False, "error": "video not found"}))

    _, reason, retryable, _ = vidara.vidara_scrape(session, "https://vidara.to/abc")

    assert retryable is False
    assert reason == "video not found"


def test_connection_error_is_retryable(vidara):
    session = _Session(OSError("connection reset"))

    _, reason, retryable, _ = vidara.vidara_scrape(session, "https://vidara.to/abc")

    assert retryable is True
    assert reason == "OSError"


# ── backoff ──────────────────────────────────────────────────────────


def test_retry_delay_backs_off_and_stays_capped(vidara):
    assert 2 <= vidara.vidara_retry_delay(1) <= 3.5
    assert 4 <= vidara.vidara_retry_delay(2) <= 5.5
    assert vidara.vidara_retry_delay(20) <= vidara.VIDARA_MAX_RETRY_DELAY + 1.5


def test_retry_delay_honours_retry_after(vidara):
    assert 6 <= vidara.vidara_retry_delay(1, "6") <= 7.5
    # a header the gateway sent as a date, or as nonsense, must not raise
    assert 2 <= vidara.vidara_retry_delay(1, "Wed, 21 Oct 2015 07:28:00 GMT") <= 3.5


def test_retry_delay_is_jittered(vidara):
    delays = {round(vidara.vidara_retry_delay(1), 6) for _ in range(20)}
    assert len(delays) > 1


# ── the retry loop ───────────────────────────────────────────────────


def test_resolve_retries_a_rate_limit_then_succeeds(vidara, monkeypatch):
    session = _Session(_Resp(429, {"success": False, "error": "slow down"}),
                       _Resp(200, _ok_body()))
    monkeypatch.setattr(vidara, "Session", lambda: session)
    monkeypatch.setattr(vidara, "sleep", lambda seconds: None)

    name, link, headers = vidara.vidara_resolve("https://vidara.to/abc")

    assert name == "clip"
    assert link == "https://cdn.test/master.m3u8"
    assert headers["Referer"]
    assert session.calls == 2


def test_resolve_gives_up_after_its_attempts(vidara, monkeypatch):
    session = _Session(_Resp(429, {"success": False, "error": "slow down"}))
    monkeypatch.setattr(vidara, "Session", lambda: session)
    monkeypatch.setattr(vidara, "sleep", lambda seconds: None)

    with pytest.raises(vidara.DirectDownloadLinkException, match="slow down"):
        vidara.vidara_resolve("https://vidara.to/abc")

    assert session.calls == vidara.VIDARA_ATTEMPTS


def test_resolve_does_not_retry_a_dead_link(vidara, monkeypatch):
    session = _Session(_Resp(404, {"success": False, "error": "video not found"}))
    monkeypatch.setattr(vidara, "Session", lambda: session)
    monkeypatch.setattr(vidara, "sleep", lambda seconds: None)

    with pytest.raises(vidara.DirectDownloadLinkException, match="video not found"):
        vidara.vidara_resolve("https://vidara.to/abc")

    assert session.calls == 1


def test_handler_retries_a_rate_limit_then_returns_an_ytdlp_link(vidara, monkeypatch):
    session = _Session(_Resp(429, {"success": False, "error": "too many requests"}),
                       _Resp(200, _ok_body()),
                       _hls_resp())
    monkeypatch.setattr(vidara, "Session", lambda: session)
    monkeypatch.setattr(vidara, "sleep", lambda seconds: None)

    resolved = vidara.vidara("https://vidara.to/abc")

    assert resolved["ytdlp"] is True
    assert resolved["link"] == "https://cdn.test/master.m3u8"
    # two scrapes plus the playlist probe
    assert session.calls == 3


def test_handler_does_not_retry_a_dead_link(vidara, monkeypatch):
    session = _Session(_Resp(410, {"success": False, "error": "video removed"}))
    monkeypatch.setattr(vidara, "Session", lambda: session)
    monkeypatch.setattr(vidara, "sleep", lambda seconds: None)

    with pytest.raises(vidara.DirectDownloadLinkException, match="video removed"):
        vidara.vidara("https://vidara.to/abc")

    assert session.calls == 1
