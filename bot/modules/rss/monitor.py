"""The scheduled sweep: fetch every subscription, ship whatever is new.

`rss_monitor` used to be one 158-line function with a `for`, a `for`, a retry
`while`, and an item `while` nested four deep. The shape below keeps that
control flow exactly — including which failure `continue`s the next feed and
which one abandons the whole user — but each level is now its own function.

The 10-second `sleep` between items is not politeness: it is the only place the
sweep can be interrupted. APScheduler shuts the job down by cancelling the task,
so the sleep is where the cancellation lands, and it is turned into
`RssShutdownException` to unwind one feed at a time.
"""

from __future__ import annotations

from asyncio import sleep
from datetime import datetime, timedelta
from typing import NamedTuple

from apscheduler.triggers.interval import IntervalTrigger

from ... import LOGGER, rss_dict, scheduler
from ...core.config_manager import Config
from ...helper.ext_utils.exceptions import RssShutdownException
from ...helper.ext_utils.status_utils import get_readable_file_size
from ...helper.telegram_helper.message_utils import send_rss
from . import feed, store
from .download_bridge import start_rss_download


class _Head(NamedTuple):
    """The newest entry of a feed, plus the entry list to walk backwards."""

    entries: list
    url: str | None
    title: str | None


class _Item(NamedTuple):
    title: str
    url: str
    size: int


class _Sweep:
    """One run's shared state.

    `active` flips as soon as any feed answers with entries — even if shipping
    its items then fails — and a sweep where nothing answered pauses the
    scheduler until a menu action wakes it up again.
    """

    def __init__(self):
        self.active = False


def add_job():
    scheduler.add_job(
        rss_monitor,
        trigger=IntervalTrigger(seconds=Config.RSS_DELAY),
        id="0",
        name="RSS",
        misfire_grace_time=15,
        max_instances=1,
        next_run_time=datetime.now() + timedelta(seconds=20),
        replace_existing=True,
    )


def resume_or_start(allowed: bool = True) -> bool:
    """Wake the scheduler up after a feed became active.

    State 2 is APScheduler's `STATE_PAUSED`: the job is still there, so a
    resume is enough. A scheduler that was shut down has to be rebuilt, and
    only a sudo may do that (a normal user resuming their own feed must not
    restart a monitor an admin stopped). Returns True if it was rebuilt.
    """
    if scheduler.state == 2:
        scheduler.resume()
    elif allowed and not scheduler.running:
        add_job()
        scheduler.start()
        return True
    return False


def _feed_message(item: _Item, tag, user) -> str:
    clean = item.title.replace(">", "").replace("<", "")
    msg = f"<b>Name: </b><code>{clean}</code>"
    msg += f"\n\n<b>Link: </b><code>{item.url}</code>"
    if item.size:
        msg += f"\n<b>Size: </b>{get_readable_file_size(item.size)}"
    msg += f"\n<b>Tag: </b><code>{tag}</code> <code>{user}</code>"
    return msg


async def _latest(data) -> _Head | None:
    """Fetch a feed (3 retries) and read its newest entry. None if empty."""
    rss_d = feed.parse(await feed.fetch_text(data["link"], retries=3))
    if not rss_d.entries:
        return None
    entry0 = rss_d.entries[0]
    return _Head(rss_d.entries, feed.latest_url(entry0), entry0.get("title"))


async def _dispatch_item(user, data, target, item: _Item) -> None:
    """Start a download for an item, or just announce it in RSS_CHAT."""
    rss_chat_id, rss_topic_id = target
    if command := data["command"]:
        if item.size and Config.RSS_SIZE_LIMIT and Config.RSS_SIZE_LIMIT < item.size:
            return
        await start_rss_download(
            url=item.url,
            command=command,
            user_id=user,
            rss_chat_id=rss_chat_id,
            rss_topic_id=rss_topic_id,
            item_title=item.title,
        )
        return
    await send_rss(_feed_message(item, data["tag"], user), rss_chat_id, rss_topic_id)


async def _ship_new_items(user, title, data, head: _Head, target) -> None:
    """Walk the feed newest-first until the item we shipped last time."""
    feed_count = 0
    while True:
        try:
            await sleep(10)
        except BaseException:  # cancellation is the expected way out
            raise RssShutdownException("Rss Monitor Stopped!")
        try:
            entry = head.entries[feed_count]
            item_title = entry["title"]
            url = feed.item_url(entry)
            if data["last_feed"] == url or data["last_title"] == item_title:
                break
            size = feed.item_size(entry)
        except IndexError:
            LOGGER.warning(
                f"Reached Max index no. {feed_count} for this feed: {title}. "
                "Maybe you need to use less RSS_DELAY to not miss some torrents"
            )
            break
        if not feed.item_blocked(item_title, data):
            await _dispatch_item(user, data, target, _Item(item_title, url, size))
        feed_count += 1


async def _check_feed(sweep: _Sweep, user, title, data, target) -> None:
    if data["paused"]:
        return
    head = await _latest(data)
    if head is None:
        LOGGER.warning(
            f"No entries found for > Feed Title: {title} - Feed Link: {data['link']}"
        )
        return
    sweep.active = True
    if data["last_feed"] == head.url or data["last_title"] == head.title:
        return
    await _ship_new_items(user, title, data, head, target)
    if await store.remember_last_item(user, title, head.url, head.title):
        LOGGER.info(f"Feed Name: {title}")
        LOGGER.info(f"Last item: {head.url}")


async def rss_monitor():
    chat = Config.RSS_CHAT
    if not chat:
        LOGGER.warning("RSS_CHAT not added! Shutting down rss scheduler...")
        scheduler.shutdown(wait=False)
        return
    if len(rss_dict) == 0:
        scheduler.pause()
        return
    target = store.parse_chat_target(chat)
    sweep = _Sweep()
    for user, items in list(rss_dict.items()):
        for title, data in items.items():
            try:
                await _check_feed(sweep, user, title, data, target)
            except RssShutdownException as ex:
                LOGGER.info(ex)
                break
            except Exception as e:
                LOGGER.error(f"{e} - Feed Name: {title} - Feed Link: {data['link']}")
                continue
    if not sweep.active:
        scheduler.pause()
