"""Phase 6 tests — ``service_uri`` credential discriminator.

Test group 1: ``service_uri`` matcher precedence
-------------------------------------------------
Covers the new Tier-0 (service_uri) in ``find_match_for_spec`` via the
public ``GET /catalog/{bundle_id}/install-context`` API surface so the
full ``CatalogService.build_install_context → find_match_for_spec`` chain
is exercised (no direct service imports per README).

Scenarios:
  A. ``service_uri`` beats name: spec carries ``service_uri=S``, type=api_token;
     a shared credential with ``service_uri=S`` but a DIFFERENT name is
     suggested; a same-name credential WITHOUT ``service_uri`` is not
     preferred over it.
  B. Owned before shared: when both an owned and a shared credential carry
     ``service_uri=S``, the owned one is suggested.
  C. NULL ``service_uri`` = legacy (regression guard): spec with no
     ``service_uri`` falls through to the existing name+type tiers, returning
     the same suggestion as before the change.
  D. PBT value-anchor interaction (OQ1): a PBT spec with ``service_uri=S``
     matches the ``service_uri``-tagged shared credential even when its
     decrypted data would fail value-anchoring; when ``service_uri`` is
     absent, the existing value-anchor behavior is unchanged.
  E. Divergent-name per-user tokens auto-detect: two specs/credentials share
     a ``service_uri`` but have distinct human names — the matcher surfaces
     the suggestion (the original name-match blocker is gone).

api_token credential shares go through the public
``POST /credentials/{id}/shares`` API (``share_credential_via_api``). Direct
``CredentialShare`` inserts via the ``db`` fixture are retained only for the
Odoo scenario (D), whose credentials are ``allow_sharing=False`` and so cannot
be shared through that API (see ``_share_credential_with_user``).
"""
import uuid

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import settings
from app.models.credentials.credential_share import CredentialShare
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import (
    get_install_context as _install_context,
    link_bundle_credential_to_agent as _link_credential_to_agent,
    make_bundle_public as _make_public,
    make_user_and_headers as _make_user_and_headers,
    publish_bundle as _publish,
)
from tests.utils.credential import share_credential_via_api

API = settings.API_V1_STR


# ── Module-level helpers ──────────────────────────────────────────────────────
# Shared bundle helpers (_make_user_and_headers, _publish, _make_public,
# _install_context, _link_credential_to_agent) are imported above from
# tests.utils.bundle. The service_uri-aware credential factories stay local.


def _create_api_token_credential(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str,
    allow_sharing: bool = False,
    service_uri: str | None = None,
) -> dict:
    """Create an api_token credential, optionally stamped with ``service_uri``."""
    body: dict = {
        "name": name,
        "type": "api_token",
        "allow_sharing": allow_sharing,
        "credential_data": {
            "api_token_type": "bearer",
            "api_token_template": "Authorization: Bearer {TOKEN}",
            "api_token": f"test-token-{uuid.uuid4().hex[:8]}",
        },
    }
    if service_uri is not None:
        body["service_uri"] = service_uri
    r = client.post(f"{API}/credentials/", headers=headers, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _create_odoo_credential(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str,
    allow_template_sharing: bool = True,
    template_private_fields: list[str] | None = None,
    url: str = "https://erp.example.com",
    database_name: str = "prod_db",
    login: str = "admin",
    api_token: str = "secret-token",
    service_uri: str | None = None,
) -> dict:
    body: dict = {
        "name": name,
        "type": "odoo",
        "allow_sharing": False,
        "allow_template_sharing": allow_template_sharing,
        "credential_data": {
            "url": url,
            "database_name": database_name,
            "login": login,
            "api_token": api_token,
        },
    }
    if template_private_fields is not None:
        body["template_private_fields"] = template_private_fields
    if service_uri is not None:
        body["service_uri"] = service_uri
    r = client.post(f"{API}/credentials/", headers=headers, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _share_credential_with_user(
    db: Session,
    *,
    credential_id: uuid.UUID,
    credential_owner_id: uuid.UUID,
    shared_with_user_id: uuid.UUID,
) -> None:
    """Directly insert a CredentialShare row to set up the 'shared' tier.

    Retained only for the Odoo-credential scenario (D): those credentials are
    created with ``allow_sharing=False`` (see ``_create_odoo_credential``), which
    the public ``POST /credentials/{id}/shares`` endpoint rejects. The scenario
    needs the credential in the installer's shared set regardless of the sharing
    toggle, so the share row is inserted directly. All api_token shares in this
    file go through the public API (``share_credential_via_api``).
    """
    existing = db.exec(
        select(CredentialShare).where(
            CredentialShare.credential_id == credential_id,
            CredentialShare.shared_with_user_id == shared_with_user_id,
        )
    ).first()
    if existing is None:
        db.add(CredentialShare(
            credential_id=credential_id,
            shared_with_user_id=shared_with_user_id,
            shared_by_user_id=credential_owner_id,
            access_level="read",
        ))
        db.commit()


# ── Scenario A — service_uri beats name ──────────────────────────────────────


def test_service_uri_beats_name_for_match(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """A. ``service_uri`` tier takes precedence over the name tier.

    Setup:
      - Publisher publishes a bundle with one api_token spec stamped
        ``service_uri="slot://company-scope-token"``.  The spec name is
        ``slot-spec-token``.
      - Installer has TWO credentials:
          * ``same-name-no-uri``: name matches the spec (``slot-spec-token``)
            but has NO ``service_uri`` — the old name-tier candidate.
          * ``diff-name-with-uri``: different name (``my-personal-token``),
            but ``service_uri`` matches — the new Tier-0 candidate.
        The shared credential is ``diff-name-with-uri``.

    Expected: install-context suggests ``diff-name-with-uri`` (the
    service_uri match wins), NOT ``same-name-no-uri`` (the name match).
    """
    slot_uri = f"slot://company-scope-token-{uuid.uuid4().hex[:6]}"
    spec_name = f"slot-spec-token-{uuid.uuid4().hex[:6]}"

    # ── Phase 1: Publish bundle with service_uri-stamped spec ─────────────────
    pub_cred = _create_api_token_credential(
        client,
        superuser_token_headers,
        name=spec_name,
        allow_sharing=False,
        service_uri=slot_uri,
    )
    # Verify service_uri persisted
    assert pub_cred.get("service_uri") == slot_uri, (
        f"service_uri not persisted on create; got {pub_cred.get('service_uri')}"
    )

    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name=f"SvcUri-A-Publisher-{uuid.uuid4().hex[:4]}"
    )
    drain_tasks()
    _link_credential_to_agent(
        client, superuser_token_headers, publisher_agent["id"], pub_cred["id"]
    )
    fresh_pub = _publish(client, superuser_token_headers, publisher_agent["id"])
    _make_public(client, superuser_token_headers, fresh_pub["bundle_uuid"])
    bundle_id = fresh_pub["bundle_id"]

    # ── Phase 2: Installer creates two credentials ────────────────────────────
    sharer, sharer_headers = _make_user_and_headers(client)

    # Credential with matching service_uri but different name (shared by a third user)
    diff_name_uri_cred = _create_api_token_credential(
        client,
        sharer_headers,
        name="my-personal-token",  # DIFFERENT name from spec
        allow_sharing=True,
        service_uri=slot_uri,  # MATCHING service_uri
    )
    diff_name_uri_id = uuid.UUID(diff_name_uri_cred["id"])

    installer, installer_headers = _make_user_and_headers(client)

    # Credential with the SAME name but no service_uri (owned by installer)
    same_name_cred = _create_api_token_credential(
        client,
        installer_headers,
        name=spec_name,  # SAME name as spec
        allow_sharing=False,
        service_uri=None,  # NO service_uri
    )

    # Share the service_uri-matching credential with the installer
    share_credential_via_api(
        client, sharer_headers, diff_name_uri_cred["id"], installer["email"]
    )

    # ── Phase 3: GET install-context — service_uri tier must win ──────────────
    ctx = _install_context(client, installer_headers, bundle_id)
    specs = ctx["service_specs"]
    assert len(specs) == 1, f"Expected 1 spec; got {specs}"
    spec = specs[0]
    assert spec["provided_by"] == "user"

    suggested_id = spec["suggested_credential_id"]
    assert suggested_id == str(diff_name_uri_id), (
        f"service_uri tier must win over name tier: expected {diff_name_uri_id} "
        f"(diff-name, service_uri match), got {suggested_id} "
        f"(same_name_cred={same_name_cred['id']})"
    )


# ── Scenario B — Owned before shared when both carry service_uri ──────────────


def test_service_uri_owned_before_shared(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """B. Tier 0a (owned) beats Tier 0b (shared) when both carry service_uri=S.

    Setup:
      - Publisher publishes a bundle with a spec stamped ``service_uri=S``.
      - Installer OWNS a credential with ``service_uri=S`` (Tier 0a).
      - Installer also has a SHARED credential from a third user with the
        same ``service_uri=S`` (Tier 0b).

    Expected: install-context suggests the installer's OWNED credential.
    """
    slot_uri = f"slot://owned-beats-shared-{uuid.uuid4().hex[:6]}"
    spec_name = f"slot-obs-spec-{uuid.uuid4().hex[:6]}"

    # ── Phase 1: Publish ───────────────────────────────────────────────────────
    pub_cred = _create_api_token_credential(
        client,
        superuser_token_headers,
        name=spec_name,
        allow_sharing=False,
        service_uri=slot_uri,
    )
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name=f"SvcUri-B-Publisher-{uuid.uuid4().hex[:4]}"
    )
    drain_tasks()
    _link_credential_to_agent(
        client, superuser_token_headers, publisher_agent["id"], pub_cred["id"]
    )
    fresh_pub = _publish(client, superuser_token_headers, publisher_agent["id"])
    _make_public(client, superuser_token_headers, fresh_pub["bundle_uuid"])
    bundle_id = fresh_pub["bundle_id"]

    # ── Phase 2: Third party creates a shareable credential with service_uri ──
    third, third_headers = _make_user_and_headers(client)
    shared_cred = _create_api_token_credential(
        client,
        third_headers,
        name="third-party-token",
        allow_sharing=True,
        service_uri=slot_uri,  # same service_uri
    )
    shared_cred_id = uuid.UUID(shared_cred["id"])

    # ── Phase 3: Installer creates an OWNED credential with the same service_uri
    installer, installer_headers = _make_user_and_headers(client)
    owned_cred = _create_api_token_credential(
        client,
        installer_headers,
        name="my-owned-slot-token",  # different name — only service_uri steers matching
        allow_sharing=False,
        service_uri=slot_uri,  # same service_uri (Tier 0a)
    )
    owned_cred_id = uuid.UUID(owned_cred["id"])

    # Share the third-party credential with the installer (Tier 0b)
    share_credential_via_api(
        client, third_headers, shared_cred["id"], installer["email"]
    )

    # ── Phase 4: GET install-context — owned must win ─────────────────────────
    ctx = _install_context(client, installer_headers, bundle_id)
    spec = ctx["service_specs"][0]

    suggested_id = spec["suggested_credential_id"]
    assert suggested_id == str(owned_cred_id), (
        f"Owned credential must be preferred over shared when both carry service_uri=S. "
        f"Expected {owned_cred_id}, got {suggested_id} "
        f"(shared={shared_cred_id})"
    )


# ── Scenario C — NULL service_uri = legacy (regression guard) ─────────────────


def test_null_service_uri_falls_through_to_legacy_tiers(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """C. When ``service_uri`` is NULL on the spec, behavior is unchanged
    (regression guard for I5).

    A spec with no ``service_uri`` falls through to the existing name+type
    tier and produces the same suggestion as before the new tier was added.

    Setup:
      - Publisher publishes a bundle with a PBU spec (no service_uri).
      - Installer has exactly one api_token credential with the SAME name.

    Expected: suggestion = the installer's credential (via Tier 1, name match).
    This is identical to the pre-change behavior, confirming NULL = legacy.
    """
    spec_name = f"legacy-match-spec-{uuid.uuid4().hex[:6]}"

    # ── Phase 1: Publish bundle (no service_uri on the publisher credential) ──
    pub_cred = _create_api_token_credential(
        client,
        superuser_token_headers,
        name=spec_name,
        allow_sharing=False,
        service_uri=None,  # no service_uri → legacy path
    )
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name=f"SvcUri-C-Publisher-{uuid.uuid4().hex[:4]}"
    )
    drain_tasks()
    _link_credential_to_agent(
        client, superuser_token_headers, publisher_agent["id"], pub_cred["id"]
    )
    fresh_pub = _publish(client, superuser_token_headers, publisher_agent["id"])
    _make_public(client, superuser_token_headers, fresh_pub["bundle_uuid"])

    # Verify that the emitted spec has no service_uri (or None)
    revision_specs = fresh_pub.get("required_credential_specs") or []
    if revision_specs:
        matching_spec = next((s for s in revision_specs if s["name"] == spec_name), None)
        if matching_spec:
            assert matching_spec.get("service_uri") is None, (
                f"NULL service_uri on publisher cred must emit None in spec; "
                f"got {matching_spec.get('service_uri')}"
            )

    # ── Phase 2: Installer has a credential with the SAME name + type ─────────
    _, installer_headers = _make_user_and_headers(client)
    installer_cred = _create_api_token_credential(
        client,
        installer_headers,
        name=spec_name,  # same name → name-tier match
        allow_sharing=False,
    )

    # ── Phase 3: GET install-context — name tier still works (regression guard) ─
    ctx = _install_context(client, installer_headers, fresh_pub["bundle_id"])
    spec = ctx["service_specs"][0]
    assert spec["provided_by"] == "user"
    assert spec["suggested_credential_id"] == installer_cred["id"], (
        "NULL service_uri must fall through to name+type match (I5 regression guard). "
        f"Expected {installer_cred['id']}, got {spec['suggested_credential_id']}"
    )
    assert spec["suggested_credential_name"] == installer_cred["name"]


# ── Scenario D — PBT value-anchor interaction (OQ1) ──────────────────────────


def test_service_uri_short_circuits_pbt_value_anchor(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """D. OQ1 resolution: service_uri tier short-circuits even on the PBT path.

    When a PBT spec carries ``service_uri=S``:
      - A shared credential stamped with ``service_uri=S`` is suggested EVEN
        when its decrypted data would fail PBT value-anchoring (the url differs
        from template_data).
      - Confirms the slot-id is the authoritative signal when present.

    When a PBT spec has NO ``service_uri``:
      - The existing value-anchor behavior is unchanged (value mismatch → no
        suggestion). This is the I5 regression guard for the PBT path.

    Both sub-cases use the same installer and a single publish to make the
    scenario self-contained.
    """
    slot_uri = f"slot://oq1-pbt-test-{uuid.uuid4().hex[:6]}"
    spec_name_with_uri = f"oq1-pbt-with-uri-{uuid.uuid4().hex[:6]}"
    spec_name_no_uri = f"oq1-pbt-no-uri-{uuid.uuid4().hex[:6]}"
    target_url = "https://erp-target.example.com"
    different_url = "https://erp-DIFFERENT.example.com"

    # ── Phase 1: Publish bundle with TWO PBT specs ────────────────────────────
    #   Spec A: PBT Odoo with service_uri stamped
    #   Spec B: PBT Odoo with NO service_uri (legacy value-anchor path)
    pub_cred_a = _create_odoo_credential(
        client,
        superuser_token_headers,
        name=spec_name_with_uri,
        allow_template_sharing=True,
        template_private_fields=["login", "api_token"],
        url=target_url,
        database_name="prod_oq1",
        service_uri=slot_uri,
    )
    pub_cred_b = _create_odoo_credential(
        client,
        superuser_token_headers,
        name=spec_name_no_uri,
        allow_template_sharing=True,
        template_private_fields=["login", "api_token"],
        url=target_url,
        database_name="prod_oq1",
        service_uri=None,  # no service_uri
    )

    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name=f"SvcUri-D-Publisher-{uuid.uuid4().hex[:4]}"
    )
    drain_tasks()
    _link_credential_to_agent(
        client, superuser_token_headers, publisher_agent["id"], pub_cred_a["id"]
    )
    _link_credential_to_agent(
        client, superuser_token_headers, publisher_agent["id"], pub_cred_b["id"]
    )
    fresh_pub = _publish(client, superuser_token_headers, publisher_agent["id"])
    _make_public(client, superuser_token_headers, fresh_pub["bundle_uuid"])
    bundle_id = fresh_pub["bundle_id"]

    # ── Phase 2: Third user creates shared credentials ────────────────────────
    third, third_headers = _make_user_and_headers(client)
    third_id = uuid.UUID(third["id"])

    # Credential for Spec A: stamped with service_uri but with a DIFFERENT url
    # (value-anchor would fail if the service_uri tier did NOT short-circuit)
    mismatching_uri_cred = _create_odoo_credential(
        client,
        third_headers,
        name=f"third-slot-token-{uuid.uuid4().hex[:4]}",  # different name too
        allow_template_sharing=False,
        url=different_url,       # MISMATCH vs template_data (value-anchor would block this)
        database_name="prod_oq1",
        login="third-login",
        api_token="third-token",
        service_uri=slot_uri,    # BUT service_uri MATCHES → Tier 0 short-circuits
    )
    mismatching_uri_cred_id = uuid.UUID(mismatching_uri_cred["id"])

    # Credential for Spec B: matches name but url mismatches (value-anchor blocks)
    mismatching_value_cred = _create_odoo_credential(
        client,
        third_headers,
        name=spec_name_no_uri,   # same name as Spec B
        allow_template_sharing=False,
        url=different_url,        # MISMATCH vs template_data → no suggestion expected
        database_name="prod_oq1",
        login="third-login",
        api_token="third-token",
        service_uri=None,         # no service_uri
    )
    mismatching_value_cred_id = uuid.UUID(mismatching_value_cred["id"])

    # ── Phase 3: Installer has no owned credentials; both are shared ──────────
    installer, installer_headers = _make_user_and_headers(client)
    installer_id = uuid.UUID(installer["id"])

    _share_credential_with_user(
        db,
        credential_id=mismatching_uri_cred_id,
        credential_owner_id=third_id,
        shared_with_user_id=installer_id,
    )
    _share_credential_with_user(
        db,
        credential_id=mismatching_value_cred_id,
        credential_owner_id=third_id,
        shared_with_user_id=installer_id,
    )

    # ── Phase 4: GET install-context ──────────────────────────────────────────
    ctx = _install_context(client, installer_headers, bundle_id)
    specs_by_name = {s["name"]: s for s in ctx["service_specs"]}

    # Spec A (with service_uri): service_uri match must WIN over value-anchor check
    spec_a = specs_by_name.get(spec_name_with_uri)
    assert spec_a is not None, f"Spec A '{spec_name_with_uri}' not in install-context"
    assert spec_a["provided_by"] == "template"
    assert spec_a["suggested_credential_id"] == str(mismatching_uri_cred_id), (
        "OQ1: service_uri tier must short-circuit PBT value-anchor check. "
        f"Expected {mismatching_uri_cred_id} (service_uri match, url mismatch), "
        f"got {spec_a['suggested_credential_id']}"
    )

    # Spec B (no service_uri): value-anchor still applies — url mismatch → no suggestion
    spec_b = specs_by_name.get(spec_name_no_uri)
    assert spec_b is not None, f"Spec B '{spec_name_no_uri}' not in install-context"
    assert spec_b["provided_by"] == "template"
    assert spec_b["suggested_credential_id"] is None, (
        "I5: without service_uri, value-anchor mismatch must still block suggestion. "
        f"Got {spec_b['suggested_credential_id']}"
    )


# ── Scenario E — Divergent-name per-user tokens auto-detect ──────────────────


def test_divergent_name_per_user_tokens_auto_detect(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """E. The original blocker is gone: two specs/credentials share a service_uri
    but have distinct human names — each matcher call now surfaces the correct
    per-user token for each installer.

    This is the core motivating scenario from the plan:
      - Publisher publishes a bundle with one spec stamped ``service_uri=S``.
        The spec is named after the publisher's token (``publisher-slot-token``).
      - Installer A has a pre-shared token named ``company-a-token`` with
        ``service_uri=S`` (DIFFERENT name, same slot).
      - Installer B has a pre-shared token named ``company-b-token`` with
        ``service_uri=S`` (DIFFERENT name, same slot).

    Before this fix, name matching never matched → both installers got
    suggested_credential_id=None.  After the fix, both see a suggestion.

    Assert:
      - Installer A's install-context suggests ``company-a-token``.
      - Installer B's install-context suggests ``company-b-token``.
    """
    slot_uri = f"slot://per-user-divergent-{uuid.uuid4().hex[:6]}"
    spec_name = f"publisher-slot-token-{uuid.uuid4().hex[:6]}"

    # ── Phase 1: Publish bundle ────────────────────────────────────────────────
    pub_cred = _create_api_token_credential(
        client,
        superuser_token_headers,
        name=spec_name,
        allow_sharing=False,
        service_uri=slot_uri,
    )
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name=f"SvcUri-E-Publisher-{uuid.uuid4().hex[:4]}"
    )
    drain_tasks()
    _link_credential_to_agent(
        client, superuser_token_headers, publisher_agent["id"], pub_cred["id"]
    )
    fresh_pub = _publish(client, superuser_token_headers, publisher_agent["id"])
    _make_public(client, superuser_token_headers, fresh_pub["bundle_uuid"])
    bundle_id = fresh_pub["bundle_id"]

    # ── Phase 2: Publisher (superuser) pre-creates per-user tokens ────────────
    # Token for installer A
    token_a = _create_api_token_credential(
        client,
        superuser_token_headers,
        name="company-a-token",    # DIFFERENT name from spec
        allow_sharing=True,
        service_uri=slot_uri,      # SAME service_uri
    )
    token_a_id = uuid.UUID(token_a["id"])

    # Token for installer B
    token_b = _create_api_token_credential(
        client,
        superuser_token_headers,
        name="company-b-token",    # DIFFERENT name from spec
        allow_sharing=True,
        service_uri=slot_uri,      # SAME service_uri
    )
    token_b_id = uuid.UUID(token_b["id"])

    # ── Phase 3: Set up installers ────────────────────────────────────────────
    installer_a, installer_a_headers = _make_user_and_headers(client)
    installer_b, installer_b_headers = _make_user_and_headers(client)

    # Publisher shares each token to the correct installer
    share_credential_via_api(
        client, superuser_token_headers, token_a["id"], installer_a["email"]
    )
    share_credential_via_api(
        client, superuser_token_headers, token_b["id"], installer_b["email"]
    )

    # ── Phase 4: GET install-context for each installer ───────────────────────
    ctx_a = _install_context(client, installer_a_headers, bundle_id)
    ctx_b = _install_context(client, installer_b_headers, bundle_id)

    spec_a = ctx_a["service_specs"][0]
    spec_b = ctx_b["service_specs"][0]

    # Installer A → token A
    assert spec_a["suggested_credential_id"] == str(token_a_id), (
        "Divergent-name auto-detect: installer A must get token A suggestion. "
        f"Expected {token_a_id}, got {spec_a['suggested_credential_id']}"
    )

    # Installer B → token B
    assert spec_b["suggested_credential_id"] == str(token_b_id), (
        "Divergent-name auto-detect: installer B must get token B suggestion. "
        f"Expected {token_b_id}, got {spec_b['suggested_credential_id']}"
    )
