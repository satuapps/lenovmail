# Lenovmail — authored by satuapps (satuapps.com)
"""Generic SMTP client for outbox delivery.

Used for `provider='imap'` accounts (Graph accounts use `/me/sendMail`). Protocol errors
are mapped to `MailAuthError`/`MailTransientError`/`MailPermanentError` from
`providers.imap_pool` — those types are protocol-neutral even though they live in the IMAP
module, so the outbox layer doesn't need to distinguish the failure's origin (IMAP or SMTP).

Passwords are NEVER logged.
"""

from __future__ import annotations

import contextlib
import ssl
from dataclasses import dataclass

import aiosmtplib
from aiosmtplib.errors import (
    SMTPAuthenticationError,
    SMTPConnectError,
    SMTPException,
    SMTPRecipientsRefused,
    SMTPResponseException,
    SMTPServerDisconnected,
    SMTPTimeoutError,
)

from ..logging import get_logger
from .imap_pool import ALLOWED_SECURITY, MailAuthError, MailPermanentError, MailTransientError

log = get_logger(__name__)


@dataclass(slots=True)
class SmtpConfig:
    host: str
    port: int
    security: str  # 'ssl' | 'starttls' | 'none'
    username: str | None = None
    password: str | None = None
    timeout_s: int = 30


def _validate(config: SmtpConfig) -> None:
    if config.security not in ALLOWED_SECURITY:
        raise MailPermanentError(f"unrecognized SMTP security mode: {config.security}")


def _new_client(config: SmtpConfig) -> aiosmtplib.SMTP:
    """SMTP client for the configured security mode: direct TLS (`ssl`) or explicit `starttls`."""
    _validate(config)
    return aiosmtplib.SMTP(
        hostname=config.host,
        port=config.port,
        use_tls=config.security == "ssl",
        start_tls=config.security == "starttls",
        timeout=config.timeout_s,
    )


def _translate_error(exc: Exception) -> Exception:
    """Map an `aiosmtplib` error to a domain type (see module docstring)."""
    if isinstance(exc, SMTPAuthenticationError):
        return MailAuthError(str(exc) or "SMTP authentication rejected")
    if isinstance(exc, SMTPRecipientsRefused):
        detail = "; ".join(f"{r.recipient}: {r.code} {r.message}" for r in exc.recipients)
        return MailPermanentError(f"recipient rejected: {detail}")
    if isinstance(exc, SMTPResponseException):
        if exc.code >= 500:
            return MailPermanentError(f"{exc.code}: {exc.message}")
        return MailTransientError(f"{exc.code}: {exc.message}")
    if isinstance(exc, (SMTPTimeoutError, SMTPConnectError, SMTPServerDisconnected)):
        return MailTransientError(str(exc) or "SMTP connection failed")
    if isinstance(exc, SMTPException):
        return MailTransientError(str(exc) or f"{type(exc).__name__} SMTP")
    return MailTransientError(str(exc) or f"{type(exc).__name__} SMTP")


async def send_message(
    config: SmtpConfig, raw: bytes, *, envelope_from: str, recipients: list[str]
) -> None:
    """Send one raw MIME message over SMTP. Errors are mapped to domain types."""
    _validate(config)
    try:
        await aiosmtplib.send(
            raw,
            sender=envelope_from,
            recipients=recipients,
            hostname=config.host,
            port=config.port,
            use_tls=config.security == "ssl",
            start_tls=config.security == "starttls",
            timeout=config.timeout_s,
            username=config.username,
            password=config.password,
        )
    except (ssl.SSLError, OSError) as exc:
        raise MailTransientError(str(exc) or "SMTP connection failed") from exc
    except SMTPException as exc:
        raise _translate_error(exc) from exc


async def verify_connection(config: SmtpConfig) -> None:
    """Connect + NOOP + QUIT, without sending anything. Used by the GUI's "test SMTP" button."""
    smtp = _new_client(config)
    try:
        await smtp.connect()
        if config.username:
            await smtp.login(config.username, config.password or "")
        await smtp.noop()
    except (ssl.SSLError, OSError) as exc:
        raise MailTransientError(str(exc) or "SMTP connection failed") from exc
    except SMTPException as exc:
        raise _translate_error(exc) from exc
    finally:
        with contextlib.suppress(Exception):
            await smtp.quit()
