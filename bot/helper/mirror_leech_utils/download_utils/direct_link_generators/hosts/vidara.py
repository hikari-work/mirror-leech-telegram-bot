"""Vidara HLS resolver - resolve video pages to HLS master playlist and quality variants."""

from os import path as ospath
from random import uniform
from time import sleep
from urllib.parse import urlparse

from requests import Session

from .._common import (
    LOGGER,
    Config,
    DirectDownloadLinkException,
    user_agent,
)
from ..registry import register

VIDARA_HOST = "vidara.to"
VIDARA_ATTEMPTS = 3
VIDARA_MAX_RETRY_DELAY = 30
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


def vidara_scrape(session, target, probe=False):
    """Return (response, reason, retryable, retry_after). A gateway hiccup or a
    rate limit is worth another attempt, a removed video is not; retry_after is
    the gateway's own hint when it sent one."""
    gateway_base = (getattr(Config, "GATEWAY_URL", "") or "https://api.piyann.me").rstrip("/")
    vidara_api = f"{gateway_base}/api/v1/scrape/vidara"
    headers = {"accept": "application/json"}
    if token := getattr(Config, "GATEWAY_TOKEN", ""):
        headers["Authorization"] = f"Bearer {token}"
    params = {"q": target}
    if probe:
        params["probe"] = "true"
    try:
        resp = session.get(
            vidara_api,
            params=params,
            headers=headers,
            timeout=60,
        )
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
    if ext.lower() in (".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".ts", ".flv", ".wmv"):
        return stem
    return title


def vidara_resolve(url, name=""):
    """Resolve a Vidara page to its HLS master playlist for yt-dlp.
    Returns (name, link, headers) for HLS streaming."""
    sanitized = vidara_sanitize_url(url)
    targets = [url] if sanitized == url else [url, sanitized]
    reason = "unknown error"

    with Session() as session:
        for attempt in range(1, VIDARA_ATTEMPTS + 1):
            retryable = False
            retry_after = None
            for target in targets:
                response, reason, retryable, retry_after = vidara_scrape(
                    session, target
                )
                if response:
                    if not name:
                        name = vidara_stem(response)
                    master_url = response.get("master_url") or response.get("master_playlist_url")
                    return (
                        name,
                        master_url,
                        vidara_headers(response.get("download") or {}),
                    )
                LOGGER.info(f"Vidara: {target} rejected by the API ({reason})")

            if not retryable or attempt == VIDARA_ATTEMPTS:
                break
            delay = vidara_retry_delay(attempt, retry_after)
            LOGGER.info(
                f"Vidara: {reason}, retrying in {delay:.1f}s "
                f"[{attempt}/{VIDARA_ATTEMPTS}]"
            )
            sleep(delay)

    raise DirectDownloadLinkException(f"ERROR: {reason}")


@register(predicate=is_vidara_link, order=37)
def vidara(url):
    """Main Vidara handler that returns HLS stream information for yt-dlp."""
    
    max_attempts = VIDARA_ATTEMPTS

    def __build_header(download_info):
        headers = vidara_headers(download_info)
        return [f"{key}: {value}" for key, value in sorted(headers.items())]

    def __header_dict(header):
        headers = {}
        for line in header:
            key, _, value = line.partition(":")
            headers[key.strip()] = value.strip()
        return headers

    def __filename(response):
        # First try the filename field from API response
        filename = response.get("filename", "").strip()
        if filename:
            name = ospath.basename(filename).strip()
            if name:
                return name
        
        # Fall back to title
        name = (response.get("title") or "").strip().replace("\\", "/")
        name = ospath.basename(name).strip()
        if not name:
            name = (response.get("file_code") or "").strip() or "vidara"
        
        # For HLS streams, ensure we have an extension
        if not ospath.splitext(name)[1]:
            name = f"{name}.mp4"
        return name

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

    sanitized_url = vidara_sanitize_url(url)
    targets = [url] if sanitized_url == url else [url, sanitized_url]
    reason = "unknown error"

    with Session() as session:
        for attempt in range(1, max_attempts + 1):
            response = None
            retryable = False
            retry_after = None
            for target in targets:
                response, reason, retryable, retry_after = vidara_scrape(
                    session, target, probe=True
                )
                if response:
                    break
                LOGGER.info(f"Vidara: {target} rejected by the API ({reason})")

            if response:
                # Get master URL - try both field names for compatibility
                master_url = response.get("master_url") or response.get("master_playlist_url")
                if master_url:
                    header = __build_header(response.get("download") or {})
                    hls_link, probe_reason = __probe_hls(master_url, __header_dict(header))
                    if hls_link:
                        # Return for yt-dlp processing
                        return {
                            "ytdlp": True,
                            "link": hls_link,
                            "name": vidara_stem(response),
                            "headers": vidara_headers(response.get("download") or {}),
                        }
                    reason = f"HLS playlist rejected ({probe_reason})"
                    retryable = True
                    retry_after = None
                else:
                    reason = "no master URL in API response"
                    retryable = False

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

    # This should not be reached due to the raise above, but just in case
    raise DirectDownloadLinkException(f"ERROR: Vidara resolution failed ({reason})")
