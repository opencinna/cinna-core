"""
Backend tests for agent-creation limits keyed on email-confirmation status.

Coverage:
  1. Unconfirmed developer: up to 5 agents allowed; 6th → 403 with limit message.
  2. Confirmed developer: 6th agent is allowed (5-cap does not apply).
  3. Superuser is not blocked at the unconfirmed boundary (creates past 5 freely).
  4. Account-CLI account_create_agent path enforces the same limit (403 on 6th).

Design notes
────────────
``POST /agents/`` calls ``AgentService.create_agent``, which calls
``_enforce_agent_creation_limit`` before inserting the row. On limit hit the
service raises ``ValueError``, which the route translates to HTTP 403.

The limit counts only user-created standalone agents:
  owner_id == user AND bundle_uuid IS NULL AND is_publisher_install == False

Email confirmation is set by generating a token via
``generate_email_confirmation_token(email=...)`` and posting it to
``POST /confirm-email/``.

Role promotion is required because ``POST /agents/`` is gated on the
``agent-developer`` role (Phase-3 RBAC). Tests that use a freshly signed-up
user must promote them with ``promote_to_developer`` before creating agents.

The autouse fixtures in ``conftest.py`` (environment adapter stub, session
proxy, background task collector) apply to this file automatically.

Settings:
  AGENT_LIMIT_UNCONFIRMED = 5
  AGENT_LIMIT_CONFIRMED   = 50
"""

from fastapi.testclient import TestClient

from app.core.config import settings
from app.utils import generate_email_confirmation_token
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.cli import (
    account_cli_headers,
    bootstrap_account_token,
)
from tests.utils.user import (
    create_random_user,
    promote_to_developer,
    user_authentication_headers,
)

_BASE = settings.API_V1_STR
_AGENTS_URL = f"{_BASE}/agents/"


# ── Inline helpers ────────────────────────────────────────────────────────────


def _confirm_email(client: TestClient, email: str) -> None:
    """Generate a confirmation token and POST it to confirm the user's email."""
    token = generate_email_confirmation_token(email=email)
    r = client.post(f"{_BASE}/confirm-email/", json={"token": token})
    assert r.status_code == 200, f"Email confirmation failed: {r.text}"


def _create_developer_user(
    client: TestClient,
    superuser_headers: dict[str, str],
) -> tuple[dict, dict[str, str]]:
    """Create a user, promote to developer, and add a default AI credential.

    A default AI credential is required for ``create_environment`` validation —
    without one, agent creation raises ``EnvironmentCredentialError`` before even
    reaching the creation-limit check. This mirrors ``make_user_and_headers`` from
    tests/utils/bundle.py but also promotes to the developer role.
    """
    user = create_random_user(client)
    headers = user_authentication_headers(
        client=client, email=user["email"], password=user["_password"]
    )
    promote_to_developer(client, superuser_headers, user["id"])
    # Each non-superuser needs their own default AI credential for environment creation.
    create_random_ai_credential(client, headers, set_default=True)
    return user, headers


def _create_agent(client: TestClient, headers: dict[str, str], name: str = "Agent") -> dict:
    """POST /agents/ and return the response JSON (asserts 200)."""
    r = client.post(_AGENTS_URL, headers=headers, json={"name": name})
    assert r.status_code == 200, f"Agent creation failed: {r.text}"
    return r.json()


def _create_n_agents(
    client: TestClient, headers: dict[str, str], n: int
) -> None:
    """Create ``n`` agents and assert all succeed."""
    for i in range(n):
        _create_agent(client, headers, name=f"Agent {i + 1}")


# ── Scenario 1: Unconfirmed developer hits the 5-agent cap ───────────────────


def test_unconfirmed_developer_hits_5_agent_cap(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    An unconfirmed developer can create up to 5 agents; the 6th returns 403.

    1. Sign up a new user (unconfirmed by default).
    2. Promote to agent-developer (required to use POST /agents/).
    3. Verify email_confirmed=False on /users/me.
    4. Create agents 1–5 — all succeed (200).
    5. Create agent 6 — must return 403 with the limit message.
    6. Verify the 403 detail references the limit and the confirm-email upgrade path.
    """
    # ── Phase 1: Create + promote a developer with AI credential ─────────────
    user, headers = _create_developer_user(client, superuser_token_headers)

    # ── Phase 2: Verify user is unconfirmed ───────────────────────────────────
    me = client.get(f"{_BASE}/users/me", headers=headers).json()
    assert me["email_confirmed"] is False, "Test pre-condition: user must be unconfirmed"

    # ── Phase 3: Create 5 agents (at the limit, not over it) ─────────────────
    _create_n_agents(client, headers, 5)

    # ── Phase 4: 6th agent must be rejected with 403 ─────────────────────────
    r = client.post(_AGENTS_URL, headers=headers, json={"name": "Over Limit Agent"})
    assert r.status_code == 403, (
        f"Expected 403 on 6th agent for unconfirmed user, got {r.status_code}: {r.text}"
    )
    detail = r.json().get("detail", "")
    assert "5" in detail, (
        f"Expected limit value '5' in error detail, got: {detail!r}"
    )
    # The message should mention confirming email to raise the limit
    assert "confirm" in detail.lower() or "email" in detail.lower(), (
        f"Expected email-confirmation nudge in error detail, got: {detail!r}"
    )


# ── Scenario 2: Confirmed developer is NOT blocked at the 5-agent cap ────────


def test_confirmed_developer_exceeds_5_without_403(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A confirmed developer can create more than 5 agents (tier is 50, not 5).

    1. Sign up, promote to developer.
    2. Confirm email via token.
    3. Create 6 agents — all must succeed (proves the 5-cap does not apply
       to confirmed users).

    Note: We do not brute-force the 50-agent boundary — only verify that the
    confirmed tier is higher than 5 by successfully creating a 6th agent.
    """
    # ── Phase 1: Create + promote a developer with AI credential ─────────────
    user, headers = _create_developer_user(client, superuser_token_headers)

    # ── Phase 2: Confirm the email ────────────────────────────────────────────
    _confirm_email(client, user["email"])

    # Verify confirmation is reflected on /users/me
    me = client.get(f"{_BASE}/users/me", headers=headers).json()
    assert me["email_confirmed"] is True, "Pre-condition: email must be confirmed"

    # ── Phase 3: Create 6 agents — all must succeed ───────────────────────────
    for i in range(6):
        r = client.post(
            _AGENTS_URL, headers=headers, json={"name": f"Confirmed Agent {i + 1}"}
        )
        assert r.status_code == 200, (
            f"Agent {i + 1} creation failed for confirmed user "
            f"(expected 200, got {r.status_code}): {r.text}"
        )


# ── Scenario 3: Superuser is never blocked at the unconfirmed boundary ────────


def test_superuser_not_blocked_at_unconfirmed_boundary(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A superuser can create well past 5 agents regardless of confirmation status.

    The superuser seeded by setup_db is already confirmed (backfill invariant),
    but the enforcement path skips the limit check entirely for superusers
    (``if user.is_superuser: return``). Creating 6 agents proves this bypass.

    Note: Each test transaction is rolled back, so these agents don't accumulate
    across tests.
    """
    # ── Create 6 agents as superuser — all must succeed ───────────────────────
    for i in range(6):
        r = client.post(
            _AGENTS_URL,
            headers=superuser_token_headers,
            json={"name": f"Superuser Agent {i + 1}"},
        )
        assert r.status_code == 200, (
            f"Superuser agent {i + 1} creation failed "
            f"(expected 200, got {r.status_code}): {r.text}"
        )


# ── Scenario 4: Account-CLI path enforces the same limit ─────────────────────


def test_account_cli_create_agent_enforces_unconfirmed_limit(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    The account-CLI account_create_agent path (POST /cli/account/agents)
    enforces the same 5-agent cap for unconfirmed users and returns 403.

    1. Sign up + promote to developer.
    2. Bootstrap an account CLI token (create setup token + exchange).
    3. Create 5 agents via the CLI endpoint — all succeed.
    4. Attempt a 6th — must return 403.

    This validates that the ValueError → 403 translation in the CLI route
    (``except ValueError as e: raise HTTPException(403, ...)``) is wired
    correctly, not just the main agents route.
    """
    # ── Phase 1: Create + promote an unconfirmed developer with AI credential ─
    user, user_headers = _create_developer_user(client, superuser_token_headers)

    # ── Phase 2: Bootstrap account CLI token ──────────────────────────────────
    account_jwt, _ = bootstrap_account_token(client, user_headers)
    acc_headers = account_cli_headers(account_jwt)

    # ── Phase 3: Create 5 agents via CLI endpoint — all must succeed ──────────
    cli_account_url = f"{_BASE}/cli/account/agents"
    for i in range(5):
        r = client.post(
            cli_account_url,
            headers=acc_headers,
            json={"name": f"CLI Agent {i + 1}"},
        )
        assert r.status_code == 200, (
            f"CLI agent {i + 1} creation failed "
            f"(expected 200, got {r.status_code}): {r.text}"
        )

    # ── Phase 4: 6th CLI agent must be rejected with 403 ─────────────────────
    r = client.post(
        cli_account_url,
        headers=acc_headers,
        json={"name": "CLI Over Limit Agent"},
    )
    assert r.status_code == 403, (
        f"Expected 403 on 6th CLI agent for unconfirmed user, "
        f"got {r.status_code}: {r.text}"
    )
    detail = r.json().get("detail", "")
    assert "5" in detail, (
        f"Expected limit value '5' in CLI error detail, got: {detail!r}"
    )
