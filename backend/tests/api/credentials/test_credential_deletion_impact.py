"""Credential deletion impact gate — tests for service and AI credentials.

Covers the blast-radius classification endpoint and the force/non-force delete
gate for both service credentials (GET /credentials/{id}/deletion-impact and
DELETE /credentials/{id}?force=...) and AI credentials
(GET /ai-credentials/{id}/deletion-impact and DELETE /ai-credentials/{id}?force=...).

Scenarios:
  A. Service credential Tier 0: no shares, no bundles — impact GET returns tier=0,
     unforced DELETE succeeds.  Also asserts bundle_usages==[].
  B. Service credential Tier 1: direct CredentialShare exists, no PBP bundle —
     impact GET returns tier=1 with correct direct_share_count, unforced DELETE
     succeeds (warning tier, not blocking).
  C. Service credential Tier 2: credential is PBP in a published bundle with a
     live foreign install — impact GET returns tier=2 with bundle_pbp_usages
     and active_install_count>=1; unforced DELETE returns 409 (structured detail);
     force=true DELETE succeeds and credential is gone.
     Also asserts bundle_usages contains the bundle with provided_by=="publisher".
  D. PBT exclusion: credential is template-only (allow_template_sharing=True,
     allow_sharing=False) in a published bundle with foreign installs — tier must
     stay 0 (PBT installs are independent copies, not linked to publisher credential).
     Unforced DELETE succeeds.
  E. Direct-share-only recipient linking to own agent must NOT inflate
     active_install_count or trigger Tier 2.
  F. Authorization: non-owner GET deletion-impact returns 404; missing id returns
     404; non-owner DELETE returns appropriate error.
  G. AI credential Tier 0: no bundle references — impact GET returns tier=0,
     DELETE succeeds.
  H. AI credential Tier 2: referenced as publisher_ai_credential_conversation_id
     in a published bundle — impact GET returns tier=2 with bundle_usages;
     unforced DELETE returns 409; force=true succeeds.
  I. AI credential authorization: non-owner GET deletion-impact returns 404.
  J. PBT bundle membership surfaces in bundle_usages without raising the tier:
     a PBT credential appears in bundle_usages (provided_by=="template") but NOT
     in bundle_pbp_usages, and tier remains < 2.
  K. PBP credential with active install appears in BOTH bundle_usages and
     bundle_pbp_usages, with provided_by=="publisher" in both.
"""

import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.credential import create_random_credential
from tests.utils.user import create_random_user, promote_to_developer, user_authentication_headers

API = settings.API_V1_STR


# ── Module-level helpers ──────────────────────────────────────────────────────


def _make_user_and_headers(
    client: TestClient,
    superuser_headers: dict[str, str] | None = None,
    *,
    developer: bool = True,
) -> tuple[dict, dict[str, str]]:
    """Create a random user with a default AI credential; return (user, headers).

    When *developer* is True and *superuser_headers* is supplied, the user is
    promoted to agent-developer so they can create agents.
    """
    user = create_random_user(client)
    headers = user_authentication_headers(
        client=client, email=user["email"], password=user["_password"]
    )
    create_random_ai_credential(client, headers, set_default=True)
    if developer and superuser_headers is not None:
        promote_to_developer(client, superuser_headers, user["id"])
    return user, headers


def _create_pbp_credential(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str | None = None,
) -> dict:
    """Create a service credential with allow_sharing=True (PBP-eligible)."""
    name = name or f"pbp-cred-{uuid.uuid4().hex[:8]}"
    r = client.post(
        f"{API}/credentials/",
        headers=headers,
        json={
            "name": name,
            "type": "api_token",
            "allow_sharing": True,
            "credential_data": {
                "api_token_type": "bearer",
                "api_token_template": "Authorization: Bearer {TOKEN}",
                "api_token": "test-token-pbp",
            },
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _create_pbt_credential(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str | None = None,
) -> dict:
    """Create an Odoo credential with allow_template_sharing=True (PBT-only)."""
    name = name or f"pbt-cred-{uuid.uuid4().hex[:8]}"
    r = client.post(
        f"{API}/credentials/",
        headers=headers,
        json={
            "name": name,
            "type": "odoo",
            "allow_sharing": False,
            "allow_template_sharing": True,
            "template_private_fields": ["api_token"],
            "credential_data": {
                "url": "https://erp.example.com",
                "database_name": "test_db",
                "login": "admin",
                "api_token": "secret-token",
            },
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def _link_credential(
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
    """Publish agent, drain tasks, return fresh agent row."""
    r = client.post(f"{API}/agents/{agent_id}/publish", headers=headers, json={})
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
) -> dict:
    r = client.post(
        f"{API}/catalog/{bundle_id}/install",
        headers=headers,
        json={},
    )
    assert r.status_code == 200, r.text
    drain_tasks()
    return r.json()


def _share_credential(
    client: TestClient,
    headers: dict[str, str],
    credential_id: str,
    recipient_email: str,
) -> dict:
    r = client.post(
        f"{API}/credentials/{credential_id}/shares",
        headers=headers,
        json={"shared_with_email": recipient_email},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _get_deletion_impact(
    client: TestClient,
    headers: dict[str, str],
    credential_id: str,
) -> dict:
    r = client.get(
        f"{API}/credentials/{credential_id}/deletion-impact",
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _get_ai_deletion_impact(
    client: TestClient,
    headers: dict[str, str],
    credential_id: str,
) -> dict:
    r = client.get(
        f"{API}/ai-credentials/{credential_id}/deletion-impact",
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return r.json()


# ── Scenario A: Service credential Tier 0 ────────────────────────────────────


def test_service_credential_tier0_no_shares_no_bundle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """A. Credential linked only to own agents, no shares, no PBP bundle — Tier 0.

    1. Create a credential and link it to a new agent.
    2. GET deletion-impact → tier=0, direct_share_count=0,
       bundle_pbp_usages=[], active_install_count=0.
    3. DELETE (no force) → 200.
    4. Verify credential is gone (GET 404).
    """
    # ── Phase 1: create credential and link to agent ──────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers, name="ImpA-Agent")
    drain_tasks()

    cred = create_random_credential(client, superuser_token_headers)
    cred_id = cred["id"]
    _link_credential(client, superuser_token_headers, agent["id"], cred_id)

    # ── Phase 2: assert Tier 0 impact ────────────────────────────────────────
    impact = _get_deletion_impact(client, superuser_token_headers, cred_id)
    assert impact["tier"] == 0
    assert impact["direct_share_count"] == 0
    assert impact["bundle_pbp_usages"] == []
    assert impact["bundle_usages"] == []
    assert impact["active_install_count"] == 0
    # affected_own_agents should contain the linked agent
    agent_ids = [a["id"] for a in impact.get("affected_own_agents", [])]
    assert agent["id"] in agent_ids

    # ── Phase 3: unforced delete succeeds ────────────────────────────────────
    r = client.delete(
        f"{API}/credentials/{cred_id}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["message"] == "Credential deleted successfully"

    # ── Phase 4: credential is gone ───────────────────────────────────────────
    r = client.get(
        f"{API}/credentials/{cred_id}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 404


# ── Scenario B: Service credential Tier 1 (direct shares, no PBP bundle) ─────


def test_service_credential_tier1_direct_shares_no_pbp(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """B. Credential shared directly with two users, no PBP bundle — Tier 1.

    1. Create allow_sharing=True credential.
    2. Share with two fresh users.
    3. GET deletion-impact → tier=1, direct_share_count=2, bundle_pbp_usages=[].
    4. DELETE (no force) → 200 (Tier 1 is a warning, not blocking).
    5. Verify credential is gone.
    """
    # ── Phase 1: create shareable credential ─────────────────────────────────
    r = client.post(
        f"{API}/credentials/",
        headers=superuser_token_headers,
        json={
            "name": f"tier1-cred-{uuid.uuid4().hex[:8]}",
            "type": "api_token",
            "allow_sharing": True,
            "credential_data": {
                "api_token_type": "bearer",
                "api_token_template": "Authorization: Bearer {TOKEN}",
                "api_token": "tier1-token",
            },
        },
    )
    assert r.status_code == 200, r.text
    cred = r.json()
    cred_id = cred["id"]

    # ── Phase 2: share with two users ─────────────────────────────────────────
    user2 = create_random_user(client)
    user3 = create_random_user(client)
    _share_credential(client, superuser_token_headers, cred_id, user2["email"])
    _share_credential(client, superuser_token_headers, cred_id, user3["email"])

    # ── Phase 3: assert Tier 1 impact ────────────────────────────────────────
    impact = _get_deletion_impact(client, superuser_token_headers, cred_id)
    assert impact["tier"] == 1
    assert impact["direct_share_count"] == 2
    assert impact["bundle_pbp_usages"] == []
    assert impact["active_install_count"] == 0

    # ── Phase 4: unforced delete succeeds (Tier 1 is not blocking) ───────────
    r = client.delete(
        f"{API}/credentials/{cred_id}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200, r.text

    # ── Phase 5: credential is gone ───────────────────────────────────────────
    r = client.get(
        f"{API}/credentials/{cred_id}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 404


# ── Scenario C: Service credential Tier 2 — PBP in published bundle with install


def test_service_credential_tier2_pbp_with_active_install(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """C. PBP credential in published bundle with active foreign install — Tier 2.

    1. Publisher creates agent + PBP credential + publish → make public.
    2. Foreign installer installs from catalog.
    3. GET deletion-impact → tier=2, bundle_pbp_usages populated,
       active_install_count >= 1.
    4. Unforced DELETE → 409 with structured detail (tier==2 in detail body).
    5. Force DELETE → 200; credential is gone (GET 404).
    """
    pub_headers = superuser_token_headers

    # ── Phase 1: publish bundle with PBP credential ───────────────────────────
    publisher_agent = create_agent_via_api(
        client, pub_headers, name="ImpC-Publisher"
    )
    drain_tasks()

    pbp_cred = _create_pbp_credential(client, pub_headers, name="imp-c-pbp-cred")
    pbp_cred_id = pbp_cred["id"]
    _link_credential(client, pub_headers, publisher_agent["id"], pbp_cred_id)

    fresh_pub = _publish(client, pub_headers, publisher_agent["id"])
    bundle_uuid = fresh_pub["bundle_uuid"]
    bundle_id = fresh_pub["bundle_id"]
    _make_public(client, pub_headers, bundle_uuid)

    # ── Phase 2: foreign installer installs ──────────────────────────────────
    installer, installer_headers = _make_user_and_headers(client, superuser_token_headers)
    _install(client, installer_headers, bundle_id)

    # ── Phase 3: impact GET → Tier 2 ─────────────────────────────────────────
    impact = _get_deletion_impact(client, pub_headers, pbp_cred_id)
    assert impact["tier"] == 2
    assert impact["active_install_count"] >= 1
    assert len(impact["bundle_pbp_usages"]) >= 1

    # Bundle usage must reference the published bundle
    usage_bundle_ids = [u["bundle_id"] for u in impact["bundle_pbp_usages"]]
    assert bundle_id in usage_bundle_ids

    # All PBP usages must have provided_by="publisher" (implicit by tier computation)
    for usage in impact["bundle_pbp_usages"]:
        assert usage["bundle_uuid"] is not None
        assert usage["bundle_id"] is not None

    # bundle_usages must also include the bundle (Scenario K: PBP appears in both)
    assert len(impact["bundle_usages"]) >= 1
    all_bundle_ids = [u["bundle_id"] for u in impact["bundle_usages"]]
    assert bundle_id in all_bundle_ids
    # Every entry in bundle_usages for this bundle must carry provided_by=="publisher"
    pbp_in_all = [u for u in impact["bundle_usages"] if u["bundle_id"] == bundle_id]
    assert len(pbp_in_all) >= 1
    for u in pbp_in_all:
        assert u["provided_by"] == "publisher", (
            f"PBP credential bundle_usages entry must have provided_by='publisher'; "
            f"got {u['provided_by']}"
        )

    # ── Phase 4: unforced DELETE → 409 with structured detail ────────────────
    r = client.delete(
        f"{API}/credentials/{pbp_cred_id}",
        headers=pub_headers,
    )
    assert r.status_code == 409, f"Expected 409 for Tier 2; got {r.status_code}: {r.text}"
    detail = r.json()["detail"]
    # The detail is the serialised CredentialDeletionImpact
    assert detail["tier"] == 2, f"Expected tier==2 in 409 detail; got {detail}"
    assert detail["active_install_count"] >= 1
    assert len(detail["bundle_pbp_usages"]) >= 1

    # Credential must still exist
    r_check = client.get(
        f"{API}/credentials/{pbp_cred_id}",
        headers=pub_headers,
    )
    assert r_check.status_code == 200, "Credential should still exist after 409"

    # ── Phase 5: force DELETE → 200; credential gone ─────────────────────────
    r = client.delete(
        f"{API}/credentials/{pbp_cred_id}?force=true",
        headers=pub_headers,
    )
    assert r.status_code == 200, f"Expected 200 for force delete; got {r.status_code}: {r.text}"
    assert r.json()["message"] == "Credential deleted successfully"

    r_gone = client.get(
        f"{API}/credentials/{pbp_cred_id}",
        headers=pub_headers,
    )
    assert r_gone.status_code == 404, "Credential should be deleted after force=true"


# ── Scenario D: PBT credential — template installs must NOT push to Tier 2 ────


def test_pbt_credential_not_tier2_with_foreign_installs(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """D. PBT (template) credential with foreign installs stays Tier 0.

    A template install creates an independent copy (placeholder) owned by the
    installer, not linked to the publisher's original credential.  The publisher's
    own credential should therefore see zero active_install_count and remain Tier 0.

    1. Create PBT credential (allow_template_sharing=True, allow_sharing=False).
    2. Link to publisher agent, publish, make public.
    3. Foreign installer installs.
    4. GET deletion-impact for the original PBT credential → tier=0 (not tier 2).
    5. Unforced DELETE succeeds.
    """
    pub_headers = superuser_token_headers

    # ── Phase 1: publish bundle with PBT credential ───────────────────────────
    publisher_agent = create_agent_via_api(
        client, pub_headers, name="ImpD-Publisher"
    )
    drain_tasks()

    pbt_cred = _create_pbt_credential(client, pub_headers, name="imp-d-pbt-cred")
    pbt_cred_id = pbt_cred["id"]
    assert pbt_cred["allow_sharing"] is False
    assert pbt_cred["allow_template_sharing"] is True

    _link_credential(client, pub_headers, publisher_agent["id"], pbt_cred_id)

    fresh_pub = _publish(client, pub_headers, publisher_agent["id"])
    bundle_uuid = fresh_pub["bundle_uuid"]
    bundle_id = fresh_pub["bundle_id"]
    _make_public(client, pub_headers, bundle_uuid)

    # ── Phase 2: foreign installer installs ──────────────────────────────────
    installer, installer_headers = _make_user_and_headers(client, superuser_token_headers)
    _install(client, installer_headers, bundle_id)

    # ── Phase 3: PBT credential impact must stay Tier 0 ──────────────────────
    impact = _get_deletion_impact(client, pub_headers, pbt_cred_id)
    assert impact["tier"] == 0, (
        f"PBT-only credential must not be Tier 2; got tier={impact['tier']}"
    )
    assert impact["active_install_count"] == 0
    assert impact["bundle_pbp_usages"] == [], (
        f"PBT usages must not appear in bundle_pbp_usages; got {impact['bundle_pbp_usages']}"
    )

    # ── Phase 4: unforced DELETE succeeds ────────────────────────────────────
    r = client.delete(
        f"{API}/credentials/{pbt_cred_id}",
        headers=pub_headers,
    )
    assert r.status_code == 200, r.text


# ── Scenario E: Direct-share recipient linking cred to own agent stays Tier 1 ─


def test_direct_share_recipient_link_does_not_inflate_tier2(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """E. A direct-share recipient who links the credential to their own agent
    must NOT inflate active_install_count or trigger Tier 2.

    The service scopes active_install_count to PBP bundle-uuid installs only,
    so a CredentialShare-linked agent outside a bundle is invisible to the counter.

    1. Owner creates allow_sharing=True credential (NOT in any bundle).
    2. Owner shares with user2.
    3. user2 creates their own agent and links the shared credential to it.
    4. GET deletion-impact for the original credential → tier=1 (direct shares),
       active_install_count=0 (no bundle install), NOT tier=2.
    5. Unforced DELETE succeeds.
    """
    pub_headers = superuser_token_headers

    # ── Phase 1: create shareable credential, share with user2 ──────────────
    r = client.post(
        f"{API}/credentials/",
        headers=pub_headers,
        json={
            "name": f"shared-no-bundle-{uuid.uuid4().hex[:8]}",
            "type": "api_token",
            "allow_sharing": True,
            "credential_data": {
                "api_token_type": "bearer",
                "api_token_template": "Authorization: Bearer {TOKEN}",
                "api_token": "shared-token",
            },
        },
    )
    assert r.status_code == 200, r.text
    cred = r.json()
    cred_id = cred["id"]

    user2, user2_headers = _make_user_and_headers(client, superuser_token_headers)
    _share_credential(client, pub_headers, cred_id, user2["email"])

    # ── Phase 2: user2 creates an agent and links the shared credential ───────
    user2_agent = create_agent_via_api(
        client, user2_headers, name="ImpE-User2-Agent"
    )
    drain_tasks()

    r_link = client.post(
        f"{API}/agents/{user2_agent['id']}/credentials",
        headers=user2_headers,
        json={"credential_id": cred_id},
    )
    assert r_link.status_code == 200, r_link.text

    # ── Phase 3: impact must stay Tier 1, not Tier 2 ─────────────────────────
    impact = _get_deletion_impact(client, pub_headers, cred_id)
    assert impact["tier"] == 1, (
        f"Direct-share linking to user agent must not push to Tier 2; got tier={impact['tier']}"
    )
    assert impact["direct_share_count"] >= 1
    assert impact["active_install_count"] == 0, (
        f"active_install_count must be 0 (no bundle install); got {impact['active_install_count']}"
    )

    # ── Phase 4: unforced DELETE succeeds ────────────────────────────────────
    r = client.delete(
        f"{API}/credentials/{cred_id}",
        headers=pub_headers,
    )
    assert r.status_code == 200, r.text


# ── Scenario F: Authorization — non-owner and missing ID ─────────────────────


def test_service_credential_deletion_impact_authorization(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """F. Authorization guards on GET deletion-impact and DELETE.

    - Non-owner GET deletion-impact → 404 (not 403, not 200).
    - Non-owner DELETE → 400 "Not enough permissions".
    - Missing credential ID for GET deletion-impact → 404.
    """
    # ── Phase 1: create credential owned by superuser ────────────────────────
    cred = create_random_credential(client, superuser_token_headers)
    cred_id = cred["id"]

    # ── Phase 2: non-owner impact GET → 404 ──────────────────────────────────
    other_user, other_headers = _make_user_and_headers(client)

    r = client.get(
        f"{API}/credentials/{cred_id}/deletion-impact",
        headers=other_headers,
    )
    assert r.status_code == 404, (
        f"Non-owner GET deletion-impact must be 404; got {r.status_code}: {r.text}"
    )

    # ── Phase 3: non-owner DELETE → 400 ──────────────────────────────────────
    r = client.delete(
        f"{API}/credentials/{cred_id}",
        headers=other_headers,
    )
    assert r.status_code == 400, (
        f"Non-owner DELETE must be 400; got {r.status_code}: {r.text}"
    )
    assert "permissions" in r.json()["detail"].lower()

    # ── Phase 4: missing credential ID → 404 ─────────────────────────────────
    ghost_id = str(uuid.uuid4())
    r = client.get(
        f"{API}/credentials/{ghost_id}/deletion-impact",
        headers=superuser_token_headers,
    )
    assert r.status_code == 404, (
        f"Missing credential must return 404; got {r.status_code}: {r.text}"
    )


# ── Scenario G: AI credential Tier 0 ─────────────────────────────────────────


def test_ai_credential_tier0_no_bundle_reference(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """G. AI credential with no bundle references — Tier 0.

    1. Create an AI credential.
    2. GET /ai-credentials/{id}/deletion-impact → tier=0, bundle_usages=[].
    3. DELETE (no force) → 200.
    4. Credential is gone (GET 404).
    """
    # ── Phase 1: create AI credential ────────────────────────────────────────
    ai_cred = create_random_ai_credential(client, superuser_token_headers)
    ai_cred_id = ai_cred["id"]

    # ── Phase 2: impact GET → Tier 0 ─────────────────────────────────────────
    impact = _get_ai_deletion_impact(client, superuser_token_headers, ai_cred_id)
    assert impact["tier"] == 0
    assert impact["bundle_usages"] == []

    # ── Phase 3: unforced DELETE succeeds ────────────────────────────────────
    r = client.delete(
        f"{API}/ai-credentials/{ai_cred_id}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200, r.text

    # ── Phase 4: credential is gone ───────────────────────────────────────────
    r = client.get(
        f"{API}/ai-credentials/{ai_cred_id}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 404


# ── Scenario H: AI credential Tier 2 — publisher-provided in published bundle ─


def test_ai_credential_tier2_publisher_provided_in_bundle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """H. AI credential referenced as publisher_ai_credential_conversation_id — Tier 2.

    1. Create + publish bundle; wire AI credential as publisher-provided.
    2. GET /ai-credentials/{id}/deletion-impact → tier=2, bundle_usages populated.
    3. Unforced DELETE → 409 with structured detail (tier==2 in detail).
    4. force=true DELETE → 200; credential gone.
    """
    pub_headers = superuser_token_headers

    # ── Phase 1: publish bundle, wire AI credential ───────────────────────────
    publisher_agent = create_agent_via_api(
        client, pub_headers, name="ImpH-Publisher"
    )
    drain_tasks()

    r = client.post(
        f"{API}/agents/{publisher_agent['id']}/publish",
        headers=pub_headers,
        json={},
    )
    assert r.status_code == 200, r.text
    drain_tasks()

    fresh = client.get(
        f"{API}/agents/{publisher_agent['id']}", headers=pub_headers
    ).json()
    bundle_uuid = fresh["bundle_uuid"]
    bundle_id = fresh["bundle_id"]

    # Create AI credential and wire it as publisher-provided.
    ai_cred = create_random_ai_credential(client, pub_headers)
    ai_cred_id = ai_cred["id"]

    r = client.patch(
        f"{API}/bundles/{bundle_uuid}",
        headers=pub_headers,
        json={
            "publisher_ai_credential_conversation_id": ai_cred_id,
            "is_listed": True,
            "visibility": "public",
        },
    )
    assert r.status_code == 200, r.text

    # ── Phase 2: impact GET → Tier 2 ─────────────────────────────────────────
    impact = _get_ai_deletion_impact(client, pub_headers, ai_cred_id)
    assert impact["tier"] == 2, (
        f"Expected tier=2 for publisher-wired AI cred; got {impact['tier']}"
    )
    assert len(impact["bundle_usages"]) >= 1

    usage = impact["bundle_usages"][0]
    assert usage["bundle_id"] == bundle_id
    assert usage["used_for_conversation"] is True

    # ── Phase 3: unforced DELETE → 409 ───────────────────────────────────────
    r = client.delete(
        f"{API}/ai-credentials/{ai_cred_id}",
        headers=pub_headers,
    )
    assert r.status_code == 409, (
        f"Expected 409 for Tier 2 AI cred; got {r.status_code}: {r.text}"
    )
    detail = r.json()["detail"]
    assert detail["tier"] == 2
    assert len(detail["bundle_usages"]) >= 1

    # Credential must still exist.
    r_check = client.get(
        f"{API}/ai-credentials/{ai_cred_id}",
        headers=pub_headers,
    )
    assert r_check.status_code == 200, "AI credential should survive an unforced 409"

    # ── Phase 4: force DELETE → 200; credential gone ─────────────────────────
    r = client.delete(
        f"{API}/ai-credentials/{ai_cred_id}?force=true",
        headers=pub_headers,
    )
    assert r.status_code == 200, (
        f"Expected 200 for forced AI cred delete; got {r.status_code}: {r.text}"
    )

    r_gone = client.get(
        f"{API}/ai-credentials/{ai_cred_id}",
        headers=pub_headers,
    )
    assert r_gone.status_code == 404, "AI credential should be gone after force=true"


# ── Scenario I: AI credential authorization ───────────────────────────────────


def test_ai_credential_deletion_impact_authorization(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """I. AI credential deletion-impact authorization: non-owner returns 404.

    - Non-owner GET /ai-credentials/{id}/deletion-impact → 404 (not 403).
    - Missing id → 404.
    """
    # ── Phase 1: create AI credential for superuser ───────────────────────────
    ai_cred = create_random_ai_credential(client, superuser_token_headers)
    ai_cred_id = ai_cred["id"]

    # ── Phase 2: other user's GET → 404 ──────────────────────────────────────
    _, other_headers = _make_user_and_headers(client)

    r = client.get(
        f"{API}/ai-credentials/{ai_cred_id}/deletion-impact",
        headers=other_headers,
    )
    assert r.status_code == 404, (
        f"Non-owner AI cred deletion-impact must be 404; got {r.status_code}: {r.text}"
    )

    # ── Phase 3: missing id → 404 ────────────────────────────────────────────
    ghost_id = str(uuid.uuid4())
    r = client.get(
        f"{API}/ai-credentials/{ghost_id}/deletion-impact",
        headers=superuser_token_headers,
    )
    assert r.status_code == 404, (
        f"Missing AI cred must return 404; got {r.status_code}: {r.text}"
    )


# ── Scenario J: PBT bundle membership surfaces in bundle_usages, tier stays < 2


def test_pbt_credential_bundle_usages_populated_tier_not_raised(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """J. PBT credential in a published bundle appears in bundle_usages but not bundle_pbp_usages.

    A template-only credential (allow_template_sharing=True, allow_sharing=False)
    is linked to a published bundle.  The install creates an independent placeholder
    copy, so the publisher's original credential is NOT PBP.  The UI should still
    be told that the credential belongs to a bundle — that information comes from
    bundle_usages.  This test is the core regression for "credential in a bundle
    but nothing mentioned about bundles".

    1. Create a PBT credential and link to a publisher agent; publish + make public.
    2. Foreign installer installs from catalog.
    3. GET deletion-impact for the original PBT credential:
       - tier < 2  (template installs don't block deletion)
       - bundle_pbp_usages == []  (not a PBP credential)
       - bundle_usages is non-empty and contains the bundle
       - The bundle_usages entry has provided_by == "template"
    4. Unforced DELETE succeeds (tier is not blocking).
    """
    pub_headers = superuser_token_headers

    # ── Phase 1: publish bundle with PBT credential ───────────────────────────
    publisher_agent = create_agent_via_api(
        client, pub_headers, name="ImpJ-Publisher"
    )
    drain_tasks()

    pbt_cred = _create_pbt_credential(client, pub_headers, name="imp-j-pbt-cred")
    pbt_cred_id = pbt_cred["id"]
    assert pbt_cred["allow_sharing"] is False
    assert pbt_cred["allow_template_sharing"] is True

    _link_credential(client, pub_headers, publisher_agent["id"], pbt_cred_id)

    fresh_pub = _publish(client, pub_headers, publisher_agent["id"])
    bundle_uuid = fresh_pub["bundle_uuid"]
    bundle_id = fresh_pub["bundle_id"]
    _make_public(client, pub_headers, bundle_uuid)

    # ── Phase 2: foreign installer installs ──────────────────────────────────
    installer, installer_headers = _make_user_and_headers(client, superuser_token_headers)
    _install(client, installer_headers, bundle_id)

    # ── Phase 3: assert bundle_usages is populated; tier stays below 2 ───────
    impact = _get_deletion_impact(client, pub_headers, pbt_cred_id)

    assert impact["tier"] < 2, (
        f"PBT credential must not reach Tier 2; got tier={impact['tier']}"
    )
    assert impact["bundle_pbp_usages"] == [], (
        f"PBT credential must not appear in bundle_pbp_usages; got {impact['bundle_pbp_usages']}"
    )
    assert len(impact["bundle_usages"]) >= 1, (
        "PBT credential in a published bundle must appear in bundle_usages "
        "(regression: 'credential in a bundle but nothing mentioned about bundles')"
    )

    # The bundle_usages entry must identify the correct bundle
    bundle_usages_ids = [u["bundle_id"] for u in impact["bundle_usages"]]
    assert bundle_id in bundle_usages_ids, (
        f"Expected bundle_id={bundle_id!r} in bundle_usages; got {bundle_usages_ids}"
    )

    # The provided_by must be "template" for a PBT credential
    pbt_entries = [u for u in impact["bundle_usages"] if u["bundle_id"] == bundle_id]
    assert len(pbt_entries) >= 1
    for entry in pbt_entries:
        assert entry["provided_by"] == "template", (
            f"PBT bundle usage must have provided_by='template'; got {entry['provided_by']!r}"
        )
        assert entry["bundle_uuid"] is not None

    # ── Phase 4: unforced DELETE succeeds (tier is not blocking) ─────────────
    r = client.delete(
        f"{API}/credentials/{pbt_cred_id}",
        headers=pub_headers,
    )
    assert r.status_code == 200, (
        f"Unforced DELETE of PBT credential must succeed; got {r.status_code}: {r.text}"
    )
