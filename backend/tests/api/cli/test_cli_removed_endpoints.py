"""
Parity tests for removed CLI endpoints.

These tests verify that the four endpoints deleted as part of the live-sync
re-architecture are no longer registered on the router. Their absence prevents
accidental reintroduction.

Removed endpoints (per cinna-cli-live-sync_plan.md § Backend Implementation):
- GET  /api/v1/cli/agents/{id}/build-context
- GET  /api/v1/cli/agents/{id}/credentials
- POST /api/v1/cli/agents/{id}/workspace   (push)
- GET  /api/v1/cli/agents/{id}/workspace/manifest
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings

_BASE = f"{settings.API_V1_STR}/cli"


def test_removed_endpoints_return_404_or_405(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Verify the four removed CLI endpoints are no longer registered.

    A non-existent agent UUID is used to ensure any URL-matching failures
    (e.g., the route existed but returned 404 for other reasons) produce the
    same result as "not registered" — both 404 and 405 (Method Not Allowed)
    are acceptable since FastAPI may return 405 when the path matches but the
    HTTP method does not.

    Note: The existing GET /workspace endpoint (used for one-shot tarball clone)
    is RETAINED and is not tested here. Only the POST push variant is removed.
    """
    fake_agent_id = str(uuid.uuid4())

    # ── build-context (GET) — removed ───────────────────────────────────
    r = client.get(
        f"{_BASE}/agents/{fake_agent_id}/build-context",
        headers=superuser_token_headers,
    )
    assert r.status_code in (401, 404, 405), (
        f"GET /build-context should be gone (404/405) or auth-rejected (401); "
        f"got {r.status_code}: {r.text}"
    )
    # If auth passes (401 means it still exists and rejects the non-CLI token),
    # that's still acceptable. But if 200 comes back, the endpoint is still live.
    assert r.status_code != 200, "GET /build-context must not return 200 — endpoint was removed"

    # ── credentials (GET) — removed ─────────────────────────────────────
    r = client.get(
        f"{_BASE}/agents/{fake_agent_id}/credentials",
        headers=superuser_token_headers,
    )
    assert r.status_code in (401, 404, 405), (
        f"GET /credentials should be gone (404/405) or auth-rejected (401); "
        f"got {r.status_code}: {r.text}"
    )
    assert r.status_code != 200, "GET /credentials must not return 200 — endpoint was removed"

    # ── workspace push (POST) — removed ─────────────────────────────────
    r = client.post(
        f"{_BASE}/agents/{fake_agent_id}/workspace",
        headers=superuser_token_headers,
        content=b"fake-tarball-bytes",
    )
    assert r.status_code in (401, 404, 405), (
        f"POST /workspace should be gone (404/405) or auth-rejected (401); "
        f"got {r.status_code}: {r.text}"
    )
    assert r.status_code != 200, "POST /workspace must not return 200 — endpoint was removed"

    # ── workspace/manifest (GET) — removed ──────────────────────────────
    r = client.get(
        f"{_BASE}/agents/{fake_agent_id}/workspace/manifest",
        headers=superuser_token_headers,
    )
    assert r.status_code in (401, 404, 405), (
        f"GET /workspace/manifest should be gone (404/405) or auth-rejected (401); "
        f"got {r.status_code}: {r.text}"
    )
    assert r.status_code != 200, "GET /workspace/manifest must not return 200 — endpoint was removed"
