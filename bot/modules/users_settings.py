from dataclasses import dataclass
from functools import partial
from io import BytesIO
from re import findall

from aiofiles.os import path as aiopath
from aiofiles.os import remove

from .. import (
    auth_chats,
    sudo_users,
    user_data,
)
from ..core.telegram_manager import TgClient
from ..helper.storage.copy_presets import (
    MAX_DESTS,
    MAX_PRESETS,
    additions_to,
    parse_destinations,
    presets_of,
    valid_name,
)
from ..helper.storage.db_handler import database
from ..helper.telegram.button_build import ButtonMaker
from ..helper.telegram.conversation import wait_for_message
from ..helper.telegram.message_utils import (
    delete_message,
    edit_message,
    send_file,
    send_message,
)
from ..helper.util.bot_utils import (
    get_size_bytes,
    new_task,
    update_user_ldata,
)
from ..helper.util.help_messages import user_settings_text
from ..helper.util.media_utils import create_thumb
from .settings import (
    build_option_menu,
    build_settings,
    parse_dict,
    resolve_ffmpeg_cmds,
)

handler_dict = {}


async def update_user_settings(query, stype="main"):
    handler_dict[query.from_user.id] = False
    msg, button = await build_settings(query.from_user, stype)
    await edit_message(query.message, msg, button)


@new_task
async def send_user_settings(_, message):
    from_user = message.from_user
    handler_dict[from_user.id] = False
    msg, button = await build_settings(from_user)
    await send_message(message, msg, button)


@new_task
async def add_file(_, message, ftype):
    user_id = message.from_user.id
    handler_dict[user_id] = False
    des_dir = await create_thumb(message, user_id)
    update_user_ldata(user_id, ftype, des_dir)
    await delete_message(message)
    await database.update_user_doc(user_id, ftype, des_dir)


@new_task
async def add_one(_, message, option):
    user_id = message.from_user.id
    handler_dict[user_id] = False
    user_dict = user_data.get(user_id, {})
    value = message.text
    if value.startswith("{") and value.endswith("}"):
        try:
            value = parse_dict(value)
            if user_dict[option]:
                user_dict[option].update(value)
            else:
                update_user_ldata(user_id, option, value)
        except Exception as e:
            await send_message(message, str(e))
            return
    else:
        await send_message(message, "It must be dict!")
        return
    await delete_message(message)
    await database.update_user_data(user_id)


@new_task
async def remove_one(_, message, option):
    user_id = message.from_user.id
    handler_dict[user_id] = False
    user_dict = user_data.get(user_id, {})
    names = message.text.split("/")
    for name in names:
        if name in user_dict[option]:
            del user_dict[option][name]
    await delete_message(message)
    await database.update_user_data(user_id)


@new_task
async def set_option(_, message, option):
    user_id = message.from_user.id
    handler_dict[user_id] = False
    value = message.text
    if option == "LEECH_SPLIT_SIZE":
        if not value.isdigit():
            value = get_size_bytes(value)
        value = min(int(value), TgClient.MAX_SPLIT_SIZE)
    elif option == "EXCLUDED_EXTENSIONS":
        fx = value.split()
        value = ["aria2", "!qB"]
        for x in fx:
            x = x.lstrip(".")
            value.append(x.strip().lower())
    elif option == "INCLUDED_EXTENSIONS":
        fx = value.split()
        value = []
        for x in fx:
            x = x.lstrip(".")
            value.append(x.strip().lower())
    elif option in ["FFMPEG_CMDS", "YT_DLP_OPTIONS"]:
        if value.startswith("{") and value.endswith("}"):
            try:
                value = parse_dict(value)
            except Exception as e:
                await send_message(message, str(e))
                return
        else:
            await send_message(message, "It must be dict!")
            return
    update_user_ldata(user_id, option, value)
    await delete_message(message)
    await database.update_user_data(user_id)


async def get_menu(option, message, user_id):
    handler_dict[user_id] = False
    text, button = build_option_menu(option, user_id)
    await edit_message(message, text, button)


def _stored_presets(user_id):
    """The user's copy presets, created in place so that edits are kept.

    ``presets_of`` answers with a throwaway ``{}`` when there is nothing saved,
    which is what a reader wants and useless to a writer. Everything below edits
    the stored mapping directly, the way ``remove_one`` does, and then asks the
    database to write the whole user dict back.
    """
    user_dict = user_data.setdefault(user_id, {})
    presets = user_dict.get("COPY_PRESETS")
    if not isinstance(presets, dict):
        # "Remove" leaves a "" behind, which is an opinion but not a mapping.
        presets = user_dict["COPY_PRESETS"] = {}
    return presets


@new_task
async def create_copy_preset(_, message):
    """Name an empty copy preset. The name is what ``-c`` will be given."""
    user_id = message.from_user.id
    handler_dict[user_id] = False
    name = message.text.strip()
    presets = presets_of(user_data.get(user_id, {}))
    if not valid_name(name):
        await send_message(
            message,
            "A preset name may only hold letters, digits, <code>-</code> and"
            " <code>_</code>, up to 24 of them. It has to survive both"
            " <code>-c</code> and the buttons on this menu, so <b>no spaces</b>.",
        )
        return
    if name in presets:
        await send_message(message, f"You already have a preset named <b>{name}</b>.")
        return
    if len(presets) >= MAX_PRESETS:
        await send_message(
            message,
            f"{MAX_PRESETS} presets is the most you can keep. Delete one first.",
        )
        return
    _stored_presets(user_id)[name] = []
    await delete_message(message)
    await database.update_user_data(user_id)


@new_task
async def add_copy_dests(_, message, name):
    """Add chats to one preset, as many as a single message can carry."""
    user_id = message.from_user.id
    handler_dict[user_id] = False
    presets = _stored_presets(user_id)
    if name not in presets:
        return
    found, error = parse_destinations(message.text)
    if error:
        await send_message(message, error)
        return
    fresh, error = additions_to(presets[name], found)
    if error:
        await send_message(message, f"<b>{name}</b>: {error}")
        return
    presets[name].extend(fresh)
    await delete_message(message)
    await database.update_user_data(user_id)


@new_task
async def remove_copy_dests(_, message, name):
    """Drop chats from one preset, ``/``-separated as ``remove_one`` does."""
    user_id = message.from_user.id
    handler_dict[user_id] = False
    presets = _stored_presets(user_id)
    if name not in presets:
        return
    unwanted = {entry.strip() for entry in message.text.split("/") if entry.strip()}
    presets[name] = [dest for dest in presets[name] if dest not in unwanted]
    await delete_message(message)
    await database.update_user_data(user_id)


async def set_ffmpeg_variable(_, message, key, value, index):
    user_id = message.from_user.id
    handler_dict[user_id] = False
    txt = message.text
    user_dict = user_data.setdefault(user_id, {})
    ffvar_data = user_dict.setdefault("FFMPEG_VARIABLES", {})
    ffvar_data = ffvar_data.setdefault(key, {})
    ffvar_data = ffvar_data.setdefault(index, {})
    ffvar_data[value] = txt
    await delete_message(message)
    await database.update_user_data(user_id)


def _ffvar_key_list(buttons, user_id, ffc):
    """Screen 1: the FFmpeg keys that actually contain a `{variable}`."""
    for k, cmds in list(ffc.items()):
        if any(findall(r"\{(.*?)\}", cmd) for cmd in cmds):
            buttons.data_button(k, f"userset {user_id} ffvar {k}")
    buttons.data_button("Back", f"userset {user_id} menu FFMPEG_CMDS")
    buttons.data_button("Close", f"userset {user_id} close")
    return "Choose which key you want to fill/edit variables in it:"


def _ffvar_variable_list(buttons, user_id, ffc, key):
    """Screen 2: every `{variable}` inside one key's commands."""
    for ind, cmd in enumerate(ffc[key]):
        for var in set(findall(r"\{(.*?)\}", cmd)):
            buttons.data_button(var, f"userset {user_id} ffvar {key} {var} {ind}")
    buttons.data_button("Reset", f"userset {user_id} ffvar {key} ffmpegvarreset")
    buttons.data_button("Back", f"userset {user_id} ffvar")
    buttons.data_button("Close", f"userset {user_id} close")
    return (
        f"Choose which variable you want to fill/edit: <u>{key}</u>"
        f"\n\nCMDS:\n{ffc[key]}"
    )


def _ffvar_editor(buttons, user_id, user_dict, ffc, key, value, index):
    """Screen 3: the prompt for one variable's value."""
    buttons.data_button("Back", f"userset {user_id} setevent")
    buttons.data_button("Close", f"userset {user_id} close")
    msg = (
        f"Edit/Fill this FFmpeg Variable: <u>{key}</u>"
        f"\n\nItem: {ffc[key][int(index)]}\n\nVariable: {value}"
    )
    old_value = (
        user_dict.get("FFMPEG_VARIABLES", {}).get(key, {}).get(index, {}).get(value, "")
    )
    if old_value:
        msg += f"\n\nCurrent Value: {old_value}"
    return msg


async def ffmpeg_variables(
    client, query, message, user_id, key=None, value=None, index=None
):
    user_dict = user_data.get(user_id, {})
    ffc = resolve_ffmpeg_cmds(user_dict)
    if not ffc:
        return
    buttons = ButtonMaker()
    if key is None:
        msg = _ffvar_key_list(buttons, user_id, ffc)
    elif key in ffc and value is None:
        msg = _ffvar_variable_list(buttons, user_id, ffc, key)
    elif key in ffc and value:
        msg = _ffvar_editor(buttons, user_id, user_dict, ffc, key, value, index)
    else:
        return
    await edit_message(message, msg, buttons.build_menu(2))
    if key in ffc and value:
        # Collect the value, then drop back to the variable list for this key.
        pfunc = partial(set_ffmpeg_variable, key=key, value=value, index=index)
        await event_handler(client, query, pfunc)
        await ffmpeg_variables(client, query, message, user_id, key)


async def event_handler(client, query, pfunc, photo=False, document=False):
    """Wait for the answer to the prompt this callback has just put on screen."""
    if photo:
        media = ("photo",)
    elif document:
        media = ("document",)
    else:
        media = ("text",)
    await wait_for_message(
        client, query, pfunc, handler_dict, query.from_user.id, media
    )


@dataclass(slots=True)
class _Ctx:
    """Everything a `userset` action handler needs, parsed once."""

    client: object
    query: object
    message: object
    data: list
    user_id: int
    name: str
    thumb_path: str
    user_dict: dict

    @property
    def option(self):
        """The option key the callback targets — absent for nav-only actions."""
        return self.data[3]


async def _prompt(ctx, text):
    """The "now send me a value" screen every event-driven action shows."""
    buttons = ButtonMaker()
    buttons.data_button("Back", f"userset {ctx.user_id} setevent")
    buttons.data_button("Close", f"userset {ctx.user_id} close")
    await edit_message(ctx.message, text, buttons.build_menu(2))


async def _act_setevent(ctx):
    await ctx.query.answer()


async def _act_leech(ctx):
    await ctx.query.answer()
    await update_user_settings(ctx.query, "leech")


async def _act_back(ctx):
    await ctx.query.answer()
    await update_user_settings(ctx.query)


async def _act_menu(ctx):
    await ctx.query.answer()
    await get_menu(ctx.option, ctx.message, ctx.user_id)


async def _act_tog(ctx):
    await ctx.query.answer()
    update_user_ldata(ctx.user_id, ctx.option, ctx.data[4] == "t")
    await update_user_settings(ctx.query, stype="leech")
    await database.update_user_data(ctx.user_id)


async def _act_file(ctx):
    await ctx.query.answer()
    await _prompt(ctx, "Send a photo to save it as custom thumbnail. Timeout: 60 sec")
    pfunc = partial(add_file, ftype=ctx.option)
    await event_handler(ctx.client, ctx.query, pfunc, photo=True)
    await get_menu(ctx.option, ctx.message, ctx.user_id)


async def _act_ffvar(ctx):
    await ctx.query.answer()
    data = ctx.data
    key = data[3] if len(data) > 3 else None
    value = data[4] if len(data) > 4 else None
    if value == "ffmpegvarreset":
        ff_data = user_data.get(ctx.user_id, {}).get("FFMPEG_VARIABLES", {})
        if key in ff_data:
            del ff_data[key]
            await database.update_user_data(ctx.user_id)
        return
    index = data[5] if len(data) > 5 else None
    await ffmpeg_variables(
        ctx.client, ctx.query, ctx.message, ctx.user_id, key, value, index
    )


def _copy_preset_list(buttons, user_id, presets):
    """Screen 1: every preset the user keeps, and room for one more."""
    for name in presets:
        buttons.data_button(name, f"userset {user_id} copyp {name}")
    if len(presets) < MAX_PRESETS:
        buttons.data_button("New Preset", f"userset {user_id} copynew")
    buttons.data_button("Back", f"userset {user_id} menu COPY_PRESETS")
    buttons.data_button("Close", f"userset {user_id} close")
    if not presets:
        return (
            "No copy preset yet.\n\nA preset is a named set of chats that every"
            " album a task uploads gets copied to. Pick one per task with"
            " <code>-c name</code>; it takes the place of your Clone Dump Chats"
            " for that task."
        )
    return (
        f"Copy presets ({len(presets)}/{MAX_PRESETS}). Choose one to edit, or"
        " use it in a task with <code>-c name</code>:"
    )


def _copy_preset_editor(buttons, user_id, name, dests):
    """Screen 2: one preset's chats, and what can be done to them."""
    if len(dests) < MAX_DESTS:
        buttons.data_button("Add Chats", f"userset {user_id} copyp {name} add")
    if dests:
        buttons.data_button("Remove Chats", f"userset {user_id} copyp {name} rm")
    buttons.data_button("Delete Preset", f"userset {user_id} copyp {name} drop")
    buttons.data_button("Back", f"userset {user_id} copyp")
    buttons.data_button("Close", f"userset {user_id} close")
    listed = "\n".join(f"• <code>{dest}</code>" for dest in dests) or "nothing yet"
    return (
        f"Copy preset <u>{name}</u> ({len(dests)}/{MAX_DESTS})\n\n{listed}"
        f"\n\nUse it with <code>-c {name}</code>. The bot has to be able to post"
        " to all of them, or the task is refused before it downloads anything."
    )


async def _draw_copy_presets(ctx):
    """Redraw the preset list, whichever screen the user came from."""
    buttons = ButtonMaker()
    presets = presets_of(user_data.get(ctx.user_id, {}))
    text = _copy_preset_list(buttons, ctx.user_id, presets)
    await edit_message(ctx.message, text, buttons.build_menu(2))


async def _draw_copy_preset(ctx, name):
    """Redraw one preset, falling back to the list once it is gone."""
    presets = presets_of(user_data.get(ctx.user_id, {}))
    if name not in presets:
        await _draw_copy_presets(ctx)
        return
    buttons = ButtonMaker()
    text = _copy_preset_editor(buttons, ctx.user_id, name, presets[name])
    await edit_message(ctx.message, text, buttons.build_menu(2))


# The two prompts a preset screen can raise, each with the collector that reads
# the answer. Keyed by the verb the callback carries.
_COPY_PROMPTS = {
    "add": (
        add_copy_dests,
        "Send the chats to copy to: <code>chat_id</code>, or"
        " <code>chat_id|thread_id</code> for one topic of a forum. A @username"
        " works, and <code>pm</code> means your own chat. Several at once is"
        " fine -- one per line, or separated by commas or spaces."
        " Timeout: 60 sec",
    ),
    "rm": (
        remove_copy_dests,
        "Send the chats to drop, separated by <code>/</code>, exactly as they"
        " are listed. Example: <code>-100123456|5/pm</code>. Timeout: 60 sec",
    ),
}


async def _act_copyp(ctx):
    """The copy preset screens: the list, one preset, and its two prompts.

    Shaped like ``_act_ffvar``: the press that raises a prompt is also what
    waits for the answer and then redraws the screen underneath, so the user
    ends up back where they were instead of at the top.
    """
    await ctx.query.answer()
    name = ctx.data[3] if len(ctx.data) > 3 else None
    if name is None:
        await _draw_copy_presets(ctx)
        return
    verb = ctx.data[4] if len(ctx.data) > 4 else ""
    if verb == "drop":
        _stored_presets(ctx.user_id).pop(name, None)
        await database.update_user_data(ctx.user_id)
        await _draw_copy_presets(ctx)
        return
    if verb in _COPY_PROMPTS:
        func, prompt = _COPY_PROMPTS[verb]
        await _prompt(ctx, prompt)
        await event_handler(ctx.client, ctx.query, partial(func, name=name))
    await _draw_copy_preset(ctx, name)


async def _act_copynew(ctx):
    """Ask for a name and add the empty preset it names.

    A separate action from ``copyp`` so that a preset can never be mistaken for
    a verb: both travel in the same position of the callback data, and a user is
    free to name a preset "add".
    """
    await ctx.query.answer()
    await _prompt(
        ctx,
        "Send a name for the new copy preset. Letters, digits, <code>-</code>"
        " and <code>_</code> only, no spaces -- it is what you will pass to"
        " <code>-c</code>. Timeout: 60 sec",
    )
    await event_handler(ctx.client, ctx.query, create_copy_preset)
    await _draw_copy_presets(ctx)


# The three actions that ask the user for a message differ only in the prompt
# they show and the collector they hand the reply to.
_EVENT_ACTIONS = {
    "set": (
        set_option,
        lambda option: user_settings_text[option],
    ),
    "addone": (
        add_one,
        lambda option: f"Add one or more string key and value to {option}. Example: "
        "{'key 1': 62625261, 'key 2': 'value 2'}. Timeout: 60 sec",
    ),
    "rmone": (
        remove_one,
        lambda option: f"Remove one or more key from {option}. Example: "
        "key 1/key2/key 3. Timeout: 60 sec",
    ),
}


async def _act_event(ctx):
    await ctx.query.answer()
    func, prompt_for = _EVENT_ACTIONS[ctx.data[2]]
    await _prompt(ctx, prompt_for(ctx.option))
    pfunc = partial(func, option=ctx.option)
    await event_handler(ctx.client, ctx.query, pfunc)
    await get_menu(ctx.option, ctx.message, ctx.user_id)


async def _act_remove(ctx):
    await ctx.query.answer("Removed!", show_alert=True)
    if ctx.option == "THUMBNAIL":
        if await aiopath.exists(ctx.thumb_path):
            await remove(ctx.thumb_path)
        del ctx.user_dict[ctx.option]
        await database.update_user_doc(ctx.user_id, ctx.option)
    else:
        update_user_ldata(ctx.user_id, ctx.option, "")
        await database.update_user_data(ctx.user_id)


# "Reset All" wipes settings but not identity: auth level, the thumbnail file
# and the login session survive.
_KEEP_ON_RESET = ("SUDO", "AUTH", "THUMBNAIL", "USER_SESSION_STRING")


async def _act_reset(ctx):
    await ctx.query.answer("Reseted!", show_alert=True)
    if ctx.option in ctx.user_dict:
        del ctx.user_dict[ctx.option]
    else:
        for k in list(ctx.user_dict.keys()):
            if k not in _KEEP_ON_RESET:
                del ctx.user_dict[k]
        await update_user_settings(ctx.query)
    await database.update_user_data(ctx.user_id)


async def _act_view(ctx):
    await ctx.query.answer()
    if ctx.option == "THUMBNAIL":
        await send_file(ctx.message, ctx.thumb_path, ctx.name)
    elif ctx.option == "FFMPEG_CMDS":
        msg_ecd = str(resolve_ffmpeg_cmds(ctx.user_dict)).encode()
        with BytesIO(msg_ecd) as ofile:
            ofile.name = "users_settings.txt"
            await send_file(ctx.message, ofile)


async def _act_close(ctx):
    await ctx.query.answer()
    await delete_message(ctx.message.reply_to_message)
    await delete_message(ctx.message)


_USER_ACTIONS = {
    "setevent": _act_setevent,
    "leech": _act_leech,
    "menu": _act_menu,
    "tog": _act_tog,
    "file": _act_file,
    "ffvar": _act_ffvar,
    "copyp": _act_copyp,
    "copynew": _act_copynew,
    "set": _act_event,
    "addone": _act_event,
    "rmone": _act_event,
    "remove": _act_remove,
    "reset": _act_reset,
    "view": _act_view,
    "back": _act_back,
}


@new_task
async def edit_user_settings(client, query):
    from_user = query.from_user
    user_id = from_user.id
    data = query.data.split()
    handler_dict[user_id] = False
    if user_id != int(data[1]):
        await query.answer("Not Yours!", show_alert=True)
        return
    ctx = _Ctx(
        client=client,
        query=query,
        message=query.message,
        data=data,
        user_id=user_id,
        name=from_user.mention,
        thumb_path=f"thumbnails/{user_id}.jpg",
        user_dict=user_data.get(user_id, {}),
    )
    # "close" has no handler of its own: it and any unknown action both fall
    # through to closing the screen, exactly as the old `else` branch did.
    await _USER_ACTIONS.get(data[2], _act_close)(ctx)


def _dump_value(k, v):
    """A session string is a credential — report only that one exists."""
    if k == "USER_SESSION_STRING" and v:
        return "Exists"
    return v or None


@new_task
async def get_users_settings(_, message):
    msg = ""
    if auth_chats:
        msg += f"AUTHORIZED_CHATS: {auth_chats}\n"
    if sudo_users:
        msg += f"SUDO_USERS: {sudo_users}\n\n"
    if user_data:
        for u, d in user_data.items():
            kmsg = f"\n<b>{u}:</b>\n"
            if vmsg := "".join(
                f"{k}: <code>{_dump_value(k, v)}</code>\n" for k, v in d.items()
            ):
                msg += kmsg + vmsg
        if not msg:
            await send_message(message, "No users data!")
            return
        msg_ecd = msg.encode()
        if len(msg_ecd) > 4000:
            with BytesIO(msg_ecd) as ofile:
                ofile.name = "users_settings.txt"
                await send_file(message, ofile)
        else:
            await send_message(message, msg)
    else:
        await send_message(message, "No users data!")
