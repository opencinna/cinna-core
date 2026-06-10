"""
Backend tests for the Agent Environment Console feature (web terminal + logs follow).

Covers the security boundary layer that fires *before* any env-core connection is
attempted — all assertions are purely at the auth/gate/status/cap layer so no live
Docker environment is required.

Scenarios:
  1. WS auth boundary — missing token, invalid token, non-owner user, wrong-env
     non-owner → socket closed with 1008 (raises WebSocketDisconnect / Exception).
  2. Token-type rejection — scoped guest_share / webapp_share tokens → 1008.
  3. Terminal role gate — agent-user owner rejected (1008); superuser/developer owner
     reaches the service layer (not 1008 at the dep level).
  4. Status guard — terminal and logs rejected when env is not running (close 4404).
  5. Open-rate cap (unit-style, direct tracker) — sliding-window enforces the limit
     and raises ConsoleRateLimitError on excess; resets cleanly between tests.
  6. Concurrency cap (unit-style, direct tracker) — tracker counts per-env and
     per-user; register/unregister are symmetric; is_console_warm reflects live state.
  7. Suspension-scheduler gate — scheduler skips envs where is_console_warm() is True.

Notes on the WS/Docker boundary:
  - FastAPI's TestClient WebSocket support is used for all WS tests.
  - Auth/role/status checks fire in the WS dependency *before* the service opens a
    connection to env-core. When a dep rejects the socket it raises
    WebSocketDisconnect (which propagates through TestClient as an exception).
  - Tests that need to reach *past* the dep to test the service-layer status guard
    (4404) patch ``EnvironmentConsoleService.run_terminal_tunnel`` /
    ``follow_logs`` so no real env-core socket is needed. The close code 4404
    is emitted by the service before accepting the browser WS, so it is
    observable as a disconnect.
  - Tracker / cap unit tests import the module-level singleton directly
    (``env_console_activity_tracker``) and call ``reset()`` for isolation;
    this mirrors the pattern used by ``_check_and_suspend_environments`` tests
    in ``test_cli_live_sync.py``.
"""

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session
from starlette.websockets import WebSocketDisconnect

from app.core.config import settings
from app.models.environments.environment import AgentEnvironment
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.environment import list_environments
from tests.utils.platform_token import mint_platform_token
from tests.utils.user import (
    create_random_user,
    create_random_user_with_headers,
    promote_to_developer,
    user_authentication_headers,
)

_API = settings.API_V1_STR
# Console WebSocket routes live under a dedicated /env-console prefix (separate
# from the REST /environments router) so the reverse proxy can scope WS-upgrade
# to one prefix block. See docs/infrastructure/nginx_setup.md.
_ENV_BASE = f"{_API}/env-console"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _bootstrap_running_env(
    client: TestClient,
    headers: dict[str, str],
    db: Session,
) -> tuple[str, str]:
    """Create an agent with a running environment.

    Returns ``(agent_id, env_id)``.
    """
    agent = create_agent_via_api(client, headers)
    drain_tasks()
    agent = get_agent(client, headers, agent["id"])
    agent_id = agent["id"]
    env_id = agent["active_environment_id"]
    assert env_id is not None, "Agent must have an active environment"

    # Set status to running (mirrors the pattern in test_cli_live_sync.py)
    env = db.get(AgentEnvironment, env_id)
    env.status = "running"
    db.add(env)
    db.flush()

    return agent_id, env_id


def _set_env_status(db: Session, env_id: str, status: str) -> None:
    """Update environment status directly in the test transaction."""
    env = db.get(AgentEnvironment, env_id)
    env.status = status
    db.add(env)
    db.flush()


def _platform_token_for_user(user_id: str, extra_claims: dict | None = None) -> str:
    """Build a valid signed platform JWT for the given user id."""
    return mint_platform_token(user_id, extra_claims=extra_claims)


def _scoped_token(token_type: str, role: str, subject: str | None = None) -> str:
    """Build a JWT that carries a scoped token_type / role (guest or webapp viewer)."""
    sub = subject or str(uuid.uuid4())
    return mint_platform_token(
        sub, extra_claims={"token_type": token_type, "role": role}
    )


def _assert_ws_close_code(
    client: TestClient, ws_url: str, expected_code: int
) -> None:
    """Connect to a WS that the server rejects/closes, asserting the close code.

    The server closes the socket (before or after accept); TestClient surfaces
    this as ``WebSocketDisconnect`` carrying the server's close ``code``.
    """
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(ws_url) as ws:
            ws.receive_text()
    assert exc.value.code == expected_code, (
        f"Expected WS close code {expected_code} for {ws_url}, "
        f"got {exc.value.code}"
    )


# Close codes (see EnvironmentConsoleService): 1008 auth/dep rejection,
# 4404 env-not-running status guard, 1011 dep+status passed but the service
# could not reach env-core (i.e. the security boundary yielded successfully).
_WS_CLOSE_AUTH = 1008
_WS_CLOSE_ENV_NOT_RUNNING = 4404
_WS_CLOSE_ENV_UNREACHABLE = 1011


# ── Scenario 1: WS auth boundary ─────────────────────────────────────────────


def test_console_ws_auth_rejects(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    WS auth boundary for both terminal and logs/stream endpoints:
      1. Bootstrap agent + running env
      2. No token → 1008 close (WebSocketDisconnect)
      3. Garbage string (not a JWT) → 1008 close
      4. Expired JWT → 1008 close
      5. Non-owner user (valid JWT, different user) → 1008 close
      6. Non-existent env id → 1008 close
    """
    # ── Phase 1: Bootstrap ────────────────────────────────────────────────
    agent_id, env_id = _bootstrap_running_env(
        client, superuser_token_headers, db
    )

    terminal_url = f"{_ENV_BASE}/{env_id}/terminal"
    logs_url = f"{_ENV_BASE}/{env_id}/logs/stream"

    for ws_url in (terminal_url, logs_url):
        # ── Phase 2: No token → 1008 ────────────────────────────────────
        _assert_ws_close_code(client, ws_url, _WS_CLOSE_AUTH)

        # ── Phase 3: Garbage string → 1008 ─────────────────────────────
        _assert_ws_close_code(
            client, f"{ws_url}?token=not-a-valid-jwt-at-all", _WS_CLOSE_AUTH
        )

        # ── Phase 4: Expired JWT → 1008 ────────────────────────────────
        superuser_id = _get_superuser_id(client, superuser_token_headers)
        expired_token = mint_platform_token(
            superuser_id,
            expires_delta=timedelta(seconds=-1),  # already expired
        )
        _assert_ws_close_code(
            client, f"{ws_url}?token={expired_token}", _WS_CLOSE_AUTH
        )

        # ── Phase 5: Non-owner (other user, valid token) → 1008 ─────────
        other_user, other_headers = create_random_user_with_headers(client)
        promote_to_developer(client, superuser_token_headers, other_user["id"])
        other_token = _platform_token_for_user(other_user["id"])
        _assert_ws_close_code(
            client, f"{ws_url}?token={other_token}", _WS_CLOSE_AUTH
        )

        # ── Phase 6: Non-existent env id → 1008 (no existence leak) ─────
        ghost_id = str(uuid.uuid4())
        ghost_url = ws_url.replace(str(env_id), ghost_id)
        _assert_ws_close_code(
            client,
            f"{ghost_url}?token={_get_superuser_raw_token(client)}",
            _WS_CLOSE_AUTH,
        )


def _get_superuser_id(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> str:
    """Return the superuser's user id."""
    r = client.get(f"{_API}/users/me", headers=superuser_token_headers)
    assert r.status_code == 200
    return r.json()["id"]


def _get_superuser_raw_token(client: TestClient) -> str:
    """Obtain a raw JWT for the superuser (used in ?token= query param)."""
    login_data = {
        "username": settings.FIRST_SUPERUSER,
        "password": settings.FIRST_SUPERUSER_PASSWORD,
    }
    r = client.post(f"{_API}/login/access-token", data=login_data)
    assert r.status_code == 200
    return r.json()["access_token"]


# ── Scenario 2: Token-type rejection ─────────────────────────────────────────


def test_scoped_token_types_rejected(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    Scoped guest_share and webapp_share token types must be rejected (1008)
    before ownership checks even run — defense-in-depth to prevent narrow-scope
    tokens being escalated to a full shell or log stream.

      1. guest_share token → both terminal and logs/stream reject
      2. webapp_share token → both terminal and logs/stream reject
    """
    agent_id, env_id = _bootstrap_running_env(
        client, superuser_token_headers, db
    )

    terminal_url = f"{_ENV_BASE}/{env_id}/terminal"
    logs_url = f"{_ENV_BASE}/{env_id}/logs/stream"

    # guest_share token — sub is a share id, not a user id
    guest_token = _scoped_token(
        token_type="guest_share",
        role="chat-guest",
        subject=str(uuid.uuid4()),
    )
    # webapp_share token
    webapp_token = _scoped_token(
        token_type="webapp_share",
        role="webapp-viewer",
        subject=str(uuid.uuid4()),
    )

    for bad_token, token_label in (
        (guest_token, "guest_share"),
        (webapp_token, "webapp_share"),
    ):
        for ws_url, ep_label in ((terminal_url, "terminal"), (logs_url, "logs")):
            _assert_ws_close_code(
                client, f"{ws_url}?token={bad_token}", _WS_CLOSE_AUTH
            )


# ── Scenario 3: Terminal role gate ───────────────────────────────────────────


def test_terminal_role_gate(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    Terminal WS requires owner + agent-developer (or superuser) role.
    Logs WS only requires ownership (any role).

      1. Create agent owned by a freshly-signed-up user (role defaults to agent-user).
         The user is NOT promoted to developer.
      2. Terminal → reject 1008 (agent-user cannot open terminal).
      3. Promote the user to developer.
      4. Terminal → now passes the dep (reaches service layer; service closes 4404
         because the env status in the dep is running but the service also checks —
         here we just verify the dep no longer throws).
      5. Logs → agent-user owner accepted (no developer gate on logs).
    """
    # ── Phase 1: Create an agent-user owner ──────────────────────────────
    user, user_headers = create_random_user_with_headers(client)
    user_id = user["id"]
    # Promote just to developer so they can create an agent (RBAC gate), then
    # we'll demote back for the terminal-gate test.
    promote_to_developer(client, superuser_token_headers, user_id)

    # The new user needs their own AI credentials to create an agent.
    create_random_ai_credential(
        client, user_headers,
        credential_type="anthropic",
        api_key="sk-ant-api03-test-role-gate-key",
        set_default=True,
    )

    agent = create_agent_via_api(client, user_headers)
    drain_tasks()
    agent = get_agent(client, user_headers, agent["id"])
    env_id = agent["active_environment_id"]
    assert env_id is not None

    # Mark env running
    env = db.get(AgentEnvironment, env_id)
    env.status = "running"
    db.add(env)
    db.flush()

    # Demote back to agent-user (no longer a developer)
    r = client.patch(
        f"{_API}/users/{user_id}/role",
        headers=superuser_token_headers,
        json={"role": "agent-user"},
    )
    assert r.status_code == 200, f"Failed to demote user: {r.text}"

    terminal_url = f"{_ENV_BASE}/{env_id}/terminal"
    logs_url = f"{_ENV_BASE}/{env_id}/logs/stream"

    # ── Phase 2: Terminal rejects agent-user owner (1008 at dep layer) ───
    user_raw_token = _get_user_raw_token(client, user["email"], user["_password"])
    _assert_ws_close_code(
        client, f"{terminal_url}?token={user_raw_token}", _WS_CLOSE_AUTH
    )

    # ── Phase 3: Promote to developer ────────────────────────────────────
    promote_to_developer(client, superuser_token_headers, user_id)
    # Refresh token after role change so the new role is encoded
    user_raw_token = _get_user_raw_token(client, user["email"], user["_password"])

    # ── Phase 4: Terminal passes the dep now (developer owner) ───────────
    # The dep yields; the service accepts the socket then tries to open a shell
    # WS to env-core. We patch that to raise RuntimeError so the service closes
    # with 1011 (env_unreachable). Observing 1011 (NOT 1008/4404) proves the
    # security boundary passed and the service ran past the status guard.
    with patch(
        "app.services.environments.agent_env_connector.agent_env_connector.open_shell_websocket",
        new=AsyncMock(side_effect=RuntimeError("no env-core in test")),
    ):
        _assert_ws_close_code(
            client,
            f"{terminal_url}?token={user_raw_token}",
            _WS_CLOSE_ENV_UNREACHABLE,
        )

    # ── Phase 5: Demote again; logs still accept an agent-user owner ─────
    r = client.patch(
        f"{_API}/users/{user_id}/role",
        headers=superuser_token_headers,
        json={"role": "agent-user"},
    )
    assert r.status_code == 200
    user_raw_token = _get_user_raw_token(client, user["email"], user["_password"])

    # Logs follow: the dep passes (no developer gate on logs). We patch the
    # service's follow_logs to accept then close — observing that it was CALLED
    # (and the WS was accepted → close code 1000) proves the dep yielded for an
    # agent-user owner. A 1008 here would mean the dep wrongly rejected.
    follow_logs_called: list[bool] = []

    async def _accept_and_close(*args, **kwargs):
        follow_logs_called.append(True)
        # Route calls follow_logs(websocket=ws, environment=..., user=..., ...)
        ws = kwargs.get("websocket")
        if ws is None:
            for a in args:
                if hasattr(a, "accept") and callable(a.accept):
                    ws = a
                    break
        assert ws is not None, "follow_logs must receive the websocket"
        await ws.accept()
        await ws.close(code=1000)

    with patch(
        "app.services.environments.environment_console_service.EnvironmentConsoleService.follow_logs",
        new=_accept_and_close,
    ):
        _assert_ws_close_code(
            client, f"{logs_url}?token={user_raw_token}", 1000
        )

    assert follow_logs_called, (
        "follow_logs was never reached — the dep rejected an agent-user "
        "owner on the logs endpoint (logs has no developer gate)"
    )


def _get_user_raw_token(client: TestClient, email: str, password: str) -> str:
    """Obtain a raw JWT for the given user credentials."""
    r = client.post(
        f"{_API}/login/access-token",
        data={"username": email, "password": password},
    )
    assert r.status_code == 200, f"Login failed: {r.text}"
    return r.json()["access_token"]


# ── Scenario 4: Status guard ─────────────────────────────────────────────────


def test_console_status_guard_not_running(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    Terminal and logs WS must be rejected (close 4404) when the environment is
    not in the ``running`` state. The rejection happens in
    ``EnvironmentConsoleService`` *after* the dep passes (because the status check
    sits in the service, not the dep).

    Approach: force env status to ``stopped`` then attempt to connect to each WS.
    The service closes with code 4404 *before* calling accept(), so the browser
    side receives a disconnect.

      1. Bootstrap agent with running env; get a superuser token.
      2. Force env status → stopped.
      3. Terminal → close code 4404 (disconnect observed as Exception).
      4. Logs → close code 4404 (same).
      5. Restore status → running (for any downstream test isolation checks).
    """
    agent_id, env_id = _bootstrap_running_env(
        client, superuser_token_headers, db
    )
    su_token = _get_superuser_raw_token(client)

    # ── Phase 2: Force env to stopped ────────────────────────────────────
    _set_env_status(db, env_id, "stopped")

    terminal_url = f"{_ENV_BASE}/{env_id}/terminal"
    logs_url = f"{_ENV_BASE}/{env_id}/logs/stream"

    # ── Phase 3: Terminal → rejected (4404) ──────────────────────────────
    _assert_ws_close_code(
        client, f"{terminal_url}?token={su_token}", _WS_CLOSE_ENV_NOT_RUNNING
    )

    # ── Phase 4: Logs → rejected (4404) ──────────────────────────────────
    _assert_ws_close_code(
        client, f"{logs_url}?token={su_token}", _WS_CLOSE_ENV_NOT_RUNNING
    )

    # ── Phase 5: Running again → status guard passes (close 1011, not 4404) ─
    _set_env_status(db, env_id, "running")
    # Patch the connector so no env-core is needed. A 1011 close proves the
    # status guard passed (the service ran past it); 4404 would mean it didn't.
    with patch(
        "app.services.environments.agent_env_connector.agent_env_connector.open_shell_websocket",
        new=AsyncMock(side_effect=RuntimeError("no env-core in test")),
    ):
        _assert_ws_close_code(
            client,
            f"{terminal_url}?token={su_token}",
            _WS_CLOSE_ENV_UNREACHABLE,
        )


# Unit tests for EnvConsoleActivityTracker (open-rate cap + concurrency/warm
# invariants, pure in-memory) live in tests/unit/test_env_console_tracker.py.


# ── Scenario 7: Suspension scheduler gate ────────────────────────────────────


def test_suspension_scheduler_skips_console_warm_env(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    The suspension scheduler must skip environments where is_console_warm() is True.

    Approach:
      1. Create an agent → drain tasks → env is "running".
      2. Call _check_and_suspend_environments() with is_console_warm patched to
         True for ALL environments (simplest way to verify the gate path without
         stale last_activity_at state that crosses session boundaries).
      3. Verify EnvironmentLifecycleManager.suspend_environment was NOT called.

    This mirrors the sync-warm test in test_cli_live_sync.py but verifies the
    console-tracker gate path separately.
    """
    import asyncio

    from app.services.environments.environment_lifecycle import EnvironmentLifecycleManager
    from app.services.environments.environment_suspension_scheduler import (
        _check_and_suspend_environments,
    )

    # ── Phase 1: Create agent → env running ───────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    env_id = agent["active_environment_id"]
    assert env_id is not None

    # ── Phase 2 & 3: Run scheduler with is_console_warm returning True ─────
    suspend_calls: list[str] = []

    async def _mock_suspend(db_session, environment):
        suspend_calls.append(str(environment.id))

    with (
        patch(
            "app.services.environments.env_console_activity_tracker.env_console_activity_tracker.is_console_warm",
            return_value=True,
        ),
        patch.object(
            EnvironmentLifecycleManager,
            "suspend_environment",
            side_effect=_mock_suspend,
        ),
    ):
        asyncio.run(_check_and_suspend_environments())

    # ── Phase 4: env was NOT suspended ────────────────────────────────────
    assert env_id not in suspend_calls, (
        f"Scheduler must skip env {env_id} when is_console_warm=True; "
        f"but suspend was called for: {suspend_calls}"
    )
