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


# ── Account CLI helpers ───────────────────────────────────────────────────────


def create_account_setup_token(
    client: TestClient,
    headers: dict[str, str],
) -> dict:
    """POST /api/v1/cli/account/setup-tokens — create an account setup token.

    Requires the caller to have the ``agent-developer`` or ``admin`` role.
    Returns the ``CLISetupTokenCreated`` payload (includes ``token`` and
    ``setup_command``).
    """
    r = client.post(
        f"{_BASE}/account/setup-tokens",
        headers=headers,
    )
    assert r.status_code == 200, f"Create account setup token failed: {r.text}"
    return r.json()


def exchange_account_setup_token(
    client: TestClient,
    token_str: str,
    machine_name: str = "Test Account Machine",
    machine_info: str | None = None,
) -> dict:
    """POST /api/cli-setup/account/{token} — exchange an account setup token.

    No authentication required — the setup token is the credential.
    Returns ``{account_token, platform_url, frontend_url, machine_name}``.
    """
    r = client.post(
        f"/api/cli-setup/account/{token_str}",
        json={"machine_name": machine_name, "machine_info": machine_info},
    )
    assert r.status_code == 200, f"Exchange account setup token failed: {r.text}"
    return r.json()


def list_account_tokens(
    client: TestClient,
    headers: dict[str, str],
) -> list[dict]:
    """GET /api/v1/cli/account/tokens — list the user's account CLI tokens."""
    r = client.get(f"{_BASE}/account/tokens", headers=headers)
    assert r.status_code == 200, f"List account tokens failed: {r.text}"
    return r.json()["data"]


def revoke_account_token(
    client: TestClient,
    headers: dict[str, str],
    token_id: str,
) -> dict:
    """DELETE /api/v1/cli/account/tokens/{token_id} — revoke an account token.

    Cascade-revokes all child per-agent tokens minted by this account token.
    Returns the message with the revoke count.
    """
    r = client.delete(f"{_BASE}/account/tokens/{token_id}", headers=headers)
    assert r.status_code == 200, f"Revoke account token failed: {r.text}"
    return r.json()


def account_cli_headers(account_token_jwt: str) -> dict[str, str]:
    """Build Authorization headers for an account CLI JWT token."""
    return {"Authorization": f"Bearer {account_token_jwt}"}


def list_account_agents(
    client: TestClient,
    account_headers: dict[str, str],
) -> list[dict]:
    """GET /api/v1/cli/account/agents — list accessible agents via account token."""
    r = client.get(f"{_BASE}/account/agents", headers=account_headers)
    assert r.status_code == 200, f"List account agents failed: {r.text}"
    return r.json()["data"]


def list_account_user_workspaces(
    client: TestClient,
    account_headers: dict[str, str],
) -> list[dict]:
    """GET /api/v1/cli/account/user-workspaces — list the user's workspaces.

    Account-token-reachable catalogue the CLI uses for ``cinna account
    user-workspace list`` / ``--activate`` validation.
    """
    r = client.get(f"{_BASE}/account/user-workspaces", headers=account_headers)
    assert r.status_code == 200, f"List account user-workspaces failed: {r.text}"
    return r.json()["data"]


def account_create_agent(
    client: TestClient,
    account_headers: dict[str, str],
    name: str = "CLI Agent",
    description: str | None = None,
    user_workspace_id: str | None = None,
) -> dict:
    """POST /api/v1/cli/account/agents — thin-client agent create.

    Returns the full ``AgentPublic`` record. ``user_workspace_id`` targets the
    account user's active workspace (``None`` = Default).
    """
    body: dict = {"name": name}
    if description is not None:
        body["description"] = description
    if user_workspace_id is not None:
        body["user_workspace_id"] = user_workspace_id
    r = client.post(f"{_BASE}/account/agents", headers=account_headers, json=body)
    assert r.status_code == 200, f"Account create agent failed: {r.text}"
    return r.json()


def account_list_credentials(
    client: TestClient,
    account_headers: dict[str, str],
    user_workspace_id: str | None = None,
) -> list[dict]:
    """GET /api/v1/cli/account/credentials — metadata-only credential listing."""
    params = {} if user_workspace_id is None else {"user_workspace_id": user_workspace_id}
    r = client.get(
        f"{_BASE}/account/credentials", headers=account_headers, params=params
    )
    assert r.status_code == 200, f"Account list credentials failed: {r.text}"
    return r.json()["data"]


def account_create_credential(
    client: TestClient,
    account_headers: dict[str, str],
    name: str = "Draft Cred",
    cred_type: str = "api_token",
    notes: str | None = None,
    service_uri: str | None = None,
    allow_sharing: bool = False,
    user_workspace_id: str | None = None,
) -> dict:
    """POST /api/v1/cli/account/credentials — create a draft credential.

    Returns the ``AccountCredentialDraftResult`` ({credential, required_fields,
    setup_url}).
    """
    body: dict = {"name": name, "type": cred_type, "allow_sharing": allow_sharing}
    if notes is not None:
        body["notes"] = notes
    if service_uri is not None:
        body["service_uri"] = service_uri
    if user_workspace_id is not None:
        body["user_workspace_id"] = user_workspace_id
    r = client.post(
        f"{_BASE}/account/credentials", headers=account_headers, json=body
    )
    assert r.status_code == 200, f"Account create credential failed: {r.text}"
    return r.json()


def account_share_credential_with_agent(
    client: TestClient,
    account_headers: dict[str, str],
    credential_id: str,
    agent_id: str,
) -> dict:
    """POST /api/v1/cli/account/credentials/{id}/share-with-agent — attach to agent."""
    r = client.post(
        f"{_BASE}/account/credentials/{credential_id}/share-with-agent",
        headers=account_headers,
        json={"agent_id": agent_id},
    )
    assert r.status_code == 200, f"Account share credential failed: {r.text}"
    return r.json()


def mint_child_token(
    client: TestClient,
    account_headers: dict[str, str],
    agent_id: str,
    machine_name: str = "Test Child Machine",
    machine_info: str | None = None,
) -> dict:
    """POST /api/v1/cli/account/agents/{agent_id}/mint — mint a child CLI token.

    Requires building rights (developer role, not a foreign install). Returns the
    child token payload (JWT shown once) plus workspace-bootstrap fields.
    """
    r = client.post(
        f"{_BASE}/account/agents/{agent_id}/mint",
        headers=account_headers,
        json={"machine_name": machine_name, "machine_info": machine_info},
    )
    assert r.status_code == 200, f"Mint child token failed: {r.text}"
    return r.json()


def bootstrap_account_token(
    client: TestClient,
    user_headers: dict[str, str],
    machine_name: str = "Test Account Machine",
) -> tuple[str, str]:
    """Create and exchange an account setup token in one shot.

    Returns ``(account_jwt, account_token_id)`` where ``account_token_id``
    is obtained from the account tokens list (the JWT id is embedded in the
    token but this is more convenient for revocation tests).
    """
    setup = create_account_setup_token(client, user_headers)
    exchange = exchange_account_setup_token(client, setup["token"], machine_name=machine_name)
    account_jwt = exchange["account_token"]
    # Retrieve the token id from the list so callers can revoke it
    tokens = list_account_tokens(client, user_headers)
    assert tokens, "Account token should appear in the list after exchange"
    account_token_id = tokens[0]["id"]
    return account_jwt, account_token_id


def revoke_account_child_token(
    client: TestClient,
    account_headers: dict[str, str],
    child_token_id: str,
) -> dict:
    """DELETE /api/v1/cli/account/tokens/children/{child_token_id} — revoke a child token.

    Authenticated by an account CLI JWT (``AccountCLIContextDep``). Returns the
    ``{"message": "..."}`` response on success.
    """
    r = client.delete(
        f"{_BASE}/account/tokens/children/{child_token_id}",
        headers=account_headers,
    )
    assert r.status_code == 200, f"Revoke account child token failed: {r.text}"
    return r.json()


def get_account_context_package(
    client: TestClient,
    account_headers: dict[str, str],
) -> bytes:
    """GET /api/v1/cli/account/context-package — download the context tarball.

    Authenticated by an account CLI JWT (``AccountCLIContextDep``). Returns the
    raw response bytes (a gzip tarball).
    """
    r = client.get(f"{_BASE}/account/context-package", headers=account_headers)
    assert r.status_code == 200, f"Get account context package failed: {r.text}"
    return r.content
