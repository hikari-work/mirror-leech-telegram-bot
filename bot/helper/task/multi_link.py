from asyncio import sleep
from itertools import count
from secrets import token_urlsafe

from pyrogram.types import User

from ... import (
    DOWNLOAD_DIR,
    LOGGER,
    bot_loop,
    intervals,
    multi_batches,
    multi_tags,
    task_dict,
    task_dict_lock,
)
from ..telegram.bot_commands import BotCommands
from ..telegram.message_utils import (
    chat_of,
    delete_message,
    edit_message,
    send_message,
    send_status_message,
)
from ..util.bot_utils import new_task
from ..util.bulk_links import extract_bulk_links
from ..util.task_args import parse_folder_name, strip_link_tokens
from ._host import TaskConfigHost
from .batch_tracker import new_batch

BULK_SPAWN_DELAY = 0.5
"""Seconds between two bulk tasks being started.

A courtesy stagger only. Two things actually bound how much a bulk does at once:
RESOLVE_CONCURRENCY gates the scrape / metadata stage every task runs before it
reaches the queue (see ``util/resolve_gate.py``), and QUEUE_DOWNLOAD /
QUEUE_UPLOAD gate the transfers through ``check_running_tasks``. Tune those for
large bulks, not this.
"""

_bulk_mid_seq = count(10**9)


def _next_bulk_mid():
    """Hand out a synthetic task id for a bulk child.

    Bulk children share one telegram message, so they cannot take their identity
    from ``message.id``. The sequence starts far above any real message id in a
    chat because ``task_dict`` is keyed by mid and ``/cancel`` resolves a task by
    the id of the message it replies to -- a collision would cancel a stranger.
    """
    for mid in _bulk_mid_seq:
        if mid not in task_dict:
            return mid


class MultiLinkMixin(TaskConfigHost):
    async def register_same_dir(self) -> None:
        """Book-keep ``same_dir`` when ``multi > 0`` and not bulk.

        Extracted from the duplicated block in ``leech.py`` and
        ``ytdlp.py`` so both commands share one implementation.

        Bulk no longer comes through here: ``dispatch_bulk`` registers the whole
        group up front instead, which is what keeps the merge deterministic.
        """
        if self.multi > 0:
            if self.folder_name:
                async with task_dict_lock:
                    if self.folder_name in self.same_dir:
                        self.same_dir[self.folder_name]["tasks"].add(self.mid)
                        for fd_name in self.same_dir:
                            if fd_name != self.folder_name:
                                self.same_dir[fd_name]["total"] -= 1
                    elif self.same_dir:
                        self.same_dir[self.folder_name] = {
                            "total": self.multi,
                            "tasks": {self.mid},
                        }
                        for fd_name in self.same_dir:
                            if fd_name != self.folder_name:
                                self.same_dir[fd_name]["total"] -= 1
                    else:
                        self.same_dir = {
                            self.folder_name: {
                                "total": self.multi,
                                "tasks": {self.mid},
                            }
                        }
            elif self.same_dir:
                async with task_dict_lock:
                    for fd_name in self.same_dir:
                        self.same_dir[fd_name]["total"] -= 1

    async def get_tag(self, text: list[str]) -> None:
        if self.user:
            if username := self.user.username:
                self.tag = f"@{username}"
            elif isinstance(self.user, User):
                # ``mention`` is a User property and ``title`` a Chat one, which
                # is what the old ``hasattr(self.user, "mention")`` was testing
                # for; asking about the class says so directly.
                self.tag = self.user.mention
            else:
                # A Chat gets here only as a ``sender_chat``, i.e. a group or a
                # channel posting, and those have a title; ``Chat.title`` is
                # optional for the private chats that never reach this branch.
                # ``tag`` keeps the "" it started with rather than becoming the
                # string "None" in every message that quotes it.
                self.tag = self.user.title or self.tag

    async def _shrink_same_dir(self, count_: int) -> None:
        """Tell the same-dir group that ``count_`` siblings will never arrive.

        Its ``total`` is the number of tasks still expected to contribute, so a
        chain that stops early has to give the slots back -- otherwise the last
        member waits for contributions that cannot come and every sibling behind
        it strands its files.
        """
        if count_ <= 0 or not self.same_dir:
            return
        async with task_dict_lock:
            for fd_name in self.same_dir:
                self.same_dir[fd_name]["total"] -= count_

    @new_task
    async def run_multi(self, input_list, obj):
        """Chain the next ``-i`` task through a fresh command message.

        Bulk does not use this path any more (see ``dispatch_bulk``); it is
        still how a user-written ``-i`` sequence walks its replies.
        """
        if self.bulk_child:
            return
        try:
            await self._run_multi(input_list, obj)
        except Exception as e:
            # the chain is dead: nothing else will send the remaining commands,
            # so release the slots they were holding
            LOGGER.error(f"Multi chain stopped after {self.mid}: {e}")
            await self._shrink_same_dir(self.multi - 1)
            await self._shrink_batch(self.multi - 1)

    async def _run_multi(self, input_list, obj):
        if not self.multi_tag and self.multi > 1:
            self.multi_tag = token_urlsafe(3)
            multi_tags.add(self.multi_tag)

        if self.multi > 1 and self.multi_tag and self.multi_tag not in multi_batches:
            batch_name = (
                self.folder_name.strip("/") if self.folder_name else self.multi_tag
            )
            anchor = await send_message(
                self.message,
                f"<b>Batch {batch_name}:</b> 0/{self.multi} completed",
            )
            if isinstance(anchor, str):
                # anchor failed to send, fall back to per task messages
                LOGGER.error(f"Can't send batch anchor: {anchor}")
            else:
                multi_batches[self.multi_tag] = new_batch(
                    anchor, self.multi, batch_name
                )

        await sleep(3)
        if self.multi <= 1:
            if self.multi_tag in multi_tags:
                multi_tags.discard(self.multi_tag)
            return
        if self.multi_tag and self.multi_tag not in multi_tags:
            await send_message(
                self.message, f"{self.tag} Multi Task has been cancelled!"
            )
            await send_status_message(self.message)
            await self._shrink_same_dir(self.multi - 1)
            if self.multi_tag in multi_batches:
                batch = multi_batches[self.multi_tag]
                await edit_message(
                    batch["anchor"],
                    f"<b>Batch {batch['name']}:</b> cancelled!",
                )
                for cmd_msg in batch.get("cmd_msgs", []):
                    await delete_message(cmd_msg)
                del multi_batches[self.multi_tag]
            return

        if len(self.bulk) != 0:
            msg = input_list[:1]
            msg.append(f"{self.bulk[0]} -i {self.multi - 1} {self.options}")
            msgts = " ".join(msg)
            if self.multi > 2:
                msgts += f"\nCancel Multi: <code>/{BotCommands.CancelTaskCommand[1]} {self.multi_tag}</code>"
            nextmsg = await send_message(self.message, msgts)
        else:
            msg = [s.strip() for s in input_list]
            index = msg.index("-i")
            msg[index + 1] = f"{self.multi - 1}"
            reply_id = self.message.reply_to_message_id
            if reply_id is None:
                # ``-i`` walks the messages that follow the one the command
                # replies to, so a chain not started as a reply has nothing to
                # walk. ``run_multi`` turns this into a logged dead chain --
                # which is what the TypeError on ``None + 1`` used to do, less
                # legibly.
                raise ValueError("-i needs the command to be a reply")
            nextmsg = await self.client.get_messages(
                chat_id=chat_of(self.message).id,
                message_ids=reply_id + 1,
            )
            # A single id is answered with a single message; the signature covers
            # the list form too, which only a list of ids gets back.
            if nextmsg.empty:  # pyrefly: ignore[missing-attribute]
                await send_message(
                    self.message,
                    "Bot can't fetch old messages (older than 48H), forward those messages and try multi/bulk again!",
                )
                await send_status_message(self.message)
                await self._shrink_same_dir(self.multi - 1)
                await self._shrink_batch(self.multi - 1)
                return
            msgts = " ".join(msg)
            if self.multi > 2:
                msgts += f"\nCancel Multi: <code>/{BotCommands.CancelTaskCommand[1]} {self.multi_tag}</code>"
            nextmsg = await send_message(nextmsg, msgts)

        if isinstance(nextmsg, str):
            # a rejected send used to raise on the next line and kill the chain
            # without telling anyone; account for the tasks that never start
            raise ValueError(f"can't send next command message: {nextmsg}")

        if self.multi_tag and self.multi_tag in multi_batches:
            multi_batches[self.multi_tag]["cmd_msgs"].append(nextmsg)

        if self.message.from_user:
            nextmsg.from_user = self.user
        else:
            nextmsg.sender_chat = self.user
        if intervals["stopAll"]:
            await self._shrink_same_dir(self.multi - 1)
            await self._shrink_batch(self.multi - 1)
            return
        await obj(
            self.client,
            nextmsg,
            self.is_qbit,
            self.same_dir,
            self.bulk,
            self.multi_tag,
            self.options,
        ).new_event()

    # ── bulk ────────────────────────────────────────────────────────

    async def init_bulk(self, input_list, bulk_start, bulk_end, obj):
        try:
            links = await extract_bulk_links(self.message, bulk_start, bulk_end)
            if len(links) == 0:
                raise ValueError("Bulk Empty!")

            self.multi_tag = token_urlsafe(3)
            multi_tags.add(self.multi_tag)

            options = strip_link_tokens(input_list[1:], ytdlp=self.is_ytdlp)
            index = options.index("-b")
            del options[index]
            if bulk_start or bulk_end:
                del options[index]

            if "-m" not in " ".join(options):
                options.append(f"-m bulk-{self.multi_tag}")

            self.options = " ".join(options)
            await self.get_tag([])
            await self.dispatch_bulk(input_list[0], links, obj)
        except Exception as e:
            multi_tags.discard(self.multi_tag)
            await send_message(
                self.message,
                f"Reply to text file or to telegram message that have links separated by new line! {e}",
            )

    async def dispatch_bulk(self, cmd, links, obj):
        """Start one task per link without a telegram message per link.

        The old path replied to the chat once per link purely to carry the
        command text for the next task, and chained the tasks through those
        replies. At a hundred links that is a hundred sends plus a hundred
        deletes: it earns a FloodWait, and one rejected send broke the chain --
        the same-dir group was then left waiting for siblings that would never
        register, so every download that finished afterwards stranded its files.

        Building the whole plan up front removes both failure modes: there is
        nothing left to send while the batch runs, and the same-dir group knows
        all of its members before the first download starts.
        """
        folder_name = parse_folder_name(
            f"{links[0]} {self.options}".split(), ytdlp=self.is_ytdlp
        )
        name = folder_name.strip("/") if folder_name else self.multi_tag
        header = f"<b>Batch {name}:</b> 0/{len(links)} completed"
        if len(links) > 2:
            header += f"\nCancel Multi: <code>/{BotCommands.CancelTaskCommand[1]} {self.multi_tag}</code>"
        anchor = await send_message(self.message, header)
        if isinstance(anchor, str):
            raise ValueError(anchor)

        multi_batches[self.multi_tag] = new_batch(anchor, len(links), name)

        # the bot sent the anchor, so lend it the requester's identity: every
        # child reads its user, tag and settings off the message it is handed
        if self.message.from_user:
            anchor.from_user = self.user
        else:
            anchor.sender_chat = self.user

        mids = [_next_bulk_mid() for _ in links]
        same_dir = {}
        if folder_name:
            # the group is complete before the first download starts, so no
            # member ever waits for a sibling to register -- that wait is what
            # used to deadlock while holding same_directory_lock
            same_dir = {
                folder_name: {
                    "total": len(links),
                    "tasks": set(mids),
                    "stage": f"{DOWNLOAD_DIR}sd{mids[0]}",
                }
            }

        LOGGER.info(
            f"Bulk {name}: starting {len(links)} tasks"
            + (f" merging into {folder_name}" if folder_name else "")
        )
        for index, link in enumerate(links):
            if self.multi_tag not in multi_tags:
                LOGGER.info(f"Bulk {name} cancelled after {index} tasks")
                await self._drop_unspawned(same_dir, folder_name, mids[index:])
                return
            bot_loop.create_task(
                obj(
                    self.client,
                    anchor,
                    self.is_qbit,
                    same_dir,
                    [],
                    self.multi_tag,
                    self.options,
                    mids[index],
                    f"{cmd} {link} {self.options}",
                ).new_event()
            )
            await sleep(BULK_SPAWN_DELAY)

    async def _drop_unspawned(self, same_dir, folder_name, unspawned) -> None:
        """Forget the links a mid-dispatch cancel means we never started."""
        if folder_name and same_dir:
            async with task_dict_lock:
                group = same_dir[folder_name]
                group["tasks"].difference_update(unspawned)
                group["total"] -= len(unspawned)
        await self._shrink_batch(len(unspawned))

    async def _shrink_batch(self, count_: int) -> None:
        """Lower the batch target, then let it settle if that completes it."""
        if count_ <= 0:
            return
        batch = self._batch()
        if not batch:
            return
        async with batch["lock"]:
            batch["total"] -= count_
        await self._record()
