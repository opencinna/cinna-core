"""
Backend tests for the Local Development CLI feature.

Covers:
- Setup token creation, exchange (full lifecycle), and error cases
- CLI token listing, filtering by agent, and revocation
- CLI-authenticated endpoints: build-context, credentials, building-context,
  workspace (404 without env), workspace/manifest (404 without env),
  knowledge/search
- Auth guards: unauthenticated, wrong user, revoked token, wrong agent scope
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.cli import (
    cli_auth_headers,
    create_setup_token,
    exchange_setup_token,
    list_cli_tokens,
    revoke_cli_token,
)
from tests.utils.user import create_random_user, user_authentication_headers

_BASE = f"{settings.API_V1_STR}/cli"


# ── Scenario 1: Setup token and CLI token full lifecycle ─────────────────────

def test_setup_token_and_cli_token_full_lifecycle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Full setup token + CLI token lifecycle:
      1. Create an agent
      2. Create a setup token → verify response fields and setup_command
      3. List CLI tokens → empty initially
      4. Exchange setup token → get CLI JWT and agent info
      5. List CLI tokens → new token appears with is_revoked=False
      6. Exchange same setup token again → 400 (already used)
      7. Exchange a non-existent setup token → 400
      8. Filter tokens by agent_id → token appears
      9. Revoke CLI token → 200
     10. List tokens → token has is_revoked=True
     11. Auth guard: create setup token for another user's agent → 400
     12. Auth guard: unauthenticated setup token creation → 401/403
     13. Revoke non-existent token → 404
     14. Revoke another user's token → 404
    """
    # ── Phase 1: Create agent ─────────────────────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    # ── Phase 2: Create setup token ───────────────────────────────────────
    token_resp = create_setup_token(client, superuser_token_headers, agent_id)
    assert token_resp["agent_id"] == agent_id
    assert "token" in token_resp
    assert "id" in token_resp
    assert "expires_at" in token_resp
    assert "created_at" in token_resp
    assert "setup_command" in token_resp
    setup_token_str = token_resp["token"]
    # The setup_command should embed the token string
    assert setup_token_str in token_resp["setup_command"]

    # ── Phase 3: List CLI tokens → empty ─────────────────────────────────
    tokens = list_cli_tokens(client, superuser_token_headers)
    assert tokens == []

    # ── Phase 4: Exchange setup token ─────────────────────────────────────
    exchange = exchange_setup_token(client, setup_token_str, machine_name="Dev Laptop")
    assert "cli_token" in exchange
    assert "agent" in exchange
    assert "platform_url" in exchange
    assert exchange["agent"]["id"] == agent_id
    assert "credentials" in exchange
    assert "knowledge_sources" in exchange
    cli_jwt = exchange["cli_token"]
    assert isinstance(cli_jwt, str) and len(cli_jwt) > 20

    # ── Phase 5: List CLI tokens → new token present ──────────────────────
    tokens = list_cli_tokens(client, superuser_token_headers)
    assert len(tokens) == 1
    tok = tokens[0]
    assert tok["agent_id"] == agent_id
    assert tok["is_revoked"] is False
    assert tok["name"] == "Dev Laptop"
    assert "id" in tok
    assert "prefix" in tok
    assert "expires_at" in tok
    cli_token_id = tok["id"]

    # ── Phase 6: Re-exchange same setup token → 400 (already used) ────────
    r = client.post(
        f"/api/cli-setup/{setup_token_str}",
        json={"machine_name": "Another Machine"},
    )
    assert r.status_code == 400
    assert "already been used" in r.json()["detail"].lower()

    # ── Phase 7: Exchange a non-existent token → 400 ──────────────────────
    r = client.post(
        "/api/cli-setup/this-token-does-not-exist-at-all",
        json={"machine_name": "Ghost"},
    )
    assert r.status_code == 400

    # ── Phase 8: Filter tokens by agent_id ───────────────────────────────
    filtered = list_cli_tokens(client, superuser_token_headers, agent_id=agent_id)
    assert len(filtered) == 1
    assert filtered[0]["id"] == cli_token_id

    # Filter by a different (random) agent_id → empty
    other_agent_id = str(uuid.uuid4())
    filtered_other = list_cli_tokens(client, superuser_token_headers, agent_id=other_agent_id)
    assert filtered_other == []

    # ── Phase 9: Revoke CLI token ─────────────────────────────────────────
    result = revoke_cli_token(client, superuser_token_headers, cli_token_id)
    assert "message" in result
    assert "revoked" in result["message"].lower()

    # ── Phase 10: Revoked token no longer appears in active list ────────
    tokens = list_cli_tokens(client, superuser_token_headers)
    assert len(tokens) == 0

    # ── Phase 11: Create setup token for another user's agent → 400 ───────
    other_user = create_random_user(client)
    other_headers = user_authentication_headers(
        client=client,
        email=other_user["email"],
        password=other_user["_password"],
    )
    r = client.post(
        f"{_BASE}/setup-tokens",
        headers=other_headers,
        json={"agent_id": agent_id},
    )
    assert r.status_code == 400

    # ── Phase 12: Unauthenticated setup token creation → 401/403 ──────────
    r = client.post(
        f"{_BASE}/setup-tokens",
        json={"agent_id": agent_id},
    )
    assert r.status_code in (401, 403)

    # ── Phase 13: Revoke non-existent token → 404 ─────────────────────────
    ghost_id = str(uuid.uuid4())
    r = client.delete(f"{_BASE}/tokens/{ghost_id}", headers=superuser_token_headers)
    assert r.status_code == 404

    # ── Phase 14: Revoke another user's token ─────────────────────────────
    # Create a fresh token for the superuser, then try to revoke it as other user
    token_resp2 = create_setup_token(client, superuser_token_headers, agent_id)
    exchange2 = exchange_setup_token(client, token_resp2["token"], machine_name="Second Machine")
    cli_jwt2 = exchange2["cli_token"]
    tokens2 = list_cli_tokens(client, superuser_token_headers)
    assert len(tokens2) == 1  # only the new (non-revoked) token
    fresh_token_id = tokens2[0]["id"]

    r = client.delete(f"{_BASE}/tokens/{fresh_token_id}", headers=other_headers)
    assert r.status_code == 404


# ── Scenario 2: CLI-authenticated endpoint access ────────────────────────────

def test_cli_authenticated_endpoints(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    CLI-authenticated endpoint access:
      1. Create agent, setup token, exchange → obtain CLI JWT
      2. GET build-context → 200, tar+gzip bytes returned
      3. GET credentials → 200, response has "credentials" key
      4. GET building-context → 200, has "building_prompt" (minimal fallback)
      5. GET workspace → 404 (no active environment in test)
      6. GET workspace/manifest → 404 (no active environment in test)
      7. POST knowledge/search → 200, has "results" key
      8. Scoping guard: access different agent's endpoints with the token → 403
      9. Auth guard: plain user JWT rejected by CLI endpoints → 401
     10. Revoke token, then use it → 401
    """
    # ── Phase 1: Bootstrap CLI token ─────────────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    token_resp = create_setup_token(client, superuser_token_headers, agent_id)
    exchange = exchange_setup_token(client, token_resp["token"], machine_name="CI Machine")
    cli_jwt = exchange["cli_token"]
    cli_headers = cli_auth_headers(cli_jwt)

    # Fetch token id for later revocation (phase 10)
    tokens = list_cli_tokens(client, superuser_token_headers)
    assert len(tokens) == 1
    cli_token_id = tokens[0]["id"]

    # ── Phase 2: GET build-context → 200 ──────────────────────────────────
    r = client.get(f"{_BASE}/agents/{agent_id}/build-context", headers=cli_headers)
    assert r.status_code == 200
    # Should be a tar.gz stream — at minimum non-empty bytes
    assert len(r.content) > 0
    # Content-Disposition should reference the agent name
    content_disposition = r.headers.get("content-disposition", "")
    assert "build-context.tar.gz" in content_disposition

    # ── Phase 3: GET credentials → 200 ───────────────────────────────────
    r = client.get(f"{_BASE}/agents/{agent_id}/credentials", headers=cli_headers)
    assert r.status_code == 200
    body = r.json()
    assert "credentials" in body
    assert isinstance(body["credentials"], list)

    # ── Phase 4: GET building-context → 200 (minimal fallback) ───────────
    r = client.get(f"{_BASE}/agents/{agent_id}/building-context", headers=cli_headers)
    assert r.status_code == 200
    body = r.json()
    # No live Docker environment in tests → _minimal_building_context is returned
    assert "building_prompt" in body
    assert "settings" in body

    # ── Phase 5: GET workspace → 404 (no active environment) ─────────────
    r = client.get(f"{_BASE}/agents/{agent_id}/workspace", headers=cli_headers)
    assert r.status_code == 404

    # ── Phase 6: GET workspace/manifest → 404 (no active environment) ─────
    r = client.get(f"{_BASE}/agents/{agent_id}/workspace/manifest", headers=cli_headers)
    assert r.status_code == 404

    # ── Phase 7: POST knowledge/search → 200 ─────────────────────────────
    r = client.post(
        f"{_BASE}/agents/{agent_id}/knowledge/search",
        headers=cli_headers,
        json={"query": "test query", "topic": None},
    )
    assert r.status_code == 200
    body = r.json()
    assert "results" in body
    assert isinstance(body["results"], list)
    # No knowledge sources configured in test → empty results expected
    assert body["results"] == []

    # ── Phase 8: Scoping guard — wrong agent_id → 403 ────────────────────
    other_agent = create_agent_via_api(client, superuser_token_headers)
    other_agent_id = other_agent["id"]

    # CLI token is scoped to `agent_id`, so accessing `other_agent_id` → 403
    r = client.get(
        f"{_BASE}/agents/{other_agent_id}/build-context",
        headers=cli_headers,
    )
    assert r.status_code == 403

    r = client.get(
        f"{_BASE}/agents/{other_agent_id}/credentials",
        headers=cli_headers,
    )
    assert r.status_code == 403

    r = client.get(
        f"{_BASE}/agents/{other_agent_id}/building-context",
        headers=cli_headers,
    )
    assert r.status_code == 403

    # ── Phase 9: Plain user JWT rejected by CLI dep → 401 ────────────────
    # The CLIContextDep decodes the token and checks token_type == "cli"
    # A regular user JWT has no token_type claim → 401
    r = client.get(
        f"{_BASE}/agents/{agent_id}/build-context",
        headers=superuser_token_headers,
    )
    assert r.status_code == 401

    r = client.get(
        f"{_BASE}/agents/{agent_id}/credentials",
        headers=superuser_token_headers,
    )
    assert r.status_code == 401

    # ── Phase 10: Revoke token, then use it → 401 ─────────────────────────
    revoke_cli_token(client, superuser_token_headers, cli_token_id)

    r = client.get(
        f"{_BASE}/agents/{agent_id}/build-context",
        headers=cli_headers,
    )
    assert r.status_code == 401
    assert "revoked" in r.json()["detail"].lower()

    r = client.get(
        f"{_BASE}/agents/{agent_id}/credentials",
        headers=cli_headers,
    )
    assert r.status_code == 401
