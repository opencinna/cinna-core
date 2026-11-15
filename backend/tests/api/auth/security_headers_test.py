"""Baseline security headers on backend responses (``SecurityHeadersMiddleware``).

* ``X-Content-Type-Options: nosniff`` everywhere except agent-served content
  (``/webapp``, ``/agents/{id}/webapp``, ``/shared/workspace``);
* ``Referrer-Policy: strict-origin-when-cross-origin`` everywhere;
* ``Strict-Transport-Security`` only on https requests (scheme, or
  ``X-Forwarded-Proto: https`` from a private-network proxy peer — the
  TestClient peer ``testclient`` is not one), never over plain http.
"""
from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.core.config import settings

API = settings.API_V1_STR
HEALTH = f"{API}/utils/health-check/"


def test_nosniff_and_referrer_policy_on_api_responses(client: TestClient) -> None:
    """Success, 401 and 404 responses all carry both headers; no HSTS over http."""
    for path in (HEALTH, f"{API}/users/me", f"{API}/no-such-route"):
        r = client.get(path)
        assert r.headers.get("x-content-type-options") == "nosniff", path
        assert r.headers.get("referrer-policy") == "strict-origin-when-cross-origin", path
        assert "strict-transport-security" not in r.headers, path

    # A spoofed forwarded scheme from a non-proxy peer does not turn HSTS on.
    r = client.get(HEALTH, headers={"X-Forwarded-Proto": "https"})
    assert "strict-transport-security" not in r.headers


def test_hsts_only_over_https(client: TestClient) -> None:
    """``https://`` requests get HSTS with the configured max-age."""
    # Same app (and DB override) as the fixture client, https base URL.
    https = TestClient(client.app, base_url="https://testserver")
    r = https.get(HEALTH)
    assert r.status_code == 200, r.text
    assert (
        r.headers.get("strict-transport-security")
        == f"max-age={settings.SECURITY_HSTS_MAX_AGE_SECONDS}"
    )
    assert r.headers.get("x-content-type-options") == "nosniff"


def test_agent_served_content_paths_skip_nosniff(client: TestClient) -> None:
    """Agent content prefixes keep the Referrer-Policy but not nosniff."""
    agent_id = uuid.uuid4()
    for path in (
        f"{API}/agents/{agent_id}/webapp/index.html",
        f"{API}/webapp/{uuid.uuid4()}/index.html",
        f"{API}/shared/workspace/{uuid.uuid4()}",
    ):
        r = client.get(path)
        assert "x-content-type-options" not in r.headers, (path, r.status_code)
        assert r.headers.get("referrer-policy") == "strict-origin-when-cross-origin", path
    # A sibling route under /agents is not exempt.
    r = client.get(f"{API}/agents/{agent_id}")
    assert r.headers.get("x-content-type-options") == "nosniff"
