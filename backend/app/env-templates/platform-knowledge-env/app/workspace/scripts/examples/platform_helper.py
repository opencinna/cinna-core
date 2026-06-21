"""
Platform API helper — shared authentication and request utilities.
Import this in your scripts for authenticated API calls.

IMPORTANT (security scoping): ``AGENT_AUTH_TOKEN`` is a SCOPED agent-environment
token. It authenticates ONLY the environment's own callback routes
(``/agent/tasks/*``, ``/knowledge/query``, ``/security-events/report``, and the
``/environments/{id}/...`` callbacks) via the backend's ``AgentEnvContextDep``.
It is REJECTED by the generic ``CurrentUser`` dependency, so it CANNOT call
owner-wide endpoints such as ``/credentials/*``, ``/agents/*``, ``/sessions/*``,
``/workspaces/*`` or ``/mail-servers/*`` — those now return 401/403. The example
scripts in this directory that call those endpoints are historical and will not
authenticate with the env token; they are kept only as API-shape references.
Requests through this helper must also send the ``X-Agent-Env-Id`` header for the
scoped routes (see ``ENV_ID`` below).
"""
import os
import sys
import httpx

BACKEND_URL = os.getenv("BACKEND_URL", "http://backend:8000")
AGENT_AUTH_TOKEN = os.getenv("AGENT_AUTH_TOKEN")
ENV_ID = os.getenv("ENV_ID", "")  # scopes the auth token to this environment

if not AGENT_AUTH_TOKEN:
    print("ERROR: AGENT_AUTH_TOKEN environment variable not set", file=sys.stderr)
    sys.exit(1)

HEADERS = {
    "Authorization": f"Bearer {AGENT_AUTH_TOKEN}",
    "X-Agent-Env-Id": ENV_ID,
    "Content-Type": "application/json",
}


def api_get(path: str, params: dict | None = None, timeout: float = 30.0) -> dict:
    """GET request to platform API."""
    response = httpx.get(f"{BACKEND_URL}{path}", headers=HEADERS, params=params, timeout=timeout)
    response.raise_for_status()
    return response.json()


def api_post(path: str, json: dict | None = None, timeout: float = 30.0) -> dict:
    """POST request to platform API."""
    response = httpx.post(f"{BACKEND_URL}{path}", headers=HEADERS, json=json, timeout=timeout)
    response.raise_for_status()
    return response.json()


def api_put(path: str, json: dict | None = None, timeout: float = 30.0) -> dict:
    """PUT request to platform API."""
    response = httpx.put(f"{BACKEND_URL}{path}", headers=HEADERS, json=json, timeout=timeout)
    response.raise_for_status()
    return response.json()


def api_patch(path: str, json: dict | None = None, timeout: float = 30.0) -> dict:
    """PATCH request to platform API."""
    response = httpx.patch(f"{BACKEND_URL}{path}", headers=HEADERS, json=json, timeout=timeout)
    response.raise_for_status()
    return response.json()


def api_delete(path: str, timeout: float = 30.0) -> dict:
    """DELETE request to platform API."""
    response = httpx.delete(f"{BACKEND_URL}{path}", headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.json()
