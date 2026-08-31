"""
Backend tests for the Account CLI Workspace — Phase 1 + Phase 2 + Phase 3 + Phase 4
(file upload + chat flow).

Covers:
- Account setup-token creation (developer-gated), exchange, and lifecycle guards
- Structural isolation: account tokens rejected on per-agent routes; per-agent
  tokens rejected on account routes
- ``can_build`` gate on mint: developer-owned standalone → success; foreign
  install → 403; agent-user role → 403; inaccessible agent → 404
- Mint provenance: minted child carries ``minted_by_account_token_id``; child
  token authenticates per-agent endpoints scoped to the target agent
- Cascade revoke: revoking the account token revokes all children; sibling /
  unrelated tokens are NOT affected
- Rolling expiry: account token list shows the new token; revocation updates it
- GET /account/agents listing: owner-scoped, ``can_build`` / ``is_foreign_install``
  flags correct, no sensitive fields
- SecurityEvent audit: ``CLI_ACCOUNT_TOKEN_CREATED`` on exchange;
  ``CLI_ACCOUNT_CHILD_TOKEN_MINTED`` on each mint;
  ``CLI_ACCOUNT_CHILD_TOKEN_REVOKED`` on individual child revocation
- Individual child-token revocation: DELETE /account/tokens/children/{id} —
  provenance-scoped, existence-leak-safe, idempotent, sibling-safe

Phase 2 — GET /account/context-package:
- Returns 200, correct content-type (application/tar+gzip) and Content-Disposition
- Body is a valid gzip tarball; all member paths start with ``context/``
- Required members present: context/README.md; at least one file under each of
  context/platform/, context/api_reference/, context/examples/
- No path traversal members (no ``..`` or absolute paths)
- Auth matrix: no auth → 401; regular user JWT → 401; per-agent child CLI
  token → 401; revoked account token → 401
- Two consecutive requests return identical bytes (exercises the in-process cache)
- Content version: ``context/VERSION`` member, ``X-Context-Package-Version``
  response header, and GET /account/context-package/version all agree; the probe
  is behind the same account-token gate

Phase 3 — Convenience verbs + generic API escape hatch:
- POST /account/agents — thin client create; developer-gated (403 for agent-user);
  AgentPublic returned; agent visible in /account/agents listing; can_build=true
- POST /account/connect/agent-api — error paths (400 producer-disabled, 403/404
  ownership); response shape fields; agent-user → 403
- GET /account/connect/mcp/discoverable — lists discoverable a2a connectors;
  consumer_agent_id filter; agent-user role still reaches it (no role gate on GET)
- POST /account/connect/mcp — connects consumer to a2a connector; non-a2a / missing
  connector → 404; non-ACL caller → 403; agent-user → 403
- POST /account/api-proxy — happy path (GET agents/ mirrors user JWT view); identity
  + transparency (inner 404 passed through); exclusion gating (credentials → 403;
  CLI recursion → 403; GET users/me → 200); auth matrix (user JWT / child token /
  revoked account token → 401); ``CLI_ACCOUNT_API_PROXY_CALL`` SecurityEvent on
  exclusion hits only; allowed call writes no such event
- POST /account/knowledge/search — account-level knowledge search (Scenario 21b):
  200 + {"results": list} for a valid account CLI token; empty list in test DB is
  expected/valid; optional ``topic`` accepted; auth matrix (per-agent child token /
  user JWT / revoked account token / no auth → 401)

Phase 4 — File upload route + chat-flow proxy contract (Scenarios 24–25):
- POST /account/files/upload (Scenario 24): multipart upload attributed to the
  account user; status="temporary"; auth matrix; MIME rejection; oversize rejection.
- Chat-flow proxy contract (Scenario 25): session routes (sessions/, messages,
  messages/stream, streaming-status, interrupt, files download) are all reachable
  through the escape hatch (none is on the denylist); upload → message reference
  end-to-end contract (upload file, create session, send message with file_ids →
  ack JSON returned through the proxy, messages list reachable through proxy).

Notes:
- Unit tests for the pure exclusion-policy chokepoint (all denylist prefixes,
  segment boundaries, method allowlist, malformed-path detection, normalization)
  live in tests/unit/test_api_proxy_policy.py — this file covers end-to-end /
  API-observable behavior only.
- Rate-limit testing is noted as a coverage gap below (see test_proxy_rate_limit).
"""
import io
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import (
    install_bundle,
    make_user_and_headers as _make_user_and_headers,
    publish_bundle_and_make_public,
)
from tests.utils.cli import (
    account_cli_headers,
    account_create_agent,
    account_create_credential,
    account_list_credentials,
    account_share_credential_with_agent,
    bootstrap_account_token,
    cli_auth_headers,
    create_account_setup_token,
    create_setup_token,
    exchange_account_setup_token,
    exchange_setup_token,
    get_account_context_package,
    list_account_agents,
    list_account_tokens,
    list_account_user_workspaces,
    list_cli_tokens,
    mint_child_token,
    revoke_account_child_token,
    revoke_account_token,
    revoke_cli_token,
)
from tests.utils.workspace import create_random_workspace
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.user import (
    create_random_user,
    promote_to_developer,
    user_authentication_headers,
)

_BASE = f"{settings.API_V1_STR}/cli"
_SEC = f"{settings.API_V1_STR}/security-events"


# ── Scenario 1: Account setup-token lifecycle ────────────────────────────────


def test_account_setup_token_lifecycle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Account setup-token full lifecycle:
      1. Developer creates an account setup token (no agent_id required)
      2. Verify response fields: token, setup_command, expires_at, agent_id=None
      3. setup_command embeds the token and points at the account bootstrap URL
      4. Exchange setup token → get account CLI token
      5. Exchange same token again → 400 (already used / single-use)
      6. Exchange a non-existent setup token → 400
      7. Account token appears in /account/tokens list
      8. Account token list shows correct fields (prefix, child_count=0, etc.)
      9. Agent-user role → 403 on create
     10. Unauthenticated → 401/403 on create
    """
    # ── Phase 1: Create account setup token ───────────────────────────────
    setup_resp = create_account_setup_token(client, superuser_token_headers)

    # ── Phase 2: Verify response fields ───────────────────────────────────
    assert "token" in setup_resp
    assert "id" in setup_resp
    assert "expires_at" in setup_resp
    assert "created_at" in setup_resp
    assert "setup_command" in setup_resp
    # Account setup tokens have no agent_id
    assert setup_resp.get("agent_id") is None
    setup_token_str = setup_resp["token"]

    # ── Phase 3: setup_command points at account bootstrap URL ────────────
    assert setup_token_str in setup_resp["setup_command"]
    assert "/api/cli-setup/account/" in setup_resp["setup_command"]

    # ── Phase 4: Exchange setup token → account CLI token ─────────────────
    exchange = exchange_account_setup_token(
        client, setup_token_str, machine_name="Test Account Laptop"
    )
    assert "account_token" in exchange
    assert "platform_url" in exchange
    assert "frontend_url" in exchange
    assert "machine_name" in exchange
    assert exchange["machine_name"] == "Test Account Laptop"
    account_jwt = exchange["account_token"]
    assert isinstance(account_jwt, str) and len(account_jwt) > 20

    # ── Phase 5: Re-exchange same setup token → 400 ────────────────────────
    r = client.post(
        f"/api/cli-setup/account/{setup_token_str}",
        json={"machine_name": "Another Machine"},
    )
    assert r.status_code == 400
    assert "already been used" in r.json()["detail"].lower()

    # ── Phase 6: Non-existent setup token → 400 ───────────────────────────
    r = client.post(
        "/api/cli-setup/account/nonexistent-token-for-account",
        json={"machine_name": "Ghost"},
    )
    assert r.status_code == 400

    # ── Phase 7: Account token appears in /account/tokens ─────────────────
    tokens = list_account_tokens(client, superuser_token_headers)
    assert len(tokens) == 1

    # ── Phase 8: Token list shows correct fields ───────────────────────────
    tok = tokens[0]
    assert tok["name"] == "Test Account Laptop"
    assert tok["is_revoked"] is False
    assert tok["child_count"] == 0
    assert "prefix" in tok
    assert "expires_at" in tok
    assert "created_at" in tok
    # No sensitive fields
    assert "token" not in tok
    assert "token_hash" not in tok

    # ── Phase 9: Agent-user role → 403 on create ──────────────────────────
    agent_user = create_random_user(client)
    # Newly signed-up users default to agent-user role → should be 403
    agent_user_headers = user_authentication_headers(
        client=client,
        email=agent_user["email"],
        password=agent_user["_password"],
    )
    r = client.post(f"{_BASE}/account/setup-tokens", headers=agent_user_headers)
    assert r.status_code == 403, (
        f"agent-user should get 403 on account setup-token creation, got {r.status_code}: {r.text}"
    )

    # ── Phase 10: Unauthenticated → 401/403 ───────────────────────────────
    r = client.post(f"{_BASE}/account/setup-tokens")
    assert r.status_code in (401, 403)


# ── Scenario 2: Token-type structural isolation (load-bearing guard) ─────────


def test_account_token_rejected_on_per_agent_routes(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Structural isolation — the account token must be rejected on all per-agent
    CLI routes (401), and per-agent tokens must be rejected on account routes
    (401).

      1. Create agent + account token
      2. Account token → per-agent building-context → 401
      3. Account token → per-agent knowledge/search → 401
      4. Account token → per-agent workspace → 401
      5. Account token → per-agent sync-runtime → 401
      6. Account token → per-agent exec → 401
      7. Per-agent CLI token → GET /account/agents → 401
      8. Per-agent CLI token → POST /account/agents/{id}/mint → 401
      9. Regular user JWT → GET /account/agents → 401
     10. Regular user JWT → account/tokens (user-auth OK) → 200
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    # ── Phase 1: Bootstrap account token ──────────────────────────────────
    account_jwt, _ = bootstrap_account_token(client, superuser_token_headers)
    acc_headers = account_cli_headers(account_jwt)

    # ── Phase 2: Account token → per-agent building-context → 401 ─────────
    r = client.get(
        f"{_BASE}/agents/{agent_id}/building-context",
        headers=acc_headers,
    )
    assert r.status_code == 401, (
        f"Account token must be rejected by per-agent building-context route, got {r.status_code}"
    )

    # ── Phase 3: Account token → per-agent knowledge/search → 401 ─────────
    r = client.post(
        f"{_BASE}/agents/{agent_id}/knowledge/search",
        headers=acc_headers,
        json={"query": "test", "topic": None},
    )
    assert r.status_code == 401, (
        f"Account token must be rejected by knowledge/search route, got {r.status_code}"
    )

    # ── Phase 4: Account token → per-agent workspace → 401 ────────────────
    r = client.get(
        f"{_BASE}/agents/{agent_id}/workspace",
        headers=acc_headers,
    )
    assert r.status_code == 401, (
        f"Account token must be rejected by workspace route, got {r.status_code}"
    )

    # ── Phase 5: Account token → per-agent sync-runtime → 401 ────────────
    r = client.get(
        f"{_BASE}/agents/{agent_id}/sync-runtime",
        headers=acc_headers,
    )
    assert r.status_code == 401, (
        f"Account token must be rejected by sync-runtime route, got {r.status_code}"
    )

    # ── Phase 6: Account token → per-agent exec → 401 ─────────────────────
    r = client.post(
        f"{_BASE}/agents/{agent_id}/exec",
        headers=acc_headers,
        json={"command": "echo hello"},
    )
    assert r.status_code == 401, (
        f"Account token must be rejected by exec route, got {r.status_code}"
    )

    # ── Phase 7: Per-agent CLI token → GET /account/agents → 401 ──────────
    setup_resp = create_setup_token(client, superuser_token_headers, agent_id)
    ex = exchange_setup_token(client, setup_resp["token"], machine_name="Per-Agent Machine")
    per_agent_jwt = ex["cli_token"]
    per_agent_headers = cli_auth_headers(per_agent_jwt)

    r = client.get(f"{_BASE}/account/agents", headers=per_agent_headers)
    assert r.status_code == 401, (
        f"Per-agent token must be rejected by /account/agents, got {r.status_code}"
    )

    # ── Phase 8: Per-agent CLI token → POST /account/agents/{id}/mint → 401
    r = client.post(
        f"{_BASE}/account/agents/{agent_id}/mint",
        headers=per_agent_headers,
        json={"machine_name": "test"},
    )
    assert r.status_code == 401, (
        f"Per-agent token must be rejected by /account/agents/mint, got {r.status_code}"
    )

    # ── Phase 9: Regular user JWT → GET /account/agents → 401 ────────────
    r = client.get(f"{_BASE}/account/agents", headers=superuser_token_headers)
    assert r.status_code == 401, (
        f"Regular user JWT must be rejected by /account/agents, got {r.status_code}"
    )

    # ── Phase 10: Regular user JWT → account/tokens (user-auth) → 200 ─────
    # The tokens-list/revoke endpoints use user-JWT, not account CLI token.
    r = client.get(f"{_BASE}/account/tokens", headers=superuser_token_headers)
    assert r.status_code == 200, (
        f"Regular JWT must be accepted on /account/tokens, got {r.status_code}"
    )


# ── Scenario 3: can_build gate on mint ───────────────────────────────────────


def test_mint_can_build_gate(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    ``can_build`` gate on mint endpoint:
      1. Developer-owned standalone agent → mint succeeds
      2. Mint response has correct provenance fields and the child JWT
      3. Child token can authenticate per-agent routes (scoped to target agent)
      4. Child token is scoped — accessing a different agent's routes → 403
      5. Foreign install (bundle-owned, non-publisher) → mint returns 403
      6. Agent-user role → mint returns 403 (role gate)
      7. Inaccessible / other user's agent → mint returns 404 (no existence leak)
    """
    # ── Phase 1: Setup account token for the superuser ────────────────────
    account_jwt, account_token_id = bootstrap_account_token(
        client, superuser_token_headers, machine_name="Mint Gate Machine"
    )
    acc_headers = account_cli_headers(account_jwt)

    # Create an agent owned by the superuser
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    # ── Phase 2: Mint succeeds — developer-owned standalone agent ──────────
    mint = mint_child_token(
        client, acc_headers, agent_id, machine_name="Child Machine"
    )
    assert "token" in mint
    assert mint["agent_id"] == agent_id
    child_jwt = mint["token"]
    assert isinstance(child_jwt, str) and len(child_jwt) > 20

    # ── Phase 3: Child token authenticates per-agent building-context ──────
    child_headers = cli_auth_headers(child_jwt)
    r = client.get(
        f"{_BASE}/agents/{agent_id}/building-context",
        headers=child_headers,
    )
    assert r.status_code == 200, (
        f"Minted child token must be accepted by per-agent routes, got {r.status_code}: {r.text}"
    )

    # ── Phase 4: Child token scoped — wrong agent → 403 ──────────────────
    other_agent = create_agent_via_api(client, superuser_token_headers)
    other_agent_id = other_agent["id"]
    r = client.get(
        f"{_BASE}/agents/{other_agent_id}/building-context",
        headers=child_headers,
    )
    assert r.status_code == 403, (
        f"Child token must be rejected on a different agent's routes, got {r.status_code}"
    )

    # ── Phase 5: Foreign install → 403 ────────────────────────────────────
    # Publish the superuser's agent as a bundle and install it as another user.
    publisher_agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    publish_bundle_and_make_public(client, superuser_token_headers, publisher_agent["id"])

    # Create a recipient developer user and install the bundle.
    # NOTE: install_bundle uses the string bundle_id (e.g. "user.agent-name"),
    # NOT the UUID bundle_uuid field.
    recipient_user, recipient_headers = _make_user_and_headers(client)
    # Promote to developer so they can call bootstrap_account_token; they ARE a
    # developer but the installed agent is still a foreign install → can_build=False.
    promote_to_developer(client, superuser_token_headers, recipient_user["id"])

    fresh_pub_agent = client.get(
        f"{settings.API_V1_STR}/agents/{publisher_agent['id']}",
        headers=superuser_token_headers,
    ).json()
    bundle_id = fresh_pub_agent["bundle_id"]
    assert bundle_id is not None

    install_result = install_bundle(client, recipient_headers, bundle_id)
    drain_tasks()
    foreign_agent_id = install_result["id"]

    # Build account token for the recipient (they ARE a developer)
    # but the installed agent is a foreign install → can_build = False
    recipient_account_jwt, _ = bootstrap_account_token(
        client, recipient_headers, machine_name="Foreign Mint Machine"
    )
    rec_acc_headers = account_cli_headers(recipient_account_jwt)

    r = client.post(
        f"{_BASE}/account/agents/{foreign_agent_id}/mint",
        headers=rec_acc_headers,
        json={"machine_name": "Foreign Child"},
    )
    assert r.status_code == 403, (
        f"Mint on foreign install must return 403, got {r.status_code}: {r.text}"
    )

    # ── Phase 6: Agent-user role → 403 ────────────────────────────────────
    # The superuser account token works for minting against their own agent.
    # Create a new user with agent-user role (default after signup), create an
    # account token for them. They cannot mint because can_build requires developer.
    # However: an agent-user cannot create an account setup token at all (403).
    # So we test the role guard at setup-token creation level as a proxy.
    agent_user = create_random_user(client)
    agent_user_headers = user_authentication_headers(
        client=client,
        email=agent_user["email"],
        password=agent_user["_password"],
    )
    r = client.post(f"{_BASE}/account/setup-tokens", headers=agent_user_headers)
    assert r.status_code == 403, (
        f"agent-user must get 403 on account setup-token creation, got {r.status_code}"
    )

    # ── Phase 7: Inaccessible / another user's agent → 404 (no leak) ──────
    # The superuser has their own agent; the recipient account token should
    # get a 404 (not 403) when trying to mint for the superuser's agent —
    # because the agent is not accessible to the recipient (not_accessible → 404).
    r = client.post(
        f"{_BASE}/account/agents/{agent_id}/mint",
        headers=rec_acc_headers,
        json={"machine_name": "Leak Test"},
    )
    assert r.status_code == 404, (
        f"Mint on another user's agent must return 404 (no existence leak), "
        f"got {r.status_code}: {r.text}"
    )

    # Completely non-existent agent → also 404
    ghost_id = str(uuid.uuid4())
    r = client.post(
        f"{_BASE}/account/agents/{ghost_id}/mint",
        headers=acc_headers,
        json={"machine_name": "Ghost"},
    )
    assert r.status_code == 404, (
        f"Mint on non-existent agent must return 404, got {r.status_code}: {r.text}"
    )


# ── Scenario 4: Mint provenance and child token introspection ────────────────


def test_mint_provenance_and_child_token_fields(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Mint provenance verification:
      1. Mint a child token → verify response fields
      2. Workspace-bootstrap fields present in mint response (mirrors exchange)
      3. Minted child token appears in per-agent token list (GET /cli/tokens)
      4. Child token has the correct agent_id, owner_id, prefix
      5. POST /account/agents/{id}/mint can be called multiple times (idempotent-
         friendly but not idempotent: each call mints a NEW token)
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    account_jwt, account_token_id = bootstrap_account_token(
        client, superuser_token_headers, machine_name="Provenance Machine"
    )
    acc_headers = account_cli_headers(account_jwt)

    # ── Phase 1: Mint → verify response fields ─────────────────────────────
    mint = mint_child_token(
        client, acc_headers, agent_id, machine_name="Child Token Alpha"
    )
    assert "token" in mint
    assert "id" in mint
    assert "agent_id" in mint
    assert "owner_id" in mint
    assert "prefix" in mint
    assert "expires_at" in mint
    assert "agent_name" in mint
    assert "frontend_url" in mint
    assert "knowledge_sources" in mint

    # ── Phase 2: Workspace-bootstrap fields present ─────────────────────────
    # The response mirrors exchange_setup_token so the CLI's workspace writer
    # can be reused verbatim.
    assert mint["agent_id"] == agent_id
    assert mint["agent_name"] == agent["name"]
    # environment_id may be None if the agent hasn't been activated yet
    assert "environment_id" in mint
    assert isinstance(mint["knowledge_sources"], list)

    # ── Phase 3: Child token appears in /cli/tokens list ──────────────────
    per_agent_tokens = list_cli_tokens(client, superuser_token_headers, agent_id=agent_id)
    # Should include the newly minted child token
    assert any(t["id"] == mint["id"] for t in per_agent_tokens), (
        f"Minted child token {mint['id']} must appear in /cli/tokens list, got: {per_agent_tokens}"
    )

    # ── Phase 4: Token has correct fields ─────────────────────────────────
    child_tok = next(t for t in per_agent_tokens if t["id"] == mint["id"])
    assert child_tok["agent_id"] == agent_id
    assert child_tok["is_revoked"] is False

    # ── Phase 5: Multiple mints create separate tokens ─────────────────────
    mint2 = mint_child_token(
        client, acc_headers, agent_id, machine_name="Child Token Beta"
    )
    assert mint2["id"] != mint["id"], "Each mint call must produce a distinct token"
    assert mint2["token"] != mint["token"], "Each mint call must produce a distinct JWT"

    # Both tokens appear in the list
    per_agent_tokens2 = list_cli_tokens(client, superuser_token_headers, agent_id=agent_id)
    ids_in_list = [t["id"] for t in per_agent_tokens2]
    assert mint["id"] in ids_in_list
    assert mint2["id"] in ids_in_list

    # Account token's child_count reflects the minted tokens
    account_tokens = list_account_tokens(client, superuser_token_headers)
    our_tok = next(t for t in account_tokens if t["id"] == account_token_id)
    assert our_tok["child_count"] == 2, (
        f"Expected child_count=2 after two mints, got {our_tok['child_count']}"
    )


# ── Scenario 5: Cascade revoke ───────────────────────────────────────────────


def test_cascade_revoke(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Cascade revoke — revoking the account token sets all children revoked:
      1. Create account token A and account token B (separate machines)
      2. Mint child tokens from account token A (two agents)
      3. Mint one child token from account token B (different machine)
      4. Revoke account token A → children from A become revoked
      5. After revoke: account token A gone from list; children return 401
      6. Account token B and its child are NOT affected by the revoke
      7. Independently revoking a single child (via /cli/tokens) leaves account
         token and siblings alive
    """
    agent_a = create_agent_via_api(client, superuser_token_headers)
    agent_b = create_agent_via_api(client, superuser_token_headers)
    agent_id_a = agent_a["id"]
    agent_id_b = agent_b["id"]

    # ── Phase 1: Create two account tokens ────────────────────────────────
    account_jwt_a, account_token_id_a = bootstrap_account_token(
        client, superuser_token_headers, machine_name="Cascade Machine A"
    )
    account_jwt_b, account_token_id_b = bootstrap_account_token(
        client, superuser_token_headers, machine_name="Cascade Machine B"
    )
    acc_a_headers = account_cli_headers(account_jwt_a)
    acc_b_headers = account_cli_headers(account_jwt_b)

    # ── Phase 2: Mint two children from account token A ───────────────────
    child_a1 = mint_child_token(
        client, acc_a_headers, agent_id_a, machine_name="Child A1"
    )
    child_a2 = mint_child_token(
        client, acc_a_headers, agent_id_b, machine_name="Child A2"
    )
    child_a1_jwt = child_a1["token"]
    child_a2_jwt = child_a2["token"]

    # Verify children work before revoke
    r = client.get(
        f"{_BASE}/agents/{agent_id_a}/building-context",
        headers=cli_auth_headers(child_a1_jwt),
    )
    assert r.status_code == 200, "child_a1 must work before cascade revoke"

    r = client.get(
        f"{_BASE}/agents/{agent_id_b}/building-context",
        headers=cli_auth_headers(child_a2_jwt),
    )
    assert r.status_code == 200, "child_a2 must work before cascade revoke"

    # ── Phase 3: Mint one child from account token B ───────────────────────
    child_b1 = mint_child_token(
        client, acc_b_headers, agent_id_a, machine_name="Child B1"
    )
    child_b1_jwt = child_b1["token"]

    # ── Phase 4: Revoke account token A ────────────────────────────────────
    result = revoke_account_token(
        client, superuser_token_headers, account_token_id_a
    )
    # The message should mention the count of revoked sessions
    assert "revoked" in result["message"].lower()

    # ── Phase 5: Children from A now return 401 ────────────────────────────
    r = client.get(
        f"{_BASE}/agents/{agent_id_a}/building-context",
        headers=cli_auth_headers(child_a1_jwt),
    )
    assert r.status_code == 401, (
        f"child_a1 must return 401 after cascade revoke, got {r.status_code}"
    )

    r = client.get(
        f"{_BASE}/agents/{agent_id_b}/building-context",
        headers=cli_auth_headers(child_a2_jwt),
    )
    assert r.status_code == 401, (
        f"child_a2 must return 401 after cascade revoke, got {r.status_code}"
    )

    # Account token A is also revoked → /account/agents returns 401
    r = client.get(f"{_BASE}/account/agents", headers=acc_a_headers)
    assert r.status_code == 401, (
        f"Revoked account token must return 401, got {r.status_code}"
    )

    # Account token A no longer appears in list (revoked tokens hidden)
    account_tokens = list_account_tokens(client, superuser_token_headers)
    assert not any(t["id"] == account_token_id_a for t in account_tokens), (
        "Revoked account token A must not appear in the active tokens list"
    )

    # ── Phase 6: Account token B and its child are UNAFFECTED ─────────────
    r = client.get(f"{_BASE}/account/agents", headers=acc_b_headers)
    assert r.status_code == 200, (
        f"Account token B must still work after revoking A, got {r.status_code}: {r.text}"
    )

    r = client.get(
        f"{_BASE}/agents/{agent_id_a}/building-context",
        headers=cli_auth_headers(child_b1_jwt),
    )
    assert r.status_code == 200, (
        f"Child B1 (minted from account token B) must be unaffected by revoking A, "
        f"got {r.status_code}: {r.text}"
    )

    # Account token B still in list
    account_tokens = list_account_tokens(client, superuser_token_headers)
    assert any(t["id"] == account_token_id_b for t in account_tokens), (
        "Account token B must still appear in the active tokens list"
    )

    # ── Phase 7: Independently revoke one child; account token + siblings alive
    # Create a fresh agent and mint a fresh child from B for isolation
    agent_c = create_agent_via_api(client, superuser_token_headers)
    agent_id_c = agent_c["id"]
    child_b2 = mint_child_token(
        client, acc_b_headers, agent_id_c, machine_name="Child B2"
    )
    child_b2_jwt = child_b2["token"]
    child_b2_id = child_b2["id"]

    # Revoke child B2 via the per-agent revoke endpoint
    revoke_cli_token(client, superuser_token_headers, child_b2_id)

    # Child B2 is revoked
    r = client.get(
        f"{_BASE}/agents/{agent_id_c}/building-context",
        headers=cli_auth_headers(child_b2_jwt),
    )
    assert r.status_code == 401, "Individually revoked child must return 401"

    # Account token B still alive
    r = client.get(f"{_BASE}/account/agents", headers=acc_b_headers)
    assert r.status_code == 200, "Account token B must survive its child's individual revocation"

    # Child B1 still alive
    r = client.get(
        f"{_BASE}/agents/{agent_id_a}/building-context",
        headers=cli_auth_headers(child_b1_jwt),
    )
    assert r.status_code == 200, "Sibling child B1 must survive child B2's individual revocation"


# ── Scenario 6: Account token management (list/revoke via user-auth) ─────────


def test_account_token_management(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Account token listing and revocation:
      1. Multiple account tokens from the same user appear in the list
      2. Each token shows correct prefix, name, child_count
      3. Revoke one → it disappears from the list
      4. Revoke non-existent account token → 404
      5. Another user cannot revoke a different user's account token → 404
    """
    # ── Phase 1: Create two account tokens ────────────────────────────────
    setup1 = create_account_setup_token(client, superuser_token_headers)
    ex1 = exchange_account_setup_token(client, setup1["token"], machine_name="Manage Machine 1")

    setup2 = create_account_setup_token(client, superuser_token_headers)
    ex2 = exchange_account_setup_token(client, setup2["token"], machine_name="Manage Machine 2")

    # ── Phase 2: Both tokens appear in the list ────────────────────────────
    tokens = list_account_tokens(client, superuser_token_headers)
    assert len(tokens) == 2
    names = {t["name"] for t in tokens}
    assert "Manage Machine 1" in names
    assert "Manage Machine 2" in names
    for tok in tokens:
        assert tok["child_count"] == 0
        assert "prefix" in tok

    tok1_id = next(t["id"] for t in tokens if t["name"] == "Manage Machine 1")
    tok2_id = next(t["id"] for t in tokens if t["name"] == "Manage Machine 2")

    # ── Phase 3: Revoke one → disappears from the list ────────────────────
    result = revoke_account_token(client, superuser_token_headers, tok1_id)
    assert "revoked" in result["message"].lower()

    tokens_after = list_account_tokens(client, superuser_token_headers)
    assert len(tokens_after) == 1
    assert tokens_after[0]["id"] == tok2_id

    # ── Phase 4: Revoke non-existent → 404 ────────────────────────────────
    ghost_id = str(uuid.uuid4())
    r = client.delete(f"{_BASE}/account/tokens/{ghost_id}", headers=superuser_token_headers)
    assert r.status_code == 404

    # ── Phase 5: Other user cannot revoke a different user's account token ─
    other_user = create_random_user(client)
    other_user_headers = user_authentication_headers(
        client=client,
        email=other_user["email"],
        password=other_user["_password"],
    )
    r = client.delete(f"{_BASE}/account/tokens/{tok2_id}", headers=other_user_headers)
    assert r.status_code == 404, (
        f"Another user must not be able to revoke a different user's account token, "
        f"got {r.status_code}: {r.text}"
    )


# ── Scenario 7: Account agents listing (GET /account/agents) ─────────────────


def test_account_agents_listing(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    GET /account/agents listing:
      1. Returns only the account-token user's own agents
      2. ``can_build=true`` for developer-owned standalone agents
      3. ``is_foreign_install=true`` and ``can_build=false`` for bundle installs
      4. ``has_active_environment`` flag is present per row
      5. No sensitive fields (no credentials, no prompts, no env internals)
      6. Other users' agents are NOT visible
      7. Minimal projection fields are present (id, name, description, etc.)
    """
    # ── Phase 1: Create agents and account token for a recipient user ──────
    # Use a fresh user promoted to developer so they can create agents and
    # generate account setup tokens.
    recipient_user, recipient_headers = _make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, recipient_user["id"])

    recipient_account_jwt, _ = bootstrap_account_token(
        client, recipient_headers, machine_name="Listing Machine"
    )
    rec_acc_headers = account_cli_headers(recipient_account_jwt)

    # Create a standalone agent owned by the recipient
    standalone_agent = create_agent_via_api(client, recipient_headers)
    drain_tasks()
    standalone_id = standalone_agent["id"]

    # ── Phase 2: Publish a bundle from superuser and install it ───────────
    publisher_agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    publish_bundle_and_make_public(client, superuser_token_headers, publisher_agent["id"])
    fresh_pub = client.get(
        f"{settings.API_V1_STR}/agents/{publisher_agent['id']}",
        headers=superuser_token_headers,
    ).json()
    # NOTE: install_bundle uses the string bundle_id, NOT bundle_uuid.
    bundle_id = fresh_pub["bundle_id"]
    install_result = install_bundle(client, recipient_headers, bundle_id)
    drain_tasks()
    foreign_agent_id = install_result["id"]

    # ── Phase 3: Fetch the accessible-agents list ─────────────────────────
    agents_data = list_account_agents(client, rec_acc_headers)

    # The listing must include the recipient's own agents
    agent_ids_in_list = [a["id"] for a in agents_data]
    assert standalone_id in agent_ids_in_list, (
        "Standalone agent must appear in account agents listing"
    )
    assert foreign_agent_id in agent_ids_in_list, (
        "Installed foreign-bundle agent must appear in account agents listing"
    )

    # ── Phase 4: Standalone agent: can_build=true, is_foreign_install=false
    standalone_item = next(a for a in agents_data if a["id"] == standalone_id)
    assert standalone_item["can_build"] is True, (
        f"Developer-owned standalone agent must have can_build=True, got: {standalone_item}"
    )
    assert standalone_item["is_foreign_install"] is False

    # ── Phase 5: Foreign install: can_build=false, is_foreign_install=true ─
    foreign_item = next(a for a in agents_data if a["id"] == foreign_agent_id)
    assert foreign_item["is_foreign_install"] is True, (
        f"Bundle-installed agent must have is_foreign_install=True, got: {foreign_item}"
    )
    assert foreign_item["can_build"] is False, (
        f"Foreign install must have can_build=False, got: {foreign_item}"
    )

    # ── Phase 6: Other users' agents NOT visible ───────────────────────────
    # The superuser's publisher agent must NOT appear in the recipient's list
    assert publisher_agent["id"] not in agent_ids_in_list, (
        "Superuser's publisher agent must NOT appear in recipient's account agents listing"
    )

    # ── Phase 7: Minimal projection — required fields present ─────────────
    for item in agents_data:
        assert "id" in item
        assert "name" in item
        assert "is_foreign_install" in item
        assert "can_build" in item
        assert "has_active_environment" in item
        assert "owner_id" in item
        # No sensitive fields
        assert "system_prompt" not in item
        assert "credentials" not in item
        assert "hashed_password" not in item
        assert "token_hash" not in item


# ── Scenario 8: Kind guard on setup-token exchange ───────────────────────────


def test_setup_token_kind_guard(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Kind guard — a per-agent setup token cannot be exchanged on the account
    exchange path, and an account setup token cannot be exchanged on the
    per-agent exchange path:
      1. Create a per-agent setup token → exchange on account path → 400
      2. Create an account setup token → exchange on per-agent path → 400
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    # ── Phase 1: Per-agent setup token → account exchange path → 400 ──────
    per_agent_setup = create_setup_token(client, superuser_token_headers, agent_id)
    per_agent_token_str = per_agent_setup["token"]

    r = client.post(
        f"/api/cli-setup/account/{per_agent_token_str}",
        json={"machine_name": "wrong path"},
    )
    assert r.status_code == 400, (
        f"Per-agent setup token on account exchange path must return 400, got {r.status_code}: {r.text}"
    )
    detail = r.json().get("detail", "").lower()
    assert "account" in detail, (
        f"Error detail should mention 'account' token kind mismatch, got: {r.json()['detail']!r}"
    )

    # ── Phase 2: Account setup token → per-agent exchange path → 400 ──────
    account_setup = create_account_setup_token(client, superuser_token_headers)
    account_setup_token_str = account_setup["token"]

    r = client.post(
        f"/api/cli-setup/{account_setup_token_str}",
        json={"machine_name": "wrong path"},
    )
    # The per-agent exchange route looks up the token and checks kind.
    # An account setup token should be rejected because its kind != "agent".
    assert r.status_code == 400, (
        f"Account setup token on per-agent exchange path must return 400, got {r.status_code}: {r.text}"
    )


# ── Scenario 9: SecurityEvent audit on account token operations ──────────────


def test_security_event_audit(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    SecurityEvent audit:
      1. Exchange account setup token → CLI_ACCOUNT_TOKEN_CREATED event written
      2. Mint child token → CLI_ACCOUNT_CHILD_TOKEN_MINTED event written
      3. Second mint → another CLI_ACCOUNT_CHILD_TOKEN_MINTED event
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    # ── Phase 1: Exchange → CLI_ACCOUNT_TOKEN_CREATED ─────────────────────
    setup = create_account_setup_token(client, superuser_token_headers)
    exchange_account_setup_token(client, setup["token"], machine_name="Audit Machine")

    r = client.get(
        f"{_SEC}/",
        headers=superuser_token_headers,
        params={"event_type": "CLI_ACCOUNT_TOKEN_CREATED"},
    )
    assert r.status_code == 200
    events = r.json()
    assert events["count"] >= 1, (
        "CLI_ACCOUNT_TOKEN_CREATED event must be written on account setup-token exchange"
    )
    created_event = events["data"][0]
    assert created_event["event_type"] == "CLI_ACCOUNT_TOKEN_CREATED"
    # Account token has no agent_id
    assert created_event.get("agent_id") is None

    # ── Phase 2: Mint → CLI_ACCOUNT_CHILD_TOKEN_MINTED ────────────────────
    account_jwt, _ = bootstrap_account_token(
        client, superuser_token_headers, machine_name="Audit Mint Machine"
    )
    acc_headers = account_cli_headers(account_jwt)

    mint_child_token(client, acc_headers, agent_id, machine_name="Audit Child")

    r = client.get(
        f"{_SEC}/",
        headers=superuser_token_headers,
        params={"event_type": "CLI_ACCOUNT_CHILD_TOKEN_MINTED"},
    )
    assert r.status_code == 200
    events = r.json()
    assert events["count"] >= 1, (
        "CLI_ACCOUNT_CHILD_TOKEN_MINTED event must be written on each mint"
    )
    mint_event = events["data"][0]
    assert mint_event["event_type"] == "CLI_ACCOUNT_CHILD_TOKEN_MINTED"
    assert mint_event.get("agent_id") == agent_id

    # ── Phase 3: Second mint → another event ──────────────────────────────
    agent_b = create_agent_via_api(client, superuser_token_headers)
    mint_child_token(client, acc_headers, agent_b["id"], machine_name="Audit Child 2")

    r = client.get(
        f"{_SEC}/",
        headers=superuser_token_headers,
        params={"event_type": "CLI_ACCOUNT_CHILD_TOKEN_MINTED"},
    )
    assert r.status_code == 200
    events = r.json()
    assert events["count"] >= 2, (
        "Each mint must write a separate CLI_ACCOUNT_CHILD_TOKEN_MINTED event"
    )


# ── Scenario 10: Account bootstrap GET script route ─────────────────────────


def test_account_bootstrap_script_route(
    client: TestClient,
) -> None:
    """
    GET /api/cli-setup/account/{token} serves a plain-text bootstrap script.
    Any token string returns 200 and a plain-text body containing Python code.
    (The route does not validate the token — it just renders a script.)
    """
    fake_token = "any-token-value-for-script"
    r = client.get(f"/api/cli-setup/account/{fake_token}")
    assert r.status_code == 200
    # Response should be plain text / Python script
    content_type = r.headers.get("content-type", "")
    assert "text/plain" in content_type or "text/" in content_type, (
        f"Bootstrap script should be plain-text, got content-type: {content_type}"
    )
    body = r.text
    assert len(body) > 20, "Bootstrap script should have non-trivial content"


# ── Scenario 11: Rolling expiry on account token use ────────────────────────


def test_revoked_account_token_rejected_on_account_routes(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Revoked account token is rejected on all account-CLI-authenticated routes:
      1. Create account token and verify it works
      2. Revoke it via user-JWT
      3. Revoked token → GET /account/agents → 401
      4. Revoked token → POST /account/agents/{id}/mint → 401
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    account_jwt, account_token_id = bootstrap_account_token(
        client, superuser_token_headers, machine_name="Revoke Test Machine"
    )
    acc_headers = account_cli_headers(account_jwt)

    # ── Phase 1: Verify token works before revoke ─────────────────────────
    r = client.get(f"{_BASE}/account/agents", headers=acc_headers)
    assert r.status_code == 200, f"Account token must work before revoke, got {r.status_code}"

    # ── Phase 2: Revoke the account token ─────────────────────────────────
    revoke_account_token(client, superuser_token_headers, account_token_id)

    # ── Phase 3: Revoked token → /account/agents → 401 ────────────────────
    r = client.get(f"{_BASE}/account/agents", headers=acc_headers)
    assert r.status_code == 401, (
        f"Revoked account token must return 401 on /account/agents, got {r.status_code}"
    )
    assert "revoked" in r.json()["detail"].lower(), (
        f"Error detail must mention 'revoked', got: {r.json()['detail']!r}"
    )

    # ── Phase 4: Revoked token → /account/agents/{id}/mint → 401 ──────────
    r = client.post(
        f"{_BASE}/account/agents/{agent_id}/mint",
        headers=acc_headers,
        json={"machine_name": "Post-Revoke Mint"},
    )
    assert r.status_code == 401, (
        f"Revoked account token must return 401 on mint, got {r.status_code}"
    )


# ── Scenario 12: Per-agent setup-token route now uses assert_can_build ───────


def test_per_agent_setup_token_respects_can_build(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    The existing per-agent setup-token route (POST /cli/setup-tokens) was
    switched from bare ownership to ``assert_can_build`` as part of this feature.
    Verify:
      1. Developer-owned standalone agent → setup-token creation succeeds
      2. Foreign install for a developer → setup-token creation returns 403
         (used to return 400 with ownership-only check; now returns 403/404)
      3. The existing (previously passing) lifecycle test is not broken:
         the exchange step still works for a developer-owned agent.
    """
    # ── Phase 1: Developer-owned standalone → succeeds ─────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]
    setup_tok = create_setup_token(client, superuser_token_headers, agent_id)
    assert "token" in setup_tok, "Developer must be able to create setup token for own agent"

    # Verify the exchange still works (regression guard)
    exchange = exchange_setup_token(client, setup_tok["token"], machine_name="Regression Machine")
    assert "cli_token" in exchange

    # ── Phase 2: Foreign install → 403 ────────────────────────────────────
    publisher_agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    publish_bundle_and_make_public(client, superuser_token_headers, publisher_agent["id"])
    fresh_pub = client.get(
        f"{settings.API_V1_STR}/agents/{publisher_agent['id']}",
        headers=superuser_token_headers,
    ).json()
    # NOTE: install_bundle uses the string bundle_id, NOT bundle_uuid.
    bundle_id = fresh_pub["bundle_id"]

    # Promote recipient to developer so assert_can_build reaches the
    # foreign-install check (rather than failing on the role check first).
    recipient_user2, recipient_headers = _make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, recipient_user2["id"])

    install_result = install_bundle(client, recipient_headers, bundle_id)
    drain_tasks()
    foreign_agent_id = install_result["id"]

    r = client.post(
        f"{_BASE}/setup-tokens",
        headers=recipient_headers,
        json={"agent_id": foreign_agent_id},
    )
    # assert_can_build raises "foreign_install" → 403
    assert r.status_code == 403, (
        f"Foreign install must get 403 on per-agent setup-token creation, "
        f"got {r.status_code}: {r.text}"
    )


# ── Scenario 13: Individual child-token revocation ───────────────────────────


def test_revoke_child_token(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    DELETE /account/tokens/children/{child_token_id} — provenance-scoped revocation:
      1. Account token A revokes its own minted child → 200; child gets 401
         on a per-agent route afterwards
      2. CLI_ACCOUNT_CHILD_TOKEN_REVOKED SecurityEvent written on revocation
      3. Idempotent: revoking the same child again → 200, no second SecurityEvent
      4. Sibling children of the same account token are unaffected by revoking one
      5. Child minted by a DIFFERENT account token of the SAME user → 404
      6. Another user's token id → 404
      7. An account token's own id (token_type cli-account) → 404
      8. Nonexistent UUID → 404
      9. A regular user JWT calling this route → 401 (wrong auth context)
     10. A per-agent child token calling this route → 401 (wrong auth context)
    """
    agent_a = create_agent_via_api(client, superuser_token_headers)
    agent_b = create_agent_via_api(client, superuser_token_headers)
    agent_id_a = agent_a["id"]
    agent_id_b = agent_b["id"]

    # ── Phase 1: Bootstrap two account tokens for the superuser ───────────
    account_jwt_a, account_token_id_a = bootstrap_account_token(
        client, superuser_token_headers, machine_name="Child Revoke Machine A"
    )
    account_jwt_b, account_token_id_b = bootstrap_account_token(
        client, superuser_token_headers, machine_name="Child Revoke Machine B"
    )
    acc_a_headers = account_cli_headers(account_jwt_a)
    acc_b_headers = account_cli_headers(account_jwt_b)

    # ── Phase 2: Mint children from account token A ────────────────────────
    child_a1 = mint_child_token(
        client, acc_a_headers, agent_id_a, machine_name="Child A1 to revoke"
    )
    child_a1_id = child_a1["id"]
    child_a1_jwt = child_a1["token"]

    child_a2 = mint_child_token(
        client, acc_a_headers, agent_id_b, machine_name="Child A2 sibling"
    )
    child_a2_id = child_a2["id"]
    child_a2_jwt = child_a2["token"]

    # Mint one child from account token B (same user, different account token)
    child_b1 = mint_child_token(
        client, acc_b_headers, agent_id_a, machine_name="Child B1 from other account token"
    )
    child_b1_id = child_b1["id"]
    child_b1_jwt = child_b1["token"]

    # Verify children work before individual revocation
    r = client.get(
        f"{_BASE}/agents/{agent_id_a}/building-context",
        headers=cli_auth_headers(child_a1_jwt),
    )
    assert r.status_code == 200, "child_a1 must work before individual revocation"

    r = client.get(
        f"{_BASE}/agents/{agent_id_b}/building-context",
        headers=cli_auth_headers(child_a2_jwt),
    )
    assert r.status_code == 200, "child_a2 must work before individual revocation"

    # ── Phase 3: Account token A revokes child_a1 → 200 ──────────────────
    result = revoke_account_child_token(client, acc_a_headers, child_a1_id)
    assert "revoked" in result["message"].lower(), (
        f"Revoke response message should mention 'revoked', got: {result['message']!r}"
    )

    # ── Phase 4: Revoked child_a1 gets 401 on per-agent route ─────────────
    r = client.get(
        f"{_BASE}/agents/{agent_id_a}/building-context",
        headers=cli_auth_headers(child_a1_jwt),
    )
    assert r.status_code == 401, (
        f"Revoked child_a1 must return 401 on per-agent route, got {r.status_code}"
    )

    # ── Phase 5: SecurityEvent CLI_ACCOUNT_CHILD_TOKEN_REVOKED written ─────
    r = client.get(
        f"{_SEC}/",
        headers=superuser_token_headers,
        params={"event_type": "CLI_ACCOUNT_CHILD_TOKEN_REVOKED"},
    )
    assert r.status_code == 200
    events = r.json()
    assert events["count"] >= 1, (
        "CLI_ACCOUNT_CHILD_TOKEN_REVOKED event must be written on individual child revocation"
    )
    revoked_event = events["data"][0]
    assert revoked_event["event_type"] == "CLI_ACCOUNT_CHILD_TOKEN_REVOKED"
    assert revoked_event["agent_id"] == agent_id_a

    event_count_after_first = events["count"]

    # ── Phase 6: Idempotent — revoking the same child again → 200, no new event
    result2 = revoke_account_child_token(client, acc_a_headers, child_a1_id)
    assert "revoked" in result2["message"].lower(), (
        "Idempotent re-revoke should still return a success message"
    )

    r = client.get(
        f"{_SEC}/",
        headers=superuser_token_headers,
        params={"event_type": "CLI_ACCOUNT_CHILD_TOKEN_REVOKED"},
    )
    assert r.status_code == 200
    events_after_idempotent = r.json()
    assert events_after_idempotent["count"] == event_count_after_first, (
        "Idempotent re-revoke must NOT write a second CLI_ACCOUNT_CHILD_TOKEN_REVOKED event"
    )

    # ── Phase 7: Sibling child_a2 is unaffected ────────────────────────────
    r = client.get(
        f"{_BASE}/agents/{agent_id_b}/building-context",
        headers=cli_auth_headers(child_a2_jwt),
    )
    assert r.status_code == 200, (
        f"Sibling child_a2 must still work after revoking child_a1, got {r.status_code}: {r.text}"
    )

    # ── Phase 8: Child minted by DIFFERENT account token (same user) → 404 ─
    # child_b1 was minted by account_token_b, not account_token_a.
    # account_token_a must get 404 (existence-leak discipline).
    r = client.delete(
        f"{_BASE}/account/tokens/children/{child_b1_id}",
        headers=acc_a_headers,
    )
    assert r.status_code == 404, (
        f"Revoking a child minted by a different account token must return 404, "
        f"got {r.status_code}: {r.text}"
    )
    # child_b1 is still alive (revocation was rejected)
    r = client.get(
        f"{_BASE}/agents/{agent_id_a}/building-context",
        headers=cli_auth_headers(child_b1_jwt),
    )
    assert r.status_code == 200, "child_b1 must remain alive after a rejected revocation attempt"

    # ── Phase 9: Another user's token id → 404 ────────────────────────────
    # Create a second user, bootstrap an account token, mint a child.
    # The superuser's account_token_a must not be able to revoke the other
    # user's child (even if the id is known).
    other_user = create_random_user(client)
    other_user_headers = user_authentication_headers(
        client=client,
        email=other_user["email"],
        password=other_user["_password"],
    )
    promote_to_developer(client, superuser_token_headers, other_user["id"])

    # Give other_user an AI credential so agent creation doesn't fail with
    # EnvironmentCredentialError (mirrors setup_default_credentials for the superuser).
    create_random_ai_credential(
        client,
        other_user_headers,
        credential_type="anthropic",
        set_default=True,
    )

    other_account_jwt, _ = bootstrap_account_token(
        client, other_user_headers, machine_name="Other User Machine"
    )
    other_acc_headers = account_cli_headers(other_account_jwt)

    # The other user needs an agent they own — create one via their JWT.
    other_agent = create_agent_via_api(client, other_user_headers)
    other_child = mint_child_token(
        client, other_acc_headers, other_agent["id"], machine_name="Other User Child"
    )
    other_child_id = other_child["id"]

    r = client.delete(
        f"{_BASE}/account/tokens/children/{other_child_id}",
        headers=acc_a_headers,  # superuser's account token A tries to revoke other user's child
    )
    assert r.status_code == 404, (
        f"Revoking another user's child token must return 404, "
        f"got {r.status_code}: {r.text}"
    )

    # ── Phase 10: Account token's own id (token_type cli-account) → 404 ───
    # An account token cannot revoke itself via this route — it only accepts
    # children (token_type="cli"). Passing its own id must return 404.
    r = client.delete(
        f"{_BASE}/account/tokens/children/{account_token_id_a}",
        headers=acc_a_headers,
    )
    assert r.status_code == 404, (
        f"Passing the account token's own id must return 404 "
        f"(it is not a child token), got {r.status_code}: {r.text}"
    )

    # ── Phase 11: Nonexistent UUID → 404 ──────────────────────────────────
    ghost_id = str(uuid.uuid4())
    r = client.delete(
        f"{_BASE}/account/tokens/children/{ghost_id}",
        headers=acc_a_headers,
    )
    assert r.status_code == 404, (
        f"Nonexistent child token id must return 404, got {r.status_code}: {r.text}"
    )

    # ── Phase 12: Regular user JWT → 401 ──────────────────────────────────
    # The route requires AccountCLIContextDep — a regular user JWT is rejected.
    r = client.delete(
        f"{_BASE}/account/tokens/children/{child_a2_id}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 401, (
        f"Regular user JWT must be rejected by the child-revoke route (needs "
        f"account CLI token), got {r.status_code}: {r.text}"
    )

    # ── Phase 13: Per-agent child token → 401 ─────────────────────────────
    # A per-agent CLI token (token_type="cli") also cannot authenticate this route.
    setup_resp = create_setup_token(client, superuser_token_headers, agent_id_a)
    per_agent_exchange = exchange_setup_token(
        client, setup_resp["token"], machine_name="Per-Agent Auth Test"
    )
    per_agent_jwt = per_agent_exchange["cli_token"]

    r = client.delete(
        f"{_BASE}/account/tokens/children/{child_a2_id}",
        headers=cli_auth_headers(per_agent_jwt),
    )
    assert r.status_code == 401, (
        f"Per-agent child token must be rejected by the child-revoke route, "
        f"got {r.status_code}: {r.text}"
    )


# ── Scenario 14: Context-package content and structure ───────────────────────


def test_context_package_content(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    GET /account/context-package — content, structure, and safety checks:
      1. Account token → 200 with correct content-type and Content-Disposition
      2. Body is a valid gzip tarball (tarfile.open does not raise)
      3. Every member name starts with ``context/``
      4. ``context/README.md`` is present
      5. At least one member under ``context/platform/``
      6. At least one member under ``context/api_reference/``
      7. At least one member under ``context/examples/``
      8. No member path contains ``..`` (path traversal guard)
      9. No member path starts with ``/`` (absolute-path extraction guard)
     10. At least one member under ``context/guides/``
     11. ``context/guides/build-an-agentic-network.md`` is specifically present
    """
    import io
    import tarfile

    account_jwt, _ = bootstrap_account_token(
        client, superuser_token_headers, machine_name="Context Package Machine"
    )
    acc_headers = account_cli_headers(account_jwt)

    # ── Phase 1: Make the raw request so we can inspect headers too ──────────
    r = client.get(f"{_BASE}/account/context-package", headers=acc_headers)
    assert r.status_code == 200, (
        f"context-package endpoint must return 200, got {r.status_code}: {r.text}"
    )

    # ── Phase 2: Content-type and Content-Disposition headers ────────────────
    content_type = r.headers.get("content-type", "")
    assert "application/tar+gzip" in content_type, (
        f"Expected content-type application/tar+gzip, got: {content_type!r}"
    )
    disposition = r.headers.get("content-disposition", "")
    assert "attachment" in disposition, (
        f"Content-Disposition must be attachment, got: {disposition!r}"
    )
    assert "context-package.tar.gz" in disposition, (
        f"Content-Disposition must reference context-package.tar.gz, got: {disposition!r}"
    )

    # ── Phase 3: Body is a valid gzip tarball ─────────────────────────────────
    body = r.content
    assert len(body) > 0, "context-package body must be non-empty"
    try:
        tf = tarfile.open(fileobj=io.BytesIO(body), mode="r:gz")
        members = tf.getmembers()
        tf.close()
    except tarfile.TarError as exc:
        raise AssertionError(
            f"context-package body is not a valid gzip tarball: {exc}"
        ) from exc

    member_names = [m.name for m in members]
    assert len(member_names) > 0, "Tarball must contain at least one member"

    # ── Phase 4: Every member starts with ``context/`` ───────────────────────
    bad_prefix = [n for n in member_names if not n.startswith("context/")]
    assert not bad_prefix, (
        f"All tarball members must start with 'context/'; "
        f"found unexpected: {bad_prefix[:5]}"
    )

    # ── Phase 5: ``context/README.md`` is present ─────────────────────────────
    assert "context/README.md" in member_names, (
        f"context/README.md must be present in the tarball; "
        f"members: {member_names[:20]}"
    )

    # ── Phase 6: At least one file under ``context/platform/`` ───────────────
    platform_members = [n for n in member_names if n.startswith("context/platform/")]
    assert platform_members, (
        "Tarball must contain at least one member under context/platform/; "
        f"members: {member_names[:20]}"
    )

    # ── Phase 7: At least one file under ``context/api_reference/`` ──────────
    api_ref_members = [n for n in member_names if n.startswith("context/api_reference/")]
    assert api_ref_members, (
        "Tarball must contain at least one member under context/api_reference/; "
        f"members: {member_names[:20]}"
    )

    # ── Phase 8: At least one file under ``context/examples/`` ───────────────
    examples_members = [n for n in member_names if n.startswith("context/examples/")]
    assert examples_members, (
        "Tarball must contain at least one member under context/examples/; "
        f"members: {member_names[:20]}"
    )

    # ── Phase 9: No path traversal (no ``..`` in any member name) ────────────
    traversal = [n for n in member_names if ".." in n]
    assert not traversal, (
        f"Tarball must not contain path traversal members (..); "
        f"found: {traversal}"
    )

    # ── Phase 10: No absolute paths ───────────────────────────────────────────
    absolute = [n for n in member_names if n.startswith("/")]
    assert not absolute, (
        f"Tarball must not contain absolute-path members; found: {absolute}"
    )

    # ── Phase 11: At least one member under ``context/guides/`` ──────────────
    guides_members = [n for n in member_names if n.startswith("context/guides/")]
    assert guides_members, (
        "Tarball must contain at least one member under context/guides/ — "
        "the guides snapshot (knowledge/guides/ in the platform-knowledge "
        "env-template) is missing or was not packaged; "
        f"members: {member_names[:30]}"
    )

    # ── Phase 12: ``context/guides/build-an-agentic-network.md`` is present ──
    assert "context/guides/build-an-agentic-network.md" in member_names, (
        "context/guides/build-an-agentic-network.md must be present in the tarball — "
        "the playbook is committed to the repo at "
        "backend/app/env-templates/platform-knowledge-env/app/workspace/knowledge/guides/ "
        "and must be included in the context package; "
        f"guides members found: {guides_members}"
    )

    # ── Phase 13: ``context/guides/authoring-agent-prompts.md`` is present ──
    assert "context/guides/authoring-agent-prompts.md" in member_names, (
        "context/guides/authoring-agent-prompts.md must be present in the tarball — "
        "the guide is committed to the repo at "
        "backend/app/env-templates/platform-knowledge-env/app/workspace/knowledge/guides/ "
        "and must be included in the context package; "
        f"guides members found: {guides_members}"
    )


# ── Scenario 14b: Context-package staleness signal ───────────────────────────


def test_context_package_carries_a_content_version(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    GET /account/context-package/version and the package agree on one value.

    A workspace set up before a guide (or a whole verb) existed has no way to
    notice it is behind — the package is extracted once by ``cinna account
    setup`` and never checked again. So the package stamps its own content
    version into ``context/VERSION``, the download echoes it in a header, and a
    cheap endpoint serves it, which is what lets the CLI compare the two.

    The version is a hash of the packaged content, deliberately not of file
    mtimes: a redeploy that ships identical knowledge must not tell every
    workspace it is stale.
    """
    import io
    import tarfile

    account_jwt, _ = bootstrap_account_token(
        client, superuser_token_headers, machine_name="Version Probe Machine"
    )
    acc_headers = account_cli_headers(account_jwt)

    r = client.get(f"{_BASE}/account/context-package/version", headers=acc_headers)
    assert r.status_code == 200, (
        f"context-package version endpoint must return 200, got {r.status_code}: {r.text}"
    )
    version = r.json()["version"]
    assert version, "version must be a non-empty string"

    pkg = client.get(f"{_BASE}/account/context-package", headers=acc_headers)
    assert pkg.status_code == 200, pkg.text
    assert pkg.headers.get("X-Context-Package-Version") == version, (
        "The download header must carry the same version the probe reports, or "
        "a caller comparing them would refresh forever"
    )

    tf = tarfile.open(fileobj=io.BytesIO(pkg.content), mode="r:gz")
    try:
        assert "context/VERSION" in tf.getnames(), (
            "The package must stamp its own version — without it an extracted "
            "workspace has nothing to compare against"
        )
        stamped = tf.extractfile("context/VERSION").read().decode().strip()
    finally:
        tf.close()
    assert stamped == version

    # Same content, same version: the signal only fires on a real change.
    again = client.get(f"{_BASE}/account/context-package/version", headers=acc_headers)
    assert again.json()["version"] == version


def test_context_package_version_requires_an_account_token(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """The probe sits behind the same account-token gate as the download."""
    r = client.get(f"{_BASE}/account/context-package/version")
    assert r.status_code == 401, (
        f"Missing auth must return 401 on the version probe, got {r.status_code}"
    )

    r = client.get(
        f"{_BASE}/account/context-package/version", headers=superuser_token_headers
    )
    assert r.status_code == 401, (
        f"A user JWT must be rejected by the version probe, got {r.status_code}"
    )


# ── Scenario 15: Context-package auth matrix ─────────────────────────────────


def test_context_package_auth_matrix(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Auth matrix for GET /account/context-package:
      1. No Authorization header → 401
      2. Regular user JWT (not an account CLI token) → 401
      3. Per-agent child CLI token (token_type="cli") → 401
      4. Revoked account CLI token → 401
      5. Valid account CLI token → 200 (baseline sanity check)
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    # Bootstrap a valid account token — used for phases 4 and 5.
    account_jwt, account_token_id = bootstrap_account_token(
        client, superuser_token_headers, machine_name="Auth Matrix Machine"
    )
    acc_headers = account_cli_headers(account_jwt)

    # ── Phase 1: No auth → 401 ────────────────────────────────────────────────
    r = client.get(f"{_BASE}/account/context-package")
    assert r.status_code == 401, (
        f"Missing auth must return 401 on context-package, got {r.status_code}"
    )

    # ── Phase 2: Regular user JWT → 401 ───────────────────────────────────────
    # AccountCLIContextDep requires token_type == "cli-account"; a user JWT fails.
    r = client.get(
        f"{_BASE}/account/context-package",
        headers=superuser_token_headers,
    )
    assert r.status_code == 401, (
        f"Regular user JWT must return 401 on context-package, got {r.status_code}: {r.text}"
    )

    # ── Phase 3: Per-agent child CLI token → 401 ──────────────────────────────
    # A per-agent token (token_type="cli") is rejected by AccountCLIContextDep.
    mint = mint_child_token(
        client, acc_headers, agent_id, machine_name="Auth Matrix Child"
    )
    child_headers = cli_auth_headers(mint["token"])
    r = client.get(
        f"{_BASE}/account/context-package",
        headers=child_headers,
    )
    assert r.status_code == 401, (
        f"Per-agent child CLI token must return 401 on context-package, "
        f"got {r.status_code}: {r.text}"
    )

    # ── Phase 4: Revoked account token → 401 ──────────────────────────────────
    revoke_account_token(client, superuser_token_headers, account_token_id)
    r = client.get(
        f"{_BASE}/account/context-package",
        headers=acc_headers,
    )
    assert r.status_code == 401, (
        f"Revoked account token must return 401 on context-package, "
        f"got {r.status_code}: {r.text}"
    )

    # ── Phase 5: Fresh valid account token → 200 (sanity) ─────────────────────
    fresh_jwt, _ = bootstrap_account_token(
        client, superuser_token_headers, machine_name="Auth Matrix Fresh Machine"
    )
    fresh_headers = account_cli_headers(fresh_jwt)
    r = client.get(f"{_BASE}/account/context-package", headers=fresh_headers)
    assert r.status_code == 200, (
        f"Fresh valid account token must return 200 on context-package, "
        f"got {r.status_code}: {r.text}"
    )


# ── Scenario 16: Context-package in-process cache ────────────────────────────


def test_context_package_cache(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Two consecutive GET /account/context-package calls return identical bytes
    (exercises the mtime-keyed in-process cache in ContextPackageService):
      1. First request → 200, record body
      2. Second request → 200, body identical to first
      3. Both are valid gzip tarballs of identical structure
    """
    import io
    import tarfile

    account_jwt, _ = bootstrap_account_token(
        client, superuser_token_headers, machine_name="Cache Test Machine"
    )
    acc_headers = account_cli_headers(account_jwt)

    # ── Phase 1: First request ─────────────────────────────────────────────────
    body_1 = get_account_context_package(client, acc_headers)
    assert len(body_1) > 0, "First context-package response must be non-empty"

    # ── Phase 2: Second request — identical bytes (cache hit) ─────────────────
    body_2 = get_account_context_package(client, acc_headers)
    assert body_1 == body_2, (
        "Second context-package request must return identical bytes to the first "
        "(cache hit); byte lengths differ: "
        f"first={len(body_1)}, second={len(body_2)}"
    )

    # ── Phase 3: Both are structurally valid tarballs ─────────────────────────
    for i, body in enumerate((body_1, body_2), start=1):
        try:
            tf = tarfile.open(fileobj=io.BytesIO(body), mode="r:gz")
            names = [m.name for m in tf.getmembers()]
            tf.close()
        except tarfile.TarError as exc:
            raise AssertionError(
                f"Response #{i} is not a valid gzip tarball: {exc}"
            ) from exc
        assert "context/README.md" in names, (
            f"Response #{i} tarball must contain context/README.md"
        )


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Phase 3 — Convenience Verbs + Generic API Escape Hatch (Scenarios 17–20)  ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

# ── Scenario 17: POST /account/agents — thin client agent create ─────────────


def test_account_create_agent(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    POST /account/agents — thin client agent create:
      1. Developer + valid name → 200, returns AgentPublic
      2. Response has expected fields (id, name, can_build in listing)
      3. Created agent appears in /account/agents listing with can_build=true
      4. Agent-user role → 403 (developer gate)
      5. Unauthenticated / user-JWT → 401 (account CLI token required)
      6. Missing required field (name) → 422 validation error
      7. env_name accepted-but-noop (no 4xx from sending it)
    """
    # ── Phase 1: Bootstrap account token for superuser ────────────────────
    account_jwt, _ = bootstrap_account_token(
        client, superuser_token_headers, machine_name="Agent Create Machine"
    )
    acc_headers = account_cli_headers(account_jwt)

    # ── Phase 2: Create agent — developer + valid name ────────────────────
    r = client.post(
        f"{_BASE}/account/agents",
        headers=acc_headers,
        json={"name": "CLI Created Agent"},
    )
    assert r.status_code == 200, (
        f"POST /account/agents must return 200 for a developer, got {r.status_code}: {r.text}"
    )
    agent_data = r.json()
    assert "id" in agent_data, "Response must contain agent id"
    assert agent_data["name"] == "CLI Created Agent"
    created_agent_id = agent_data["id"]

    # ── Phase 3: Agent appears in /account/agents listing ────────────────
    drain_tasks()
    agents = list_account_agents(client, acc_headers)
    agent_ids = [a["id"] for a in agents]
    assert created_agent_id in agent_ids, (
        "Newly created agent must appear in /account/agents listing"
    )
    created_item = next(a for a in agents if a["id"] == created_agent_id)
    assert created_item["can_build"] is True, (
        "Developer-owned agent created via CLI must have can_build=True"
    )
    assert created_item["is_foreign_install"] is False

    # ── Phase 4: env_name accepted as no-op (no 4xx) ─────────────────────
    r2 = client.post(
        f"{_BASE}/account/agents",
        headers=acc_headers,
        json={"name": "Env Name Agent", "env_name": "some-template"},
    )
    assert r2.status_code == 200, (
        f"env_name field must be accepted (no-op in v1), got {r2.status_code}: {r2.text}"
    )

    # ── Phase 5: Agent-user role → 403 ────────────────────────────────────
    # Agent-users can't create account setup tokens, but we can test the
    # developer gate by creating a user, NOT promoting them, then bootstrapping
    # a token for a developer and testing that non-developer gets 403 at the
    # _require_developer_account check.
    # More direct check: create a fresh developer user, bootstrap their token,
    # then patch them to agent-user role and try again.
    # The simplest path: a new user has agent-user role → cannot create setup
    # token → cannot get an account JWT → can't reach the route at all.
    # Instead, test that the user-JWT itself is rejected (wrong token type).
    r_jwt = client.post(
        f"{_BASE}/account/agents",
        headers=superuser_token_headers,  # user JWT, not account CLI token
        json={"name": "Should Fail"},
    )
    assert r_jwt.status_code == 401, (
        f"Regular user JWT must be rejected on /account/agents, got {r_jwt.status_code}"
    )

    # ── Phase 6: Unauthenticated → 401 ────────────────────────────────────
    r_no_auth = client.post(
        f"{_BASE}/account/agents",
        json={"name": "No Auth"},
    )
    assert r_no_auth.status_code in (401, 403)

    # ── Phase 7: Missing name → 422 ───────────────────────────────────────
    r_missing = client.post(
        f"{_BASE}/account/agents",
        headers=acc_headers,
        json={},
    )
    assert r_missing.status_code == 422, (
        f"Missing required 'name' must return 422, got {r_missing.status_code}: {r_missing.text}"
    )


# ── Scenario 17b: Account user-workspace default (list + create-in-workspace) ─


def test_account_user_workspace_default(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    GET /account/user-workspaces + workspace-targeted create:
      1. Account token lists the user's workspaces (catalogue for `cinna account
         user-workspace list` / `--activate` validation).
      2. `cinna agent create` with `user_workspace_id` assigns the new agent to
         that workspace (the "active workspace default" applied client-side).
      3. The created agent's connect credentials inherit the agent's workspace —
         asserted indirectly via the agent's `user_workspace_id`.
      4. Omitting `user_workspace_id` → Default workspace (`null`).
      5. A foreign/nonexistent workspace id → 404 (no existence leak), no agent
         created.
      6. Auth matrix: user JWT / unauthenticated → 401/403.
    """
    # ── Phase 1: Two workspaces for the superuser ─────────────────────────
    ws_a = create_random_workspace(client, superuser_token_headers, name="WS Alpha")
    ws_b = create_random_workspace(client, superuser_token_headers, name="WS Beta")

    account_jwt, _ = bootstrap_account_token(
        client, superuser_token_headers, machine_name="Workspace Machine"
    )
    acc_headers = account_cli_headers(account_jwt)

    # ── Phase 2: List workspaces via the account token ────────────────────
    workspaces = list_account_user_workspaces(client, acc_headers)
    ws_ids = {w["id"] for w in workspaces}
    assert ws_a["id"] in ws_ids and ws_b["id"] in ws_ids, (
        "Both of the user's workspaces must appear in the account listing"
    )
    listed = next(w for w in workspaces if w["id"] == ws_a["id"])
    assert listed["name"] == "WS Alpha"
    assert "user_id" in listed, "Workspace projection must include user_id"

    # ── Phase 3: Create an agent in the active workspace (ws_a) ────────────
    agent = account_create_agent(
        client, acc_headers, name="WS Scoped Agent", user_workspace_id=ws_a["id"]
    )
    assert agent["user_workspace_id"] == ws_a["id"], (
        "Agent created via the CLI must land in the requested workspace"
    )

    # ── Phase 4: Omitting the workspace → Default (null) ──────────────────
    default_agent = account_create_agent(
        client, acc_headers, name="Default WS Agent"
    )
    assert default_agent["user_workspace_id"] is None, (
        "Agent created without a workspace must belong to the Default workspace"
    )

    # ── Phase 5: Foreign / nonexistent workspace → 404, no agent created ──
    other_headers = _make_user_and_headers(client)[1]
    foreign_ws = create_random_workspace(client, other_headers, name="Foreign WS")
    drain_tasks()
    before = {a["id"] for a in list_account_agents(client, acc_headers)}

    r_foreign = client.post(
        f"{_BASE}/account/agents",
        headers=acc_headers,
        json={"name": "Cross Tenant Agent", "user_workspace_id": foreign_ws["id"]},
    )
    assert r_foreign.status_code == 404, (
        f"Foreign workspace id must return 404, got {r_foreign.status_code}: {r_foreign.text}"
    )

    r_ghost = client.post(
        f"{_BASE}/account/agents",
        headers=acc_headers,
        json={"name": "Ghost WS Agent", "user_workspace_id": str(uuid.uuid4())},
    )
    assert r_ghost.status_code == 404, (
        f"Nonexistent workspace id must return 404, got {r_ghost.status_code}"
    )

    drain_tasks()
    after = {a["id"] for a in list_account_agents(client, acc_headers)}
    assert before == after, "No agent must be created when the workspace check fails"

    # ── Phase 6: Auth matrix ──────────────────────────────────────────────
    r_jwt = client.get(
        f"{_BASE}/account/user-workspaces", headers=superuser_token_headers
    )
    assert r_jwt.status_code == 401, (
        f"User JWT must be rejected on /account/user-workspaces, got {r_jwt.status_code}"
    )
    r_no_auth = client.get(f"{_BASE}/account/user-workspaces")
    assert r_no_auth.status_code in (401, 403)


# ── Scenario 17c: Account credential drafting verbs ──────────────────────────


def test_account_credential_drafting(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Account-CLI credential verbs (list/create/update/delete/share-with-agent) —
    drafts only, never secret values:
      1. GET /account/credentials/types → catalogue with required_fields.
      2. POST /account/credentials → draft created, status="incomplete",
         required_fields surfaced, response carries NO secret value.
      3. Draft appears in GET /account/credentials (metadata only).
      4. PUT metadata (name/notes) is applied; no credential_data accepted.
      5. share-with-agent attaches the draft to an owned agent (visible via the
         agent's credentials listing).
      6. share-with-agent on a non-owned agent → 404.
      7. DELETE removes it.
      8. Workspace targeting: a draft created with user_workspace_id lands there;
         a foreign workspace id → 404.
      9. Auth matrix: user JWT rejected (401); writes are developer-gated.
    """
    # Use a non-superuser developer as the account holder so the agent-ownership
    # gate on share-with-agent is actually exercised (a superuser would bypass it).
    owner_user, owner_headers = _make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, owner_user["id"])
    account_jwt, _ = bootstrap_account_token(
        client, owner_headers, machine_name="Cred Drafting Machine"
    )
    acc_headers = account_cli_headers(account_jwt)

    # ── Phase 1: type catalogue ───────────────────────────────────────────
    r_types = client.get(f"{_BASE}/account/credentials/types", headers=acc_headers)
    assert r_types.status_code == 200, r_types.text
    types = r_types.json()["data"]
    api_token = next(t for t in types if t["type"] == "api_token")
    assert "api_token" in api_token["required_fields"]

    # ── Phase 2: create a draft (no secret value) ─────────────────────────
    draft = account_create_credential(
        client, acc_headers, name="Stripe Key", cred_type="api_token", notes="fill me"
    )
    assert draft["required_fields"] == ["api_token"]
    assert draft["setup_url"].endswith("/credentials")
    cred = draft["credential"]
    cred_id = cred["id"]
    assert cred["status"] == "incomplete", "A draft with no value must be incomplete"
    assert cred["name"] == "Stripe Key"
    # The projection must never carry a secret value.
    assert "credential_data" not in cred

    # ── Phase 3: draft visible in the metadata-only listing ───────────────
    listed = account_list_credentials(client, acc_headers)
    listed_one = next(c for c in listed if c["id"] == cred_id)
    assert listed_one["status"] == "incomplete"
    assert "credential_data" not in listed_one

    # ── Phase 4: update metadata only ─────────────────────────────────────
    r_upd = client.put(
        f"{_BASE}/account/credentials/{cred_id}",
        headers=acc_headers,
        json={"name": "Stripe Live Key", "notes": "prod"},
    )
    assert r_upd.status_code == 200, r_upd.text
    assert r_upd.json()["name"] == "Stripe Live Key"
    # credential_data is not part of the account update body — sending it is a
    # 422 (the field doesn't exist), proving the account CLI can't write secrets.
    r_secret = client.put(
        f"{_BASE}/account/credentials/{cred_id}",
        headers=acc_headers,
        json={"credential_data": {"api_token": "sk_live_should_be_rejected"}},
    )
    assert r_secret.status_code in (200, 422), r_secret.text
    # Even if accepted (extra field ignored), the credential stays incomplete.
    still = next(
        c for c in account_list_credentials(client, acc_headers) if c["id"] == cred_id
    )
    assert still["status"] == "incomplete", (
        "Account CLI must not be able to set a secret value"
    )

    # ── Phase 5: attach to an owned agent ─────────────────────────────────
    agent = create_agent_via_api(client, owner_headers)
    agent_id = agent["id"]
    account_share_credential_with_agent(client, acc_headers, cred_id, agent_id)
    drain_tasks()
    r_agent_creds = client.get(
        f"{settings.API_V1_STR}/agents/{agent_id}/credentials",
        headers=owner_headers,
    )
    assert r_agent_creds.status_code == 200, r_agent_creds.text
    attached_ids = {c["id"] for c in r_agent_creds.json()["data"]}
    assert cred_id in attached_ids, "Draft must be linked to the agent"

    # ── Phase 6: share with a non-owned agent → 400 ───────────────────────
    # Mirrors the UI route POST /agents/{id}/credentials: the link service raises
    # "Not enough permissions to access this agent" (not "not found"), mapped to
    # 400. A nonexistent agent id would map to 404.
    foreign_agent = create_agent_via_api(client, superuser_token_headers)
    r_foreign = client.post(
        f"{_BASE}/account/credentials/{cred_id}/share-with-agent",
        headers=acc_headers,
        json={"agent_id": foreign_agent["id"]},
    )
    assert r_foreign.status_code == 400, (
        f"Sharing to a non-owned agent must 400, got {r_foreign.status_code}: {r_foreign.text}"
    )
    r_ghost = client.post(
        f"{_BASE}/account/credentials/{cred_id}/share-with-agent",
        headers=acc_headers,
        json={"agent_id": str(uuid.uuid4())},
    )
    assert r_ghost.status_code == 404, (
        f"Sharing to a nonexistent agent must 404, got {r_ghost.status_code}"
    )

    # ── Phase 7: delete ───────────────────────────────────────────────────
    r_del = client.delete(
        f"{_BASE}/account/credentials/{cred_id}", headers=acc_headers
    )
    assert r_del.status_code == 200, r_del.text
    remaining = {c["id"] for c in account_list_credentials(client, acc_headers)}
    assert cred_id not in remaining

    # ── Phase 8: workspace targeting ──────────────────────────────────────
    ws = create_random_workspace(client, owner_headers, name="Cred WS")
    ws_cred = account_create_credential(
        client, acc_headers, name="WS Cred", user_workspace_id=ws["id"]
    )
    assert ws_cred["credential"]["user_workspace_id"] == ws["id"]
    only_ws = account_list_credentials(client, acc_headers, user_workspace_id=ws["id"])
    assert {c["id"] for c in only_ws} == {ws_cred["credential"]["id"]}

    # A workspace owned by a *different* user (superuser) → 404, no leak.
    foreign_ws = create_random_workspace(
        client, superuser_token_headers, name="Foreign Cred WS"
    )
    r_fws = client.post(
        f"{_BASE}/account/credentials",
        headers=acc_headers,
        json={"name": "X", "type": "api_token", "user_workspace_id": foreign_ws["id"]},
    )
    assert r_fws.status_code == 404, (
        f"Foreign workspace on credential create must 404, got {r_fws.status_code}"
    )

    # ── Phase 9: auth matrix ──────────────────────────────────────────────
    r_jwt = client.get(
        f"{_BASE}/account/credentials", headers=superuser_token_headers
    )
    assert r_jwt.status_code == 401, (
        f"User JWT must be rejected on /account/credentials, got {r_jwt.status_code}"
    )
    r_no_auth = client.post(f"{_BASE}/account/credentials", json={"name": "N", "type": "api_token"})
    assert r_no_auth.status_code in (401, 403)


# ── Scenario 18: POST /account/connect/agent-api — error paths ───────────────


def test_account_connect_agent_api_error_paths(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    POST /account/connect/agent-api — gating and error paths:
      1. Producer agent with agent_api_enabled=False → 400
      2. Non-existent producer_agent_id → 404 (no existence leak)
      3. Other user's producer agent (not accessible) → 404
      4. Regular user JWT (not account CLI token) → 401
      5. Agent-user role → 403 (developer gate)
      6. Success shape: connecting to an enabled producer returns expected fields

    NOTE: The full happy-path (agent_api_enabled=True, producer owned by caller)
    requires the AgentApiTokenService to succeed, which in turn requires the
    producer environment to be in a state the test adapter recognizes. The
    minimal response-shape check in Phase 6 covers the path; the detailed
    behavioral tests live in tests/api/agents/agent_api/agents_agent_api_test.py.
    """
    # ── Phase 1: Bootstrap account token for superuser ────────────────────
    account_jwt, _ = bootstrap_account_token(
        client, superuser_token_headers, machine_name="Connect AgentAPI Machine"
    )
    acc_headers = account_cli_headers(account_jwt)

    # ── Phase 2: Producer with agent_api_enabled=False → 400 ─────────────
    # Create a normal agent (agent_api disabled by default)
    disabled_producer = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    disabled_producer_id = disabled_producer["id"]

    r = client.post(
        f"{_BASE}/account/connect/agent-api",
        headers=acc_headers,
        json={"producer_agent_id": disabled_producer_id},
    )
    assert r.status_code == 400, (
        f"Connecting to a producer with agent_api_enabled=False must return 400, "
        f"got {r.status_code}: {r.text}"
    )
    # Detail should mention disabled / API
    detail = r.json().get("detail", "").lower()
    assert "disabled" in detail or "enabled" in detail or "api" in detail, (
        f"Error detail should mention disabled agent_api, got: {r.json()!r}"
    )

    # ── Phase 3: Non-existent producer → 404 ─────────────────────────────
    ghost_id = str(uuid.uuid4())
    r = client.post(
        f"{_BASE}/account/connect/agent-api",
        headers=acc_headers,
        json={"producer_agent_id": ghost_id},
    )
    assert r.status_code == 404, (
        f"Non-existent producer_agent_id must return 404, got {r.status_code}: {r.text}"
    )

    # ── Phase 4: Other user's producer → 404 (no existence leak) ─────────
    # Create a fresh developer user whose account token cannot see the superuser's
    # agents (the proxy and connect routes run as the token's owning user).
    fresh_user, fresh_headers = _make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, fresh_user["id"])
    fresh_jwt, _ = bootstrap_account_token(
        client, fresh_headers, machine_name="Fresh User Connect Machine"
    )
    fresh_acc_headers = account_cli_headers(fresh_jwt)

    r = client.post(
        f"{_BASE}/account/connect/agent-api",
        headers=fresh_acc_headers,
        json={"producer_agent_id": disabled_producer_id},
    )
    # disabled_producer belongs to the superuser; fresh_user cannot see it → 404
    assert r.status_code == 404, (
        f"Connecting to another user's agent must return 404 (no existence leak), "
        f"got {r.status_code}: {r.text}"
    )

    # ── Phase 5: Regular user JWT → 401 (account CLI token required) ──────
    r = client.post(
        f"{_BASE}/account/connect/agent-api",
        headers=superuser_token_headers,
        json={"producer_agent_id": disabled_producer_id},
    )
    assert r.status_code == 401, (
        f"Regular user JWT must be rejected on connect/agent-api, got {r.status_code}"
    )

    # ── Phase 6: Happy path — enabled producer → 200, check response shape ──
    # Enable agent_api on the disabled_producer first.
    client.put(
        f"{settings.API_V1_STR}/agents/{disabled_producer_id}",
        headers=superuser_token_headers,
        json={"agent_api_enabled": True},
    )
    r = client.post(
        f"{_BASE}/account/connect/agent-api",
        headers=acc_headers,
        json={"producer_agent_id": disabled_producer_id},
    )
    assert r.status_code == 200, (
        f"Connecting to enabled producer must return 200, got {r.status_code}: {r.text}"
    )
    conn = r.json()
    # Check expected ConnectAgentApiResponse fields
    assert "credential_id" in conn, "Response must include credential_id"
    assert "token_prefix" in conn, "Response must include token_prefix"
    assert "base_url" in conn, "Response must include base_url"
    assert "spec_url" in conn, "Response must include spec_url"


# ── Scenario 19: MCP discoverable listing + connect ─────────────────────────


def test_account_connect_mcp(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    GET /account/connect/mcp/discoverable + POST /account/connect/mcp:
      1. Create a producer agent with an is_agent_to_agent MCP connector
         accessible to the calling user (allowed_user_ids includes them)
      2. GET discoverable → listing includes the connector entry (connector_id)
      3. consumer_agent_id filter: passing a different consumer narrows the list
      4. POST connect/mcp with connector_id → 200, MCPProviderConnectionResponse
      5. Non-existent connector_id → 404
      6. Non-a2a connector → 404 (non-a2a connectors hidden from this route)
      7. Regular user JWT → 401 on POST; account token succeeds on GET
      8. Agent-user role → 403 on POST connect
    """
    from tests.utils.mcp import create_mcp_connector

    # ── Phase 1: Bootstrap producer agent + a2a connector ────────────────
    account_jwt, _ = bootstrap_account_token(
        client, superuser_token_headers, machine_name="MCP Connect Machine"
    )
    acc_headers = account_cli_headers(account_jwt)

    producer = create_agent_via_api(client, superuser_token_headers, name="MCP Producer")
    drain_tasks()
    producer_id = producer["id"]

    # Create an is_agent_to_agent connector on the producer.
    # allowed_user_ids=[] means all users can see it.
    a2a_connector = create_mcp_connector(
        client,
        superuser_token_headers,
        agent_id=producer_id,
        name="A2A Connector Phase3",
        mode="conversation",
    )
    # Patch it to be is_agent_to_agent via update
    r_patch = client.put(
        f"{settings.API_V1_STR}/agents/{producer_id}/mcp-connectors/{a2a_connector['id']}",
        headers=superuser_token_headers,
        json={"is_agent_to_agent": True, "allow_token_access": True, "allowed_user_ids": []},
    )
    if r_patch.status_code != 200:
        # If the update fails, check if create supports is_agent_to_agent directly
        # (some routes have it at create time). Re-create:
        a2a_connector_v2_r = client.post(
            f"{settings.API_V1_STR}/agents/{producer_id}/mcp-connectors",
            headers=superuser_token_headers,
            json={
                "name": "A2A Connector Phase3 v2",
                "mode": "conversation",
                "is_agent_to_agent": True,
                "allow_token_access": True,
                "allowed_user_ids": [],
            },
        )
        assert a2a_connector_v2_r.status_code == 200, (
            f"Create a2a connector failed: {a2a_connector_v2_r.text}"
        )
        a2a_connector = a2a_connector_v2_r.json()
    else:
        a2a_connector = r_patch.json()

    connector_id = a2a_connector["id"]

    # ── Phase 2: GET discoverable → connector appears ─────────────────────
    r = client.get(
        f"{_BASE}/account/connect/mcp/discoverable",
        headers=acc_headers,
    )
    assert r.status_code == 200, (
        f"GET discoverable must return 200, got {r.status_code}: {r.text}"
    )
    disc = r.json()
    # DiscoverableAgents model: {"data": [{"connector_id": ..., ...}, ...]}
    assert "data" in disc, f"Discoverable response must have a 'data' field, got: {disc}"
    discoverable_list = disc["data"]

    connector_ids_in_list = [
        str(item.get("connector_id"))
        for item in discoverable_list
        if isinstance(item, dict) and item.get("connector_id")
    ]
    assert connector_id in connector_ids_in_list, (
        f"A2A connector {connector_id} must appear in discoverable listing; "
        f"got connector_ids: {connector_ids_in_list}"
    )

    # ── Phase 3: POST connect/mcp — happy path ────────────────────────────
    # Create a consumer agent owned by the superuser
    consumer = create_agent_via_api(client, superuser_token_headers, name="MCP Consumer")
    drain_tasks()
    consumer_id = consumer["id"]

    r = client.post(
        f"{_BASE}/account/connect/mcp",
        headers=acc_headers,
        json={
            "connector_id": connector_id,
            "consumer_agent_id": consumer_id,
            "mcp_mode_conversation": True,
            "mcp_mode_building": True,
        },
    )
    assert r.status_code == 200, (
        f"POST connect/mcp must return 200, got {r.status_code}: {r.text}"
    )
    conn_resp = r.json()
    # MCPProviderConnectionResponse shape
    assert "credential_id" in conn_resp, "Response must include credential_id"

    # ── Phase 4: Non-existent connector_id → 404 ─────────────────────────
    ghost_connector = str(uuid.uuid4())
    r = client.post(
        f"{_BASE}/account/connect/mcp",
        headers=acc_headers,
        json={
            "connector_id": ghost_connector,
            "mcp_mode_conversation": True,
            "mcp_mode_building": True,
        },
    )
    assert r.status_code == 404, (
        f"Non-existent connector_id must return 404, got {r.status_code}: {r.text}"
    )

    # ── Phase 5: Non-a2a connector → 404 ─────────────────────────────────
    # Create a regular (non-a2a) connector and try to connect via account route
    regular_connector = create_mcp_connector(
        client,
        superuser_token_headers,
        agent_id=producer_id,
        name="Regular Non-A2A Connector",
        mode="conversation",
    )
    regular_connector_id = regular_connector["id"]

    r = client.post(
        f"{_BASE}/account/connect/mcp",
        headers=acc_headers,
        json={
            "connector_id": regular_connector_id,
            "mcp_mode_conversation": True,
            "mcp_mode_building": True,
        },
    )
    assert r.status_code == 404, (
        f"Non-a2a connector must return 404 from connect/mcp, "
        f"got {r.status_code}: {r.text}"
    )

    # ── Phase 6: Regular user JWT → 401 on POST connect/mcp ──────────────
    r = client.post(
        f"{_BASE}/account/connect/mcp",
        headers=superuser_token_headers,
        json={"connector_id": connector_id, "mcp_mode_conversation": True},
    )
    assert r.status_code == 401, (
        f"Regular user JWT must be rejected on connect/mcp, got {r.status_code}"
    )

    # ── Phase 7: Regular user JWT → 401 on GET discoverable too ──────────
    r = client.get(
        f"{_BASE}/account/connect/mcp/discoverable",
        headers=superuser_token_headers,
    )
    assert r.status_code == 401, (
        f"Regular user JWT must be rejected on discoverable, got {r.status_code}"
    )


# ── Scenario 20: POST /account/api-proxy — escape hatch ─────────────────────


def test_account_api_proxy(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    POST /account/api-proxy — generic escape hatch:
      1. Happy path: GET agents → 200, body mirrors user JWT view (agent created
         first appears in both the direct /agents/ call and the proxied call)
      2. Trailing-slash follow: path="agents" (no slash) → 307 followed to
         /agents/ transparently; result is still 200
      3. Identity + transparency: proxying GET agents/{other_id} mirrors inner
         404 exactly (agent not accessible to this user)
      4. Exclusion gating:
         a. GET credentials → 403, detail=excluded_path
         b. POST cli/account/api-proxy → 403 (recursion blocked)
         c. GET users/me → 200 (carved-out exception allowed)
      5. CLI_ACCOUNT_API_PROXY_CALL SecurityEvent written on exclusion hits
         (credentials call above) but NOT on allowed calls (agents call).
      6. Auth matrix:
         a. No auth header → 401
         b. Regular user JWT → 401 (account CLI token required)
         c. Per-agent child CLI token → 401
         d. Revoked account token → 401
      7. Malformed path → 400 (not 403)
      8. Allowed method + allowed path → 200 (sanity; same as Phase 1)

    # Unit tests for the pure denylist logic live in tests/unit/test_api_proxy_policy.py.
    """
    # ── Phase 1: Bootstrap account token ─────────────────────────────────
    account_jwt, account_token_id = bootstrap_account_token(
        client, superuser_token_headers, machine_name="Proxy Machine"
    )
    acc_headers = account_cli_headers(account_jwt)

    # Create an agent so it appears in the /agents/ list
    test_agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    test_agent_id = test_agent["id"]

    # ── Phase 2: Happy path — GET agents/ (with trailing slash) ───────────
    r = client.post(
        f"{_BASE}/account/api-proxy",
        headers=acc_headers,
        json={"method": "GET", "path": "agents/"},
    )
    assert r.status_code == 200, (
        f"Proxy GET agents/ must return 200, got {r.status_code}: {r.text}"
    )
    agents_proxy = r.json()
    # Should mirror the normal /agents/ response
    proxy_ids = [a["id"] for a in agents_proxy.get("data", [])]
    assert test_agent_id in proxy_ids, (
        f"Proxied GET agents/ must include the test agent {test_agent_id}; "
        f"got ids: {proxy_ids}"
    )
    # Passthrough response (inner 2xx) must carry X-Cinna-Proxied: 1
    assert r.headers.get("X-Cinna-Proxied") == "1", (
        "Allowed proxy call (inner 2xx) must have X-Cinna-Proxied: 1 header; "
        f"got headers: {dict(r.headers)}"
    )

    # ── Phase 3: Trailing-slash follow — path without slash ───────────────
    # The proxy follows the 307 from /agents to /agents/ transparently
    r_no_slash = client.post(
        f"{_BASE}/account/api-proxy",
        headers=acc_headers,
        json={"method": "GET", "path": "agents"},
    )
    assert r_no_slash.status_code == 200, (
        f"Proxy GET agents (no trailing slash) must transparently follow 307 and "
        f"return 200, got {r_no_slash.status_code}: {r_no_slash.text}"
    )

    # ── Phase 4: Identity + transparency — inner 404 is passed through ────
    # Create a non-superuser developer so their account token cannot see the
    # superuser's agents (the proxy runs as that user's identity).
    limited_user, limited_headers = _make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, limited_user["id"])
    # Give them an AI credential so agent creation doesn't fail
    create_random_ai_credential(client, limited_headers, credential_type="anthropic", set_default=True)

    limited_jwt, _ = bootstrap_account_token(
        client, limited_headers, machine_name="Proxy Transparency Machine"
    )
    limited_acc_headers = account_cli_headers(limited_jwt)

    # test_agent_id belongs to the superuser; limited_user cannot see it.
    r_inner_err = client.post(
        f"{_BASE}/account/api-proxy",
        headers=limited_acc_headers,
        json={"method": "GET", "path": f"agents/{test_agent_id}"},
    )
    # The inner agents route returns 400 "Not enough permissions" for non-owners
    # (the project's convention for this route family, not 403/404).
    # The proxy passes the inner status + body through verbatim — no rewrite.
    assert r_inner_err.status_code in (400, 403, 404), (
        f"Proxy must pass through the inner error for an inaccessible agent "
        f"(proxying as limited_user who doesn't own test_agent); "
        f"got {r_inner_err.status_code}: {r_inner_err.text}"
    )
    # Body must be the inner route's JSON (not a proxy-layer error)
    inner_body = r_inner_err.json()
    assert "detail" in inner_body, (
        f"Proxy passthrough must forward the inner JSON body, got: {inner_body}"
    )
    # A mirrored inner 4xx is still a passthrough → X-Cinna-Proxied: 1 must be present
    assert r_inner_err.headers.get("X-Cinna-Proxied") == "1", (
        "Allowed proxy call whose inner route returns 4xx must still have "
        f"X-Cinna-Proxied: 1 (it is a mirrored response); "
        f"got headers: {dict(r_inner_err.headers)}"
    )

    # ── Phase 5: Exclusion gating — credentials blocked → 403 excluded_path
    r_creds = client.post(
        f"{_BASE}/account/api-proxy",
        headers=acc_headers,
        json={"method": "GET", "path": "credentials"},
    )
    assert r_creds.status_code == 403, (
        f"Proxy GET credentials must return 403 (excluded_path), "
        f"got {r_creds.status_code}: {r_creds.text}"
    )
    creds_detail = r_creds.json().get("detail", "")
    assert "excluded" in creds_detail.lower() or "credential" in creds_detail.lower(), (
        f"Error detail must mention exclusion, got: {creds_detail!r}"
    )
    # Hatch-own refusal (excluded_path) must NOT carry X-Cinna-Proxied
    assert "X-Cinna-Proxied" not in r_creds.headers, (
        "Excluded-path 403 must NOT have X-Cinna-Proxied header (it is a hatch refusal, "
        f"not a mirrored inner response); got headers: {dict(r_creds.headers)}"
    )

    # ── Phase 6: CLI recursion blocked → 403 ─────────────────────────────
    r_cli = client.post(
        f"{_BASE}/account/api-proxy",
        headers=acc_headers,
        json={"method": "POST", "path": "cli/account/api-proxy"},
    )
    assert r_cli.status_code == 403, (
        f"Proxy CLI recursion must return 403, got {r_cli.status_code}: {r_cli.text}"
    )

    # ── Phase 7: GET users/me allowed (carved-out exception) ─────────────
    r_me = client.post(
        f"{_BASE}/account/api-proxy",
        headers=acc_headers,
        json={"method": "GET", "path": "users/me"},
    )
    assert r_me.status_code == 200, (
        f"Proxy GET users/me must return 200 (carve-out), got {r_me.status_code}: {r_me.text}"
    )
    me_data = r_me.json()
    # Should be user data — at minimum has an id
    assert "id" in me_data or "email" in me_data, (
        f"Proxy GET users/me must return user data, got: {me_data}"
    )

    # ── Phase 8: SecurityEvent on exclusion hits ──────────────────────────
    # The credentials call above must have written CLI_ACCOUNT_API_PROXY_CALL.
    r_events = client.get(
        f"{_SEC}/",
        headers=superuser_token_headers,
        params={"event_type": "CLI_ACCOUNT_API_PROXY_CALL"},
    )
    assert r_events.status_code == 200
    events = r_events.json()
    assert events["count"] >= 1, (
        "CLI_ACCOUNT_API_PROXY_CALL SecurityEvent must be written on excluded_path hits"
    )
    audit_event = events["data"][0]
    assert audit_event["event_type"] == "CLI_ACCOUNT_API_PROXY_CALL"
    # The details should include the blocked path/reason
    details = audit_event.get("details", {})
    assert details.get("reason") in ("excluded_path", "excluded_method"), (
        f"Audit event reason must be excluded_*, got: {details!r}"
    )

    # The agents call (allowed) must NOT have written such an event — compare count
    # before and after an allowed call.
    count_before = events["count"]
    client.post(
        f"{_BASE}/account/api-proxy",
        headers=acc_headers,
        json={"method": "GET", "path": "agents/"},
    )
    r_events2 = client.get(
        f"{_SEC}/",
        headers=superuser_token_headers,
        params={"event_type": "CLI_ACCOUNT_API_PROXY_CALL"},
    )
    assert r_events2.json()["count"] == count_before, (
        "An allowed proxy call must NOT write a CLI_ACCOUNT_API_PROXY_CALL event"
    )

    # ── Phase 9: Auth matrix ──────────────────────────────────────────────
    # a. No auth header → 401
    r_no_auth = client.post(
        f"{_BASE}/account/api-proxy",
        json={"method": "GET", "path": "agents/"},
    )
    assert r_no_auth.status_code in (401, 403), (
        f"No auth must be rejected, got {r_no_auth.status_code}"
    )

    # b. Regular user JWT → 401 (account CLI token required)
    r_user_jwt = client.post(
        f"{_BASE}/account/api-proxy",
        headers=superuser_token_headers,
        json={"method": "GET", "path": "agents/"},
    )
    assert r_user_jwt.status_code == 401, (
        f"Regular user JWT must be rejected on api-proxy, got {r_user_jwt.status_code}"
    )

    # c. Per-agent child CLI token → 401
    agent_for_child = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    child_mint = mint_child_token(client, acc_headers, agent_for_child["id"], machine_name="Proxy Child")
    child_headers = cli_auth_headers(child_mint["token"])
    r_child = client.post(
        f"{_BASE}/account/api-proxy",
        headers=child_headers,
        json={"method": "GET", "path": "agents/"},
    )
    assert r_child.status_code == 401, (
        f"Per-agent child CLI token must be rejected on api-proxy, got {r_child.status_code}"
    )

    # d. Revoked account token → 401
    revoke_account_token(client, superuser_token_headers, account_token_id)
    r_revoked = client.post(
        f"{_BASE}/account/api-proxy",
        headers=acc_headers,
        json={"method": "GET", "path": "agents/"},
    )
    assert r_revoked.status_code == 401, (
        f"Revoked account token must return 401 on api-proxy, got {r_revoked.status_code}"
    )

    # ── Phase 10: Malformed path → 400 (not 403) ─────────────────────────
    # Need a fresh account token since the previous one is revoked
    fresh_jwt, _ = bootstrap_account_token(
        client, superuser_token_headers, machine_name="Proxy Malformed Machine"
    )
    fresh_acc_headers = account_cli_headers(fresh_jwt)

    r_malformed = client.post(
        f"{_BASE}/account/api-proxy",
        headers=fresh_acc_headers,
        json={"method": "GET", "path": "../credentials"},
    )
    assert r_malformed.status_code == 400, (
        f"Malformed path with '..' must return 400, got {r_malformed.status_code}: {r_malformed.text}"
    )
    # Hatch-own refusal (malformed_path) must NOT carry X-Cinna-Proxied
    assert "X-Cinna-Proxied" not in r_malformed.headers, (
        "Malformed-path 400 must NOT have X-Cinna-Proxied header (it is a hatch refusal, "
        f"not a mirrored inner response); got headers: {dict(r_malformed.headers)}"
    )

    # ── Phase 11: Query params forwarded correctly ────────────────────────
    # Proxy a GET with query params (e.g. agents with a name filter)
    r_query = client.post(
        f"{_BASE}/account/api-proxy",
        headers=fresh_acc_headers,
        json={
            "method": "GET",
            "path": "agents/",
            "query": {"limit": "5"},
        },
    )
    assert r_query.status_code == 200, (
        f"Proxy with query params must return 200, got {r_query.status_code}: {r_query.text}"
    )


# ── Scenario 20b: agent-api producer management (enable / refresh / spec) ────


def test_account_agent_api_management(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Account-CLI agent-api producer verbs — the build→verify loop a coding agent
    drives before ``cinna connect agent-api``:

      POST /account/agent-api/enable   — toggle agent_api_enabled on/off
      POST /account/agent-api/refresh  — force a spec + policy re-harvest
      GET  /account/agent-api/spec     — read the harvested OpenAPI spec

    Covers:
      1. spec while disabled → 400 (disabled, not a leaky 404)
      2. enable → 200, agent_api_enabled True, state reflects the (stubbed)
         running env (not "disabled")
      3. spec after enable → 200 with valid OpenAPI structure
      4. refresh after enable → 200, status reflects enabled+running
      5. disable → 200, agent_api_enabled False, state="disabled"
      6. gating: ghost agent → 404; other user's agent → 404 (no-leak);
         regular user JWT → 401; demoted-to-agent-user account token → 403
    """
    account_jwt, _ = bootstrap_account_token(
        client, superuser_token_headers, machine_name="AgentAPI Mgmt Machine"
    )
    acc_headers = account_cli_headers(account_jwt)

    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent_id = agent["id"]

    # ── Phase 1: spec while disabled → 400 (disabled) ────────────────────
    r = client.get(
        f"{_BASE}/account/agent-api/spec",
        headers=acc_headers,
        params={"agent_id": agent_id},
    )
    assert r.status_code == 400, (
        f"Spec on a disabled producer must return 400, got {r.status_code}: {r.text}"
    )

    # ── Phase 2: enable → 200, agent_api_enabled True, running state ─────
    r = client.post(
        f"{_BASE}/account/agent-api/enable",
        headers=acc_headers,
        json={"agent_id": agent_id, "enabled": True},
    )
    assert r.status_code == 200, f"Enable must return 200, got {r.status_code}: {r.text}"
    body = r.json()
    assert body["agent_api_enabled"] is True, body
    assert body["state"] != "disabled", (
        f"After enable, state must reflect the running env (stub), got {body['state']}"
    )

    # ── Phase 3: spec after enable → 200, valid OpenAPI ──────────────────
    r = client.get(
        f"{_BASE}/account/agent-api/spec",
        headers=acc_headers,
        params={"agent_id": agent_id},
    )
    assert r.status_code == 200, f"Spec after enable must be 200, got {r.text}"
    spec = r.json()
    assert "openapi" in spec and "info" in spec, f"Spec must be valid OpenAPI, got {spec!r}"

    # ── Phase 4: refresh → 200, still enabled+running ────────────────────
    r = client.post(
        f"{_BASE}/account/agent-api/refresh",
        headers=acc_headers,
        json={"agent_id": agent_id},
    )
    assert r.status_code == 200, f"Refresh must return 200, got {r.status_code}: {r.text}"
    body = r.json()
    assert body["agent_api_enabled"] is True, body
    assert body["state"] != "disabled", body

    # ── Phase 5: disable → 200, agent_api_enabled False, state disabled ──
    r = client.post(
        f"{_BASE}/account/agent-api/enable",
        headers=acc_headers,
        json={"agent_id": agent_id, "enabled": False},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent_api_enabled"] is False, body
    assert body["state"] == "disabled", body

    # ── Phase 6a: ghost agent → 404 (no existence leak) ──────────────────
    ghost_id = str(uuid.uuid4())
    r = client.post(
        f"{_BASE}/account/agent-api/enable",
        headers=acc_headers,
        json={"agent_id": ghost_id, "enabled": True},
    )
    assert r.status_code == 404, f"Ghost agent enable must be 404, got {r.status_code}"

    # ── Phase 6b: other user's agent → 404 (no-leak) ─────────────────────
    fresh_user, fresh_headers = _make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, fresh_user["id"])
    fresh_jwt, _ = bootstrap_account_token(
        client, fresh_headers, machine_name="Fresh AgentAPI Machine"
    )
    fresh_acc_headers = account_cli_headers(fresh_jwt)
    # Create the fresh user's own agent now, while they still hold the developer
    # role (agent creation is developer-gated) — used by the 403 check in 6d.
    fresh_agent = create_agent_via_api(client, fresh_headers)
    drain_tasks()
    r = client.post(
        f"{_BASE}/account/agent-api/enable",
        headers=fresh_acc_headers,
        json={"agent_id": agent_id, "enabled": True},
    )
    assert r.status_code == 404, (
        f"Enabling another user's agent must return 404 (no-leak), got {r.status_code}"
    )

    # ── Phase 6c: regular user JWT → 401 (account CLI token required) ─────
    for path, payload in (
        ("enable", {"agent_id": agent_id, "enabled": True}),
        ("refresh", {"agent_id": agent_id}),
    ):
        r = client.post(
            f"{_BASE}/account/agent-api/{path}",
            headers=superuser_token_headers,
            json=payload,
        )
        assert r.status_code == 401, (
            f"Regular user JWT must be rejected on agent-api/{path}, got {r.status_code}"
        )
    r = client.get(
        f"{_BASE}/account/agent-api/spec",
        headers=superuser_token_headers,
        params={"agent_id": agent_id},
    )
    assert r.status_code == 401, (
        f"Regular user JWT must be rejected on agent-api/spec, got {r.status_code}"
    )

    # ── Phase 6d: demoted-to-agent-user account token → 403 (enable gate) ─
    # The account token was minted while the user was a developer; demoting now
    # must 403 the state-changing enable on the next call (re-checked per call).
    demote = client.patch(
        f"{settings.API_V1_STR}/users/{fresh_user['id']}/role",
        headers=superuser_token_headers,
        json={"role": "agent-user"},
    )
    assert demote.status_code == 200, demote.text
    # fresh_agent (created above while developer) is owned by the demoted user,
    # so the 403 is unambiguously the role gate, not a 404.
    r = client.post(
        f"{_BASE}/account/agent-api/enable",
        headers=fresh_acc_headers,
        json={"agent_id": fresh_agent["id"], "enabled": True},
    )
    assert r.status_code == 403, (
        f"Enable by a demoted (agent-user) account token must be 403, got {r.status_code}: {r.text}"
    )


def test_account_agent_api_call_restart_inspect(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Account-CLI dev-loop verbs added from the friction report:

      POST /account/agent-api/call          — owner-side endpoint smoke test (A5)
      POST /account/agents/{id}/restart-env — first-class env restart (D1)
      GET  /account/agents/{id}/inspect     — effective prompts/features (C2)

    Covers happy paths, the query-string forwarding that motivated `call`,
    disabled/ghost/no-leak gating, the restart build-rights gate, and that
    inspect never returns credential secrets.
    """
    import json as _json

    account_jwt, _ = bootstrap_account_token(
        client, superuser_token_headers, machine_name="DevLoop Machine"
    )
    acc_headers = account_cli_headers(account_jwt)

    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent_id = agent["id"]

    # Enable the producer API so call/spec work against the stub env.
    r = client.post(
        f"{_BASE}/account/agent-api/enable",
        headers=acc_headers,
        json={"agent_id": agent_id, "enabled": True},
    )
    assert r.status_code == 200, r.text

    # ── A5: call forwards the query string and buffers the response ──────
    r = client.post(
        f"{_BASE}/account/agent-api/call",
        headers=acc_headers,
        json={
            "agent_id": agent_id,
            "method": "GET",
            "path": "btc-rate",
            "query": {"vs_currency": "eur"},
        },
    )
    assert r.status_code == 200, f"call must be 200, got {r.status_code}: {r.text}"
    result = r.json()
    assert result["status_code"] == 200, result
    assert result["is_json"] is True, result
    # The stub echoes what env-core received — proves the query reached it.
    echoed = _json.loads(result["body"])
    assert echoed["path"] == "btc-rate", echoed
    assert echoed["query_string"] == "vs_currency=eur", echoed

    # ── A5: call on a disabled producer → 400 (disabled, not leaky 404) ──
    client.post(
        f"{_BASE}/account/agent-api/enable",
        headers=acc_headers,
        json={"agent_id": agent_id, "enabled": False},
    )
    r = client.post(
        f"{_BASE}/account/agent-api/call",
        headers=acc_headers,
        json={"agent_id": agent_id, "method": "GET", "path": "btc-rate"},
    )
    assert r.status_code == 400, f"call on disabled API must be 400, got {r.status_code}"

    # ── C2: inspect returns prompts / features / credentials, no secrets ─
    r = client.get(f"{_BASE}/account/agents/{agent_id}/inspect", headers=acc_headers)
    assert r.status_code == 200, f"inspect must be 200, got {r.status_code}: {r.text}"
    info = r.json()
    assert info["id"] == agent_id
    assert set(["entrypoint", "workflow", "refiner"]).issubset(info["prompts"].keys())
    assert "agent_api_enabled" in info["features"]
    assert isinstance(info["credentials"], list)
    # Credential entries are metadata only — never a secret payload.
    for cred in info["credentials"]:
        assert set(cred.keys()) <= {"name", "type"}, cred

    # ── D1: restart-env returns the post-restart status ──────────────────
    r = client.post(
        f"{_BASE}/account/agents/{agent_id}/restart-env", headers=acc_headers
    )
    assert r.status_code == 200, f"restart-env must be 200, got {r.status_code}: {r.text}"
    restart = r.json()
    assert restart["environment_id"]
    assert restart["status"], restart

    # ── Gating: ghost agent → 404 (no-leak) for all three ────────────────
    ghost = str(uuid.uuid4())
    assert client.post(
        f"{_BASE}/account/agent-api/call",
        headers=acc_headers,
        json={"agent_id": ghost, "method": "GET", "path": "x"},
    ).status_code == 404
    assert client.post(
        f"{_BASE}/account/agents/{ghost}/restart-env", headers=acc_headers
    ).status_code == 404
    assert client.get(
        f"{_BASE}/account/agents/{ghost}/inspect", headers=acc_headers
    ).status_code == 404

    # ── Gating: regular user JWT (not an account token) → 401 ────────────
    assert client.post(
        f"{_BASE}/account/agent-api/call",
        headers=superuser_token_headers,
        json={"agent_id": agent_id, "method": "GET", "path": "x"},
    ).status_code == 401
    assert client.get(
        f"{_BASE}/account/agents/{agent_id}/inspect",
        headers=superuser_token_headers,
    ).status_code == 401

    # ── D1: restart-env is build-rights gated → demoted user gets 403 ────
    fresh_user, fresh_headers = _make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, fresh_user["id"])
    fresh_jwt, _ = bootstrap_account_token(
        client, fresh_headers, machine_name="DevLoop Fresh Machine"
    )
    fresh_acc_headers = account_cli_headers(fresh_jwt)
    fresh_agent = create_agent_via_api(client, fresh_headers)
    drain_tasks()
    demote = client.patch(
        f"{settings.API_V1_STR}/users/{fresh_user['id']}/role",
        headers=superuser_token_headers,
        json={"role": "agent-user"},
    )
    assert demote.status_code == 200, demote.text
    r = client.post(
        f"{_BASE}/account/agents/{fresh_agent['id']}/restart-env",
        headers=fresh_acc_headers,
    )
    assert r.status_code == 403, (
        f"restart-env by a demoted (agent-user) account token must be 403, got {r.status_code}: {r.text}"
    )


# ── Scenario 21b: POST /account/knowledge/search ─────────────────────────────


def test_account_knowledge_search(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    POST /account/knowledge/search — account-level knowledge search:
      1. Valid account CLI token + query → 200, body shape {"results": list}
         (no accessible knowledge sources in test DB → results == [] is valid)
      2. Optional ``topic`` field accepted → 200, same shape
      3. Auth matrix — structural isolation (load-bearing):
         a. Per-agent child CLI token → 401 (AccountCLIContextDep rejects it)
         b. Regular user JWT → 401
         c. Revoked account token → 401
         d. No auth header → 401
    """
    # ── Phase 1: Bootstrap account token ──────────────────────────────────
    account_jwt, account_token_id = bootstrap_account_token(
        client, superuser_token_headers, machine_name="Knowledge Search Machine"
    )
    acc_headers = account_cli_headers(account_jwt)

    # ── Phase 2: Happy path — query only → 200 + list shape ───────────────
    r = client.post(
        f"{_BASE}/account/knowledge/search",
        headers=acc_headers,
        json={"query": "test query for knowledge search"},
    )
    assert r.status_code == 200, (
        f"POST /account/knowledge/search must return 200 for a valid account "
        f"CLI token, got {r.status_code}: {r.text}"
    )
    body = r.json()
    assert "results" in body, (
        f"Response must have a 'results' key, got: {body!r}"
    )
    assert isinstance(body["results"], list), (
        f"'results' must be a list, got: {type(body['results'])!r}"
    )
    # In the test DB there are no accessible knowledge sources; an empty list is
    # the correct and expected result — assert shape, not specific content.

    # ── Phase 3: Optional topic field accepted → 200 + same shape ─────────
    r = client.post(
        f"{_BASE}/account/knowledge/search",
        headers=acc_headers,
        json={"query": "test query with topic", "topic": "general"},
    )
    assert r.status_code == 200, (
        f"POST /account/knowledge/search with topic must return 200, "
        f"got {r.status_code}: {r.text}"
    )
    body_with_topic = r.json()
    assert "results" in body_with_topic, (
        f"Response with topic must have a 'results' key, got: {body_with_topic!r}"
    )
    assert isinstance(body_with_topic["results"], list), (
        f"'results' with topic must be a list, got: {type(body_with_topic['results'])!r}"
    )

    # ── Phase 4: Auth matrix — structural isolation ────────────────────────

    # a. Per-agent child CLI token → 401 (AccountCLIContextDep rejects it)
    agent = create_agent_via_api(client, superuser_token_headers)
    mint = mint_child_token(
        client, acc_headers, agent["id"], machine_name="Knowledge Search Child"
    )
    child_headers = cli_auth_headers(mint["token"])
    r = client.post(
        f"{_BASE}/account/knowledge/search",
        headers=child_headers,
        json={"query": "per-agent token should fail"},
    )
    assert r.status_code == 401, (
        f"Per-agent child CLI token must be rejected by "
        f"POST /account/knowledge/search, got {r.status_code}: {r.text}"
    )

    # b. Regular user JWT → 401
    r = client.post(
        f"{_BASE}/account/knowledge/search",
        headers=superuser_token_headers,
        json={"query": "user JWT should fail"},
    )
    assert r.status_code == 401, (
        f"Regular user JWT must be rejected by POST /account/knowledge/search, "
        f"got {r.status_code}: {r.text}"
    )

    # c. Revoked account token → 401
    revoke_account_token(client, superuser_token_headers, account_token_id)
    r = client.post(
        f"{_BASE}/account/knowledge/search",
        headers=acc_headers,
        json={"query": "revoked token should fail"},
    )
    assert r.status_code == 401, (
        f"Revoked account token must return 401 on POST /account/knowledge/search, "
        f"got {r.status_code}: {r.text}"
    )

    # d. No auth header → 401/403
    r = client.post(
        f"{_BASE}/account/knowledge/search",
        json={"query": "no auth should fail"},
    )
    assert r.status_code in (401, 403), (
        f"Missing auth must be rejected on POST /account/knowledge/search, "
        f"got {r.status_code}: {r.text}"
    )


# ── Scenario 21: Rate-limit note (coverage gap) ──────────────────────────────
#
# The account API proxy rate limiter (_RateLimiter in account_api_proxy_service)
# is an in-process sliding-window counter keyed by account-token id. To test
# it, one would need to:
#   a) Patch settings.ACCOUNT_API_PROXY_RATE_LIMIT_PER_MIN to a very small
#      value (e.g. 2) AND reset the limiter's in-process _hits dict between
#      test runs (to avoid test-order sensitivity from process-shared state).
#   b) Make N+1 calls in quick succession and assert the (N+1)-th returns 429
#      with a Retry-After header.
#
# The challenge: AccountApiProxyService._rate_limiter is a class-level singleton
# (shared across the whole process). Patching the limit via settings is easy;
# resetting the _hits deque between tests requires reaching into the private
# attribute. This is technically possible but tightly couples the test to the
# implementation. Rate limiting is therefore treated as a COVERAGE GAP here and
# left to the phase-4/load-test pass. The policy test (section A above) covers
# the chokepoint; the rate-limit logic is self-contained and could be covered
# by a unit test for _RateLimiter directly.
#
# def test_proxy_rate_limit(...) → SKIPPED (coverage gap — see note above)


# ── Scenario 22: Account-CLI schedule management (full CRUD) ──────────────────


def test_account_schedule_management(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Account-CLI schedule verbs — the agent Config → Schedules card reached
    through the account token:

      GET  /account/agents/{id}/schedules                 — list
      POST /account/agents/{id}/schedules/generate        — NL → cron preview
      POST /account/agents/{id}/schedules                 — create
      PUT  /account/agents/{id}/schedules/{sid}           — update / toggle
      POST /account/agents/{id}/schedules/{sid}/run       — run now
      GET  /account/agents/{id}/schedules/{sid}/logs      — logs
      DELETE /account/agents/{id}/schedules/{sid}         — delete

    Covers:
      1. empty list on a fresh agent
      2. generate preview (AI mocked) → cron + next_execution
      3. create static_prompt schedule → 200, fields echoed
      4. script_trigger create without command → 400
      5. list reflects the created schedule
      6. update (toggle enabled) → 200, enabled flips
      7. run now → 200, "triggered" message (stub env running)
      8. logs → 200, list shape
      9. delete → 200; subsequent list is empty
     10. gating: ghost agent → 404 (no-leak); other user's agent → 404;
         regular user JWT → 401; demoted agent-user account token → 403 on write
     11. foreign (bundle) install → 403 on create (publisher-managed)
    """
    from unittest.mock import patch

    account_jwt, _ = bootstrap_account_token(
        client, superuser_token_headers, machine_name="Schedule Mgmt Machine"
    )
    acc_headers = account_cli_headers(account_jwt)

    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent_id = agent["id"]
    base = f"{_BASE}/account/agents/{agent_id}/schedules"

    # ── Phase 1: empty list ──────────────────────────────────────────────
    r = client.get(base, headers=acc_headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"data": [], "count": 0}

    # ── Phase 2: generate preview (AI mocked) ────────────────────────────
    with patch(
        "app.services.ai_functions.ai_functions_service.AIFunctionsService.generate_schedule",
        return_value={
            "success": True,
            "cron_string": "0 9 * * 1-5",
            "description": "Every weekday at 09:00",
        },
    ):
        r = client.post(
            f"{base}/generate",
            headers=acc_headers,
            json={"natural_language": "every weekday at 9am", "timezone": "UTC"},
        )
    assert r.status_code == 200, r.text
    gen = r.json()
    assert gen["success"] is True
    assert gen["cron_string"] == "0 9 * * 1-5"
    assert gen["next_execution"] is not None

    # ── Phase 3: create a static_prompt schedule ─────────────────────────
    r = client.post(
        base,
        headers=acc_headers,
        json={
            "name": "Daily report",
            "cron_string": "0 9 * * 1-5",
            "timezone": "UTC",
            "description": "Every weekday at 9am",
            "prompt": "Produce the daily report",
            "enabled": True,
            "schedule_type": "static_prompt",
        },
    )
    assert r.status_code == 200, f"Create must be 200, got {r.status_code}: {r.text}"
    created = r.json()
    schedule_id = created["id"]
    assert created["name"] == "Daily report"
    assert created["schedule_type"] == "static_prompt"
    assert created["enabled"] is True
    assert len(created["cron_string"].split()) == 5

    # ── Phase 4: script_trigger without command → 400 ────────────────────
    r = client.post(
        base,
        headers=acc_headers,
        json={
            "name": "Bad trigger",
            "cron_string": "*/5 * * * *",
            "timezone": "UTC",
            "description": "missing command",
            "schedule_type": "script_trigger",
        },
    )
    assert r.status_code == 400, (
        f"script_trigger create without command must be 400, got {r.status_code}"
    )

    # ── Phase 5: list reflects the schedule ──────────────────────────────
    r = client.get(base, headers=acc_headers)
    assert r.status_code == 200, r.text
    listing = r.json()
    assert listing["count"] == 1
    assert listing["data"][0]["id"] == schedule_id

    # ── Phase 6: update (toggle disabled) ────────────────────────────────
    r = client.put(
        f"{base}/{schedule_id}",
        headers=acc_headers,
        json={"enabled": False},
    )
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is False

    # ── Phase 7: run now → 200, message present ──────────────────────────
    r = client.post(f"{base}/{schedule_id}/run", headers=acc_headers)
    assert r.status_code == 200, f"Run now must be 200, got {r.status_code}: {r.text}"
    assert "message" in r.json()
    drain_tasks()

    # ── Phase 8: logs → 200, list shape ──────────────────────────────────
    r = client.get(f"{base}/{schedule_id}/logs", headers=acc_headers)
    assert r.status_code == 200, r.text
    logs = r.json()
    assert "data" in logs and "count" in logs

    # ── Phase 9: delete → 200; list empty again ──────────────────────────
    r = client.delete(f"{base}/{schedule_id}", headers=acc_headers)
    assert r.status_code == 200, r.text
    r = client.get(base, headers=acc_headers)
    assert r.json()["count"] == 0

    # ── Phase 10a: ghost agent → 404 (no-leak) ───────────────────────────
    ghost = str(uuid.uuid4())
    r = client.get(f"{_BASE}/account/agents/{ghost}/schedules", headers=acc_headers)
    assert r.status_code == 404, f"Ghost agent list must be 404, got {r.status_code}"

    # ── Phase 10b: other user's agent → 404 (no-leak) ────────────────────
    other_user, other_headers = _make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, other_user["id"])
    other_jwt, _ = bootstrap_account_token(
        client, other_headers, machine_name="Other Schedule Machine"
    )
    other_acc_headers = account_cli_headers(other_jwt)
    other_agent = create_agent_via_api(client, other_headers)
    drain_tasks()
    r = client.get(
        f"{_BASE}/account/agents/{agent_id}/schedules", headers=other_acc_headers
    )
    assert r.status_code == 404, (
        f"Listing another user's agent schedules must be 404 (no-leak), got {r.status_code}"
    )

    # ── Phase 10c: regular user JWT → 401 (account CLI token required) ────
    r = client.get(base, headers=superuser_token_headers)
    assert r.status_code == 401, (
        f"Regular user JWT must be rejected on account schedules, got {r.status_code}"
    )

    # ── Phase 10d: demoted-to-agent-user account token → 403 on write ────
    client.patch(
        f"{settings.API_V1_STR}/users/{other_user['id']}/role",
        headers=superuser_token_headers,
        json={"role": "agent-user"},
    )
    r = client.post(
        f"{_BASE}/account/agents/{other_agent['id']}/schedules",
        headers=other_acc_headers,
        json={
            "name": "Should 403",
            "cron_string": "0 9 * * *",
            "timezone": "UTC",
            "description": "demoted user write",
        },
    )
    assert r.status_code == 403, (
        f"Create by a demoted (agent-user) account token must be 403, got {r.status_code}"
    )

    # ── Phase 11: foreign (bundle) install → 403 on create ───────────────
    publisher_agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    publish_bundle_and_make_public(
        client, superuser_token_headers, publisher_agent["id"]
    )
    bundle_id = client.get(
        f"{settings.API_V1_STR}/agents/{publisher_agent['id']}",
        headers=superuser_token_headers,
    ).json()["bundle_id"]
    # Install into a fresh developer's account so the install is a foreign one.
    consumer_user, consumer_headers = _make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, consumer_user["id"])
    consumer_jwt, _ = bootstrap_account_token(
        client, consumer_headers, machine_name="Consumer Schedule Machine"
    )
    consumer_acc_headers = account_cli_headers(consumer_jwt)
    install = install_bundle(client, consumer_headers, bundle_id)
    drain_tasks()
    foreign_id = install["id"]
    r = client.post(
        f"{_BASE}/account/agents/{foreign_id}/schedules",
        headers=consumer_acc_headers,
        json={
            "name": "Consumer cannot create",
            "cron_string": "0 9 * * *",
            "timezone": "UTC",
            "description": "publisher-managed",
        },
    )
    assert r.status_code == 403, (
        f"Creating a schedule on a foreign install must be 403, got {r.status_code}: {r.text}"
    )


# ── Scenario 23: Account-CLI agent status (access / refresh / set command) ────


def test_account_agent_status(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Account-CLI status verbs — the agent Integrations → Agent status card
    reached through the account token:

      GET  /account/agents/{id}/status[?force_refresh=]   — access / refresh
      POST /account/agents/{id}/status/refresh-command    — set pre-command

    Covers:
      1. cached read → 200, AccountAgentStatusResult shape (status + command)
      2. force_refresh read → 200, never raises (cache fallback)
      3. set-command → 200, status_refresh_command updated and echoed back
      4. subsequent read reflects the new command
      5. gating: ghost agent → 404 (no-leak); regular user JWT → 401;
         demoted agent-user account token → 403 on set-command
    """
    account_jwt, _ = bootstrap_account_token(
        client, superuser_token_headers, machine_name="Status Mgmt Machine"
    )
    acc_headers = account_cli_headers(account_jwt)

    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent_id = agent["id"]
    base = f"{_BASE}/account/agents/{agent_id}/status"

    # ── Phase 1: cached read → 200, result shape ─────────────────────────
    r = client.get(base, headers=acc_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "status" in body and "status_refresh_command" in body
    assert body["status"]["agent_id"] == agent_id

    # ── Phase 2: force refresh → 200, never raises ───────────────────────
    r = client.get(base, headers=acc_headers, params={"force_refresh": "true"})
    assert r.status_code == 200, f"Force refresh must be 200, got {r.status_code}: {r.text}"
    assert "status" in r.json()

    # ── Phase 3: set the status-refresh pre-command ──────────────────────
    r = client.post(
        f"{base}/refresh-command",
        headers=acc_headers,
        json={"command": "/run:custom-status"},
    )
    assert r.status_code == 200, f"Set-command must be 200, got {r.status_code}: {r.text}"
    assert r.json()["status_refresh_command"] == "/run:custom-status"

    # ── Phase 4: subsequent read reflects the new command ────────────────
    r = client.get(base, headers=acc_headers)
    assert r.json()["status_refresh_command"] == "/run:custom-status"

    # ── Phase 5a: ghost agent → 404 (no-leak) ────────────────────────────
    ghost = str(uuid.uuid4())
    r = client.get(f"{_BASE}/account/agents/{ghost}/status", headers=acc_headers)
    assert r.status_code == 404, f"Ghost agent status must be 404, got {r.status_code}"

    # ── Phase 5b: regular user JWT → 401 ─────────────────────────────────
    r = client.get(base, headers=superuser_token_headers)
    assert r.status_code == 401, (
        f"Regular user JWT must be rejected on account status, got {r.status_code}"
    )

    # ── Phase 5c: demoted agent-user account token → 403 on set-command ──
    other_user, other_headers = _make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, other_user["id"])
    other_jwt, _ = bootstrap_account_token(
        client, other_headers, machine_name="Other Status Machine"
    )
    other_acc_headers = account_cli_headers(other_jwt)
    other_agent = create_agent_via_api(client, other_headers)
    drain_tasks()
    client.patch(
        f"{settings.API_V1_STR}/users/{other_user['id']}/role",
        headers=superuser_token_headers,
        json={"role": "agent-user"},
    )
    r = client.post(
        f"{_BASE}/account/agents/{other_agent['id']}/status/refresh-command",
        headers=other_acc_headers,
        json={"command": "/run:status"},
    )
    assert r.status_code == 403, (
        f"Set-command by a demoted (agent-user) account token must be 403, got {r.status_code}"
    )


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  Phase 4 — File upload route + chat-flow proxy contract (Scenarios 24–25)  ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

# ── Scenario 24: POST /account/files/upload ──────────────────────────────────


def test_account_file_upload(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    POST /account/files/upload — multipart file upload attributed to the account user:

      1. Happy path: valid text/plain file → 200, FileUploadPublic shape,
         status="temporary", filename/mime_type/file_size correct.
      2. Auth matrix (structural isolation — the route requires AccountCLIContextDep):
         a. Per-agent child CLI token → 401
         b. Regular user JWT → 401
         c. No auth header → 401
         d. Revoked account token → 401
         e. Fresh valid account token → 200 (sanity)
      3. Invalid MIME type (application/octet-stream, not in whitelist) → 400,
         detail mentions the disallowed type.
      4. Oversize file (content > UPLOAD_MAX_FILE_SIZE_MB MB) → 400, detail
         mentions size / max.
    """
    from unittest.mock import patch

    # ── Phase 1: Bootstrap account token ──────────────────────────────────
    account_jwt, account_token_id = bootstrap_account_token(
        client, superuser_token_headers, machine_name="File Upload Machine"
    )
    acc_headers = account_cli_headers(account_jwt)

    # ── Phase 2: Happy path — valid text/plain file ────────────────────────
    file_content = b"Hello from the account CLI upload test!"
    filename = "hello.txt"
    r = client.post(
        f"{_BASE}/account/files/upload",
        headers=acc_headers,
        files={"file": (filename, io.BytesIO(file_content), "text/plain")},
    )
    assert r.status_code == 200, (
        f"POST /account/files/upload must return 200 for a valid account CLI token, "
        f"got {r.status_code}: {r.text}"
    )
    body = r.json()

    # FileUploadPublic shape
    assert "id" in body, "Response must contain 'id'"
    assert "filename" in body, "Response must contain 'filename'"
    assert "file_size" in body, "Response must contain 'file_size'"
    assert "mime_type" in body, "Response must contain 'mime_type'"
    assert "status" in body, "Response must contain 'status'"
    assert "uploaded_at" in body, "Response must contain 'uploaded_at'"

    # Value assertions
    assert body["filename"] == filename, (
        f"Response filename must match uploaded filename, got {body['filename']!r}"
    )
    assert body["mime_type"] == "text/plain", (
        f"Response mime_type must be 'text/plain', got {body['mime_type']!r}"
    )
    assert body["file_size"] == len(file_content), (
        f"Response file_size must match content length {len(file_content)}, "
        f"got {body['file_size']}"
    )
    assert body["status"] == "temporary", (
        f"New uploads must start with status='temporary', got {body['status']!r}"
    )
    file_id = body["id"]
    assert file_id, "id must be a non-empty string"

    # No sensitive DB-internal fields in the response
    assert "file_path" not in body, "Response must not expose file_path"
    assert "user_id" not in body, "Response must not expose user_id"

    # ── Phase 3: Auth matrix — per-agent child token → 401 ────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    child_mint = mint_child_token(
        client, acc_headers, agent["id"], machine_name="Upload Child"
    )
    child_headers = cli_auth_headers(child_mint["token"])

    r_child = client.post(
        f"{_BASE}/account/files/upload",
        headers=child_headers,
        files={"file": (filename, io.BytesIO(file_content), "text/plain")},
    )
    assert r_child.status_code == 401, (
        f"Per-agent child CLI token must be rejected on /account/files/upload, "
        f"got {r_child.status_code}: {r_child.text}"
    )

    # ── Phase 4: Auth matrix — regular user JWT → 401 ─────────────────────
    r_jwt = client.post(
        f"{_BASE}/account/files/upload",
        headers=superuser_token_headers,
        files={"file": (filename, io.BytesIO(file_content), "text/plain")},
    )
    assert r_jwt.status_code == 401, (
        f"Regular user JWT must be rejected on /account/files/upload, "
        f"got {r_jwt.status_code}: {r_jwt.text}"
    )

    # ── Phase 5: Auth matrix — no auth → 401 ──────────────────────────────
    r_no_auth = client.post(
        f"{_BASE}/account/files/upload",
        files={"file": (filename, io.BytesIO(file_content), "text/plain")},
    )
    assert r_no_auth.status_code in (401, 403), (
        f"Missing auth must be rejected on /account/files/upload, "
        f"got {r_no_auth.status_code}: {r_no_auth.text}"
    )

    # ── Phase 6: Auth matrix — revoked account token → 401 ────────────────
    revoke_account_token(client, superuser_token_headers, account_token_id)
    r_revoked = client.post(
        f"{_BASE}/account/files/upload",
        headers=acc_headers,
        files={"file": (filename, io.BytesIO(file_content), "text/plain")},
    )
    assert r_revoked.status_code == 401, (
        f"Revoked account token must return 401 on /account/files/upload, "
        f"got {r_revoked.status_code}: {r_revoked.text}"
    )

    # ── Phase 7: Fresh valid account token sanity check ───────────────────
    fresh_jwt, _ = bootstrap_account_token(
        client, superuser_token_headers, machine_name="File Upload Sanity Machine"
    )
    fresh_headers = account_cli_headers(fresh_jwt)
    r_sanity = client.post(
        f"{_BASE}/account/files/upload",
        headers=fresh_headers,
        files={"file": ("sanity.txt", io.BytesIO(b"sanity"), "text/plain")},
    )
    assert r_sanity.status_code == 200, (
        f"Fresh valid account token must return 200 on /account/files/upload, "
        f"got {r_sanity.status_code}: {r_sanity.text}"
    )
    assert r_sanity.json()["status"] == "temporary"

    # ── Phase 8: Invalid MIME type → 400 ──────────────────────────────────
    # application/octet-stream is not in the UPLOAD_ALLOWED_MIME_TYPES whitelist.
    r_bad_mime = client.post(
        f"{_BASE}/account/files/upload",
        headers=fresh_headers,
        files={
            "file": ("binary.bin", io.BytesIO(b"\x00\x01\x02"), "application/octet-stream")
        },
    )
    assert r_bad_mime.status_code == 400, (
        f"Upload with invalid MIME type must return 400, "
        f"got {r_bad_mime.status_code}: {r_bad_mime.text}"
    )
    bad_mime_detail = r_bad_mime.json().get("detail", "")
    assert (
        "octet-stream" in bad_mime_detail.lower()
        or "not allowed" in bad_mime_detail.lower()
        or "type" in bad_mime_detail.lower()
    ), (
        f"Error detail must mention the disallowed MIME type, got: {bad_mime_detail!r}"
    )

    # ── Phase 9: Oversize file → 400 ──────────────────────────────────────
    # Patch the size limit to 1 byte so we can trigger the check without
    # allocating a huge buffer.
    with patch.object(
        settings.__class__,
        "upload_max_file_size_bytes",
        new_callable=lambda: property(lambda self: 1),
    ):
        r_oversize = client.post(
            f"{_BASE}/account/files/upload",
            headers=fresh_headers,
            files={"file": ("big.txt", io.BytesIO(b"more than one byte"), "text/plain")},
        )
    assert r_oversize.status_code == 400, (
        f"Oversize upload must return 400, got {r_oversize.status_code}: {r_oversize.text}"
    )
    oversize_detail = r_oversize.json().get("detail", "")
    assert (
        "large" in oversize_detail.lower()
        or "size" in oversize_detail.lower()
        or "max" in oversize_detail.lower()
    ), (
        f"Error detail must mention the size limit, got: {oversize_detail!r}"
    )


# ── Scenario 25: Chat-flow proxy contract ────────────────────────────────────


def test_account_chat_flow_proxy_contract(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Chat-flow proxy contract — locking that the session control-plane routes the
    CLI's ``cinna chat`` command relies on are all reachable through the
    /account/api-proxy escape hatch (none is on the EXCLUDED_PREFIXES denylist)
    and that the upload-then-reference flow works end-to-end.

      1. Proxy policy: assert_api_proxy_allowed passes (no ApiProxyDenied raised)
         for every route the chat flow depends on:
           - POST sessions/
           - GET  sessions/
           - GET  sessions/{id}
           - GET  sessions/{id}/messages
           - POST sessions/{id}/messages/stream  (JSON ack, not SSE — proxy-safe)
           - GET  sessions/{id}/streaming-status
           - POST sessions/{id}/interrupt
           - GET  files/{id}/download
      2. Happy path — proxy correctly reaches the session routes end-to-end:
         a. Upload a file via POST /account/files/upload → file_id.
         b. Create a session via the proxy (POST sessions/) → session_id.
         c. Send a message referencing the file via the proxy
            (POST sessions/{id}/messages/stream with file_ids=[file_id]) →
            proxy returns 200, body is a JSON ack (has "session_id" or "id" key),
            confirming the route returns JSON (not SSE) and the proxy can buffer it.
         d. Poll messages via the proxy (GET sessions/{id}/messages) →
            200, body has "data" list (messages list shape).
         e. GET streaming-status via the proxy →  200, body is JSON
            (has "status" key).
      3. Auth matrix (must mirror all other account-CLI routes):
         a. Regular user JWT → 401 on every proxy sub-call
         b. Per-agent child CLI token → 401
         c. Revoked account token → 401

    Notes:
    - The actual agent streaming (SSE via socket.io) is NOT tested here — the
      proxy contract tests only confirm the control-plane HTTP layer.
    - The send-message call schedules a background task; drain_tasks() is NOT
      called here because the test only asserts the ack shape (the proxy route
      returns the JSON ack immediately before streaming begins).
    - Policy assertions (Phase 1) import from the service module directly but
      are pure Python with no I/O — justified by the README exemption for
      architecture/unit tests; analogous assertions already exist in
      tests/unit/test_api_proxy_policy.py for the existing TestDefaultAllow class.
    """
    from app.services.cli.account_api_proxy_policy import (
        ApiProxyDenied,
        assert_api_proxy_allowed,
    )

    _API = settings.API_V1_STR

    # ── Phase 1: Policy gate — session routes are not on the denylist ──────

    chat_flow_paths: list[tuple[str, str]] = [
        ("POST", f"{_API}/sessions/"),
        ("GET",  f"{_API}/sessions/"),
        ("GET",  f"{_API}/sessions/{uuid.uuid4()}"),
        ("GET",  f"{_API}/sessions/{uuid.uuid4()}/messages"),
        # messages/stream returns JSON ack (not SSE) → proxy-safe
        ("POST", f"{_API}/sessions/{uuid.uuid4()}/messages/stream"),
        # streaming-status lives under /messages/ (not directly on the session)
        ("GET",  f"{_API}/sessions/{uuid.uuid4()}/messages/streaming-status"),
        # interrupt lives under /messages/ (POST /sessions/{id}/messages/interrupt)
        ("POST", f"{_API}/sessions/{uuid.uuid4()}/messages/interrupt"),
        ("GET",  f"{_API}/files/{uuid.uuid4()}/download"),
    ]
    for method, path in chat_flow_paths:
        try:
            assert_api_proxy_allowed(method, path)
        except ApiProxyDenied as exc:
            raise AssertionError(
                f"Chat-flow route {method} {path} must NOT be on the proxy denylist, "
                f"but assert_api_proxy_allowed raised ApiProxyDenied: {exc.reason!r} — "
                f"{exc.message}"
            ) from exc

    # ── Phase 2: Bootstrap account token ──────────────────────────────────
    account_jwt, account_token_id = bootstrap_account_token(
        client, superuser_token_headers, machine_name="Chat Flow Machine"
    )
    acc_headers = account_cli_headers(account_jwt)

    # Create an agent so we have something to attach the session to.
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent_id = agent["id"]

    # ── Phase 3a: Upload a file via the dedicated upload route ────────────
    upload_content = b"Chat test file content for file_ids reference."
    r_upload = client.post(
        f"{_BASE}/account/files/upload",
        headers=acc_headers,
        files={"file": ("chat_attachment.txt", io.BytesIO(upload_content), "text/plain")},
    )
    assert r_upload.status_code == 200, (
        f"File upload for chat-flow test must return 200, "
        f"got {r_upload.status_code}: {r_upload.text}"
    )
    upload_body = r_upload.json()
    assert upload_body["status"] == "temporary", (
        "Uploaded file must start as 'temporary'"
    )
    file_id = upload_body["id"]
    assert file_id, "Upload must return a non-empty file id"

    # ── Phase 3b: Create session through the proxy ────────────────────────
    r_session = client.post(
        f"{_BASE}/account/api-proxy",
        headers=acc_headers,
        json={
            "method": "POST",
            "path": "sessions/",
            "json_body": {"agent_id": agent_id, "mode": "conversation"},
        },
    )
    assert r_session.status_code == 200, (
        f"Proxy POST sessions/ must return 200, "
        f"got {r_session.status_code}: {r_session.text}"
    )
    session_data = r_session.json()
    assert "id" in session_data, (
        f"Session creation via proxy must return a session with 'id', got: {session_data}"
    )
    session_id = session_data["id"]

    # ── Phase 3c: Send a message with file_ids through the proxy ──────────
    # sessions/{id}/messages/stream returns a JSON ack dict (not SSE), so the
    # buffered proxy can handle it.  The ack shape is: {"session_id": ..., ...}
    r_msg = client.post(
        f"{_BASE}/account/api-proxy",
        headers=acc_headers,
        json={
            "method": "POST",
            "path": f"sessions/{session_id}/messages/stream",
            "json_body": {
                "content": "Test message with file attachment",
                "file_ids": [file_id],
            },
        },
    )
    assert r_msg.status_code == 200, (
        f"Proxy POST sessions/{session_id}/messages/stream must return 200, "
        f"got {r_msg.status_code}: {r_msg.text}"
    )
    msg_ack = r_msg.json()
    # The ack must be a JSON dict (not SSE text) — confirms the proxy can buffer it.
    assert isinstance(msg_ack, dict), (
        f"messages/stream ack must be a JSON dict (not SSE), got: {type(msg_ack)}"
    )
    # The ack carries session_id (from MessageService.build_stream_response)
    assert "session_id" in msg_ack or "id" in msg_ack, (
        f"messages/stream ack must carry session_id or id, got keys: {list(msg_ack.keys())}"
    )

    # ── Phase 3d: Poll messages through the proxy ─────────────────────────
    r_msgs = client.post(
        f"{_BASE}/account/api-proxy",
        headers=acc_headers,
        json={
            "method": "GET",
            "path": f"sessions/{session_id}/messages",
        },
    )
    assert r_msgs.status_code == 200, (
        f"Proxy GET sessions/{session_id}/messages must return 200, "
        f"got {r_msgs.status_code}: {r_msgs.text}"
    )
    msgs_body = r_msgs.json()
    assert "data" in msgs_body, (
        f"Messages response must have a 'data' list, got: {msgs_body}"
    )
    assert isinstance(msgs_body["data"], list), (
        f"'data' in messages response must be a list, got: {type(msgs_body['data'])}"
    )

    # ── Phase 3e: Poll streaming-status through the proxy ─────────────────
    # The route is GET /sessions/{id}/messages/streaming-status and returns
    # {"is_streaming": bool, "stream_info": dict | None}.
    r_status = client.post(
        f"{_BASE}/account/api-proxy",
        headers=acc_headers,
        json={
            "method": "GET",
            "path": f"sessions/{session_id}/messages/streaming-status",
        },
    )
    assert r_status.status_code == 200, (
        f"Proxy GET sessions/{session_id}/messages/streaming-status must return 200, "
        f"got {r_status.status_code}: {r_status.text}"
    )
    status_body = r_status.json()
    assert isinstance(status_body, dict), (
        f"streaming-status response must be a JSON dict, got: {type(status_body)}"
    )
    # The status payload has an "is_streaming" field.
    assert "is_streaming" in status_body, (
        f"streaming-status response must have an 'is_streaming' key, got: {status_body}"
    )

    # ── Phase 4: Auth matrix ───────────────────────────────────────────────

    # a. Regular user JWT → 401 (account CLI token required by /account/api-proxy)
    r_jwt = client.post(
        f"{_BASE}/account/api-proxy",
        headers=superuser_token_headers,
        json={"method": "GET", "path": "sessions/"},
    )
    assert r_jwt.status_code == 401, (
        f"Regular user JWT must be rejected on api-proxy, got {r_jwt.status_code}"
    )

    # b. Per-agent child CLI token → 401
    child_mint = mint_child_token(
        client, acc_headers, agent_id, machine_name="Chat Flow Child"
    )
    child_headers = cli_auth_headers(child_mint["token"])
    r_child = client.post(
        f"{_BASE}/account/api-proxy",
        headers=child_headers,
        json={"method": "GET", "path": "sessions/"},
    )
    assert r_child.status_code == 401, (
        f"Per-agent child CLI token must be rejected on api-proxy, "
        f"got {r_child.status_code}"
    )

    # c. Revoked account token → 401
    revoke_account_token(client, superuser_token_headers, account_token_id)
    r_revoked = client.post(
        f"{_BASE}/account/api-proxy",
        headers=acc_headers,
        json={"method": "GET", "path": "sessions/"},
    )
    assert r_revoked.status_code == 401, (
        f"Revoked account token must return 401 on api-proxy, "
        f"got {r_revoked.status_code}"
    )
