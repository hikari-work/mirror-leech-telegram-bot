"""ShyAV direct link handler.

A ShyAV page is a video, and a *series* is one page with an episode list behind
it -- ``/api/v1/scrape/shyav`` answers with both, the MP4 in ``video_url`` and
the episodes in ``episodes``. The MP4 is a signed CDN URL (``?verify=``), so the
listing is fetched here and each episode is resolved when its turn comes, which
is what ``lazy=`` on the result dict asks ``DirectListener`` for.
"""

from urllib.parse import parse_qs, urlencode, urlparse

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

SHYAV_HOST = "shyav.com"
SHYAV_DOMAINS = (
    "shyav.com",
    "shyav.xxx",
    "shyav.net",
    "shyav.to",
)

def is_shyav(url):
    """Match the host itself or a subdomain of it, so a lookalike like
    notshyav.com is not mistaken for the real thing."""
    domain = (urlparse(url).hostname or "").lower()
    return any(domain == x or domain.endswith(f".{x}") for x in SHYAV_DOMAINS)

def shyav_sanitize_url(url):
    """ShyAV rotates hosts; aim the retired ones at the current one."""
    parsed = urlparse(url)
    hostname = parsed.hostname.lower() if parsed.hostname else ""
    if not hostname or hostname == SHYAV_HOST:
        return url
    return parsed._replace(netloc=SHYAV_HOST).geturl()

def shyav_episode_fid(url):
    """The ``fid`` of an episode link, or "" when the URL names no episode.

    A series page and its episode pages are the same endpoint, so the fid is
    what tells one episode of a 352-video series from the series itself.
    """
    if not isinstance(url, str):
        return ""
    return (parse_qs(urlparse(url).query).get("fid") or [""])[0]

def shyav_read(session, endpoint, params, timeout=60):
    """Read one gateway endpoint and classify what came back.

    Returns ``(response, reason, retryable)``; a gateway hiccup is worth another
    attempt, a removed video is not.
    """
    try:
        resp = session.get(
            gateway_url(f"/api/v1/scrape/shyav{endpoint}"),
            params=params,
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

def shyav_episodes(response):
    """The ``(fid, label)`` pairs of a detail response, episodes in order.

    A single video answers with ``total_episodes: 0`` and no list, which is one
    entry rather than a failure.
    """
    episodes = response.get("episodes") or []
    if episodes:
        return [
            (str(ep.get("fid") or ""), ep.get("label") or "")
            for ep in episodes
            if isinstance(ep, dict) and ep.get("fid")
        ]
    if response.get("video_url"):
        return [("", response.get("title") or "")]
    return []

def shyav_episode_url(page_url, fid):
    """*page_url* with *fid* as its file id -- and only one of them.

    The link the user sent may already be an episode (``…&fid=1&ep=1``), and
    appending a second fid leaves the gateway to pick one of two.
    """
    parsed = urlparse(page_url)
    query = {
        key: value for key, value in parse_qs(parsed.query).items() if key != "fid"
    }
    query["fid"] = [fid]
    return parsed._replace(query=urlencode(query, doseq=True)).geturl()

def shyav_entry(page_url, fid, label, taken, index):
    """One episode as the ``contents`` entry ``DirectListener`` takes.

    ``url`` is the episode page rather than the CDN link: a signed URL minted
    now is stale by the time a 352-episode series reaches the ones at the end,
    so each is resolved as its turn comes.
    """
    name = unique_stem(safe_stem(label, f"episode {index}"), taken, fid)
    return {
        "path": "",
        "filename": f"{name}.mp4",
        "url": shyav_episode_url(page_url, fid),
    }


def shyav_resolve_entry(session, page_url):
    """Resolve one episode page to its signed MP4, or "" when it is gone."""
    response, reason, _retryable = shyav_read(
        session, "", {"q": page_url, "fid": shyav_episode_fid(page_url)}, 90
    )
    if not response:
        LOGGER.info(f"ShyAV: {page_url} rejected by the API ({reason})")
        return ""
    link = (response.get("data") or {}).get("video_url") or ""
    return link if link.startswith("http") else ""


async def shyav_resolve_download(page_url):
    """Async resolve one episode page to its signed MP4.

    ``DirectListener`` calls this with the entry URL the handler handed it, and
    resolves the CDN host in the ``q`` the gateway wants.
    """
    from ....util.bot_utils import sync_to_async

    return await sync_to_async(shyav_lookup_sync, page_url)


def shyav_lookup_sync(page_url):
    """The blocking half of :func:`shyav_resolve_download`, for a thread."""
    with Session() as session:
        return shyav_resolve_entry(session, page_url)


@register(predicate=is_shyav, order=90)
def shyav(url):
    """ShyAV handler: one video is one entry, a series is one entry per episode."""
    target = shyav_sanitize_url(url)

    with Session() as session:
        response, reason, _retryable = shyav_read(session, "", {"q": target}, 90)

    if not response:
        raise DirectDownloadLinkException(f"ERROR: {reason}")

    detail = response.get("data") or {}
    title = detail.get("title") or "ShyAV"
    page_url = detail.get("page_url") or target
    episodes = shyav_episodes(detail)
    if not episodes:
        raise DirectDownloadLinkException("ERROR: no video in this ShyAV page")

    # One video: hand over the MP4 that came with the response, which is what
    # was asked for and is fresher than a re-resolve.
    if len(episodes) == 1 and not episodes[0][0]:
        link = detail.get("video_url") or ""
        if not link.startswith("http"):
            raise DirectDownloadLinkException("ERROR: no video URL in response")
        stem = safe_stem(title, "shyav")
        return {
            "contents": [{"path": "", "filename": f"{stem}.mp4", "url": link}],
            "title": stem,
            "total_size": 0,
        }

    taken = set()
    contents = [
        shyav_entry(page_url, fid, label, taken, index)
        for index, (fid, label) in enumerate(episodes, 1)
    ]
    LOGGER.info(f"ShyAV: {title} lists {len(contents)} episode(s)")
    return {
        "contents": contents,
        "title": safe_stem(title, "shyav"),
        "total_size": 0,
        "lazy": "shyav",
    }
