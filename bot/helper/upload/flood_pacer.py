"""How hard the uploader is allowed to push telegram.

Two pieces of state -- the gap currently left between files, and how many
files have gone by without a complaint -- used to live on ``TelegramUploader``
alongside the twenty-odd attributes of an upload in progress, even though
nothing else in the class read or wrote them. They are the whole of this
object instead, so the policy can be exercised without building an upload.

The pacer does not know what a task is: it takes a callable that answers
whether the work has been cancelled, which is the only reason waiting out a
flood limit ever had to reach for the listener.
"""

from asyncio import sleep
from collections.abc import Callable
from logging import getLogger

from pyrogram.errors import FloodPremiumWait, FloodWait

from ..telegram.flood import flood_seconds

LOGGER = getLogger(__name__)

# Telegram's own wait, plus a margin: asking again the instant it expires tends
# to earn a second, longer FloodWait.
FLOOD_SLACK = 1.3


class FloodPacer:
    """The gap left between two files, widened by floods and decayed by calm."""

    # How wide that gap is allowed to grow, and how many flood-free files it
    # takes to start closing it again.
    _MAX_PACE = 4.0
    _CALM_FILES = 5

    def __init__(self, is_cancelled: Callable[[], bool]):
        self._is_cancelled = is_cancelled
        self._pace = 0.0
        self._calm = 0

    def note_flood(self):
        """Telegram complained, so widen the gap we leave between files."""
        self._pace = min(self._MAX_PACE, self._pace * 2 or 0.5)
        self._calm = 0

    async def pace(self):
        """Wait between two files, but only as long as telegram asked for.

        A flat second per file used to be paid unconditionally: invisible next
        to one big upload, and pure overhead across a few hundred small ones. So
        start with no gap and let a FloodWait be what introduces one, then let
        it decay once telegram stops complaining.
        """
        if not self._pace:
            return
        await sleep(self._pace)
        self._calm += 1
        if self._calm >= self._CALM_FILES:
            self._calm = 0
            self._pace = self._pace / 2 if self._pace > 0.5 else 0.0

    async def guard(self, func, *args, **kwargs):
        """Run a telegram call, waiting out any flood limit instead of failing.

        Telegram answers with FloodWait on almost any call once the account is
        rate limited, including the very first message of an upload. Those are
        transient, so wait the requested time and try again rather than killing
        the task. Returns None if the task gets canceled while waiting.

        The per-file senders deliberately do not come through here: their flood
        wait is caught further out so the thumbnail outlives the sleep. Both
        paths share ``note_flood`` and nothing else.
        """
        while True:
            if self._is_cancelled():
                return None
            try:
                return await func(*args, **kwargs)
            except (FloodWait, FloodPremiumWait) as f:
                name = getattr(func, "__name__", str(func))
                LOGGER.warning(f"Rate limited on {name}: waiting {f.value}s. {f}")
                self.note_flood()
                await sleep(flood_seconds(f) * FLOOD_SLACK)
