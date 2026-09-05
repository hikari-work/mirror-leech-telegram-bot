"""Declarative description of the user settings menus.

`users_settings.get_user_settings` used to hardcode every option twice: once to
append its button and once to interpolate its line into the menu text. The two
listings drifted apart in shape — button order is not text order, three
different value-resolution rules coexist, and two toggles are premium-gated in
a way that is *not* a plain "hide if not premium".

Those differences are real, so they live here as data (fields on `Field` /
`Toggle`) rather than as branches in the builder.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from aiofiles.os import path as aiopath

from ... import excluded_extensions, included_extensions
from ...core.config_manager import Config
from ...helper.storage.copy_presets import presets_of


class _NotSet:
    """Sentinel: the option has no value from the user nor from Config.

    A plain ``"None"`` string would collide with a user who literally stores
    ``"None"``; the original code had that collision, this does not.
    """

    def __repr__(self) -> str:
        return "NOT_SET"


NOT_SET = _NotSet()


# --------------------------------------------------------------------------
# value resolution — one function per rule the original actually used
# --------------------------------------------------------------------------


async def user_or_config_or_none(key, user_dict, user_id):
    """User value, else Config *if truthy*, else nothing. The common rule."""
    if user_dict.get(key, False):
        return user_dict[key]
    if key not in user_dict and getattr(Config, key):
        return getattr(Config, key)
    return NOT_SET


async def user_or_config(key, user_dict, user_id):
    """User value, else Config unconditionally — no "None" fallback.

    Only `LEECH_SPLIT_SIZE` works this way: an unset split size still has a
    meaningful default, so it never renders as "None".
    """
    if user_dict.get(key, False):
        return user_dict[key]
    return getattr(Config, key)


_RUNTIME_LISTS = {
    "EXCLUDED_EXTENSIONS": excluded_extensions,
    "INCLUDED_EXTENSIONS": included_extensions,
}


async def user_or_runtime_list(key, user_dict, user_id):
    """User value, else the live global list — even when that list is empty.

    Unlike the Config rule there is no truthiness guard on the fallback, so an
    empty runtime list renders as `[]`, not as "None".
    """
    if user_dict.get(key, False):
        return user_dict[key]
    if key not in user_dict:
        return _RUNTIME_LISTS[key]
    return NOT_SET


async def copy_preset_count(key, user_dict, user_id):
    """How many copy presets the user keeps, and nothing when they keep none.

    The only option with no bot-wide default behind it: a preset names chats one
    person wants their own uploads copied to, so there is no ``Config`` twin --
    and the shared resolvers would raise reaching for one. The names live on the
    preset screens; this line only says whether there is anything to look at.
    """
    presets = presets_of(user_dict)
    return f"{len(presets)} saved" if presets else NOT_SET


async def thumb_exists(key, user_dict, user_id):
    """The thumbnail is a file on disk, not a stored value."""
    exists = await aiopath.exists(f"thumbnails/{user_id}.jpg")
    return "Exists" if exists else "Not Exists"


# --------------------------------------------------------------------------
# option descriptors
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Field:
    """An option edited through its own submenu (`userset <id> menu <KEY>`)."""

    key: str
    label: str
    line: str
    resolve: Callable = user_or_config_or_none
    present: str = ""
    """When set, any resolved value renders as this word instead of itself."""
    escape_value: bool = False
    kind: str = "text"
    """Shapes the submenu: "file" is set by upload, "dict"/"ffmpeg" support
    per-key add/remove, "ffmpeg" additionally exposes the variables editor,
    "copy" has no value to type and opens the preset screens instead.
    """


@dataclass(frozen=True, slots=True)
class Toggle:
    """A boolean flipped straight from the menu (`userset <id> tog <KEY> t|f`)."""

    key: str
    line: str
    on: str
    off: str
    enable_label: str
    disable_label: str
    premium: bool = False
    """Premium gating suppresses the *user's own* value and the button — but a
    truthy Config default still enables it for everyone (see `resolve_toggle`).
    """


@dataclass(frozen=True, slots=True)
class Menu:
    """One rendered screen: a header, an ordered button list, ordered lines."""

    header: str
    buttons: tuple[str, ...]
    lines: tuple[str, ...]
    separator: str = "\n"
    trailer: str = ""
    leading: tuple[tuple[str, str], ...] = ()
    trailing: tuple[tuple[str, str], ...] = ()
    show_reset_all: bool = False
    options: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# the options themselves
# --------------------------------------------------------------------------


LEECH_OPTIONS = (
    Field(
        "THUMBNAIL",
        "Thumbnail",
        "Custom Thumbnail <b>{value}</b>",
        thumb_exists,
        kind="file",
    ),
    Field(
        "LEECH_SPLIT_SIZE",
        "Leech Split Size",
        "Leech Split Size is <b>{value}</b>",
        user_or_config,
    ),
    Field(
        "LEECH_FILENAME_PREFIX",
        "Leech Prefix",
        "Leech Prefix is <code>{value}</code>",
        escape_value=True,
    ),
    Field(
        "THUMBNAIL_LAYOUT",
        "Thumbnail Layout",
        "Thumbnail Layout is <b>{value}</b>",
    ),
    Field(
        "CLONE_DUMP_CHATS",
        "Clone Dump Chats",
        "Clone Dump Chats is <code>{value}</code>",
    ),
    Field(
        "COPY_PRESETS",
        "Copy Presets",
        "Copy Presets is <b>{value}</b>",
        copy_preset_count,
        kind="copy",
    ),
    Toggle(
        "AS_DOCUMENT",
        "Leech Type is <b>{value}</b>",
        on="DOCUMENT",
        off="MEDIA",
        enable_label="Send As Document",
        disable_label="Send As Media",
    ),
    Toggle(
        "EQUAL_SPLITS",
        "Equal Splits is <b>{value}</b>",
        on="Enabled",
        off="Disabled",
        enable_label="Enable Equal Splits",
        disable_label="Disable Equal Splits",
    ),
    Toggle(
        "MEDIA_GROUP",
        "Media Group is <b>{value}</b>",
        on="Enabled",
        off="Disabled",
        enable_label="Enable Media Group",
        disable_label="Disable Media Group",
    ),
    Toggle(
        "USER_TRANSMISSION",
        "Leech by <b>{value}</b> session",
        on="user",
        off="bot",
        enable_label="Leech by User",
        disable_label="Leech by Bot",
        premium=True,
    ),
    Toggle(
        "HYBRID_LEECH",
        "Hybrid Leech is <b>{value}</b>",
        on="Enabled",
        off="Disabled",
        # "Hybride" / "HYBRID" casing is what users see today; kept verbatim.
        enable_label="Enable HYBRID Leech",
        disable_label="Disable Hybride Leech",
        premium=True,
    ),
    Toggle(
        "FILES_LINKS",
        "Files Links is <b>{value}</b>",
        on="Enabled",
        off="Disabled",
        enable_label="Enable FILES LINKS",
        disable_label="Disable FILES LINKS",
    ),
)

MAIN_OPTIONS = (
    Field(
        "EXCLUDED_EXTENSIONS",
        "Excluded Extensions",
        "Excluded Extensions is <code>{value}</code>",
        user_or_runtime_list,
    ),
    Field(
        "INCLUDED_EXTENSIONS",
        "Included Extensions",
        "Included Extensions is <code>{value}</code>",
        user_or_runtime_list,
    ),
    Field(
        "NAME_SUBSTITUTE",
        "Name Substitute",
        "Name substitution is <code>{value}</code>",
        present="Added",
    ),
    Field(
        "YT_DLP_OPTIONS",
        "YT-DLP Options",
        "YT-DLP Options is <code>{value}</code>",
        kind="dict",
    ),
    Field(
        "FFMPEG_CMDS",
        "FFmpeg Cmds",
        "FFMPEG Commands is <b>{value}</b>",
        present="Exists",
        kind="ffmpeg",
    ),
)


def _by_key(options):
    return {opt.key: opt for opt in options}


MENUS = {
    "main": Menu(
        header="<u>Settings for {name}</u>",
        buttons=(
            "EXCLUDED_EXTENSIONS",
            "INCLUDED_EXTENSIONS",
            "NAME_SUBSTITUTE",
            "YT_DLP_OPTIONS",
            "FFMPEG_CMDS",
        ),
        lines=(
            "NAME_SUBSTITUTE",
            "EXCLUDED_EXTENSIONS",
            "INCLUDED_EXTENSIONS",
            "YT_DLP_OPTIONS",
            "FFMPEG_CMDS",
        ),
        separator="\n\n",
        leading=(("Leech", "leech"),),
        trailing=(("Close", "close"),),
        show_reset_all=True,
        options=_by_key(MAIN_OPTIONS),
    ),
    "leech": Menu(
        header="<u>Leech Settings for {name}</u>",
        buttons=(
            "THUMBNAIL",
            "LEECH_SPLIT_SIZE",
            "LEECH_FILENAME_PREFIX",
            "AS_DOCUMENT",
            "EQUAL_SPLITS",
            "MEDIA_GROUP",
            "USER_TRANSMISSION",
            "HYBRID_LEECH",
            "FILES_LINKS",
            "THUMBNAIL_LAYOUT",
            "CLONE_DUMP_CHATS",
            "COPY_PRESETS",
        ),
        lines=(
            "AS_DOCUMENT",
            "THUMBNAIL",
            "LEECH_SPLIT_SIZE",
            "EQUAL_SPLITS",
            "MEDIA_GROUP",
            "LEECH_FILENAME_PREFIX",
            "CLONE_DUMP_CHATS",
            "COPY_PRESETS",
            "USER_TRANSMISSION",
            "HYBRID_LEECH",
            "THUMBNAIL_LAYOUT",
            "FILES_LINKS",
        ),
        trailer="\n",
        trailing=(("Back", "back"), ("Close", "close")),
        options=_by_key(LEECH_OPTIONS),
    ),
}

# The submenu's "Back" button returns to the leech screen for these keys.
leech_options = [opt.key for opt in LEECH_OPTIONS if isinstance(opt, Field)]

# Every option, regardless of which screen shows it — the submenu builder and the
# callback handlers look options up by key alone.
OPTIONS_BY_KEY = _by_key(LEECH_OPTIONS + MAIN_OPTIONS)


def resolve_ffmpeg_cmds(user_dict):
    """The user's FFmpeg commands, else the Config ones, else None.

    Same rule as `user_or_config_or_none`, but the three callers that need it
    (submenu, variables editor, viewer) want a plain value they can test.
    """
    if user_dict.get("FFMPEG_CMDS", False):
        return user_dict["FFMPEG_CMDS"]
    if "FFMPEG_CMDS" not in user_dict and Config.FFMPEG_CMDS:
        return Config.FFMPEG_CMDS
    return None
