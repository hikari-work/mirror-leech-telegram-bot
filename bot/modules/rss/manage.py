"""Reply handlers that act on existing subscriptions: pause/resume/unsubscribe,
get-items, and delete-user.

None of these redefine the subscription flag grammar (that lives in
`subscribe.py`) — they look feeds up by title and change their state or read
them back. Like every handler here, each is wired up by `listener.py` through
`menu.event_handler`, runs on the user's next message, gets the callback query
as `pre_event`, clears `handler_dict` to end the wait, and redraws the menu.
"""

from __future__ import annotations

from io import BytesIO

from ... import LOGGER, rss_dict
from ...core.config_manager import Config
from ...helper.ext_utils.bot_utils import new_task
from ...helper.telegram_helper.filters import CustomFilters
from ...helper.telegram_helper.message_utils import (
    delete_message,
    edit_message,
    send_file,
    send_message,
)
from . import feed
from .menu import update_rss_menu
from .monitor import resume_or_start
from .store import (
    drop_user,
    handler_dict,
    prune_user_if_empty,
    rss_dict_lock,
    save_user,
)

# ---------------------------------------------------------------------------
# pause / resume / unsubscribe
# ---------------------------------------------------------------------------


async def get_user_id(title):
    # Broken as written: `feeds` is that user's `{title: data}` map, so
    # `feeds["title"]` looks for a subscription literally named "title" and
    # raises KeyError instead. Left alone — a refactor commit is the wrong place
    # to change what a sudo pausing someone else's feed does (Fase 10).
    async with rss_dict_lock:
        return next(
            (
                (True, user_id)
                for user_id, feeds in rss_dict.items()
                if feeds["title"] == title
            ),
            (False, False),
        )


def _drop(feeds, title):
    del feeds[title]


def _pause(feeds, title):
    feeds[title]["paused"] = True


def _resume(feeds, title):
    feeds[title]["paused"] = False


_MUTATIONS = {"unsubscribe": _drop, "pause": _pause, "resume": _resume}

# The `paused` value that makes each request a no-op. `unsubscribe` is absent:
# it is never a no-op. Compared against `bool(paused)`, not against `paused`
# itself, because the old chain tested truthiness (`istate and ...`).
_ALREADY = {"pause": True, "resume": False}


async def _resolve_owner(message, user_id, title, is_sudo):
    """Whose subscription `title` is — the caller's, or anyone's for a sudo."""
    if rss_dict[user_id].get(title, False):
        return user_id
    if is_sudo:
        found, owner = await get_user_id(title)
        if found:
            return owner
    await send_message(message, f"{title} not found!")
    return None


async def _update_one(message, user_id, title, state, is_sudo, updated) -> int:
    """Apply one state change. Returns the user id the next line starts from."""
    owner = await _resolve_owner(message, user_id, title, is_sudo)
    if owner is None:
        return message.from_user.id
    user_id = owner
    already = _ALREADY.get(state)
    if already is not None and bool(rss_dict[user_id][title].get("paused")) == already:
        await send_message(message, f"{title} already {state}d!")
        return user_id
    async with rss_dict_lock:
        updated.append(title)
        _MUTATIONS[state](rss_dict[user_id], title)
    if state == "resume":
        resume_or_start(is_sudo)
    if is_sudo and Config.DATABASE_URL and user_id != message.from_user.id:
        await save_user(user_id)
    await prune_user_if_empty(user_id)
    return user_id


@new_task
async def rss_update(_, message, pre_event, state):
    user_id = message.from_user.id
    handler_dict[user_id] = False
    is_sudo = await CustomFilters.is_sudo(message)
    updated = []
    for raw in message.text.split():
        user_id = await _update_one(
            message, user_id, raw.strip(), state, is_sudo, updated
        )
    if updated:
        LOGGER.info(f"Rss link with Title(s): {updated} has been {state}d!")
        await send_message(
            message,
            f"Rss links with Title(s): <code>{updated}</code> has been {state}d!",
        )
        if rss_dict.get(user_id):
            await save_user(user_id)
    await update_rss_menu(pre_event)


# ---------------------------------------------------------------------------
# get items
# ---------------------------------------------------------------------------


async def _items_text(link, count) -> str:
    rss_d = feed.parse(await feed.fetch_text(link))
    item_info = ""
    for item_num in range(count):
        entry = rss_d.entries[item_num]
        clean = (feed.item_title(entry) or "").replace(">", "").replace("<", "")
        item_info += f"<b>Name: </b><code>{clean}</code>\n"
        item_info += f"<b>Link: </b><code>{feed.item_url(entry)}</code>\n\n"
    return item_info


async def _deliver_items(message, status, title, count, item_info):
    """Edit the status message, or replace it with a file when too long."""
    encoded = item_info.encode()
    if len(encoded) > 4000:
        with BytesIO(encoded) as out_file:
            out_file.name = f"rssGet {title} items_no. {count}.txt"
            await send_file(message, out_file)
        await delete_message(status)
    else:
        await edit_message(status, item_info)


@new_task
async def rss_get(_, message, pre_event):
    user_id = message.from_user.id
    handler_dict[user_id] = False
    args = message.text.split()
    if len(args) < 2:
        await send_message(
            message,
            f"{args}. Wrong Input format. You should add number of the items "
            "you want to get. Read help message before adding new subscription!",
        )
        await update_rss_menu(pre_event)
        return
    try:
        title = args[0]
        count = int(args[1])
        data = rss_dict[user_id].get(title, False)
        if data and count > 0:
            # Sent outside the try: the handlers below edit this message, so a
            # failure to send it has to fall through to the outer handler rather
            # than land in one that needs it.
            status = await send_message(
                message, f"Getting the last <b>{count}</b> item(s) from {title}"
            )
            try:
                item_info = await _items_text(data["link"], count)
                await _deliver_items(message, status, title, count, item_info)
            except IndexError as e:
                LOGGER.error(str(e))
                await edit_message(
                    status, "Parse depth exceeded. Try again with a lower value."
                )
            except Exception as e:
                LOGGER.error(str(e))
                await edit_message(status, str(e))
        else:
            await send_message(message, "Enter a valid title. Title not found!")
    except Exception as e:
        LOGGER.error(str(e))
        await send_message(message, f"Enter a valid value!. {e}")
    await update_rss_menu(pre_event)


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@new_task
async def rss_delete(_, message, pre_event):
    handler_dict[message.from_user.id] = False
    for user in message.text.split():
        await drop_user(int(user))
    await update_rss_menu(pre_event)
