"""
Unit tests for the defensive (DB-corruption) branches of
``InstallReadinessGate.check``.

These branches are unreachable through the API in production because the FK
constraints either cascade-delete the dangling link (Credential delete →
AgentCredentialLink CASCADE) or NULL the bundle reference (AICredential delete →
SET NULL). They are exercised here with a fully mocked DB session — no real
database, no TestClient. The API-observable readiness verdicts (ready /
needs_setup / publisher_broken) are covered against ``GET /agents/{id}/setup-status``
in ``tests/api/agents/agents_bundles_install_readiness_test.py``.
"""
import uuid
from unittest.mock import MagicMock

from app.models.bundles.agent_bundle import AgentBundle
from app.models.credentials.ai_credential import AICredential  # noqa: F401 — documents the SET NULL FK
from app.services.bundles.install_readiness_gate import InstallReadinessGate


def test_gate_publisher_broken_when_pbp_credential_missing() -> None:
    """
    AgentCredentialLink points at a non-existent credential → publisher_broken.

    In production, deleting a Credential cascades to AgentCredentialLink
    (ondelete="CASCADE"), so the link disappears with the credential. The
    ``publisher_credential_missing`` reason is a defensive code path reachable
    only if the DB is manipulated below the ORM or the FK is bypassed.

    Mock the DB session so ``session.get(Credential, ...)`` returns ``None``
    while the link still exists.
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


def test_gate_publisher_broken_when_publisher_ai_credential_missing() -> None:
    """
    Bundle has publisher_ai_credential_conversation_id; AI cred row doesn't
    exist → publisher_broken.

    In production, deleting an AICredential row NULLs the bundle FK
    (ondelete="SET NULL"), so the gate never sees the stale reference. The
    ``publisher_credential_missing`` (is_ai=True) branch is defensive.

    Mock the DB session so ``session.get(AICredential, id)`` returns ``None``
    while the bundle's field is still set.
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
