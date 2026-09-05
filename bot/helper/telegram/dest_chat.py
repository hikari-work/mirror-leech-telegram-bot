"""Upload-destination checks, shared by the tasks of a batch.

``before_start`` validates the destination for every task it runs: ``get_chat``,
then ``get_chat_member`` for the privileges, or a typing action for a private
chat. That is one to two calls per task about a chat that does not change, so a
bulk of a hundred links asks Telegram the same questions a hundred times -- and
the bot client runs with ``sleep_threshold=0``, which means pyrogram hands the
FloodWait to us instead of sitting it out.

The old code read any exception from those calls as an answer about the chat:
``except Exception: chat = None`` followed by ``raise ValueError("Chat not
found!")``. A rate limit therefore came out as a dead destination, on links the
gateway had resolved perfectly well -- and the batch summary blamed the links.

What this module does about it, in order of how much it matters:

- One lookup per chat per ``CHAT_TTL``, so a batch asks once instead of once per
  task. That removes the flood instead of coping with it. Concurrent first
  callers queue on one lock, so they share the single lookup rather than racing
  to repeat it.
- A FloodWait is waited out, honouring the wait Telegram asked for plus jitter
  (a batch floods in lockstep, so a fixed retry only replays the same burst).
  How long is worth waiting depends on what the alternative is: with an answer
  already cached, none -- fall back and let the batch move. With nothing cached
  the alternative is failing a task that is otherwise fine, which is worth
  minutes of waiting.
- Only with nothing to fall back on does a lookup raise, and it raises
  ``ChatLookupError`` -- "could not ask", which is not "not there" and must
  never be reported as such.
"""

from asyncio import Lock, sleep
from random import uniform
from time import monotonic
from typing import Any

from pyrogram.enums import ChatAction
from pyrogram.errors import FloodPremiumWait, FloodWait
from pyrogram.types import Chat, ChatMember

from ... import LOGGER
from ...core.telegram_manager import TgClient, own_account

CHAT_TTL = 300
"""Seconds an answer about a chat is reused before it is looked up again.

Long enough to cover the dispatch of a large bulk (a hundred links at
``BULK_SPAWN_DELAY`` apart), short enough that a demotion or a changed
destination is picked up on the next task rather than needing a restart.
"""

ATTEMPTS = 3
"""Tries for a lookup that has an answer to fall back on."""

MAX_FLOOD_WAIT = 30
"""With an answer in hand, a flood longer than this is not worth waiting for."""

MAX_FLOOD_TOTAL = 180
"""With no answer at all, how long to keep waiting before giving up on it.

Generous on purpose: the task has nothing else to do, and the only other outcome
is failing a link whose download would have worked.
"""

FLOOD_COOLDOWN = 15
"""Seconds the tasks queued behind a failed lookup skip retrying it.

Otherwise the hundred tasks of a batch take the lock one after another and each
spends the full wait budget on a flood that is clearly still going.
"""

_RAISE = object()
_MISS = object()

_cache = {}
_failures = {}
_locks = {}


class ChatLookupError(Exception):
    """Telegram could not be asked. Says nothing about the chat itself."""


def reset_dest_cache() -> None:
    """Forget every cached answer. Used by tests to keep runs hermetic."""
    _cache.clear()
    _failures.clear()
    _locks.clear()


def _client_key(client):
    """Identity of the session a lookup was made with.

    The bot and the user session get different answers about the same chat (one
    is admin, the other may not even see it), so they cannot share an entry.
    """
    return getattr(client, "name", None) or id(client)


def _fresh(key):
    entry = _cache.get(key)
    if entry is not None and monotonic() - entry["at"] < CHAT_TTL:
        return entry["value"]
    return _MISS


def _remember(key, value):
    _cache[key] = {"value": value, "at": monotonic()}
    _failures.pop(key, None)


def _waitable(key, attempt, wait, waited) -> bool:
    """Whether to sit out a flood of *wait* seconds instead of falling back."""
    if key in _cache:
        return attempt < ATTEMPTS and wait <= MAX_FLOOD_WAIT
    # nothing to fall back on: waiting beats failing the task
    return waited + wait <= MAX_FLOOD_TOTAL


def _recent_failure(key) -> None:
    """Re-raise a lookup that just failed instead of repeating its wait."""
    failure = _failures.get(key)
    if failure is not None and monotonic() - failure["at"] < FLOOD_COOLDOWN:
        raise ChatLookupError(failure["error"])


def _fall_back(key, what, error):
    """Reuse the last known answer for *key*, or admit we do not have one."""
    entry = _cache.get(key)
    if entry is None:
        _failures[key] = {"at": monotonic(), "error": str(error)}
        raise ChatLookupError(f"{error}") from error
    LOGGER.warning(
        f"Telegram is rate limiting the check of {what} ({error}); "
        "reusing the last known answer"
    )
    # asking again while the flood lasts only feeds it, and the answer we have
    # is the answer -- it just aged
    entry["at"] = monotonic()
    return entry["value"]


async def _ask(key, call, what, on_definite) -> Any:
    """Run *call*, retrying what a retry can fix.

    A definite answer -- a bad peer, a missing right -- is returned as
    *on_definite* and cached, or re-raised when the caller passed no default.
    """
    attempt = 0
    waited = 0.0
    while True:
        attempt += 1
        try:
            value = await call()
        except (FloodWait, FloodPremiumWait) as f:
            # floor the accounting at a second: a stream of zero-second waits
            # would otherwise never spend the budget
            wait = max(float(getattr(f, "value", 0) or 0), 1.0)
            if not _waitable(key, attempt, wait, waited):
                return _fall_back(key, what, f)
            LOGGER.info(
                f"Destination check of {what}: waiting out a {wait:.0f}s "
                f"flood [try {attempt}]"
            )
            waited += wait
            await sleep(wait * 1.2 + uniform(0, 1.5))
        except (TimeoutError, OSError) as e:
            if attempt >= ATTEMPTS:
                return _fall_back(key, what, e)
            await sleep(2**attempt + uniform(0, 1.5))
        except Exception as e:
            if on_definite is _RAISE:
                raise
            LOGGER.warning(f"Destination check of {what} failed: {e}")
            _remember(key, on_definite)
            return on_definite
        else:
            _remember(key, value)
            return value


async def _lookup(key, call, what, on_definite=_RAISE) -> Any:
    value = _fresh(key)
    if value is not _MISS:
        return value
    _recent_failure(key)
    lock = _locks.setdefault(key, Lock())
    async with lock:
        # a sibling task may have answered it while this one queued on the lock;
        # without this every task of a batch that started together still asks
        value = _fresh(key)
        if value is not _MISS:
            return value
        _recent_failure(key)
        return await _ask(key, call, what, on_definite)


async def get_dest_chat(client, chat_id) -> Chat | None:
    """The destination chat, or *None* when Telegram says it is not usable.

    Raises ``ChatLookupError`` when the lookup could not be completed: the
    caller must not turn that into "Chat not found!".
    """
    return await _lookup(
        ("chat", _client_key(client), chat_id),
        lambda: client.get_chat(chat_id),
        f"chat {chat_id}",
        on_definite=None,
    )


async def get_dest_member(client, chat_id, user_id) -> ChatMember:
    """*user_id*'s membership in *chat_id*, looked up with *client*.

    Definite errors propagate -- the caller decides what a missing membership
    means. ``ChatLookupError`` still means "could not ask".
    """
    return await _lookup(
        ("member", _client_key(client), chat_id, user_id),
        lambda: client.get_chat_member(chat_id, user_id),
        f"membership of {user_id} in {chat_id}",
    )


async def can_reach_dest(client, chat_id) -> bool:
    """Whether *client* can write to a private destination.

    Probing with a typing action is how a "start the bot first" destination is
    told apart from a usable one; the answer is cached like the others because
    the probe itself floods when a batch repeats it per task.
    """

    async def _probe():
        await client.send_chat_action(chat_id, ChatAction.TYPING)
        return True

    return await _lookup(
        ("reach", _client_key(client), chat_id),
        _probe,
        f"access to chat {chat_id}",
        on_definite=False,
    )


# A destination the bot can post many files into and clean up after; anything
# else (a PM, most of all) is handled as a plain chat.
GROUP_CHAT_TYPES = ("SUPERGROUP", "CHANNEL", "GROUP", "FORUM")


def is_group_chat(chat: Chat) -> bool:
    """Whether a destination is a group or channel rather than a plain chat.

    ``Chat.type`` is optional in pyrogram, and an answer without one is no
    evidence that the destination is a group, so it takes the same path a PM
    does.
    """
    return chat.type is not None and chat.type.name in GROUP_CHAT_TYPES


def can_manage_and_delete(member: ChatMember) -> bool:
    """Whether an account can both manage the chat and delete messages in it."""
    return member.privileges.can_manage_chat and member.privileges.can_delete_messages


async def verify_copy_target(entry: str, chat_id: int | str) -> None:
    """Stop the task unless the bot can post copies into *chat_id*.

    Checked as the bot, whichever session is uploading: the copies are sent
    with ``TgClient.bot`` regardless, so its rights are the ones that decide
    whether they will arrive.

    Unlike the upload destination there is no degraded mode to fall back to
    -- a copy target that cannot be verified has no second session to try --
    so ``ChatLookupError`` stops the task too, and says that it was the
    check that failed rather than the chat that is missing.
    """
    try:
        chat = await get_dest_chat(TgClient.bot, chat_id)
    except ChatLookupError as e:
        raise ValueError(
            f"Can't check copy destination {entry} right now: {e}."
            " Try again in a moment."
        ) from e
    if chat is None:
        raise ValueError(
            f"Copy destination {entry} was not found. Add the bot to it first."
        )
    if not is_group_chat(chat):
        await verify_copy_target_reachable(entry, chat_id)
        return
    if not chat.is_admin:
        raise ValueError(f"Bot is not admin in copy destination {entry}!")
    try:
        member = await get_dest_member(
            TgClient.bot, chat.id, own_account(TgClient.bot).id
        )
    except ChatLookupError as e:
        raise ValueError(
            f"Can't check the bot's privileges in copy destination {entry}:"
            f" {e}. Try again in a moment."
        ) from e
    if not can_manage_and_delete(member):
        raise ValueError(
            f"Not enough privileges in copy destination {entry}! Enable"
            " manage chat and delete messages for this bot."
        )


async def verify_copy_target_reachable(entry: str, chat_id: int | str) -> None:
    """Check a non-group copy destination has actually started the bot."""
    try:
        reachable = await can_reach_dest(TgClient.bot, chat_id)
    except ChatLookupError as e:
        raise ValueError(
            f"Can't check copy destination {entry} right now: {e}."
            " Try again in a moment."
        ) from e
    if not reachable:
        raise ValueError(
            f"Copy destination {entry} has not started the bot. Start it"
            " and try again."
        )
