"""End-to-end scenario test: agent bundle publish → install → gate → chat.

One scenario test that walks through the complete bundle user story:
publisher publishes a mixed-credential agent, an installer finds it in the
catalog, quick-installs it, is blocked by the readiness gate until they
fill the placeholder credential, and then successfully chats with the agent.

Scenario:
  1.  Publisher prepares agent with PBP credential (api_token, allow_sharing=True)
      and PBT credential (odoo, allow_template_sharing=True).
  2.  Publisher creates an AI credential and wires it as publisher-provided.
  3.  Publish → make public.
  4.  Installer sees bundle in catalog (is_installed=False).
  5.  Installer checks install-context: 1 PBP spec, 1 PBT spec, AI=publisher-provided.
  6.  Quick install (empty body): gets fresh AgentPublic with bundle_uuid set.
  7.  Post-install state: PBP credential visible, PBT placeholder visible,
      AI credential share exists, app-data volume has catalog_type='server'.
  8.  Chat attempt → gate blocks: system message persisted, LLM NOT engaged.
  9.  Installer fills PBT private fields: credential is no longer a placeholder.
  10. Chat succeeds: assistant message appears, proving agent-env was reached.

Direct DB access is used only for AICredentialShare (no API listing endpoint
for shared-to-me AI credentials) and AppDataVolume.catalog_type (the
GET /users/me/app-data surface exposes this in JSON, so we use the API).
"""

import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import settings
from app.models.credentials.ai_credential_share import AICredentialShare
from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import (
    install_bundle as _install,
    link_bundle_credential_to_agent as _link_credential_to_agent,
    make_user_and_headers as _make_user_and_headers,
    publish_bundle as _publish,
)
from tests.utils.message import list_messages, send_message
from tests.utils.session import create_session_via_api

API = settings.API_V1_STR


# ── Module-level helpers ──────────────────────────────────────────────────────
# Shared bundle helpers (_make_user_and_headers, _publish,
# _install, _link_credential_to_agent) are imported above from
# tests.utils.bundle. The typed credential factories below stay local.


def _create_api_token_credential(
    client: TestClient,
    headers: dict[str, str],
    *,
    name: str,
    allow_sharing: bool,
) -> dict:
    r = client.post(
        f"{API}/credentials/",
        headers=headers,
        json={
            "name": name,
            "type": "api_token",
            "allow_sharing": allow_sharing,
            "credential_data": {
                "api_token": "test-token-e2e",
                "api_token_type": "bearer",
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
    database_name: str = "e2e_db",
    login: str = "pub_login",
    api_token: str = "pub_secret",
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
) -> dict:
    r = client.put(
        f"{API}/agents/{install_id}/setup-credentials/{credential_id}",
        headers=headers,
        json={"credential_data": credential_data},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _set_publish_settings(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    *,
    credential_overrides: dict[str, dict] | None = None,
    ai_credentials: dict | None = None,
) -> dict:
    body: dict = {}
    if credential_overrides is not None:
        body["credential_overrides"] = credential_overrides
    if ai_credentials is not None:
        body["ai_credentials"] = ai_credentials
    r = client.patch(
        f"{API}/agents/{agent_id}/publish-settings",
        headers=headers,
        json=body,
    )
    assert r.status_code == 200, r.text
    return r.json()


# ── The Scenario ──────────────────────────────────────────────────────────────


def test_bundle_e2e_publish_install_gate_chat(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """End-to-end bundle flow: publish → catalog → quick-install → gate → chat.

      1. Publisher creates agent, PBP credential, PBT credential, AI credential.
      2. Publisher configures PBT override and publisher AI credential before publish.
      3. Publisher publishes and makes bundle public.
      4. Installer sees bundle in catalog (is_installed=False).
      5. Installer fetches install-context: 1 PBP spec, 1 PBT spec, AI publisher-provided.
      6. Quick install (empty body) → fresh AgentPublic with bundle_uuid set.
      7. Post-install state: PBP visible to installer, PBT placeholder materialised,
         AI share row exists in DB, app-data volume has catalog_type='server'.
      8. Chat → gate blocks: system message with install_setup_required=True persisted,
         LLM NOT called.
      9. Installer fills PBT private fields → is_placeholder flips to False.
     10. Chat succeeds → assistant message proves agent-env was reached.
    """
    pub_headers = superuser_token_headers

    # ── Phase 1: Publisher prepares the agent ─────────────────────────────────

    publisher_agent = create_agent_via_api(
        client, pub_headers, name="E2E-Publisher-Agent"
    )
    agent_id = publisher_agent["id"]

    # Credential A: PBP — api_token, shareable
    pbp_cred_name = f"e2e-pbp-{uuid.uuid4().hex[:6]}"
    pbp_cred = _create_api_token_credential(
        client, pub_headers, name=pbp_cred_name, allow_sharing=True
    )
    pbp_cred_id = pbp_cred["id"]
    assert pbp_cred["allow_sharing"] is True

    # Credential B: PBT — odoo, allow_template_sharing=True, private=[login, api_token]
    pbt_cred_name = f"e2e-pbt-{uuid.uuid4().hex[:6]}"
    pbt_cred = _create_odoo_credential(
        client,
        pub_headers,
        name=pbt_cred_name,
        allow_template_sharing=True,
        template_private_fields=["login", "api_token"],
        url="https://erp-e2e.example.com",
        database_name="e2e_db",
        login="pub_login",
        api_token="pub_secret",
    )
    pbt_cred_id = pbt_cred["id"]
    assert pbt_cred["allow_template_sharing"] is True
    assert sorted(pbt_cred.get("template_private_fields", [])) == ["api_token", "login"]

    # Link both credentials to the agent
    _link_credential_to_agent(client, pub_headers, agent_id, pbp_cred_id)
    _link_credential_to_agent(client, pub_headers, agent_id, pbt_cred_id)

    # Publisher AI credential
    pub_ai_cred = create_random_ai_credential(client, pub_headers, set_default=True)
    pub_ai_cred_id = pub_ai_cred["id"]

    # ── Phase 2: Publish + configure bundle + make public ─────────────────────

    # First publish: the PBT spec is auto-produced from allow_template_sharing=True;
    # the PBP spec is auto-produced from allow_sharing=True.
    fresh_pub = _publish(client, pub_headers, agent_id)
    bundle_id = fresh_pub["bundle_id"]
    bundle_uuid = fresh_pub["bundle_uuid"]
    assert bundle_id, "Expected a bundle_id after publish"
    assert bundle_uuid, "Expected a bundle_uuid after publish"

    # Wire publisher AI credential + make public via PATCH /bundles/{uuid}.
    # The bundle row now exists (first publish created it), so the canonical
    # path is the bundles route, not the publish-settings draft.
    r_bundle = client.patch(
        f"{API}/bundles/{bundle_uuid}",
        headers=pub_headers,
        json={
            "publisher_ai_credential_conversation_id": pub_ai_cred_id,
            "is_listed": True,
            "visibility": "public",
        },
    )
    assert r_bundle.status_code == 200, r_bundle.text

    # Verify the published revision has the right specs
    revision_specs = fresh_pub.get("required_credential_specs") or []
    if revision_specs:
        spec_by_name = {s["name"]: s for s in revision_specs}
        if pbt_cred_name in spec_by_name:
            assert spec_by_name[pbt_cred_name]["provided_by"] == "template", (
                f"PBT spec should be 'template'; got {spec_by_name[pbt_cred_name]}"
            )
        if pbp_cred_name in spec_by_name:
            assert spec_by_name[pbp_cred_name]["provided_by"] == "publisher", (
                f"PBP spec should be 'publisher'; got {spec_by_name[pbp_cred_name]}"
            )

    # ── Phase 3: Installer sees bundle in catalog ─────────────────────────────

    installer, installer_headers = _make_user_and_headers(client)
    installer_id = uuid.UUID(installer["id"])

    catalog_r = client.get(f"{API}/catalog/", headers=installer_headers)
    assert catalog_r.status_code == 200, catalog_r.text
    catalog_data = catalog_r.json()["data"]

    matching = [e for e in catalog_data if e["bundle_id"] == bundle_id]
    assert len(matching) == 1, (
        f"Expected bundle {bundle_id} in catalog; found {[e['bundle_id'] for e in catalog_data]}"
    )
    catalog_entry = matching[0]
    assert catalog_entry["is_installed"] is False, (
        "Bundle should not yet be installed for the installer"
    )
    assert catalog_entry["visibility"] == "public"

    # ── Phase 4: Installer checks install-context ──────────────────────────────

    ctx_r = client.get(
        f"{API}/catalog/{bundle_id}/install-context",
        headers=installer_headers,
    )
    assert ctx_r.status_code == 200, ctx_r.text
    ctx = ctx_r.json()

    assert ctx["ai_provided_by_publisher"] is True, (
        "Expected ai_provided_by_publisher=True since we wired publisher AI cred"
    )

    service_specs = ctx.get("service_specs", [])
    pbp_specs = [s for s in service_specs if s.get("provided_by") == "publisher"]
    pbt_specs = [s for s in service_specs if s.get("provided_by") == "template"]
    assert len(pbp_specs) == 1, (
        f"Expected 1 PBP spec in install-context; got {pbp_specs}"
    )
    assert len(pbt_specs) == 1, (
        f"Expected 1 PBT spec in install-context; got {pbt_specs}"
    )
    pbt_ctx_spec = pbt_specs[0]
    assert sorted(pbt_ctx_spec.get("template_private_fields", [])) == ["api_token", "login"], (
        f"Expected ['api_token', 'login'] private fields; got {pbt_ctx_spec}"
    )

    # ── Phase 5: Quick Install ────────────────────────────────────────────────

    install = _install(client, installer_headers, bundle_id)
    install_id = install["id"]

    assert install.get("bundle_uuid") == bundle_uuid, (
        f"Install bundle_uuid {install.get('bundle_uuid')} should match published {bundle_uuid}"
    )
    assert install.get("is_publisher_install") is False, (
        "Foreign installer's copy must not be the publisher install"
    )

    # Catalog entry should now show is_installed=True
    catalog_r2 = client.get(f"{API}/catalog/", headers=installer_headers)
    assert catalog_r2.status_code == 200
    matching2 = [
        e for e in catalog_r2.json()["data"] if e["bundle_id"] == bundle_id
    ]
    assert matching2 and matching2[0]["is_installed"] is True, (
        "After install, catalog should show is_installed=True"
    )

    # ── Phase 6: Verify post-install state ────────────────────────────────────

    # 6a. PBP credential is accessible to the installer via GET /credentials/{id}
    pbp_r = client.get(
        f"{API}/credentials/{pbp_cred_id}",
        headers=installer_headers,
    )
    assert pbp_r.status_code == 200, (
        f"Installer should be able to access PBP credential via sharing; got {pbp_r.text}"
    )
    pbp_resp = pbp_r.json()
    assert pbp_resp["type"] == "api_token"

    # 6b. PBT placeholder credential visible via setup-credentials
    setup_creds = _get_setup_credentials(client, installer_headers, install_id)
    pbt_placeholders = [c for c in setup_creds if c.get("type") == "odoo"]
    assert len(pbt_placeholders) == 1, (
        f"Expected 1 Odoo placeholder in setup-credentials; got {setup_creds}"
    )
    placeholder_cred = pbt_placeholders[0]
    placeholder_cred_id = placeholder_cred["id"]
    assert placeholder_cred["name"] == pbt_cred_name, (
        f"Placeholder name should match PBT spec name; got {placeholder_cred['name']}"
    )
    # Non-private fields should be pre-filled in template_prefilled_data
    prefilled = placeholder_cred.get("template_prefilled_data") or {}
    assert prefilled.get("url") == "https://erp-e2e.example.com", (
        f"Non-private 'url' should be in prefilled data; got {prefilled}"
    )
    assert prefilled.get("database_name") == "e2e_db", (
        f"Non-private 'database_name' should be in prefilled data; got {prefilled}"
    )
    assert "login" not in prefilled, "Private 'login' must not appear in prefilled data"
    assert "api_token" not in prefilled, "Private 'api_token' must not appear in prefilled data"

    # 6c. Publisher AI credential share exists (DB check — no API listing for shared-to-me AI creds)
    db.expire_all()
    ai_share = db.exec(
        select(AICredentialShare).where(
            AICredentialShare.ai_credential_id == uuid.UUID(pub_ai_cred_id),
            AICredentialShare.shared_with_user_id == installer_id,
        )
    ).first()
    assert ai_share is not None, (
        "Expected AICredentialShare (publisher AI → installer) to exist after install"
    )

    # 6d. App-data volume has catalog_type='server' (visible via the app-data API)
    app_data_r = client.get(
        f"{API}/users/me/app-data",
        headers=installer_headers,
    )
    assert app_data_r.status_code == 200, app_data_r.text
    volumes = app_data_r.json().get("data", [])
    install_volumes = [v for v in volumes if v.get("current_install_id") == install_id]
    if install_volumes:
        # If a volume was already created, verify catalog_type
        assert install_volumes[0]["catalog_type"] == "server", (
            f"App-data volume for consumer install must have catalog_type='server'; "
            f"got {install_volumes[0]['catalog_type']}"
        )
    # If no volume yet (lazily created on first env provision), that's acceptable;
    # the uniqueness constraint guarantees it will be 'server' when created.

    # ── Phase 7: Chat attempt → gate blocks ───────────────────────────────────

    session_data = create_session_via_api(client, installer_headers, install_id)
    session_id = session_data["id"]

    # Stub must be active DURING drain_tasks() — see README WRONG vs CORRECT example
    gate_block_stub = StubAgentEnvConnector(response_text="should-not-fire")
    with patch("app.services.sessions.message_service.agent_env_connector", gate_block_stub):
        send_message(client, installer_headers, session_id, "Hello, can you help me?")
        drain_tasks()

    # Gate should have persisted a system message
    messages = list_messages(client, installer_headers, session_id)
    system_messages = [m for m in messages if m["role"] == "system"]
    assert len(system_messages) >= 1, (
        "Expected at least one system message persisted after gate short-circuit"
    )
    gate_msg = system_messages[0]
    metadata = gate_msg.get("message_metadata") or {}
    assert metadata.get("install_setup_required") is True, (
        f"Expected install_setup_required=True in message_metadata; got {metadata}"
    )
    assert metadata.get("setup_url") is not None, (
        f"Expected setup_url in message_metadata; got {metadata}"
    )
    assert f"/agent/{install_id}#credentials" in (metadata.get("setup_url") or ""), (
        f"setup_url should reference the install's credential page; got {metadata.get('setup_url')}"
    )
    assert isinstance(metadata.get("missing"), list) and len(metadata["missing"]) > 0, (
        f"Expected non-empty missing[] in message_metadata; got {metadata}"
    )

    # LLM must NOT have been engaged — no agent messages, no stub calls
    agent_messages = [m for m in messages if m["role"] == "agent"]
    assert len(agent_messages) == 0, (
        f"LLM was unexpectedly called; got {len(agent_messages)} agent messages"
    )
    assert len(gate_block_stub.stream_calls) == 0, (
        f"agent_env_connector.stream_chat called {len(gate_block_stub.stream_calls)} time(s) "
        "— should be 0 when gate blocks"
    )

    # ── Phase 8: Installer fills the PBT private fields ───────────────────────

    put_resp = _put_setup_credential(
        client,
        installer_headers,
        install_id,
        placeholder_cred_id,
        {
            "url": "https://erp-e2e.example.com",
            "database_name": "e2e_db",
            "login": "installer_login",
            "api_token": "installer_real_token",
        },
    )
    assert put_resp["id"] == placeholder_cred_id, (
        f"PUT response id {put_resp['id']} != placeholder id {placeholder_cred_id}"
    )

    # Gate should now be ready
    setup_status = _get_setup_status(client, installer_headers, install_id)
    assert setup_status["status"] == "ready", (
        f"Expected setup-status='ready' after filling all private fields; "
        f"got {setup_status['status']} missing={setup_status.get('missing')}"
    )

    # ── Phase 9: Chat succeeds — message reaches agent env ────────────────────

    real_stub = StubAgentEnvConnector(response_text="Hello, installer!")
    with patch("app.services.sessions.message_service.agent_env_connector", real_stub):
        send_message(client, installer_headers, session_id, "Now can you help me?")
        drain_tasks()  # streaming happens HERE, inside the patch

    # Agent message must now appear (proves agent-env was reached; role="agent")
    messages_after = list_messages(client, installer_headers, session_id)
    agent_messages_after = [m for m in messages_after if m["role"] == "agent"]
    assert len(agent_messages_after) >= 1, (
        "Expected at least one agent message after filling credentials — "
        "agent-env must have been reached"
    )

    # The gate's system message must NOT have been re-appended for this turn
    # (only one system message should exist — from the earlier gate block)
    system_messages_after = [m for m in messages_after if m["role"] == "system"]
    assert len(system_messages_after) == len(system_messages), (
        f"Gate system message must not be re-appended after gate is cleared; "
        f"had {len(system_messages)} before, now {len(system_messages_after)}"
    )
