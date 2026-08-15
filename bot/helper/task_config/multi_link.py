from asyncio import sleep
from secrets import token_urlsafe

from ... import (
    LOGGER,
    intervals,
    multi_batches,
    multi_tags,
    task_dict_lock,
)
from ..ext_utils.bot_utils import new_task
from ..ext_utils.bulk_links import extract_bulk_links
from ..telegram_helper.bot_commands import BotCommands
from ..telegram_helper.message_utils import (
    delete_message,
    edit_message,
    send_message,
    send_status_message,
)


class MultiLinkMixin:
    async def register_same_dir(self) -> None:
        """Book-keep ``same_dir`` when ``multi > 0`` and not bulk.

        Extracted from the duplicated block in ``leech.py`` and
        ``ytdlp.py`` so both commands share one implementation.
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
            elif hasattr(self.user, "mention"):
                self.tag = self.user.mention
            else:
                self.tag = self.user.title

    @new_task
    async def run_multi(self, input_list, obj):
        if not self.multi_tag and self.multi > 1:
            self.multi_tag = token_urlsafe(3)
            multi_tags.add(self.multi_tag)

        if (
            self.multi > 1
            and self.multi_tag
            and self.multi_tag not in multi_batches
        ):
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
                multi_batches[self.multi_tag] = {
                    "anchor": anchor,
                    "total": self.multi,
                    "done": 0,
                    "results": [],
                    "errors": [],
                    "cmd_msgs": [],
                    "name": batch_name,
                }

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
            async with task_dict_lock:
                for fd_name in self.same_dir:
                    self.same_dir[fd_name]["total"] -= self.multi
            if self.multi_tag in multi_batches:
                batch = multi_batches[self.multi_tag]
                await edit_message(
                    batch["anchor"],
                    f"<b>Batch {batch.get('name', self.multi_tag)}:</b> cancelled!",
                )
                for cmd_msg in batch.get("cmd_msgs", []):
                    try:
                        await delete_message(cmd_msg)
                    except Exception:
                        pass
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
            nextmsg = await self.client.get_messages(
                chat_id=self.message.chat.id,
                message_ids=self.message.reply_to_message_id + 1,
            )
            if nextmsg.empty:
                await send_message(
                    self.message,
                    "Bot can't fetch old messages (older than 48H), forward those messages and try multi/bulk again!",
                )
                await send_status_message(self.message)
                return
            msgts = " ".join(msg)
            if self.multi > 2:
                msgts += f"\nCancel Multi: <code>/{BotCommands.CancelTaskCommand[1]} {self.multi_tag}</code>"
            nextmsg = await send_message(nextmsg, msgts)

        if self.multi_tag and self.multi_tag in multi_batches:
            multi_batches[self.multi_tag]["cmd_msgs"].append(nextmsg)

        if self.message.from_user:
            nextmsg.from_user = self.user
        else:
            nextmsg.sender_chat = self.user
        if intervals["stopAll"]:
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

    async def init_bulk(self, input_list, bulk_start, bulk_end, obj):
        try:
            self.bulk = await extract_bulk_links(self.message, bulk_start, bulk_end)
            if len(self.bulk) == 0:
                raise ValueError("Bulk Empty!")

            self.multi_tag = token_urlsafe(3)
            multi_tags.add(self.multi_tag)

            b_msg = input_list[:1]
            self.options = input_list[1:]
            index = self.options.index("-b")
            del self.options[index]
            if bulk_start or bulk_end:
                del self.options[index]

            if "-m" not in " ".join(self.options):
                self.options.append(f"-m bulk-{self.multi_tag}")

            self.options = " ".join(self.options)
            b_msg.append(f"{self.bulk[0]} -i {len(self.bulk)} {self.options}")
            msg = " ".join(b_msg)
            if len(self.bulk) > 2:
                msg += f"\nCancel Multi: <code>/{BotCommands.CancelTaskCommand[1]} {self.multi_tag}</code>"

            nextmsg = await send_message(self.message, msg)
            if isinstance(nextmsg, str):
                raise ValueError(nextmsg)

            multi_batches[self.multi_tag] = {
                "anchor": nextmsg,
                "total": len(self.bulk),
                "done": 0,
                "results": [],
                "errors": [],
                "cmd_msgs": [],
                "name": self.options.split("-m")[-1].split()[0] if "-m" in self.options else self.multi_tag,
            }

            if self.message.from_user:
                nextmsg.from_user = self.user
            else:
                nextmsg.sender_chat = self.user
            await obj(
                self.client,
                nextmsg,
                self.is_qbit,
                self.same_dir,
                self.bulk,
                self.multi_tag,
                self.options,
            ).new_event()
        except Exception as e:
            await send_message(
                self.message,
                f"Reply to text file or to telegram message that have links separated by new line! {e}",
            )
