"""Shared setup helpers for Agent-API capability request (``_cinna/``) tests."""
from fastapi.testclient import TestClient

from app.core.config import settings
from app.services.environments.environment_service import EnvironmentService
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.credential import get_credential_with_data, link_credential_to_agent

API = settings.API_V1_STR


def requests_url(producer_id: str) -> str:
    return f"{API}/agent-api/{producer_id}/_cinna/improvement-requests"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def connect(
    client: TestClient,
    headers: dict[str, str],
    producer_id: str,
    consumer_agent_id: str | None = None,
) -> dict:
    """Connect helper: returns ``{id, credential_id, token}`` (L1 connection)."""
    body: dict = {}
    if consumer_agent_id is not None:
        body["consumer_agent_id"] = consumer_agent_id
    r = client.post(f"{API}/agents/{producer_id}/agent-api/connect", headers=headers, json=body)
    assert r.status_code == 200, f"Connect failed: {r.text}"
    conn = r.json()
    cred = get_credential_with_data(client, headers, conn["credential_id"])
    return {
        "id": conn["token_id"],
        "credential_id": conn["credential_id"],
        "token": cred["credential_data"]["token"],
    }


def read_owner_identity_token(
    client: TestClient, headers: dict[str, str], consumer_id: str, credential_id: str
) -> str:
    """The ``owner_identity_token`` the env credentials sync injects for the consumer."""
    persistent = EnvironmentTestAdapter()
    lm = EnvironmentService._lifecycle_manager
    original = lm.get_adapter
    lm.get_adapter = lambda env: persistent
    try:
        link_credential_to_agent(client, headers, consumer_id, credential_id)
        drain_tasks()
    finally:
        lm.get_adapter = original
    assert persistent.credentials_set, "credential sync never reached the env adapter"
    creds = persistent.credentials_set["credentials_json"]
    identity = next((c for c in creds if c["type"] == "owner_identity_token"), None)
    assert identity is not None, [c["type"] for c in creds]
    return identity["credential_data"]["token"]


def connected_consumer(
    client: TestClient, headers: dict[str, str], producer_id: str, name: str = "Req Consumer"
) -> dict:
    """A consumer agent connected to ``producer_id``, with its bearer + identity token.

    Returns ``{agent, connection, headers}`` where ``headers`` carry both
    ``Authorization`` and ``X-Cinna-Caller-Identity``.
    """
    consumer = create_agent_via_api(client, headers, name=name)
    drain_tasks()
    connection = connect(client, headers, producer_id, consumer_agent_id=consumer["id"])
    identity = read_owner_identity_token(
        client, headers, consumer["id"], connection["credential_id"]
    )
    return {
        "agent": consumer,
        "connection": connection,
        "identity": identity,
        "headers": {**bearer(connection["token"]), "X-Cinna-Caller-Identity": identity},
    }


def valid_body(title: str = "Need a bulk export endpoint", **overrides) -> dict:
    body = {
        "title": title,
        "need": "I need to export all orders for a month in one call.",
        "proposed_endpoint": {
            "method": "GET",
            "path": "/orders/export",
            "params": [{"name": "month", "in": "query", "type": "string", "description": "YYYY-MM"}],
            "response_fields": [{"name": "rows", "type": "array", "description": "orders"}],
        },
        "spec_version_seen": "1.0.0",
    }
    body.update(overrides)
    return body


def file_request(
    client: TestClient, producer_id: str, headers: dict[str, str], **body_overrides
) -> dict:
    r = client.post(requests_url(producer_id), headers=headers, json=valid_body(**body_overrides))
    assert r.status_code == 201, r.text
    return r.json()
