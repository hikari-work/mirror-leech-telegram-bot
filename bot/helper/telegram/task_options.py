"""The option keyboard a ``/leech`` or ``/ytdl`` offers before the task starts.

Flags used to be something you typed and then lived with: a forgotten ``-e`` or a
mistyped ``-c`` name meant cancelling the task and sending the command again.
The keyboard moves that decision to where it is still free -- after the command
was parsed and validated, but before anything has been downloaded.

Three pieces live here:

* the *spec* -- which flags the keyboard offers and which commands take them;
* the *state* -- pure functions over one plain dict, so what a button does can
  be tested without telegram (``state_from`` / ``toggle`` / ``select_preset`` /
  ``set_value`` / ``apply_state`` / ``render``);
* the *exchange* -- the prompt message, the ``pending`` registry the ``^lopt``
  callback reads, and the wait that holds the task until Start or Cancel.

The state is written back onto the parsed args and the task is re-settled from
there, so the flag-to-attribute mapping stays in one place
(``CommandTask._apply_args``) instead of growing a second copy here.
"""

from __future__ import annotations

from asyncio import Future, get_running_loop, wait_for
from dataclasses import dataclass
from html import escape
from time import time
from typing import TYPE_CHECKING, Any

from ... import LOGGER
from ..storage.copy_presets import presets_of
from ..util.bot_utils import get_size_bytes
from .button_build import ButtonMaker
from .conversation import wait_for_reply
from .message_utils import (
    auto_delete_message,
    chat_of,
    delete_message,
    edit_message,
    send_message,
)

if TYPE_CHECKING:
    from pyrogram import Client
    from pyrogram.types import CallbackQuery, InlineKeyboardMarkup, Message

    from ..listeners.command_task import CommandTask

PREFIX = "lopt"
"""Callback-data prefix the handler is registered under."""

PROMPT_TTL = 600
"""Seconds a keyboard stays answerable before the task is dropped.

The wait is open-ended by design -- a task runs when the user presses Start --
but a keyboard nobody presses would otherwise park its task and its ``pending``
entry for the life of the process. Ten minutes is long enough to read a message
and decide, and the expiry says so in the chat instead of leaving buttons that
look alive.
"""

VALUE_TIMEOUT = 60
"""Seconds the bot waits for the text behind ``-n`` or ``-sp``."""

BOOL, VALUE, EXTRA, PRESET = "bool", "value", "extra", "preset"
"""What a button does.

``bool`` switches on and off, ``value`` is a flag whose whole answer is what the
user types, ``extra`` is a flag that is on or off *and* can carry a password
(``-e`` and ``-z`` both read theirs from the command line), and ``preset`` is
one of the user's copy destinations, which live on their own page.
"""

MAIN_VIEW, PRESET_VIEW = "main", "presets"
"""Which page of the keyboard is on screen, one message with two of them."""


@dataclass(frozen=True)
class Option:
    """One button: the flag it stands for, and what it writes to the args."""

    label: str
    """What the button says, before the on/off mark and any value."""

    field: str
    """The attribute this flag lands on in the parsed args dataclass."""

    kind: str
    """``bool``, ``value``, ``extra`` (see the constants above) or ``preset``."""

    group: str = ""
    """Flags sharing a group are mutually exclusive; "" means it stands alone."""

    commands: tuple[str, ...] = ("leech", "ytdl")
    """Which commands offer it."""


OPTIONS: dict[str, Option] = {
    "-n": Option("Name", "name", VALUE),
    "-sp": Option("Split", "split_size", VALUE),
    "-c": Option("Copy Destination", "copy_preset", PRESET),
    "-z": Option("Zip", "compress", EXTRA),
    "-e": Option("Extract", "extract", EXTRA, commands=("leech",)),
    "-s": Option("Select", "select", BOOL),
    "-ss": Option("Screenshots", "screen_shots", BOOL),
    "-doc": Option("As doc", "as_doc", BOOL, group="format"),
    "-med": Option("As media", "as_med", BOOL, group="format"),
    "-su": Option("Stream", "stream_upload", BOOL, commands=("leech",)),
    "-s3": Option("S3", "is_s3", BOOL, group="destination"),
    "-tg": Option("Telegram", "is_tg", BOOL, group="destination"),
}
"""The flags this keyboard offers, in the order their buttons appear.

Deliberately not every flag: ``-b`` and ``-i`` change how the command is
dispatched, and a task that has been dispatched cannot be asked to dispatch
differently. The rest either need the command line (``-ff``, ``-ca``/``-cv``)
or are their own prompt (``-t``, headers, debrid passwords).
"""


def options_for(command: str) -> list[str]:
    """The flags offered to *command*, in display order."""
    return [flag for flag, spec in OPTIONS.items() if command in spec.commands]


# ── state ───────────────────────────────────────────────────────────


def state_from(args: Any, command: str) -> dict[str, Any]:
    """The starting state: a flag the user typed shows up already switched on."""
    return {
        flag: getattr(args, OPTIONS[flag].field) for flag in options_for(command)
    }


def toggle(state: dict[str, Any], flag: str) -> None:
    """Switch *flag* on or off, dropping whichever flag it excludes."""
    spec = OPTIONS[flag]
    state[flag] = not state[flag]
    if not state[flag] or not spec.group:
        return
    for other, other_spec in OPTIONS.items():
        if other != flag and other_spec.group == spec.group:
            state[other] = False


def select_preset(state: dict[str, Any], name: str) -> None:
    """Press a preset button: that preset, or none when it was the one on."""
    state["-c"] = "" if state["-c"] == name else name


def set_value(state: dict[str, Any], flag: str, text: str) -> str:
    """Store what the user typed for ``-n`` or ``-sp``, or explain the refusal.

    Returns "" when the value was taken, and a message for the chat when it was
    not -- the caller shows it and keeps the keyboard up.
    """
    text = text.strip()
    if not text:
        return "That was an empty message, so nothing was set."
    if flag == "-sp":
        error = _split_size_error(text)
        if error:
            return error
    state[flag] = text
    return ""


def _split_size_error(text: str) -> str:
    """Why *text* is not a split size, or "" when it is.

    The same two shapes ``_resolve_split_sizes`` accepts, asked one step
    earlier: plain bytes, or ``get_size_bytes``'s "2g" / "500mb". Nothing else
    is refused there -- it quietly falls back to the user's setting -- and a
    task that splits at a size nobody asked for is worse than being told the
    value was read wrong.
    """
    if text.isdigit():
        return "" if int(text) > 0 else "A split size has to be more than zero."
    try:
        size = get_size_bytes(text)
    except ValueError:
        size = 0
    if size > 0:
        return ""
    return (
        f"<code>{escape(text)}</code> is not a size."
        " Use 500mb, 2gb, or a number of bytes."
    )


def apply_state(state: dict[str, Any], args: Any) -> None:
    """Write the keyboard's state back onto the parsed args."""
    for flag, value in state.items():
        setattr(args, OPTIONS[flag].field, value)


# ── rendering ───────────────────────────────────────────────────────


def render(
    state: dict[str, Any],
    presets: dict[str, Any],
    link: str,
    command: str,
    user_id: int,
    view: str = MAIN_VIEW,
) -> tuple[str, InlineKeyboardMarkup]:
    """The prompt text and the keyboard that goes under it.

    One message, two pages. The flags are the page a task normally needs, and
    the copy destinations have one of their own: a user with five presets would
    otherwise push everything else off a keyboard that has to stay readable.
    """
    buttons = ButtonMaker()
    if view == PRESET_VIEW:
        for name in _preset_names(state, presets):
            buttons.data_button(
                f"{_mark(state['-c'] == name)} {name}",
                _data(user_id, f"p:{name}"),
            )
        buttons.data_button("↩️ Back", _data(user_id, "b"), "footer")
    else:
        for flag in options_for(command):
            if OPTIONS[flag].kind != PRESET:
                buttons.data_button(
                    _flag_label(state, flag), _data(user_id, f"t:{flag}")
                )
        if presets or state["-c"]:
            buttons.data_button(_copy_label(state), _data(user_id, "c"))
        buttons.data_button("▶️ Start", _data(user_id, "go"), "footer")
    buttons.data_button("✖️ Cancel", _data(user_id, "x"), "footer")
    return _text(state, presets, link, view), buttons.build_menu(2)


def _copy_label(state: dict[str, Any]) -> str:
    """The way into the preset page, which also says what it would take."""
    name = state["-c"]
    return f"📁 Copy Destination: {name}" if name else "📁 Copy Destination"


def _data(user_id: int, action: str) -> str:
    """The callback data of one button, which every press re-checks the owner of."""
    return f"{PREFIX} {user_id} {action}"


def _mark(on: bool) -> str:
    return "✅" if on else "⬜"


def _flag_label(state: dict[str, Any], flag: str) -> str:
    """One flag's button text: its state, its typed value, or that it has one."""
    spec = OPTIONS[flag]
    value = state[flag]
    if spec.kind == VALUE:
        return f"✏️ {spec.label}: {value}" if value else f"✏️ {spec.label}"
    if isinstance(value, str):
        # a password for -e / -z or a count for -ss: on, but not printed back
        return f"{_mark(True)} {spec.label} 🔒"
    return f"{_mark(bool(value))} {spec.label}"


def _preset_names(state: dict[str, Any], presets: dict[str, Any]) -> list[str]:
    """The preset buttons: the user's, plus a typed name that matches none.

    A ``-c`` name that was typo'd in the command would otherwise be invisible:
    its button would not exist, so there would be no way to switch it off.
    """
    names = list(presets)
    typed = state["-c"]
    if typed and typed not in presets:
        names.append(typed)
    return names


def _text(
    state: dict[str, Any],
    presets: dict[str, Any],
    link: str,
    view: str = MAIN_VIEW,
) -> str:
    lines = ["<b>Options for this task</b>"]
    if link:
        lines.append(f"<code>{escape(_short(link))}</code>")
    active = " ".join(_active_parts(state)) or "none"
    lines.append(f"\n<b>Active:</b> <code>{escape(active)}</code>")
    if view == PRESET_VIEW:
        lines.append(
            f"<b>Copy Destination:</b> <code>{escape(state['-c'] or 'none')}</code>"
        )
        lines.append("Press one to copy this task's upload there, or ↩️ Back.")
    elif state["-c"]:
        count = len(presets.get(state["-c"], []))
        lines.append(
            f"<b>Copy Destination:</b> <code>{escape(state['-c'])}</code> ({count})"
        )
    elif not presets:
        lines.append(
            "You have no copy presets. Make one in User Settings -> Leech ->"
            " Copy Presets."
        )
    # Both off is not "no format": it is the user's AS_DOCUMENT setting having
    # the last word, which the two blank boxes cannot say on their own.
    if not state["-doc"] and not state["-med"]:
        lines.append("As doc / As media: unset, your upload setting decides.")
    lines.append(
        "\nPress ▶️ Start to run the task, ✖️ Cancel to drop it. The keyboard"
        f" stops working after {PROMPT_TTL // 60} minutes."
    )
    return "\n".join(lines)


def _active_parts(state: dict[str, Any]) -> list[str]:
    """The flags that are on, with their values and without their secrets."""
    parts = []
    for flag, value in state.items():
        if not value or flag == "-c":
            continue
        spec = OPTIONS[flag]
        if spec.kind == VALUE:
            parts.append(f"{flag} {value}")
        elif isinstance(value, str):
            parts.append(f"{flag} ••••")
        else:
            parts.append(flag)
    return parts


def _short(link: str) -> str:
    """A link short enough to sit in a one-line prompt."""
    return link if len(link) <= 80 else f"{link[:77]}..."


# ── the exchange ────────────────────────────────────────────────────


@dataclass
class Prompt:
    """One live keyboard, and the task waiting behind it."""

    user_id: int
    command: str
    args: Any
    state: dict[str, Any]
    presets: dict[str, Any]
    link: str
    listener: CommandTask
    future: Future[bool]
    at: float
    view: str = MAIN_VIEW


pending: dict[tuple[int | None, int], Prompt] = {}
"""``(chat id, prompt message id)`` -> the keyboard offered there.

In memory, like ``/copy``'s prompts: a restart expires every one of them at
once, and the task behind each was running in the process that went away.

The chat id is spelled optional because pyrogram calls it optional; both ends
of the key are read the same way, so a chat it cannot name still finds its own
key rather than missing it.
"""


def offers_keyboard(listener: CommandTask, args: Any) -> bool:
    """Whether this task gets a keyboard at all.

    Not everything that reaches a leech listener is one command a user is
    sitting in front of: an RSS feed calls the same handler and would stall
    behind a keyboard nobody is there to press, and a bulk or ``-i`` chain
    starts several tasks from one command, whose children inherit the parent's
    option string rather than its buttons.
    """
    if listener.is_rss or listener.bulk_child or args.is_bulk:
        return False
    # A keyboard belongs to whoever typed the command, and a channel post or an
    # anonymous admin has no such person: telegram names the chat as the author
    # while answering the press with the real admin's id, so every button would
    # come back "not yours". Those run as they did before instead of waiting out
    # the expiry for a button nobody can press.
    if getattr(listener.message, "sender_chat", None) is not None:
        return False
    # ``-i`` reads the parsed count rather than the task's copy of it: the two
    # agree by now, and the count is what the command said.
    return args.multi <= 1 and not listener.multi_tag


async def ask_options(
    listener: CommandTask, args: Any, command: str
) -> bool:
    """Offer the keyboard and hold the task until Start or Cancel.

    Returns True when the task should run. A keyboard telegram refused to send
    also returns True: the options already parsed are the ones the user typed,
    and refusing to run would be a worse answer to a rejected *edit* than
    running with what they asked for in the first place.
    """
    if not offers_keyboard(listener, args):
        return True
    # ``_apply_args`` writes the link back onto the task too, and by now the task
    # may hold one the parser never saw -- a command sent as a reply carries its
    # link in the message replied to, and only the reply resolver has read it.
    # Syncing it here is what keeps the first re-apply from undoing that.
    args.link = listener.link
    presets = presets_of(listener.user_dict)
    state = state_from(args, command)
    text, buttons = render(
        state, presets, listener.link, command, listener.user_id
    )
    message = await send_message(listener.message, text, buttons)
    if not hasattr(message, "id"):
        LOGGER.error(f"Option keyboard not sent: {message}")
        return True
    prompt = Prompt(
        user_id=listener.user_id,
        command=command,
        args=args,
        state=state,
        presets=presets,
        link=listener.link,
        listener=listener,
        future=get_running_loop().create_future(),
        at=time(),
    )
    key = (chat_of(listener.message).id, message.id)
    pending[key] = prompt
    try:
        return await wait_for(prompt.future, PROMPT_TTL)
    except TimeoutError:
        await edit_message(
            message,
            "This keyboard expired, so the task was not started. Send the"
            " command again to get a fresh one.",
        )
        return False
    finally:
        pending.pop(key, None)


async def task_options_callback(client: Client, query: CallbackQuery) -> None:
    """Answer one press of an option button.

    Registered under ``^lopt``: the data names the user whose keyboard this is,
    the action, and -- for a value or a preset -- which flag it belongs to. The
    prompt is looked up by message, the way ``/copy`` does, because the state
    behind the buttons is not something a callback can carry.
    """
    parts = (query.data if isinstance(query.data, str) else "").split()
    if len(parts) != 3 or not parts[1].isdigit():
        # data this handler did not write -- nothing to answer for, and nothing
        # to act on. The three `startswith` branches below can trust `parts[2]`.
        await query.answer()
        return
    prompt = _prompt_of(query)
    if prompt is None:
        # closed by a decision, or gone with a restart
        await query.answer("This keyboard is closed.", show_alert=True)
        return
    if int(parts[1]) != query.from_user.id:
        await query.answer("This keyboard is not yours!", show_alert=True)
        return
    if time() - prompt.at > PROMPT_TTL:
        _forget(query)
        await query.answer(
            "This keyboard expired. Send the command again.", show_alert=True
        )
        return
    action = parts[2]
    if action in ("go", "x"):
        await _close(query, prompt, run=action == "go")
        return
    if action in ("c", "b"):
        # the copy destinations, and the way back from them
        prompt.view = PRESET_VIEW if action == "c" else MAIN_VIEW
        await query.answer()
        await _redraw(query.message, prompt)
        return
    await _press_option(client, query, prompt, action)


async def _press_option(
    client: Client, query: CallbackQuery, prompt: Prompt, action: str
) -> None:
    """Answer a press on a flag or on a copy destination."""
    kind, _, value = action.partition(":")
    if kind == "p":
        select_preset(prompt.state, value)
    elif kind == "t" and value in options_for(prompt.command):
        toggle(prompt.state, value)
        if prompt.state[value] is True and OPTIONS[value].kind == EXTRA:
            # Switched on, and what this flag can carry is a password the user
            # may want to give it now -- the command line was the only way to
            # before, and the task has not started yet.
            await _ask_value(client, query, prompt, value)
            return
    elif kind == "v" and value in options_for(prompt.command):
        await _ask_value(client, query, prompt, value)
        return
    else:
        # an action, or a flag, that this keyboard never wrote
        await query.answer()
        return
    await query.answer()
    await _redraw(query.message, prompt)


def _prompt_of(query: CallbackQuery) -> Prompt | None:
    return pending.get((chat_of(query.message).id, query.message.id))


def _forget(query: CallbackQuery) -> None:
    pending.pop((chat_of(query.message).id, query.message.id), None)


async def _close(query: CallbackQuery, prompt: Prompt, run: bool) -> None:
    """Let the task go, or drop it, and take the buttons away either way."""
    await query.answer()
    _forget(query)
    if run:
        # Nothing left for it to say: the task's own status message is the next
        # thing in the chat, and a second line about the keyboard is noise.
        await delete_message(query.message)
    else:
        await edit_message(query.message, "Task cancelled.")
        await auto_delete_message(query.message)
    if not prompt.future.done():
        prompt.future.set_result(run)


async def _ask_value(
    client: Client, query: CallbackQuery, prompt: Prompt, flag: str
) -> None:
    """Take the text behind ``-n`` / ``-sp``, or the password behind ``-e`` / ``-z``.

    The two kinds ask for different things and answer nothing arriving in
    different ways: a name that never arrives leaves the flag as it was, while a
    password that never arrives leaves an extract switched on without one --
    which is a task that may well run, and a nested archive that may not need it.
    """
    spec = OPTIONS[flag]
    password = spec.kind == EXTRA
    await query.answer()
    ask = (
        f"Send the password for <code>{escape(spec.label)}</code>, or press a"
        " button on the keyboard to run it without one."
        if password
        else f"Send the <code>{escape(spec.label)}</code> for this task, or"
        " press a button on the keyboard when you are done."
    )
    await edit_message(query.message, f"{ask} Waiting {VALUE_TIMEOUT}s.")
    value = await wait_for_reply(
        client, query.message, prompt.user_id, VALUE_TIMEOUT
    )
    if value is None:
        note = (
            f"No password arrived, so {spec.label} runs without one."
            if password
            else "No value arrived, so nothing changed."
        )
        await _redraw(query.message, prompt, note)
        return
    error = set_value(prompt.state, flag, value)
    note = error or (
        f"{spec.label} password set." if password else f"{spec.label} set."
    )
    await _redraw(query.message, prompt, note)


async def _redraw(message: Message, prompt: Prompt, note: str = "") -> None:
    """Re-settle the task from the state, then show the state again.

    Every button press goes through here, so the args and the keyboard can
    never disagree: what the task will use is what the buttons say.
    """
    if pending.get((chat_of(message).id, message.id)) is not prompt:
        # The keyboard was closed while this press was in flight -- Start was
        # pressed during a value prompt, or the expiry passed. The task has
        # moved on, and redrawing would hang live-looking buttons back on a
        # message that is finished.
        return
    apply_state(prompt.state, prompt.args)
    prompt.listener.apply_options(prompt.args)
    text, buttons = render(
        prompt.state,
        prompt.presets,
        prompt.link,
        prompt.command,
        prompt.user_id,
        prompt.view,
    )
    if note:
        text += f"\n\n{note}"
    await edit_message(message, text, buttons, block=False)
