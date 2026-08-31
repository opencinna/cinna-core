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
from datetime import datetime, timedelta, UTC
from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import Session

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


def set_update_mode(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    mode: str,
) -> dict:
    """Flip an install's update mode via PATCH /agents/{id}/update-mode."""
    r = client.patch(
        f"{API}/agents/{agent_id}/update-mode",
        headers=headers,
        json={"update_mode": mode},
    )
    assert r.status_code == 200, r.text
    return r.json()


# ── Bundle-auto-update sweep bookkeeping — documented DB seams ────────────
#
# InstallService.sweep_automatic_updates (bundle_auto_update_and_install_ux
# plan, section 3.2) drives its selection off two internal bookkeeping
# columns — Agent.installed_revision_id and the failure-backoff pair
# (Agent.last_update_status / Agent.last_update_attempt_at) — that have no
# public API seam to set directly:
#
#   - installed_revision_id only ever moves *forward*, and only via
#     apply_update / PublishService.publish (the publisher's own install is
#     kept in permanent lockstep with the bundle's latest revision by
#     ``publish`` itself, before the sweep ever runs). There is no route
#     that can put an install "behind" other than the natural
#     publish-creates-a-gap flow the sweep itself closes.
#   - last_update_attempt_at is intentionally NOT exposed on AgentPublic
#     (plan §3.1) and is only ever written by the sweep / apply_update
#     internals — there is no endpoint that sets it.
#
# These two helpers poke those columns directly on the test session, mirroring
# the documented DB-seam pattern in tests/utils/environment.py
# (set_environment_status / link_ai_credential_to_environment) for state that
# is otherwise unreachable through the API.


def force_install_revision(
    db: Session,
    install_id: str | uuid.UUID,
    revision_id: str | uuid.UUID,
) -> None:
    """Force ``Agent.installed_revision_id`` directly on the test DB.

    Used to construct an install that is artificially "behind" the bundle's
    latest revision in ways the public API cannot produce — in particular,
    proving that ``sweep_automatic_updates`` excludes
    ``is_publisher_install=True`` rows even when they would otherwise match
    its selection query.
    """
    from app.models.agents.agent import Agent

    if isinstance(install_id, str):
        install_id = uuid.UUID(install_id)
    if isinstance(revision_id, str):
        revision_id = uuid.UUID(revision_id)

    install = db.get(Agent, install_id)
    assert install is not None, f"Install {install_id} not found"
    install.installed_revision_id = revision_id
    db.add(install)
    db.commit()


def stamp_install_update_failure(
    db: Session,
    install_id: str | uuid.UUID,
    *,
    hours_ago: float,
) -> None:
    """Stamp ``last_update_status='failed'`` + a recent ``last_update_attempt_at``.

    Simulates a previously-failed automatic-update attempt so the sweep's
    retry-backoff filter (``AUTO_UPDATE_RETRY_BACKOFF_HOURS``) can be
    exercised. Neither column is settable via any route — see the module
    docstring above.
    """
    from app.models.agents.agent import Agent

    if isinstance(install_id, str):
        install_id = uuid.UUID(install_id)

    install = db.get(Agent, install_id)
    assert install is not None, f"Install {install_id} not found"
    install.last_update_status = "failed"
    install.last_update_attempt_at = datetime.now(UTC) - timedelta(hours=hours_ago)
    db.add(install)
    db.commit()
