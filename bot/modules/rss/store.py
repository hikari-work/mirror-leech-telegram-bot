"""Where RSS state lives: the subscription dict, its lock, and the DB writes.

`rss_dict` itself is a global in `bot/__init__.py` (shape:
`{user_id: {title: {link, last_feed, last_title, inf, exf, paused, command,
sensitive, tag}}}`) and `bot/core/startup.py` fills it from Mongo on boot. This
module owns the *rules* for touching it: take `rss_dict_lock` around every
mutation, then persist the same user in the same call.

The bulk operations below existed four times over as inline
`async with rss_dict_lock:` blocks followed by a `database.*` call, which is
exactly where a missing persist goes unnoticed.
"""

from __future__ import annotations

from asyncio import Lock

from ... import rss_dict
from ...helper.storage.db_handler import database

rss_dict_lock = Lock()

# user_id -> True while a menu is waiting for that user's next message. The
# conversation handler in `menu.event_handler` polls it; every callback action
# that is not itself a prompt sets it False to cancel a pending wait.
handler_dict: dict[int, bool] = {}


def parse_chat_target(chat) -> tuple[int | str | None, int | str | None]:
    """Split a `RSS_CHAT` config value into `(chat_id, topic_id)`.

    Accepted forms: an int, `"-100123"`, or `"-100123|45"` for a forum topic.
    Anything else (`"@channel"`) yields `(None, None)` — that is the
    pre-refactor behaviour of `rss_monitor`, and the menu only ever compares
    the result against `event.chat.id`, where `None` and the raw string are
    equally unequal. Digits are kept as digits on both sides of the `|`.
    """
    if isinstance(chat, int):
        return chat, None
    if not chat:
        return None, None
    if "|" in chat:
        chat_id, topic_id = (_as_id(part) for part in chat.split("|", 1))
        return chat_id, topic_id
    if chat.lstrip("-").isdigit():
        return int(chat), None
    return None, None


def _as_id(value: str) -> int | str:
    return int(value) if value.lstrip("-").isdigit() else value


async def save_user(user_id) -> None:
    """Persist one user's whole subscription set."""
    await database.rss_update(user_id)


async def save_everyone() -> None:
    await database.rss_update_all()


async def drop_user(user_id) -> None:
    """Forget a user completely, in memory and in the DB."""
    async with rss_dict_lock:
        del rss_dict[user_id]
    await database.rss_delete(user_id)


async def drop_everyone() -> None:
    async with rss_dict_lock:
        rss_dict.clear()
    await database.trunc_table("rss")


async def prune_user_if_empty(user_id) -> None:
    """Drop a user whose last subscription just went away.

    Called after `unsubscribe`; the table itself is truncated once the last
    user is gone, so a fresh bot does not read back an empty collection.
    """
    if rss_dict[user_id]:
        return
    async with rss_dict_lock:
        del rss_dict[user_id]
    await database.rss_delete(user_id)
    if not rss_dict:
        await database.trunc_table("rss")


async def set_user_paused(user_id, paused: bool) -> None:
    """Pause or resume every feed of one user."""
    async with rss_dict_lock:
        for info in rss_dict[user_id].values():
            info["paused"] = paused


async def set_everyone_paused(paused: bool) -> None:
    async with rss_dict_lock:
        for user_feeds in rss_dict.values():
            for feed in user_feeds.values():
                feed["paused"] = paused


async def remember_last_item(user_id, title, link, item_title) -> bool:
    """Record the newest item seen for a feed, if the feed still exists.

    The monitor sleeps 10s per item, so a user can unsubscribe while its loop
    is running; writing back blindly would resurrect the subscription. Returns
    False when the feed disappeared — the caller then skips its own logging,
    same as the `continue` this replaced.
    """
    async with rss_dict_lock:
        if user_id not in rss_dict or not rss_dict[user_id].get(title, False):
            return False
        rss_dict[user_id][title].update({"last_feed": link, "last_title": item_title})
    await database.rss_update(user_id)
    return True
