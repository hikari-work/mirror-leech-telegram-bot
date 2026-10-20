from re import compile as re_compile
from re import match as re_match

_URL_RE = re_compile(r"https?://[^\s<>\"'`]+")
"""One http(s) URL. Deliberately greedy: the trimming below decides the end."""

_URL_TRAILING = ".,;:!?\"'"
"""Sentence punctuation that never belongs to a URL."""

_URL_CLOSERS = {")": "(", "]": "[", "}": "{"}
"""Closing brackets that may be part of a path -- or may wrap the link."""


def is_magnet(url: str):
    return bool(
        re_match(
            r"^magnet:\?.*xt=urn:(btih|btmh):([a-zA-Z0-9]{32,40}|[a-z2-7]{32}).*", url
        )
    )


def is_url(url: str):
    return bool(
        re_match(
            r"^(?!\/)(rtmps?:\/\/|mms:\/\/|rtsp:\/\/|https?:\/\/|ftp:\/\/)?([^\/:]+:[^\/@]+@)?(www\.)?(?=[^\/:\s]+\.[^\/:\s]+)([^\/:\s]+\.[^\/:\s]+)(:\d+)?(\/[^#\s]*[\s\S]*)?(\?[^#\s]*)?(#.*)?$",
            url,
        )
    )


def is_telegram_link(url: str):
    return url.startswith(("https://t.me/", "tg://openmessage?user_id="))


def extract_urls(text: str | None) -> list[str]:
    """Every http(s) URL in *text*, in first-seen order, without duplicates.

    Text that came out of a chat carries links wrapped in prose, Markdown and
    punctuation, so the end of a URL cannot be settled by the character class
    alone: ``(https://x/a)`` ends at the closing bracket, while
    ``https://en.wikipedia.org/wiki/Foo_(bar)`` needs it. A closer is trimmed
    only while it is unbalanced, which tells the two apart.
    """
    if not text:
        return []
    found: list[str] = []
    for match in _URL_RE.finditer(text):
        url = _trim_url(match.group())
        if url and url not in found:
            found.append(url)
    return found


def _trim_url(url: str) -> str:
    while url:
        last = url[-1]
        if last in _URL_TRAILING:
            url = url[:-1]
        elif last in _URL_CLOSERS and url.count(last) > url.count(_URL_CLOSERS[last]):
            url = url[:-1]
        else:
            break
    return url


def is_share_link(url: str):
    return bool(
        re_match(
            r"https?:\/\/.+\.gdtot\.\S+|https?:\/\/(filepress|filebee|appdrive|gdflix)\.\S+",
            url,
        )
    )
