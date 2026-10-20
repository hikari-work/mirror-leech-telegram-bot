"""Tests for telegram ranges typed into ``/leech``.

The parser itself is covered in ``test_message_utils.py``; what is pinned here
is the handover: a range becomes a bulk of message links, a range with a
trailing keyword becomes a bulk of the URLs found inside those messages, and
nothing in between reaches the media download path.
"""

from __future__ import annotations

from importlib import import_module
from types import SimpleNamespace

import pytest

import bot.helper.task.batch_tracker as bt
from bot.helper.telegram.bot_commands import BotCommands
from bot.helper.util.exceptions import TgLinkException

# ``bot.modules.__init__`` re-exports the *function* named ``leech``, which
# shadows the submodule of the same name: ``import bot.modules.leech as le``
# would bind that function. Same trap as ``tests/test_private_leech.py``.
le = import_module("bot.modules.leech")

RANGE = "https://t.me/c/1/5-6"
KEYWORD_RANGE = f"{RANGE} terabox"


class _CommandMessage:
    """The message a ``/leech`` command arrived in."""

    def __init__(self, text, user_id=7, chat_id=99, message_id=400):
        self.id = message_id
        self.text = text
        self.caption = None
        self.from_user = SimpleNamespace(id=user_id)
        self.sender_chat = None
        self.chat = SimpleNamespace(id=chat_id, type=None)
        self.reply_to_message = None


@pytest.fixture
def wires(monkeypatch):
    """Record what is sent and what is dispatched, with the real _handle_bulk."""
    sent: list[str] = []
    dispatched: dict = {}

    async def send_message(message, text, *args, **kwargs):
        sent.append(text)
        return SimpleNamespace(id=len(sent))

    async def dispatch_bulk(self, cmd, links, obj):
        dispatched.update(cmd=cmd, links=links, obj=obj, options=self.options)

    monkeypatch.setattr(le, "send_message", send_message)
    # ``fail_task`` lives in batch_tracker and holds its own reference; an error
    # that never reaches the user is exactly what this test file must notice.
    monkeypatch.setattr(bt, "send_message", send_message)
    monkeypatch.setattr(le.Leech, "dispatch_bulk", dispatch_bulk)
    return SimpleNamespace(sent=sent, dispatched=dispatched)


def _listener(link):
    listener = le.Leech(object(), _CommandMessage(f"/leech {link}"))
    listener.link = link
    return listener


async def test_a_range_is_dispatched_as_a_bulk(monkeypatch, wires):
    links = [f"{RANGE.rsplit('-', 1)[0]}5", f"{RANGE.rsplit('-', 1)[0]}6"]

    async def resolve(text, user_id=None):
        assert text == RANGE
        return links, "bot"

    monkeypatch.setattr(le, "get_tg_link_batch", resolve)
    listener = _listener(RANGE)

    await listener._resolve_reply(["/leech", RANGE])

    assert wires.dispatched["links"] == links
    assert wires.dispatched["obj"] is le.Leech
    assert wires.dispatched["cmd"] == f"/{BotCommands.LeechCommand[0]}"


async def test_a_keyword_range_dispatches_the_urls_it_found(monkeypatch, wires):
    urls = ["https://a.test/1", "https://b.test/2"]

    async def resolve(text, user_id=None):
        assert text == KEYWORD_RANGE, "the keyword is the command's business"
        return urls, "bot"

    monkeypatch.setattr(le, "get_tg_link_batch", resolve)
    listener = _listener(KEYWORD_RANGE)

    await listener._resolve_reply(["/leech", RANGE, "terabox"])

    assert wires.dispatched["links"] == urls
    assert wires.dispatched["obj"] is le.Leech


async def test_the_keyword_never_reaches_the_child_options(monkeypatch, wires):
    async def resolve(text, user_id=None):
        return ["https://a.test/1"], "bot"

    monkeypatch.setattr(le, "get_tg_link_batch", resolve)
    listener = _listener(KEYWORD_RANGE)

    await listener._resolve_reply(["/leech", RANGE, "terabox"])

    assert "terabox" not in listener.options
    assert "terabox" not in wires.dispatched["options"]
    assert listener.multi_tag
    assert f"-m bulk-{listener.multi_tag}" in listener.options


async def test_flags_after_the_keyword_are_preserved(monkeypatch, wires):
    async def resolve(text, user_id=None):
        return ["https://a.test/1"], "bot"

    monkeypatch.setattr(le, "get_tg_link_batch", resolve)
    listener = _listener(KEYWORD_RANGE)

    await listener._resolve_reply(["/leech", RANGE, "terabox", "-doc"])

    assert "-doc" in listener.options
    assert "terabox" not in listener.options
    assert "-doc" in wires.dispatched["options"]


async def test_a_failed_scan_reports_the_reason(monkeypatch, wires):
    async def resolve(text, user_id=None):
        raise TgLinkException("No links found in the selected messages.")

    monkeypatch.setattr(le, "get_tg_link_batch", resolve)
    listener = _listener(KEYWORD_RANGE)

    await listener._resolve_reply(["/leech", RANGE, "terabox"])

    assert wires.dispatched == {}
    assert "No links found" in wires.sent[-1]
