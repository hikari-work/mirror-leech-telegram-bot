"""Mega's client-side crypto and the two API calls a download needs.

Mega encrypts every file in the browser before upload, so its CDN only ever
serves ciphertext and the key never reaches Mega itself - it lives in the '#'
fragment of a share link, the one part a browser does not send to a server.
Downloading therefore means doing the decryption here.

Nothing in this module talks to the bot: it takes a link's handle and key and
returns node metadata or a CDN URL. Policy - which proxy, when to rotate an IP,
where bytes land - belongs to the caller.
"""

from asyncio import sleep
from base64 import b64decode, b64encode
from json import loads
from random import randrange

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

API_URL = "https://g.api.mega.co.nz/cs"
BLOCK = 16

# Mega's error channel is a bare negative number where a JSON object was
# expected. Only three of them change what the caller should do.
EOVERQUOTA = -17  # bandwidth spent for this IP
ERATELIMIT = -4  # too many requests, briefly
EAGAIN = -3  # transient, Mega asks for a retry

# What the rest of them mean to someone reading a Telegram message.
MEGA_ERRORS = {
    -1: "Mega had an internal error",
    -2: "malformed handle or key",
    -6: "too many concurrent connections from this IP",
    -9: "the share or file no longer exists",
    -11: "access to this share was denied",
    -13: "the link points at a node of the wrong type",
    -15: "the Mega session expired",
    -16: "the account that owns this file is suspended",
    EAGAIN: "Mega is busy and asked to retry",
    ERATELIMIT: "Mega is rate limiting this IP",
    EOVERQUOTA: "Mega bandwidth quota is exhausted for this IP",
}

API_ATTEMPTS = 4


class MegaApiError(Exception):
    """A negative code from Mega, carrying enough for the caller to decide."""

    def __init__(self, code, context=""):
        self.code = code
        self.context = context
        reason = MEGA_ERRORS.get(code, f"Mega error {code}")
        super().__init__(f"{reason} ({context})" if context else reason)

    @property
    def is_quota(self):
        """Waiting will not help, but a different egress IP will."""
        return self.code in (EOVERQUOTA, ERATELIMIT)

    @property
    def is_transient(self):
        return self.code == EAGAIN


def b64url_decode(value):
    """Decode Mega's unpadded base64url."""
    value = (value or "").replace("-", "+").replace("_", "/")
    return b64decode(value + "=" * (-len(value) % 4))


def b64url_encode(raw):
    return b64encode(raw).decode().replace("+", "-").replace("/", "_").rstrip("=")


def _aes(key, mode):
    return Cipher(algorithms.AES(key), mode).decryptor()


def ecb_decrypt(key, data):
    """Unwrap a node key. ECB is right here precisely because each 16-byte key
    block must decrypt independently of the others."""
    return _aes(key, modes.ECB()).update(data)


def cbc_decrypt(key, data, iv=b"\0" * BLOCK):
    """CBC with no padding removal - attribute blobs are null-padded, not
    PKCS#7, so stripping padding would corrupt or reject them."""
    return _aes(key, modes.CBC(iv)).update(data)


def ctr_decrypt(key, counter, data):
    """AES-CTR from an explicit counter block, so any offset in the file can be
    decrypted without having read what came before it."""
    return _aes(key, modes.CTR(counter)).update(data)


def ctr_stream(key, counter):
    """A CTR context to feed a whole segment through, chunk by chunk.

    ctr_decrypt() derives its counter from a byte offset, which floors to a
    block boundary - correct only if every piece handed to it is a multiple of
    16 bytes. A socket does not promise that, so a stream is decrypted through
    one context that carries the keystream position across reads instead.
    """
    return _aes(key, modes.CTR(counter))


def unpack_file_key(k32):
    """Split a 32-byte file key into (aes_key, nonce, meta_mac).

    The key is folded by XORing its halves; bytes 16..24 double as the CTR
    nonce and 24..32 are the integrity MAC. Folder keys are 16 bytes and are
    used as they are, which is why they never come through here.
    """
    if len(k32) < 32:
        raise ValueError("file key must be 32 bytes")
    key = bytes(a ^ b for a, b in zip(k32[:16], k32[16:32]))
    return key, k32[16:24], k32[24:32]


def counter_at(nonce, offset):
    """The CTR counter block for an absolute byte offset: the 8-byte nonce
    followed by a big-endian block index. Deriving it from the offset rather
    than accumulating it is what makes ranged requests and resume possible."""
    return bytes(nonce[:8]) + (offset // BLOCK).to_bytes(8, "big")


def decrypt_attr(attr_key, blob):
    """Decrypt a node's attribute blob ("MEGA" + JSON, null-padded).

    Returns None rather than raising: this is also the oracle that tells
    recover_node_key whether a candidate key was the right one, and a wrong
    guess is an ordinary outcome there, not an error.
    """
    raw = b64url_decode(blob)
    if not raw or len(raw) % BLOCK:
        return None
    try:
        text = cbc_decrypt(attr_key, raw).rstrip(b"\0").decode("utf-8", "replace")
    except Exception:
        return None
    if not text.startswith("MEGA"):
        return None
    try:
        return loads(text[4:])
    except Exception:
        return None


def recover_node_key(share_key, node):
    """Recover a node's key by trying each "<handle>:<wrapped>" pair against
    the share key. Returns (raw_key, attributes) or None.

    A node carries one pair per share it is reachable through and the handles
    do not say which is which, so every pair is tried and a successful
    attribute decrypt is what proves the guess: a wrong key yields bytes with
    no "MEGA" magic, which decrypt_attr rejects.
    """
    is_file = node.get("t") == 0

    for pair in str(node.get("k") or "").split("/"):
        _, sep, encoded = pair.partition(":")
        if not sep:
            continue

        wrapped = b64url_decode(encoded)
        # 16 = folder key, 32 = file key, 64 = file key with an RSA-wrapped
        # tail we cannot use. Anything else is not a key at all.
        if len(wrapped) not in (16, 32, 64):
            continue

        try:
            raw = ecb_decrypt(share_key, wrapped)
        except Exception:
            continue

        if len(raw) < (32 if is_file else 16):
            continue

        try:
            attr_key = unpack_file_key(raw[:32])[0] if is_file else raw[:16]
        except ValueError:
            continue

        if (attr := decrypt_attr(attr_key, node.get("a"))) and attr.get("n"):
            return raw, attr

    return None


async def api_request(session, payload, node=None, context=""):
    """One call to Mega's API. `session` is an aiohttp session the caller has
    already pointed at whatever proxy it wants.

    EAGAIN is retried here because it means precisely "ask again"; every other
    code is raised, including quota, which the caller answers by changing IP.
    """
    error = None

    for attempt in range(1, API_ATTEMPTS + 1):
        # 'id' is a cache-buster Mega expects to vary per request, not a
        # security parameter. 'n' scopes the call to a folder share.
        params = {"id": randrange(1_000_000_000)}
        if node:
            params["n"] = node

        try:
            # The command goes in a one-element batch. Mega answers a bare
            # object with an empty array rather than an error, so getting this
            # wrong looks like an empty folder instead of a bad request.
            async with session.post(API_URL, params=params, json=[payload]) as resp:
                if resp.status != 200:
                    raise ConnectionError(f"Mega API returned HTTP {resp.status}")
                body = await resp.json(content_type=None)
        except MegaApiError:
            raise
        except Exception as e:
            error = e
            if attempt == API_ATTEMPTS:
                break
            await sleep(2 * attempt)
            continue

        if isinstance(body, int):
            error = MegaApiError(body, context)
            if not error.is_transient or attempt == API_ATTEMPTS:
                raise error
            await sleep(2 * attempt)
            continue

        if isinstance(body, list):
            if not body:
                raise ConnectionError(
                    f"Mega returned an empty batch while {context or 'calling it'}"
                )
            result = body[0]
        else:
            result = body

        if isinstance(result, int):
            error = MegaApiError(result, context)
            if not error.is_transient or attempt == API_ATTEMPTS:
                raise error
            await sleep(2 * attempt)
            continue

        return result

    raise error if isinstance(error, MegaApiError) else ConnectionError(
        f"Mega API unreachable while {context or 'calling it'}: {error}"
    )


def _sorted_nodes(response, share_key):
    """Decrypt every node in a listing into (node, raw_key, attributes).

    Nodes that are not part of this share, or whose key is RSA-wrapped for a
    specific account, simply do not decrypt - they are skipped rather than
    failing the listing, because the rest of the share is still downloadable.
    """
    for node in response.get("f") or []:
        if not node.get("k"):
            continue
        if found := recover_node_key(share_key, node):
            yield node, found[0], found[1]


def _safe_name(name, fallback):
    """Node names come from the link's owner, so they are untrusted input that
    ends up in a filesystem path: drop separators and traversal."""
    cleaned = (name or "").replace("/", "_").replace("\\", "_").strip().strip(".")
    return cleaned or fallback


async def list_folder(session, handle, key, node=None):
    """List a folder share as a flat list of files, each with the relative path
    it should be written to.

    Returns {"name": str, "files": [{handle, name, path, size, key}]}. The tree
    is flattened here because the caller only ever needs "where does this file
    go" - it recreates directories from `path`.
    """
    share_key = b64url_decode(key)[:16]
    if len(share_key) != 16:
        raise ValueError("folder links need a 16-byte key")

    response = await api_request(
        session,
        {"a": "f", "c": 1, "r": 1, "ca": 1},
        node=node or handle,
        context="listing the folder",
    )

    files, folders = [], {}

    for node_data, raw_key, attr in _sorted_nodes(response, share_key):
        kind = node_data.get("t")
        node_handle = node_data.get("h")
        name = _safe_name(attr.get("n"), node_handle or "unnamed")

        if kind == 1:  # directory
            folders[node_handle] = (name, node_data.get("p"))
        elif kind == 0:  # file
            files.append(
                {
                    "handle": node_handle,
                    "name": name,
                    "parent": node_data.get("p"),
                    "size": int(node_data.get("s") or 0),
                    "key": raw_key[:32],
                }
            )

    # The share's root is the folder whose own parent is not in the listing -
    # the handle in the URL is not reliably it. A share of loose files has no
    # such folder, and then every path is already relative to the root.
    tops = [h for h, (_, parent) in folders.items() if parent not in folders]
    root = tops[0] if len(tops) == 1 else None


    # Resolve each file's directory chain into a relative path, stopping at the
    # share root so the tree is not nested under an absolute-looking prefix.
    for item in files:
        parts, parent, guard = [], item.pop("parent"), 0
        while parent in folders and parent != root and guard < 64:
            folder_name, grandparent = folders[parent]
            parts.append(folder_name)
            parent, guard = grandparent, guard + 1
        item["path"] = "/".join(reversed(parts))

    if not files:
        raise ValueError("no downloadable files found in this Mega folder")

    root_name = folders.get(root, (None,))[0] if root else None
    return {"name": root_name or handle, "files": files}


async def resolve_file(session, file_handle, folder=None):
    """Trade a file handle for a CDN URL and the ciphertext size.

    The URL is short-lived and tied to the IP that asked for it, so it is
    resolved just before the download and re-resolved after any IP change.
    """
    if folder:
        # Inside a folder share the file is addressed by node handle, and the
        # share handle scopes the call.
        payload, node = {"a": "g", "g": 1, "n": file_handle}, folder
    else:
        # A public single-file link addresses it by the link's own handle.
        payload, node = {"a": "g", "g": 1, "p": file_handle}, None

    result = await api_request(
        session, payload, node=node, context="resolving the download URL"
    )

    # A per-file refusal arrives inside the object rather than as a bare code,
    # so quota on one file of a folder does not look like a listing failure.
    if code := result.get("e"):
        raise MegaApiError(int(code), "resolving the download URL")
    if not result.get("g"):
        raise ConnectionError("Mega returned no download URL")

    return result["g"], int(result.get("s") or 0)


async def file_info(session, handle, key):
    """Name and size for a public single-file link.

    The attributes come back from the same 'g' call that resolves the URL, so
    this costs nothing extra beyond decrypting them.
    """
    file_key = b64url_decode(key)
    if len(file_key) < 32:
        raise ValueError("file links need a 32-byte key")

    result = await api_request(
        session,
        {"a": "g", "p": handle},
        context="reading the file's attributes",
    )
    if code := result.get("e"):
        raise MegaApiError(int(code), "reading the file's attributes")

    attr = decrypt_attr(unpack_file_key(file_key)[0], result.get("at")) or {}
    return {
        "handle": handle,
        "name": _safe_name(attr.get("n"), handle),
        "path": "",
        "size": int(result.get("s") or 0),
        "key": file_key[:32],
    }

