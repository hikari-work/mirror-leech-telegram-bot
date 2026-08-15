"""PornHub scraper via api.piyann.me gateway.

Endpoints used:
  /api/v1/scrape/pornhub/video    – single video detail + download URLs
  /api/v1/scrape/pornhub/channel  – list videos from a channel
  /api/v1/scrape/pornhub/model    – list videos from a model
  /api/v1/scrape/pornhub/pornstar – list videos from a pornstar
"""

from __future__ import annotations

from re import match as re_match, sub as re_sub
from urllib.parse import urlparse, parse_qs

from ....core.config_manager import Config
from ...ext_utils.exceptions import DirectDownloadLinkException


_API_BASE = "/api/v1/scrape/pornhub"

_QUALITY_SCORE = {"2160": 5, "1440": 4, "1080": 3, "720": 2, "480": 1, "360": 0, "240": -1}

_PH_HEADERS = {
    "Referer": "https://www.pornhub.com/",
    "Origin": "https://www.pornhub.com",
}


def is_pornhub_link(url: str) -> bool:
    """Return True if *url* is a PornHub URL."""
    try:
        return "pornhub" in (urlparse(url).hostname or "").lower()
    except Exception:
        return False


def _gateway_get(endpoint: str, params: dict, timeout: int = 120) -> dict:
    from requests import get as req_get

    gateway_base = (getattr(Config, "GATEWAY_URL", "") or "https://api.piyann.me").rstrip("/")
    url = f"{gateway_base}{endpoint}"
    headers = {}
    if token := getattr(Config, "GATEWAY_TOKEN", ""):
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = req_get(url, params=params, headers=headers, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        raise DirectDownloadLinkException(f"ERROR: PornHub gateway request failed: {exc}") from exc
    if not data.get("success"):
        err = data.get("error") or "Unknown gateway error"
        raise DirectDownloadLinkException(f"ERROR: PornHub scrape failed: {err}")
    return data


def scrape_video(viewkey: str) -> dict:
    """Fetch single video detail. Returns the ``data`` dict from the API."""
    resp = _gateway_get(f"{_API_BASE}/video", {"viewkey": viewkey})
    detail = resp.get("data")
    if not detail:
        raise DirectDownloadLinkException("ERROR: No video data returned by gateway")
    return detail


def scrape_list(list_type: str, name: str, *, all_pages: bool = True, max_pages: int = 0) -> list[dict]:
    """Fetch video listing for channel / model / pornstar.

    Returns a flat list of video dicts (each has ``video_key``, ``title``, etc.).
    """
    if list_type not in ("channel", "model", "pornstar"):
        raise DirectDownloadLinkException(f"ERROR: Invalid PornHub list type: {list_type}")
    params: dict = {"name": name}
    if all_pages:
        params["all"] = "true"
        if max_pages > 0:
            params["max_pages"] = max_pages
    resp = _gateway_get(f"{_API_BASE}/{list_type}", params, timeout=300)
    return resp.get("videos") or []


def _pick_best(downloads: list[dict]) -> dict:
    """Select highest quality download entry."""
    best = downloads[0]
    best_score = -99
    for dl in downloads:
        qual = dl.get("quality", "0").replace("p", "")
        score = _QUALITY_SCORE.get(qual, -50)
        if score > best_score:
            best_score = score
            best = dl
    return best


def _sanitize_filename(title: str) -> str:
    """Create a filesystem-safe filename from a video title."""
    name = re_sub(r'[<>:"/\\|?*]', "", title).strip()
    return name[:200] if name else "pornhub_video"


def _resolve_video_entry(viewkey: str) -> dict | None:
    """Resolve one video into a download entry dict.

    Returns ``{"url": ..., "name": ..., "is_hls": bool}`` or None on failure.
    """
    detail = scrape_video(viewkey)
    downloads = detail.get("downloads") or []
    if not downloads:
        return None
    best = _pick_best(downloads)
    title = detail.get("title") or viewkey
    safe_name = _sanitize_filename(title)
    url = best["url"]
    fmt = (best.get("format") or "").lower()
    is_hls = fmt == "hls" or url.endswith(".m3u8") or ".m3u8" in url.split("?")[0]
    return {
        "url": url,
        "name": f"{safe_name}.mp4",
        "is_hls": is_hls,
    }


def resolve_single_video(viewkey: str) -> dict:
    """Scrape a single video and return a pornhub link dict.

    Returns ``{"pornhub": True, "title": ..., "videos": [...], "headers": ...}``
    so the downloader handles it as one task.
    """
    entry = _resolve_video_entry(viewkey)
    if not entry:
        raise DirectDownloadLinkException("ERROR: No download URLs found for this video")
    return {
        "pornhub": True,
        "title": entry["name"].rsplit(".", 1)[0],
        "videos": [entry],
        "headers": _PH_HEADERS,
    }


def resolve_listing(list_type: str, name: str) -> dict:
    """Scrape a channel/model/pornstar and return a pornhub link dict.

    All resolved videos are packed into a single dict so the downloader
    handles them as one task (like Mega handles folder downloads).
    """
    from logging import getLogger
    LOGGER = getLogger(__name__)

    videos = scrape_list(list_type, name)
    if not videos:
        raise DirectDownloadLinkException(
            f"ERROR: No videos found for {list_type} '{name}'"
        )

    entries = []
    seen_keys = set()
    seen_names = set()
    for vid in videos:
        vk = vid.get("video_key", "")
        if not vk or vk in seen_keys:
            continue
        seen_keys.add(vk)
        try:
            entry = _resolve_video_entry(vk)
            if entry:
                fname = entry["name"]
                if fname in seen_names:
                    base, ext = fname.rsplit(".", 1) if "." in fname else (fname, "mp4")
                    fname = f"{base}_{vk}.{ext}"
                    entry["name"] = fname
                seen_names.add(fname)
                entries.append(entry)
        except Exception as exc:
            LOGGER.warning(f"PornHub: skipping {vk}: {exc}")

    if not entries:
        raise DirectDownloadLinkException(
            f"ERROR: Failed to resolve any videos for {list_type} '{name}'"
        )

    folder = f"PH_{list_type}_{name}"
    return {
        "pornhub": True,
        "title": folder,
        "videos": entries,
        "headers": _PH_HEADERS,
    }


def parse_pornhub_url(url: str) -> tuple[str, str] | None:
    """Detect PornHub URL type and extract identifier.

    Returns ``(type, identifier)`` or ``None`` if not a recognised PornHub URL.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if "pornhub" not in host:
        return None
    path = parsed.path.rstrip("/")
    if "view_video" in path:
        qs = parse_qs(parsed.query)
        viewkey = qs.get("viewkey", [""])[0]
        if not viewkey:
            viewkey_match = re_match(r".*/view_video\.php\?viewkey=([a-zA-Z0-9]+)", url)
            if viewkey_match:
                viewkey = viewkey_match.group(1)
        if viewkey:
            return ("video", viewkey)
        return None
    segments = [s for s in path.split("/") if s]
    if len(segments) >= 2:
        kind = segments[0].lower()
        name = segments[1]
        if kind == "channels":
            return ("channel", name)
        if kind == "model":
            return ("model", name)
        if kind == "pornstar":
            return ("pornstar", name)
    return None
