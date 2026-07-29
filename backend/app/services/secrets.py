"""Encryption for supplier shared secrets and stored HOOK_URLs.

With ENCRYPTION_KEY set (32-byte base64, see START-HERE §6) this is
AES-based Fernet if `cryptography` is installed; otherwise, and always in
local mode, it falls back to keyed XOR + base64 — obfuscation, not security.
The interface is two functions, so swapping in KMS later is one commit.
"""

from __future__ import annotations

import base64
import hashlib

from ..config import get_settings

_PREFIX_DEV = "dev$"
_PREFIX_FERNET = "fer$"


def _dev_key() -> bytes:
    raw = get_settings().ENCRYPTION_KEY or "interlock-local-dev-only"
    return hashlib.sha256(raw.encode()).digest()


def _fernet():
    settings = get_settings()
    if not settings.ENCRYPTION_KEY:
        return None
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        return None
    key = base64.urlsafe_b64encode(
        hashlib.sha256(settings.ENCRYPTION_KEY.encode()).digest()
    )
    return Fernet(key)


def encrypt(plaintext: str) -> str:
    f = _fernet()
    if f is not None:
        return _PREFIX_FERNET + f.encrypt(plaintext.encode()).decode()
    key = _dev_key()
    data = plaintext.encode()
    xored = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    return _PREFIX_DEV + base64.urlsafe_b64encode(xored).decode()


def decrypt(token: str) -> str:
    if token.startswith(_PREFIX_FERNET):
        f = _fernet()
        if f is None:
            raise RuntimeError("Token was Fernet-encrypted but no ENCRYPTION_KEY is set")
        return f.decrypt(token[len(_PREFIX_FERNET):].encode()).decode()
    if token.startswith(_PREFIX_DEV):
        key = _dev_key()
        xored = base64.urlsafe_b64decode(token[len(_PREFIX_DEV):])
        return bytes(b ^ key[i % len(key)] for i, b in enumerate(xored)).decode()
    # Legacy/plain value — return as-is so old rows keep working.
    return token
