"""Shared helpers for direct link generator host modules."""

from re import findall

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
