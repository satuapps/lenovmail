# Lenovmail — authored by satuapps (satuapps.com)
"""Application configuration (pydantic-settings, env prefix `LENOVMAIL_`)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LENOVMAIL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+asyncpg://lenovmail:lenovmail@localhost:5432/lenovmail"
    redis_url: str = "redis://localhost:6379/0"
    secret_key: str = ""

    blob_root: Path = Path("./var/blobs")
    public_base_url: str = "http://localhost:8080"
    allowed_hosts: str = "localhost:8080,127.0.0.1:8080,localhost,127.0.0.1"
    session_ttl_days: int = 14

    ms_client_id: str = ""
    # Empty for an app registration with no secret (Azure "Mobile and desktop applications"):
    # the OAuth flow then runs as a public client. Set it for a confidential client.
    ms_client_secret: str = ""
    ms_authority: str = "https://login.microsoftonline.com/common"
    # Azure rejects a granular scope list on the refresh path with AADSTS70000, so the default
    # asks for whatever the app registration was already consented to.
    ms_scopes: str = "https://graph.microsoft.com/.default"

    sync_interval_s: int = 300
    graph_poll_interval_s: int = 60
    imap_max_conn_per_account: int = 3
    header_fetch_chunk: int = 200
    body_fetch_chunk: int = 25
    body_full_fetch_max_bytes: int = 26_214_400
    body_backfill_max_per_run: int = 500
    graph_body_concurrency: int = 6
    flag_refresh_every: int = 6

    worker_max_jobs: int = 10
    worker_job_timeout_s: int = 900
    idle_renew_s: int = 1500
    smtp_timeout_s: int = 30

    agent_send_limit_per_hour: int = 20

    # Janitor: how long an account may stay rejected before it is retired, what retiring
    # means, and how long dead tokens/audit rows are kept. `delete` drops the account row
    # (and its mail, by cascade), so it stays opt-in.
    janitor_account_grace_days: int = 14
    janitor_account_action: Literal["disable", "delete"] = "disable"
    janitor_check_batch: int = 50
    janitor_token_retention_days: int = 7
    janitor_audit_retention_days: int = 90

    log_level: str = "INFO"

    @property
    def allowed_host_list(self) -> list[str]:
        return [h.strip() for h in self.allowed_hosts.split(",") if h.strip()]

    @property
    def ms_scope_list(self) -> list[str]:
        """Graph scopes to request, split on commas or whitespace.

        `openid`, `profile` and `offline_access` are dropped: msal appends them itself and
        raises `ValueError` if they are passed in, so leaving one in the env var would break
        every OAuth start instead of widening the grant.
        """
        reserved = {"openid", "profile", "offline_access"}
        parts = self.ms_scopes.replace(",", " ").split()
        return [s for s in parts if s.lower() not in reserved]

    @property
    def cookies_secure(self) -> bool:
        return self.public_base_url.startswith("https://")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
