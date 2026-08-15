"""Starting a download from a feed item, by replaying it as a bot command.

A subscription's `-c` argument is a command line the user would have typed
(`ql -doc`). Rather than reimplementing argument handling, this bridge finds the
handler pyrogram registered for that command word, posts the command text to
`RSS_CHAT` so there is a real message to reply to, and calls the handler with
that message.

The command table is read back out of the dispatcher instead of being imported,
so the bridge cannot go stale against `bot/core/handlers.py`, and it is built
lazily: at import time `add_handlers()` has not run yet.
"""

from __future__ import annotations

from pyrogram.handlers import MessageHandler

from ... import LOGGER
from ...core.config_manager import Config
from ...core.telegram_manager import TgClient
from ...helper.telegram_helper.message_utils import send_rss


def _find_command_filters(flt):
    """Recursively extract CommandFilter instances from a composite filter tree."""
    if hasattr(flt, "commands"):
        yield flt
    for attr in ("base", "other"):
        if child := getattr(flt, attr, None):
            yield from _find_command_filters(child)


def _build_command_map():
    """Build a mapping from command name -> handler callback by inspecting
    the bot's registered message handlers."""
    mapping = {}
    for group in TgClient.bot.dispatcher.groups.values():
        for handler in group:
            if not isinstance(handler, MessageHandler):
                continue
            if handler.filters is None:
                continue
            for cmd_filter in _find_command_filters(handler.filters):
                for cmd in cmd_filter.commands:
                    mapping[cmd] = handler.callback
    return mapping


_command_map = None


def _get_command_map():
    global _command_map
    if _command_map is None:
        _command_map = _build_command_map()
    return _command_map


def _resolve_command(command_str):
    """Resolve a command string like 'ql -doc' into its handler function.

    Returns the handler function, or None if not recognized.
    Handles commands with or without CMD_SUFFIX.
    """
    cmd_name = command_str.strip().lstrip("/").split(maxsplit=1)[0]
    mapping = _get_command_map()
    handler = mapping.get(cmd_name)
    if handler is None and Config.CMD_SUFFIX:
        handler = mapping.get(cmd_name + Config.CMD_SUFFIX)
    if handler is None:
        LOGGER.warning(f"RSS: Unknown command '{cmd_name}' (from '{command_str}')")
    return handler


def _command_text(command: str, url: str) -> str:
    """`"ql -doc"` + url -> `"/ql <url> -doc"`: the link goes first."""
    cmd_text = f"/{command.strip().lstrip('/')}"
    parts = cmd_text.split(maxsplit=1)
    if len(parts) > 1:
        return f"{parts[0]} {url} {parts[1]}"
    return f"{parts[0]} {url}"


async def start_rss_download(
    url, command, user_id, rss_chat_id, rss_topic_id, item_title
):
    """Send a notification to RSS_CHAT and start the download directly."""
    handler = _resolve_command(command)
    if handler is None:
        LOGGER.error(f"RSS: Cannot start download, unknown command: {command}")
        return

    cmd_text = _command_text(command, url)

    try:
        user = await TgClient.bot.get_users(user_id)
    except Exception as e:
        LOGGER.error(
            f"RSS: Failed to get user {user_id}, "
            f"cannot start download for '{item_title}': {e}"
        )
        return

    msg = await send_rss(cmd_text, rss_chat_id, rss_topic_id)
    if isinstance(msg, str):
        LOGGER.error(f"RSS: Failed to send to RSS_CHAT: {msg}")
        return

    # The handler reads the command off the message and attributes the task to
    # the subscriber, not to the bot; `_rss_trigger` is what `TaskConfig` reads
    # to set `is_rss` (see helper/common.py).
    msg.text = cmd_text
    msg.from_user = user
    msg._rss_trigger = True

    await handler(TgClient.bot, msg)
