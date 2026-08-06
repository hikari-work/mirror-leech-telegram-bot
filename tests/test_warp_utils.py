"""Tests for the WARP tunnel control used to escape Mega's per-IP quota.

The behaviour worth pinning is the escalation: a plain reconnect often comes
back on the same egress IP, and if nothing noticed that, a download would
retry into the same quota error until it ran out of attempts. So the module
checks, and only then rotates the key-pair.

warp-cli is never actually run here - cmd_exec and the IP probe are replaced,
so the tests assert on which commands *would* have been issued.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from types import ModuleType

import pytest


@pytest.fixture
def warp(monkeypatch):
    """Import warp_utils with the bot package stubbed out, then reset its
    module-level restart state so tests do not leak into each other."""
    bot_pkg = ModuleType("bot")
    bot_pkg.__path__ = []

    class _Logger:
        @staticmethod
        def info(msg):
            pass

        error = warning = debug = info

    bot_pkg.LOGGER = _Logger()

    core_pkg = ModuleType("bot.core")
    core_pkg.__path__ = []
    config_manager = ModuleType("bot.core.config_manager")

    class Config:
        WARP_ENABLED = True
        WARP_PROXY_PORT = 40000
        MEGA_PROXY_URL = ""

    config_manager.Config = Config

    helper_pkg = ModuleType("bot.helper")
    helper_pkg.__path__ = []
    ext_utils_pkg = ModuleType("bot.helper.ext_utils")
    ext_utils_pkg.__path__ = [
        str(__import__("pathlib").Path(__file__).resolve().parent.parent
            / "bot" / "helper" / "ext_utils")
    ]

    bot_utils_mod = ModuleType("bot.helper.ext_utils.bot_utils")

    async def _unset(cmd, shell=False):
        raise AssertionError("cmd_exec should be replaced by the test")

    bot_utils_mod.cmd_exec = _unset

    for name, mod in {
        "bot": bot_pkg,
        "bot.core": core_pkg,
        "bot.core.config_manager": config_manager,
        "bot.helper": helper_pkg,
        "bot.helper.ext_utils": ext_utils_pkg,
        "bot.helper.ext_utils.bot_utils": bot_utils_mod,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)

    sys.modules.pop("bot.helper.ext_utils.warp_utils", None)
    module = importlib.import_module("bot.helper.ext_utils.warp_utils")

    module.Config = Config
    module._last_restart = 0.0
    module._last_ip = ""
    monkeypatch.setattr(module, "sleep", _no_sleep)
    return module


async def _no_sleep(seconds):
    """Reconnecting waits seconds at a time; nothing here needs to."""


class _Warp:
    """Records the warp-cli invocations and serves a scripted sequence of
    egress IPs, one per lookup."""

    def __init__(self, warp, monkeypatch, ips):
        self.commands = []
        self._ips = list(ips)
        self.reconnects = 0

        async def cmd_exec(cmd, shell=False):
            args = [a for a in cmd[1:] if a != "--accept-tos"]
            self.commands.append(args)
            if args[:2] == ["-j", "status"]:
                return '{"status": "Connected"}', "", 0
            if args == ["connect"]:
                self.reconnects += 1
            return "", "", 0

        async def egress():
            return self._ips.pop(0) if self._ips else ""

        async def listening(timeout=2):
            return True

        async def ensure():
            # Stubbed so the scripted IP list means what it says: the real
            # ensure_proxy_mode() validates the tunnel with its own egress
            # lookup, which is tested separately and would otherwise consume
            # an observation these rotation tests have accounted for.
            return True

        monkeypatch.setattr(warp, "cmd_exec", cmd_exec)
        monkeypatch.setattr(warp, "current_egress_ip", egress)
        monkeypatch.setattr(warp, "_proxy_listening", listening)
        monkeypatch.setattr(warp, "ensure_proxy_mode", ensure)

    def ran(self, *args):
        return list(args) in self.commands


def test_proxy_url_prefers_explicit_override(warp):
    """A host with its own proxy should be able to say so."""
    warp.Config.MEGA_PROXY_URL = "socks5://10.0.0.2:1080"
    assert warp.warp_proxy_url() == "socks5://10.0.0.2:1080"


def test_proxy_url_falls_back_to_warp(warp):
    warp.Config.MEGA_PROXY_URL = ""
    assert warp.warp_proxy_url() == "socks5://127.0.0.1:40000"


def test_proxy_url_is_empty_when_disabled(warp):
    """With WARP off the traffic goes out directly, which the downloader
    warns about rather than failing over."""
    warp.Config.MEGA_PROXY_URL = ""
    warp.Config.WARP_ENABLED = False
    assert warp.warp_proxy_url() == ""


async def test_restart_reconnects_and_reports_new_ip(warp, monkeypatch):
    fake = _Warp(warp, monkeypatch, ["1.1.1.1", "2.2.2.2"])

    assert await warp.restart_warp() is True
    assert fake.ran("disconnect") and fake.ran("connect")
    # A different IP came back, so there is no reason to rotate keys.
    assert not fake.ran("tunnel", "rotate-keys")


async def test_restart_rotates_keys_when_the_ip_is_unchanged(warp, monkeypatch):
    """The whole point: Cloudflare frequently hands back the same egress, and
    retrying into the same quota error would be pointless."""
    fake = _Warp(warp, monkeypatch, ["1.1.1.1", "1.1.1.1", "3.3.3.3"])

    assert await warp.restart_warp() is True
    assert fake.ran("tunnel", "rotate-keys")
    assert fake.reconnects == 2


async def test_restart_does_not_delete_the_registration(warp, monkeypatch):
    """Re-registering would work but drops the WARP+ license attached to it."""
    fake = _Warp(warp, monkeypatch, ["1.1.1.1", "1.1.1.1", "1.1.1.1"])

    await warp.restart_warp()

    assert not any(c and c[0] == "registration" for c in fake.commands)


async def test_concurrent_restarts_collapse_into_one(warp, monkeypatch):
    """Every file of a folder hits quota within the same moment. Without the
    cooldown they would each tear the tunnel down again and none would get
    anywhere."""
    fake = _Warp(warp, monkeypatch, ["1.1.1.1", "2.2.2.2"])

    results = await asyncio.gather(*(warp.restart_warp() for _ in range(4)))

    assert all(results)
    assert fake.reconnects == 1


async def test_force_bypasses_the_cooldown(warp, monkeypatch):
    fake = _Warp(warp, monkeypatch, ["1.1.1.1", "2.2.2.2", "2.2.2.2", "4.4.4.4"])

    await warp.restart_warp()
    await warp.restart_warp(force=True)

    assert fake.reconnects >= 2


async def test_restart_is_a_no_op_when_warp_is_disabled(warp, monkeypatch):
    """A host that opted out should never have its tunnel touched."""
    fake = _Warp(warp, monkeypatch, ["1.1.1.1"])
    warp.Config.WARP_ENABLED = False

    assert await warp.restart_warp() is False
    assert fake.commands == []


async def test_restart_reports_failure_when_the_tunnel_stays_down(warp, monkeypatch):
    """The downloader needs to hear this, or it would retry against a tunnel
    that is not there."""
    fake = _Warp(warp, monkeypatch, ["1.1.1.1"])

    async def never_connected():
        return False

    monkeypatch.setattr(warp, "_is_connected", never_connected)
    monkeypatch.setattr(warp, "CONNECT_TIMEOUT", 2)

    assert await warp.restart_warp() is False


async def test_proxy_mode_rejects_a_listener_that_carries_no_traffic(warp, monkeypatch):
    """The bug that made every Mega file fail: WARP keeps the SOCKS listener
    up while the tunnel behind it is down, so a port check alone reports a
    working proxy and every request sent into it dies. Reporting failure lets
    the caller fall back to a direct download instead."""
    async def listening(timeout=2):
        return True

    async def no_egress():
        return ""

    monkeypatch.setattr(warp, "_proxy_listening", listening)
    monkeypatch.setattr(warp, "current_egress_ip", no_egress)

    assert await warp.ensure_proxy_mode() is False


async def test_proxy_mode_accepts_a_listener_that_reaches_the_internet(warp, monkeypatch):
    """The other half of the same check: an open port that does carry traffic
    is exactly what the caller is asking about."""
    async def listening(timeout=2):
        return True

    async def egress():
        return "1.1.1.1"

    monkeypatch.setattr(warp, "_proxy_listening", listening)
    monkeypatch.setattr(warp, "current_egress_ip", egress)

    assert await warp.ensure_proxy_mode() is True
