from asyncio import Lock, sleep
from html import escape
from time import monotonic

from ... import multi_batches, multi_tags
from ..ext_utils.status_utils import get_readable_file_size
from ..telegram_helper.message_utils import (
    delete_message,
    edit_message,
    send_message,
)
from ._host import TaskConfigHost

BATCH_EDIT_INTERVAL = 5.0
"""Minimum seconds between two edits of the batch anchor message.

A batch can hold hundreds of tasks that all finish within the same few seconds.
Editing the anchor once per task is what earns the FloodWaits that used to stall
the whole batch, so progress edits are coalesced and the intermediate ones are
simply dropped -- the counter is read from the batch, not accumulated in the
message, so a skipped edit loses nothing.
"""

MAX_LISTED_ERRORS = 10
"""Failed tasks named in the final summary before it switches to a count."""


def new_batch(anchor, total, name):
    """Build a batch record.

    One factory so every creator agrees on the shape -- ``lock`` and
    ``finalized`` in particular, without which concurrent tasks race to publish
    the summary and the batch reports itself complete more than once.
    """
    return {
        "anchor": anchor,
        "total": total,
        "done": 0,
        "results": [],
        "errors": [],
        "cmd_msgs": [],
        "name": name,
        "lock": Lock(),
        "finalized": False,
        "last_edit": 0.0,
        "last_text": "",
    }


class BatchTrackerMixin(TaskConfigHost):
    async def fail_task(self, error: str | Exception, *, notify: bool = True) -> None:
        """Report a task that died before the download/upload listeners ran.

        Replaces the 13x / 5x duplicated triple in ``leech.py`` / ``ytdlp.py``.
        Inside a batch the error goes to the anchor instead of its own message:
        a bulk of dead links would otherwise answer with one message per link.
        """
        if notify and not self._batch():
            await send_message(self.message, f"{error}")
        await self.remove_from_same_dir()
        await self.register_batch_failure(str(error))

    def _batch(self):
        if not self.multi_tag or self.multi_tag not in multi_batches:
            return None
        return multi_batches[self.multi_tag]

    # ── recording ───────────────────────────────────────────────────

    async def record_batch_result(self, result) -> None:
        """Account for a task that finished uploading."""
        await self._record(done=1, result=result)

    async def record_batch_done(self) -> None:
        """Account for a task that handed its files to its same-dir group."""
        await self._record(done=1)

    async def register_batch_failure(self, error: str) -> None:
        """Account for a task that failed anywhere along the way.

        ``new_event`` bails out early on dead links, failed resolvers and the
        like. Without recording those the batch counter never adds up: the
        anchor stays stuck at "x/y" and the multi tag leaks, which makes the
        rest of the batch look cancelled.
        """
        await self._record(
            error={
                "name": self.name
                or (self.link if isinstance(self.link, str) else "")
                or "Unknown",
                "error": str(error),
            }
        )

    async def _record(self, *, done: int = 0, error=None, result=None) -> None:
        """Fold one task's outcome into the batch, then publish what changed.

        The mutation and the "am I the one that completed the batch" test happen
        together under the batch lock, so exactly one task ever publishes the
        summary no matter how many finish at the same instant. Every message is
        sent *outside* the lock -- a FloodWait on the anchor must not block the
        tasks still recording their results.
        """
        batch = self._batch()
        if not batch:
            return
        async with batch["lock"]:
            if result is not None:
                batch["results"].append(result)
            if error is not None:
                batch["errors"].append(error)
            batch["done"] += done
            closing = (
                batch["done"] + len(batch["errors"]) >= batch["total"]
                and not batch["finalized"]
            )
            if closing:
                batch["finalized"] = True
                progress = None
            else:
                progress = self._due_progress(batch)

        if closing:
            await self._publish_summary(batch)
        elif progress:
            # block=False: drop this update rather than sleep out a FloodWait
            await edit_message(batch["anchor"], progress, block=False)

    def _due_progress(self, batch):
        """Progress text to publish now, or None while the edit is throttled."""
        done = batch["done"]
        errors = len(batch["errors"])
        text = f"<b>Batch {batch['name']}:</b> {done}/{batch['total']} completed"
        if errors:
            text += f", {errors} failed"
        now = monotonic()
        if text == batch["last_text"] or now - batch["last_edit"] < BATCH_EDIT_INTERVAL:
            return None
        batch["last_edit"] = now
        batch["last_text"] = text
        return text

    # ── summary ─────────────────────────────────────────────────────

    async def _publish_summary(self, batch) -> None:
        head = f"<b>Batch {batch['name']} Complete</b>\n\n"
        total_files = 0
        total_corrupted = 0
        total_size = 0
        all_files = {}

        for res in batch["results"]:
            total_files += res["folders"]
            total_corrupted += res["corrupted"]
            total_size += res["size"]
            if res["files"]:
                all_files.update(res["files"])

        head += f"<b>Total Files:</b> {total_files}"
        head += f"\n<b>Total Size:</b> {get_readable_file_size(total_size)}"
        if total_corrupted:
            head += f"\n<b>Corrupted Files:</b> {total_corrupted}"
        # Every child carried its own mid in its result; this line is the only
        # place a bulk names them, and without it a task inside one could never
        # be /copy'd.
        mids = [str(res["mid"]) for res in batch["results"] if "mid" in res]
        if mids:
            head += f"\n<b>Task IDs:</b> {', '.join(mids)}"
        head += self._error_digest(batch["errors"])
        head += f"\n<b>cc:</b> {self.tag}\n\n"

        await self._send_chunked(batch["anchor"], head, all_files)
        await self._release(batch)

    def _error_digest(self, errors):
        """Compact rundown of the failures, since each one stayed silent."""
        if not errors:
            return ""
        digest = f"\n<b>Failed:</b> {len(errors)}"
        for err in errors[:MAX_LISTED_ERRORS]:
            name = escape(str(err["name"]))[:48]
            reason = escape(str(err["error"]).replace("\n", " "))[:96]
            digest += f"\n• <code>{name}</code>: {reason}"
        if len(errors) > MAX_LISTED_ERRORS:
            digest += f"\n• …and {len(errors) - MAX_LISTED_ERRORS} more"
        return digest

    async def _send_chunked(self, anchor, head, all_files) -> None:
        """Edit the anchor with the summary, spilling over into replies."""
        if not all_files:
            await edit_message(anchor, head)
            return
        msg = head
        fmsg = ""
        anchor_used = False
        for index, (flink, fname) in enumerate(all_files.items(), start=1):
            fmsg += f"{index}. <a href='{flink}'>{fname}</a>\n"
            if len(fmsg.encode()) + len(msg.encode()) > 4000:
                if anchor_used:
                    await send_message(anchor, msg + fmsg)
                else:
                    await edit_message(anchor, msg + fmsg)
                    anchor_used = True
                await sleep(1)
                fmsg = ""
                msg = ""
        if fmsg:
            if anchor_used:
                await send_message(anchor, msg + fmsg)
            else:
                await edit_message(anchor, msg + fmsg)

    async def _release(self, batch) -> None:
        """Drop the batch bookkeeping and the command messages it owns."""
        multi_tags.discard(self.multi_tag)
        if multi_batches.pop(self.multi_tag, None) is None:
            return
        for cmd_msg in batch.get("cmd_msgs", []):
            await delete_message(cmd_msg)
