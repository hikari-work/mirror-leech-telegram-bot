"""One place that knows the gateway.

Half a dozen scrapers, the shortlink bypassers, the proxy pool and the Mega
client all read api.piyann.me, and each of them used to spell out the same two
things: the base URL, with its config fallback and its trailing slash trimmed,
and the bearer token when one is set. Ten copies is ten places to edit when the
gateway moves, and they had already drifted on whether to ask for JSON at all.
"""

from ...core.config_manager import Config

GATEWAY_FALLBACK = "https://api.piyann.me"


def gateway_url(path=""):
    """The gateway base with *path* appended, e.g. "/api/v1/scrape/vidara"."""
    base = (getattr(Config, "GATEWAY_URL", "") or GATEWAY_FALLBACK).rstrip("/")
    return f"{base}{path}"


def gateway_headers(accept_json=True):
    """Request headers for the gateway: the bearer token when one is configured.

    ``accept_json=False`` is for the endpoints that have never been asked for
    JSON explicitly -- they answer with it regardless, and sending the header now
    would change a request that works.
    """
    headers = {"accept": "application/json"} if accept_json else {}
    if token := getattr(Config, "GATEWAY_TOKEN", ""):
        headers["Authorization"] = f"Bearer {token}"
    return headers
