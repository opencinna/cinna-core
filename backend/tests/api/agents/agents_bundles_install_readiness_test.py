"""Install readiness gate and setup endpoint tests for agent bundles.

Covers the pre-LLM gate (InstallReadinessGate), setup endpoints
(setup-status, setup-credentials), and the chat/MCP short-circuit behaviour
for installs that are not yet ready.

Scenarios:
  A. Gate check — ready when no missing (fully-filled, owner-installed credential).
  B. Gate check — needs_setup with placeholder PBU credential.
  C. Gate check — publisher_broken when PBP credential deleted.
  D. Gate check — publisher_broken when sharing revoked (allow_sharing=False).
  E. Gate check — publisher AI credential missing (row deleted).
  F. Gate check — publisher AI share missing (AICredentialShare deleted).
  G. Gate check — publisher installs own bundle, no share needed → ready.
  H. GET /agents/{id}/setup-status — happy path (needs_setup install).
  I. GET /agents/{id}/setup-status — auth: another user gets 403/404.
  J. GET /agents/{id}/setup-credentials — returns only owner-placeholder creds.
  K. PUT /agents/{id}/setup-credentials/{credential_id} — happy path, flips is_placeholder.
  L. PUT /agents/{id}/setup-credentials/{credential_id} — rejected for non-placeholder creds.
  M. Chat short-circuit — placeholder install: system message persisted, LLM not engaged.
  N. MCP short-circuit — handle_send_message returns gate shape, not LLM.

A2A and webhook short-circuit tests are deferred (deep transport mocking needed).
Gate logic already validates the A2A/webhook channels — see scenarios A–G.

Direct DB access (``db`` fixture) is used only for credential mutation (breaking
PBP state after install) and reading back gate-level invariants.  All other setup
and assertion is through the API.
"""
import asyncio
import json
import uuid
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import settings
from app.models.agents.agent import Agent
from app.models.bundles.agent_bundle import AgentBundle
from app.models.credentials.ai_credential import AICredential
from app.models.credentials.ai_credential_share import AICredentialShare
from app.models.credentials.credential import Credential
from app.models.credentials.credential_share import CredentialShare
from app.models.credentials.link_models import AgentCredentialLink
from app.models.environments.environment import AgentEnvironment
from app.models.mcp.mcp_connector import MCPConnector
from app.services.bundles.install_readiness_gate import (
    GateMissingItem,
    GateResult,
    InstallReadinessGate,
)
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.message import list_messages, send_message
from tests.utils.session import create_session_via_api
from tests.utils.user import create_random_user, user_authentication_headers

API = settings.API_V1_STR


# ── Module-level helpers ──────────────────────────────────────────────────────


def _make_user_and_headers(client: TestClient) -> tuple[dict, dict[str, str]]:
    """Create a random user with a default AI credential and return both."""
    user = create_random_user(client)
    headers = user_authentication_headers(
        client=client, email=user["email"], password=user["_password"]
    )
    create_random_ai_credential(client, headers, set_default=True)
    return user, headers


def _create_credential(
    client: TestClient,
    headers: dict[str, str],
    name: str = "ir-cred",
    allow_sharing: bool = False,
) -> dict:
    """Create a service credential (api_token type) via the credentials API."""
    r = client.post(
        f"{API}/credentials/",
        headers=headers,
        json={
            "name": name,
            "type": "api_token",
            "notes": "Install readiness test credential",
            "allow_sharing": allow_sharing,
            "credential_data": {
                "api_token": "test-token-ir",
                "api_token_type": "bearer",
            },
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _link_credential_to_agent(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    credential_id: str,
) -> None:
    r = client.post(
        f"{API}/agents/{agent_id}/credentials",
        headers=headers,
        json={"credential_id": credential_id},
    )
    assert r.status_code in (200, 201), r.text


def _publish(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
) -> dict:
    """Publish agent and drain tasks; return the fresh agent row."""
    r = client.post(
        f"{API}/agents/{agent_id}/publish",
        headers=headers,
        json={},
    )
    assert r.status_code == 200, r.text
    drain_tasks()
    fresh = client.get(f"{API}/agents/{agent_id}", headers=headers).json()
    return fresh


def _make_public(
    client: TestClient,
    headers: dict[str, str],
    bundle_uuid: str,
) -> None:
    r = client.patch(
        f"{API}/bundles/{bundle_uuid}",
        headers=headers,
        json={"is_listed": True, "visibility": "public"},
    )
    assert r.status_code == 200, r.text


def _install(
    client: TestClient,
    headers: dict[str, str],
    bundle_id: str,
    *,
    request_body: dict | None = None,
) -> dict:
    r = client.post(
        f"{API}/catalog/{bundle_id}/install",
        headers=headers,
        json=request_body or {},
    )
    assert r.status_code == 200, r.text
    drain_tasks()
    return r.json()


def _get_placeholder_link(
    db: Session, install_id: uuid.UUID
) -> tuple[AgentCredentialLink, Credential]:
    """Return the first placeholder AgentCredentialLink + Credential for an install."""
    links = db.exec(
        select(AgentCredentialLink).where(
            AgentCredentialLink.agent_id == install_id
        )
    ).all()
    for link in links:
        cred = db.get(Credential, link.credential_id)
        if cred and cred.is_placeholder:
            return link, cred
    raise AssertionError(f"No placeholder credential found for install {install_id}")


def _setup_pbu_install(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> tuple[dict, dict, dict[str, str]]:
    """Helper: publish bundle with PBU (non-shareable) cred; foreign user installs.

    Returns (install, installer_user, installer_headers).
    """
    # Publisher side
    publisher_agent = create_agent_via_api(client, superuser_token_headers, name="IR-PBU-Publisher")
    cred = _create_credential(
        client, superuser_token_headers, name="ir-pbu-cred", allow_sharing=False
    )
    _link_credential_to_agent(client, superuser_token_headers, publisher_agent["id"], cred["id"])
    fresh_pub = _publish(client, superuser_token_headers, publisher_agent["id"])
    _make_public(client, superuser_token_headers, fresh_pub["bundle_uuid"])

    # Installer side
    installer, installer_headers = _make_user_and_headers(client)
    install = _install(client, installer_headers, fresh_pub["bundle_id"])
    db.expire_all()
    return install, installer, installer_headers


def _setup_pbp_install(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> tuple[dict, dict, dict[str, str], dict]:
    """Helper: publish bundle with PBP (shareable) cred; foreign user installs.

    Returns (install, installer_user, installer_headers, publisher_cred).
    """
    publisher_agent = create_agent_via_api(client, superuser_token_headers, name="IR-PBP-Publisher")
    shared_cred = _create_credential(
        client, superuser_token_headers, name="ir-pbp-cred", allow_sharing=True
    )
    _link_credential_to_agent(
        client, superuser_token_headers, publisher_agent["id"], shared_cred["id"]
    )
    fresh_pub = _publish(client, superuser_token_headers, publisher_agent["id"])
    _make_public(client, superuser_token_headers, fresh_pub["bundle_uuid"])

    installer, installer_headers = _make_user_and_headers(client)
    install = _install(client, installer_headers, fresh_pub["bundle_id"])
    db.expire_all()
    return install, installer, installer_headers, shared_cred


# ── Scenario A — Gate: ready when no missing ─────────────────────────────────


def test_gate_ready_when_no_missing(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """A. Fully-filled credential owned by installer → gate says ready."""
    # Superuser installs their own agent (publisher = installer).
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="IR-A-Publisher"
    )
    cred = _create_credential(
        client, superuser_token_headers, name="ir-a-cred", allow_sharing=False
    )
    _link_credential_to_agent(
        client, superuser_token_headers, publisher_agent["id"], cred["id"]
    )

    db.expire_all()
    install = db.get(Agent, uuid.UUID(publisher_agent["id"]))
    assert install is not None

    result = InstallReadinessGate.check(db, install)

    assert result.status == "ready"
    assert result.missing == []
    assert result.setup_url is None
    assert result.user_message == ""


# ── Scenario B — Gate: needs_setup with PBU placeholder ─────────────────────


def test_gate_needs_setup_with_placeholder_pbu_credential(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """B. PBU install with placeholder cred → needs_setup, placeholder_empty."""
    install_dict, installer, installer_headers = _setup_pbu_install(
        client, superuser_token_headers, db
    )
    install_id = uuid.UUID(install_dict["id"])
    install = db.get(Agent, install_id)
    assert install is not None

    result = InstallReadinessGate.check(db, install)

    assert result.status == "needs_setup"
    assert len(result.missing) == 1
    assert result.missing[0].reason == "placeholder_empty"
    assert result.missing[0].is_ai is False
    assert result.setup_url is not None
    assert f"/agent/{install_id}#credentials" in result.setup_url
    assert result.user_message != ""


# ── Scenario C — Gate: publisher_broken when PBP credential missing ──────────


def test_gate_publisher_broken_when_pbp_credential_missing() -> None:
    """C. AgentCredentialLink points at a non-existent credential → publisher_broken.

    NOTE: In production, deleting a Credential cascades to AgentCredentialLink
    (ondelete="CASCADE"), so the link disappears with the credential. The
    ``publisher_credential_missing`` reason is therefore a defensive code path
    reachable only if the DB is manipulated below the ORM or the FK is bypassed.

    We exercise it here via a unit-level call: mock the DB session so that
    ``session.get(Credential, ...)`` returns ``None`` while the link still
    exists. This validates the gate's defensive branch without needing
    client/db fixtures.
    """
    install_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    missing_cred_id = uuid.uuid4()

    # Use MagicMock for the install to avoid SQLModel/SA __init__ issues.
    stub_install = MagicMock(spec=["id", "owner_id", "bundle_uuid", "installed_revision_id"])
    stub_install.id = install_id
    stub_install.owner_id = owner_id
    stub_install.bundle_uuid = None
    stub_install.installed_revision_id = None

    # Stub link pointing at the non-existent credential.
    stub_link = MagicMock(spec=["agent_id", "credential_id"])
    stub_link.agent_id = install_id
    stub_link.credential_id = missing_cred_id

    # Mock DB session: exec returns link list; get(Credential) returns None.
    mock_db = MagicMock()

    class _ExecResult:
        def all(self):
            return [stub_link]

        def first(self):
            return None

    mock_db.exec.return_value = _ExecResult()
    mock_db.get.return_value = None  # credential row doesn't exist

    result = InstallReadinessGate.check(mock_db, stub_install)

    assert result.status == "publisher_broken", (
        f"Expected publisher_broken; got {result.status}"
    )
    assert len(result.missing) >= 1
    reasons = {m.reason for m in result.missing}
    assert "publisher_credential_missing" in reasons
    assert all(not m.is_ai for m in result.missing)


# ── Scenario D — Gate: publisher_broken when sharing revoked ─────────────────


def test_gate_publisher_broken_when_sharing_revoked(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """D. Publisher flips allow_sharing=False post-install → publisher_broken, publisher_credential_unshared."""
    install_dict, installer, installer_headers, shared_cred = _setup_pbp_install(
        client, superuser_token_headers, db
    )
    install_id = uuid.UUID(install_dict["id"])

    # Flip allow_sharing=False directly (publisher revokes sharing).
    cred_row = db.get(Credential, uuid.UUID(shared_cred["id"]))
    assert cred_row is not None
    cred_row.allow_sharing = False
    db.add(cred_row)
    db.commit()
    db.expire_all()

    install = db.get(Agent, install_id)
    assert install is not None

    result = InstallReadinessGate.check(db, install)

    assert result.status == "publisher_broken"
    reasons = {m.reason for m in result.missing}
    assert "publisher_credential_unshared" in reasons
    assert all(not m.is_ai for m in result.missing)


# ── Scenario E — Gate: publisher AI credential missing ───────────────────────


def test_gate_publisher_broken_when_publisher_ai_credential_missing() -> None:
    """E. Bundle has publisher_ai_credential_conversation_id; AI cred row doesn't exist → publisher_broken.

    NOTE: In production, deleting an AICredential row NULLs the bundle FK
    (ondelete="SET NULL"), so the gate never sees the stale reference. The
    ``publisher_credential_missing`` (is_ai=True) branch is a defensive path.

    We exercise it here via a unit-level call: mock the DB session so that
    ``session.get(AICredential, id)`` returns ``None`` while the bundle's
    field is still set. This validates the gate's defensive AI-credential branch.
    """
    install_id = uuid.uuid4()
    bundle_id = uuid.uuid4()
    owner_id = uuid.uuid4()
    stale_ai_cred_id = uuid.uuid4()

    # Use MagicMock for the install and bundle.
    stub_install = MagicMock(
        spec=["id", "owner_id", "bundle_uuid", "installed_revision_id"]
    )
    stub_install.id = install_id
    stub_install.owner_id = owner_id
    stub_install.bundle_uuid = bundle_id
    stub_install.installed_revision_id = None

    stub_bundle = MagicMock(
        spec=[
            "id",
            "publisher_ai_credential_conversation_id",
            "publisher_ai_credential_building_id",
        ]
    )
    stub_bundle.id = bundle_id
    stub_bundle.publisher_ai_credential_conversation_id = stale_ai_cred_id
    stub_bundle.publisher_ai_credential_building_id = None

    # Mock DB session:
    #   exec (AgentCredentialLink scan) → empty list
    #   get(AgentBundle, bundle_id) → stub_bundle
    #   get(AICredential, stale_ai_cred_id) → None
    mock_db = MagicMock()

    class _EmptyExecResult:
        def all(self):
            return []

        def first(self):
            return None

    mock_db.exec.return_value = _EmptyExecResult()

    def _mock_get(model_class, model_id):
        if model_class is AgentBundle:
            return stub_bundle
        return None  # AICredential row doesn't exist

    mock_db.get.side_effect = _mock_get

    result = InstallReadinessGate.check(mock_db, stub_install)

    assert result.status == "publisher_broken", (
        f"Expected publisher_broken; got {result.status}"
    )
    ai_missing = [m for m in result.missing if m.is_ai]
    assert len(ai_missing) >= 1, f"Expected AI missing item; got {result.missing}"
    assert ai_missing[0].reason == "publisher_credential_missing"
    assert ai_missing[0].is_ai is True


# ── Scenario F — Gate: publisher AI share missing ────────────────────────────


def test_gate_publisher_broken_when_ai_share_deleted(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """F. AICredentialShare row deleted post-install → publisher_broken, publisher_credential_unshared, is_ai=True."""
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="IR-F-Publisher"
    )
    ai_cred_data = create_random_ai_credential(
        client, superuser_token_headers, set_default=True
    )
    ai_cred_id = uuid.UUID(ai_cred_data["id"])

    fresh_pub = _publish(client, superuser_token_headers, publisher_agent["id"])
    _make_public(client, superuser_token_headers, fresh_pub["bundle_uuid"])

    bundle = db.get(AgentBundle, uuid.UUID(fresh_pub["bundle_uuid"]))
    assert bundle is not None
    bundle.publisher_ai_credential_conversation_id = ai_cred_id
    db.add(bundle)
    db.commit()

    installer, installer_headers = _make_user_and_headers(client)
    install_dict = _install(client, installer_headers, fresh_pub["bundle_id"])
    install_id = uuid.UUID(install_dict["id"])
    installer_id = uuid.UUID(installer["id"])
    db.expire_all()

    # Ensure a share row was created (install_service should have created it).
    # If not, create one manually so the gate can then detect its absence.
    share = db.exec(
        select(AICredentialShare).where(
            AICredentialShare.ai_credential_id == ai_cred_id,
            AICredentialShare.shared_with_user_id == installer_id,
        )
    ).first()
    if share is None:
        # Install didn't create the share automatically — create it so we can
        # then delete it and confirm the gate detects the absence.
        share = AICredentialShare(
            ai_credential_id=ai_cred_id,
            shared_with_user_id=installer_id,
        )
        db.add(share)
        db.commit()
        db.expire_all()
        share = db.exec(
            select(AICredentialShare).where(
                AICredentialShare.ai_credential_id == ai_cred_id,
                AICredentialShare.shared_with_user_id == installer_id,
            )
        ).first()

    db.delete(share)
    db.commit()
    db.expire_all()

    install = db.get(Agent, install_id)
    assert install is not None

    result = InstallReadinessGate.check(db, install)

    assert result.status == "publisher_broken"
    ai_missing = [m for m in result.missing if m.is_ai]
    assert len(ai_missing) >= 1
    assert ai_missing[0].reason == "publisher_credential_unshared"
    assert ai_missing[0].is_ai is True


# ── Scenario G — Gate: publisher installs own bundle, no share needed ─────────


def test_gate_ready_when_publisher_installs_own_bundle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """G. Publisher == installer; AI credential owned by them → no share needed → ready."""
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="IR-G-Publisher"
    )
    ai_cred_data = create_random_ai_credential(
        client, superuser_token_headers, set_default=True
    )
    ai_cred_id = uuid.UUID(ai_cred_data["id"])

    fresh_pub = _publish(client, superuser_token_headers, publisher_agent["id"])

    bundle = db.get(AgentBundle, uuid.UUID(fresh_pub["bundle_uuid"]))
    assert bundle is not None
    bundle.publisher_ai_credential_conversation_id = ai_cred_id
    db.add(bundle)
    db.commit()
    db.expire_all()

    # The publisher_agent row IS the publisher install.
    install = db.get(Agent, uuid.UUID(publisher_agent["id"]))
    assert install is not None

    result = InstallReadinessGate.check(db, install)

    assert result.status == "ready", (
        f"Expected ready for publisher's own install; got {result.status} "
        f"missing={result.missing}"
    )


# ── Scenario H — GET /setup-status: happy path ───────────────────────────────


def test_get_setup_status_needs_setup(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """H. GET setup-status for placeholder install returns needs_setup + missing + setup_url. No user_message."""
    install_dict, installer, installer_headers = _setup_pbu_install(
        client, superuser_token_headers, db
    )
    install_id = install_dict["id"]

    r = client.get(
        f"{API}/agents/{install_id}/setup-status",
        headers=installer_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["status"] == "needs_setup"
    assert isinstance(body["missing"], list)
    assert len(body["missing"]) >= 1
    assert body["setup_url"] is not None
    assert f"/agent/{install_id}#credentials" in body["setup_url"]
    # user_message must NOT be in the response (frontend renders its own copy).
    assert "user_message" not in body


# ── Scenario I — GET /setup-status: auth ──────────────────────────────────────


def test_get_setup_status_forbidden_for_other_user(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """I. Another user hitting /setup-status gets 403/404."""
    install_dict, installer, installer_headers = _setup_pbu_install(
        client, superuser_token_headers, db
    )
    install_id = install_dict["id"]

    stranger, stranger_headers = _make_user_and_headers(client)

    r = client.get(
        f"{API}/agents/{install_id}/setup-status",
        headers=stranger_headers,
    )
    assert r.status_code in (403, 404), (
        f"Expected 403 or 404 for non-owner; got {r.status_code}: {r.text}"
    )


# ── Scenario J — GET /setup-credentials ──────────────────────────────────────


def test_list_setup_credentials_returns_only_owner_placeholder(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """J. /setup-credentials returns only owner-placeholder creds; includes id/name/type/description."""
    install_dict, installer, installer_headers = _setup_pbu_install(
        client, superuser_token_headers, db
    )
    install_id = install_dict["id"]

    r = client.get(
        f"{API}/agents/{install_id}/setup-credentials",
        headers=installer_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert isinstance(body, list)
    assert len(body) >= 1, "Expected at least one placeholder credential"

    cred_summary = body[0]
    # Required fields per spec.
    for field in ("id", "name", "type"):
        assert field in cred_summary, f"Missing field: {field}"
    # description may be None but key must exist.
    assert "description" in cred_summary

    # Confirm the returned credential is actually a placeholder owned by installer.
    cred_id = uuid.UUID(cred_summary["id"])
    db.expire_all()
    cred_row = db.get(Credential, cred_id)
    assert cred_row is not None
    assert cred_row.is_placeholder is True
    assert cred_row.owner_id == uuid.UUID(installer["id"])


def test_list_setup_credentials_excludes_publisher_shared(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """J (extension). PBP install: /setup-credentials returns nothing (shared cred is excluded)."""
    install_dict, installer, installer_headers, shared_cred = _setup_pbp_install(
        client, superuser_token_headers, db
    )
    install_id = install_dict["id"]

    r = client.get(
        f"{API}/agents/{install_id}/setup-credentials",
        headers=installer_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()

    # Publisher-shared (non-placeholder, foreign-owned) cred must not appear.
    returned_ids = {item["id"] for item in body}
    assert shared_cred["id"] not in returned_ids


# ── Scenario K — PUT /setup-credentials/{id}: happy path ────────────────────


def test_put_setup_credential_flips_placeholder(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """K. PUT with non-empty data flips is_placeholder=False; gate returns ready."""
    install_dict, installer, installer_headers = _setup_pbu_install(
        client, superuser_token_headers, db
    )
    install_id = install_dict["id"]
    install_id_uuid = uuid.UUID(install_id)

    # Find the placeholder credential.
    db.expire_all()
    _, placeholder_cred = _get_placeholder_link(db, install_id_uuid)
    cred_id = str(placeholder_cred.id)

    r = client.put(
        f"{API}/agents/{install_id}/setup-credentials/{cred_id}",
        headers=installer_headers,
        json={
            "credential_data": {
                "api_token": "real-token-now",
                "api_token_type": "bearer",
            }
        },
    )
    assert r.status_code == 200, r.text
    resp = r.json()

    # Response should include the credential id.
    assert resp["id"] == cred_id

    # Credential must now be non-placeholder.
    db.expire_all()
    updated_cred = db.get(Credential, uuid.UUID(cred_id))
    assert updated_cred is not None
    assert updated_cred.is_placeholder is False, (
        "Expected is_placeholder=False after PUT setup-credential"
    )

    # Gate should now return ready.
    install = db.get(Agent, install_id_uuid)
    assert install is not None
    gate_result = InstallReadinessGate.check(db, install)
    assert gate_result.status == "ready", (
        f"Gate should be ready after filling credential; got {gate_result.status}"
    )


# ── Scenario L — PUT /setup-credentials/{id}: rejected for non-placeholder ───


def test_put_setup_credential_rejected_for_non_placeholder(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """L. PUT against an already-filled credential returns 4xx."""
    # Create a fresh non-placeholder credential and link it to the superuser's agent.
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="IR-L-Agent"
    )
    real_cred = _create_credential(
        client, superuser_token_headers, name="ir-l-real-cred", allow_sharing=False
    )
    _link_credential_to_agent(
        client, superuser_token_headers, publisher_agent["id"], real_cred["id"]
    )

    # Confirm it is NOT a placeholder.
    db.expire_all()
    cred_row = db.get(Credential, uuid.UUID(real_cred["id"]))
    assert cred_row is not None
    assert cred_row.is_placeholder is False

    r = client.put(
        f"{API}/agents/{publisher_agent['id']}/setup-credentials/{real_cred['id']}",
        headers=superuser_token_headers,
        json={
            "credential_data": {
                "api_token": "attempt-overwrite",
                "api_token_type": "bearer",
            }
        },
    )
    assert r.status_code in (400, 403, 404, 409), (
        f"Expected 4xx for non-placeholder credential; got {r.status_code}: {r.text}"
    )


# ── Scenario M — Chat short-circuit: placeholder install ────────────────────


def test_chat_short_circuit_persists_system_message_for_placeholder_install(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """M. Chat message into a placeholder install: system message persisted, interaction not flipped to running."""
    install_dict, installer, installer_headers = _setup_pbu_install(
        client, superuser_token_headers, db
    )
    install_id = install_dict["id"]

    # Create a chat session for the installer.
    session_data = create_session_via_api(client, installer_headers, install_id)
    session_id = session_data["id"]

    response = send_message(client, installer_headers, session_id, "Hello agent!")
    drain_tasks()

    # A system message must have been persisted.
    messages = list_messages(client, installer_headers, session_id)
    system_messages = [m for m in messages if m["role"] == "system"]
    assert len(system_messages) >= 1, "Expected at least one system message after gate short-circuit"

    gate_msg = system_messages[0]
    # message_metadata must include setup_url and missing.
    metadata = gate_msg.get("message_metadata") or {}
    assert "setup_url" in metadata, f"setup_url missing from message_metadata: {metadata}"
    assert "missing" in metadata, f"missing missing from message_metadata: {metadata}"
    assert metadata.get("install_setup_required") is True
    assert metadata["setup_url"] is not None
    assert isinstance(metadata["missing"], list)

    # interaction_status must NOT be running (env was never engaged).
    db.expire_all()
    from app.models.sessions.session import Session as ChatSessionModel
    chat_session = db.get(ChatSessionModel, uuid.UUID(session_id))
    assert chat_session is not None, f"Chat session {session_id} not found in DB"
    assert chat_session.interaction_status != "running", (
        f"interaction_status should not be 'running' after gate short-circuit; "
        f"got {chat_session.interaction_status}"
    )


# ── Scenario N — MCP short-circuit ───────────────────────────────────────────


def test_mcp_short_circuit_returns_gate_shape_without_llm(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """N. MCPRequestHandler.handle_send_message returns gate JSON shape; LLM not invoked.

    We call the handler with a stub connector (persisted) so FK constraints
    are satisfied, and patch get_or_create_mcp_session to return a dummy
    platform session — the gate fires before any streaming, so the mock
    session id just needs to be a valid UUID.
    """
    install_dict, installer, installer_headers = _setup_pbu_install(
        client, superuser_token_headers, db
    )
    install_id = uuid.UUID(install_dict["id"])
    installer_id = uuid.UUID(installer["id"])

    db.expire_all()
    install = db.get(Agent, install_id)
    assert install is not None

    # Persist a real MCPConnector via the API so FK constraints are satisfied.
    r = client.post(
        f"{API}/agents/{install_id}/mcp-connectors",
        headers=installer_headers,
        json={"name": "ir-n-connector"},
    )
    assert r.status_code in (200, 201), f"Failed to create MCP connector: {r.text}"
    connector_id = uuid.UUID(r.json()["id"])
    db.expire_all()
    connector = db.get(MCPConnector, connector_id)
    assert connector is not None

    # Build a stub environment (required by constructor; not used in gate path).
    environment = db.exec(
        select(AgentEnvironment).where(AgentEnvironment.agent_id == install_id)
    ).first()
    if environment is None:
        environment = AgentEnvironment(id=uuid.uuid4(), agent_id=install_id)

    # Stub platform session returned by get_or_create_mcp_session.
    # The gate fires BEFORE message creation so the session contents don't matter.
    stub_session_id = uuid.uuid4()

    class _StubSession:
        id = stub_session_id

    @contextmanager
    def _get_db():
        yield db

    from app.mcp.request_handler import MCPRequestHandler

    handler = MCPRequestHandler(
        agent=install,
        environment=environment,
        connector=connector,
        get_db_session=_get_db,
        authenticated_user_id=installer_id,
    )

    stream_mock = AsyncMock(return_value="should-not-be-called")
    session_mock = MagicMock(return_value=(_StubSession(), True))
    with patch("app.mcp.message_streaming.stream_and_collect_response", stream_mock):
        with patch("app.mcp.request_handler.stream_and_collect_response", stream_mock):
            with patch(
                "app.services.sessions.session_service.SessionService.get_or_create_mcp_session",
                session_mock,
            ):
                result_json = asyncio.run(handler.handle_send_message(message="ping"))

    # LLM must NOT have been invoked.
    assert stream_mock.call_count == 0, (
        f"stream_and_collect_response called {stream_mock.call_count} time(s) — "
        "should be 0 when gate blocks"
    )

    # Response must be the gate JSON shape.
    result = json.loads(result_json)
    assert "response" in result, f"Missing 'response' key in MCP gate response: {result}"
    assert "context_id" in result, f"Missing 'context_id' key in MCP gate response: {result}"
    assert "setup_url" in result, f"Missing 'setup_url' key in MCP gate response: {result}"
    assert result["setup_url"] is not None
    assert result["response"] != "", "Gate user_message should be non-empty"
