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


# ── Scenario 3: PBT specs receive auto-prefill suggestion ────────────────────


def test_install_context_pbt_spec_receives_suggestion(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """A PBT spec must also run through the auto-prefill matcher so the
    install page can offer to reuse an existing credential of the same
    type instead of materialising a fresh one.
    """
    publisher_cred = _create_odoo_credential(
        client,
        superuser_token_headers,
        name="ctm-pbt-odoo",
        allow_template_sharing=True,
        template_private_fields=["login", "api_token"],
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

    # Installer already has a complete Odoo credential — different name,
    # same type. Type-only fallback should suggest it.
    _, installer_headers = _make_user_and_headers(client)
    installer_cred = _create_odoo_credential(
        client,
        installer_headers,
        name="my-existing-odoo",
        allow_template_sharing=False,
    )

    ctx = _install_context(client, installer_headers, fresh["bundle_id"])
    spec = ctx["service_specs"][0]
    assert spec["provided_by"] == "template", (
        f"Spec must be PBT; got provided_by={spec['provided_by']}"
    )
    assert spec["suggested_credential_id"] == installer_cred["id"], (
        "PBT spec must surface a matching installer credential as the "
        f"suggestion; got {spec['suggested_credential_id']}"
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
