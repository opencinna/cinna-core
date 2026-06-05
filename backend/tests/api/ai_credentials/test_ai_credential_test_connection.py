"""
Tests for POST /ai-credentials/test-connection

Scenario-based integration tests. All provider I/O is intercepted by patching
``app.services.credentials.model_discovery_service.probe_models`` so no real
network call is ever made.

Scenarios:
  1. Happy-path (Add form — api_key only, no credential_id)
       - success=True, models populated, model_count==len(models)
       - error=None, skip_reason=None
       - Credential-list discovery columns NOT changed (nothing persisted)
  2. Happy-path (Edit form — credential_id present)
       - success=True, models populated
       - discovered_models / models_discovered_at persisted on the stored credential
         (observable via GET /ai-credentials/{id})
  3. Skip reasons → success=True, skip_reason set, error=None
       - OAuth token (sk-ant-oat*)  → skip_reason="oauth_token_unsupported"
       - MiniMax                     → skip_reason="no_list_endpoint"
       - openai_compatible no base_url (tested via probe_models skip path)
  4. invalid_key → success=False, error="invalid_key", skip_reason=None
  5. Validation: neither api_key nor credential_id → 422
  6. Auth guard: unauthenticated → 401/403
  7. Ownership guard (credential_id belongs to another user) → 403
  8. Nonexistent credential_id → 404
  9. Edit path, invalid_key: models_discovery_error persisted, prior discovered_models
     preserved (observable via GET)
  10. Response shape invariant: model_count == len(models) always
"""
import uuid
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.ai_credential import (
    create_random_ai_credential,
    get_ai_credential,
)
from tests.utils.user import create_random_user, user_authentication_headers

_BASE = f"{settings.API_V1_STR}/ai-credentials"
_PROBE_TARGET = "app.services.credentials.model_discovery_service.probe_models"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _probe_success(models: list[str]):
    """Return a probe_models AsyncMock that signals a successful listing."""
    from app.services.credentials.model_discovery_service import ProbeResult
    return AsyncMock(return_value=ProbeResult(ok=True, models=models, reason=None))


def _probe_skip(reason: str):
    """Return a probe_models AsyncMock that signals a benign skip."""
    from app.services.credentials.model_discovery_service import ProbeResult
    return AsyncMock(return_value=ProbeResult(ok=True, models=[], reason=reason))


def _probe_invalid_key():
    """Return a probe_models AsyncMock that signals a rejected key."""
    from app.services.credentials.model_discovery_service import ProbeResult
    return AsyncMock(
        return_value=ProbeResult(
            ok=False, models=[], reason="invalid_key"
        )
    )


def _test_connection(client, headers, payload):
    """POST /ai-credentials/test-connection and return (status, body)."""
    r = client.post(f"{_BASE}/test-connection", headers=headers, json=payload)
    return r.status_code, r.json()


# ---------------------------------------------------------------------------
# Scenario 1: Happy path — Add form (api_key only, no credential_id)
# ---------------------------------------------------------------------------

def test_test_connection_add_form_success(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Add-form happy path (api_key only, no credential_id):
      1. Call test-connection with a raw anthropic api_key → success
      2. Verify response shape: success=True, models populated, model_count==len(models)
      3. Verify error=None, skip_reason=None
      4. Create a real credential to confirm discovered_models is still None (nothing persisted)
    """
    models_returned = ["claude-sonnet-4-6", "claude-haiku-4-5", "claude-opus-4"]

    # ── Phase 1: test-connection with api_key, no credential_id ──────────
    with patch(_PROBE_TARGET, _probe_success(models_returned)):
        status, body = _test_connection(
            client, superuser_token_headers,
            {
                "type": "anthropic",
                "api_key": "sk-ant-api03-test-fresh-key",
            },
        )

    assert status == 200, f"Expected 200, got {status}: {body}"

    # ── Phase 2: Verify response shape ────────────────────────────────────
    assert body["success"] is True
    assert body["models"] == models_returned
    assert body["model_count"] == len(models_returned)
    assert body["error"] is None
    assert body["skip_reason"] is None

    # ── Phase 3: Verify nothing was persisted (no credential row) ─────────
    # We separately create a real credential and confirm its discovery columns
    # are still None — the Add-form test above could not have touched it.
    cred = create_random_ai_credential(
        client, superuser_token_headers,
        credential_type="anthropic",
        api_key="sk-ant-api03-separate-key",
    )
    fetched = get_ai_credential(client, superuser_token_headers, cred["id"])
    assert fetched["discovered_models"] is None, (
        "The Add-form test-connection must not persist anything to an unrelated credential"
    )
    assert fetched["models_discovered_at"] is None
    assert fetched["models_discovery_error"] is None


# ---------------------------------------------------------------------------
# Scenario 2: Happy path — Edit form (credential_id present, persists models)
# ---------------------------------------------------------------------------

def test_test_connection_edit_form_persists_models(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Edit-form happy path (credential_id present):
      1. Create a stored anthropic credential (discovered_models is None initially)
      2. Call test-connection with credential_id, no api_key (uses stored key)
      3. Verify success response with models
      4. GET the credential → discovered_models and models_discovered_at are now set
    """
    # ── Phase 1: Create stored credential ────────────────────────────────
    cred = create_random_ai_credential(
        client, superuser_token_headers,
        credential_type="anthropic",
        api_key="sk-ant-api03-edit-form-key",
    )
    cred_id = cred["id"]

    # Confirm initial state: no discovery data yet.
    initial = get_ai_credential(client, superuser_token_headers, cred_id)
    assert initial["discovered_models"] is None
    assert initial["models_discovered_at"] is None

    # ── Phase 2: test-connection with credential_id ───────────────────────
    models_returned = ["claude-sonnet-4-6", "claude-haiku-4-5"]

    with patch(_PROBE_TARGET, _probe_success(models_returned)):
        status, body = _test_connection(
            client, superuser_token_headers,
            {
                "type": "anthropic",
                "credential_id": cred_id,
                # no api_key — uses the stored key
            },
        )

    assert status == 200, f"Expected 200, got {status}: {body}"

    # ── Phase 3: Verify response shape ────────────────────────────────────
    assert body["success"] is True
    assert body["models"] == models_returned
    assert body["model_count"] == len(models_returned)
    assert body["error"] is None
    assert body["skip_reason"] is None

    # ── Phase 4: GET credential → discovery columns are now set (persisted) ─
    after = get_ai_credential(client, superuser_token_headers, cred_id)
    assert after["discovered_models"] == models_returned, (
        "Edit-form test-connection must persist discovered_models onto the credential row"
    )
    assert after["models_discovered_at"] is not None, (
        "Edit-form test-connection must persist models_discovered_at"
    )
    assert after["models_discovery_error"] is None


# ---------------------------------------------------------------------------
# Scenario 3: Skip reasons → success=True, skip_reason set
# ---------------------------------------------------------------------------

def test_test_connection_oauth_token_skip(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    OAuth token (sk-ant-oat*) skip:
      - success=True, models=[], skip_reason="oauth_token_unsupported", error=None
    """
    with patch(_PROBE_TARGET, _probe_skip("oauth_token_unsupported")):
        status, body = _test_connection(
            client, superuser_token_headers,
            {
                "type": "anthropic",
                "api_key": "sk-ant-oat01-oauth-token",
            },
        )

    assert status == 200
    assert body["success"] is True
    assert body["models"] == []
    assert body["model_count"] == 0
    assert body["skip_reason"] == "oauth_token_unsupported"
    assert body["error"] is None


def test_test_connection_minimax_skip(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    MiniMax has no list endpoint → skip:
      - success=True, models=[], skip_reason="no_list_endpoint", error=None
    """
    with patch(_PROBE_TARGET, _probe_skip("no_list_endpoint")):
        status, body = _test_connection(
            client, superuser_token_headers,
            {
                "type": "minimax",
                "api_key": "mm-test-key-123",
            },
        )

    assert status == 200
    assert body["success"] is True
    assert body["models"] == []
    assert body["model_count"] == 0
    assert body["skip_reason"] == "no_list_endpoint"
    assert body["error"] is None


def test_test_connection_openai_compatible_no_base_url_skip(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    openai_compatible without base_url → no_base_url skip:
      - success=True, models=[], skip_reason="no_base_url", error=None
    """
    with patch(_PROBE_TARGET, _probe_skip("no_base_url")):
        status, body = _test_connection(
            client, superuser_token_headers,
            {
                "type": "openai_compatible",
                "api_key": "sk-compat-key",
                # no base_url supplied
            },
        )

    assert status == 200
    assert body["success"] is True
    assert body["skip_reason"] == "no_base_url"
    assert body["error"] is None


# ---------------------------------------------------------------------------
# Scenario 4: invalid_key → success=False, error set
# ---------------------------------------------------------------------------

def test_test_connection_invalid_key(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Provider rejects the key (401/403):
      - success=False, error="invalid_key", skip_reason=None, models=[]
    """
    with patch(_PROBE_TARGET, _probe_invalid_key()):
        status, body = _test_connection(
            client, superuser_token_headers,
            {
                "type": "anthropic",
                "api_key": "sk-ant-api03-bad-key",
            },
        )

    assert status == 200
    assert body["success"] is False
    assert body["error"] == "invalid_key"
    assert body["skip_reason"] is None
    assert body["models"] == []
    assert body["model_count"] == 0


# ---------------------------------------------------------------------------
# Scenario 5: Validation — neither api_key nor credential_id → 422
# ---------------------------------------------------------------------------

def test_test_connection_no_key_422(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When neither api_key nor credential_id is supplied, the service raises HTTP 422.
    """
    r = client.post(
        f"{_BASE}/test-connection",
        headers=superuser_token_headers,
        json={"type": "anthropic"},
    )
    assert r.status_code == 422, (
        f"Expected 422 when no key or credential_id supplied, got {r.status_code}: {r.text}"
    )


# ---------------------------------------------------------------------------
# Scenario 6: Auth guard — unauthenticated request
# ---------------------------------------------------------------------------

def test_test_connection_unauthenticated(client: TestClient) -> None:
    """Unauthenticated request must be rejected."""
    r = client.post(
        f"{_BASE}/test-connection",
        json={"type": "anthropic", "api_key": "sk-ant-api03-any"},
    )
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Scenario 7 + 8: Ownership guard + not-found — credential_id access control
# ---------------------------------------------------------------------------

def test_test_connection_credential_ownership_and_not_found(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Ownership guard and not-found scenarios for credential_id:
      1. Create credential owned by superuser
      2. Another user attempts test-connection with that credential_id → 403
      3. Nonexistent credential_id → 404
    """
    # ── Phase 1: Create credential ────────────────────────────────────────
    cred = create_random_ai_credential(
        client, superuser_token_headers,
        credential_type="anthropic",
        api_key="sk-ant-api03-ownership-key",
    )
    cred_id = cred["id"]

    # ── Phase 2: Other user → 403 ─────────────────────────────────────────
    other = create_random_user(client)
    other_headers = user_authentication_headers(
        client=client, email=other["email"], password=other["_password"]
    )

    r_cross = client.post(
        f"{_BASE}/test-connection",
        headers=other_headers,
        json={
            "type": "anthropic",
            "credential_id": cred_id,
        },
    )
    assert r_cross.status_code == 403, (
        f"Cross-user credential_id should return 403, got {r_cross.status_code}: {r_cross.text}"
    )

    # ── Phase 3: Nonexistent credential_id → 404 ──────────────────────────
    r_missing = client.post(
        f"{_BASE}/test-connection",
        headers=superuser_token_headers,
        json={
            "type": "anthropic",
            "credential_id": str(uuid.uuid4()),
        },
    )
    assert r_missing.status_code == 404, (
        f"Nonexistent credential_id should return 404, got {r_missing.status_code}: {r_missing.text}"
    )


# ---------------------------------------------------------------------------
# Scenario 9: Edit path + invalid_key → error persisted, prior list preserved
# ---------------------------------------------------------------------------

def test_test_connection_edit_invalid_key_persists_error(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Edit form, invalid key:
      1. Create credential (no prior discovery data)
      2. Call test-connection with credential_id → invalid_key probe result
      3. Response: success=False, error="invalid_key"
      4. GET credential → models_discovery_error="invalid_key", discovered_models still None
    """
    # ── Phase 1: Create credential ────────────────────────────────────────
    cred = create_random_ai_credential(
        client, superuser_token_headers,
        credential_type="openai",
        api_key="sk-openai-invalid",
    )
    cred_id = cred["id"]

    # ── Phase 2: test-connection → invalid_key ────────────────────────────
    with patch(_PROBE_TARGET, _probe_invalid_key()):
        status, body = _test_connection(
            client, superuser_token_headers,
            {
                "type": "openai",
                "credential_id": cred_id,
            },
        )

    assert status == 200
    assert body["success"] is False
    assert body["error"] == "invalid_key"

    # ── Phase 3: GET credential → error persisted, models still None ──────
    after = get_ai_credential(client, superuser_token_headers, cred_id)
    assert after["models_discovery_error"] == "invalid_key", (
        "Edit-form invalid_key result must persist models_discovery_error"
    )
    assert after["discovered_models"] is None, (
        "Prior discovered_models (None) must be preserved after an invalid_key result"
    )


# ---------------------------------------------------------------------------
# Scenario 10: Edit path + skip → skip_reason persisted, prior list preserved
# ---------------------------------------------------------------------------

def test_test_connection_edit_skip_persists_skip_reason(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Edit form, skip result (e.g. OAuth token):
      1. Create credential
      2. test-connection → skip probe result
      3. GET credential → models_discovery_error=skip_reason
    """
    # ── Phase 1: Create credential ────────────────────────────────────────
    cred = create_random_ai_credential(
        client, superuser_token_headers,
        credential_type="anthropic",
        api_key="sk-ant-api03-for-skip-test",
    )
    cred_id = cred["id"]

    # ── Phase 2: test-connection → oauth skip ─────────────────────────────
    with patch(_PROBE_TARGET, _probe_skip("oauth_token_unsupported")):
        status, body = _test_connection(
            client, superuser_token_headers,
            {
                "type": "anthropic",
                "credential_id": cred_id,
            },
        )

    assert status == 200
    assert body["success"] is True
    assert body["skip_reason"] == "oauth_token_unsupported"

    # ── Phase 3: GET credential → skip_reason persisted ───────────────────
    after = get_ai_credential(client, superuser_token_headers, cred_id)
    assert after["models_discovery_error"] == "oauth_token_unsupported"
    assert after["discovered_models"] is None  # unchanged (no fresh list)


# ---------------------------------------------------------------------------
# Scenario 11: Response shape invariant — model_count == len(models)
# ---------------------------------------------------------------------------

def test_test_connection_model_count_invariant(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    model_count must always equal len(models) regardless of the provider or result.
    Checked across: success (non-empty list), skip (empty list), invalid_key (empty).
    """
    # Success with list
    models_list = ["m1", "m2", "m3", "m4", "m5"]
    with patch(_PROBE_TARGET, _probe_success(models_list)):
        _, body = _test_connection(
            client, superuser_token_headers,
            {"type": "openai", "api_key": "sk-openai-count-test"},
        )
    assert body["model_count"] == len(body["models"])
    assert body["model_count"] == 5

    # Skip (empty list)
    with patch(_PROBE_TARGET, _probe_skip("no_list_endpoint")):
        _, body = _test_connection(
            client, superuser_token_headers,
            {"type": "minimax", "api_key": "mm-count-test"},
        )
    assert body["model_count"] == len(body["models"])
    assert body["model_count"] == 0

    # invalid_key (empty list)
    with patch(_PROBE_TARGET, _probe_invalid_key()):
        _, body = _test_connection(
            client, superuser_token_headers,
            {"type": "anthropic", "api_key": "sk-ant-api03-bad"},
        )
    assert body["model_count"] == len(body["models"])
    assert body["model_count"] == 0


# ---------------------------------------------------------------------------
# Scenario 12: Add form with api_key does NOT persist to any credential
# ---------------------------------------------------------------------------

def test_test_connection_add_form_does_not_touch_other_credentials(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When api_key is supplied without credential_id (Add case), no stored
    credential row must be modified. Two pre-existing credentials' discovery
    columns remain untouched after the test-connection call.
    """
    # ── Phase 1: Create two existing credentials ──────────────────────────
    cred_a = create_random_ai_credential(
        client, superuser_token_headers,
        credential_type="anthropic",
        api_key="sk-ant-api03-credential-a",
    )
    cred_b = create_random_ai_credential(
        client, superuser_token_headers,
        credential_type="openai",
        api_key="sk-openai-credential-b",
    )

    # ── Phase 2: test-connection with unrelated api_key ───────────────────
    with patch(_PROBE_TARGET, _probe_success(["some-model"])):
        _test_connection(
            client, superuser_token_headers,
            {
                "type": "anthropic",
                "api_key": "sk-ant-api03-fresh-probe-key",
            },
        )

    # ── Phase 3: Both stored credentials are unchanged ────────────────────
    after_a = get_ai_credential(client, superuser_token_headers, cred_a["id"])
    after_b = get_ai_credential(client, superuser_token_headers, cred_b["id"])

    assert after_a["discovered_models"] is None
    assert after_a["models_discovered_at"] is None
    assert after_a["models_discovery_error"] is None
    assert after_b["discovered_models"] is None
    assert after_b["models_discovered_at"] is None
    assert after_b["models_discovery_error"] is None
