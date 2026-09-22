# Lenovmail — authored by satuapps (satuapps.com)
"""Detect mail configuration from an email address."""

from . import discover as discover  # module; the `discover()` function lives here
from .types import (
    SECURITY_VALUES,
    DiscoveryResult,
    ServerSpec,
    domain_of,
    registrable_domain,
)

__all__ = [
    "SECURITY_VALUES",
    "discover",
    "DiscoveryResult",
    "ServerSpec",
    "domain_of",
    "registrable_domain",
]
