"""Terabox direct link handler: scrape the gateway, fetch the CDN directly.

The bot keeps its own Terabox session (``Config.TERABOX_NDUS``) so no relay is
needed: the listing is scraped with that session, and each file's ``dlink`` is
fetched straight from the CDN with the ``ndus`` cookie header. When no cookie is
configured the gateway mints the links with its own pool and hands back the
session in ``download.cookie``, which the same header reuses.
"""

from os import path as ospath

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

_API_TIMEOUT = 30


def _build_header(download_info, header_cookie=""):
    """The CDN request header as a list of "Key: value" lines, cookie first.

    Cookie precedence: the gateway's ``X-Terabox-Cookies`` header, then the
    ``download`` block, then ``Config.TERABOX_NDUS`` -- the header is what the
    dlinks were actually minted with, so it is always the correct one.
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


@register(*_TERABOX_DOMAINS, order=35)
def terabox(url):
    if "/file/" in url:
        return url

    api_url = gateway_url("/api/v1/scrape/terabox")
    params = {"q": url}
    if ndus := (Config.TERABOX_NDUS or "").strip():
        params["ndus"] = ndus

    with Session() as session:
        try:
            resp = session.get(
                api_url,
                params=params,
                headers=gateway_headers(),
                timeout=_API_TIMEOUT,
            )
            header_cookie = resp.headers.get("X-Terabox-Cookies", "")
            response = resp.json()
        except Exception as e:
            raise DirectDownloadLinkException(f"ERROR: {e.__class__.__name__}") from e

    if not response.get("success"):
        raise DirectDownloadLinkException(
            f"ERROR: {response.get('error', 'Unknown error')}"
        )

    warning = response.get("warning", "")
    files = [f for f in (response.get("files") or []) if f.get("dlink")]
    if not files:
        reason = warning.split(";")[0] if warning else "No download links found"
        raise DirectDownloadLinkException(f"ERROR: {reason}")

    # The gateway's own session wins: it is the one the dlinks were minted with.
    # Config.TERABOX_NDUS is only the fallback when the header is absent.
    header = _build_header(response.get("download") or {}, header_cookie)
    if not any(line.startswith("Cookie:") for line in header):
        LOGGER.warning(f"Terabox: no cookie for CDN; dlinks may 403. {warning}")

    details = {"contents": [], "title": response.get("title", ""), "total_size": 0}
    for file in files:
        details["contents"].append(
            {
                "path": ospath.dirname(file.get("path", "")).lstrip("/"),
                "filename": file.get("name", ""),
                "url": file["dlink"],
            }
        )
        try:
            details["total_size"] += int(file.get("size") or 0)
        except (TypeError, ValueError):
            pass

    if not details["title"]:
        details["title"] = details["contents"][0]["filename"]

    LOGGER.info(f"Terabox: listed {len(files)} file(s), direct CDN download")

    if len(details["contents"]) == 1:
        return details["contents"][0]["url"], header

    details["header"] = "\n".join(header)
    return details
