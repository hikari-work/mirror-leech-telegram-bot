"""Tests for the upload-destination checks.

The bulk that went wrong reported half its links as dead while the gateway had
answered 200 for every one of them. The links were never the problem: each task
validates the dump chat in ``before_start`` with ``get_chat`` plus
``get_chat_member``, the bot client runs with ``sleep_threshold=0`` so pyrogram
raises the FloodWait instead of sitting it out, and the old code read any
exception there as "the chat is gone" -- ``raise ValueError("Chat not found!")``.

So what is pinned here is the difference between the two answers a lookup can
give. "Not there" is a verdict and may fail the task; "could not ask" is not,
and must produce a cached answer, a wait, a stale fallback, or an error that
says so -- never a verdict.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pyrogram.errors import FloodPremiumWait, FloodWait, PeerIdInvalid

import bot.helper.telegram_helper.dest_chat as dc
from bot.helper.task_config.settings_resolver import SettingsResolverMixin

DEST = -1001234567890


# ── fakes ───────────────────────────────────────────────────────────


class Script:
    """Scripted answers for one lookup; the last one repeats forever."""

    def __init__(self, items):
        self.items = list(items)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        item = self.items[min(self.calls - 1, len(self.items) - 1)]
        if isinstance(item, BaseException):
            raise item
        return item


def _chat(chat_id=DEST, kind="CHANNEL", is_admin=True):
    return SimpleNamespace(
        id=chat_id, type=SimpleNamespace(name=kind), is_admin=is_admin
    )


def _member(can_manage=True, can_delete=True):
    return SimpleNamespace(
        privileges=SimpleNamespace(
            can_manage_chat=can_manage, can_delete_messages=can_delete
        )
    )


class FakeClient:
    def __init__(self, name="bot", *, chats=None, members=None, actions=None):
        self.name = name
        self.chat = Script(chats if chats is not None else [_chat()])
        self.member = Script(members if members is not None else [_member()])
        self.action = Script(actions if actions is not None else [True])

    async def get_chat(self, chat_id):
        await asyncio.sleep(0)
        return self.chat()

    async def get_chat_member(self, chat_id, user_id):
        await asyncio.sleep(0)
        return self.member()

    async def send_chat_action(self, chat_id, action):
        await asyncio.sleep(0)
        return self.action()


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    """Answers are cached in module state, and the retries sleep for real."""
    dc.reset_dest_cache()
    monkeypatch.setattr(dc, "sleep", lambda _: asyncio.sleep(0))
    yield
    dc.reset_dest_cache()


def _age_cache():
    """Let the TTL of every cached answer run out, without waiting for it.

    Ageing the entries rather than shortening ``CHAT_TTL`` keeps the real TTL in
    force, so a lookup that refreshes an entry is still seen to refresh it.
    """
    for entry in dc._cache.values():
        entry["at"] -= dc.CHAT_TTL + 1


# ── the flood the batch used to cause ───────────────────────────────


async def test_one_lookup_answers_the_whole_batch():
    """The reason for the flood: the same question, once per task."""
    client = FakeClient()

    chats = await asyncio.gather(
        *[dc.get_dest_chat(client, DEST) for _ in range(50)]
    )

    assert client.chat.calls == 1
    assert {c.id for c in chats} == {DEST}


async def test_ttl_expiry_asks_again():
    client = FakeClient()
    await dc.get_dest_chat(client, DEST)

    _age_cache()
    await dc.get_dest_chat(client, DEST)

    assert client.chat.calls == 2


async def test_bot_and_user_sessions_get_their_own_answer():
    """The two sessions see different chats; one entry would leak across them."""
    bot = FakeClient("bot", chats=[_chat(is_admin=True)])
    user = FakeClient("user", chats=[_chat(is_admin=False)])

    assert (await dc.get_dest_chat(bot, DEST)).is_admin is True
    assert (await dc.get_dest_chat(user, DEST)).is_admin is False


# ── a flood is not a verdict ────────────────────────────────────────


async def test_flood_is_waited_out():
    client = FakeClient(chats=[FloodWait(2), FloodWait(2), _chat()])

    chat = await dc.get_dest_chat(client, DEST)

    assert chat.id == DEST
    assert client.chat.calls == 3


async def test_premium_flood_is_waited_out_too():
    client = FakeClient(chats=[FloodPremiumWait(2), _chat()])

    assert (await dc.get_dest_chat(client, DEST)).id == DEST


async def test_flood_falls_back_to_the_last_known_answer():
    """The case that saves a batch: asked once, flooded ever after."""
    client = FakeClient(chats=[_chat()])
    await dc.get_dest_chat(client, DEST)

    _age_cache()
    client.chat.items = [FloodWait(5)]
    client.chat.calls = 0

    chat = await dc.get_dest_chat(client, DEST)

    assert chat is not None and chat.id == DEST


async def test_a_used_fallback_stops_the_retries():
    """Re-asking while the flood lasts only feeds it."""
    client = FakeClient(chats=[_chat()])
    await dc.get_dest_chat(client, DEST)

    _age_cache()
    client.chat.items = [FloodWait(5)]
    client.chat.calls = 0
    await dc.get_dest_chat(client, DEST)
    flooded = client.chat.calls

    await dc.get_dest_chat(client, DEST)

    assert client.chat.calls == flooded


async def test_flood_without_anything_cached_is_not_a_missing_chat():
    """``None`` here is what became "Chat not found!" for a whole batch."""
    client = FakeClient(chats=[FloodWait(dc.MAX_FLOOD_TOTAL)])

    with pytest.raises(dc.ChatLookupError):
        await dc.get_dest_chat(client, DEST)


async def test_a_first_lookup_waits_far_longer_than_the_budget_of_a_retry():
    """Nothing cached means the choice is waiting or failing a good link."""
    floods = [FloodWait(dc.MAX_FLOOD_WAIT)] * 4
    assert dc.MAX_FLOOD_WAIT * len(floods) <= dc.MAX_FLOOD_TOTAL
    client = FakeClient(chats=[*floods, _chat()])

    chat = await dc.get_dest_chat(client, DEST)

    assert chat.id == DEST
    # waited out more times than ATTEMPTS allows once an answer is known
    assert client.chat.calls == len(floods) + 1
    assert client.chat.calls > dc.ATTEMPTS


async def test_a_long_flood_is_not_waited_out_once_the_answer_is_known():
    client = FakeClient(chats=[_chat()])
    await dc.get_dest_chat(client, DEST)

    _age_cache()
    client.chat.items = [FloodWait(dc.MAX_FLOOD_WAIT + 1)]
    client.chat.calls = 0

    assert (await dc.get_dest_chat(client, DEST)).id == DEST
    # holding the batch for that is pointless when the answer is already here
    assert client.chat.calls == 1


async def test_a_failed_lookup_is_not_repeated_by_the_next_task():
    """A hundred tasks must not each spend the whole wait budget in turn."""
    client = FakeClient(chats=[FloodWait(dc.MAX_FLOOD_TOTAL * 2)])

    with pytest.raises(dc.ChatLookupError):
        await dc.get_dest_chat(client, DEST)
    spent = client.chat.calls

    with pytest.raises(dc.ChatLookupError):
        await dc.get_dest_chat(client, DEST)

    assert client.chat.calls == spent


async def test_the_cooldown_lets_go(monkeypatch):
    client = FakeClient(chats=[FloodWait(dc.MAX_FLOOD_TOTAL * 2), _chat()])
    with pytest.raises(dc.ChatLookupError):
        await dc.get_dest_chat(client, DEST)

    monkeypatch.setattr(dc, "FLOOD_COOLDOWN", 0)

    assert (await dc.get_dest_chat(client, DEST)).id == DEST


async def test_network_trouble_is_retried_like_a_flood():
    client = FakeClient(chats=[ConnectionError("reset"), TimeoutError(), _chat()])

    assert (await dc.get_dest_chat(client, DEST)).id == DEST


# ── a verdict is a verdict ──────────────────────────────────────────


async def test_an_invalid_peer_is_an_answer_and_is_remembered():
    client = FakeClient(chats=[PeerIdInvalid()])

    assert await dc.get_dest_chat(client, DEST) is None
    assert await dc.get_dest_chat(client, DEST) is None
    # a genuinely bad destination must not be re-asked once per link either
    assert client.chat.calls == 1


async def test_member_lookup_shares_the_cache():
    client = FakeClient()

    await asyncio.gather(
        *[dc.get_dest_member(client, DEST, 42) for _ in range(20)]
    )

    assert client.member.calls == 1


async def test_member_flood_does_not_read_as_missing_privileges():
    client = FakeClient(members=[FloodWait(3)])

    with pytest.raises(dc.ChatLookupError):
        await dc.get_dest_member(client, DEST, 42)


async def test_member_verdict_propagates():
    """Without a default there is nothing to return; the caller decides."""
    client = FakeClient(members=[PeerIdInvalid()])

    with pytest.raises(PeerIdInvalid):
        await dc.get_dest_member(client, DEST, 42)


async def test_private_destination_is_probed_once():
    client = FakeClient()

    assert await dc.can_reach_dest(client, 777) is True
    assert await dc.can_reach_dest(client, 777) is True
    assert client.action.calls == 1


async def test_unreachable_private_destination_is_false():
    client = FakeClient(actions=[ValueError("bot was blocked")])

    assert await dc.can_reach_dest(client, 777) is False


async def test_reach_flood_is_not_a_closed_pm():
    client = FakeClient(actions=[FloodWait(3)])

    with pytest.raises(dc.ChatLookupError):
        await dc.can_reach_dest(client, 777)


# ── what before_start does with "could not ask" ─────────────────────


class FakeListener(SettingsResolverMixin):
    def __init__(self, user_transmission):
        self.user_transmission = user_transmission
        self.hybrid_leech = True


def test_an_unverified_dest_fails_a_bot_upload_honestly():
    listener = FakeListener(user_transmission=False)

    with pytest.raises(ValueError) as excinfo:
        listener._dest_unverified(
            "the destination chat", dc.ChatLookupError("rate limited")
        )

    message = str(excinfo.value)
    # the whole point: the message must not claim anything about the chat
    assert "not found" not in message.lower()
    assert "rate limited" in message


def test_an_unverified_dest_only_costs_hybrid_leech_when_the_user_session_can_upload():
    listener = FakeListener(user_transmission=True)

    listener._dest_unverified(
        "the bot's privileges", dc.ChatLookupError("rate limited")
    )

    assert listener.hybrid_leech is False
    assert listener.user_transmission is True
