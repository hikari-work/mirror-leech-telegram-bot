"""What a mixin in this package may assume about the object it lives in.

The four mixins here are pieces of one object, not four independent ones:
``TaskConfig`` composes them, and each reads state that a sibling -- or
``TaskListener`` above them -- owns. That works at runtime because the composed
instance really does carry every attribute by the time a mixin runs. Read one
mixin on its own, though, and those reads have no visible source: 36 attributes
and 3 methods arriving from nowhere, their types knowable only by grepping the
other three files and the two classes above them.

``TaskConfigHost`` writes that contract down. It is deliberately inert -- no
values, no method bodies, no ``__init__`` -- so composing it changes nothing at
runtime; every attribute is still assigned exactly where it was before. What it
buys is that the assumption each mixin makes about its host is now stated in one
place, and a checker (or a reader) can tell whether a given ``self.x`` is part of
the shared surface or a typo.

A ``Protocol`` would be the usual reach here, but it describes objects seen from
*outside*. The problem is the opposite one: code *inside* a mixin needs to know
what ``self`` has, and only a real base class gives ``self`` that type. Nothing
is checked structurally against this, so a plain class is the honest choice.

Owner of each attribute is noted below, because "where is this set?" is the
question this file exists to answer.
"""

from asyncio.subprocess import Process
from typing import TYPE_CHECKING, Any, NotRequired, TypedDict

from pyrogram import Client
from pyrogram.types import Chat, Message, User


class SameDirGroup(TypedDict):
    """One ``-m`` folder and the tasks that will merge their files into it.

    Three files share this record -- the dispatcher creates it, ``MultiLinkMixin``
    keeps its count, ``TaskListener`` drains it -- and none of them said what it
    holds. ``stage`` is absent until the first sibling to get there names the
    directory, which is why it is ``NotRequired`` rather than ``str``.
    """

    total: int
    """Siblings still expected to contribute; the last one out does the upload."""
    tasks: set[int]
    """The mids that have registered so far."""
    stage: NotRequired[str]
    """The shared directory the siblings move their files into."""


class TaskConfigHost:
    """The shared attribute surface of a composed ``TaskConfig``.

    Annotation-only: subclasses assign these, this class never does.
    """

    # ── set by the subclass *before* ``TaskConfig.__init__`` runs ────────
    # ``CommandTask.__init__`` fills these in, and ``TaskConfig.__init__``
    # reads ``message`` immediately, so they cannot wait for it.
    client: Client
    message: Message
    # A batch anchor's tag, or None for a task that is not part of one.
    multi_tag: str | None
    # Remaining links of a ``-b`` bulk; the dispatcher pops from the front.
    bulk: list[str]
    # The ``-m`` groups this task's batch is merging into, keyed by folder name.
    # Shared by reference across every sibling, which is what makes the
    # book-keeping in ``register_same_dir`` visible to all of them.
    same_dir: dict[str, SameDirGroup]

    # ── set by ``TaskConfig.__init__`` (bot/helper/common.py) ────────────
    # identity
    mid: int
    bulk_child: bool
    # Whoever the task is attributed to: the sender, or the chat when a channel
    # or an anonymous admin posted the command. Not optional, because the line
    # after the one that sets it reads ``user.id``; ``TaskConfig.__init__`` is
    # where that is stated.
    user: User | Chat
    user_id: int
    user_dict: dict[str, Any]
    tag: str

    # paths and naming
    dir: str
    up_dir: str
    link: str
    name: str
    folder_name: str
    copy_preset: str
    # The ``-t`` argument, then the thumbnail file ``_resolve_thumbnail`` made of
    # it. "" for a task without one, and never None: ``is_telegram_link`` calls
    # ``startswith`` on it unguarded.
    thumb: str
    # A chat id once ``_normalize_up_dest`` has run, the text the user typed
    # ("pm", "-100…|12", "b:-100…") before that.
    up_dest: str | int
    # Same shape shift: the ``old/new | old/new`` spec as typed, then the parsed
    # pairs ``_resolve_name_substitutions`` turns it into, which is what
    # ``perform_substitution`` wants.
    name_sub: str | list[list[str]]

    # sizes
    size: int
    # The text the user typed after ``-sp`` ("2g"), until
    # ``_resolve_split_sizes`` reduces it to a byte count.
    split_size: str | int
    max_split_size: int
    multi: int

    # source of the task
    is_qbit: bool
    is_ytdlp: bool
    is_super_chat: bool

    # The child process an ffmpeg or 7z step currently has running. ``FFMpeg``
    # and ``SevenZ`` are handed the task and leave it here rather than keeping it
    # themselves, because killing it is ``cancel_task``'s job and ``cancel_task``
    # only has the task. None between steps, and again once the last one is done.
    subproc: Process | None
    is_cancelled: bool

    # ── media pipeline switches ──────────────────────────────────────────
    equal_splits: bool

    # Value-carrying flags: bare ``-e`` parses to True, ``-e secret`` to the
    # string. ``TaskConfig.__init__`` defaults them to False and the arg parser
    # fills them from ``raw`` with an untyped ``setattr``, so both halves are
    # reachable -- which is why the pipeline guards each one with
    # ``isinstance(..., str)`` before using it as a value. Note that
    # ``CommonArgs`` annotates several of these as plain ``bool`` or ``str``;
    # that annotation predates this file and does not survive the setattr.
    extract: str | bool  # password for 7z
    compress: str | bool  # password for 7z
    screen_shots: str | bool  # how many to take
    sample_video: str | bool  # "duration:part" spec

    # These two look like the four above but are not: ``-ca``/``-cv`` are absent
    # from ``arg_parser``'s ``bool_arg_set``, so they never parse to True -- they
    # carry a spec string or keep the falsy default. ``convert_media`` guards
    # them with the same ``isinstance`` as the four above anyway, so adding
    # either flag to ``bool_arg_set`` would read as "no spec" rather than
    # crashing on ``True.split()``.
    convert_audio: str | bool  # "ext [+|-] ext..." spec
    convert_video: str | bool  # same shape as convert_audio

    # Loose on purpose, and it changes shape twice: the arg parser fills a bare
    # ``set`` of preset names, ``TaskConfig.__init__`` defaults it to None, and
    # ``_resolve_ffmpeg_commands`` replaces it with the list of command lines
    # ``proceed_ffmpeg`` actually runs.
    ffmpeg_cmds: set[Any] | list[Any] | None

    # upload switches
    as_doc: bool
    as_med: bool
    bot_trans: bool
    user_trans: bool

    if TYPE_CHECKING:
        # Methods a mixin calls on a sibling or on ``TaskListener``. Declared
        # under TYPE_CHECKING so this class stays empty at runtime -- a real
        # body here could shadow the implementation instead of documenting it.

        # BatchTrackerMixin
        def _batch(self) -> dict[str, Any] | None: ...

        async def _record(
            self,
            *,
            done: int = 0,
            error: Any = None,
            result: Any = None,
        ) -> None: ...

        # TaskListener
        async def remove_from_same_dir(self) -> None: ...
