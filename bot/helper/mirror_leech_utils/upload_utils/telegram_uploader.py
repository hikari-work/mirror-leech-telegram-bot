from asyncio import sleep
from contextlib import asynccontextmanager
from logging import getLogger
from os import path as ospath
from os import walk
from re import match as re_match
from re import sub as re_sub
from time import time

from aiofiles.os import (
    path as aiopath,
)
from aiofiles.os import (
    remove,
    rename,
)
from aioshutil import rmtree
from natsort import natsorted
from PIL import Image
from pyrogram.errors import BadRequest, FloodPremiumWait, FloodWait, RPCError
from pyrogram.types import (
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    ReplyParameters,
)
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .... import intervals
from ....core.config_manager import Config
from ....core.telegram_manager import TgClient
from ...ext_utils.bot_utils import sync_to_async
from ...ext_utils.files_utils import get_base_name, is_archive
from ...ext_utils.media_utils import (
    get_audio_thumbnail,
    get_document_type,
    get_media_info,
    get_multiple_frames_thumbnail,
    get_video_thumbnail,
)
from ...telegram_helper.message_utils import delete_message

LOGGER = getLogger(__name__)

# Matches the stem of a split part, e.g. "movie.mkv" out of "movie.mkv.001"
# or "movie.mkv.part2.rar". Parts sharing a stem are grouped into one album.
SPLIT_NAME_RE = r".+(?=\.0*\d+$)|.+(?=\.part\d+\..+$)"


class _Attempt:
    """Mutable state of one send, shared between the sender and its wrapper.

    ``thumb`` is what the senders actually pass to telegram: they may generate
    one, so the wrapper reads it back to know what to clean up. ``key`` is the
    media bucket, which decides whether a BadRequest is worth retrying as a
    document. ``aborted`` marks a send that cancellation stopped before it
    reached telegram.
    """

    __slots__ = ("thumb", "key", "is_video", "aborted")

    def __init__(self, thumb):
        self.thumb = thumb
        self.key = None
        self.is_video = False
        self.aborted = False


class TelegramUploader:
    # How wide the gap between two files is allowed to grow, and how many
    # flood-free files it takes to start closing it again.
    _MAX_PACE = 4.0
    _CALM_FILES = 5

    def __init__(self, listener, path):
        self._last_uploaded = 0
        self._processed_bytes = 0
        self._listener = listener
        self._path = path
        self._start_time = time()
        self._total_files = 0
        self._thumb = self._listener.thumb or f"thumbnails/{listener.user_id}.jpg"
        self._msgs_dict = {}
        self._corrupted = 0
        self._media_dict = {"videos": {}, "documents": {}}
        self._album_msgs = []
        self._last_msg_in_group = False
        self._up_path = ""
        self._lprefix = ""
        self._media_group = False
        self._is_private = False
        self._sent_msg = None
        self._user_session = self._listener.user_transmission
        self._error = ""
        self._base_msg = None
        self._files_links = False
        self._pace = 0.0
        self._calm = 0

    async def _upload_progress(self, current, _):
        if self._listener.is_cancelled:
            self._send_client.stop_transmission()
        chunk_size = current - self._last_uploaded
        self._last_uploaded = current
        self._processed_bytes += chunk_size

    @property
    def _send_client(self):
        """The client that carries this file's bytes.

        A plain task stays on one client the whole way; hybrid leech flips per
        file on size, so this is read at every send rather than cached.
        """
        return TgClient.user if self._user_session else self._listener.client

    def _reply_args(self):
        """Where the next message goes: same chat and topic, under the anchor.

        This is what ``Message.reply_*`` used to fill in from the message it was
        bound to. Spelling it out means any client can send the reply, so the
        anchor no longer has to be re-fetched through the client that happens to
        need it next.
        """
        anchor = self._sent_msg
        return {
            "chat_id": anchor.chat.id,
            "reply_parameters": ReplyParameters(message_id=anchor.id),
            "message_thread_id": anchor.message_thread_id,
        }

    def _note_flood(self):
        """Telegram complained, so widen the gap we leave between files."""
        self._pace = min(self._MAX_PACE, self._pace * 2 or 0.5)
        self._calm = 0

    async def _pace_next_file(self):
        """Wait between two files, but only as long as telegram asked for.

        A flat second per file used to be paid unconditionally: invisible next
        to one big upload, and pure overhead across a few hundred small ones. So
        start with no gap and let a FloodWait be what introduces one, then let
        it decay once telegram stops complaining.
        """
        if not self._pace:
            return
        await sleep(self._pace)
        self._calm += 1
        if self._calm >= self._CALM_FILES:
            self._calm = 0
            self._pace = self._pace / 2 if self._pace > 0.5 else 0.0

    async def _wait_flood(self, func, *args, **kwargs):
        """Run a telegram call, waiting out any flood limit instead of failing.

        Telegram answers with FloodWait on almost any call once the account is
        rate limited, including the very first message of an upload. Those are
        transient, so wait the requested time and try again rather than killing
        the task. Returns None if the task gets canceled while waiting.
        """
        while True:
            if self._listener.is_cancelled:
                return None
            try:
                return await func(*args, **kwargs)
            except (FloodWait, FloodPremiumWait) as f:
                name = getattr(func, "__name__", str(func))
                LOGGER.warning(f"Rate limited on {name}: waiting {f.value}s. {f}")
                self._note_flood()
                await sleep(f.value * 1.3)

    async def _user_settings(self):
        self._media_group = self._listener.user_dict.get("MEDIA_GROUP", False) or (
            Config.MEDIA_GROUP
            if "MEDIA_GROUP" not in self._listener.user_dict
            else False
        )
        self._lprefix = self._listener.user_dict.get("LEECH_FILENAME_PREFIX") or (
            Config.LEECH_FILENAME_PREFIX
            if "LEECH_FILENAME_PREFIX" not in self._listener.user_dict
            else ""
        )
        if self._thumb != "none" and not await aiopath.exists(self._thumb):
            self._thumb = None
        self._files_links = self._listener.user_dict.get("FILES_LINKS", False) or (
            Config.FILES_LINKS
            if "FILES_LINKS" not in self._listener.user_dict
            else False
        )

    async def _msg_to_reply(self):
        if self._listener.up_dest:
            msg = (
                self._listener.message.link
                if self._listener.is_super_chat
                else self._listener.cmd_text.lstrip("/")
            )
            try:
                if self._user_session:
                    self._sent_msg = await self._wait_flood(
                        TgClient.user.send_message,
                        chat_id=self._listener.up_dest,
                        text=msg,
                        message_thread_id=self._listener.chat_thread_id,
                        disable_notification=True,
                    )
                else:
                    self._sent_msg = await self._wait_flood(
                        self._listener.client.send_message,
                        chat_id=self._listener.up_dest,
                        text=msg,
                        message_thread_id=self._listener.chat_thread_id,
                        disable_notification=True,
                    )
                    if self._sent_msg is not None:
                        self._is_private = self._sent_msg.chat.type.name == "PRIVATE"
                if self._sent_msg is None:
                    # only reachable when the task was canceled mid wait
                    return False
            except Exception as e:
                await self._listener.on_upload_error(str(e))
                return False
            finally:
                self._base_msg = self._sent_msg
        elif self._user_session:
            self._sent_msg = await self._wait_flood(
                TgClient.user.get_messages,
                chat_id=self._listener.message.chat.id,
                message_ids=self._listener.cmd_msg_id,
            )
            if self._sent_msg is None:
                if self._listener.is_cancelled:
                    return False
                self._sent_msg = await self._wait_flood(
                    TgClient.user.send_message,
                    chat_id=self._listener.message.chat.id,
                    text="Deleted Cmd Message! Don't delete the cmd message again!",
                    disable_notification=True,
                )
                if self._sent_msg is None:
                    return False
        else:
            self._sent_msg = self._listener.message
        return True

    async def _prepare_file(self, file_, dirpath):
        if self._lprefix:
            cap_mono = f"{self._lprefix} <code>{file_}</code>"
            self._lprefix = re_sub("<.*?>", "", self._lprefix)
            new_path = ospath.join(dirpath, f"{self._lprefix} {file_}")
            await rename(self._up_path, new_path)
            self._up_path = new_path
        else:
            cap_mono = f"<code>{file_}</code>"
        if len(file_) > 60:
            if is_archive(file_):
                name = get_base_name(file_)
                ext = file_.split(name, 1)[1]
            elif match := re_match(r".+(?=\..+\.0*\d+$)|.+(?=\.part\d+\..+$)", file_):
                name = match.group(0)
                ext = file_.split(name, 1)[1]
            elif len(fsplit := ospath.splitext(file_)) > 1:
                name = fsplit[0]
                ext = fsplit[1]
            else:
                name = file_
                ext = ""
            extn = len(ext)
            remain = 60 - extn
            name = name[:remain]
            new_path = ospath.join(dirpath, f"{name}{ext}")
            await rename(self._up_path, new_path)
            self._up_path = new_path
        return cap_mono

    def _get_input_media(self, subkey, key):
        rlist = []
        for msg in self._media_dict[key][subkey]:
            if key == "videos":
                input_media = InputMediaVideo(
                    media=msg.video.file_id, caption=msg.caption
                )
            else:
                input_media = InputMediaDocument(
                    media=msg.document.file_id, caption=msg.caption
                )
            rlist.append(input_media)
        return rlist

    async def _send_screenshots(self, dirpath, outputs):
        inputs = [
            InputMediaPhoto(ospath.join(dirpath, p), p.rsplit("/", 1)[-1])
            for p in outputs
        ]
        for i in range(0, len(inputs), 10):
            batch = inputs[i : i + 10]
            sent = await self._wait_flood(
                self._group_client.send_media_group,
                **self._reply_args(),
                media=batch,
                disable_notification=True,
            )
            if sent is None:
                return
            self._sent_msg = sent[-1]

    @property
    def _group_client(self):
        if self._listener.hybrid_leech or not self._user_session:
            return self._listener.client
        return TgClient.user

    async def _get_message(self, chat_id, message_id):
        return await self._wait_flood(
            self._group_client.get_messages,
            chat_id=chat_id,
            message_ids=message_id,
        )

    async def _copy_group_to_clone_dumps(self, from_chat_id, message_id):
        """Copy a whole album to the clone dump chats.

        ``copy_media_group`` takes no ``message_thread_id``, so topics are
        targeted by replying inside them, falling back to the topic root.
        """
        for ch, ch_data in list(self._listener.clone_dump_chats.items()):
            try:
                res = await self._wait_flood(
                    TgClient.bot.copy_media_group,
                    chat_id=ch,
                    from_chat_id=from_chat_id,
                    message_id=message_id,
                    disable_notification=True,
                    reply_to_message_id=ch_data["last_sent_msg"]
                    or ch_data["thread_id"],
                )
                if res is None:
                    return
                self._listener.clone_dump_chats[ch]["last_sent_msg"] = res[-1].id
            except Exception as e:
                LOGGER.error(
                    f"Can't forward message to clone dump chat: {ch}. Error: {e}"
                )

    async def _send_album(self):
        """Group the pending photos/videos of this task into one album."""
        msgs = self._album_msgs
        self._album_msgs = []
        if len(msgs) < 2:
            return
        media = []
        for index, msg in enumerate(msgs):
            msgs[index] = msg = await self._get_message(msg[0], msg[1])
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
            return
        return await self._send_group(msgs, media)

    async def _send_group(self, msgs, media):
        """Send *msgs* as one album and retire the originals.

        What differs between an album and a group of split parts is how *media*
        was built; what happens to the messages afterwards is the same, so both
        callers land here. Returns the sent list, or None when the send did not
        go through -- the caller must not treat a None as a delivered group.
        """
        msgs_list = await self._wait_flood(
            self._group_client.send_media_group,
            chat_id=msgs[0].chat.id,
            media=media,
            reply_to_message_id=msgs[0].reply_to_message_id,
            disable_notification=True,
        )
        if msgs_list is None:
            return None
        for msg in msgs:
            if msg.link in self._msgs_dict:
                del self._msgs_dict[msg.link]
            await delete_message(msg)
        if self._files_links and (
            self._listener.is_super_chat or self._listener.up_dest
        ):
            for m in msgs_list:
                self._msgs_dict[m.link] = m.caption
        await self._copy_group_to_clone_dumps(msgs_list[-1].chat.id, msgs_list[-1].id)
        self._sent_msg = msgs_list[-1]
        if self._base_msg:
            await delete_message(self._base_msg)
            self._base_msg = None
        return msgs_list

    async def _send_media_group(self, subkey, key, msgs):
        for index, msg in enumerate(msgs):
            msgs[index] = await self._get_message(msg[0], msg[1])
        media = self._get_input_media(subkey, key)
        if await self._send_group(msgs, media) is None:
            return
        del self._media_dict[key][subkey]

    async def _flush_media_groups(self, where=""):
        """Send every media bucket that has more than one part waiting.

        *where* labels the log line and, by being set at all, says failures are
        to be swallowed: the end-of-task flush has nothing left to abort, while
        the mid-upload flush wants the error to reach the per-file handler.
        """
        for key, value in list(self._media_dict.items()):
            for subkey, msgs in list(value.items()):
                if len(msgs) <= 1:
                    continue
                if not where:
                    await self._send_media_group(subkey, key, msgs)
                    continue
                try:
                    await self._send_media_group(subkey, key, msgs)
                except Exception as e:
                    LOGGER.info(
                        f"While sending media group at the end of {where}. Error: {e}"
                    )

    async def _upload_one(self, file_, dirpath, f_path):
        """Upload the file at ``self._up_path`` and delete it once it is sent.

        Cancellation is reported through ``self._listener.is_cancelled`` instead
        of a return value: every caller has to consult it anyway to decide
        whether to abandon the rest of the batch. A file that is skipped -- zero
        size, canceled, or canceled after an error -- is left on disk, which is
        what both callers did before this was one method.
        """
        try:
            f_size = await aiopath.getsize(self._up_path)
            self._total_files += 1
            if f_size == 0:
                LOGGER.error(
                    f"{self._up_path} size is zero, "
                    "telegram don't upload zero size files"
                )
                self._corrupted += 1
                return
            if self._listener.is_cancelled:
                return
            cap_mono = await self._prepare_file(file_, dirpath)
            if self._last_msg_in_group:
                group_lists = [
                    x for v in self._media_dict.values() for x in v.keys()
                ]
                match = re_match(SPLIT_NAME_RE, f_path)
                if not match or match and match.group(0) not in group_lists:
                    await self._flush_media_groups()
            if self._listener.hybrid_leech and self._listener.user_transmission:
                # only picks the client for this file; the anchor is
                # addressed by id, so nothing has to be re-fetched
                self._user_session = f_size > 2097152000
            self._last_msg_in_group = False
            self._last_uploaded = 0
            await self._upload_file(cap_mono, file_, f_path)
            if self._listener.is_cancelled:
                return
            if (
                self._files_links
                and (self._listener.is_super_chat or self._listener.up_dest)
                and not self._is_private
            ):
                self._msgs_dict[self._sent_msg.link] = file_
            await self._pace_next_file()
        except Exception as err:
            if isinstance(err, RetryError):
                LOGGER.info(f"Total Attempts: {err.last_attempt.attempt_number}")
                err = err.last_attempt.exception()
            LOGGER.error(f"{err}. Path: {self._up_path}")
            self._error = str(err)
            self._corrupted += 1
            if self._listener.is_cancelled:
                return
        if not self._listener.is_cancelled and await aiopath.exists(self._up_path):
            await remove(self._up_path)

    async def _finish(self, stream=False):
        """Flush what is still buffered and report the outcome of the task."""
        where = "stream task" if stream else "task"
        try:
            await self._send_album()
        except Exception as e:
            LOGGER.info(f"While sending album at the end of {where}. Error: {e}")
        await self._flush_media_groups(where)
        if self._base_msg:
            await delete_message(self._base_msg)
            self._base_msg = None
        if self._listener.is_cancelled:
            return
        if self._total_files == 0:
            await self._listener.on_upload_error(
                "No files to upload. In case you have filled "
                "EXCLUDED/INCLUDED EXTENSIONS, then check if all files "
                "have those extensions or not."
            )
            return
        if self._total_files <= self._corrupted:
            await self._listener.on_upload_error(
                f"Files Corrupted or unable to upload. {self._error or 'Check logs!'}"
            )
            return
        done = "Stream Leech Completed" if stream else "Leech Completed"
        LOGGER.info(f"{done}: {self._listener.name}")
        await self._listener.on_upload_complete(
            None, self._msgs_dict, self._total_files, self._corrupted
        )

    async def upload(self):
        await self._user_settings()
        res = await self._msg_to_reply()
        if not res:
            return
        walk_result = await sync_to_async(lambda: list(walk(self._path)))
        for dirpath, _, files in natsorted(walk_result):
            if dirpath.strip().endswith("/yt-dlp-thumb"):
                continue
            if dirpath.strip().endswith("_mltbss"):
                await self._send_album()
                await self._send_screenshots(dirpath, files)
                await rmtree(dirpath, ignore_errors=True)
                continue
            for file_ in natsorted(files):
                self._error = ""
                self._up_path = f_path = ospath.join(dirpath, file_)
                if not await aiopath.exists(self._up_path):
                    if intervals["stopAll"]:
                        return
                    LOGGER.error(f"{self._up_path} not exists! Continue uploading!")
                    continue
                await self._upload_one(file_, dirpath, f_path)
                if self._listener.is_cancelled:
                    return
        await self._finish()

    async def _resolve_thumb(self, file, thumb, is_video, is_audio, is_image):
        """Pick the thumbnail for this file, or None to let the sender make one.

        yt-dlp drops a thumbnail next to the media it downloaded, so prefer
        that over anything we could generate. Audio has no frame to grab, so it
        falls back to the cover art embedded in the file.
        """
        if is_image or thumb is not None:
            return thumb
        file_name = ospath.splitext(file)[0]
        thumb_path = f"{self._path}/yt-dlp-thumb/{file_name}.jpg"
        if await aiopath.isfile(thumb_path):
            return thumb_path
        beside_media = thumb_path.replace("/yt-dlp-thumb", "")
        if await aiopath.isfile(beside_media):
            return beside_media
        if is_audio and not is_video:
            return await get_audio_thumbnail(self._up_path)
        return thumb

    @asynccontextmanager
    async def _temp_thumb(self, attempt):
        """Drop a thumbnail we generated ourselves once the send is over.

        A user thumbnail (``self._thumb``) is kept for the whole task, so only
        clean up when there is none and the sender produced one. A send that
        cancellation aborted keeps its thumbnail: the old code returned early
        on that path, before reaching any cleanup.
        """
        try:
            yield
        finally:
            if (
                not attempt.aborted
                and self._thumb is None
                and attempt.thumb is not None
                and await aiopath.exists(attempt.thumb)
            ):
                await remove(attempt.thumb)

    async def _send_as_document(self, cap_mono, attempt):
        if attempt.is_video and attempt.thumb is None:
            attempt.thumb = await get_video_thumbnail(self._up_path, None)
        if self._listener.is_cancelled:
            return False
        if attempt.thumb == "none":
            attempt.thumb = None
        await self._send_album()
        self._sent_msg = await self._send_client.send_document(
            **self._reply_args(),
            document=self._up_path,
            thumb=attempt.thumb,
            caption=cap_mono,
            force_document=True,
            disable_notification=True,
            progress=self._upload_progress,
        )
        return True

    async def _send_as_video(self, cap_mono, attempt):
        duration = (await get_media_info(self._up_path))[0]
        if attempt.thumb is None and self._listener.thumbnail_layout:
            attempt.thumb = await get_multiple_frames_thumbnail(
                self._up_path,
                self._listener.thumbnail_layout,
                self._listener.screen_shots,
            )
        if attempt.thumb is None:
            attempt.thumb = await get_video_thumbnail(self._up_path, duration)
        if attempt.thumb is not None and attempt.thumb != "none":
            with Image.open(attempt.thumb) as img:
                width, height = img.size
        else:
            width = 480
            height = 320
        if self._listener.is_cancelled:
            return False
        if attempt.thumb == "none":
            attempt.thumb = None
        self._sent_msg = await self._send_client.send_video(
            **self._reply_args(),
            video=self._up_path,
            caption=cap_mono,
            duration=duration,
            width=width,
            height=height,
            thumb=attempt.thumb,
            supports_streaming=True,
            disable_notification=True,
            progress=self._upload_progress,
        )
        return True

    async def _send_as_audio(self, cap_mono, attempt):
        duration, artist, title = await get_media_info(self._up_path)
        if self._listener.is_cancelled:
            return False
        if attempt.thumb == "none":
            attempt.thumb = None
        await self._send_album()
        self._sent_msg = await self._send_client.send_audio(
            **self._reply_args(),
            audio=self._up_path,
            caption=cap_mono,
            duration=duration,
            performer=artist,
            title=title,
            thumb=attempt.thumb,
            disable_notification=True,
            progress=self._upload_progress,
        )
        return True

    async def _send_as_photo(self, cap_mono, attempt):
        if self._listener.is_cancelled:
            return False
        self._sent_msg = await self._send_client.send_photo(
            **self._reply_args(),
            photo=self._up_path,
            caption=cap_mono,
            disable_notification=True,
            progress=self._upload_progress,
        )
        return True

    _SENDERS = {
        "documents": _send_as_document,
        "videos": _send_as_video,
        "audios": _send_as_audio,
        "photos": _send_as_photo,
    }

    def _pick_key(self, force_document, is_video, is_audio, is_image):
        """Pick the media bucket telegram should receive this file as."""
        if (
            self._listener.as_doc
            or force_document
            or (not is_video and not is_audio and not is_image)
        ):
            return "documents"
        if is_video:
            return "videos"
        if is_audio:
            return "audios"
        return "photos"

    async def _queue_in_group(self, key, pname):
        """Hold a split part until its group is full, then send it as one."""
        group = self._media_dict[key]
        msgs = group.setdefault(pname, [])
        msgs.append([self._sent_msg.chat.id, self._sent_msg.id])
        if len(msgs) == 10:
            await self._send_media_group(pname, key, msgs)
        else:
            self._last_msg_in_group = True

    async def _track_media_group(self, o_path, attempt):
        """File the message just sent into its split group or into the album.

        Parts of one split file belong together, so they are keyed by the stem
        they share. Everything else goes to the running album.
        """
        if self._listener.is_cancelled:
            return
        if self._sent_msg.photo or self._sent_msg.video:
            match = re_match(SPLIT_NAME_RE, o_path)
            if match and self._media_group and self._sent_msg.video:
                attempt.key = "videos"
                await self._queue_in_group("videos", match.group(0))
            elif self._media_group:
                self._album_msgs.append([self._sent_msg.chat.id, self._sent_msg.id])
                if len(self._album_msgs) == 10:
                    await self._send_album()
        elif self._media_group and self._sent_msg.document:
            attempt.key = "documents"
            if match := re_match(SPLIT_NAME_RE, o_path):
                await self._queue_in_group("documents", match.group(0))

    async def _send_one(self, cap_mono, file, o_path, force_document, attempt):
        """Send one file. Returns False if cancellation cut the send short."""
        is_video, is_audio, is_image = await get_document_type(self._up_path)
        attempt.is_video = is_video
        attempt.thumb = await self._resolve_thumb(
            file, attempt.thumb, is_video, is_audio, is_image
        )
        attempt.key = self._pick_key(force_document, is_video, is_audio, is_image)
        if not await self._SENDERS[attempt.key](self, cap_mono, attempt):
            attempt.aborted = True
            return False
        await self._track_media_group(o_path, attempt)
        return True

    @retry(
        wait=wait_exponential(multiplier=2, min=4, max=8),
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(Exception),
    )
    async def _upload_file(self, cap_mono, file, o_path, force_document=False):
        if (
            self._thumb is not None
            and not await aiopath.exists(self._thumb)
            and self._thumb != "none"
        ):
            self._thumb = None
        attempt = _Attempt(self._thumb)
        try:
            async with self._temp_thumb(attempt):
                # The flood wait is handled in here so the thumbnail outlives
                # the sleep, as it did when cleanup was written out inline.
                try:
                    sent = await self._send_one(
                        cap_mono, file, o_path, force_document, attempt
                    )
                except (FloodWait, FloodPremiumWait) as f:
                    LOGGER.warning(str(f))
                    self._note_flood()
                    await sleep(f.value * 1.3)
                    raise
        except (FloodWait, FloodPremiumWait):
            return await self._upload_file(cap_mono, file, o_path)
        except Exception as err:
            err_type = "RPCError: " if isinstance(err, RPCError) else ""
            LOGGER.error(f"{err_type}{err}. Path: {self._up_path}")
            # No bucket yet means the probe or the thumbnail failed, not the
            # send — resending as a document would just fail the same way.
            retryable = attempt.key is not None and attempt.key != "documents"
            if isinstance(err, BadRequest) and retryable:
                LOGGER.error(f"Retrying As Document. Path: {self._up_path}")
                return await self._upload_file(cap_mono, file, o_path, True)
            raise err
        if not sent:
            return
        if self._base_msg and not self._last_msg_in_group and not self._album_msgs:
            await delete_message(self._base_msg)
            self._base_msg = None

    @property
    def speed(self):
        try:
            return self._processed_bytes / (time() - self._start_time)
        except Exception:
            return 0

    @property
    def processed_bytes(self):
        return self._processed_bytes

    async def cancel_task(self):
        self._listener.is_cancelled = True
        LOGGER.info(f"Cancelling Upload: {self._listener.name}")
        await self._listener.on_upload_error("your upload has been stopped!")

    async def init_stream(self):
        """Initialize uploader for stream mode (one file at a time)."""
        await self._user_settings()
        return await self._msg_to_reply()

    async def upload_single(self, file_path):
        """Upload a single file. Call init_stream() once before first use."""
        if self._listener.is_cancelled:
            return
        file_ = ospath.basename(file_path)
        dirpath = ospath.dirname(file_path)
        self._error = ""
        self._up_path = f_path = file_path
        if not await aiopath.exists(self._up_path):
            LOGGER.error(f"{self._up_path} not exists! Skipping.")
            return
        await self._upload_one(file_, dirpath, f_path)

    async def finalize_stream(self):
        """Flush pending albums and report completion after stream uploads."""
        await self._finish(stream=True)
