"""Helpers for agent-component dashboard tests (env-core ``/dashboards/*`` stubbed).

``component_adapter`` is a drop-in override of the ``patch_environment_adapter``
fixture that keeps ONE adapter instance, so a test can configure the components
the environment "has" and read back which endpoints ran.
"""
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.dashboard import create_dashboard
from tests.utils.environment import set_environment_status
from tests.utils.fixtures import setup_environment_adapter, teardown_environment_adapter

API = settings.API_V1_STR


@pytest.fixture(autouse=True)
def patch_environment_adapter(tmp_path_factory):
    """Persistent stub adapter, reachable as ``patch_environment_adapter._test_adapter``."""
    lm = setup_environment_adapter(tmp_path_factory, persistent_adapter=True)
    yield lm
    teardown_environment_adapter()


def make_component(
    name: str = "sales",
    *,
    skill: str | None = None,
    params: dict | None = None,
    endpoints: dict | None = None,
    source: dict | None = None,
    refresh: dict | None = None,
    view: str = "table",
    requires_credentials: list[str] | None = None,
    **extra,
) -> dict:
    """One entry of env-core's ``/dashboards/components`` list."""
    spec: dict = {
        "title": name.title(),
        "view": view,
        "source": source or {"endpoint": "get_data"},
        "endpoints": endpoints if endpoints is not None else {"get_data": {}, "lookup": {}},
    }
    if params:
        spec["params"] = params
    if refresh:
        spec["refresh"] = refresh
    if requires_credentials:
        spec["requires_credentials"] = requires_credentials
    spec.update(extra)
    return {
        "name": name,
        "skill": skill,
        "component_json": spec,
        "findings": [],
        "size_bytes": 128,
    }


def setup_component_agent(
    client: TestClient,
    db: Session,
    headers: dict[str, str],
    adapter,
    components: list[dict],
    *,
    env_status: str = "running",
) -> dict:
    """Create an agent whose (stubbed) environment holds ``components``."""
    adapter.dashboard_components = components
    agent = create_agent_via_api(client, headers)
    drain_tasks()
    agent = get_agent(client, headers, agent["id"])
    set_environment_status(db, agent["active_environment_id"], env_status)
    return agent


def add_component_block(
    client: TestClient,
    headers: dict[str, str],
    dashboard_id: str,
    agent_id: str,
    component: str = "sales",
    *,
    skill: str | None = None,
    params: dict | None = None,
    refresh: dict | None = None,
):
    """POST an ``agent_component`` block; returns the raw response."""
    config: dict = {"component": component, "skill": skill, "params": params or {}}
    if refresh is not None:
        config["refresh"] = refresh
    return client.post(
        f"{API}/dashboards/{dashboard_id}/blocks",
        headers=headers,
        json={
            "agent_id": agent_id,
            "view_type": "agent_component",
            "grid_x": 0, "grid_y": 0, "grid_w": 4, "grid_h": 3,
            "config": config,
        },
    )


def create_component_tile(
    client: TestClient,
    db: Session,
    headers: dict[str, str],
    adapter,
    components: list[dict] | None = None,
    **block_kwargs,
) -> dict:
    """Agent + dashboard + one component block. Returns ids and the tile URL base."""
    components = components if components is not None else [make_component()]
    agent = setup_component_agent(client, db, headers, adapter, components)
    dashboard = create_dashboard(client, headers)
    r = add_component_block(
        client, headers, dashboard["id"], agent["id"], components[0]["name"], **block_kwargs
    )
    assert r.status_code == 200, f"Add component block failed: {r.text}"
    block = r.json()
    return {
        "agent": agent,
        "dashboard": dashboard,
        "block": block,
        "base": f"{API}/dashboards/{dashboard['id']}/blocks/{block['id']}/component",
    }


# ── Dashboard chat helpers (P4) ──────────────────────────────────────────────

def open_chat(
    client: TestClient, headers: dict[str, str], dashboard_id: str, agent_id: str, *, new: bool = False
) -> dict:
    """POST /dashboards/{id}/chat; returns the session."""
    r = client.post(
        f"{API}/dashboards/{dashboard_id}/chat", headers=headers, json={"agent_id": agent_id, "new": new}
    )
    assert r.status_code == 200, f"Open chat failed: {r.text}"
    session = r.json()["session"]
    assert session is not None
    return session


def send_chat_message(
    client: TestClient,
    headers: dict[str, str],
    session_id: str,
    content: str,
    dashboard_context: str | None = None,
    *,
    expect: int = 200,
):
    """Send a message through the stream route with a stub env connector.

    Returns ``(response, stub)``; ``stub.stream_calls[0]["payload"]`` is what the
    environment received. ``drain_tasks`` runs inside the patch.
    """
    from unittest.mock import patch

    from tests.stubs.agent_env_stub import StubAgentEnvConnector

    stub = StubAgentEnvConnector(response_text="ok")
    body: dict = {"content": content, "file_ids": []}
    if dashboard_context is not None:
        body["dashboard_context"] = dashboard_context
    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        r = client.post(f"{API}/sessions/{session_id}/messages/stream", headers=headers, json=body)
        assert r.status_code == expect, r.text
        drain_tasks()
    return r, stub
