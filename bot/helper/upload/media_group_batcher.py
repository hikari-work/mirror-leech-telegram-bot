"""Holding media back so it can go out as one album instead of many messages."""

from asyncio import gather
from logging import getLogger
from re import match as re_match
from typing import TYPE_CHECKING

from pyrogram.types import InputMediaDocument, InputMediaPhoto, InputMediaVideo

if TYPE_CHECKING:
    # Annotation only. The three concrete classes above are what this module
    # builds; their shared base is imported separately so the runtime import
    # list stays the set of names actually used.
    from pyrogram.types import InputMedia

LOGGER = getLogger(__name__)

# Matches the stem of a split part, e.g. "movie.mkv" out of "movie.mkv.001"
# or "movie.mkv.part2.rar". Parts sharing a stem are grouped into one album.
SPLIT_NAME_RE = r".+(?=\.0*\d+$)|.+(?=\.part\d+\..+$)"

# What telegram accepts in one media group, and therefore how many messages are
# held back before the group is sent.
GROUP_SIZE = 10


class MediaGroupBatcher:
    """Files each sent message into a group, and sends the group when it is full.

    Two kinds of grouping live here and they never mix. Parts of one split file
    are keyed by the stem they share and go out as a group per stem; everything
    else photo- or video-shaped joins a single running album. Which of the two a
    message lands in is decided from what telegram made of it, not from what was
    uploaded -- a video sent as a document is a document here.

    The uploader remains the one thing talking to telegram, and this object asks
    it for six:

    ``anchor``
        the message the reply chain currently hangs under. Re-read every time,
        because sending a group moves it.
    ``anchor_for_group``
        that same message when the client the album goes out through can use it
        as it stands, and None when it has to be read back first. The batcher
        does not know which client is which and does not need to.
    ``is_cancelled``
        whether the task is still worth sending anything for.
    ``resolve_message(chat_id, message_id)``
        a message fetched back, for the ``file_id`` an album has to reuse.
    ``send_group(chat_id, media, reply_to_message_id)``
        the album send, or None when it never reached telegram.
    ``retire_group(originals, sent)``
        book an album that went out and dispose of what it replaced.
    """

    def __init__(self, sender):
        self._sender = sender
        # Resolved once per task from the MEDIA_GROUP setting; with it off
        # nothing is ever held back and every file stays its own message.
        self.enabled = False
        self._media_dict = {"videos": {}, "documents": {}}
        self._album_msgs = []
        self._holding = False

    @property
    def pending(self):
        """True while anything is still being held back."""
        return self._holding or bool(self._album_msgs)

    def classify(self, o_path):
        """The media bucket the message just sent belongs to, or None.

        Decided from what telegram made of the file, not from what was
        uploaded, and pure so the caller can have the answer before anything is
        filed: filing sends groups, sending can fail, and the bucket is what a
        failed send gets judged on.

        None means the message is filed under no bucket -- it may still join the
        album, which is not a bucket.
        """
        if self._sender.is_cancelled:
            return None
        anchor = self._sender.anchor
        if anchor.photo or anchor.video:
            # A photo named like a split part is left to the album: only videos
            # and documents are ever grouped by stem.
            if self.enabled and anchor.video and re_match(SPLIT_NAME_RE, o_path):
                return "videos"
            return None
        if self.enabled and anchor.document:
            return "documents"
        return None

    async def track(self, o_path):
        """File the message just sent into its split group or into the album.

        A document is only ever grouped, and only when its name says it is a
        part of something; anything else photo- or video-shaped that is not a
        split part joins the album.
        """
        bucket = self.classify(o_path)
        if bucket == "videos":
            # ``classify`` only answers "videos" for a name that matches, so this
            # match is there; asking again keeps that local instead of threading
            # it out of a function whose answer is just the bucket.
            if match := re_match(SPLIT_NAME_RE, o_path):
                await self._queue("videos", match.group(0))
            return
        if bucket == "documents":
            if match := re_match(SPLIT_NAME_RE, o_path):
                await self._queue("documents", match.group(0))
            return
        if self._sender.is_cancelled:
            return
        anchor = self._sender.anchor
        if self.enabled and (anchor.photo or anchor.video):
            self._album_msgs.append(self._hold(anchor))
            if len(self._album_msgs) == GROUP_SIZE:
                await self.send_album()

    def _hold(self, anchor):
        """A sent message as it is kept until its group goes out.

        ``[chat_id, message_id, message]``: the coordinates are always there,
        because the message may have to be read back, and the message itself is
        filled in when it does not. The uploader answers None to
        ``anchor_for_group`` when the client that will send the album is not the
        one that sent this message, and then reading it back is the only way to
        get a ``file_id`` that client can use.
        """
        return [anchor.chat.id, anchor.id, self._sender.anchor_for_group()]

    async def _resolve(self, held):
        """The messages behind *held*, fetching back only those that need it.

        A message sent through the client the album goes out through is already
        the right one to build the album from, and asking telegram for it again
        was a round trip per member of the group.

        What is fetched is fetched together rather than one after another: the
        requests do not depend on each other, and a ten-file album used to pay
        ten round trips in series. None in the answer means the task was
        cancelled while waiting out a flood, or a fetch failed -- the caller
        gets that back rather than an ``AttributeError`` on ``msg.photo``.
        """
        waiting = [slot for slot in held if slot[2] is None]
        fetched = await gather(
            *(self._sender.resolve_message(slot[0], slot[1]) for slot in waiting),
            return_exceptions=True,
        )
        for slot, msg in zip(waiting, fetched):
            if isinstance(msg, BaseException):
                # raised only after every fetch has settled, so none is left
                # running behind the caller
                raise msg
            slot[2] = msg
        return [slot[2] for slot in held]

    async def send_album(self):
        """Send the photos and videos waiting to go out as one album.

        The pending list is taken and cleared before anything can fail: an album
        telegram refuses is gone from the bookkeeping, though the messages
        themselves stay where they already were.
        """
        msgs = self._album_msgs
        self._album_msgs = []
        if len(msgs) < 2:
            return None
        msgs = await self._resolve(msgs)
        if any(msg is None for msg in msgs):
            return None
        media: list[InputMedia] = []
        for msg in msgs:
            if msg.photo:
                media.append(
                    InputMediaPhoto(media=msg.photo.file_id, caption=msg.caption)
                )
            elif msg.video:
                media.append(
                    InputMediaVideo(media=msg.video.file_id, caption=msg.caption)
                )
        if len(media) != len(msgs):
            # telegram reclassified something on its way out, leave them alone
            LOGGER.info("Skipping album, not every message is a photo or video")
            return None
        return await self._ship(msgs, media)

    async def flush(self, where=""):
        """Send every split group that has more than one part waiting.

        *where* labels the log line and, by being set at all, says failures are
        to be swallowed: the end-of-task flush has nothing left to abort, while
        the mid-upload flush wants the error to reach the per-file handler.

        A group of one is neither sent nor dropped. A lone part already went out
        as an ordinary message, and it stays in the bucket for the rest of the
        task in case a sibling turns up.
        """
        for key, bucket in list(self._media_dict.items()):
            for subkey, msgs in list(bucket.items()):
                if len(msgs) <= 1:
                    continue
                if not where:
                    await self._send_bucket(subkey, key)
                    continue
                try:
                    await self._send_bucket(subkey, key)
                except Exception as e:
                    LOGGER.info(
                        f"While sending media group at the end of {where}. Error: {e}"
                    )

    async def release_unless_continued(self, f_path):
        """Settle the hold the previous file left, before *f_path* is sent.

        A part queued into a group that did not fill up leaves that group
        waiting. The next file either continues it -- same split stem -- or it
        does not, and then whatever is waiting has to go out first, so the group
        keeps the order the parts were uploaded in.

        The hold is only cleared once that is done: a flush that fails leaves it
        standing, and the next file gets the same chance to send the group.
        """
        if not self._holding:
            return
        match = re_match(SPLIT_NAME_RE, f_path)
        stems = [stem for bucket in self._media_dict.values() for stem in bucket]
        if not match or match.group(0) not in stems:
            await self.flush()
        self._holding = False

    async def _queue(self, key, pname):
        """Hold a split part until its group is full, then send it as one."""
        anchor = self._sender.anchor
        msgs = self._media_dict[key].setdefault(pname, [])
        msgs.append(self._hold(anchor))
        if len(msgs) == GROUP_SIZE:
            await self._send_bucket(pname, key)
        else:
            self._holding = True

    async def _send_bucket(self, subkey, key):
        """Send one split group, and forget it only once telegram has it.

        A bucket holds ``[chat_id, message_id, message]`` slots, resolved just
        before the send. The resolved list is a new one, so a send that did not
        land leaves the bucket holding the slots rather than messages -- a
        retry then resolves them again instead of reading coordinates off a
        message.
        """
        msgs = await self._resolve(self._media_dict[key][subkey])
        if any(msg is None for msg in msgs):
            return
        if await self._ship(msgs, self._input_media(msgs, key)) is None:
            return
        del self._media_dict[key][subkey]

    @staticmethod
    def _input_media(msgs, key):
        """The album payload for a split group, reusing what was uploaded."""
        media: list[InputMedia] = []
        for msg in msgs:
            if key == "videos":
                media.append(
                    InputMediaVideo(media=msg.video.file_id, caption=msg.caption)
                )
            else:
                media.append(
                    InputMediaDocument(media=msg.document.file_id, caption=msg.caption)
                )
        return media

    async def _ship(self, msgs, media):
        """Send *msgs* as one album and let the uploader retire the originals.

        What differs between an album and a group of split parts is how *media*
        was built; what happens to the messages afterwards is the same, so both
        callers land here. Returns the sent list, or None when the send did not
        go through -- a None must not be read as a delivered group.
        """
        sent = await self._sender.send_group(
            chat_id=msgs[0].chat.id,
            media=media,
            reply_to_message_id=msgs[0].reply_to_message_id,
        )
        if sent is None:
            return None
        await self._sender.retire_group(msgs, sent)
        return sent
