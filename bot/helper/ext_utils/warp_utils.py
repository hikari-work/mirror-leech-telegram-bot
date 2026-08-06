"""Driving the WARP client, so a Mega download can change its egress IP.

Mega meters anonymous downloads per IP: once a share has been pulled hard
enough the API answers -17 for that address and no amount of waiting fixes it.
WARP is what gives us another address - the tunnel is reconnected, and if
Cloudflare hands back the same egress the key-pair is rotated to force a new
one.

Everything here is best-effort. A host without warp-cli, or one where the user
would rather not have the bot touch the tunnel, still downloads - just without
the ability to rotate out of a quota error.
"""

from asyncio import Lock, open_connection, sleep, wait_for
from json import loads
from time import time

from ... import LOGGER
from ...core.config_manager import Config
from .bot_utils import cmd_exec

TRACE_URL = "https://www.cloudflare.com/cdn-cgi/trace"

# How long a just-completed restart counts as "recent". Several files of one
# folder hit quota within moments of each other; without this they would each
# tear the tunnel down again and none would get anywhere.
RESTART_COOLDOWN = 30

# Reconnecting is not instant and a download is blocked while it happens.
CONNECT_TIMEOUT = 45
PROXY_WAIT = 20

_restart_lock = Lock()
_last_restart = 0.0
_last_ip = ""
# Whether the last restart left behind a proxy that actually carries traffic.
# Callers inside the cooldown are told this rather than an unconditional yes.
_last_ok = False


def warp_proxy_url():
    """The proxy Mega traffic should use, or "" to go out directly.

    An explicit MEGA_PROXY_URL wins so a host can point at an existing proxy;
    otherwise WARP's own SOCKS5 listener is used when enabled.
    """
    if url := (Config.MEGA_PROXY_URL or "").strip():
        return url
    if Config.WARP_ENABLED:
        return f"socks5://127.0.0.1:{Config.WARP_PROXY_PORT}"
    return ""


async def _warp(*args, timeout=30):
    """Run warp-cli and return (ok, output). A missing binary is reported the
    same as a failure because the caller's response is identical."""
    try:
        stdout, stderr, code = await wait_for(
            cmd_exec(["warp-cli", "--accept-tos", *args]), timeout
        )
    except FileNotFoundError:
        return False, "warp-cli is not installed"
    except Exception as e:
        return False, f"warp-cli {' '.join(args)} failed: {e}"
    return code == 0, (stdout or stderr or "").strip()


async def _proxy_listening(timeout=2):
    """Whether something accepts connections on the SOCKS port. This is the
    only check that matters - warp-cli can report Connected while the proxy
    listener itself is not up yet."""
    try:
        reader, writer = await wait_for(
            open_connection("127.0.0.1", Config.WARP_PROXY_PORT), timeout
        )
    except Exception:
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    return True


async def _is_connected():
    """Whether the tunnel reports itself up, via warp-cli's JSON status."""
    ok, out = await _warp("-j", "status")
    if not ok:
        return False
    try:
        return (loads(out).get("status") or "").lower().startswith("connected")
    except Exception:
        # Older builds print plain text instead of JSON.
        return "connected" in out.lower()


async def ensure_proxy_mode():
    """Make sure the SOCKS listener is up, switching WARP into proxy mode if
    it is not. Returns True if the proxy is usable.

    This changes the host's tunnel mode, which also affects traffic that has
    nothing to do with the bot - so it only happens when WARP_ENABLED is set,
    and it is left in proxy mode afterwards rather than being switched back
    under a download.
    """
    if not Config.WARP_ENABLED:
        return False
    if await _proxy_listening():
        # The listener staying up says nothing about the tunnel behind it:
        # WARP keeps accepting connections on the SOCKS port while the tunnel
        # is down, and every request sent into it then dies. Falling back to a
        # direct download beats handing the caller a proxy that swallows
        # traffic, which is indistinguishable from Mega refusing us.
        if await current_egress_ip():
            return True
        LOGGER.warning(
            "WARP: the proxy port is open but no traffic gets through it; "
            "the tunnel is probably down"
        )
        return False

    port = str(Config.WARP_PROXY_PORT)
    LOGGER.info(f"WARP: enabling proxy mode on port {port}")

    for args in (("mode", "proxy"), ("proxy", "port", port), ("connect",)):
        ok, out = await _warp(*args)
        # 'connect' on an already-connected tunnel is a no-op error, and the
        # mode switch may report nothing useful; the port check below decides.
        if not ok and args[0] == "mode":
            LOGGER.error(f"WARP: could not switch to proxy mode: {out}")
            return False

    for _ in range(PROXY_WAIT):
        if await _proxy_listening():
            if await current_egress_ip():
                LOGGER.info(f"WARP: proxy listening on 127.0.0.1:{port}")
                return True
            LOGGER.error(f"WARP: proxy on port {port} accepts connections but "
                         "carries no traffic")
            return False
        await sleep(1)

    LOGGER.error(f"WARP: proxy did not come up on port {port}")
    return False


async def current_egress_ip():
    """The IP the world sees, asked through the proxy so it reflects what Mega
    would see. Returns "" if it cannot be determined - that only costs us the
    ability to tell whether a restart actually changed anything.
    """
    from aiohttp import ClientSession, ClientTimeout

    from .aiohttp_helper import proxy_connector

    proxy = warp_proxy_url()
    connector, request_proxy = proxy_connector(proxy)

    try:
        async with ClientSession(
            connector=connector, timeout=ClientTimeout(total=15)
        ) as session, session.get(TRACE_URL, proxy=request_proxy) as resp:
            body = await resp.text()
    except Exception as e:
        LOGGER.debug(f"WARP: egress IP lookup failed: {e}")
        return ""

    for line in body.splitlines():
        if line.startswith("ip="):
            return line[3:].strip()
    return ""


async def _reconnect():
    """Bounce the tunnel and wait for it to report Connected again."""
    await _warp("disconnect")
    await sleep(2)
    await _warp("connect")

    for _ in range(CONNECT_TIMEOUT):
        if await _is_connected():
            return True
        await sleep(1)
    return False


async def restart_warp(force=False):
    """Reconnect WARP to get a different egress IP. Returns True if the tunnel
    came back up and traffic flows through the proxy.

    Serialised and rate-limited: when a folder download hits quota on several
    files at once they all end up here within the same second, and one restart
    answers all of them. Pass force=True to bypass the cooldown.
    """
    global _last_restart, _last_ip, _last_ok

    if not Config.WARP_ENABLED:
        return False

    async with _restart_lock:
        # A restart that another task just finished is this task's restart too.
        if not force and (time() - _last_restart) < RESTART_COOLDOWN:
            LOGGER.info("WARP: reusing the restart that just happened")
            return _last_ok

        before = await current_egress_ip() or _last_ip
        LOGGER.info(f"WARP: restarting the tunnel (egress was {before or 'unknown'})")

        if not await _reconnect():
            LOGGER.error("WARP: the tunnel did not reconnect")
            _last_restart = time()
            _last_ok = False
            return False

        if not await ensure_proxy_mode():
            LOGGER.error("WARP: the tunnel reconnected but the proxy is unusable")
            _last_restart = time()
            _last_ok = False
            return False

        after = await current_egress_ip()

        # Cloudflare often hands back the same egress on a plain reconnect.
        # Rotating the device key-pair is what actually forces a new one.
        if after and before and after == before:
            LOGGER.info("WARP: same egress IP, rotating keys")
            ok, out = await _warp("tunnel", "rotate-keys")
            if not ok:
                LOGGER.warning(f"WARP: key rotation failed: {out}")
            elif await _reconnect():
                if not await ensure_proxy_mode():
                    LOGGER.error("WARP: the tunnel reconnected but the proxy is unusable after key rotation")
                    _last_restart = time()
                    _last_ok = False
                    return False
                after = await current_egress_ip() or after

        _last_restart = time()
        _last_ip = after or before
        _last_ok = True

        if after and before and after == before:
            LOGGER.warning(f"WARP: egress IP is still {after}")
        else:
            LOGGER.info(f"WARP: egress IP is now {after or 'unknown'}")
        return True
