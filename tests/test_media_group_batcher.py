"""Direct tests for `MediaGroupBatcher`.

The batcher talks to telegram only through the five members it asks of its
sender, so it is tested against a fake sender and no uploader at all -- the
module is loaded straight from its file, with none of the `bot.*` stubbing the
uploader's own test files need.

What is pinned here is deliberately lopsided, and all of it is load-bearing:
split parts group by stem while everything else photo- or video-shaped joins one
album, documents are only ever grouped, a group of one is neither sent nor
dropped, and an album is cleared before the send that may reject it.
"""

import importlib.util
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parent.parent
    / "bot/helper/mirror_leech_utils/upload_utils/media_group_batcher.py"
)
_spec = importlib.util.spec_from_file_location("media_group_batcher", MODULE_PATH)
batcher_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(batcher_module)
MediaGroupBatcher = batcher_module.MediaGroupBatcher


class FakeMessage:
    """Minimal stand-in for a pyrogram Message."""

    _next_id = 500

    def __init__(self, kind=None, caption=None, registry=None):
        FakeMessage._next_id += 1
        self.id = FakeMessage._next_id
        self.chat = SimpleNamespace(id=-1001)
        self.caption = caption
        self.reply_to_message_id = 7
        self.link = f"https://t.me/c/1001/{self.id}"
        for name in ("photo", "video", "document", "audio"):
            value = (
                SimpleNamespace(file_id=f"{name}{self.id}") if kind == name else None
            )
            setattr(self, name, value)
        if registry is not None:
            registry[self.id] = self


class FakeSender:
    """The five members the batcher asks of the uploader, and nothing else."""

    def __init__(self, group_answers=None, resolve_as=None):
        self.registry = {}
        self.calls = []
        self.retired = []
        self.is_cancelled = False
        self.anchor = FakeMessage(registry=self.registry)
        self._group_answers = list(group_answers or [])
        self._resolve_as = list(resolve_as or [])

    def sends(self, kind, caption=None):
        """Stand in for the uploader having just sent a file."""
        self.anchor = FakeMessage(kind, caption=caption, registry=self.registry)
        return self.anchor

    async def resolve_message(self, chat_id, message_id):
        self.calls.append(("resolve", message_id))
        msg = self.registry.get(message_id)
        if self._resolve_as:
            kind = self._resolve_as.pop(0)
            if kind != "same":
                return FakeMessage(kind, caption=None if msg is None else msg.caption)
        return msg

    async def send_group(self, chat_id, media, reply_to_message_id):
        payload = [(type(m).__name__, m.caption) for m in media]
        self.calls.append(("send_group", payload))
        if self._group_answers:
            answer = self._group_answers.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            if answer is None:
                return None
        return [FakeMessage("photo", caption=m.caption) for m in media]

    async def retire_group(self, originals, sent):
        self.retired.append((list(originals), list(sent)))
        self.anchor = sent[-1]

    @property
    def groups_sent(self):
        return [payload for name, payload in self.calls if name == "send_group"]


def _make(enabled=True, **kwargs):
    sender = FakeSender(**kwargs)
    batcher = MediaGroupBatcher(sender)
    batcher.enabled = enabled
    return batcher, sender


DIR = "/tmp/task"


async def _upload(batcher, sender, kind, name):
    """One file sent and then filed, the way `_send_one` does it.

    The path is spelled out in full, because the uploader hands the very same
    string to `track` and to `release_unless_continued` -- the stems the two
    compare have to be built the same way.
    """
    o_path = f"{DIR}/{name}"
    sender.sends(kind, caption=f"<code>{name}</code>")
    bucket = batcher.classify(o_path)
    await batcher.track(o_path)
    return bucket


# --- filing: which pile a sent message lands in ----------------------------


@pytest.mark.asyncio
async def test_split_video_parts_are_grouped_not_albumed():
    batcher, sender = _make()

    bucket = await _upload(batcher, sender, "video", "big.mkv.001")

    assert bucket == "videos"
    assert list(batcher._media_dict["videos"]) == [f"{DIR}/big.mkv"]
    assert batcher._album_msgs == []


@pytest.mark.asyncio
async def test_a_whole_video_joins_the_album():
    batcher, sender = _make()

    bucket = await _upload(batcher, sender, "video", "movie.mkv")

    assert bucket is None
    assert batcher._media_dict["videos"] == {}
    assert len(batcher._album_msgs) == 1


@pytest.mark.asyncio
async def test_documents_never_join_the_album():
    batcher, sender = _make()

    bucket = await _upload(batcher, sender, "document", "a.bin")

    assert bucket == "documents"
    assert batcher._album_msgs == []
    assert batcher._media_dict["documents"] == {}


@pytest.mark.asyncio
async def test_split_named_photos_fall_to_the_album():
    """The split branch demands a video, so a part-named photo is not grouped."""
    batcher, sender = _make()

    bucket = await _upload(batcher, sender, "photo", "shot.jpg.001")

    assert bucket is None
    assert batcher._media_dict["videos"] == {}
    assert len(batcher._album_msgs) == 1


@pytest.mark.asyncio
async def test_audio_is_filed_nowhere():
    batcher, sender = _make()

    assert await _upload(batcher, sender, "audio", "song.mp3.001") is None
    assert batcher._album_msgs == []
    assert batcher._media_dict == {"videos": {}, "documents": {}}


@pytest.mark.asyncio
async def test_a_cancelled_task_files_nothing():
    batcher, sender = _make()
    sender.sends("photo")
    sender.is_cancelled = True

    assert batcher.classify("a.jpg") is None
    await batcher.track("a.jpg")

    assert batcher._album_msgs == []
    assert not batcher.pending


@pytest.mark.asyncio
async def test_grouping_off_files_nothing():
    batcher, sender = _make(enabled=False)

    assert await _upload(batcher, sender, "photo", "a.jpg") is None
    assert await _upload(batcher, sender, "video", "big.mkv.001") is None
    assert await _upload(batcher, sender, "document", "arch.part1.rar") is None

    assert batcher._album_msgs == []
    assert batcher._media_dict == {"videos": {}, "documents": {}}


# --- the bucket is settled before anything can fail ------------------------


def test_classify_files_nothing_by_itself():
    """It is asked before the send, so it must have no effect of its own."""
    batcher, sender = _make()
    sender.sends("video")

    assert batcher.classify(f"{DIR}/big.mkv.001") == "videos"
    assert batcher.classify(f"{DIR}/big.mkv.001") == "videos"

    assert batcher._media_dict["videos"] == {}
    assert not batcher.pending


@pytest.mark.asyncio
async def test_the_bucket_is_known_even_when_the_filing_fails():
    """A failed send is judged on the bucket, so it cannot depend on the send.

    The tenth part fills the group, the group send is refused, and the caller
    still has to know it was dealing with a video.
    """
    batcher, sender = _make(group_answers=[RuntimeError("group refused")])
    for i in range(1, 10):
        await _upload(batcher, sender, "video", f"big.mkv.{i:03d}")

    sender.sends("video")
    assert batcher.classify(f"{DIR}/big.mkv.010") == "videos"
    with pytest.raises(RuntimeError):
        await batcher.track(f"{DIR}/big.mkv.010")


# --- queueing: holding parts until the group is full ----------------------


@pytest.mark.asyncio
async def test_a_part_holds_its_group_until_it_is_full():
    batcher, sender = _make()

    for _ in range(9):
        sender.sends("document")
        await batcher._queue("documents", "big.rar")
        assert batcher.pending

    assert len(batcher._media_dict["documents"]["big.rar"]) == 9
    assert sender.groups_sent == []


@pytest.mark.asyncio
async def test_the_tenth_part_sends_the_group():
    batcher, sender = _make()

    for i in range(10):
        sender.sends("video", caption=f"part{i}")
        await batcher._queue("videos", "big.mkv")

    assert len(sender.groups_sent) == 1
    assert len(sender.groups_sent[0]) == 10
    assert {name for name, _cap in sender.groups_sent[0]} == {"InputMediaVideo"}
    # a group that went out is forgotten -- but the hold the ninth part left
    # still stands, and it is the next file that settles it. Harmlessly: the
    # flush it may trigger finds nothing left to send.
    assert batcher._media_dict["videos"] == {}
    assert batcher._holding is True


@pytest.mark.asyncio
async def test_parts_are_keyed_by_their_shared_stem():
    batcher, sender = _make()

    for pname in ("big.mkv", "big.mkv", "other.mkv"):
        sender.sends("document")
        await batcher._queue("documents", pname)

    assert len(batcher._media_dict["documents"]["big.mkv"]) == 2
    assert len(batcher._media_dict["documents"]["other.mkv"]) == 1


@pytest.mark.asyncio
async def test_documents_are_sent_as_documents():
    batcher, sender = _make()
    for i in range(1, 11):
        await _upload(batcher, sender, "document", f"arch.part{i}.rar")

    assert {name for name, _cap in sender.groups_sent[0]} == {"InputMediaDocument"}


# --- the album -------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_album_of_one_is_never_sent():
    batcher, sender = _make()
    await _upload(batcher, sender, "photo", "only.jpg")

    assert await batcher.send_album() is None
    assert sender.groups_sent == []
    assert batcher._album_msgs == []


@pytest.mark.asyncio
async def test_photos_and_videos_keep_their_order_in_the_album():
    batcher, sender = _make()
    for kind, name in (("photo", "a.jpg"), ("video", "b.mp4"), ("photo", "c.jpg")):
        await _upload(batcher, sender, kind, name)

    await batcher.send_album()

    assert sender.groups_sent == [
        [
            ("InputMediaPhoto", "<code>a.jpg</code>"),
            ("InputMediaVideo", "<code>b.mp4</code>"),
            ("InputMediaPhoto", "<code>c.jpg</code>"),
        ]
    ]


@pytest.mark.asyncio
async def test_the_album_goes_out_at_ten_on_its_own():
    batcher, sender = _make()
    for i in range(10):
        await _upload(batcher, sender, "photo", f"{i}.jpg")

    assert len(sender.groups_sent) == 1
    assert len(sender.groups_sent[0]) == 10
    assert batcher._album_msgs == []


@pytest.mark.asyncio
async def test_the_album_is_cleared_before_a_failing_send():
    """A rejected album is gone from the bookkeeping; the messages stay put."""
    batcher, sender = _make(group_answers=[RuntimeError("too big")])
    for i in range(3):
        await _upload(batcher, sender, "photo", f"{i}.jpg")

    with pytest.raises(RuntimeError):
        await batcher.send_album()

    assert batcher._album_msgs == []


@pytest.mark.asyncio
async def test_the_album_is_skipped_when_a_message_comes_back_a_document(caplog):
    batcher, sender = _make(resolve_as=["same", "document"])
    for i in range(2):
        await _upload(batcher, sender, "photo", f"{i}.jpg")

    with caplog.at_level(logging.INFO, logger="media_group_batcher"):
        assert await batcher.send_album() is None

    assert sender.groups_sent == []
    assert "Skipping album" in caplog.text


@pytest.mark.asyncio
async def test_an_album_that_never_landed_is_not_retired():
    batcher, sender = _make(group_answers=[None])
    for i in range(2):
        await _upload(batcher, sender, "photo", f"{i}.jpg")

    assert await batcher.send_album() is None
    assert sender.retired == []


# --- flushing what is left -------------------------------------------------


@pytest.mark.asyncio
async def test_a_group_of_one_is_neither_sent_nor_dropped():
    batcher, sender = _make()
    await _upload(batcher, sender, "video", "big.mkv.001")

    await batcher.flush()
    await batcher.flush("task")

    assert sender.groups_sent == []
    assert list(batcher._media_dict["videos"]) == [f"{DIR}/big.mkv"]


@pytest.mark.asyncio
async def test_flush_sends_the_next_bucket_after_skipping_a_lone_one():
    batcher, sender = _make()
    await _upload(batcher, sender, "video", "one.mkv.001")
    await _upload(batcher, sender, "video", "two.mkv.001")
    await _upload(batcher, sender, "video", "two.mkv.002")

    await batcher.flush("task")

    assert len(sender.groups_sent) == 1
    assert list(batcher._media_dict["videos"]) == [f"{DIR}/one.mkv"]


@pytest.mark.asyncio
async def test_an_unnamed_flush_lets_the_error_out():
    """Mid-upload there is still a file to fail, so the error has to travel."""
    batcher, sender = _make(group_answers=[RuntimeError("no rights")])
    for i in (1, 2):
        await _upload(batcher, sender, "video", f"big.mkv.{i:03d}")

    with pytest.raises(RuntimeError):
        await batcher.flush()


@pytest.mark.asyncio
async def test_a_named_flush_swallows_the_error_and_logs(caplog):
    """At the end of a task there is nothing left to abort."""
    batcher, sender = _make(group_answers=[RuntimeError("no rights")])
    for i in (1, 2):
        await _upload(batcher, sender, "video", f"big.mkv.{i:03d}")

    with caplog.at_level(logging.INFO, logger="media_group_batcher"):
        await batcher.flush("task")

    assert "at the end of task" in caplog.text
    assert "no rights" in caplog.text


@pytest.mark.asyncio
async def test_a_bucket_is_kept_when_the_send_never_landed():
    batcher, sender = _make(group_answers=[None])
    for i in (1, 2):
        await _upload(batcher, sender, "video", f"big.mkv.{i:03d}")

    await batcher.flush()

    assert list(batcher._media_dict["videos"]) == [f"{DIR}/big.mkv"]
    assert sender.retired == []


@pytest.mark.asyncio
async def test_a_sent_bucket_is_forgotten():
    batcher, sender = _make()
    for i in (1, 2):
        await _upload(batcher, sender, "video", f"big.mkv.{i:03d}")

    await batcher.flush()

    assert batcher._media_dict["videos"] == {}
    assert len(sender.retired) == 1
    assert len(sender.retired[0][0]) == 2


# --- the hold the next file has to settle ---------------------------------


@pytest.mark.asyncio
async def test_a_continued_stem_keeps_the_group_waiting():
    batcher, sender = _make()
    for i in (1, 2):
        await _upload(batcher, sender, "video", f"big.mkv.{i:03d}")

    await batcher.release_unless_continued("/tmp/task/big.mkv.003")

    assert sender.groups_sent == []
    assert list(batcher._media_dict["videos"]) == [f"{DIR}/big.mkv"]


@pytest.mark.asyncio
async def test_an_unrelated_file_sends_the_group_first():
    """The group has to keep the order its parts were uploaded in."""
    batcher, sender = _make()
    for i in (1, 2):
        await _upload(batcher, sender, "video", f"big.mkv.{i:03d}")

    await batcher.release_unless_continued("/tmp/task/plain.jpg")

    assert len(sender.groups_sent) == 1
    assert not batcher.pending


@pytest.mark.asyncio
async def test_a_document_stem_continues_its_own_group():
    batcher, sender = _make()
    for i in (1, 2):
        await _upload(batcher, sender, "document", f"arch.part{i}.rar")

    await batcher.release_unless_continued("/tmp/task/arch.part3.rar")

    assert sender.groups_sent == []
    assert list(batcher._media_dict["documents"]) == [f"{DIR}/arch"]


@pytest.mark.asyncio
async def test_nothing_held_back_means_nothing_to_settle():
    batcher, sender = _make()
    for i in range(2):
        await _upload(batcher, sender, "photo", f"{i}.jpg")

    await batcher.release_unless_continued("/tmp/task/plain.jpg")

    # a pending album is not what the hold is about
    assert sender.groups_sent == []
    assert len(batcher._album_msgs) == 2


@pytest.mark.asyncio
async def test_the_hold_stands_when_the_flush_fails():
    """The next file gets the same chance to send the group."""
    batcher, sender = _make(group_answers=[RuntimeError("no rights")])
    for i in (1, 2):
        await _upload(batcher, sender, "video", f"big.mkv.{i:03d}")

    with pytest.raises(RuntimeError):
        await batcher.release_unless_continued("/tmp/task/plain.jpg")

    assert batcher._holding is True


@pytest.mark.asyncio
async def test_pending_covers_both_the_hold_and_the_album():
    batcher, sender = _make()
    assert not batcher.pending

    await _upload(batcher, sender, "photo", "a.jpg")
    assert batcher.pending, "a waiting album counts"

    await batcher.send_album()
    assert not batcher.pending

    await _upload(batcher, sender, "video", "big.mkv.001")
    assert batcher.pending, "a held split group counts"
