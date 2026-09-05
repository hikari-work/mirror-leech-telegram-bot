"""Re-send a finished task to a copy preset, without downloading again.

A task that ran without ``-c`` -- or one whose files should reach one more
chat -- used to mean leeching it all over again. What the task sent is in
the database now, so ``/copy <task id>`` can replay it; the id is the one
under ``Task ID`` in the task's result message.

Two entry points share one prompt. The command builds it -- a summary of
the record plus one button per preset -- and the ``copyt`` callback answers
it. The preset names are buttons, not a typed argument, for two reasons: a
mistyped name is an error the user only meets after the command is sent,
and the keyboard is also the confirmation step -- once a copy starts there
is no taking it back, so the last click before that should be a deliberate
one.
"""

from html import escape
from time import monotonic, time
from typing import Any

from pyrogram.types import CallbackQuery, Message

from .. import user_data
from ..core.config_manager import Config
from ..core.telegram_manager import TgClient
from ..helper.storage.copy_presets import as_dump_target, presets_of
from ..helper.storage.copy_records import MAX_TASK_RECORDS, copy_unit
from ..helper.storage.db_handler import database
from ..helper.telegram.button_build import ButtonMaker
from ..helper.telegram.dest_chat import verify_copy_target
from ..helper.telegram.message_utils import (
    auto_delete_message,
    chat_of,
    edit_message,
    send_message,
)
from ..helper.upload.flood_pacer import FloodPacer
from ..helper.util.bot_utils import new_task

PENDING_TTL = 300
"""Seconds a preset prompt stays answerable.

The dict behind it lives in memory, so a restart expires every pending
prompt at once; the answer is to run /copy again, which is what the expiry
message says rather than leaving the buttons looking alive.
"""

PROGRESS_EDIT_INTERVAL = 5.0
"""Seconds between two progress edits of the prompt while a copy runs."""

pending: dict[tuple[int, int], dict[str, Any]] = {}
"""(chat id, prompt message id) -> what that prompt offers: the user whose
presets its buttons name, the record to send, and the presets as they were
displayed. Snapshotted rather than read back at press time on purpose --
editing the presets in between must not change what a button does, because
what it says is what was confirmed."""

running = set()
"""User ids with a copy in flight. One at a time per user: a /copy sent
twice would double every file, and there is no cancelling mid-copy."""

USAGE = """<b>Copy a finished task to one of your copy presets.</b>

<code>/copy task_id</code> -- the number under "Task ID" in the task's
result message. When the same id was used in several chats:
<code>/copy chat_id:task_id</code> for the one you mean."""


@new_task
async def copy_task(_: Any, message: Message) -> None:
    """Offer the presets: what the task sent is known, ask where it goes."""
    args = (message.text or "").split()
    if len(args) != 2:
        await send_message(message, USAGE)
        return
    if not Config.DATABASE_URL:
        await send_message(
            message,
            "Copying finished tasks needs a database, and this bot has"
            " none configured.",
        )
        return
    user_id = (
        message.from_user.id
        if message.from_user
        # An anonymous admin or channel post is attributed to its chat; a
        # command always has one of the two.
        else message.sender_chat.id  # pyrefly: ignore[missing-attribute]
    )
    # Chat.id is optional only for the partial updates pyrogram builds; a
    # command the bot is handling came from a real chat.
    here: int = chat_of(message).id  # pyrefly: ignore[bad-assignment]
    record, complaint = await _find_record(args[1], here)
    if record is None:
        await send_message(message, complaint or _bad_token(args[1]))
        return
    presets = list(presets_of(user_data.get(user_id, {})).items())
    if not presets:
        await send_message(
            message,
            "You have no copy presets. Make one in User Settings -> Leech ->"
            " Copy Presets, then run /copy again.",
        )
        return
    buttons = ButtonMaker()
    for index, (name, _entries) in enumerate(presets):
        buttons.data_button(name, f"copyt {user_id} {index}")
    buttons.data_button("Cancel", f"copyt {user_id} x", "footer")
    units = record["units"]
    prompt = await send_message(
        message,
        f"<b>Copy</b> <code>{escape(str(record['name']))}</code>\n"
        f"<b>Items:</b> {len(units)}\n"
        f"<b>Task ID:</b> <code>{record['cid']}:{record['mid']}</code>\n\n"
        "Copy it to which preset?",
        buttons.build_menu(2),
    )
    # send_message returns the sent message, or the error string when
    # telegram refused it; only a real message can be answered by a button.
    if hasattr(prompt, "id"):
        pending[(here, prompt.id)] = {
            "user": user_id,
            "record": record,
            "presets": presets,
            "at": time(),
        }


async def _find_record(
    token: str, here: int
) -> tuple[dict[str, Any] | None, str | None]:
    """The record *token* names, or the complaint that it names none.

    A message id is only unique inside its chat, so ``<mid>`` may match
    several records. The chat the /copy was typed in is the obvious pick;
    anything else has to be spelled out as ``<cid>:<mid>``.
    """
    if ":" in token:
        chat_part, _, mid_part = token.partition(":")
        if not chat_part.lstrip("-").isdigit() or not mid_part.isdigit():
            return None, _bad_token(token)
        cid, mid = int(chat_part), int(mid_part)
        records = [
            r for r in await database.find_copy_records(mid) if r["cid"] == cid
        ]
    else:
        if not token.lstrip("-").isdigit():
            return None, _bad_token(token)
        mid = int(token)
        records = await database.find_copy_records(mid)
    if not records:
        return None, (
            f"No record of task <code>{escape(token)}</code>. Only the last"
            f" {MAX_TASK_RECORDS} finished tasks per user are kept, and tasks"
            " that finished before copying was added have none."
        )
    if len(records) > 1:
        same_chat = [r for r in records if r["cid"] == here]
        if len(same_chat) != 1:
            spelled = ", ".join(f"<code>{r['cid']}:{r['mid']}</code>" for r in records)
            return None, (
                f"Task id <code>{mid}</code> was used in several chats:"
                f" {spelled}. Run /copy again with the chat id in front,"
                f" as <code>/copy chat_id:{mid}</code>."
            )
        return same_chat[0], None
    return records[0], None


def _bad_token(token: str) -> str:
    """The complaint about a task id that names no task unambiguously."""
    return (
        f"<code>{escape(token)}</code> is not a task id. Use"
        " <code>/copy task_id</code> or <code>/copy chat_id:task_id</code>."
    )


@new_task
async def copy_choice(client: Any, query: CallbackQuery) -> None:
    """Do what the pressed button says, or explain why it can no longer."""
    data = query.data.split()
    if query.from_user.id != int(data[1]):
        await query.answer("Not yours!", show_alert=True)
        return
    # Same as in copy_task: a button the bot is handling belongs to a
    # message from a real chat.
    chat_id: int = chat_of(query.message).id  # pyrefly: ignore[bad-assignment]
    key = (chat_id, query.message.id)
    if data[2] == "x":
        await query.answer()
        pending.pop(key, None)
        await edit_message(query.message, "Copy cancelled.")
        await auto_delete_message(query.message)
        return
    # Consumed on first press: a second press of the same button would
    # otherwise copy twice, and the expiry check below only works on what
    # the pop handed back.
    entry = pending.pop(key, None)
    if entry is None or time() - entry["at"] > PENDING_TTL:
        await query.answer(
            f"This prompt is older than {PENDING_TTL // 60} minutes."
            " Run /copy again.",
            show_alert=True,
        )
        return
    user_id = entry["user"]
    if user_id in running:
        await query.answer(
            "You already have a copy running. Let it finish first.",
            show_alert=True,
        )
        return
    await query.answer()
    name, entries = entry["presets"][int(data[2])]
    targets = {}
    problems = []
    for dest in entries:
        chat_id, thread_id = as_dump_target(dest, user_id)
        try:
            # Every target is checked before the first copy leaves: telling
            # the user one chat is unwritable after half the files went out
            # is a state nothing can tidy up.
            await verify_copy_target(dest, chat_id)
        except ValueError as err:
            problems.append(str(err))
            continue
        targets.setdefault((chat_id, thread_id), {"last_sent_msg": None})
    if problems:
        await edit_message(
            query.message,
            "No copy was sent:\n" + "\n".join(problems),
        )
        await auto_delete_message(query.message)
        return
    await _run_copy(query.message, entry["record"], name, targets, user_id)


async def _run_copy(
    prompt: Message,
    record: dict[str, Any],
    name: str,
    targets: dict[tuple[Any, Any], dict[str, Any]],
    user_id: int,
) -> None:
    """Send every unit of *record* to every *targets* chat, then summarize.

    The prompt the preset was chosen on becomes the progress message and
    finally the per-destination summary, so the whole copy reads as one
    exchange from the user's side.
    """
    units = record["units"]
    failures = {}
    pacer = FloodPacer(lambda: False)
    last_edit = 0.0
    running.add(user_id)
    try:
        for index, unit in enumerate(units, start=1):
            errors = await copy_unit(pacer, targets, unit, TgClient.bot)
            for target, error in errors.items():
                failures.setdefault(target, []).append(error)
            now = monotonic()
            if index < len(units) and now - last_edit >= PROGRESS_EDIT_INTERVAL:
                last_edit = now
                await edit_message(
                    prompt,
                    f"Copying <code>{escape(str(record['name']))}</code> to"
                    f" {escape(name)}: {index}/{len(units)}",
                    block=False,
                )
    finally:
        running.discard(user_id)
    head = (
        f"<b>Copied</b> <code>{escape(str(record['name']))}</code>"
        f" <b>to</b> {escape(name)}\n"
    )
    lines = []
    for (chat_id, thread_id), _data in targets.items():
        where = str(chat_id) + (f"|{thread_id}" if thread_id is not None else "")
        missed = failures.get((chat_id, thread_id), [])
        if not missed:
            lines.append(f"<code>{where}</code>: {len(units)}/{len(units)} sent")
            continue
        reasons = "; ".join(dict.fromkeys(missed))
        lines.append(
            f"<code>{where}</code>: {len(units) - len(missed)}/{len(units)} sent"
            f" -- {escape(reasons)}"
        )
    await edit_message(prompt, head + "\n".join(lines))
