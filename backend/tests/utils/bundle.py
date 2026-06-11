"""Shared helpers for agent bundle / catalog API tests.

These consolidate the near-identical private helpers that were copy-pasted
across the ``tests/api/agents/agents_bundles_*`` files: creating a fresh user
with a default AI credential, publishing an agent into a bundle revision,
flipping a bundle to public/listed, installing from the catalog, and creating /
linking service credentials.

Behavior is intentionally identical to the migrated call sites. Where the local
copies diverged (publish returning the *agent row* vs the *revision*, optional
visibility flip, ``expected_status``), the divergence is exposed as a parameter
rather than baked in, so each call site keeps its original semantics.

FS seam note (schema_version 2 workspace check):
  ``PublishService._assert_workspace_readable`` blocks publish when an active
  env exists but its ``app/workspace/`` dir is absent. Since the test
  environment adapter stubs out Docker (no real FS), the ``setup_environment_adapter``
  fixture in ``tests/utils/fixtures.py`` handles this at the infrastructure level:
  it creates ``app/workspace`` inside the test template dir so every new env
  instance has the directory, AND patches ``settings.ENV_INSTANCES_DIR`` to match
  the lifecycle manager's tmp instances dir.  Publish helpers here do not need to
  create the workspace dir themselves.  Tests that want to assert on workspace
  content (e.g. captured files) should seed files into
  ``Path(settings.ENV_INSTANCES_DIR) / env_id / "app" / "workspace"`` directly.
"""
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.user import create_random_user, user_authentication_headers

API = settings.API_V1_STR


def make_user_and_headers(client: TestClient) -> tuple[dict, dict[str, str]]:
    """Create a fresh user with a default AI credential; return (user, headers).

    The default AI credential is required so that installing a bundle can
    provision the new agent's environment.
    """
    user = create_random_user(client)
    headers = user_authentication_headers(
        client=client, email=user["email"], password=user["_password"]
    )
    create_random_ai_credential(client, headers, set_default=True)
    return user, headers


def create_bundle_credential(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str | None = None,
    cred_type: str = "api_token",
    allow_sharing: bool = False,
) -> dict:
    """Create a service credential via POST /credentials/.

    Defaults to an ``api_token`` credential with a bearer template — the shape
    used by the bundle install/credential tests.
    """
    name = name or f"cred-{uuid.uuid4().hex[:8]}"
    r = client.post(
        f"{API}/credentials/",
        headers=headers,
        json={
            "name": name,
            "type": cred_type,
            "allow_sharing": allow_sharing,
            "credential_data": {
                "api_token_type": "bearer",
                "api_token_template": "Authorization: Bearer {TOKEN}",
                "api_token": "test-token-value",
            },
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def link_bundle_credential_to_agent(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    credential_id: str,
) -> None:
    """Link a credential to an agent via POST /agents/{id}/credentials."""
    r = client.post(
        f"{API}/agents/{agent_id}/credentials",
        headers=headers,
        json={"credential_id": credential_id},
    )
    assert r.status_code in (200, 201), r.text


def publish_bundle(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    *,
    notes: str | None = None,
) -> dict:
    """Publish an agent, drain tasks, and return the agent's fresh row.

    This is the dominant publish shape across the bundle tests: callers need
    the refreshed agent (for ``bundle_uuid`` / ``active_environment_id``).
    """
    r = client.post(
        f"{API}/agents/{agent_id}/publish",
        headers=headers,
        json={"release_notes": notes} if notes else {},
    )
    assert r.status_code == 200, r.text
    drain_tasks()
    return client.get(f"{API}/agents/{agent_id}", headers=headers).json()


def publish_bundle_revision(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    *,
    notes: str | None = None,
    expected_status: int = 200,
) -> dict:
    """Publish an agent and return the *revision* JSON from the publish call.

    Use this when the caller cares about the revision payload (e.g. asserting
    on revision columns) rather than the refreshed agent row. Does NOT flip
    visibility. Honors ``expected_status`` for failure-path assertions; drains
    tasks only on success.
    """
    r = client.post(
        f"{API}/agents/{agent_id}/publish",
        headers=headers,
        json={"release_notes": notes} if notes else {},
    )
    assert r.status_code == expected_status, (
        f"Expected {expected_status}; got {r.status_code}: {r.text}"
    )
    body = r.json()
    if r.status_code == 200:
        drain_tasks()
    return body


def make_bundle_public(
    client: TestClient,
    headers: dict[str, str],
    bundle_uuid: str,
    *,
    is_listed: bool = True,
    visibility: str = "public",
) -> None:
    """Flip a bundle to listed/public via PATCH /bundles/{uuid}."""
    r = client.patch(
        f"{API}/bundles/{bundle_uuid}",
        headers=headers,
        json={"is_listed": is_listed, "visibility": visibility},
    )
    assert r.status_code == 200, r.text


def publish_bundle_and_make_public(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    *,
    notes: str | None = None,
    visibility: str = "public",
    is_listed: bool = True,
) -> dict:
    """Publish an agent and flip its bundle to listed/public in one shot.

    Returns the *revision* JSON from the publish call (not the agent row).
    Looks up ``bundle_uuid`` from the freshly published agent to perform the
    visibility PATCH.
    """
    r = client.post(
        f"{API}/agents/{agent_id}/publish",
        headers=headers,
        json={"release_notes": notes} if notes else {},
    )
    assert r.status_code == 200, r.text
    revision = r.json()
    drain_tasks()

    fresh = client.get(f"{API}/agents/{agent_id}", headers=headers).json()
    bundle_uuid = fresh["bundle_uuid"]
    assert bundle_uuid is not None
    make_bundle_public(
        client, headers, bundle_uuid, is_listed=is_listed, visibility=visibility
    )
    return revision


def install_bundle(
    client: TestClient,
    headers: dict[str, str],
    bundle_id: str,
    *,
    request_body: dict | None = None,
    expected_status: int = 200,
) -> dict:
    """Install a bundle via POST /catalog/{bundle_id}/install.

    Drains tasks on success (the install provisions the new agent's env). Honors
    ``expected_status`` so callers can assert rejection paths inline.
    """
    r = client.post(
        f"{API}/catalog/{bundle_id}/install",
        headers=headers,
        json=request_body or {},
    )
    assert r.status_code == expected_status, (
        f"Expected {expected_status}; got {r.status_code}: {r.text}"
    )
    if r.status_code == 200:
        drain_tasks()
    return r.json()


def get_install_context(
    client: TestClient,
    headers: dict[str, str],
    bundle_id: str,
) -> dict:
    """GET /catalog/{bundle_id}/install-context."""
    r = client.get(
        f"{API}/catalog/{bundle_id}/install-context",
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()
