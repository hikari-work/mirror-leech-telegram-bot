"""Handler registration — one row of data per Telegram entry point.

`add_handlers()` used to be 246 lines of `TgClient.bot.add_handler(...)` calls
that differed in only three things: the callback, the command (or callback-data
prefix), and the access filter. Adding a command meant copying a seven-line
block and editing it in three places.

The rows below are that same registration, in the same order, as data.

Order is preserved *within* each handler kind because pyrogram depends on it:
every handler goes into group 0, and the dispatcher walks the group in insertion
order and stops at the first handler that matches (`pyrogram/dispatcher.py`,
`handler_worker`). Order *across* kinds carries no meaning — `MessageHandler`,
`EditedMessageHandler` and `CallbackQueryHandler` each subclass `Handler`
directly, so the dispatcher's `isinstance(handler, handler_type)` gate can only
ever select one of the three for any given update.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple

from pyrogram.filters import Filter, command, regex
from pyrogram.handlers import (
    CallbackQueryHandler,
    EditedMessageHandler,
    MessageHandler,
)
from pyrogram.types import BotCommand

from ..helper.telegram_helper.bot_commands import BotCommands
from ..helper.telegram_helper.filters import CustomFilters
from ..modules import (
    add_sudo,
    aioexecute,
    arg_usage,
    authorize,
    bot_help,
    bot_stats,
    bypass_scrape_cmd,
    cancel,
    cancel_all_buttons,
    cancel_all_update,
    cancel_multi,
    clear,
    confirm_restart,
    confirm_selection,
    edit_bot_settings,
    edit_user_settings,
    execute,
    get_rss_menu,
    get_users_settings,
    leech,
    log,
    ping,
    qb_leech,
    remove_from_queue,
    remove_sudo,
    restart_bot,
    rss_listener,
    run_shell,
    select,
    send_bot_settings,
    send_user_settings,
    start,
    status_pages,
    task_status,
    torrent_search,
    torrent_search_update,
    unauthorize,
    user_login,
    user_logout,
    ytdl_leech,
)
from .telegram_manager import TgClient


class _Command(NamedTuple):
    """A `/command` entry point."""

    func: Callable
    cmd: str | list[str]
    access: Filter | None = None
    """Who may run it. `None` means the command is open to everyone."""

    on_edit: bool = False
    """Also fire when an already-sent message is edited into the command."""

    desc: str = ""
    """Short description shown in Telegram's command menu (BotFather style)."""


class _Callback(NamedTuple):
    """An inline-button entry point, matched on its callback-data prefix."""

    func: Callable
    pattern: str
    access: Filter | None = None


# Short aliases so every row of the tables below fits on one line, which is the
# only reason the tables are easier to scan than the calls they replaced.
_OWNER, _SUDO, _AUTH = CustomFilters.owner, CustomFilters.sudo, CustomFilters.authorized

COMMAND_HANDLERS: tuple[_Command, ...] = (
    _Command(authorize, BotCommands.AuthorizeCommand, _SUDO, desc="Authorize a chat or user"),
    _Command(unauthorize, BotCommands.UnAuthorizeCommand, _SUDO, desc="Unauthorize a chat or user"),
    _Command(add_sudo, BotCommands.AddSudoCommand, _OWNER, desc="Add sudo user"),
    _Command(remove_sudo, BotCommands.RmSudoCommand, _OWNER, desc="Remove sudo user"),
    _Command(send_bot_settings, BotCommands.BotSetCommand, _SUDO, desc="Bot settings"),
    _Command(cancel, BotCommands.CancelTaskCommand, _AUTH, desc="Cancel task by gid or reply"),
    _Command(cancel_all_buttons, BotCommands.CancelAllCommand, _AUTH, desc="Cancel all tasks"),
    _Command(aioexecute, BotCommands.AExecCommand, _OWNER, desc="Exec async functions"),
    _Command(execute, BotCommands.ExecCommand, _OWNER, desc="Exec sync functions"),
    _Command(clear, BotCommands.ClearLocalsCommand, _OWNER, desc="Clear exec locals"),
    _Command(select, BotCommands.SelectCommand, _AUTH, desc="Select files from torrents"),
    _Command(remove_from_queue, BotCommands.ForceStartCommand, _AUTH, desc="Force start task"),
    _Command(bypass_scrape_cmd, BotCommands.BypassCommand, _AUTH, desc="Bypass and get direct link"),
    _Command(leech, BotCommands.LeechCommand, _AUTH, desc="Leech to Telegram"),
    _Command(qb_leech, BotCommands.QbLeechCommand, _AUTH, desc="Leech using qBittorrent"),
    _Command(get_rss_menu, BotCommands.RssCommand, _AUTH, desc="RSS menu"),
    _Command(run_shell, BotCommands.ShellCommand, _OWNER, on_edit=True, desc="Run shell commands"),
    _Command(start, BotCommands.StartCommand, desc="Start the bot"),
    _Command(log, BotCommands.LogCommand, _SUDO, desc="Get bot log file"),
    _Command(restart_bot, BotCommands.RestartCommand, _SUDO, desc="Restart and update the bot"),
    _Command(ping, BotCommands.PingCommand, _AUTH, desc="Ping the bot"),
    _Command(bot_help, BotCommands.HelpCommand, _AUTH, desc="List available commands"),
    _Command(bot_stats, BotCommands.StatsCommand, _AUTH, desc="Show host machine stats"),
    _Command(task_status, BotCommands.StatusCommand, _AUTH, desc="Show download status"),
    _Command(torrent_search, BotCommands.SearchCommand, _AUTH, desc="Search for torrents"),
    _Command(get_users_settings, BotCommands.UsersCommand, _SUDO, desc="Show users settings"),
    _Command(send_user_settings, BotCommands.UserSetCommand, _AUTH, desc="User settings"),
    _Command(user_login, BotCommands.LoginCommand, _AUTH, desc="Login with personal session"),
    _Command(user_logout, BotCommands.LogoutCommand, _AUTH, desc="Revoke personal session"),
    _Command(ytdl_leech, BotCommands.YtdlLeechCommand, _AUTH, desc="Leech yt-dlp supported link"),
)

# Most callbacks carry no access filter of their own: the menu they belong to
# was already gated by the command that opened it. `botset` and `botrestart` are
# gated anyway, and that asymmetry is deliberate — both act on the whole bot, so
# they re-check instead of trusting whoever holds the button.
CALLBACK_HANDLERS: tuple[_Callback, ...] = (
    _Callback(edit_bot_settings, "^botset", _SUDO),
    _Callback(cancel_all_update, "^canall"),
    _Callback(cancel_multi, "^stopm"),
    _Callback(confirm_selection, "^sel"),
    _Callback(arg_usage, "^help"),
    _Callback(rss_listener, "^rss"),
    _Callback(confirm_restart, "^botrestart", _SUDO),
    _Callback(status_pages, "^status"),
    _Callback(torrent_search_update, "^torser"),
    _Callback(edit_user_settings, "^userset"),
)


def _gated(flt: Filter, access: Filter | None) -> Filter:
    return flt if access is None else flt & access


def add_handlers() -> None:
    for row in COMMAND_HANDLERS:
        flt = _gated(command(row.cmd, case_sensitive=True), row.access)
        TgClient.bot.add_handler(MessageHandler(row.func, filters=flt))
        if row.on_edit:
            TgClient.bot.add_handler(EditedMessageHandler(row.func, filters=flt))

    for row in CALLBACK_HANDLERS:
        flt = _gated(regex(row.pattern), row.access)
        TgClient.bot.add_handler(CallbackQueryHandler(row.func, filters=flt))


async def set_commands() -> None:
    """Register the bot's command menu with Telegram (visible in chat input)."""
    commands = []
    for row in COMMAND_HANDLERS:
        if not row.desc:
            continue
        cmds = row.cmd if isinstance(row.cmd, list) else [row.cmd]
        commands.append(BotCommand(cmds[0], row.desc))
    await TgClient.bot.set_bot_commands(commands)
