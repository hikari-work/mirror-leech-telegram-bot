from base64 import urlsafe_b64encode
from hashlib import scrypt
from os import getenv

from cryptography.fernet import Fernet, InvalidToken

from ... import LOGGER

# Prefix written ahead of every ciphertext so that a blob stored before
# encryption was enabled can still be read back verbatim.
_MAGIC = b"MLTB1:"

KEY_VAR = "DB_ENCRYPTION_KEY"


def _build_key() -> bytes | None:
    """Derive a Fernet key from the DB_ENCRYPTION_KEY env var, or None if unset.

    Read straight from the environment on purpose: it must never become a Config
    attribute, since Config.get_all() and update_deploy_config() both persist
    their contents to Mongo, which would store the key next to the data it
    protects.

    scrypt lets the operator use any passphrase instead of having to generate a
    32-byte urlsafe-base64 value by hand. The salt is fixed because the same
    passphrase must yield the same key on every boot, otherwise previously
    stored blobs become unreadable.
    """
    secret = (getenv(KEY_VAR) or "").strip()
    if not secret:
        return None
    raw = scrypt(secret.encode(), salt=b"mltb-blob-v1", n=2**14, r=8, p=1, dklen=32)
    return urlsafe_b64encode(raw)


class _Box:
    def __init__(self):
        self._fernet = None
        self._loaded = False

    @property
    def enabled(self) -> bool:
        self._load()
        return self._fernet is not None

    def _load(self):
        if self._loaded:
            return
        self._loaded = True
        if key := _build_key():
            self._fernet = Fernet(key)
        else:
            LOGGER.warning(
                f"{KEY_VAR} is not set, private files (cookies.txt, .netrc, "
                "config.py) are stored unencrypted!"
            )

    def encrypt(self, data: bytes) -> bytes:
        self._load()
        if self._fernet is None:
            return data
        return _MAGIC + self._fernet.encrypt(data)

    def decrypt(self, data: bytes) -> bytes:
        if not data.startswith(_MAGIC):
            # Stored before encryption was turned on.
            return data
        self._load()
        if self._fernet is None:
            LOGGER.error(
                f"An encrypted blob was found but {KEY_VAR} is not set, skipping it"
            )
            return b""
        try:
            return self._fernet.decrypt(data[len(_MAGIC) :])
        except InvalidToken:
            LOGGER.error(
                f"Failed to decrypt a stored blob, {KEY_VAR} likely changed"
            )
            return b""


blob_box = _Box()
