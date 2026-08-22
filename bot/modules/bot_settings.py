from asyncio import (
    create_subprocess_exec,
    create_subprocess_shell,
    gather,
)
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from io import BytesIO
from os import getcwd

from aiofiles.os import path as aiopath
from aiofiles.os import remove, rename

from .. import (
    aria2_options,
    auth_chats,
    excluded_extensions,
    included_extensions,
    intervals,
    qbit_options,
    sudo_users,
    task_dict,
)
from ..core.config_manager import Config
from ..core.startup import update_qb_options, update_variables
from ..core.telegram_manager import TgClient
from ..core.torrent_manager import TorrentManager
from ..helper.ext_utils.bot_utils import (
    SetInterval,
    new_task,
)
from ..helper.ext_utils.db_handler import database
from ..helper.ext_utils.task_manager import start_from_queued
from ..helper.telegram_helper.button_build import ButtonMaker
from ..helper.telegram_helper.conversation import wait_for_message
from ..helper.telegram_helper.message_utils import (
    delete_message,
    edit_message,
    send_file,
    send_message,
    update_status_message,
)
from .rss import add_job
from .search import initiate_search_tools
from .settings import parse_literal

handler_dict = {}
DEFAULT_VALUES = {
    "LEECH_SPLIT_SIZE": TgClient.MAX_SPLIT_SIZE,
    "RSS_DELAY": 600,
    "STATUS_UPDATE_INTERVAL": 15,
    "SEARCH_LIMIT": 0,
    "UPSTREAM_BRANCH": "master",
}


class Paging:
    """Which slice of a long option list is on screen, and what a tap does.

    The bot settings screen is a singleton — there is no per-message state to
    hang this off — so it stays module-global. It used to be two bare globals
    written through `globals()[...]`; the class just gives them a name.
    """

    start = 0
    state = "view"


# --------------------------------------------------------------------------
# shared side effects
# --------------------------------------------------------------------------


async def _kill_gunicorn():
    await (await create_subprocess_exec("pkill", "-9", "-f", "gunicorn")).wait()


async def _start_gunicorn(port):
    await create_subprocess_shell(
        "gunicorn -k uvicorn.workers.UvicornWorker -w 1 web.wserver:app "
        f"--bind 0.0.0.0:{port}"
    )


def _reschedule_status_intervals(value):
    """Restart the live status refresh loops at a new interval."""
    if len(task_dict) == 0 or not (st := intervals["status"]):
        return
    for cid, intvl in list(st.items()):
        intvl.cancel()
        intervals["status"][cid] = SetInterval(value, update_status_message, cid)


async def _apply_config_change(key):
    """Restart whatever a freshly changed config value feeds."""
    if key in ["SEARCH_PLUGINS", "SEARCH_API_LINK"]:
        await initiate_search_tools()
    elif key in ["QUEUE_ALL", "QUEUE_DOWNLOAD", "QUEUE_UPLOAD"]:
        await start_from_queued()


# --------------------------------------------------------------------------
# screens
# --------------------------------------------------------------------------


_ROOT_BUTTONS = (
    ("Config Variables", "botset var"),
    ("Private Files", "botset private"),
    ("Qbit Settings", "botset qbit"),
    ("Aria2c Settings", "botset aria"),
    ("Close", "botset close"),
)

_PRIVATE_FILES_MSG = (
    "Send private file: config.py, cookies.txt, .netrc or any other private file!\n"
    "To delete private file send only the file name as text message.\n"
    "Note: Changing .netrc will not take effect for aria2c until restart.\n"
    "Timeout: 60 sec"
)

# Editing these only reaches the database; the running bot keeps the old value.
_RESTART_REQUIRED = (
    "CMD_SUFFIX",
    "OWNER_ID",
    "USER_SESSION_STRING",
    "TELEGRAM_HASH",
    "TELEGRAM_API",
    "BOT_TOKEN",
    "TG_PROXY",
)
# Credentials have no meaningful "default" to reset to.
_NO_DEFAULT_BUTTON = ("TELEGRAM_HASH", "TELEGRAM_API", "OWNER_ID", "BOT_TOKEN")


def _prompt_botvar(key, buttons):
    buttons.data_button("Back", "botset var")
    if key not in _NO_DEFAULT_BUTTON:
        buttons.data_button("Default", f"botset resetvar {key}")
    buttons.data_button("Close", "botset close")
    msg = ""
    if key in _RESTART_REQUIRED:
        msg += (
            "Restart required for this edit to take effect! You will not see the "
            "changes in bot vars, the edit will be in database only!\n\n"
        )
    return msg + (
        f"Send a valid value for {key}. Current value is '{Config.get(key)}'. "
        "Timeout: 60 sec"
    )


def _prompt_ariavar(key, buttons):
    buttons.data_button("Back", "botset aria")
    if key != "newkey":
        buttons.data_button("Empty String", f"botset emptyaria {key}")
    buttons.data_button("Close", "botset close")
    if key == "newkey":
        return "Send a key with value. Example: https-proxy-user:value. Timeout: 60 sec"
    return (
        f"Send a valid value for {key}. Current value is '{aria2_options[key]}'. "
        "Timeout: 60 sec"
    )


def _prompt_qbitvar(key, buttons):
    buttons.data_button("Back", "botset qbit")
    buttons.data_button("Empty String", f"botset emptyqbit {key}")
    buttons.data_button("Close", "botset close")
    return (
        f"Send a valid value for {key}. Current value is '{qbit_options[key]}'. "
        "Timeout: 60 sec"
    )


_EDIT_PROMPTS = {
    "botvar": _prompt_botvar,
    "ariavar": _prompt_ariavar,
    "qbitvar": _prompt_qbitvar,
}


@dataclass(frozen=True, slots=True)
class _Screen:
    """A paged list of options — config vars, aria2 options or qbit options."""

    key: str
    title: str
    action: str
    """Callback verb for tapping one option, e.g. `botset botvar <NAME>`."""
    options: Callable
    hidden: Callable = None
    """Keys that still consume a slot on the page but grow no button."""
    extra: tuple = field(default_factory=tuple)
    """Screen-specific buttons, shown between Edit/View and Back."""


_SCREENS = {
    "var": _Screen(
        key="var",
        title="Config Variables",
        action="botvar",
        options=Config.get_all,
        # Connection strings stay hidden while editing is armed.
        hidden=lambda k: k in ["DATABASE_URL", "DATABASE_NAME"]
        and Paging.state != "view",
    ),
    "aria": _Screen(
        key="aria",
        title="Aria2c Options",
        action="ariavar",
        options=lambda: aria2_options,
        hidden=lambda k: k in ["checksum", "index-out", "out", "pause", "select-file"],
        extra=(("Add new key", "botset ariavar newkey"),),
    ),
    "qbit": _Screen(
        key="qbit",
        title="Qbittorrent Options",
        action="qbitvar",
        options=lambda: qbit_options,
        extra=(("Sync Qbittorrent", "botset syncqbit"),),
    ),
}


def _paged_screen(screen, buttons):
    options = screen.options()
    for k in list(options.keys())[Paging.start : 10 + Paging.start]:
        if screen.hidden is not None and screen.hidden(k):
            continue
        buttons.data_button(k, f"botset {screen.action} {k}")
    if Paging.state == "view":
        buttons.data_button("Edit", f"botset edit {screen.key}")
    else:
        buttons.data_button("View", f"botset view {screen.key}")
    for label, action in screen.extra:
        buttons.data_button(label, action)
    buttons.data_button("Back", "botset back")
    buttons.data_button("Close", "botset close")
    for x in range(0, len(options), 10):
        buttons.data_button(
            f"{int(x / 10)}", f"botset start {screen.key} {x}", position="footer"
        )
    return f"{screen.title} | Page: {int(Paging.start / 10)} | State: {Paging.state}"


async def get_buttons(key=None, edit_type=None):
    buttons = ButtonMaker()
    if key is None:
        for label, action in _ROOT_BUTTONS:
            buttons.data_button(label, action)
        msg = "Bot Settings:"
    elif edit_type is not None:
        msg = _EDIT_PROMPTS[edit_type](key, buttons)
    elif key == "private":
        buttons.data_button("Back", "botset back")
        buttons.data_button("Close", "botset close")
        msg = _PRIVATE_FILES_MSG
    else:
        msg = _paged_screen(_SCREENS[key], buttons)
    return msg, buttons.build_menu(1 if key is None else 2)


async def update_buttons(message, key=None, edit_type=None):
    msg, button = await get_buttons(key, edit_type)
    await edit_message(message, msg, button)


# --------------------------------------------------------------------------
# collecting a new value from the user
# --------------------------------------------------------------------------


def _split_extensions(text):
    return [x.lstrip(".").strip().lower() for x in text.split()]


async def _coerce_status_interval(value):
    value = int(value)
    _reschedule_status_intervals(value)
    return value


async def _coerce_torrent_timeout(value):
    await TorrentManager.change_aria2_option("bt-stop-timeout", value)
    return int(value)


async def _coerce_split_size(value):
    return min(int(value), TgClient.MAX_SPLIT_SIZE)


async def _coerce_base_url_port(value):
    value = int(value)
    if Config.BASE_URL:
        await _kill_gunicorn()
        await _start_gunicorn(value)
    return value


async def _coerce_excluded(value):
    excluded_extensions.clear()
    excluded_extensions.extend(["aria2", "!qB"])
    excluded_extensions.extend(_split_extensions(value))
    return value


async def _coerce_included(value):
    included_extensions.clear()
    included_extensions.extend(_split_extensions(value))
    return value


async def _coerce_auth_chats(value):
    auth_chats.clear()
    for id_ in value.split():
        chat_id, *thread_ids = id_.split("|")
        auth_chats[int(chat_id.strip())] = [int(t.strip()) for t in thread_ids]
    return value


async def _coerce_sudo_users(value):
    sudo_users.clear()
    sudo_users.extend(int(id_.strip()) for id_ in value.split())
    return value


# These keys keep their raw text in Config; the coercer's real job is updating
# the live globals that mirror them.
_VALUE_COERCERS = {
    "STATUS_UPDATE_INTERVAL": _coerce_status_interval,
    "TORRENT_TIMEOUT": _coerce_torrent_timeout,
    "LEECH_SPLIT_SIZE": _coerce_split_size,
    "BASE_URL_PORT": _coerce_base_url_port,
    "EXCLUDED_EXTENSIONS": _coerce_excluded,
    "INCLUDED_EXTENSIONS": _coerce_included,
    "AUTHORIZED_CHATS": _coerce_auth_chats,
    "SUDO_USERS": _coerce_sudo_users,
}


async def _coerce_value(key, value):
    """Turn the raw message text into the value `Config` should hold.

    Order is load-bearing: "true"/"false" beat every key-specific rule, and the
    generic digit/list/dict parsing only runs for keys with no rule of their own.
    """
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        if key == "INCOMPLETE_TASK_NOTIFIER" and Config.DATABASE_URL:
            await database.trunc_table("tasks")
        return False
    if coercer := _VALUE_COERCERS.get(key):
        return await coercer(value)
    if value.isdigit():
        return int(value)
    if (value.startswith("[") and value.endswith("]")) or (
        value.startswith("{") and value.endswith("}")
    ):
        return parse_literal(value)
    return value


@new_task
async def edit_variable(_, message, pre_message, key):
    handler_dict[message.chat.id] = False
    value = await _coerce_value(key, str(message.text))
    Config.set(key, value)
    await update_buttons(pre_message, "var")
    await delete_message(message)
    await database.update_config({key: value})
    if key == "RSS_DELAY":
        add_job()
    else:
        await _apply_config_change(key)


@new_task
async def edit_aria(_, message, pre_message, key):
    handler_dict[message.chat.id] = False
    value = str(message.text)
    if key == "newkey":
        key, value = [x.strip() for x in value.split(":", 1)]
    elif value.lower() == "true":
        value = "true"
    elif value.lower() == "false":
        value = "false"
    await TorrentManager.change_aria2_option(key, value)
    await update_buttons(pre_message, "aria")
    await delete_message(message)
    await database.update_aria2(key, value)


@new_task
async def edit_qbit(_, message, pre_message, key):
    handler_dict[message.chat.id] = False
    value = str(message.text)
    if value.lower() == "true":
        value = True
    elif value.lower() == "false":
        value = False
    elif key == "max_ratio":
        value = float(value)
    elif value.isdigit():
        value = int(value)
    await TorrentManager.qbittorrent.app.set_preferences({key: value})
    qbit_options[key] = value
    await update_buttons(pre_message, "qbit")
    await delete_message(message)
    await database.update_qbittorrent(key, value)


async def _install_netrc(copy_only=False):
    if not copy_only:
        await (await create_subprocess_exec("touch", ".netrc")).wait()
    await (await create_subprocess_exec("chmod", "600", ".netrc")).wait()
    await (await create_subprocess_exec("cp", ".netrc", "/root/.netrc")).wait()


@new_task
async def update_private_file(_, message, pre_message):
    handler_dict[message.chat.id] = False
    if not message.media and (file_name := str(message.text)):
        if await aiopath.isfile(file_name) and file_name != "config.py":
            await remove(file_name)
        if file_name in {".netrc", "netrc"}:
            await _install_netrc()
        await delete_message(message)
    elif doc := message.document:
        file_name = doc.file_name
        fpath = f"{getcwd()}/{file_name}"
        if await aiopath.exists(fpath):
            await remove(fpath)
        await message.download(file_name=fpath)
        if file_name in [".netrc", "netrc"]:
            if file_name == "netrc":
                await rename("netrc", ".netrc")
                file_name = ".netrc"
            await _install_netrc(copy_only=True)
        elif file_name == "config.py":
            await load_config()
        if "@github.com" in Config.UPSTREAM_REPO:
            buttons = ButtonMaker()
            msg = "Push to UPSTREAM_REPO ?"
            buttons.data_button("Yes!", f"botset push {file_name}")
            buttons.data_button("No", "botset close")
            await send_message(message, msg, buttons.build_menu(2))
        else:
            await delete_message(message)
    await update_buttons(pre_message)
    await database.update_private_file(file_name)


async def event_handler(client, query, pfunc, rfunc, document=False):
    """Wait for the answer to the prompt this callback has just put on screen.

    Keyed by chat rather than by user: the bot settings menu is a single
    conversation per chat, whoever of the sudo users is driving it.
    """
    await wait_for_message(
        client,
        query,
        pfunc,
        handler_dict,
        query.message.chat.id,
        ("text", "document") if document else ("text",),
        on_timeout=rfunc,
    )


# --------------------------------------------------------------------------
# resetting one config variable
# --------------------------------------------------------------------------


# Resetting means "the empty value of whatever type it currently holds".
_ZERO_VALUES = {bool: bool, int: int, str: str, list: list, dict: dict}


async def _reset_excluded(value):
    excluded_extensions.clear()
    excluded_extensions.extend(["aria2", "!qB"])
    return value


async def _reset_included(value):
    included_extensions.clear()
    return value


async def _reset_torrent_timeout(value):
    await TorrentManager.change_aria2_option("bt-stop-timeout", "0")
    await database.update_aria2("bt-stop-timeout", "0")
    return value


async def _reset_base_url(value):
    await _kill_gunicorn()
    return value


async def _reset_base_url_port(value):
    value = 80
    if Config.BASE_URL:
        await _kill_gunicorn()
        await _start_gunicorn(value)
    return value


async def _reset_incomplete_notifier(value):
    await database.trunc_table("tasks")
    return value


async def _reset_auth_chats(value):
    auth_chats.clear()
    return value


async def _reset_sudo_users(value):
    sudo_users.clear()
    return value


_RESET_SIDE_EFFECTS = {
    "EXCLUDED_EXTENSIONS": _reset_excluded,
    "INCLUDED_EXTENSIONS": _reset_included,
    "TORRENT_TIMEOUT": _reset_torrent_timeout,
    "BASE_URL": _reset_base_url,
    "BASE_URL_PORT": _reset_base_url_port,
    "INCOMPLETE_TASK_NOTIFIER": _reset_incomplete_notifier,
    "AUTHORIZED_CHATS": _reset_auth_chats,
    "SUDO_USERS": _reset_sudo_users,
}


async def _reset_value(key):
    """The value the "Default" button writes, plus whatever it has to tear down.

    A key with an explicit default takes it and runs no side effect — only the
    keys *without* one reach `_RESET_SIDE_EFFECTS`.
    """
    value = _ZERO_VALUES[type(getattr(Config, key))]()
    if key in DEFAULT_VALUES:
        value = DEFAULT_VALUES[key]
        if key == "STATUS_UPDATE_INTERVAL":
            _reschedule_status_intervals(value)
        return value
    if side_effect := _RESET_SIDE_EFFECTS.get(key):
        value = await side_effect(value)
    return value


# --------------------------------------------------------------------------
# callback routing
# --------------------------------------------------------------------------


@dataclass(slots=True)
class _Ctx:
    """Everything a `botset` action handler needs, parsed once."""

    client: object
    query: object
    message: object
    data: list

    @property
    def name(self):
        """The option the callback targets — absent for nav-only actions."""
        return self.data[2]


async def _show_value(ctx, value):
    """Long values go out as a file; short ones fit in an alert popup."""
    if len(value) > 200:
        await ctx.query.answer()
        with BytesIO(str.encode(value)) as out_file:
            out_file.name = f"{ctx.name}.txt"
            await send_file(ctx.message, out_file)
        return
    await ctx.query.answer(f"{value or None}", show_alert=True)


async def _start_edit(ctx, edit_type, collector, screen):
    await ctx.query.answer()
    await update_buttons(ctx.message, ctx.name, edit_type)
    pfunc = partial(collector, pre_message=ctx.message, key=ctx.name)
    rfunc = partial(update_buttons, ctx.message, screen)
    await event_handler(ctx.client, ctx.query, pfunc, rfunc)


async def _bot_close(ctx):
    await ctx.query.answer()
    await delete_message(ctx.message.reply_to_message)
    await delete_message(ctx.message)


async def _bot_back(ctx):
    await ctx.query.answer()
    Paging.start = 0
    await update_buttons(ctx.message, None)


async def _bot_screen(ctx):
    await ctx.query.answer()
    await update_buttons(ctx.message, ctx.data[1])


async def _bot_resetvar(ctx):
    await ctx.query.answer()
    key = ctx.name
    value = await _reset_value(key)
    Config.set(key, value)
    await update_buttons(ctx.message, "var")
    if key == "DATABASE_URL":
        await database.disconnect()
    await database.update_config({key: value})
    await _apply_config_change(key)


async def _bot_syncqbit(ctx):
    await ctx.query.answer(
        "Synchronization Started. It takes up to 2 sec!", show_alert=True
    )
    qbit_options.clear()
    await update_qb_options()
    await database.save_qbit_settings()


async def _bot_emptyaria(ctx):
    await ctx.query.answer()
    aria2_options[ctx.name] = ""
    await update_buttons(ctx.message, "aria")
    await TorrentManager.change_aria2_option(ctx.name, "")
    await database.update_aria2(ctx.name, "")


async def _bot_emptyqbit(ctx):
    await ctx.query.answer()
    await TorrentManager.qbittorrent.app.set_preferences({ctx.name: ""})
    qbit_options[ctx.name] = ""
    await update_buttons(ctx.message, "qbit")
    await database.update_qbittorrent(ctx.name, "")


async def _bot_private(ctx):
    await ctx.query.answer()
    await update_buttons(ctx.message, "private")
    pfunc = partial(update_private_file, pre_message=ctx.message)
    rfunc = partial(update_buttons, ctx.message)
    await event_handler(ctx.client, ctx.query, pfunc, rfunc, True)


async def _bot_botvar(ctx):
    if Paging.state == "edit":
        await _start_edit(ctx, "botvar", edit_variable, "var")
    elif Paging.state == "view":
        await _show_value(ctx, f"{Config.get(ctx.name)}")


async def _bot_ariavar(ctx):
    # "Add new key" is an edit even while the screen is in view state.
    if Paging.state == "edit" or ctx.name == "newkey":
        await _start_edit(ctx, "ariavar", edit_aria, "aria")
    elif Paging.state == "view":
        await _show_value(ctx, f"{aria2_options[ctx.name]}")


async def _bot_qbitvar(ctx):
    if Paging.state == "edit":
        await _start_edit(ctx, "qbitvar", edit_qbit, "qbit")
    elif Paging.state == "view":
        await _show_value(ctx, f"{qbit_options[ctx.name]}")


async def _bot_edit(ctx):
    await ctx.query.answer()
    Paging.state = "edit"
    await update_buttons(ctx.message, ctx.name)


async def _bot_view(ctx):
    await ctx.query.answer()
    Paging.state = "view"
    await update_buttons(ctx.message, ctx.name)


async def _bot_start(ctx):
    await ctx.query.answer()
    if Paging.start != int(ctx.data[3]):
        Paging.start = int(ctx.data[3])
        await update_buttons(ctx.message, ctx.name)


async def _bot_push(ctx):
    await ctx.query.answer()
    filename = ctx.name.rsplit(".zip", 1)[0]
    # A file that is gone locally is a deletion upstream, not an addition.
    stage = (
        f"git add -f {filename}"
        if await aiopath.exists(filename)
        else f"git rm -r --cached {filename}"
    )
    await (
        await create_subprocess_shell(
            f"{stage} \
                    && git commit -sm botsettings -q \
                    && git push origin {Config.UPSTREAM_BRANCH} -qf"
        )
    ).wait()
    await delete_message(ctx.message.reply_to_message)
    await delete_message(ctx.message)


async def _bot_ignore(ctx):
    """Unknown action: the old if/elif chain simply fell off the end."""


_BOT_ACTIONS = {
    "close": _bot_close,
    "back": _bot_back,
    "var": _bot_screen,
    "aria": _bot_screen,
    "qbit": _bot_screen,
    "resetvar": _bot_resetvar,
    "syncqbit": _bot_syncqbit,
    "emptyaria": _bot_emptyaria,
    "emptyqbit": _bot_emptyqbit,
    "private": _bot_private,
    "botvar": _bot_botvar,
    "ariavar": _bot_ariavar,
    "qbitvar": _bot_qbitvar,
    "edit": _bot_edit,
    "view": _bot_view,
    "start": _bot_start,
    "push": _bot_push,
}


@new_task
async def edit_bot_settings(client, query):
    data = query.data.split()
    message = query.message
    handler_dict[message.chat.id] = False
    ctx = _Ctx(client=client, query=query, message=message, data=data)
    await _BOT_ACTIONS.get(data[1], _bot_ignore)(ctx)


@new_task
async def send_bot_settings(_, message):
    handler_dict[message.chat.id] = False
    msg, button = await get_buttons()
    Paging.start = 0
    await send_message(message, msg, button)


async def load_config():
    Config.load()
    await update_variables()

    _reschedule_status_intervals(Config.STATUS_UPDATE_INTERVAL)

    if Config.TORRENT_TIMEOUT:
        await TorrentManager.change_aria2_option(
            "bt-stop-timeout", f"{Config.TORRENT_TIMEOUT}"
        )
        await database.update_aria2("bt-stop-timeout", f"{Config.TORRENT_TIMEOUT}")

    if not Config.INCOMPLETE_TASK_NOTIFIER:
        await database.trunc_table("tasks")

    await _kill_gunicorn()
    if Config.BASE_URL:
        await _start_gunicorn(Config.BASE_URL_PORT)

    if Config.DATABASE_URL:
        await database.connect()
        config_dict = Config.get_all()
        await database.update_config(config_dict)
    else:
        await database.disconnect()
    await gather(initiate_search_tools(), start_from_queued())
    add_job()
