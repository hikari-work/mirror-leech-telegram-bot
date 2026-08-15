"""Semprot.com thread scraper via api.piyann.me.

Endpoint: https://api.piyann.me/api/v1/scrape/semprot
Params: q=<url>&pageList=<spec>&filter=<host>
Response: {success, links, total_pages}
"""

from urllib.parse import urlparse, urlunparse

from aiohttp import ClientSession, ClientTimeout

from ....core.config_manager import Config
from ...ext_utils.exceptions import DirectDownloadLinkException


def _normalize_url(url: str) -> str:
    """Replace senang.top domain with semprot.com."""
    parsed = urlparse(url)
    if parsed.netloc:
        netloc = parsed.netloc.replace("senang.top", "semprot.com")
        return urlunparse(parsed._replace(netloc=netloc))
    return url.replace("senang.top", "semprot.com")


def _gateway_headers():
    headers = {"accept": "application/json"}
    if token := getattr(Config, "GATEWAY_TOKEN", ""):
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _gateway_url():
    base = (getattr(Config, "GATEWAY_URL", "") or "https://api.piyann.me").rstrip("/")
    return f"{base}/api/v1/scrape/semprot"


async def scrape_pages(url: str, page_list: str = "1", filter_host: str = ""):
    """Scrape semprot thread pages in one async request.

    Args:
        url: Thread URL.
        page_list: Page spec sent as ``pageList`` param
            (e.g. ``"1"``, ``"1-10"``, ``"1,5,7"``).
        filter_host: Optional host filter sent as ``filter`` param
            (e.g. ``"vidara.to"``).  Server-side filtering.

    Returns:
        (title, links, total_pages)
    """
    target_url = _normalize_url(url)
    params = {"q": target_url, "pageList": page_list}
    if filter_host:
        params["filter"] = filter_host
    try:
        timeout = ClientTimeout(total=600)
        async with ClientSession(timeout=timeout) as session:
            async with session.get(
                _gateway_url(),
                params=params,
                headers=_gateway_headers(),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
    except Exception as e:
        raise DirectDownloadLinkException(f"ERROR: semprot gateway request failed: {e}") from e

    if not data.get("success"):
        error_msg = data.get("error") or "Unknown gateway error"
        raise DirectDownloadLinkException(f"ERROR: semprot scrape failed: {error_msg}")

    links = data.get("links") or []
    total_pages = data.get("total_pages") or 1
    title = target_url.rstrip("/").split("/")[-1]
    return title, sorted(links), total_pages
