"""Client for the gateway's matok live routes.

The gateway signs in to matok, assembles the CDN URL and answers with a plain
JSON envelope; this module is only the bot's half of that conversation:

- ``GET /api/v1/live/matok/rooms``   -> the live catalogue, paginated
- ``GET /api/v1/live/matok/stream``  -> a signed URL for one anchor

The request functions take their HTTP client, base URL and headers as
arguments. That keeps the module importable without ``bot`` (whose ``__init__``
installs uvloop and opens files) and lets the tests drive it through
``httpx.MockTransport``. ``rooms()`` / ``stream()`` are the production wrappers
that fill those arguments from ``Config`` via ``bot.helper.net.gateway``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

ROOMS_PATH = "/api/v1/live/matok/rooms"
STREAM_PATH = "/api/v1/live/matok/stream"
DEFAULT_PAGE_SIZE = 10

_ALLOWED_SCHEMES = frozenset({"rtmp", "rtmps", "http", "https"})
_TIMEOUT = 30.0

#: What the user should read for each failure the gateway names. Codes the
#: gateway adds later fall back to its own message.
_FRIENDLY = {
    "NOT_CONFIGURED": (
        "The live gateway is not configured yet (matok credentials are missing)."
    ),
    "TOKEN_INVALID": "The gateway could not sign in to matok. Try again later.",
    "AUTH_FAILED": "The gateway could not sign in to matok. Try again later.",
    "ROOM_VERIFY_REQUIRED": (
        "That room needs manual verification in the matok app, so it cannot "
        "be recorded."
    ),
    "ROOM_OFFLINE": "That room is offline now.",
    "URL_UNAVAILABLE": "The stream is not available right now. Try again later.",
    "GATEWAY_ERROR": "The live gateway did not answer properly. Try again later.",
}


class LiveError(Exception):
    """A failure the gateway named, or a request that never got an answer."""

    def __init__(self, code: str, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    @property
    def friendly(self) -> str:
        return _FRIENDLY.get(self.code, self.message)


@dataclass(frozen=True)
class LiveRoom:
    anchor_id: str
    nickname: str
    game: str = ""
    online_count: int = 0


@dataclass(frozen=True)
class LivePage:
    rooms: tuple[LiveRoom, ...]
    page: int
    pages: int
    total: int


@dataclass(frozen=True)
class LiveStream:
    url: str
    anchor_id: str
    nickname: str
    game: str
    expires_at: int


def valid_stream_url(url: str) -> bool:
    """Whether *url* is something ffmpeg may be pointed at.

    The URL is a credential, so the scheme is checked rather than trusted:
    ``file:``, ``concat:`` and ``pipe:`` are not streams, and a control
    character in a URL handed to ``create_subprocess_exec`` is not a URL.
    """
    if not url or any(ord(char) < 32 for char in url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme.lower() in _ALLOWED_SCHEMES and bool(parts.netloc)


async def _get(
    http: httpx.AsyncClient,
    url: str,
    params: Mapping[str, str | int],
    headers: Mapping[str, str],
) -> httpx.Response:
    try:
        return await http.get(url, params=params, headers=headers)
    except httpx.HTTPError as error:
        raise LiveError(
            "GATEWAY_ERROR", "The live gateway could not be reached."
        ) from error


def _envelope(response: httpx.Response) -> dict[str, Any]:
    """The JSON body, once it is known to be a success envelope."""
    try:
        payload = response.json()
    except ValueError as error:
        raise LiveError(
            "GATEWAY_ERROR",
            f"The live gateway answered {response.status_code} without JSON.",
            response.status_code,
        ) from error

    if not isinstance(payload, dict):
        raise LiveError(
            "GATEWAY_ERROR",
            "The live gateway answered with an unexpected body.",
            response.status_code,
        )

    if payload.get("success") is not True:
        code = payload.get("code")
        message = payload.get("error")
        raise LiveError(
            code if isinstance(code, str) and code else "GATEWAY_ERROR",
            message
            if isinstance(message, str) and message
            else f"The live gateway answered {response.status_code}.",
            response.status_code,
        )

    if not response.is_success:
        raise LiveError(
            "GATEWAY_ERROR",
            f"The live gateway answered {response.status_code}.",
            response.status_code,
        )

    return payload


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return default


def _room(raw: Any) -> LiveRoom | None:
    """One room, or None when the row cannot name one.

    A row without a numeric ``anchor_id`` cannot be recorded, so it is dropped
    rather than shown as a button that would always fail.
    """
    if not isinstance(raw, dict):
        return None
    anchor_id = raw.get("anchor_id")
    if not isinstance(anchor_id, str):
        return None
    anchor_id = anchor_id.strip()
    if not anchor_id.isdigit():
        return None
    nickname = raw.get("nickname")
    game = raw.get("game")
    return LiveRoom(
        anchor_id=anchor_id,
        nickname=nickname if isinstance(nickname, str) else "",
        game=game if isinstance(game, str) else "",
        online_count=_as_int(raw.get("online_count")),
    )


async def list_rooms(
    http: httpx.AsyncClient,
    base_url: str,
    headers: Mapping[str, str],
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> LivePage:
    response = await _get(
        http,
        f"{base_url.rstrip('/')}{ROOMS_PATH}",
        {"page": page, "page_size": page_size},
        headers,
    )
    payload = _envelope(response)

    raw_rooms = payload.get("rooms")
    if not isinstance(raw_rooms, list):
        raw_rooms = []
    rooms = tuple(room for raw in raw_rooms if (room := _room(raw)) is not None)

    return LivePage(
        rooms=rooms,
        page=_as_int(payload.get("page"), page),
        pages=_as_int(payload.get("pages"), 1),
        total=_as_int(payload.get("total")),
    )


async def fetch_stream(
    http: httpx.AsyncClient,
    base_url: str,
    headers: Mapping[str, str],
    anchor_id: str,
) -> LiveStream:
    if not anchor_id.isdigit():
        raise LiveError("INVALID_REQUEST", "The room id must be numeric.")

    response = await _get(
        http,
        f"{base_url.rstrip('/')}{STREAM_PATH}",
        {"anchor_id": anchor_id},
        headers,
    )
    payload = _envelope(response)

    url = payload.get("url")
    if not isinstance(url, str) or not valid_stream_url(url):
        raise LiveError(
            "URL_UNAVAILABLE",
            "The live gateway did not return a usable stream URL.",
            response.status_code,
        )

    nickname = payload.get("nickname")
    game = payload.get("game")
    return LiveStream(
        url=url,
        anchor_id=anchor_id,
        nickname=nickname if isinstance(nickname, str) else "",
        game=game if isinstance(game, str) else "",
        expires_at=_as_int(payload.get("expires_at")),
    )


# ── production wrappers ─────────────────────────────────────────────────────

_CLIENT: httpx.AsyncClient | None = None


def _shared_client() -> httpx.AsyncClient:
    """One client for the whole process, built on first use.

    Built lazily rather than at import: a module-level ``AsyncClient`` is
    created before the bot's loop exists, and that is the kind of thing that
    only fails in production.
    """
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = httpx.AsyncClient(timeout=_TIMEOUT)
    return _CLIENT


async def rooms(page: int = 1, page_size: int = DEFAULT_PAGE_SIZE) -> LivePage:
    # Imported here: the gateway helper belongs to ``bot``, and importing it at
    # module level would make this file refuse to load standalone in tests.
    from .gateway import gateway_headers, gateway_url

    return await list_rooms(
        _shared_client(), gateway_url(), gateway_headers(), page, page_size
    )


async def stream(anchor_id: str) -> LiveStream:
    from .gateway import gateway_headers, gateway_url

    return await fetch_stream(
        _shared_client(), gateway_url(), gateway_headers(), anchor_id
    )
