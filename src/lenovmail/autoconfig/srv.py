# Lenovmail — authored by satuapps (satuapps.com)
"""RFC 6186 SRV lookup (`_imaps._tcp`, `_submission._tcp`, etc).

Important note: an SRV target of `"."` (root) means the service is **explicitly
unavailable** — not "no record". Fastmail, for example, publishes
`_submission._tcp.fastmail.com -> 0 0 0 .`, meaning don't guess SMTP from SRV.
"""

from __future__ import annotations

from dataclasses import dataclass

import dns.asyncresolver
import dns.exception
import dns.resolver

from ..logging import get_logger

log = get_logger(__name__)

_DNS_TIMEOUT_S = 4.0

# Priority order per protocol (RFC 6186 service names).
IMAP_SERVICES = ("_imaps._tcp", "_imap._tcp")
SMTP_SERVICES = ("_submissions._tcp", "_submission._tcp")


@dataclass(frozen=True, slots=True)
class SrvTarget:
    """Result of a single SRV lookup.

    `available=False` means the record exists but points to `"."` (service disabled).
    """

    available: bool
    host: str | None = None
    port: int | None = None
    priority: int = 0
    weight: int = 0


async def lookup(service: str, domain: str) -> SrvTarget | None:
    """A single SRV lookup. `None` = record doesn't exist / resolver failed (not negative)."""
    name = f"{service}.{domain}"
    resolver = dns.asyncresolver.Resolver()
    resolver.lifetime = _DNS_TIMEOUT_S
    resolver.timeout = _DNS_TIMEOUT_S
    try:
        answer = await resolver.resolve(name, "SRV")
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
        return None
    except (dns.exception.Timeout, dns.exception.DNSException) as exc:
        log.debug("srv_lookup_failed", name=name, error=str(exc))
        return None

    records = sorted(
        answer,
        key=lambda r: (r.priority, -r.weight, str(r.target)),
    )
    if not records:
        return None

    best = records[0]
    target = str(best.target).rstrip(".")
    if target in ("", "."):
        return SrvTarget(available=False, priority=best.priority, weight=best.weight)
    # Some providers publish port 0 as a form of "unavailable".
    if best.port == 0:
        return SrvTarget(available=False, priority=best.priority, weight=best.weight)
    return SrvTarget(
        available=True,
        host=target,
        port=best.port,
        priority=best.priority,
        weight=best.weight,
    )


async def lookup_first(services: tuple[str, ...], domain: str) -> tuple[str, SrvTarget] | None:
    """Try each service in order; return the (service, result) of the first record found.

    A result with `available=False` is still returned (the caller must stop guessing
    for that protocol) and is not treated as "try the next service".
    """
    for service in services:
        found = await lookup(service, domain)
        if found is not None:
            return service, found
    return None
