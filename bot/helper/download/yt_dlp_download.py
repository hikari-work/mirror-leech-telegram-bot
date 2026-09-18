from __future__ import annotations

from collections.abc import Callable
from logging import getLogger
from os import path as ospath, listdir
from re import search as re_search
from secrets import token_urlsafe
from typing import TYPE_CHECKING, Any
from yt_dlp import YoutubeDL
from yt_dlp.utils import YoutubeDLError


from ... import task_dict_lock, task_dict
from ..util.bot_utils import sync_to_async, async_to_sync
from ..util.resolve_gate import resolve_gate
from ..util.task_manager import check_running_tasks
from ..progress.queue_status import QueueStatus
from ..telegram.message_utils import send_status_message
from ..progress.yt_dlp_status import YtDlpStatus
from .yt_dlp_hooks import final_paths_hook

if TYPE_CHECKING:
    from ..upload.stream_uploader import StreamUploader

LOGGER = getLogger(__name__)


class MyLogger:
    def __init__(self, obj, listener):
        self._obj = obj
        self._listener = listener

    def debug(self, msg):
        # Hack to fix changing extension
        if not self._obj.is_playlist:
            if match := re_search(
                r".Merger..Merging formats into..(.*?).$", msg
            ) or re_search(r".ExtractAudio..Destination..(.*?)$", msg):
                LOGGER.info(msg)
                newname = match.group(1)
                newname = newname.rsplit("/", 1)[-1]
                self._listener.name = newname

    @staticmethod
    def warning(msg):
        LOGGER.warning(msg)

    @staticmethod
    def error(msg):
        if msg != "ERROR: Cancelling...":
            LOGGER.error(msg)


class YoutubeDLHelper:
    def __init__(self, listener):
        self._last_downloaded = 0
        self._progress = 0
        self._downloaded_bytes = 0
        self._download_speed = 0
        self._eta = "-"
        self._listener = listener
        self._gid = ""
        self._ext = ""
        self.is_playlist = False
        self.keep_thumb = False
        # What the probe extracted, so the download does not fetch the same
        # metadata over again. Set only for a single video -- see ``_download``.
        # ``Any`` because the info dict's type is yt-dlp's own and is not
        # importable; it is also mutated in place during a download, so a
        # ``Mapping`` here would claim more than holds.
        self._ie_result: Any = None
        # Streaming (``-su``): the uploader the finished files are handed to as
        # they land. None unless the flag is on -- see ``add_download``.
        self._stream: StreamUploader | None = None
        # yt-dlp options are heterogeneous by design -- flags, counts, hooks,
        # nested dicts of callables -- and the setup below adds lists and more
        # callables to it, so the values cannot be narrowed to what this literal
        # happens to hold.
        self.opts: dict[str, Any] = {
            "progress_hooks": [self._on_download_progress],
            "logger": MyLogger(self, self._listener),
            "usenetrc": True,
            "cookiefile": "cookies.txt",
            "allow_multiple_video_streams": True,
            "allow_multiple_audio_streams": True,
            "noprogress": True,
            "allow_playlist_files": True,
            "overwrites": True,
            "writethumbnail": True,
            "trim_file_name": 220,
            "fragment_retries": 10,
            "retries": 10,
            "retry_sleep_functions": {
                "http": lambda n: 3,
                "fragment": lambda n: 3,
                "file_access": lambda n: 3,
                "extractor": lambda n: 3,
            },
        }

    @property
    def download_speed(self):
        return self._download_speed

    @property
    def downloaded_bytes(self):
        return self._downloaded_bytes

    @property
    def size(self):
        return self._listener.size

    @property
    def progress(self):
        return self._progress

    @property
    def eta(self):
        return self._eta

    def _on_download_progress(self, d):
        if self._listener.is_cancelled:
            raise ValueError("Cancelling...")
        if d["status"] == "finished":
            if self.is_playlist:
                self._last_downloaded = 0
        elif d["status"] == "downloading":
            self._download_speed = d["speed"] or 0
            if self.is_playlist:
                downloadedBytes = d["downloaded_bytes"] or 0
                chunk_size = downloadedBytes - self._last_downloaded
                self._last_downloaded = downloadedBytes
                self._downloaded_bytes += chunk_size
            else:
                if d.get("total_bytes"):
                    self._listener.size = d["total_bytes"] or 0
                elif d.get("total_bytes_estimate"):
                    self._listener.size = d["total_bytes_estimate"] or 0
                self._downloaded_bytes = d["downloaded_bytes"] or 0
                self._eta = d.get("eta", "-") or "-"
            try:
                self._progress = (self._downloaded_bytes / self._listener.size) * 100
            except Exception:
                pass

    async def _on_download_start(self, from_queue=False):
        async with task_dict_lock:
            task_dict[self._listener.mid] = YtDlpStatus(self._listener, self, self._gid)
        if not from_queue:
            await self._listener.on_download_start()
            if self._listener.multi <= 1 and not self._listener.is_rss:
                await send_status_message(self._listener.message)

    def _on_download_error(self, error):
        self._listener.is_cancelled = True
        async_to_sync(self._listener.on_download_error, error)

    def _extract_meta_data(self):
        if self._listener.link.startswith(("rtmp", "mms", "rstp", "rtmps")):
            self.opts["external_downloader"] = "ffmpeg"
        # Cleared rather than left alone: this decides whether ``_download``
        # reuses a result, and a stale one from an earlier probe would be the
        # wrong video.
        self._ie_result = None
        # yt-dlp types ``params`` as a TypedDict naming every option it knows,
        # and ``self.opts`` is assembled from what the user asked for: ``-opt``
        # can set any of them, so no fixed set of keys describes it.
        with YoutubeDL(self.opts) as ydl:  # pyrefly: ignore[bad-argument-type]
            try:
                result = ydl.extract_info(self._listener.link, download=False)
                if result is None:
                    raise ValueError("Info result is None")
            except Exception as e:
                return self._on_download_error(str(e))
            if "entries" in result:
                for entry in result["entries"]:
                    if not entry:
                        continue
                    elif "filesize_approx" in entry:
                        self._listener.size += entry.get("filesize_approx", 0) or 0
                    elif "filesize" in entry:
                        self._listener.size += entry.get("filesize", 0) or 0
                    if not self._listener.name:
                        outtmpl_ = "%(series,playlist_title,channel)s%(season_number& |)s%(season_number&S|)s%(season_number|)02d.%(ext)s"
                        self._listener.name, ext = ospath.splitext(
                            ydl.prepare_filename(entry, outtmpl=outtmpl_)
                        )
                        if not self._ext:
                            self._ext = ext
            else:
                outtmpl_ = "%(title,fulltitle,alt_title)s%(season_number& |)s%(season_number&S|)s%(season_number|)02d%(episode_number&E|)s%(episode_number|)02d%(height& |)s%(height|)s%(height&p|)s%(fps|)s%(fps&fps|)s%(tbr& |)s%(tbr|)d.%(ext)s"
                realName = ydl.prepare_filename(result, outtmpl=outtmpl_)
                ext = ospath.splitext(realName)[-1]
                self._listener.name = (
                    f"{self._listener.name}{ext}" if self._listener.name else realName
                )
                if not self._ext:
                    self._ext = ext
                # Handed to the download so the link is resolved once instead
                # of twice. Safe to reuse because yt-dlp reads ``outtmpl`` when
                # it prepares the filename, not from this dict -- so the
                # template ``add_download`` sets afterwards still decides where
                # the file lands.
                self._ie_result = result

    def _stream_hook(self, stream: StreamUploader) -> Callable[[dict[str, Any]], None]:
        """Hand each finished file over, from the thread yt-dlp downloads on.

        The hook is called on that thread, so handing a path over means putting
        the upload on the event loop and waiting for it. ``submit`` answers only
        once the consumer has taken the path, and that wait is the backpressure:
        a playlist does not pull its next video down while the previous one is
        still being sent. It cannot deadlock either -- the loop this thread
        waits on is the one running the consumer, and the thread holds nothing
        the consumer needs.
        """

        def hand_over(path: str) -> None:
            async_to_sync(stream.submit, path)

        return final_paths_hook(hand_over)

    def _download(self, path):
        try:
            # Same runtime-assembled opts as in ``_extract_meta_data``.
            with YoutubeDL(self.opts) as ydl:  # pyrefly: ignore[bad-argument-type]
                try:
                    if self._ie_result is not None:
                        # The probe already resolved this link, and asking for
                        # it again would fetch the same metadata a second time
                        # -- for a video behind an HLS master playlist that is
                        # a second round of requests before a byte moves.
                        # ``process_ie_result`` is what ``download`` calls
                        # underneath, with ``download`` turned on.
                        ydl.process_ie_result(self._ie_result, download=True)
                    else:
                        ydl.download([self._listener.link])
                except YoutubeDLError as e:
                    # ``YoutubeDLError`` rather than ``DownloadError``: the
                    # exception type depends on which of the two calls above
                    # failed, and both are its subclasses. Anything else is a
                    # bug here, not a download that failed.
                    if not self._listener.is_cancelled:
                        self._on_download_error(str(e))
                    return
            # A playlist leaves its directory on disk even when a stream has
            # taken every file out of it -- the guard looks at the task's own
            # directory, and the playlist's is still in there. An empty one
            # therefore still means what it always did: nothing was downloaded.
            if self.is_playlist and (
                not ospath.exists(path) or len(listdir(path)) == 0
            ):
                self._on_download_error(
                    "No video available to download from this playlist. Check logs for more details"
                )
                return
            if self._listener.is_cancelled:
                return
            if self._stream is not None:
                # the uploader reports the task: every file is already sent
                async_to_sync(self._stream.finalize)
                return
            async_to_sync(self._listener.on_download_complete)
        except Exception:
            pass
        finally:
            # whichever way the above ended -- the guard above, a cancel, a
            # download that failed -- the uploader is stopped rather than left
            # holding a file, so the task can end
            if self._stream is not None:
                async_to_sync(self._stream.drain)
        return

    async def add_download(self, path, qual, playlist, options):
        if playlist:
            self.opts["ignoreerrors"] = True
            self.is_playlist = True

        self._gid = token_urlsafe(10)

        await self._on_download_start()

        self.opts["postprocessors"] = [
            {
                "add_chapters": True,
                "add_infojson": "if_exists",
                "add_metadata": True,
                "key": "FFmpegMetadata",
            }
        ]

        if qual.startswith("ba/b-"):
            audio_info = qual.split("-")
            qual = audio_info[0]
            audio_format = audio_info[1]
            rate = audio_info[2]
            self.opts["postprocessors"].append(
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": audio_format,
                    "preferredquality": rate,
                }
            )
            if audio_format == "vorbis":
                self._ext = ".ogg"
            elif audio_format == "alac":
                self._ext = ".m4a"
            else:
                self._ext = f".{audio_format}"

        if self._listener.thumbnail_layout:
            self.opts["writethumbnail"] = False

        if options:
            self._set_options(options)

        self.opts["format"] = qual

        # Probing the source (an HLS master playlist, a playlist page) is
        # pre-queue network work, so a bulk goes through the same gate the
        # scrapers use instead of probing a hundred links at once.
        async with resolve_gate():
            await sync_to_async(self._extract_meta_data)
        if self._listener.is_cancelled:
            return

        base_name, ext = ospath.splitext(self._listener.name)
        trim_name = self._listener.name if self.is_playlist else base_name
        if len(trim_name.encode()) > 200:
            self._listener.name = (
                self._listener.name[:200]
                if self.is_playlist
                else f"{base_name[:200]}{ext}"
            )
            base_name = ospath.splitext(self._listener.name)[0]

        start_path = path if self.keep_thumb else f"{path}/yt-dlp-thumb"
        if self.is_playlist:
            self.opts["outtmpl"] = {
                "default": f"{path}/{self._listener.name}/%(title,fulltitle,alt_title)s%(season_number& |)s%(season_number&S|)s%(season_number|)02d%(episode_number&E|)s%(episode_number|)02d%(height& |)s%(height|)s%(height&p|)s%(fps|)s%(fps&fps|)s%(tbr& |)s%(tbr|)d.%(ext)s",
                "thumbnail": f"{start_path}/%(title,fulltitle,alt_title)s%(season_number& |)s%(season_number&S|)s%(season_number|)02d%(episode_number&E|)s%(episode_number|)02d%(height& |)s%(height|)s%(height&p|)s%(fps|)s%(fps&fps|)s%(tbr& |)s%(tbr|)d.%(ext)s",
            }
        elif "download_ranges" in options:
            self.opts["outtmpl"] = {
                "default": f"{path}/{base_name}/%(section_number|)s%(section_number&.|)s%(section_title|)s%(section_title&-|)s%(title,fulltitle,alt_title)s %(section_start)s to %(section_end)s.%(ext)s",
                "thumbnail": f"{start_path}/%(section_number|)s%(section_number&.|)s%(section_title|)s%(section_title&-|)s%(title,fulltitle,alt_title)s %(section_start)s to %(section_end)s.%(ext)s",
            }
        elif any(
            key in options
            for key in [
                "writedescription",
                "writeinfojson",
                "writeannotations",
                "writedesktoplink",
                "writewebloclink",
                "writelink",
                "writeurllink",
                "writesubtitles",
                "write_all_thumbnails",
            ]
        ):
            self.opts["outtmpl"] = {
                "default": f"{path}/{base_name}/{self._listener.name}",
                "thumbnail": f"{start_path}/{base_name}.%(ext)s",
            }
        else:
            self.opts["outtmpl"] = {
                "default": f"{path}/{self._listener.name}",
                "thumbnail": f"{start_path}/{base_name}.%(ext)s",
            }

        if qual.startswith("ba/b"):
            self._listener.name = f"{base_name}{self._ext}"

        if self.opts["writethumbnail"]:
            self.opts["postprocessors"].append(
                {
                    "format": "jpg",
                    "key": "FFmpegThumbnailsConvertor",
                    "when": "before_dl",
                }
            )
        if self._ext in [
            ".mp3",
            ".mkv",
            ".mka",
            ".ogg",
            ".opus",
            ".flac",
            ".m4a",
            ".mp4",
            ".mov",
            ".m4v",
        ]:
            self.opts["postprocessors"].append(
                {
                    "already_have_thumbnail": self.opts["writethumbnail"],
                    "key": "EmbedThumbnail",
                }
            )

        add_to_queue, event = await check_running_tasks(self._listener)
        if add_to_queue:
            LOGGER.info(f"Added to Queue/Download: {self._listener.name}")
            async with task_dict_lock:
                task_dict[self._listener.mid] = QueueStatus(
                    self._listener, self._gid, "dl"
                )
            await event.wait()
            if self._listener.is_cancelled:
                return
            LOGGER.info(f"Start Queued Download from YT_DLP: {self._listener.name}")
            await self._on_download_start(True)

        if not add_to_queue:
            LOGGER.info(f"Download with YT_DLP: {self._listener.name}")

        if self._listener.stream_upload:
            # imported here: this is the download side of the package the
            # component uploads through. Built after the queue wait, so a task
            # that is still waiting for its download slot has not announced an
            # uploader yet.
            from ..upload.stream_uploader import StreamUploader

            base = f"{path}/{self._listener.name}" if self.is_playlist else path
            stream = StreamUploader(self._listener, base)
            if not await stream.start():
                return
            self._stream = stream
            # extended rather than set: ``-opt`` can name this option too, and
            # dropping a hook the user asked for would drop their work silently
            self.opts.setdefault("postprocessor_hooks", []).append(
                self._stream_hook(stream)
            )

        await sync_to_async(self._download, path)

    async def cancel_task(self):
        self._listener.is_cancelled = True
        LOGGER.info(f"Cancelling Download: {self._listener.name}")
        await self._listener.on_download_error("Stopped by User!")

    def _set_options(self, options):
        for key, value in options.items():
            if key == "postprocessors":
                if isinstance(value, list):
                    self.opts[key].extend(tuple(value))
                elif isinstance(value, dict):
                    self.opts[key].append(value)
            elif key == "download_ranges":
                if isinstance(value, list):
                    self.opts[key] = lambda info, ytdl: value
            else:
                if key == "writethumbnail" and value is True:
                    self.keep_thumb = True
                self.opts[key] = value


async def add_ytdlp_download(listener, path):
    """Take over a link the direct link generator could only resolve to a stream
    that has to be muxed rather than fetched whole, such as an HLS ladder. The
    generator hands back the descriptor as listener.link."""
    details = listener.link
    listener.link = details["link"]
    if not listener.name:
        listener.name = details.get("name") or ""
    options = {}
    # Some CDNs gate the stream on a Referer, so the headers ride with the link.
    if headers := details.get("headers"):
        options["http_headers"] = headers
    await YoutubeDLHelper(listener).add_download(
        path, details.get("format") or "bv*+ba/b", False, options
    )
