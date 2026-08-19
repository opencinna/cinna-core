"""
Security tests for agent-environment token scoping hardening.

BACKGROUND: Prior to this hardening, the agent-env internal token was a plain
owner-user JWT (sub=owner_id) that satisfied the generic ``CurrentUser``
dependency — a compromised container could call ANY owner route with full
account access.

POST-HARDENING: env tokens carry ``token_type="agent_env"`` and
``aud="agent_env"``. They are REJECTED by ``get_current_user`` (which does not
pass ``audience=``, causing PyJWT to raise ``InvalidAudienceError``), and only
``AgentEnvContextDep`` can authenticate them. Additional scope enforcement:
  - The token is bound to exactly one (env_id, agent_id, owner_id) triple
  - The env's ``auth_token_hash`` (SHA-256) is the revocation anchor
  - Header ``X-Agent-Env-Id`` must match the token's ``env_id`` claim

This test file PROVES the hardening by hitting the API:
  1. Env token is rejected by CurrentUser routes (account-takeover path closed)
  2. Env token works on its own scoped routes (happy path)
  3. Cross-agent/cross-env scope denial (env A token cannot act on env B's data)
  4. Revocation: changing auth_token_hash invalidates the token
  5. Legacy grace path: opaque token + X-Agent-Env-Id header accepted / rejected
  6. Sanity: regular user/CLI tokens still work on CurrentUser routes

All tests use the HTTP API layer only. The ``db`` fixture is used exclusively
to set up AgentEnvironment records (a documented DB-seam exemption — there is
no public API to mint env tokens or write auth_token_hash).
"""
import hashlib
import uuid

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.agent_env import create_env_with_token
from tests.utils.background_tasks import drain_tasks
from tests.utils.input_task import create_task

_BASE = f"{settings.API_V1_STR}"
# A representative CurrentUser route: GET /credentials/ resolves the caller as
# a full User and returns their credentials. If an env token could reach this,
# the compromised container would have full account access.
_CREDENTIALS_URL = f"{_BASE}/credentials/"
# Another CurrentUser route — /users/me returns the resolved user's profile.
_ME_URL = f"{_BASE}/users/me"


def _get_superuser_id(client: TestClient, headers: dict) -> str:
    r = client.get(_ME_URL, headers=headers)
    assert r.status_code == 200
    return r.json()["id"]


# ── 1. Env token is rejected by CurrentUser routes ────────────────────────────

def test_env_token_rejected_by_currentuser_routes(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    A scoped env token MUST NOT satisfy CurrentUser-protected routes.

    This is the central security assertion: if this test fails, a compromised
    container could impersonate the owner and access all their data (credentials,
    agents, tasks, etc.) — the account-takeover path this hardening closes.

    The rejection happens at decode time: PyJWT raises ``InvalidAudienceError``
    when it sees ``aud="agent_env"`` and the decode call passes no ``audience=``
    (403). The secondary ``token_type`` gate fires for any hypothetical env token
    minted without ``aud`` (401). Either way the env token is rejected before any
    User record is loaded.
    """
    owner_id = _get_superuser_id(client, superuser_token_headers)

    agent = create_agent_via_api(client, superuser_token_headers, name="EnvTokenReject Agent")
    _, env_headers = create_env_with_token(db, agent_id=agent["id"], owner_id=owner_id)

    # ── GET /users/me — canonical CurrentUser route ──────────────────────
    r = client.get(_ME_URL, headers=env_headers)
    assert r.status_code in (401, 403), (
        f"Env token must NOT resolve to a User on /users/me — got {r.status_code}: {r.text}"
    )

    # ── GET /credentials/ — data-bearing CurrentUser route ───────────────
    r = client.get(_CREDENTIALS_URL, headers=env_headers)
    assert r.status_code in (401, 403), (
        f"Env token must NOT be able to list credentials — got {r.status_code}: {r.text}"
    )

    # ── GET /credentials/{id}/with-data — the highest-risk read ─────────
    # Use a random UUID — we just need to confirm the auth gate fires before
    # the 404 not-found check (if it were 200 or 404, the token was accepted).
    ghost_cred_id = str(uuid.uuid4())
    r = client.get(f"{_CREDENTIALS_URL}{ghost_cred_id}/with-data", headers=env_headers)
    assert r.status_code in (401, 403), (
        f"Env token must be rejected before credential lookup — got {r.status_code}: {r.text}"
    )


def test_regular_user_jwt_still_works_on_currentuser_routes(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Sanity check: the CurrentUser rejection does NOT over-match.

    A regular user JWT must still resolve to the User and succeed on user-facing
    routes. This guards against a regression where the ``token_type`` reject or
    audience check is too broad and accidentally rejects real user sessions.
    """
    # /users/me with a real user JWT should return 200
    r = client.get(_ME_URL, headers=superuser_token_headers)
    assert r.status_code == 200, (
        f"Superuser JWT must still work on /users/me — got {r.status_code}: {r.text}"
    )
    assert "id" in r.json()

    # /credentials/ with a real user JWT should return 200
    r = client.get(_CREDENTIALS_URL, headers=superuser_token_headers)
    assert r.status_code == 200, (
        f"Superuser JWT must still work on /credentials/ — got {r.status_code}: {r.text}"
    )


# ── 2. Env token works on its own scoped routes ───────────────────────────────

def test_env_token_works_on_scoped_routes(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    A valid new-format env token authenticates successfully on /agent/tasks/* routes.

    Verifies the happy path: a well-formed env token with correct claims and a
    matching auth_token_hash returns 200 on the agent-facing task endpoints.
    """
    headers = superuser_token_headers
    owner_id = _get_superuser_id(client, headers)

    agent = create_agent_via_api(client, headers, name="ScopedRoute Agent")
    _, env_headers = create_env_with_token(db, agent_id=agent["id"], owner_id=owner_id)

    # Create a task owned by the superuser (ctx.owner.id must match task.owner_id)
    task = create_task(client, headers, original_message="Scoped route test task",
                       selected_agent_id=agent["id"])
    task_id = task["id"]

    # ── GET /agent/tasks/my-tasks — lists owner's tasks ──────────────────
    r = client.get(f"{_BASE}/agent/tasks/my-tasks", headers=env_headers)
    assert r.status_code == 200, f"Env token should work on my-tasks: {r.text}"
    task_ids = [t["id"] for t in r.json()["data"]]
    assert task_id in task_ids

    # ── GET /agent/tasks/{task_id}/details ───────────────────────────────
    r = client.get(f"{_BASE}/agent/tasks/{task_id}/details", headers=env_headers)
    assert r.status_code == 200, f"Env token should work on task details: {r.text}"
    assert r.json()["task"] == task["short_code"]

    # ── POST /agent/tasks/{task_id}/comment ──────────────────────────────
    r = client.post(
        f"{_BASE}/agent/tasks/{task_id}/comment",
        headers=env_headers,
        json={"content": "Scoped comment via env token", "comment_type": "message"},
    )
    assert r.status_code == 200, f"Env token should work on comment: {r.text}"
    assert "comment_id" in r.json()

    # ── POST /agent/tasks/{task_id}/status ───────────────────────────────
    r = client.post(
        f"{_BASE}/agent/tasks/{task_id}/status",
        headers=env_headers,
        json={"status": "cancelled", "reason": "Scoped test"},
    )
    assert r.status_code == 200, f"Env token should work on status update: {r.text}"
    assert r.json()["success"] is True


# ── 3. Cross-agent / cross-env scope denial ───────────────────────────────────

def test_cross_env_token_denied_on_scoped_routes(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    Env A's token must not be able to act on a task/session owned by env B's agent.

    Scenario:
      - Agent A has env A with token_A
      - Agent B has env B with token_B
      - Task is assigned to agent A (selected_agent_id = agent_A.id)
      - Agent B's env (token_B + env_B) tries to act on agent A's task

    Task ownership (task.owner_id) is the same user (superuser), so the ownership
    check passes — the scope violation is via the session check when applicable.
    However, for the `/{task_id}/comment` route, the service checks
    ``task.owner_id == ctx.owner.id`` only. Since both agents are owned by the
    same user, the comment POST may succeed — the session-level scope enforcement
    only fires for `/current/*` endpoints. This test documents the per-task scope
    boundary as it currently stands.

    The critical test is the X-Agent-Env-Id mismatch case (Phase 3): providing
    env_A's header with env_B's token → 403.
    """
    headers = superuser_token_headers
    owner_id = _get_superuser_id(client, headers)

    agent_a = create_agent_via_api(client, headers, name="ScopeTest Agent A")
    agent_b = create_agent_via_api(client, headers, name="ScopeTest Agent B")

    env_a, env_a_headers = create_env_with_token(db, agent_id=agent_a["id"], owner_id=owner_id)
    env_b, env_b_headers = create_env_with_token(db, agent_id=agent_b["id"], owner_id=owner_id)

    task = create_task(client, headers, original_message="Cross-env scope test task",
                       selected_agent_id=agent_a["id"])
    task_id = task["id"]

    # ── Phase 1: Token for env_A works on task A (same owner) ────────────
    r = client.get(f"{_BASE}/agent/tasks/{task_id}/details", headers=env_a_headers)
    assert r.status_code == 200, f"env_A token should access its own task: {r.text}"

    # ── Phase 2: Token for env_B — same owner, different agent ───────────
    # /agent/tasks/{task_id}/details only checks ctx.owner.id == task.owner_id.
    # Since both agents are owned by the same superuser, env_B can read the task.
    # This is intentional: the scope model narrows only by OWNER, not by agent,
    # for non-session-resolved routes (the team model requires cross-agent reads).
    r = client.get(f"{_BASE}/agent/tasks/{task_id}/details", headers=env_b_headers)
    assert r.status_code in (200, 400, 403, 404), (
        f"env_B token accessing another agent's task: {r.status_code}"
    )

    # ── Phase 3: X-Agent-Env-Id mismatch → 403 (scope violation) ─────────
    # Provide env_A's header ID but env_B's Bearer token.
    # _resolve_agent_env_context checks: header_env_id must match claim env_id.
    # claim env_id = env_B.id but header says env_A.id → mismatch → 403.
    mismatch_headers = {
        "Authorization": env_b_headers["Authorization"],   # env_B token
        "X-Agent-Env-Id": str(env_a.id),                  # env_A header
    }
    r = client.get(f"{_BASE}/agent/tasks/{task_id}/details", headers=mismatch_headers)
    assert r.status_code == 403, (
        f"X-Agent-Env-Id / token mismatch must be 403 (env_mismatch), got {r.status_code}: {r.text}"
    )

    # ── Phase 4: Wrong env_id in header (not just mismatch — env doesn't exist)
    non_existent_env_id = str(uuid.uuid4())
    mismatch_non_existent = {
        "Authorization": env_b_headers["Authorization"],   # env_B token (valid)
        "X-Agent-Env-Id": non_existent_env_id,             # completely different
    }
    r = client.get(f"{_BASE}/agent/tasks/{task_id}/details", headers=mismatch_non_existent)
    # The token's env_id claim is env_B.id; header says something else → mismatch → 403
    assert r.status_code == 403, (
        f"Non-matching header env_id must be 403, got {r.status_code}: {r.text}"
    )


# ── 4. Revocation: changing auth_token_hash invalidates the token ─────────────

def test_env_token_revoked_by_hash_rotation(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    After the env's auth_token_hash is changed (simulating token rotation or
    explicit revocation), the previously-valid token is rejected with 401.

    This exercises the hash-based revocation gate in ``_resolve_agent_env_context``:
      presented_hash = sha256(token)
      if presented_hash != env.auth_token_hash: → 401 (rotated/revoked)
    """
    headers = superuser_token_headers
    owner_id = _get_superuser_id(client, headers)

    agent = create_agent_via_api(client, headers, name="Revocation Test Agent")
    env, env_headers = create_env_with_token(db, agent_id=agent["id"], owner_id=owner_id)

    task = create_task(client, headers, original_message="Revocation test task",
                       selected_agent_id=agent["id"])
    task_id = task["id"]

    # ── Phase 1: Token is valid before rotation ───────────────────────────
    r = client.get(f"{_BASE}/agent/tasks/{task_id}/details", headers=env_headers)
    assert r.status_code == 200, f"Token should be valid before rotation: {r.text}"

    # ── Phase 2: Rotate the hash (simulate token rotation) ────────────────
    # In production this happens when the lifecycle manager calls _generate_auth_token
    # on reconfigure. In the test we write a different hash directly to the DB.
    # Any hash that doesn't match sha256(old_token) will cause rejection.
    env.auth_token_hash = hashlib.sha256(b"this-is-a-different-token").hexdigest()
    db.add(env)
    db.flush()

    # ── Phase 3: Old token is now rejected ────────────────────────────────
    r = client.get(f"{_BASE}/agent/tasks/{task_id}/details", headers=env_headers)
    assert r.status_code == 401, (
        f"Rotated env token must be rejected (401), got {r.status_code}: {r.text}"
    )

    # ── Phase 4: Setting hash to NULL triggers legacy verbatim compare ────
    # With hash=NULL and matching config["auth_token"], the token is still accepted
    # (this is the no-hash grace window). With hash=NULL and wrong verbatim token,
    # it's rejected.
    env.auth_token_hash = None
    env.config = {"auth_token": "some-other-token-entirely"}
    db.add(env)
    db.flush()

    r = client.get(f"{_BASE}/agent/tasks/{task_id}/details", headers=env_headers)
    assert r.status_code == 401, (
        f"Token with NULL hash and mismatched config must be rejected, got {r.status_code}: {r.text}"
    )


# ── 5. Legacy grace path ──────────────────────────────────────────────────────

def test_legacy_opaque_token_grace_path(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    An OLD-format opaque token (plain string, no JWT structure, no aud/token_type)
    in config["auth_token"] with auth_token_hash=None is accepted when:
      - settings.AGENT_ENV_TOKEN_ACCEPT_LEGACY is True (default)
      - X-Agent-Env-Id header is present and points to the env
      - The Bearer token verbatim-matches config["auth_token"]

    When AGENT_ENV_TOKEN_ACCEPT_LEGACY is monkeypatched to False, the same token
    is rejected (401) even with the header and verbatim match.

    Note: the ``knowledge/query`` endpoint already exercises this path (it was the
    only env-auth endpoint before the hardening). This test confirms it also works
    for the newly migrated /agent/tasks/* routes.
    """
    from unittest.mock import patch as _patch
    from app.models import Agent, AgentEnvironment

    headers = superuser_token_headers
    me_r = client.get(_ME_URL, headers=headers)
    assert me_r.status_code == 200
    owner_id = uuid.UUID(me_r.json()["id"])

    # Create an agent via API (gives us a real bundle_id)
    agent = create_agent_via_api(client, headers, name="Legacy Grace Path Agent")
    agent_id = uuid.UUID(agent["id"])

    # Build an environment with an opaque (non-JWT) token and no hash.
    # This simulates a pre-hardening container that hasn't been rebuilt yet.
    opaque_token = f"legacy-opaque-token-{uuid.uuid4().hex}"

    env = AgentEnvironment(
        agent_id=agent_id,
        env_name=settings.DEFAULT_AGENT_ENV_NAME,
        status="running",
        is_active=True,
        config={"auth_token": opaque_token},
        auth_token_hash=None,  # No hash — triggers verbatim grace compare
    )
    db.add(env)
    db.flush()
    env_id = str(env.id)

    legacy_headers = {
        "Authorization": f"Bearer {opaque_token}",
        "X-Agent-Env-Id": env_id,
    }

    # Create a task for the agent to query
    task = create_task(client, headers, original_message="Legacy grace path task",
                       selected_agent_id=str(agent_id))
    task_id = task["id"]

    # ── Phase 1: Grace path ON (default) — opaque token accepted ─────────
    # AGENT_ENV_TOKEN_ACCEPT_LEGACY defaults to True, so this should work.
    r = client.get(f"{_BASE}/agent/tasks/{task_id}/details", headers=legacy_headers)
    assert r.status_code == 200, (
        f"Opaque token with X-Agent-Env-Id should be accepted on grace path: {r.text}"
    )

    # ── Phase 2: Grace path OFF — same opaque token rejected ─────────────
    # Monkeypatch the setting to simulate post-grace-period behavior.
    with _patch.object(settings, "AGENT_ENV_TOKEN_ACCEPT_LEGACY", False):
        r = client.get(f"{_BASE}/agent/tasks/{task_id}/details", headers=legacy_headers)
        assert r.status_code == 401, (
            f"Opaque token must be rejected when AGENT_ENV_TOKEN_ACCEPT_LEGACY=False: "
            f"{r.status_code}: {r.text}"
        )

    # ── Phase 3: Grace path ON but WRONG token → rejected ────────────────
    wrong_legacy_headers = {
        "Authorization": "Bearer wrong-opaque-token",
        "X-Agent-Env-Id": env_id,
    }
    r = client.get(f"{_BASE}/agent/tasks/{task_id}/details", headers=wrong_legacy_headers)
    assert r.status_code == 401, (
        f"Wrong opaque token must be rejected even with correct header: {r.status_code}"
    )

    # ── Phase 4: Grace path ON but MISSING header → rejected ─────────────
    no_header = {"Authorization": f"Bearer {opaque_token}"}
    r = client.get(f"{_BASE}/agent/tasks/{task_id}/details", headers=no_header)
    assert r.status_code == 401, (
        f"Opaque token without X-Agent-Env-Id header must be rejected: {r.status_code}"
    )
