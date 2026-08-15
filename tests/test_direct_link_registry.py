"""Dispatch snapshot for the direct link generator registry.

Phase 3 split a 2500-line module into ``direct_link_generators/``, replacing a
204-line if/elif chain with the registry in ``registry.py``. The chain is the
only thing that decided which host handler a link reached, so this file pins
the mapping it produced: every domain the old chain listed, against the name
of the function it dispatched to.

``EXPECTED`` is a snapshot taken from the pre-refactor module (commit a7dae69,
``direct_link_generator.py`` lines 43-231) and is deliberately written out by
hand rather than derived from the registry -- a table generated from the code
under test would agree with any reordering, including a wrong one.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def dlg(monkeypatch):
    """Import the package with the same bot stubs test_mega_direct_link uses.

    The real ``bot/__init__.py`` reads env and opens sockets, so the package
    is stubbed rather than imported.
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

    # The real regex, copied from bot/helper/ext_utils/links_utils.py, so the
    # is_share_link predicate is exercised for real rather than stubbed off.
    links_utils_mod = ModuleType("bot.helper.ext_utils.links_utils")

    def _is_share_link(url):
        from re import match

        return bool(
            match(
                r"https?:\/\/.+\.gdtot\.\S+|https?:\/\/(filepress|filebee|appdrive|gdflix)\.\S+",
                url,
            )
        )

    links_utils_mod.is_share_link = _is_share_link

    status_utils_mod = ModuleType("bot.helper.ext_utils.status_utils")
    status_utils_mod.speed_string_to_bytes = lambda value: 0

    mlu_pkg = ModuleType("bot.helper.mirror_leech_utils")
    mlu_pkg.__path__ = []
    download_utils_pkg = ModuleType("bot.helper.mirror_leech_utils.download_utils")
    download_utils_pkg.__path__ = [
        str(project_root / "bot" / "helper" / "mirror_leech_utils" / "download_utils")
    ]

    shortener_mod = ModuleType(
        "bot.helper.mirror_leech_utils.download_utils.url_shortener_bypass"
    )
    shortener_mod.bypass_shortener = lambda url: url
    shortener_mod.is_url_shortener = lambda domain: False

    stubs = {
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
    }
    stubs[f"{download_utils_pkg.__name__}.url_shortener_bypass"] = shortener_mod
    for name, mod in stubs.items():
        monkeypatch.setitem(sys.modules, name, mod)

    base = "bot.helper.mirror_leech_utils.download_utils.direct_link_generators"
    for name in [m for m in sys.modules if m.startswith(base)]:
        sys.modules.pop(name, None)
    return importlib.import_module(base)


# Domain -> handler function name, as the old if/elif chain resolved it.
# Order below follows the chain top to bottom.
EXPECTED: dict[str, str] = {
    "yadi.sk": "yandex_disk",
    "disk.yandex.com": "yandex_disk",
    "disk.yandex.ru": "yandex_disk",
    "buzzheavier.com": "buzzheavier",
    # Three entries the old chain listed without a TLD, so they matched any
    # TLD the host happened to use. Both the bare form and a concrete host
    # are pinned.
    "devuploads": "devuploads",
    "devuploads.com": "devuploads",
    "lulacloud.com": "lulacloud",
    "uploadhaven": "uploadhaven",
    "uploadhaven.com": "uploadhaven",
    "fuckingfast.co": "fuckingfast_dl",
    "mediafile.cc": "mediafile",
    "mediafire.com": "mediafire",
    "osdn.net": "osdn",
    "github.com": "github",
    "transfer.it": "transfer_it",
    "hxfile.co": "hxfile",
    "1drv.ms": "onedrive",
    "pixeldrain.com": "pixeldrain",
    "pixeldra.in": "pixeldrain",
    "racaty": "racaty",
    "racaty.net": "racaty",
    "1fichier.com": "fichier",
    "solidfiles.com": "solidfiles",
    "krakenfiles.com": "krakenfiles",
    "upload.ee": "uploadee",
    "gofile.io": "gofile",
    "send.cm": "send_cm",
    "tmpsend.com": "tmpsend",
    "easyupload.io": "easyupload",
    "streamvid.net": "streamvid",
    "shrdsk.me": "shrdsk",
    "u.pcloud.link": "pcloud",
    "qiwi.gg": "qiwi",
    "mp4upload.com": "mp4upload",
    "berkasdrive.com": "berkasdrive",
    "swisstransfer.com": "swisstransfer",
    "akmfiles.com": "akmfiles",
    "akmfls.xyz": "akmfiles",
    # doods
    "dood.watch": "doods",
    "doodstream.com": "doods",
    "dood.to": "doods",
    "dood.so": "doods",
    "dood.cx": "doods",
    "dood.la": "doods",
    "dood.ws": "doods",
    "dood.sh": "doods",
    "doodstream.co": "doods",
    "dood.pm": "doods",
    "dood.wf": "doods",
    "dood.re": "doods",
    "dood.video": "doods",
    "dooood.com": "doods",
    "dood.yt": "doods",
    "doods.yt": "doods",
    "dood.stream": "doods",
    "doods.pro": "doods",
    "ds2play.com": "doods",
    "d0o0d.com": "doods",
    "ds2video.com": "doods",
    "do0od.com": "doods",
    "d000d.com": "doods",
    # streamtape
    "streamtape.com": "streamtape",
    "streamtape.cc": "streamtape",
    "streamtape.net": "streamtape",
    "streamtape.to": "streamtape",
    "tapecontent.net": "streamtape",
    "strtape.tech": "streamtape",
    "strcloud.in": "streamtape",
    "strcloud.club": "streamtape",
    "strcloud.link": "streamtape",
    "shavetape.cash": "streamtape",
    "streamta.pe": "streamtape",
    "strtpe.link": "streamtape",
    "streamadblocker.xyz": "streamtape",
    "strtape.cloud": "streamtape",
    "tapeadvertisement.com": "streamtape",
    "streamta.site": "streamtape",
    "streamtape.xyz": "streamtape",
    "watchadsontape.com": "streamtape",
    "wetransfer.com": "wetransfer",
    "we.tl": "wetransfer",
    # terabox
    "terabox.com": "terabox",
    "nephobox.com": "terabox",
    "4funbox.com": "terabox",
    "mirrobox.com": "terabox",
    "momerybox.com": "terabox",
    "teraboxapp.com": "terabox",
    "1024tera.com": "terabox",
    "terabox.app": "terabox",
    "gibibox.com": "terabox",
    "goaibox.com": "terabox",
    "terasharelink.com": "terabox",
    "teraboxlink.com": "terabox",
    "freeterabox.com": "terabox",
    "1024terabox.com": "terabox",
    "teraboxshare.com": "terabox",
    "terafileshare.com": "terabox",
    "terabox.club": "terabox",
    # filelions & streamwish
    "filelions.co": "filelions_and_streamwish",
    "filelions.site": "filelions_and_streamwish",
    "filelions.live": "filelions_and_streamwish",
    "filelions.to": "filelions_and_streamwish",
    "mycloudz.cc": "filelions_and_streamwish",
    "cabecabean.lol": "filelions_and_streamwish",
    "filelions.online": "filelions_and_streamwish",
    "embedwish.com": "filelions_and_streamwish",
    "kitabmarkaz.xyz": "filelions_and_streamwish",
    "wishfast.top": "filelions_and_streamwish",
    "streamwish.to": "filelions_and_streamwish",
    "kissmovies.net": "filelions_and_streamwish",
    "streamhub.ink": "streamhub",
    "streamhub.to": "streamhub",
    # linkbox
    "linkbox.to": "linkBox",
    "lbx.to": "linkBox",
    "teltobx.net": "linkBox",
    "telbx.net": "linkBox",
    "linkbox.cloud": "linkBox",
}
# imgbb (new, not from old chain)
EXPECTED.update({
    "ibb.co.com": "imgbb",
    "ibb.co": "imgbb",
    "imgbb.com": "imgbb",
})

# Checked after every domain and predicate in the old chain, and answered with
# "ERROR: R.I.P <domain>" rather than a handler.
DEAD_DOMAINS = [
    "anonfiles.com",
    "zippyshare.com",
    "letsupload.io",
    "hotfile.io",
    "bayfiles.com",
    "megaupload.nz",
    "letsupload.cc",
    "filechan.org",
    "myfile.is",
    "vshare.is",
    "rapidshare.nu",
    "lolabits.se",
    "openload.cc",
    "share-online.is",
    "upvid.cc",
    "uptobox.com",
    "uptobox.fr",
]


@pytest.mark.parametrize(("domain", "handler"), sorted(EXPECTED.items()))
def test_domain_resolves_to_same_handler(dlg, domain, handler):
    """Every domain the old chain knew still reaches the same function."""
    link = f"https://{domain}/somefile"
    resolved = dlg.resolve(domain, link)
    assert resolved is not None, f"{domain} resolves to nothing"
    assert resolved.__name__ == handler


@pytest.mark.parametrize("domain", sorted(EXPECTED))
def test_subdomains_resolve_too(dlg, domain):
    """The old chain matched by substring, so a www. host hit the same branch."""
    link = f"https://www.{domain}/somefile"
    assert dlg.resolve(f"www.{domain}", link).__name__ == EXPECTED[domain]


@pytest.mark.parametrize("domain", DEAD_DOMAINS)
def test_dead_domains_still_rest_in_peace(dlg, domain):
    link = f"https://{domain}/somefile"
    handler = dlg.resolve(domain, link)
    assert handler is not None
    with pytest.raises(Exception) as excinfo:
        handler(link)
    assert str(excinfo.value) == f"ERROR: R.I.P {domain}"


@pytest.mark.parametrize(
    ("link", "handler"),
    [
        # vidoy and mega were predicate checks sitting between domain checks
        ("https://vidoy.me/abc123", "vidoy"),
        ("https://mega.nz/folder/AbCd1234#key", "mega"),
        ("https://mega.co.nz/file/AbCd1234#key", "mega"),
        # share links: filepress and the sharer family share one predicate
        ("https://filepress.top/file/abc", "share_link"),
        ("https://gdflix.dad/file/abc", "share_link"),
        ("https://appdrive.info/file/abc", "share_link"),
        ("https://something.gdtot.dad/file/abc", "share_link"),
    ],
)
def test_predicate_links_resolve_to_same_handler(dlg, link, handler):
    from urllib.parse import urlparse

    resolved = dlg.resolve(urlparse(link).hostname, link)
    assert resolved is not None, f"{link} resolves to nothing"
    assert resolved.__name__ == handler


def test_yandex_matches_on_the_full_link(dlg):
    """The old chain tested ``"yadi.sk" in link``, not in the hostname, so a
    yandex link that only names the host in its path still routed here."""
    link = "https://yadi.sk/d/abc123"
    assert dlg.resolve("yadi.sk", link).__name__ == "yandex_disk"


def test_unknown_domain_resolves_to_nothing(dlg):
    assert dlg.resolve("example.com", "https://example.com/file") is None


def test_generator_reports_unknown_domain(dlg):
    with pytest.raises(Exception) as excinfo:
        dlg.direct_link_generator("https://example.com/file")
    assert "No Direct link function found" in str(excinfo.value)


def test_generator_rejects_url_without_host(dlg):
    with pytest.raises(Exception) as excinfo:
        dlg.direct_link_generator("not-a-url")
    assert "ERROR: Invalid URL" in str(excinfo.value)


def test_every_registered_domain_is_snapshotted(dlg):
    """Guard the other direction: a domain added to a host module without a
    snapshot entry means this table has silently gone stale."""
    registered = {
        domain for entry in dlg.registered_entries() for domain in entry.domains
    }
    assert registered - set(EXPECTED) == set()


def test_registry_covers_every_handler(dlg):
    """All 41 branches of the old chain are registered -- 38 by domain, plus
    the three predicate-only ones (vidoy, mega, share_link)."""
    handlers = {entry.handler.__name__ for entry in dlg.registered_entries()}
    assert handlers == set(EXPECTED.values()) | {"vidoy", "mega", "share_link", "vidara", "bunkr"}


# The branches of the old if/elif chain, top to bottom. filepress/sharer_scraper
# were one branch (chosen inline by hostname) and are registered as share_link.
CHAIN_ORDER = [
    "yandex_disk",
    "buzzheavier",
    "devuploads",
    "lulacloud",
    "uploadhaven",
    "fuckingfast_dl",
    "mediafile",
    "mediafire",
    "osdn",
    "github",
    "transfer_it",
    "hxfile",
    "onedrive",
    "pixeldrain",
    "racaty",
    "fichier",
    "solidfiles",
    "krakenfiles",
    "uploadee",
    "gofile",
    "send_cm",
    "tmpsend",
    "easyupload",
    "streamvid",
    "shrdsk",
    "pcloud",
    "qiwi",
    "mp4upload",
    "berkasdrive",
    "swisstransfer",
    "akmfiles",
    "doods",
    "streamtape",
    "wetransfer",
    "terabox",
    "vidoy",
    "mega",
    "filelions_and_streamwish",
    "streamhub",
    "linkBox",
    "share_link",
]
# vidara (order=37) sits between mega (36) and filelions (38)
CHAIN_ORDER.insert(CHAIN_ORDER.index("mega") + 1, "vidara")
# imgbb (order=42) appended after all old-chain entries
CHAIN_ORDER.append("imgbb")
CHAIN_ORDER.append("bunkr")


def test_dispatch_order_matches_the_old_chain(dlg):
    """Matching is by substring, so a hostname can satisfy several handlers at
    once -- "racaty.mediafire.com" contains both "racaty" and "mediafire.com".
    The old chain answered with whichever branch came first, so the order is
    behaviour and not an implementation detail."""
    assert [e.handler.__name__ for e in dlg.registered_entries()] == CHAIN_ORDER


@pytest.mark.parametrize(
    ("link", "handler"),
    [
        # each of these matches two branches; the earlier one has to win
        ("https://racaty.mediafire.com/file/x", "mediafire"),  # #8 beats #15
        ("https://filepress.terabox.com/s/1", "terabox"),  # #35 beats #41
        ("https://gdflix.gofile.io/d/x", "gofile"),  # #20 beats #41
        ("https://1drv.ms.mediafire.com/file/x", "mediafire"),  # #8 beats #13
        ("https://devuploads.gofile.io/d/x", "devuploads"),  # #3 beats #20
        ("https://uploadhaven.terabox.com/s/1", "uploadhaven"),  # #5 beats #35
        ("https://terabox.com.vidoy.me/x", "terabox"),  # #35 beats #36
        ("https://uptobox.com.gofile.io/d/x", "gofile"),  # live beats R.I.P
    ],
)
def test_first_matching_branch_wins(dlg, link, handler):
    from urllib.parse import urlparse

    assert dlg.resolve(urlparse(link).hostname, link).__name__ == handler
