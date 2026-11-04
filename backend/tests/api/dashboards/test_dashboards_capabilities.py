"""``GET /dashboards/capabilities``: shape, values, auth, routing, api-proxy reach.

Scenarios:
  1. Shape and values: every limit equals the constant that enforces it;
     contract_version equals the kit's CONTRACT_VERSION member.
  2. Auth: no token -> 401/403; the literal ``capabilities`` is not parsed as a
     dashboard UUID (no 422), while a real UUID path is still a dashboard route.
  3. Reachable through the account api-proxy with an account CLI token.
  4. Dashboard frame caps / page-state writes are refused through the proxy
     (no X-Cinna-Proxied marker) while other component routes are not refused.
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from app.services.cli.platform_knowledge_assets import local_kit_dir
from app.services.dashboards import dashboard_context, run_budget
from app.services.dashboards.capabilities import (
    CAPABILITIES_VERSION,
    ENDPOINT_DEFAULT_TIMEOUT_S,
    ENDPOINT_MAX_STDOUT_BYTES,
    ENDPOINT_MAX_TIMEOUT_S,
)
from app.services.users.user_dashboard_service import (
    MAX_BLOCKS_PER_DASHBOARD,
    MAX_DASHBOARDS_PER_USER,
)
from tests.utils.cli import account_cli_headers, bootstrap_account_token

API = settings.API_V1_STR
URL = f"{API}/dashboards/capabilities"
PROXY = f"{API}/cli/account/api-proxy"


def _expected_limits() -> dict:
    return {
        "runs_per_minute": run_budget.MAX_RUNS_PER_WINDOW,
        "blocks_per_dashboard": MAX_BLOCKS_PER_DASHBOARD,
        "dashboards_per_user": MAX_DASHBOARDS_PER_USER,
        "context_chars": dashboard_context.MAX_CHARS,
        "summary_chars": dashboard_context.SUMMARY_MAX_CHARS,
        "stdout_bytes": ENDPOINT_MAX_STDOUT_BYTES,
        "timeout_s_default": ENDPOINT_DEFAULT_TIMEOUT_S,
        "timeout_s_max": ENDPOINT_MAX_TIMEOUT_S,
    }


def test_capabilities_shape_and_values(
    client: TestClient, normal_user_token_headers: dict[str, str]
) -> None:
    r = client.get(URL, headers=normal_user_token_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"version", "contract_version", "limits", "chat", "component_refs"}
    assert body["version"] == CAPABILITIES_VERSION == 1
    assert body["chat"] is True
    assert body["component_refs"] is True
    kit_version = (local_kit_dir() / "CONTRACT_VERSION").read_text().strip()
    assert body["contract_version"] == kit_version
    assert body["limits"] == _expected_limits()
    assert all(isinstance(v, int) and v > 0 for v in body["limits"].values())


def test_capabilities_requires_auth(client: TestClient) -> None:
    r = client.get(URL)
    assert r.status_code in (401, 403), r.text
    r = client.get(URL, headers={"Authorization": "Bearer not-a-token"})
    assert r.status_code in (401, 403), r.text


def test_capabilities_is_not_parsed_as_a_dashboard_id(
    client: TestClient, normal_user_token_headers: dict[str, str]
) -> None:
    r = client.get(URL, headers=normal_user_token_headers)
    assert r.status_code == 200, r.text
    assert "limits" in r.json()
    # A real dashboard id path is still the dashboard route (404 for unknown).
    r = client.get(f"{API}/dashboards/{uuid.uuid4()}", headers=normal_user_token_headers)
    assert r.status_code == 404, r.text
    assert "limits" not in r.json()


def test_capabilities_reachable_through_account_api_proxy(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    jwt, _ = bootstrap_account_token(client, superuser_token_headers, machine_name="Dash Caps")
    acc = account_cli_headers(jwt)
    r = client.post(PROXY, headers=acc, json={"method": "GET", "path": "dashboards/capabilities"})
    assert r.status_code == 200, r.text
    assert r.headers.get("X-Cinna-Proxied") == "1"
    direct = client.get(URL, headers=superuser_token_headers).json()
    assert r.json() == direct
    assert r.json()["limits"] == _expected_limits()


def test_dashboard_frame_and_page_state_routes_refused_through_api_proxy(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    jwt, _ = bootstrap_account_token(client, superuser_token_headers, machine_name="Dash Frames")
    acc = account_cli_headers(jwt)
    a, d, b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    refused = [
        ("POST", f"agents/{a}/dashboards/revenue/frame"),
        ("POST", f"dashboards/{d}/blocks/{b}/component/frame"),
        ("PUT", f"dashboards/{d}/blocks/{b}/component/page-state"),
    ]
    for method, path in refused:
        r = client.post(PROXY, headers=acc, json={"method": method, "path": path, "body": {}})
        assert r.status_code == 403, (method, path, r.text)
        assert "X-Cinna-Proxied" not in r.headers, (method, path)
    # Sibling component routes pass the policy (dispatched; inner 404 mirrored).
    for method, path in [
        ("POST", f"dashboards/{d}/blocks/{b}/component/run"),
        ("GET", f"dashboards/{d}/blocks/{b}/component/data"),
    ]:
        r = client.post(PROXY, headers=acc, json={"method": method, "path": path, "body": {}})
        assert r.headers.get("X-Cinna-Proxied") == "1", (method, path, r.text)
