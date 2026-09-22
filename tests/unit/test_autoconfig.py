# Lenovmail — authored by satuapps
"""Auto-detection: ISPDB XML parser, negative SRV handling, Microsoft MX rule."""

from __future__ import annotations

from pathlib import Path

import pytest

from lenovmail.autoconfig import discover as discover_mod
from lenovmail.autoconfig import srv
from lenovmail.autoconfig.discover import _step_mx, _step_srv, override_for
from lenovmail.autoconfig.ispdb import parse_mozilla_config, username_from_config
from lenovmail.autoconfig.types import DiscoveryResult, ServerSpec, domain_of, registrable_domain

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestIspdbParser:
    def test_gmail_fixture_yields_imap_smtp_and_oauth(self):
        parsed = parse_mozilla_config(fixture("ispdb_gmail_com.xml"), "user@gmail.com")

        assert parsed.imap is not None
        assert (parsed.imap.host, parsed.imap.port, parsed.imap.security) == (
            "imap.gmail.com",
            993,
            "ssl",
        )
        assert parsed.smtp is not None
        assert (parsed.smtp.host, parsed.smtp.port, parsed.smtp.security) == (
            "smtp.gmail.com",
            465,
            "ssl",
        )
        # Gmail's ISPDB entry declares authentication=OAuth2.
        assert parsed.oauth_required is True

    def test_zoho_fixture_is_plain_password(self):
        parsed = parse_mozilla_config(fixture("ispdb_zoho_com.xml"), "user@zoho.com")

        assert parsed.imap is not None
        assert (parsed.imap.host, parsed.imap.port, parsed.imap.security) == (
            "imap.zoho.com",
            993,
            "ssl",
        )
        assert parsed.oauth_required is False

    def test_placeholders_substituted_in_username(self):
        xml = """<?xml version="1.0"?>
        <clientConfig><emailProvider id="example.com">
          <incomingServer type="imap">
            <hostname>imap.example.com</hostname><port>993</port>
            <socketType>SSL</socketType><authentication>password-cleartext</authentication>
            <username>%EMAILLOCALPART%@example.com</username>
          </incomingServer>
          <outgoingServer type="smtp">
            <hostname>smtp.example.com</hostname><port>587</port>
            <socketType>STARTTLS</socketType><authentication>password-cleartext</authentication>
          </outgoingServer>
        </emailProvider></clientConfig>"""

        assert username_from_config(xml, "alex@example.com") == "alex@example.com"
        parsed = parse_mozilla_config(xml, "alex@example.com")
        assert parsed.smtp is not None and parsed.smtp.security == "starttls"

    def test_malformed_xml_is_not_fatal(self):
        parsed = parse_mozilla_config("<clientConfig><emailProvider", "user@example.com")

        assert parsed.imap is None and parsed.smtp is None and parsed.oauth_required is False

    def test_server_without_hostname_is_skipped(self):
        xml = """<clientConfig><emailProvider>
          <incomingServer type="imap"><port>993</port></incomingServer>
        </emailProvider></clientConfig>"""

        assert parse_mozilla_config(xml, "user@example.com").imap is None


class TestSrvNegatives:
    async def test_root_target_means_service_unavailable(self, monkeypatch):
        """Target `.` is an explicit negative answer, not "no record"."""

        class FakeRecord:
            priority = 0
            weight = 0
            port = 0
            target = "."

        class FakeResolver:
            lifetime = 0.0
            timeout = 0.0

            async def resolve(self, name, rdtype):
                return [FakeRecord()]

        monkeypatch.setattr(srv.dns.asyncresolver, "Resolver", FakeResolver)
        result = await srv.lookup("_submission._tcp", "example.com")

        assert result is not None
        assert result.available is False
        assert result.host is None

    async def test_negative_smtp_srv_falls_back_to_ispdb_not_dot_host(self, monkeypatch):
        """SRV marks SMTP as dead -> SMTP is filled from ISPDB, and host `.` is never used."""

        async def fake_lookup_first(services, domain):
            if services is srv.IMAP_SERVICES:
                return ("_imaps._tcp", srv.SrvTarget(True, "imap.example.com", 993))
            return ("_submission._tcp", srv.SrvTarget(False))

        async def fake_ispdb(domain, email):
            return DiscoveryResult(
                source="ispdb",
                imap=ServerSpec("imap.ispdb.example.com", 993, "ssl"),
                smtp=ServerSpec("smtp.ispdb.example.com", 587, "starttls"),
            )

        monkeypatch.setattr(discover_mod, "lookup_first", fake_lookup_first)
        monkeypatch.setattr(discover_mod, "_ispdb", fake_ispdb)

        result = await _step_srv("example.com", "user@example.com")

        assert result is not None
        assert result.imap == ServerSpec("imap.example.com", 993, "ssl")
        assert result.smtp == ServerSpec("smtp.ispdb.example.com", 587, "starttls")
        assert result.note is not None and "unavailable" in result.note
        for spec in (result.imap, result.smtp):
            assert spec is not None and spec.host not in (".", "")

    async def test_negative_smtp_srv_uses_probe_when_ispdb_misses(self, monkeypatch):
        async def fake_lookup_first(services, domain):
            if services is srv.IMAP_SERVICES:
                return ("_imap._tcp", srv.SrvTarget(True, "imap.example.com", 143))
            return ("_submission._tcp", srv.SrvTarget(False))

        async def fake_ispdb(domain, email):
            return None

        async def fake_probe_smtp(domain):
            return ServerSpec("smtp.example.com", 587, "starttls")

        monkeypatch.setattr(discover_mod, "lookup_first", fake_lookup_first)
        monkeypatch.setattr(discover_mod, "_ispdb", fake_ispdb)
        monkeypatch.setattr(discover_mod, "probe_smtp", fake_probe_smtp)

        result = await _step_srv("example.com", "user@example.com")

        assert result is not None
        # `_imap._tcp` (143) -> STARTTLS, not SSL.
        assert result.imap == ServerSpec("imap.example.com", 143, "starttls")
        assert result.smtp == ServerSpec("smtp.example.com", 587, "starttls")

    async def test_srv_without_imap_record_yields_none(self, monkeypatch):
        async def fake_lookup_first(services, domain):
            return None

        monkeypatch.setattr(discover_mod, "lookup_first", fake_lookup_first)
        assert await _step_srv("example.com", "user@example.com") is None


class TestMxRule:
    async def test_outlook_mx_selects_graph(self, monkeypatch):
        async def fake_mx(domain):
            return ["example-com.mail.protection.outlook.com"]

        monkeypatch.setattr(discover_mod, "_mx_targets", fake_mx)
        result = await _step_mx("example.com", "user@example.com")

        assert result is not None
        assert result.provider == "graph"
        assert result.oauth_required is True
        assert result.usable is True

    async def test_non_outlook_mx_retries_ispdb_with_mx_domain(self, monkeypatch):
        seen: list[str] = []

        async def fake_mx(domain):
            return ["mx1.mail.host.com"]

        async def fake_ispdb(domain, email):
            seen.append(domain)
            return DiscoveryResult(source="ispdb", imap=ServerSpec("imap.host.com", 993, "ssl"))

        monkeypatch.setattr(discover_mod, "_mx_targets", fake_mx)
        monkeypatch.setattr(discover_mod, "_ispdb", fake_ispdb)

        result = await _step_mx("customer.com", "user@customer.com")

        assert seen == ["host.com"]
        assert result is not None and result.imap is not None
        assert result.imap.host == "imap.host.com"

    async def test_no_mx_returns_none(self, monkeypatch):
        async def fake_mx(domain):
            return []

        monkeypatch.setattr(discover_mod, "_mx_targets", fake_mx)
        assert await _step_mx("nowhere.invalid", "user@nowhere.invalid") is None


class TestOverrides:
    def test_gmail_override_flags_app_password(self):
        result = override_for("gmail.com")

        assert result is not None
        assert result.imap is not None and result.imap.host == "imap.gmail.com"
        assert result.smtp is not None and result.smtp.host == "smtp.gmail.com"
        assert result.oauth_required is True
        assert result.note is not None and "app password" in result.note.lower()

    def test_microsoft_consumers_go_to_graph(self):
        for domain in ("outlook.com", "hotmail.com", "live.com"):
            result = override_for(domain)
            assert result is not None and result.provider == "graph"
            assert result.imap is None and result.smtp is None

    def test_every_override_is_usable_and_has_sane_ports(self):
        for entry in discover_mod.load_overrides():
            assert entry.result.usable
            for spec in (entry.result.imap, entry.result.smtp):
                if spec is not None:
                    assert 1 <= spec.port <= 65535
                    assert spec.security in ("ssl", "starttls", "none")
                    assert spec.host != "." and spec.host

    def test_zoho_override_is_not_oauth(self):
        result = override_for("zoho.com")
        assert result is not None and result.oauth_required is False


class TestHelpers:
    @pytest.mark.parametrize(
        ("host", "expected"),
        [
            ("mx1.mail.host.com", "host.com"),
            ("in1-smtp.messagingengine.com", "messagingengine.com"),
            ("example.com", "example.com"),
            ("localhost", "localhost"),
        ],
    )
    def test_registrable_domain(self, host, expected):
        assert registrable_domain(host) == expected

    def test_domain_of_normalises_and_rejects(self):
        assert domain_of("  User@Example.COM ") == "example.com"
        for bad in ("no-at-sign", "a@b", "a@@b.com", "user@.com"):
            with pytest.raises(ValueError):
                domain_of(bad)
