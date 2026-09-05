"""Pytest fixtures shared across the new test suite."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

# The mltb package expects to be imported with the project root on
# sys.path; make sure that's true regardless of where pytest is run.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture
def bunkr(monkeypatch):
    """Load ``hosts/bunkr.py`` with its package stubbed out.

    Same reason as the ``vidara`` fixture: the real package pulls in the whole
    generator chain, and bunkr only needs LOGGER, the exception and the two
    gateway helpers.
    """
    path = (
        _ROOT
        / "bot" / "helper" / "download" / "direct_link_generators" / "hosts"
        / "bunkr.py"
    )

    pkg = ModuleType("bunkr_stub")
    pkg.__path__ = []
    hosts_pkg = ModuleType("bunkr_stub.hosts")
    hosts_pkg.__path__ = []

    class _Logger:
        @staticmethod
        def info(msg):
            pass

        error = warning = debug = info

    class DirectDownloadLinkException(Exception):
        pass

    common = ModuleType("bunkr_stub._common")
    common.LOGGER = _Logger()
    common.DirectDownloadLinkException = DirectDownloadLinkException
    common.gateway_url = lambda path="": f"https://gateway.test{path}"
    common.gateway_headers = lambda accept_json=True: {"accept": "application/json"}

    registry = ModuleType("bunkr_stub.registry")
    registry.register = lambda **kwargs: (lambda func: func)

    for name, mod in {
        "bunkr_stub": pkg,
        "bunkr_stub.hosts": hosts_pkg,
        "bunkr_stub._common": common,
        "bunkr_stub.registry": registry,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)

    spec = importlib.util.spec_from_file_location("bunkr_stub.hosts.bunkr", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "bunkr_stub.hosts.bunkr", module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def vidara(monkeypatch):
    """Load ``hosts/vidara.py`` with its package stubbed out.

    Importing the real package drags in lxml, cloudscraper and the rest of the
    generator chain; vidara itself only needs LOGGER, Config and the exception.

    Shared by ``test_vidara_retry.py`` (the single-video retry classification)
    and ``test_vidara_folder.py`` (the /f/ listing), which load the same module
    the same way.
    """
    path = (
        _ROOT
        / "bot" / "helper" / "download" / "direct_link_generators" / "hosts"
        / "vidara.py"
    )

    pkg = ModuleType("dlg_stub")
    pkg.__path__ = []
    hosts_pkg = ModuleType("dlg_stub.hosts")
    hosts_pkg.__path__ = []

    class _Logger:
        @staticmethod
        def info(msg):
            pass

        error = warning = debug = info

    class Config:
        GATEWAY_URL = "https://gateway.test"
        GATEWAY_TOKEN = ""

    class DirectDownloadLinkException(Exception):
        pass

    common = ModuleType("dlg_stub._common")
    common.LOGGER = _Logger()
    common.Config = Config
    common.DirectDownloadLinkException = DirectDownloadLinkException
    common.user_agent = "UA"
    # vidara reads the gateway through _common; the tests replace Session, so
    # only the shape of these matters here
    common.gateway_url = lambda path="": f"{Config.GATEWAY_URL}{path}"
    common.gateway_headers = lambda accept_json=True: {"accept": "application/json"}

    registry = ModuleType("dlg_stub.registry")
    registry.register = lambda **kwargs: (lambda func: func)

    for name, mod in {
        "dlg_stub": pkg,
        "dlg_stub.hosts": hosts_pkg,
        "dlg_stub._common": common,
        "dlg_stub.registry": registry,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)

    spec = importlib.util.spec_from_file_location("dlg_stub.hosts.vidara", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "dlg_stub.hosts.vidara", module)
    spec.loader.exec_module(module)
    return module
