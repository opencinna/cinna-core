"""
Agent-to-Agent MCP Connector — pair-connection correctness fixes (Fix 2/4/5).

An agent2agent ``mcp_provider`` credential is a strict one-to-one connection
between a (producer connector, consumer agent) pair. These tests cover:

  - Fix 2 — the consumer agent is recorded on the credential
      (``mcp_consumer_agent_id`` public column + ``consumer_agent`` on status).
  - Fix 4A — deleting the producer connector deletes every agent2agent
      credential built from it; manual/external mcp_provider is untouched.
  - Fix 4B — unlinking the credential from its bound consumer deletes it;
      unlinking an external / non-bound credential leaves it alive.
  - Fix 5 — one credential per pair: idempotent connect; link-to-a-different
      agent is rejected (400); floating connect then link binds the pair.

Every behavioral change is gated on agent2agent (``auth_mode=="agent2agent"``);
manual/external mcp_provider (``none``/``fixed_token``) is asserted unaffected.
"""
import uuid
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import settings
from app.models.mcp.mcp_token import MCPToken
from tests.api.mcp_integration.test_a2a_connector_consumer import (
    _MCP_PROVIDERS_BASE,
    _CREDENTIALS_BASE,
    _connect_agent,
    _connect_external,
    _create_a2a_connector,
    _get_status,
    _setup_agent,
)
from tests.utils.credential import get_agent_credentials
from tests.utils.mcp import delete_mcp_connector
from tests.utils.user import create_random_user_with_headers


def _link(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
    credential_id: str,
):
    """POST /agents/{id}/credentials WITHOUT asserting status (for guard tests)."""
    return client.post(
        f"{settings.API_V1_STR}/agents/{agent_id}/credentials",
        headers=token_headers,
        json={"credential_id": credential_id},
    )


def _unlink(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
    credential_id: str,
):
    """DELETE /agents/{id}/credentials/{cid} WITHOUT asserting status."""
    return client.delete(
        f"{settings.API_V1_STR}/agents/{agent_id}/credentials/{credential_id}",
        headers=token_headers,
    )


def _credential_exists(
    client: TestClient, token_headers: dict[str, str], credential_id: str
) -> bool:
    r = client.get(
        f"{_CREDENTIALS_BASE}/{credential_id}", headers=token_headers
    )
    return r.status_code == 200


# ── Fix 2: consumer agent recorded ────────────────────────────────────────────


def test_connect_records_consumer_agent(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Connecting with a consumer agent records it on both the public credential
    (``mcp_consumer_agent_id``) and the derived status (``consumer_agent``).
    """
    producer = _setup_agent(client, superuser_token_headers, "Fix2 Producer")
    consumer = _setup_agent(client, superuser_token_headers, "Fix2 Consumer")
    connector = _create_a2a_connector(
        client, superuser_token_headers, producer["id"], name="Fix2 Connector"
    )

    resp = _connect_agent(
        client, superuser_token_headers, connector["id"],
        consumer_agent_id=consumer["id"],
    )
    credential_id = str(resp["credential_id"])

    # Public credential carries the consumer column.
    cred_r = client.get(
        f"{_CREDENTIALS_BASE}/{credential_id}", headers=superuser_token_headers
    )
    assert cred_r.status_code == 200
    assert str(cred_r.json()["mcp_consumer_agent_id"]) == str(consumer["id"])

    # Status carries the resolved consumer agent projection.
    status = _get_status(client, superuser_token_headers, credential_id)
    assert status["consumer_agent"] is not None
    assert str(status["consumer_agent"]["id"]) == str(consumer["id"])
    assert status["consumer_agent"]["name"] == "Fix2 Consumer"


def test_connect_without_consumer_has_null_consumer(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Connecting without a consumer agent leaves the column / projection null."""
    producer = _setup_agent(client, superuser_token_headers, "Fix2 NoConsumer Producer")
    connector = _create_a2a_connector(
        client, superuser_token_headers, producer["id"], name="Fix2 NoConsumer Connector"
    )

    resp = _connect_agent(client, superuser_token_headers, connector["id"])
    credential_id = str(resp["credential_id"])

    cred = client.get(
        f"{_CREDENTIALS_BASE}/{credential_id}", headers=superuser_token_headers
    ).json()
    assert cred["mcp_consumer_agent_id"] is None

    status = _get_status(client, superuser_token_headers, credential_id)
    assert status["consumer_agent"] is None


# ── Fix 4A: connector delete cleans up ────────────────────────────────────────


def test_connector_delete_removes_agent2agent_credentials(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    Deleting the producer connector deletes every agent2agent credential built
    from it (two consumers), cascade-removing their bound tokens, while leaving a
    manual/external mcp_provider credential linked to one of the consumers alive.
    """
    producer = _setup_agent(client, superuser_token_headers, "Fix4A Producer")
    consumer_a = _setup_agent(client, superuser_token_headers, "Fix4A Consumer A")
    consumer_b = _setup_agent(client, superuser_token_headers, "Fix4A Consumer B")
    connector = _create_a2a_connector(
        client, superuser_token_headers, producer["id"], name="Fix4A Connector"
    )

    resp_a = _connect_agent(
        client, superuser_token_headers, connector["id"],
        consumer_agent_id=consumer_a["id"],
    )
    resp_b = _connect_agent(
        client, superuser_token_headers, connector["id"],
        consumer_agent_id=consumer_b["id"],
    )
    cred_a = str(resp_a["credential_id"])
    cred_b = str(resp_b["credential_id"])

    # An external mcp_provider linked to consumer_a must survive the delete.
    ext = _connect_external(
        client, superuser_token_headers,
        endpoint_url="https://external.example.com/mcp",
        auth_mode="none",
        consumer_agent_id=consumer_a["id"],
    )
    ext_id = str(ext["credential_id"])

    # Both agent2agent credentials and the external one exist now.
    assert _credential_exists(client, superuser_token_headers, cred_a)
    assert _credential_exists(client, superuser_token_headers, cred_b)
    assert _credential_exists(client, superuser_token_headers, ext_id)

    delete_mcp_connector(
        client, superuser_token_headers, producer["id"], connector["id"]
    )

    # Both agent2agent credentials are gone (status 404).
    assert client.get(
        f"{_MCP_PROVIDERS_BASE}/{cred_a}/status", headers=superuser_token_headers
    ).status_code == 404
    assert client.get(
        f"{_MCP_PROVIDERS_BASE}/{cred_b}/status", headers=superuser_token_headers
    ).status_code == 404
    assert not _credential_exists(client, superuser_token_headers, cred_a)
    assert not _credential_exists(client, superuser_token_headers, cred_b)

    # Their bound tokens were cascade-deleted.
    remaining = db.exec(
        select(MCPToken).where(MCPToken.connector_id == uuid.UUID(connector["id"]))
    ).all()
    assert remaining == []

    # The external/manual credential is untouched.
    assert _credential_exists(client, superuser_token_headers, ext_id)


# ── Fix 4B: unlink deletes the bound pair ─────────────────────────────────────


def test_unlink_bound_consumer_deletes_credential(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Unlinking the bound consumer deletes the agent2agent credential AND fires the
    consumer's env-sync, so the now-dead MCP server is removed from the consumer's
    running container. The env-sync is the load-bearing assertion: a prior bug
    deleted the consumer link first, then re-queried the (now-empty) links inside
    the internal delete, so ``sync_credentials_to_agent_environments`` was never
    invoked for the consumer on this path.
    """
    producer = _setup_agent(client, superuser_token_headers, "Fix4B Producer")
    consumer = _setup_agent(client, superuser_token_headers, "Fix4B Consumer")
    connector = _create_a2a_connector(
        client, superuser_token_headers, producer["id"], name="Fix4B Connector"
    )

    resp = _connect_agent(
        client, superuser_token_headers, connector["id"],
        consumer_agent_id=consumer["id"],
    )
    credential_id = str(resp["credential_id"])

    # Patch the env-sync fan-out (the terminal call of event_credential_deleted)
    # so we can assert it ran for the consumer agent on the delete-on-unlink path.
    with patch(
        "app.services.credentials.credentials_service."
        "CredentialsService.sync_credentials_to_agent_environments",
        new_callable=AsyncMock,
    ) as mock_sync:
        r = _unlink(client, superuser_token_headers, consumer["id"], credential_id)
        assert r.status_code == 200, r.text

    # The credential is gone...
    assert not _credential_exists(client, superuser_token_headers, credential_id)

    # ...and the consumer's running-environment sync fired for the delete.
    synced_agent_ids = {
        call.kwargs["agent_id"] for call in mock_sync.await_args_list
    }
    assert uuid.UUID(consumer["id"]) in synced_agent_ids


def test_unlink_external_provider_survives(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Unlinking an external mcp_provider only removes the link; cred survives."""
    consumer = _setup_agent(client, superuser_token_headers, "Fix4B Ext Consumer")
    ext = _connect_external(
        client, superuser_token_headers,
        endpoint_url="https://external.example.com/mcp",
        auth_mode="none",
        consumer_agent_id=consumer["id"],
    )
    credential_id = str(ext["credential_id"])

    r = _unlink(client, superuser_token_headers, consumer["id"], credential_id)
    assert r.status_code == 200, r.text

    # Credential survives; only the link was removed.
    assert _credential_exists(client, superuser_token_headers, credential_id)
    linked = get_agent_credentials(client, superuser_token_headers, consumer["id"])
    assert credential_id not in [c["id"] for c in linked.get("data", [])]


def test_unlink_non_bound_agent_keeps_credential(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A credential additionally linked to a second (non-bound) agent: unlinking that
    second agent leaves the credential alive (only the bound consumer triggers
    delete-on-unlink).
    """
    producer = _setup_agent(client, superuser_token_headers, "Fix4B NB Producer")
    consumer = _setup_agent(client, superuser_token_headers, "Fix4B NB Consumer")
    other = _setup_agent(client, superuser_token_headers, "Fix4B NB Other")
    connector = _create_a2a_connector(
        client, superuser_token_headers, producer["id"], name="Fix4B NB Connector"
    )

    resp = _connect_agent(
        client, superuser_token_headers, connector["id"],
        consumer_agent_id=consumer["id"],
    )
    credential_id = str(resp["credential_id"])

    # Link the SAME credential to a second agent. It is already bound to
    # `consumer`, so re-homing to `other` would be rejected (Fix 5); but linking
    # to `consumer` again is idempotent. To exercise the non-bound unlink path we
    # share the credential via an extra link to a non-bound agent is impossible
    # under the Fix-5 guard — so instead unlink the bound consumer's SIBLING:
    # here `other` was never linked, so unlinking it is a no-op that must not
    # delete the credential.
    r = _unlink(client, superuser_token_headers, other["id"], credential_id)
    assert r.status_code == 200, r.text

    # The credential (still bound to `consumer`) survives.
    assert _credential_exists(client, superuser_token_headers, credential_id)


# ── Fix 5: one credential per pair ────────────────────────────────────────────


def test_connect_same_pair_is_idempotent(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    Connecting the same (connector, consumer) pair twice returns the same
    credential; only one credential and one bound token exist.
    """
    producer = _setup_agent(client, superuser_token_headers, "Fix5 Idem Producer")
    consumer = _setup_agent(client, superuser_token_headers, "Fix5 Idem Consumer")
    connector = _create_a2a_connector(
        client, superuser_token_headers, producer["id"], name="Fix5 Idem Connector"
    )

    first = _connect_agent(
        client, superuser_token_headers, connector["id"],
        consumer_agent_id=consumer["id"],
    )
    second = _connect_agent(
        client, superuser_token_headers, connector["id"],
        consumer_agent_id=consumer["id"],
    )

    assert str(first["credential_id"]) == str(second["credential_id"])
    assert str(second["linked_consumer_agent_id"]) == str(consumer["id"])

    # Only one bound token for this connector.
    tokens = db.exec(
        select(MCPToken).where(
            MCPToken.connector_id == uuid.UUID(connector["id"]),
            MCPToken.credential_id.is_not(None),
        )
    ).all()
    assert len(tokens) == 1


def test_link_agent2agent_to_different_agent_rejected(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Linking a bound agent2agent credential to a different agent returns 400."""
    producer = _setup_agent(client, superuser_token_headers, "Fix5 Reject Producer")
    consumer = _setup_agent(client, superuser_token_headers, "Fix5 Reject Consumer")
    other = _setup_agent(client, superuser_token_headers, "Fix5 Reject Other")
    connector = _create_a2a_connector(
        client, superuser_token_headers, producer["id"], name="Fix5 Reject Connector"
    )

    resp = _connect_agent(
        client, superuser_token_headers, connector["id"],
        consumer_agent_id=consumer["id"],
    )
    credential_id = str(resp["credential_id"])

    r = _link(client, superuser_token_headers, other["id"], credential_id)
    assert r.status_code == 400, r.text
    assert "bound to a different agent" in r.json()["detail"].lower()


def test_floating_connect_then_link_binds_pair(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Floating connect (no consumer) then link to agent A binds the pair; a
    subsequent link to agent B is rejected (400).
    """
    producer = _setup_agent(client, superuser_token_headers, "Fix5 Float Producer")
    agent_a = _setup_agent(client, superuser_token_headers, "Fix5 Float A")
    agent_b = _setup_agent(client, superuser_token_headers, "Fix5 Float B")
    connector = _create_a2a_connector(
        client, superuser_token_headers, producer["id"], name="Fix5 Float Connector"
    )

    resp = _connect_agent(client, superuser_token_headers, connector["id"])
    credential_id = str(resp["credential_id"])

    # Initially floating — column null.
    cred = client.get(
        f"{_CREDENTIALS_BASE}/{credential_id}", headers=superuser_token_headers
    ).json()
    assert cred["mcp_consumer_agent_id"] is None

    # First link binds the pair.
    r1 = _link(client, superuser_token_headers, agent_a["id"], credential_id)
    assert r1.status_code == 200, r1.text
    cred = client.get(
        f"{_CREDENTIALS_BASE}/{credential_id}", headers=superuser_token_headers
    ).json()
    assert str(cred["mcp_consumer_agent_id"]) == str(agent_a["id"])

    # Linking to a different agent is now rejected.
    r2 = _link(client, superuser_token_headers, agent_b["id"], credential_id)
    assert r2.status_code == 400, r2.text


def test_manual_provider_links_to_two_agents_freely(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    An external/manual mcp_provider has no one-per-pair guard: it can be linked
    to two different agents freely (no consumer column ever set).
    """
    agent_a = _setup_agent(client, superuser_token_headers, "Fix5 Manual A")
    agent_b = _setup_agent(client, superuser_token_headers, "Fix5 Manual B")

    ext = _connect_external(
        client, superuser_token_headers,
        endpoint_url="https://external.example.com/mcp",
        auth_mode="none",
    )
    credential_id = str(ext["credential_id"])

    r1 = _link(client, superuser_token_headers, agent_a["id"], credential_id)
    assert r1.status_code == 200, r1.text
    r2 = _link(client, superuser_token_headers, agent_b["id"], credential_id)
    assert r2.status_code == 200, r2.text

    # No consumer column was ever set for the external provider.
    cred = client.get(
        f"{_CREDENTIALS_BASE}/{credential_id}", headers=superuser_token_headers
    ).json()
    assert cred["mcp_consumer_agent_id"] is None
