"""URL shortener bypassers.

Each handler resolves a shortened URL back to its underlying target URL.
Handlers are pure resolvers: input a shortened URL, output the target URL
string (or raise DirectDownloadLinkException). The caller in
direct_link_generator.py is responsible for re-dispatching the returned URL
through the regular host handlers.
"""

from random import uniform
from re import search
from time import sleep
from urllib.parse import urlparse

from requests import Session

from ....core.config_manager import Config
from ...ext_utils.exceptions import DirectDownloadLinkException

_OUO_DOMAINS = ("ouo.io", "ouo.press")

# Mirror domains the gateway's /bypass/linkvertise accepts. Handing it a host it
# doesn't know earns a 400, so keep this in step with the gateway — notably
# link-hub.net and link-center.net are Linkvertise fronts it currently rejects.
_LINKVERTISE_DOMAINS = (
    "linkvertise.com",
    "linkvertise.net",
    "link-to.net",
    "direct-link.net",
    "up-to-down.net",
    "file-link.net",
    "link-target.net",
)

# (name, domains it owns, gateway endpoint). Both shorteners gate the
# destination behind a flow the gateway already walks for us — ouo's CSRF chain
# behind Cloudflare, Linkvertise's GraphQL API — so neither is scraped here.
_BYPASSERS = (
    ("ouo", _OUO_DOMAINS, "/api/v1/bypass/ouo"),
    ("linkvertise", _LINKVERTISE_DOMAINS, "/api/v1/bypass/linkvertise"),
)

_GATEWAY_ATTEMPTS = 3
_GATEWAY_TIMEOUT = 60
_GATEWAY_MAX_RETRY_DELAY = 30
# 500 is deliberately absent: the gateway answers a dead shortlink with
# 500 + "Content Not Found", and retrying that only triples the wait.
_RETRY_STATUSES = (408, 425, 429, 502, 503, 504)
_TRANSIENT_HINTS = (
    "rate limit",
    "rate-limit",
    "too many request",
    "slow down",
    "timeout",
    "timed out",
    "temporarily",
    "try again",
)
_URL_IN_TEXT = r"https?://[^\s\"'<>]+"
# Some gateway errors quote the whole fetched page back — ouo does that when the
# CSRF token is missing — so clamp what ends up in the user's chat.
_MAX_REASON = 300


def _matches(domain, domains):
    """True when ``domain`` is one of ``domains`` or a subdomain of one."""
    domain = (domain or "").lower()
    return any(domain == d or domain.endswith(f".{d}") for d in domains)


def is_url_shortener(domain):
    return any(_matches(domain, domains) for _name, domains, _path in _BYPASSERS)


def bypass_shortener(link):
    domain = (urlparse(link).hostname or "").lower()
    for name, domains, path in _BYPASSERS:
        if _matches(domain, domains):
            return _gateway_bypass(name, path, link)
    raise DirectDownloadLinkException(f"ERROR: No bypasser for {domain}")


def _gateway(path):
    """(url, headers) for a gateway endpoint."""
    base = (getattr(Config, "GATEWAY_URL", "") or "https://api.piyann.me").rstrip("/")
    headers = {"accept": "application/json"}
    if token := getattr(Config, "GATEWAY_TOKEN", ""):
        headers["Authorization"] = f"Bearer {token}"
    return f"{base}{path}", headers


def _transient(reason):
    """The gateway also reports a rate limit as HTTP 200 + success: false."""
    text = str(reason).lower()
    return any(hint in text for hint in _TRANSIENT_HINTS)


def _clip(reason):
    """A one-line, chat-sized version of a gateway error."""
    text = " ".join(str(reason).split())
    return text if len(text) <= _MAX_REASON else f"{text[:_MAX_REASON]}…"


def _retry_delay(attempt, retry_after=None):
    """Seconds to wait before attempt+1, jittered.

    The jitter is the point: a bulk resolves in lockstep, so a fixed sleep only
    reschedules the same burst.
    """
    if retry_after:
        try:
            delay = min(float(retry_after), _GATEWAY_MAX_RETRY_DELAY)
            return delay + uniform(0, 1.5)
        except (TypeError, ValueError):
            pass
    return min(2 ** (attempt - 1), _GATEWAY_MAX_RETRY_DELAY) + uniform(0, 1.5)


def _target(payload):
    """The destination URL carried by a BypassResponse.

    Paste-type Linkvertise links come back as the paste body instead of a URL,
    so pull the first link out of it when that happens.
    """
    value = (payload.get("url") or "").strip()
    if value.startswith("http"):
        return value
    if value and (m := search(_URL_IN_TEXT, value)):
        return m.group(0)
    return ""


def _gateway_request(session, api, headers, link):
    """Return (target, reason, retryable, retry_after).

    A gateway hiccup or a rate limit is worth another attempt; a shortlink whose
    content is gone is not — the gateway reports that as 500, so the retry
    decision leans on the error text rather than the status alone.
    """
    try:
        resp = session.get(
            api, params={"url": link}, headers=headers, timeout=_GATEWAY_TIMEOUT
        )
    except Exception as e:
        return None, e.__class__.__name__, True, None

    retry_after = resp.headers.get("Retry-After")
    retryable = resp.status_code in _RETRY_STATUSES

    try:
        payload = resp.json()
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        reason = f"HTTP {resp.status_code} (non-JSON response)"
        return None, reason, retryable, retry_after

    if not payload.get("success"):
        reason = payload.get("error") or f"HTTP {resp.status_code}"
        return None, reason, retryable or _transient(reason), retry_after

    if target := _target(payload):
        return target, None, False, None

    body = (payload.get("url") or "").strip()
    reason = "no target URL in response"
    if body:
        reason += f" (paste content: {body[:200]})"
    return None, reason, False, None


def _gateway_bypass(name, path, link):
    """Resolve a shortlink via one of the gateway's /bypass endpoints."""
    api, headers = _gateway(path)
    reason = "unknown error"

    with Session() as session:
        for attempt in range(1, _GATEWAY_ATTEMPTS + 1):
            target, reason, retryable, retry_after = _gateway_request(
                session, api, headers, link
            )
            if target:
                return target
            if not retryable or attempt == _GATEWAY_ATTEMPTS:
                break
            sleep(_retry_delay(attempt, retry_after))

    raise DirectDownloadLinkException(f"ERROR: {name} bypass failed: {_clip(reason)}")
