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
from fastapi.testclient import TestClient

from app.core.config import settings

_BASE = f"{settings.API_V1_STR}/cli"


def _registered_cli_routes() -> set[tuple[str, str]]:
    """Return the set of ``(METHOD, path-template)`` pairs registered under /cli.

    Asserting against the live route table (rather than HTTP status codes) is the
    only way to prove a route is *absent* — a 401/404 from a request can also
    mean "registered but rejected/not-found", which proves nothing about
    registration.
    """
    from app.main import app

    pairs: set[tuple[str, str]] = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path or f"{settings.API_V1_STR}/cli/" not in path:
            continue
        for method in getattr(route, "methods", None) or {"WEBSOCKET"}:
            pairs.add((method, path))
    return pairs


def test_removed_cli_endpoints_are_not_registered() -> None:
    """The four removed CLI endpoints must be absent from the FastAPI route table.

    Each is asserted as a ``(METHOD, path-template)`` pair that does NOT appear
    among the registered /cli routes.
    """
    registered = _registered_cli_routes()

    removed = {
        ("GET", f"{settings.API_V1_STR}/cli/agents/{{agent_id}}/build-context"),
        ("GET", f"{settings.API_V1_STR}/cli/agents/{{agent_id}}/credentials"),
        ("POST", f"{settings.API_V1_STR}/cli/agents/{{agent_id}}/workspace"),
        ("GET", f"{settings.API_V1_STR}/cli/agents/{{agent_id}}/workspace/manifest"),
    }

    for method, path in removed:
        assert (method, path) not in registered, (
            f"{method} {path} must NOT be registered — it was removed in the "
            f"live-sync re-architecture. Registered /cli routes: {sorted(registered)}"
        )

    # Sanity: routes that were intentionally RETAINED must still be present, so
    # the absence assertions above aren't passing because the prefix changed.
    assert (
        "GET",
        f"{settings.API_V1_STR}/cli/agents/{{agent_id}}/workspace",
    ) in registered, "The one-shot GET /workspace clone endpoint must be retained"


def test_removed_cli_endpoints_do_not_serve_with_valid_cli_token(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Hitting the removed paths must never return 2xx — only 404/405.

    Complements the route-table check with a live request: a valid superuser
    token is presented so a 401 (auth rejection) cannot mask a still-live route.
    """
    import uuid

    fake_agent_id = str(uuid.uuid4())

    calls = [
        ("GET", f"{_BASE}/agents/{fake_agent_id}/build-context", None),
        ("GET", f"{_BASE}/agents/{fake_agent_id}/credentials", None),
        ("POST", f"{_BASE}/agents/{fake_agent_id}/workspace", b"fake-tarball-bytes"),
        ("GET", f"{_BASE}/agents/{fake_agent_id}/workspace/manifest", None),
    ]

    for method, url, body in calls:
        if method == "GET":
            r = client.get(url, headers=superuser_token_headers)
        else:
            r = client.post(url, headers=superuser_token_headers, content=body)
        assert r.status_code in (404, 405), (
            f"{method} {url} should be removed (404/405); got "
            f"{r.status_code}: {r.text}"
        )
