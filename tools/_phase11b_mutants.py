"""Mutation check for the Fase 11b differential harness.

A harness that reports "identical" tells you nothing until you know it can tell
the difference. This breaks the extracted batcher -- and the uploader's side of
the seam -- one small change at a time, and demands that
`tools/_phase11b_diff.py` fail on every single one.

A mutation that survives is not a pass. It is either a hole in the harness or a
line that does not matter, and which of the two it is has to be established, not
assumed.

Run from the repo root: `python tools/_phase11b_mutants.py`
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BATCHER = ROOT / "bot/helper/mirror_leech_utils/upload_utils/media_group_batcher.py"
UPLOADER = ROOT / "bot/helper/mirror_leech_utils/upload_utils/telegram_uploader.py"

# (name, file, text to find, replacement). Each is one plausible way to get the
# grouping subtly wrong while leaving code that still imports and runs.
MUTANTS = [
    # --- the policy constants ---
    ("group size lowered", BATCHER, "GROUP_SIZE = 10", "GROUP_SIZE = 9"),
    (
        "split pattern loses the part form",
        BATCHER,
        r'SPLIT_NAME_RE = r".+(?=\.0*\d+$)|.+(?=\.part\d+\..+$)"',
        r'SPLIT_NAME_RE = r".+(?=\.0*\d+$)"',
    ),
    # --- what goes into which pile ---
    (
        "cancellation no longer stops the classification",
        BATCHER,
        """        if self._sender.is_cancelled:
            return None
        anchor = self._sender.anchor""",
        "        anchor = self._sender.anchor",
    ),
    (
        "cancellation no longer stops the filing",
        BATCHER,
        """        if self._sender.is_cancelled:
            return
        anchor = self._sender.anchor""",
        "        anchor = self._sender.anchor",
    ),
    (
        "split-named photos start forming groups",
        BATCHER,
        "if self.enabled and anchor.video and re_match(SPLIT_NAME_RE, o_path):",
        "if self.enabled and re_match(SPLIT_NAME_RE, o_path):",
    ),
    (
        "any video forms a group, split-named or not",
        BATCHER,
        "if self.enabled and anchor.video and re_match(SPLIT_NAME_RE, o_path):",
        "if self.enabled and anchor.video:",
    ),
    (
        "grouping is on for split videos regardless of the switch",
        BATCHER,
        "if self.enabled and anchor.video and re_match(SPLIT_NAME_RE, o_path):",
        "if anchor.video and re_match(SPLIT_NAME_RE, o_path):",
    ),
    (
        "documents are classified as videos",
        BATCHER,
        "        if anchor.photo or anchor.video:",
        "        if anchor.photo or anchor.video or anchor.document:",
    ),
    (
        "the album ignores the grouping switch",
        BATCHER,
        "        if self.enabled and (anchor.photo or anchor.video):",
        "        if anchor.photo or anchor.video:",
    ),
    (
        "anything unbucketed joins the album",
        BATCHER,
        "        if self.enabled and (anchor.photo or anchor.video):",
        "        if self.enabled:",
    ),
    (
        "plain documents are grouped by their whole name",
        BATCHER,
        """            if match := re_match(SPLIT_NAME_RE, o_path):
                await self._queue("documents", match.group(0))""",
        '            await self._queue("documents", o_path)',
    ),
    (
        "a video part is filed into the document bucket",
        BATCHER,
        '        if bucket == "videos":',
        "        if bucket is not None:",
    ),
    (
        "a classified part is filed nowhere",
        BATCHER,
        """        if bucket == "videos":
            await self._queue("videos", re_match(SPLIT_NAME_RE, o_path).group(0))
            return""",
        """        if bucket == "videos":
            return""",
    ),
    (
        "a video part is not reported",
        BATCHER,
        '                return "videos"',
        "                return None",
    ),
    (
        "a document is not reported",
        BATCHER,
        '            return "documents"',
        "            return None",
    ),
    # --- when a full pile goes out ---
    (
        "the album goes out one short",
        BATCHER,
        "            if len(self._album_msgs) == GROUP_SIZE:",
        "            if len(self._album_msgs) >= GROUP_SIZE - 1:",
    ),
    (
        "a full album is not sent",
        BATCHER,
        """            if len(self._album_msgs) == GROUP_SIZE:
                await self.send_album()""",
        "            pass",
    ),
    (
        "a split group re-sends past ten",
        BATCHER,
        """        if len(msgs) == GROUP_SIZE:
            await self._send_bucket(pname, key)""",
        """        if len(msgs) >= GROUP_SIZE:
            await self._send_bucket(pname, key)""",
    ),
    (
        "a part always leaves a hold behind",
        BATCHER,
        """        if len(msgs) == GROUP_SIZE:
            await self._send_bucket(pname, key)
        else:
            self._holding = True""",
        """        if len(msgs) == GROUP_SIZE:
            await self._send_bucket(pname, key)
        self._holding = True""",
    ),
    (
        "a part leaves no hold at all",
        BATCHER,
        """        else:
            self._holding = True""",
        """        else:
            pass""",
    ),
    # --- the album ---
    (
        "the album is not cleared before it can fail",
        BATCHER,
        """        msgs = self._album_msgs
        self._album_msgs = []""",
        "        msgs = list(self._album_msgs)",
    ),
    (
        "an album of one is sent",
        BATCHER,
        "        if len(msgs) < 2:",
        "        if len(msgs) < 1:",
    ),
    (
        "an album of two is held back",
        BATCHER,
        "        if len(msgs) < 2:",
        "        if len(msgs) < 3:",
    ),
    (
        "a reclassified album is sent anyway",
        BATCHER,
        "        if len(media) != len(msgs):",
        "        if len(media) > len(msgs):",
    ),
    (
        "the skipped album is not logged",
        BATCHER,
        "            LOGGER.info("
        '"Skipping album, not every message is a photo or video")',
        "            pass",
    ),
    (
        "videos are dropped from the album payload",
        BATCHER,
        """            elif msg.video:
                media.append(
                    InputMediaVideo(media=msg.video.file_id, caption=msg.caption)
                )""",
        """            elif msg.video:
                pass""",
    ),
    # --- the split groups ---
    (
        "a group of one is sent",
        BATCHER,
        "                if len(msgs) <= 1:",
        "                if len(msgs) < 1:",
    ),
    (
        "a group of one stops the flush",
        BATCHER,
        """                if len(msgs) <= 1:
                    continue""",
        """                if len(msgs) <= 1:
                    break""",
    ),
    (
        "the flush iterates the bucket it deletes from",
        BATCHER,
        "            for subkey, msgs in list(bucket.items()):",
        "            for subkey, msgs in bucket.items():",
    ),
    (
        "every flush swallows its errors",
        BATCHER,
        "                if not where:",
        "                if where is None:",
    ),
    (
        "no flush swallows its errors",
        BATCHER,
        "                if not where:",
        "                if where is not None:",
    ),
    (
        "the swallowed error is not logged",
        BATCHER,
        """                    LOGGER.info(
                        f"While sending media group at the end of {where}. Error: {e}"
                    )""",
        "                    pass",
    ),
    (
        "a bucket is forgotten even when the send did not land",
        BATCHER,
        """        if await self._ship(msgs, self._input_media(msgs, key)) is None:
            return
        del self._media_dict[key][subkey]""",
        """        await self._ship(msgs, self._input_media(msgs, key))
        del self._media_dict[key][subkey]""",
    ),
    (
        "a sent bucket is kept",
        BATCHER,
        "        del self._media_dict[key][subkey]",
        "        pass",
    ),
    (
        "the resolved messages are not written back",
        BATCHER,
        "            msgs[index] = await self._sender.resolve_message(msg[0], msg[1])",
        "            await self._sender.resolve_message(msg[0], msg[1])",
    ),
    (
        "documents are sent as videos",
        BATCHER,
        '            if key == "videos":',
        '            if key != "videos":',
    ),
    # --- the hold the next file has to settle ---
    (
        "the hold is cleared before the flush can fail",
        BATCHER,
        """        if not match or match.group(0) not in stems:
            await self.flush()
        self._holding = False""",
        """        self._holding = False
        if not match or match.group(0) not in stems:
            await self.flush()""",
    ),
    (
        "a continued stem is flushed anyway",
        BATCHER,
        "        if not match or match.group(0) not in stems:",
        "        if True:",
    ),
    (
        "a broken hold flushes nothing",
        BATCHER,
        "            await self.flush()",
        "            pass",
    ),
    (
        "a settled hold is settled again",
        BATCHER,
        """        if not self._holding:
            return
        match = re_match(SPLIT_NAME_RE, f_path)""",
        "        match = re_match(SPLIT_NAME_RE, f_path)",
    ),
    (
        "the stems are read from one bucket only",
        BATCHER,
        "        stems = [stem for bucket in self._media_dict.values()"
        " for stem in bucket]",
        '        stems = list(self._media_dict["videos"])',
    ),
    # --- what "still holding something back" means ---
    (
        "pending forgets the album",
        BATCHER,
        "return self._holding or bool(self._album_msgs)",
        "return self._holding",
    ),
    (
        "pending forgets the hold",
        BATCHER,
        "return self._holding or bool(self._album_msgs)",
        "return bool(self._album_msgs)",
    ),
    # --- the send the batcher borrows ---
    (
        "the group is sent to the wrong reply target",
        BATCHER,
        "            reply_to_message_id=msgs[0].reply_to_message_id,",
        "            reply_to_message_id=msgs[-1].reply_to_message_id,",
    ),
    (
        "a group that never landed is booked as delivered",
        BATCHER,
        """        if sent is None:
            return None
        await self._sender.retire_group(msgs, sent)""",
        "        await self._sender.retire_group(msgs, sent)",
    ),
    # --- the uploader's side of the seam ---
    (
        "the grouping switch is never read",
        UPLOADER,
        "        self._batcher.enabled = self._listener.user_dict.get("
        '"MEDIA_GROUP", False) or (',
        "        self._batcher.enabled = False or (",
    ),
    (
        "the stale hold is never settled",
        UPLOADER,
        "            await self._batcher.release_unless_continued(f_path)",
        "            pass",
    ),
    (
        "the end-of-task flush stops swallowing",
        UPLOADER,
        "        await self._batcher.flush(where)",
        "        await self._batcher.flush()",
    ),
    (
        "the bucket the batcher chose is ignored",
        UPLOADER,
        "        attempt.key = self._batcher.classify(o_path) or attempt.key",
        "        self._batcher.classify(o_path)",
    ),
    (
        "the bucket is settled after the filing that can fail",
        UPLOADER,
        """        attempt.key = self._batcher.classify(o_path) or attempt.key
        await self._batcher.track(o_path)""",
        """        await self._batcher.track(o_path)
        attempt.key = self._batcher.classify(o_path) or attempt.key""",
    ),
    (
        "nothing is filed at all",
        UPLOADER,
        "        await self._batcher.track(o_path)",
        "        pass",
    ),
    (
        "the base message is dropped while a group waits",
        UPLOADER,
        "        if self._base_msg and not self._batcher.pending:",
        "        if self._base_msg:",
    ),
    (
        "the album is not sent before a document",
        UPLOADER,
        """        await self._batcher.send_album()
        self._sent_msg = await self._send_client.send_document(""",
        "        self._sent_msg = await self._send_client.send_document(",
    ),
    # --- what the uploader does with a group that went out ---
    (
        "the replaced links are kept",
        UPLOADER,
        """            if msg.link in self._msgs_dict:
                del self._msgs_dict[msg.link]
            await delete_message(msg)""",
        "            await delete_message(msg)",
    ),
    (
        "the replaced messages are kept",
        UPLOADER,
        "            await delete_message(msg)\n",
        "            pass\n",
    ),
    (
        "album links are booked without the files_links switch",
        UPLOADER,
        """        if self._files_links and (
            self._listener.is_super_chat or self._listener.up_dest
        ):""",
        "        if self._listener.is_super_chat or self._listener.up_dest:",
    ),
    (
        "the album is not copied to the clone dumps",
        UPLOADER,
        "        await self._copy_group_to_clone_dumps(sent[-1].chat.id, sent[-1].id)",
        "        pass",
    ),
    (
        "the reply chain re-anchors to the wrong end of the album",
        UPLOADER,
        "        self._sent_msg = sent[-1]\n        if self._base_msg:",
        "        self._sent_msg = sent[0]\n        if self._base_msg:",
    ),
    (
        "the base message survives the album that replaced it",
        UPLOADER,
        "        self._sent_msg = sent[-1]\n        if self._base_msg:",
        "        self._sent_msg = sent[-1]\n        if False:",
    ),
    (
        "the group send drops the silent flag",
        UPLOADER,
        """            reply_to_message_id=reply_to_message_id,
            disable_notification=True,""",
        "            reply_to_message_id=reply_to_message_id,",
    ),
    (
        "fetching a message back loses its flood guard",
        UPLOADER,
        """        return await self._pacer.guard(
            self._group_client.get_messages,
            chat_id=chat_id,
            message_ids=message_id,
        )""",
        """        return await self._group_client.get_messages(
            chat_id=chat_id,
            message_ids=message_id,
        )""",
    ),
    (
        "the anchor is read once instead of every time",
        UPLOADER,
        """    @property
    def anchor(self):
        \"\"\"The message the next send replies under.\"\"\"
        return self._sent_msg""",
        """    @property
    def anchor(self):
        \"\"\"The message the next send replies under.\"\"\"
        return self._base_msg or self._sent_msg""",
    ),
]


def _run_harness():
    proc = subprocess.run(
        [sys.executable, "tools/_phase11b_diff.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout


def main():
    originals = {path: path.read_text() for path in (BATCHER, UPLOADER)}
    survivors = []
    broken = []
    try:
        for name, path, find, replace in MUTANTS:
            src = originals[path]
            if src.count(find) != 1:
                broken.append((name, f"anchor matched {src.count(find)} times"))
                print(f"  ! stale     {name}")
                continue
            path.write_text(src.replace(find, replace))
            code, out = _run_harness()
            path.write_text(src)
            if code == 0:
                survivors.append((name, out.strip().splitlines()[-1]))
                print(f"  SURVIVED  {name}")
            else:
                print(f"  caught    {name}")
    finally:
        for path, src in originals.items():
            path.write_text(src)

    total = len(MUTANTS)
    print(f"\n{total - len(survivors) - len(broken)}/{total} mutations caught")
    for name, why in broken:
        print(f"  ! anchor stale: {name} ({why})")
    for name, last in survivors:
        print(f"  ! survived: {name} -- harness said: {last}")
    return 1 if survivors or broken else 0


if __name__ == "__main__":
    raise SystemExit(main())
