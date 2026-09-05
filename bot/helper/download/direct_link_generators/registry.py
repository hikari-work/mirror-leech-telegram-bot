"""Ordered handler registry for direct link generation.

This replaces the 200-line if/elif chain the old module dispatched on. The
chain's semantics are preserved literally, which is why entries carry an
explicit ``order`` rather than relying on a dict or on import order:

* Matching is **substring against the hostname** (``"racaty" in domain``),
  not exact or suffix match. Several old entries were bare substrings
  ("racaty", "devuploads", "uploadhaven") with no TLD at all.
* Because matching is by substring, one hostname can satisfy several
  branches at once -- ``racaty.mediafire.com`` contains both "racaty" and
  "mediafire.com". The chain returned whichever branch it reached first, so
  the ordering *is* behaviour, not incidental.
* Consecutive branches of the chain now live in different modules, and the
  chain interleaved them (mediafire at #8, racaty at #15, fichier at #16).
  No import order can reproduce that, so each handler declares its own
  position with ``order=`` and the registry sorts on it.
* One check looked at the whole **link** instead of the hostname, and the
  predicate checks (vidoy, mega, share links) sat *between* domain checks
  rather than after all of them -- an entry therefore carries its own
  matcher.

``tests/test_direct_link_registry.py`` pins every domain to its handler, and
``test_dispatch_order_matches_the_old_chain`` pins the sequence itself.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple


class _Entry(NamedTuple):
    order: int
    domains: tuple[str, ...]
    predicate: Callable | None
    handler: Callable

    def matches(self, domain: str, link: str) -> bool:
        if any(d in domain for d in self.domains):
            return True
        return bool(self.predicate and self.predicate(link))


_HANDLERS: list[_Entry] = []
_sorted_cache: list[_Entry] | None = None

# R.I.P dead services -- data, not branches. Checked last, exactly as the old
# chain did, so a live handler always wins over the tombstone.
_DEAD_DOMAINS: tuple[str, ...] = (
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
)


def register(*domains: str, order: int, predicate: Callable | None = None):
    """Bind *domains* (substring-matched against the hostname) and/or a
    *predicate* (called with the full link) to a handler.

    *order* is the handler's 1-based position in the old if/elif chain and is
    mandatory: a hostname can match several handlers, and this is what decides
    which one answers. Passing both a domain list and a predicate means either
    one matching is enough -- ``yandex_disk`` needs that, since the old chain
    tested ``"yadi.sk" in link``.
    """

    def decorator(func: Callable) -> Callable:
        global _sorted_cache
        _HANDLERS.append(_Entry(order, domains, predicate, func))
        _sorted_cache = None
        return func

    return decorator


def _ordered() -> list[_Entry]:
    global _sorted_cache
    if _sorted_cache is None:
        _sorted_cache = sorted(_HANDLERS, key=lambda e: e.order)
    return _sorted_cache


def resolve(domain: str, link: str) -> Callable | None:
    """Return the handler for *domain* / *link*, or ``None`` when nothing
    claims it. Walks the chain in order; first match wins."""
    for entry in _ordered():
        if entry.matches(domain, link):
            return entry.handler

    if any(dead in domain for dead in _DEAD_DOMAINS):
        return _dead_handler

    return None


def _dead_handler(link: str):
    """Stand-in for the services the old chain listed only to reject."""
    from urllib.parse import urlparse

    from ._common import DirectDownloadLinkException

    raise DirectDownloadLinkException(f"ERROR: R.I.P {urlparse(link).hostname}")


def registered_entries() -> list[_Entry]:
    """Snapshot of the registry, in dispatch order. For tests."""
    return list(_ordered())
