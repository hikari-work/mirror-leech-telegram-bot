"""Bunkr album/file resolver via piyann gateway API.

Album scraping is fast (single request). Individual file download-URL
resolution is deferred: the handler returns ``bunkr_lazy: True`` in the
result dict and each ``contents`` entry carries the *file page URL* rather
than the signed CDN link.  ``DirectListener.download`` resolves them
just-in-time via ``bunkr_resolve_download`` so the task appears instantly.
"""

from asyncio import Semaphore, gather, sleep as asleep
from os import path as ospath
from random import uniform
from time import sleep
from urllib.parse import urlparse

from aiohttp import ClientSession as AioSession, ClientTimeout, TCPConnector
from requests import Session

from .._common import (
    LOGGER,
    DirectDownloadLinkException,
    gateway_headers,
    gateway_url,
)
from ..registry import register

BUNKR_HOST = "bunkr.cr"
BUNKR_ATTEMPTS = 3
BUNKR_MAX_RETRY_DELAY = 30
# An album is resolved one file at a time, and a 458-file album used to ask for
# all 458 at once -- a session per file, every connection opened in the same
# tick. The gateway answered that burst with 5xx and HTML error pages, and the
# container ran out of sockets before most requests left it, which is the
# "ClientConnectorError on every file" a large album reported.
BUNKR_MAX_CONCURRENCY = 8

# "Come back later" statuses, as opposed to a file that is actually gone.
BUNKR_RETRY_STATUSES = (408, 425, 429)
BUNKR_RATE_LIMIT_HINTS = ("rate limit", "rate-limit", "too many request", "slow down")

BUNKR_DOMAINS = (
    "bunkr.cr",
    "bunkr.si",
    "bunkr.sk",
    "bunkr.ac",
    "bunkr.is",
    "bunkr.to",
    "bunkr.la",
    "bunkr.su",
    "bunkr.ru",
    "bunkr.ph",
    "bunkr.fi",
    "bunkr.cat",
    "bunkr.black",
    "bunkr.red",
    "bunkr.media",
    "bunkr.site",
    "bunkr.ws",
    "bunkrr.su",
)


def is_bunkr_link(url):
    """Match bunkr domains (exact or subdomain)."""
    domain = (urlparse(url).hostname or "").lower()
    return any(domain == d or domain.endswith(f".{d}") for d in BUNKR_DOMAINS)


def bunkr_retryable_status(status):
    """A gateway hiccup or a rate limit; not a deleted file."""
    return status >= 500 or status in BUNKR_RETRY_STATUSES


def bunkr_rate_limited(reason):
    """The gateway also reports a rate limit as HTTP 200 + success: false."""
    text = str(reason).lower()
    return any(hint in text for hint in BUNKR_RATE_LIMIT_HINTS)


def bunkr_retry_delay(attempt, retry_after=None):
    """Seconds to wait before attempt+1, jittered.

    The jitter is the point: an album resolves in lockstep, so a fixed sleep
    only reschedules the same burst.
    """
    if retry_after:
        try:
            return min(float(retry_after), BUNKR_MAX_RETRY_DELAY) + uniform(0, 1.5)
        except (TypeError, ValueError):
            pass
    return min(2**attempt, BUNKR_MAX_RETRY_DELAY) + uniform(0, 1.5)


def _parse_size(size_str):
    """Best-effort parse of human-readable size like '3.98 MB' to bytes."""
    if not size_str:
        return 0
    parts = size_str.strip().split()
    if len(parts) != 2:
        return 0
    try:
        num = float(parts[0])
    except ValueError:
        return 0
    unit = parts[1].upper()
    multipliers = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}
    return int(num * multipliers.get(unit, 1))


# ── sync helpers (used by the @register handler) ────────────────────

def _gateway_get(session, endpoint, query):
    """Read one bunkr gateway endpoint -> (response_dict, error_reason, retryable).

    A dead session or a 5xx is worth another attempt; an album that no longer
    exists is not. Both endpoints answer in the same envelope.
    """
    try:
        resp = session.get(
            gateway_url(endpoint),
            params={"q": query},
            headers=gateway_headers(),
            timeout=60,
        )
    except Exception as exc:
        return None, exc.__class__.__name__, True

    try:
        data = resp.json()
    except Exception:
        return None, f"HTTP {resp.status_code} (non-JSON)", resp.status_code >= 500

    if not data.get("success"):
        reason = data.get("error") or f"HTTP {resp.status_code}"
        return None, reason, resp.status_code >= 500

    return data, None, False


def _scrape_album(session, album_url):
    """Scrape album page -> (response_dict, error_reason, retryable)."""
    data, reason, retryable = _gateway_get(session, "/api/v1/scrape/bunkr", album_url)
    if not data:
        return None, reason, retryable

    if not data.get("files"):
        return None, "album has no files", False

    return data, None, False


def _resolve_file(session, file_url):
    """Resolve single file -> (response_dict, error_reason, retryable)."""
    data, reason, retryable = _gateway_get(
        session, "/api/v1/scrape/bunkr/download", file_url
    )
    if not data:
        return None, reason, retryable

    if not (data.get("download_url") or "").startswith("http"):
        return None, "no download URL in response", False

    return data, None, False


def _fetch_with_retries(fetch, failed, exhausted):
    """Call *fetch* until it answers, or until the retryable attempts run out.

    *failed* names what went wrong in the log line each attempt writes;
    *exhausted* is what the user is told once the attempts are spent.
    """
    reason = "unknown error"
    for attempt in range(1, BUNKR_ATTEMPTS + 1):
        data, reason, retryable = fetch()
        if data:
            return data
        LOGGER.info(f"Bunkr: {failed} ({reason})")
        if not retryable:
            raise DirectDownloadLinkException(f"ERROR: {reason}")
        if attempt < BUNKR_ATTEMPTS:
            sleep(3)
    raise DirectDownloadLinkException(f"ERROR: {exhausted} ({reason})")


# ── async resolver (called by DirectListener at download time) ──────

async def _resolve_once(session, file_url):
    """One resolve attempt -> (result, reason, retryable, retry_after).

    *result* is the ``(download_url, filename, file_size)`` triple on success.
    Under load the gateway answers with an HTML error page rather than JSON, so
    the body is parsed with ``content_type=None`` and a decode failure is read
    off the status instead of raising past the classification.
    """
    try:
        async with session.get(
            gateway_url("/api/v1/scrape/bunkr/download"),
            params={"q": file_url},
            headers=gateway_headers(),
        ) as resp:
            retry_after = resp.headers.get("Retry-After")
            try:
                data = await resp.json(content_type=None)
            except Exception:
                return (
                    None,
                    f"HTTP {resp.status} (non-JSON)",
                    bunkr_retryable_status(resp.status),
                    retry_after,
                )
            status = resp.status
    except Exception as exc:
        return None, exc.__class__.__name__, True, None

    if not isinstance(data, dict):
        return (
            None,
            f"HTTP {status} (unexpected body)",
            bunkr_retryable_status(status),
            retry_after,
        )

    if not data.get("success"):
        reason = data.get("error") or f"HTTP {status}"
        # No reason at all is the gateway shedding load, not a missing file.
        retryable = (
            bunkr_retryable_status(status)
            or bunkr_rate_limited(reason)
            or not data.get("error")
        )
        return None, reason, retryable, retry_after

    dl_url = data.get("download_url") or ""
    if not dl_url.startswith("http"):
        return None, "no download URL in response", False, retry_after

    return (
        (dl_url, data.get("filename", ""), data.get("file_size", 0)),
        None,
        False,
        retry_after,
    )


async def _resolve_with_retries(session, file_url):
    """Resolve one file over *session*, backing off on the answers worth it."""
    for attempt in range(1, BUNKR_ATTEMPTS + 1):
        result, reason, retryable, retry_after = await _resolve_once(session, file_url)
        if result:
            return result
        LOGGER.info(
            f"Bunkr async resolve {file_url}: {reason} [{attempt}/{BUNKR_ATTEMPTS}]"
        )
        if not retryable or attempt == BUNKR_ATTEMPTS:
            break
        await asleep(bunkr_retry_delay(attempt, retry_after))
    return None, "", 0


async def bunkr_resolve_download(file_url, session=None):
    """Async resolve a single bunkr file_url -> signed CDN download_url.

    Returns (download_url, filename, file_size) on success,
    or (None, "", 0) on failure.  Pass *session* to reuse one connection pool
    across an album; without it a private session is opened for this call.
    """
    if session is not None:
        return await _resolve_with_retries(session, file_url)
    async with AioSession(timeout=ClientTimeout(total=60)) as own:
        return await _resolve_with_retries(own, file_url)


async def bunkr_resolve_many(file_urls, limit=BUNKR_MAX_CONCURRENCY):
    """Resolve *file_urls* over one connection pool, ``limit`` at a time.

    Returns a list of ``(download_url, filename, file_size)`` triples, in the
    order asked for, with ``(None, "", 0)`` where a file could not be resolved.
    """
    urls = list(file_urls)
    if not urls:
        return []

    limit = max(1, min(limit, len(urls)))
    gate = Semaphore(limit)

    async with AioSession(
        timeout=ClientTimeout(total=60), connector=TCPConnector(limit=limit)
    ) as session:

        async def one(file_url):
            async with gate:
                return await _resolve_with_retries(session, file_url)

        return await gather(*[one(url) for url in urls])


# ── @register handler ──────────────────────────────────────────────

@register(predicate=is_bunkr_link, order=43)
def bunkr(url):
    """Bunkr handler -- album URLs return lazy-resolve contents,
    single file URLs resolve immediately."""

    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    is_album = path.startswith("/a/")

    with Session() as session:
        if is_album:
            return _handle_album(session, url)
        return _handle_single(session, url)


def _handle_album(session, url):
    """Scrape album (single request), return contents with unresolved file URLs.

    Each content entry carries the bunkr file page URL.  DirectListener
    resolves them just-in-time via ``bunkr_resolve_download``."""
    album = _fetch_with_retries(
        lambda: _scrape_album(session, url),
        "album scrape failed",
        "Bunkr album could not be scraped",
    )

    title = (album.get("album_title") or "").strip() or album.get("album_id", "bunkr")
    contents = []
    total_size = 0

    for entry in album["files"]:
        file_url = entry.get("file_url", "")
        if not file_url:
            continue

        filename = (
            entry.get("name")
            or ospath.basename(urlparse(file_url).path)
            or "unknown"
        )
        size = entry.get("size_bytes") or _parse_size(entry.get("size", ""))
        total_size += size

        contents.append({
            "path": "",
            "filename": filename,
            "url": file_url,
        })

    if not contents:
        raise DirectDownloadLinkException(
            "ERROR: No files found in the Bunkr album"
        )

    return {
        "contents": contents,
        "title": title,
        "total_size": total_size,
        "bunkr_lazy": True,
    }


def _handle_single(session, url):
    """Resolve a single Bunkr file URL (immediate, not lazy)."""
    data = _fetch_with_retries(
        lambda: _resolve_file(session, url),
        "single file resolve failed",
        "Bunkr file could not be resolved",
    )

    filename = data.get("filename") or ospath.basename(urlparse(url).path) or "bunkr"
    size = data.get("file_size") or 0

    return {
        "contents": [{"path": "", "filename": filename, "url": data["download_url"]}],
        "title": ospath.splitext(filename)[0],
        "total_size": size,
    }
