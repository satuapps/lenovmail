# Lenovmail — authored by satuapps (satuapps.com)
"""Parser for Mozilla autoconfig / ISPDB XML (`config-v1.1.xml`)."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass

from .types import Security, ServerSpec

# socketType (Mozilla schema) -> internal security value.
_SOCKET_TYPES: dict[str, Security] = {
    "SSL": "ssl",
    "SSL/TLS": "ssl",
    "STARTTLS": "starttls",
    "plain": "none",
    "none": "none",
}

_PLACEHOLDERS = ("%EMAILADDRESS%", "%EMAILLOCALPART%", "%EMAILDOMAIN%")


@dataclass(slots=True)
class MozillaConfig:
    imap: ServerSpec | None
    smtp: ServerSpec | None
    oauth_required: bool


def _substitute_placeholders(value: str, email: str) -> str:
    local, _, domain = email.partition("@")
    out = value
    for token, replacement in zip(_PLACEHOLDERS, (email, local, domain), strict=True):
        out = out.replace(token, replacement)
    return out


def _text(node: ET.Element, tag: str) -> str | None:
    found = node.find(tag)
    if found is None or found.text is None:
        return None
    value = found.text.strip()
    return value or None


def _spec_from_server(node: ET.Element, email: str) -> tuple[ServerSpec | None, bool]:
    """Build a ServerSpec from an incomingServer/outgoingServer element.

    Returns (spec, oauth_required). Servers without authentication (`authentication`
    is `none`) are skipped since this client can't use them.
    """
    host = _text(node, "hostname")
    port_raw = _text(node, "port")
    if not host or not port_raw:
        return None, False

    socket_type = _text(node, "socketType") or "plain"
    security = _SOCKET_TYPES.get(socket_type, "none")

    auth_text = (_text(node, "authentication") or "").lower()
    oauth_required = auth_text in {"oauth2", "gssapi"}

    username = _text(node, "username") or "%EMAILADDRESS%"
    username = _substitute_placeholders(username, email)

    # v1 client only supports password/app-password on IMAP/SMTP; OAuth is used via
    # the Graph provider. `auth` is therefore always "password" on the IMAP path.
    spec = ServerSpec(host=host, port=int(port_raw), security=security, auth="password")
    return spec, oauth_required


def parse_mozilla_config(xml_text: str, email: str) -> MozillaConfig:
    """Take the first incomingServer[type=imap] and outgoingServer[type=smtp].

    Malformed XML or no usable server -> both specs are None.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return MozillaConfig(imap=None, smtp=None, oauth_required=False)

    imap: ServerSpec | None = None
    smtp: ServerSpec | None = None
    oauth_required = False

    for provider in root.iter("emailProvider"):
        if imap is None:
            for server in provider.findall("incomingServer"):
                if (server.get("type") or "").lower() != "imap":
                    continue
                spec, oauth = _spec_from_server(server, email)
                if spec is not None:
                    imap, oauth_required = spec, oauth_required or oauth
                    break
        if smtp is None:
            for server in provider.findall("outgoingServer"):
                if (server.get("type") or "").lower() != "smtp":
                    continue
                spec, oauth = _spec_from_server(server, email)
                if spec is not None:
                    smtp, oauth_required = spec, oauth_required or oauth
                    break
        if imap is not None and smtp is not None:
            break

    return MozillaConfig(imap=imap, smtp=smtp, oauth_required=oauth_required)


def username_from_config(xml_text: str, email: str) -> str | None:
    """`<username>` of the incoming IMAP server after placeholder substitution (if any)."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    for provider in root.iter("emailProvider"):
        for server in provider.findall("incomingServer"):
            if (server.get("type") or "").lower() != "imap":
                continue
            raw = _text(server, "username")
            if raw:
                return _substitute_placeholders(raw, email)
    return None
