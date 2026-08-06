"""One place that knows how to point aiohttp at a proxy.

aiohttp speaks HTTP proxies natively but has no SOCKS support of its own, and
WARP only offers SOCKS5 - so the two cases need different wiring: a SOCKS URL
becomes a connector, an HTTP one stays a per-request argument. Callers should
not have to care which they were handed.
"""

from ... import LOGGER

_warned = False


def proxy_connector(proxy_url):
    """Return (connector, request_proxy) for a proxy URL, either of which may
    be None. Pass the first to ClientSession(connector=...) and the second to
    each request's proxy= argument.
    """
    global _warned

    proxy_url = (proxy_url or "").strip()
    if not proxy_url:
        return None, None

    if not proxy_url.startswith("socks"):
        return None, proxy_url

    try:
        from aiohttp_socks import ProxyConnector
    except ImportError:
        # Downloading directly is better than not downloading, but it spends
        # the host's own IP against Mega's quota and cannot be rotated.
        if not _warned:
            _warned = True
            LOGGER.error(
                "aiohttp-socks is not installed, so the SOCKS proxy cannot be "
                "used; traffic will go out directly"
            )
        return None, None

    return ProxyConnector.from_url(proxy_url), None
