"""Link resolution helpers for torbox, alldebrid and direct-link generators.

Each function follows the same contract:
- Mutates ``listener`` in-place (``listener.link``, torbox/alldebrid IDs).
- On recoverable error, calls ``listener.fail_task(…)`` and returns ``False``.
- On success returns ``True`` so the caller can short-circuit on failure.

Extracted from the inline blocks in ``Leech.new_event()`` to eliminate
the duplicated error-handling triple. What is left for each resolver to decide is
whether a refusal is fatal; ``_fail`` and ``_declined`` hold the two answers, so
the ``ERROR:`` convention is written down once instead of six times.
"""

from __future__ import annotations

from os import path as ospath
from re import match as re_match

from aiofiles import open as aiopen
from aiofiles.os import path as aiopath

from ... import LOGGER
from ..util.bot_utils import get_content_type, sync_to_async
from ..util.exceptions import DirectDownloadLinkException
from ..util.links_utils import is_magnet
from ..util.resolve_gate import resolve_gate


def _link_is_broken(msg: str) -> bool:
    """Whether a resolver is blaming the link rather than passing on it.

    The resolvers signal that in the exception text: an ``ERROR:`` prefix is a
    verdict on the link and worth showing the user, anything else means this
    resolver has nothing to say about it and the caller should carry on with the
    link it already had.
    """
    return msg.startswith("ERROR:")


async def _fail(listener, exc, notify=None) -> None:
    """Log *exc* and end the task over it.

    For the debrid resolvers there is no carrying on -- they were asked for the
    link and could not produce one -- so the prefix only decides whether the user
    hears about it. *notify* overrides that for the resolvers that always report.
    """
    msg = str(exc)
    LOGGER.info(msg)
    await listener.fail_task(
        msg, notify=_link_is_broken(msg) if notify is None else notify
    )


async def _declined(listener, exc, quiet="") -> bool:
    """True when *exc* only means this resolver passed on the link.

    False means the task is already failed and the caller has nothing left to do.
    *quiet* is text that should not reach the log -- a password prompt is a normal
    answer for the direct-link generators, not an incident.
    """
    msg = str(exc)
    if not quiet or quiet not in msg:
        LOGGER.info(msg)
    if _link_is_broken(msg):
        await listener.fail_task(msg)
        return False
    return True


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
        await _fail(listener, e)
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
        if not await _declined(listener, e):
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
        await _fail(listener, e)
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
        if not await _declined(listener, e):
            return False
    except Exception as e:
        await listener.fail_task(e)
        return False
    return True


async def resolve_direct_link(listener, headers: list[str]) -> list[str] | None:
    """Try to resolve ``listener.link`` via ``direct_link_generator``.

    Returns the (possibly updated) *headers* list.  On fatal error calls
    ``listener.fail_task`` and returns ``None`` -- which is what the caller in
    ``leech.py`` tests for, and what the annotation now says.

    The scrape runs behind ``resolve_gate``: a bulk that resolves every link at
    once is what earns the rate limits this used to report as dead links. Only
    the network calls are inside the gate -- ``fail_task`` can sit out a
    FloodWait, and a slot held through that stalls the rest of the batch.
    """
    from .direct_link_generators import direct_link_generator

    try:
        async with resolve_gate():
            content_type = await get_content_type(listener.link)
            if content_type is not None and not re_match(
                r"text/html|text/plain", content_type
            ):
                return headers
            result = await sync_to_async(direct_link_generator, listener.link)
        # unpacking stays inside the try: a generator returning an unexpected
        # shape has to fail this task, not escape into the bulk dispatcher where
        # nothing records it and the batch waits for it forever
        if isinstance(result, tuple):
            listener.link, headers = result
        elif isinstance(result, str):
            listener.link = result
        elif isinstance(result, dict):
            listener.link = result
            LOGGER.info(f"Generated link: {listener.link}")
    except DirectDownloadLinkException as e:
        if not await _declined(listener, e, quiet="This link requires a password!"):
            return None
        return headers
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
        async with resolve_gate():
            if url_type == "video":
                resolved = await sync_to_async(resolve_single_video, identifier)
            else:
                resolved = await sync_to_async(resolve_listing, url_type, identifier)
    except DirectDownloadLinkException as exc:
        # every PornHub failure ends the task, and the user is always told which
        await _fail(listener, exc, notify=True)
        return False
    except Exception as exc:
        await listener.fail_task(exc)
        return False

    listener.link = resolved
    LOGGER.info(f"PornHub resolved: {url_type} {identifier}")
    return True
