"""Template-sharing credential mode tests.

Covers the "Share as Template" feature where a credential can be marked with
``allow_template_sharing=True`` and ``template_private_fields``, producing a
``provided_by="template"`` spec in the bundle revision on publish, and
materialising a partially-prefilled placeholder Credential for the installer.

Scenarios:

1. **Credential CRUD** — PATCH /credentials/{id} persists
   ``allow_template_sharing=True`` + ``template_private_fields``; GET
   /credentials/{id}/with-data returns them.

2. **Publish with template credential** — a revision's
   ``required_credential_specs[0]`` has ``provided_by="template"``,
   ``template_data`` contains only the non-private fields (url,
   database_name), and ``template_private_fields`` = ["login","api_token"].

3. **Publish-time validation** — setting
   ``publish_settings.credential_overrides[name].provided_by="template"``
   for a credential with ``allow_template_sharing=False`` MUST fail publish
   with HTTP 400.

4. **Install flow — happy path** — after a foreign user installs:
   - GET setup-status → ``status="needs_setup"``, missing item reason
     ``placeholder_empty``
   - GET setup-credentials → entry with ``template_prefilled_data`` containing
     ``{url, database_name}`` and ``template_private_fields`` =
     ["login","api_token"]
   - The materialised Credential is owned by the installer (not the
     publisher), ``is_placeholder=True``, ``allow_sharing=False``,
     ``allow_template_sharing=False``.

5. **Setup completion** — PUT setup-credentials/{id} with all four fields
   flips ``is_placeholder=False``; GET setup-status returns ``ready``.

6. **Setup partial** — submitting only ``login`` (still missing ``api_token``)
   keeps ``is_placeholder=True`` and gate returns ``needs_setup``.

7. **Install opt-out** — installer sends ``mode="use_existing"`` with their
   own Odoo credential for a template spec; no template materialisation occurs,
   their credential is linked instead.

8. **Override + unflag** — publisher re-publishes after flipping
   ``allow_template_sharing`` back to ``False`` but keeping the override as
   ``"template"``; publish must fail with HTTP 400.

Direct DB access (``db`` fixture) is used for reading credential + link rows
not exposed via listing endpoints, consistent with the Phase 3/4 test precedent.
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import settings
from app.models.credentials.credential import Credential
from app.models.credentials.link_models import AgentCredentialLink
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
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


def _create_odoo_credential(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str | None = None,
    allow_template_sharing: bool = False,
    template_private_fields: list[str] | None = None,
    url: str = "https://erp.example.com",
    database_name: str = "my_db",
    login: str = "admin",
    api_token: str = "secret-token",
) -> dict:
    """Create an Odoo credential via the credentials API."""
    name = name or f"odoo-{uuid.uuid4().hex[:8]}"
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


def _publish(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    *,
    expected_status: int = 200,
) -> dict:
    """Publish agent; return the parsed response body."""
    r = client.post(
        f"{API}/agents/{agent_id}/publish",
        headers=headers,
        json={},
    )
    assert r.status_code == expected_status, (
        f"Expected {expected_status}; got {r.status_code}: {r.text}"
    )
    if r.status_code == 200:
        drain_tasks()
        return r.json()
    return r.json()


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
    expected_status: int = 200,
) -> dict:
    r = client.post(
        f"{API}/catalog/{bundle_id}/install",
        headers=headers,
        json=request_body or {},
    )
    assert r.status_code == expected_status, (
        f"Expected {expected_status}; got {r.status_code}: {r.text}"
    )
    if r.status_code == 200:
        drain_tasks()
    return r.json()


def _set_publish_settings(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    overrides: dict[str, dict],
    *,
    expected_status: int = 200,
) -> dict:
    """PATCH publish-settings with the supplied credential_overrides map."""
    r = client.patch(
        f"{API}/agents/{agent_id}/publish-settings",
        headers=headers,
        json={"credential_overrides": overrides},
    )
    assert r.status_code == expected_status, (
        f"Expected {expected_status} from publish-settings; "
        f"got {r.status_code}: {r.text}"
    )
    return r.json()


def _get_setup_status(
    client: TestClient,
    headers: dict[str, str],
    install_id: str,
) -> dict:
    r = client.get(f"{API}/agents/{install_id}/setup-status", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _get_setup_credentials(
    client: TestClient,
    headers: dict[str, str],
    install_id: str,
) -> list[dict]:
    r = client.get(f"{API}/agents/{install_id}/setup-credentials", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _put_setup_credential(
    client: TestClient,
    headers: dict[str, str],
    install_id: str,
    credential_id: str,
    credential_data: dict,
    *,
    expected_status: int = 200,
) -> dict:
    r = client.put(
        f"{API}/agents/{install_id}/setup-credentials/{credential_id}",
        headers=headers,
        json={"credential_data": credential_data},
    )
    assert r.status_code == expected_status, (
        f"Expected {expected_status}; got {r.status_code}: {r.text}"
    )
    return r.json()


# ── Scenario 1: Credential CRUD persists template-sharing fields ──────────────


def test_credential_crud_allow_template_sharing_persists(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """1. PATCH /credentials/{id} with allow_template_sharing=True +
    template_private_fields persists; GET /credentials/{id}/with-data
    returns those fields.

    1. Create Odoo credential (allow_template_sharing=False, no private fields).
    2. PATCH with allow_template_sharing=True, template_private_fields=[login,api_token].
    3. GET /credentials/{id}/with-data — assert allow_template_sharing=True
       and template_private_fields=["login","api_token"] in response.
    4. Also verify the data fields are still intact.
    """
    # ── Phase 1: Create credential ─────────────────────────────────────────────
    cred = _create_odoo_credential(
        client,
        superuser_token_headers,
        name="ts-crud-odoo",
        allow_template_sharing=False,
    )
    cred_id = cred["id"]
    assert cred["allow_template_sharing"] is False
    assert cred["template_private_fields"] == []

    # ── Phase 2: PUT to enable template sharing (CredentialUpdate accepts partials) ─
    r = client.put(
        f"{API}/credentials/{cred_id}",
        headers=superuser_token_headers,
        json={
            "allow_template_sharing": True,
            "template_private_fields": ["login", "api_token"],
        },
    )
    assert r.status_code == 200, r.text
    updated = r.json()
    assert updated["allow_template_sharing"] is True, (
        f"Expected allow_template_sharing=True; got {updated['allow_template_sharing']}"
    )
    assert sorted(updated["template_private_fields"]) == ["api_token", "login"], (
        f"Expected private_fields=[api_token,login]; got {updated['template_private_fields']}"
    )

    # ── Phase 3: GET with-data returns the same fields ─────────────────────────
    r2 = client.get(f"{API}/credentials/{cred_id}/with-data", headers=superuser_token_headers)
    assert r2.status_code == 200, r2.text
    with_data = r2.json()
    assert with_data["allow_template_sharing"] is True, (
        f"with-data must reflect allow_template_sharing=True; got {with_data['allow_template_sharing']}"
    )
    assert sorted(with_data["template_private_fields"]) == ["api_token", "login"], (
        f"with-data must reflect private_fields; got {with_data['template_private_fields']}"
    )

    # ── Phase 4: credential_data is still fully intact ─────────────────────────
    data = with_data["credential_data"]
    assert data.get("url") == "https://erp.example.com"
    assert data.get("database_name") == "my_db"
    assert data.get("login") == "admin"
    assert data.get("api_token") == "secret-token"


# ── Scenario 2: Publish produces template spec ────────────────────────────────


def test_publish_template_credential_produces_correct_spec(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """2. A published revision's required_credential_specs[0] has:
       provided_by="template", template_data={url, database_name},
       template_private_fields=["login","api_token"].

    1. Create Odoo credential with allow_template_sharing=True,
       private_fields=[login,api_token], all data filled.
    2. Link credential to agent.
    3. Publish.
    4. Inspect the returned revision's required_credential_specs.
    """
    # ── Phase 1: Create template-sharing Odoo credential ──────────────────────
    cred = _create_odoo_credential(
        client,
        superuser_token_headers,
        name="ts-publish-odoo",
        allow_template_sharing=True,
        template_private_fields=["login", "api_token"],
        url="https://erp2.example.com",
        database_name="prod_db",
        login="publisher_login",
        api_token="publisher_token",
    )
    cred_id = cred["id"]

    # ── Phase 2: Create agent + link credential ────────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers, name="TS-Pub-Agent")
    drain_tasks()
    _link_credential_to_agent(client, superuser_token_headers, agent["id"], cred_id)

    # ── Phase 3: Publish ───────────────────────────────────────────────────────
    revision = _publish(client, superuser_token_headers, agent["id"])
    specs = revision.get("required_credential_specs", [])
    assert len(specs) == 1, f"Expected 1 spec; got {specs}"

    spec = specs[0]
    assert spec["name"] == "ts-publish-odoo"
    assert spec["provided_by"] == "template", (
        f"Expected provided_by='template'; got '{spec['provided_by']}'"
    )

    # ── Phase 4: Check template_data excludes private fields ──────────────────
    template_data = spec.get("template_data")
    assert isinstance(template_data, dict), (
        f"Expected template_data dict; got {template_data!r}"
    )
    assert template_data.get("url") == "https://erp2.example.com", (
        f"url should be in template_data; got {template_data}"
    )
    assert template_data.get("database_name") == "prod_db", (
        f"database_name should be in template_data; got {template_data}"
    )
    # Private fields must NOT appear in template_data.
    assert "login" not in template_data, (
        f"Private field 'login' must not appear in template_data; got {template_data}"
    )
    assert "api_token" not in template_data, (
        f"Private field 'api_token' must not appear in template_data; got {template_data}"
    )

    # ── Phase 5: Check template_private_fields ─────────────────────────────────
    template_private_fields = spec.get("template_private_fields")
    assert isinstance(template_private_fields, list), (
        f"Expected template_private_fields list; got {template_private_fields!r}"
    )
    assert sorted(template_private_fields) == ["api_token", "login"], (
        f"Expected ['api_token','login']; got {template_private_fields}"
    )

    # publisher_credential_id must be None for template specs (template ≠ share).
    assert spec.get("publisher_credential_id") is None, (
        f"Template spec must have publisher_credential_id=None; "
        f"got {spec.get('publisher_credential_id')}"
    )


# ── Scenario 3: Publish-time validation rejects template override with unflagged credential ─


def test_publish_template_override_rejects_when_allow_template_sharing_false(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """3. Setting credential_overrides[name].provided_by='template' for a
    credential with allow_template_sharing=False must fail publish with HTTP 400.

    1. Create Odoo credential with allow_template_sharing=False.
    2. Link to agent.
    3. PATCH publish-settings: credential_overrides = {name: {provided_by: "template"}}.
    4. Publish → assert HTTP 400 with a message referencing template sharing.
    """
    # ── Phase 1: Credential with template sharing disabled ────────────────────
    cred = _create_odoo_credential(
        client,
        superuser_token_headers,
        name="ts-val-odoo",
        allow_template_sharing=False,
    )
    cred_id = cred["id"]

    # ── Phase 2: Create agent + link credential ────────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers, name="TS-Val-Agent")
    drain_tasks()
    _link_credential_to_agent(client, superuser_token_headers, agent["id"], cred_id)

    # We must publish once first to become the publisher install, then set
    # overrides and re-publish. But an easier path: set the overrides first
    # (the endpoint requires is_publisher_install=True, so we need to
    # publish once before setting the override).
    revision_first = _publish(client, superuser_token_headers, agent["id"])
    fresh = client.get(f"{API}/agents/{agent['id']}", headers=superuser_token_headers).json()
    agent_id = fresh["id"]

    # ── Phase 3: PATCH publish-settings to force "template" override ──────────
    _set_publish_settings(
        client,
        superuser_token_headers,
        agent_id,
        {"ts-val-odoo": {"provided_by": "template"}},
    )

    # ── Phase 4: Re-publish must fail with 400 ────────────────────────────────
    r = client.post(
        f"{API}/agents/{agent_id}/publish",
        headers=superuser_token_headers,
        json={},
    )
    assert r.status_code == 400, (
        f"Expected 400 when template override is set but allow_template_sharing=False; "
        f"got {r.status_code}: {r.text}"
    )
    detail = str(r.json().get("detail", ""))
    assert detail, "Expected non-empty detail on 400"
    detail_lower = detail.lower()
    assert (
        "template" in detail_lower
        or "allow_template_sharing" in detail_lower
        or "ts-val-odoo" in detail_lower
    ), f"Detail should mention template sharing; got: {detail}"


# ── Scenario 4: Install flow — happy path with template credential ────────────


def test_install_template_credential_happy_path(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """4. Install of a bundle with a template credential — full install flow.

    1. Publisher creates Odoo credential (url, database_name, login, api_token);
       private=[login,api_token]; allow_template_sharing=True.
    2. Publisher links to agent, publishes, makes bundle public.
    3. Foreign user installs.
    4. Assert:
       a. GET setup-status → needs_setup, missing[0].reason=placeholder_empty.
       b. GET setup-credentials → one entry with template_prefilled_data={url,
          database_name} and template_private_fields=[login,api_token].
       c. Materialised Credential is owned by the installer (NOT publisher),
          is_placeholder=True, allow_sharing=False, allow_template_sharing=False.
    """
    # ── Phase 1: Publisher sets up credential ─────────────────────────────────
    cred = _create_odoo_credential(
        client,
        superuser_token_headers,
        name="ts-install-odoo",
        allow_template_sharing=True,
        template_private_fields=["login", "api_token"],
        url="https://erp-pub.example.com",
        database_name="pub_db",
        login="pub_login",
        api_token="pub_token",
    )
    cred_id = cred["id"]

    # ── Phase 2: Create agent, link, publish, make public ─────────────────────
    agent = create_agent_via_api(client, superuser_token_headers, name="TS-Install-Agent")
    drain_tasks()
    _link_credential_to_agent(client, superuser_token_headers, agent["id"], cred_id)
    revision = _publish(client, superuser_token_headers, agent["id"])
    fresh_agent = client.get(
        f"{API}/agents/{agent['id']}", headers=superuser_token_headers
    ).json()
    bundle_id = fresh_agent["bundle_id"]
    bundle_uuid = fresh_agent["bundle_uuid"]
    _make_public(client, superuser_token_headers, bundle_uuid)

    # ── Phase 3: Foreign user installs ────────────────────────────────────────
    installer, installer_headers = _make_user_and_headers(client)
    installer_id = uuid.UUID(installer["id"])
    install = _install(client, installer_headers, bundle_id)
    install_id = install["id"]

    # ── Phase 4a: setup-status → needs_setup / placeholder_empty ──────────────
    status_resp = _get_setup_status(client, installer_headers, install_id)
    assert status_resp["status"] == "needs_setup", (
        f"Expected needs_setup; got {status_resp['status']}"
    )
    missing = status_resp.get("missing", [])
    assert len(missing) >= 1, "Expected at least one missing item"
    reasons = [m["reason"] for m in missing]
    assert "placeholder_empty" in reasons, (
        f"Expected placeholder_empty in missing reasons; got {reasons}"
    )

    # ── Phase 4b: setup-credentials returns prefilled data ────────────────────
    creds_list = _get_setup_credentials(client, installer_headers, install_id)
    assert len(creds_list) == 1, (
        f"Expected exactly 1 setup credential entry; got {creds_list}"
    )
    entry = creds_list[0]

    prefilled = entry.get("template_prefilled_data") or {}
    assert prefilled.get("url") == "https://erp-pub.example.com", (
        f"Expected url in prefilled; got {prefilled}"
    )
    assert prefilled.get("database_name") == "pub_db", (
        f"Expected database_name in prefilled; got {prefilled}"
    )
    # Private fields must NOT be in prefilled data.
    assert "login" not in prefilled, (
        f"Private field 'login' must not appear in template_prefilled_data; got {prefilled}"
    )
    assert "api_token" not in prefilled, (
        f"Private field 'api_token' must not appear in template_prefilled_data; got {prefilled}"
    )

    private_fields = entry.get("template_private_fields") or []
    assert sorted(private_fields) == ["api_token", "login"], (
        f"Expected ['api_token','login'] in template_private_fields; got {private_fields}"
    )

    # ── Phase 4c: DB verification of materialised Credential ──────────────────
    db.expire_all()
    cred_id_installed = uuid.UUID(entry["id"])
    installed_cred = db.get(Credential, cred_id_installed)
    assert installed_cred is not None, "Materialised Credential row must exist in DB"
    assert installed_cred.owner_id == installer_id, (
        f"Credential must be owned by installer {installer_id}; "
        f"got {installed_cred.owner_id}"
    )
    assert installed_cred.is_placeholder is True, (
        "Materialised template credential must be a placeholder"
    )
    assert installed_cred.allow_sharing is False, (
        "Template credential must have allow_sharing=False"
    )
    assert installed_cred.allow_template_sharing is False, (
        "Template credential must have allow_template_sharing=False (no downstream re-sharing)"
    )
    # template_private_fields must be mirrored onto the installer's row.
    assert sorted(installed_cred.template_private_fields or []) == ["api_token", "login"], (
        f"template_private_fields mismatch on DB row; "
        f"got {installed_cred.template_private_fields}"
    )


# ── Scenario 5: Setup completion flips is_placeholder + gate → ready ─────────


def test_setup_completion_flips_placeholder_and_gate_ready(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """5. PUT setup-credentials/{id} with ALL four fields flips is_placeholder
    to False; GET setup-status returns ready.

    1. Publisher creates + publishes Odoo template credential.
    2. Foreign user installs.
    3. PUT setup-credentials with all four fields (url, database_name, login, api_token).
    4. Assert:
       - is_placeholder flipped to False in DB.
       - GET setup-status returns status="ready".
    """
    # ── Phase 1: Publish Odoo template bundle ─────────────────────────────────
    cred = _create_odoo_credential(
        client,
        superuser_token_headers,
        name="ts-complete-odoo",
        allow_template_sharing=True,
        template_private_fields=["login", "api_token"],
        url="https://erp-complete.example.com",
        database_name="complete_db",
        login="pub_login",
        api_token="pub_token",
    )
    cred_id = cred["id"]
    agent = create_agent_via_api(client, superuser_token_headers, name="TS-Complete-Agent")
    drain_tasks()
    _link_credential_to_agent(client, superuser_token_headers, agent["id"], cred_id)
    _publish(client, superuser_token_headers, agent["id"])
    fresh_agent = client.get(
        f"{API}/agents/{agent['id']}", headers=superuser_token_headers
    ).json()
    bundle_id = fresh_agent["bundle_id"]
    _make_public(client, superuser_token_headers, fresh_agent["bundle_uuid"])

    # ── Phase 2: Install ───────────────────────────────────────────────────────
    installer, installer_headers = _make_user_and_headers(client)
    install = _install(client, installer_headers, bundle_id)
    install_id = install["id"]

    # ── Phase 3: Find placeholder credential ─────────────────────────────────
    creds_list = _get_setup_credentials(client, installer_headers, install_id)
    assert len(creds_list) == 1, f"Expected 1 placeholder; got {creds_list}"
    placeholder_id = creds_list[0]["id"]

    # ── Phase 4: PUT with all four fields ─────────────────────────────────────
    put_resp = _put_setup_credential(
        client,
        installer_headers,
        install_id,
        placeholder_id,
        {
            "url": "https://erp-complete.example.com",
            "database_name": "complete_db",
            "login": "installer_login",
            "api_token": "installer_real_token",
        },
    )
    assert put_resp["id"] == placeholder_id

    # ── Phase 5: DB — is_placeholder must be False ────────────────────────────
    db.expire_all()
    updated_cred = db.get(Credential, uuid.UUID(placeholder_id))
    assert updated_cred is not None
    assert updated_cred.is_placeholder is False, (
        "Expected is_placeholder=False after filling all required fields"
    )

    # ── Phase 6: setup-status must return ready ───────────────────────────────
    status_resp = _get_setup_status(client, installer_headers, install_id)
    assert status_resp["status"] == "ready", (
        f"Expected ready after credential filled; got {status_resp['status']} "
        f"missing={status_resp.get('missing')}"
    )


# ── Scenario 6: Setup partial keeps is_placeholder / gate needs_setup ─────────


def test_setup_partial_keeps_placeholder_and_needs_setup(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """6. Submitting only ``login`` (missing ``api_token``) keeps is_placeholder=True
    and gate still returns needs_setup.

    This guards against the regression where any non-empty value was
    treated as "complete".

    1. Publish Odoo template bundle (private=[login,api_token]).
    2. Install.
    3. PUT setup-credentials with only {url, database_name, login} — missing api_token.
    4. Assert is_placeholder=True, gate=needs_setup.
    """
    # ── Phase 1: Publish ───────────────────────────────────────────────────────
    cred = _create_odoo_credential(
        client,
        superuser_token_headers,
        name="ts-partial-odoo",
        allow_template_sharing=True,
        template_private_fields=["login", "api_token"],
        url="https://erp-partial.example.com",
        database_name="partial_db",
        login="pub_login",
        api_token="pub_token",
    )
    cred_id = cred["id"]
    agent = create_agent_via_api(client, superuser_token_headers, name="TS-Partial-Agent")
    drain_tasks()
    _link_credential_to_agent(client, superuser_token_headers, agent["id"], cred_id)
    _publish(client, superuser_token_headers, agent["id"])
    fresh_agent = client.get(
        f"{API}/agents/{agent['id']}", headers=superuser_token_headers
    ).json()
    bundle_id = fresh_agent["bundle_id"]
    _make_public(client, superuser_token_headers, fresh_agent["bundle_uuid"])

    # ── Phase 2: Install ───────────────────────────────────────────────────────
    installer, installer_headers = _make_user_and_headers(client)
    install = _install(client, installer_headers, bundle_id)
    install_id = install["id"]
    creds_list = _get_setup_credentials(client, installer_headers, install_id)
    placeholder_id = creds_list[0]["id"]

    # ── Phase 3: PUT with only login — missing api_token ─────────────────────
    _put_setup_credential(
        client,
        installer_headers,
        install_id,
        placeholder_id,
        {
            "url": "https://erp-partial.example.com",
            "database_name": "partial_db",
            "login": "installer_login",
            # api_token intentionally omitted
        },
    )

    # ── Phase 4: DB — is_placeholder must still be True ──────────────────────
    db.expire_all()
    cred_row = db.get(Credential, uuid.UUID(placeholder_id))
    assert cred_row is not None
    assert cred_row.is_placeholder is True, (
        "is_placeholder must remain True when api_token is missing"
    )

    # ── Phase 5: Gate still returns needs_setup ───────────────────────────────
    status_resp = _get_setup_status(client, installer_headers, install_id)
    assert status_resp["status"] == "needs_setup", (
        f"Gate should still be needs_setup when api_token is missing; "
        f"got {status_resp['status']}"
    )
    missing_reasons = [m["reason"] for m in status_resp.get("missing", [])]
    assert "placeholder_empty" in missing_reasons, (
        f"Expected placeholder_empty in missing; got {missing_reasons}"
    )


# ── Scenario 7: Install opt-out — use_existing skips template materialisation ─


def test_install_use_existing_skips_template_materialisation(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """7. Installer sends mode='use_existing' for a template spec.

    No template credential is materialised; the installer's own Odoo
    credential is linked instead, and setup-status returns ready.

    1. Publish Odoo template bundle.
    2. Installer creates their own fully-filled Odoo credential.
    3. Install with credentials={spec_name: {mode:'use_existing', credential_id:<id>}}.
    4. Assert:
       - AgentCredentialLink points at the installer's credential.
       - The linked credential is NOT a placeholder.
       - setup-status returns ready (no materialised placeholder to fill).
    """
    # ── Phase 1: Publish template bundle ──────────────────────────────────────
    cred = _create_odoo_credential(
        client,
        superuser_token_headers,
        name="ts-optout-odoo",
        allow_template_sharing=True,
        template_private_fields=["login", "api_token"],
        url="https://erp-optout.example.com",
        database_name="optout_db",
        login="pub_login",
        api_token="pub_token",
    )
    cred_id = cred["id"]
    agent = create_agent_via_api(client, superuser_token_headers, name="TS-Optout-Agent")
    drain_tasks()
    _link_credential_to_agent(client, superuser_token_headers, agent["id"], cred_id)
    _publish(client, superuser_token_headers, agent["id"])
    fresh_agent = client.get(
        f"{API}/agents/{agent['id']}", headers=superuser_token_headers
    ).json()
    bundle_id = fresh_agent["bundle_id"]
    _make_public(client, superuser_token_headers, fresh_agent["bundle_uuid"])

    # ── Phase 2: Installer creates their own fully-filled Odoo credential ──────
    installer, installer_headers = _make_user_and_headers(client)
    installer_id = uuid.UUID(installer["id"])

    own_cred = _create_odoo_credential(
        client,
        installer_headers,
        name="ts-optout-odoo",  # same spec name
        allow_template_sharing=False,
        url="https://erp-optout.example.com",
        database_name="optout_db",
        login="my_own_login",
        api_token="my_own_token",
    )
    own_cred_id = uuid.UUID(own_cred["id"])

    # ── Phase 3: Install with use_existing ────────────────────────────────────
    install = _install(
        client,
        installer_headers,
        bundle_id,
        request_body={
            "credentials": {
                "ts-optout-odoo": {
                    "mode": "use_existing",
                    "credential_id": own_cred["id"],
                }
            }
        },
    )
    install_id = uuid.UUID(install["id"])

    # ── Phase 4: Link points at the installer's credential ────────────────────
    db.expire_all()
    links = db.exec(
        select(AgentCredentialLink).where(
            AgentCredentialLink.agent_id == install_id
        )
    ).all()
    assert len(links) == 1, f"Expected 1 link; got {len(links)}"
    linked_cred_id = links[0].credential_id
    assert linked_cred_id == own_cred_id, (
        f"Link must point at installer's own credential {own_cred_id}; "
        f"got {linked_cred_id}"
    )

    linked_cred = db.get(Credential, linked_cred_id)
    assert linked_cred is not None
    assert linked_cred.is_placeholder is False, (
        "Linked credential must NOT be a placeholder when use_existing was provided"
    )
    assert linked_cred.owner_id == installer_id, (
        "Linked credential must be owned by the installer"
    )

    # ── Phase 5: No template placeholder must have been created ───────────────
    # No placeholder Credentials should be owned by the installer AND linked
    # to this install.
    all_linked_creds = [db.get(Credential, lnk.credential_id) for lnk in links]
    placeholder_creds = [c for c in all_linked_creds if c and c.is_placeholder]
    assert len(placeholder_creds) == 0, (
        f"No placeholder should exist when installer opted out with use_existing; "
        f"got {[c.id for c in placeholder_creds]}"
    )

    # ── Phase 6: setup-status must be ready ───────────────────────────────────
    status_resp = _get_setup_status(client, installer_headers, str(install_id))
    assert status_resp["status"] == "ready", (
        f"Gate should be ready when own full credential was linked; "
        f"got {status_resp['status']} missing={status_resp.get('missing')}"
    )


# ── Scenario 8: Re-publish fails when override still says template but flag is gone ─


def test_republish_fails_when_template_flag_removed_but_override_kept(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """8. Publisher flips allow_template_sharing=False AFTER a successful publish
    but keeps credential_overrides[name].provided_by="template". Re-publish must
    fail with HTTP 400 because the override references a credential that no
    longer consents to template sharing.

    1. Create Odoo credential with allow_template_sharing=True.
    2. Link, publish (first publish), make public.
    3. Set publish-settings override = {name: {provided_by: "template"}}.
    4. PATCH the credential to allow_template_sharing=False (revoke consent).
    5. Re-publish → expect HTTP 400 mentioning template.
    """
    # ── Phase 1: Create and publish with template credential ──────────────────
    cred = _create_odoo_credential(
        client,
        superuser_token_headers,
        name="ts-unflag-odoo",
        allow_template_sharing=True,
        template_private_fields=["login", "api_token"],
        url="https://erp-unflag.example.com",
        database_name="unflag_db",
        login="pub_login",
        api_token="pub_token",
    )
    cred_id = cred["id"]
    agent = create_agent_via_api(client, superuser_token_headers, name="TS-Unflag-Agent")
    drain_tasks()
    _link_credential_to_agent(client, superuser_token_headers, agent["id"], cred_id)
    _publish(client, superuser_token_headers, agent["id"])
    fresh_agent = client.get(
        f"{API}/agents/{agent['id']}", headers=superuser_token_headers
    ).json()
    agent_id = fresh_agent["id"]
    _make_public(client, superuser_token_headers, fresh_agent["bundle_uuid"])

    # ── Phase 2: Set explicit override = "template" ────────────────────────────
    _set_publish_settings(
        client,
        superuser_token_headers,
        agent_id,
        {"ts-unflag-odoo": {"provided_by": "template"}},
    )

    # ── Phase 3: Revoke template consent on the credential ────────────────────
    r = client.put(
        f"{API}/credentials/{cred_id}",
        headers=superuser_token_headers,
        json={"allow_template_sharing": False},
    )
    assert r.status_code == 200, r.text
    assert r.json()["allow_template_sharing"] is False

    # ── Phase 4: Re-publish must fail with 400 ────────────────────────────────
    r2 = client.post(
        f"{API}/agents/{agent_id}/publish",
        headers=superuser_token_headers,
        json={},
    )
    assert r2.status_code == 400, (
        f"Re-publish should fail with 400 when template flag was revoked but "
        f"override still says 'template'; got {r2.status_code}: {r2.text}"
    )
    detail = str(r2.json().get("detail", ""))
    detail_lower = detail.lower()
    assert (
        "template" in detail_lower
        or "allow_template_sharing" in detail_lower
        or "ts-unflag-odoo" in detail_lower
    ), f"400 detail should reference template sharing; got: {detail}"
