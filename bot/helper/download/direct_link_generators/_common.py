"""Shared helpers for direct link generator host modules."""

from os import path as ospath
from re import findall
from re import sub as resub

from lxml.etree import HTML
from requests import post

from .... import LOGGER  # noqa: F401
from ....core.config_manager import Config  # noqa: F401
from ...net.gateway import gateway_headers, gateway_url  # noqa: F401
from ...util.exceptions import DirectDownloadLinkException  # noqa: F401
from ...util.help_messages import PASSWORD_ERROR_MESSAGE  # noqa: F401
from ...util.links_utils import is_share_link  # noqa: F401
from ...util.status_utils import speed_string_to_bytes  # noqa: F401
from ..url_shortener_bypass import bypass_shortener, is_url_shortener  # noqa: F401

user_agent = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0"
)

MEDIA_EXTS = frozenset(
    (".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".ts", ".flv", ".wmv", ".m3u8")
)


def safe_name(name, fallback):
    """One path component, never a path: a title decides a file name, not where
    the file lands."""
    cleaned = resub(r'[<>:"/\\|?*\x00-\x1f]', "", (name or "").strip()).strip(" .")
    return cleaned[:200] or fallback


def safe_stem(title, fallback):
    """A title as a file stem: separator junk dropped, container suffix cut.

    yt-dlp appends the container it muxed into, so a title handed over whole
    lands as "clip.mp4.mp4".
    """
    stem, ext = ospath.splitext(safe_name(title, fallback))
    return stem if ext.lower() in MEDIA_EXTS else safe_name(title, fallback)


def unique_stem(stem, taken, code=""):
    """*stem*, or one that has not been used in this listing yet.

    Two videos in a listing can carry the same title, and the second would
    otherwise land on top of the first.
    """
    if (key := stem.lower()) not in taken:
        taken.add(key)
        return stem
    stem = f"{stem} {code}" if code else f"{stem} {len(taken)}"
    taken.add(stem.lower())
    return stem


def header_lines(headers):
    """A header map as the "Key: value" lines aria2 takes."""
    return [f"{key}: {value}" for key, value in sorted(headers.items())]


def header_dict(lines):
    """"Key: value" lines back to a map, for the probes that want one.

    Only the first colon separates: a value is a URL as often as not.
    """
    headers = {}
    for line in lines:
        key, _, value = line.partition(":")
        headers[key.strip()] = value.strip()
    return headers


def get_captcha_token(session, params):
    recaptcha_api = "https://www.google.com/recaptcha/api2"
    res = session.get(f"{recaptcha_api}/anchor", params=params)
    anchor_html = HTML(res.text)
    if not (anchor_token := anchor_html.xpath('//input[@id="recaptcha-token"]/@value')):
        return None
    params["c"] = anchor_token[0]
    params["reason"] = "q"
    res = session.post(f"{recaptcha_api}/reload", params=params)
    if token := findall(r'"rresp","(.*?)"', res.text):
        return token[0]


def cf_bypass(url):
    "DO NOT ABUSE THIS"
    try:
        data = {"cmd": "request.get", "url": url, "maxTimeout": 60000}
        _json = post(
            "https://cf.jmdkh.eu.org/v1",
            headers={"Content-Type": "application/json"},
            json=data,
        ).json()
        if _json["status"] == "ok":
            return _json["solution"]["response"]
    except Exception as e:
        e
    raise DirectDownloadLinkException("ERROR: Con't bypass cloudflare")
