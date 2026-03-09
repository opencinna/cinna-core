"""
Security Events API Tests

Tests for:
- POST /api/v1/security-events/report  — blockable ingest (returns action decision)
- POST /api/v1/security-events/        — fire-and-forget ingest
- GET  /api/v1/security-events/        — list events (paginated, filterable)

All tests use the HTTP API exclusively (no direct DB access).
Auth: Standard user JWT via superuser_token_headers fixture.
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings


# ── Helpers ────────────────────────────────────────────────────────────────────

BASE_URL = f"{settings.API_V1_STR}/security-events"


def _report_url() -> str:
    return f"{BASE_URL}/report"


def _ingest_url() -> str:
    return f"{BASE_URL}/"


def _list_url() -> str:
    return f"{BASE_URL}/"


def _valid_report_payload(event_type: str = "CREDENTIAL_READ_ATTEMPT") -> dict:
    return {
        "event_type": event_type,
        "tool_name": "Read",
        "tool_input": "/app/workspace/credentials/credentials.json",
        "severity": "high",
        "details": {"test": True},
    }


def _valid_ingest_payload(event_type: str = "OUTPUT_REDACTED") -> dict:
    return {
        "event_type": event_type,
        "severity": "medium",
        "details": {"test": True},
    }


# ── POST /report — blockable ingest ───────────────────────────────────────────

def test_report_security_event_returns_allow(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Blockable report endpoint always returns action='allow' initially."""
    response = client.post(
        _report_url(),
        headers=superuser_token_headers,
        json=_valid_report_payload("CREDENTIAL_READ_ATTEMPT"),
    )
    assert response.status_code == 200
    content = response.json()
    assert content["action"] == "allow"
    assert content["reason"] is None


def test_report_security_event_logs_event(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Reporting via /report creates a retrievable event in the list endpoint."""
    report_payload = _valid_report_payload("CREDENTIAL_BASH_ACCESS")
    report_response = client.post(
        _report_url(),
        headers=superuser_token_headers,
        json=report_payload,
    )
    assert report_response.status_code == 200

    # The event should appear in the list
    list_response = client.get(
        _list_url(),
        headers=superuser_token_headers,
        params={"event_type": "CREDENTIAL_BASH_ACCESS"},
    )
    assert list_response.status_code == 200
    content = list_response.json()
    assert content["count"] >= 1
    events = content["data"]
    assert any(e["event_type"] == "CREDENTIAL_BASH_ACCESS" for e in events)


def test_report_security_event_with_text_fields(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Report endpoint accepts all non-FK optional fields."""
    payload = {
        "event_type": "CREDENTIAL_WRITE_ATTEMPT",
        "tool_name": "Write",
        "tool_input": "/app/workspace/credentials/credentials.json",
        "severity": "critical",
        "details": {"reason": "test"},
    }
    response = client.post(
        _report_url(),
        headers=superuser_token_headers,
        json=payload,
    )
    assert response.status_code == 200
    assert response.json()["action"] == "allow"


def test_report_security_event_requires_auth(client: TestClient) -> None:
    """Report endpoint requires authentication."""
    response = client.post(
        _report_url(),
        json=_valid_report_payload(),
    )
    assert response.status_code == 401


def test_report_security_event_requires_event_type(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Report endpoint requires event_type field."""
    response = client.post(
        _report_url(),
        headers=superuser_token_headers,
        json={"tool_name": "Read"},  # Missing event_type
    )
    assert response.status_code == 422


def test_report_security_event_invalid_uuid_fields_are_ignored(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Invalid UUID strings in optional fields should not cause 500 — they are silently ignored."""
    payload = {
        "event_type": "CREDENTIAL_READ_ATTEMPT",
        "session_id": "not-a-uuid",
        "environment_id": "also-not-a-uuid",
        "severity": "high",
    }
    response = client.post(
        _report_url(),
        headers=superuser_token_headers,
        json=payload,
    )
    # Should succeed — invalid UUIDs are parsed as None by _safe_uuid()
    assert response.status_code == 200
    assert response.json()["action"] == "allow"


# ── POST / — fire-and-forget ingest ──────────────────────────────────────────

def test_ingest_security_event_creates_event(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Fire-and-forget ingest creates and returns the event."""
    payload = _valid_ingest_payload("OUTPUT_REDACTED")
    response = client.post(
        _ingest_url(),
        headers=superuser_token_headers,
        json=payload,
    )
    assert response.status_code == 200
    content = response.json()
    assert content["event_type"] == "OUTPUT_REDACTED"
    assert content["severity"] == "medium"
    assert "id" in content
    assert "created_at" in content
    assert "user_id" in content
    assert isinstance(content["details"], dict)


def test_ingest_security_event_response_shape(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Ingest response has all required SecurityEventPublic fields."""
    response = client.post(
        _ingest_url(),
        headers=superuser_token_headers,
        json=_valid_ingest_payload(),
    )
    assert response.status_code == 200
    content = response.json()

    required_fields = [
        "id", "created_at", "user_id", "agent_id", "environment_id",
        "session_id", "guest_share_id", "event_type", "severity",
        "details", "risk_score",
    ]
    for field in required_fields:
        assert field in content, f"Missing field: {field}"

    # Optional fields are null for minimal payload
    assert content["agent_id"] is None
    assert content["environment_id"] is None
    assert content["session_id"] is None
    assert content["guest_share_id"] is None
    assert content["risk_score"] is None


def test_ingest_security_event_requires_auth(client: TestClient) -> None:
    """Ingest endpoint requires authentication."""
    response = client.post(
        _ingest_url(),
        json=_valid_ingest_payload(),
    )
    assert response.status_code == 401


def test_ingest_security_event_details_preserved(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Details dict is stored and returned correctly."""
    details = {"redacted_field": "api_token", "session_count": 3, "nested": {"key": "val"}}
    payload = {
        "event_type": "OUTPUT_REDACTED",
        "severity": "low",
        "details": details,
    }
    response = client.post(
        _ingest_url(),
        headers=superuser_token_headers,
        json=payload,
    )
    assert response.status_code == 200
    content = response.json()
    assert content["details"] == details


def test_ingest_security_event_default_severity(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Severity defaults to 'medium' if not specified."""
    response = client.post(
        _ingest_url(),
        headers=superuser_token_headers,
        json={"event_type": "OUTPUT_REDACTED"},
    )
    assert response.status_code == 200
    assert response.json()["severity"] == "medium"


# ── GET / — list events ────────────────────────────────────────────────────────

def test_list_security_events_empty(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """List endpoint returns a valid (possibly empty) response."""
    # Use a random event_type filter that won't match anything
    random_type = f"TEST_TYPE_{uuid.uuid4().hex[:8].upper()}"
    response = client.get(
        _list_url(),
        headers=superuser_token_headers,
        params={"event_type": random_type},
    )
    assert response.status_code == 200
    content = response.json()
    assert "data" in content
    assert "count" in content
    assert isinstance(content["data"], list)
    assert content["count"] == 0


def test_list_security_events_pagination(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """List endpoint respects skip and limit parameters."""
    unique_type = f"PAGINATION_TEST_{uuid.uuid4().hex[:8].upper()}"

    # Create 3 events
    for _ in range(3):
        client.post(
            _ingest_url(),
            headers=superuser_token_headers,
            json={"event_type": unique_type, "severity": "low"},
        )

    # Get first 2
    response = client.get(
        _list_url(),
        headers=superuser_token_headers,
        params={"event_type": unique_type, "skip": 0, "limit": 2},
    )
    assert response.status_code == 200
    content = response.json()
    assert content["count"] == 3
    assert len(content["data"]) == 2

    # Get remaining 1
    response = client.get(
        _list_url(),
        headers=superuser_token_headers,
        params={"event_type": unique_type, "skip": 2, "limit": 2},
    )
    assert response.status_code == 200
    content = response.json()
    assert len(content["data"]) == 1


def test_list_security_events_ordered_newest_first(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """List returns events ordered newest first."""
    unique_type = f"ORDER_TEST_{uuid.uuid4().hex[:8].upper()}"

    # Create 2 events
    first = client.post(
        _ingest_url(),
        headers=superuser_token_headers,
        json={"event_type": unique_type, "details": {"order": 1}},
    ).json()
    second = client.post(
        _ingest_url(),
        headers=superuser_token_headers,
        json={"event_type": unique_type, "details": {"order": 2}},
    ).json()

    response = client.get(
        _list_url(),
        headers=superuser_token_headers,
        params={"event_type": unique_type},
    )
    assert response.status_code == 200
    events = response.json()["data"]
    assert len(events) >= 2

    # Newest (second) should come first
    ids = [e["id"] for e in events]
    assert ids.index(second["id"]) < ids.index(first["id"])


def test_list_security_events_user_scoped(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    normal_user_token_headers: dict[str, str],
) -> None:
    """Events are scoped to the creating user — one user cannot see another's events."""
    unique_type = f"SCOPE_TEST_{uuid.uuid4().hex[:8].upper()}"

    # Superuser creates an event
    client.post(
        _ingest_url(),
        headers=superuser_token_headers,
        json={"event_type": unique_type},
    )

    # Normal user should not see superuser's events
    response = client.get(
        _list_url(),
        headers=normal_user_token_headers,
        params={"event_type": unique_type},
    )
    assert response.status_code == 200
    content = response.json()
    assert content["count"] == 0
    assert content["data"] == []


def test_list_security_events_filter_by_event_type(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """event_type filter returns only matching events."""
    base = uuid.uuid4().hex[:6].upper()
    type_a = f"TYPE_A_{base}"
    type_b = f"TYPE_B_{base}"

    client.post(_ingest_url(), headers=superuser_token_headers, json={"event_type": type_a})
    client.post(_ingest_url(), headers=superuser_token_headers, json={"event_type": type_b})
    client.post(_ingest_url(), headers=superuser_token_headers, json={"event_type": type_a})

    response = client.get(
        _list_url(),
        headers=superuser_token_headers,
        params={"event_type": type_a},
    )
    assert response.status_code == 200
    content = response.json()
    assert content["count"] == 2
    for event in content["data"]:
        assert event["event_type"] == type_a


def test_list_security_events_requires_auth(client: TestClient) -> None:
    """List endpoint requires authentication."""
    response = client.get(_list_url())
    assert response.status_code == 401


def test_list_security_events_limit_validation(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Limit must be between 1 and 200."""
    # limit=0 should fail
    response = client.get(
        _list_url(),
        headers=superuser_token_headers,
        params={"limit": 0},
    )
    assert response.status_code == 422

    # limit=201 should fail
    response = client.get(
        _list_url(),
        headers=superuser_token_headers,
        params={"limit": 201},
    )
    assert response.status_code == 422

    # limit=200 should succeed
    response = client.get(
        _list_url(),
        headers=superuser_token_headers,
        params={"limit": 200},
    )
    assert response.status_code == 200


def test_list_security_events_skip_validation(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Skip must be >= 0."""
    response = client.get(
        _list_url(),
        headers=superuser_token_headers,
        params={"skip": -1},
    )
    assert response.status_code == 422
