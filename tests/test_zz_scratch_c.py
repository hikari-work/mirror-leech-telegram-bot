"""Scratch: what lands in the destination for a single-file `-c` task?

The user's bot has LEECH_DUMP_CHAT set, so `up_dest` is truthy and the uploader
first posts a base message into the dump chat and then uploads replying to it.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_telegram_uploader_album import (  # noqa: E402
    FakeMessage,
    _make_uploader,
    uploader_module,
)

DUMP = -1004421291343
PRESET = (-1004365842775, 34)


def _instrument(uploader):
    """Log every message the uploader puts into a chat, and every delete."""
    log = []
    client = uploader._listener.client
    saved = {name: getattr(client, name) for name in ("send_photo", "send_video")}

    async def send_message(chat_id, text=None, **kwargs):
        msg = FakeMessage(None)
        msg._client = client
        log.append(("TEXT", chat_id, msg.id, text))
        return msg

    client.send_message = send_message

    def wrap(name, orig):
        async def inner(chat_id, **kwargs):
            msg = await orig(chat_id, **kwargs)
            log.append((name, chat_id, msg.id, kwargs.get("caption")))
            return msg

        return inner

    for name, orig in saved.items():
        setattr(client, name, wrap(name, orig))

    async def send_media_group(chat_id, media, **kwargs):
        sent = [FakeMessage("photo", caption=m.caption) for m in media]
        for msg in sent:
            msg._client = client
        log.append(("ALBUM", chat_id, [m.id for m in sent], None))
        return sent

    client.send_media_group = send_media_group

    async def delete_messages(chat_id, message_ids, revoke=True):
        log.append(("DELETE", chat_id, message_ids, None))
        return 1

    client.delete_messages = delete_messages
    return log


def _record_copies():
    copied = []

    async def copy_media_group(chat_id, **kwargs):
        copied.append(("group", chat_id, kwargs.get("message_thread_id")))
        return [FakeMessage("photo"), FakeMessage("photo")]

    async def copy_message(chat_id, **kwargs):
        copied.append(("one", chat_id, kwargs.get("message_thread_id")))
        return FakeMessage("photo")

    sys.modules["bot.core.telegram_manager"].TgClient.bot = SimpleNamespace(
        copy_media_group=copy_media_group, copy_message=copy_message
    )
    return copied


def _setup(uploader_module, preset):
    uploader, _ = _make_uploader(uploader_module, [])
    uploader._listener.user_dict = {
        "MEDIA_GROUP": True,
        "LEECH_FILENAME_PREFIX": "",
        "FILES_LINKS": False,
    }
    uploader._listener.message = FakeMessage("video")
    uploader._listener.is_super_chat = True
    uploader._listener.up_dest = DUMP
    uploader._listener.chat_thread_id = None
    uploader._listener.copy_preset = preset
    uploader._listener.clone_dump_chats = (
        {PRESET: {"last_sent_msg": None}} if preset else {}
    )
    uploader._listener.copy_units = []
    uploader._thumb = None
    return uploader


async def _run(uploader, tmp_path, monkeypatch, module, label, name="eer.mp4"):
    (tmp_path / name).write_bytes(b"x")
    uploader._path = str(tmp_path)

    async def _sync(func):
        return func()

    monkeypatch.setattr(module, "sync_to_async", _sync)
    await uploader.upload()

    print(f"\n--- {label} ---")
    for row in run_log:
        print("   ", row)
    print("    COPIES:", run_copies)
    print("    BASE MSG LEFT:", uploader._base_msg, "ERROR:", uploader._error)


run_log = []
run_copies = []


async def _report(uploader_module, tmp_path, monkeypatch, preset, label):
    global run_log, run_copies
    uploader = _setup(uploader_module, preset)
    run_log = _instrument(uploader)
    run_copies = _record_copies()
    sys.modules["aiofiles.os"].path.exists = AsyncMock(return_value=True)
    sys.modules["bot.core.config_manager"].Config.MEDIA_GROUP = True
    sys.modules["bot.core.config_manager"].Config.DATABASE_URL = "postgres://x"
    media = sys.modules["bot.helper.util.media_utils"]
    media.get_document_type = AsyncMock(return_value=(True, False, False))
    await _run(uploader, tmp_path, monkeypatch, uploader_module, label)


@pytest.mark.asyncio
async def test_video_with_a_preset(uploader_module, tmp_path, monkeypatch):
    await _report(uploader_module, tmp_path, monkeypatch, "group", "WITH preset")


@pytest.mark.asyncio
async def test_video_without_a_preset(uploader_module, tmp_path, monkeypatch):
    await _report(uploader_module, tmp_path, monkeypatch, "", "NO preset")
