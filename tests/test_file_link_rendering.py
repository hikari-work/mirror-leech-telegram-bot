"""Tests for the result lines a finished task sends.

Two halves of one line are written by the bot and read by Telegram's HTML
parser: the file name, which came off disk, and the link, which for a bucket
carries ``&`` between its query parameters. Neither used to be escaped. A file
called ``<b>episode</b>.mkv`` arrived as markup and a bucket link arrived with
a bare ``&`` -- which the parser happens to tolerate often enough that the
breakage looked like "some links do not work".

The escaping is a no-op for the telegram message links this was first written
for, so the message a leech has always sent is asserted here unchanged.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

import pytest

import bot.helper.listeners.task_listener as tl
import bot.helper.task.batch_tracker as bt
from bot.helper.telegram.message_utils import file_link_line

# ── the helper ──────────────────────────────────────────────────────


def test_a_plain_line_is_what_it_always_was():
    assert file_link_line(1, "https://t.me/c/1/2", "a.mkv") == (
        "1. <a href='https://t.me/c/1/2'>a.mkv</a>\n"
    )


def test_a_link_query_string_is_escaped():
    """``?bucket=x&prefix=y`` must not reach the parser as a character entity."""
    assert file_link_line(1, "http://h/?b=x&p=10032/", "10032/") == (
        "1. <a href='http://h/?b=x&amp;p=10032/'>10032/</a>\n"
    )


def test_a_name_that_looks_like_markup_is_not_markup():
    assert file_link_line(2, "http://h/x", "<b>episode</b>.mkv") == (
        "2. <a href='http://h/x'>&lt;b&gt;episode&lt;/b&gt;.mkv</a>\n"
    )


def test_a_quote_in_a_name_cannot_close_the_attribute():
    assert "&#x27;" in file_link_line(3, "http://h/x", "it's.mkv")


# ── the two callers ─────────────────────────────────────────────────


class FakeTask:
    """Enough of a task for ``on_upload_complete`` to write its message."""

    on_upload_complete = tl.TaskListener.on_upload_complete

    def __init__(self):
        self.mid = 10032
        self.name = "Big.Buck.Bunny"
        self.size = 1000
        self.tag = "@me"
        self.is_super_chat = False
        self.copy_units = []
        # what streaming had to drop; a task that does not stream has none
        self.stream_notices = []
        self.seed = False
        self.dir = "/downloads/10032"
        self.message = SimpleNamespace(chat=SimpleNamespace(id=-100123))
        self.sent: list[str] = []
        # a plain task: no bulk group to hand the result to
        self._batch = lambda: None

    async def clean(self):
        pass


@pytest.fixture
def task(monkeypatch):
    """A task whose chat and disk are recorded rather than touched."""
    fake = FakeTask()

    async def record(message, text, *args, **kwargs):
        fake.sent.append(text)

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(tl, "send_message", record)
    monkeypatch.setattr(tl, "clean_download", noop)
    monkeypatch.setattr(tl, "start_from_queued", noop)
    monkeypatch.setattr(tl, "task_dict", {})
    monkeypatch.setattr(tl, "task_dict_lock", asyncio.Lock())
    monkeypatch.setattr(tl, "queue_dict_lock", asyncio.Lock())
    monkeypatch.setattr(tl, "non_queued_up", set())
    monkeypatch.setattr(tl, "upload_chat_of", {})
    return fake


async def test_the_message_reports_a_bucket_link_the_parser_can_read(task):
    link = "http://127.0.0.1:8080/?bucket=bucketmirror&prefix=10032/"

    await task.on_upload_complete(link, {link: "10032/"}, 3, 0)

    body = task.sent[-1]
    assert task.sent == [body]
    assert "&amp;" in body
    assert "&prefix" not in body
    assert body.count("<a href=") == 1


async def test_the_message_reports_a_name_the_parser_cannot_see(task):
    await task.on_upload_complete(
        "http://h/x", {"http://h/x": "<b>episode</b>.mkv"}, 1, 0
    )

    body = task.sent[-1]
    assert "&lt;b&gt;episode&lt;/b&gt;.mkv" in body
    assert "<b>episode</b>.mkv" not in body


async def test_a_plain_telegram_result_is_unchanged(task):
    await task.on_upload_complete(
        "https://t.me/c/1/2", {"https://t.me/c/1/2": "a.mkv"}, 1, 0
    )

    body = task.sent[-1]
    assert "<a href='https://t.me/c/1/2'>a.mkv</a>" in body


@pytest.mark.parametrize(
    "renderer",
    [
        pytest.param(tl.TaskListener.on_upload_complete, id="finished_task"),
        pytest.param(bt.BatchTrackerMixin._send_chunked, id="bulk_summary"),
    ],
)
def test_both_renderers_go_through_the_escaping_helper(renderer):
    source = inspect.getsource(renderer)

    assert "file_link_line(" in source
    assert "href='{link}'" not in source
