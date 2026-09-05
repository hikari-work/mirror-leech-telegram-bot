from __future__ import annotations

from uvloop import install

install()
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from asyncio import Lock, Task, new_event_loop, set_event_loop
from logging import (
    getLogger,
    FileHandler,
    StreamHandler,
    INFO,
    basicConfig,
    WARNING,
    ERROR,
)
from time import time
from os import cpu_count
from typing import TYPE_CHECKING, Literal, TypedDict

if TYPE_CHECKING:
    # For ``_Intervals`` only; importing it for real would be a cycle, since
    # everything under ``bot.helper`` imports back from here.
    from .helper.ext_utils.bot_utils import SetInterval

getLogger("requests").setLevel(WARNING)
getLogger("urllib3").setLevel(WARNING)
getLogger("pyrogram").setLevel(ERROR)
getLogger("httpx").setLevel(WARNING)
getLogger("aiohttp").setLevel(WARNING)

bot_start_time = time()

bot_loop = new_event_loop()
set_event_loop(bot_loop)

basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[FileHandler("log.txt"), StreamHandler()],
    level=INFO,
)

LOGGER = getLogger(__name__)
# `cpu_count()` returns None when it cannot tell, which used to make the `//`
# below raise TypeError at import. Falling back to 1 is what the `max(1, ...)`
# already intended.
cpu_no = cpu_count() or 1
threads = max(1, cpu_no // 2)
cores = ",".join(str(i) for i in reversed(range(threads)))

DOWNLOAD_DIR = "/app/downloads/"


class _Intervals(TypedDict):
    """The background tickers, in one dict because one flag stops all of them.

    ``qb`` and the values of ``status`` are the running tasks themselves, so a
    handler can cancel them. The empty string is the "no ticker" placeholder --
    what the dict starts with and what the qbittorrent listener puts back when
    its loop exits -- and it stays a string rather than becoming None because
    every reader only tests it for truth.
    """

    status: dict[int, SetInterval]
    """Keyed by the chat (or user) id whose status message it refreshes."""
    qb: Task | Literal[""]
    stopAll: bool
    """Set once on the way to a restart; the listeners check it and stop."""


intervals: _Intervals = {"status": {}, "qb": "", "stopAll": False}
qb_torrents = {}
user_data = {}
user_clients = {}
aria2_options = {}
qbit_options = {}
queued_dl = {}
queued_up = {}
status_dict = {}
task_dict = {}
rss_dict = {}
auth_chats = {}
excluded_extensions = ["aria2", "!qB"]
included_extensions = []
sudo_users = []
non_queued_dl = set()
non_queued_up = set()
upload_chat_of = {}
multi_tags = set()
multi_batches = {}
task_dict_lock = Lock()
queue_dict_lock = Lock()
qb_listener_lock = Lock()
cpu_eater_lock = Lock()
same_directory_lock = Lock()

scheduler = AsyncIOScheduler(event_loop=bot_loop)
