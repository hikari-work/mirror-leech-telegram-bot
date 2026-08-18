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

from curl_cffi import requests as cffi_requests
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

_CSRF_PATTERN = (
    r'<input[^>]*\bname="_token"[^>]*\bvalue="([^"]*)"'
    r'|<input[^>]*\bvalue="([^"]*)"[^>]*\bname="_token"'
)

_LINKVERTISE_ATTEMPTS = 3
_LINKVERTISE_TIMEOUT = 60
_LINKVERTISE_MAX_RETRY_DELAY = 30
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


def _matches(domain, domains):
    """True when ``domain`` is one of ``domains`` or a subdomain of one."""
    domain = (domain or "").lower()
    return any(domain == d or domain.endswith(f".{d}") for d in domains)


def is_url_shortener(domain):
    return _matches(domain, _OUO_DOMAINS) or _matches(domain, _LINKVERTISE_DOMAINS)


def bypass_shortener(link):
    domain = (urlparse(link).hostname or "").lower()
    if _matches(domain, _OUO_DOMAINS):
        return _ouo(link)
    if _matches(domain, _LINKVERTISE_DOMAINS):
        return _linkvertise(link)
    raise DirectDownloadLinkException(f"ERROR: No bypasser for {domain}")


def _extract_csrf(html):
    m = search(_CSRF_PATTERN, html)
    return next((g for g in m.groups() if g), "") if m else ""


def _ouo(link):
    """Resolve ouo.io / ouo.press shortlinks.

    Three-step CSRF dance: GET landing → POST /go/<id> → POST /xreallcygo/<id>.
    Cloudflare fronts ouo.io and fingerprints both TLS ClientHello and HTTP/2
    SETTINGS, so curl_cffi's chrome impersonation is required — stdlib
    requests/httpx gets 403.
    """
    normalized = link.replace("ouo.press", "ouo.io")
    parsed = urlparse(normalized)
    short_id = parsed.path.rsplit("/", 1)[-1]
    if not short_id:
        raise DirectDownloadLinkException("ERROR: ouo: empty id segment")

    base = f"{parsed.scheme}://{parsed.netloc}"
    go_url = f"{base}/go/{short_id}"
    final_url = f"{base}/xreallcygo/{short_id}"

    try:
        with cffi_requests.Session(impersonate="chrome136", timeout=30) as s:
            r1 = s.get(normalized, allow_redirects=True)
            if r1.status_code == 403:
                raise DirectDownloadLinkException(
                    "ERROR: ouo.io blocked the request (403)"
                )
            tok1 = _extract_csrf(r1.text)
            if not tok1:
                raise DirectDownloadLinkException(
                    "ERROR: ouo: _token not found on initial page "
                    f"(status={r1.status_code})"
                )

            r2 = s.post(
                go_url,
                data={"_token": tok1, "x-token": "", "v-token": "vm"},
                headers={"Origin": "https://ouo.io", "Referer": normalized},
                allow_redirects=False,
            )
            if r2.status_code == 403:
                raise DirectDownloadLinkException(
                    "ERROR: ouo.io blocked the request (403)"
                )
            if r2.status_code != 200:
                raise DirectDownloadLinkException(
                    f"ERROR: ouo: /go/ status {r2.status_code}"
                )
            tok2 = _extract_csrf(r2.text)
            if not tok2:
                raise DirectDownloadLinkException(
                    "ERROR: ouo: _token not found on /go/ page"
                )

            r3 = s.post(
                final_url,
                data={"_token": tok2, "x-token": ""},
                headers={"Origin": "https://ouo.io", "Referer": go_url},
                allow_redirects=False,
            )
            location = r3.headers.get("Location", "")
            if r3.status_code != 302 or not location:
                raise DirectDownloadLinkException(
                    f"ERROR: ouo: /xreallcygo/ status {r3.status_code} "
                    f"(location={location!r})"
                )
            return location
    except DirectDownloadLinkException:
        raise
    except Exception as e:
        raise DirectDownloadLinkException(f"ERROR: ouo bypass failed: {e}") from e


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


def _retry_delay(attempt, retry_after=None):
    """Seconds to wait before attempt+1, jittered.

    The jitter is the point: a bulk resolves in lockstep, so a fixed sleep only
    reschedules the same burst.
    """
    if retry_after:
        try:
            delay = min(float(retry_after), _LINKVERTISE_MAX_RETRY_DELAY)
            return delay + uniform(0, 1.5)
        except (TypeError, ValueError):
            pass
    return min(2 ** (attempt - 1), _LINKVERTISE_MAX_RETRY_DELAY) + uniform(0, 1.5)


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


def _linkvertise_request(session, api, headers, link):
    """Return (target, reason, retryable, retry_after).

    A gateway hiccup or a rate limit is worth another attempt; a shortlink whose
    content is gone is not — the gateway reports that as 500, so the retry
    decision leans on the error text rather than the status alone.
    """
    try:
        resp = session.get(
            api, params={"url": link}, headers=headers, timeout=_LINKVERTISE_TIMEOUT
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


def _linkvertise(link):
    """Resolve a Linkvertise shortlink via the gateway's bypass endpoint.

    Linkvertise gates the destination behind its own GraphQL flow; the gateway
    walks it and hands back the target URL, so there is nothing to scrape here.
    """
    api, headers = _gateway("/api/v1/bypass/linkvertise")
    reason = "unknown error"

    with Session() as session:
        for attempt in range(1, _LINKVERTISE_ATTEMPTS + 1):
            target, reason, retryable, retry_after = _linkvertise_request(
                session, api, headers, link
            )
            if target:
                return target
            if not retryable or attempt == _LINKVERTISE_ATTEMPTS:
                break
            sleep(_retry_delay(attempt, retry_after))

    raise DirectDownloadLinkException(f"ERROR: linkvertise bypass failed: {reason}")
