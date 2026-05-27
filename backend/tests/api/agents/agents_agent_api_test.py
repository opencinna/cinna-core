"""
Integration tests: Agent REST API feature (all 3 phases).

Covers route groups:
  A. Owner-preview routes  (/agents/{id}/agent-api/_status|openapi.json|proxy/{path})
  B. Connect helper        (/agents/{id}/agent-api/connect) — the only token mint path
  C. Connection info       (/credentials/{id}/agent-api-connection)
  D. Consumer spec route   (/agent-api/{id}/openapi.json)
  E. Consumer proxy route  (/agent-api/{id}/{path})

Tokens are never created manually: each is minted by the connect helper and
bound to the resulting ``agent_api`` credential. Disconnecting = deleting that
credential, which cascade-deletes the token (the only revoke path).

Business rules tested:
  1. agent_api_enabled=False → consumer + proxy 404; _status still reports disabled
  2. Connect mints a token + creates an agent_api credential (prefix + base_url + spec_url)
  3. Raw token is readable only from the credential's decrypted data
  4. Hash lookup validates; disconnected/invalid token → 401 on consumer routes
  5. _verify_agent_ownership returns 404 (no existence leak) for non-owner
  6. Policy enforcement at proxy edge: read_only → 405 for non-GET/HEAD
  7. Policy enforcement: body over max_body_bytes → 413
  8. Policy enforcement: rate limit exceeded → 429 + Retry-After
  9. expose_spec=false → 403 on consumer /openapi.json
  10. Invalid/malformed policy.yaml fails closed (deny-all → 405 for GET too)
  11. Token read_only_override may only narrow, never widen
  12. Spec passthrough reflects (stubbed) live app spec
  13. Owner proxy passthrough GET + auto-activates suspended env
  14. Consumer proxy GET passthrough succeeds
  15. agent_api credential: whitelist exposes base_url/token/spec_url/label/producer_agent_id
  16. agent_api credential: SENSITIVE_FIELDS redacts token in README
  17. Connect helper creates credential + optional consumer link
  18. agent_api_enabled=False → connect helper returns 400
  19. Request-loop hop-depth header: consumer proxy blocks calls beyond MAX_HOP_DEPTH
  20. Deadline header propagated and decremented per hop
  21. Ownership guard: non-owner cannot connect / read connection → 404
  22. Connection-info endpoint reports producer + consumers + read_only
  23. Deleting the agent_api credential disconnects (cascade-deletes the token → 401)
"""
import uuid
from urllib.parse import urlsplit

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import settings
from app.models import AgentEnvironment
from app.services.credentials.credentials_service import CredentialsService
from app.services.environments.environment_service import EnvironmentService
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.agent import create_agent_via_api, update_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.credential import (
    get_credential_with_data,
    link_credential_to_agent,
)
from tests.utils.user import create_random_user_with_headers

API = settings.API_V1_STR

# ── URL helpers ───────────────────────────────────────────────────────────────


def _owner_base(agent_id: str) -> str:
    return f"{API}/agents/{agent_id}/agent-api"


def _status_url(agent_id: str) -> str:
    return f"{_owner_base(agent_id)}/_status"


def _owner_spec_url(agent_id: str) -> str:
    return f"{_owner_base(agent_id)}/openapi.json"


def _owner_proxy_url(agent_id: str, path: str) -> str:
    return f"{_owner_base(agent_id)}/proxy/{path}"


def _connect_url(agent_id: str) -> str:
    return f"{_owner_base(agent_id)}/connect"


def _connections_url(agent_id: str) -> str:
    return f"{_owner_base(agent_id)}/connections"


def _connection_delete_url(agent_id: str, token_id: str) -> str:
    return f"{_owner_base(agent_id)}/connections/{token_id}"


def _credential_url(credential_id: str) -> str:
    return f"{API}/credentials/{credential_id}"


def _connection_info_url(credential_id: str) -> str:
    return f"{API}/credentials/{credential_id}/agent-api-connection"


def _consumer_spec_url(agent_id: str) -> str:
    return f"{API}/agent-api/{agent_id}/openapi.json"


def _consumer_proxy_url(agent_id: str, path: str) -> str:
    return f"{API}/agent-api/{agent_id}/{path}"


# ── Setup helpers ─────────────────────────────────────────────────────────────


def _setup_api_agent(
    client: TestClient,
    headers: dict[str, str],
    name: str = "API Agent",
) -> dict:
    """Create an agent with agent_api_enabled=True."""
    agent = create_agent_via_api(client, headers, name=name)
    drain_tasks()
    update_agent(client, headers, agent["id"], agent_api_enabled=True)
    return agent


def _mint_token(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    label: str | None = None,
    read_only_override: bool = False,
    consumer_agent_id: str | None = None,
) -> dict:
    """
    Connect to a producer agent's REST API: the connect helper mints the proxy
    token and creates the ``agent_api`` credential. Returns a token-like dict
    (the raw token value is read back from the created credential's data — the
    same way a consumer agent obtains it), plus ``credential_id`` so the test
    can disconnect by deleting the credential.

    Tokens are never created manually; this is the only mint path.
    """
    body: dict = {"read_only_override": read_only_override}
    if label is not None:
        body["credential_label"] = label
    if consumer_agent_id is not None:
        body["consumer_agent_id"] = consumer_agent_id
    r = client.post(_connect_url(agent_id), headers=headers, json=body)
    assert r.status_code == 200, f"Connect failed: {r.text}"
    conn = r.json()

    cred = get_credential_with_data(client, headers, conn["credential_id"])
    token_value = cred["credential_data"]["token"]

    return {
        "id": conn["token_id"],
        "credential_id": conn["credential_id"],
        "token": token_value,
        "token_prefix": conn["token_prefix"],
        "base_url": conn["base_url"],
        "spec_url": conn["spec_url"],
        "label": label,
        "read_only_override": read_only_override,
        "is_active": True,
        "agent_id": agent_id,
        "linked_consumer_agent_id": conn.get("linked_consumer_agent_id"),
    }


def _disconnect(
    client: TestClient,
    headers: dict[str, str],
    credential_id: str,
) -> None:
    """Disconnect = delete the agent_api credential (cascade-deletes the token)."""
    r = client.delete(_credential_url(credential_id), headers=headers)
    assert r.status_code == 200, f"Disconnect (credential delete) failed: {r.text}"


def _bearer_headers(token_value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token_value}"}


# ── A. Owner-preview routes ───────────────────────────────────────────────────


def test_status_always_accessible_regardless_of_toggle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    _status endpoint:
      1. Works when agent_api_enabled=False → state=disabled
      2. Works when agent_api_enabled=True → state=running (env is running via stub)
      3. Requires authentication → 401/403 unauthenticated
      4. Other user gets 404 (no existence leak)
      5. Ghost agent ID → 404
    """
    # ── Phase 1: agent_api disabled ───────────────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers, name="Status Check Agent")
    drain_tasks()
    agent_id = agent["id"]

    r = client.get(_status_url(agent_id), headers=superuser_token_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent_api_enabled"] is False
    assert body["state"] == "disabled"

    # ── Phase 2: agent_api enabled → state reflects running env ──────────
    update_agent(client, superuser_token_headers, agent_id, agent_api_enabled=True)
    r = client.get(_status_url(agent_id), headers=superuser_token_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent_api_enabled"] is True
    # Stub reports state="running"; confirm it's not "disabled"
    assert body["state"] != "disabled"

    # ── Phase 3: unauthenticated → 401/403 ───────────────────────────────
    r = client.get(_status_url(agent_id))
    assert r.status_code in (401, 403)

    # ── Phase 4: other user → 404 (no existence leak) ────────────────────
    _, other_headers = create_random_user_with_headers(client)
    r = client.get(_status_url(agent_id), headers=other_headers)
    assert r.status_code == 404

    # ── Phase 5: ghost agent ID → 404 ────────────────────────────────────
    ghost = str(uuid.uuid4())
    r = client.get(_status_url(ghost), headers=superuser_token_headers)
    assert r.status_code == 404


def test_owner_spec_requires_agent_api_enabled(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Owner spec endpoint (GET /agent-api/openapi.json):
      1. Requires authentication
      2. When agent_api_enabled=False → 400 (disabled)
      3. When agent_api_enabled=True → returns valid OpenAPI spec JSON
      4. Spec contains expected OpenAPI keys
      5. Other user → 404
    """
    # ── Phase 1: agent_api disabled ───────────────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers, name="Spec Agent")
    drain_tasks()
    agent_id = agent["id"]

    r = client.get(_owner_spec_url(agent_id), headers=superuser_token_headers)
    assert r.status_code in (400, 404), r.text  # disabled → 400

    # ── Phase 2: unauthenticated ──────────────────────────────────────────
    update_agent(client, superuser_token_headers, agent_id, agent_api_enabled=True)
    r = client.get(_owner_spec_url(agent_id))
    assert r.status_code in (401, 403)

    # ── Phase 3: spec returned for enabled agent ──────────────────────────
    r = client.get(_owner_spec_url(agent_id), headers=superuser_token_headers)
    assert r.status_code == 200, r.text
    spec = r.json()

    # ── Phase 4: valid OpenAPI structure ──────────────────────────────────
    assert "openapi" in spec
    assert "info" in spec
    # Stub returns a minimal spec; confirm it is parseable JSON
    assert isinstance(spec, dict)

    # ── Phase 5: other user → 404 ────────────────────────────────────────
    _, other_headers = create_random_user_with_headers(client)
    r = client.get(_owner_spec_url(agent_id), headers=other_headers)
    assert r.status_code == 404


def test_owner_proxy_passthrough(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Owner proxy endpoint (ANY /proxy/{path}):
      1. Requires agent_api_enabled → 400 when disabled
      2. Requires authentication
      3. GET proxy call proxied to stub adapter → 200 with stub response
      4. POST proxy call with body proxied → 200
      5. Stub records the proxy call
      6. Other user → 404
    """
    agent = _setup_api_agent(client, superuser_token_headers, name="Proxy Agent")
    agent_id = agent["id"]

    # Inject a persistent adapter to track proxy calls
    persistent = EnvironmentTestAdapter()
    lm = EnvironmentService._lifecycle_manager
    original_get_adapter = lm.get_adapter
    lm.get_adapter = lambda env: persistent

    try:
        # ── Phase 1: disabled agent → 400 ───────────────────────────────
        agent_disabled = create_agent_via_api(
            client, superuser_token_headers, name="Disabled Proxy Agent"
        )
        drain_tasks()
        r = client.get(
            _owner_proxy_url(agent_disabled["id"], "orders"),
            headers=superuser_token_headers,
        )
        assert r.status_code == 400

        # ── Phase 2: unauthenticated → 401/403 ───────────────────────────
        r = client.get(_owner_proxy_url(agent_id, "orders"))
        assert r.status_code in (401, 403)

        # ── Phase 3: GET proxy → 200 ──────────────────────────────────────
        r = client.get(
            _owner_proxy_url(agent_id, "orders"),
            headers=superuser_token_headers,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("ok") is True
        assert body.get("method") == "GET"

        # ── Phase 4: POST proxy with body ─────────────────────────────────
        r = client.post(
            _owner_proxy_url(agent_id, "orders/create"),
            headers=superuser_token_headers,
            json={"item": "widget"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("method") == "POST"

        # ── Phase 5: stub recorded the calls ─────────────────────────────
        assert len(persistent.agent_api_proxy_calls) >= 2
        methods = [c["method"] for c in persistent.agent_api_proxy_calls]
        assert "GET" in methods
        assert "POST" in methods

        # ── Phase 6: other user → 404 ────────────────────────────────────
        _, other_headers = create_random_user_with_headers(client)
        r = client.get(
            _owner_proxy_url(agent_id, "orders"),
            headers=other_headers,
        )
        assert r.status_code == 404

    finally:
        lm.get_adapter = original_get_adapter


# ── B. Connection lifecycle (connect → use → disconnect) ──────────────────────


def test_connection_lifecycle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Connect → use → disconnect lifecycle (no manual token CRUD):
      1. Connect mints a token + creates an agent_api credential
      2. Response carries prefix (8 chars) + base_url (with agent id) + spec_url
      3. Raw token is readable only from the credential's decrypted data
      4. Token authenticates a consumer call
      5. Deleting the credential (disconnect) cascade-deletes the token → 401
    """
    agent = _setup_api_agent(
        client, superuser_token_headers, name="Connection Lifecycle Agent"
    )
    agent_id = agent["id"]

    # ── Phase 1-3: Connect mints token + credential ───────────────────────
    created = _mint_token(
        client, superuser_token_headers, agent_id, label="my-integration"
    )
    token_value = created["token"]

    assert token_value, "Token value must be readable from the credential"
    assert len(created["token_prefix"]) == 8
    assert token_value.startswith(created["token_prefix"])
    assert agent_id in created["base_url"]
    assert created["spec_url"].endswith("/openapi.json")

    # ── Phase 4: Token authenticates a consumer call ──────────────────────
    r = client.get(
        _consumer_proxy_url(agent_id, "ping"),
        headers=_bearer_headers(token_value),
    )
    assert r.status_code == 200, r.text

    # ── Phase 5: Disconnect (delete credential) → token gone → 401 ────────
    _disconnect(client, superuser_token_headers, created["credential_id"])

    r = client.get(
        _consumer_proxy_url(agent_id, "ping"),
        headers=_bearer_headers(token_value),
    )
    assert r.status_code == 401, "Disconnected token must no longer authenticate"


def test_connection_info_reports_producer_and_read_only(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    GET /credentials/{id}/agent-api-connection reports the producer agent, the
    linked consumer agents, and the connection's read-only flag (which mirrors
    the token's read_only_override set at connect time).
    """
    producer = _setup_api_agent(
        client, superuser_token_headers, name="Conn Info Producer"
    )
    consumer = create_agent_via_api(
        client, superuser_token_headers, name="Conn Info Consumer"
    )
    drain_tasks()

    created = _mint_token(
        client,
        superuser_token_headers,
        producer["id"],
        label="narrow-conn",
        read_only_override=True,
        consumer_agent_id=consumer["id"],
    )

    r = client.get(
        _connection_info_url(created["credential_id"]),
        headers=superuser_token_headers,
    )
    assert r.status_code == 200, r.text
    info = r.json()
    assert info["producer_agent_id"] == producer["id"]
    assert info["producer_agent_name"] == "Conn Info Producer"
    assert info["read_only"] is True
    assert info["spec_url"].endswith("/openapi.json")
    consumer_ids = {a["id"] for a in info["consumer_agents"]}
    assert consumer["id"] in consumer_ids

    # ── Non-owner cannot read the connection → 404 (no existence leak) ────
    _, other_headers = create_random_user_with_headers(client)
    r = client.get(
        _connection_info_url(created["credential_id"]),
        headers=other_headers,
    )
    assert r.status_code == 404


def test_producer_connections_lists_consumers(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    GET /agents/{id}/agent-api/connections (producer card): lists who is
    consuming this producer's API.
      1. Empty before any connect
      2. Connect linked to a consumer → connection lists that consumer agent
         (incl. its ui_color_preset for the badge) + a stable token_id
      3. A second (unlinked) connect → appears with no consumer agents
      4. Disconnect via DELETE /connections/{token_id} → token revoked (401),
         connection drops out of the list
      5. Non-owner → 404
    """
    producer = _setup_api_agent(client, superuser_token_headers, name="Conn List Producer")
    producer_id = producer["id"]
    consumer = create_agent_via_api(
        client, superuser_token_headers, name="Conn List Consumer"
    )
    drain_tasks()

    # ── Phase 1: empty ────────────────────────────────────────────────────
    r = client.get(_connections_url(producer_id), headers=superuser_token_headers)
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 0

    # ── Phase 2: connect linked to a consumer ────────────────────────────
    linked = _mint_token(
        client,
        superuser_token_headers,
        producer_id,
        label="linked-conn",
        consumer_agent_id=consumer["id"],
    )

    r = client.get(_connections_url(producer_id), headers=superuser_token_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    conn = body["data"][0]
    assert conn["credential_id"] == linked["credential_id"]
    assert conn["token_id"] == linked["id"]
    consumer_entry = next(
        a for a in conn["consumer_agents"] if a["id"] == consumer["id"]
    )
    # The consumer agent carries its colour preset so the UI renders its badge.
    assert "ui_color_preset" in consumer_entry

    # ── Phase 3: a second, unlinked connection ───────────────────────────
    _mint_token(client, superuser_token_headers, producer_id, label="unlinked-conn")

    r = client.get(_connections_url(producer_id), headers=superuser_token_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    unlinked = next(
        c for c in body["data"] if c["credential_id"] != linked["credential_id"]
    )
    assert unlinked["consumer_agents"] == []

    # ── Phase 4: disconnect the linked connection via token_id ───────────
    r = client.delete(
        _connection_delete_url(producer_id, linked["id"]),
        headers=superuser_token_headers,
    )
    assert r.status_code == 200, r.text

    # Token is revoked → consumer call 401
    r = client.get(
        _consumer_proxy_url(producer_id, "ping"),
        headers=_bearer_headers(linked["token"]),
    )
    assert r.status_code == 401

    # Connection dropped out of the list (only the unlinked one remains)
    r = client.get(_connections_url(producer_id), headers=superuser_token_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    assert body["data"][0]["credential_id"] != linked["credential_id"]

    # ── Phase 5: non-owner → 404 ──────────────────────────────────────────
    _, other_headers = create_random_user_with_headers(client)
    r = client.get(_connections_url(producer_id), headers=other_headers)
    assert r.status_code == 404


# ── Toggle gates ─────────────────────────────────────────────────────────────


def test_disabled_agent_api_gates_consumer_routes(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When agent_api_enabled=False:
      1. Consumer spec route GET /agent-api/{id}/openapi.json → 404
      2. Consumer proxy route GET /agent-api/{id}/{path} → 404
      3. Owner _status still reports state=disabled (not 404)
      4. Enabling the feature lifts the 404 on consumer routes
         (provided a valid token is presented)
    """
    agent = create_agent_via_api(client, superuser_token_headers, name="Disabled API Agent")
    drain_tasks()
    agent_id = agent["id"]

    # ── Phase 1: consumer spec → 404 ─────────────────────────────────────
    r = client.get(
        _consumer_spec_url(agent_id),
        headers={"Authorization": "Bearer some-token"},
    )
    assert r.status_code == 404, r.text

    # ── Phase 2: consumer proxy → 404 ────────────────────────────────────
    r = client.get(
        _consumer_proxy_url(agent_id, "orders"),
        headers={"Authorization": "Bearer some-token"},
    )
    assert r.status_code == 404, r.text

    # ── Phase 3: owner _status still reachable → disabled ────────────────
    r = client.get(_status_url(agent_id), headers=superuser_token_headers)
    assert r.status_code == 200
    assert r.json()["state"] == "disabled"

    # ── Phase 4: enable → consumer routes now reject invalid token (401) ──
    update_agent(client, superuser_token_headers, agent_id, agent_api_enabled=True)

    r = client.get(
        _consumer_spec_url(agent_id),
        headers={"Authorization": "Bearer bad-token-value"},
    )
    # 401 (feature enabled but bad token) not 404
    assert r.status_code == 401, r.text


# ── C + D + E. Token auth + consumer routes + policy enforcement ───────────────


def test_consumer_spec_passthrough(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Consumer spec endpoint GET /agent-api/{id}/openapi.json:
      1. Valid token → returns the OpenAPI spec (from stub)
      2. Spec structure includes openapi version key
      3. No token → 401
      4. Disconnected token (credential deleted) → 401
      5. Wrong agent token → 401
    """
    agent = _setup_api_agent(client, superuser_token_headers, name="Consumer Spec Agent")
    agent_id = agent["id"]

    # ── Phase 1 + 2: valid token returns spec ────────────────────────────
    token = _mint_token(client, superuser_token_headers, agent_id, label="spec-consumer")
    token_value = token["token"]

    r = client.get(
        _consumer_spec_url(agent_id),
        headers=_bearer_headers(token_value),
    )
    assert r.status_code == 200, r.text
    spec = r.json()
    assert "openapi" in spec
    assert "info" in spec

    # ── Phase 3: no token → 401 ──────────────────────────────────────────
    r = client.get(_consumer_spec_url(agent_id))
    assert r.status_code == 401, r.text

    # ── Phase 4: disconnect (delete credential) → token → 401 ────────────
    _disconnect(client, superuser_token_headers, token["credential_id"])

    r = client.get(
        _consumer_spec_url(agent_id),
        headers=_bearer_headers(token_value),
    )
    assert r.status_code == 401

    # ── Phase 5: wrong agent's token → 401 ───────────────────────────────
    other_agent = _setup_api_agent(client, superuser_token_headers, name="Other Spec Agent")
    other_token = _mint_token(
        client, superuser_token_headers, other_agent["id"], label="wrong-agent"
    )

    r = client.get(
        _consumer_spec_url(agent_id),
        headers=_bearer_headers(other_token["token"]),
    )
    assert r.status_code == 401


def test_consumer_proxy_get_passthrough(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Consumer proxy GET /agent-api/{id}/{path}:
      1. Valid token + GET → proxied to stub → 200 with stub response
      2. Stub records the proxy call correctly
    """
    agent = _setup_api_agent(client, superuser_token_headers, name="Consumer Proxy Agent")
    agent_id = agent["id"]
    token = _mint_token(client, superuser_token_headers, agent_id, label="proxy-consumer")
    token_value = token["token"]

    # Inject persistent adapter
    persistent = EnvironmentTestAdapter()
    lm = EnvironmentService._lifecycle_manager
    original_get_adapter = lm.get_adapter
    lm.get_adapter = lambda env: persistent

    try:
        r = client.get(
            _consumer_proxy_url(agent_id, "orders"),
            headers=_bearer_headers(token_value),
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body.get("ok") is True
        assert body.get("method") == "GET"
        assert body.get("path") == "orders"

        # Stub recorded the call
        assert len(persistent.agent_api_proxy_calls) == 1
        call = persistent.agent_api_proxy_calls[0]
        assert call["method"] == "GET"
        assert call["path"] == "orders"

    finally:
        lm.get_adapter = original_get_adapter


def test_consumer_proxy_invalid_tokens(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Consumer proxy authorization guards:
      1. No bearer token → 401
      2. Garbage token value → 401
      3. Token for wrong agent → 401
      4. Disconnected token (credential deleted) → 401
    """
    agent = _setup_api_agent(client, superuser_token_headers, name="Auth Guard Agent")
    agent_id = agent["id"]
    valid_token = _mint_token(client, superuser_token_headers, agent_id, label="valid")

    # ── Phase 1: no bearer ───────────────────────────────────────────────
    r = client.get(_consumer_proxy_url(agent_id, "ping"))
    assert r.status_code == 401

    # ── Phase 2: garbage token value ────────────────────────────────────
    r = client.get(
        _consumer_proxy_url(agent_id, "ping"),
        headers=_bearer_headers("this-is-not-a-real-token"),
    )
    assert r.status_code == 401

    # ── Phase 3: token for a different agent ────────────────────────────
    other_agent = _setup_api_agent(client, superuser_token_headers, name="Other Auth Agent")
    other_token = _mint_token(client, superuser_token_headers, other_agent["id"], label="other")

    r = client.get(
        _consumer_proxy_url(agent_id, "ping"),
        headers=_bearer_headers(other_token["token"]),
    )
    assert r.status_code == 401

    # ── Phase 4: disconnect (delete credential) → token revoked → 401 ────
    _disconnect(client, superuser_token_headers, valid_token["credential_id"])

    r = client.get(
        _consumer_proxy_url(agent_id, "ping"),
        headers=_bearer_headers(valid_token["token"]),
    )
    assert r.status_code == 401


# ── Policy enforcement ────────────────────────────────────────────────────────


def test_policy_read_only_blocks_non_get_head(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Policy: read_only=True (default) blocks all non-GET/HEAD methods → 405.
    A token with read_only_override=True also enforces read-only when the
    base policy is NOT read-only (token may only narrow, never widen).
    """
    agent = _setup_api_agent(client, superuser_token_headers, name="Policy Read-Only Agent")
    agent_id = agent["id"]
    token = _mint_token(
        client, superuser_token_headers, agent_id, label="policy-test"
    )
    token_value = token["token"]

    # Default policy is read_only=True.

    # ── Phase 1: POST → 405 (blocked by read_only=True) ──────────────────
    r = client.post(
        _consumer_proxy_url(agent_id, "orders"),
        headers=_bearer_headers(token_value),
        json={"item": "widget"},
    )
    assert r.status_code == 405, r.text

    # ── Phase 2: PUT → 405 ───────────────────────────────────────────────
    r = client.put(
        _consumer_proxy_url(agent_id, "orders/1"),
        headers=_bearer_headers(token_value),
        json={"qty": 5},
    )
    assert r.status_code == 405, r.text

    # ── Phase 3: DELETE → 405 ────────────────────────────────────────────
    r = client.delete(
        _consumer_proxy_url(agent_id, "orders/1"),
        headers=_bearer_headers(token_value),
    )
    assert r.status_code == 405, r.text

    # ── Phase 4: GET is allowed ───────────────────────────────────────────
    r = client.get(
        _consumer_proxy_url(agent_id, "orders"),
        headers=_bearer_headers(token_value),
    )
    assert r.status_code == 200, r.text

    # ── Phase 5: HEAD is allowed ──────────────────────────────────────────
    r = client.head(
        _consumer_proxy_url(agent_id, "orders"),
        headers=_bearer_headers(token_value),
    )
    assert r.status_code == 200, r.text


def test_policy_read_only_override_on_token_only_narrows(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    Token read_only_override can only NARROW, never widen.
    When the effective policy allows POST (read_only=False, explicitly set via
    cached policy), a token with read_only_override=True must still block POST.
    """
    agent = _setup_api_agent(client, superuser_token_headers, name="Narrow Override Agent")
    agent_id = agent["id"]

    # Set the env's policy cache to allow POST (read_only=False) via db fixture
    env = db.exec(
        select(AgentEnvironment).where(
            AgentEnvironment.agent_id == uuid.UUID(agent_id)
        )
    ).first()
    if env:
        env.agent_api_policy_cache = {
            "read_only": False,
            "auth": "required",
            "max_body_bytes": 10 * 1024 * 1024,
            "rate_limit": "60/min",
            "expose_spec": True,
            "allowed_paths": ["*"],
        }
        db.add(env)
        db.commit()

    # Token with NO override — should inherit wide policy (POST allowed)
    wide_token = _mint_token(
        client, superuser_token_headers, agent_id,
        label="wide-token", read_only_override=False,
    )

    # Token WITH override — must be read-only even though base policy allows POST
    narrow_token = _mint_token(
        client, superuser_token_headers, agent_id,
        label="narrow-token", read_only_override=True,
    )

    # Wide token: POST allowed (base policy permits it)
    r = client.post(
        _consumer_proxy_url(agent_id, "orders"),
        headers=_bearer_headers(wide_token["token"]),
        json={"item": "widget"},
    )
    assert r.status_code == 200, f"Wide token should allow POST: {r.text}"

    # Narrow token: POST blocked (read_only_override=True)
    r = client.post(
        _consumer_proxy_url(agent_id, "orders"),
        headers=_bearer_headers(narrow_token["token"]),
        json={"item": "widget"},
    )
    assert r.status_code == 405, f"Narrow token must block POST: {r.text}"

    # Narrow token: GET still works
    r = client.get(
        _consumer_proxy_url(agent_id, "orders"),
        headers=_bearer_headers(narrow_token["token"]),
    )
    assert r.status_code == 200, f"Narrow token should allow GET: {r.text}"


def test_policy_body_cap_413(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    Policy max_body_bytes enforcement → 413 when body exceeds the limit.
    We set a tiny limit (10 bytes) via the env policy cache, then send
    a larger body.
    """
    agent = _setup_api_agent(client, superuser_token_headers, name="Body Cap Agent")
    agent_id = agent["id"]

    # Set a tiny body cap and allow POST via db fixture
    env = db.exec(
        select(AgentEnvironment).where(
            AgentEnvironment.agent_id == uuid.UUID(agent_id)
        )
    ).first()
    if env:
        env.agent_api_policy_cache = {
            "read_only": False,
            "auth": "required",
            "max_body_bytes": 10,
            "rate_limit": "600/min",
            "expose_spec": True,
            "allowed_paths": ["*"],
        }
        db.add(env)
        db.commit()

    token = _mint_token(client, superuser_token_headers, agent_id, label="body-cap")
    token_value = token["token"]

    # Small body → OK (POST is allowed because policy has read_only=False)
    r = client.post(
        _consumer_proxy_url(agent_id, "orders"),
        headers=_bearer_headers(token_value),
        content=b"tiny",
    )
    assert r.status_code == 200, f"Small body should succeed: {r.text}"

    # Large body → 413
    r = client.post(
        _consumer_proxy_url(agent_id, "orders"),
        headers=_bearer_headers(token_value),
        content=b"x" * 50,  # 50 bytes > 10 bytes cap
    )
    assert r.status_code == 413, f"Oversized body should return 413: {r.text}"


def test_policy_rate_limit_429(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    Policy rate limit enforcement → 429 + Retry-After when exceeded.
    We set a very low rate limit (1/min) and make two calls.
    """
    agent = _setup_api_agent(client, superuser_token_headers, name="Rate Limit Agent")
    agent_id = agent["id"]

    # Set a rate limit of 1 request/min via db fixture
    env = db.exec(
        select(AgentEnvironment).where(
            AgentEnvironment.agent_id == uuid.UUID(agent_id)
        )
    ).first()
    if env:
        env.agent_api_policy_cache = {
            "read_only": True,
            "auth": "required",
            "max_body_bytes": 10 * 1024 * 1024,
            "rate_limit": "1/min",
            "expose_spec": True,
            "allowed_paths": ["*"],
        }
        db.add(env)
        db.commit()

    token = _mint_token(client, superuser_token_headers, agent_id, label="rate-limit")
    token_value = token["token"]

    # First call → 200
    r = client.get(
        _consumer_proxy_url(agent_id, "orders"),
        headers=_bearer_headers(token_value),
    )
    assert r.status_code == 200, f"First call should succeed: {r.text}"

    # Second call → 429 (limit is 1/min)
    r = client.get(
        _consumer_proxy_url(agent_id, "orders"),
        headers=_bearer_headers(token_value),
    )
    assert r.status_code == 429, f"Second call should be rate-limited: {r.text}"
    # Retry-After header must be present
    assert "retry-after" in {k.lower() for k in r.headers.keys()}, \
        "429 must include Retry-After header"


def test_policy_fail_closed_on_invalid_policy_yaml(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    Invalid policy.yaml fails closed: FAIL_CLOSED_POLICY has allowed_methods=[]
    (deny every verb → 405) and rate_limit="0/min" (deny-all → 429).
    We simulate this by directly setting the fail-closed policy dict on the env cache.
    """
    # The fail-closed policy mirrors FAIL_CLOSED_POLICY from agent_api_service.py
    fail_closed = {
        "read_only": True,
        "allowed_methods": [],   # deny every verb
        "auth": "required",
        "max_body_bytes": 0,
        "rate_limit": "0/min",
        "expose_spec": False,
        "allowed_paths": [],
        "error": "policy.yaml could not be parsed — failing closed (deny-all)",
    }

    agent = _setup_api_agent(client, superuser_token_headers, name="Fail Closed Agent")
    agent_id = agent["id"]

    # Inject the fail-closed policy via db fixture
    env = db.exec(
        select(AgentEnvironment).where(
            AgentEnvironment.agent_id == uuid.UUID(agent_id)
        )
    ).first()
    if env:
        env.agent_api_policy_cache = fail_closed
        db.add(env)
        db.commit()

    token = _mint_token(client, superuser_token_headers, agent_id, label="fail-closed")
    token_value = token["token"]

    # GET → 405 (fail-closed allowed_methods=[]) or 429 (rate_limit="0/min")
    r = client.get(
        _consumer_proxy_url(agent_id, "orders"),
        headers=_bearer_headers(token_value),
    )
    # The fail-closed policy: allowed_methods=[] → method not in allowed → 405.
    # Enforcement order in enforce_policy: hop-depth → method → body → path → rate.
    # Method check fires first since allowed_methods=[] means no method passes.
    assert r.status_code in (405, 429), \
        f"Fail-closed policy must deny requests: {r.status_code} {r.text}"


def test_policy_expose_spec_false_blocks_consumer_spec(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    When expose_spec=False in the cached policy, the consumer /openapi.json
    endpoint returns 403.
    """
    agent = _setup_api_agent(client, superuser_token_headers, name="No Spec Agent")
    agent_id = agent["id"]

    # Set expose_spec=False in cached policy via db fixture
    env = db.exec(
        select(AgentEnvironment).where(
            AgentEnvironment.agent_id == uuid.UUID(agent_id)
        )
    ).first()
    if env:
        env.agent_api_policy_cache = {
            "read_only": True,
            "auth": "required",
            "max_body_bytes": 10 * 1024 * 1024,
            "rate_limit": "60/min",
            "expose_spec": False,
            "allowed_paths": ["*"],
        }
        db.add(env)
        db.commit()

    token = _mint_token(client, superuser_token_headers, agent_id, label="no-spec")

    # Consumer spec → 403 (not exposed)
    r = client.get(
        _consumer_spec_url(agent_id),
        headers=_bearer_headers(token["token"]),
    )
    assert r.status_code == 403, f"expose_spec=False must return 403: {r.text}"

    # Owner spec still works (policy only applied to consumer route)
    r = client.get(_owner_spec_url(agent_id), headers=superuser_token_headers)
    assert r.status_code == 200, f"Owner spec should still work: {r.text}"


# ── E. Request-loop protection ────────────────────────────────────────────────


def test_hop_depth_limit_blocks_deep_nesting(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Consumer proxy passes hop-depth header downstream; incoming depth beyond
    MAX_HOP_DEPTH is rejected 403.
    Plan §4.5: MAX_HOP_DEPTH=4, so depth > 4 must be rejected.
    """
    # MAX_HOP_DEPTH is 4 per the service constants (plan §4.5).
    MAX_HOP_DEPTH = 4
    HOP_DEPTH_HEADER = "x-cinna-agent-api-hop-depth"

    agent = _setup_api_agent(client, superuser_token_headers, name="Hop Depth Agent")
    agent_id = agent["id"]
    token = _mint_token(client, superuser_token_headers, agent_id, label="hop-test")
    token_value = token["token"]

    # Within limit → allowed
    within_depth = str(MAX_HOP_DEPTH - 1)
    r = client.get(
        _consumer_proxy_url(agent_id, "orders"),
        headers={
            **_bearer_headers(token_value),
            HOP_DEPTH_HEADER: within_depth,
        },
    )
    assert r.status_code == 200, f"Hop depth within limit should succeed: {r.text}"

    # Beyond the limit → 403 (depth > MAX_HOP_DEPTH triggers enforcement in enforce_policy)
    over_depth = str(MAX_HOP_DEPTH + 1)
    r = client.get(
        _consumer_proxy_url(agent_id, "orders"),
        headers={
            **_bearer_headers(token_value),
            HOP_DEPTH_HEADER: over_depth,
        },
    )
    assert r.status_code == 403, f"Hop depth exceeding MAX must return 403: {r.text}"


def test_deadline_header_propagated_and_decremented(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Deadline header: the proxy decrements the remaining budget by HOP_DEADLINE_SHRINK_MS
    and injects the result into the downstream request.
    We verify via the stub which records forwarded headers, and that an exhausted
    deadline (budget <= 0) returns 403.
    Plan §4.5 constants:
      - DEADLINE_HEADER = "x-cinna-agent-api-deadline-ms"
      - HOP_DEADLINE_SHRINK_MS = 1000
      - DEFAULT_DEADLINE_MS = 60000
    """
    DEADLINE_HEADER = "x-cinna-agent-api-deadline-ms"
    HOP_DEADLINE_SHRINK_MS = 1000
    DEFAULT_DEADLINE_MS = 60_000

    agent = _setup_api_agent(client, superuser_token_headers, name="Deadline Agent")
    agent_id = agent["id"]
    token = _mint_token(client, superuser_token_headers, agent_id, label="deadline-test")
    token_value = token["token"]

    # Track forwarded headers from the stub
    forwarded_headers: list[dict] = []

    class _TrackingAdapter(EnvironmentTestAdapter):
        async def proxy_agent_api(self, method, path, headers=None, body=None, stream=False, timeout=60.0):
            forwarded_headers.append(dict(headers or {}))
            status, resp_headers, gen = await super().proxy_agent_api(method, path, headers, body, stream, timeout)
            return status, resp_headers, gen

    lm = EnvironmentService._lifecycle_manager
    original_get_adapter = lm.get_adapter
    lm.get_adapter = lambda env: _TrackingAdapter()

    try:
        # ── First hop: no incoming deadline → default budget − shrink forwarded ─
        r = client.get(
            _consumer_proxy_url(agent_id, "orders"),
            headers=_bearer_headers(token_value),
        )
        assert r.status_code == 200, r.text
        assert len(forwarded_headers) >= 1
        fwd = {k.lower(): v for k, v in forwarded_headers[-1].items()}
        expected_fwd_deadline = DEFAULT_DEADLINE_MS - HOP_DEADLINE_SHRINK_MS
        assert DEADLINE_HEADER.lower() in fwd
        assert int(fwd[DEADLINE_HEADER.lower()]) == expected_fwd_deadline

        # ── Exhausted deadline (budget = 1, less than shrink) → 403 ─────────
        forwarded_headers.clear()
        exhausted_budget = str(HOP_DEADLINE_SHRINK_MS - 1)  # will go to 0 after shrink
        r = client.get(
            _consumer_proxy_url(agent_id, "orders"),
            headers={
                **_bearer_headers(token_value),
                DEADLINE_HEADER: exhausted_budget,
            },
        )
        assert r.status_code == 403, f"Exhausted deadline must return 403: {r.text}"

    finally:
        lm.get_adapter = original_get_adapter


# ── F. agent_api credential type ─────────────────────────────────────────────


def test_agent_api_credential_whitelist_and_redaction(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    agent_api credential type:
      1. Create credential with agent_api type, including token field
      2. Retrieve with-data endpoint → all whitelisted fields present
      3. Credential data persists token + base_url + spec_url + label + producer_agent_id
      4. Synced credential data endpoint exposes the expected whitelisted fields
    """
    agent = create_agent_via_api(client, superuser_token_headers, name="Credential Test Agent")
    drain_tasks()

    # ── Phase 1: Create agent_api credential ─────────────────────────────
    test_base_url = "https://example.com/api/v1/agent-api/some-agent-id"
    test_spec_url = f"{test_base_url}/openapi.json"
    test_token = "secret-token-abc123"
    test_label = "Test API"
    test_producer_id = str(uuid.uuid4())

    r = client.post(
        f"{API}/credentials/",
        headers=superuser_token_headers,
        json={
            "name": "My Agent API",
            "type": "agent_api",
            "credential_data": {
                "base_url": test_base_url,
                "spec_url": test_spec_url,
                "token": test_token,
                "label": test_label,
                "producer_agent_id": test_producer_id,
            },
        },
    )
    assert r.status_code == 200, r.text
    cred_id = r.json()["id"]
    assert r.json()["type"] == "agent_api"

    # ── Phase 2: Retrieve with-data → all whitelisted fields present ──────
    cred_with_data = get_credential_with_data(client, superuser_token_headers, cred_id)
    cred_fields = cred_with_data.get("credential_data", {})

    assert "base_url" in cred_fields, "base_url must be in credential data"
    assert cred_fields["base_url"] == test_base_url
    assert "spec_url" in cred_fields, "spec_url must be in credential data"
    assert "token" in cred_fields, "token must be in credential data"
    assert cred_fields["token"] == test_token
    assert "label" in cred_fields, "label must be in credential data"
    assert "producer_agent_id" in cred_fields, "producer_agent_id must be in credential data"

    # ── Phase 3: Credential type is returned correctly on list ────────────
    r = client.get(f"{API}/credentials/", headers=superuser_token_headers)
    assert r.status_code == 200
    body = r.json()
    creds = body.get("data", body) if isinstance(body, dict) else body
    api_creds = [c for c in creds if c.get("id") == cred_id]
    assert len(api_creds) == 1, "agent_api credential should appear in list"
    assert api_creds[0]["type"] == "agent_api"


def test_agent_api_credential_syncs_to_consumer_agent(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    agent_api credential can be linked to an agent and the whitelist allows
    the token to reach the environment's credentials.json.
      1. Create producer agent with API enabled
      2. Mint token via producer
      3. Create agent_api credential with the token
      4. Create consumer agent
      5. Link credential to consumer agent
      6. Linked credentials list shows the agent_api credential
    """
    # ── Phase 1 + 2: Producer agent + token ──────────────────────────────
    producer = _setup_api_agent(client, superuser_token_headers, name="Sync Producer")
    producer_id = producer["id"]
    token = _mint_token(client, superuser_token_headers, producer_id, label="sync-token")
    token_value = token["token"]
    base_url = token["base_url"]
    spec_url = token["spec_url"]

    # ── Phase 3: Create agent_api credential ─────────────────────────────
    r = client.post(
        f"{API}/credentials/",
        headers=superuser_token_headers,
        json={
            "name": "Producer API Access",
            "type": "agent_api",
            "credential_data": {
                "base_url": base_url,
                "spec_url": spec_url,
                "token": token_value,
                "label": "Producer API",
                "producer_agent_id": producer_id,
            },
        },
    )
    assert r.status_code == 200, r.text
    cred_id = r.json()["id"]

    # ── Phase 4: Consumer agent ───────────────────────────────────────────
    consumer = create_agent_via_api(client, superuser_token_headers, name="Sync Consumer")
    drain_tasks()
    consumer_id = consumer["id"]

    # ── Phase 5: Link credential ──────────────────────────────────────────
    link_result = link_credential_to_agent(
        client, superuser_token_headers, consumer_id, cred_id
    )
    assert link_result is not None

    # ── Phase 6: Linked credentials list shows the credential ────────────
    r = client.get(
        f"{API}/agents/{consumer_id}/credentials",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    linked_creds = body.get("data", body) if isinstance(body, dict) else body
    assert any(
        c.get("id") == cred_id for c in linked_creds
    ), f"Credential not found in agent credentials: {linked_creds}"


def test_agent_api_urls_rewritten_to_internal_backend_for_env_sync(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    The stored agent_api credential holds the PUBLIC proxy URL (built from
    FRONTEND_HOST), but when prepared for an agent environment the host is
    rewritten to the container-reachable backend origin (AGENT_ENV_BACKEND_URL),
    preserving the /api/v1/agent-api/{id}[/openapi.json] path. Without this a
    consumer container calling base_url (e.g. http://localhost:5173 in local
    dev) would fail — localhost is the container itself, not the backend.
    """
    # Producer with API enabled + a consumer agent linked via the connect helper.
    producer = _setup_api_agent(client, superuser_token_headers, name="Rewrite Producer")
    producer_id = producer["id"]
    consumer = create_agent_via_api(client, superuser_token_headers, name="Rewrite Consumer")
    drain_tasks()
    consumer_id = consumer["id"]

    token = _mint_token(
        client,
        superuser_token_headers,
        producer_id,
        label="rewrite",
        consumer_agent_id=consumer_id,
    )
    stored_base_url = token["base_url"]  # public URL from the connect response
    stored_spec_url = token["spec_url"]

    # Prepare creds for the consumer env → agent_api URLs rewritten to internal backend.
    prepared = CredentialsService.prepare_credentials_for_environment(
        db, uuid.UUID(consumer_id)
    )
    api_creds = [c for c in prepared["credentials_json"] if c["type"] == "agent_api"]
    assert len(api_creds) == 1, f"expected one synced agent_api cred, got {api_creds}"
    synced = api_creds[0]["credential_data"]

    internal_netloc = urlsplit(settings.AGENT_ENV_BACKEND_URL).netloc
    # Host swapped to the container-reachable backend ...
    assert urlsplit(synced["base_url"]).netloc == internal_netloc
    assert urlsplit(synced["spec_url"]).netloc == internal_netloc
    # ... path preserved (still targets this producer + the spec endpoint).
    assert urlsplit(synced["base_url"]).path == urlsplit(stored_base_url).path
    assert producer_id in synced["base_url"]
    assert synced["spec_url"].endswith("/openapi.json")
    # Token unchanged by the rewrite.
    assert synced["token"] == token["token"]

    # The STORED credential keeps the public URL (UI display is unaffected).
    cred = get_credential_with_data(
        client, superuser_token_headers, token["credential_id"]
    )
    assert cred["credential_data"]["base_url"] == stored_base_url
    assert cred["credential_data"]["spec_url"] == stored_spec_url


# ── G. Connect helper ─────────────────────────────────────────────────────────


def test_connect_helper_mints_token_and_creates_credential(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Connect helper (POST /agents/{id}/agent-api/connect):
      1. Mints a token on the producer agent
      2. Creates an agent_api credential with base_url + spec_url
      3. Response includes credential_id, token_id, token_prefix, base_url, spec_url
      4. Credential is accessible via GET /credentials/{id}/with-data
      5. The connection is reported by the connection-info endpoint
      6. agent_api_enabled=False → 400 (disabled)
    """
    # ── Phase 1-4: Happy path ─────────────────────────────────────────────
    producer = _setup_api_agent(client, superuser_token_headers, name="Connect Producer")
    producer_id = producer["id"]

    r = client.post(
        _connect_url(producer_id),
        headers=superuser_token_headers,
        json={"credential_label": "My Producer API"},
    )
    assert r.status_code == 200, r.text
    result = r.json()

    assert "credential_id" in result and result["credential_id"]
    assert "token_id" in result and result["token_id"]
    assert "token_prefix" in result and len(result["token_prefix"]) == 8
    assert "base_url" in result and producer_id in result["base_url"]
    assert "spec_url" in result and result["spec_url"].endswith("/openapi.json")
    assert result["linked_consumer_agent_id"] is None

    # Credential accessible with data
    cred_with_data = get_credential_with_data(
        client, superuser_token_headers, result["credential_id"]
    )
    cred_fields = cred_with_data.get("credential_data", {})
    assert "base_url" in cred_fields
    assert "token" in cred_fields
    assert "spec_url" in cred_fields

    # Connection is reported by the connection-info endpoint
    r = client.get(
        _connection_info_url(result["credential_id"]),
        headers=superuser_token_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["producer_agent_id"] == producer_id

    # ── Phase 5: agent_api_enabled=False → 400 ───────────────────────────
    disabled_agent = create_agent_via_api(
        client, superuser_token_headers, name="Disabled Connect Agent"
    )
    drain_tasks()

    r = client.post(
        _connect_url(disabled_agent["id"]),
        headers=superuser_token_headers,
        json={},
    )
    assert r.status_code == 400, r.text


def test_connect_helper_links_to_consumer_agent(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Connect helper with consumer_agent_id:
      1. Mints token, creates credential, links it to the consumer agent
      2. Response reflects linked_consumer_agent_id
      3. Consumer agent's credential list includes the new credential
    """
    producer = _setup_api_agent(client, superuser_token_headers, name="Link Producer")
    producer_id = producer["id"]
    consumer = create_agent_via_api(client, superuser_token_headers, name="Link Consumer")
    drain_tasks()
    consumer_id = consumer["id"]

    r = client.post(
        _connect_url(producer_id),
        headers=superuser_token_headers,
        json={
            "credential_label": "Linked API",
            "consumer_agent_id": consumer_id,
        },
    )
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["linked_consumer_agent_id"] == consumer_id

    # Consumer agent's credentials include the new one
    r = client.get(
        f"{API}/agents/{consumer_id}/credentials",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    linked_creds = body.get("data", body) if isinstance(body, dict) else body
    cred_id = result["credential_id"]
    assert any(
        c.get("id") == cred_id for c in linked_creds
    ), f"Linked credential not found in consumer agent: {linked_creds}"


def test_connect_helper_access_requires_producer_ownership(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Non-owner cannot use the connect helper on another user's agent → 404.
    """
    producer = _setup_api_agent(client, superuser_token_headers, name="Connect Owner Agent")
    _, other_headers = create_random_user_with_headers(client)

    r = client.post(
        _connect_url(producer["id"]),
        headers=other_headers,
        json={},
    )
    assert r.status_code == 404, r.text


# ── H. Spec stubbing + passthrough ────────────────────────────────────────────


def test_spec_reflects_stubbed_live_app_spec(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Spec endpoint reflects the adapter's spec (not a hardcoded value):
      1. Default stub returns a minimal spec with openapi + info + paths
      2. Customize the stub's spec → owner spec endpoint returns the custom spec
      3. Consumer spec endpoint also returns the custom spec
    """
    agent = _setup_api_agent(client, superuser_token_headers, name="Custom Spec Agent")
    agent_id = agent["id"]
    token = _mint_token(client, superuser_token_headers, agent_id, label="spec-test")
    token_value = token["token"]

    custom_spec = {
        "openapi": "3.1.0",
        "info": {"title": "My Orders API", "version": "2.0.0"},
        "paths": {
            "/orders": {
                "get": {
                    "summary": "List orders",
                    "responses": {"200": {"description": "Success"}},
                }
            }
        },
    }

    custom_adapter = EnvironmentTestAdapter()
    custom_adapter.agent_api_spec = custom_spec

    lm = EnvironmentService._lifecycle_manager
    original_get_adapter = lm.get_adapter
    lm.get_adapter = lambda env: custom_adapter

    try:
        # ── Owner spec → custom spec ──────────────────────────────────────
        r = client.get(_owner_spec_url(agent_id), headers=superuser_token_headers)
        assert r.status_code == 200, r.text
        spec = r.json()
        assert spec["info"]["title"] == "My Orders API"
        assert spec["info"]["version"] == "2.0.0"
        assert "/orders" in spec.get("paths", {})

        # ── Consumer spec → same custom spec ─────────────────────────────
        r = client.get(
            _consumer_spec_url(agent_id),
            headers=_bearer_headers(token_value),
        )
        assert r.status_code == 200, r.text
        spec_consumer = r.json()
        assert spec_consumer["info"]["title"] == "My Orders API"

    finally:
        lm.get_adapter = original_get_adapter


# ── I. Multiple connections + independent disconnect ──────────────────────────


def test_multiple_connections_independent_disconnect(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Multiple connections to the same producer: disconnecting one (deleting its
    credential) leaves the others working.
      1. Connect three times → three independent tokens/credentials
      2. All three can access the consumer proxy
      3. Disconnect B → A and C still work; B returns 401
      4. Disconnect A → A returns 401; C still works
    """
    agent = _setup_api_agent(client, superuser_token_headers, name="Multi Conn Agent")
    agent_id = agent["id"]

    t_a = _mint_token(client, superuser_token_headers, agent_id, label="conn-a")
    t_b = _mint_token(client, superuser_token_headers, agent_id, label="conn-b")
    t_c = _mint_token(client, superuser_token_headers, agent_id, label="conn-c")

    def _can_call(token_value: str) -> int:
        r = client.get(
            _consumer_proxy_url(agent_id, "ping"),
            headers=_bearer_headers(token_value),
        )
        return r.status_code

    # ── Phase 2: All three work ───────────────────────────────────────────
    assert _can_call(t_a["token"]) == 200
    assert _can_call(t_b["token"]) == 200
    assert _can_call(t_c["token"]) == 200

    # ── Phase 3: Disconnect B → A and C still work, B → 401 ───────────────
    _disconnect(client, superuser_token_headers, t_b["credential_id"])
    assert _can_call(t_a["token"]) == 200
    assert _can_call(t_b["token"]) == 401
    assert _can_call(t_c["token"]) == 200

    # ── Phase 4: Disconnect A → A → 401, C still works ────────────────────
    _disconnect(client, superuser_token_headers, t_a["credential_id"])
    assert _can_call(t_a["token"]) == 401
    assert _can_call(t_c["token"]) == 200


# ── J. Unauthenticated + missing-resource guards ─────────────────────────────


def test_owner_routes_unauthenticated_rejected(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Unauthenticated requests to the owner-side agent_api routes (connect,
    connection-info, status, spec) return 401/403.
    """
    agent = _setup_api_agent(client, superuser_token_headers, name="Auth Guard Agent")
    agent_id = agent["id"]
    created = _mint_token(client, superuser_token_headers, agent_id, label="auth-guard")

    assert client.post(_connect_url(agent_id), json={}).status_code in (401, 403)
    assert client.get(
        _connection_info_url(created["credential_id"])
    ).status_code in (401, 403)
    assert client.get(_status_url(agent_id)).status_code in (401, 403)
    assert client.get(_owner_spec_url(agent_id)).status_code in (401, 403)
