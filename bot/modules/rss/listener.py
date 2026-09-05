"""Routing the RSS menu's buttons: one table, one function per action.

Callback data is `rss <action> <target_user_id> [page]`. The router used to be a
200-line `if/elif` chain in which three separate conventions were invisible:
which actions cancel a pending prompt, which ones refuse to run on an empty
subscription list (some check the *target's* feeds, some check whether anyone at
all has feeds), and which ones answer the callback themselves because they have
something to say. Those three are now fields on `_Action`.

`uall*` and `all*` stay prefix-matched rather than becoming six exact keys, so
that unrecognised data ending in neither `unsub`, `pause` nor `resume` keeps
landing in the same place it used to.
"""

from __future__ import annotations

from asyncio import sleep
from collections.abc import Callable
from functools import partial
from typing import NamedTuple

from pyrogram import Client
from pyrogram.types import CallbackQuery, Message

from ... import rss_dict, scheduler
from ...core.config_manager import Config
from ...helper.storage.db_handler import database
from ...helper.telegram.filters import CustomFilters
from ...helper.telegram.message_utils import (
    chat_of,
    delete_message,
    edit_message,
)
from ...helper.util.bot_utils import new_task
from ...helper.util.help_messages import RSS_HELP_MESSAGE
from . import store
from .manage import rss_delete, rss_get, rss_update
from .menu import event_handler, nav_buttons, rss_list, update_rss_menu
from .monitor import add_job, resume_or_start
from .subscribe import rss_edit, rss_sub


class _Ctx(NamedTuple):
    """A parsed callback query. `target` is whose feeds the action is about."""

    client: Client
    query: CallbackQuery
    # `query.message`, which pyrogram leaves optional for a callback on a
    # message too old to be in its cache. Every button here comes from a menu
    # the bot has open, so it is there; `rss_listener` is where that is
    # assumed, and the actions below read it freely.
    message: Message
    user_id: int
    action: str
    target: int
    data: list[str]


class _Action(NamedTuple):
    run: Callable
    # When the prompt this user may be replying to gets cancelled: "first"
    # before the gate below, "late" after the callback is answered, None never.
    #
    # The old chain was not uniform about this, and the difference is not
    # cosmetic: `close`, `back` and `sub` answered the query *before* clearing,
    # so an `answer()` that raises (an expired callback does) leaves the pending
    # prompt alive, while every gated action had already cleared it by then.
    # Hence a field rather than one tidy order for all of them.
    wait: str | None = "first"
    # "user": the target must have feeds. "any": somebody must. None: no gate.
    needs: str | None = None
    # False when the action answers the callback query itself.
    answer: bool = True


_GET_PROMPT = (
    "Send one title with value separated by space get last X items.\n"
    "Title Value\nTimeout: 60 sec."
)

_DELUSER_PROMPT = (
    "Send one or more user_id separated by space to delete their resources.\n"
    "Timeout: 60 sec."
)

# Trailing whitespace included: this was a triple-quoted literal indented
# inside the old if/elif chain, and the user sees what it sends.
_EDIT_PROMPT = (
    "Send one or more rss titles with new filters or command separated by new line.\n"
    "Examples:\n"
    "Title1 -c mirror -up remote:path/subdir -exf none -inf 1080 or 720 -stv true\n"
    "Title2 -c none -inf none -stv false\n"
    "Title3 -c mirror -rcf xxx -up xxx -z pswd -stv false\n"
    "Note: Only what you provide will be edited, the rest will be the same "
    "like example 2: exf will stay same as it is.\n"
    "Timeout: 60 sec. Argument -c for command and arguments\n"
    "            "
)

# action -> (prompt text, the handler that reads the reply)
_PROMPTS = {
    "sub": (RSS_HELP_MESSAGE, rss_sub),
    "get": (_GET_PROMPT, rss_get),
    "edit": (_EDIT_PROMPT, rss_edit),
    "deluser": (_DELUSER_PROMPT, rss_delete),
}

# The three actions that take a list of titles also offer "do all of mine".
_BULK_BUTTON = {
    "pause": ("Pause AllMyFeeds", "uallpause"),
    "resume": ("Resume AllMyFeeds", "uallresume"),
    "unsubscribe": ("Unsub AllMyFeeds", "uallunsub"),
}


async def _ask(ctx: _Ctx, text, handler, extra=None, **kwargs):
    """Show a prompt, then hand the user's next message to `handler`."""
    await edit_message(ctx.message, text, nav_buttons(ctx.user_id, extra))
    pfunc = partial(handler, pre_event=ctx.query, **kwargs)
    await event_handler(ctx.client, ctx.query, pfunc)


async def _act_close(ctx: _Ctx):
    await delete_message(ctx.message.reply_to_message)
    await delete_message(ctx.message)


async def _act_back(ctx: _Ctx):
    await update_rss_menu(ctx.query)


async def _act_prompt(ctx: _Ctx):
    text, handler = _PROMPTS[ctx.action]
    await _ask(ctx, text, handler)


async def _act_state_prompt(ctx: _Ctx):
    """pause / resume / unsubscribe: ask for titles, then apply that state."""
    text = (
        f"Send one or more rss titles separated by space to {ctx.action}.\n"
        "Timeout: 60 sec."
    )
    await _ask(
        ctx,
        text,
        rss_update,
        extra=_BULK_BUTTON[ctx.action],
        state=ctx.action,
    )


async def _act_list(ctx: _Ctx):
    await rss_list(ctx.query, int(ctx.data[3]))


async def _act_listall(ctx: _Ctx):
    await rss_list(ctx.query, int(ctx.data[3]), all_users=True)


async def _act_user_bulk(ctx: _Ctx):
    """`uall*`: every feed of one user."""
    if ctx.action.endswith("unsub"):
        await store.drop_user(ctx.target)
        await update_rss_menu(ctx.query)
    elif ctx.action.endswith("pause"):
        await store.set_user_paused(ctx.target, True)
        await store.save_user(ctx.target)
    elif ctx.action.endswith("resume"):
        await store.set_user_paused(ctx.target, False)
        if scheduler.state == 2:
            scheduler.resume()
        await store.save_user(ctx.target)
    await update_rss_menu(ctx.query)


async def _act_global_bulk(ctx: _Ctx):
    """`all*`: every feed of every user."""
    if ctx.action.endswith("unsub"):
        await store.drop_everyone()
        await update_rss_menu(ctx.query)
    elif ctx.action.endswith("pause"):
        await store.set_everyone_paused(True)
        if scheduler.running:
            scheduler.pause()
        await store.save_everyone()
    elif ctx.action.endswith("resume"):
        await store.set_everyone_paused(False)
        if resume_or_start():
            await update_rss_menu(ctx.query)
        await store.save_everyone()


async def _act_shutdown(ctx: _Ctx):
    if not scheduler.running:
        await ctx.query.answer(text="Already Stopped!", show_alert=True)
        return
    await ctx.query.answer()
    scheduler.shutdown(wait=False)
    await sleep(0.5)
    await update_rss_menu(ctx.query)


async def _act_start(ctx: _Ctx):
    if scheduler.running:
        await ctx.query.answer(text="Already Running!", show_alert=True)
        return
    await ctx.query.answer()
    add_job()
    scheduler.start()
    await update_rss_menu(ctx.query)


async def _act_setchat(ctx: _Ctx):
    """Point RSS_CHAT at the chat (and forum topic) the menu is open in."""
    message = ctx.message
    thread_id = (
        message.message_thread_id if getattr(message, "topic_message", False) else None
    )
    chat_id = chat_of(message).id
    chat_value = f"{chat_id}|{thread_id}" if thread_id else str(chat_id)
    old_value = Config.RSS_CHAT
    Config.set("RSS_CHAT", chat_value)
    await database.update_config({"RSS_CHAT": chat_value})
    await ctx.query.answer(text=f"RSS_CHAT set to {chat_value}", show_alert=True)
    if not scheduler.running:
        add_job()
        scheduler.start()
    if str(old_value) != chat_value:
        await update_rss_menu(ctx.query)


_ACTIONS: dict[str, _Action] = {
    "close": _Action(_act_close, wait="late"),
    "back": _Action(_act_back, wait="late"),
    "sub": _Action(_act_prompt, wait="late"),
    "list": _Action(_act_list, needs="user"),
    "get": _Action(_act_prompt, needs="user"),
    "unsubscribe": _Action(_act_state_prompt, needs="user"),
    "pause": _Action(_act_state_prompt, needs="user"),
    "resume": _Action(_act_state_prompt, needs="user"),
    "edit": _Action(_act_prompt, needs="user"),
    "deluser": _Action(_act_prompt, wait=None, needs="any"),
    "listall": _Action(_act_listall, wait=None, needs="any"),
    "shutdown": _Action(_act_shutdown, wait=None, answer=False),
    "start": _Action(_act_start, wait=None, answer=False),
    "setchat": _Action(_act_setchat, wait=None, answer=False),
}

# Checked after the exact table above; no exact action starts with either.
_PREFIX_ACTIONS = (
    ("uall", _Action(_act_user_bulk, needs="user")),
    ("all", _Action(_act_global_bulk, wait=None, needs="any")),
)


def _resolve(action: str) -> _Action | None:
    if found := _ACTIONS.get(action):
        return found
    for prefix, entry in _PREFIX_ACTIONS:
        if action.startswith(prefix):
            return entry
    return None


def _has_feeds(needs: str, target) -> bool:
    if needs == "user":
        return len(rss_dict.get(target, {})) != 0
    return len(rss_dict) != 0


@new_task
async def rss_listener(client, query):
    data = query.data.split()
    ctx = _Ctx(
        client=client,
        query=query,
        message=query.message,
        user_id=query.from_user.id,
        action=data[1],
        target=int(data[2]),
        data=data,
    )
    if ctx.target != ctx.user_id and not await CustomFilters.is_sudo(query):
        await query.answer(
            text="You don't have permission to use these buttons!", show_alert=True
        )
        return
    action = _resolve(ctx.action)
    if action is None:
        return
    if action.wait == "first":
        store.handler_dict[ctx.user_id] = False
    if action.needs and not _has_feeds(action.needs, ctx.target):
        await query.answer(text="No subscriptions!", show_alert=True)
        return
    if action.answer:
        await query.answer()
    if action.wait == "late":
        store.handler_dict[ctx.user_id] = False
    await action.run(ctx)
