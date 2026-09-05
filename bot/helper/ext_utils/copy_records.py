"""What a task's uploads leave behind, and how copies reach several chats.

Two jobs share this file because they share a vocabulary. ``fan_out`` is the
loop that sends one message -- or one album -- to every extra destination a
task named; it used to live inside ``TelegramUploader`` where only a running
task could reach it. The unit records are the other job: what a finished task
uploaded, written down so ``/copy`` can replay it without downloading
anything again.
"""

from __future__ import annotations

from logging import getLogger
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Annotation only: importing the pacer for real would tie this module to
    # the upload package, whose uploader is the one importing this file.
    from ..mirror_leech_utils.upload_utils.flood_pacer import FloodPacer

LOGGER = getLogger(__name__)


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
