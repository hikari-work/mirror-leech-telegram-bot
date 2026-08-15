"""ImgBB / ibb.co direct-image resolver via piyann API."""

from requests import get as http_get

from .._common import DirectDownloadLinkException
from ..registry import register

IMGBB_API = "https://api.piyann.me/api/v1/scrape/imgbb"

IMGBB_DOMAINS = ("ibb.co.com", "ibb.co", "imgbb.com")


def is_imgbb_link(url: str) -> bool:
    from urllib.parse import urlparse

    hostname = urlparse(url).hostname or ""
    return any(hostname == d or hostname.endswith(f".{d}") for d in IMGBB_DOMAINS)


@register(*IMGBB_DOMAINS, order=42)
def imgbb(url: str) -> str:
    """Resolve an ImgBB page URL to a direct download link."""
    try:
        resp = http_get(IMGBB_API, params={"q": url}, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise DirectDownloadLinkException(f"ERROR: ImgBB API request failed: {exc}") from exc

    if not data.get("success"):
        error = data.get("error", "Unknown error")
        raise DirectDownloadLinkException(f"ERROR: ImgBB resolve failed: {error}")

    result = data.get("data", {})
    download_url = result.get("download_url")
    if not download_url:
        raise DirectDownloadLinkException("ERROR: ImgBB API returned no download URL")

    return download_url
