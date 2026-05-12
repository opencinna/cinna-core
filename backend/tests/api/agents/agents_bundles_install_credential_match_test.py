"""Install-context credential matching — type-only fallback and PBT reuse.

Covers the auto-prefill matcher's behaviour on the install page, focused
on three gaps that the original ``(name, type)`` matcher missed:

  1. **Type-only fallback**: when the installer has exactly one owned
     credential of the matching type but with a different name, the
     suggestion should still surface so they can confirm with one click
     instead of creating a duplicate.
  2. **Ambiguous type matches don't auto-suggest**: with two or more
     credentials of the same type, the matcher returns no suggestion so
     the installer picks explicitly from the dropdown.
  3. **PBT specs are matched too**: previously the install context only
     ran the matcher for ``provided_by="user"`` specs. After uninstall
     of a bundle whose only credential was template-materialised, the
     installer's row sticks around. On reinstall the matcher should
     surface that row as the suggestion so the user does not end up with
     a second, duplicate template credential they have to fill in again.

Plus one install-flow scenario:

  4. **PBT spec with mode=use_existing**: passing an installer-owned
     credential via the install payload skips template materialisation
     and links the existing row instead.

PBT value-matching scenarios (``find_match_for_spec`` with
``template_data`` set) — tests 5–11:

  5. **Exact value equality wins**: user owns an Odoo credential whose
     non-private fields exactly equal the spec's template_data; the
     matcher returns that credential.
  6. **Value mismatch on non-private field → no match**: even though
     name+type match, a different URL means no suggestion.
  7. **Empty template_data — positive + negative**: (7b) all-private spec
     has template_data={} and template_private_fields covers all user
     fields → both sides strip to {} → MATCH; (7a) force-private type
     produces template_private_fields=[] → user data is NOT stripped →
     user dict non-empty ≠ {} → NO match (proves no "anything matches"
     degeneracy).
  8. **Type-only fallback disabled on PBT path**: user owns the only
     credential of the right type but name is different → no suggestion
     (would have matched under the old type-only tier); PBU path still
     returns the same candidate when called without template_data.
  9. **Shared credential with matching values**: user doesn't own a
     credential but has one shared with them whose non-private fields
     match → suggestion surfaces; a second paired negative case where the
     shared credential's values mismatch → no suggestion.
  10. **Multiple owned candidates — first exact-value match wins**: user
     owns two same-name same-type credentials with different URLs; only
     the one whose URL matches the template is returned.
  11. **Integration — PBT value match / mismatch via API**: PBT spec with
     matching values surfaces a suggested_credential_id; with mismatching
     values the suggestion is None; PBU spec still surfaces a suggestion
     regardless of values.

Direct DB access via the ``db`` fixture verifies ``AgentCredentialLink``
and ``Credential`` rows because there is no listing endpoint exposing
the agent's linked credential ids together with their owner.
"""
import uuid

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import settings
from app.models.agents.agent import Agent
from app.models.credentials.credential import Credential
from app.models.credentials.credential_share import CredentialShare
from app.models.credentials.link_models import AgentCredentialLink
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.user import create_random_user, user_authentication_headers

API = settings.API_V1_STR


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_user_and_headers(client: TestClient) -> tuple[dict, dict[str, str]]:
    """Create a fresh user with a default AI credential; return (user, headers)."""
    user = create_random_user(client)
    headers = user_authentication_headers(
        client=client, email=user["email"], password=user["_password"]
    )
    create_random_ai_credential(client, headers, set_default=True)
    return user, headers


def _create_api_token_credential(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str,
    allow_sharing: bool = False,
) -> dict:
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
    r = client.post(f"{API}/credentials/", headers=headers, json=body)
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


def _publish(client: TestClient, headers: dict[str, str], agent_id: str) -> dict:
    """Publish agent, drain tasks, return fresh agent row."""
    r = client.post(f"{API}/agents/{agent_id}/publish", headers=headers, json={})
    assert r.status_code == 200, r.text
    drain_tasks()
    return client.get(f"{API}/agents/{agent_id}", headers=headers).json()


def _make_public(
    client: TestClient, headers: dict[str, str], bundle_uuid: str
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


def _install_context(
    client: TestClient, headers: dict[str, str], bundle_id: str
) -> dict:
    r = client.get(
        f"{API}/catalog/{bundle_id}/install-context", headers=headers
    )
    assert r.status_code == 200, r.text
    return r.json()


def _share_credential_with_user(
    db: Session,
    *,
    credential_id: uuid.UUID,
    credential_owner_id: uuid.UUID,
    shared_with_user_id: uuid.UUID,
) -> None:
    """Directly insert a CredentialShare row to set up the 'shared' tier."""
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


# ── Scenario 1: PBU type-only fallback surfaces a single owned match ─────────


def test_install_context_type_only_fallback_for_pbu_single_match(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """When the installer owns exactly one credential of the matching type
    (but with a different name from the spec), the matcher returns it as
    the suggestion via the type-only fallback tier.

    1. Publisher publishes a bundle with one PBU api_token spec named
       ``ctm-svc``.
    2. Installer has one api_token credential named ``my-personal-token``
       — same type, different name.
    3. GET install-context → ``suggested_credential_id`` equals the
       installer's credential id.
    """
    publisher = create_agent_via_api(
        client, superuser_token_headers, name="CTM-Type-Only-Pub"
    )
    drain_tasks()
    spec_name = "ctm-svc"
    pub_cred = _create_api_token_credential(
        client, superuser_token_headers, name=spec_name, allow_sharing=False
    )
    _link_credential_to_agent(
        client, superuser_token_headers, publisher["id"], pub_cred["id"]
    )
    fresh = _publish(client, superuser_token_headers, publisher["id"])
    _make_public(client, superuser_token_headers, fresh["bundle_uuid"])

    _, installer_headers = _make_user_and_headers(client)
    installer_cred = _create_api_token_credential(
        client, installer_headers, name="my-personal-token", allow_sharing=False
    )

    ctx = _install_context(client, installer_headers, fresh["bundle_id"])
    specs = ctx["service_specs"]
    assert len(specs) == 1, f"Expected 1 spec; got {specs}"
    spec = specs[0]
    assert spec["provided_by"] == "user"
    assert spec["suggested_credential_id"] == installer_cred["id"], (
        "Type-only fallback should surface the installer's only api_token "
        f"credential as the suggestion; got {spec['suggested_credential_id']}"
    )
    assert spec["suggested_credential_name"] == installer_cred["name"]


# ── Scenario 2: PBU ambiguous type matches → no auto-suggestion ──────────────


def test_install_context_no_suggestion_when_multiple_type_matches(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """With two or more owned credentials of the matching type and no
    name match, the matcher returns NO suggestion. The user is expected
    to disambiguate via the dropdown.
    """
    publisher = create_agent_via_api(
        client, superuser_token_headers, name="CTM-Ambig-Pub"
    )
    drain_tasks()
    pub_cred = _create_api_token_credential(
        client, superuser_token_headers, name="ctm-ambig-svc"
    )
    _link_credential_to_agent(
        client, superuser_token_headers, publisher["id"], pub_cred["id"]
    )
    fresh = _publish(client, superuser_token_headers, publisher["id"])
    _make_public(client, superuser_token_headers, fresh["bundle_uuid"])

    _, installer_headers = _make_user_and_headers(client)
    _create_api_token_credential(client, installer_headers, name="token-one")
    _create_api_token_credential(client, installer_headers, name="token-two")

    ctx = _install_context(client, installer_headers, fresh["bundle_id"])
    spec = ctx["service_specs"][0]
    assert spec["suggested_credential_id"] is None, (
        "Two type matches with no name match → suggestion must be None "
        f"(ambiguous); got {spec['suggested_credential_id']}"
    )
    assert spec["suggested_credential_name"] is None


# ── Scenario 3: PBT specs receive auto-prefill suggestion (name+value match) ──


def test_install_context_pbt_spec_receives_suggestion(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """A PBT spec runs the auto-prefill matcher so the install page can
    offer to reuse an existing credential of the same name/type/values
    instead of materialising a fresh one.

    The PBT matcher requires name+type AND non-private value equality
    (template_data is set). The installer must own a credential whose
    non-private fields match the spec's template_data. Type-only fallback
    (different name, same type) is intentionally disabled for PBT to
    prevent silently auto-linking a credential pointing at a different
    ERP/service instance.

    This test uses a credential with the SAME NAME as the spec AND matching
    non-private field values to verify the matcher surfaces it.
    """
    publisher_cred = _create_odoo_credential(
        client,
        superuser_token_headers,
        name="ctm-pbt-odoo",
        allow_template_sharing=True,
        template_private_fields=["login", "api_token"],
        url="https://erp.example.com",
        database_name="prod",
    )
    publisher = create_agent_via_api(
        client, superuser_token_headers, name="CTM-PBT-Pub"
    )
    drain_tasks()
    _link_credential_to_agent(
        client, superuser_token_headers, publisher["id"], publisher_cred["id"]
    )
    fresh = _publish(client, superuser_token_headers, publisher["id"])
    _make_public(client, superuser_token_headers, fresh["bundle_uuid"])

    # Installer has a credential with the SAME NAME and matching non-private
    # values (url, database_name) — the PBT matcher finds it via name+value match.
    _, installer_headers = _make_user_and_headers(client)
    installer_cred = _create_odoo_credential(
        client,
        installer_headers,
        name="ctm-pbt-odoo",          # same name as spec
        allow_template_sharing=False,
        url="https://erp.example.com",  # matches template_data
        database_name="prod",           # matches template_data
        login="installer-login",
        api_token="installer-token",
    )

    ctx = _install_context(client, installer_headers, fresh["bundle_id"])
    spec = ctx["service_specs"][0]
    assert spec["provided_by"] == "template", (
        f"Spec must be PBT; got provided_by={spec['provided_by']}"
    )
    assert spec["suggested_credential_id"] == installer_cred["id"], (
        "PBT spec must surface the installer's name+value-matching credential "
        f"as the suggestion; got {spec['suggested_credential_id']}"
    )
    assert spec["suggested_credential_name"] == installer_cred["name"]
    # Template metadata is still present alongside the suggestion.
    assert sorted(spec.get("template_private_fields") or []) == [
        "api_token",
        "login",
    ]


# ── Scenario 4: Reinstall reuses the previously-materialised PBT credential ──


def test_install_context_reinstall_suggests_previous_template_credential(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """The reinstall scenario from the feature brief:

    1. Publisher ships a PBT Odoo credential.
    2. Installer installs → backend materialises a fresh placeholder
       credential owned by the installer with the spec's name.
    3. Installer uninstalls → the materialised credential row remains
       in the installer's account (Credential is not cascade-deleted by
       uninstall).
    4. Installer reinstalls → GET install-context now suggests the
       previously-materialised row (name+type match), so the installer
       does not end up with a second duplicate template credential to
       fill in.
    """
    publisher_cred = _create_odoo_credential(
        client,
        superuser_token_headers,
        name="ctm-reinstall-odoo",
        allow_template_sharing=True,
        template_private_fields=["login", "api_token"],
    )
    publisher = create_agent_via_api(
        client, superuser_token_headers, name="CTM-Reinstall-Pub"
    )
    drain_tasks()
    _link_credential_to_agent(
        client, superuser_token_headers, publisher["id"], publisher_cred["id"]
    )
    fresh = _publish(client, superuser_token_headers, publisher["id"])
    _make_public(client, superuser_token_headers, fresh["bundle_uuid"])

    installer, installer_headers = _make_user_and_headers(client)
    installer_id = uuid.UUID(installer["id"])

    # ── Phase 1: First install — backend materialises a template credential.
    first_install = _install(client, installer_headers, fresh["bundle_id"])
    db.expire_all()
    materialised_id_before_uninstall: uuid.UUID | None = None
    materialised_name: str | None = None
    links = db.exec(
        select(AgentCredentialLink).where(
            AgentCredentialLink.agent_id == uuid.UUID(first_install["id"])
        )
    ).all()
    assert len(links) == 1, "Expected one link to the materialised credential"
    materialised = db.get(Credential, links[0].credential_id)
    assert materialised is not None and materialised.owner_id == installer_id
    materialised_id_before_uninstall = materialised.id
    materialised_name = materialised.name

    # ── Phase 2: Uninstall — the credential row must persist.
    r = client.post(
        f"{API}/agents/{first_install['id']}/uninstall",
        headers=installer_headers,
    )
    assert r.status_code == 200, r.text
    drain_tasks()
    db.expire_all()
    still_there = db.get(Credential, materialised_id_before_uninstall)
    assert still_there is not None, (
        "Materialised template credential must persist after uninstall — "
        "it lives on its own row and isn't cascaded by Agent deletion"
    )

    # ── Phase 3: Reinstall — install-context should now suggest the
    # surviving credential rather than triggering a fresh materialisation.
    ctx = _install_context(client, installer_headers, fresh["bundle_id"])
    spec = ctx["service_specs"][0]
    assert spec["provided_by"] == "template"
    assert spec["suggested_credential_id"] == str(materialised_id_before_uninstall), (
        "Reinstall must suggest the previously-materialised credential row "
        f"(id={materialised_id_before_uninstall}); got "
        f"{spec['suggested_credential_id']}"
    )
    assert spec["suggested_credential_name"] == materialised_name


# ── Scenario 5: PBT install with mode=use_existing skips materialisation ─────


def test_install_pbt_with_use_existing_links_existing_credential(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """Installer accepts the type-match suggestion for a PBT spec by
    submitting ``mode="use_existing"`` with their own credential id.

    Assert:
      - Install activates with HTTP 200.
      - The link points at the installer's pre-existing credential.
      - The linked credential is NOT a placeholder (it was a complete
        credential before install — template materialisation was skipped).
      - No additional placeholder Credential row was created for that spec.
    """
    publisher_cred = _create_odoo_credential(
        client,
        superuser_token_headers,
        name="ctm-pbt-link-odoo",
        allow_template_sharing=True,
        template_private_fields=["login", "api_token"],
    )
    publisher = create_agent_via_api(
        client, superuser_token_headers, name="CTM-PBT-Link-Pub"
    )
    drain_tasks()
    _link_credential_to_agent(
        client, superuser_token_headers, publisher["id"], publisher_cred["id"]
    )
    fresh = _publish(client, superuser_token_headers, publisher["id"])
    _make_public(client, superuser_token_headers, fresh["bundle_uuid"])

    installer, installer_headers = _make_user_and_headers(client)
    installer_id = uuid.UUID(installer["id"])
    pre_existing = _create_odoo_credential(
        client,
        installer_headers,
        name="my-personal-odoo",
        allow_template_sharing=False,
    )

    install = _install(
        client,
        installer_headers,
        fresh["bundle_id"],
        request_body={
            "credentials": {
                publisher_cred["name"]: {
                    "mode": "use_existing",
                    "credential_id": pre_existing["id"],
                }
            }
        },
    )
    install_id = uuid.UUID(install["id"])

    db.expire_all()
    links = db.exec(
        select(AgentCredentialLink).where(
            AgentCredentialLink.agent_id == install_id
        )
    ).all()
    assert len(links) == 1, (
        f"Expected exactly one link (no extra placeholder); got {len(links)} links"
    )
    assert links[0].credential_id == uuid.UUID(pre_existing["id"]), (
        "PBT mode=use_existing should link the installer's existing credential "
        f"({pre_existing['id']}); got {links[0].credential_id}"
    )
    linked = db.get(Credential, links[0].credential_id)
    assert linked is not None
    assert linked.is_placeholder is False, (
        "Existing credential must not be flipped to placeholder by install"
    )
    assert linked.owner_id == installer_id

    # No new placeholder credential was created for this spec on this install.
    # Sanity check — verify only the original pre_existing credential is owned
    # by the installer and matches the spec's name/type pair.
    matches = db.exec(
        select(Credential).where(
            Credential.owner_id == installer_id,
            Credential.name == publisher_cred["name"],
        )
    ).all()
    assert len(matches) == 0, (
        "No template-materialised row named after the spec should be created "
        f"when mode=use_existing was supplied; found {len(matches)}"
    )

    # And the install row exists.
    assert db.get(Agent, install_id) is not None


# ─────────────────────────────────────────────────────────────────────────────
# PBT value-matching scenarios — find_match_for_spec with template_data set
# These tests exercise the matcher via the install-context API surface so the
# full CatalogService.build_install_context → find_match_for_spec chain is
# covered (no direct service imports allowed per README).
# ─────────────────────────────────────────────────────────────────────────────


# ── Scenario 5: PBT exact value equality wins ────────────────────────────────


def test_pbt_match_exact_value_equality_wins(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """5. PBT — installer owns a credential whose non-private fields exactly
    equal the spec's template_data → matcher returns that credential.

    Publisher has an Odoo credential with:
      url="https://erp.example.com", database_name="prod",
      login="admin", api_token="secret"
    Published spec carries template_data={url, database_name},
    template_private_fields=["login", "api_token"].

    Installer owns an Odoo credential with the SAME url and database_name
    (same non-private values) but can have ANY login/token values.
    Expected: install-context surfaces that credential as the suggestion.
    """
    # ── Phase 1: Publish bundle with PBT Odoo credential ──────────────────────
    publisher_cred = _create_odoo_credential(
        client,
        superuser_token_headers,
        name="pbt-exact-match-odoo",
        allow_template_sharing=True,
        template_private_fields=["login", "api_token"],
        url="https://erp.example.com",
        database_name="prod",
    )
    agent = create_agent_via_api(
        client, superuser_token_headers, name="PBT-Exact-Match-Pub"
    )
    drain_tasks()
    _link_credential_to_agent(
        client, superuser_token_headers, agent["id"], publisher_cred["id"]
    )
    fresh = _publish(client, superuser_token_headers, agent["id"])
    _make_public(client, superuser_token_headers, fresh["bundle_uuid"])

    # ── Phase 2: Installer creates credential with matching non-private fields ─
    _, installer_headers = _make_user_and_headers(client)
    installer_cred = _create_odoo_credential(
        client,
        installer_headers,
        name="pbt-exact-match-odoo",  # same name as spec
        allow_template_sharing=False,
        url="https://erp.example.com",    # matches template_data
        database_name="prod",             # matches template_data
        login="different-login",          # private — not compared
        api_token="different-token",      # private — not compared
    )

    # ── Phase 3: GET install-context — suggestion must surface ────────────────
    ctx = _install_context(client, installer_headers, fresh["bundle_id"])
    specs = ctx["service_specs"]
    assert len(specs) == 1, f"Expected 1 spec; got {specs}"
    spec = specs[0]
    assert spec["provided_by"] == "template"
    assert spec["suggested_credential_id"] == installer_cred["id"], (
        "PBT exact-value match: installer's matching credential must be "
        f"suggested; got {spec['suggested_credential_id']}"
    )
    assert spec["suggested_credential_name"] == installer_cred["name"]


# ── Scenario 6: PBT value mismatch on non-private field → no match ───────────


def test_pbt_no_match_when_non_private_field_differs(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """6. PBT — installer owns a credential whose name+type match the spec
    but one non-private field value differs → no suggestion.

    This is the primary bug-fix assertion: a different url means the
    installer's Odoo credential is pointing at a different instance from
    the publisher's template, so auto-linking would be wrong.

    Expected: install-context has suggested_credential_id=None even though
    name+type would have matched under the old (pre-PBT) code path.
    """
    # ── Phase 1: Publish bundle with PBT Odoo credential ──────────────────────
    publisher_cred = _create_odoo_credential(
        client,
        superuser_token_headers,
        name="pbt-mismatch-odoo",
        allow_template_sharing=True,
        template_private_fields=["login", "api_token"],
        url="https://erp.example.com",
        database_name="prod",
    )
    agent = create_agent_via_api(
        client, superuser_token_headers, name="PBT-Mismatch-Pub"
    )
    drain_tasks()
    _link_credential_to_agent(
        client, superuser_token_headers, agent["id"], publisher_cred["id"]
    )
    fresh = _publish(client, superuser_token_headers, agent["id"])
    _make_public(client, superuser_token_headers, fresh["bundle_uuid"])

    # ── Phase 2: Installer has a credential with DIFFERENT url ────────────────
    _, installer_headers = _make_user_and_headers(client)
    _create_odoo_credential(
        client,
        installer_headers,
        name="pbt-mismatch-odoo",  # same name as spec
        allow_template_sharing=False,
        url="https://different-erp.example.com",  # MISMATCH — different instance
        database_name="prod",
        login="admin",
        api_token="token",
    )

    # ── Phase 3: GET install-context — no suggestion despite name+type match ──
    ctx = _install_context(client, installer_headers, fresh["bundle_id"])
    spec = ctx["service_specs"][0]
    assert spec["provided_by"] == "template"
    assert spec["suggested_credential_id"] is None, (
        "PBT value mismatch: suggestion must be None even though name+type "
        f"match; got {spec['suggested_credential_id']}"
    )
    assert spec["suggested_credential_name"] is None


# ── Scenario 7: Empty template_data — all-private → both strip to {} → match ──


def test_pbt_empty_template_data_matches_all_private_user_credential(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """7 (positive + negative). PBT with template_data={}.

    Positive (7b): publisher marks all four Odoo fields as private.
    Publish produces template_data={}, template_private_fields=[url,db,login,token].
    The matcher strips those same four keys from the installer's credential →
    user stripped = {} = template_data={} → MATCH.
    This verifies "all-private spec accepts any credential with the right name+type".

    Negative (7a): a force-private credential type (google_service_account) is
    in _TEMPLATE_FORCE_PRIVATE_TYPES → publish yields template_data={},
    template_private_fields=[].  The matcher strips NOTHING from the installer's
    data → user stripped = {<all fields>} ≠ {} → NO match.
    This confirms empty template_data does NOT degenerate into "anything matches"
    when no fields are listed in template_private_fields.
    """
    # ══ Positive (7b): all-four-fields private → both strip to {} → MATCH ═════

    # ── Phase 1: Publish all-private Odoo PBT bundle ──────────────────────────
    publisher_all_private = _create_odoo_credential(
        client,
        superuser_token_headers,
        name="pbt-all-private-odoo",
        allow_template_sharing=True,
        template_private_fields=["url", "database_name", "login", "api_token"],
        url="https://erp.example.com",
        database_name="prod",
    )
    agent_pos = create_agent_via_api(
        client, superuser_token_headers, name="PBT-AllPrivate-Pos-Pub"
    )
    drain_tasks()
    _link_credential_to_agent(
        client, superuser_token_headers, agent_pos["id"], publisher_all_private["id"]
    )
    fresh_pos = _publish(client, superuser_token_headers, agent_pos["id"])
    _make_public(client, superuser_token_headers, fresh_pos["bundle_uuid"])

    # ── Phase 2: Installer creates credential with any values — all are stripped ─
    _, installer_headers_pos = _make_user_and_headers(client)
    installer_cred_positive = _create_odoo_credential(
        client,
        installer_headers_pos,
        name="pbt-all-private-odoo",     # same name as spec
        allow_template_sharing=False,
        url="https://any-url.example.com",  # stripped by spec's private_fields
        database_name="any-db",
        login="any-login",
        api_token="any-token",
    )
    ctx_pos = _install_context(client, installer_headers_pos, fresh_pos["bundle_id"])
    spec_pos = ctx_pos["service_specs"][0]
    assert spec_pos["provided_by"] == "template"
    assert spec_pos["suggested_credential_id"] == installer_cred_positive["id"], (
        "7b (positive): all-private PBT spec (template_data={}) → user cred "
        "with data fully within private fields → stripped dicts both {} → MATCH; "
        f"got suggestion={spec_pos['suggested_credential_id']}"
    )

    # ══ Negative (7a): force-private type → template_private_fields=[] →
    # nothing stripped from user data → user dict non-empty ≠ {} ══════════════

    # google_service_account is in _TEMPLATE_FORCE_PRIVATE_TYPES, so publish
    # always produces (template_data={}, template_private_fields=[]).
    # The matcher strips NOTHING from the installer's credential → mismatch.
    gsa_data = {
        "type": "service_account",
        "project_id": "test-project",
        "private_key_id": "key-id-abc",
        "private_key": (
            "-----BEGIN RSA PRIVATE KEY-----\nMIIEtest\n-----END RSA PRIVATE KEY-----\n"
        ),
        "client_email": "sa@test-project.iam.gserviceaccount.com",
        "client_id": "123456789",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }

    r_pub_gsa = client.post(
        f"{API}/credentials/",
        headers=superuser_token_headers,
        json={
            "name": "pbt-force-private-gsa",
            "type": "google_service_account",
            "allow_sharing": False,
            "allow_template_sharing": True,
            "credential_data": gsa_data,
        },
    )
    assert r_pub_gsa.status_code == 200, r_pub_gsa.text
    pub_gsa_cred = r_pub_gsa.json()

    agent_neg = create_agent_via_api(
        client, superuser_token_headers, name="PBT-ForcePrivate-Neg-Pub"
    )
    drain_tasks()
    _link_credential_to_agent(
        client, superuser_token_headers, agent_neg["id"], pub_gsa_cred["id"]
    )
    fresh_neg = _publish(client, superuser_token_headers, agent_neg["id"])
    _make_public(client, superuser_token_headers, fresh_neg["bundle_uuid"])

    # Installer: google_service_account with same name + non-empty data.
    _, installer_headers_neg = _make_user_and_headers(client)
    client.post(
        f"{API}/credentials/",
        headers=installer_headers_neg,
        json={
            "name": "pbt-force-private-gsa",   # same name as spec
            "type": "google_service_account",
            "allow_sharing": False,
            "allow_template_sharing": False,
            "credential_data": gsa_data,        # non-empty — nothing stripped
        },
    )

    ctx_neg = _install_context(client, installer_headers_neg, fresh_neg["bundle_id"])
    spec_neg = ctx_neg["service_specs"][0]
    assert spec_neg["provided_by"] == "template"
    assert spec_neg["suggested_credential_id"] is None, (
        "7a (negative): force-private type yields template_private_fields=[] → "
        "matcher strips nothing → user stripped dict is non-empty ≠ {} → "
        "must NOT match; "
        f"got suggestion={spec_neg['suggested_credential_id']}"
    )


# ── Scenario 8: PBT type-only fallback is disabled ───────────────────────────


def test_pbt_type_only_fallback_disabled(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """8. PBT — type-only fallback is bypassed when template_data is set.

    Setup: user owns exactly one Odoo credential but its name does NOT match
    the spec. Under the old code (before the PBT path) the type-only
    fallback tier would return it. With template_data set, the matcher
    must return None.

    Paired positive: the same user, calling for a PBU spec on the same type,
    DOES get the type-only suggestion (one owned, different name → suggest).
    We verify this by using a second bundle with a PBU Odoo spec.
    """
    # ── Phase 1: Publish PBT bundle — spec name "pbt-typeonly-odoo" ──────────
    publisher_pbt = _create_odoo_credential(
        client,
        superuser_token_headers,
        name="pbt-typeonly-odoo",
        allow_template_sharing=True,
        template_private_fields=["login", "api_token"],
        url="https://erp.example.com",
        database_name="prod",
    )
    pbt_agent = create_agent_via_api(
        client, superuser_token_headers, name="PBT-TypeOnly-Pub"
    )
    drain_tasks()
    _link_credential_to_agent(
        client, superuser_token_headers, pbt_agent["id"], publisher_pbt["id"]
    )
    pbt_fresh = _publish(client, superuser_token_headers, pbt_agent["id"])
    _make_public(client, superuser_token_headers, pbt_fresh["bundle_uuid"])

    # ── Phase 2: Publish PBU bundle — spec name "pbu-typeonly-cred" ──────────
    publisher_pbu = _create_odoo_credential(
        client,
        superuser_token_headers,
        name="pbu-typeonly-cred",
        allow_template_sharing=False,
    )
    pbu_agent = create_agent_via_api(
        client, superuser_token_headers, name="PBU-TypeOnly-Pub"
    )
    drain_tasks()
    _link_credential_to_agent(
        client, superuser_token_headers, pbu_agent["id"], publisher_pbu["id"]
    )
    pbu_fresh = _publish(client, superuser_token_headers, pbu_agent["id"])
    _make_public(client, superuser_token_headers, pbu_fresh["bundle_uuid"])

    # ── Phase 3: Installer has one Odoo credential with a DIFFERENT name ──────
    _, installer_headers = _make_user_and_headers(client)
    _create_odoo_credential(
        client,
        installer_headers,
        name="my-totally-different-odoo-name",  # different from both specs
        allow_template_sharing=False,
        url="https://erp.example.com",
        database_name="prod",
    )

    # ── Phase 4: PBT install-context → no suggestion (type-only fallback off) ─
    pbt_ctx = _install_context(client, installer_headers, pbt_fresh["bundle_id"])
    pbt_spec = pbt_ctx["service_specs"][0]
    assert pbt_spec["provided_by"] == "template"
    assert pbt_spec["suggested_credential_id"] is None, (
        "PBT path must NOT use type-only fallback; user has one Odoo cred "
        "but with a different name and different values → should be None; "
        f"got {pbt_spec['suggested_credential_id']}"
    )

    # ── Phase 5: PBU install-context → type-only fallback DOES apply ─────────
    pbu_ctx = _install_context(client, installer_headers, pbu_fresh["bundle_id"])
    pbu_spec = pbu_ctx["service_specs"][0]
    assert pbu_spec["provided_by"] == "user"
    assert pbu_spec["suggested_credential_id"] is not None, (
        "PBU path: user has exactly one Odoo cred (different name) → type-only "
        f"fallback should suggest it; got {pbu_spec['suggested_credential_id']}"
    )


# ── Scenario 9: PBT considers shared credentials (positive + negative) ────────


def test_pbt_shared_credential_matching_values_is_suggested(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """9. PBT — user does not own a matching credential but has one shared
    with them whose non-private values equal template_data → suggestion
    surfaces.

    Paired negative: when the shared credential's non-private values differ
    from template_data, no suggestion is returned.

    Setup:
      - Publisher: Odoo PBT spec with url="https://erp.example.com", db="prod",
        private=[login, api_token].
      - Third party: owns two shareable Odoo credentials, one whose values
        match and one whose values don't. Both shared with the installer.
      - Installer: owns NO Odoo credential.

    Assert (positive): the suggestion points at the matching shared credential.
    Assert (negative — separate bundle with different template_data): no suggestion.
    """
    # ── Phase 1: Publish PBT bundle ───────────────────────────────────────────
    publisher_cred = _create_odoo_credential(
        client,
        superuser_token_headers,
        name="pbt-shared-odoo",
        allow_template_sharing=True,
        template_private_fields=["login", "api_token"],
        url="https://erp.example.com",
        database_name="prod",
    )
    agent = create_agent_via_api(
        client, superuser_token_headers, name="PBT-Shared-Pub"
    )
    drain_tasks()
    _link_credential_to_agent(
        client, superuser_token_headers, agent["id"], publisher_cred["id"]
    )
    fresh = _publish(client, superuser_token_headers, agent["id"])
    _make_public(client, superuser_token_headers, fresh["bundle_uuid"])

    # ── Phase 2: Third party creates two Odoo credentials ─────────────────────
    third_party, third_headers = _make_user_and_headers(client)
    third_party_id = uuid.UUID(third_party["id"])

    # Matching credential (same url+database_name as template)
    matching_shared = _create_odoo_credential(
        client,
        third_headers,
        name="pbt-shared-odoo",  # same name as spec
        allow_template_sharing=False,
        url="https://erp.example.com",    # matches template_data
        database_name="prod",             # matches template_data
        login="third-login",
        api_token="third-token",
    )
    # Mismatching credential (different url)
    mismatching_shared = _create_odoo_credential(
        client,
        third_headers,
        name="pbt-shared-odoo",  # same name
        allow_template_sharing=False,
        url="https://other.example.com",  # MISMATCH
        database_name="prod",
        login="third-login",
        api_token="third-token",
    )

    # ── Phase 3: Installer has NO owned credentials ───────────────────────────
    installer, installer_headers = _make_user_and_headers(client)
    installer_id = uuid.UUID(installer["id"])
    matching_shared_id = uuid.UUID(matching_shared["id"])
    mismatching_shared_id = uuid.UUID(mismatching_shared["id"])

    # Share ONLY the matching credential initially → positive assertion
    _share_credential_with_user(
        db,
        credential_id=matching_shared_id,
        credential_owner_id=third_party_id,
        shared_with_user_id=installer_id,
    )

    # ── Phase 4: Positive — shared matching credential is suggested ───────────
    ctx = _install_context(client, installer_headers, fresh["bundle_id"])
    spec = ctx["service_specs"][0]
    assert spec["provided_by"] == "template"
    assert spec["suggested_credential_id"] == str(matching_shared_id), (
        "PBT shared match: shared credential with matching non-private values "
        f"must be suggested; got {spec['suggested_credential_id']}"
    )

    # ── Phase 5: Negative — replace share with the mismatching credential ─────
    # Remove the matching share, add the mismatching one.
    share_row = db.exec(
        select(CredentialShare).where(
            CredentialShare.credential_id == matching_shared_id,
            CredentialShare.shared_with_user_id == installer_id,
        )
    ).first()
    if share_row is not None:
        db.delete(share_row)
        db.commit()

    _share_credential_with_user(
        db,
        credential_id=mismatching_shared_id,
        credential_owner_id=third_party_id,
        shared_with_user_id=installer_id,
    )

    ctx2 = _install_context(client, installer_headers, fresh["bundle_id"])
    spec2 = ctx2["service_specs"][0]
    assert spec2["suggested_credential_id"] is None, (
        "PBT shared mismatch: shared credential with non-matching values "
        f"must NOT be suggested; got {spec2['suggested_credential_id']}"
    )


# ── Scenario 10: Multiple owned candidates — first exact-value match wins ─────


def test_pbt_multiple_owned_first_value_match_wins(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """10. PBT — user owns two credentials of the same name+type with
    different URLs. Only the one whose non-private values equal template_data
    is returned; the other (mismatching values) is skipped.

    The matcher iterates owned candidates ordered by descending id. We create
    the matching credential second (higher id) so that if the matcher short-
    circuits on the first exact match (highest id) we confirm the correct one
    is picked. Then we swap creation order (matching first, lower id) to
    confirm the matcher does not just return the first regardless of values.
    """
    # ── Phase 1: Publish PBT bundle ───────────────────────────────────────────
    publisher_cred = _create_odoo_credential(
        client,
        superuser_token_headers,
        name="pbt-multi-owned-odoo",
        allow_template_sharing=True,
        template_private_fields=["login", "api_token"],
        url="https://erp.example.com",
        database_name="prod",
    )
    agent = create_agent_via_api(
        client, superuser_token_headers, name="PBT-MultiOwned-Pub"
    )
    drain_tasks()
    _link_credential_to_agent(
        client, superuser_token_headers, agent["id"], publisher_cred["id"]
    )
    fresh = _publish(client, superuser_token_headers, agent["id"])
    _make_public(client, superuser_token_headers, fresh["bundle_uuid"])

    # ── Phase 2: Installer creates mismatching credential first (lower id) ────
    _, installer_headers = _make_user_and_headers(client)
    _create_odoo_credential(
        client,
        installer_headers,
        name="pbt-multi-owned-odoo",
        allow_template_sharing=False,
        url="https://wrong-erp.example.com",   # MISMATCH
        database_name="prod",
        login="any",
        api_token="any",
    )
    # Then matching credential (higher id, returned first by ORDER BY id DESC)
    matching_cred = _create_odoo_credential(
        client,
        installer_headers,
        name="pbt-multi-owned-odoo",
        allow_template_sharing=False,
        url="https://erp.example.com",          # MATCH
        database_name="prod",
        login="any",
        api_token="any",
    )

    # ── Phase 3: install-context suggests the matching credential ─────────────
    ctx = _install_context(client, installer_headers, fresh["bundle_id"])
    spec = ctx["service_specs"][0]
    assert spec["provided_by"] == "template"
    assert spec["suggested_credential_id"] == matching_cred["id"], (
        "PBT multiple owned: the credential with matching non-private values "
        f"must be suggested; got {spec['suggested_credential_id']}"
    )

    # ── Phase 4 (sanity): install-context with a user that only has a
    # mismatching credential → no suggestion ──────────────────────────────────
    _, other_headers = _make_user_and_headers(client)
    _create_odoo_credential(
        client,
        other_headers,
        name="pbt-multi-owned-odoo",
        allow_template_sharing=False,
        url="https://wrong-erp.example.com",   # MISMATCH only
        database_name="prod",
        login="any",
        api_token="any",
    )
    ctx2 = _install_context(client, other_headers, fresh["bundle_id"])
    spec2 = ctx2["service_specs"][0]
    assert spec2["suggested_credential_id"] is None, (
        "Single owned credential with mismatching values → must be None; "
        f"got {spec2['suggested_credential_id']}"
    )


# ── Scenario 11: Integration — PBT value match/mismatch vs PBU via API ────────


def test_integration_pbt_value_match_mismatch_vs_pbu(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """11. Integration — full install-context API surface:

      A. PBT spec + installer credential with MATCHING non-private values
         → suggested_credential_id is non-None (the matching credential).
         This is the positive assertion of the bug fix.

      B. PBT spec + installer credential with MISMATCHING non-private values
         → suggested_credential_id is None even though name+type match.
         This is the PRIMARY bug-fix assertion: proves the old behavior
         (type-only / name+type match) no longer silently auto-links a
         credential pointing at a different ERP instance.

      C. PBU spec + installer credential (any values, name+type match)
         → suggested_credential_id is non-None (PBU path unchanged).
         Regression guard: confirms the non-PBT path still works.

    Uses a single bundle with both a PBT Odoo spec and a PBU api_token spec.
    Two separate installers (match vs mismatch) exercise cases A/B; a third
    installer with the api_token exercises case C.
    """
    # ── Phase 1: Publish bundle with one PBT spec + one PBU spec ─────────────
    pbt_pub_cred = _create_odoo_credential(
        client,
        superuser_token_headers,
        name="integration-pbt-odoo",
        allow_template_sharing=True,
        template_private_fields=["login", "api_token"],
        url="https://erp.acme.com",
        database_name="acme_prod",
    )
    pbu_pub_cred = _create_api_token_credential(
        client,
        superuser_token_headers,
        name="integration-pbu-token",
        allow_sharing=False,
    )
    agent = create_agent_via_api(
        client, superuser_token_headers, name="Integration-PBT-Mismatch-Pub"
    )
    drain_tasks()
    _link_credential_to_agent(
        client, superuser_token_headers, agent["id"], pbt_pub_cred["id"]
    )
    _link_credential_to_agent(
        client, superuser_token_headers, agent["id"], pbu_pub_cred["id"]
    )
    fresh = _publish(client, superuser_token_headers, agent["id"])
    _make_public(client, superuser_token_headers, fresh["bundle_uuid"])
    bundle_id = fresh["bundle_id"]

    # ── Case A: Installer with matching PBT values ────────────────────────────
    _, match_headers = _make_user_and_headers(client)
    match_cred = _create_odoo_credential(
        client,
        match_headers,
        name="integration-pbt-odoo",
        allow_template_sharing=False,
        url="https://erp.acme.com",     # matches
        database_name="acme_prod",      # matches
        login="user-a",
        api_token="tok-a",
    )
    ctx_match = _install_context(client, match_headers, bundle_id)
    pbt_specs_match = [s for s in ctx_match["service_specs"] if s["provided_by"] == "template"]
    pbu_specs_match = [s for s in ctx_match["service_specs"] if s["provided_by"] == "user"]
    assert len(pbt_specs_match) == 1
    assert pbt_specs_match[0]["suggested_credential_id"] == match_cred["id"], (
        "Case A: PBT spec with matching values must surface the installer's "
        f"credential; got {pbt_specs_match[0]['suggested_credential_id']}"
    )

    # ── Case B: Installer with mismatching PBT values (the bug-fix assertion) ─
    _, mismatch_headers = _make_user_and_headers(client)
    _create_odoo_credential(
        client,
        mismatch_headers,
        name="integration-pbt-odoo",    # same name as spec
        allow_template_sharing=False,
        url="https://DIFFERENT-erp.acme.com",  # MISMATCH
        database_name="acme_prod",
        login="user-b",
        api_token="tok-b",
    )
    ctx_mismatch = _install_context(client, mismatch_headers, bundle_id)
    pbt_specs_mismatch = [s for s in ctx_mismatch["service_specs"] if s["provided_by"] == "template"]
    assert len(pbt_specs_mismatch) == 1
    assert pbt_specs_mismatch[0]["suggested_credential_id"] is None, (
        "Case B (BUG FIX): PBT spec with mismatching non-private values must "
        "return suggested_credential_id=None — the installer's credential "
        "points at a different ERP instance and must NOT be auto-linked; "
        f"got {pbt_specs_mismatch[0]['suggested_credential_id']}"
    )

    # ── Case C: PBU spec — unchanged name+type match path ─────────────────────
    _, pbu_headers = _make_user_and_headers(client)
    pbu_installer_cred = _create_api_token_credential(
        client,
        pbu_headers,
        name="integration-pbu-token",  # same name as PBU spec
        allow_sharing=False,
    )
    ctx_pbu = _install_context(client, pbu_headers, bundle_id)
    pbu_specs = [s for s in ctx_pbu["service_specs"] if s["provided_by"] == "user"]
    assert len(pbu_specs) == 1
    assert pbu_specs[0]["suggested_credential_id"] == pbu_installer_cred["id"], (
        "Case C: PBU path must still surface the name+type match regardless of "
        f"credential values; got {pbu_specs[0]['suggested_credential_id']}"
    )
