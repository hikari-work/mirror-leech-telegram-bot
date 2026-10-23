"""DoodStream (Playmogo) resolver - page to direct MP4 through the gateway.

DoodStream serves a plain MP4 rather than a playlist: the gateway walks the
embed page, the ``/pass_md5`` hop and the CDN, and answers with the final URL
plus the header set the CDN insists on. That header set is not optional -- a
request without the embed page as ``Referer`` is answered with a 302 -- so it
travels with every descriptor this module returns.

Retry classification mirrors vidara's: a gateway hiccup or a rate limit is worth
another attempt, a removed video is not. The old handler scraped the page
itself and looked for a ``window.open`` token; DoodStream's player no longer
mints links that way, which is what the gateway endpoint replaced.
"""

from __future__ import annotations

from collections.abc import Callable
from os import path as ospath
from random import uniform
from time import sleep
from typing import Any
from urllib.parse import urlparse

from requests import Session

from .._common import (
    LOGGER,
    DirectDownloadLinkException,
    gateway_headers,
    gateway_url,
    header_lines,
    safe_name,
    user_agent,
)
from ..registry import register

DOODSTREAM_HOST = "playmogo.com"
DOODSTREAM_ATTEMPTS = 3
DOODSTREAM_MAX_RETRY_DELAY = 30

# Every mirror DoodStream has answered on. All of them except playmogo.com
# redirect there, so the gateway accepts any of them and falls back to the
# canonical host for one it does not know. The retired aliases stay listed so
# a link from one still reaches this handler instead of nothing.
DOODSTREAM_DOMAINS = (
    "playmogo.com",
    "dood.watch",
    "doodstream.com",
    "dood.to",
    "dood.so",
    "dood.cx",
    "dood.la",
    "dood.ws",
    "dood.sh",
    "doodstream.co",
    "dood.pm",
    "dood.wf",
    "dood.re",
    "dood.video",
    "dooood.com",
    "dood.yt",
    "doods.yt",
    "dood.stream",
    "doods.pro",
    "ds2play.com",
    "d0o0d.com",
    "ds2video.com",
    "do0od.com",
    "d000d.com",
)

# "Come back later" statuses, and the throttling the gateway reports inside a
# 200. Mirrors vidara's classification, which exists for the same reason: a
# bulk resolves in lockstep, so a fixed retry only reschedules the burst.
DOODSTREAM_RETRY_STATUSES = (408, 425, 429)
DOODSTREAM_RATE_LIMIT_HINTS = (
    "rate limit",
    "rate-limit",
    "too many request",
    "slow down",
)

# (response, reason, retryable, retry_after)
ScrapeAnswer = tuple[dict[str, Any] | None, str | None, bool, str | None]


def is_doodstream_link(url: str) -> bool:
    """Match the host itself or a subdomain of it, so a lookalike like
    notplaymogo.com is not mistaken for the real thing."""
    domain = (urlparse(url).hostname or "").lower()
    return any(domain == x or domain.endswith(f".{x}") for x in DOODSTREAM_DOMAINS)


def doodstream_retryable_status(status: int) -> bool:
    """A gateway hiccup or a rate limit; not a removed video."""
    return status >= 500 or status in DOODSTREAM_RETRY_STATUSES


def doodstream_rate_limited(reason: str) -> bool:
    """The gateway also reports a rate limit as HTTP 200 + success: false."""
    text = str(reason).lower()
    return any(hint in text for hint in DOODSTREAM_RATE_LIMIT_HINTS)


def doodstream_retry_delay(attempt: int, retry_after: str | None = None) -> float:
    """Seconds to wait before attempt+1, jittered.

    The jitter is the point: a bulk resolves in lockstep, so a fixed sleep only
    reschedules the same burst.
    """
    if retry_after:
        try:
            return min(float(retry_after), DOODSTREAM_MAX_RETRY_DELAY) + uniform(0, 1.5)
        except (TypeError, ValueError):
            pass
    return min(2**attempt, DOODSTREAM_MAX_RETRY_DELAY) + uniform(0, 1.5)


def doodstream_targets(url: str) -> list[str]:
    """The spellings of *url* worth trying, the one the user sent first.

    A retired alias only bounces through a redirect and a dead one answers
    nothing, so the canonical host is tried second.
    """
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if not hostname or hostname == DOODSTREAM_HOST:
        return [url]
    canonical = parsed._replace(netloc=DOODSTREAM_HOST).geturl()
    return [url] if canonical == url else [url, canonical]


def doodstream_read(
    session: Session, target: str, verify: bool = True, nocache: bool = False
) -> ScrapeAnswer:
    """Read the gateway endpoint for one page and classify what came back.

    ``verify`` makes the gateway range-probe the CDN, which is also where the
    size this module reports comes from. Returns the retry hint the gateway sent
    as ``Retry-After``, when it sent one.

    ``nocache`` asks the gateway to skip its stored answer. The gateway keeps a
    failed response for a short while, which is longer than this module's own
    retry window: without it, every attempt would be served the same stored
    failure and a transient hiccup would read as a dead page. Only a retry sets
    it, so a first attempt still takes the cached answer.
    """
    params = {"q": target, "verify": "true" if verify else "false"}
    if nocache:
        params["nocache"] = "1"
    try:
        resp = session.get(
            gateway_url("/api/v1/scrape/doodstream"),
            params=params,
            headers=gateway_headers(),
            timeout=90,
        )
    except Exception as e:
        return None, e.__class__.__name__, True, None

    resp_headers = getattr(resp, "headers", None)
    retry_after = resp_headers.get("Retry-After") if resp_headers else None
    retryable = doodstream_retryable_status(resp.status_code)

    try:
        response = resp.json()
    except Exception:
        return (
            None,
            f"HTTP {resp.status_code} (non-JSON response)",
            retryable,
            retry_after,
        )

    if not response.get("success"):
        reason = response.get("error") or f"HTTP {resp.status_code}"
        return None, reason, retryable or doodstream_rate_limited(reason), retry_after
    if not str(response.get("direct_url") or "").startswith("http"):
        return None, "no direct MP4 link in response", False, None
    return response, None, False, None


def doodstream_scrape_targets(
    session: Session, targets: list[str], verify: bool = True, nocache: bool = False
) -> ScrapeAnswer:
    """Scrape each spelling, stopping at the first that answers.

    When none of them resolved, the reason and retry hint are the last one's,
    which is what decides the next attempt. ``nocache`` reaches the gateway, as
    it does in :func:`doodstream_read`.
    """
    reason, retryable, retry_after = "unknown error", False, None
    for target in targets:
        response, reason, retryable, retry_after = doodstream_read(
            session, target, verify, nocache
        )
        if response:
            return response, None, retryable, retry_after
        LOGGER.info(f"DoodStream: {target} rejected by the API ({reason})")
    return None, reason, retryable, retry_after


def doodstream_retry(
    scrape: Callable[[bool], ScrapeAnswer], label: str = ""
) -> dict[str, Any]:
    """Call *scrape* until it answers, backing off on the answers worth retrying.

    *scrape* takes the ``nocache`` flag the gateway understands: a retry is
    asking again because the stored answer was the problem, so only a retry
    bypasses it. Raises with the last reason once the attempts are spent, or as
    soon as one comes back final.
    """
    reason = "unknown error"
    for attempt in range(1, DOODSTREAM_ATTEMPTS + 1):
        response, reason, retryable, retry_after = scrape(attempt > 1)
        if response:
            return response
        if not retryable or attempt == DOODSTREAM_ATTEMPTS:
            break
        delay = doodstream_retry_delay(attempt, retry_after)
        LOGGER.info(
            f"DoodStream: {reason}, retrying{label} in {delay:.1f}s "
            f"[{attempt}/{DOODSTREAM_ATTEMPTS}]"
        )
        sleep(delay)
    raise DirectDownloadLinkException(f"ERROR: {reason}")


def doodstream_headers(download_info: dict[str, Any]) -> dict[str, str]:
    """Header map the CDN expects.

    The gateway supplies the exact set it probed with -- ``Referer`` above all,
    since the CDN answers 302 without it. The defaults only fill a field the
    gateway left out.
    """
    headers = download_info.get("headers")
    if not isinstance(headers, dict):
        headers = {}
    headers = {str(key): str(value) for key, value in headers.items() if value}

    headers.setdefault(
        "User-Agent",
        str(download_info.get("user_agent") or "").strip() or user_agent,
    )
    headers.setdefault(
        "Referer",
        str(download_info.get("referer") or "").strip()
        or f"https://{DOODSTREAM_HOST}/",
    )
    headers["Accept-Encoding"] = "identity"
    return headers


def doodstream_filename(response: dict[str, Any]) -> str:
    """The file name, as one path component with the MP4 suffix.

    The API already normalises it; ``safe_name`` is here because the value still
    comes off a page title and a title decides a file name, not where the file
    lands.
    """
    code = str(response.get("file_code") or "").strip()
    name = safe_name(
        response.get("filename") or response.get("title"), code or "doodstream"
    )
    stem, ext = ospath.splitext(name)
    if ext.lower() == ".mp4":
        return name
    return f"{stem or code or 'doodstream'}.mp4"


def doodstream_stem(response: dict[str, Any]) -> str:
    """The title without its container suffix."""
    return ospath.splitext(doodstream_filename(response))[0]


@register(predicate=is_doodstream_link, order=32)
def doodstream(url: str) -> dict[str, Any]:
    """Resolve a DoodStream page to the direct MP4 aria2 fetches.

    The descriptor is the direct-download shape rather than ``ytdlp``: the
    endpoint answers one MP4 with the headers it must be fetched with, which is
    what aria2 already does for vidoy.
    """
    targets = doodstream_targets(url)

    with Session() as session:
        response = doodstream_retry(
            lambda nocache=False: doodstream_scrape_targets(
                session, targets, nocache=nocache
            )
        )

    headers = doodstream_headers(response.get("download") or {})
    filename = doodstream_filename(response)
    try:
        total_size = int(response.get("size_bytes") or 0)
    except (TypeError, ValueError):
        total_size = 0

    return {
        "contents": [
            {
                "path": "",
                "filename": filename,
                "url": response["direct_url"],
            }
        ],
        "title": doodstream_stem(response),
        "total_size": total_size,
        "header": header_lines(headers),
    }
