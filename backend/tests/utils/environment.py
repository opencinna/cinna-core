"""Helper functions for agent environment API calls in tests."""
import uuid

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings


def create_environment(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
    env_name: str = "python-env-advanced",
    instance_name: str | None = None,
) -> dict:
    """Create environment via POST /api/v1/agents/{agent_id}/environments."""
    data: dict = {"env_name": env_name}
    if instance_name:
        data["instance_name"] = instance_name
    r = client.post(
        f"{settings.API_V1_STR}/agents/{agent_id}/environments",
        headers=token_headers,
        json=data,
    )
    assert r.status_code == 200, f"Create environment failed: {r.text}"
    return r.json()


def list_environments(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
) -> dict:
    """List environments via GET /api/v1/agents/{agent_id}/environments.

    Returns the full AgentEnvironmentsPublic response with ``data`` and ``count``.
    """
    r = client.get(
        f"{settings.API_V1_STR}/agents/{agent_id}/environments",
        headers=token_headers,
    )
    assert r.status_code == 200, f"List environments failed: {r.text}"
    return r.json()


def get_environment(
    client: TestClient,
    token_headers: dict[str, str],
    env_id: str,
) -> dict:
    """Get environment via GET /api/v1/environments/{env_id}."""
    r = client.get(
        f"{settings.API_V1_STR}/environments/{env_id}",
        headers=token_headers,
    )
    assert r.status_code == 200, f"Get environment failed: {r.text}"
    return r.json()


def update_environment(
    client: TestClient,
    token_headers: dict[str, str],
    env_id: str,
    **fields,
) -> dict:
    """Update environment via PATCH /api/v1/environments/{env_id}."""
    r = client.patch(
        f"{settings.API_V1_STR}/environments/{env_id}",
        headers=token_headers,
        json=fields,
    )
    assert r.status_code == 200, f"Update environment failed: {r.text}"
    return r.json()


def delete_environment(
    client: TestClient,
    token_headers: dict[str, str],
    env_id: str,
) -> dict:
    """Delete environment via DELETE /api/v1/environments/{env_id}."""
    r = client.delete(
        f"{settings.API_V1_STR}/environments/{env_id}",
        headers=token_headers,
    )
    assert r.status_code == 200, f"Delete environment failed: {r.text}"
    return r.json()


def set_environment_status(
    db: Session,
    env_id: str | uuid.UUID,
    status: str,
) -> None:
    """Force an agent environment's runtime ``status`` directly on the test DB.

    There is no public API seam for setting a running/stopped status: the
    lifecycle status is driven by the real Docker build/start path, which the
    test suite stubs out. Tests that need a deterministic env status (e.g. to
    exercise the upload "environment must be running" gate) poke it here so the
    DB write lives in one documented place rather than being copy-pasted.

    TODO: replace with a lifecycle/test-only status API if one is introduced.
    """
    from app.models import AgentEnvironment

    if isinstance(env_id, str):
        env_id = uuid.UUID(env_id)
    env = db.get(AgentEnvironment, env_id)
    assert env is not None, f"Environment {env_id} not found"
    env.status = status
    db.add(env)
    db.flush()


def set_environment_critical_state(
    db: Session,
    env_id: str | uuid.UUID,
    critical_state: bool,
) -> None:
    """Force an agent environment's ``critical_state`` flag directly on the test DB.

    Same rationale and same documented-seam pattern as ``set_environment_status``
    above: there is no public API seam for flipping this flag (it is set by the
    real container health-check path, which the test suite stubs out), and
    tests that need to exercise the *coexistence* of ``critical_state=True``
    with ``status="running"`` — e.g. the server-channels pending-flush "critical
    state is not a failure" behavior — need a deterministic way to produce it.
    """
    from app.models import AgentEnvironment

    if isinstance(env_id, str):
        env_id = uuid.UUID(env_id)
    env = db.get(AgentEnvironment, env_id)
    assert env is not None, f"Environment {env_id} not found"
    env.critical_state = critical_state
    db.add(env)
    db.flush()


def link_ai_credential_to_environment(
    db: Session,
    environment_id: str | uuid.UUID,
    credential_id: str | uuid.UUID,
    *,
    conversation: bool = False,
    building: bool = False,
) -> None:
    """Stamp a per-mode AI credential id directly onto an environment row.

    There IS a public seam — ``POST /environments/{id}/reconfigure`` accepts
    ``conversation_ai_credential_id`` / ``building_ai_credential_id`` — but it
    validates SDK↔credential pairings and triggers a (stubbed) rebuild, which is
    far heavier than these query-only ``get_affected_environments`` tests need.
    Until a lightweight link endpoint exists, the column write lives here in one
    documented place.

    TODO: switch to the reconfigure endpoint (or a dedicated link route) if the
    AI-credential / environment linkage gets a lighter-weight API seam.
    """
    from app.models.environments.environment import AgentEnvironment

    if isinstance(environment_id, str):
        environment_id = uuid.UUID(environment_id)
    if isinstance(credential_id, str):
        credential_id = uuid.UUID(credential_id)

    env = db.get(AgentEnvironment, environment_id)
    assert env is not None, f"Environment {environment_id} not found"
    if conversation:
        env.conversation_ai_credential_id = credential_id
    if building:
        env.building_ai_credential_id = credential_id
    db.add(env)
    db.commit()
    db.refresh(env)


def activate_environment(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
    env_id: str,
) -> dict:
    """Activate environment via POST /api/v1/agents/{agent_id}/environments/{env_id}/activate."""
    r = client.post(
        f"{settings.API_V1_STR}/agents/{agent_id}/environments/{env_id}/activate",
        headers=token_headers,
    )
    assert r.status_code == 200, f"Activate environment failed: {r.text}"
    return r.json()
