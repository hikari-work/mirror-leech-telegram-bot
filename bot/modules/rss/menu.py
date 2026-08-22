"""The RSS menu: what the user sees, and the wait-for-one-message helper.

Nothing here decides anything — `listener.py` routes button presses and
`subscribe.py`/`manage.py` handle the replies. Keeping the view below the router
is what lets a command handler end with `update_rss_menu(...)` without importing
the router that called it.
"""

from __future__ import annotations

from functools import partial

from ... import rss_dict, scheduler
from ...core.config_manager import Config
from ...helper.ext_utils.bot_utils import new_task
from ...helper.telegram_helper.button_build import ButtonMaker
from ...helper.telegram_helper.conversation import wait_for_message
from ...helper.telegram_helper.filters import CustomFilters
from ...helper.telegram_helper.message_utils import edit_message, send_message
from .store import handler_dict, parse_chat_target, rss_dict_lock

# (label, callback action) for the root menu, in button order.
_USER_BUTTONS = (
    ("Subscribe", "sub"),
    ("Subscriptions", "list"),
    ("Get Items", "get"),
    ("Edit", "edit"),
    ("Pause", "pause"),
    ("Resume", "resume"),
    ("Unsubscribe", "unsubscribe"),
)

_SUDO_BUTTONS = (
    ("All Subscriptions", "listall"),
    ("Pause All", "allpause"),
    ("Resume All", "allresume"),
    ("Unsubscribe All", "allunsub"),
    ("Delete User", "deluser"),
    ("Use This Chat", "setchat"),
)

# `list`/`listall` take a page number; the rest ignore the extra field.
_PAGED = {"list", "listall"}


def _root_buttons(user_id, is_sudo: bool):
    buttons = ButtonMaker()
    rows = list(_USER_BUTTONS)
    if is_sudo:
        rows += list(_SUDO_BUTTONS)
        rows.append(
            ("Shutdown Rss", "shutdown")
            if scheduler.running
            else ("Start Rss", "start")
        )
    rows.append(("Close", "close"))
    for label, action in rows:
        page = " 0" if action in _PAGED else ""
        buttons.data_button(label, f"rss {action} {user_id}{page}")
    return buttons.build_menu(2)


def _chat_display(event) -> str:
    """`RSS_CHAT` as shown in the menu header."""
    chat = Config.RSS_CHAT
    if not chat:
        return "<b>Not Set!</b>"
    rss_id, _ = parse_chat_target(chat)
    # A callback query has `.message`; a command has `.chat` directly.
    event_chat = getattr(event, "chat", None) or event.message.chat
    if event_chat.id == rss_id:
        return "This Chat"
    return f"<code>{chat}</code>"


async def rss_menu(event):
    """The root menu: header text plus button matrix."""
    button = _root_buttons(event.from_user.id, await CustomFilters.sudo("", event))
    msg = (
        f"Rss Menu | Users: {len(rss_dict)} | Running: {scheduler.running}\n"
        f"RSS Chat: {_chat_display(event)}"
    )
    return msg, button


async def update_rss_menu(query):
    msg, button = await rss_menu(query)
    await edit_message(query.message, msg, button)


@new_task
async def get_rss_menu(_, message):
    msg, button = await rss_menu(message)
    await send_message(message, msg, button)


def nav_buttons(user_id, extra: tuple[str, str] | None = None):
    """Back / Close, with an optional bulk-action button wedged between them."""
    buttons = ButtonMaker()
    buttons.data_button("Back", f"rss back {user_id}")
    if extra:
        label, action = extra
        buttons.data_button(label, f"rss {action} {user_id}")
    buttons.data_button("Close", f"rss close {user_id}")
    return buttons.build_menu(2)


def _feed_body(data) -> str:
    """The five lines both subscription listings share."""
    return (
        f"<b>Command:</b> <code>{data['command']}</code>\n"
        f"<b>Inf:</b> <code>{data['inf']}</code>\n"
        f"<b>Exf:</b> <code>{data['exf']}</code>\n"
        f"<b>Sensitive:</b> <code>{data.get('sensitive', False)}</code>\n"
        f"<b>Paused:</b> <code>{data['paused']}</code>\n"
    )


def _all_users_listing(start: int) -> tuple[str, int]:
    list_feed = f"<b>All subscriptions | Page: {int(start / 5)} </b>"
    keys_count = sum(len(v.keys()) for v in rss_dict.values())
    # Up to five per user, taken from each user's own slice — this pages every
    # user in lockstep rather than paging one flat list. Pre-existing.
    for titles in rss_dict.values():
        for index, (title, data) in enumerate(list(titles.items())[start : 5 + start]):
            list_feed += f"\n\n<b>Title:</b> <code>{title}</code>\n"
            list_feed += f"<b>Feed Url:</b> <code>{data['link']}</code>\n"
            list_feed += _feed_body(data)
            list_feed += f"<b>User:</b> {data['tag'].replace('@', '', 1)}"
            if index + 1 == 5:
                break
    return list_feed, keys_count


def _own_listing(user_id, start: int) -> tuple[str, int]:
    list_feed = f"<b>Your subscriptions | Page: {int(start / 5)} </b>"
    keys_count = len(rss_dict.get(user_id, {}).keys())
    for title, data in list(rss_dict[user_id].items())[start : 5 + start]:
        list_feed += f"\n\n<b>Title:</b> <code>{title}</code>\n"
        list_feed += f"<b>Feed Url: </b><code>{data['link']}</code>\n"
        list_feed += _feed_body(data)
    return list_feed, keys_count


async def rss_list(query, start, all_users=False):
    user_id = query.from_user.id
    async with rss_dict_lock:
        if all_users:
            list_feed, keys_count = _all_users_listing(start)
        else:
            list_feed, keys_count = _own_listing(user_id, start)
    buttons = ButtonMaker()
    buttons.data_button("Back", f"rss back {user_id}")
    buttons.data_button("Close", f"rss close {user_id}")
    if keys_count > 5:
        for x in range(0, keys_count, 5):
            buttons.data_button(
                f"{int(x / 5)}", f"rss list {user_id} {x}", position="footer"
            )
    button = buttons.build_menu(2)
    if query.message.text.html == list_feed:
        return
    await edit_message(query.message, list_feed, button)


async def event_handler(client, query, pfunc):
    """Wait up to 60s for one message from this user in this chat.

    Registered in group -1 so it wins over the normal command handlers, and
    torn down by whatever sets `handler_dict[user_id] = False` — either the
    reply handler itself or the next button press.
    """
    await wait_for_message(
        client,
        query,
        pfunc,
        handler_dict,
        query.from_user.id,
        on_timeout=partial(update_rss_menu, query),
    )
