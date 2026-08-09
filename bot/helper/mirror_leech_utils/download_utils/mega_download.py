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
from urllib.parse import quote

from aiofiles import open as aiopen
from aiofiles.os import makedirs, remove
from aiofiles.os import path as aiopath
from aiohttp import ClientError, ClientSession, ClientTimeout

from .... import LOGGER, task_dict, task_dict_lock
from ....core.config_manager import Config
from ...ext_utils.mega_client import (
    BLOCK,
    MegaApiError,
    counter_at,
    ctr_stream,
    file_cdn,
    key_from_node,
    list_folder,
    resolve_link,
)
from ...ext_utils.task_manager import check_running_tasks, stop_duplicate_check
from ...telegram_helper.message_utils import send_status_message
from ..status_utils.mega_status import MegaStatus
from ..status_utils.queue_status import QueueStatus

# Read size per connection.
CHUNK = 1024 * 1024

# A file smaller than this is not worth splitting.
MIN_SPLIT = 8 * 1024 * 1024

# CDN / Proxy statuses that mean rate limiting, quota exhaustion, or worker outage.
QUOTA_STATUSES = (402, 403, 429, 502, 503, 504, 509)

# Default fallback proxies 1 to 5 if MEGA_PROXY_URL is empty
_DEFAULT_PROXIES = [
    f"https://proxy-{n}.vianstefani754.workers.dev" for n in range(1, 6)
]

_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


class _QuotaReached(Exception):
    """Mega CDN refused because of the egress IP."""


class _CDNExpiredError(Exception):
    """CDN URL has expired or returned 404."""


def _get_proxy_list():
    """Parse Config.MEGA_PROXY_URL into a list of proxy base URLs."""
    raw = getattr(Config, "MEGA_PROXY_URL", "")
    if isinstance(raw, (list, tuple)):
        proxies = [str(p).strip() for p in raw if str(p).strip()]
    elif isinstance(raw, str) and raw.strip():
        # Support space or comma separated proxies
        items = raw.replace(",", " ").split()
        proxies = [p.strip() for p in items if p.strip()]
    else:
        proxies = []
    return proxies or _DEFAULT_PROXIES


def _proxied_url(cdn_url, proxy_index=0):
    """Wrap cdn_url through the selected proxy worker from Config.MEGA_PROXY_URL or default list."""
    proxies = _get_proxy_list()
    base = proxies[proxy_index % len(proxies)]

    encoded_cdn = quote(cdn_url, safe="")
    if "{url}" in base:
        return base.format(url=encoded_cdn)
    if base.endswith("=") or base.endswith("&"):
        return f"{base}{encoded_cdn}"

    if "?" in base:
        return f"{base}&url={encoded_cdn}"
    if base.endswith("/"):
        return f"{base}?url={encoded_cdn}"
    return f"{base}/?url={encoded_cdn}"


class MegaDownloadHelper:
    """Drives one Mega task and reports progress to the status bar."""

    def __init__(self, listener):
        self._listener = listener
        self._gid = token_urlsafe(10)
        self._processed = 0
        self._lock = Lock()
        self._speed = 0
        self._last_time = 0.0
        self._last_bytes = 0
        proxies = _get_proxy_list()
        self._worker_sems = [Semaphore(6) for _ in range(max(1, len(proxies)))]

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
        return ClientSession(
            headers={"User-Agent": _USER_AGENT},
            timeout=ClientTimeout(total=None, sock_read=120, sock_connect=60),
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

        proxied = _proxied_url(cdn_url, proxy_n)
        headers = {"Range": f"bytes={aligned}-{end}"}
        worker_sem = self._worker_sems[proxy_n % len(self._worker_sems)]
        async with worker_sem:
            async with session.get(proxied, headers=headers) as resp:
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

        # folder: use /list for the full listing (resolve gives partial data)
        listing = await list_folder(session, handle, key)
        return listing["files"], listing["name"], handle

    async def add_download(self, path):
        link = self._listener.link["mega"]

        try:
            async with self._session() as session:
                files, title, folder_handle = await self._gather_files(session, link)
        except (MegaApiError, ValueError, ConnectionError) as e:
            await self._listener.on_download_error(f"Mega: {e}")
            return

        self._listener.size = sum(item.get("size", 0) for item in files)
        if not self._listener.name:
            self._listener.name = title
        if self._listener.name.lower().endswith(".m4v"):
            self._listener.name = f"{self._listener.name[:-4]}.mp4"
        single = len(files) == 1 and not files[0].get("path")

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

        await self._run(path, folder_handle, files, single)

    async def _run(self, path, folder_handle, files, single):
        """Download files concurrently (up to number of proxies), then hand over to the listener."""
        base = path if single else f"{path}/{self._listener.name}"
        failed = []
        sem = Semaphore(len(self._worker_sems))

        async def _download_item(item, idx):
            if self._listener.is_cancelled:
                return
            name = item["name"] if not single else self._listener.name
            if name.lower().endswith(".m4v"):
                name = f"{name[:-4]}.mp4"
            dest = ospath.join(base, item.get("path", ""), name)
            async with sem:
                try:
                    await self._download_file(session, item, folder_handle, dest, idx)
                except CancelledError:
                    raise
                except Exception as e:
                    LOGGER.error(f"Mega: {item['name']} failed: {e}")
                    failed.append(f"{item['name']} ({e})")
                    if await aiopath.exists(dest):
                        await remove(dest)

        try:
            async with self._session() as session:
                await gather(*[_download_item(item, idx) for idx, item in enumerate(files)])
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
            self._listener.size = self._processed

        await self._listener.on_download_complete()

    async def cancel_task(self):
        self._listener.is_cancelled = True
        LOGGER.info(f"Cancelling Download: {self._listener.name}")
        await self._listener.on_download_error("Stopped by User!")


async def add_mega_download(listener, path):
    await MegaDownloadHelper(listener).add_download(path)
