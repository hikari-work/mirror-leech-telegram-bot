"""Downloading from Mega, decrypting as the bytes arrive.

Mega serves ciphertext and nothing else, so aria2 cannot be handed one of these
links - the file would land unreadable. The transfer therefore runs here: each
file is fetched over a few ranged connections, decrypted in flight, and written
straight to its final offset on disk.

AES-CTR is what makes that shape possible. Its keystream is derived from the
absolute byte offset, so any range of the file decrypts independently of the
rest, which is also why a download interrupted by a quota error can resume from
where it stopped instead of starting over.
"""

from asyncio import CancelledError, Lock, create_task, gather, sleep
from os import path as ospath
from secrets import token_urlsafe
from time import time

from aiofiles import open as aiopen
from aiofiles.os import makedirs, remove
from aiofiles.os import path as aiopath
from aiohttp import ClientSession, ClientTimeout

from .... import LOGGER, task_dict, task_dict_lock
from ....core.config_manager import Config
from ...ext_utils.aiohttp_helper import proxy_connector
from ...ext_utils.mega_client import (
    BLOCK,
    MegaApiError,
    counter_at,
    ctr_decrypt,
    file_info,
    list_folder,
    resolve_file,
    unpack_file_key,
)
from ...ext_utils.task_manager import check_running_tasks, stop_duplicate_check
from ...ext_utils.warp_utils import ensure_proxy_mode, restart_warp, warp_proxy_url
from ...telegram_helper.message_utils import send_status_message
from ..status_utils.mega_status import MegaStatus
from ..status_utils.queue_status import QueueStatus

# Read size per connection. Large enough that the AES call is not dominated by
# per-call overhead, small enough that cancelling stays responsive.
CHUNK = 1024 * 1024

# A file smaller than this is not worth splitting across connections.
MIN_SPLIT = 8 * 1024 * 1024

# CDN statuses that mean "this IP has had enough", as opposed to a broken URL.
QUOTA_STATUSES = (402, 403, 429, 509)


class _QuotaReached(Exception):
    """Mega refused because of the egress IP, not because of the request."""


class MegaDownloadHelper:
    """Drives one Mega task and reports progress to the status bar.

    Shaped like the other non-aria2 downloaders (gallery-dl, GoFile): the
    listener owns naming and completion, this owns bytes.
    """

    def __init__(self, listener):
        self._listener = listener
        self._gid = token_urlsafe(10)
        self._processed = 0
        self._lock = Lock()
        self._speed = 0
        self._last_time = 0.0
        self._last_bytes = 0
        self._proxy = ""
        self._request_proxy = None

    @property
    def processed_bytes(self):
        return self._processed

    @property
    def speed(self):
        """Sampled rather than averaged over the whole task, so the number
        tracks what the connection is doing now."""
        now = time()
        if (elapsed := now - self._last_time) > 0.5:
            self._speed = (self._processed - self._last_bytes) / elapsed
            self._last_time = now
            self._last_bytes = self._processed
        return self._speed

    def _session(self):
        connector, request_proxy = proxy_connector(self._proxy)
        self._request_proxy = request_proxy
        # No total timeout: a multi-GB file legitimately takes hours. The
        # per-read timeout is what catches a connection that has actually died.
        return ClientSession(
            connector=connector,
            timeout=ClientTimeout(total=None, sock_read=120, sock_connect=60),
        )

    async def _segment(self, session, url, path, aes_key, nonce, start, end, done):
        """Fetch [start, end] of the ciphertext, decrypt it, write it at its
        own offset. `done` carries how much of this segment already landed on
        a previous attempt, so a retry resumes instead of refetching.

        Each segment opens the file separately and seeks: they write disjoint
        ranges, so there is nothing to coordinate beyond the progress counter.
        """
        at = start + done[0]
        if at > end:
            return

        # CTR can only start on a block boundary, so an unaligned resume point
        # is backed up to one and the few replayed bytes are dropped below.
        aligned = (at // BLOCK) * BLOCK
        skip = at - aligned

        headers = {"Range": f"bytes={aligned}-{end}"}
        async with session.get(url, headers=headers, proxy=self._request_proxy) as resp:
            if resp.status in QUOTA_STATUSES:
                raise _QuotaReached(f"the CDN answered HTTP {resp.status}")
            if resp.status not in (200, 206):
                raise ConnectionError(f"the CDN answered HTTP {resp.status}")

            async with aiopen(path, "r+b") as f:
                await f.seek(at)
                offset = aligned

                async for chunk in resp.content.iter_chunked(CHUNK):
                    if self._listener.is_cancelled:
                        return

                    plain = ctr_decrypt(aes_key, counter_at(nonce, offset), chunk)
                    offset += len(chunk)

                    if skip:
                        # Guarded against a chunk shorter than the replayed
                        # prefix, which a tiny final chunk can be.
                        if len(plain) <= skip:
                            skip -= len(plain)
                            continue
                        plain, skip = plain[skip:], 0

                    await f.write(plain)
                    done[0] += len(plain)
                    async with self._lock:
                        self._processed += len(plain)

        if not self._listener.is_cancelled and done[0] < end - start + 1:
            # A stream that stops early otherwise looks like success and leaves
            # a hole of zero bytes in the middle of the file.
            raise ConnectionError(
                f"the CDN closed the connection after {done[0]} of "
                f"{end - start + 1} bytes"
            )

    def _spans(self, size):
        """Split a file into contiguous ranges, one per connection.

        Boundaries are block-aligned so no cipher block is shared between two
        connections - each can then derive its own counter and they never need
        to agree on anything.
        """
        want = max(1, int(Config.MEGA_CONNECTIONS or 1))
        if size < MIN_SPLIT or want == 1:
            return [(0, size - 1)]

        count = min(want, max(1, size // MIN_SPLIT))
        step = ((size // count) // BLOCK) * BLOCK or BLOCK

        spans = []
        for index in range(count):
            start = index * step
            end = size - 1 if index == count - 1 else start + step - 1
            if start <= end:
                spans.append((start, end))
        return spans

    async def _download_file(self, session, item, folder, dest):
        """One file, retrying across egress IPs when Mega meters us.

        The URL is re-resolved on each attempt because Mega binds it to the
        requesting IP - after a rotation the old one is dead anyway.
        """
        await makedirs(ospath.dirname(dest), exist_ok=True)
        aes_key, nonce, _ = unpack_file_key(item["key"])
        progress = None
        # Budgeted per file: a folder share is thousands of files, and one
        # spent budget must not condemn every file that comes after it.
        restarts = 0

        while True:
            try:
                url, size = await resolve_file(session, item["handle"], folder)
                size = size or item["size"]

                if progress is None:
                    # Preallocated so every connection can seek to its own
                    # offset immediately; sparse until each range is filled.
                    async with aiopen(dest, "wb") as f:
                        await f.truncate(size)
                    progress = [[0] for _ in self._spans(size)]

                spans = self._spans(size)
                tasks = [
                    create_task(
                        self._segment(
                            session, url, dest, aes_key, nonce, start, end, done
                        )
                    )
                    for (start, end), done in zip(spans, progress)
                ]
                try:
                    await gather(*tasks)
                except BaseException:
                    # gather propagates the first failure but leaves the other
                    # connections reading into a URL that is about to be
                    # replaced, so they are stopped before the retry.
                    for task in tasks:
                        task.cancel()
                    await gather(*tasks, return_exceptions=True)
                    raise
                return not self._listener.is_cancelled

            except (_QuotaReached, MegaApiError) as e:
                if isinstance(e, MegaApiError) and not e.is_quota:
                    raise
                if not self._proxy:
                    # Nothing is routed through WARP, so bouncing the tunnel
                    # would not change the address this request comes from -
                    # it would only stall every file for the reconnect timeout.
                    raise
                if restarts >= int(Config.MEGA_MAX_RESTARTS or 0):
                    raise
                restarts += 1
                LOGGER.info(
                    f"Mega: {e} on {item['name']}, rotating the egress IP "
                    f"[{restarts}/{Config.MEGA_MAX_RESTARTS}]"
                )
                # Progress is deliberately kept: the bytes already written are
                # valid plaintext, so the retry resumes rather than restarts.
                if not await restart_warp():
                    # The tunnel came back up but the proxy carries no traffic,
                    # or the tunnel never reconnected. Either way rotation is
                    # off the table, so fall back to a direct download for this
                    # file and every file after it.
                    LOGGER.warning(
                        "Mega: WARP restart failed, falling back to direct download - "
                        "further quota errors will not be recoverable"
                    )
                    self._proxy = ""
                    self._request_proxy = None
                await sleep(3)
                # The CDN URL is one-shot, IP-locked, and dies after rotation:
                # reuse returns 403 with no body. It must be re-resolved from
                # the new egress before the retry can proceed.
                continue

    async def _register(self, from_queue=False):
        async with task_dict_lock:
            task_dict[self._listener.mid] = MegaStatus(self._listener, self, self._gid)
        if not from_queue:
            await self._listener.on_download_start()
            if self._listener.multi <= 1 and not self._listener.is_rss:
                await send_status_message(self._listener.message)

    async def _gather_files(self, session, link):
        """Resolve the link into the file list and the task's name."""
        if link["kind"] == "file":
            item = await file_info(session, link["handle"], link["key"])
            return [item], item["name"]

        listing = await list_folder(session, link["handle"], link["key"])
        return listing["files"], listing["name"]

    async def add_download(self, path):
        link = self._listener.link["mega"]

        # Proxy mode is arranged before anything is fetched, so the listing and
        # the download share one egress and rotating it affects both.
        self._proxy = warp_proxy_url()
        if self._proxy.startswith("socks5://127.0.0.1"):
            if not await ensure_proxy_mode():
                LOGGER.warning(
                    "Mega: WARP proxy is unavailable, downloading directly - "
                    "a quota error will not be recoverable"
                )
                self._proxy = ""

        try:
            async with self._session() as session:
                files, title = await self._gather_files(session, link)
        except (MegaApiError, ValueError, ConnectionError) as e:
            await self._listener.on_download_error(f"Mega: {e}")
            return

        # Known up front from the API, so the status bar shows a real total and
        # a meaningful ETA from the first second.
        self._listener.size = sum(item["size"] for item in files)
        if not self._listener.name:
            self._listener.name = title
        single = len(files) == 1 and not files[0]["path"]

        msg, button = await stop_duplicate_check(self._listener)
        if msg:
            await self._listener.on_download_error(msg, button)
            return

        add_to_queue, event = await check_running_tasks(self._listener)
        if add_to_queue:
            LOGGER.info(f"Added to Queue/Download: {self._listener.name}")
            async with task_dict_lock:
                task_dict[self._listener.mid] = QueueStatus(
                    self._listener, self._gid, "dl"
                )
            await self._listener.on_download_start()
            if self._listener.multi <= 1 and not self._listener.is_rss:
                await send_status_message(self._listener.message)
            await event.wait()
            if self._listener.is_cancelled:
                return

        await self._register(add_to_queue)
        LOGGER.info(
            f"Download from Mega: {self._listener.name} "
            f"({len(files)} file{'s' if len(files) != 1 else ''})"
        )

        await self._run(path, link, files, single)

    async def _run(self, path, link, files, single):
        """Download every file, then hand over to the listener.

        A single-file task writes straight into the task directory; a folder
        recreates its tree under one named after the share.
        """
        base = path if single else f"{path}/{self._listener.name}"
        folder = link["handle"] if link["kind"] == "folder" else None
        failed = []

        try:
            async with self._session() as session:
                for item in files:
                    if self._listener.is_cancelled:
                        return

                    name = item["name"] if not single else self._listener.name
                    dest = ospath.join(base, item["path"], name)

                    try:
                        await self._download_file(session, item, folder, dest)
                    except CancelledError:
                        raise
                    except Exception as e:
                        # One dead file should not lose a whole folder, so the
                        # rest continues and the failures are reported at the end.
                        LOGGER.error(f"Mega: {item['name']} failed: {e}")
                        failed.append(f"{item['name']} ({e})")
                        if await aiopath.exists(dest):
                            await remove(dest)
        except CancelledError:
            return
        except Exception as e:
            await self._listener.on_download_error(f"Mega: {e}")
            return

        if self._listener.is_cancelled:
            return

        if len(failed) == len(files):
            await self._listener.on_download_error(
                f"Mega: every file failed ({'; '.join(failed[:3])})"
            )
            return

        if failed:
            LOGGER.info(f"Mega: {len(failed)} of {len(files)} file(s) failed")
            # The total was computed from the listing, so it has to come back
            # down or the upload would report a size that never arrived.
            self._listener.size = self._processed

        await self._listener.on_download_complete()

    async def cancel_task(self):
        self._listener.is_cancelled = True
        LOGGER.info(f"Cancelling Download: {self._listener.name}")
        await self._listener.on_download_error("Stopped by User!")


async def add_mega_download(listener, path):
    await MegaDownloadHelper(listener).add_download(path)

