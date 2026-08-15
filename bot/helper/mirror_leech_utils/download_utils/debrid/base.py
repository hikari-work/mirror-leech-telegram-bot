"""Machinery shared by the debrid providers (AllDebrid, TorBox).

Both providers do the same three things in different vocabulary:

1. Call a JSON API and turn a failure into ``DirectDownloadLinkException``.
2. Poll an item until the remote side finished fetching it, giving up when
   the user cancels, when the swarm goes quiet, or when it takes too long.
3. Turn every file of a finished item into a direct URL, a few at a time.

Only the skeleton lives here. Everything provider-specific — endpoints,
status vocabulary, error wording — stays in ``alldebrid_resolver.py`` and
``torbox_resolver.py``, and is handed in as arguments.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from httpx import AsyncClient, HTTPError

POLL_INTERVAL_S = 5.0
NO_SEED_TIMEOUT_S = 180.0
MAX_DURATION_S = 7200.0
UNLOCK_CONCURRENCY = 3

StatusFetcher = Callable[[], Awaitable[dict[str, Any]]]
ProgressCallback = Callable[[dict[str, Any]], Awaitable[None]]


def _error(message: str) -> Exception:
    """Build the project's direct-link error.

    The class is looked up per call rather than bound at import time: the
    resolver tests install their own ``bot.helper.ext_utils.exceptions``
    in ``sys.modules`` per test, and a module-level import here would pin
    whichever class happened to be installed first.
    """
    from bot.helper.ext_utils.exceptions import DirectDownloadLinkException

    return DirectDownloadLinkException(message)


def require_key(value: str | None, missing_error: str) -> str:
    """Return the stripped API key, or raise ``missing_error``."""
    if key := (value or "").strip():
        return key
    raise _error(missing_error)


async def request_json(
    method: str,
    url: str,
    *,
    provider: str,
    timeout: float,
    shape_error: str,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    data: Any = None,
    files: Any = None,
) -> dict[str, Any]:
    """Make one API call and return the decoded JSON object.

    Transport and decoding failures become ``DirectDownloadLinkException``
    labelled with *provider*; a payload that is not a JSON object raises
    *shape_error*. Checking the provider's own success/error envelope is
    the caller's job.
    """
    try:
        async with AsyncClient(timeout=timeout, headers=headers) as client:
            request_kwargs: dict[str, Any] = {"params": params or {}}
            if data is not None:
                request_kwargs["data"] = data
            if files is not None:
                request_kwargs["files"] = files
            response = await client.request(method, url, **request_kwargs)
            response.raise_for_status()
            payload = response.json()
    except HTTPError as exc:
        raise _error(f"ERROR: {provider} network error: {exc}") from exc
    except ValueError as exc:
        raise _error(f"ERROR: {provider} returned malformed JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise _error(shape_error)

    return payload


class _StallTimer:
    """How long the transfer has had nothing to pull from.

    The clock starts on the first stalled poll, not on the first poll, and
    resets the moment anything shows up again.
    """

    def __init__(self, timeout: float) -> None:
        self._timeout = timeout
        self._since = 0.0

    def expired(self, stalled: bool, now: float) -> bool:
        if not stalled:
            self._since = 0.0
            return False
        if self._since == 0.0:
            self._since = now
            return False
        return now - self._since >= self._timeout


async def wait_until_ready(
    fetch_status: StatusFetcher,
    *,
    is_ready: Callable[[dict[str, Any]], bool],
    error_message: Callable[[dict[str, Any]], str],
    cancelled_message: str,
    timeout_message: str,
    stall_message: str = "",
    is_stalled: Callable[[dict[str, Any]], bool] | None = None,
    poll_interval: float = POLL_INTERVAL_S,
    no_seed_timeout: float = NO_SEED_TIMEOUT_S,
    max_duration: float = MAX_DURATION_S,
    is_cancelled: Callable[[], bool] | None = None,
    progress_callback: ProgressCallback | None = None,
    progress_payload: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Poll ``fetch_status`` until the item is ready, and return that status.

    Gives up — by raising ``DirectDownloadLinkException`` — when the caller
    cancels, when the provider reports an error (``error_message`` returns a
    non-empty string), when ``is_stalled`` has held for *no_seed_timeout*, or
    when *max_duration* elapses. Pass ``is_stalled=None`` for transfers with
    no swarm to stall on, such as a web download.
    """
    payload = progress_payload or (lambda status: status)
    stall_timer = _StallTimer(no_seed_timeout)
    loop = asyncio.get_event_loop()
    started = loop.time()

    while True:
        if is_cancelled is not None and is_cancelled():
            raise _error(cancelled_message)

        status = await fetch_status()

        if progress_callback is not None:
            await progress_callback(payload(status))

        if is_ready(status):
            return status

        if message := error_message(status):
            raise _error(message)

        now = loop.time()

        if is_stalled is not None and stall_timer.expired(is_stalled(status), now):
            raise _error(stall_message)

        if now - started >= max_duration:
            raise _error(timeout_message)

        await asyncio.sleep(poll_interval)


async def resolve_files_concurrently(
    items: list[Any],
    build_entry: Callable[[int, Any], Awaitable[dict[str, Any] | None]],
    *,
    concurrency: int = UNLOCK_CONCURRENCY,
    ordered: bool = True,
) -> list[dict[str, Any]]:
    """Run ``build_entry`` over ``items``, at most *concurrency* at a time.

    Entries that come back ``None`` are dropped. ``ordered`` keeps the input
    order; without it the result is in completion order, which is what
    TorBox has always handed back.
    """
    semaphore = asyncio.Semaphore(concurrency)
    slots: list[dict[str, Any] | None] = [None] * len(items)
    completed: list[dict[str, Any]] = []

    async def run(index: int, item: Any) -> None:
        async with semaphore:
            entry = await build_entry(index, item)
            if entry is None:
                return
            if ordered:
                slots[index] = entry
            else:
                completed.append(entry)

    await asyncio.gather(*(run(index, item) for index, item in enumerate(items)))

    if not ordered:
        return completed
    return [entry for entry in slots if entry is not None]
