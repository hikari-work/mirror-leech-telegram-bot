"""Link resolution helpers for torbox, alldebrid and direct-link generators.

Each function follows the same contract:
- Mutates ``listener`` in-place (``listener.link``, torbox/alldebrid IDs).
- On recoverable error, calls ``listener.fail_task(…)`` and returns ``False``.
- On success returns ``True`` so the caller can short-circuit on failure.

Extracted from the inline blocks in ``Leech.new_event()`` to eliminate
the duplicated error-handling triple.
"""

from __future__ import annotations

from os import path as ospath
from re import match as re_match

from aiofiles import open as aiopen
from aiofiles.os import path as aiopath

from .... import LOGGER
from ...ext_utils.bot_utils import get_content_type, sync_to_async
from ...ext_utils.exceptions import DirectDownloadLinkException
from ...ext_utils.links_utils import is_magnet


async def resolve_torbox_torrent(listener) -> bool:
    """Resolve magnet / .torrent via TorBox.  Returns *True* on success."""
    from .torbox_resolver import (
        torbox_resolve_magnet,
        torbox_resolve_torrent,
    )

    try:
        if is_magnet(listener.link):
            resolved = await torbox_resolve_magnet(
                listener.link,
                is_cancelled=lambda: listener.is_cancelled,
            )
            listener._torbox_torrent_id = resolved.get("torbox_torrent_id", 0)
            listener.link = resolved

        elif (
            isinstance(listener.link, str)
            and listener.link.endswith(".torrent")
            and await aiopath.exists(listener.link)
        ):
            async with aiopen(listener.link, "rb") as f:
                torrent_bytes = await f.read()

            resolved = await torbox_resolve_torrent(
                torrent_bytes,
                ospath.basename(listener.link),
                is_cancelled=lambda: listener.is_cancelled,
            )
            listener._torbox_torrent_id = resolved.get("torbox_torrent_id", 0)
            listener.link = resolved

    except DirectDownloadLinkException as e:
        msg = str(e)
        LOGGER.info(msg)
        notify = msg.startswith("ERROR:")
        await listener.fail_task(msg, notify=notify)
        return False
    except Exception as e:
        await listener.fail_task(e)
        return False

    return True


async def resolve_alldebrid_torrent(listener) -> bool:
    """Resolve magnet / .torrent via AllDebrid.  Returns *True* on success."""
    from .alldebrid_resolver import (
        alldebrid_resolve_magnet,
        alldebrid_resolve_torrent,
    )

    if not (is_magnet(listener.link) or listener.link.endswith(".torrent")):
        return True

    try:
        if is_magnet(listener.link):
            LOGGER.info("AllDebrid magnet route")
            resolved = await alldebrid_resolve_magnet(
                listener.link,
                is_cancelled=lambda: listener.is_cancelled,
            )
        else:
            LOGGER.info(f"AllDebrid torrent file route: {listener.link}")
            async with aiopen(listener.link, "rb") as fh:
                torrent_bytes = await fh.read()
            resolved = await alldebrid_resolve_torrent(
                torrent_bytes,
                ospath.basename(listener.link),
                is_cancelled=lambda: listener.is_cancelled,
            )
    except DirectDownloadLinkException as e:
        msg = str(e)
        LOGGER.info(msg)
        if msg.startswith("ERROR:"):
            await listener.fail_task(msg)
            return False
        resolved = None
    except Exception as e:
        await listener.fail_task(e)
        return False

    if isinstance(resolved, dict):
        listener._alldebrid_magnet_id = resolved.get("magnet_id", 0)
        listener.link = resolved
        listener.is_qbit = False

    return True


async def resolve_torbox_web(listener) -> bool:
    """Resolve a web link via TorBox.  Returns *True* on success."""
    from .torbox_resolver import torbox_resolve

    try:
        resolved = await torbox_resolve(
            listener.link,
            is_cancelled=lambda: listener.is_cancelled,
        )
        listener._torbox_web_id = resolved.get("torbox_web_id", 0)
        listener.link = resolved
    except DirectDownloadLinkException as e:
        msg = str(e)
        LOGGER.info(msg)
        notify = msg.startswith("ERROR:")
        await listener.fail_task(msg, notify=notify)
        return False
    except Exception as e:
        await listener.fail_task(e)
        return False
    return True


async def resolve_alldebrid_web(listener) -> bool:
    """Resolve a web link via AllDebrid.  Returns *True* on success."""
    from .alldebrid_resolver import alldebrid_resolve

    try:
        resolved = await alldebrid_resolve(listener.link)
        if isinstance(resolved, str):
            listener.link = resolved
            LOGGER.info(f"AllDebrid link: {listener.link}")
        else:
            listener.link = resolved
    except DirectDownloadLinkException as e:
        msg = str(e)
        LOGGER.info(msg)
        if msg.startswith("ERROR:"):
            await listener.fail_task(msg)
            return False
    except Exception as e:
        await listener.fail_task(e)
        return False
    return True


async def resolve_direct_link(listener, headers: list[str]) -> list[str]:
    """Try to resolve ``listener.link`` via ``direct_link_generator``.

    Returns the (possibly updated) *headers* list.  On fatal error calls
    ``listener.fail_task`` and returns ``None``.
    """
    from .direct_link_generator import direct_link_generator

    content_type = await get_content_type(listener.link)
    if content_type is not None and not re_match(
        r"text/html|text/plain", content_type
    ):
        return headers

    try:
        result = await sync_to_async(direct_link_generator, listener.link)
        if isinstance(result, tuple):
            listener.link, headers = result
        elif isinstance(result, str):
            listener.link = result
        elif isinstance(result, dict):
            listener.link = result
            LOGGER.info(f"Generated link: {listener.link}")
    except DirectDownloadLinkException as e:
        e = str(e)
        if "This link requires a password!" not in e:
            LOGGER.info(e)
        if e.startswith("ERROR:"):
            await listener.fail_task(e)
            return None
    except Exception as e:
        await listener.fail_task(e)
        return None

    return headers


async def resolve_pornhub(listener) -> bool:
    """Resolve a PornHub URL (video or listing) into a pornhub link dict.

    Returns *True* on success (or if the link is not PornHub).
    """
    from .pornhub_scraper import (
        is_pornhub_link,
        parse_pornhub_url,
        resolve_listing,
        resolve_single_video,
    )

    if not isinstance(listener.link, str) or not is_pornhub_link(listener.link):
        return True

    parsed = parse_pornhub_url(listener.link)
    if not parsed:
        return True

    url_type, identifier = parsed
    try:
        if url_type == "video":
            resolved = await sync_to_async(resolve_single_video, identifier)
        else:
            resolved = await sync_to_async(resolve_listing, url_type, identifier)
        listener.link = resolved
        LOGGER.info(f"PornHub resolved: {url_type} {identifier}")
    except DirectDownloadLinkException as exc:
        msg = str(exc)
        LOGGER.info(msg)
        await listener.fail_task(msg)
        return False
    except Exception as exc:
        await listener.fail_task(exc)
        return False
    return True
