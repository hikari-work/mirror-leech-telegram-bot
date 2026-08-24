"""Direct link generation, dispatched by domain.

The public entry point is ``direct_link_generator(link)``, unchanged from
when this was a single 2500-line module.  Handlers live in ``hosts/`` and
bind themselves to domains via ``@register`` in ``registry.py``.
"""

from urllib.parse import urlparse

# Importing the host package runs every @register decorator.
from . import hosts  # noqa: F401
from ._common import (
    DirectDownloadLinkException,
    bypass_shortener,
    is_url_shortener,
    user_agent,
)

# Re-exported for call sites that import them directly (ytdlp.py) and for
# tests that reach for the handlers by name.
from .hosts.mega import MEGA_DOMAINS, is_mega_link, mega
from .hosts.vidoy import (
    VIDOY_ATTEMPTS,
    VIDOY_DOMAINS,
    VIDOY_HOST,
    VIDOY_MEDIA_EXTS,
    is_vidoy_link,
    vidoy,
    vidoy_headers,
    vidoy_resolve,
    vidoy_sanitize_url,
    vidoy_scrape,
    vidoy_stem,
)
from .hosts.vidara import (
    VIDARA_ATTEMPTS,
    VIDARA_DOMAINS,
    VIDARA_FOLDER_MAX_DEPTH,
    VIDARA_FOLDER_MAX_VIDEOS,
    VIDARA_HOST,
    is_vidara_folder_link,
    is_vidara_link,
    vidara,
    vidara_folder_code,
    vidara_folder_list,
    vidara_folder_scrape,
    vidara_headers,
    vidara_resolve,
    vidara_sanitize_url,
    vidara_scrape,
    vidara_stem,
)
from .registry import register, registered_entries, resolve
from .hosts.bunkr import (
    BUNKR_DOMAINS,
    is_bunkr_link,
    bunkr,
    bunkr_resolve_download,
    bunkr_resolve_many,
)
from .hosts.imgbb import (
    IMGBB_DOMAINS,
    is_imgbb_link,
    imgbb,
)


def direct_link_generator(link):
    """direct links generator"""
    domain = urlparse(link).hostname
    if not domain:
        raise DirectDownloadLinkException("ERROR: Invalid URL")

    if is_url_shortener(domain):
        resolved = bypass_shortener(link)
        try:
            return direct_link_generator(resolved)
        except DirectDownloadLinkException as e:
            if str(e).startswith("ERROR: No Direct link function found"):
                return resolved
            raise

    if handler := resolve(domain, link):
        return handler(link)

    raise DirectDownloadLinkException(f"No Direct link function found for {link}")


__all__ = [
    "direct_link_generator",
    "register",
    "registered_entries",
    "resolve",
    "user_agent",
    "is_mega_link",
    "mega",
    "MEGA_DOMAINS",
    "is_vidoy_link",
    "vidoy",
    "vidoy_resolve",
    "vidoy_sanitize_url",
    "vidoy_scrape",
    "vidoy_headers",
    "vidoy_stem",
    "VIDOY_HOST",
    "VIDOY_DOMAINS",
    "VIDOY_ATTEMPTS",
    "VIDOY_MEDIA_EXTS",
    "is_vidara_link",
    "is_vidara_folder_link",
    "vidara",
    "vidara_resolve",
    "vidara_sanitize_url",
    "vidara_scrape",
    "vidara_folder_code",
    "vidara_folder_list",
    "vidara_folder_scrape",
    "vidara_headers",
    "vidara_stem",
    "VIDARA_HOST",
    "VIDARA_DOMAINS",
    "VIDARA_ATTEMPTS",
    "VIDARA_FOLDER_MAX_DEPTH",
    "VIDARA_FOLDER_MAX_VIDEOS",
    "BUNKR_DOMAINS",
    "is_bunkr_link",
    "bunkr",
    "bunkr_resolve_download",
    "bunkr_resolve_many",
    "IMGBB_DOMAINS",
    "is_imgbb_link",
    "imgbb",
]
