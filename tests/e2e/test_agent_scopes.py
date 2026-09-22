# Lenovmail — authored by satuapps
"""An agent token may only read mail when it holds `mail.read`.

The REST read routes used to check ownership alone, so a token created for sending only —
the common "let the assistant reply, nothing else" case — could still page through every
message in the mailbox. MCP always enforced the scope; REST did not.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from httpx import ASGITransport

from lenovmail.api.app import create_app
from lenovmail.api.security import new_agent_token
from lenovmail.models import AgentToken


async def issue_token(session, account, scopes: list[str]) -> str:
    plain, digest = new_agent_token()
    session.add(
        AgentToken(
            owner_id=account.owner_id,
            name=f"test-{uuid.uuid4().hex[:8]}",
            token_hash=digest,
            scopes=scopes,
        )
    )
    await session.commit()
    return plain


async def get(path: str, token: str) -> httpx.Response:
    transport = ASGITransport(app=create_app())
    # `localhost`, not the httpx default `testserver`: TrustedHostMiddleware enforces
    # LENOVMAIL_ALLOWED_HOSTS and answers anything else with 400 before routing.
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        return await client.get(path, headers={"Authorization": f"Bearer {token}"})


@pytest.mark.parametrize(
    "path",
    [
        "/api/accounts",
        "/api/accounts/{account_id}",
        "/api/accounts/{account_id}/folders",
        "/api/accounts/{account_id}/messages",
        "/api/accounts/{account_id}/outbox",
    ],
)
async def test_send_only_token_cannot_read_mail(session, account, path):
    token = await issue_token(session, account, ["mail.send"])

    response = await get(path.format(account_id=account.id), token)

    assert response.status_code == 403
    assert response.json()["detail"] == "token lacks scope mail.read"


async def test_read_token_reaches_the_same_routes(session, account):
    token = await issue_token(session, account, ["mail.read"])

    assert (await get("/api/accounts", token)).status_code == 200
    assert (await get(f"/api/accounts/{account.id}/folders", token)).status_code == 200
    assert (await get(f"/api/accounts/{account.id}/messages", token)).status_code == 200
