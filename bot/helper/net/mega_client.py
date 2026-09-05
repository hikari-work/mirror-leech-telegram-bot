"""Mega gateway client and client-side CTR crypto.

Metadata resolution (name, size, per-file AES key + nonce) is delegated to
https://api.piyann.me — it talks to Mega's API and decrypts node attributes
so the bot never has to. "No plaintext passes through this gateway" means the
CDN bytes are not proxied; the per-file decryption keys are returned in the
clear and the bot still decrypts the file stream itself.

Only the primitives needed for AES-CTR byte-stream decryption remain here;
ECB/CBC, share-key recovery, and the raw Mega API are gone.
"""

from asyncio import sleep
from base64 import b64decode, b64encode
from logging import getLogger
from random import uniform
from urllib.parse import quote, urlencode
from yarl import URL

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from bot.helper.net.gateway import gateway_headers, gateway_url

LOGGER = getLogger(__name__)

BLOCK = 16
_API_ATTEMPTS = 4
_MAX_RETRY_DELAY = 30

# Statuses that mean "come back later", not "this link is gone". Failing fast on
# a rate limit is what turned a bulk into a pile of dead links: the gateway was
# only asking us to slow down.
_RETRY_STATUSES = (408, 425, 429, 500, 502, 503, 504)

# The gateway also reports a rate limit as HTTP 200 with success: false.
_RATE_LIMIT_HINTS = ("rate limit", "rate-limit", "too many request", "slow down")


# ---------------------------------------------------------------------------
# Gateway error
# ---------------------------------------------------------------------------

class MegaApiError(Exception):
    """An error response from the gateway or CDN."""

    def __init__(self, message, context=""):
        self.message = message
        super().__init__(f"{message} ({context})" if context else message)


# ---------------------------------------------------------------------------
# base64url helpers (Mega uses unpadded base64url throughout)
# ---------------------------------------------------------------------------

def b64url_decode(value):
    """Decode Mega's unpadded base64url."""
    value = (value or "").replace("-", "+").replace("_", "/")
    return b64decode(value + "=" * (-len(value) % 4))


def b64url_encode(raw):
    return b64encode(raw).decode().replace("+", "-").replace("/", "_").rstrip("=")


# ---------------------------------------------------------------------------
# AES-CTR stream (byte-stream decryption — unchanged)
# ---------------------------------------------------------------------------

def _aes_ctr(key, counter):
    return Cipher(algorithms.AES(key), modes.CTR(counter)).decryptor()


def ctr_decrypt(key, counter, data):
    """AES-CTR from an explicit counter block."""
    return _aes_ctr(key, counter).update(data)


def ctr_stream(key, counter):
    """A CTR context to feed a whole segment through, chunk by chunk."""
    return _aes_ctr(key, counter)


def counter_at(nonce, offset):
    """CTR counter block for an absolute byte offset."""
    return bytes(nonce[:8]) + (offset // BLOCK).to_bytes(8, "big")


# ---------------------------------------------------------------------------
# Path sanitisation (names come from share owner — untrusted)
# ---------------------------------------------------------------------------

def _safe_name(name, fallback):
    cleaned = (name or "").replace("/", "_").replace("\\", "_").strip().strip(".")
    return cleaned or fallback


def _path_within(folders, parent, target):
    """``parent``'s path relative to ``target``, or None if not under it.

    The gateway's own "path" field is relative to the share root, so it cannot
    answer "is this node inside subfolder X?" - walking the parent chain can,
    and it rebuilds the path re-rooted at the subfolder while it is at it.
    """
    segments = []
    node = parent
    for _ in range(len(folders) + 1):  # bounded: a parent cycle would hang
        if node == target:
            return "/".join(reversed(segments))
        folder = folders.get(node)
        if not folder:
            return None  # chain left the share without passing through target
        segments.append(_safe_name(folder.get("name"), node))
        node = folder.get("parent") or ""
    return None


# ---------------------------------------------------------------------------
# Gateway HTTP helper
# ---------------------------------------------------------------------------

_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def _retry_after(resp):
    """The gateway's own Retry-After, when it sent one."""
    headers = getattr(resp, "headers", None)
    return headers.get("Retry-After") if headers else None


def _retry_delay(attempt, retry_after=None):
    """Seconds to wait before attempt+1, jittered.

    The jitter matters more here than the base delay: a bulk resolves its links
    in lockstep, so a fixed backoff just replays the same burst one beat later.
    """
    if retry_after:
        try:
            return min(float(retry_after), _MAX_RETRY_DELAY) + uniform(0, 1.5)
        except (TypeError, ValueError):
            pass
    return min(2**attempt, _MAX_RETRY_DELAY) + uniform(0, 1.5)


def _is_rate_limited(reason):
    text = str(reason).lower()
    return any(hint in text for hint in _RATE_LIMIT_HINTS)


async def _gateway_attempt(session, req_url, headers, context, attempt):
    """One request at the gateway.

    Returns ``(body, reason, delay)``: a body on success, otherwise the reason
    and how long to wait before trying again. Errors that will not change on a
    retry -- a dead link, a bad handle -- raise instead.
    """
    try:
        async with session.get(req_url, headers=headers) as resp:
            if resp.status in _RETRY_STATUSES:
                reason = (
                    "gateway rate limited"
                    if resp.status == 429
                    else f"gateway returned HTTP {resp.status}"
                )
                return None, reason, _retry_delay(attempt, _retry_after(resp))
            if resp.status != 200:
                raise MegaApiError(f"gateway returned HTTP {resp.status}", context)
            body = await resp.json(content_type=None)
    except MegaApiError:
        raise
    except Exception as e:
        reason = f"gateway unreachable ({e.__class__.__name__}: {e})"
        return None, reason, _retry_delay(attempt)

    if body.get("success"):
        return body, None, None

    reason = body.get("error") or "unknown gateway error"
    if not _is_rate_limited(reason):
        raise MegaApiError(reason, context)
    return None, reason, _retry_delay(attempt)


async def _gateway_get(session, path, params, context):
    """GET <GATEWAY><path>?<params>, retry on transient failures.

    Returns the parsed JSON body on success; raises MegaApiError on a final
    gateway error or once the attempts are spent.
    """
    base = gateway_url(f"/api/v1/scrape/mega{path}")
    query_str = urlencode(params or {}, safe="%")
    req_url = URL(f"{base}?{query_str}", encoded=True) if query_str else URL(base)
    headers = {"User-Agent": _USER_AGENT, **gateway_headers(accept_json=False)}
    reason = "unknown gateway error"

    for attempt in range(1, _API_ATTEMPTS + 1):
        body, reason, delay = await _gateway_attempt(
            session, req_url, headers, context, attempt
        )
        if body is not None:
            return body
        if attempt == _API_ATTEMPTS:
            break
        # Every failure comes back with a delay to go with its reason; the one
        # None is the one that arrives with a body, and that returned above.
        wait = delay or 0
        LOGGER.info(
            f"Mega gateway: {reason} while {context}, retrying in "
            f"{wait:.1f}s [{attempt}/{_API_ATTEMPTS}]"
        )
        await sleep(wait)

    raise MegaApiError(f"{reason} after {_API_ATTEMPTS} attempts", context)


# ---------------------------------------------------------------------------
# Public API: resolve / list / cdn_url
# ---------------------------------------------------------------------------

def _reconstruct_url(kind, handle, key):
    """Re-assemble a mega.nz share URL from its parsed parts.

    The gateway accepts the full URL and percent-encodes '#' itself; passing
    the fragment as '%23' avoids URL parsers stripping it.
    """
    return f"https://mega.nz/{kind}/{handle}%23{quote(key, safe='')}"


def _normalise_file(f):
    """Map a MegaResolvedFile dict to the internal {handle,name,path,size,aes_key,nonce} shape."""
    if "aes_key" in f:
        aes_key = b64url_decode(f["aes_key"])
        nonce = b64url_decode(f["nonce"])
    elif f.get("key_b64"):
        raw = b64url_decode(f["key_b64"])
        if len(raw) < 32:
            raise ValueError(f"node key too short: {len(raw)} bytes")
        aes_key = bytes(a ^ b for a, b in zip(raw[:16], raw[16:32]))
        nonce = raw[16:24]
    else:
        aes_key = b""
        nonce = b""

    return {
        "handle": f["handle"],
        "name": _safe_name(f.get("name"), f["handle"]),
        "path": f.get("path") or "",
        "size": int(f.get("size") or 0),
        "aes_key": aes_key,
        "nonce": nonce,
        # cdn url is only present for single-file links
        "cdn_url": f.get("url") or "",
    }


async def resolve_link(session, kind, handle, key):
    """Resolve a Mega share link via the gateway.

    Returns::

        {
          "kind":   "file" | "folder",
          "name":   str,
          "handle": str,          # folder handle (for /download calls)
          "files":  [normalised_file, ...],
        }

    For a single-file link ``files[0]["cdn_url"]`` is already populated and
    no further ``file_cdn`` call is needed.
    """
    url = _reconstruct_url(kind, handle, key)
    body = await _gateway_get(session, "", {"url": url}, "resolving the share link")

    if body.get("kind") == "file":
        f = body.get("file") or {}
        return {
            "kind": "file",
            "name": _safe_name(body.get("name") or f.get("name"), handle),
            "handle": body.get("handle") or handle,
            "files": [_normalise_file(f)] if f else [],
        }

    # folder: files array in resolve response
    files = [_normalise_file(f) for f in (body.get("files") or [])]
    return {
        "kind": "folder",
        "name": _safe_name(body.get("name"), handle),
        "handle": body.get("handle") or handle,
        "files": files,
    }


async def list_folder(session, handle, key, target="", target_kind=""):
    """List a folder share via the gateway's /list endpoint.

    ``target`` narrows the result to one node inside the share - the subfolder
    or file a "#<key>/folder/<h>" link was pointing at. Mega only issues a
    handle+key pair for the share root, so the gateway can only ever list the
    whole tree; the narrowing is a filter over that listing, done here.

    Returns ``{"name": str, "files": [{handle,name,path,size,key_b64}, ...]}``.
    """
    body = await _gateway_get(
        session, "/list", {"folder": handle, "key": key}, "listing the folder"
    )

    nodes = body.get("files") or []

    # Folder nodes arrive in their own array, but older gateway builds mixed
    # them into "files" behind an is_folder flag - accept both.
    folders = {
        node["h"]: node
        for node in [*(body.get("folders") or []), *nodes]
        if node.get("h") and node.get("is_folder")
    }

    if target and target_kind == "file":
        files = [
            {
                "handle": node["h"],
                "name": _safe_name(node.get("name"), node["h"]),
                "path": "",
                "size": int(node.get("s") or 0),
                "key_b64": node.get("key_b64") or "",
            }
            for node in nodes
            if not node.get("is_folder") and node.get("h") == target
        ]
        if not files:
            raise ValueError(f"file {target} is not in this Mega folder")
        return {"name": files[0]["name"], "files": files}

    files = []
    for node in nodes:
        if node.get("is_folder"):
            continue
        if target:
            path = _path_within(folders, node.get("parent") or "", target)
            if path is None:
                continue  # outside the requested subfolder
        else:
            path = node.get("path") or ""
        files.append({
            "handle": node["h"],
            "name": _safe_name(node.get("name"), node["h"]),
            "path": path,
            "size": int(node.get("s") or 0),
            "key_b64": node.get("key_b64") or "",
        })

    if not files:
        if target:
            raise ValueError(
                f"no downloadable files found in subfolder {target} "
                "of this Mega folder"
            )
        raise ValueError("no downloadable files found in this Mega folder")

    name = body.get("name")
    if target and target in folders:
        name = folders[target].get("name")

    return {"name": _safe_name(name, target or handle), "files": files}


async def file_cdn(session, folder_handle, file_handle):
    """Fetch a short-lived CDN URL for one file inside a folder share.

    Returns ``(cdn_url, size)``.
    """
    body = await _gateway_get(
        session,
        "/download",
        {"folder": folder_handle, "file": file_handle},
        "resolving the download URL",
    )
    if not body.get("cdn_url"):
        raise MegaApiError("gateway returned no CDN URL", "resolving the download URL")
    return body["cdn_url"], int(body.get("size") or 0)


def key_from_node(node):
    """Decode aes_key + nonce from a list_folder node (key_b64 is 32 bytes).

    The 32-byte file key is folded the same way Mega does it: bytes 0-15 XOR
    bytes 16-31 = AES key; bytes 16-23 = CTR nonce.
    """
    raw = b64url_decode(node["key_b64"])
    if len(raw) < 32:
        raise ValueError(f"node key too short: {len(raw)} bytes")
    aes_key = bytes(a ^ b for a, b in zip(raw[:16], raw[16:32]))
    nonce = raw[16:24]
    return aes_key, nonce
