"""Settings UI: declarative menu schema plus the renderer that consumes it.

`users_settings` keeps the handlers and the conversation flow; everything about
*what a screen looks like* lives here.
"""

from .literals import parse_dict, parse_literal
from .menu_builder import build_option_menu, build_settings, resolve_toggle
from .schema import (
    MENUS,
    NOT_SET,
    OPTIONS_BY_KEY,
    Field,
    Menu,
    Toggle,
    leech_options,
    resolve_ffmpeg_cmds,
)

__all__ = [
    "MENUS",
    "NOT_SET",
    "OPTIONS_BY_KEY",
    "Field",
    "Menu",
    "Toggle",
    "build_option_menu",
    "build_settings",
    "leech_options",
    "parse_dict",
    "parse_literal",
    "resolve_ffmpeg_cmds",
    "resolve_toggle",
]
