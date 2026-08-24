"""Reply handlers that define the subscription flag grammar: subscribe + edit.

Both `rss_sub` and `rss_edit` parse the `-c/-inf/-exf/-stv` flags of a feed
line, so they share the arg helpers below. Everything here is wired up by
`listener.py` through `menu.event_handler`: it runs on the *next* message the
user sends, gets the callback query they came from as `pre_event`, and ends by
redrawing the menu on it. The first thing each handler does is clear
`handler_dict`, which is what ends the wait in `event_handler`.
"""

from __future__ import annotations

from ... import LOGGER, rss_dict
from ...helper.ext_utils.bot_utils import arg_parser, new_task
from ...helper.ext_utils.status_utils import get_readable_file_size
from ...helper.telegram_helper.filters import CustomFilters
from ...helper.telegram_helper.message_utils import send_message
from . import feed
from .menu import update_rss_menu
from .monitor import resume_or_start
from .store import handler_dict, rss_dict_lock, save_user

# The flags a subscription understands, for `arg_parser`.
_SUB_FLAGS = ("-c", "-inf", "-exf", "-stv")


def _arg_base(args):
    base = dict.fromkeys(_SUB_FLAGS)
    arg_parser(args, base)
    return base


def _filter_groups(value: str) -> list[list[str]]:
    """`"1080 or 720|x264"` -> `[["1080", "720"], ["x264"]]`."""
    return [group.split(" or ") for group in value.split("|")]


# ---------------------------------------------------------------------------
# subscribe
# ---------------------------------------------------------------------------


def _parse_sub_args(args):
    """Read the flags of one subscribe line -> (cmd, inf, exf, stv, groups).

    With no flags at all `stv` is `False`; with some flags but no `-stv` it
    stays `None` and is stored as `None`. Pre-existing, and visible to the user
    in the confirmation message, so it is kept.
    """
    if not args:
        return None, None, None, False, [], []
    base = _arg_base(args)
    cmd, inf, exf, stv = (base[flag] for flag in _SUB_FLAGS)
    if stv is not None:
        stv = stv.lower() == "true"
    inf_lists = _filter_groups(inf) if inf is not None else []
    exf_lists = _filter_groups(exf) if exf is not None else []
    return cmd, inf, exf, stv, inf_lists, exf_lists


def _sub_report(title, feed_link, entries, feed_title, last, cmd, inf, exf, stv):
    last_title, last_link, size = last
    msg = "<b>Subscribed!</b>"
    msg += f"\n<b>Title: </b><code>{title}</code>\n<b>Feed Url: </b>{feed_link}"
    if entries:
        msg += f"\n<b>latest record for </b>{feed_title}:"
        clean = last_title.replace(">", "").replace("<", "")
        msg += f"\nName: <code>{clean}</code>"
        msg += f"\n<b>Link: </b><code>{last_link}</code>"
        if size:
            msg += f"\nSize: {get_readable_file_size(size)}"
    else:
        msg += (
            "\n<b>Note:</b> Feed is currently empty, will be monitored for new items."
        )
    msg += f"\n<b>Command: </b><code>{cmd}</code>"
    msg += f"\n<b>Filters:-</b>\ninf: <code>{inf}</code>\nexf: <code>{exf}</code>"
    msg += f"\n<b>sensitive: </b>{stv}"
    return msg


async def _reject_sub_line(message, index, item, args, user_id):
    """The three ways a subscribe line is rejected before we fetch anything."""
    if len(args) < 2:
        await send_message(
            message,
            f"{item}. Wrong Input format. Read help message before adding "
            "new subscription!",
        )
        return True
    title = args[0].strip()
    if (user_feeds := rss_dict.get(user_id, False)) and title in user_feeds:
        await send_message(
            message, f"This title {title} already subscribed! Choose another title!"
        )
        return True
    if args[1].strip().startswith(("-inf", "-exf", "-c")):
        await send_message(
            message,
            f"Wrong input in line {index}! Add Title! Read the example!",
        )
        return True
    return False


async def _subscribe_one(message, index, item, user_id, tag) -> str:
    """Validate, fetch and store one subscribe line. Returns its report text."""
    args = item.split()
    if await _reject_sub_line(message, index, item, args, user_id):
        return ""
    title = args[0].strip()
    feed_link = args[1].strip()
    cmd, inf, exf, stv, inf_lists, exf_lists = _parse_sub_args(args[2:])
    try:
        rss_d = feed.parse(await feed.fetch_text(feed_link))
        last_link = ""
        last_title = ""
        size = 0
        feed_title = feed.feed_title(rss_d)
        if rss_d.entries:
            last_title = rss_d.entries[0]["title"]
            size = feed.item_size(rss_d.entries[0])
            last_link = feed.item_url(rss_d.entries[0])
        msg = _sub_report(
            title,
            feed_link,
            rss_d.entries,
            feed_title,
            (last_title, last_link, size),
            cmd,
            inf,
            exf,
            stv,
        )
        async with rss_dict_lock:
            rss_dict.setdefault(user_id, {})[title] = {
                "link": feed_link,
                "last_feed": last_link,
                "last_title": last_title,
                "inf": inf_lists,
                "exf": exf_lists,
                "paused": False,
                "command": cmd,
                "sensitive": stv,
                "tag": tag,
            }
        LOGGER.info(
            f"Rss Feed Added: id: {user_id} - title: {title} - link: {feed_link} "
            f"- c: {cmd} - inf: {inf} - exf: {exf} - stv: {stv}"
        )
        return msg
    except (IndexError, AttributeError) as e:
        emsg = (
            f"The link: {feed_link} doesn't seem to be a RSS feed "
            "or it's region-blocked!"
        )
        await send_message(message, emsg + "\nError: " + str(e))
    except Exception as e:
        await send_message(message, str(e))
    return ""


@new_task
async def rss_sub(_, message, pre_event):
    user_id = message.from_user.id
    handler_dict[user_id] = False
    if username := message.from_user.username:
        tag = f"@{username}"
    else:
        tag = message.from_user.mention
    msg = ""
    for index, item in enumerate(message.text.split("\n"), start=1):
        msg += await _subscribe_one(message, index, item, user_id, tag)
    if msg:
        await save_user(user_id)
        await send_message(message, msg)
        resume_or_start(await CustomFilters.is_sudo(message))
    await update_rss_menu(pre_event)


# ---------------------------------------------------------------------------
# edit
# ---------------------------------------------------------------------------


async def _apply_edit(user_id, title, args):
    """Overwrite only the flags this line carries; `none` clears one."""
    base = _arg_base(args)
    async with rss_dict_lock:
        entry = rss_dict[user_id][title]
        if (stv := base["-stv"]) is not None:
            entry["sensitive"] = stv.lower() == "true"
        if (cmd := base["-c"]) is not None:
            entry["command"] = None if cmd.lower() == "none" else cmd
        for key, flag in (("inf", "-inf"), ("exf", "-exf")):
            if (value := base[flag]) is None:
                continue
            entry[key] = [] if value.lower() == "none" else _filter_groups(value)


@new_task
async def rss_edit(_, message, pre_event):
    user_id = message.from_user.id
    handler_dict[user_id] = False
    updated = False
    for item in message.text.split("\n"):
        args = item.split()
        title = args[0].strip()
        if len(args) < 2:
            await send_message(
                message,
                f"{item}. Wrong Input format. Read help message before editing!",
            )
            continue
        if not rss_dict[user_id].get(title, False):
            await send_message(message, "Enter a valid title. Title not found!")
            continue
        updated = True
        await _apply_edit(user_id, title, args[1:])
    if updated:
        await save_user(user_id)
    await update_rss_menu(pre_event)
