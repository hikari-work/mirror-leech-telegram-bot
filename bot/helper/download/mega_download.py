"""Downloading from Mega, decrypting as the bytes arrive.

Mega serves ciphertext and nothing else. Metadata resolution (name, size,
per-file AES key + nonce) is done by the gateway at api.piyann.me. The CDN
URL returned by the gateway is fetched through one of five Cloudflare Worker
proxies (proxy-{1-5}.vianstefani754.workers.dev) to avoid per-IP quota limits.

AES-CTR makes parallel ranged downloads and mid-file resume both possible:
the keystream for any byte offset is derived from that offset alone, so
connections never need to coordinate and an interrupted transfer can continue
from exactly where it stopped.
"""

from asyncio import CancelledError, Lock, Semaphore, create_task, gather, sleep
from os import path as ospath
from secrets import token_urlsafe
from time import time

from aiofiles import open as aiopen
from aiofiles.os import makedirs, remove
from aiofiles.os import path as aiopath
from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector

from ... import LOGGER, task_dict, task_dict_lock
from ...core.config_manager import Config
from ..net.mega_client import (
    BLOCK,
    MegaApiError,
    counter_at,
    ctr_stream,
    file_cdn,
    key_from_node,
    list_folder,
    resolve_link,
)
from ..net.proxy_pool import get_proxy_pool, proxied_url, refresh_proxy_pool
from ..progress.mega_status import MegaStatus
from ..progress.queue_status import QueueStatus
from ..telegram.message_utils import send_status_message
from ..util.resolve_gate import resolve_gate
from ..util.task_manager import check_running_tasks

# Read size per connection.
CHUNK = 1024 * 1024

# A file smaller than this is not worth splitting.
MIN_SPLIT = 8 * 1024 * 1024

# CDN / Proxy statuses that mean rate limiting, quota exhaustion, or worker outage.
QUOTA_STATUSES = (402, 403, 429, 502, 503, 504, 509)

# Ceiling on sockets this task may hold open at once, across every proxy.
# The per-worker semaphores below multiply with the pool -- 102 proxies was
# reaching ~600 concurrent segment requests -- while aiohttp's connector queues
# everything past its own limit *inside* itself. A quota retry then cancels
# requests out of that queue and can leave the socket behind, which is how the
# loop ends up reusing a descriptor that a transport still holds ("File
# descriptor N is used by transport ... closed=False", then Errno 9 and, on
# 2026-09-23, an abort). One gate in front of the connector keeps the queue
# empty, so every request in flight is one this code is actively reading.
MAX_CONNECTIONS = 64

# Per proxy. Low on purpose: the pool is wide (one worker per ~5 files at this
# size) and the throughput comes from that width, not from piling connections
# onto any single worker.
PER_WORKER_CONNECTIONS = 2

_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


class _QuotaReached(Exception):
    """Mega CDN refused because of the egress IP."""


class _CDNExpiredError(Exception):
    """CDN URL has expired or returned 404."""


def _get_proxy_list():
    """Return the current proxy pool (gateway-backed, cached; see proxy_pool)."""
    return get_proxy_pool()


class MegaDownloadHelper:
    """Drives one Mega task and reports progress to the status bar."""

    def __init__(self, listener):
        self._listener = listener
        self._gid = token_urlsafe(10)
        self._processed = 0
        self._lock = Lock()
        # Bytes per second, so a float: ``speed`` below divides a byte delta by
        # the seconds it took.
        self._speed: float = 0
        self._last_time = 0.0
        self._last_bytes = 0
        proxies = _get_proxy_list()
        self._worker_sems = [
            Semaphore(PER_WORKER_CONNECTIONS) for _ in range(max(1, len(proxies)))
        ]
        self._gate = Semaphore(MAX_CONNECTIONS)

    @property
    def processed_bytes(self):
        return self._processed

    @property
    def speed(self):
        now = time()
        if (elapsed := now - self._last_time) > 0.5:
            self._speed = (self._processed - self._last_bytes) / elapsed
            self._last_time = now
            self._last_bytes = self._processed
        return self._speed

    def _session(self):
        # The connector limit is the backstop; ``_gate`` in ``_segment`` is what
        # normally keeps the queue empty. No per-host limit: this session also
        # carries the gateway calls (resolve, file_cdn), which all share one
        # host, and a per-host cap would park them in the connector -- the very
        # place a cancellation can strand a socket. Per-proxy concurrency is
        # already capped by the worker semaphores.
        return ClientSession(
            headers={"User-Agent": _USER_AGENT},
            timeout=ClientTimeout(total=None, sock_read=120, sock_connect=60),
            connector=TCPConnector(limit=MAX_CONNECTIONS),
        )

    async def _segment(self, session, cdn_url, path, aes_key, nonce, start, end, done, proxy_n=0):
        """Fetch [start, end] of ciphertext, decrypt, write at its offset.

        `done` carries how many bytes of this segment already landed on a prior
        attempt so a retry resumes rather than refetches.
        """
        at = start + done[0]
        if at > end:
            return

        aligned = (at // BLOCK) * BLOCK
        skip = at - aligned

        proxied = proxied_url(cdn_url, proxy_n)
        headers = {"Range": f"bytes={aligned}-{end}"}
        worker_sem = self._worker_sems[proxy_n % len(self._worker_sems)]
        # Gate before worker slot, in that order everywhere, so the two can
        # never deadlock -- a task holding a worker slot is already past the
        # gate and is doing I/O, so it always gives the slot back.
        async with self._gate:
            async with worker_sem, session.get(proxied, headers=headers) as resp:
                if resp.status in QUOTA_STATUSES:
                    raise _QuotaReached(f"CDN answered HTTP {resp.status}")
                if resp.status in (404, 410):
                    raise _CDNExpiredError(f"CDN answered HTTP {resp.status}")
                if resp.status not in (200, 206):
                    raise ConnectionError(f"CDN answered HTTP {resp.status}")

                async with aiopen(path, "r+b") as f:
                    await f.seek(at)
                    cipher = ctr_stream(aes_key, counter_at(nonce, aligned))

                    async for chunk in resp.content.iter_chunked(CHUNK):
                        if self._listener.is_cancelled:
                            return

                        plain = cipher.update(chunk)

                        if skip:
                            if len(plain) <= skip:
                                skip -= len(plain)
                                continue
                            plain, skip = plain[skip:], 0

                        await f.write(plain)
                        done[0] += len(plain)
                        async with self._lock:
                            self._processed += len(plain)

        if not self._listener.is_cancelled and done[0] < end - start + 1:
            raise ConnectionError(
                f"CDN closed after {done[0]} of {end - start + 1} bytes"
            )

    def _spans(self, size):
        """Split a file into contiguous block-aligned ranges."""
        if size <= 0:
            return [(0, 0)]
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

    async def _download_file(self, session, item, folder_handle, dest, file_idx=0):
        """One file, rotating the proxy worker on quota."""
        await makedirs(ospath.dirname(dest), exist_ok=True)

        # Items from resolve_link carry pre-decoded aes_key/nonce bytes.
        # Items from list_folder carry key_b64 and need key_from_node.
        if "aes_key" in item:
            aes_key = item["aes_key"]
            nonce = item["nonce"]
        else:
            aes_key, nonce = key_from_node(item)

        cdn_url = item.get("cdn_url") or ""
        progress = None
        restarts = 0
        proxies = _get_proxy_list()
        proxy_n = file_idx % len(proxies) if proxies else 0

        while True:
            if self._listener.is_cancelled:
                return False
            try:
                if not cdn_url:
                    cdn_url, size = await file_cdn(session, folder_handle, item["handle"])
                    size = size or item.get("size", 0)
                else:
                    size = item.get("size", 0)

                if size == 0:
                    async with aiopen(dest, "wb") as f:
                        pass
                    return True

                if progress is None:
                    async with aiopen(dest, "wb") as f:
                        await f.truncate(size)
                    progress = [[0] for _ in self._spans(size)]

                spans = self._spans(size)
                tasks = [
                    create_task(
                        self._segment(
                            session, cdn_url, dest, aes_key, nonce,
                            start, end, done, proxy_n,
                        )
                    )
                    for (start, end), done in zip(spans, progress)
                ]
                try:
                    await gather(*tasks)
                except BaseException:
                    for task in tasks:
                        task.cancel()
                    await gather(*tasks, return_exceptions=True)
                    raise
                return not self._listener.is_cancelled

            except (_QuotaReached, MegaApiError, _CDNExpiredError, ConnectionError, TimeoutError, ClientError) as e:
                if self._listener.is_cancelled:
                    return False
                proxies = _get_proxy_list()
                max_restarts = int(Config.MEGA_MAX_RESTARTS or 0)
                if proxies:
                    max_restarts = max(max_restarts, len(proxies))
                if restarts >= max_restarts:
                    raise
                restarts += 1
                proxy_n += 1
                if folder_handle:
                    cdn_url = ""  # Re-resolve fresh CDN URL from gateway for folder items
                LOGGER.info(
                    f"Mega: {e} on {item['name']}, rotating proxy index to {proxy_n} "
                    f"[{restarts}/{max_restarts}]"
                )
                await sleep(3)
                continue

    async def _register(self, from_queue=False):
        async with task_dict_lock:
            task_dict[self._listener.mid] = MegaStatus(self._listener, self, self._gid)
        if not from_queue:
            await self._listener.on_download_start()
            if self._listener.multi <= 1 and not self._listener.is_rss:
                await send_status_message(self._listener.message)

    async def _gather_files(self, session, link):
        """Resolve the link into a file list and a task name."""
        kind = link["kind"]
        handle = link["handle"]
        key = link["key"]

        if kind == "file":
            resolved = await resolve_link(session, kind, handle, key)
            return resolved["files"], resolved["name"], None

        # folder: use /list for the full listing (resolve gives partial data).
        # `target` narrows it to the subfolder/file the link pointed at; the
        # root handle still drives /download, since only it has a share key.
        listing = await list_folder(
            session,
            handle,
            key,
            link.get("target", ""),
            link.get("target_kind", ""),
        )
        return listing["files"], listing["name"], handle

    async def add_download(self, path):
        link = self._listener.link["mega"]

        try:
            # Metadata resolution is pre-queue work, so a bulk would otherwise
            # ask the gateway to resolve every link at once and get rate limited
            # for it. The gate covers the gateway calls only -- the queue wait
            # and the transfer below must never hold a slot.
            async with resolve_gate():
                # Pull the live proxy pool from the gateway once per task and
                # size the per-worker semaphores (and thus _run's file
                # concurrency) to it.
                await refresh_proxy_pool()
                self._worker_sems = [
                    Semaphore(PER_WORKER_CONNECTIONS)
                    for _ in range(max(1, len(get_proxy_pool())))
                ]
                async with self._session() as session:
                    files, title, folder_handle = await self._gather_files(
                        session, link
                    )
        except (MegaApiError, ValueError, ConnectionError) as e:
            await self._listener.on_download_error(f"Mega: {e}")
            return

        self._listener.size = sum(item.get("size", 0) for item in files)
        self._user_set_name = bool(self._listener.name)
        if not self._listener.name:
            self._listener.name = title
        if self._listener.name.lower().endswith(".m4v"):
            self._listener.name = f"{self._listener.name[:-4]}.mp4"
        single = len(files) == 1 and not files[0].get("path")

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

        await self._run(path, folder_handle, files, single)

    async def _run(self, path, folder_handle, files, single):
        """Download files concurrently (up to number of proxies), then hand over to the listener."""
        base = path if single else f"{path}/{self._listener.name}"
        failed = []
        sem = Semaphore(len(self._worker_sems))
        stream = None
        if self._listener.stream_upload:
            # Imported here rather than at the top: the component is the upload
            # side of the same package that this module is the download side of.
            from ..upload.stream_uploader import StreamUploader

            # ``base`` and not the task directory: a thumbnail is looked for
            # beside the file, so the uploader has to be pointed at where the
            # files ended up.
            stream = StreamUploader(self._listener, base)
            if not await stream.start():
                return

        async def _download_item(item, idx):
            if self._listener.is_cancelled:
                return
            if single:
                name = self._listener.name
            else:
                name = item["name"]
                if name.lower().endswith(".m4v"):
                    name = f"{name[:-4]}.mp4"
            dest = ospath.join(base, item.get("path", ""), name)
            async with sem:
                try:
                    await self._download_file(session, item, folder_handle, dest, idx)
                except CancelledError:
                    raise
                except Exception as e:
                    if self._listener.is_cancelled:
                        return
                    LOGGER.error(f"Mega: {item['name']} failed: {e}")
                    failed.append(f"{item['name']} ({e})")
                    if await aiopath.exists(dest):
                        await remove(dest)
                    return
                if stream is not None:
                    # Every one of these coroutines arrives here at once; the
                    # component's single consumer is what keeps the uploader --
                    # which holds one file's state at a time -- from being
                    # written by two of them. Waiting is the backpressure: a
                    # file is handed over only once there is room for it.
                    await stream.submit(dest)

        error = None
        try:
            async with self._session() as session:
                await gather(*[_download_item(item, idx) for idx, item in enumerate(files)])
        except CancelledError:
            return
        except Exception as e:
            error = e
        finally:
            # in a finally so a cancellation still gives the uploader back what
            # it was sent, and before the outcome is reported: reporting it ends
            # the task, which sweeps the directory an upload may still be
            # reading from.
            if stream is not None:
                await stream.drain()

        if error is not None:
            await self._listener.on_download_error(f"Mega: {error}")
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
            self._listener.size = self._processed

        if stream is not None:
            await stream.finalize()
            return
        await self._listener.on_download_complete()

    async def cancel_task(self):
        self._listener.is_cancelled = True
        LOGGER.info(f"Cancelling Download: {self._listener.name}")
        await self._listener.on_download_error("Stopped by User!")


async def add_mega_download(listener, path):
    await MegaDownloadHelper(listener).add_download(path)
