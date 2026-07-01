"""
Backend tests for the Local Development CLI feature.

Covers:
- Setup token creation, exchange (full lifecycle), and error cases
- CLI token listing, filtering by agent, and revocation
- CLI-authenticated endpoints: build-context, building-context,
  workspace (404 without env), workspace/manifest (404 without env),
  knowledge/search
- Auth guards: unauthenticated, wrong user, revoked token, wrong agent scope
- Keep-alive: CLI calls refresh environment.last_activity_at
- Auto-activation: suspended env is activated on workspace endpoints
- Error state guard: non-recoverable env state returns 409
- No environment guard: missing active env returns 404
"""
import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
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
    # The per-agent setup-token route is now gated by ``assert_can_build``
    # rather than bare ownership. Another user cannot access this agent, so the
    # request 404s (no existence leak) instead of the old ownership 400.
    assert r.status_code == 404

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
      2. GET building-context → 200, has "building_prompt" (minimal fallback)
      3. POST knowledge/search → 200, has "results" key
      4. Scoping guard: access different agent's endpoints with the token → 403
      5. Auth guard: plain user JWT rejected by CLI endpoints → 401
      6. Revoke token, then use it → 401
    """
    # ── Phase 1: Bootstrap CLI token ─────────────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    token_resp = create_setup_token(client, superuser_token_headers, agent_id)
    exchange = exchange_setup_token(client, token_resp["token"], machine_name="CI Machine")
    cli_jwt = exchange["cli_token"]
    cli_headers = cli_auth_headers(cli_jwt)

    # Fetch token id for later revocation (phase 9)
    tokens = list_cli_tokens(client, superuser_token_headers)
    assert len(tokens) == 1
    cli_token_id = tokens[0]["id"]

    # ── Phase 2: GET building-context → 200 (minimal fallback) ───────────
    r = client.get(f"{_BASE}/agents/{agent_id}/building-context", headers=cli_headers)
    assert r.status_code == 200
    body = r.json()
    # No live Docker environment in tests → _minimal_building_context is returned
    assert "building_prompt" in body
    assert "settings" in body

    # ── Phase 3: POST knowledge/search → 200 ─────────────────────────────
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

    # ── Phase 4: Scoping guard — wrong agent_id → 403 ────────────────────
    other_agent = create_agent_via_api(client, superuser_token_headers)
    other_agent_id = other_agent["id"]

    # CLI token is scoped to `agent_id`, so accessing `other_agent_id` → 403
    r = client.get(
        f"{_BASE}/agents/{other_agent_id}/building-context",
        headers=cli_headers,
    )
    assert r.status_code == 403

    # ── Phase 5: Plain user JWT rejected by CLI dep → 401 ────────────────
    # The CLIContextDep decodes the token and checks token_type == "cli"
    # A regular user JWT has no token_type claim → 401
    r = client.get(
        f"{_BASE}/agents/{agent_id}/building-context",
        headers=superuser_token_headers,
    )
    assert r.status_code == 401

    # ── Phase 6: Revoke token, then use it → 401 ─────────────────────────
    revoke_cli_token(client, superuser_token_headers, cli_token_id)

    r = client.get(
        f"{_BASE}/agents/{agent_id}/building-context",
        headers=cli_headers,
    )
    assert r.status_code == 401
    assert "revoked" in r.json()["detail"].lower()


# ── Scenario 3: Keep-alive and auto-activation ───────────────────────────────

def test_cli_environment_keep_alive_and_auto_activation(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    CLI environment keep-alive and auto-activation:
      1. Create agent, setup token, exchange → obtain CLI JWT
      2. Keep-alive: set env to running, make a CLI call, verify last_activity_at updated
      3. Auto-activation: set env to suspended, call workspace → succeeds
         (not 404/409), proving the auto-activation path ran
      4. Error state: set env to error, call workspace → 409
      5. No environment: deactivate env (is_active=False), call workspace → 404
    """
    from app.models import AgentEnvironment

    # ── Phase 1: Bootstrap CLI token ─────────────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    # Environment creation runs as a background task — drain before checking
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    agent_id = agent["id"]
    env_id = agent["active_environment_id"]
    assert env_id is not None, "Agent must have an active environment for this test"

    token_resp = create_setup_token(client, superuser_token_headers, agent_id)
    exchange = exchange_setup_token(client, token_resp["token"], machine_name="Keep-Alive Machine")
    cli_jwt = exchange["cli_token"]
    cli_headers = cli_auth_headers(cli_jwt)

    # ── Phase 2: Keep-alive — CLI calls update last_activity_at ──────────
    # Set environment to running via test DB so the keep-alive path executes
    env = db.get(AgentEnvironment, env_id)
    assert env is not None
    env.status = "running"
    env.last_activity_at = None  # clear so we can detect the update
    db.add(env)
    db.flush()

    # Record time just before the CLI call
    before_call = datetime.now(UTC) - timedelta(seconds=1)

    # Make any CLI-authenticated call — get_cli_context runs on every CLI request
    r = client.get(f"{_BASE}/agents/{agent_id}/building-context", headers=cli_headers)
    assert r.status_code == 200

    # Refresh and verify last_activity_at was updated by get_cli_context
    db.refresh(env)
    assert env.last_activity_at is not None, (
        "get_cli_context must update environment.last_activity_at on every CLI call"
    )
    # Normalise to UTC for comparison (DB may return naive datetime)
    activity_ts = env.last_activity_at
    if activity_ts.tzinfo is None:
        from datetime import timezone
        activity_ts = activity_ts.replace(tzinfo=timezone.utc)
    assert activity_ts >= before_call, (
        f"last_activity_at ({activity_ts}) should be >= time before CLI call ({before_call})"
    )

    # ── Phase 3: Suspended env → auto-activation on workspace/manifest ───
    # Set status to suspended in the test DB
    db.refresh(env)
    env.status = "suspended"
    db.add(env)
    db.flush()

    # The route calls CLIService.ensure_environment_running(), which now activates
    # the env via create_session() (patched to the test session) using the test
    # environment adapter — so the suspended → running transition actually happens
    # against the test DB and is observable here.
    #
    # The activation succeeds; only the subsequent workspace tarball download
    # fails (no real Docker container behind the stub adapter), which the route
    # maps to a deterministic 502. A 404 (no env) or 409 (bad state) would mean
    # the auto-activation path was skipped or rejected — those must NOT occur.
    r = client.get(f"{_BASE}/agents/{agent_id}/workspace", headers=cli_headers)
    assert r.status_code == 502, (
        f"Expected 502 (env activated, tarball download has no real container), "
        f"got {r.status_code}: {r.text}"
    )

    # The environment must have actually transitioned suspended → running.
    db.expire(env)
    env = db.get(AgentEnvironment, env_id)
    assert env.status == "running", (
        f"Auto-activation must transition the env to 'running', got {env.status!r}"
    )

    # ── Phase 4: Non-recoverable state → 409 ─────────────────────────────
    # Re-fetch env inside the test session — it may have been updated in a separate
    # session during phase 3, so expire and reload from the test DB.
    db.expire(env)
    env = db.get(AgentEnvironment, env_id)
    env.status = "error"
    db.add(env)
    db.flush()

    r = client.get(f"{_BASE}/agents/{agent_id}/workspace", headers=cli_headers)
    assert r.status_code == 409, (
        f"Expected 409 for non-recoverable env state, got {r.status_code}: {r.text}"
    )
    detail = r.json()["detail"].lower()
    assert "error" in detail or "state" in detail, (
        f"409 detail should mention the env state, got: {r.json()['detail']!r}"
    )

    # ── Phase 5: No environment → 404 ────────────────────────────────────
    # Deactivate the environment so get_cli_context finds no active env
    db.expire(env)
    env = db.get(AgentEnvironment, env_id)
    env.is_active = False
    db.add(env)
    db.flush()

    r = client.get(f"{_BASE}/agents/{agent_id}/workspace", headers=cli_headers)
    assert r.status_code == 404, (
        f"Expected 404 when no active environment, got {r.status_code}: {r.text}"
    )
    assert "active" in r.json()["detail"].lower() or "environment" in r.json()["detail"].lower(), (
        f"404 detail should mention environment, got: {r.json()['detail']!r}"
    )


# ── Scenario 5: Bootstrap script renders a CLI-version gate ──────────────────

def test_bootstrap_script_embeds_version_gate(
    client: TestClient,
) -> None:
    """The `curl | python3` bootstrap script must guard against an outdated CLI.

    The GET bootstrap routes render a self-contained Python script (no token
    validation — the token is just embedded). Both the per-agent and account
    flavors must embed the platform's MINIMUM_CLI_VERSION and the version-check
    logic so an old `cinna` is told to upgrade instead of failing with a
    confusing "No such command" error when the new subcommand is invoked.
    """
    for path, expected_subcommand in (
        ("/api/cli-setup/sometoken", '"setup"'),
        ("/api/cli-setup/account/sometoken", '"account", "setup"'),
    ):
        r = client.get(path)
        assert r.status_code == 200, f"{path} -> {r.status_code}: {r.text}"
        script = r.text

        # The configured minimum version and the gate that consumes it.
        assert f'MINIMUM_CLI_VERSION = "{settings.MINIMUM_CLI_VERSION}"' in script
        assert "_installed_cli_version" in script
        assert "installed < required" in script
        # The upgrade guidance shown when the gate trips.
        assert "pip install --upgrade cinna-cli" in script
        assert "uv tool upgrade cinna-cli" in script
        # The correct subcommand is still wired for the up-to-date path.
        assert expected_subcommand in script

        # The rendered script must be valid, importable Python.
        import ast

        ast.parse(script)
