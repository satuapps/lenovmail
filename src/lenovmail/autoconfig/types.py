# Lenovmail — authored by satuapps (satuapps.com)
"""Result types for mail configuration auto-detection."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

Security = Literal["ssl", "starttls", "none"]
AuthKind = Literal["password", "oauth2"]
ProviderKind = Literal["imap", "graph"]

SECURITY_VALUES: tuple[str, ...] = ("ssl", "starttls", "none")


@dataclass(frozen=True, slots=True)
class ServerSpec:
    host: str
    port: int
    security: Security
    # Mechanism the Lenovmail client will use, not a provider policy.
    auth: AuthKind = "password"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> ServerSpec | None:
        if not data:
            return None
        return cls(
            host=data["host"],
            port=int(data["port"]),
            security=data["security"],
            auth=data.get("auth", "password"),
        )


@dataclass(slots=True)
class DiscoveryResult:
    """Result of the detection chain. `provider` decides which connector is used."""

    source: str
    provider: ProviderKind = "imap"
    imap: ServerSpec | None = None
    smtp: ServerSpec | None = None
    # True if credentials must be OAuth (or a dedicated app password), not the account password.
    oauth_required: bool = False
    note: str | None = None

    @property
    def usable(self) -> bool:
        """Result is considered valid when the IMAP provider has incoming settings, or Graph."""
        return self.provider == "graph" or self.imap is not None

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "provider": self.provider,
            "imap": self.imap.to_dict() if self.imap else None,
            "smtp": self.smtp.to_dict() if self.smtp else None,
            "oauth_required": self.oauth_required,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> DiscoveryResult:
        return cls(
            source=data["source"],
            provider=data.get("provider", "imap"),
            imap=ServerSpec.from_dict(data.get("imap")),
            smtp=ServerSpec.from_dict(data.get("smtp")),
            oauth_required=bool(data.get("oauth_required", False)),
            note=data.get("note"),
        )


def domain_of(email: str) -> str:
    """Lowercase domain from an email address; raises ValueError if malformed."""
    email = (email or "").strip()
    if email.count("@") != 1:
        raise ValueError(f"invalid email address: {email!r}")
    local, _, domain = email.partition("@")
    domain = domain.strip().lower().rstrip(".")
    labels = domain.split(".")
    # The domain is used for DNS lookups and cache keys, so an empty label (e.g.
    # `a@.com`) must be rejected here rather than left for the resolver.
    if not local or len(labels) < 2 or any(not label for label in labels):
        raise ValueError(f"invalid email address: {email!r}")
    return domain


def registrable_domain(host: str) -> str:
    """Last two labels of a hostname.

    Naive about multi-part TLDs (e.g. `example.co.uk` -> `co.uk`). Good enough for
    mapping an MX target to a provider domain (`mx1.mail.host.com` -> `host.com`).
    """
    labels = [label for label in host.strip().lower().rstrip(".").split(".") if label]
    return ".".join(labels[-2:]) if len(labels) >= 2 else ".".join(labels)
