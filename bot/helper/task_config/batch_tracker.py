from asyncio import sleep

from ... import multi_batches, multi_tags
from ..telegram_helper.message_utils import (
    delete_message,
    edit_message,
    send_message,
)


class BatchTrackerMixin:
    async def fail_task(self, error: str, *, notify: bool = True) -> None:
        """Convenience: send error + remove_from_same_dir + register_batch_failure.

        Replaces the 13× / 5× duplicated triple in ``leech.py`` / ``ytdlp.py``.
        """
        if notify:
            await send_message(self.message, f"{error}")
        await self.remove_from_same_dir()
        await self.register_batch_failure(str(error))

    def _batch(self):
        if not self.multi_tag or self.multi_tag not in multi_batches:
            return None
        return multi_batches[self.multi_tag]

    async def update_batch_progress(self) -> None:
        batch = self._batch()
        if not batch:
            return
        done = batch["done"]
        total = batch["total"]
        errors = len(batch["errors"])
        name = batch.get("name", self.multi_tag)
        status = f"<b>Batch {name}:</b> {done}/{total} completed"
        if errors > 0:
            status += f", {errors} failed"
        await edit_message(batch["anchor"], status)

    async def register_batch_failure(self, error: str) -> None:
        """Account for a task that died before the download/upload listeners ran.

        new_event bails out early on dead links, failed resolvers and the like.
        Those paths never reach on_download_error, so without recording them the
        batch counter never adds up: the anchor stays stuck at "x/y" and the
        multi tag leaks, which makes the rest of the batch look cancelled.
        """
        batch = self._batch()
        if not batch:
            return
        batch["errors"].append(
            {
                "name": self.name or (self.link if isinstance(self.link, str) else "") or "Unknown",
                "error": str(error),
            }
        )
        await self.update_batch_progress()
        await self.finalize_batch()

    async def finalize_batch(self) -> None:
        batch = self._batch()
        if not batch or batch["done"] + len(batch["errors"]) < batch["total"]:
            return
        results = batch["results"]
        name = batch.get("name", self.multi_tag)

        msg = f"<b>Batch {name} Complete</b>\n\n"
        total_files = 0
        total_corrupted = 0
        all_files = {}

        for res in results:
            total_files += res["folders"]
            total_corrupted += res["corrupted"]
            if res["files"]:
                all_files.update(res["files"])

        msg += f"<b>Total Files:</b> {total_files}"
        if total_corrupted > 0:
            msg += f"\n<b>Corrupted Files:</b> {total_corrupted}"
        msg += f"\n<b>cc:</b> {self.tag}\n\n"

        if all_files:
            fmsg = ""
            anchor_used = False
            for index, (flink, fname) in enumerate(all_files.items(), start=1):
                fmsg += f"{index}. <a href='{flink}'>{fname}</a>\n"
                if len(fmsg.encode() + msg.encode()) > 4000:
                    if anchor_used:
                        await send_message(batch["anchor"], msg + fmsg)
                    else:
                        await edit_message(batch["anchor"], msg + fmsg)
                        anchor_used = True
                    await sleep(1)
                    fmsg = ""
                    msg = ""
            if fmsg:
                if anchor_used:
                    await send_message(batch["anchor"], msg + fmsg)
                else:
                    await edit_message(batch["anchor"], msg + fmsg)
        elif msg:
            await edit_message(batch["anchor"], msg)

        if self.multi_tag in multi_tags:
            multi_tags.discard(self.multi_tag)
        if self.multi_tag in multi_batches:
            for cmd_msg in batch.get("cmd_msgs", []):
                try:
                    await delete_message(cmd_msg)
                except Exception:
                    pass
            del multi_batches[self.multi_tag]
