"""A private seedbox: an nginx directory listing behind HTTP basic auth.

The host serves a directory as an HTML table rather than as a file, so a link to
one is scraped here and handed to the downloader as a list of contents -- the
same shape ``bunkr.py`` gives an album, and the reason this is a direct link
generator at all: nothing here generates a link, it reads a listing.

Every request carries the credentials the listing was read with, which is why
the result also carries a ``header``. aria2 is the one fetching the files later,
and the host answers anything without them with a 401.

Only the files the page shows directly are taken. A subdirectory in the listing
is counted and left alone: how deep a private seedbox goes is the owner's
business, and one link should not quietly become a mirror of the whole host.
"""

from __future__ import annotations

from base64 import b64encode
from os import path as ospath
from re import split as re_split
from re import sub as re_sub
from urllib.parse import unquote, urljoin, urlsplit

from lxml.etree import HTML
from requests import Session

from .._common import (
    LOGGER,
    Config,
    DirectDownloadLinkException,
    user_agent,
)
from ..registry import register

# Files one listing may hand over. The page is a single directory, so this only
# stops a link pointed at something enormous; what it drops is logged rather
# than passed off as the whole folder.
SEEDBOX_MAX_FILES = 2000

_SIZE_UNITS = {
    "B": 1,
    "K": 1024,
    "KB": 1024,
    "M": 1024**2,
    "MB": 1024**2,
    "G": 1024**3,
    "GB": 1024**3,
    "T": 1024**4,
    "TB": 1024**4,
}


def seedbox_hosts() -> list[str]:
    """The configured hosts, however they were separated.

    Read per call rather than at import: the value is editable from the settings
    menu, and a host added there should be claimed by the next link, not by the
    next restart. ``getattr`` because a host module has no business assuming
    which keys a given ``Config`` carries -- the tests here stub it with what
    the module under test reads, and this one is read by a registry predicate
    that runs on every link the bot resolves.
    """
    raw = getattr(Config, "SEEDBOX_HOSTS", "") or ""
    return [piece.lower() for piece in re_split(r"[,\s]+", str(raw).strip()) if piece]


def is_seedbox_link(url) -> bool:
    """Whether *url* is on a host the configured credentials belong to."""
    if not isinstance(url, str):
        return False
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        # urlsplit refuses malformed input, and this runs over every link the
        # bot is handed -- a predicate is not the place to raise
        return False
    if not host:
        return False
    return any(host == h or host.endswith(f".{h}") for h in seedbox_hosts())


def seedbox_auth() -> str | None:
    """The ``Authorization`` value for the configured credentials, or ``None``.

    Half a credential is a 401 either way, so a missing half answers ``None``
    and the caller says so, instead of sending it and reading the server's
    refusal.
    """
    username = getattr(Config, "SEEDBOX_USERNAME", "") or ""
    password = getattr(Config, "SEEDBOX_PASSWORD", "") or ""
    if not username or not password:
        return None
    token = b64encode(f"{username}:{password}".encode()).decode("ascii")
    return f"Basic {token}"


def seedbox_auth_header() -> str | None:
    """The same credential as one aria2 header line, which is what the
    downloader is handed -- one string, the form the other contents generators
    (``gofile``, ``sendcm``) already pass down."""
    value = seedbox_auth()
    return f"Authorization: {value}" if value else None


def seedbox_parse_size(text) -> int:
    """Bytes for a listing's size cell, or 0 when it does not say.

    nginx writes these in binary units ("110.3 MiB") and its headers often in
    decimal ones; both are read at 1024, which is what ``bunkr.py`` does with
    the sizes the gateway sends. A directory's size is "-", and that is 0.
    """
    parts = str(text or "").split()
    if len(parts) != 2:
        return 0
    try:
        number = float(parts[0])
    except ValueError:
        return 0
    # "MiB" and "MB" name the same bucket here: the difference is 5% on a size
    # that is only ever shown as progress
    unit = parts[1].upper().replace("I", "")
    return int(number * _SIZE_UNITS.get(unit, 1))


def seedbox_stem(value: str) -> str:
    """One path component's worth of *value*.

    The title becomes the folder the download lands in, so a name carrying a
    separator loses it here rather than at the filesystem call: a link to
    ``/..%2F..%2Fetc/`` decodes to something that would otherwise walk out of
    the download directory.
    """
    cleaned = value.strip().strip("/\\").strip()
    cleaned = re_sub(r"[\\/]+", "_", cleaned).strip(".")
    return cleaned or "seedbox"


def seedbox_parse_listing(html: str, base_url: str) -> tuple[list[dict], int]:
    """The files a listing page shows, and how many subdirectories it passed.

    One row per entry: an anchor naming it, then its size and date. The sort
    links in the header row and the parent entry at the top are anchors too, so
    they are told apart by their href -- a query, or a path ending in a slash --
    rather than by where they sit.
    """
    files: list[dict] = []
    directories = 0
    for row in HTML(html).xpath('//table[@id="list"]//tr'):
        cells = row.xpath("./td")
        anchors = cells[0].xpath("./a") if cells else []
        if not anchors:
            continue
        href = (anchors[0].get("href") or "").strip()
        name = unquote((anchors[0].text or "").strip())
        if not href or not name or "?" in href:
            continue
        if href.rstrip("/") in (".", ".."):
            # the link back up, not a subdirectory this listing dropped
            continue
        if href.endswith("/"):
            directories += 1
            continue
        url = urljoin(base_url, href)
        if urlsplit(url).scheme not in ("http", "https"):
            continue
        # a filename from a listing is a basename; anything else would become a
        # path the downloader is asked to write through
        filename = ospath.basename(name)
        if not filename:
            continue
        files.append({
            "filename": filename,
            "url": url,
            "size": seedbox_parse_size(cells[1].text if len(cells) > 1 else ""),
        })
    return files, directories


def seedbox_title(url: str, html: str) -> str:
    """The folder the download lands in: the last segment of the URL.

    The page's own heading is the fallback, for the listings reached by a URL
    that names no directory -- a bare host, or one carrying a query.
    """
    tail = unquote(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
    if tail:
        return seedbox_stem(tail)
    headings = HTML(html).xpath("//h1//text()")
    return seedbox_stem("".join(headings))


def _single_file(url: str, response, auth: str | None) -> dict:
    """A URL on the seedbox that answered with the file itself.

    Reached when the host serves something that is not a page, in which case the
    link is already a direct one and all it was missing is the credentials.
    """
    name = unquote(ospath.basename(urlsplit(url).path)) or "seedbox_file"
    try:
        size = int(response.headers.get("Content-Length") or 0)
    except ValueError:
        size = 0
    return {
        "contents": [{"path": "", "filename": name, "url": url}],
        "title": ospath.splitext(name)[0] or "seedbox",
        "total_size": size,
        "header": auth,
    }


def _listing(url: str, html: str, auth: str | None) -> dict:
    """The contents dict for a directory listing page."""
    files, directories = seedbox_parse_listing(html, url)
    if directories:
        LOGGER.info(
            f"Seedbox: {directories} subfolder(s) in {url} left alone, only the "
            "files this listing shows are downloaded"
        )
    if not files:
        raise DirectDownloadLinkException(
            f"ERROR: No files in this seedbox listing: {url}"
        )
    if len(files) > SEEDBOX_MAX_FILES:
        LOGGER.warning(
            f"Seedbox: {url} lists {len(files)} files, downloading the first "
            f"{SEEDBOX_MAX_FILES} (SEEDBOX_MAX_FILES)"
        )
        files = files[:SEEDBOX_MAX_FILES]

    return {
        # flat, and not the subdirectory the file sat in: a subdirectory here is
        # exactly what this does not walk into
        "contents": [
            {"path": "", "filename": f["filename"], "url": f["url"]} for f in files
        ],
        "title": seedbox_title(url, html),
        "total_size": sum(f["size"] for f in files),
        "header": auth,
    }


@register(predicate=is_seedbox_link, order=44)
def seedbox(url: str) -> dict:
    """Scrape one seedbox link into the contents the direct downloader takes."""
    auth = seedbox_auth()
    if auth is None:
        raise DirectDownloadLinkException(
            "ERROR: Seedbox link, but SEEDBOX_USERNAME and SEEDBOX_PASSWORD "
            "are not both set"
        )

    try:
        with Session() as session:
            response = session.get(
                url,
                headers={"User-Agent": user_agent, "Authorization": auth},
                timeout=60,
            )
    except Exception as exc:
        raise DirectDownloadLinkException(
            f"ERROR: Seedbox request failed: {exc}"
        ) from exc

    if response.status_code in (401, 403):
        raise DirectDownloadLinkException(
            f"ERROR: Seedbox rejected the credentials for "
            f"{urlsplit(url).hostname} (HTTP {response.status_code})"
        )
    if response.status_code >= 400:
        raise DirectDownloadLinkException(
            f"ERROR: Seedbox answered HTTP {response.status_code} for {url}"
        )

    if "html" not in response.headers.get("Content-Type", "").lower():
        return _single_file(url, response, seedbox_auth_header())
    return _listing(url, response.text, seedbox_auth_header())
