"""TorBox torrent / web-download resolver.

Turns a magnet, a ``.torrent`` file, or a plain link into the multi-file
payload ``add_direct_download`` consumes. The poll-until-ready loop and
the bounded fan-out over files are shared with the AllDebrid resolver and
live in ``debrid/base.py``; this module holds the TorBox API surface.
"""

import asyncio
from collections.abc import Awaitable, Callable
from os import path as ospath
from typing import Any

from bot import LOGGER
from bot.core.config_manager import Config
from bot.helper.ext_utils.exceptions import DirectDownloadLinkException

from .debrid import base

_API_BASE = "https://api.torbox.app/v1/api"
_TIMEOUT = 45.0
_POLL_INTERVAL = base.POLL_INTERVAL_S
_MAX_WAIT = base.MAX_DURATION_S
_NO_SEED_WAIT = base.NO_SEED_TIMEOUT_S
_UNLOCK_CONCURRENCY = base.UNLOCK_CONCURRENCY

_READY_STATES = {"cached", "completed", "uploading"}
_ERROR_STATES = {
    "error",
    "failed",
    "missingfiles",
    "stalled",
    "stalled (no seeds)",
    "dead",
    "unknown",
}


def _token() -> str:
    return base.require_key(
        getattr(Config, "TORBOX_API_KEY", ""),
        "ERROR: TORBOX_API_KEY is not configured",
    )


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_token()}",
        "User-Agent": "mltb-torbox/1.0",
    }


def _err(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(
            payload.get("detail")
            or payload.get("message")
            or payload.get("error")
            or payload
        )
    return str(payload)


async def _api(
    method: str,
    endpoint: str,
    *,
    params: dict[str, Any] | None = None,
    data: Any = None,
    files: Any = None,
) -> Any:
    payload = await base.request_json(
        method,
        f"{_API_BASE}{endpoint}",
        provider="TorBox",
        timeout=_TIMEOUT,
        shape_error="ERROR: TorBox returned unexpected payload",
        headers=_headers(),
        params=params,
        data=data,
        files=files,
    )

    if payload.get("success") is not True:
        raise DirectDownloadLinkException(f"ERROR: TorBox: {_err(payload)}")

    return payload.get("data")


def _first_item(data: Any) -> dict[str, Any]:
    if isinstance(data, list):
        return data[0] if data else {}

    if isinstance(data, dict):
        return next(
            (
                data[key]
                for key in ("torrent", "webdl", "download", "item")
                if isinstance(data.get(key), dict)
            ),
            data,
        )
    return {}


def _is_ready(item: dict[str, Any]) -> bool:
    if item.get("download_finished") is True or item.get("download_present") is True:
        return True

    state = str(item.get("download_state") or "").lower()
    return state in _READY_STATES and bool(item.get("files"))


def _has_error(item: dict[str, Any]) -> str:
    if item.get("error"):
        return str(item["error"])

    state = str(item.get("download_state") or "").lower()
    return state if state in _ERROR_STATES else ""


def _basename(name: str) -> str:
    return ospath.basename(name.rstrip("/")) or "file"


async def _create_torrent_from_magnet(magnet: str) -> dict[str, Any]:
    LOGGER.info("TorBox: creating torrent from magnet")
    files = {
        "magnet": (None, magnet),
        "seed": (None, "3"),
        "allow_zip": (None, "true"),
    }
    data = await _api("POST", "/torrents/createtorrent", files=files)
    if item := _first_item(data):
        return item
    else:
        raise DirectDownloadLinkException("ERROR: TorBox returned no torrent data")


async def _create_torrent_from_file(
    torrent_bytes: bytes, filename: str
) -> dict[str, Any]:
    LOGGER.info(f"TorBox: creating torrent from file: {filename}")
    files = {
        "file": (filename, torrent_bytes, "application/x-bittorrent"),
        "seed": (None, "3"),
        "allow_zip": (None, "true"),
    }
    data = await _api("POST", "/torrents/createtorrent", files=files)
    if item := _first_item(data):
        return item
    else:
        raise DirectDownloadLinkException("ERROR: TorBox returned no torrent data")


async def _create_webdl(link: str) -> dict[str, Any]:
    LOGGER.info("TorBox: creating web download")
    files = {"link": (None, link)}
    data = await _api("POST", "/webdl/createwebdownload", files=files)
    if item := _first_item(data):
        return item
    else:
        raise DirectDownloadLinkException("ERROR: TorBox returned no webdl data")


async def _get_torrent(torrent_id: int | str) -> dict[str, Any]:
    data = await _api(
        "GET",
        "/torrents/mylist",
        params={"id": str(torrent_id), "bypass_cache": "true"},
    )
    if item := _first_item(data):
        return item
    else:
        raise DirectDownloadLinkException(
            f"ERROR: TorBox returned no torrent status for {torrent_id}"
        )


async def _get_webdl(web_id: int | str) -> dict[str, Any]:
    data = await _api(
        "GET",
        "/webdl/mylist",
        params={"id": str(web_id), "bypass_cache": "true"},
    )
    if item := _first_item(data):
        return item
    else:
        raise DirectDownloadLinkException(
            f"ERROR: TorBox returned no webdl status for {web_id}"
        )


async def delete_torrent(torrent_id: int | str) -> bool:
    try:
        await _api(
            "POST",
            "/torrents/controltorrent",
            data={"torrent_id": str(torrent_id), "operation": "Delete"},
        )
        return True
    except Exception as exc:
        LOGGER.warning(f"TorBox: failed to delete torrent {torrent_id}: {exc}")
        return False


async def delete_web_download(web_id: int | str) -> bool:
    try:
        await _api(
            "POST",
            "/webdl/controlwebdownload",
            data={"web_id": str(web_id), "operation": "Delete"},
        )
        return True
    except Exception as exc:
        LOGGER.warning(f"TorBox: failed to delete webdl {web_id}: {exc}")
        return False


def _progress_snapshot(kind: str, item: dict[str, Any]) -> dict[str, Any]:
    return {
        "phase": kind,
        "name": item.get("name"),
        "state": item.get("download_state"),
        "progress": item.get("progress"),
        "seeds": item.get("seeds"),
        "peers": item.get("peers"),
        "eta": item.get("eta"),
    }


def _swarm_is_empty(item: dict[str, Any]) -> bool:
    return int(item.get("seeds") or 0) == 0 and int(item.get("peers") or 0) == 0


async def _wait_ready(
    item_id: int | str,
    kind: str,
    *,
    is_cancelled: Callable[[], bool] | None = None,
    progress_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    getter = _get_torrent if kind == "torrent" else _get_webdl

    return await base.wait_until_ready(
        lambda: getter(item_id),
        is_ready=_is_ready,
        error_message=lambda item: (
            f"ERROR: TorBox {kind} failed: {err}" if (err := _has_error(item)) else ""
        ),
        # A web download has no swarm, so only torrents can stall on one.
        is_stalled=_swarm_is_empty if kind == "torrent" else None,
        cancelled_message="ERROR: TorBox task cancelled",
        stall_message="ERROR: TorBox no seed / no peer timeout",
        timeout_message="ERROR: TorBox max wait timeout",
        progress_payload=lambda item: _progress_snapshot(kind, item),
        progress_callback=progress_callback,
        is_cancelled=is_cancelled,
        poll_interval=_POLL_INTERVAL,
        no_seed_timeout=_NO_SEED_WAIT,
        max_duration=_MAX_WAIT,
    )


async def _request_file_link(kind: str, item_id: int | str, file_id: int | str) -> str:
    endpoint = "/torrents/requestdl" if kind == "torrent" else "/webdl/requestdl"
    id_key = "torrent_id" if kind == "torrent" else "web_id"

    params = {
        "token": _token(),
        id_key: str(item_id),
        "file_id": str(file_id),
    }

    last_exc = None

    for attempt in range(1, 4):
        try:
            data = await _api("GET", endpoint, params=params)
            if isinstance(data, str) and data:
                return data
            raise DirectDownloadLinkException("ERROR: TorBox did not return direct URL")
        except Exception as exc:
            last_exc = exc
            if attempt < 3:
                await asyncio.sleep(attempt * 2)
                continue
            raise

    raise DirectDownloadLinkException(f"ERROR: TorBox requestdl failed: {last_exc}")


async def _payload(
    item: dict[str, Any], kind: str, item_id: int | str
) -> dict[str, Any]:
    files = item.get("files") or []

    if not isinstance(files, list) or not files:
        raise DirectDownloadLinkException("ERROR: TorBox returned no files")

    async def one(_index: int, file_item: dict[str, Any]) -> dict[str, Any] | None:
        file_id = file_item.get("id")
        if file_id is None:
            return None

        direct = await _request_file_link(kind, item_id, file_id)
        full_name = file_item.get("name") or file_item.get("short_name") or "file"

        return {
            "filename": file_item.get("short_name") or _basename(full_name),
            "path": full_name,
            "url": direct,
            "size": int(file_item.get("size") or 0),
            "headers": {},
        }

    contents = await base.resolve_files_concurrently(
        [f for f in files if isinstance(f, dict)],
        one,
        concurrency=_UNLOCK_CONCURRENCY,
        # Preserved from the original gather-and-append: entries land in
        # completion order, not in the order TorBox listed the files.
        ordered=False,
    )

    if not contents:
        raise DirectDownloadLinkException("ERROR: TorBox could not create direct links")

    return {
        "title": item.get("name") or "TorBox",
        "total_size": sum(x["size"] for x in contents),
        "contents": contents,
    }


async def _resolve_created(
    entry: dict[str, Any],
    kind: str,
    id_keys: tuple[str, ...],
    missing_id_error: str,
    result_key: str,
    delete: Callable[[int | str], Awaitable[bool]],
    *,
    is_cancelled: Callable[[], bool] | None = None,
    progress_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Wait out a created TorBox item and turn its files into direct links.

    On any failure the item is removed from the TorBox account so a retry
    does not pile up duplicates.
    """
    item_id = next((entry[key] for key in id_keys if entry.get(key)), None)

    if not item_id:
        raise DirectDownloadLinkException(missing_id_error)

    try:
        item = await _wait_ready(
            item_id,
            kind,
            is_cancelled=is_cancelled,
            progress_callback=progress_callback,
        )
        result = await _payload(item, kind, item_id)
        result[result_key] = item_id
        return result
    except Exception:
        await delete(item_id)
        raise


async def torbox_resolve_magnet(
    magnet: str,
    *,
    is_cancelled: Callable[[], bool] | None = None,
    progress_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    entry = await _create_torrent_from_magnet(magnet)
    return await _resolve_created(
        entry,
        "torrent",
        ("torrent_id", "id"),
        "ERROR: TorBox did not return torrent_id",
        "torbox_torrent_id",
        delete_torrent,
        is_cancelled=is_cancelled,
        progress_callback=progress_callback,
    )


async def torbox_resolve_torrent(
    torrent_bytes: bytes,
    filename: str,
    *,
    is_cancelled: Callable[[], bool] | None = None,
    progress_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    entry = await _create_torrent_from_file(torrent_bytes, filename)
    return await _resolve_created(
        entry,
        "torrent",
        ("torrent_id", "id"),
        "ERROR: TorBox did not return torrent_id",
        "torbox_torrent_id",
        delete_torrent,
        is_cancelled=is_cancelled,
        progress_callback=progress_callback,
    )


async def torbox_resolve(
    link: str,
    *,
    is_cancelled: Callable[[], bool] | None = None,
    progress_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    entry = await _create_webdl(link)
    return await _resolve_created(
        entry,
        "webdl",
        ("webdownload_id", "web_id", "id"),
        "ERROR: TorBox did not return webdownload_id",
        "torbox_web_id",
        delete_web_download,
        is_cancelled=is_cancelled,
        progress_callback=progress_callback,
    )
