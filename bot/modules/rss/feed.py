"""Talking to the feed: one HTTP client config, one way to read an entry.

Both the subscribe/get commands and the monitor fetched feeds with their own
copy of the same `AsyncClient(...)` call and dug the link and the size out of
an entry with their own copy of the same `try/except IndexError`.

The two link readers below are deliberately *not* merged: `item_url` and
`latest_url` disagree about an entry that carries exactly one `<link>`
(`item_url` falls back to `link`, `latest_url` uses `links[0]`), and that
difference is pre-existing behaviour of two different call sites.
"""

from __future__ import annotations

from re import I, compile

from feedparser import parse
from httpx import AsyncClient

from ...helper.ext_utils.bot_utils import get_size_bytes

__all__ = [
    "HEADERS",
    "SIZE_REGEX",
    "fetch_text",
    "item_blocked",
    "item_size",
    "item_url",
    "latest_url",
    "parse",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.5",
}

SIZE_REGEX = compile(r"(\d+(\.\d+)?\s?(GB|MB|KB|GiB|MiB|KiB))", I)


async def fetch_text(link: str, retries: int = 0) -> str:
    """Fetch a feed document. `retries` extra attempts on any failure.

    The loop swallows everything on purpose — the monitor has always treated
    "the tracker is flaky" and "we are being cancelled" alike — and re-raises
    the last failure, so the caller still sees it. Parsing stays outside the
    loop: a document that parses badly parses badly every time.
    """
    tries = 0
    while True:
        try:
            async with AsyncClient(
                headers=HEADERS, follow_redirects=True, timeout=60, verify=False
            ) as client:
                res = await client.get(link)
            return res.text
        except BaseException:  # the bare `except:` this replaces, spelled out
            tries += 1
            if tries > retries:
                raise


def item_url(entry) -> str:
    """The download URL of an entry: the enclosure if there is one."""
    try:
        return entry["links"][1]["href"]
    except IndexError:
        return entry["link"]


def latest_url(entry) -> str | None:
    """Same as `item_url`, but tolerates an entry with no links at all."""
    links = entry.get("links", [])
    if len(links) > 1:
        return links[1].get("href")
    if links:
        return links[0].get("href")
    return entry.get("link")


def item_size(entry) -> int:
    """Size in bytes: the `size` element, else the first size in the summary.

    Raises `IndexError` when a summary exists but holds no size — both callers
    already handle that, and one of them reports it to the user.
    """
    if entry.get("size"):
        return int(entry["size"])
    if entry.get("summary"):
        matches = SIZE_REGEX.findall(entry["summary"])
        sizes = [match[0] for match in matches]
        return get_size_bytes(sizes[0])
    return 0


def item_blocked(item_title: str, data: dict) -> bool:
    """Whether a subscription's filters reject this item title.

    `inf` is include ("every group must match one of its words"), `exf` is
    exclude ("no group may match").

    `sensitive` is inverted with respect to its name and always has been:
    `-stv true` lowercases both sides, i.e. matches case-*insensitively*, and
    the default (`False`) compares verbatim. Kept as-is — the flag is stored in
    the DB and documented by example in the help text.
    """
    sensitive = data.get("sensitive", False)
    title = item_title.lower() if sensitive else item_title
    for flist in data["inf"]:
        if all(_needle(word, sensitive) not in title for word in flist):
            return True
    for flist in data["exf"]:
        if any(_needle(word, sensitive) in title for word in flist):
            return True
    return False


def _needle(word: str, sensitive: bool) -> str:
    return word.lower() if sensitive else word
