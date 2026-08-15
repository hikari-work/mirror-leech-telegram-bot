"""Mega.nz link parser."""

from urllib.parse import unquote, urlparse

from .._common import DirectDownloadLinkException
from ..registry import register

MEGA_DOMAINS = ("mega.nz", "mega.co.nz", "mega.io")


def is_mega_link(url):
    """Match the host itself or a subdomain of it, so megaupload.nz - an
    unrelated host that is already listed as dead - is not caught here."""
    domain = (urlparse(url).hostname or "").lower()
    return any(domain == x or domain.endswith(f".{x}") for x in MEGA_DOMAINS)


@register(predicate=is_mega_link, order=37)
def mega(url):
    """Parse a Mega link into the handle and key the downloader needs.

    Mega encrypts client-side, so there is no direct link to hand back: the key
    lives in the '#' fragment - the part a browser never sends to a server,
    which is why Mega itself cannot read the file - and the bot decrypts the
    stream as it downloads. This only parses; the API calls happen in
    mega_download, so a bad link fails here rather than mid-transfer.

    Four layouts are accepted:

        https://mega.nz/folder/<handle>#<key>[/file/<h>]
        https://mega.nz/file/<handle>#<key>
        https://mega.nz/#F!<handle>!<key>                        (pre-2019)
        https://mega.nz/#!<handle>!<key>                         (pre-2019)
    """
    parsed = urlparse(url)
    fragment = unquote(parsed.fragment or "").strip()
    segments = [s for s in (parsed.path or "").split("/") if s]

    if not fragment and "%23" in url:
        parsed = urlparse(url.replace("%23", "#", 1))
        fragment = unquote(parsed.fragment or "").strip()
        segments = [s for s in (parsed.path or "").split("/") if s]

    kind = handle = key = ""

    if segments and segments[0] in ("folder", "file"):
        kind = "folder" if segments[0] == "folder" else "file"
        handle = segments[1] if len(segments) > 1 else ""
        key = fragment.split("/")[0]
    elif fragment.startswith("F!"):
        bits = fragment[2:].split("!")
        kind, handle = "folder", bits[0] if bits else ""
        key = bits[1] if len(bits) > 1 else ""
    elif fragment.startswith("!"):
        bits = fragment[1:].split("!")
        kind, handle = "file", bits[0] if bits else ""
        key = bits[1] if len(bits) > 1 else ""

    if not kind or not handle or not key:
        raise DirectDownloadLinkException(f"ERROR: Unrecognised Mega link: {url}")

    return {"mega": {"kind": kind, "handle": handle, "key": key}}
