"""Named sets of chats a task's uploads are copied to.

``CLONE_DUMP_CHATS`` is one flat list shared by every task, so a user who wants
different targets for different content has to retype it. A copy preset is that
list given a name, picked per task with ``-c <name>``.

The rules live here rather than in the menu because two places enforce them: the
editor that stores a preset and the resolver that reads one back. Names are the
fussy part -- they travel through telegram callback data, which is split on
whitespace and capped at 64 bytes, and through ``arg_parser``, which joins a
flag's value with spaces. A name with a space in it would break the menu's
routing outright, so it is refused at the point it is typed.
"""

from re import fullmatch
from typing import Any

MAX_PRESETS = 5
"""How many presets one user may keep."""

MAX_DESTS = 5
"""How many destinations one preset may hold."""

NAME_PATTERN = r"[A-Za-z0-9_-]{1,24}"
"""Letters, digits, dash and underscore. No whitespace, and short enough that
``userset <user id> copyp <name> <verb>`` stays inside the callback data limit.
"""


def valid_name(name):
    """Whether *name* is usable as a preset name."""
    return bool(name) and fullmatch(NAME_PATTERN, name) is not None


def presets_of(user_dict):
    """The user's presets, as a mapping -- ``{}`` when they have none.

    Tolerates the key being absent, empty, or left as the ``""`` that removing
    an option writes, so callers can index the result without checking first.
    """
    presets = user_dict.get("COPY_PRESETS")
    return presets if isinstance(presets, dict) else {}


def parse_destinations(text):
    """The destinations in *text*, or a complaint about the first bad one.

    Returns ``(destinations, error)``: one of the two is always empty. Users
    paste these a group at a time, so newlines, commas and plain spaces all
    separate -- the shapes themselves never contain any of the three.

    Only the shape is checked here. Whether the bot can actually post to a
    destination is a question for telegram, asked once per task before the
    download starts.
    """
    found = []
    for raw in text.replace(",", "\n").split():
        entry = raw.strip()
        if not entry:
            continue
        error = _shape_error(entry)
        if error:
            return [], error
        if entry not in found:
            found.append(entry)
    if not found:
        return [], "No destination found in that message."
    return found, ""


def additions_to(existing, found):
    """Which of *found* to add to *existing*, or why they will not fit.

    Returns ``(additions, error)``: one of the two is always empty. A chat the
    preset already holds is not an addition and does not count against the
    limit, so re-sending a list with one new entry in it does the obvious thing
    rather than complaining about the ones that are already there.
    """
    fresh = [entry for entry in found if entry not in existing]
    room = MAX_DESTS - len(existing)
    if len(fresh) > room:
        return [], (
            f"that is {len(fresh)} new destinations and there is room for"
            f" {room} -- a preset holds {MAX_DESTS}."
        )
    return fresh, ""


def _shape_error(entry):
    """Why *entry* cannot be a destination, or ``""`` when it can.

    ``chat|thread`` addresses one topic of a forum, a bare value addresses a
    whole chat, and ``pm`` is the requester's own chat -- the three shapes
    ``as_dump_target`` already understands.
    """
    chat, sep, thread = entry.partition("|")
    if entry.count("|") > 1:
        return f"<code>{entry}</code> has more than one <code>|</code> in it."
    if not chat:
        return f"<code>{entry}</code> is missing the chat before the <code>|</code>."
    if sep and not thread.lstrip("-").isdigit():
        return f"<code>{thread}</code> is not a thread id."
    if chat.lower() == "pm" or chat.startswith("@"):
        return ""
    if not chat.lstrip("-").isdigit():
        return (
            f"<code>{chat}</code> is not a chat id. Use the numeric id, a"
            " @username, or <code>pm</code>."
        )
    return ""


def as_chat_id(value: str) -> int | str:
    """A chat or thread id as an int when it looks numeric, else untouched."""
    return int(value) if value.lstrip("-").isdigit() else value


def as_dump_target(entry: Any, user_id: int) -> tuple[Any, int | str | None]:
    """One stored destination as a ``(chat_id, thread_id)`` pair.

    ``pm`` is whose chat it means that decides: the id is a parameter because
    the reader is not always the owner -- ``/copy`` resolves a preset of one
    user on behalf of another.
    """
    if not isinstance(entry, str):
        return entry, None
    if "|" in entry:
        chat, thread = entry.split("|", 1)
        return as_chat_id(chat), as_chat_id(thread)
    if entry.lower() == "pm":
        return user_id, None
    return as_chat_id(entry), None
