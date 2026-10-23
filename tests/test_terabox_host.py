"""Terabox host: the gateway read, the retry it never had, the CDN cookie.

The handler used to make a single call with a 30 s timeout while the gateway's
own budget is 85 s: one slow or transient scrape was fatal, and the share was
reported as dead though a manual request a moment later worked. These tests pin
the retry, the timeout that outlasts the gateway, and the cookie precedence the
dlinks depend on -- the session the listing was minted under wins over the one
configured locally, because only the former can fetch those dlinks.
"""

from __future__ import annotations

from urllib.parse import urlparse

import pytest

# The ``terabox`` fixture lives in conftest.py: the real package pulls in lxml
# and the rest of the generator chain, so the leaf module is loaded with its
# package stubbed.


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
        self.timeouts: list[int] = []

    def get(self, url, **kwargs):
        self.calls += 1
        self.urls.append(url)
        self.params.append(kwargs.get("params") or {})
        self.timeouts.append(kwargs.get("timeout"))
        item = self._q.pop(0) if len(self._q) > 1 else self._q[0]
        if isinstance(item, Exception):
            raise item
        return item

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _listing(files=2, cookie=None):
    """A gateway success with ``files`` dlinks, and a session when asked."""
    download = {"cookie": cookie} if cookie else None
    return {
        "success": True,
        "title": "share",
        "files": [
            {"name": f"f{i}.mp4", "dlink": f"https://cdn.test/{i}", "size": 10}
            for i in range(files)
        ],
        "download": download,
    }


GATEWAY_PATH = "/api/v1/scrape/terabox"


# ── the retry ────────────────────────────────────────────────────────


def test_a_transient_failure_is_retried(terabox, monkeypatch):
    session = _Session(_Resp(503, None), _Resp(200, _listing()))
    monkeypatch.setattr(terabox, "Session", lambda: session)
    monkeypatch.setattr(terabox, "sleep", lambda seconds: None)

    result = terabox.terabox("https://terabox.com/s/abc")

    assert session.calls == 2
    assert result["contents"][0]["filename"] == "f0.mp4"


def test_a_rate_limit_is_retried(terabox, monkeypatch):
    session = _Session(
        _Resp(429, {"success": False, "error": "slow down"},
              {"Retry-After": "3"}),
        _Resp(200, _listing()),
    )
    monkeypatch.setattr(terabox, "Session", lambda: session)
    monkeypatch.setattr(terabox, "sleep", lambda seconds: None)

    result = terabox.terabox("https://terabox.com/s/abc")

    assert session.calls == 2
    assert result["title"] == "share"


def test_a_share_with_no_links_is_final(terabox, monkeypatch):
    """The gateway listed it and found nothing; the next scrape finds nothing too."""
    session = _Session(_Resp(200, {"success": True, "title": "empty", "files": [],
                                   "warning": "no links; try later"}))
    monkeypatch.setattr(terabox, "Session", lambda: session)
    monkeypatch.setattr(terabox, "sleep", lambda seconds: None)

    with pytest.raises(terabox.DirectDownloadLinkException, match="no links"):
        terabox.terabox("https://terabox.com/s/abc")

    assert session.calls == 1


def test_the_status_decides_even_an_empty_listing(terabox, monkeypatch):
    """A throttled scrape carries no files either, and is worth another attempt."""
    session = _Session(
        _Resp(429, {"success": True, "files": [], "warning": "no links"}),
        _Resp(200, _listing()),
    )
    monkeypatch.setattr(terabox, "Session", lambda: session)
    monkeypatch.setattr(terabox, "sleep", lambda seconds: None)

    result = terabox.terabox("https://terabox.com/s/abc")

    assert session.calls == 2
    assert result["contents"][0]["filename"] == "f0.mp4"


def test_gives_up_after_its_attempts(terabox, monkeypatch):
    session = _Session(_Resp(503, None))
    monkeypatch.setattr(terabox, "Session", lambda: session)
    monkeypatch.setattr(terabox, "sleep", lambda seconds: None)

    with pytest.raises(terabox.DirectDownloadLinkException, match="503"):
        terabox.terabox("https://terabox.com/s/abc")

    assert session.calls == terabox.TERABOX_ATTEMPTS


def test_a_removed_share_is_not_retried(terabox, monkeypatch):
    session = _Session(_Resp(404, {"success": False, "error": "share not found"}))
    monkeypatch.setattr(terabox, "Session", lambda: session)
    monkeypatch.setattr(terabox, "sleep", lambda seconds: None)

    with pytest.raises(terabox.DirectDownloadLinkException, match="share not found"):
        terabox.terabox("https://terabox.com/s/abc")

    assert session.calls == 1


def test_only_a_retry_bypasses_the_gateway_cache(terabox, monkeypatch):
    """The gateway stores a failed answer past this module's retry window.

    Without ``nocache`` on the retries, every attempt is served the same stored
    failure -- which is how a one-off hiccup read as a dead share.
    """
    session = _Session(_Resp(503, None), _Resp(200, _listing()))
    monkeypatch.setattr(terabox, "Session", lambda: session)
    monkeypatch.setattr(terabox, "sleep", lambda seconds: None)

    terabox.terabox("https://terabox.com/s/abc")

    assert "nocache" not in session.params[0]
    assert session.params[1]["nocache"] == "1"


# ── the timeout ──────────────────────────────────────────────────────


def test_the_timeout_outlasts_the_gateway(terabox, monkeypatch):
    """The old 30 s expired before the gateway's own 85 s budget was spent."""
    session = _Session(_Resp(200, _listing()))
    monkeypatch.setattr(terabox, "Session", lambda: session)

    terabox.terabox("https://terabox.com/s/abc")

    assert session.timeouts[0] == terabox._API_TIMEOUT
    assert terabox._API_TIMEOUT >= 85


# ── the CDN cookie ───────────────────────────────────────────────────


def test_the_gateway_session_beats_the_configured_one(terabox, monkeypatch):
    """Only the session the dlinks were minted under can fetch them."""
    terabox.Config.TERABOX_NDUS = "configured"
    session = _Session(_Resp(200, _listing(cookie="ndus=minted"),
                             {"X-Terabox-Cookies": "ndus=header"}))
    monkeypatch.setattr(terabox, "Session", lambda: session)

    result = terabox.terabox("https://terabox.com/s/abc")

    assert result["header"].startswith("Cookie: ndus=header\n")


def test_the_configured_session_is_the_fallback(terabox, monkeypatch):
    terabox.Config.TERABOX_NDUS = "configured"
    session = _Session(_Resp(200, _listing()))
    monkeypatch.setattr(terabox, "Session", lambda: session)

    result = terabox.terabox("https://terabox.com/s/abc")

    assert result["header"].startswith("Cookie: ndus=configured\n")


def test_the_download_block_beats_the_configured_session(terabox, monkeypatch):
    terabox.Config.TERABOX_NDUS = "configured"
    session = _Session(_Resp(200, _listing(cookie="ndus=restored")))
    monkeypatch.setattr(terabox, "Session", lambda: session)

    result = terabox.terabox("https://terabox.com/s/abc")

    assert result["header"].startswith("Cookie: ndus=restored\n")


def test_no_session_anywhere_leaves_the_header_cookieless(terabox, monkeypatch):
    terabox.Config.TERABOX_NDUS = ""
    session = _Session(_Resp(200, _listing()))
    monkeypatch.setattr(terabox, "Session", lambda: session)

    result = terabox.terabox("https://terabox.com/s/abc")

    assert "Cookie:" not in result["header"]


def test_a_single_file_is_returned_bare(terabox, monkeypatch):
    """One file has no folder to lay out, so the caller gets url + header."""
    terabox.Config.TERABOX_NDUS = ""
    session = _Session(_Resp(200, _listing(files=1, cookie="ndus=x")))
    monkeypatch.setattr(terabox, "Session", lambda: session)

    url, header = terabox.terabox("https://terabox.com/s/abc")

    assert url == "https://cdn.test/0"
    assert header[0].startswith("Cookie: ndus=x")


def test_the_configured_session_reaches_the_gateway_as_a_param(terabox, monkeypatch):
    """The gateway mints the dlinks against it when it is passed."""
    terabox.Config.TERABOX_NDUS = "configured"
    session = _Session(_Resp(200, _listing()))
    monkeypatch.setattr(terabox, "Session", lambda: session)

    terabox.terabox("https://terabox.com/s/abc")

    assert urlparse(session.urls[0]).path == GATEWAY_PATH
    assert session.params[0]["ndus"] == "configured"


def test_a_direct_file_link_is_passed_through(terabox):
    """``/file/`` is already a CDN address; nothing to scrape."""
    assert terabox.terabox("https://terabox.com/file/abc") == "https://terabox.com/file/abc"


# ── backoff ──────────────────────────────────────────────────────────


def test_retry_delay_backs_off_and_stays_capped(terabox):
    assert 2 <= terabox.terabox_retry_delay(1) <= 3.5
    assert 4 <= terabox.terabox_retry_delay(2) <= 5.5
    assert terabox.terabox_retry_delay(20) <= terabox.TERABOX_MAX_RETRY_DELAY + 1.5


def test_retry_delay_honours_retry_after(terabox):
    assert 6 <= terabox.terabox_retry_delay(1, "6") <= 7.5
    # a date, or nonsense, must not raise
    assert 2 <= terabox.terabox_retry_delay(1, "Wed, 21 Oct 2015 07:28:00 GMT") <= 3.5


def test_retry_delay_is_jittered(terabox):
    delays = {round(terabox.terabox_retry_delay(1), 6) for _ in range(20)}
    assert len(delays) > 1
