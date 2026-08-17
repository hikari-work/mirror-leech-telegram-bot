"""One place that knows the proxy pool.

The pool used to be a static string an admin typed into ``Config.MEGA_PROXY_URL``.
It now lives on the gateway (``GET /api/v1/proxy``), so the bot fetches the
current rotation and only falls back to the config string, then a hardcoded
list, when the gateway is unreachable. The result is cached for a few minutes so
the hot path (one call per download segment) never touches the network.
"""

from time import time

from aiohttp import ClientSession, ClientTimeout
from requests import get as _requests_get

from ... import LOGGER
from ...core.config_manager import Config

# Final fallback when both the gateway and Config.MEGA_PROXY_URL are empty.
_DEFAULT_PROXIES = [
    f"https://proxy-{n}.vianstefani754.workers.dev" for n in range(1, 6)
]

# The pool rarely changes; a short TTL keeps a burst of tasks on one fetch while
# still picking up gateway changes within a few minutes.
_TTL = 300

_cache = {"proxies": None, "ts": 0.0}


def _config_proxies():
    """Parse Config.MEGA_PROXY_URL into a list of bare proxy base URLs."""
    raw = getattr(Config, "MEGA_PROXY_URL", "")
    if isinstance(raw, (list, tuple)):
        return [str(p).strip() for p in raw if str(p).strip()]
    if isinstance(raw, str) and raw.strip():
        return [p for p in raw.replace(",", " ").split() if p]
    return []


def _fallback():
    return _config_proxies() or list(_DEFAULT_PROXIES)


def _parse(body):
    """Extract the proxy list from the gateway response body.

    Shape: ``{"success": true, "data": ["https://...", ...], "count": N}``.
    Returns de-duplicated, stripped, non-empty entries; ``[]`` on anything else.
    """
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        return []
    seen = set()
    proxies = []
    for item in data:
        url = str(item).strip()
        if url and url not in seen:
            seen.add(url)
            proxies.append(url)
    return proxies


def _proxy_endpoint():
    base = (getattr(Config, "GATEWAY_URL", "") or "https://api.piyann.me").rstrip("/")
    return f"{base}/api/v1/proxy"


def _gateway_headers():
    headers = {"accept": "application/json"}
    if token := getattr(Config, "GATEWAY_TOKEN", ""):
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _store(proxies):
    _cache["proxies"] = proxies
    _cache["ts"] = time()
    return proxies


def _is_fresh():
    return _cache["proxies"] is not None and (time() - _cache["ts"]) < _TTL


def get_proxy_pool():
    """Return the current pool from cache, or the fallback.

    This is the hot path (called per segment): pure in-memory, never blocks,
    never raises.
    """
    return _cache["proxies"] or _fallback()


def reset_proxy_pool():
    """Clear the cache. Used by tests to keep runs hermetic."""
    _cache["proxies"] = None
    _cache["ts"] = 0.0


def refresh_proxy_pool_sync(force=False):
    """Refresh the pool from the gateway with a blocking request (terabox path).

    Returns the pool either way; gateway errors log a warning and fall back.
    """
    if not force and _is_fresh():
        return _cache["proxies"]
    try:
        resp = _requests_get(_proxy_endpoint(), headers=_gateway_headers(), timeout=10)
        resp.raise_for_status()
        proxies = _parse(resp.json())
        if proxies:
            return _store(proxies)
        LOGGER.warning("proxy pool: gateway returned no proxies, using fallback")
    except Exception as e:
        LOGGER.warning(f"proxy pool: gateway fetch failed ({e}), using fallback")
    return get_proxy_pool()


async def refresh_proxy_pool(session=None, force=False):
    """Async refresh from the gateway (mega path).

    Reuses ``session`` when given, otherwise opens a short-lived one. Returns the
    pool either way; gateway errors log a warning and fall back.
    """
    if not force and _is_fresh():
        return _cache["proxies"]
    own = session is None
    try:
        if own:
            session = ClientSession(timeout=ClientTimeout(total=15))
        async with session.get(_proxy_endpoint(), headers=_gateway_headers()) as resp:
            resp.raise_for_status()
            body = await resp.json(content_type=None)
        proxies = _parse(body)
        if proxies:
            return _store(proxies)
        LOGGER.warning("proxy pool: gateway returned no proxies, using fallback")
    except Exception as e:
        LOGGER.warning(f"proxy pool: gateway fetch failed ({e}), using fallback")
    finally:
        if own and session is not None:
            await session.close()
    return get_proxy_pool()
