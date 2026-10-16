"""Tests for ``/pleech`` -- leeching out of a chat that has no link.

A private chat is the one place ``/leech`` cannot reach: telegram writes no link
for a DM, and the bot is not a party to it, so there is nothing to paste and
nothing to reply to. ``/pleech <chat> <count>`` closes that gap by looking the
last *count* messages up and turning every one that carries media into a link --
at which point the ordinary bulk takes over.

What is pinned here is the part before that handover: which messages are picked,
which link form each one gets, and which of the two clients does the reading.
Everything downstream is ``_handle_bulk``/``dispatch_bulk`` and is covered by
``test_bulk_same_dir.py``.
"""

from __future__ import annotations

from importlib import import_module
from types import SimpleNamespace

import pytest

from bot.helper.telegram.bot_commands import BotCommands
from bot.helper.telegram.message_utils import get_tg_link_message
from bot.modules.leech import Leech

# Not ``import bot.modules.private_leech as pl``: the package ``__init__``
# re-exports the *function* of the same name, which shadows the submodule
# attribute, and the module object is what the patching below needs.
pl = import_module("bot.modules.private_leech")

MEDIA_ATTRS = (
    "document",
    "photo",
    "video",
    "audio",
    "voice",
    "video_note",
    "sticker",
    "animation",
)


# ── fakes ───────────────────────────────────────────────────────────


class _Message:
    """A telegram message, carrying media only if it was given one."""

    document = photo = video = audio = None
    voice = video_note = sticker = animation = None
    empty = False
    text = None

    def __init__(self, message_id, media=None):
        self.id = message_id
        if media is not None:
            # documents are what real chat dumps are mostly made of
            self.document = media


class _CommandMessage:
    """The message a ``/pleech`` command arrived in."""

    def __init__(self, text, user_id=7, chat_id=99, message_id=400):
        self.id = message_id
        self.text = text
        self.caption = None
        self.from_user = SimpleNamespace(id=user_id)
        self.sender_chat = None
        self.chat = SimpleNamespace(id=chat_id, type=None)
        self.reply_to_message = None


class _Client:
    """Just the three calls the lookup makes.

    ``get_chat_history`` is an async generator in kurigram, not a coroutine, so
    the fake returns one too -- a plain coroutine here would make the module's
    ``async for`` fail rather than making this test meaningful.
    """

    def __init__(self, chat=None, messages=(), me_id=None, unreadable=False):
        self.chat = chat
        self.messages = list(messages)
        self.me = SimpleNamespace(id=me_id) if me_id is not None else None
        self.unreadable = unreadable
        self.asked_for: list[int] = []

    async def get_chat(self, spec):
        if self.unreadable:
            raise ValueError(f"no access to {spec}")
        return self.chat

    def get_chat_history(self, chat_id, limit=0):
        self.asked_for.append(chat_id)

        async def gen():
            # newest first, the way telegram answers
            for message in self.messages[:limit]:
                yield message

        return gen()


def _user_client_returning(client):
    async def get_user_client(user_id):
        return client

    return get_user_client


@pytest.fixture
def wires(monkeypatch):
    """Install a fake bot client and user session, and record what is sent.

    ``dispatch_bulk`` is what is replaced, not ``_handle_bulk`` -- so the real
    ``_handle_bulk`` still runs, still mints the ``multi_tag``, and still adds
    the ``-m`` that merges the batch into one folder.
    """
    sent: list[str] = []
    dispatched: dict = {}

    async def send_message(message, text, *args, **kwargs):
        sent.append(text)
        return SimpleNamespace(id=len(sent))

    async def dispatch_bulk(self, cmd, links, obj):
        dispatched.update(cmd=cmd, links=links, obj=obj, options=self.options)

    monkeypatch.setattr(pl, "send_message", send_message)
    monkeypatch.setattr(Leech, "dispatch_bulk", dispatch_bulk)

    def install(bot=None, user=None):
        monkeypatch.setattr(pl.TgClient, "bot", bot)
        monkeypatch.setattr(pl.TgClient, "user", None)
        monkeypatch.setattr(pl, "get_user_client", _user_client_returning(user))

    return SimpleNamespace(sent=sent, dispatched=dispatched, install=install)


def _run(text, user_id=7):
    return pl.PrivateLeech(object(), _CommandMessage(text, user_id=user_id)).new_event()


def _history(*ids_last_to_first):
    """Messages as telegram yields them: newest first."""
    return [_Message(message_id, media=object()) for message_id in ids_last_to_first]


# ── parsing ─────────────────────────────────────────────────────────


def test_spec_is_chat_count_and_options():
    assert pl.parse_spec(["@somebot", "10", "-c", "preset"]) == (
        "@somebot",
        10,
        "-c preset",
    )


def test_options_are_optional():
    assert pl.parse_spec(["@somebot", "3"]) == ("@somebot", 3, "")


@pytest.mark.parametrize("tokens", [[], ["@somebot"]])
def test_a_missing_count_is_refused(tokens):
    with pytest.raises(ValueError, match="both required"):
        pl.parse_spec(tokens)


@pytest.mark.parametrize("count", ["0", "101", "-4"])
def test_a_count_outside_the_range_is_refused(count):
    with pytest.raises(ValueError, match="between 1 and 100"):
        pl.parse_spec(["@somebot", count])


def test_a_count_that_is_not_a_number_is_refused():
    with pytest.raises(ValueError, match="is not a message count"):
        pl.parse_spec(["@somebot", "-c"])


# ── link form ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "chat_id,bot_readable,username,expected",
    [
        # a chat the bot can read and name: the bot reads it, no session needed
        (-1001234567890, True, "somechannel", "https://t.me/somechannel/5"),
        # a channel the bot can read but that has no username
        (-1001234567890, True, None, "https://t.me/c/1234567890/5"),
        # a DM, and anything only the user's session can open
        (123456, False, None, "tg://openmessage?user_id=123456&message_id=5"),
        # a plain group: an id of its own, no -100 to strip
        (-987654, False, None, "tg://openmessage?user_id=-987654&message_id=5"),
        # a username is no help when the bot is not the reader: the user session
        # is what opens a tg:// link, and that is the form it can read
        (
            -1001234567890,
            False,
            "somechannel",
            "tg://openmessage?user_id=-1001234567890&message_id=5",
        ),
    ],
)
def test_the_link_form_follows_who_can_read_the_chat(
    chat_id, bot_readable, username, expected
):
    assert (
        pl.link_for(chat_id, 5, bot_readable=bot_readable, username=username)
        == expected
    )


async def test_a_channel_link_round_trips_through_the_parser(monkeypatch):
    """The ``c/`` form strips the ``-100``; the parser has to put it back.

    Getting this wrong is silent: the link still parses, it just names a chat
    that does not exist. So the link ``link_for`` builds is fed to the real
    parser rather than compared against an expected string.
    """
    asked = {}

    async def get_messages(chat_id, message_ids):
        asked["chat_id"] = chat_id
        return _Message(5, object())

    monkeypatch.setattr(pl.TgClient, "bot", SimpleNamespace(get_messages=get_messages))
    monkeypatch.setattr(pl, "get_user_client", _user_client_returning(None))
    monkeypatch.setattr(pl.TgClient, "user", None)

    link = pl.link_for(-1001234567890, 5, bot_readable=True, username=None)
    _, session = await get_tg_link_message(link, user_id=7)

    assert link == "https://t.me/c/1234567890/5"
    assert asked["chat_id"] == -1001234567890
    assert session == "bot"


# ── dispatch ────────────────────────────────────────────────────────


async def test_only_the_messages_with_media_become_tasks(wires):
    bot = _Client(
        chat=SimpleNamespace(id=123456, username=None),
        messages=[_Message(14, object()), _Message(13), _Message(12, object())],
    )
    wires.install(bot=bot)

    await _run("/pleech @someone 3")

    assert wires.dispatched["links"] == [
        "tg://openmessage?user_id=123456&message_id=12",
        "tg://openmessage?user_id=123456&message_id=14",
    ]


async def test_the_oldest_message_is_dispatched_first(wires):
    """get_chat_history answers newest first; the batch must run oldest first."""
    bot = _Client(
        chat=SimpleNamespace(id=123456, username=None),
        messages=_history(30, 20, 10),
    )
    wires.install(bot=bot)

    await _run("/pleech @someone 3")

    ids = [link.rsplit("=", 1)[1] for link in wires.dispatched["links"]]
    assert ids == ["10", "20", "30"]


async def test_the_batch_is_merged_into_one_folder(wires):
    """``_handle_bulk`` forces ``-m bulk-<tag>``, which is what groups the files."""
    bot = _Client(
        chat=SimpleNamespace(id=123456, username=None),
        messages=_history(2, 1),
    )
    wires.install(bot=bot)

    listener = pl.PrivateLeech(
        object(), _CommandMessage("/pleech @someone 2 -c preset")
    )
    await listener.new_event()

    assert listener.multi_tag
    assert listener.options.startswith("-c preset")
    assert f"-m bulk-{listener.multi_tag}" in listener.options


async def test_the_children_are_ordinary_leech_tasks(wires):
    bot = _Client(
        chat=SimpleNamespace(id=123456, username=None),
        messages=_history(1),
    )
    wires.install(bot=bot)

    await _run("/pleech @someone 1")

    assert wires.dispatched["obj"] is Leech
    assert wires.dispatched["cmd"] == BotCommands.LeechCommand[0]


async def test_the_count_is_how_many_messages_are_scanned(wires):
    bot = _Client(
        chat=SimpleNamespace(id=123456, username=None),
        messages=_history(9, 8, 7, 6, 5, 4),
    )
    wires.install(bot=bot)

    await _run("/pleech @someone 3")

    # limit is how far back the scan went, and it reached the history reader
    assert len(wires.dispatched["links"]) == 3


# ── which client reads ──────────────────────────────────────────────


async def test_the_user_session_is_used_when_the_bot_cannot_read(wires):
    user = _Client(
        chat=SimpleNamespace(id=123456, username=None),
        messages=_history(2),
    )
    wires.install(bot=_Client(unreadable=True), user=user)

    await _run("/pleech @someone 5")

    assert user.asked_for == [123456]
    assert wires.dispatched["links"] == ["tg://openmessage?user_id=123456&message_id=2"]


async def test_a_chat_the_bot_can_read_does_not_need_a_session(wires):
    bot = _Client(
        chat=SimpleNamespace(id=-1001234567890, username="somechannel"),
        messages=_history(3),
    )
    wires.install(bot=bot, user=None)

    await _run("/pleech @somechannel 5")

    assert bot.asked_for == [-1001234567890]
    assert wires.dispatched["links"] == ["https://t.me/somechannel/3"]


async def test_a_chat_nobody_can_read_says_to_log_in(wires):
    wires.install(bot=_Client(unreadable=True), user=None)

    await _run("/pleech @someone 5")

    assert wires.dispatched == {}
    assert "login" in wires.sent[-1]


async def test_an_unreadable_chat_reports_the_reason(wires):
    wires.install(
        bot=_Client(unreadable=True),
        user=_Client(unreadable=True),
    )

    await _run("/pleech @someone 5")

    assert wires.dispatched == {}
    assert "Could not read" in wires.sent[-1]


async def test_me_reads_saved_messages(wires):
    user = _Client(
        me_id=7, chat=SimpleNamespace(id=7, username=None), messages=_history(4)
    )
    wires.install(bot=_Client(unreadable=True), user=user)

    await _run("/pleech me 5")

    assert user.asked_for == [7]
    assert wires.dispatched["links"] == ["tg://openmessage?user_id=7&message_id=4"]


async def test_me_without_a_session_says_to_log_in(wires):
    wires.install(bot=_Client(unreadable=True), user=None)

    await _run("/pleech me 5")

    assert wires.dispatched == {}
    assert "login" in wires.sent[-1]


# ── nothing to do ───────────────────────────────────────────────────


async def test_a_history_with_no_media_is_reported(wires):
    bot = _Client(
        chat=SimpleNamespace(id=123456, username=None),
        messages=[_Message(2), _Message(1)],
    )
    wires.install(bot=bot)

    await _run("/pleech @someone 2")

    assert wires.dispatched == {}
    assert "No media in the last 2 messages" in wires.sent[-1]


async def test_an_empty_history_is_reported(wires):
    bot = _Client(chat=SimpleNamespace(id=123456, username=None), messages=[])
    wires.install(bot=bot)

    await _run("/pleech @someone 2")

    assert wires.dispatched == {}
    assert "No media" in wires.sent[-1]


# ── bad commands ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("/pleech", "both required"),
        ("/pleech @someone", "both required"),
        ("/pleech @someone 0", "between 1 and 100"),
        ("/pleech @someone 101", "between 1 and 100"),
        ("/pleech @someone lots", "is not a message count"),
    ],
)
async def test_a_bad_command_is_shown_the_usage(wires, text, expected):
    wires.install(bot=_Client(unreadable=True), user=None)

    await _run(text)

    assert wires.dispatched == {}
    assert expected in wires.sent[-1]
    assert "chat count" in wires.sent[-1]
