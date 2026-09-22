# Lenovmail — authored by satuapps
"""Microsoft Graph client: MSAL auth, retries, and the mail operations used by sync/sender.

The MSAL token cache is stored encrypted in `graph_settings.token_cache_enc` (AAD
`accounts:<id>:graph_token_cache`). Graph's `deltaLink` also carries a long-lived sync
token, so it is stored encrypted in a separate column by the sync layer.

Note: webhooks (change notifications) are not used in v1 — they require a public HTTPS
endpoint. Delta polling guarantees correctness without one.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import msal

from ..config import settings
from ..crypto import account_aad, decrypt, encrypt
from ..logging import get_logger

log = get_logger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
SCOPES = ["offline_access", "User.Read", "Mail.ReadWrite", "Mail.Send"]

TOKEN_CACHE_FIELD = "graph_token_cache"

MAX_ATTEMPTS = 5
BACKOFF_SECONDS = (1, 2, 4, 8, 16)
RETRY_STATUS = {429, 500, 502, 503, 504}

# Message fields needed by sync (deliberately limited to keep responses light).
MESSAGE_SELECT = ",".join(
    [
        "id",
        "internetMessageId",
        "subject",
        "from",
        "toRecipients",
        "ccRecipients",
        "bccRecipients",
        "replyTo",
        "receivedDateTime",
        "sentDateTime",
        "isRead",
        "isDraft",
        "hasAttachments",
        "bodyPreview",
        "conversationId",
        "parentFolderId",
    ]
)


class GraphAuthError(Exception):
    """Reauthorization required (token could not be refreshed). Account is marked `auth_error`."""


class GraphTransientError(Exception):
    """Transient failure (network/5xx) — safe to retry."""


class GraphRequestError(Exception):
    """Request rejected by Graph (4xx other than 401/429)."""

    def __init__(self, status_code: int, code: str | None, message: str) -> None:
        super().__init__(f"HTTP {status_code} {code or ''}: {message}".strip())
        self.status_code = status_code
        self.code = code


@dataclass(slots=True)
class GraphAccount:
    """App credentials + identity of the connected account."""

    account_id: uuid.UUID
    home_account_id: str | None = None


def token_cache_aad(account_id: uuid.UUID) -> str:
    return account_aad(account_id, TOKEN_CACHE_FIELD)


def load_token_cache(account_id: uuid.UUID, blob: bytes | None) -> msal.SerializableTokenCache:
    cache = msal.SerializableTokenCache()
    if blob:
        cache.deserialize(decrypt(token_cache_aad(account_id), blob).decode())
    return cache


def dump_token_cache(account_id: uuid.UUID, cache: msal.SerializableTokenCache) -> bytes | None:
    """Encrypted token cache, or `None` if nothing changed."""
    if not cache.has_state_changed:
        return None
    return encrypt(token_cache_aad(account_id), cache.serialize().encode())


def build_msal_app(
    token_cache: msal.SerializableTokenCache | None = None,
) -> msal.ConfidentialClientApplication:
    if not settings.ms_client_id or not settings.ms_client_secret:
        raise GraphAuthError("LENOVMAIL_MS_CLIENT_ID/LENOVMAIL_MS_CLIENT_SECRET is not set")
    return msal.ConfidentialClientApplication(
        settings.ms_client_id,
        client_credential=settings.ms_client_secret,
        authority=settings.ms_authority,
        token_cache=token_cache,
    )


def redirect_uri() -> str:
    return f"{settings.public_base_url.rstrip('/')}/api/oauth/microsoft/callback"


def authorization_url(state: str) -> str:
    """Microsoft consent URL for the authorization-code flow."""
    return build_msal_app().get_authorization_request_url(
        SCOPES, state=state, redirect_uri=redirect_uri()
    )


def exchange_code(code: str, token_cache: msal.SerializableTokenCache) -> dict[str, Any]:
    """Exchange an authorization code for tokens.

    Returns the raw msal result, which carries `id_token_claims`.
    """
    app = build_msal_app(token_cache)
    result = app.acquire_token_by_authorization_code(
        code, scopes=SCOPES, redirect_uri=redirect_uri()
    )
    if "access_token" not in result:
        raise GraphAuthError(
            result.get("error_description") or result.get("error") or "code exchange failed"
        )
    return result


class GraphClient:
    """HTTP wrapper for Graph with automatic token refresh and retries."""

    def __init__(
        self,
        account_id: uuid.UUID,
        token_cache: msal.SerializableTokenCache,
        home_account_id: str | None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.account_id = account_id
        self.token_cache = token_cache
        self.home_account_id = home_account_id
        self._client = client
        self._owns_client = client is None

    @classmethod
    def from_stored(
        cls, account_id: uuid.UUID, token_cache_enc: bytes | None, home_account_id: str | None
    ) -> GraphClient:
        return cls(account_id, load_token_cache(account_id, token_cache_enc), home_account_id)

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=GRAPH_BASE_URL, timeout=30.0)
        return self._client

    # --- token ------------------------------------------------------------------

    def _account(self) -> dict[str, Any] | None:
        accounts = build_msal_app(self.token_cache).get_accounts()
        if not accounts:
            return None
        if self.home_account_id:
            for candidate in accounts:
                if candidate.get("home_account_id") == self.home_account_id:
                    return candidate
        return accounts[0]

    def _acquire_token_sync(self) -> str:
        account = self._account()
        if account is None:
            raise GraphAuthError("no account stored in the token cache; reconnect the account")
        result = build_msal_app(self.token_cache).acquire_token_silent(SCOPES, account=account)
        if not result or "access_token" not in result:
            raise GraphAuthError(
                (result or {}).get("error_description")
                or (result or {}).get("error")
                or "token refresh failed"
            )
        return result["access_token"]

    async def access_token(self) -> str:
        """Access token (msal is synchronous, so this runs in a thread)."""
        return await asyncio.to_thread(self._acquire_token_sync)

    def export_token_cache(self) -> bytes | None:
        return dump_token_cache(self.account_id, self.token_cache)

    # --- core request -------------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        raw: bool = False,
        absolute: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Send a Graph request with retry + token refresh.

        `absolute=True` means `path` is a full URL (used for `@odata.nextLink`
        and `@odata.deltaLink`).
        """
        client = await self._http()
        url = path if absolute else path
        last_error: Exception | None = None

        for attempt in range(MAX_ATTEMPTS):
            token = await self.access_token()
            headers = dict(kwargs.pop("headers", {}) or {})
            headers["Authorization"] = f"Bearer {token}"
            try:
                response = await client.request(
                    method, url, headers=headers, follow_redirects=not absolute, **kwargs
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
                await self._sleep_backoff(attempt, None)
                continue

            if response.status_code == 401 and attempt < MAX_ATTEMPTS - 1:
                # Token may have been revoked tenant-side; force one refresh.
                log.info("graph_token_rejected", status=401, attempt=attempt)
                with contextlib.suppress(GraphAuthError):
                    await asyncio.to_thread(self._force_refresh)
                continue
            if response.status_code in RETRY_STATUS:
                last_error = GraphTransientError(f"HTTP {response.status_code}")
                await self._sleep_backoff(attempt, response.headers.get("Retry-After"))
                continue
            if response.status_code >= 400:
                raise self._error_from(response)

            if raw:
                return response.content
            if not response.content:
                return {}
            return response.json()

        raise GraphTransientError(f"Graph failed after {MAX_ATTEMPTS} attempts: {last_error}")

    def _force_refresh(self) -> None:
        account = self._account()
        if account is None:
            raise GraphAuthError("no account stored in the token cache")
        build_msal_app(self.token_cache).acquire_token_silent(
            SCOPES, account=account, force_refresh=True
        )

    def _error_from(self, response: httpx.Response) -> Exception:
        code: str | None = None
        message = response.text[:500]
        with contextlib.suppress(ValueError):
            payload = response.json()
            error = payload.get("error") or {}
            code = error.get("code")
            message = error.get("message") or message
        if response.status_code == 401:
            return GraphAuthError(f"{code or 401}: {message}")
        return GraphRequestError(response.status_code, code, message)

    async def _sleep_backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
        if retry_after:
            with contextlib.suppress(ValueError):
                delay = max(delay, int(float(retry_after)))
        log.info("graph_backoff", attempt=attempt + 1, delay=delay)
        await asyncio.sleep(delay)

    # --- mail operations ----------------------------------------------------------

    async def me(self) -> dict[str, Any]:
        return await self.request("GET", "/me")

    async def folders_delta(self, delta_link: str | None = None) -> tuple[list[dict], str | None]:
        """All folders + `@odata.deltaLink` for the next cycle."""
        return await self.delta(delta_link or "/me/mailFolders/delta")

    async def messages_delta(
        self, remote_folder_id: str, delta_link: str | None = None
    ) -> tuple[list[dict], str | None]:
        """Messages for one folder + deltaLink. Per-folder, per Graph's constraints."""
        return await self.delta(
            delta_link or f"/me/mailFolders/{remote_folder_id}/messages/delta",
            params={"$select": MESSAGE_SELECT},
            headers={"Prefer": "odata.maxpagesize=100"},
        )

    async def delta(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[list[dict], str | None]:
        """Follow `@odata.nextLink` until exhausted; return items + `@odata.deltaLink`."""
        items: list[dict] = []
        next_url: str | None = path
        delta_link: str | None = None
        absolute = not path.startswith("/")

        while next_url:
            payload = await self.request(
                "GET",
                next_url,
                absolute=absolute,
                params=params if not absolute else None,
                headers=headers,
            )
            items.extend(payload.get("value") or [])
            next_url = payload.get("@odata.nextLink")
            delta_link = payload.get("@odata.deltaLink") or delta_link
            absolute = True
            params = None
            headers = None
        return items, delta_link

    async def message_mime(self, message_id: str) -> bytes:
        """Raw MIME (`/$value`)."""
        return await self.request("GET", f"/me/messages/{message_id}/$value", raw=True)

    async def message(
        self, message_id: str, *, html_body: bool = False, select: str | None = None
    ) -> dict[str, Any]:
        headers = {}
        if html_body:
            headers["Prefer"] = 'outlook.body-content-type="html"'
        params = {"$select": select} if select else None
        return await self.request(
            "GET", f"/me/messages/{message_id}", params=params, headers=headers
        )

    async def message_attachments(self, message_id: str) -> list[dict[str, Any]]:
        payload = await self.request(
            "GET",
            f"/me/messages/{message_id}/attachments",
            params={"$select": "id,name,contentType,size,isInline"},
        )
        return payload.get("value") or []

    async def attachment_bytes(self, message_id: str, attachment_id: str) -> bytes:
        """Attachment content (`/$value`) for Graph accounts whose body is fetched via JSON."""
        return await self.request(
            "GET", f"/me/messages/{message_id}/attachments/{attachment_id}/$value", raw=True
        )

    async def patch_message(self, message_id: str, payload: dict[str, Any]) -> dict:
        return await self.request("PATCH", f"/me/messages/{message_id}", json=payload)

    async def move_message(self, message_id: str, destination_id: str) -> dict:
        return await self.request(
            "POST", f"/me/messages/{message_id}/move", json={"destinationId": destination_id}
        )

    async def delete_message(self, message_id: str) -> None:
        """Move to Deleted Items (Graph's documented delete pattern)."""
        await self.request(
            "POST", f"/me/messages/{message_id}/move", json={"destinationId": "deleteditems"}
        )

    async def send_mail(self, message: dict[str, Any]) -> None:
        await self.request(
            "POST", "/me/sendMail", json={"message": message, "saveToSentItems": True}
        )

    async def create_reply(self, message_id: str, *, reply_all: bool = False) -> dict:
        suffix = "createReplyAll" if reply_all else "createReply"
        return await self.request("POST", f"/me/messages/{message_id}/{suffix}")

    async def send_draft(self, draft_id: str) -> None:
        await self.request("POST", f"/me/messages/{draft_id}/send")
