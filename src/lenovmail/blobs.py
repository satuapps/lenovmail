# Lenovmail — authored by satuapps
"""Raw MIME blob storage: content-addressed, gzip, atomic writes.

Layout: `BLOB_ROOT/<hex[0:2]>/<hex[2:4]>/<hex>.eml.gz`.

Content is addressed by the sha256 of the *raw* MIME bytes (not the gzip output), so
two folders/accounts that contain an identical message share a single file. Because of
this, a blob cannot be deleted just because one message was deleted; v1 never deletes
blobs.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import os
from pathlib import Path

from .config import settings
from .logging import get_logger

log = get_logger(__name__)

_COMPRESS_LEVEL = 6


def root() -> Path:
    return Path(settings.blob_root)


def path_for(digest: bytes) -> Path:
    hexd = digest.hex()
    return root() / hexd[:2] / hexd[2:4] / f"{hexd}.eml.gz"


def digest_of(raw: bytes) -> bytes:
    return hashlib.sha256(raw).digest()


def _put_sync(raw: bytes, digest: bytes) -> None:
    target = path_for(digest)
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + f".tmp{os.getpid()}")
    try:
        with gzip.open(tmp, "wb", compresslevel=_COMPRESS_LEVEL) as fh:
            fh.write(raw)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def _get_sync(digest: bytes) -> bytes:
    with gzip.open(path_for(digest), "rb") as fh:
        return fh.read()


async def put(raw: bytes) -> bytes:
    """Store `raw`; return the raw sha256. Idempotent for the same digest."""
    digest = digest_of(raw)
    await asyncio.to_thread(_put_sync, raw, digest)
    return digest


async def put_known(digest: bytes, raw: bytes) -> bytes:
    """Like `put` but the digest was already computed by the caller (avoids double hashing)."""
    await asyncio.to_thread(_put_sync, raw, digest)
    return digest


async def get(digest: bytes) -> bytes:
    """Read back the raw MIME. `FileNotFoundError` if the blob is missing from disk."""
    return await asyncio.to_thread(_get_sync, digest)


async def exists(digest: bytes) -> bool:
    return await asyncio.to_thread(path_for(digest).exists)
