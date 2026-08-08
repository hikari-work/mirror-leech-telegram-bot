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
from urllib.parse import quote, urlencode
from yarl import URL

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

GATEWAY = "https://api.piyann.me/api/v1/scrape/mega"
BLOCK = 16
_API_ATTEMPTS = 4


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


# ---------------------------------------------------------------------------
# Gateway HTTP helper
# ---------------------------------------------------------------------------

_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


async def _gateway_get(session, path, params, context):
    """GET <GATEWAY><path>?<params>, retry on transient failures.

    Returns the parsed JSON body on success; raises MegaApiError on gateway
    error or after all attempts exhausted.
    """
    base = GATEWAY + path
    query_str = urlencode(params or {}, safe="%")
    req_url = URL(f"{base}?{query_str}", encoded=True) if query_str else URL(base)
    headers = {"User-Agent": _USER_AGENT}
    error = None

    for attempt in range(1, _API_ATTEMPTS + 1):
        try:
            async with session.get(req_url, headers=headers) as resp:
                if resp.status == 429:
                    raise MegaApiError("gateway rate limited", context)
                if resp.status != 200:
                    raise MegaApiError(
                        f"gateway returned HTTP {resp.status}", context
                    )
                body = await resp.json(content_type=None)
        except MegaApiError:
            raise
        except Exception as e:
            error = e
            if attempt == _API_ATTEMPTS:
                break
            await sleep(2 * attempt)
            continue

        if not body.get("success"):
            msg = body.get("error") or "unknown gateway error"
            raise MegaApiError(msg, context)

        return body

    raise MegaApiError(
        f"gateway unreachable while {context}: {error}", context
    )


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
    return {
        "handle": f["handle"],
        "name": _safe_name(f.get("name"), f["handle"]),
        "path": f.get("path") or "",
        "size": int(f.get("size") or 0),
        "aes_key": b64url_decode(f["aes_key"]),   # 16 bytes
        "nonce": b64url_decode(f["nonce"]),         # 8 bytes
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


async def list_folder(session, handle, key):
    """List a folder share via the gateway's /list endpoint.

    Returns ``{"name": str, "files": [{handle,name,path,size,key_b64}, ...]}``.
    """
    body = await _gateway_get(
        session, "/list", {"folder": handle, "key": key}, "listing the folder"
    )

    files = []
    for node in body.get("files") or []:
        if node.get("is_folder"):
            continue
        files.append({
            "handle": node["h"],
            "name": _safe_name(node.get("name"), node["h"]),
            "path": node.get("path") or "",
            "size": int(node.get("s") or 0),
            "key_b64": node.get("key_b64") or "",
        })

    if not files:
        raise ValueError("no downloadable files found in this Mega folder")

    return {"name": _safe_name(body.get("name"), handle), "files": files}


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
