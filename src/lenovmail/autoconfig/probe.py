# Lenovmail — authored by satuapps (satuapps.com)
"""Heuristic probing: guess IMAP/SMTP hosts by connecting and reading the banner.

A probe is only considered successful if the server actually answers as expected:
IMAP starts with `* OK`/`* PREAUTH` (and for STARTTLS, the `STARTTLS` candidate
appears in CAPABILITY), SMTP starts with `220` (and `STARTTLS` appears in EHLO for
the STARTTLS candidate).

All candidates are tried in parallel; the one chosen is the first by priority
order, not the one that answers fastest.
"""

from __future__ import annotations

import asyncio
import contextlib
import ssl
from dataclasses import dataclass

from ..logging import get_logger
from .types import Security, ServerSpec

log = get_logger(__name__)

PROBE_TIMEOUT_S = 4.0
_MAX_BANNER_LINES = 3


@dataclass(frozen=True, slots=True)
class Candidate:
    host: str
    port: int
    security: Security


def imap_candidates(domain: str) -> list[Candidate]:
    return [
        Candidate(f"imap.{domain}", 993, "ssl"),
        Candidate(f"mail.{domain}", 993, "ssl"),
        Candidate(domain, 993, "ssl"),
        Candidate(f"imap.{domain}", 143, "starttls"),
        Candidate(f"mail.{domain}", 143, "starttls"),
        Candidate(domain, 143, "starttls"),
    ]


def smtp_candidates(domain: str) -> list[Candidate]:
    return [
        Candidate(f"smtp.{domain}", 465, "ssl"),
        Candidate(f"mail.{domain}", 465, "ssl"),
        Candidate(domain, 465, "ssl"),
        Candidate(f"smtp.{domain}", 587, "starttls"),
        Candidate(f"mail.{domain}", 587, "starttls"),
        Candidate(domain, 587, "starttls"),
    ]


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    return ctx


async def _read_banner(reader: asyncio.StreamReader, prefix: bytes) -> list[bytes]:
    """Read up to `_MAX_BANNER_LINES` lines; return the lines read so far."""
    lines: list[bytes] = []
    for _ in range(_MAX_BANNER_LINES):
        line = await asyncio.wait_for(reader.readline(), timeout=PROBE_TIMEOUT_S)
        if not line:
            break
        lines.append(line)
        if line.startswith(prefix):
            break
    return lines


async def _probe_imap(candidate: Candidate) -> bool:
    use_tls = candidate.security == "ssl"
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(
            candidate.host,
            candidate.port,
            ssl=_ssl_context() if use_tls else None,
            server_hostname=candidate.host if use_tls else None,
        ),
        timeout=PROBE_TIMEOUT_S,
    )
    try:
        lines = await _read_banner(reader, b"* OK")
        greeting = b"".join(lines)
        if not (greeting.startswith(b"* OK") or greeting.startswith(b"* PREAUTH")):
            return False
        if candidate.security != "starttls":
            return True
        writer.write(b"a1 CAPABILITY\r\n")
        await asyncio.wait_for(writer.drain(), timeout=PROBE_TIMEOUT_S)
        payload = b""
        for _ in range(12):
            line = await asyncio.wait_for(reader.readline(), timeout=PROBE_TIMEOUT_S)
            if not line:
                break
            payload += line
            if line.startswith(b"a1 "):
                break
        return b"STARTTLS" in payload.upper()
    finally:
        writer.close()
        with contextlib.suppress(TimeoutError, OSError):
            await asyncio.wait_for(writer.wait_closed(), timeout=2)


async def _probe_smtp(candidate: Candidate) -> bool:
    use_tls = candidate.security == "ssl"
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(
            candidate.host,
            candidate.port,
            ssl=_ssl_context() if use_tls else None,
            server_hostname=candidate.host if use_tls else None,
        ),
        timeout=PROBE_TIMEOUT_S,
    )
    try:
        lines = await _read_banner(reader, b"220")
        greeting = b"".join(lines)
        if not greeting.startswith(b"220"):
            return False
        if candidate.security != "starttls":
            return True
        writer.write(b"EHLO probe.lenovmail.invalid\r\n")
        await asyncio.wait_for(writer.drain(), timeout=PROBE_TIMEOUT_S)
        payload = b""
        for _ in range(20):
            line = await asyncio.wait_for(reader.readline(), timeout=PROBE_TIMEOUT_S)
            if not line:
                break
            payload += line
            # The last line of an EHLO reply looks like "250 <text>" (space, not dash).
            if line.startswith(b"250 ") or line[:4] in (b"421 ", b"550 "):
                break
        return b"STARTTLS" in payload.upper()
    finally:
        writer.close()
        with contextlib.suppress(TimeoutError, OSError):
            await asyncio.wait_for(writer.wait_closed(), timeout=2)


async def _probe_many(candidates: list[Candidate], prober) -> Candidate | None:
    if not candidates:
        return None

    async def guarded(candidate: Candidate) -> bool:
        try:
            return await prober(candidate)
        except (TimeoutError, OSError, ssl.SSLError) as exc:
            log.debug(
                "probe_failed",
                host=candidate.host,
                port=candidate.port,
                error=type(exc).__name__,
            )
            return False

    results = await asyncio.gather(*(guarded(c) for c in candidates))
    for candidate, ok in zip(candidates, results, strict=True):
        if ok:
            return candidate
    return None


async def probe_imap(domain: str) -> ServerSpec | None:
    hit = await _probe_many(imap_candidates(domain), _probe_imap)
    if hit is None:
        return None
    return ServerSpec(host=hit.host, port=hit.port, security=hit.security, auth="password")


async def probe_smtp(domain: str) -> ServerSpec | None:
    hit = await _probe_many(smtp_candidates(domain), _probe_smtp)
    if hit is None:
        return None
    return ServerSpec(host=hit.host, port=hit.port, security=hit.security, auth="password")
