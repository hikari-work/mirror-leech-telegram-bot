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


# ── parse_tg_link: one grammar for both range forms ─────────────────


TWO_LINK = (
    "https://t.me/c/4419364912/6617-https://t.me/c/4419364912/6900"
)


def test_a_two_link_range_names_every_message():
    parsed = mu.parse_tg_link(TWO_LINK)

    assert parsed.chat == -1004419364912
    assert (parsed.first_id, parsed.last_id) == (6617, 6900)
    assert parsed.is_range
    assert len(parsed.ids) == 6900 - 6617 + 1
    assert parsed.link_for(6617) == "https://t.me/c/4419364912/6617"


def test_a_short_range_can_be_written_backwards():
    parsed = mu.parse_tg_link("https://t.me/c/4419364912/6617-6500")

    assert (parsed.first_id, parsed.last_id) == (6500, 6617)
    assert parsed.link_for(6500) == "https://t.me/c/4419364912/6500"


def test_a_two_link_range_can_be_written_backwards():
    parsed = mu.parse_tg_link("https://t.me/c/1/900-https://t.me/c/1/700")

    assert (parsed.first_id, parsed.last_id) == (700, 900)


def test_a_single_link_is_not_a_range():
    parsed = mu.parse_tg_link("https://t.me/somechannel/42")

    assert not parsed.is_range
    assert parsed.count == 1
    assert parsed.chat == "somechannel"


def test_the_two_ends_must_name_the_same_chat():
    with pytest.raises(mu.TgLinkException, match="same chat"):
        mu.parse_tg_link("https://t.me/c/1/5-https://t.me/c/2/7")


def test_the_two_ends_must_name_the_same_topic():
    with pytest.raises(mu.TgLinkException, match="same topic"):
        mu.parse_tg_link(
            "https://t.me/c/1/111/5-https://t.me/c/1/222/7"
        )


def test_a_range_over_the_cap_is_refused():
    with pytest.raises(mu.TgLinkException, match="too large"):
        mu.parse_tg_link("https://t.me/c/1/1-1001")


@pytest.mark.parametrize(
    "address",
    ["https://t.me/c/1/0", "https://t.me/c/1/0-3", "https://t.me/c/1/"],
)
def test_message_ids_start_at_one(address):
    with pytest.raises(mu.TgLinkException, match="Wrong link format"):
        mu.parse_tg_link(address)


def test_the_private_range_form_still_works():
    parsed = mu.parse_tg_link("tg://openmessage?user_id=123&message_id=5-7")

    assert parsed.chat == 123
    assert [parsed.link_for(i) for i in parsed.ids] == [
        "tg://openmessage?user_id=123&message_id=5",
        "tg://openmessage?user_id=123&message_id=6",
        "tg://openmessage?user_id=123&message_id=7",
    ]


def test_a_topic_prefix_survives_the_expansion():
    parsed = mu.parse_tg_link("https://t.me/c/123/456/10-12")

    assert parsed.link_for(10) == "https://t.me/c/123/456/10"


# ── get_tg_link_message: ranges ─────────────────────────────────────


class _ProbeBot:
    """Answers a single-id probe, optionally only for some of the ids."""

    def __init__(self, readable=(), unreadable=False):
        self.readable = set(readable)
        self.unreadable = unreadable
        self.asked: list[tuple] = []

    async def get_messages(self, chat_id, message_ids):
        if self.unreadable:
            raise ValueError(f"no access to {chat_id}")
        self.asked.append((chat_id, message_ids))
        message = _Message(message_id=message_ids)
        if message_ids not in self.readable:
            message.empty = True
        return message


@pytest.fixture
def no_bot_session(monkeypatch):
    """Take the bot and the user session away, leaving only what a test sets."""
    monkeypatch.setattr(mu.TgClient, "bot", _ProbeBot())
    monkeypatch.setattr(mu, "get_user_client", _user_client_returning(None))
    monkeypatch.setattr(mu.TgClient, "user", None)


async def test_a_two_link_range_becomes_one_link_per_message(
    monkeypatch, no_bot_session
):
    bot = _ProbeBot(readable={6617})
    monkeypatch.setattr(mu.TgClient, "bot", bot)

    links, session = await mu.get_tg_link_message(TWO_LINK, user_id=7)

    assert session == "bot"
    assert len(links) == 284
    assert links[0] == "https://t.me/c/4419364912/6617"
    assert links[-1] == "https://t.me/c/4419364912/6900"
    assert bot.asked == [(-1004419364912, 6617)]


async def test_a_reverse_range_expands_ascending(monkeypatch, no_bot_session):
    monkeypatch.setattr(mu.TgClient, "bot", _ProbeBot(readable={5}))

    links, _ = await mu.get_tg_link_message(
        "https://t.me/c/1/7-5", user_id=7
    )

    assert links == [
        "https://t.me/c/1/5",
        "https://t.me/c/1/6",
        "https://t.me/c/1/7",
    ]


async def test_a_degenerate_range_is_a_one_link_batch(monkeypatch, no_bot_session):
    monkeypatch.setattr(mu.TgClient, "bot", _ProbeBot(readable={5}))

    links, _ = await mu.get_tg_link_message("https://t.me/c/1/5-5", user_id=7)

    assert links == ["https://t.me/c/1/5"]


async def test_a_range_over_the_cap_is_refused_before_any_request(
    monkeypatch, no_bot_session
):
    bot = _ProbeBot(readable={1})
    monkeypatch.setattr(mu.TgClient, "bot", bot)

    with pytest.raises(mu.TgLinkException, match="too large"):
        await mu.get_tg_link_message("https://t.me/c/1/1-1001", user_id=7)

    assert bot.asked == []


async def test_a_deleted_first_message_does_not_condemn_the_range(
    monkeypatch, no_bot_session
):
    bot = _ProbeBot(readable={9})
    monkeypatch.setattr(mu.TgClient, "bot", bot)

    links, session = await mu.get_tg_link_message(
        "https://t.me/c/1/5-9", user_id=7
    )

    assert links[0] == "https://t.me/c/1/5"
    assert session == "bot"
    assert bot.asked == [(-1001, 5), (-1001, 9)]


async def test_trailing_text_does_not_change_a_single_link(monkeypatch):
    monkeypatch.setattr(
        mu.TgClient, "bot", SimpleNamespace(get_messages=_bot_returning(_Message(42)))
    )
    monkeypatch.setattr(mu, "get_user_client", _user_client_returning(None))
    monkeypatch.setattr(mu.TgClient, "user", None)

    message, session = await mu.get_tg_link_message(
        "https://t.me/somechannel/42 some trailing text", user_id=7
    )

    assert session == "bot"
    assert message.id == 42
