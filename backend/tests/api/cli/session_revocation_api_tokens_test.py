"""Which session-ending events also revoke the user's API tokens.

User decision (plan ``security_hardening_slice1_plan.md`` §13.1) plus the
manager ruling on C1:
  * forgot-password reset, "Sign out everywhere" (``revoke-all``) and the C1
    Google wipe-and-link revoke CLI account tokens, per-agent CLI tokens,
    unexchanged CLI setup tokens, A2A access tokens and MCP direct tokens;
  * a password change, a first ``set-password`` and the admin password set
    (``PATCH /users/{id}``) leave every one of them working.

Lives in ``tests/api/cli/`` because it needs agents, and this directory's
conftest carries the environment/background stubs agent creation requires.
Web/desktop session effects of the same events are in
``tests/api/auth/session_revocation_test.py``.

Usability checks per token kind:
  * account CLI token → ``GET /cli/account/agents``;
  * per-agent CLI token → ``GET /cli/agents/{id}/building-context``;
  * CLI setup token → the exchange itself (single use, so only at the end);
  * A2A token → agent card (working) / JSON-RPC call 401 (revoked);
  * MCP direct token → the owner's token list ``revoked`` flag (the MCP
    verifier reads through its own engine session, outside the test
    transaction; the flag is what it checks).
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.a2a import (
    build_streaming_request,
    create_access_token,
    get_a2a_agent_card,
    post_a2a_raw,
)
from tests.utils.agent import create_agent_via_api, enable_a2a, get_agent
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.cli import (
    account_cli_headers,
    bootstrap_account_token,
    cli_auth_headers,
    create_setup_token,
    exchange_setup_token,
)
from tests.utils.fixtures import CREATE_SESSION_TARGETS_AGENT, patched_create_sessions
from tests.utils.google_oauth import google_oauth_patched, random_google_id
from tests.utils.user import promote_to_developer
from tests.utils.utils import random_email, random_lower_string
from tests.utils.web_session import (
    admin_set_password,
    bearer,
    bearer_login,
    change_password,
    me_status,
    reset_password,
    revoke_all,
    set_own_password,
)

API = settings.API_V1_STR


@pytest.fixture(autouse=True)
def patch_a2a_create_session(db):
    """The A2A route opens its own session; keep it in the test transaction
    (same extra target as ``tests/api/a2a_integration/conftest.py``)."""
    with patched_create_sessions(
        db, CREATE_SESSION_TARGETS_AGENT + ["app.api.routes.a2a.create_session"]
    ):
        yield


@dataclass
class ApiTokens:
    agent_id: str
    account_jwt: str
    cli_jwt: str
    setup_token: str
    a2a_token: str
    connector_id: str
    mcp_token_id: str


def _provision(client: TestClient, headers: dict[str, str]) -> ApiTokens:
    """Give the (developer) user one of every API token kind."""
    create_random_ai_credential(client, headers, set_default=True)
    agent = create_agent_via_api(client, headers)
    drain_tasks()
    agent = get_agent(client, headers, agent["id"])
    agent_id = agent["id"]

    account_jwt, _ = bootstrap_account_token(client, headers)
    setup = create_setup_token(client, headers, agent_id)
    cli_jwt = exchange_setup_token(client, setup["token"])["cli_token"]
    unused_setup = create_setup_token(client, headers, agent_id)["token"]

    enable_a2a(client, headers, agent_id)
    a2a_token = create_access_token(client, headers, agent_id)["token"]

    r = client.post(
        f"{API}/agents/{agent_id}/mcp-connectors",
        headers=headers,
        json={"name": "Direct", "mode": "conversation", "allow_token_access": True},
    )
    assert r.status_code == 200, r.text
    connector_id = r.json()["id"]
    r = client.post(
        f"{API}/agents/{agent_id}/mcp-connectors/{connector_id}/tokens",
        headers=headers,
        json={"label": "direct"},
    )
    assert r.status_code == 200, r.text
    return ApiTokens(
        agent_id=agent_id,
        account_jwt=account_jwt,
        cli_jwt=cli_jwt,
        setup_token=unused_setup,
        a2a_token=a2a_token,
        connector_id=connector_id,
        mcp_token_id=r.json()["id"],
    )


def _mcp_revoked(client: TestClient, headers: dict[str, str], t: ApiTokens) -> bool:
    r = client.get(
        f"{API}/agents/{t.agent_id}/mcp-connectors/{t.connector_id}/tokens",
        headers=headers,
    )
    assert r.status_code == 200, r.text
    row = next(x for x in r.json()["data"] if x["id"] == t.mcp_token_id)
    return row["revoked"]


def _account_status(client: TestClient, t: ApiTokens) -> int:
    return client.get(
        f"{API}/cli/account/agents", headers=account_cli_headers(t.account_jwt)
    ).status_code


def _agent_cli_status(client: TestClient, t: ApiTokens) -> int:
    return client.get(
        f"{API}/cli/agents/{t.agent_id}/building-context",
        headers=cli_auth_headers(t.cli_jwt),
    ).status_code


def _assert_working(
    client: TestClient, headers: dict[str, str], t: ApiTokens, *, label: str
) -> None:
    """Every token still works (the setup token is checked separately)."""
    assert _account_status(client, t) == 200, label
    assert _agent_cli_status(client, t) == 200, label
    get_a2a_agent_card(client, t.agent_id, t.a2a_token)
    assert _mcp_revoked(client, headers, t) is False, label


def _assert_revoked(client: TestClient, headers: dict[str, str], t: ApiTokens) -> None:
    assert _account_status(client, t) in (401, 403)
    assert _agent_cli_status(client, t) == 401
    r = post_a2a_raw(
        client, t.agent_id, build_streaming_request("after revoke"), a2a_token=t.a2a_token
    )
    assert r.status_code == 401, r.text
    assert _mcp_revoked(client, headers, t) is True
    r = client.post(f"/api/cli-setup/{t.setup_token}", json={"machine_name": "late"})
    assert r.status_code != 200, r.text
    assert r.status_code in (400, 401, 403, 404, 410), r.text


def _developer(
    client: TestClient, superuser_headers: dict[str, str]
) -> tuple[dict, str, dict[str, str]]:
    email, pw = random_email(), random_lower_string()
    r = client.post(f"{API}/users/signup", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    user = r.json()
    promote_to_developer(client, superuser_headers, user["id"])
    return user, pw, bearer_login(client, email, pw)


# ── Revoking events ──────────────────────────────────────────────────────


def test_forgot_password_reset_revokes_every_api_token(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
      1. Developer with every API token kind; all work.
      2. Forgot-password reset.
      3. Every token is unusable, the setup token cannot be exchanged.
    """
    user, _pw, headers = _developer(client, superuser_token_headers)
    tokens = _provision(client, headers)
    _assert_working(client, headers, tokens, label="before reset")

    new_pw = random_lower_string()
    reset_password(client, user["email"], new_pw)

    _assert_revoked(client, bearer_login(client, user["email"], new_pw), tokens)


def test_sign_out_everywhere_revokes_every_api_token(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
      1. Developer with every API token kind.
      2. ``POST /login/session/revoke-all``.
      3. The caller's own token is dead and every API token is unusable.
    """
    user, pw, headers = _developer(client, superuser_token_headers)
    tokens = _provision(client, headers)

    revoke_all(client, headers)

    assert me_status(client, headers) == 401
    _assert_revoked(client, bearer_login(client, user["email"], pw), tokens)


def test_c1_google_wipe_revokes_every_api_token(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
      1. An unconfirmed password account (the pre-registrant) holds every API
         token kind.
      2. The address owner signs in with Google (wipe-and-link).
      3. Every token is unusable.
    """
    user, _pw, headers = _developer(client, superuser_token_headers)
    tokens = _provision(client, headers)

    with google_oauth_patched(email=user["email"], google_id=random_google_id()):
        r = client.post(f"{API}/auth/google/callback", json={"code": "c", "state": "s"})
    assert r.status_code == 200, r.text
    victim = bearer(r.json()["access_token"])

    _assert_revoked(client, victim, tokens)


# ── Non-revoking events ──────────────────────────────────────────────────


def test_password_change_first_set_and_admin_set_leave_api_tokens_working(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
      1. A Google-only developer provisions every API token kind.
      2. First ``set-password`` → all tokens work.
      3. ``PATCH /users/me/password`` → all tokens work.
      4. Admin ``PATCH /users/{id}`` password → all tokens work, and the
         unexchanged setup token can still be exchanged.
    """
    # ── Phase 1: Google-only developer ─────────────────────────────────
    email = random_email()
    with google_oauth_patched(email=email, google_id=random_google_id()):
        r = client.post(f"{API}/auth/google/callback", json={"code": "c", "state": "s"})
    assert r.status_code == 200, r.text
    headers = bearer(r.json()["access_token"])
    me = client.get(f"{API}/users/me", headers=headers).json()
    assert me["has_password"] is False
    promote_to_developer(client, superuser_token_headers, me["id"])
    tokens = _provision(client, headers)

    # ── Phase 2: first password ────────────────────────────────────────
    pw1 = random_lower_string()
    set_own_password(client, headers, pw1)
    headers = bearer_login(client, email, pw1)
    _assert_working(client, headers, tokens, label="after set-password")

    # ── Phase 3: password change ───────────────────────────────────────
    pw2 = random_lower_string()
    change_password(client, headers, pw1, pw2)
    headers = bearer_login(client, email, pw2)
    _assert_working(client, headers, tokens, label="after change")

    # ── Phase 4: admin set ─────────────────────────────────────────────
    pw3 = random_lower_string()
    admin_set_password(client, superuser_token_headers, me["id"], pw3)
    assert me_status(client, headers) == 401  # the session did end
    headers = bearer_login(client, email, pw3)
    _assert_working(client, headers, tokens, label="after admin set")
    exchanged = exchange_setup_token(client, tokens.setup_token)
    assert exchanged["cli_token"]
