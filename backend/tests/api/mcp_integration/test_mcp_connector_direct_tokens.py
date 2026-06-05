"""
MCP Connector direct access token integration tests.

Tests connector-scoped opaque bearer tokens (token_type="direct"):

  - Generate token returns full value once + correct 8-char prefix
  - List never includes full token value (only prefix)
  - Revoke → token is revoked per GET response; restore undoes that
  - Delete removes token; no longer appears in list
  - generate returns 403 when allow_token_access is False
  - Non-owner cannot generate / list / revoke / delete
  - MCPTokenVerifier accepts direct token (via patched DB session)
  - Verifier rejects revoked token
  - Direct token for connector A rejected on connector B (connector-match check)
  - Connector deletion cascades direct tokens (FK ON DELETE CASCADE checked via list)
  - last_used_at updates after successful verification (patched verifier)
  - Multiple tokens on same connector: each independent, prefix is correct

Note on the verifier tests:
  MCPTokenVerifier uses DBSession(engine) — the production engine — which is
  invisible to the test transaction savepoint. Tests that exercise the verifier
  patch ``app.mcp.token_verifier.DBSession`` to inject the test session proxy,
  keeping the same code path while staying in the test transaction. This is the
  correct pattern (same as patching create_session at other sites).
"""
import asyncio
import uuid
from contextlib import contextmanager
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.mcp import (
    create_mcp_connector,
    delete_mcp_connector,
    update_mcp_connector,
)
from tests.utils.db_proxy import NonClosingSessionProxy
from tests.utils.user import create_random_user_with_headers


# ── Constants ─────────────────────────────────────────────────────────────────

_TOKENS_URL_TMPL = (
    "{api}/agents/{agent_id}/mcp-connectors/{connector_id}/tokens"
)
_TOKEN_URL_TMPL = (
    "{api}/agents/{agent_id}/mcp-connectors/{connector_id}/tokens/{token_id}"
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _setup_agent_with_connector(
    client: TestClient,
    token_headers: dict[str, str],
    agent_name: str = "Token Agent",
    connector_name: str = "Token Connector",
    allow_token_access: bool = True,
) -> tuple[dict, dict]:
    """Create agent + connector, returning (agent, connector)."""
    agent = create_agent_via_api(client, token_headers, name=agent_name)
    drain_tasks()
    agent = get_agent(client, token_headers, agent["id"])
    connector = _create_connector_with_flag(
        client, token_headers, agent["id"],
        name=connector_name,
        allow_token_access=allow_token_access,
    )
    return agent, connector


def _create_connector_with_flag(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
    name: str = "Direct Token Connector",
    allow_token_access: bool = True,
) -> dict:
    r = client.post(
        f"{settings.API_V1_STR}/agents/{agent_id}/mcp-connectors",
        headers=token_headers,
        json={
            "name": name,
            "mode": "conversation",
            "allow_token_access": allow_token_access,
        },
    )
    assert r.status_code == 200, f"Create connector failed: {r.text}"
    return r.json()


def _tokens_url(agent_id: str, connector_id: str) -> str:
    return _TOKENS_URL_TMPL.format(
        api=settings.API_V1_STR, agent_id=agent_id, connector_id=connector_id
    )


def _token_url(agent_id: str, connector_id: str, token_id: str) -> str:
    return _TOKEN_URL_TMPL.format(
        api=settings.API_V1_STR, agent_id=agent_id,
        connector_id=connector_id, token_id=token_id,
    )


def _generate_token(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
    connector_id: str,
    label: str = "My Token",
) -> dict:
    """POST .../tokens — asserts 200 and returns created token."""
    r = client.post(
        _tokens_url(agent_id, connector_id),
        headers=token_headers,
        json={"label": label},
    )
    assert r.status_code == 200, f"Generate token failed: {r.text}"
    return r.json()


def _list_tokens(
    client: TestClient,
    token_headers: dict[str, str],
    agent_id: str,
    connector_id: str,
) -> dict:
    """GET .../tokens — asserts 200 and returns MCPConnectorTokensPublic."""
    r = client.get(
        _tokens_url(agent_id, connector_id),
        headers=token_headers,
    )
    assert r.status_code == 200, f"List tokens failed: {r.text}"
    return r.json()


@contextmanager
def _patched_verifier_session(db: Session):
    """Patch MCPTokenVerifier so its DBSession(engine) uses the test session.

    MCPTokenVerifier uses ``with DBSession(engine) as db:`` directly, which
    bypasses the test transaction savepoint. We replace the DBSession constructor
    in the token_verifier module with a context-manager that yields a
    NonClosingSessionProxy wrapping the test session instead.
    """
    proxy = NonClosingSessionProxy(db)

    class _FakeSessionCM:
        """Minimal context-manager that yields the test session proxy."""
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return proxy
        def __exit__(self, *args):
            pass

    with patch("app.mcp.token_verifier.DBSession", _FakeSessionCM):
        yield


def _verify_token_via_verifier(
    connector_id: str,
    token_value: str,
    db: Session | None = None,
) -> object:
    """Call MCPTokenVerifier.verify_token() synchronously.

    When ``db`` is provided, patches the verifier's DBSession to use the test
    session so the token rows are visible. When None, calls the real engine
    (useful only when the token is committed to the actual DB).
    """
    from app.mcp.token_verifier import MCPTokenVerifier
    verifier = MCPTokenVerifier(connector_id=connector_id)

    if db is not None:
        with _patched_verifier_session(db):
            return asyncio.run(verifier.verify_token(token_value))
    return asyncio.run(verifier.verify_token(token_value))


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_direct_token_full_lifecycle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    Full direct token lifecycle:
      1. Create agent + connector with allow_token_access=True
      2. Generate token → full value returned once, prefix matches
      3. List tokens → prefix present; full token value absent
      4. MCPTokenVerifier accepts direct token (test session patched in)
      5. last_used_at set after verification
      6. Revoke token → verifier rejects it
      7. Restore token → verifier accepts it again
      8. Delete token → gone from list
      9. Verifier rejects deleted token
    """
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Full Lifecycle Token Agent",
        allow_token_access=True,
    )
    agent_id = agent["id"]
    connector_id = connector["id"]

    # ── Phase 2: Generate token ───────────────────────────────────────────
    created = _generate_token(
        client, superuser_token_headers, agent_id, connector_id,
        label="CI token",
    )
    token_id = created["id"]
    full_token = created["token"]

    assert len(full_token) > 0, "Full token value must be returned on creation"
    assert created["prefix"] == full_token[:8], "Prefix must be first 8 chars of token"
    assert created["label"] == "CI token"
    assert created["revoked"] is False
    assert created["connector_id"] == connector_id
    assert "expires_at" in created

    # ── Phase 3: List tokens — full value absent ──────────────────────────
    listing = _list_tokens(client, superuser_token_headers, agent_id, connector_id)
    assert listing["count"] == 1
    token_in_list = listing["data"][0]

    assert "token" not in token_in_list, (
        "Full token value MUST NOT appear in list response"
    )
    assert token_in_list["prefix"] == full_token[:8]
    assert token_in_list["label"] == "CI token"
    assert token_in_list["id"] == token_id
    assert token_in_list["revoked"] is False

    # ── Phase 4: Verifier accepts direct token ────────────────────────────
    access_token = _verify_token_via_verifier(connector_id, full_token, db=db)
    assert access_token is not None, "Verifier must accept a valid direct token"
    assert access_token.client_id == "direct"

    # ── Phase 5: last_used_at set after verification ──────────────────────
    # Re-fetch via list — last_used_at should now be populated
    listing2 = _list_tokens(client, superuser_token_headers, agent_id, connector_id)
    assert listing2["data"][0]["last_used_at"] is not None, (
        "last_used_at must be updated after successful verification"
    )

    # ── Phase 6: Revoke token → verifier rejects ─────────────────────────
    r = client.put(
        _token_url(agent_id, connector_id, token_id),
        headers=superuser_token_headers,
        json={"revoked": True},
    )
    assert r.status_code == 200
    assert r.json()["revoked"] is True

    revoked_result = _verify_token_via_verifier(connector_id, full_token, db=db)
    assert revoked_result is None, "Verifier must reject a revoked token"

    # ── Phase 7: Restore token → verifier accepts again ──────────────────
    r = client.put(
        _token_url(agent_id, connector_id, token_id),
        headers=superuser_token_headers,
        json={"revoked": False},
    )
    assert r.status_code == 200
    assert r.json()["revoked"] is False

    restored_result = _verify_token_via_verifier(connector_id, full_token, db=db)
    assert restored_result is not None, "Verifier must accept a restored token"

    # ── Phase 8: Delete token ─────────────────────────────────────────────
    r = client.delete(
        _token_url(agent_id, connector_id, token_id),
        headers=superuser_token_headers,
    )
    assert r.status_code == 200

    # Gone from list
    listing3 = _list_tokens(client, superuser_token_headers, agent_id, connector_id)
    assert listing3["count"] == 0

    # ── Phase 9: Verifier rejects deleted token ───────────────────────────
    deleted_result = _verify_token_via_verifier(connector_id, full_token, db=db)
    assert deleted_result is None, "Verifier must reject a deleted token"


def test_generate_token_blocked_when_flag_off(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    POST .../tokens returns 403 when connector.allow_token_access is False.
    List and delete are still allowed (ownership check only).
    """
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Token Flag Off Agent",
        allow_token_access=False,
    )
    agent_id = agent["id"]
    connector_id = connector["id"]

    # ── Generate → 403 ────────────────────────────────────────────────────
    r = client.post(
        _tokens_url(agent_id, connector_id),
        headers=superuser_token_headers,
        json={"label": "Should Fail"},
    )
    assert r.status_code == 403, f"Expected 403 when allow_token_access=False, got {r.status_code}"
    assert "disabled" in r.json()["detail"].lower() or "token access" in r.json()["detail"].lower()

    # ── List still returns 200 with empty list ────────────────────────────
    listing = _list_tokens(client, superuser_token_headers, agent_id, connector_id)
    assert listing["count"] == 0


def test_generate_token_after_flag_toggled_on(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    After toggling allow_token_access from False to True, token generation
    succeeds (the flag is re-checked per request).
    """
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Toggle Flag Agent",
        allow_token_access=False,
    )
    agent_id = agent["id"]
    connector_id = connector["id"]

    # Confirm blocked
    r = client.post(
        _tokens_url(agent_id, connector_id),
        headers=superuser_token_headers,
        json={"label": "Before Toggle"},
    )
    assert r.status_code == 403

    # Enable the flag
    update_mcp_connector(
        client, superuser_token_headers, agent_id, connector_id,
        allow_token_access=True,
    )

    # Now generation should succeed
    created = _generate_token(
        client, superuser_token_headers, agent_id, connector_id,
        label="After Toggle",
    )
    assert "token" in created
    assert len(created["token"]) > 0


def test_non_owner_cannot_manage_tokens(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Non-owner gets 403 on all token sub-routes:
      - POST .../tokens (generate)
      - GET .../tokens (list)
      - PUT .../tokens/{id} (revoke)
      - DELETE .../tokens/{id} (delete)
    """
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Non-Owner Token Agent",
        allow_token_access=True,
    )
    agent_id = agent["id"]
    connector_id = connector["id"]

    # Owner generates a token (for the revoke/delete checks)
    created = _generate_token(
        client, superuser_token_headers, agent_id, connector_id,
        label="Owner's Token",
    )
    token_id = created["id"]

    # Create a different user
    _, other_headers = create_random_user_with_headers(client)

    # ── Non-owner: generate → 403 ─────────────────────────────────────────
    r = client.post(
        _tokens_url(agent_id, connector_id),
        headers=other_headers,
        json={"label": "Stolen Token"},
    )
    assert r.status_code == 403, f"Expected 403, got {r.status_code}"

    # ── Non-owner: list → 403 ─────────────────────────────────────────────
    r = client.get(
        _tokens_url(agent_id, connector_id),
        headers=other_headers,
    )
    assert r.status_code == 403, f"Expected 403, got {r.status_code}"

    # ── Non-owner: revoke → 403 ───────────────────────────────────────────
    r = client.put(
        _token_url(agent_id, connector_id, token_id),
        headers=other_headers,
        json={"revoked": True},
    )
    assert r.status_code == 403, f"Expected 403, got {r.status_code}"

    # ── Non-owner: delete → 403 ───────────────────────────────────────────
    r = client.delete(
        _token_url(agent_id, connector_id, token_id),
        headers=other_headers,
    )
    assert r.status_code == 403, f"Expected 403, got {r.status_code}"

    # Owner's token is untouched
    listing = _list_tokens(client, superuser_token_headers, agent_id, connector_id)
    assert listing["count"] == 1
    assert listing["data"][0]["revoked"] is False


def test_direct_token_connector_mismatch_rejected(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    A direct token for connector A is rejected by MCPTokenVerifier when presented
    to connector B. The verifier checks connector_id match on every request.
    """
    agent, connector_a = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Connector Mismatch Agent",
        connector_name="Connector A",
        allow_token_access=True,
    )
    agent_id = agent["id"]

    # Create a second connector on the same agent
    connector_b = _create_connector_with_flag(
        client, superuser_token_headers, agent_id,
        name="Connector B",
        allow_token_access=True,
    )

    connector_a_id = connector_a["id"]
    connector_b_id = connector_b["id"]

    # Generate a token for connector A
    created = _generate_token(
        client, superuser_token_headers, agent_id, connector_a_id,
        label="Connector A Token",
    )
    token_value = created["token"]

    # Token passes for connector A
    result_a = _verify_token_via_verifier(connector_a_id, token_value, db=db)
    assert result_a is not None, "Token must be accepted by its own connector"

    # Token rejected for connector B (different connector_id)
    result_b = _verify_token_via_verifier(connector_b_id, token_value, db=db)
    assert result_b is None, (
        "Token for connector A must be rejected by connector B's verifier"
    )


def test_connector_deletion_cascades_direct_tokens(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    Deleting a connector cascades its direct tokens (FK ON DELETE CASCADE).
    After deletion, the token list endpoint returns 404 (connector gone),
    and the verifier rejects the token (no row in DB + no active connector).
    """
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Cascade Delete Agent",
        allow_token_access=True,
    )
    agent_id = agent["id"]
    connector_id = connector["id"]

    # Generate a token
    created = _generate_token(
        client, superuser_token_headers, agent_id, connector_id,
        label="Soon Deleted",
    )
    token_value = created["token"]

    # Verify it works before deletion (verifier sees it in test session)
    pre_delete = _verify_token_via_verifier(connector_id, token_value, db=db)
    assert pre_delete is not None

    # Delete the connector (cascades tokens via FK)
    delete_mcp_connector(client, superuser_token_headers, agent_id, connector_id)

    # List endpoint returns 404 (connector gone)
    r = client.get(
        _tokens_url(agent_id, connector_id),
        headers=superuser_token_headers,
    )
    assert r.status_code == 404, (
        f"Expected 404 after connector deletion, got {r.status_code}"
    )

    # Verifier rejects the token (cascaded row deleted + connector gone)
    post_delete = _verify_token_via_verifier(connector_id, token_value, db=db)
    assert post_delete is None, (
        "After connector deletion, the cascaded token must be rejected"
    )


def test_multiple_direct_tokens_on_same_connector(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    Multiple direct tokens on the same connector:
      1. Generate three tokens with distinct labels
      2. List → all three appear (prefix only, no full token)
      3. Each token verified independently by the verifier
      4. Revoking one does not affect the others
    """
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Multi Token Agent",
        allow_token_access=True,
    )
    agent_id = agent["id"]
    connector_id = connector["id"]

    # ── Generate three tokens ─────────────────────────────────────────────
    tok1 = _generate_token(client, superuser_token_headers, agent_id, connector_id, label="Alpha")
    tok2 = _generate_token(client, superuser_token_headers, agent_id, connector_id, label="Beta")
    tok3 = _generate_token(client, superuser_token_headers, agent_id, connector_id, label="Gamma")

    # All tokens have different values
    assert tok1["token"] != tok2["token"]
    assert tok2["token"] != tok3["token"]

    # ── List → three entries, no full token ───────────────────────────────
    listing = _list_tokens(client, superuser_token_headers, agent_id, connector_id)
    assert listing["count"] == 3
    for entry in listing["data"]:
        assert "token" not in entry, "Full token must not appear in list"

    labels = {e["label"] for e in listing["data"]}
    assert labels == {"Alpha", "Beta", "Gamma"}

    # ── Verify all three independently ───────────────────────────────────
    for tok in [tok1, tok2, tok3]:
        result = _verify_token_via_verifier(connector_id, tok["token"], db=db)
        assert result is not None, f"Token {tok['label']} should be accepted"

    # ── Revoke tok2, others unaffected ────────────────────────────────────
    r = client.put(
        _token_url(agent_id, connector_id, tok2["id"]),
        headers=superuser_token_headers,
        json={"revoked": True},
    )
    assert r.status_code == 200

    assert _verify_token_via_verifier(connector_id, tok1["token"], db=db) is not None
    assert _verify_token_via_verifier(connector_id, tok2["token"], db=db) is None
    assert _verify_token_via_verifier(connector_id, tok3["token"], db=db) is not None


def test_direct_token_list_never_includes_full_token(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    GET .../tokens list endpoint must NEVER include the full token value in any
    response key. Only the 8-char prefix is returned.
    """
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="No Leak Agent",
        allow_token_access=True,
    )
    agent_id = agent["id"]
    connector_id = connector["id"]

    created = _generate_token(
        client, superuser_token_headers, agent_id, connector_id,
        label="Secret Token",
    )
    full_token = created["token"]

    # Verify full token was returned on create
    assert len(full_token) > 8

    # List must not contain the full token value
    listing = _list_tokens(client, superuser_token_headers, agent_id, connector_id)
    response_text = str(listing)
    assert full_token not in response_text, (
        "Full token value must not appear anywhere in the list response"
    )

    # Only prefix present
    entry = listing["data"][0]
    assert entry["prefix"] == full_token[:8]
    assert "token" not in entry


def test_token_verifier_accepts_direct_not_refresh(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    The MCPTokenVerifier only accepts token_type in ("access", "direct").
    A garbage value that matches no row is correctly rejected.
    A direct token is accepted.
    """
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Verifier Type Agent",
        allow_token_access=True,
    )
    connector_id = connector["id"]

    # Generate a direct token
    created = _generate_token(
        client, superuser_token_headers, agent["id"], connector_id,
        label="Direct Check",
    )
    direct_token = created["token"]

    # Direct token is accepted
    result = _verify_token_via_verifier(connector_id, direct_token, db=db)
    assert result is not None, "Direct token must be accepted by verifier"
    assert result.client_id == "direct"

    # Random garbage string is rejected (not a real token)
    garbage = "garbage-value-that-does-not-exist"
    result_garbage = _verify_token_via_verifier(connector_id, garbage, db=db)
    assert result_garbage is None, "Garbage token value must be rejected"


def test_token_crud_not_found_cases(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    404 cases for token sub-routes:
      - PUT with nonexistent token_id returns 404
      - DELETE with nonexistent token_id returns 404
      - Token from connector A returns 404 when accessed via connector B's URL
    """
    agent, connector_a = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="404 Token Agent",
        connector_name="Connector A 404",
        allow_token_access=True,
    )
    agent_id = agent["id"]
    connector_a_id = connector_a["id"]

    # Create a second connector
    connector_b = _create_connector_with_flag(
        client, superuser_token_headers, agent_id,
        name="Connector B 404",
        allow_token_access=True,
    )
    connector_b_id = connector_b["id"]

    # Generate a token for connector A
    created = _generate_token(
        client, superuser_token_headers, agent_id, connector_a_id,
        label="Token A",
    )
    token_a_id = created["id"]

    # ── Nonexistent token_id → 404 ────────────────────────────────────────
    fake_id = str(uuid.uuid4())
    r = client.put(
        _token_url(agent_id, connector_a_id, fake_id),
        headers=superuser_token_headers,
        json={"revoked": True},
    )
    assert r.status_code == 404

    r = client.delete(
        _token_url(agent_id, connector_a_id, fake_id),
        headers=superuser_token_headers,
    )
    assert r.status_code == 404

    # ── Token from connector A accessed via connector B's URL → 404 ───────
    # (get_token checks connector_id match)
    r = client.put(
        _token_url(agent_id, connector_b_id, token_a_id),
        headers=superuser_token_headers,
        json={"revoked": True},
    )
    assert r.status_code == 404, (
        "Accessing connector A token via connector B URL must return 404"
    )

    r = client.delete(
        _token_url(agent_id, connector_b_id, token_a_id),
        headers=superuser_token_headers,
    )
    assert r.status_code == 404


def test_token_label_validation(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Label field validation:
      - Empty string rejected (min_length=1)
      - Label at max length (255 chars) accepted
      - Over-max label (256 chars) rejected
    """
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Label Validation Agent",
        allow_token_access=True,
    )
    agent_id = agent["id"]
    connector_id = connector["id"]

    # ── Empty label → 422 ────────────────────────────────────────────────
    r = client.post(
        _tokens_url(agent_id, connector_id),
        headers=superuser_token_headers,
        json={"label": ""},
    )
    assert r.status_code == 422, f"Expected 422 for empty label, got {r.status_code}"

    # ── Max-length label (255 chars) → 200 ───────────────────────────────
    long_label = "x" * 255
    created = _generate_token(
        client, superuser_token_headers, agent_id, connector_id,
        label=long_label,
    )
    assert created["label"] == long_label

    # ── Over-max label (256 chars) → 422 ─────────────────────────────────
    r = client.post(
        _tokens_url(agent_id, connector_id),
        headers=superuser_token_headers,
        json={"label": "x" * 256},
    )
    assert r.status_code == 422, f"Expected 422 for label > 255 chars, got {r.status_code}"


def test_direct_token_unauthenticated_access_rejected(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    All token sub-routes reject unauthenticated requests with 401.
    """
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Unauth Token Agent",
        allow_token_access=True,
    )
    agent_id = agent["id"]
    connector_id = connector["id"]

    created = _generate_token(
        client, superuser_token_headers, agent_id, connector_id,
        label="Auth Test",
    )
    token_id = created["id"]

    # No auth headers
    r = client.post(_tokens_url(agent_id, connector_id), json={"label": "No Auth"})
    assert r.status_code == 401

    r = client.get(_tokens_url(agent_id, connector_id))
    assert r.status_code == 401

    r = client.put(_token_url(agent_id, connector_id, token_id), json={"revoked": True})
    assert r.status_code == 401

    r = client.delete(_token_url(agent_id, connector_id, token_id))
    assert r.status_code == 401


def test_inactive_connector_direct_token_rejected_by_verifier(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    When the connector is deactivated, its direct tokens are rejected by the
    verifier (MCPTokenVerifier checks is_active on the connector row).
    """
    agent, connector = _setup_agent_with_connector(
        client, superuser_token_headers,
        agent_name="Inactive Connector Token Agent",
        allow_token_access=True,
    )
    agent_id = agent["id"]
    connector_id = connector["id"]

    created = _generate_token(
        client, superuser_token_headers, agent_id, connector_id,
        label="Before Deactivate",
    )
    token_value = created["token"]

    # Token is valid while connector is active
    assert _verify_token_via_verifier(connector_id, token_value, db=db) is not None

    # Deactivate connector
    update_mcp_connector(
        client, superuser_token_headers, agent_id, connector_id,
        is_active=False,
    )

    # Verifier rejects token for inactive connector
    result = _verify_token_via_verifier(connector_id, token_value, db=db)
    assert result is None, (
        "Direct token must be rejected when connector is inactive"
    )
