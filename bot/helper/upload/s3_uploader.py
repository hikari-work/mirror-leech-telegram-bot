"""Upload a finished task's files into an S3-compatible bucket.

The one uploader that does not talk to telegram. A task bound for a bucket is
meant to be fetched with a plain HTTP GET, so objects are keyed one folder per
task -- named after the task id -- and the completion message carries a single
link to that folder rather than a message link per file.

``boto3`` is imported inside :func:`_make_client` instead of at module level.
Every module under ``bot/`` is imported by the test suite's import sweep and by
the bot's own boot, and neither should be made to depend on the object store:
only a task actually headed for a bucket needs it.
"""

from __future__ import annotations

from os import path as ospath
from os import walk
from threading import Lock
from time import time
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from aiofiles.os import path as aiopath
from natsort import natsorted

from ... import LOGGER
from ...core.config_manager import Config
from ..util.bot_utils import sync_to_async

if TYPE_CHECKING:
    from ..listeners.task_listener import TaskListener


NO_FILES_ERROR = (
    "No files to upload. In case you have filled EXCLUDED/INCLUDED EXTENSIONS, "
    "then check if all files have those extensions or not."
)

_MULTIPART_THRESHOLD = 8 * 1024 * 1024
_MULTIPART_CHUNK_SIZE = 8 * 1024 * 1024
"""Every part but the last must be at least 5 MiB for the service to accept it;
8 MiB clears that by a wide margin and is boto3's own default size besides."""

_SKIPPED_DIRS = ("yt-dlp-thumb", "_mltbss")
"""Directories that exist for telegram rather than for the bucket: yt-dlp's
thumbnail scratch folder, which ``TelegramUploader.upload`` skips too, and the
screenshot staging folder that uploader reads and then deletes. A bucket-bound
task takes no screenshots, so the second should never appear -- skipping it
means a stray one is never stored silently.
"""


def object_prefix(listener: TaskListener) -> str:
    """The part of every object key of *listener* that is not the file path.

    The task id, under the optional configured folder: ``"10032"``, or
    ``"mirror/10032"`` when ``S3_KEY_PREFIX`` is ``"mirror"``. Slashes are
    stripped here as well as by the config loader, because a ``/bsetting`` write
    goes through ``Config.set``, which never reaches the normalising path.
    """
    prefix = str(Config.S3_KEY_PREFIX or "").strip("/")
    return f"{prefix}/{listener.mid}" if prefix else str(listener.mid)


def browse_url(listener: TaskListener) -> str:
    """The one link a completed task reports: its folder in the bucket.

    A presigned URL is not an option and could not be one: S3 signs a single
    object, and there is no way to sign "list this folder". So the link points
    at the operator's own bucket browser, which holds the credentials and does
    the listing for whoever follows it.

    ``safe="/"`` happens to be what ``quote`` defaults to, and it is spelled out
    because it is load-bearing rather than incidental: quoting the folder
    separator would turn ``10032/`` into ``10032%2F``, and an app reading
    ``prefix`` as a path would list nothing. The bucket name needs no such
    argument -- a slash in it is not a separator anywhere.
    """
    base = str(Config.S3_BROWSER_URL or "").rstrip("/")
    prefix = quote(object_prefix(listener) + "/", safe="/")
    return f"{base}/?bucket={quote(Config.S3_BUCKET)}&prefix={prefix}"


def s3_config_error() -> str:
    """Why a bucket-bound task cannot run, or ``""`` when it can.

    The five settings a task needs for its files to be both stored and
    reachable. Checked before the download rather than after it: failing an
    hour of transfer because a secret was unset is the failure this exists to
    prevent.
    """
    missing = [
        name
        for name in (
            "S3_ENDPOINT_URL",
            "S3_ACCESS_KEY_ID",
            "S3_SECRET_ACCESS_KEY",
            "S3_BUCKET",
            "S3_BROWSER_URL",
        )
        if not getattr(Config, name)
    ]
    if not missing:
        return ""
    return f"S3 is not configured: set {', '.join(missing)} in config.py"


def _make_client() -> Any:
    """An S3 client pointed at the configured bucket, in the dialect R2 speaks.

    The checksum settings are load-bearing rather than tuning. Since boto3 1.36,
    ``PutObject`` defaults ``request_checksum_calculation`` to "when_supported"
    and sends flexible-checksum / ``STREAMING-UNSIGNED-PAYLOAD-TRAILER`` headers
    that R2 rejects -- "x-amz-content-sha256 must be UNSIGNED-PAYLOAD", or "You
    can only specify one non-default checksum at a time". "when_required"
    restores what every earlier boto3 sent.

    The addressing style is deliberately left to botocore's "auto", which
    resolves to virtual-hosted addressing for the endpoint R2 hands out -- the
    same thing Cloudflare's own boto3 example relies on. The bucket must not be
    folded into ``endpoint_url``: under virtual hosting that repeats it at the
    front of every key.
    """
    import boto3
    from botocore.config import Config as BotoConfig

    return boto3.session.Session().client(
        "s3",
        endpoint_url=Config.S3_ENDPOINT_URL,
        region_name=Config.S3_REGION or "auto",
        aws_access_key_id=Config.S3_ACCESS_KEY_ID,
        aws_secret_access_key=Config.S3_SECRET_ACCESS_KEY,
        config=BotoConfig(
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def _transfer_config() -> Any:
    """How one file is divided into parts, and how many of them go at once."""
    from boto3.s3.transfer import TransferConfig

    return TransferConfig(
        multipart_threshold=_MULTIPART_THRESHOLD,
        multipart_chunksize=_MULTIPART_CHUNK_SIZE,
        max_concurrency=int(Config.S3_MULTIPART_CONCURRENCY or 1),
        use_threads=True,
    )


class _UploadCancelled(Exception):
    """Raised from the transfer callback to stop an upload that is under way."""


class _ProgressCallback:
    """boto3's per-chunk hook, feeding the status line.

    A plain callable rather than a ``boto3.s3.transfer.Callback`` subclass,
    which would drag the import up to module level for nothing: boto3 calls
    whatever it is handed as ``callback(bytes_amount)``.

    The lock is real. A multipart transfer invokes the callback from several
    part threads at once -- ``max_concurrency`` above -- and a bare ``+=``
    loses counts. It is a ``threading.Lock`` rather than an ``asyncio`` one
    because none of these calls happens on the event loop.
    """

    def __init__(self, uploader: S3Uploader) -> None:
        self._uploader = uploader

    def __call__(self, bytes_amount: int) -> None:
        self._uploader.note_progress(bytes_amount)
        if self._uploader.is_cancelled:
            raise _UploadCancelled


class S3Uploader:
    """Puts one task's files in the bucket, in one folder named after the task.

    The contract is the one ``TelegramUploader`` has: from the outside only
    ``upload``, ``processed_bytes``, ``speed`` and ``cancel_task`` are used, and
    the two listeners are reported to the same way.
    """

    def __init__(self, listener: TaskListener, path: str) -> None:
        self._listener = listener
        self._path = path
        # Where the object keys are relative to, decided in ``upload`` -- the
        # payload directory when there is one, the task directory otherwise.
        self._walk_root = path
        self._prefix = object_prefix(listener)
        self._bucket = Config.S3_BUCKET
        self._client: Any = None
        # How one file is split into parts never changes within a task, and the
        # answer is a plain object built from config -- so it is built once here
        # rather than per file in the transfer loop.
        self._transfer_config = _transfer_config()
        self._callback = _ProgressCallback(self)
        self._lock = Lock()
        self._processed_bytes = 0
        self._start_time = time()
        self._total_files = 0
        self._corrupted = 0
        self._error = ""

    @property
    def is_cancelled(self) -> bool:
        return self._listener.is_cancelled

    def note_progress(self, bytes_amount: int) -> None:
        with self._lock:
            self._processed_bytes += bytes_amount

    @property
    def processed_bytes(self) -> int:
        return self._processed_bytes

    @property
    def speed(self) -> float:
        """Bytes a second since the upload started, for the status line."""
        elapsed = time() - self._start_time
        return self._processed_bytes / elapsed if elapsed > 0 else 0

    async def upload(self) -> None:
        """Store every file of the task, then report where they went."""
        error = s3_config_error()
        if error:
            await self._listener.on_upload_error(error)
            return
        # Off the loop: this imports boto3 and builds a session that reads
        # credentials off disk, all of it synchronous, and the event loop it
        # would otherwise run on is shared by every task in the bot.
        self._client = await sync_to_async(_make_client)
        try:
            self._walk_root = self._root()
            LOGGER.info(
                f"Bucket upload: {self._listener.name}"
                f" -> {self._bucket}/{self._prefix}/"
            )
            walk_result = await sync_to_async(lambda: list(walk(self._walk_root)))
            for dirpath, _, files in natsorted(walk_result):
                if ospath.basename(dirpath) in _SKIPPED_DIRS:
                    continue
                for file_ in natsorted(files):
                    if self.is_cancelled:
                        return
                    key = f"{self._prefix}/{self._rel_key(dirpath, file_)}"
                    await self._upload_one(ospath.join(dirpath, file_), key)
                    if self.is_cancelled:
                        return
            await self._finish()
        finally:
            # A cancelled or failed task returns from inside the loop above, and
            # nothing else holds this client -- left open it keeps its
            # connections until the garbage collector gets to it.
            await self._close_client()

    async def _close_client(self) -> None:
        """Drop the bucket client's connections, tolerating a half-built one."""
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await sync_to_async(client.close)
        except Exception as e:
            LOGGER.error(f"Closing the bucket client failed: {e}")

    def _root(self) -> str:
        """The directory the object keys are relative to.

        ``up_dir`` holds the payload under a name of its own -- the download's
        -- plus, sometimes, a thumbnail scratch folder. Walking from there would
        key every file as ``10032/Big.Buck.Bunny/…``, so the task's folder would
        no longer be the one the completion message links to. The payload
        directory itself is the root when it is one; a bare file has no folder
        of its own to peel off.
        """
        payload = ospath.join(self._path, self._listener.name)
        return payload if ospath.isdir(payload) else self._path

    def _rel_key(self, dirpath: str, file_: str) -> str:
        """The object key under the task prefix, sub-directories preserved."""
        rel = ospath.relpath(ospath.join(dirpath, file_), self._walk_root)
        return rel.replace(ospath.sep, "/")

    async def _upload_one(self, file_path: str, key: str) -> None:
        """Store one file, counting a failure instead of dying on it.

        The same contract the telegram uploader keeps: one unreadable file must
        not lose the other nine hundred, and a zero-byte file is counted and
        never sent -- an object nobody can fetch has no value.
        """
        try:
            f_size = await aiopath.getsize(file_path)
            self._total_files += 1
            if f_size == 0:
                LOGGER.error(f"{file_path} size is zero, nothing to store")
                self._corrupted += 1
                return
            if self.is_cancelled:
                return
            self._error = ""
            await sync_to_async(
                self._client.upload_file,
                file_path,
                self._bucket,
                key,
                Callback=self._callback,
                Config=self._transfer_config,
            )
        except _UploadCancelled:
            # the listener knows; the caller stops walking the rest
            return
        except Exception as err:
            LOGGER.error(f"Bucket upload failed for {key}: {err}")
            self._error = str(err)
            self._corrupted += 1

    async def _finish(self) -> None:
        """Report the outcome, or the folder link when there is one."""
        if self.is_cancelled:
            return
        if self._total_files == 0:
            await self._listener.on_upload_error(NO_FILES_ERROR)
            return
        if self._total_files <= self._corrupted:
            await self._listener.on_upload_error(
                f"Files Corrupted or unable to upload. {self._error or 'Check logs!'}"
            )
            return
        LOGGER.info(f"Bucket upload completed: {self._listener.name}")
        link = browse_url(self._listener)
        await self._listener.on_upload_complete(
            link,
            {link: f"{self._prefix}/"},
            self._total_files,
            self._corrupted,
        )

    async def cancel_task(self) -> None:
        """Stop the transfer, the way every other status object can be asked to.

        ``/cancel`` reaches this through ``BaseStatus.task()``. The flag is what
        the progress callback tests to abort an upload already in flight -- a
        future on the thread pool cannot be called back once it is running.
        """
        self._listener.is_cancelled = True
        LOGGER.info(f"Cancelling Upload: {self._listener.name}")
        await self._listener.on_upload_error("your upload has been stopped!")
