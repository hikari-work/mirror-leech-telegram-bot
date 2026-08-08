"""Semprot.com thread scraper via api.piyann.me.

Scrapes all external links from a semprot.com / senang.top XenForo thread.
Uses gateway API endpoint: https://api.piyann.me/api/v1/scrape/semprot
"""

from urllib.parse import urlparse, urlunparse

from ...ext_utils.exceptions import DirectDownloadLinkException

GATEWAY_API = "https://api.piyann.me/api/v1/scrape/semprot"


def _normalize_url(url: str) -> str:
    """Replace senang.top domain with semprot.com."""
    parsed = urlparse(url)
    if parsed.netloc:
        netloc = parsed.netloc.replace("senang.top", "semprot.com")
        return urlunparse(parsed._replace(netloc=netloc))
    return url.replace("senang.top", "semprot.com")


def scrape_thread(url: str):
    """Scrape all pages of a semprot thread via gateway API. Returns (title, sorted links)."""
    from requests import get as req_get

    target_url = _normalize_url(url)
    try:
        r = req_get(GATEWAY_API, params={"q": target_url}, timeout=60)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        raise DirectDownloadLinkException(f"ERROR: semprot gateway request failed: {e}") from e

    if not data.get("success"):
        error_msg = data.get("error") or "Unknown gateway error"
        raise DirectDownloadLinkException(f"ERROR: semprot scrape failed: {error_msg}")

    links = data.get("links") or []
    # Strip thread title or return simple thread url label if not provided by gateway
    title = target_url.rstrip("/").split("/")[-1]
    return title, sorted(links)
