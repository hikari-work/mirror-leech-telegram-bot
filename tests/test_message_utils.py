"""Tests for ``bot/helper/telegram/message_utils.py``.

Two things here are shared policy rather than plumbing. ``message_media`` is the
one answer to "which file is this message about", used by the leech command, the
telegram downloader and the private-chat lookup -- if those disagreed, a message
could count as carrying media and then produce no file. And ``get_tg_link_message``
decides which link forms it will read at all, which is what the ``tg://`` branch
below pins.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import bot.helper.telegram.message_utils as mu

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


class _Message:
    """A message that carries exactly the media it was built with."""

    document = photo = video = audio = None
    voice = video_note = sticker = animation = None
    empty = False

    def __init__(self, message_id=1, **media):
        self.id = message_id
        for attr in MEDIA_ATTRS:
            if attr in media:
                setattr(self, attr, media[attr])


# ── message_media ───────────────────────────────────────────────────


def test_a_message_with_no_media_has_none():
    assert mu.message_media(_Message()) is None


@pytest.mark.parametrize("attr", MEDIA_ATTRS)
def test_every_kind_of_media_is_found(attr):
    media = object()

    assert mu.message_media(_Message(**{attr: media})) is media


def test_the_earlier_entry_in_the_chain_wins():
    """A document beats a photo beats a video, whatever order they arrived in.

    The chain is the shared answer to "which file", so it cannot depend on the
    call site. Anyone changing this order changes what every task downloads.
    """
    document, photo, video = object(), object(), object()

    assert mu.message_media(_Message(document=document, photo=photo)) is document
    assert mu.message_media(_Message(photo=photo, video=video)) is photo
    assert mu.message_media(_Message(video=video)) is video


# ── get_tg_link_message: the tg:// branch ───────────────────────────


class _UserClient:
    """A user session that answers ``get_messages`` with one message."""

    def __init__(self, message=None):
        self.message = message if message is not None else _Message()
        self.asked: list[tuple[int, int]] = []

    async def get_messages(self, chat_id, message_ids):
        self.asked.append((chat_id, message_ids))
        return self.message


def _user_client_returning(client):
    async def get_user_client(user_id):
        return client

    return get_user_client


@pytest.fixture
def user_session(monkeypatch):
    """Patch in a user session and take the bot's spare one away."""

    def install(client):
        monkeypatch.setattr(mu, "get_user_client", _user_client_returning(client))
        monkeypatch.setattr(mu.TgClient, "user", None)
        return client

    return install


async def test_a_private_link_is_read_with_the_user_session(user_session):
    client = user_session(_UserClient())

    message, session = await mu.get_tg_link_message(
        "tg://openmessage?user_id=123456&message_id=5", user_id=7
    )

    assert client.asked == [(123456, 5)]
    assert session == "user"
    assert message.id == 1


async def test_a_private_link_may_name_a_group_with_a_negative_id(user_session):
    """A plain group's peer id is negative, and it has no ``-100`` to strip.

    The regex used to accept digits only, so every such link came back "Wrong
    link format!" -- a group was unreachable by the one form that can name it.
    """
    client = user_session(_UserClient())

    _, session = await mu.get_tg_link_message(
        "tg://openmessage?user_id=-987654&message_id=5", user_id=7
    )

    assert client.asked == [(-987654, 5)]
    assert session == "user"


async def test_a_private_range_becomes_one_link_per_message(user_session):
    """``message_id=5-7`` is the form a bulk of private messages is built from."""
    user_session(_UserClient())

    links, session = await mu.get_tg_link_message(
        "tg://openmessage?user_id=123&message_id=5-7", user_id=7
    )

    assert links == [
        "tg://openmessage?user_id=123&message_id=5",
        "tg://openmessage?user_id=123&message_id=6",
        "tg://openmessage?user_id=123&message_id=7",
    ]
    assert session == "user"


async def test_a_private_link_without_a_session_asks_for_one(monkeypatch):
    monkeypatch.setattr(mu, "get_user_client", _user_client_returning(None))
    monkeypatch.setattr(mu.TgClient, "user", None)

    with pytest.raises(mu.TgLinkException, match="USER_SESSION_STRING"):
        await mu.get_tg_link_message(
            "tg://openmessage?user_id=123&message_id=5", user_id=7
        )


async def test_a_public_link_the_bot_can_read_stays_with_the_bot(monkeypatch):
    """The https branch still asks the bot first, which is what keeps it free."""
    bot = SimpleNamespace(get_messages=_bot_returning(_Message(message_id=42)))
    monkeypatch.setattr(mu.TgClient, "bot", bot)
    monkeypatch.setattr(mu, "get_user_client", _user_client_returning(None))
    monkeypatch.setattr(mu.TgClient, "user", None)

    message, session = await mu.get_tg_link_message(
        "https://t.me/somechannel/42", user_id=7
    )

    assert session == "bot"
    assert message.id == 42


def _bot_returning(message):
    async def get_messages(chat_id, message_ids):
        return message

    return get_messages


# ── send_notice: a fresh message the user is told about ─────────────


class _SendingBot:
    def __init__(self):
        self.calls: list[dict] = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(id=5)


async def test_a_notice_is_a_new_message_with_notification_on(monkeypatch):
    """An edit notifies nobody, so the batch summary has to be sent, not edited."""
    bot = _SendingBot()
    monkeypatch.setattr(mu.TgClient, "bot", bot)
    message = SimpleNamespace(
        chat=SimpleNamespace(id=99), message_thread_id=7
    )

    sent = await mu.send_notice(message, "done", notify=True)

    assert sent.id == 5
    assert bot.calls == [
        {
            "chat_id": 99,
            "text": "done",
            "message_thread_id": 7,
            "disable_notification": False,
        }
    ]


async def test_a_notice_can_follow_the_first_one_silently(monkeypatch):
    bot = _SendingBot()
    monkeypatch.setattr(mu.TgClient, "bot", bot)
    message = SimpleNamespace(chat=SimpleNamespace(id=99), message_thread_id=None)

    await mu.send_notice(message, "more", notify=False)

    assert bot.calls[0]["disable_notification"] is True
    assert bot.calls[0]["message_thread_id"] is None
