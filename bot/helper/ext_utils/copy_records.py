"""What a task's uploads leave behind, and how copies reach several chats.

Two jobs share this file because they share a vocabulary. ``fan_out`` is the
loop that sends one message -- or one album -- to every extra destination a
task named; it used to live inside ``TelegramUploader`` where only a running
task could reach it. The unit records are the other job: what a finished task
uploaded, written down so ``/copy`` can replay it without downloading
anything again.

A *unit* is one copy command's worth of messages, in the order the task sent
them: a ``single`` unit is one message -- ``copy_message`` replays it, and the
``send_*`` method matching its ``kind`` is the fallback should the message be
gone -- and a ``group`` unit is one album -- ``copy_media_group`` replays it,
with a ``send_media_group`` of ``InputMedia`` built from the ``file_id`` list
as the fallback. The ``file_id`` fallback is second choice on purpose: the
``file_reference`` inside a ``file_id`` expires within hours to days, so the
coordinates of the original message are the durable half and the ``file_id``
only ever buys the record a little more time.
"""

from __future__ import annotations

from logging import getLogger
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Annotation only: importing the pacer for real would tie this module to
    # the upload package, whose uploader is the one importing this file.
    from ..mirror_leech_utils.upload_utils.flood_pacer import FloodPacer

LOGGER = getLogger(__name__)

MAX_RECORD_UNITS = 1000
"""Units one task may record. Mongo caps a document at 16 MB and a unit is
about 200 bytes, so a thousand leaves an order of magnitude of headroom while
bounding what one pathological task can pile up."""

MAX_TASK_RECORDS = 200
"""Finished tasks remembered per user. Older ones stop being copyable."""


async def fan_out(
    pacer: FloodPacer,
    targets: dict[tuple[Any, Any], dict[str, Any]],
    copy: Any,
    from_chat_id: int,
    message_id: int,
) -> None:
    """Copy one album, or one message, to every *targets* chat.

    A topic is addressed with ``message_thread_id`` rather than by replying
    into it: the reply only lands in the right topic by inheriting it from
    the message it answers, which is one deletion away from being wrong.

    One unreachable dump chat is not the others' problem, so a failure --
    including a send that never reached telegram -- moves on to the next.
    """
    for (ch, thread_id), ch_data in list(targets.items()):
        try:
            res = await pacer.guard(
                copy,
                chat_id=ch,
                from_chat_id=from_chat_id,
                message_id=message_id,
                disable_notification=True,
                message_thread_id=thread_id,
                reply_to_message_id=ch_data["last_sent_msg"],
            )
            if res is None:
                continue
            # An album answers with every message it became, a single copy
            # with the one; the chain hangs off the last of them either way.
            ch_data["last_sent_msg"] = res[-1].id if isinstance(res, list) else res.id
        except Exception as e:
            LOGGER.error(f"Can't copy message to clone dump chat: {ch}. Error: {e}")


def _media_entry(msg: Any) -> dict[str, Any] | None:
    """One sent message as a ``{kind, file_id, caption}`` fallback entry."""
    for kind, media in (
        ("document", msg.document),
        ("video", msg.video),
        ("audio", msg.audio),
        ("photo", msg.photo),
    ):
        if media is not None:
            return {
                "kind": kind,
                "file_id": media.file_id,
                "caption": msg.caption or "",
            }
    return None


def single_unit(msg: Any) -> dict[str, Any] | None:
    """One sent message as a copy unit, or None for one with no media in it."""
    entry = _media_entry(msg)
    if entry is None:
        return None
    return {"mode": "single", "chat": msg.chat.id, "msg": msg.id, "media": [entry]}


def group_unit(sent: list[Any]) -> dict[str, Any]:
    """One sent album as a copy unit, anchored on its last message.

    Any member of the album works for ``copy_media_group``; the last is the
    one the reply chain already hangs under, so it is the one already at
    hand in the caller.
    """
    last = sent[-1]
    media = [entry for msg in sent if (entry := _media_entry(msg)) is not None]
    return {"mode": "group", "chat": last.chat.id, "msg": last.id, "media": media}


def strike(units: list[dict[str, Any]], carried: set[tuple[int, int]]) -> None:
    """Drop the single units whose messages an album took away.

    The album replaces the messages it absorbed, and they are about to be
    deleted -- copying them one at a time later would double everything the
    album already carries.
    """
    units[:] = [
        unit
        for unit in units
        if unit["mode"] != "single" or (unit["chat"], unit["msg"]) not in carried
    ]


def record(units: list[dict[str, Any]], unit: dict[str, Any] | None) -> None:
    """Append one unit, dropping -- loudly -- past the per-task cap.

    A silent cut would read as "everything was recorded" when /copy replays
    the task later and finds the tail missing.
    """
    if unit is None:
        return
    if len(units) >= MAX_RECORD_UNITS:
        LOGGER.warning(
            f"Copy record is full at {MAX_RECORD_UNITS} units; dropping the"
            f" {unit['mode']} unit of chat {unit['chat']} message {unit['msg']}."
            " /copy of this task will be incomplete."
        )
        return
    units.append(unit)
