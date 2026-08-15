"""Bunkr album/file resolver via piyann gateway API.

Album scraping is fast (single request). Individual file download-URL
resolution is deferred: the handler returns ``bunkr_lazy: True`` in the
result dict and each ``contents`` entry carries the *file page URL* rather
than the signed CDN link.  ``DirectListener.download`` resolves them
just-in-time via ``bunkr_resolve_download`` so the task appears instantly.
"""

from asyncio import sleep as asleep
from os import path as ospath
from time import sleep
from urllib.parse import urlparse

from aiohttp import ClientSession as AioSession, ClientTimeout
from requests import Session

from .._common import (
    LOGGER,
    Config,
    DirectDownloadLinkException,
    user_agent,
)
from ..registry import register

BUNKR_HOST = "bunkr.cr"
BUNKR_ATTEMPTS = 3
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


def _gateway_headers():
    headers = {"accept": "application/json"}
    if token := getattr(Config, "GATEWAY_TOKEN", ""):
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _gateway_base():
    return (getattr(Config, "GATEWAY_URL", "") or "https://api.piyann.me").rstrip("/")


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

def _scrape_album(session, album_url):
    """Scrape album page -> (response_dict, error_reason, retryable)."""
    api = f"{_gateway_base()}/api/v1/scrape/bunkr"
    try:
        resp = session.get(
            api,
            params={"q": album_url},
            headers=_gateway_headers(),
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

    if not data.get("files"):
        return None, "album has no files", False

    return data, None, False


def _resolve_file(session, file_url):
    """Resolve single file -> (response_dict, error_reason, retryable)."""
    api = f"{_gateway_base()}/api/v1/scrape/bunkr/download"
    try:
        resp = session.get(
            api,
            params={"q": file_url},
            headers=_gateway_headers(),
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

    if not (data.get("download_url") or "").startswith("http"):
        return None, "no download URL in response", False

    return data, None, False


# ── async resolver (called by DirectListener at download time) ──────

async def bunkr_resolve_download(file_url):
    """Async resolve a single bunkr file_url -> signed CDN download_url.

    Returns (download_url, filename, file_size) on success,
    or (None, "", 0) on failure.
    """
    api = f"{_gateway_base()}/api/v1/scrape/bunkr/download"
    headers = _gateway_headers()
    timeout = ClientTimeout(total=60)

    for attempt in range(1, BUNKR_ATTEMPTS + 1):
        try:
            async with AioSession(timeout=timeout) as session:
                async with session.get(api, params={"q": file_url}, headers=headers) as resp:
                    data = await resp.json()
        except Exception as exc:
            LOGGER.info(f"Bunkr async resolve {file_url}: {exc.__class__.__name__}")
            if attempt < BUNKR_ATTEMPTS:
                await asleep(2)
            continue

        if not data.get("success"):
            reason = data.get("error") or f"HTTP {resp.status}"
            LOGGER.info(f"Bunkr async resolve {file_url}: {reason}")
            if resp.status < 500:
                break
            if attempt < BUNKR_ATTEMPTS:
                await asleep(2)
            continue

        dl_url = data.get("download_url", "")
        if dl_url.startswith("http"):
            return dl_url, data.get("filename", ""), data.get("file_size", 0)
        break

    return None, "", 0


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
    reason = "unknown error"
    for attempt in range(1, BUNKR_ATTEMPTS + 1):
        album, reason, retryable = _scrape_album(session, url)
        if album:
            break
        LOGGER.info(f"Bunkr: album scrape failed ({reason})")
        if not retryable:
            raise DirectDownloadLinkException(f"ERROR: {reason}")
        if attempt < BUNKR_ATTEMPTS:
            sleep(3)
    else:
        raise DirectDownloadLinkException(
            f"ERROR: Bunkr album could not be scraped ({reason})"
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
    reason = "unknown error"
    for attempt in range(1, BUNKR_ATTEMPTS + 1):
        data, reason, retryable = _resolve_file(session, url)
        if data:
            break
        LOGGER.info(f"Bunkr: single file resolve failed ({reason})")
        if not retryable:
            raise DirectDownloadLinkException(f"ERROR: {reason}")
        if attempt < BUNKR_ATTEMPTS:
            sleep(3)
    else:
        raise DirectDownloadLinkException(
            f"ERROR: Bunkr file could not be resolved ({reason})"
        )

    filename = data.get("filename") or ospath.basename(urlparse(url).path) or "bunkr"
    size = data.get("file_size") or 0

    return {
        "contents": [{"path": "", "filename": filename, "url": data["download_url"]}],
        "title": ospath.splitext(filename)[0],
        "total_size": size,
    }
