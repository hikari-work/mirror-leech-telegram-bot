"""One gate for the metadata-resolution stage of a task.

``check_running_tasks`` bounds transfers, but it is consulted *after* a task has
already resolved its link: the scrape, the gateway call and the yt-dlp probe all
happen before it. A bulk of a hundred links therefore hit the gateway a hundred
times at once no matter what ``QUEUE_DOWNLOAD`` said, and the gateway answered
what a gateway answers to that -- rate limits, which the resolvers reported as
dead links.

Every pre-queue resolve goes through this semaphore instead, so a large bulk
resolves a few links at a time and stops rate-limiting itself. The limit is read
on each acquire, so changing ``RESOLVE_CONCURRENCY`` from the settings menu takes
effect on the next task without a restart.
"""

from asyncio import Semaphore
from contextlib import asynccontextmanager
from typing import Any

from ...core.config_manager import Config

# The live gate: the limit it was built for, and the semaphore itself. One dict
# rather than two module globals so ``_semaphore`` can swap both without a
# ``global`` statement. Loose values because the two keys hold different things
# and the semaphore only exists after the first acquire.
_gate: dict[str, Any] = {"limit": 0, "sem": None}


def _limit() -> int:
    """Configured slot count; 0 or less means the gate is off."""
    try:
        return max(0, int(Config.RESOLVE_CONCURRENCY or 0))
    except (TypeError, ValueError):
        return 0


def _semaphore(limit: int) -> Semaphore:
    """The semaphore for *limit*, rebuilt when an admin changes the setting.

    Tasks already inside keep the object they acquired -- ``async with`` holds
    the instance, so they release the count they took and the old gate simply
    drains. Only new arrivals queue on the new one.
    """
    if _gate["sem"] is None or _gate["limit"] != limit:
        _gate["limit"] = limit
        _gate["sem"] = Semaphore(limit)
    return _gate["sem"]


def reset_resolve_gate() -> None:
    """Drop the cached semaphore. Used by tests to keep runs hermetic."""
    _gate["limit"] = 0
    _gate["sem"] = None


@asynccontextmanager
async def resolve_gate():
    """Hold one resolve slot for the duration of the block.

    Wrap the scrape and nothing else: a slot held across a queue wait, a
    transfer or a button press is a slot the rest of the batch waits for. Error
    reporting belongs outside too, since ``fail_task`` can sit out a FloodWait.
    """
    limit = _limit()
    if limit <= 0:
        yield
        return
    async with _semaphore(limit):
        yield
