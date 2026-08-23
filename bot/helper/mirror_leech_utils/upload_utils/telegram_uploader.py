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
from pyrogram.types import InputMediaPhoto, ReplyParameters
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
from .flood_pacer import FLOOD_SLACK, FloodPacer
from .media_group_batcher import MediaGroupBatcher

LOGGER = getLogger(__name__)


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
        self._up_path = ""
        self._lprefix = ""
        self._is_private = False
        self._sent_msg = None
        self._user_session = self._listener.user_transmission
        self._error = ""
        self._base_msg = None
        self._files_links = False
        self._pacer = FloodPacer(lambda: self._listener.is_cancelled)
        self._batcher = MediaGroupBatcher(self)
        # Messages sent but not yet copied anywhere. Only kept for a task using
        # a copy preset; see `_copy_uncopied_to_clone_dumps`.
        self._uncopied = []

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

    async def _user_settings(self):
        self._batcher.enabled = self._listener.user_dict.get("MEDIA_GROUP", False) or (
            Config.MEDIA_GROUP
            if "MEDIA_GROUP" not in self._listener.user_dict
            else False
        )
        # A copy preset delivers albums, so it decides the grouping rather than
        # the user's standing preference: with grouping off there is no album to
        # copy, and the whole point of the preset is that its chats get one.
        if self._listener.copy_preset:
            self._batcher.enabled = True
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
                    self._sent_msg = await self._pacer.guard(
                        TgClient.user.send_message,
                        chat_id=self._listener.up_dest,
                        text=msg,
                        message_thread_id=self._listener.chat_thread_id,
                        disable_notification=True,
                    )
                else:
                    self._sent_msg = await self._pacer.guard(
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
            self._sent_msg = await self._pacer.guard(
                TgClient.user.get_messages,
                chat_id=self._listener.message.chat.id,
                message_ids=self._listener.cmd_msg_id,
            )
            if self._sent_msg is None:
                if self._listener.is_cancelled:
                    return False
                self._sent_msg = await self._pacer.guard(
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

    async def _send_screenshots(self, dirpath, outputs):
        inputs = [
            InputMediaPhoto(ospath.join(dirpath, p), p.rsplit("/", 1)[-1])
            for p in outputs
        ]
        for i in range(0, len(inputs), 10):
            batch = inputs[i : i + 10]
            sent = await self._pacer.guard(
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

    # --- what the media group batcher asks of the uploader ---

    @property
    def anchor(self):
        """The message the next send replies under."""
        return self._sent_msg

    @property
    def is_cancelled(self):
        return self._listener.is_cancelled

    async def resolve_message(self, chat_id, message_id):
        """Fetch a sent message back, for the file_id an album has to reuse."""
        return await self._pacer.guard(
            self._group_client.get_messages,
            chat_id=chat_id,
            message_ids=message_id,
        )

    async def send_group(self, chat_id, media, reply_to_message_id):
        """Send one album. None means it never reached telegram."""
        return await self._pacer.guard(
            self._group_client.send_media_group,
            chat_id=chat_id,
            media=media,
            reply_to_message_id=reply_to_message_id,
            disable_notification=True,
        )

    async def retire_group(self, originals, sent):
        """Book an album that went out and dispose of what it replaced.

        The album becomes the anchor: the messages it absorbed are deleted, so
        anything still replying to one of them would have nothing to reply to.
        """
        # The album is what gets copied, and the messages it carried are about
        # to be deleted -- there is nothing left to copy one at a time.
        carried = {(msg.chat.id, msg.id) for msg in originals}
        self._uncopied = [msg for msg in self._uncopied if msg not in carried]
        for msg in originals:
            if msg.link in self._msgs_dict:
                del self._msgs_dict[msg.link]
            await delete_message(msg)
        if self._files_links and (
            self._listener.is_super_chat or self._listener.up_dest
        ):
            for msg in sent:
                self._msgs_dict[msg.link] = msg.caption
        await self._copy_to_clone_dumps(
            TgClient.bot.copy_media_group, sent[-1].chat.id, sent[-1].id
        )
        self._sent_msg = sent[-1]
        if self._base_msg:
            await delete_message(self._base_msg)
            self._base_msg = None

    async def _copy_to_clone_dumps(self, copy, from_chat_id, message_id):
        """Copy one album, or one message, to every clone dump chat.

        A topic is addressed with ``message_thread_id`` rather than by replying
        into it: the reply only lands in the right topic by inheriting it from
        the message it answers, which is one deletion away from being wrong.

        One unreachable dump chat is not the others' problem, so a failure --
        including a send that never reached telegram -- moves on to the next.
        """
        for (ch, thread_id), ch_data in list(self._listener.clone_dump_chats.items()):
            try:
                res = await self._pacer.guard(
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
                ch_data["last_sent_msg"] = (
                    res[-1].id if isinstance(res, list) else res.id
                )
            except Exception as e:
                LOGGER.error(f"Can't copy message to clone dump chat: {ch}. Error: {e}")

    async def _copy_uncopied_to_clone_dumps(self):
        """Copy whatever no album carried, one message at a time.

        Only albums are copied while the task runs, so a file that never joined
        one would otherwise not reach the dump chats at all -- a task with a
        single file, the lone part a split group of one leaves behind, an album
        telegram refused. By now there is nothing left to group it with, so it
        goes on its own.
        """
        uncopied, self._uncopied = self._uncopied, []
        for from_chat_id, message_id in uncopied:
            await self._copy_to_clone_dumps(
                TgClient.bot.copy_message, from_chat_id, message_id
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
            await self._batcher.release_unless_continued(f_path)
            if self._listener.hybrid_leech and self._listener.user_transmission:
                # only picks the client for this file; the anchor is
                # addressed by id, so nothing has to be re-fetched
                self._user_session = f_size > 2097152000
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
            await self._pacer.pace()
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
            await self._batcher.send_album()
        except Exception as e:
            LOGGER.info(f"While sending album at the end of {where}. Error: {e}")
        await self._batcher.flush(where)
        if self._base_msg:
            await delete_message(self._base_msg)
            self._base_msg = None
        if self._listener.is_cancelled:
            return
        await self._copy_uncopied_to_clone_dumps()
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
                await self._batcher.send_album()
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
        await self._batcher.send_album()
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
        await self._batcher.send_album()
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
        # What telegram made of the file beats what the probe guessed, and it is
        # settled before the filing: filing sends groups, a group send can fail,
        # and the bucket is what that failure gets judged on.
        attempt.key = self._batcher.classify(o_path) or attempt.key
        if self._listener.copy_preset:
            # Booked before the filing, because filing can ship an album that
            # takes this message with it: ``retire_group`` strikes off whatever
            # an album carried, and what survives is copied at the end.
            self._uncopied.append((self._sent_msg.chat.id, self._sent_msg.id))
        await self._batcher.track(o_path)
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
                # The flood wait is handled in here, rather than through
                # `_pacer.guard`, so the thumbnail outlives the sleep as it did
                # when cleanup was written out inline. Only the widened gap is
                # shared with the calls that do retry in place.
                try:
                    sent = await self._send_one(
                        cap_mono, file, o_path, force_document, attempt
                    )
                except (FloodWait, FloodPremiumWait) as f:
                    LOGGER.warning(str(f))
                    self._pacer.note_flood()
                    await sleep(f.value * FLOOD_SLACK)
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
        if self._base_msg and not self._batcher.pending:
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
