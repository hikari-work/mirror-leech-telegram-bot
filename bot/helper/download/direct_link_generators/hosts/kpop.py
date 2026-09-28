"""kpop.xxx direct link handler.

A video page resolves to one MP4, a playlist page to a few hundred. Both come
from the gateway as a signed CDN URL (``?verify=``), and a playlist is walked
page by page there -- ``total_pages`` says how many, and the videos come back in
one response. Like ShyAV, the listing is fetched here and each video is resolved
when its turn comes, which is what ``lazy=`` on the result dict asks for.
"""

from urllib.parse import urlparse

from requests import Session

from .._common import (
    LOGGER,
    DirectDownloadLinkException,
    gateway_headers,
    gateway_url,
    safe_stem,
    unique_stem,
)
from ..registry import register

KPOP_HOST = "kpop.xxx"
KPOP_DOMAINS = ("kpop.xxx",)
# A playlist is a few hundred videos and the gateway has already walked every
# page of it by the time it answers; this is a ceiling on what one link can add
# to a task, and what it drops is logged rather than passed off as the listing.
KPOP_PLAYLIST_MAX = 2000

def is_kpop(url):
    """Match the host itself or a subdomain of it, so a lookalike like
    notkpop.xxx is not mistaken for the real thing."""
    domain = (urlparse(url).hostname or "").lower()
    return any(domain == x or domain.endswith(f".{x}") for x in KPOP_DOMAINS)

def is_kpop_playlist(url):
    """A ``/playlists/<id>/…`` page, as opposed to a single video."""
    if not isinstance(url, str):
        return False
    parts = [part for part in urlparse(url).path.split("/") if part]
    return bool(parts) and parts[0].lower() == "playlists"

def kpop_read(session, endpoint, url, timeout=120):
    """Read one gateway endpoint and classify what came back.

    Returns ``(response, reason, retryable)``; a gateway hiccup is worth another
    attempt, a removed video is not.
    """
    try:
        resp = session.get(
            gateway_url(f"/api/v1/scrape/kpop{endpoint}"),
            params={"url": url},
            headers=gateway_headers(),
            timeout=timeout,
        )
    except Exception as exc:
        return None, exc.__class__.__name__, True

    try:
        response = resp.json()
    except Exception:
        reason = f"HTTP {resp.status_code} (non-JSON response)"
        return None, reason, resp.status_code >= 500

    if not response.get("success"):
        reason = response.get("error") or f"HTTP {resp.status_code}"
        return None, reason, resp.status_code >= 500
    return response, None, False

def kpop_playlist_entries(videos, taken):
    """The videos of a playlist listing, as ``contents`` entries.

    ``url`` is the video page, not the CDN link: the gateway mints a signed URL
    that expires, and a 224-video playlist would reach the tail well after the
    front of it stopped being valid.
    """
    entries = []
    for video in videos or []:
        if not isinstance(video, dict):
            continue
        page_url = (video.get("url") or "").strip()
        if not page_url:
            continue
        stem = safe_stem(video.get("title"), video.get("video_id") or "kpop")
        stem = unique_stem(stem, taken, video.get("video_id") or "")
        entries.append(
            {"path": "", "filename": f"{stem}.mp4", "url": page_url}
        )
    return entries

def kpop_resolve_entry(session, page_url):
    """Resolve one video page to its signed MP4, or "" when it is gone."""
    response, reason, _retryable = kpop_read(session, "", page_url)
    if not response:
        LOGGER.info(f"kpop: {page_url} rejected by the API ({reason})")
        return ""
    link = (response.get("data") or {}).get("video_url") or ""
    return link if link.startswith("http") else ""


async def kpop_resolve_download(page_url):
    """Async resolve one video page to its signed MP4.

    ``DirectListener`` calls this with the entry URL the handler handed it.
    """
    from ....util.bot_utils import sync_to_async

    return await sync_to_async(kpop_lookup_sync, page_url)


def kpop_lookup_sync(page_url):
    """The blocking half of :func:`kpop_resolve_download`, for a thread."""
    with Session() as session:
        return kpop_resolve_entry(session, page_url)


@register(predicate=is_kpop, order=91)
def kpop(url):
    """kpop.xxx handler: a video is one entry, a playlist is one per video."""
    playlist = is_kpop_playlist(url)
    endpoint = "/playlist" if playlist else ""

    with Session() as session:
        response, reason, _retryable = kpop_read(session, endpoint, url, 300)

    if not response:
        raise DirectDownloadLinkException(f"ERROR: {reason}")

    detail = response.get("data") or {}

    if not playlist:
        link = detail.get("video_url") or ""
        if not link.startswith("http"):
            raise DirectDownloadLinkException("ERROR: no video URL in response")
        stem = safe_stem(detail.get("title"), detail.get("video_id") or "kpop")
        return {
            "contents": [{"path": "", "filename": f"{stem}.mp4", "url": link}],
            "title": stem,
            "total_size": 0,
        }

    title = safe_stem(detail.get("title"), "kpop playlist")
    videos = detail.get("videos") or []
    entries = kpop_playlist_entries(videos[:KPOP_PLAYLIST_MAX], set())
    if not entries:
        raise DirectDownloadLinkException("ERROR: no videos in this kpop playlist")

    total = detail.get("total_videos") or len(entries)
    if len(videos) > KPOP_PLAYLIST_MAX:
        LOGGER.info(
            f"kpop: playlist {title} lists {total} videos; taking the first "
            f"{KPOP_PLAYLIST_MAX}"
        )
    else:
        LOGGER.info(f"kpop: playlist {title} lists {len(entries)} video(s)")
    return {
        "contents": entries,
        "title": title,
        "total_size": 0,
        "lazy": "kpop",
    }
