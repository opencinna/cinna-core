"""
Platform API helper — shared authentication and request utilities.
Import this in your scripts for authenticated API calls.
"""
import os
import sys
import httpx

BACKEND_URL = os.getenv("BACKEND_URL", "http://backend:8000")
AGENT_AUTH_TOKEN = os.getenv("AGENT_AUTH_TOKEN")

if not AGENT_AUTH_TOKEN:
    print("ERROR: AGENT_AUTH_TOKEN environment variable not set", file=sys.stderr)
    sys.exit(1)

HEADERS = {
    "Authorization": f"Bearer {AGENT_AUTH_TOKEN}",
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
