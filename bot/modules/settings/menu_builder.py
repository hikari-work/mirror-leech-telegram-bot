"""Render the user settings screens from `schema.MENUS`.

One pass over `Menu.buttons` builds the keyboard, a second over `Menu.lines`
builds the text. They are separate passes because the two orders genuinely
differ — see the module docstring in `schema`.
"""

from __future__ import annotations

from html import escape

from ... import user_data
from ...core.config_manager import Config
from ...core.telegram_manager import TgClient
from ...helper.telegram_helper.button_build import ButtonMaker
from .schema import (
    MENUS,
    NOT_SET,
    OPTIONS_BY_KEY,
    Field,
    Toggle,
    leech_options,
    resolve_ffmpeg_cmds,
)


def resolve_toggle(opt: Toggle, user_dict):
    """Return `(enabled, show_button)` for one toggle.

    Premium gating is not "hide unless premium": the user's *own* value is
    ignored for a non-premium user, but a truthy Config default still turns the
    option on for everyone — only the button disappears. That asymmetry comes
    from `A and B or C and D` binding as `(A and B) or (C and D)` in the
    original, and it is load-bearing, so it is spelled out here.
    """
    premium_ok = not opt.premium or TgClient.IS_PREMIUM_USER
    enabled = bool(
        premium_ok
        and user_dict.get(opt.key, False)
        or opt.key not in user_dict
        and getattr(Config, opt.key)
    )
    return enabled, enabled or premium_ok


async def _render_line(opt, user_dict, user_id):
    if isinstance(opt, Toggle):
        enabled, _ = resolve_toggle(opt, user_dict)
        return opt.line.format(value=opt.on if enabled else opt.off)
    value = await opt.resolve(opt.key, user_dict, user_id)
    value = "None" if value is NOT_SET else opt.present or value
    if opt.escape_value:
        value = escape(value)
    return opt.line.format(value=value)


def _add_button(buttons, opt, user_dict, user_id):
    if isinstance(opt, Field):
        buttons.data_button(opt.label, f"userset {user_id} menu {opt.key}")
        return
    enabled, show = resolve_toggle(opt, user_dict)
    if not show:
        return
    label = opt.disable_label if enabled else opt.enable_label
    flag = "f" if enabled else "t"
    buttons.data_button(label, f"userset {user_id} tog {opt.key} {flag}")


async def build_settings(from_user, stype="main"):
    """`(text, markup)` for the main or leech screen."""
    user_id = from_user.id
    user_dict = user_data.get(user_id, {})
    menu = MENUS[stype]
    buttons = ButtonMaker()

    for label, action in menu.leading:
        buttons.data_button(label, f"userset {user_id} {action}")
    for key in menu.buttons:
        _add_button(buttons, menu.options[key], user_dict, user_id)
    if menu.show_reset_all and user_dict:
        buttons.data_button("Reset All", f"userset {user_id} reset all")
    for label, action in menu.trailing:
        buttons.data_button(label, f"userset {user_id} {action}")

    lines = [
        await _render_line(menu.options[key], user_dict, user_id)
        for key in menu.lines
    ]
    # The header always joins with a single newline; only the options are
    # separated by `menu.separator` (the main screen double-spaces them).
    text = (
        menu.header.format(name=from_user.mention)
        + "\n"
        + menu.separator.join(lines)
        + menu.trailer
    )
    return text, buttons.build_menu(2)


def _add_one_remove_one(buttons, user_id, option):
    buttons.data_button("Add one", f"userset {user_id} addone {option}")
    buttons.data_button("Remove one", f"userset {user_id} rmone {option}")


def build_option_menu(option, user_id):
    """`(text, markup)` for a single option's edit submenu."""
    user_dict = user_data.get(user_id, {})
    opt = OPTIONS_BY_KEY.get(option)
    kind = opt.kind if opt is not None else "text"
    buttons = ButtonMaker()

    # A thumbnail is set by uploading a photo, so it has no stored value to
    # reset — hence "file" losing the Reset button.
    set_action = "file" if kind == "file" else "set"
    buttons.data_button("Set", f"userset {user_id} {set_action} {option}")
    if option in user_dict and kind != "file":
        buttons.data_button("Reset", f"userset {user_id} reset {option}")
    buttons.data_button("Remove", f"userset {user_id} remove {option}")

    if kind == "ffmpeg":
        # Per-key editing needs the user's own dict; the variables editor and
        # the viewer are happy with the Config fallback too.
        if user_dict.get(option, False):
            _add_one_remove_one(buttons, user_id, option)
        if resolve_ffmpeg_cmds(user_dict):
            buttons.data_button("FFMPEG VARIABLES", f"userset {user_id} ffvar")
            buttons.data_button("View", f"userset {user_id} view {option}")
    elif option in user_dict and user_dict[option]:
        if kind == "file":
            buttons.data_button("View", f"userset {user_id} view {option}")
        elif kind == "dict":
            _add_one_remove_one(buttons, user_id, option)

    back_to = "leech" if option in leech_options else "back"
    buttons.data_button("Back", f"userset {user_id} {back_to}")
    buttons.data_button("Close", f"userset {user_id} close")
    return f"Edit menu for: {option}", buttons.build_menu(2)
