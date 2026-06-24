"""Credentials list filter tabs + bundle-provenance shares — integration tests.

Tests the new ``category``, ``agent_usage_count``, and ``used_in_bundle`` fields
on ``CredentialPublic`` / ``SharedCredentialPublic``, the ``CredentialShare.source``
provenance column, and the install-time PBP auto-share wiring.

Tested surface areas:
  1. Categorization correctness (owned vs shared; automatic types; bundle vs direct)
  2. Install-time auto-share: provenance stamped + idempotency + first-writer-wins
  3. NULL source legacy: share with source=NULL categorizes as "mine"
  4. Agent-usage count: owner-scoped vs recipient-scoped
  5. Bundle badge (used_in_bundle): True after a PBP bundle publish; False otherwise

Direct DB access via the ``db`` fixture is used where no public API exposes the
exact assertion point (CredentialShare.source, NULL-source seeding).  This follows
the precedent set in ``agents_bundles_install_credentials_test.py``.
"""
import uuid

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import settings
from app.models.credentials.credential_share import CredentialShare
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import (
    create_bundle_credential as _create_credential,
    install_bundle as _install,
    link_bundle_credential_to_agent as _link_credential_to_agent,
    make_bundle_public as _make_public,
    make_user_and_headers as _make_user_and_headers,
    publish_bundle as _publish,
)
from tests.utils.credential import (
    create_random_credential,
    link_credential_to_agent,
    share_credential_via_api,
    set_credential_sharing,
)
from tests.utils.user import create_random_user, promote_to_developer, user_authentication_headers

API = settings.API_V1_STR


# ── Helpers ───────────────────────────────────────────────────────────────────


def _list_owned_credentials(
    client: TestClient,
    headers: dict[str, str],
) -> list[dict]:
    """Return all owned credentials via GET /credentials/."""
    r = client.get(f"{API}/credentials/", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _list_shared_with_me(
    client: TestClient,
    headers: dict[str, str],
) -> list[dict]:
    """Return all shared credentials via GET /credentials/shared-with-me."""
    r = client.get(f"{API}/credentials/shared-with-me", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _find_credential_in_list(
    cred_list: list[dict],
    cred_id: str,
) -> dict | None:
    """Find a credential dict by id in a list, or return None."""
    return next((c for c in cred_list if c["id"] == cred_id), None)


def _connect_agent_api(
    client: TestClient,
    headers: dict[str, str],
    producer_agent_id: str,
) -> dict:
    """Enable agent_api on the producer and call the connect helper.

    Returns the connect response (credential_id, token_id, etc.).
    """
    from tests.utils.agent import update_agent

    update_agent(client, headers, producer_agent_id, agent_api_enabled=True)
    r = client.post(
        f"{API}/agents/{producer_agent_id}/agent-api/connect",
        headers=headers,
        json={"read_only_override": False},
    )
    assert r.status_code == 200, r.text
    return r.json()


# ── Scenario 1: Owned credential categorization correctness ──────────────────


def test_owned_credential_categorization(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Owned credentials are categorized based on type.

    1. Create an ordinary ``email_smtp`` credential → category = "mine".
    2. Create an agent with agent_api enabled and connect it to produce an
       ``agent_api`` credential → category = "automatic".
    3. List GET /credentials/ and verify both categories.
    """
    # ── Phase 1: ordinary (email_smtp) credential → "mine" ────────────────────
    smtp_cred = create_random_credential(
        client, superuser_token_headers, credential_type="email_smtp"
    )
    smtp_cred_id = smtp_cred["id"]

    # ── Phase 2: agent_api credential → "automatic" ───────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers, name="CredCat-1-Agent")
    drain_tasks()
    conn = _connect_agent_api(client, superuser_token_headers, agent["id"])
    api_cred_id = conn["credential_id"]

    # ── Phase 3: verify categories in the owned list ──────────────────────────
    owned = _list_owned_credentials(client, superuser_token_headers)

    smtp_entry = _find_credential_in_list(owned, smtp_cred_id)
    assert smtp_entry is not None, "smtp credential not found in owned list"
    assert smtp_entry["category"] == "mine", (
        f"email_smtp credential should be 'mine'; got '{smtp_entry['category']}'"
    )

    api_entry = _find_credential_in_list(owned, api_cred_id)
    assert api_entry is not None, "agent_api credential not found in owned list"
    assert api_entry["type"] == "agent_api"
    assert api_entry["category"] == "automatic", (
        f"agent_api credential should be 'automatic'; got '{api_entry['category']}'"
    )


# ── Scenario 2: mcp_provider credential is "automatic" and not duplicated ────


def test_mcp_provider_credential_is_automatic(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """An mcp_provider credential is folded into 'automatic', not 'mine' or 'bundle'.

    1. Create an mcp_provider credential directly.
    2. List GET /credentials/ and verify category == "automatic".
    3. Ensure it appears only ONCE in the list (not duplicated).
    """
    # ── Phase 1: create mcp_provider credential directly ──────────────────────
    r = client.post(
        f"{API}/credentials/",
        headers=superuser_token_headers,
        json={
            "name": f"mcp-test-{uuid.uuid4().hex[:8]}",
            "type": "mcp_provider",
            "credential_data": {
                "server_url": "https://mcp.example.com",
                "name": "test-mcp-server",
            },
        },
    )
    assert r.status_code == 200, r.text
    mcp_cred_id = r.json()["id"]

    # ── Phase 2: verify category == "automatic" in the owned list ─────────────
    owned = _list_owned_credentials(client, superuser_token_headers)

    mcp_entries = [c for c in owned if c["id"] == mcp_cred_id]
    assert len(mcp_entries) == 1, (
        f"Expected mcp_provider credential to appear exactly once; got {len(mcp_entries)}"
    )
    assert mcp_entries[0]["category"] == "automatic", (
        f"mcp_provider should be 'automatic'; got '{mcp_entries[0]['category']}'"
    )


# ── Scenario 3: direct share → recipient sees "mine" ─────────────────────────


def test_direct_share_category_is_mine(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """A credential shared user→user (direct share) lands in the recipient's 'mine' tab.

    1. Publisher creates a shareable credential.
    2. Publisher shares it with a recipient.
    3. Recipient calls GET /credentials/shared-with-me and verifies
       category == "mine".
    """
    # ── Phase 1: create shareable credential ─────────────────────────────────
    owner_cred = _create_credential(
        client, superuser_token_headers, name="cc3-direct-share", allow_sharing=True
    )
    owner_cred_id = owner_cred["id"]

    # ── Phase 2: create recipient and share ───────────────────────────────────
    recipient, recipient_headers = _make_user_and_headers(client)
    recipient_email = recipient["email"]

    share_credential_via_api(
        client, superuser_token_headers, owner_cred_id, recipient_email
    )

    # ── Phase 3: recipient verifies category == "mine" ────────────────────────
    shared = _list_shared_with_me(client, recipient_headers)
    entry = _find_credential_in_list(shared, owner_cred_id)
    assert entry is not None, "shared credential not found in shared-with-me list"
    assert entry["category"] == "mine", (
        f"Direct-shared credential should be 'mine' for recipient; got '{entry['category']}'"
    )
    assert entry["source"] == "direct", (
        f"Direct share should have source='direct'; got '{entry['source']}'"
    )


# ── Scenario 4: PBP install → recipient sees "bundle" ────────────────────────


def test_pbp_install_share_category_is_bundle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """A credential shared via PBP bundle install lands in the recipient's 'bundle' tab.

    1. Publisher creates an agent with a shareable credential + publishes bundle.
    2. Installer installs the bundle.
    3. Installer calls GET /credentials/shared-with-me → category == "bundle".
    4. Verify share.source == "bundle_install" via db fixture.

    Unit tests for classify_credential_category are in tests/unit/ if any; this is
    the API-observable integration path.
    """
    # ── Phase 1: publisher creates and publishes bundle with PBP credential ───
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="CredCat-4-Publisher"
    )
    drain_tasks()

    shared_cred = _create_credential(
        client, superuser_token_headers, name="cc4-pbp-shared", allow_sharing=True
    )
    _link_credential_to_agent(
        client, superuser_token_headers, publisher_agent["id"], shared_cred["id"]
    )
    fresh_pub = _publish(client, superuser_token_headers, publisher_agent["id"])
    _make_public(client, superuser_token_headers, fresh_pub["bundle_uuid"])

    # ── Phase 2: installer installs the bundle ────────────────────────────────
    installer, installer_headers = _make_user_and_headers(client)
    installer_id = uuid.UUID(installer["id"])
    shared_cred_id = uuid.UUID(shared_cred["id"])

    _install(client, installer_headers, fresh_pub["bundle_id"])

    # ── Phase 3: verify category == "bundle" in shared-with-me ───────────────
    shared_list = _list_shared_with_me(client, installer_headers)
    entry = _find_credential_in_list(shared_list, shared_cred["id"])
    assert entry is not None, "PBP credential not found in installer's shared-with-me list"
    assert entry["category"] == "bundle", (
        f"PBP-installed credential should be 'bundle'; got '{entry['category']}'"
    )
    assert entry["source"] == "bundle_install", (
        f"PBP share source should be 'bundle_install'; got '{entry['source']}'"
    )

    # ── Phase 4: verify share.source via db fixture ───────────────────────────
    db.expire_all()
    share = db.exec(
        select(CredentialShare).where(
            CredentialShare.credential_id == shared_cred_id,
            CredentialShare.shared_with_user_id == installer_id,
        )
    ).first()
    assert share is not None, "Expected CredentialShare row to exist"
    assert share.source == "bundle_install", (
        f"CredentialShare.source should be 'bundle_install'; got '{share.source}'"
    )


# ── Scenario 5: install idempotency + provenance stability ───────────────────


def test_pbp_install_idempotency_and_source_not_overwritten(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """Re-installing does not duplicate the share and does not change its source.

    1. Publish a bundle with a PBP credential and install it once.
    2. Re-install (idempotent path) and verify: exactly one CredentialShare,
       source is still 'bundle_install'.
    3. Category stays "bundle" after re-install.
    """
    # ── Phase 1: publish + first install ──────────────────────────────────────
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="CredCat-5-Publisher"
    )
    drain_tasks()

    shared_cred = _create_credential(
        client, superuser_token_headers, name="cc5-idempotent", allow_sharing=True
    )
    _link_credential_to_agent(
        client, superuser_token_headers, publisher_agent["id"], shared_cred["id"]
    )
    fresh_pub = _publish(client, superuser_token_headers, publisher_agent["id"])
    _make_public(client, superuser_token_headers, fresh_pub["bundle_uuid"])

    installer, installer_headers = _make_user_and_headers(client)
    installer_id = uuid.UUID(installer["id"])
    shared_cred_id = uuid.UUID(shared_cred["id"])

    first_install = _install(client, installer_headers, fresh_pub["bundle_id"])

    # ── Phase 2: re-install ───────────────────────────────────────────────────
    second_install = _install(client, installer_headers, fresh_pub["bundle_id"])
    assert second_install["id"] == first_install["id"], (
        "Idempotent re-install must return the same install row"
    )

    # ── Phase 3: exactly one share + source unchanged ─────────────────────────
    db.expire_all()
    shares = db.exec(
        select(CredentialShare).where(
            CredentialShare.credential_id == shared_cred_id,
            CredentialShare.shared_with_user_id == installer_id,
        )
    ).all()
    assert len(shares) == 1, (
        f"Expected exactly 1 CredentialShare after re-install; got {len(shares)}"
    )
    assert shares[0].source == "bundle_install", (
        f"source must remain 'bundle_install' after re-install; got '{shares[0].source}'"
    )

    # ── Phase 4: API still shows "bundle" after re-install ────────────────────
    shared_list = _list_shared_with_me(client, installer_headers)
    entry = _find_credential_in_list(shared_list, shared_cred["id"])
    assert entry is not None, "Credential missing from shared-with-me after re-install"
    assert entry["category"] == "bundle", (
        f"Category should still be 'bundle' after re-install; got '{entry['category']}'"
    )


# ── Scenario 6: first-writer-wins — direct share not overwritten by install ──


def test_first_writer_wins_direct_share_not_overwritten_by_install(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """Pre-existing direct share is NOT overwritten to 'bundle_install' on install.

    1. Publisher creates a shareable credential and directly shares it with a user.
    2. User's share has source='direct' → category='mine'.
    3. Publisher publishes a bundle with the same credential.
    4. The same user installs the bundle.
    5. The share source remains 'direct' → credential stays in 'mine' tab.
    """
    # ── Phase 1: create credential and share directly with future installer ───
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="CredCat-6-Publisher"
    )
    drain_tasks()

    shared_cred = _create_credential(
        client, superuser_token_headers, name="cc6-first-writer", allow_sharing=True
    )
    shared_cred_id = uuid.UUID(shared_cred["id"])

    # Create the installer first so we can share directly before install.
    installer, installer_headers = _make_user_and_headers(client)
    installer_id = uuid.UUID(installer["id"])
    installer_email = installer["email"]

    # Direct share first — this stamps source='direct'.
    share_credential_via_api(
        client, superuser_token_headers, shared_cred["id"], installer_email
    )

    # ── Phase 2: verify direct share before install ────────────────────────────
    shared_before = _list_shared_with_me(client, installer_headers)
    entry_before = _find_credential_in_list(shared_before, shared_cred["id"])
    assert entry_before is not None, "Credential not found in shared-with-me before install"
    assert entry_before["category"] == "mine", (
        f"Direct-shared credential should be 'mine' before install; "
        f"got '{entry_before['category']}'"
    )
    assert entry_before["source"] == "direct"

    # ── Phase 3: publish bundle with same credential ───────────────────────────
    _link_credential_to_agent(
        client, superuser_token_headers, publisher_agent["id"], shared_cred["id"]
    )
    fresh_pub = _publish(client, superuser_token_headers, publisher_agent["id"])
    _make_public(client, superuser_token_headers, fresh_pub["bundle_uuid"])

    # ── Phase 4: installer installs the bundle ────────────────────────────────
    _install(client, installer_headers, fresh_pub["bundle_id"])

    # ── Phase 5: share source must remain 'direct' → category stays 'mine' ───
    db.expire_all()
    share = db.exec(
        select(CredentialShare).where(
            CredentialShare.credential_id == shared_cred_id,
            CredentialShare.shared_with_user_id == installer_id,
        )
    ).first()
    assert share is not None, "CredentialShare row should still exist"
    assert share.source == "direct", (
        f"First-writer-wins: source should still be 'direct' after install; "
        f"got '{share.source}'"
    )

    shared_after = _list_shared_with_me(client, installer_headers)
    entry_after = _find_credential_in_list(shared_after, shared_cred["id"])
    assert entry_after is not None, "Credential not found in shared-with-me after install"
    assert entry_after["category"] == "mine", (
        f"First-writer-wins: category must remain 'mine' after install; "
        f"got '{entry_after['category']}'"
    )


# ── Scenario 7: NULL source legacy — categorized as 'mine' ───────────────────


def test_null_source_share_is_categorized_mine(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """A CredentialShare with source=NULL is categorized as 'mine' (legacy=direct).

    The API always stamps a source on new shares, so we must seed a NULL-source
    row directly via the db fixture to simulate pre-feature legacy rows.

    1. Create a shareable credential and a recipient user.
    2. Insert a CredentialShare with source=NULL directly via db.
    3. Recipient calls GET /credentials/shared-with-me → category == "mine".
    """
    # ── Phase 1: create shareable credential and recipient user ───────────────
    owner_cred = _create_credential(
        client, superuser_token_headers, name="cc7-null-source", allow_sharing=True
    )
    owner_cred_id = uuid.UUID(owner_cred["id"])

    # Get owner id from credential response
    owner_id = uuid.UUID(owner_cred["owner_id"])

    recipient, recipient_headers = _make_user_and_headers(client)
    recipient_id = uuid.UUID(recipient["id"])

    # ── Phase 2: insert NULL-source CredentialShare directly ──────────────────
    # source=NULL simulates a pre-feature legacy row (backfill not done).
    null_source_share = CredentialShare(
        credential_id=owner_cred_id,
        shared_with_user_id=recipient_id,
        shared_by_user_id=owner_id,
        access_level="read",
        source=None,  # Legacy: NULL
    )
    db.add(null_source_share)
    db.commit()

    # ── Phase 3: recipient verifies category == "mine" ────────────────────────
    shared = _list_shared_with_me(client, recipient_headers)
    entry = _find_credential_in_list(shared, owner_cred["id"])
    assert entry is not None, (
        "NULL-source credential should appear in recipient's shared-with-me"
    )
    assert entry["category"] == "mine", (
        f"NULL source should be read as 'direct' → category 'mine'; "
        f"got '{entry['category']}'"
    )
    # source is exposed as-is (None/null) — not coerced on the wire
    assert entry.get("source") is None, (
        f"source should be null on wire for legacy NULL row; got '{entry.get('source')}'"
    )


# ── Scenario 8: agent_usage_count — owner-scoped ─────────────────────────────


def test_agent_usage_count_owner_scoped(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Linking a credential to N of the owner's agents yields agent_usage_count == N.

    1. Create a credential and link it to 2 agents.
    2. GET /credentials/ → agent_usage_count == 2.
    3. Unlink from one agent → agent_usage_count == 1.
    """
    # ── Phase 1: create credential and two agents ──────────────────────────────
    cred = create_random_credential(
        client, superuser_token_headers, credential_type="api_token"
    )
    cred_id = cred["id"]

    agent1 = create_agent_via_api(client, superuser_token_headers, name="CredCat-8-A1")
    agent2 = create_agent_via_api(client, superuser_token_headers, name="CredCat-8-A2")
    drain_tasks()

    # ── Phase 2: link credential to both agents ───────────────────────────────
    link_credential_to_agent(client, superuser_token_headers, agent1["id"], cred_id)
    link_credential_to_agent(client, superuser_token_headers, agent2["id"], cred_id)

    # ── Phase 3: verify agent_usage_count == 2 ────────────────────────────────
    owned = _list_owned_credentials(client, superuser_token_headers)
    entry = _find_credential_in_list(owned, cred_id)
    assert entry is not None, "Credential not found in owned list"
    assert entry["agent_usage_count"] == 2, (
        f"Expected agent_usage_count=2; got {entry['agent_usage_count']}"
    )

    # ── Phase 4: unlink from one agent → count drops to 1 ─────────────────────
    r = client.delete(
        f"{API}/agents/{agent1['id']}/credentials/{cred_id}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200, r.text

    owned_after = _list_owned_credentials(client, superuser_token_headers)
    entry_after = _find_credential_in_list(owned_after, cred_id)
    assert entry_after is not None
    assert entry_after["agent_usage_count"] == 1, (
        f"Expected agent_usage_count=1 after unlink; got {entry_after['agent_usage_count']}"
    )


# ── Scenario 9: agent_usage_count — recipient-scoped for shared credentials ──


def test_agent_usage_count_recipient_scoped_for_shared_credential(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """For a shared credential, agent_usage_count reflects only the RECIPIENT's agents.

    PBP install mechanics: when the installer installs the bundle, the install
    service creates an AgentCredentialLink from the installed agent to the
    publisher's credential. That installed agent is owned by the installer, so
    the recipient-scoped count starts at 1 (one agent: the bundle install agent)
    immediately after install.

    The publisher links the same credential to 3 additional agents of their own.
    The installer should see only their own count (1), not the publisher's (4).

    1. Publisher creates a shareable credential, links it to the bundle agent and
       3 more extra agents (publisher total: 4).
    2. Publisher publishes the bundle (bundle agent carries the credential).
    3. Installer installs the bundle → install creates AgentCredentialLink for
       the install agent → installer-scoped count == 1.
    4. Verify installer sees agent_usage_count == 1, not 4.
    5. Installer links to one more of their own agents → count becomes 2
       (confirms the count is live and recipient-scoped).
    """
    # ── Phase 1: publisher creates credential + 4 agents + links ─────────────
    # pub_agent1 will be the bundle agent (carries the credential in the bundle spec).
    pub_agent1 = create_agent_via_api(
        client, superuser_token_headers, name="CredCat-9-Pub-Bundle"
    )
    # 3 extra publisher agents purely to inflate the publisher's own count.
    pub_agent2 = create_agent_via_api(
        client, superuser_token_headers, name="CredCat-9-Pub-A2"
    )
    pub_agent3 = create_agent_via_api(
        client, superuser_token_headers, name="CredCat-9-Pub-A3"
    )
    pub_agent4 = create_agent_via_api(
        client, superuser_token_headers, name="CredCat-9-Pub-A4"
    )
    drain_tasks()

    shared_cred = _create_credential(
        client, superuser_token_headers, name="cc9-scoped-count", allow_sharing=True
    )
    shared_cred_id = shared_cred["id"]

    # Link to the bundle agent (goes into the bundle spec).
    _link_credential_to_agent(
        client, superuser_token_headers, pub_agent1["id"], shared_cred_id
    )
    # Link to 3 extra publisher agents (publisher-side usage, invisible to installer).
    link_credential_to_agent(
        client, superuser_token_headers, pub_agent2["id"], shared_cred_id
    )
    link_credential_to_agent(
        client, superuser_token_headers, pub_agent3["id"], shared_cred_id
    )
    link_credential_to_agent(
        client, superuser_token_headers, pub_agent4["id"], shared_cred_id
    )

    # Confirm publisher's own count == 4.
    pub_owned = _list_owned_credentials(client, superuser_token_headers)
    pub_entry = _find_credential_in_list(pub_owned, shared_cred_id)
    assert pub_entry is not None, "Credential not found in publisher's owned list"
    assert pub_entry["agent_usage_count"] == 4, (
        f"Publisher should see agent_usage_count=4; got {pub_entry['agent_usage_count']}"
    )

    # ── Phase 2: publish bundle (pub_agent1 carries the credential) ────────────
    fresh_pub = _publish(client, superuser_token_headers, pub_agent1["id"])
    _make_public(client, superuser_token_headers, fresh_pub["bundle_uuid"])

    # ── Phase 3: installer installs bundle ────────────────────────────────────
    # The PBP install creates an AgentCredentialLink for the install agent (owned
    # by the installer), so installer-scoped count immediately becomes 1, not 4.
    installer, installer_headers = _make_user_and_headers(client)
    _install(client, installer_headers, fresh_pub["bundle_id"])

    shared_list = _list_shared_with_me(client, installer_headers)
    entry = _find_credential_in_list(shared_list, shared_cred_id)
    assert entry is not None, "Shared credential not found in installer's shared-with-me"
    # Installer has 1 agent linked (the install agent). Publisher has 4. Only 1 visible.
    assert entry["agent_usage_count"] == 1, (
        f"Recipient should see agent_usage_count=1 (install agent only, not publisher's 4); "
        f"got {entry['agent_usage_count']}"
    )

    # ── Phase 4: installer links to one more of their own agents → count == 2 ─
    # promote_to_developer is required: agent creation is gated on the developer role.
    promote_to_developer(client, superuser_token_headers, installer["id"])
    installer_agent2 = create_agent_via_api(
        client, installer_headers, name="CredCat-9-Installer-A2"
    )
    drain_tasks()
    link_credential_to_agent(
        client, installer_headers, installer_agent2["id"], shared_cred_id
    )

    shared_list_after = _list_shared_with_me(client, installer_headers)
    entry_after = _find_credential_in_list(shared_list_after, shared_cred_id)
    assert entry_after is not None, "Shared credential missing after extra link"
    assert entry_after["agent_usage_count"] == 2, (
        f"Recipient's count should be 2 after linking a second own agent; "
        f"got {entry_after['agent_usage_count']}"
    )


# ── Scenario 10: used_in_bundle badge ────────────────────────────────────────


def test_used_in_bundle_flag(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """used_in_bundle is True for credentials used in a published bundle; False otherwise.

    1. Create two credentials: one linked to a published bundle, one not.
    2. GET /credentials/ → credential in bundle has used_in_bundle=True;
       unused credential has used_in_bundle=False.
    """
    # ── Phase 1: create two credentials ───────────────────────────────────────
    bundled_cred = _create_credential(
        client, superuser_token_headers, name="cc10-bundled", allow_sharing=True
    )
    unbundled_cred = create_random_credential(
        client, superuser_token_headers, credential_type="api_token"
    )

    # ── Phase 2: publish an agent with the bundled credential ─────────────────
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="CredCat-10-Publisher"
    )
    drain_tasks()
    _link_credential_to_agent(
        client, superuser_token_headers, publisher_agent["id"], bundled_cred["id"]
    )
    _publish(client, superuser_token_headers, publisher_agent["id"])

    # ── Phase 3: verify used_in_bundle flags ──────────────────────────────────
    owned = _list_owned_credentials(client, superuser_token_headers)

    bundled_entry = _find_credential_in_list(owned, bundled_cred["id"])
    assert bundled_entry is not None, "bundled credential not in owned list"
    assert bundled_entry["used_in_bundle"] is True, (
        f"Expected used_in_bundle=True for bundled credential; "
        f"got {bundled_entry['used_in_bundle']}"
    )

    unbundled_entry = _find_credential_in_list(owned, unbundled_cred["id"])
    assert unbundled_entry is not None, "unbundled credential not in owned list"
    assert unbundled_entry["used_in_bundle"] is False, (
        f"Expected used_in_bundle=False for unbundled credential; "
        f"got {unbundled_entry['used_in_bundle']}"
    )


# ── Scenario 11: api_token credential initial state ──────────────────────────


def test_api_token_owned_initial_badge_state(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """A fresh owned api_token credential has category='mine', agent_usage_count=0,
    used_in_bundle=False.

    This guards the default values on CredentialPublic.
    """
    cred = create_random_credential(
        client, superuser_token_headers, credential_type="api_token"
    )
    cred_id = cred["id"]

    owned = _list_owned_credentials(client, superuser_token_headers)
    entry = _find_credential_in_list(owned, cred_id)
    assert entry is not None, "Newly-created credential not in owned list"

    assert entry["category"] == "mine", (
        f"Fresh api_token should be 'mine'; got '{entry['category']}'"
    )
    assert entry["agent_usage_count"] == 0, (
        f"Fresh credential should have agent_usage_count=0; got {entry['agent_usage_count']}"
    )
    assert entry["used_in_bundle"] is False, (
        f"Fresh credential should have used_in_bundle=False; got {entry['used_in_bundle']}"
    )
