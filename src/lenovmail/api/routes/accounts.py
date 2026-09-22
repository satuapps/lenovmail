# Lenovmail — authored by satuapps (satuapps.com)
"""Mail accounts: auto-discovery, creation, connection testing, and the Microsoft OAuth
flow.

Account creation intentionally verifies the IMAP login first: an account with a wrong
password is better rejected right away than left in the GUI as one that never syncs.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select

from ... import autoconfig
from ...autoconfig.discover import discover as discover_email
from ...config import settings
from ...crypto import account_aad, encrypt
from ...logging import get_logger
from ...models import Account, GraphSettings, ImapSettings
from ...providers import imap_ops as ops
from ...providers.accounts import (
    AccountConfigError,
    account_for_owner,
    get_account_pool,
    graph_client_for,
    load_smtp_config,
    mark_account_status,
    save_graph_tokens,
)
from ...providers.graph import (
    GraphAuthError,
    authorization_url,
    dump_token_cache,
    exchange_code,
    graph_scopes,
    load_token_cache,
)
from ...providers.imap_pool import MailAuthError, MailPermanentError, MailTransientError, reset_pool
from ...redis_helpers import resolved
from ...sync import imap_sync
from ..deps import AccountDep, ArqDep, PrincipalDep, RedisDep, SessionDep, require_read
from ..schemas import (
    AccountCreate,
    AccountOut,
    AccountTestOut,
    AccountUpdate,
    DiscoverOut,
    DiscoverRequest,
    ServerIn,
    ServerOut,
)
from ..serializers import accounts_out

log = get_logger(__name__)

router = APIRouter(prefix="/api", tags=["accounts"])

OAUTH_STATE_PREFIX = "oauth:ms:"
OAUTH_STATE_TTL_S = 900


def _server_out(spec: autoconfig.ServerSpec | None) -> ServerOut | None:
    if spec is None:
        return None
    return ServerOut(
        host=spec.host, port=spec.port, security=spec.security, auth=spec.auth, username=None
    )


def _discover_out(result: autoconfig.DiscoveryResult) -> DiscoverOut:
    warnings: list[str] = []
    if result.note:
        warnings.append(result.note)
    if result.oauth_required and result.provider == "imap":
        warnings.append(
            "this provider requires OAuth/an app password; enter the app credential, "
            "not the account password"
        )
    return DiscoverOut(
        source=result.source,
        provider=result.provider,
        oauth_required=result.oauth_required,
        imap=_server_out(result.imap),
        smtp=_server_out(result.smtp),
        warnings=warnings,
    )


@router.post("/accounts/discover", response_model=DiscoverOut)
async def discover_account(
    body: DiscoverRequest, principal: PrincipalDep, session: SessionDep
) -> DiscoverOut:
    """Run the auto-discovery chain without persisting anything."""
    principal.require("mail.manage")
    try:
        result = await discover_email(body.email.strip().lower(), session)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _discover_out(result)


@router.get("/accounts", response_model=list[AccountOut], dependencies=[Depends(require_read)])
async def list_accounts(principal: PrincipalDep, session: SessionDep) -> list[AccountOut]:
    rows = (
        await session.execute(
            select(Account)
            .where(Account.owner_id == principal.user_id)
            .order_by(Account.created_at)
        )
    ).scalars()
    return await accounts_out(session, list(rows))


@router.post("/accounts", response_model=AccountOut, status_code=status.HTTP_201_CREATED)
async def create_account(
    body: AccountCreate,
    principal: PrincipalDep,
    session: SessionDep,
    arq: ArqDep,
    redis: RedisDep,
) -> AccountOut:
    principal.require("mail.manage")
    email = body.email_address.strip().lower()
    if await account_for_owner(session, principal.user_id, email) is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, detail="an account with that email already exists"
        )

    provider = body.provider
    imap_spec, smtp_spec = body.imap, body.smtp
    discovery: autoconfig.DiscoveryResult | None = None
    if provider == "auto" or (provider == "imap" and (imap_spec is None or smtp_spec is None)):
        try:
            discovery = await discover_email(email, session)
        except ValueError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        if provider == "auto":
            provider = "graph" if discovery.provider == "graph" else "imap"
        if imap_spec is None and discovery.imap is not None:
            imap_spec = ServerIn(
                host=discovery.imap.host, port=discovery.imap.port, security=discovery.imap.security
            )
        if smtp_spec is None and discovery.smtp is not None:
            smtp_spec = ServerIn(
                host=discovery.smtp.host, port=discovery.smtp.port, security=discovery.smtp.security
            )

    # The secret is optional: a registration without one is driven as a public client.
    if provider == "graph" and not settings.ms_client_id:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="LENOVMAIL_MS_CLIENT_ID is not set; Microsoft accounts can't be added",
        )
    if provider == "imap" and imap_spec is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="IMAP settings could not be auto-detected; enter host/port manually",
        )

    password = body.password or (imap_spec.password if imap_spec else None)
    if provider == "imap" and not password:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="IMAP password is required")

    account = Account(
        owner_id=principal.user_id,
        email_address=email,
        display_name=body.display_name,
        provider=provider,
        sync_interval_s=body.sync_interval_s or settings.sync_interval_s,
        status="active",
    )
    session.add(account)
    await session.flush()
    account_id = account.id

    oauth_url: str | None = None
    if provider == "imap":
        if imap_spec is None or password is None:
            # Already guarded above; this repeat makes the type clear to the checker.
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, detail="IMAP settings/password are incomplete"
            )
        session.add(
            ImapSettings(
                account_id=account_id,
                host=imap_spec.host,
                port=imap_spec.port,
                security=imap_spec.security,
                username=imap_spec.username or email,
                password_enc=encrypt(account_aad(account_id, "password_enc"), password.encode()),
                smtp_host=smtp_spec.host if smtp_spec else None,
                smtp_port=smtp_spec.port if smtp_spec else None,
                smtp_security=smtp_spec.security if smtp_spec else None,
                smtp_username=(smtp_spec.username if smtp_spec else None) or email,
                smtp_password_enc=(
                    encrypt(
                        account_aad(account_id, "smtp_password_enc"),
                        (smtp_spec.password or password).encode(),
                    )
                    if smtp_spec
                    else None
                ),
            )
        )
        await session.commit()
        await _verify_imap_login(session, account)
    else:
        session.add(GraphSettings(account_id=account_id, scopes=graph_scopes()))
        await session.commit()
        state = uuid.uuid4().hex
        await resolved(
            redis.set(f"{OAUTH_STATE_PREFIX}{state}", str(account_id), ex=OAUTH_STATE_TTL_S)
        )
        oauth_url = authorization_url(state)

    await session.refresh(account)
    items = await accounts_out(session, [account])
    result = items[0]
    result.oauth_url = oauth_url

    if provider == "imap":
        await arq.enqueue_job("sync_account", str(account_id))
    return result


async def _verify_imap_login(session, account: Account) -> None:
    """Make sure the IMAP credentials are correct before the account is considered active."""
    pool = await get_account_pool(session, account)
    try:
        async with pool.connection() as conn:
            folders = await imap_sync.sync_folders(session, account, conn)
    except MailAuthError as exc:
        await mark_account_status(session, account.id, "auth_error", str(exc))
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"IMAP login rejected: {exc}"
        ) from exc
    except (MailTransientError, MailPermanentError) as exc:
        # Server is currently unreachable: the account is still created, with an honest status.
        await mark_account_status(session, account.id, "error", str(exc))
        log.info("imap_verify_deferred", account_id=str(account.id), reason=str(exc))
        return
    finally:
        await pool.close()
    log.info("imap_verified", account_id=str(account.id), folders=len(folders))


@router.get(
    "/accounts/{account_id}", response_model=AccountOut, dependencies=[Depends(require_read)]
)
async def get_account(account: AccountDep, session: SessionDep) -> AccountOut:
    items = await accounts_out(session, [account])
    return items[0]


@router.patch("/accounts/{account_id}", response_model=AccountOut)
async def update_account(
    account_id: uuid.UUID,
    body: AccountUpdate,
    principal: PrincipalDep,
    session: SessionDep,
) -> AccountOut:
    principal.require("mail.manage")
    account = await _owned(principal, session, account_id)
    if body.display_name is not None:
        account.display_name = body.display_name
    if body.sync_interval_s is not None:
        account.sync_interval_s = body.sync_interval_s
    if body.status is not None:
        account.status = body.status
        if body.status == "active":
            account.status_detail = None
            account.invalid_since = None
    if body.password is not None and account.provider == "imap":
        row = await session.get(ImapSettings, account.id)
        if row is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="IMAP settings not found")
        row.password_enc = encrypt(account_aad(account.id, "password_enc"), body.password.encode())
        account.status = "active"
        account.status_detail = None
        account.invalid_since = None
        await reset_pool(str(account.id))
    await session.commit()
    await session.refresh(account)
    items = await accounts_out(session, [account])
    return items[0]


@router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    account_id: uuid.UUID, principal: PrincipalDep, session: SessionDep
) -> None:
    principal.require("mail.manage")
    account = await _owned(principal, session, account_id)
    await session.execute(delete(Account).where(Account.id == account.id))
    await session.commit()
    await reset_pool(str(account_id))


@router.post("/accounts/{account_id}/sync", status_code=status.HTTP_202_ACCEPTED)
async def trigger_sync(
    account_id: uuid.UUID, principal: PrincipalDep, session: SessionDep, arq: ArqDep
) -> dict[str, str | None]:
    principal.require("mail.read")
    account = await _owned(principal, session, account_id)
    job = await arq.enqueue_job("sync_account", str(account.id))
    return {"job_id": getattr(job, "job_id", None)}


@router.post("/accounts/{account_id}/test", response_model=AccountTestOut)
async def test_account(
    account_id: uuid.UUID, principal: PrincipalDep, session: SessionDep
) -> AccountTestOut:
    """Test the account's inbound/outbound connections without changing any data."""
    principal.require("mail.read")
    account = await _owned(principal, session, account_id)
    result = AccountTestOut()

    if account.provider == "imap":
        pool = await get_account_pool(session, account)
        try:
            async with pool.connection() as conn:
                folders = await ops.list_folders(conn)
            result.imap = "ok"
            result.folders = len(folders)
        except (MailAuthError, MailTransientError, MailPermanentError) as exc:
            result.imap = f"failed: {exc}"
        finally:
            await pool.close()
    else:
        client = await graph_client_for(session, account)
        try:
            profile = await client.me()
            result.graph = "ok"
            result.folders = None if not profile else 0
            await save_graph_tokens(session, account.id, client)
        except GraphAuthError as exc:
            result.graph = f"reauthorization required: {exc}"
        except Exception as exc:  # noqa: BLE001 - any Graph error is shown as-is
            result.graph = f"failed: {exc}"
        finally:
            await client.aclose()

    try:
        loaded = await load_smtp_config(session, account)
    except AccountConfigError as exc:
        result.smtp = f"not configured: {exc}"
    else:
        from ...providers.smtp import SmtpConfig, verify_connection

        config = SmtpConfig(
            host=str(loaded["host"]),
            port=int(str(loaded["port"])),
            security=str(loaded["security"]),
            username=str(loaded["username"]) if loaded.get("username") else None,
            password=str(loaded["password"]) if loaded.get("password") else None,
        )
        try:
            await verify_connection(config)
            result.smtp = "ok"
        except Exception as exc:  # noqa: BLE001 - the SMTP error message is shown directly
            result.smtp = f"failed: {exc}"
    return result


async def _owned(principal, session: SessionDep, account_id: uuid.UUID) -> Account:
    row = await session.get(Account, account_id)
    if row is None or row.owner_id != principal.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="account not found")
    return row


# --- Microsoft OAuth --------------------------------------------------------------


@router.get("/oauth/microsoft/start", response_model=None)
async def oauth_start(
    principal: PrincipalDep, session: SessionDep, redis: RedisDep, account_id: uuid.UUID
) -> RedirectResponse:
    """Start the Microsoft consent flow for an existing Graph account."""
    principal.require("mail.manage")
    account = await _owned(principal, session, account_id)
    if account.provider != "graph":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="this account is not a Microsoft account"
        )
    state = uuid.uuid4().hex
    await redis.set(f"{OAUTH_STATE_PREFIX}{state}", str(account.id), ex=OAUTH_STATE_TTL_S)
    return RedirectResponse(authorization_url(state), status_code=status.HTTP_302_FOUND)


@router.get("/oauth/microsoft/callback", response_model=None)
async def oauth_callback(
    request: Request, session: SessionDep, redis: RedisDep, code: str, state: str
) -> RedirectResponse:
    """Receive the authorization code, persist the token cache, then redirect back to the GUI."""
    account_id_raw = await resolved(redis.get(f"{OAUTH_STATE_PREFIX}{state}"))
    if not account_id_raw:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="unrecognized OAuth state")
    await resolved(redis.delete(f"{OAUTH_STATE_PREFIX}{state}"))
    account = await session.get(Account, uuid.UUID(str(account_id_raw)))
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="account not found")

    graph_settings = await session.get(GraphSettings, account.id)
    if graph_settings is None:
        graph_settings = GraphSettings(account_id=account.id, scopes=graph_scopes())
        session.add(graph_settings)
        await session.flush()
    cache = load_token_cache(account.id, graph_settings.token_cache_enc)
    try:
        claims = exchange_code(code, cache)
    except GraphAuthError as exc:
        await mark_account_status(session, account.id, "auth_error", str(exc))
        return _gui_redirect(f"/?oauth=error&account={account.id}")

    blob = dump_token_cache(account.id, cache)
    if blob is not None:
        graph_settings.token_cache_enc = blob
    home_account_id = None
    if isinstance(claims.get("id_token_claims"), dict):
        home_account_id = claims["id_token_claims"].get("preferred_username")
    if home_account_id:
        graph_settings.home_account_id = home_account_id
    await session.commit()
    await mark_account_status(session, account.id, "active")
    log.info("graph_oauth_completed", account_id=str(account.id))
    return _gui_redirect(f"/?oauth=ok&account={account.id}")


def _gui_redirect(path: str) -> RedirectResponse:
    return RedirectResponse(
        f"{settings.public_base_url.rstrip('/')}{path}", status_code=status.HTTP_303_SEE_OTHER
    )
