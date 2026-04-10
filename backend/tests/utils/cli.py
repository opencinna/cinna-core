"""Utility helpers for CLI API tests."""
import uuid
from fastapi.testclient import TestClient

from app.core.config import settings

_BASE = f"{settings.API_V1_STR}/cli"


def create_setup_token(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
) -> dict:
    """POST /api/v1/cli/setup-tokens — create a setup token for an agent."""
    r = client.post(
        f"{_BASE}/setup-tokens",
        headers=headers,
        json={"agent_id": agent_id},
    )
    assert r.status_code == 200, f"Create setup token failed: {r.text}"
    return r.json()


def exchange_setup_token(
    client: TestClient,
    token_str: str,
    machine_name: str = "Test Machine",
    machine_info: str | None = None,
) -> dict:
    """POST /api/cli-setup/{token} — exchange a setup token for a CLI JWT.

    Note: this endpoint is mounted at /api/cli-setup, NOT under /api/v1.
    """
    r = client.post(
        f"/api/cli-setup/{token_str}",
        json={"machine_name": machine_name, "machine_info": machine_info},
    )
    assert r.status_code == 200, f"Exchange setup token failed: {r.text}"
    return r.json()


def list_cli_tokens(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str | None = None,
) -> list[dict]:
    """GET /api/v1/cli/tokens — list CLI tokens for the current user."""
    params = {}
    if agent_id is not None:
        params["agent_id"] = agent_id
    r = client.get(f"{_BASE}/tokens", headers=headers, params=params)
    assert r.status_code == 200, f"List CLI tokens failed: {r.text}"
    return r.json()["data"]


def revoke_cli_token(
    client: TestClient,
    headers: dict[str, str],
    token_id: str,
) -> dict:
    """DELETE /api/v1/cli/tokens/{token_id} — revoke a CLI token."""
    r = client.delete(f"{_BASE}/tokens/{token_id}", headers=headers)
    assert r.status_code == 200, f"Revoke CLI token failed: {r.text}"
    return r.json()


def cli_auth_headers(cli_token_jwt: str) -> dict[str, str]:
    """Build Authorization headers for a CLI JWT token."""
    return {"Authorization": f"Bearer {cli_token_jwt}"}
