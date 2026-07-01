"""Bundle Permissions Management — ``GET /agents/{agent_id}/bundle-permissions-overview``.

This is the single new route the feature adds (everything else reuses existing,
already-authorized endpoints — see ``docs/plans/bundle_permissions_management_plan.md``).
It aggregates two independently owner-gated systems into one read-only response
for the publisher's "Permissions management" card:

  - Bundle catalog access (``BundleAccessGrant``), active when
    ``bundle.visibility == "users"``.
  - Producer Agent REST API per-user capability scopes
    (``agent_api_access_grant``), one entry per identity-enabled connected
    producer.

The security model IS the feature, so most scenarios here are adversarial:

  1. Bundle grants only, no connected producers.
  2. A producer the publisher OWNS → ``can_manage=True``, populated
     ``grants`` + ``scope_catalog``; a connected but identity-DISABLED
     producer must not surface at all; bundle access column is correctly
     omitted when ``visibility != "users"``.
  3. CRITICAL — a producer the publisher does NOT own → ``can_manage=False``,
     ``grants=[]``, ``scope_catalog=[]`` even though a real grant exists for
     another user on that producer (the owner-gated read never runs), and the
     existing ``POST/PUT/DELETE /agents/{producerId}/agent-api/grants`` owner
     gate still 404s the publisher (no new write surface, no regression).
  4. CRITICAL — authorization guards on the aggregator route itself: a plain
     non-developer user gets 403 (the standard role gate — not a leak, since
     it reveals nothing about this specific agent); a non-owner *developer*
     gets 404 (leak-safe, mirroring ``get_bundle_credential_drift``); a
     non-existent agent id gets 404; a foreign (non-publisher) install gets
     404 even for its own developer-owner.

Every scenario also asserts the raw ``agent_api`` credential token never
appears in the response body (the credential is decrypted server-side only to
read ``producer_agent_id`` — see plan §3 "Security considerations").

API-only: tokens are minted via the connect helper and read back from the
created credential's decrypted data, exactly as in
``agents_agent_api_grants_test.py``. ``db`` is used only for the documented
DB-seam helper that seeds a producer's parsed ``agent_api_policy_cache`` (no
API seam exists for it — the real environment sync is stubbed in this suite),
mirroring the identical helper in ``agents_agent_api_grants_test.py`` and
``agents_agent_api_test.py``.
"""
import uuid

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import settings
from app.models import AgentEnvironment
from tests.utils.agent import create_agent_via_api, update_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import make_bundle_public, make_user_and_headers, publish_bundle
from tests.utils.credential import (
    get_credential_with_data,
    link_credential_to_agent,
    set_credential_sharing,
    share_credential_via_api,
)
from tests.utils.user import (
    create_random_user,
    create_random_user_with_headers,
    promote_to_developer,
    user_authentication_headers,
)

API = settings.API_V1_STR


# ── URL + setup helpers ─────────────────────────────────────────────────────


def _overview_url(agent_id: str) -> str:
    return f"{API}/agents/{agent_id}/bundle-permissions-overview"


def _grants_url(producer_id: str) -> str:
    return f"{API}/agents/{producer_id}/agent-api/grants"


def _grant_url(producer_id: str, grant_id: str) -> str:
    return f"{_grants_url(producer_id)}/{grant_id}"


def _setup_api_agent(
    client: TestClient,
    headers: dict[str, str],
    name: str,
    identity_enabled: bool = False,
) -> dict:
    """Create an agent with agent_api_enabled=True (+ optional identity opt-in)."""
    agent = create_agent_via_api(client, headers, name=name)
    drain_tasks()
    update_agent(
        client,
        headers,
        agent["id"],
        agent_api_enabled=True,
        agent_api_identity_enabled=identity_enabled,
    )
    return agent


def _mint_token(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    label: str | None = None,
) -> dict:
    """Connect to a producer agent's REST API; return connection info + raw token."""
    body: dict = {}
    if label is not None:
        body["credential_label"] = label
    r = client.post(
        f"{API}/agents/{agent_id}/agent-api/connect", headers=headers, json=body
    )
    assert r.status_code == 200, f"Connect failed: {r.text}"
    conn = r.json()
    cred = get_credential_with_data(client, headers, conn["credential_id"])
    return {
        "credential_id": conn["credential_id"],
        "token": cred["credential_data"]["token"],
    }


def _create_grant(
    client: TestClient,
    headers: dict[str, str],
    producer_id: str,
    user_id: str,
    scopes: list[str],
) -> dict:
    r = client.post(
        _grants_url(producer_id),
        headers=headers,
        json={"user_id": user_id, "scopes": scopes},
    )
    assert r.status_code == 200, f"Create grant failed: {r.text}"
    return r.json()


def _set_policy_cache(db: Session, agent_id: str, policy: dict) -> None:
    """Force a producer env's parsed ``agent_api_policy_cache`` on the test DB.

    The policy cache is the env's parsed ``policy.yaml`` — populated by the
    real environment sync, which this suite stubs out. There is no API seam to
    set it, so this duplicates the documented DB-seam helper used by
    ``agents_agent_api_grants_test.py`` / ``agents_agent_api_test.py``.
    """
    env = db.exec(
        select(AgentEnvironment).where(
            AgentEnvironment.agent_id == uuid.UUID(agent_id)
        )
    ).first()
    assert env is not None, f"No environment for agent {agent_id}"
    env.agent_api_policy_cache = policy
    db.add(env)
    db.commit()


# ── Scenario 1: bundle grants only, no connected producers ─────────────────


def test_overview_bundle_grants_only_when_visibility_users_no_producers(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Publisher install with ``visibility == "users"`` and zero connected
    producers surfaces bundle catalog grants only:
      1. Publish an agent; flip the bundle to ``visibility="users"``.
      2. Grant a second user catalog access.
      3. Overview: ``bundle_access_applicable=True``, the one bundle grant is
         present and matches, ``producers == []``, ``show_card=True``, and the
         users union contains exactly the granted user with ``bundle_grant_id``
         set.
    """
    pub_headers = superuser_token_headers

    agent = create_agent_via_api(
        client, pub_headers, name=f"Perm-Overview-Solo-{uuid.uuid4().hex[:4]}"
    )
    drain_tasks()
    fresh = publish_bundle(client, pub_headers, agent["id"])
    bundle_uuid = fresh["bundle_uuid"]
    make_bundle_public(
        client, pub_headers, bundle_uuid, is_listed=False, visibility="users"
    )

    grantee, _ = make_user_and_headers(client)
    r = client.post(
        f"{API}/bundles/{bundle_uuid}/grants",
        headers=pub_headers,
        json={"email": grantee["email"]},
    )
    assert r.status_code == 200, r.text
    grant = r.json()

    r = client.get(_overview_url(agent["id"]), headers=pub_headers)
    assert r.status_code == 200, r.text
    overview = r.json()

    assert overview["bundle_uuid"] == bundle_uuid
    assert overview["visibility"] == "users"
    assert overview["bundle_access_applicable"] is True
    assert overview["producers"] == []
    assert overview["show_card"] is True

    assert len(overview["bundle_grants"]) == 1
    bg = overview["bundle_grants"][0]
    assert bg["id"] == grant["id"]
    assert bg["user_id"] == grantee["id"]
    assert bg["user_email"] == grantee["email"]

    assert len(overview["users"]) == 1
    user_row = overview["users"][0]
    assert user_row["user_id"] == grantee["id"]
    assert user_row["bundle_grant_id"] == grant["id"]
    assert user_row["email"] == grantee["email"]


# ── Scenario 2 + 5 + 6: owned producer (grants/catalog), identity-OFF ──────
# producer excluded, bundle access column correctly omitted off "users"  ────


def test_overview_owned_producer_grants_catalog_and_identity_disabled_excluded(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """
    Two connected producers, both owned by the publisher; visibility is
    ``"public"`` (NOT ``"users"``):
      1. Producer 1 — identity ENABLED, scope catalog seeded, one live grant.
      2. Producer 2 — identity DISABLED.
      3. Both credentials linked to the install; install published as public.
      4. Overview:
         - ``bundle_access_applicable=False`` / ``bundle_grants == []``
           (visibility isn't "users") even though producers are connected —
           the two trigger conditions are independent.
         - ``show_card=True`` anyway (driven by producers, not bundle access).
         - Only producer 1 appears (identity-disabled producer 2 is dropped).
         - Producer 1: ``can_manage=True``, populated scope_catalog + grants.
         - The raw proxy token is never present in the response body.
    """
    pub_headers = superuser_token_headers

    producer1 = _setup_api_agent(
        client,
        pub_headers,
        name=f"Perm-Producer-On-{uuid.uuid4().hex[:4]}",
        identity_enabled=True,
    )
    minted1 = _mint_token(client, pub_headers, producer1["id"], label="overview-p1")
    cred1_id = minted1["credential_id"]
    cred1_token = minted1["token"]

    producer2 = _setup_api_agent(
        client,
        pub_headers,
        name=f"Perm-Producer-Off-{uuid.uuid4().hex[:4]}",
        identity_enabled=False,
    )
    minted2 = _mint_token(client, pub_headers, producer2["id"], label="overview-p2")
    cred2_id = minted2["credential_id"]

    _set_policy_cache(
        db,
        producer1["id"],
        {
            "read_only": True,
            "auth": "required",
            "max_body_bytes": 1024 * 1024,
            "rate_limit": "60/min",
            "expose_spec": True,
            "allowed_paths": ["*"],
            "scopes": [
                {"name": "orders.read", "description": "Read orders", "requires": []}
            ],
        },
    )

    grantee, _ = make_user_and_headers(client)
    grant = _create_grant(
        client, pub_headers, producer1["id"], grantee["id"], ["orders.read"]
    )

    install_agent = create_agent_via_api(
        client, pub_headers, name=f"Perm-Install-{uuid.uuid4().hex[:4]}"
    )
    drain_tasks()
    link_credential_to_agent(client, pub_headers, install_agent["id"], cred1_id)
    link_credential_to_agent(client, pub_headers, install_agent["id"], cred2_id)

    fresh = publish_bundle(client, pub_headers, install_agent["id"])
    bundle_uuid = fresh["bundle_uuid"]
    make_bundle_public(
        client, pub_headers, bundle_uuid, is_listed=True, visibility="public"
    )

    r = client.get(_overview_url(install_agent["id"]), headers=pub_headers)
    assert r.status_code == 200, r.text
    overview = r.json()

    assert overview["visibility"] == "public"
    assert overview["bundle_access_applicable"] is False
    assert overview["bundle_grants"] == []
    assert overview["show_card"] is True

    producers = overview["producers"]
    assert len(producers) == 1, (
        f"identity-disabled producer2 must not surface: {producers}"
    )
    p = producers[0]
    assert p["producer_agent_id"] == producer1["id"]
    assert p["identity_enabled"] is True
    assert p["can_manage"] is True
    assert p["credential_id"] == cred1_id
    assert {s["name"] for s in p["scope_catalog"]} == {"orders.read"}

    assert len(p["grants"]) == 1
    g = p["grants"][0]
    assert g["user_id"] == grantee["id"]
    assert g["grant_id"] == grant["id"]
    assert g["scopes"] == ["orders.read"]

    union_ids = {u["user_id"] for u in overview["users"]}
    assert union_ids == {grantee["id"]}

    assert cred1_token not in r.text, "raw proxy token must never be serialized"


# ── Scenario 3 + 7 (CRITICAL): non-owned producer is redacted, writes still gated ──


def test_overview_non_owned_producer_redacted_and_write_routes_still_gated(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    A publisher connects to a producer they do NOT own (credential shared to
    them by the producer's real owner — the same path a PBP bundle install
    would use):
      1. Producer owner creates an identity-enabled producer, mints a token,
         enables sharing, and grants a *third* user (the "victim") real scopes
         on it.
      2. The credential is shared to the publisher, who links it to their own
         install and publishes.
      3. Overview: the producer surfaces read-only — ``can_manage=False``,
         ``owner_email`` set to the real owner, and — the critical assertion —
         ``grants == []`` / ``scope_catalog == []`` even though a real grant
         exists for the victim on that producer (the owner-gated read never
         runs for this caller).
      4. Regression guard: the publisher still cannot CREATE/READ/UPDATE/DELETE
         that producer's grants via the existing, unchanged
         ``/agents/{producerId}/agent-api/grants`` routes — all 404 (no
         existence leak), exactly as before this feature existed.
      5. The raw proxy token never appears in the overview response.
    """
    producer_owner, producer_owner_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, producer_owner["id"])

    producer = _setup_api_agent(
        client,
        producer_owner_headers,
        name=f"Perm-Foreign-Producer-{uuid.uuid4().hex[:4]}",
        identity_enabled=True,
    )
    minted = _mint_token(
        client, producer_owner_headers, producer["id"], label="foreign-producer"
    )
    cred_id = minted["credential_id"]
    cred_token = minted["token"]

    victim, _ = make_user_and_headers(client)
    real_grant = _create_grant(
        client, producer_owner_headers, producer["id"], victim["id"], ["orders.read"]
    )

    set_credential_sharing(client, producer_owner_headers, cred_id, True)
    publisher, publisher_headers = make_user_and_headers(client)
    promote_to_developer(client, superuser_token_headers, publisher["id"])
    share_credential_via_api(
        client, producer_owner_headers, cred_id, publisher["email"]
    )

    install_agent = create_agent_via_api(
        client, publisher_headers, name=f"Perm-Install-Foreign-{uuid.uuid4().hex[:4]}"
    )
    drain_tasks()
    link_credential_to_agent(client, publisher_headers, install_agent["id"], cred_id)

    fresh = publish_bundle(client, publisher_headers, install_agent["id"])
    bundle_uuid = fresh["bundle_uuid"]
    make_bundle_public(
        client, publisher_headers, bundle_uuid, is_listed=False, visibility="private"
    )

    r = client.get(_overview_url(install_agent["id"]), headers=publisher_headers)
    assert r.status_code == 200, r.text
    overview = r.json()

    assert overview["producers"], "connected identity-enabled producer must surface"
    p = next(
        p for p in overview["producers"] if p["producer_agent_id"] == producer["id"]
    )
    assert p["can_manage"] is False
    assert p["owner_email"] == producer_owner["email"]
    assert p["grants"] == [], (
        "non-manageable producer must never leak another owner's grants"
    )
    assert p["scope_catalog"] == [], (
        "non-manageable producer must never leak another owner's scope catalog"
    )
    # No user (not even the victim, who has a real grant) is pulled into the
    # union via this producer, because the owner-gated read never ran.
    assert victim["id"] not in {u["user_id"] for u in overview["users"]}

    assert cred_token not in r.text, "raw proxy token must never be serialized"

    # ── Regression guard: existing owner-gated write/read routes unchanged ──
    r_list = client.get(_grants_url(producer["id"]), headers=publisher_headers)
    assert r_list.status_code == 404, r_list.text

    r_create = client.post(
        _grants_url(producer["id"]),
        headers=publisher_headers,
        json={"user_id": victim["id"], "scopes": ["orders.write"]},
    )
    assert r_create.status_code == 404, r_create.text

    r_update = client.put(
        _grant_url(producer["id"], real_grant["id"]),
        headers=publisher_headers,
        json={"scopes": ["orders.write"]},
    )
    assert r_update.status_code == 404, r_update.text

    r_delete = client.delete(
        _grant_url(producer["id"], real_grant["id"]), headers=publisher_headers
    )
    assert r_delete.status_code == 404, r_delete.text

    # The real grant is untouched — still readable/owned by the real owner.
    r_owner_list = client.get(
        _grants_url(producer["id"]), headers=producer_owner_headers
    )
    assert r_owner_list.status_code == 200, r_owner_list.text
    assert any(g["id"] == real_grant["id"] for g in r_owner_list.json()["data"])


# ── Scenario 4 (CRITICAL): authorization guards ─────────────────────────────


def test_overview_authorization_guards(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Access control on the aggregator route itself, mirroring
    ``get_bundle_credential_drift``:
      1. Owner can access the endpoint after publish.
      2. A plain signed-up user with NO developer role gets 403 — the
         standard ``require_developer`` role gate fires before any ownership
         check; this is not a leak (it reveals nothing about this specific
         agent — every agent in the system would 403 the same way).
      3. A different user PROMOTED to developer (but not the owner) gets 404
         — leak-safe, the ownership/publisher-install guard.
      4. A non-existent agent id returns 404.
      5. A foreign (non-publisher) install returns 404 even for its own
         developer-owner — the endpoint is publisher-install-only.
    """
    owner_headers = superuser_token_headers

    agent = create_agent_via_api(
        client, owner_headers, name=f"Perm-Auth-{uuid.uuid4().hex[:4]}"
    )
    drain_tasks()
    fresh = publish_bundle(client, owner_headers, agent["id"])
    bundle_id = fresh["bundle_id"]
    bundle_uuid = fresh["bundle_uuid"]

    # ── Phase 1: owner ───────────────────────────────────────────────────
    r_owner = client.get(_overview_url(agent["id"]), headers=owner_headers)
    assert r_owner.status_code == 200, r_owner.text
    assert "show_card" in r_owner.json()

    # ── Phase 2: plain non-developer user → 403 (role gate, not a leak) ───
    plain_user = create_random_user(client)
    plain_headers = user_authentication_headers(
        client=client, email=plain_user["email"], password=plain_user["_password"]
    )
    r_plain = client.get(_overview_url(agent["id"]), headers=plain_headers)
    assert r_plain.status_code == 403, r_plain.text

    # ── Phase 3: non-owner developer → 404 (leak-safe) ─────────────────────
    other, other_headers = create_random_user_with_headers(client)
    promote_to_developer(client, owner_headers, other["id"])
    r_other = client.get(_overview_url(agent["id"]), headers=other_headers)
    assert r_other.status_code == 404, (
        f"Non-owner developer must get 404 (leak-safe, not 403); "
        f"got {r_other.status_code}: {r_other.text}"
    )

    # ── Phase 4: non-existent agent id → 404 ───────────────────────────────
    ghost_id = str(uuid.uuid4())
    r_ghost = client.get(_overview_url(ghost_id), headers=owner_headers)
    assert r_ghost.status_code == 404, r_ghost.text

    # ── Phase 5: foreign (non-publisher) install → 404 for its own owner ───
    make_bundle_public(
        client, owner_headers, bundle_uuid, is_listed=True, visibility="public"
    )
    installer, installer_headers = make_user_and_headers(client)
    promote_to_developer(client, owner_headers, installer["id"])
    r_install = client.post(
        f"{API}/catalog/{bundle_id}/install", headers=installer_headers, json={}
    )
    assert r_install.status_code == 200, r_install.text
    drain_tasks()
    install = r_install.json()
    assert install["is_publisher_install"] is False

    r_foreign = client.get(_overview_url(install["id"]), headers=installer_headers)
    assert r_foreign.status_code == 404, (
        f"Foreign install owner (developer) must get 404 (publisher-only endpoint); "
        f"got {r_foreign.status_code}: {r_foreign.text}"
    )
