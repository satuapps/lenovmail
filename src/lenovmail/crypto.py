# Lenovmail — authored by satuapps (satuapps.com)
"""Encryption for stored credentials: AES-256-GCM with an AAD binding row+column.

Ciphertext format: `b"\\x01" + nonce(12 bytes) + ciphertext+tag`.
Version byte 1 is the only known version.

The AAD is built as `f"{table}:{row_id}:{field}"` so ciphertext cannot be moved
between rows or columns — moving it makes decryption fail (InvalidTag).

The key comes from `LENOVMAIL_SECRET_KEY` (base64 of exactly 32 bytes). There is no key
rotation in v1: changing the key makes old credentials unreadable.
"""

from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import settings

_VERSION = 1
_NONCE_BYTES = 12
_KEY_BYTES = 32


class SecretKeyError(ValueError):
    """`LENOVMAIL_SECRET_KEY` is missing, not base64, or not 32 bytes long."""


def load_key(secret_key: str | None = None) -> bytes:
    raw = settings.secret_key if secret_key is None else secret_key
    if not raw:
        raise SecretKeyError(
            "LENOVMAIL_SECRET_KEY is not set. Generate one with: "
            'python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"'
        )
    try:
        key = base64.b64decode(raw, validate=True)
    except Exception as exc:  # binascii.Error and its subclasses
        raise SecretKeyError("LENOVMAIL_SECRET_KEY is not valid base64") from exc
    if len(key) != _KEY_BYTES:
        raise SecretKeyError(f"LENOVMAIL_SECRET_KEY must be 32 bytes after base64 (got {len(key)})")
    return key


def generate_key() -> str:
    """Base64 of 32 random bytes — used by the CLI `init-secret` command and tests."""
    return base64.b64encode(os.urandom(_KEY_BYTES)).decode()


def encrypt(aad: str, plaintext: bytes, *, key: bytes | None = None) -> bytes:
    nonce = os.urandom(_NONCE_BYTES)
    sealed = AESGCM(key or load_key()).encrypt(nonce, plaintext, aad.encode())
    return bytes([_VERSION]) + nonce + sealed


def decrypt(aad: str, blob: bytes, *, key: bytes | None = None) -> bytes:
    if not blob:
        raise ValueError("ciphertext is empty")
    if blob[0] != _VERSION:
        raise ValueError(f"unknown ciphertext version: {blob[0]}")
    nonce, sealed = blob[1 : 1 + _NONCE_BYTES], blob[1 + _NONCE_BYTES :]
    if len(nonce) != _NONCE_BYTES or not sealed:
        raise ValueError("ciphertext is truncated")
    try:
        return AESGCM(key or load_key()).decrypt(nonce, sealed, aad.encode())
    except InvalidTag as exc:
        raise ValueError("decryption failed: ciphertext corrupted or AAD/key mismatch") from exc


def account_aad(account_id: object, field: str) -> str:
    """AAD for a credential column that hangs off a single account."""
    return f"accounts:{account_id}:{field}"
