"""PBT (template) materialisation — service_uri copy-through tests.

When a bundle credential is ``provided_by="template"`` and the installer has
NO matching existing credential, ``InstallService._materialise_template_credential``
creates a placeholder Credential owned by the installer.  That placeholder now
carries ``service_uri`` copied from the spec, UNLESS the publisher listed
``"service_uri"`` in ``template_private_fields`` (in which case the materialised
row has ``service_uri=null``).

Scenarios:
  1. **Template copies service_uri (shared default)**: PBT spec has
     ``service_uri=S`` and ``"service_uri"`` is NOT in
     ``template_private_fields`` → materialised placeholder carries
     ``service_uri=S``.  Asserted via ``GET /credentials/{id}`` after
     discovering the placeholder id via the ``setup-credentials`` endpoint.

  2. **Private service_uri is NOT copied**: same as above but
     ``template_private_fields`` includes ``"service_uri"`` → materialised
     placeholder has ``service_uri`` null/absent.

  3. **Subsequent bundle auto-matches the owned materialised credential**:
     after scenario 1 the installer owns a placeholder with ``service_uri=S``.
     A second bundle with an identical ``service_uri=S`` spec is published.
     The install-context for that second bundle surfaces the installer's now-
     owned materialised credential as the Tier-0a suggestion.  Confirms that
     copying ``service_uri`` into the placeholder enables the per-user-scoped
     matcher to work across bundles without the installer doing extra work.

Direct DB access via the ``db`` fixture is used to look up
``AgentCredentialLink`` rows and retrieve the materialised credential id
(same established pattern as other bundle test files; there is no public
listing endpoint that exposes the install's credential links together with
their service_uri).
"""
import uuid

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import settings
from app.models.credentials.credential import Credential
from app.models.credentials.link_models import AgentCredentialLink
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import (
    get_install_context as _install_context,
    install_bundle as _install,
    link_bundle_credential_to_agent as _link_credential_to_agent,
    make_bundle_public as _make_public,
    make_user_and_headers as _make_user_and_headers,
    publish_bundle as _publish,
)

API = settings.API_V1_STR


# ── Module-level helpers ──────────────────────────────────────────────────────
# Shared bundle helpers (_make_user_and_headers, _publish, _make_public,
# _install, _install_context, _link_credential_to_agent) are imported above
# from tests.utils.bundle. The credential factories below are local because
# they need service_uri / PBT-specific shapes the shared helper does not cover.


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
    """Create an Odoo credential with optional PBT settings."""
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


def _create_api_token_credential(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str,
    allow_sharing: bool = False,
    service_uri: str | None = None,
) -> dict:
    """Create an api_token credential, optionally stamped with service_uri."""
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


def _get_credential(
    client: TestClient, headers: dict[str, str], credential_id: str
) -> dict:
    """Read a single credential (no decrypted data) via GET /credentials/{id}."""
    r = client.get(f"{API}/credentials/{credential_id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _get_setup_credentials(
    client: TestClient, headers: dict[str, str], install_id: str
) -> list[dict]:
    """List the install's user-fillable placeholder credentials."""
    r = client.get(f"{API}/agents/{install_id}/setup-credentials", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _find_materialised_credential_id(
    db: Session,
    *,
    install_id: str,
    installer_id: uuid.UUID,
) -> uuid.UUID | None:
    """Return the id of the materialised (placeholder, owned) credential for an install.

    Uses the DB fixture to look up AgentCredentialLink rows and find the one
    whose Credential is a placeholder owned by the installer — same pattern as
    other bundle tests (no public API lists the install's credential links).
    """
    db.expire_all()
    links = db.exec(
        select(AgentCredentialLink).where(
            AgentCredentialLink.agent_id == uuid.UUID(install_id)
        )
    ).all()
    for link in links:
        cred = db.get(Credential, link.credential_id)
        if cred and cred.is_placeholder and cred.owner_id == installer_id:
            return cred.id
    return None


# ── Scenario 1 — Template copies service_uri when NOT in private fields ────────


def test_pbt_materialised_placeholder_carries_service_uri_when_not_private(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """1. PBT materialisation copies service_uri to the installer's placeholder.

    When a PBT credential spec carries ``service_uri=S`` and ``"service_uri"``
    is NOT listed in ``template_private_fields``, installing the bundle
    materialises a placeholder Credential owned by the installer that carries
    ``service_uri=S``.

    Scenario:
      1. Publisher creates an Odoo PBT credential with
         ``service_uri=S``, ``template_private_fields=["login", "api_token"]``
         (``"service_uri"`` is NOT private).
      2. Publisher links the credential to an agent and publishes.
      3. A fresh installer (no pre-existing credential) does a quick install.
      4. The materialised placeholder's ``service_uri`` is verified via
         ``GET /credentials/{id}`` (the endpoint returns service_uri in
         CredentialPublic).

    Assert: the credential returned by GET /credentials/{placeholder_id}
    has ``service_uri == S``.
    """
    slot_uri = f"slot://pbt-copy-svc-uri-{uuid.uuid4().hex[:6]}"
    spec_name = f"pbt-copy-odoo-{uuid.uuid4().hex[:6]}"

    # ── Phase 1: Publisher publishes PBT bundle with service_uri on the spec ──
    pub_cred = _create_odoo_credential(
        client,
        superuser_token_headers,
        name=spec_name,
        allow_template_sharing=True,
        template_private_fields=["login", "api_token"],  # "service_uri" NOT listed → copy it
        url="https://erp.acme.com",
        database_name="acme_prod",
        service_uri=slot_uri,
    )
    # Sanity: service_uri is persisted on the publisher's credential
    assert pub_cred.get("service_uri") == slot_uri, (
        f"service_uri must be persisted on publisher credential; got {pub_cred.get('service_uri')}"
    )

    publisher_agent = create_agent_via_api(
        client, superuser_token_headers,
        name=f"PBT-SvcUri-Copy-Publisher-{uuid.uuid4().hex[:4]}",
    )
    drain_tasks()
    _link_credential_to_agent(
        client, superuser_token_headers, publisher_agent["id"], pub_cred["id"]
    )
    fresh_pub = _publish(client, superuser_token_headers, publisher_agent["id"])
    _make_public(client, superuser_token_headers, fresh_pub["bundle_uuid"])
    bundle_id = fresh_pub["bundle_id"]

    # Sanity: the emitted spec must have provided_by="template" and service_uri=S
    revision_specs = fresh_pub.get("required_credential_specs") or []
    matching_spec = next((s for s in revision_specs if s["name"] == spec_name), None)
    if matching_spec:
        assert matching_spec.get("provided_by") == "template", (
            f"Spec must be PBT; got provided_by={matching_spec.get('provided_by')}"
        )
        assert matching_spec.get("service_uri") == slot_uri, (
            f"Spec must carry service_uri=S; got {matching_spec.get('service_uri')}"
        )
        assert "service_uri" not in (matching_spec.get("template_private_fields") or []), (
            "'service_uri' must not be in template_private_fields for this scenario"
        )

    # ── Phase 2: install-context shows no suggestion (fresh installer) ─────────
    installer, installer_headers = _make_user_and_headers(client)
    installer_id = uuid.UUID(installer["id"])

    ctx = _install_context(client, installer_headers, bundle_id)
    specs = ctx["service_specs"]
    assert len(specs) == 1, f"Expected 1 spec; got {specs}"
    spec = specs[0]
    assert spec["provided_by"] == "template"
    assert spec["suggested_credential_id"] is None, (
        "Fresh installer with no pre-existing credential must have no suggestion"
    )

    # ── Phase 3: Quick install → materialises a placeholder ───────────────────
    install = _install(client, installer_headers, bundle_id)
    install_id = install["id"]

    # ── Phase 4: Find the materialised credential via DB ─────────────────────
    placeholder_id = _find_materialised_credential_id(
        db, install_id=install_id, installer_id=installer_id
    )
    assert placeholder_id is not None, (
        "Expected a placeholder Credential linked to the install after quick-install "
        "with no pre-existing matching credential"
    )

    # ── Phase 5: Verify service_uri via API ──────────────────────────────────
    cred_resp = _get_credential(client, installer_headers, str(placeholder_id))
    assert cred_resp["id"] == str(placeholder_id)
    assert cred_resp["is_placeholder"] is True
    assert cred_resp["type"] == "odoo"

    materialised_service_uri = cred_resp.get("service_uri")
    assert materialised_service_uri == slot_uri, (
        "Materialised placeholder must carry service_uri=S when 'service_uri' "
        "is NOT in template_private_fields. "
        f"Expected '{slot_uri}', got '{materialised_service_uri}'"
    )

    # Also verify via setup-credentials that the placeholder is the expected one
    setup_creds = _get_setup_credentials(client, installer_headers, install_id)
    assert any(c["id"] == str(placeholder_id) for c in setup_creds), (
        f"Placeholder {placeholder_id} must appear in setup-credentials; got {setup_creds}"
    )


# ── Scenario 2 — Private service_uri is NOT copied ────────────────────────────


def test_pbt_materialised_placeholder_service_uri_null_when_private(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """2. PBT materialisation does NOT copy service_uri when it is private.

    When a PBT credential spec carries ``service_uri=S`` AND ``"service_uri"``
    IS listed in ``template_private_fields``, installing the bundle materialises
    a placeholder Credential owned by the installer with ``service_uri=null``.

    Scenario:
      1. Publisher creates an Odoo PBT credential with
         ``service_uri=S``, ``template_private_fields=["login", "api_token",
         "service_uri"]`` (``"service_uri"`` IS private).
      2. Publisher links the credential to an agent and publishes.
      3. A fresh installer does a quick install.
      4. The materialised placeholder's ``service_uri`` is verified via
         ``GET /credentials/{id}`` — it must be null.

    Assert: the credential returned by GET /credentials/{placeholder_id}
    has ``service_uri`` null/absent.
    """
    slot_uri = f"slot://pbt-private-svc-uri-{uuid.uuid4().hex[:6]}"
    spec_name = f"pbt-private-odoo-{uuid.uuid4().hex[:6]}"

    # ── Phase 1: Publisher publishes PBT bundle with service_uri as private ────
    pub_cred = _create_odoo_credential(
        client,
        superuser_token_headers,
        name=spec_name,
        allow_template_sharing=True,
        template_private_fields=["login", "api_token", "service_uri"],  # "service_uri" IS private
        url="https://erp.acme.com",
        database_name="acme_prod",
        service_uri=slot_uri,
    )
    assert pub_cred.get("service_uri") == slot_uri, (
        f"service_uri must be persisted on publisher credential; got {pub_cred.get('service_uri')}"
    )

    publisher_agent = create_agent_via_api(
        client, superuser_token_headers,
        name=f"PBT-SvcUri-Private-Publisher-{uuid.uuid4().hex[:4]}",
    )
    drain_tasks()
    _link_credential_to_agent(
        client, superuser_token_headers, publisher_agent["id"], pub_cred["id"]
    )
    fresh_pub = _publish(client, superuser_token_headers, publisher_agent["id"])
    _make_public(client, superuser_token_headers, fresh_pub["bundle_uuid"])
    bundle_id = fresh_pub["bundle_id"]

    # Sanity: the emitted spec carries template_private_fields including "service_uri"
    revision_specs = fresh_pub.get("required_credential_specs") or []
    matching_spec = next((s for s in revision_specs if s["name"] == spec_name), None)
    if matching_spec:
        assert matching_spec.get("provided_by") == "template", (
            f"Spec must be PBT; got provided_by={matching_spec.get('provided_by')}"
        )
        private_fields = matching_spec.get("template_private_fields") or []
        assert "service_uri" in private_fields, (
            f"'service_uri' must be in template_private_fields for this scenario; "
            f"got {private_fields}"
        )

    # ── Phase 2: Fresh installer quick-installs ───────────────────────────────
    installer, installer_headers = _make_user_and_headers(client)
    installer_id = uuid.UUID(installer["id"])

    ctx = _install_context(client, installer_headers, bundle_id)
    spec = ctx["service_specs"][0]
    assert spec["provided_by"] == "template"
    assert spec["suggested_credential_id"] is None, (
        "Fresh installer with no pre-existing credential must have no suggestion"
    )

    install = _install(client, installer_headers, bundle_id)
    install_id = install["id"]

    # ── Phase 3: Find the materialised credential via DB ─────────────────────
    placeholder_id = _find_materialised_credential_id(
        db, install_id=install_id, installer_id=installer_id
    )
    assert placeholder_id is not None, (
        "Expected a placeholder Credential linked to the install after quick-install"
    )

    # ── Phase 4: Verify service_uri is null via API ───────────────────────────
    cred_resp = _get_credential(client, installer_headers, str(placeholder_id))
    assert cred_resp["id"] == str(placeholder_id)
    assert cred_resp["is_placeholder"] is True
    assert cred_resp["type"] == "odoo"

    materialised_service_uri = cred_resp.get("service_uri")
    assert materialised_service_uri is None, (
        "Materialised placeholder must have service_uri=null when 'service_uri' "
        "IS in template_private_fields (the installer must supply it). "
        f"Expected None, got '{materialised_service_uri}'"
    )


# ── Scenario 3 — Owned materialised credential auto-matches a subsequent bundle ─


def test_pbt_materialised_service_uri_enables_cross_bundle_match(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """3. Copied service_uri enables Tier-0a auto-match for a second bundle.

    After scenario 1, the installer owns a materialised placeholder with
    ``service_uri=S``.  A SECOND bundle whose PBT spec also carries
    ``service_uri=S`` is published.  The install-context for that second
    bundle should surface the installer's materialised credential as the
    suggestion via the service_uri Tier-0a (owned) matcher — without the
    installer doing anything else.

    This confirms the forward-looking benefit of copying ``service_uri``:
    if the publisher ships a follow-up bundle that reuses the same slot,
    the installer's existing (possibly partially filled) credential is
    re-used automatically.

    Setup:
      1. Publish Bundle A (PBT Odoo, service_uri=S, "service_uri" not private).
      2. Fresh installer quick-installs Bundle A → placeholder with service_uri=S.
      3. Publish Bundle B (different PBU api_token spec that also carries
         service_uri=S — effectively the same slot for a different credential type).
         Actually: publish Bundle B with another PBT Odoo spec (same service_uri=S,
         different spec name) so that the owned-tier service_uri matcher can fire.
      4. GET install-context for Bundle B → suggested_credential_id == placeholder_id.

    Note: since the materialised credential is a placeholder (is_placeholder=True)
    and the installer owns it, the service_uri matcher surfaces it just like any
    other owned credential stamped with the slot id.
    """
    slot_uri = f"slot://cross-bundle-svc-uri-{uuid.uuid4().hex[:6]}"
    spec_a_name = f"cross-bundle-a-odoo-{uuid.uuid4().hex[:6]}"
    spec_b_name = f"cross-bundle-b-odoo-{uuid.uuid4().hex[:6]}"

    # ── Phase 1: Publish Bundle A (PBT Odoo, service_uri=S) ───────────────────
    pub_cred_a = _create_odoo_credential(
        client,
        superuser_token_headers,
        name=spec_a_name,
        allow_template_sharing=True,
        template_private_fields=["login", "api_token"],  # "service_uri" NOT private
        url="https://erp.cross.example.com",
        database_name="cross_prod",
        service_uri=slot_uri,
    )
    publisher_agent_a = create_agent_via_api(
        client, superuser_token_headers,
        name=f"CrossBundle-A-Publisher-{uuid.uuid4().hex[:4]}",
    )
    drain_tasks()
    _link_credential_to_agent(
        client, superuser_token_headers, publisher_agent_a["id"], pub_cred_a["id"]
    )
    fresh_pub_a = _publish(client, superuser_token_headers, publisher_agent_a["id"])
    _make_public(client, superuser_token_headers, fresh_pub_a["bundle_uuid"])
    bundle_a_id = fresh_pub_a["bundle_id"]

    # ── Phase 2: Publish Bundle B (PBU Odoo, same service_uri=S) ──────────────
    # Bundle B uses a DIFFERENT spec name but the same service_uri and the same
    # credential type (odoo).  The Tier-0 service_uri matcher filters by type,
    # so both type AND service_uri must match for the owned placeholder from
    # Bundle A's install to be surfaced as the suggestion for Bundle B.
    pub_cred_b = _create_odoo_credential(
        client,
        superuser_token_headers,
        name=spec_b_name,
        allow_template_sharing=False,   # PBU — user-provided, not template
        url="https://erp-b.cross.example.com",  # different URL so it's a distinct agent
        database_name="cross_b_prod",
        service_uri=slot_uri,           # SAME slot URI — this is the cross-bundle link
    )
    publisher_agent_b = create_agent_via_api(
        client, superuser_token_headers,
        name=f"CrossBundle-B-Publisher-{uuid.uuid4().hex[:4]}",
    )
    drain_tasks()
    _link_credential_to_agent(
        client, superuser_token_headers, publisher_agent_b["id"], pub_cred_b["id"]
    )
    fresh_pub_b = _publish(client, superuser_token_headers, publisher_agent_b["id"])
    _make_public(client, superuser_token_headers, fresh_pub_b["bundle_uuid"])
    bundle_b_id = fresh_pub_b["bundle_id"]

    # ── Phase 3: Fresh installer quick-installs Bundle A ─────────────────────
    installer, installer_headers = _make_user_and_headers(client)
    installer_id = uuid.UUID(installer["id"])

    # No suggestion before install (no pre-existing credential)
    ctx_a_before = _install_context(client, installer_headers, bundle_a_id)
    assert ctx_a_before["service_specs"][0]["suggested_credential_id"] is None, (
        "Fresh installer must have no suggestion before installing Bundle A"
    )

    install_a = _install(client, installer_headers, bundle_a_id)
    install_a_id = install_a["id"]

    # Find the materialised placeholder from Bundle A install
    placeholder_id = _find_materialised_credential_id(
        db, install_id=install_a_id, installer_id=installer_id
    )
    assert placeholder_id is not None, (
        "Expected materialised placeholder after Bundle A install"
    )

    # Confirm it has service_uri=S via API
    cred_resp = _get_credential(client, installer_headers, str(placeholder_id))
    assert cred_resp.get("service_uri") == slot_uri, (
        f"Materialised placeholder must carry service_uri=S; "
        f"got {cred_resp.get('service_uri')}"
    )
    assert cred_resp["is_placeholder"] is True

    # ── Phase 4: GET install-context for Bundle B → Tier-0a suggests placeholder ─
    ctx_b = _install_context(client, installer_headers, bundle_b_id)
    specs_b = ctx_b["service_specs"]
    assert len(specs_b) == 1, f"Expected 1 spec in Bundle B; got {specs_b}"
    spec_b = specs_b[0]

    suggested_id = spec_b.get("suggested_credential_id")
    assert suggested_id == str(placeholder_id), (
        "After installing Bundle A, the materialised placeholder with service_uri=S "
        "must be suggested (Tier-0a owned) when Bundle B has a spec with the same "
        "service_uri=S. "
        f"Expected {placeholder_id}, got {suggested_id}. "
        "This confirms that copying service_uri into the materialised credential "
        "enables cross-bundle auto-matching."
    )


# ── Scenario 4 — Private service_uri is gated out of matching AND display ────


def test_pbt_private_service_uri_not_used_for_match_or_display(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """4. build_install_context gates private service_uri out of both matching
    and the InstallContextSpec.service_uri display field.

    When a PBT spec carries ``service_uri=S`` AND ``"service_uri"`` IS in
    ``template_private_fields``, ``catalog_service.build_install_context``
    computes ``effective_service_uri=None`` before calling
    ``find_match_for_spec``.  This means:
      (a) Tier-0a (owned-service_uri) does NOT fire — even though the
          installer already owns a credential of the matching type with
          ``service_uri=S`` — so ``suggested_credential_id`` is None.
      (b) The ``service_uri`` field on the returned ``InstallContextSpec``
          is None/absent — so the frontend badge is not shown.

    Paired positive contrast:
      A sibling bundle with the IDENTICAL spec EXCEPT ``"service_uri"`` NOT
      in ``template_private_fields`` DOES trigger Tier-0a and DOES surface
      ``service_uri=S`` on the spec.  This confirms the gating is the
      sole difference — not a matcher bug or an absent credential.

    Setup:
      1. Installer creates an Odoo credential with ``service_uri=S`` before
         any install.
      2a. Bundle PRIVATE: PBT Odoo spec, ``service_uri=S``,
          ``template_private_fields=["login", "api_token", "service_uri"]``.
      2b. Bundle PUBLIC-URI: identical PBT Odoo spec but
          ``template_private_fields=["login", "api_token"]`` only
          (``"service_uri"`` NOT private).
      3. GET install-context for Bundle PRIVATE → no suggestion, spec.service_uri=None.
      4. GET install-context for Bundle PUBLIC-URI → suggestion=installer's cred,
         spec.service_uri=S.
    """
    slot_uri = f"slot://gate-check-{uuid.uuid4().hex[:6]}"
    spec_name_private = f"gate-private-odoo-{uuid.uuid4().hex[:6]}"
    spec_name_public = f"gate-public-odoo-{uuid.uuid4().hex[:6]}"

    # ── Phase 1: Installer pre-creates an Odoo credential with service_uri=S ──
    # (Simulates a user who already has a credential stamped with this slot.)
    installer, installer_headers = _make_user_and_headers(client)
    installer_cred = _create_odoo_credential(
        client,
        installer_headers,
        name=f"installer-odoo-{uuid.uuid4().hex[:6]}",
        allow_template_sharing=False,
        url="https://erp.gate-check.example.com",
        database_name="gate_db",
        login="installer-login",
        api_token="installer-token",
        service_uri=slot_uri,
    )
    installer_cred_id = installer_cred["id"]
    assert installer_cred.get("service_uri") == slot_uri

    # ── Phase 2a: Publish Bundle PRIVATE (service_uri=S in template_private_fields) ─
    pub_cred_private = _create_odoo_credential(
        client,
        superuser_token_headers,
        name=spec_name_private,
        allow_template_sharing=True,
        template_private_fields=["login", "api_token", "service_uri"],  # "service_uri" IS private
        url="https://erp.gate-check.example.com",
        database_name="gate_db",
        service_uri=slot_uri,
    )
    agent_private = create_agent_via_api(
        client, superuser_token_headers,
        name=f"GatePrivate-Publisher-{uuid.uuid4().hex[:4]}",
    )
    drain_tasks()
    _link_credential_to_agent(
        client, superuser_token_headers, agent_private["id"], pub_cred_private["id"]
    )
    fresh_private = _publish(client, superuser_token_headers, agent_private["id"])
    _make_public(client, superuser_token_headers, fresh_private["bundle_uuid"])
    bundle_id_private = fresh_private["bundle_id"]

    # ── Phase 2b: Publish Bundle PUBLIC-URI (service_uri=S NOT in private fields) ─
    pub_cred_public = _create_odoo_credential(
        client,
        superuser_token_headers,
        name=spec_name_public,
        allow_template_sharing=True,
        template_private_fields=["login", "api_token"],  # "service_uri" NOT private
        url="https://erp.gate-check.example.com",
        database_name="gate_db",
        service_uri=slot_uri,
    )
    agent_public = create_agent_via_api(
        client, superuser_token_headers,
        name=f"GatePublic-Publisher-{uuid.uuid4().hex[:4]}",
    )
    drain_tasks()
    _link_credential_to_agent(
        client, superuser_token_headers, agent_public["id"], pub_cred_public["id"]
    )
    fresh_public = _publish(client, superuser_token_headers, agent_public["id"])
    _make_public(client, superuser_token_headers, fresh_public["bundle_uuid"])
    bundle_id_public = fresh_public["bundle_id"]

    # ── Phase 3: GET install-context for Bundle PRIVATE ───────────────────────
    ctx_private = _install_context(client, installer_headers, bundle_id_private)
    specs_private = ctx_private["service_specs"]
    assert len(specs_private) == 1, f"Expected 1 spec in private bundle; got {specs_private}"
    spec_private = specs_private[0]
    assert spec_private["provided_by"] == "template"

    # (a) No suggestion — private service_uri must NOT drive Tier-0a matching
    assert spec_private.get("suggested_credential_id") is None, (
        "Private service_uri must NOT be used for Tier-0a matching: the installer "
        f"owns credential {installer_cred_id} with service_uri=S, but because "
        "'service_uri' is in template_private_fields the effective_service_uri "
        "passed to find_match_for_spec must be None → no slot-id suggestion. "
        f"Got suggested_credential_id={spec_private.get('suggested_credential_id')}"
    )

    # (b) spec.service_uri is None — gated out of the display field too
    assert spec_private.get("service_uri") is None, (
        "Private service_uri must NOT be surfaced on InstallContextSpec.service_uri: "
        "the frontend badge must not be shown when the slot-id is private. "
        f"Got spec.service_uri={spec_private.get('service_uri')}"
    )

    # ── Phase 4: GET install-context for Bundle PUBLIC-URI (contrast) ─────────
    ctx_public = _install_context(client, installer_headers, bundle_id_public)
    specs_public = ctx_public["service_specs"]
    assert len(specs_public) == 1, f"Expected 1 spec in public-uri bundle; got {specs_public}"
    spec_public = specs_public[0]
    assert spec_public["provided_by"] == "template"

    # (contrast a) Suggestion fires — non-private service_uri drives Tier-0a
    assert spec_public.get("suggested_credential_id") == installer_cred_id, (
        "Non-private service_uri MUST be used for Tier-0a matching: the installer "
        f"owns credential {installer_cred_id} with service_uri=S and 'service_uri' "
        "is NOT in template_private_fields → effective_service_uri=S → Tier-0a fires. "
        f"Got suggested_credential_id={spec_public.get('suggested_credential_id')}"
    )

    # (contrast b) spec.service_uri is the slot id — displayed to the frontend
    assert spec_public.get("service_uri") == slot_uri, (
        "Non-private service_uri MUST be surfaced on InstallContextSpec.service_uri. "
        f"Expected '{slot_uri}', got {spec_public.get('service_uri')}"
    )
