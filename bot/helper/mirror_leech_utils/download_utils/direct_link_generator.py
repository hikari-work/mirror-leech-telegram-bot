"""Backwards-compatible shim.

The implementation moved to the ``direct_link_generators`` package in Phase 3
of the refactor. This module stays so existing imports keep working:

    from .direct_link_generator import direct_link_generator
    from ...direct_link_generator import is_vidoy_link, vidoy_resolve

New code should import from ``direct_link_generators`` directly.
"""

from .direct_link_generators import *  # noqa: F401,F403
from .direct_link_generators import (  # noqa: F401
    direct_link_generator,
    is_mega_link,
    is_vidoy_link,
    mega,
    user_agent,
    vidoy,
    vidoy_headers,
    vidoy_resolve,
    vidoy_sanitize_url,
    vidoy_scrape,
    vidoy_stem,
)
from .direct_link_generators import (  # noqa: F401
    is_bunkr_link,
    bunkr,
    BUNKR_DOMAINS,
)
