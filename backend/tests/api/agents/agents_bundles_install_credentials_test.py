"""Credential resolution at install time — agent bundle tests.

Covers PBU (provided_by="user") placeholder creation, PBP
(provided_by="publisher") shared-credential linking, degradation semantics
(revoked / deleted / wrong-owner fallback), publisher AI credential sharing
(AICredentialShare rows, env FK resolution), mixed PBP+PBU specs, and
idempotency of re-install for CredentialShare and AICredentialShare.

Direct DB access via the ``db`` fixture is used for CredentialShare,
AICredentialShare, AgentCredentialLink, and Credential rows which have no
listing API endpoints.  The ``db`` fixture IS the test transaction session —
service writes and test reads share the same savepoint-wrapped session so
visibility is guaranteed.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import settings
from app.models.credentials.ai_credential_share import AICredentialShare
from app.models.credentials.credential import Credential
from app.models.credentials.credential_share import CredentialShare
from app.models.credentials.link_models import AgentCredentialLink
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.user import create_random_user, user_authentication_headers

API = settings.API_V1_STR


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_user_and_headers(client: TestClient) -> tuple[dict, dict[str, str]]:
    """Create a fresh user with a default AI credential and return (user, headers)."""
    user = create_random_user(client)
    headers = user_authentication_headers(
        client=client, email=user["email"], password=user["_password"]
    )
    create_random_ai_credential(client, headers, set_default=True)
    return user, headers


def _create_credential(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str | None = None,
    allow_sharing: bool = False,
) -> dict:
    name = name or f"cred-{uuid.uuid4().hex[:8]}"
    r = client.post(
        f"{API}/credentials/",
        headers=headers,
        json={
            "name": name,
            "type": "api_token",
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
    assert r.status_code == 200, r.text


def _publish(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
) -> dict:
    """Publish agent and drain tasks; return the agent's fresh row."""
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
    install = r.json()
    drain_tasks()
    return install


def _get_install(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
) -> dict:
    r = client.get(f"{API}/agents/{agent_id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _get_env(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
) -> dict | None:
    r = client.get(f"{API}/agents/{agent_id}/environments", headers=headers)
    assert r.status_code == 200, r.text
    envs = r.json().get("data", [])
    return envs[0] if envs else None


# ── Scenario A: PBU placeholder is created on install ────────────────────────


def test_pbu_placeholder_credential_created_on_install(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """A. Bundle has one PBU (provided_by="user") spec; no user credential supplied.

    Assert:
    - Credential row with is_placeholder=True exists, owned by installer.
    - AgentCredentialLink points at that placeholder.
    - encrypted_data is non-empty (regression for the _encrypt_data bug fix).
    """
    # ── Phase 1: publish a bundle with a non-shareable (PBU) credential ───────
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="InstCred-A-Publisher"
    )
    drain_tasks()

    cred = _create_credential(
        client, superuser_token_headers, name="ic-a-private", allow_sharing=False
    )
    _link_credential_to_agent(
        client, superuser_token_headers, publisher_agent["id"], cred["id"]
    )
    fresh_pub = _publish(client, superuser_token_headers, publisher_agent["id"])
    _make_public(client, superuser_token_headers, fresh_pub["bundle_uuid"])

    # ── Phase 2: foreign user installs with no credentials supplied ───────────
    installer, installer_headers = _make_user_and_headers(client)
    install = _install(client, installer_headers, fresh_pub["bundle_id"])
    install_id = uuid.UUID(install["id"])
    installer_id = uuid.UUID(installer["id"])

    # ── Phase 3: assert placeholder via db fixture ────────────────────────────
    db.expire_all()
    links = db.exec(
        select(AgentCredentialLink).where(
            AgentCredentialLink.agent_id == install_id
        )
    ).all()
    assert len(links) == 1, (
        f"Expected 1 AgentCredentialLink for install {install_id}; got {len(links)}"
    )
    placeholder_cred = db.get(Credential, links[0].credential_id)
    assert placeholder_cred is not None, "Placeholder Credential row missing"
    assert placeholder_cred.is_placeholder is True, (
        f"Expected is_placeholder=True; got {placeholder_cred.is_placeholder}"
    )
    assert placeholder_cred.owner_id == installer_id, (
        f"Placeholder owner {placeholder_cred.owner_id} != installer {installer_id}"
    )
    assert placeholder_cred.encrypted_data, (
        "encrypted_data must be non-empty (regression for _encrypt_data bug)"
    )


# ── Scenario B: PBP service credential linked, not duplicated ────────────────


def test_pbp_credential_linked_not_duplicated(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """B. Publisher has allow_sharing=True credential; bundle spec is PBP.

    Foreign user installs.

    Assert:
    - AgentCredentialLink points at publisher's credential id (not a copy).
    - CredentialShare row exists publisher → installer.
    - No new Credential row created for installer.
    """
    # ── Phase 1: publish a bundle with a shareable (PBP) credential ──────────
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="InstCred-B-Publisher"
    )
    drain_tasks()

    shared_cred = _create_credential(
        client, superuser_token_headers, name="ic-b-shared", allow_sharing=True
    )
    _link_credential_to_agent(
        client, superuser_token_headers, publisher_agent["id"], shared_cred["id"]
    )
    fresh_pub = _publish(client, superuser_token_headers, publisher_agent["id"])
    _make_public(client, superuser_token_headers, fresh_pub["bundle_uuid"])

    # ── Phase 2: foreign user installs ───────────────────────────────────────
    installer, installer_headers = _make_user_and_headers(client)
    install = _install(client, installer_headers, fresh_pub["bundle_id"])
    install_id = uuid.UUID(install["id"])
    installer_id = uuid.UUID(installer["id"])
    publisher_cred_id = uuid.UUID(shared_cred["id"])

    # ── Phase 3: verify link, share, no duplicate credential ─────────────────
    db.expire_all()

    # 1. Link points at publisher's credential, not a copy.
    links = db.exec(
        select(AgentCredentialLink).where(
            AgentCredentialLink.agent_id == install_id
        )
    ).all()
    assert len(links) == 1, f"Expected 1 link; got {len(links)}"
    assert links[0].credential_id == publisher_cred_id, (
        f"Link points at {links[0].credential_id}, expected publisher cred {publisher_cred_id}"
    )

    # 2. CredentialShare exists publisher → installer.
    share = db.exec(
        select(CredentialShare).where(
            CredentialShare.credential_id == publisher_cred_id,
            CredentialShare.shared_with_user_id == installer_id,
        )
    ).first()
    assert share is not None, "Expected CredentialShare row publisher → installer"

    # 3. No new Credential owned by installer.
    installer_creds = db.exec(
        select(Credential).where(Credential.owner_id == installer_id)
    ).all()
    assert len(installer_creds) == 0, (
        f"Expected no new Credential for installer; got {len(installer_creds)}: "
        f"{[c.name for c in installer_creds]}"
    )


# ── Scenario C: PBP idempotency on re-install ─────────────────────────────────


def test_pbp_credential_idempotent_on_reinstall(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """C. Install once, then install again (idempotent path).

    Assert no duplicate CredentialShare or AgentCredentialLink rows.
    """
    # ── Phase 1: publish + first install ─────────────────────────────────────
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="InstCred-C-Publisher"
    )
    drain_tasks()

    shared_cred = _create_credential(
        client, superuser_token_headers, name="ic-c-shared", allow_sharing=True
    )
    _link_credential_to_agent(
        client, superuser_token_headers, publisher_agent["id"], shared_cred["id"]
    )
    fresh_pub = _publish(client, superuser_token_headers, publisher_agent["id"])
    _make_public(client, superuser_token_headers, fresh_pub["bundle_uuid"])

    installer, installer_headers = _make_user_and_headers(client)
    install = _install(client, installer_headers, fresh_pub["bundle_id"])
    install_id = uuid.UUID(install["id"])
    installer_id = uuid.UUID(installer["id"])
    publisher_cred_id = uuid.UUID(shared_cred["id"])

    # ── Phase 2: second install — idempotent ──────────────────────────────────
    install2 = _install(client, installer_headers, fresh_pub["bundle_id"])
    assert install2["id"] == install["id"], "Idempotent install must return same row"

    # ── Phase 3: assert no duplicates ────────────────────────────────────────
    db.expire_all()

    links = db.exec(
        select(AgentCredentialLink).where(
            AgentCredentialLink.agent_id == install_id
        )
    ).all()
    assert len(links) == 1, f"Expected exactly 1 link after re-install; got {len(links)}"

    shares = db.exec(
        select(CredentialShare).where(
            CredentialShare.credential_id == publisher_cred_id,
            CredentialShare.shared_with_user_id == installer_id,
        )
    ).all()
    assert len(shares) == 1, (
        f"Expected exactly 1 CredentialShare after re-install; got {len(shares)}"
    )


# ── Scenario D: PBP with revoked sharing → placeholder + degraded ─────────────


def test_pbp_revoked_sharing_falls_through_to_placeholder_degraded(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """D. allow_sharing is True at publish time, then revoked before install.

    Assert:
    - Install returns 200.
    - Placeholder Credential created for installer.
    - install.last_update_status == "degraded".
    - No CredentialShare created.
    """
    # ── Phase 1: publish with shareable credential, then revoke ───────────────
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="InstCred-D-Publisher"
    )
    drain_tasks()

    shared_cred = _create_credential(
        client, superuser_token_headers, name="ic-d-shared", allow_sharing=True
    )
    _link_credential_to_agent(
        client, superuser_token_headers, publisher_agent["id"], shared_cred["id"]
    )
    fresh_pub = _publish(client, superuser_token_headers, publisher_agent["id"])
    _make_public(client, superuser_token_headers, fresh_pub["bundle_uuid"])

    # Revoke sharing BEFORE install (mutate via db — no PATCH endpoint exists).
    revoked_id = uuid.UUID(shared_cred["id"])
    cred_to_revoke = db.get(Credential, revoked_id)
    assert cred_to_revoke is not None
    cred_to_revoke.allow_sharing = False
    db.add(cred_to_revoke)
    db.commit()

    # ── Phase 2: foreign user installs ───────────────────────────────────────
    installer, installer_headers = _make_user_and_headers(client)
    install = _install(client, installer_headers, fresh_pub["bundle_id"])
    install_id = uuid.UUID(install["id"])
    installer_id = uuid.UUID(installer["id"])
    publisher_cred_id = uuid.UUID(shared_cred["id"])

    # ── Phase 3: verify placeholder, degraded, no share ──────────────────────
    db.expire_all()

    links = db.exec(
        select(AgentCredentialLink).where(
            AgentCredentialLink.agent_id == install_id
        )
    ).all()
    assert len(links) == 1, f"Expected 1 link (placeholder); got {len(links)}"
    linked_cred = db.get(Credential, links[0].credential_id)
    assert linked_cred is not None
    assert linked_cred.is_placeholder is True, (
        f"Expected placeholder; got is_placeholder={linked_cred.is_placeholder}"
    )
    assert linked_cred.owner_id == installer_id

    share = db.exec(
        select(CredentialShare).where(
            CredentialShare.credential_id == publisher_cred_id,
            CredentialShare.shared_with_user_id == installer_id,
        )
    ).first()
    assert share is None, "CredentialShare must NOT be created when sharing is revoked"

    # last_update_status == "degraded" via API.
    fresh_install = _get_install(client, installer_headers, install["id"])
    assert fresh_install["last_update_status"] == "degraded", (
        f"Expected degraded; got {fresh_install['last_update_status']}"
    )


# ── Scenario E: PBP with deleted publisher credential → placeholder + degraded ─


def test_pbp_deleted_publisher_credential_falls_through_gracefully(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """E. Publisher's Credential row deleted directly via db before install.

    Assert:
    - Install activates with placeholder + degraded.
    - No crash; no orphaned AgentCredentialLink pointing at deleted id.
    """
    # ── Phase 1: publish, then delete publisher's credential row ─────────────
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="InstCred-E-Publisher"
    )
    drain_tasks()

    shared_cred = _create_credential(
        client, superuser_token_headers, name="ic-e-shared", allow_sharing=True
    )
    _link_credential_to_agent(
        client, superuser_token_headers, publisher_agent["id"], shared_cred["id"]
    )
    fresh_pub = _publish(client, superuser_token_headers, publisher_agent["id"])
    _make_public(client, superuser_token_headers, fresh_pub["bundle_uuid"])

    deleted_cred_id = uuid.UUID(shared_cred["id"])

    # Delete publisher's Credential row directly via db fixture.
    cred_row = db.get(Credential, deleted_cred_id)
    if cred_row is not None:
        db.delete(cred_row)
        db.commit()

    # ── Phase 2: foreign user installs ───────────────────────────────────────
    installer, installer_headers = _make_user_and_headers(client)
    install = _install(client, installer_headers, fresh_pub["bundle_id"])
    install_id = uuid.UUID(install["id"])
    installer_id = uuid.UUID(installer["id"])

    # ── Phase 3: verify no orphan link + placeholder exists ──────────────────
    db.expire_all()

    orphan_link = db.exec(
        select(AgentCredentialLink).where(
            AgentCredentialLink.agent_id == install_id,
            AgentCredentialLink.credential_id == deleted_cred_id,
        )
    ).first()
    assert orphan_link is None, (
        "AgentCredentialLink must NOT point at the deleted publisher credential"
    )

    links = db.exec(
        select(AgentCredentialLink).where(
            AgentCredentialLink.agent_id == install_id
        )
    ).all()
    assert len(links) == 1, f"Expected 1 (placeholder) link; got {len(links)}"
    linked_cred = db.get(Credential, links[0].credential_id)
    assert linked_cred is not None
    assert linked_cred.is_placeholder is True
    assert linked_cred.owner_id == installer_id

    # Degraded status surfaced.
    fresh_install = _get_install(client, installer_headers, install["id"])
    assert fresh_install["last_update_status"] == "degraded", (
        f"Expected degraded; got {fresh_install['last_update_status']}"
    )


# ── Scenario F: PBP credential of wrong owner → fallback to placeholder ────────


def test_pbp_wrong_owner_credential_falls_through_to_placeholder(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """F. publisher_credential_id in revision spec points at a credential
    owned by a third user (not the bundle publisher).

    We hand-mutate revision.required_credential_specs via db after publish
    to inject the bad uuid.

    Assert:
    - Install creates placeholder + degraded.
    - No AgentCredentialLink to the foreign credential.
    """
    from app.models.bundles.agent_bundle_revision import AgentBundleRevision

    # ── Phase 1: publish (no credentials linked) ──────────────────────────────
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="InstCred-F-Publisher"
    )
    drain_tasks()
    fresh_pub = _publish(client, superuser_token_headers, publisher_agent["id"])
    _make_public(client, superuser_token_headers, fresh_pub["bundle_uuid"])

    # Third user's shareable credential.
    third_user, third_headers = _make_user_and_headers(client)
    third_cred = _create_credential(
        client, third_headers, name="ic-f-third-cred", allow_sharing=True
    )
    third_cred_id = uuid.UUID(third_cred["id"])

    # Inject bad spec into revision via db fixture.
    revision_id = uuid.UUID(fresh_pub["installed_revision_id"])
    revision = db.get(AgentBundleRevision, revision_id)
    assert revision is not None
    bad_spec = {
        "name": "injected-wrong-owner",
        "type": "api_token",
        "allow_sharing": True,
        "provided_by": "publisher",
        "publisher_credential_id": str(third_cred_id),
        "description": None,
    }
    revision.required_credential_specs = [bad_spec]
    db.add(revision)
    db.commit()

    # ── Phase 2: foreign user installs ───────────────────────────────────────
    installer, installer_headers = _make_user_and_headers(client)
    install = _install(client, installer_headers, fresh_pub["bundle_id"])
    install_id = uuid.UUID(install["id"])
    installer_id = uuid.UUID(installer["id"])

    # ── Phase 3: verify no link to wrong-owner cred + placeholder + degraded ──
    db.expire_all()

    bad_link = db.exec(
        select(AgentCredentialLink).where(
            AgentCredentialLink.agent_id == install_id,
            AgentCredentialLink.credential_id == third_cred_id,
        )
    ).first()
    assert bad_link is None, (
        "Must NOT create AgentCredentialLink to wrong-owner credential"
    )

    links = db.exec(
        select(AgentCredentialLink).where(
            AgentCredentialLink.agent_id == install_id
        )
    ).all()
    assert len(links) == 1, f"Expected 1 (placeholder) link; got {len(links)}"
    linked_cred = db.get(Credential, links[0].credential_id)
    assert linked_cred is not None
    assert linked_cred.is_placeholder is True
    assert linked_cred.owner_id == installer_id

    fresh_install = _get_install(client, installer_headers, install["id"])
    assert fresh_install["last_update_status"] == "degraded", (
        f"Expected degraded; got {fresh_install['last_update_status']}"
    )


# ── Scenario G: Publisher AI credential — both modes ─────────────────────────


def test_publisher_ai_credential_both_modes_shared_on_install(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """G. Bundle has both publisher_ai_credential_conversation_id and
    publisher_ai_credential_building_id set.

    Foreign user installs without supplying any AI credential selections.

    Assert:
    - Two AICredentialShare rows (publisher → installer).
    - env.conversation_ai_credential_id == bundle's conversation field.
    - env.building_ai_credential_id == bundle's building field.
    """
    # ── Phase 1: publish + set both publisher AI creds ────────────────────────
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="InstCred-G-Publisher"
    )
    drain_tasks()
    fresh_pub = _publish(client, superuser_token_headers, publisher_agent["id"])

    conv_ai = create_random_ai_credential(client, superuser_token_headers)
    build_ai = create_random_ai_credential(client, superuser_token_headers)
    conv_ai_id = uuid.UUID(conv_ai["id"])
    build_ai_id = uuid.UUID(build_ai["id"])

    r = client.patch(
        f"{API}/bundles/{fresh_pub['bundle_uuid']}",
        headers=superuser_token_headers,
        json={
            "publisher_ai_credential_conversation_id": str(conv_ai_id),
            "publisher_ai_credential_building_id": str(build_ai_id),
            "is_listed": True,
            "visibility": "public",
        },
    )
    assert r.status_code == 200, r.text

    # ── Phase 2: foreign user installs ───────────────────────────────────────
    installer, installer_headers = _make_user_and_headers(client)
    installer_id = uuid.UUID(installer["id"])
    install = _install(client, installer_headers, fresh_pub["bundle_id"])

    # ── Phase 3: verify AICredentialShare rows ────────────────────────────────
    db.expire_all()

    conv_share = db.exec(
        select(AICredentialShare).where(
            AICredentialShare.ai_credential_id == conv_ai_id,
            AICredentialShare.shared_with_user_id == installer_id,
        )
    ).first()
    assert conv_share is not None, (
        "Expected AICredentialShare for conversation AI credential"
    )

    build_share = db.exec(
        select(AICredentialShare).where(
            AICredentialShare.ai_credential_id == build_ai_id,
            AICredentialShare.shared_with_user_id == installer_id,
        )
    ).first()
    assert build_share is not None, (
        "Expected AICredentialShare for building AI credential"
    )

    # ── Phase 4: env credential FKs match bundle PBP fields ──────────────────
    env = _get_env(client, installer_headers, install["id"])
    assert env is not None, "Expected an environment to be created"
    assert str(env["conversation_ai_credential_id"]) == str(conv_ai_id), (
        f"Env conversation_ai_credential_id {env['conversation_ai_credential_id']} "
        f"!= bundle conv cred {conv_ai_id}"
    )
    assert str(env["building_ai_credential_id"]) == str(build_ai_id), (
        f"Env building_ai_credential_id {env['building_ai_credential_id']} "
        f"!= bundle build cred {build_ai_id}"
    )


# ── Scenario H: Only conversation PBP set; request supplies building ──────────


def test_publisher_ai_credential_only_conversation_request_supplies_building(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """H. Bundle sets publisher_ai_credential_conversation_id only.

    Install request supplies building_credential_id.

    Assert:
    - env.conversation_ai_credential_id == bundle's conversation field.
    - env.building_ai_credential_id == installer's request selection.
    - One AICredentialShare (conversation only).
    - No AICredentialShare for building credential.
    """
    # ── Phase 1: publish + set conversation PBP only ──────────────────────────
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="InstCred-H-Publisher"
    )
    drain_tasks()
    fresh_pub = _publish(client, superuser_token_headers, publisher_agent["id"])

    conv_ai = create_random_ai_credential(client, superuser_token_headers)
    conv_ai_id = uuid.UUID(conv_ai["id"])

    r = client.patch(
        f"{API}/bundles/{fresh_pub['bundle_uuid']}",
        headers=superuser_token_headers,
        json={
            "publisher_ai_credential_conversation_id": str(conv_ai_id),
            "is_listed": True,
            "visibility": "public",
        },
    )
    assert r.status_code == 200, r.text

    # ── Phase 2: foreign user installs with building credential in request ─────
    installer, installer_headers = _make_user_and_headers(client)
    installer_id = uuid.UUID(installer["id"])

    installer_build_ai = create_random_ai_credential(client, installer_headers)
    installer_build_ai_id = uuid.UUID(installer_build_ai["id"])

    install = _install(
        client,
        installer_headers,
        fresh_pub["bundle_id"],
        request_body={
            "ai_credential_selections": {
                "building_credential_id": str(installer_build_ai_id),
            }
        },
    )

    # ── Phase 3: verify AICredentialShare rows and env FKs ───────────────────
    db.expire_all()

    conv_share = db.exec(
        select(AICredentialShare).where(
            AICredentialShare.ai_credential_id == conv_ai_id,
            AICredentialShare.shared_with_user_id == installer_id,
        )
    ).first()
    assert conv_share is not None, (
        "Expected AICredentialShare for conversation AI credential"
    )

    build_share = db.exec(
        select(AICredentialShare).where(
            AICredentialShare.ai_credential_id == installer_build_ai_id,
            AICredentialShare.shared_with_user_id == installer_id,
        )
    ).first()
    assert build_share is None, (
        "Must NOT create AICredentialShare for installer's own building credential"
    )

    env = _get_env(client, installer_headers, install["id"])
    assert env is not None
    assert str(env["conversation_ai_credential_id"]) == str(conv_ai_id), (
        f"Env conversation FK {env['conversation_ai_credential_id']} != bundle conv {conv_ai_id}"
    )
    assert str(env["building_ai_credential_id"]) == str(installer_build_ai_id), (
        f"Env building FK {env['building_ai_credential_id']} "
        f"!= request selection {installer_build_ai_id}"
    )


# ── Scenario I: Publisher AI credential — request selection ignored when bundle provides ─


def test_publisher_ai_credential_request_selection_ignored_when_bundle_provides(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """I. Request supplies both conversation and building credentials,
    bundle has BOTH PBP fields set.

    Assert: env credentials match bundle PBP fields, NOT request selections.
    """
    # ── Phase 1: publish + set both PBP AI creds ─────────────────────────────
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="InstCred-I-Publisher"
    )
    drain_tasks()
    fresh_pub = _publish(client, superuser_token_headers, publisher_agent["id"])

    conv_ai = create_random_ai_credential(client, superuser_token_headers)
    build_ai = create_random_ai_credential(client, superuser_token_headers)
    conv_ai_id = uuid.UUID(conv_ai["id"])
    build_ai_id = uuid.UUID(build_ai["id"])

    r = client.patch(
        f"{API}/bundles/{fresh_pub['bundle_uuid']}",
        headers=superuser_token_headers,
        json={
            "publisher_ai_credential_conversation_id": str(conv_ai_id),
            "publisher_ai_credential_building_id": str(build_ai_id),
            "is_listed": True,
            "visibility": "public",
        },
    )
    assert r.status_code == 200, r.text

    # ── Phase 2: foreign user installs with their own AI credentials ──────────
    installer, installer_headers = _make_user_and_headers(client)
    installer_conv_ai = create_random_ai_credential(client, installer_headers)
    installer_build_ai = create_random_ai_credential(client, installer_headers)

    install = _install(
        client,
        installer_headers,
        fresh_pub["bundle_id"],
        request_body={
            "ai_credential_selections": {
                "conversation_credential_id": installer_conv_ai["id"],
                "building_credential_id": installer_build_ai["id"],
            }
        },
    )

    # ── Phase 3: env FKs should match bundle PBP fields, not request ──────────
    env = _get_env(client, installer_headers, install["id"])
    assert env is not None
    assert str(env["conversation_ai_credential_id"]) == str(conv_ai_id), (
        f"Env conversation FK should be bundle's PBP cred {conv_ai_id}; "
        f"got {env['conversation_ai_credential_id']}"
    )
    assert str(env["building_ai_credential_id"]) == str(build_ai_id), (
        f"Env building FK should be bundle's PBP cred {build_ai_id}; "
        f"got {env['building_ai_credential_id']}"
    )


# ── Scenario J: Publisher AI credential — installer is publisher ──────────────


def test_publisher_ai_credential_no_self_share_on_publisher_install(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """J. Publisher's own agent already exists as a publisher install.
    Setting a PBP AI credential on the bundle must NOT create a
    AICredentialShare(publisher → publisher) row.

    Assert:
    - No AICredentialShare where shared_with_user_id == publisher_user_id.
    """
    # ── Phase 1: publish + set PBP AI credential ─────────────────────────────
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="InstCred-J-Publisher"
    )
    drain_tasks()

    conv_ai = create_random_ai_credential(client, superuser_token_headers)
    conv_ai_id = uuid.UUID(conv_ai["id"])

    fresh_pub = _publish(client, superuser_token_headers, publisher_agent["id"])

    r = client.patch(
        f"{API}/bundles/{fresh_pub['bundle_uuid']}",
        headers=superuser_token_headers,
        json={
            "publisher_ai_credential_conversation_id": str(conv_ai_id),
            "is_listed": True,
            "visibility": "public",
        },
    )
    assert r.status_code == 200, r.text

    # ── Phase 2: publisher installs their own bundle ──────────────────────────
    publisher_user_id_str = fresh_pub.get("owner_id")
    publisher_user_id = uuid.UUID(publisher_user_id_str)

    # The catalog install for the publisher themselves (idempotent — returns existing).
    _install(client, superuser_token_headers, fresh_pub["bundle_id"])

    # ── Phase 3: verify no self-share row ─────────────────────────────────────
    db.expire_all()

    self_share = db.exec(
        select(AICredentialShare).where(
            AICredentialShare.ai_credential_id == conv_ai_id,
            AICredentialShare.shared_with_user_id == publisher_user_id,
        )
    ).first()
    assert self_share is None, (
        "Must NOT create AICredentialShare(publisher → publisher) on publisher install"
    )


# ── Scenario K: Mixed PBP + PBU service credentials ──────────────────────────


def test_mixed_pbp_and_pbu_credentials_no_cross_contamination(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """K. Single revision with two specs: one PBP (shareable), one PBU.

    Assert:
    - PBP → AgentCredentialLink to publisher row + CredentialShare.
    - PBU → placeholder Credential + link to placeholder.
    - No cross-contamination (placeholder not linked to PBP spec's cred, etc.).
    """
    # ── Phase 1: publish bundle with one PBP + one PBU credential ────────────
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="InstCred-K-Publisher"
    )
    drain_tasks()

    pbp_cred = _create_credential(
        client, superuser_token_headers, name="ic-k-shared", allow_sharing=True
    )
    pbu_cred = _create_credential(
        client, superuser_token_headers, name="ic-k-private", allow_sharing=False
    )
    _link_credential_to_agent(
        client, superuser_token_headers, publisher_agent["id"], pbp_cred["id"]
    )
    _link_credential_to_agent(
        client, superuser_token_headers, publisher_agent["id"], pbu_cred["id"]
    )

    fresh_pub = _publish(client, superuser_token_headers, publisher_agent["id"])
    _make_public(client, superuser_token_headers, fresh_pub["bundle_uuid"])

    # ── Phase 2: foreign user installs ───────────────────────────────────────
    installer, installer_headers = _make_user_and_headers(client)
    install = _install(client, installer_headers, fresh_pub["bundle_id"])
    install_id = uuid.UUID(install["id"])
    installer_id = uuid.UUID(installer["id"])
    pbp_cred_id = uuid.UUID(pbp_cred["id"])

    # ── Phase 3: verify two links, share for PBP, placeholder for PBU ────────
    db.expire_all()

    links = db.exec(
        select(AgentCredentialLink).where(
            AgentCredentialLink.agent_id == install_id
        )
    ).all()
    assert len(links) == 2, f"Expected 2 links (1 PBP + 1 PBU); got {len(links)}"

    linked_ids = {lnk.credential_id for lnk in links}

    # PBP: link to publisher's credential.
    assert pbp_cred_id in linked_ids, (
        f"PBP cred {pbp_cred_id} not found in linked_ids {linked_ids}"
    )

    # CredentialShare for PBP exists.
    share = db.exec(
        select(CredentialShare).where(
            CredentialShare.credential_id == pbp_cred_id,
            CredentialShare.shared_with_user_id == installer_id,
        )
    ).first()
    assert share is not None, "Expected CredentialShare for PBP credential"

    # PBU: one placeholder created for installer.
    placeholder_link_ids = linked_ids - {pbp_cred_id}
    assert len(placeholder_link_ids) == 1, (
        f"Expected exactly 1 placeholder link; got {placeholder_link_ids}"
    )
    placeholder_cred = db.get(Credential, next(iter(placeholder_link_ids)))
    assert placeholder_cred is not None
    assert placeholder_cred.is_placeholder is True, (
        f"PBU credential must be a placeholder; got is_placeholder={placeholder_cred.is_placeholder}"
    )
    assert placeholder_cred.owner_id == installer_id, (
        f"Placeholder owner must be installer {installer_id}; got {placeholder_cred.owner_id}"
    )


# ── Scenario M: AICredentialShare deleted manually is recreated on re-install ─


def test_ai_credential_share_recreated_on_reinstall_after_manual_deletion(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """M. Install creates AICredentialShare; delete it directly via db;
    re-install (idempotent path) must recreate the share.

    The idempotent install path in install_service.py returns the existing
    Agent row without re-running _install_from_revision, but it DOES call
    _link_publisher_ai_credential (which is itself idempotent) before
    returning. This makes install_bundle self-healing for the AI share row
    across re-installs, so a manually-deleted AICredentialShare is
    recreated on the next POST /catalog/{bundle_id}/install.
    """
    # ── Phase 1: publish + set PBP AI credential + install ───────────────────
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="InstCred-M-Publisher"
    )
    drain_tasks()
    fresh_pub = _publish(client, superuser_token_headers, publisher_agent["id"])

    conv_ai = create_random_ai_credential(client, superuser_token_headers)
    conv_ai_id = uuid.UUID(conv_ai["id"])

    r = client.patch(
        f"{API}/bundles/{fresh_pub['bundle_uuid']}",
        headers=superuser_token_headers,
        json={
            "publisher_ai_credential_conversation_id": str(conv_ai_id),
            "is_listed": True,
            "visibility": "public",
        },
    )
    assert r.status_code == 200, r.text

    installer, installer_headers = _make_user_and_headers(client)
    installer_id = uuid.UUID(installer["id"])

    install = _install(client, installer_headers, fresh_pub["bundle_id"])

    # ── Phase 2: verify share created, then delete it ─────────────────────────
    db.expire_all()
    share = db.exec(
        select(AICredentialShare).where(
            AICredentialShare.ai_credential_id == conv_ai_id,
            AICredentialShare.shared_with_user_id == installer_id,
        )
    ).first()
    assert share is not None, "Expected AICredentialShare after first install"

    db.delete(share)
    db.commit()

    # Verify deleted.
    db.expire_all()
    gone = db.exec(
        select(AICredentialShare).where(
            AICredentialShare.ai_credential_id == conv_ai_id,
            AICredentialShare.shared_with_user_id == installer_id,
        )
    ).first()
    assert gone is None, "AICredentialShare should be deleted at this point"

    # ── Phase 3: re-install ───────────────────────────────────────────────────
    install2 = _install(client, installer_headers, fresh_pub["bundle_id"])
    assert install2["id"] == install["id"], "Idempotent re-install must return same row"

    # InstallService.install_bundle calls _link_publisher_ai_credential
    # before its idempotent early-return, so a manually-deleted
    # AICredentialShare is recreated on the next re-install.
    db.expire_all()
    recreated = db.exec(
        select(AICredentialShare).where(
            AICredentialShare.ai_credential_id == conv_ai_id,
            AICredentialShare.shared_with_user_id == installer_id,
        )
    ).first()
    assert recreated is not None, (
        "Expected idempotent re-install to recreate the AICredentialShare "
        "via _link_publisher_ai_credential — install_bundle should be "
        "self-healing for the publisher AI share row."
    )
