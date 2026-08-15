"""RSS subscriptions: menu, commands, and the scheduled monitor.

Was one 961-line module (Fase 9). The split follows the direction the code
already flowed in, so nothing imports upwards:

    store / feed / download_bridge   state, HTTP + entry reading, RSS -> download
      -> menu                        what the user sees, and the reply wait
        -> subscribe / manage        handlers for the replies
          -> listener                the callback router
    monitor                          the scheduled sweep (store + feed + bridge)

Importing this package starts the RSS job, exactly as importing the old module
did — `bot/modules/__init__.py` pulls it in, so the monitor is running before
`add_handlers()` is called. The job is a no-op until `RSS_CHAT` is set (it shuts
itself down) or a subscription exists (it pauses itself).
"""

from __future__ import annotations

from ... import scheduler
from .listener import rss_listener
from .menu import get_rss_menu, rss_menu, update_rss_menu
from .monitor import add_job, resume_or_start, rss_monitor

__all__ = [
    "add_job",
    "get_rss_menu",
    "resume_or_start",
    "rss_listener",
    "rss_menu",
    "rss_monitor",
    "update_rss_menu",
]

add_job()
scheduler.start()
