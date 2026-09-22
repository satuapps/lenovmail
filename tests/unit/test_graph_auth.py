# Lenovmail — authored by satuapps (satuapps.com)
"""MSAL client selection and Graph scope configuration."""

from __future__ import annotations

import msal
import pytest

from lenovmail.config import Settings
from lenovmail.providers import graph


class FakeApp:
    def __init__(self, client_id: str, **kwargs) -> None:
        self.client_id = client_id
        self.kwargs = kwargs


def _patch_msal(monkeypatch) -> dict[str, object]:
    built: dict[str, object] = {}

    def record(kind: str):
        def factory(client_id: str, **kwargs):
            built["kind"] = kind
            built["app"] = FakeApp(client_id, **kwargs)
            return built["app"]

        return factory

    monkeypatch.setattr(msal, "ConfidentialClientApplication", record("confidential"))
    monkeypatch.setattr(msal, "PublicClientApplication", record("public"))
    return built


def test_registration_without_a_secret_uses_a_public_client(monkeypatch):
    """Azure "Mobile and desktop" registrations have no secret; sending one is rejected."""
    built = _patch_msal(monkeypatch)
    monkeypatch.setattr(graph.settings, "ms_client_id", "app-id")
    monkeypatch.setattr(graph.settings, "ms_client_secret", "")

    graph.build_msal_app()

    app = built["app"]
    assert built["kind"] == "public"
    assert isinstance(app, FakeApp)
    assert "client_credential" not in app.kwargs


def test_registration_with_a_secret_stays_confidential(monkeypatch):
    built = _patch_msal(monkeypatch)
    monkeypatch.setattr(graph.settings, "ms_client_id", "app-id")
    monkeypatch.setattr(graph.settings, "ms_client_secret", "sh!")

    graph.build_msal_app()

    app = built["app"]
    assert built["kind"] == "confidential"
    assert isinstance(app, FakeApp)
    assert app.kwargs["client_credential"] == "sh!"


def test_missing_client_id_is_an_auth_error(monkeypatch):
    monkeypatch.setattr(graph.settings, "ms_client_id", "")

    with pytest.raises(graph.GraphAuthError):
        graph.build_msal_app()


def test_default_scope_is_the_one_azure_accepts_on_refresh():
    """Granular scopes are rejected with AADSTS70000 when the refresh token is redeemed."""
    assert Settings().ms_scope_list == ["https://graph.microsoft.com/.default"]


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("User.Read Mail.ReadWrite", ["User.Read", "Mail.ReadWrite"]),
        ("User.Read,Mail.Send", ["User.Read", "Mail.Send"]),
        ("  Mail.ReadWrite ,, Mail.Send  ", ["Mail.ReadWrite", "Mail.Send"]),
    ],
)
def test_scopes_are_split_on_commas_and_whitespace(configured, expected):
    assert Settings(ms_scopes=configured).ms_scope_list == expected


@pytest.mark.parametrize("reserved", ["offline_access", "openid", "profile"])
def test_reserved_scopes_are_dropped_instead_of_breaking_the_oauth_start(reserved):
    """msal appends these itself and raises on them as input, so one left in the env var
    would otherwise break every OAuth start."""
    assert Settings(ms_scopes=f"{reserved} User.Read").ms_scope_list == ["User.Read"]


def test_configured_scopes_reach_the_authorization_url(monkeypatch):
    captured: dict[str, object] = {}

    class RecordingApp:
        def get_authorization_request_url(self, scopes, state, redirect_uri):
            captured["scopes"] = scopes
            captured["state"] = state
            return "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?x=1"

    monkeypatch.setattr(graph, "build_msal_app", lambda *a, **k: RecordingApp())
    monkeypatch.setattr(graph.settings, "ms_scopes", "Mail.ReadWrite")

    graph.authorization_url("state-123")

    assert captured["scopes"] == ["Mail.ReadWrite"]
    assert captured["state"] == "state-123"
