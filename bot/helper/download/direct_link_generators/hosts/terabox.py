"""Terabox direct link handler: scrape the gateway, fetch the CDN directly.

The bot keeps its own Terabox session (``Config.TERABOX_NDUS``) so no relay is
needed: the listing is scraped with that session, and each file's ``dlink`` is
fetched straight from the CDN with the ``ndus`` cookie header. When no cookie is
configured the gateway mints the links with its own pool and hands back the
session in ``download.cookie``, which the same header reuses.
"""

from os import path as ospath
from random import uniform
from time import sleep

from requests import Session

from .._common import (
    LOGGER,
    Config,
    DirectDownloadLinkException,
    gateway_headers,
    gateway_url,
    user_agent,
)
from ..registry import register

_TERABOX_DOMAINS = (
    "terabox.com",
    "nephobox.com",
    "4funbox.com",
    "mirrobox.com",
    "momerybox.com",
    "teraboxapp.com",
    "1024tera.com",
    "terabox.app",
    "gibibox.com",
    "goaibox.com",
    "terasharelink.com",
    "teraboxlink.com",
    "freeterabox.com",
    "1024terabox.com",
    "teraboxshare.com",
    "terafileshare.com",
    "terabox.club",
)

_API_TIMEOUT = 120
"""Seconds to wait on one scrape, well past the gateway's own 85s budget.

At the old 30s the request timed out before the gateway had finished, so a
single slow scrape was fatal and the share read as dead.
"""

TERABOX_ATTEMPTS = 3
TERABOX_MAX_RETRY_DELAY = 30

# "Come back later" statuses. A bulk used to give up on the first 429 and report
# the share as dead, which is how half a batch could fail while every link in it
# was fine.
TERABOX_RETRY_STATUSES = (408, 425, 429)


def terabox_retryable_status(status: int) -> bool:
    """A gateway hiccup or a rate limit; not a share that is really gone."""
    return status >= 500 or status in TERABOX_RETRY_STATUSES


def terabox_retry_delay(attempt: int, retry_after: str | None = None) -> float:
    """Seconds to wait before attempt+1, jittered.

    The jitter is the point: a bulk scrapes in lockstep, so a fixed sleep only
    reschedules the same burst.
    """
    if retry_after:
        try:
            return min(float(retry_after), TERABOX_MAX_RETRY_DELAY) + uniform(0, 1.5)
        except (TypeError, ValueError):
            pass
    return min(2**attempt, TERABOX_MAX_RETRY_DELAY) + uniform(0, 1.5)


def _build_header(download_info: dict, header_cookie: str = "") -> list[str]:
    """The CDN request header as a list of "Key: value" lines, cookie first.

    Cookie precedence: the gateway's ``X-Terabox-Cookies`` response header, then
    the ``download`` block, then ``Config.TERABOX_NDUS``. The first is what the
    dlinks were actually minted with, so it wins; the last is only a fallback for
    a gateway that reported no session at all.
    """
    cookie = (header_cookie or download_info.get("cookie") or "").strip()
    if not cookie:
        name = (download_info.get("cookie_name") or "").strip()
        value = (download_info.get("cookie_value") or "").strip()
        if name and value:
            cookie = f"{name}={value}"
    if not cookie and (ndus := (Config.TERABOX_NDUS or "").strip()):
        cookie = f"ndus={ndus}"

    header = [
        f"User-Agent: {(download_info.get('user_agent') or '').strip() or user_agent}",
        "Referer: https://www.terabox.com/",
    ]
    if cookie:
        header.insert(0, f"Cookie: {cookie}")
    return header


def terabox_read(
    session: Session, params: dict, nocache: bool = False
) -> tuple[dict | None, str, str | None, bool, str | None]:
    """Read the gateway scrape and classify what came back.

    Returns ``(response, header_cookie, reason, retryable, retry_after)``. A
    gateway hiccup or a rate limit is worth another attempt, a share that is
    really gone is not; ``retry_after`` is the gateway's own hint when it sent
    one, and ``header_cookie`` is the session the dlinks were minted with, which
    the gateway reports as ``X-Terabox-Cookies``.

    ``nocache`` asks the gateway to skip its stored answer. The gateway keeps a
    failed response for a while, which is longer than this module's own retry
    window: without this, every attempt would be served the same stored failure
    and a transient hiccup would read as a dead share. Only a retry sets it, so
    the first attempt still takes the cached answer.
    """
    if nocache:
        params = {**params, "nocache": "1"}
    try:
        resp = session.get(
            gateway_url("/api/v1/scrape/terabox"),
            params=params,
            headers=gateway_headers(),
            timeout=_API_TIMEOUT,
        )
    except Exception as e:
        return None, "", e.__class__.__name__, True, None

    header_cookie = ""
    resp_headers = getattr(resp, "headers", None)
    if resp_headers:
        header_cookie = resp_headers.get("X-Terabox-Cookies", "")
    retry_after = resp_headers.get("Retry-After") if resp_headers else None
    retryable = terabox_retryable_status(resp.status_code)

    try:
        response = resp.json()
    except Exception:
        reason = f"HTTP {resp.status_code} (non-JSON response)"
        return None, "", reason, retryable, retry_after

    if not response.get("success"):
        reason = response.get("error") or f"HTTP {resp.status_code}"
        return None, "", reason, retryable, retry_after

    warning = response.get("warning", "")
    files = [f for f in (response.get("files") or []) if f.get("dlink")]
    if not files:
        reason = warning.split(";")[0] if warning else "No download links found"
        # A listed share with no dlinks in it is a final answer -- but the
        # status still decides: the gateway answers a throttled scrape with 429,
        # and that is worth another attempt however the body reads.
        return None, "", reason, retryable, retry_after

    return response, header_cookie, None, False, None


def _collect_contents(files: list[dict]) -> tuple[list[dict], int]:
    """Flatten the gateway's file list into ``contents`` entries and a total size."""
    contents: list[dict] = []
    total_size = 0
    for file in files:
        contents.append(
            {
                "path": ospath.dirname(file.get("path", "")).lstrip("/"),
                "filename": file.get("name", ""),
                "url": file["dlink"],
            }
        )
        try:
            total_size += int(file.get("size") or 0)
        except (TypeError, ValueError):
            pass
    return contents, total_size


@register(*_TERABOX_DOMAINS, order=35)
def terabox(url: str) -> str | tuple[str, list[str]] | dict:
    if "/file/" in url:
        return url

    params = {"q": url}
    if ndus := (Config.TERABOX_NDUS or "").strip():
        params["ndus"] = ndus

    reason, retryable, retry_after = "unknown error", False, None
    header_cookie = ""
    response = None
    with Session() as session:
        for attempt in range(1, TERABOX_ATTEMPTS + 1):
            response, header_cookie, reason, retryable, retry_after = terabox_read(
                session, params, nocache=attempt > 1
            )
            if response:
                break
            if not retryable or attempt == TERABOX_ATTEMPTS:
                break
            delay = terabox_retry_delay(attempt, retry_after)
            LOGGER.info(
                f"Terabox: {reason}, retrying in {delay:.1f}s "
                f"[{attempt}/{TERABOX_ATTEMPTS}]"
            )
            sleep(delay)

    if not response:
        raise DirectDownloadLinkException(f"ERROR: {reason}")

    warning = response.get("warning", "")
    files = [f for f in (response.get("files") or []) if f.get("dlink")]

    # The gateway's own session wins: it is the one the dlinks were minted with.
    # Config.TERABOX_NDUS is only the fallback when the header is absent.
    header = _build_header(response.get("download") or {}, header_cookie)
    if not any(line.startswith("Cookie:") for line in header):
        LOGGER.warning(f"Terabox: no cookie for CDN; dlinks may 403. {warning}")

    contents, total_size = _collect_contents(files)
    details = {
        "contents": contents,
        "title": response.get("title", ""),
        "total_size": total_size,
    }

    if not details["title"]:
        details["title"] = details["contents"][0]["filename"]

    LOGGER.info(f"Terabox: listed {len(files)} file(s), direct CDN download")

    if len(details["contents"]) == 1:
        return details["contents"][0]["url"], header

    details["header"] = "\n".join(header)
    return details
