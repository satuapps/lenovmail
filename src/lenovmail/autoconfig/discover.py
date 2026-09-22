# Lenovmail — authored by satuapps (satuapps.com)
"""Auto-detection chain for mail configuration from an email address.

Order (stops at the first usable result), each step times out after 4 seconds and
the total is capped at 15 seconds:

1. built-in override from `providers.yaml`
2. `https://autoconfig.<domain>/mail/config-v1.1.xml?emailaddress=<email>` (falls back to http)
3. `https://<domain>/.well-known/autoconfig/mail/config-v1.1.xml` (falls back to http)
4. ISPDB `https://autoconfig.thunderbird.net/v1.1/<domain>`
5. MX: target `*.mail.protection.outlook.com` -> Graph provider; otherwise retry ISPDB
   with the registrable domain of the MX target
6. RFC 6186 SRV (`_imaps._tcp` then `_imap._tcp`); target `.` means the service is
   disabled — SMTP is then taken from ISPDB then probing, never guessed from SRV
7. heuristic probing (see `probe.py`)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import dns.asyncresolver
import dns.exception
import dns.resolver
import httpx
import yaml
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..logging import get_logger
from ..models import AutoconfigCache
from .ispdb import parse_mozilla_config
from .probe import probe_imap, probe_smtp
from .srv import IMAP_SERVICES, SMTP_SERVICES, lookup_first
from .types import DiscoveryResult, ServerSpec, domain_of, registrable_domain

log = get_logger(__name__)

CHAIN_TIMEOUT_S = 15.0
HTTP_TIMEOUT_S = 4.0
DNS_TIMEOUT_S = 4.0
CACHE_TTL = timedelta(days=7)

ISPDB_URL = "https://autoconfig.thunderbird.net/v1.1/{domain}"
OUTLOOK_MX_SUFFIX = ".mail.protection.outlook.com"

_OVERRIDES_PATH = Path(__file__).with_name("providers.yaml")


@dataclass(frozen=True, slots=True)
class _Override:
    domains: frozenset[str]
    result: DiscoveryResult


@lru_cache(maxsize=1)
def load_overrides() -> tuple[_Override, ...]:
    """Read `providers.yaml` once per process."""
    data = yaml.safe_load(_OVERRIDES_PATH.read_text(encoding="utf-8")) or {}
    out: list[_Override] = []
    for entry in data.get("providers", []):
        result = DiscoveryResult(
            source="override",
            provider=entry.get("provider", "imap"),
            imap=ServerSpec.from_dict(entry.get("imap")),
            smtp=ServerSpec.from_dict(entry.get("smtp")),
            oauth_required=bool(entry.get("oauth_required", False)),
            note=entry.get("note"),
        )
        out.append(
            _Override(
                domains=frozenset(d.lower() for d in entry.get("domains", [])),
                result=result,
            )
        )
    return tuple(out)


def override_for(domain: str) -> DiscoveryResult | None:
    for entry in load_overrides():
        if domain in entry.domains:
            return entry.result
    return None


async def _fetch_text(url: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S, follow_redirects=True) as client:
            response = await client.get(url)
    except (httpx.HTTPError, OSError) as exc:
        log.debug("fetch_failed", url=url, error=type(exc).__name__)
        return None
    if response.status_code != 200 or not response.text.strip():
        return None
    return response.text


async def _fetch_config(urls: list[str], email: str) -> DiscoveryResult | None:
    """Try each URL in order; a valid XML response with an IMAP server -> result."""
    for url in urls:
        text = await _fetch_text(url)
        if text is None:
            continue
        parsed = parse_mozilla_config(text, email)
        if parsed.imap is None:
            continue
        return DiscoveryResult(
            source=url,
            provider="imap",
            imap=parsed.imap,
            smtp=parsed.smtp,
            oauth_required=parsed.oauth_required,
            note=(
                "This provider requires OAuth2/app password; a regular account password "
                "will be rejected."
                if parsed.oauth_required
                else None
            ),
        )
    return None


def _autoconfig_urls(domain: str, email: str) -> list[str]:
    query = f"?emailaddress={email}"
    return [
        f"https://autoconfig.{domain}/mail/config-v1.1.xml{query}",
        f"http://autoconfig.{domain}/mail/config-v1.1.xml{query}",
    ]


def _wellknown_urls(domain: str) -> list[str]:
    return [
        f"https://{domain}/.well-known/autoconfig/mail/config-v1.1.xml",
        f"http://{domain}/.well-known/autoconfig/mail/config-v1.1.xml",
    ]


async def _ispdb(domain: str, email: str) -> DiscoveryResult | None:
    return await _fetch_config([ISPDB_URL.format(domain=domain)], email)


async def _mx_targets(domain: str) -> list[str]:
    resolver = dns.asyncresolver.Resolver()
    resolver.lifetime = DNS_TIMEOUT_S
    resolver.timeout = DNS_TIMEOUT_S
    try:
        answer = await resolver.resolve(domain, "MX")
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        return []
    except (dns.exception.Timeout, dns.exception.DNSException) as exc:
        log.debug("mx_lookup_failed", domain=domain, error=str(exc))
        return []
    return sorted(str(r.exchange).rstrip(".").lower() for r in answer)


async def _step_mx(domain: str, email: str) -> DiscoveryResult | None:
    targets = await _mx_targets(domain)
    if not targets:
        return None
    if any(t.endswith(OUTLOOK_MX_SUFFIX) for t in targets):
        return DiscoveryResult(
            source="mx",
            provider="graph",
            oauth_required=True,
            note=(
                "This domain is served by Microsoft 365 (MX *.mail.protection.outlook.com); "
                "connect via the Microsoft button."
            ),
        )
    mx_domain = registrable_domain(targets[0])
    if mx_domain and mx_domain != domain:
        found = await _ispdb(mx_domain, email)
        if found is not None:
            return found
    return None


async def _step_srv(domain: str, email: str) -> DiscoveryResult | None:
    imap_hit = await lookup_first(IMAP_SERVICES, domain)
    smtp_hit = await lookup_first(SMTP_SERVICES, domain)

    imap: ServerSpec | None = None
    note: str | None = None
    imap_source: str | None = None
    if imap_hit is not None:
        service, target = imap_hit
        if target.available and target.host and target.port:
            imap_source = service
            imap = ServerSpec(
                host=target.host,
                port=target.port,
                security="ssl" if service == "_imaps._tcp" else "starttls",
                auth="password",
            )
    if imap is None:
        return None

    smtp: ServerSpec | None = None
    if smtp_hit is not None:
        service, target = smtp_hit
        if target.available and target.host and target.port:
            smtp = ServerSpec(
                host=target.host,
                port=target.port,
                security="ssl" if service == "_submissions._tcp" else "starttls",
                auth="password",
            )
        else:
            note = (
                f"SRV {service}.{domain} marks SMTP as unavailable; "
                "SMTP settings are taken from ISPDB/probe instead."
            )
    if smtp is None:
        from_ispdb = await _ispdb(domain, email)
        if from_ispdb is not None and from_ispdb.smtp is not None:
            smtp = from_ispdb.smtp
        else:
            smtp = await probe_smtp(domain)

    return DiscoveryResult(
        source=f"srv:{imap_source}",
        provider="imap",
        imap=imap,
        smtp=smtp,
        oauth_required=False,
        note=note,
    )


async def _step_probe(domain: str) -> DiscoveryResult | None:
    imap, smtp = await probe_imap(domain), await probe_smtp(domain)
    if imap is None:
        return None
    return DiscoveryResult(
        source="probe",
        provider="imap",
        imap=imap,
        smtp=smtp,
        oauth_required=False,
        note="Settings guessed by connecting directly to the server; verify before saving.",
    )


async def _run_chain(email: str, domain: str) -> DiscoveryResult:
    override = override_for(domain)
    if override is not None:
        return override

    # Stored as callables so steps after the winner are never constructed.
    steps = [
        lambda: _fetch_config(_autoconfig_urls(domain, email), email),
        lambda: _fetch_config(_wellknown_urls(domain), email),
        lambda: _ispdb(domain, email),
        lambda: _step_mx(domain, email),
        lambda: _step_srv(domain, email),
        lambda: _step_probe(domain),
    ]
    for step in steps:
        found = await step()
        if found is not None and found.usable:
            return found

    return DiscoveryResult(
        source="none",
        note=("Settings could not be auto-detected. Fill in the IMAP and SMTP host/port manually."),
    )


async def _load_cache(session: AsyncSession, domain: str) -> DiscoveryResult | None:
    row = await session.get(AutoconfigCache, domain)
    if row is None:
        return None
    fetched = row.fetched_at
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=UTC)
    if datetime.now(UTC) - fetched > CACHE_TTL:
        return None
    payload: dict[str, Any] = row.result or {}
    if not payload.get("found"):
        return None
    return DiscoveryResult.from_dict(payload)


async def _save_cache(session: AsyncSession, domain: str, result: DiscoveryResult) -> None:
    payload: dict[str, Any] = {"found": result.source != "none", **result.to_dict()}
    statement = (
        pg_insert(AutoconfigCache)
        .values(
            domain=domain,
            result=payload,
            source=result.source,
            fetched_at=datetime.now(UTC),
        )
        .on_conflict_do_update(
            index_elements=[AutoconfigCache.domain],
            set_={
                "result": payload,
                "source": result.source,
                "fetched_at": datetime.now(UTC),
            },
        )
    )
    await session.execute(statement)
    await session.commit()


async def discover(email: str, session: AsyncSession | None = None) -> DiscoveryResult:
    """Detect settings for `email`.

    `session` is optional: when given, the result is read from/written to
    `autoconfig_cache` (including negative results, so a domain with no settings
    isn't probed over and over).
    """
    domain = domain_of(email)

    if session is not None:
        cached = await _load_cache(session, domain)
        if cached is not None:
            log.debug("autoconfig_cache_hit", domain=domain, source=cached.source)
            return cached

    try:
        async with asyncio.timeout(CHAIN_TIMEOUT_S):
            result = await _run_chain(email, domain)
    except TimeoutError:
        result = DiscoveryResult(
            source="none", note=f"Detection exceeded {CHAIN_TIMEOUT_S:.0f} seconds."
        )

    if session is not None:
        await _save_cache(session, domain, result)
    return result


async def discover_cached_only(session: AsyncSession, email: str) -> DiscoveryResult | None:
    return await _load_cache(session, domain_of(email))


__all__ = [
    "CHAIN_TIMEOUT_S",
    "DiscoveryResult",
    "discover",
    "discover_cached_only",
    "load_overrides",
    "override_for",
]
