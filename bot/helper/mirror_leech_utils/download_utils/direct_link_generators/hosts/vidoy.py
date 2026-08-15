"""Vidoy direct link handler and helpers."""

from os import path as ospath
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

VIDOY_HOST = "vdy.to"
VIDOY_ATTEMPTS = 3
VIDOY_DOMAINS = (
    "vdy.to",
    "vidoy.com",
    "vidoy.asia",
    "vidoy.net",
    "vidoy.live",
    "vidoy.me",
    "vidoy.to",
)
VIDOY_MEDIA_EXTS = frozenset(
    (".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".ts", ".flv", ".wmv", ".m3u8")
)


def is_vidoy_link(url):
    """Match the host itself or a subdomain of it, so a lookalike like
    notvidoy.com is not mistaken for the real thing."""
    domain = (urlparse(url).hostname or "").lower()
    return any(domain == x or domain.endswith(f".{x}") for x in VIDOY_DOMAINS)


def vidoy_sanitize_url(url):
    """Vidoy rotates embed domains and only bounces the retired ones through a
    JS redirect, so aim at the current host directly."""
    parsed = urlparse(url)
    hostname = parsed.hostname.lower() if parsed.hostname else ""
    if not hostname or hostname == VIDOY_HOST:
        return url
    return parsed._replace(netloc=VIDOY_HOST).geturl()


def vidoy_scrape(session, target, probe=False):
    """Return (response, reason, retryable). A gateway hiccup is worth another
    attempt, a removed video is not."""
    gateway_base = (getattr(Config, "GATEWAY_URL", "") or "https://api.piyann.me").rstrip("/")
    vidoy_api = f"{gateway_base}/api/v1/scrape/vidoy"
    headers = {"accept": "application/json"}
    if token := getattr(Config, "GATEWAY_TOKEN", ""):
        headers["Authorization"] = f"Bearer {token}"
    params = {"q": target}
    if probe:
        params["probe"] = "true"
    try:
        resp = session.get(
            vidoy_api,
            params=params,
            headers=headers,
            timeout=60,
        )
    except Exception as e:
        return None, e.__class__.__name__, True

    try:
        response = resp.json()
    except Exception:
        return (
            None,
            f"HTTP {resp.status_code} (non-JSON response)",
            resp.status_code >= 500,
        )

    if not response.get("success"):
        reason = response.get("error") or f"HTTP {resp.status_code}"
        return None, reason, resp.status_code >= 500
    if not (response.get("cdn_url") or "").startswith("http"):
        return None, "no CDN link in response", False
    return response, None, False


def vidoy_headers(download_info):
    """Header map the CDN expects. Some of the MP4 hosts refuse a request that
    carries no Range at all - mp4-05 answers 403 where mp4-06 answers 200 - so
    an open-ended Range is kept. aria2 overrides it per connection and yields a
    file byte-identical to a single-connection fetch."""
    headers = download_info.get("headers")
    if not isinstance(headers, dict):
        headers = {}
    headers = {key: value for key, value in headers.items() if value}

    headers.setdefault(
        "User-Agent", (download_info.get("user_agent") or "").strip() or user_agent
    )
    headers.setdefault(
        "Referer",
        (download_info.get("referer") or "").strip() or f"https://{VIDOY_HOST}/",
    )
    headers["Accept-Encoding"] = "identity"
    headers.setdefault("Range", "bytes=0-")
    return headers


def vidoy_stem(response):
    """The title without its container suffix. yt-dlp appends the real extension
    to listener.name, so an MP4 title handed over whole lands as "clip.mp4.mp4"."""
    title = (response.get("title") or "").strip()
    stem, ext = ospath.splitext(title)
    return stem if ext.lower() in VIDOY_MEDIA_EXTS else title


def vidoy_resolve(url, name=""):
    """Resolve a Vidoy page to its raw CDN stream for yt-dlp, which copes with
    both the MP4 and the HLS variants. Returns (name, link, headers) - the MP4
    CDN 403s without a Referer, so the headers have to travel with the link."""
    sanitized = vidoy_sanitize_url(url)
    targets = [url] if sanitized == url else [url, sanitized]
    reason = "unknown error"

    with Session() as session:
        for attempt in range(1, VIDOY_ATTEMPTS + 1):
            retryable = False
            for target in targets:
                response, reason, retryable = vidoy_scrape(session, target)
                if response:
                    if not name:
                        name = vidoy_stem(response)
                    return (
                        name,
                        response["cdn_url"],
                        vidoy_headers(response.get("download") or {}),
                    )
                LOGGER.info(f"Vidoy: {target} rejected by the API ({reason})")

            if not retryable:
                break
            LOGGER.info(f"Vidoy: {reason}, retrying [{attempt}/{VIDOY_ATTEMPTS}]")
            if attempt < VIDOY_ATTEMPTS:
                sleep(3)

    raise DirectDownloadLinkException(f"ERROR: {reason}")


@register(predicate=is_vidoy_link, order=36)
def vidoy(url):

    max_attempts = VIDOY_ATTEMPTS

    def __build_header(download_info):
        headers = vidoy_headers(download_info)
        return [f"{key}: {value}" for key, value in sorted(headers.items())]

    def __header_dict(header):
        headers = {}
        for line in header:
            key, _, value = line.partition(":")
            headers[key.strip()] = value.strip()
        return headers

    def __filename(response):
        name = (response.get("title") or "").strip().replace("\\", "/")
        name = ospath.basename(name).strip()
        if not name:
            name = (response.get("video_id") or "").strip() or "vidoy"
        if not ospath.splitext(name)[1]:
            subtype = (
                (response.get("content_type") or "video/mp4")
                .partition("/")[2]
                .partition(";")[0]
                .strip()
            )
            name = f"{name}.{subtype or 'mp4'}"
        return name

    def __hls_link(response):
        """Some videos only exist as an HLS ladder. The master playlist is a
        couple hundred bytes of text that aria2 would save as the video, so it
        has to be spotted before the link is handed over."""
        link = response.get("cdn_url") or ""
        if (response.get("bucket") or "").lower().endswith("hls"):
            return link
        if urlparse(link).path.lower().endswith((".m3u8", ".m3u")):
            return link
        if "mpegurl" in (response.get("content_type") or "").lower():
            return link
        return None

    def __probe(link, headers):
        """Return (final_url, size, None) when the CDN serves the media itself,
        otherwise (None, 0, reason)."""
        try:
            with Session() as session, session.get(
                link,
                headers={**headers, "Range": "bytes=0-511"},
                allow_redirects=True,
                stream=True,
                timeout=30,
            ) as resp:
                if resp.status_code >= 400:
                    return None, 0, f"HTTP {resp.status_code}"
                content_type = resp.headers.get("Content-Type", "").lower()
                if (
                    content_type.startswith(("text/", "application/json"))
                    or "mpegurl" in content_type
                ):
                    return None, 0, f"got {content_type} instead of media"
                if not next(resp.iter_content(512), b""):
                    return None, 0, "empty response"
                size = resp.headers.get("Content-Range", "").rpartition("/")[2]
                return resp.url, int(size) if size.isdigit() else 0, None
        except Exception as e:
            return None, 0, e.__class__.__name__

    sanitized_url = vidoy_sanitize_url(url)
    targets = [url] if sanitized_url == url else [url, sanitized_url]
    reason = "unknown error"

    with Session() as session:
        for attempt in range(1, max_attempts + 1):
            response = None
            retryable = False
            for target in targets:
                response, reason, retryable = vidoy_scrape(session, target, probe=True)
                if response:
                    break
                LOGGER.info(f"Vidoy: {target} rejected by the API ({reason})")

            if response:
                if hls := __hls_link(response):
                    return {
                        "ytdlp": True,
                        "link": hls,
                        "name": vidoy_stem(response),
                        "headers": vidoy_headers(response.get("download") or {}),
                    }
                header = __build_header(response.get("download") or {})
                link, probed_size, reason = __probe(
                    response["cdn_url"], __header_dict(header)
                )
                if link:
                    break
                reason = f"CDN link rejected ({reason})"
                retryable = True

            if not retryable:
                raise DirectDownloadLinkException(f"ERROR: {reason}")

            LOGGER.info(f"Vidoy: {reason}, retrying [{attempt}/{max_attempts}]")
            if attempt < max_attempts:
                sleep(3)
        else:
            raise DirectDownloadLinkException(
                f"ERROR: Vidoy could not be resolved ({reason}). The link may "
                "have expired or the video was removed, try again later"
            )

    try:
        size = int(response.get("content_length") or 0)
    except (TypeError, ValueError):
        size = 0

    filename = __filename(response)
    return {
        "contents": [{"path": "", "filename": filename, "url": link}],
        "title": ospath.splitext(filename)[0],
        "total_size": size or probed_size,
        "header": "\n".join(header),
    }
