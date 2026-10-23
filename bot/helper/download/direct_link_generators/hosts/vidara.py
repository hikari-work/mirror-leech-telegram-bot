"""Vidara HLS resolver - page to HLS master playlist and quality variants."""

from collections.abc import Callable
from os import path as ospath
from random import uniform
from time import sleep
from urllib.parse import urlparse

from requests import Session

from .._common import (
    LOGGER,
    DirectDownloadLinkException,
    gateway_headers,
    gateway_url,
    user_agent,
)
from ..registry import register

VIDARA_HOST = "vidara.to"
VIDARA_ATTEMPTS = 3
VIDARA_MAX_RETRY_DELAY = 30
# A folder page can nest folders. Both caps exist so one link cannot walk the
# whole host: whatever they drop is logged rather than passed off as the folder.
VIDARA_FOLDER_MAX_DEPTH = 3
VIDARA_FOLDER_MAX_VIDEOS = 500
VIDARA_DOMAINS = (
    "vidara.to",
    "vidara.me",
    "vidara.co",
    "vidara.net",
    "vidara.org",
    "vidara.tv",
    "vidara.xyz",
    "vidara.so"
)

# "Come back later" statuses. A bulk used to give up on the first 429 and report
# it as a dead link, which is how half a batch could fail while every link in it
# was fine.
VIDARA_RETRY_STATUSES = (408, 425, 429)
VIDARA_RATE_LIMIT_HINTS = ("rate limit", "rate-limit", "too many request", "slow down")


def vidara_retryable_status(status):
    """A gateway hiccup or a rate limit; not a removed video."""
    return status >= 500 or status in VIDARA_RETRY_STATUSES


def vidara_rate_limited(reason):
    """The gateway also reports a rate limit as HTTP 200 + success: false."""
    text = str(reason).lower()
    return any(hint in text for hint in VIDARA_RATE_LIMIT_HINTS)


def vidara_retry_delay(attempt, retry_after=None):
    """Seconds to wait before attempt+1, jittered.

    The jitter is the point: a bulk resolves in lockstep, so a fixed sleep only
    reschedules the same burst.
    """
    if retry_after:
        try:
            return min(float(retry_after), VIDARA_MAX_RETRY_DELAY) + uniform(0, 1.5)
        except (TypeError, ValueError):
            pass
    return min(2**attempt, VIDARA_MAX_RETRY_DELAY) + uniform(0, 1.5)


def is_vidara_link(url):
    """Match the host itself or a subdomain of it, so a lookalike like
    notvidara.com is not mistaken for the real thing."""
    domain = (urlparse(url).hostname or "").lower()
    return any(domain == x or domain.endswith(f".{x}") for x in VIDARA_DOMAINS)


def vidara_sanitize_url(url):
    """Sanitize Vidara URL to ensure consistency."""
    parsed = urlparse(url)
    hostname = parsed.hostname.lower() if parsed.hostname else ""
    if not hostname or hostname == VIDARA_HOST:
        return url
    return parsed._replace(netloc=VIDARA_HOST).geturl()


def vidara_folder_code(url):
    """The code from a ``/f/<code>`` folder page, or "" when the URL is not one.

    Vidara serves videos at /e/ and /v/ and folders at /f/, and the stream API
    only takes the first two -- which is why a folder link came back as "no file
    code found" instead of the nine videos the page lists."""
    if not isinstance(url, str):
        return ""
    parts = [part for part in urlparse(url).path.split("/") if part]
    if len(parts) >= 2 and parts[0].lower() == "f":
        return parts[1]
    return ""


def is_vidara_folder_link(url):
    """A Vidara folder page, as opposed to a single video."""
    return bool(is_vidara_link(url) and vidara_folder_code(url))


def vidara_gateway(endpoint=""):
    """(url, headers) for a Vidara endpoint on the gateway."""
    return gateway_url(f"/api/v1/scrape/vidara{endpoint}"), gateway_headers()


def vidara_read(
    session: Session,
    endpoint: str,
    params: dict,
    timeout: int,
    nocache: bool = False,
) -> tuple[dict | None, str | None, bool, str | None]:
    """Read one gateway endpoint and classify what came back.

    Returns (response, reason, retryable, retry_after). A gateway hiccup or a
    rate limit is worth another attempt, a removed video is not; retry_after is
    the gateway's own hint when it sent one. The single-video endpoint and the
    folder endpoint answer in the same envelope, so both are read here.

    ``nocache`` asks the gateway to skip its stored answer. The gateway keeps a
    failed response for a short while, which is longer than this module's own
    retry window: without this, every attempt would be served the same stored
    failure and a transient hiccup would read as a dead video. The first attempt
    still takes the cached answer; only a retry asks for a fresh one.
    """
    api, headers = vidara_gateway(endpoint)
    if nocache:
        params = {**params, "nocache": "1"}
    try:
        resp = session.get(api, params=params, headers=headers, timeout=timeout)
    except Exception as e:
        return None, e.__class__.__name__, True, None

    resp_headers = getattr(resp, "headers", None)
    retry_after = resp_headers.get("Retry-After") if resp_headers else None
    retryable = vidara_retryable_status(resp.status_code)

    try:
        response = resp.json()
    except Exception:
        return (
            None,
            f"HTTP {resp.status_code} (non-JSON response)",
            retryable,
            retry_after,
        )

    if not response.get("success"):
        reason = response.get("error") or f"HTTP {resp.status_code}"
        return None, reason, retryable or vidara_rate_limited(reason), retry_after
    return response, None, False, None


def vidara_scrape(
    session: Session, target: str, probe: bool = False, nocache: bool = False
) -> tuple[dict | None, str | None, bool, str | None]:
    """One video page, resolved to the response that carries its HLS ladder."""
    params = {"q": target}
    if probe:
        params["probe"] = "true"
    response, reason, retryable, retry_after = vidara_read(
        session, "", params, 60, nocache
    )
    if not response:
        return None, reason, retryable, retry_after

    # Check for either master_url or master_playlist_url
    master_url = response.get("master_url") or response.get("master_playlist_url")
    if not master_url or not master_url.startswith("http"):
        return None, "no HLS master playlist in response", False, None
    return response, None, False, None


def vidara_headers(download_info):
    """Header map the HLS expects. Include standard headers for HLS streaming."""
    headers = download_info.get("headers")
    if not isinstance(headers, dict):
        headers = {}
    headers = {key: value for key, value in headers.items() if value}

    headers.setdefault(
        "User-Agent", (download_info.get("user_agent") or "").strip() or user_agent
    )
    headers.setdefault(
        "Referer",
        (download_info.get("referer") or "").strip() or f"https://{VIDARA_HOST}/",
    )
    headers["Accept"] = "application/vnd.apple.mpegurl, application/x-mpegURL, */*"
    headers["Accept-Encoding"] = "identity"
    return headers


def vidara_stem(response):
    """The title without its container suffix."""
    title = (response.get("title") or "").strip()
    stem, ext = ospath.splitext(title)
    # For HLS streams, remove common video extensions
    if ext.lower() in (
        ".mp4",
        ".mkv",
        ".webm",
        ".mov",
        ".avi",
        ".m4v",
        ".ts",
        ".flv",
        ".wmv",
    ):
        return stem
    return title


def vidara_safe_name(name, fallback):
    """One path component, never a path: a title decides a file name, not where
    the file lands."""
    name = (name or "").strip().replace("\\", "/")
    name = ospath.basename(name).strip().strip(".")
    return name or fallback


def vidara_folder_scrape(
    session: Session, target: str, nocache: bool = False
) -> tuple[dict | None, str | None, bool, str | None]:
    """Return (response, reason, retryable, retry_after) for one folder page.

    ``resolve`` is left off deliberately. The gateway can resolve every video in
    the listing for us, but the streams it mints are IP-bound and expiring, and
    the ones at the end of a long folder would be stale by the time the download
    reached them -- so the listing is fetched here and each video is resolved
    when its turn comes.
    """
    return vidara_read(
        session, "/folder", {"q": target, "resolve": "false"}, 90, nocache
    )


def vidara_targets(url):
    """The spellings of *url* worth trying, the one the user sent first."""
    sanitized = vidara_sanitize_url(url)
    return [url] if sanitized == url else [url, sanitized]


def vidara_scrape_targets(
    session: Session, targets: list[str], probe: bool = False, nocache: bool = False
) -> tuple[dict | None, str | None, bool, str | None]:
    """Scrape each spelling of a video page, stopping at the first that answers.

    Answers like ``vidara_scrape`` does; when none of them resolved, the reason
    and the retry hint are the last one's, which is what decides the next attempt.
    """
    reason, retryable, retry_after = "unknown error", False, None
    for target in targets:
        response, reason, retryable, retry_after = vidara_scrape(
            session, target, probe, nocache
        )
        if response:
            return response, None, retryable, retry_after
        LOGGER.info(f"Vidara: {target} rejected by the API ({reason})")
    return None, reason, retryable, retry_after


def vidara_retry(scrape: Callable[[bool], tuple], label: str = "") -> dict:
    """Call *scrape* until it answers, backing off on the answers worth retrying.

    *scrape* takes the ``nocache`` flag the gateway understands and hands back
    the ``(response, reason, retryable, retry_after)`` a gateway read produces.
    The jittered backoff is the point of doing this in one place: a bulk retries
    in lockstep otherwise. Raises with the last reason once the attempts are
    spent, or as soon as one comes back final.

    Only a retry asks for a fresh read: the first attempt still takes the
    gateway's stored answer, which is what a repeat of the same link wants.
    """
    reason = "unknown error"
    for attempt in range(1, VIDARA_ATTEMPTS + 1):
        response, reason, retryable, retry_after = scrape(attempt > 1)
        if response:
            return response
        if not retryable or attempt == VIDARA_ATTEMPTS:
            break
        delay = vidara_retry_delay(attempt, retry_after)
        LOGGER.info(
            f"Vidara: {reason}, retrying{label} in {delay:.1f}s "
            f"[{attempt}/{VIDARA_ATTEMPTS}]"
        )
        sleep(delay)
    raise DirectDownloadLinkException(f"ERROR: {reason}")


def vidara_folder_fetch(session, url):
    """One folder listing, retried on the answers worth retrying.

    Same classification the single-video path uses: a 429 a bulk provoked is
    another attempt, a folder that no longer exists is not.
    """

    def _scrape(nocache: bool = False):
        answer = vidara_folder_scrape(session, url, nocache)
        if not answer[0]:
            LOGGER.info(f"Vidara: folder {url} rejected by the API ({answer[1]})")
        return answer

    return vidara_retry(_scrape, label=" folder")


def vidara_folder_videos(response, subpath, taken):
    """The videos of one listing, as download entries.

    ``name`` is the stem: yt-dlp appends the container it muxed the HLS into, so
    a title handed over whole would land as "clip.mp4.mp4". Two videos in a
    folder can carry the same title, so a repeat takes its file code as a
    suffix -- otherwise the second one overwrites the first.
    """
    entries = []
    for video in response.get("videos") or []:
        if not isinstance(video, dict):
            continue
        code = (video.get("file_code") or "").strip()
        page_url = (video.get("page_url") or video.get("embed_url") or "").strip()
        if not (code or page_url):
            LOGGER.info("Vidara: skipping a folder entry with no file code")
            continue
        stem = ospath.splitext(
            vidara_safe_name(
                video.get("filename") or video.get("title"), code or "vidara"
            )
        )[0]
        if (subpath, stem.lower()) in taken:
            stem = f"{stem} {code}" if code else f"{stem} {len(taken)}"
        taken.add((subpath, stem.lower()))
        entries.append(
            {
                "name": stem,
                "url": page_url or code,
                "code": code,
                "subpath": subpath,
                "duration": video.get("duration_seconds") or 0,
            }
        )
    return entries


def vidara_folder_children(response, subpath, depth, visited):
    """The subfolders of one listing, as ``(url, subpath, depth)`` to walk next.

    ``visited`` holds the codes already queued, so a folder that links back to
    one of its ancestors cannot send the walk round in circles.
    """
    children = []
    for sub in response.get("subfolders") or []:
        if not isinstance(sub, dict):
            continue
        code = (sub.get("code") or "").strip()
        sub_url = (sub.get("url") or "").strip() or code
        if not sub_url or (code and code.lower() in visited):
            continue
        if code:
            visited.add(code.lower())
        name = vidara_safe_name(sub.get("name"), code or "folder")
        children.append(
            (sub_url, f"{subpath}/{name}" if subpath else name, depth + 1)
        )
    return children


def vidara_folder_list(url):
    """List every video in a Vidara folder page, its subfolders included.

    Returns the descriptor ``vidara_download`` consumes -- the listing only, one
    entry per video, each still a page URL rather than a stream.
    """
    root = vidara_sanitize_url(url)
    title = ""
    folder_url = root
    videos = []
    taken = set()
    visited = {vidara_folder_code(root).lower()}
    dropped = 0
    skipped_folders = 0

    with Session() as session:
        pending = [(root, "", 0)]
        while pending:
            page, subpath, depth = pending.pop(0)
            try:
                response = vidara_folder_fetch(session, page)
            except DirectDownloadLinkException:
                # the folder the user asked for has to fail the task; a
                # subfolder inside it only costs its own videos
                if depth == 0:
                    raise
                LOGGER.error(f"Vidara: skipping subfolder {page}")
                skipped_folders += 1
                continue

            if depth == 0:
                title = vidara_safe_name(
                    response.get("name"),
                    vidara_folder_code(root) or "vidara",
                )
                folder_url = response.get("folder_url") or root

            entries = vidara_folder_videos(response, subpath, taken)
            room = max(0, VIDARA_FOLDER_MAX_VIDEOS - len(videos))
            dropped += max(0, len(entries) - room)
            videos.extend(entries[:room])

            children = vidara_folder_children(response, subpath, depth, visited)
            room_left = len(videos) < VIDARA_FOLDER_MAX_VIDEOS
            if depth < VIDARA_FOLDER_MAX_DEPTH and room_left:
                pending.extend(children)
            else:
                skipped_folders += len(children)

    if not videos:
        raise DirectDownloadLinkException(
            f"ERROR: no videos in this Vidara folder ({folder_url})"
        )
    if dropped or skipped_folders:
        # a cap that trims a folder silently reads as "that was all of it"
        LOGGER.info(
            f"Vidara: folder {title} lists {len(videos)} videos; left out "
            f"{dropped} video(s) and {skipped_folders} subfolder(s) "
            f"(caps: depth {VIDARA_FOLDER_MAX_DEPTH}, "
            f"{VIDARA_FOLDER_MAX_VIDEOS} videos per link)"
        )
    else:
        LOGGER.info(f"Vidara: folder {title} lists {len(videos)} videos")

    return {
        "vidara": True,
        "title": title,
        "folder_url": folder_url,
        "videos": videos,
    }


def vidara_resolve(url, name=""):
    """Resolve a Vidara page to its HLS master playlist for yt-dlp.
    Returns (name, link, headers) for HLS streaming."""
    targets = vidara_targets(url)

    with Session() as session:
        response = vidara_retry(
            lambda nocache=False: vidara_scrape_targets(
                session, targets, nocache=nocache
            )
        )

    master_url = response.get("master_url") or response.get("master_playlist_url")
    return (
        name or vidara_stem(response),
        master_url,
        vidara_headers(response.get("download") or {}),
    )


def _try_response(
    response: dict, probe: Callable[[str, dict], tuple[str | None, str | None]]
) -> tuple[dict | None, str | None, bool]:
    """Turn a truthy scrape response into (result, reason, retryable).

    ``result`` is the yt-dlp payload when the master playlist resolved, else
    ``None`` with ``reason`` saying why and whether a retry could help.
    """
    # Get master URL - try both field names for compatibility
    master_url = response.get("master_url") or response.get("master_playlist_url")
    if not master_url:
        return None, "no master URL in API response", False
    headers = vidara_headers(response.get("download") or {})
    hls_link, probe_reason = probe(master_url, headers)
    if hls_link:
        payload = {
            "ytdlp": True,
            "link": hls_link,
            "name": vidara_stem(response),
            "headers": headers,
        }
        return payload, None, False
    return None, f"HLS playlist rejected ({probe_reason})", True


@register(predicate=is_vidara_link, order=37)
def vidara(url):
    """Main Vidara handler that returns HLS stream information for yt-dlp."""

    # a /f/ page is a listing, not a video: the stream API only takes a file
    # code, so a folder link has to be expanded before anything can be resolved
    if vidara_folder_code(url):
        return vidara_folder_list(url)

    max_attempts = VIDARA_ATTEMPTS

    def __probe_hls(link, headers):
        """Probe HLS master playlist to verify it's accessible."""
        try:
            with Session() as session, session.get(
                link,
                headers=headers,
                allow_redirects=True,
                timeout=30,
            ) as resp:
                if resp.status_code >= 400:
                    return None, f"HTTP {resp.status_code}"
                content_type = resp.headers.get("Content-Type", "").lower()
                content = resp.text[:1024]  # Read first 1KB

                # Check if it's a valid HLS playlist
                if (
                    "mpegurl" in content_type
                    or content.startswith("#EXTM3U")
                    or "#EXT-X-STREAM-INF" in content
                ):
                    return resp.url, None
                return None, f"not an HLS playlist (got {content_type})"
        except Exception as e:
            return None, e.__class__.__name__

    return _resolve_with_retries(url, max_attempts, __probe_hls)


def _resolve_with_retries(
    url: str,
    max_attempts: int,
    probe: Callable[[str, dict], tuple[str | None, str | None]],
) -> dict:
    """Scrape until the master playlist resolves, then return the yt-dlp payload."""
    targets = vidara_targets(url)
    reason: str | None = "unknown error"

    with Session() as session:
        for attempt in range(1, max_attempts + 1):
            response, reason, retryable, retry_after = vidara_scrape_targets(
                session, targets, probe=True, nocache=attempt > 1
            )

            if response:
                result, reason, retryable = _try_response(response, probe)
                if result:
                    return result
                retry_after = None

            if not retryable:
                raise DirectDownloadLinkException(f"ERROR: {reason}")

            if attempt < max_attempts:
                delay = vidara_retry_delay(attempt, retry_after)
                LOGGER.info(
                    f"Vidara: {reason}, retrying in {delay:.1f}s "
                    f"[{attempt}/{max_attempts}]"
                )
                sleep(delay)
        else:
            raise DirectDownloadLinkException(
                f"ERROR: Vidara could not be resolved ({reason}). The link may "
                "have expired or the video was removed, try again later"
            )
