"""
Integration tests for the General Assistant (GA) feature.

Covers:
  A. GA creation via POST /users/me/general-assistant
  B. GA delete protection via DELETE /agents/{id}
  C. GA share protection via POST /agents/{agent_id}/shares
  D. GA session mode — sessions created for GA are forced to "building" mode
  E. GA workspace filtering — GA appears regardless of workspace filter
  F. GA auto-creation NOT triggered on signup (GA disabled by default)

Business rules verified:
  1. GA is created with is_general_assistant=True, show_on_dashboard=True, name="General Assistant"
  2. Creating GA when general_assistant_enabled=False returns 400
  3. Creating GA twice returns 409
  4. Deleting a GA returns 403 (ValueError caught by route)
  5. Sharing a GA returns 403 (HTTPException from share service)
  6. Sessions created for GA agents are always in "building" mode regardless of requested mode
  7. When filtering agents by workspace, GA appears alongside workspace-matching agents
  8. GA has no user_workspace_id (workspace-agnostic)
  9. On signup, trigger_auto_create_background is NOT invoked (GA disabled by default)

Setup notes:
  - The migration adds general_assistant_enabled with server_default=false, so existing users
    (including the seeded superuser) have general_assistant_enabled=False. Tests that create a GA
    first enable it via PATCH /users/me.
  - Environment creation requires an AI credential. The agents conftest.py autouse fixture
    setup_default_credentials creates one for the superuser. Random users need their own.
  - The agents conftest.py autouse fixtures provide environment and session stubs so no
    real Docker containers are needed.
"""
import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.session import create_session_via_api
from tests.utils.user import (
    create_random_user_with_headers,
)
from tests.utils.utils import random_email, random_lower_string

API = settings.API_V1_STR


# ── Local helpers ─────────────────────────────────────────────────────────────


def _enable_general_assistant(
    client: TestClient,
    token_headers: dict[str, str],
) -> None:
    """Set general_assistant_enabled=True for the current user via PATCH /users/me."""
    r = client.patch(
        f"{API}/users/me",
        headers=token_headers,
        json={"general_assistant_enabled": True},
    )
    assert r.status_code == 200, f"Failed to enable GA: {r.text}"
    assert r.json()["general_assistant_enabled"] is True


def _disable_general_assistant(
    client: TestClient,
    token_headers: dict[str, str],
) -> None:
    """Set general_assistant_enabled=False for the current user via PATCH /users/me."""
    r = client.patch(
        f"{API}/users/me",
        headers=token_headers,
        json={"general_assistant_enabled": False},
    )
    assert r.status_code == 200, f"Failed to disable GA: {r.text}"


def _setup_user_for_ga(
    client: TestClient,
    token_headers: dict[str, str],
) -> None:
    """Enable the GA feature and create a default AI credential for the user.

    Required before calling POST /users/me/general-assistant because:
    - general_assistant_enabled is False for existing users (migration server_default=false)
    - GA creation calls EnvironmentService.create_environment which validates an AI credential
    """
    _enable_general_assistant(client, token_headers)
    create_random_ai_credential(
        client, token_headers,
        credential_type="anthropic",
        api_key="sk-ant-api03-test-ga-key",
        name="ga-test-credential",
        set_default=True,
    )


def _create_general_assistant(
    client: TestClient,
    token_headers: dict[str, str],
) -> dict:
    """Call POST /users/me/general-assistant. Asserts 200 and returns the agent JSON."""
    r = client.post(
        f"{API}/users/me/general-assistant",
        headers=token_headers,
    )
    assert r.status_code == 200, f"GA creation failed: {r.text}"
    return r.json()


def _create_workspace(
    client: TestClient,
    token_headers: dict[str, str],
    name: str | None = None,
) -> dict:
    """Create a user workspace and return the workspace JSON."""
    r = client.post(
        f"{API}/user-workspaces/",
        headers=token_headers,
        json={"name": name or f"workspace-{random_lower_string()[:8]}"},
    )
    assert r.status_code == 200, f"Workspace creation failed: {r.text}"
    return r.json()


# ── A. GA Creation ────────────────────────────────────────────────────────────


def test_general_assistant_creation_full_lifecycle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    GA creation lifecycle:
      1. Enable GA feature and AI credential for the user (migration sets enabled=False by default)
      2. POST /users/me/general-assistant → 200 with correct fields
      3. Verify response fields: is_general_assistant, name, show_on_dashboard, color, workspace
      4. Verify GA appears in GET /agents/ list with is_general_assistant=True
      5. POST /users/me/general-assistant again → 409 (already exists)
      6. Unauthenticated request → 401
    """
    # ── Phase 1: Setup user ───────────────────────────────────────────────
    # The superuser_token_headers fixture's user has general_assistant_enabled=False
    # due to the migration server_default. We enable it here explicitly.
    _setup_user_for_ga(client, superuser_token_headers)

    # ── Phase 2: Create GA ────────────────────────────────────────────────
    ga = _create_general_assistant(client, superuser_token_headers)
    ga_id = ga["id"]
    drain_tasks()

    # ── Phase 3: Verify response fields ───────────────────────────────────
    assert ga["is_general_assistant"] is True, "GA flag must be True"
    assert ga["name"] == "General Assistant", f"Expected 'General Assistant', got {ga['name']}"
    assert ga["show_on_dashboard"] is True, "GA must have show_on_dashboard=True"
    assert ga["ui_color_preset"] == "violet", f"Expected 'violet' color preset, got {ga['ui_color_preset']}"
    # GA is workspace-agnostic
    assert ga["user_workspace_id"] is None, "GA must have no workspace (workspace-agnostic)"

    # ── Phase 4: GA appears in agents list ────────────────────────────────
    r = client.get(f"{API}/agents/", headers=superuser_token_headers)
    assert r.status_code == 200
    agents_by_id = {a["id"]: a for a in r.json()["data"]}
    assert ga_id in agents_by_id, "GA must appear in the agents list"
    assert agents_by_id[ga_id]["is_general_assistant"] is True

    # ── Phase 5: Creating GA again → 409 ──────────────────────────────────
    r_dup = client.post(f"{API}/users/me/general-assistant", headers=superuser_token_headers)
    assert r_dup.status_code == 409, f"Expected 409 on duplicate GA creation, got {r_dup.status_code}"
    assert "already exists" in r_dup.json()["detail"].lower()

    # ── Phase 6: Unauthenticated → 401 ────────────────────────────────────
    r_unauth = client.post(f"{API}/users/me/general-assistant")
    assert r_unauth.status_code == 401


def test_general_assistant_creation_disabled_user(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When general_assistant_enabled=False on the user account:
      1. Confirm the feature is disabled (server_default leaves it False for existing users)
      2. POST /users/me/general-assistant → 400 with "Enable General Assistant" detail
      3. Enable the feature → creation succeeds
    """
    # ── Phase 1: Confirm GA is disabled (default for existing users) ─────
    r_me = client.get(f"{API}/users/me", headers=superuser_token_headers)
    assert r_me.status_code == 200
    assert r_me.json()["general_assistant_enabled"] is False

    # ── Phase 2: GA creation blocked with 400 ─────────────────────────────
    r = client.post(f"{API}/users/me/general-assistant", headers=superuser_token_headers)
    assert r.status_code == 400, f"Expected 400 for disabled GA feature, got {r.status_code}"
    detail = r.json()["detail"].lower()
    assert "enable" in detail or "not enabled" in detail or "general assistant" in detail

    # ── Phase 3: Enable and create GA ─────────────────────────────────────
    _setup_user_for_ga(client, superuser_token_headers)
    ga = _create_general_assistant(client, superuser_token_headers)
    assert ga["is_general_assistant"] is True


def test_general_assistant_creation_for_new_user(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A newly registered user has general_assistant_enabled=False by default.
    After enabling it and setting up an AI credential, they can create a GA.

      1. Sign up a new user
      2. Verify GA is disabled by default
      3. Enable GA and create AI credential
      4. POST /users/me/general-assistant → 200
      5. Second call → 409
    """
    # ── Phase 1: Sign up new user ─────────────────────────────────────────
    email = random_email()
    password = random_lower_string()
    r_signup = client.post(
        f"{API}/users/signup",
        json={"email": email, "password": password},
    )
    assert r_signup.status_code == 200
    new_user_headers = {
        "Authorization": "Bearer "
        + client.post(
            f"{API}/login/access-token",
            data={"username": email, "password": password},
        ).json()["access_token"]
    }

    # ── Phase 2: Verify GA is disabled by default ─────────────────────────
    r_me = client.get(f"{API}/users/me", headers=new_user_headers)
    assert r_me.status_code == 200
    assert r_me.json()["general_assistant_enabled"] is False

    # ── Phase 3: Enable GA and create AI credential ───────────────────────
    _setup_user_for_ga(client, new_user_headers)

    # ── Phase 4: Create GA → success ──────────────────────────────────────
    ga = _create_general_assistant(client, new_user_headers)
    drain_tasks()
    assert ga["is_general_assistant"] is True
    assert ga["name"] == "General Assistant"
    assert ga["user_workspace_id"] is None

    # ── Phase 5: Second call → 409 ────────────────────────────────────────
    r_dup = client.post(f"{API}/users/me/general-assistant", headers=new_user_headers)
    assert r_dup.status_code == 409


# ── B. GA Delete Protection ───────────────────────────────────────────────────


def test_general_assistant_cannot_be_deleted(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Attempting to delete a GA agent returns 403.
    Normal agents can still be deleted as usual.

      1. Setup user and create GA
      2. DELETE /agents/{ga_id} → 403
      3. Create a regular agent and delete it → 200 (guard is specific to GA agents)
      4. GA still exists after the failed delete attempt
    """
    # ── Phase 1: Setup and create GA ──────────────────────────────────────
    _setup_user_for_ga(client, superuser_token_headers)
    ga = _create_general_assistant(client, superuser_token_headers)
    ga_id = ga["id"]
    drain_tasks()

    # ── Phase 2: Delete GA → 403 ──────────────────────────────────────────
    r = client.delete(f"{API}/agents/{ga_id}", headers=superuser_token_headers)
    assert r.status_code == 403, f"Expected 403 when deleting GA, got {r.status_code}"
    detail = r.json()["detail"]
    assert "General Assistant" in detail or "cannot be deleted" in detail.lower()

    # ── Phase 3: Regular agent can be deleted ─────────────────────────────
    regular_agent = create_agent_via_api(client, superuser_token_headers, name="Deletable Agent")
    drain_tasks()
    r_del = client.delete(f"{API}/agents/{regular_agent['id']}", headers=superuser_token_headers)
    assert r_del.status_code == 200, f"Expected 200 when deleting regular agent, got {r_del.status_code}"

    # ── Phase 4: GA still accessible after failed delete ─────────────────
    r_get = client.get(f"{API}/agents/{ga_id}", headers=superuser_token_headers)
    assert r_get.status_code == 200, "GA must still be accessible after failed delete attempt"
    assert r_get.json()["is_general_assistant"] is True


# ── C. GA Share Protection ────────────────────────────────────────────────────


def test_general_assistant_cannot_be_shared(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Attempting to share a GA agent returns 403.
    A normal agent can be shared normally.

      1. Setup user and create GA
      2. Create a second user (the share target)
      3. POST /agents/{ga_id}/shares → 403
      4. Create a regular agent and share it → 200
    """
    # ── Phase 1: Setup and create GA ──────────────────────────────────────
    _setup_user_for_ga(client, superuser_token_headers)
    ga = _create_general_assistant(client, superuser_token_headers)
    ga_id = ga["id"]
    drain_tasks()

    # ── Phase 2: Create target user ───────────────────────────────────────
    target_user, _ = create_random_user_with_headers(client)
    target_email = target_user["email"]

    # ── Phase 3: Share GA → 403 ───────────────────────────────────────────
    r = client.post(
        f"{API}/agents/{ga_id}/shares",
        headers=superuser_token_headers,
        json={
            "shared_with_email": target_email,
            "share_mode": "user",
        },
    )
    assert r.status_code == 403, f"Expected 403 when sharing GA, got {r.status_code}"
    detail = r.json()["detail"]
    assert "General Assistant" in detail or "cannot be shared" in detail.lower()

    # ── Phase 4: Regular agent can be shared ──────────────────────────────
    regular_agent = create_agent_via_api(client, superuser_token_headers, name="Shareable Agent")
    drain_tasks()
    r_share = client.post(
        f"{API}/agents/{regular_agent['id']}/shares",
        headers=superuser_token_headers,
        json={
            "shared_with_email": target_email,
            "share_mode": "user",
        },
    )
    assert r_share.status_code == 200, f"Expected 200 when sharing regular agent, got {r_share.text}"
    assert r_share.json()["status"] == "pending"


# ── D. GA Session Mode ────────────────────────────────────────────────────────


def test_general_assistant_session_forced_to_building_mode(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    Sessions created for a GA agent are always in "building" mode regardless
    of the requested mode.

      1. Add the general-assistant-env template to the test lifecycle manager so the
         GA background environment task can complete successfully
      2. Setup user and create GA; drain tasks to let environment become active
      3. Re-fetch agent to confirm active_environment_id is set
      4. Create session with mode="conversation" → session.mode must be "building"
      5. Create session with mode="building" → session.mode is "building"
      6. Verify via GET /sessions/{session_id} that mode is persisted as "building"

    Note: The GA service prefers the 'general-assistant-env' template. The test lifecycle
    manager only has the default template. We add the GA template dir here so the background
    task can find it and complete the auto_start sequence (setting active_environment_id).
    """
    # ── Phase 1: Add GA template to test lifecycle manager ────────────────
    # patch_environment_adapter yields the EnvironmentLifecycleManager configured
    # with a temp directory. The GA service uses 'general-assistant-env' first.
    # Create a minimal template dir so create_environment_instance can find it.
    lm = patch_environment_adapter
    ga_template_dir = lm.templates_dir / "general-assistant-env"
    ga_template_dir.mkdir(parents=True, exist_ok=True)
    (ga_template_dir / "docker-compose.template.yml").write_text(
        "version: '3'\nservices:\n  agent:\n    image: test\n    ports:\n      - '${AGENT_PORT}:8000'\n"
    )

    # ── Phase 2: Setup and create GA ──────────────────────────────────────
    _setup_user_for_ga(client, superuser_token_headers)
    ga = _create_general_assistant(client, superuser_token_headers)
    ga_id = ga["id"]
    drain_tasks()

    # ── Phase 3: Re-fetch agent to confirm active_environment_id is set ──
    r_agent = client.get(f"{API}/agents/{ga_id}", headers=superuser_token_headers)
    assert r_agent.status_code == 200
    assert r_agent.json()["active_environment_id"] is not None, (
        "GA must have an active environment after drain_tasks(). "
        "The background task sets active_environment_id after environment creation."
    )

    # ── Phase 4: Request "conversation" mode → forced to "building" ───────
    conversation_session = create_session_via_api(
        client, superuser_token_headers, ga_id, mode="conversation"
    )
    assert conversation_session["mode"] == "building", (
        f"Expected GA session mode='building' even when 'conversation' was requested, "
        f"got '{conversation_session['mode']}'"
    )

    # ── Phase 5: Request "building" mode → stays "building" ───────────────
    building_session = create_session_via_api(
        client, superuser_token_headers, ga_id, mode="building"
    )
    assert building_session["mode"] == "building"

    # ── Phase 6: Verify persistence via GET ───────────────────────────────
    r_session = client.get(
        f"{API}/sessions/{conversation_session['id']}",
        headers=superuser_token_headers,
    )
    assert r_session.status_code == 200
    assert r_session.json()["mode"] == "building"


def test_regular_agent_session_mode_not_affected(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Session mode enforcement is specific to GA agents.
    Regular agents respect the requested mode.

      1. Create a regular agent
      2. Create session with mode="conversation" → mode is "conversation"
    """
    regular_agent = create_agent_via_api(client, superuser_token_headers, name="Normal Mode Agent")
    drain_tasks()

    session = create_session_via_api(
        client, superuser_token_headers, regular_agent["id"], mode="conversation"
    )
    assert session["mode"] == "conversation", (
        f"Regular agent session mode must not be changed, got '{session['mode']}'"
    )


# ── E. GA Workspace Filtering ─────────────────────────────────────────────────


def test_general_assistant_appears_in_workspace_filter(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When listing agents filtered by a specific workspace, the GA (which has
    user_workspace_id=None) still appears in the results alongside workspace-specific agents.

      1. Create a workspace
      2. Create a regular agent in that workspace
      3. Setup user and create GA (no workspace)
      4. GET /agents/?user_workspace_id={workspace_id} → both the workspace agent AND GA appear
      5. GET /agents/?user_workspace_id= (empty string, default workspace) → GA appears, workspace agent does NOT
      6. GET /agents/ (no filter) → all agents appear
    """
    # ── Phase 1: Create workspace ─────────────────────────────────────────
    workspace = _create_workspace(client, superuser_token_headers)
    workspace_id = workspace["id"]

    # ── Phase 2: Create agent in workspace ────────────────────────────────
    r_agent = client.post(
        f"{API}/agents/",
        headers=superuser_token_headers,
        json={"name": "Workspace Agent", "user_workspace_id": workspace_id},
    )
    assert r_agent.status_code == 200
    workspace_agent_id = r_agent.json()["id"]
    drain_tasks()

    # ── Phase 3: Setup user and create GA (no workspace) ──────────────────
    _setup_user_for_ga(client, superuser_token_headers)
    ga = _create_general_assistant(client, superuser_token_headers)
    ga_id = ga["id"]
    drain_tasks()

    # ── Phase 4: Filter by workspace → workspace agent + GA both appear ───
    r_filtered = client.get(
        f"{API}/agents/?user_workspace_id={workspace_id}",
        headers=superuser_token_headers,
    )
    assert r_filtered.status_code == 200
    filtered_ids = {a["id"] for a in r_filtered.json()["data"]}
    assert workspace_agent_id in filtered_ids, "Workspace agent must appear in workspace filter"
    assert ga_id in filtered_ids, "GA must appear in workspace filter even though it has no workspace"

    # ── Phase 5: Default workspace filter → GA appears, workspace agent does NOT
    r_default = client.get(
        f"{API}/agents/?user_workspace_id=",
        headers=superuser_token_headers,
    )
    assert r_default.status_code == 200
    default_ids = {a["id"] for a in r_default.json()["data"]}
    assert ga_id in default_ids, "GA must appear in default workspace filter"
    assert workspace_agent_id not in default_ids, (
        "Workspace-specific agent must NOT appear in default workspace filter"
    )

    # ── Phase 6: No filter → all appear ───────────────────────────────────
    r_all = client.get(f"{API}/agents/", headers=superuser_token_headers)
    assert r_all.status_code == 200
    all_ids = {a["id"] for a in r_all.json()["data"]}
    assert ga_id in all_ids
    assert workspace_agent_id in all_ids


def test_general_assistant_workspace_agnostic_field(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    The GA agent's user_workspace_id must always be None,
    confirming it is not associated with any specific workspace.
    """
    _setup_user_for_ga(client, superuser_token_headers)
    ga = _create_general_assistant(client, superuser_token_headers)
    drain_tasks()

    assert ga["user_workspace_id"] is None, (
        f"GA must have user_workspace_id=None, got {ga['user_workspace_id']}"
    )

    # Also verify via GET /agents/{id}
    r_get = client.get(f"{API}/agents/{ga['id']}", headers=superuser_token_headers)
    assert r_get.status_code == 200
    assert r_get.json()["user_workspace_id"] is None


# ── F. GA Auto-Creation on Signup ─────────────────────────────────────────────


def test_general_assistant_auto_creation_not_triggered_on_signup(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Since GA is disabled by default, signup does NOT trigger auto-creation.

      1. Patch trigger_auto_create_background to capture invocations
      2. Register a new user
      3. Verify trigger_auto_create_background was NOT called
    """
    captured_user_ids: list[uuid.UUID] = []

    def _capture_trigger(user_id: uuid.UUID) -> None:
        captured_user_ids.append(user_id)

    email = random_email()
    password = random_lower_string()

    with patch(
        "app.services.users.general_assistant_service.GeneralAssistantService.trigger_auto_create_background",
        side_effect=_capture_trigger,
    ):
        r = client.post(
            f"{API}/users/signup",
            json={"email": email, "password": password},
        )

    assert r.status_code == 200, f"Signup failed: {r.text}"

    assert len(captured_user_ids) == 0, (
        f"trigger_auto_create_background must NOT be called on signup "
        f"(GA disabled by default), called {len(captured_user_ids)} times"
    )


def test_general_assistant_auto_creation_not_triggered_on_login(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    The GA auto-creation background trigger fires only on signup, NOT on login.

      1. Patch trigger_auto_create_background to capture invocations
      2. Log in as an existing user
      3. Verify trigger was NOT called
    """
    captured_calls: list = []

    with patch(
        "app.services.users.general_assistant_service.GeneralAssistantService.trigger_auto_create_background",
        side_effect=lambda uid: captured_calls.append(uid),
    ):
        r = client.post(
            f"{API}/login/access-token",
            data={
                "username": settings.FIRST_SUPERUSER,
                "password": settings.FIRST_SUPERUSER_PASSWORD,
            },
        )

    assert r.status_code == 200
    assert len(captured_calls) == 0, (
        "trigger_auto_create_background must NOT be called on login, "
        f"but was called {len(captured_calls)} time(s)"
    )


# ── G. GA isolation and list fields ───────────────────────────────────────────


def test_general_assistant_is_general_assistant_field_in_list(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    The is_general_assistant field is correctly surfaced in the agents list.

      1. Setup user, create GA and a regular agent
      2. List all agents
      3. Verify GA has is_general_assistant=True, regular agent has is_general_assistant=False
    """
    _setup_user_for_ga(client, superuser_token_headers)
    ga = _create_general_assistant(client, superuser_token_headers)
    ga_id = ga["id"]

    regular_agent = create_agent_via_api(client, superuser_token_headers, name="Not A GA")
    regular_id = regular_agent["id"]
    drain_tasks()

    r = client.get(f"{API}/agents/", headers=superuser_token_headers)
    assert r.status_code == 200
    agents_by_id = {a["id"]: a for a in r.json()["data"]}

    assert ga_id in agents_by_id
    assert agents_by_id[ga_id]["is_general_assistant"] is True

    assert regular_id in agents_by_id
    assert agents_by_id[regular_id]["is_general_assistant"] is False


def test_general_assistant_isolation_between_users(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Each user has their own independent GA. One user's GA is not visible to another.

      1. Create user A, enable GA, set up AI credential, and create their GA
      2. Create user B, enable GA, set up AI credential, and create their GA
      3. User A listing agents sees only their own GA
      4. User B listing agents sees only their own GA
    """
    # ── Phase 1: User A ────────────────────────────────────────────────────
    _user_a, headers_a = create_random_user_with_headers(client)
    _setup_user_for_ga(client, headers_a)
    ga_a = _create_general_assistant(client, headers_a)
    drain_tasks()

    # ── Phase 2: User B ────────────────────────────────────────────────────
    _user_b, headers_b = create_random_user_with_headers(client)
    _setup_user_for_ga(client, headers_b)
    ga_b = _create_general_assistant(client, headers_b)
    drain_tasks()

    # ── Phase 3: User A sees only their GA ────────────────────────────────
    r_a = client.get(f"{API}/agents/", headers=headers_a)
    assert r_a.status_code == 200
    agent_ids_a = {a["id"] for a in r_a.json()["data"]}
    assert ga_a["id"] in agent_ids_a
    assert ga_b["id"] not in agent_ids_a, "User A must not see User B's GA"

    # ── Phase 4: User B sees only their GA ────────────────────────────────
    r_b = client.get(f"{API}/agents/", headers=headers_b)
    assert r_b.status_code == 200
    agent_ids_b = {a["id"] for a in r_b.json()["data"]}
    assert ga_b["id"] in agent_ids_b
    assert ga_a["id"] not in agent_ids_b, "User B must not see User A's GA"
