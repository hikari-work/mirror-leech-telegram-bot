"""Tests for the Mega link parser in direct_link_generator.

The parser is deliberately offline: it turns a link into a handle and a key and
nothing else, so the API calls all live in mega_download. What is worth pinning
here is that every link layout Mega has used is recognised, and that a link
missing its key is rejected rather than passed on to fail mid-download.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def mega_module(monkeypatch):
    """Import ``direct_link_generator`` with minimal bot package stubs.

    The real ``bot/__init__.py`` reads env and opens sockets, so the package is
    stubbed the way test_alldebrid_resolver.py does it.
    """
    project_root = Path(__file__).resolve().parent.parent

    bot_pkg = ModuleType("bot")
    bot_pkg.__path__ = []

    class _Logger:
        @staticmethod
        def info(msg):
            pass

    bot_pkg.LOGGER = _Logger()

    core_pkg = ModuleType("bot.core")
    core_pkg.__path__ = []
    config_manager = ModuleType("bot.core.config_manager")

    class Config:
        STREAMWISH_API = ""
        FILELION_API = ""

    config_manager.Config = Config

    helper_pkg = ModuleType("bot.helper")
    helper_pkg.__path__ = []
    ext_utils_pkg = ModuleType("bot.helper.ext_utils")
    ext_utils_pkg.__path__ = []

    exceptions_mod = ModuleType("bot.helper.ext_utils.exceptions")

    class DirectDownloadLinkException(Exception):
        pass

    exceptions_mod.DirectDownloadLinkException = DirectDownloadLinkException

    help_messages_mod = ModuleType("bot.helper.ext_utils.help_messages")
    help_messages_mod.PASSWORD_ERROR_MESSAGE = "{}"

    links_utils_mod = ModuleType("bot.helper.ext_utils.links_utils")
    links_utils_mod.is_share_link = lambda url: False

    status_utils_mod = ModuleType("bot.helper.ext_utils.status_utils")
    status_utils_mod.speed_string_to_bytes = lambda value: 0

    mlu_pkg = ModuleType("bot.helper.mirror_leech_utils")
    mlu_pkg.__path__ = []
    download_utils_pkg = ModuleType("bot.helper.mirror_leech_utils.download_utils")
    download_utils_pkg.__path__ = [
        str(project_root / "bot" / "helper" / "mirror_leech_utils" / "download_utils")
    ]

    # curl_cffi is an optional runtime dependency of the shortener bypass and is
    # not exercised here.
    shortener_mod = ModuleType(
        "bot.helper.mirror_leech_utils.download_utils.url_shortener_bypass"
    )
    shortener_mod.bypass_shortener = lambda url: url
    shortener_mod.is_url_shortener = lambda domain: False

    for name, mod in {
        "bot": bot_pkg,
        "bot.core": core_pkg,
        "bot.core.config_manager": config_manager,
        "bot.helper": helper_pkg,
        "bot.helper.ext_utils": ext_utils_pkg,
        "bot.helper.ext_utils.exceptions": exceptions_mod,
        "bot.helper.ext_utils.help_messages": help_messages_mod,
        "bot.helper.ext_utils.links_utils": links_utils_mod,
        "bot.helper.ext_utils.status_utils": status_utils_mod,
        "bot.helper.mirror_leech_utils": mlu_pkg,
        "bot.helper.mirror_leech_utils.download_utils": download_utils_pkg,
        "bot.helper.mirror_leech_utils.download_utils.url_shortener_bypass": shortener_mod,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)

    sys.modules.pop(
        "bot.helper.mirror_leech_utils.download_utils.direct_link_generator", None
    )
    return importlib.import_module(
        "bot.helper.mirror_leech_utils.download_utils.direct_link_generator"
    )


FOLDER_KEY = "aBcDeFgHiJkLmNoPqRsTuV"
FILE_KEY = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFG"


def test_recognises_mega_hosts(mega_module):
    assert mega_module.is_mega_link("https://mega.nz/folder/AbC#key")
    assert mega_module.is_mega_link("https://MEGA.CO.NZ/file/AbC#key")
    assert mega_module.is_mega_link("https://www.mega.nz/folder/AbC#key")


def test_ignores_lookalike_hosts(mega_module):
    """megaupload.nz is a different, dead host that must not be routed here."""
    assert not mega_module.is_mega_link("https://megaupload.nz/folder/AbC#key")
    assert not mega_module.is_mega_link("https://notmega.nz/folder/AbC#key")


def test_parses_folder_link(mega_module):
    parsed = mega_module.mega(f"https://mega.nz/folder/AbCd1234#{FOLDER_KEY}")
    assert parsed == {
        "mega": {"kind": "folder", "handle": "AbCd1234", "key": FOLDER_KEY}
    }


def test_parses_file_link(mega_module):
    """Single-file links were rejected while the Worker did the decryption."""
    parsed = mega_module.mega(f"https://mega.nz/file/XyZ98765#{FILE_KEY}")
    assert parsed == {"mega": {"kind": "file", "handle": "XyZ98765", "key": FILE_KEY}}


def test_drops_in_share_target_suffix(mega_module):
    """A link pointing inside a share carries "/file/<h>" in the fragment,
    after the key; the whole share is listed either way."""
    parsed = mega_module.mega(
        f"https://mega.nz/folder/AbCd1234#{FOLDER_KEY}/file/InNeR999"
    )
    assert parsed["mega"]["key"] == FOLDER_KEY
    assert parsed["mega"]["handle"] == "AbCd1234"


def test_parses_legacy_folder_link(mega_module):
    parsed = mega_module.mega(f"https://mega.nz/#F!AbCd1234!{FOLDER_KEY}")
    assert parsed == {
        "mega": {"kind": "folder", "handle": "AbCd1234", "key": FOLDER_KEY}
    }


def test_parses_legacy_file_link(mega_module):
    parsed = mega_module.mega(f"https://mega.nz/#!XyZ98765!{FILE_KEY}")
    assert parsed == {"mega": {"kind": "file", "handle": "XyZ98765", "key": FILE_KEY}}


def test_url_encoded_fragment_is_decoded(mega_module):
    """Telegram and shorteners percent-encode the '!' separators."""
    parsed = mega_module.mega(f"https://mega.nz/%23F%21AbCd1234%21{FOLDER_KEY}")
    assert parsed["mega"]["handle"] == "AbCd1234"


@pytest.mark.parametrize(
    "link",
    [
        "https://mega.nz/folder/AbCd1234",  # key missing entirely
        "https://mega.nz/folder/#somekey",  # handle missing
        "https://mega.nz/",  # no handle, no key
        "https://mega.nz/something/AbCd#key",  # not a share layout
    ],
)
def test_rejects_links_without_handle_and_key(mega_module, link):
    """A key-less link cannot be decrypted, so it fails here rather than
    after the task has been queued."""
    with pytest.raises(Exception) as excinfo:
        mega_module.mega(link)
    assert "Unrecognised Mega link" in str(excinfo.value)


def test_generator_routes_mega_links(mega_module):
    """direct_link_generator dispatches on the host before anything else."""
    result = mega_module.direct_link_generator(
        f"https://mega.nz/folder/AbCd1234#{FOLDER_KEY}"
    )
    assert result["mega"]["kind"] == "folder"
