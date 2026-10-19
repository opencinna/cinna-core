"""Skill credential slot provisioning at install / upgrade / uninstall (Phase 2).

Publish (Phase 1) freezes each declared slot's ``required_credential_specs``
onto the revision. This file exercises what install, upgrade and uninstall do
with those frozen specs through ``CredentialProvisioner`` (SKILL_INSTALL_POLICY):
share-and-link the publisher's credential, link the installer's own matching
credential, materialise a template, or create a placeholder -- never failing
the install for a per-slot reason (I8), always idempotently (I9). See
docs/plans/skill_credential_requirements_plan.md §2 (D1, D3, D5), §3 (I8, I9,
I10), §4 (C4), §8.

Scenarios (plan §8.9):
  1. Publisher-provided, end to end: preview, install outcome, the shared
     credential appears on the installer's agent and shared-with-me list
     (category "automatic", D6), the pushed ``credentials.json`` carries the
     slot, the Addons row is healthy, and uninstall+reinstall leaves exactly
     one share and one link (idempotent, D5-adjacent).
  2. The installer is the publisher: ``linked_publisher`` with no share row.
  3. User-provided slot match: an owned credential, and a directly-shared one.
  4. User-provided, no match: a placeholder is created; the Addons row warns
     with ``credential_missing`` / ``not_configured``; setup-status stays
     ready (D1); a second install on another agent reuses the SAME
     credential (I9); filling the placeholder clears the warning.
  5. Template: the publisher's template-shared credential materialises a
     placeholder carrying its non-private fields, never the live secret.
  6. Publisher unavailable at install (sharing turned off before install):
     ``publisher_unavailable``, a placeholder, and a warning row.
  7. Access revoked after install: either sharing-disable path unlinks the
     recipient's agents (so the value leaves their containers) and the row's
     reason becomes ``not_linked``. Setup-status stays ready (D1).
  8. Upgrade provisions only the slot the new revision adds; a slot the user
     unlinked earlier is not re-linked.
  9. Uninstall release semantics: an orphaned placeholder is unlinked and
     deleted, a real linked credential is untouched, and a placeholder shared
     by two installed skills survives until the last one is gone.
  10. Guards: preview on another user's agent, an invisible package, and an
      unknown revision number are all 404.
  11. Publisher slot drift warns existing consumers and creates a correctly
      slotted placeholder for new installs; a frozen id is not a runtime alias.
  12. Directly shared placeholders warn until their owner fills them in.

Publish-time resolution matrix (owned/shareable/template/not-owned) is
covered in ``agents_skill_credentials_publish_test.py``. The gate's D1
exclusion for a bundle-install agent that also carries a catalog skill is
covered in ``agents_skill_credentials_gate_test.py``. Deletion-impact skill
awareness is covered in
``tests/api/credentials/test_credential_deletion_impact_skills.py``.
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.addons import addons_by_name, refresh_agent_addons
from tests.utils.credential import (
    create_random_credential,
    get_agent_credentials,
    get_credential,
    get_credential_with_data,
    link_credential_to_agent,
    real_credentials_json,
    set_credential_sharing,
    share_credential_via_api,
    unlink_credential_from_agent,
    update_credential,
)
from tests.utils.skill_catalog import (
    get_skill_install_preview,
    get_skill_package,
    install_skill,
    list_agent_plugins,
    make_agent_with_env,
    make_developer,
    patched_skill_storage,
    publish_skill,
    uninstall_agent_plugin,
    upgrade_agent_plugin,
    write_skill_with_credentials,
)

API = settings.API_V1_STR


@pytest.fixture(autouse=True)
def skill_storage(tmp_path: Path):
    with patched_skill_storage(tmp_path / "skill-storage") as root:
        yield root


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _install_adapter(lifecycle_manager) -> EnvironmentTestAdapter:
    """Route every ``get_adapter`` call at one adapter the test controls."""
    adapter = EnvironmentTestAdapter()
    adapter.skills_index = {"hash": "hash-empty", "skills": [], "errors": []}
    adapter.workspace_files = {}
    lifecycle_manager.get_adapter = lambda env: adapter
    return adapter


def _addon_index_row(name: str) -> dict:
    """One env-core ``GET /config/skills`` entry for a catalog install."""
    return {
        "name": name,
        "description": f"Does {name} things.",
        "source": "catalog",
        "plugin_ref": f"cinna-skills/{name}",
        "path": f"plugins/cinna-skills/{name}/skills/{name}",
        "has_scripts": False,
        "user_invocable": True,
        "model_invocable": True,
        "size_bytes": 512,
        "error": None,
        "warning": None,
        "secret_paths": [],
    }


def _addon_row_for_skill(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    adapter: EnvironmentTestAdapter,
    skill_name: str,
) -> dict:
    """Point the env index at ``skill_name`` and refresh the addons projection."""
    adapter.skills_index = {
        "hash": f"hash-{skill_name}",
        "skills": [_addon_index_row(skill_name)],
        "errors": [],
    }
    payload = refresh_agent_addons(client, headers, agent_id)
    return addons_by_name(payload)[skill_name]


def _get_credential_404(client: TestClient, headers: dict[str, str], credential_id: str) -> None:
    r = client.get(f"{API}/credentials/{credential_id}", headers=headers)
    assert r.status_code == 404, r.text


def _setup_status(client: TestClient, headers: dict[str, str], agent_id: str) -> dict:
    r = client.get(f"{API}/agents/{agent_id}/setup-status", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Scenario 1: publisher-provided, end to end
# ---------------------------------------------------------------------------


def test_publisher_provided_credential_end_to_end(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    1. Publisher A shares an ``api_token`` credential for a declared slot.
    2. Consumer B previews and installs -> ``linked_publisher``.
    3. B's own credentials list and shared-with-me list both carry it, the
       latter under category ``automatic`` (D6).
    4. The ``credentials.json`` pushed to B's env carries the slot.
    5. The Addons row is healthy (``ok``).
    6. Uninstall then reinstall leaves exactly one share and one link.
    """
    pub, pub_headers = make_developer(client, superuser_token_headers)
    pub_agent, pub_env = make_agent_with_env(client, pub_headers, "PubEndToEnd-Publisher")

    pub_cred = create_random_credential(client, pub_headers, credential_type="api_token")
    update_credential(
        client, pub_headers, pub_cred["id"], service_uri="slot-pub", allow_sharing=True,
    )
    link_credential_to_agent(client, pub_headers, pub_agent, pub_cred["id"])

    write_skill_with_credentials(
        pub_env, "pub-skill", [{"slot": "slot-pub", "type": "api_token"}],
    )
    revision = publish_skill(
        client, pub_headers, pub_agent, "pub-skill", visibility="public",
    )
    package_uuid = revision["package_id"]

    con, con_headers = make_developer(client, superuser_token_headers)
    con_agent, con_env = make_agent_with_env(client, con_headers, "PubEndToEnd-Consumer")
    adapter = _install_adapter(patch_environment_adapter)

    # ── Preview ────────────────────────────────────────────────────────
    preview = get_skill_install_preview(client, con_headers, con_agent, package_uuid)
    assert preview["credentials"][0]["slot"] == "slot-pub"
    assert preview["credentials"][0]["outcome"] == "linked_publisher"
    assert preview["credentials"][0]["needs_setup"] is False
    assert preview["credentials"][0]["credential_id"] is None
    assert preview["credentials"][0]["credential_name"] is None

    # ── Install ────────────────────────────────────────────────────────
    result = install_skill(client, con_headers, con_agent, package_uuid)
    provisioning = result["credential_provisioning"]
    assert len(provisioning) == 1
    assert provisioning[0]["outcome"] == "linked_publisher"
    assert provisioning[0]["needs_setup"] is False
    assert provisioning[0]["credential_id"] == pub_cred["id"]

    con_creds = get_agent_credentials(client, con_headers, con_agent)["data"]
    assert pub_cred["id"] in [c["id"] for c in con_creds]

    shared = client.get(f"{API}/credentials/shared-with-me", headers=con_headers)
    assert shared.status_code == 200, shared.text
    shared_entry = next(c for c in shared.json()["data"] if c["id"] == pub_cred["id"])
    assert shared_entry["category"] == "automatic"
    assert shared_entry["source"] == "skill_install"

    creds_json = real_credentials_json(adapter.credentials_set)
    by_id = {e["id"]: e for e in creds_json}
    assert by_id[pub_cred["id"]]["service_uri"] == "slot-pub"

    row = _addon_row_for_skill(client, con_headers, con_agent, adapter, "pub-skill")
    assert row["status"] == "ok"
    assert row["credential_issues"] == []

    # ── Uninstall, reinstall: still one share, one link ───────────────
    link_id = list_agent_plugins(client, con_headers, con_agent)[0]["id"]
    uninstall_agent_plugin(client, con_headers, con_agent, link_id)
    install_skill(client, con_headers, con_agent, package_uuid)

    con_creds_2 = get_agent_credentials(client, con_headers, con_agent)["data"]
    matching = [c for c in con_creds_2 if c["id"] == pub_cred["id"]]
    assert len(matching) == 1

    shares = client.get(
        f"{API}/credentials/{pub_cred['id']}/shares", headers=pub_headers
    )
    assert shares.status_code == 200, shares.text
    assert shares.json()["count"] == 1


# ---------------------------------------------------------------------------
# Scenarios 11 / 12: live slot readiness after publish and sharing
# ---------------------------------------------------------------------------


def test_publisher_slot_change_warns_and_degrades_new_installs(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """Frozen publisher ids cannot satisfy a slot the live credential dropped."""
    _, pub_headers = make_developer(client, superuser_token_headers)
    pub_agent, pub_env = make_agent_with_env(client, pub_headers, "SlotDrift-Publisher")
    credential = create_random_credential(client, pub_headers, credential_type="api_token")
    update_credential(
        client, pub_headers, credential["id"], service_uri="frozen-slot", allow_sharing=True,
    )
    link_credential_to_agent(client, pub_headers, pub_agent, credential["id"])
    write_skill_with_credentials(
        pub_env, "slot-drift", [{"slot": "frozen-slot", "type": "api_token"}],
    )
    revision = publish_skill(client, pub_headers, pub_agent, "slot-drift", visibility="public")
    _, con_headers = make_developer(client, superuser_token_headers)
    con_agent, _ = make_agent_with_env(client, con_headers, "SlotDrift-Existing")
    new_agent, _ = make_agent_with_env(client, con_headers, "SlotDrift-New")
    adapter = _install_adapter(patch_environment_adapter)
    install_skill(client, con_headers, con_agent, revision["package_id"])

    update_credential(client, pub_headers, credential["id"], service_uri="different-slot")
    row = _addon_row_for_skill(client, con_headers, con_agent, adapter, "slot-drift")
    assert row["credential_issues"] == [
        {"slot": "frozen-slot", "type": "api_token", "reason": "not_linked"}
    ]
    assert row["status"] == "warning"
    assert _setup_status(client, con_headers, con_agent)["status"] == "ready"

    # Neither the existing link nor the existing share can bring the drifted
    # publisher credential back as a working frozen slot.
    for agent_id in (con_agent, new_agent):
        preview = get_skill_install_preview(client, con_headers, agent_id, revision["package_id"])
        assert preview["credentials"][0]["outcome"] == "publisher_unavailable"
        assert preview["credentials"][0]["needs_setup"] is True
    installed = install_skill(client, con_headers, new_agent, revision["package_id"])
    provision = installed["credential_provisioning"][0]
    assert provision["outcome"] == "publisher_unavailable"
    assert provision["credential_id"] != credential["id"]
    materialized = get_credential(client, con_headers, provision["credential_id"])
    assert materialized["service_uri"] == "frozen-slot"
    assert materialized["is_placeholder"] is True


def test_shared_placeholder_keeps_addon_warning_until_owner_fills_it(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """A share grants access; it does not fill a placeholder for SDK consumers."""
    _, owner_headers = make_developer(client, superuser_token_headers)
    owner_agent, owner_env = make_agent_with_env(client, owner_headers, "SharedEmpty-Owner")
    write_skill_with_credentials(
        owner_env, "shared-empty", [{"slot": "shared-empty-slot", "type": "api_token"}],
    )
    revision = publish_skill(client, owner_headers, owner_agent, "shared-empty", visibility="public")
    adapter = _install_adapter(patch_environment_adapter)
    owner_install = install_skill(client, owner_headers, owner_agent, revision["package_id"])
    placeholder_id = owner_install["credential_provisioning"][0]["credential_id"]
    update_credential(client, owner_headers, placeholder_id, allow_sharing=True)
    consumer, con_headers = make_developer(client, superuser_token_headers)
    share_credential_via_api(client, owner_headers, placeholder_id, consumer["email"])
    con_agent, _ = make_agent_with_env(client, con_headers, "SharedEmpty-Consumer")
    installed = install_skill(client, con_headers, con_agent, revision["package_id"])
    assert installed["credential_provisioning"][0]["needs_setup"] is True
    row = _addon_row_for_skill(client, con_headers, con_agent, adapter, "shared-empty")
    assert row["status"] == "warning"
    assert row["credential_issues"] == [
        {"slot": "shared-empty-slot", "type": "api_token", "reason": "not_configured"}
    ]
    update_credential(
        client, owner_headers, placeholder_id,
        credential_data={"api_token_type": "bearer", "api_token": "filled-secret"},
    )
    row = _addon_row_for_skill(client, con_headers, con_agent, adapter, "shared-empty")
    assert row["status"] == "ok"
    assert row["credential_issues"] == []


# ---------------------------------------------------------------------------
# Scenario 2: installer is the publisher -- no share row
# ---------------------------------------------------------------------------


def test_installer_is_the_publisher_no_share_row(
    client: TestClient, superuser_token_headers: dict[str, str],
) -> None:
    pub, pub_headers = make_developer(client, superuser_token_headers)
    pub_agent, pub_env = make_agent_with_env(client, pub_headers, "SelfInstall-Source")
    pub_agent_2, _ = make_agent_with_env(client, pub_headers, "SelfInstall-Target")

    pub_cred = create_random_credential(client, pub_headers, credential_type="api_token")
    update_credential(
        client, pub_headers, pub_cred["id"], service_uri="slot-self", allow_sharing=True,
    )
    link_credential_to_agent(client, pub_headers, pub_agent, pub_cred["id"])

    write_skill_with_credentials(
        pub_env, "self-skill", [{"slot": "slot-self", "type": "api_token"}],
    )
    revision = publish_skill(
        client, pub_headers, pub_agent, "self-skill", visibility="private",
    )
    package_uuid = revision["package_id"]

    result = install_skill(client, pub_headers, pub_agent_2, package_uuid)
    provisioning = result["credential_provisioning"]
    assert provisioning[0]["outcome"] == "linked_publisher"
    assert provisioning[0]["credential_id"] == pub_cred["id"]

    shares = client.get(
        f"{API}/credentials/{pub_cred['id']}/shares", headers=pub_headers
    )
    assert shares.status_code == 200, shares.text
    assert shares.json()["data"] == []


# ---------------------------------------------------------------------------
# Scenario 3: user-provided slot match -- owned, and directly shared
# ---------------------------------------------------------------------------


def test_user_provided_slot_match_linked_existing(
    client: TestClient, superuser_token_headers: dict[str, str],
) -> None:
    pub, pub_headers = make_developer(client, superuser_token_headers)
    pub_agent, pub_env = make_agent_with_env(client, pub_headers, "UserMatch-Publisher")
    write_skill_with_credentials(
        pub_env, "user-match-skill", [{"slot": "slot-user", "type": "api_token"}],
    )
    revision = publish_skill(
        client, pub_headers, pub_agent, "user-match-skill", visibility="public",
    )
    package_uuid = revision["package_id"]
    # The publisher has nothing linked to the slot -> the revision requires
    # the installer to bring their own.
    assert revision["required_credentials"][0]["provided_by"] == "user"

    # B owns a credential carrying the slot.
    b, b_headers = make_developer(client, superuser_token_headers)
    b_agent, _ = make_agent_with_env(client, b_headers, "UserMatch-B")
    b_cred = create_random_credential(client, b_headers, credential_type="api_token")
    update_credential(client, b_headers, b_cred["id"], service_uri="slot-user")
    # Deliberately NOT linked to b_agent yet -- install must discover and
    # link it itself (linked_existing), not find it already linked.

    b_result = install_skill(client, b_headers, b_agent, package_uuid)
    assert b_result["credential_provisioning"][0]["outcome"] == "linked_existing"
    assert b_result["credential_provisioning"][0]["credential_id"] == b_cred["id"]

    # C has a direct share of a slot-matching credential it does not own.
    d, d_headers = make_developer(client, superuser_token_headers)
    d_cred = create_random_credential(client, d_headers, credential_type="api_token")
    update_credential(
        client, d_headers, d_cred["id"], service_uri="slot-user", allow_sharing=True,
    )
    c, c_headers = make_developer(client, superuser_token_headers)
    share_credential_via_api(client, d_headers, d_cred["id"], c["email"])
    c_agent, _ = make_agent_with_env(client, c_headers, "UserMatch-C")

    c_result = install_skill(client, c_headers, c_agent, package_uuid)
    assert c_result["credential_provisioning"][0]["outcome"] == "linked_existing"
    assert c_result["credential_provisioning"][0]["credential_id"] == d_cred["id"]


# ---------------------------------------------------------------------------
# Scenario 4: user-provided, no match -> placeholder, then filled
# ---------------------------------------------------------------------------


def test_user_provided_no_match_placeholder_then_fill(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    pub, pub_headers = make_developer(client, superuser_token_headers)
    pub_agent, pub_env = make_agent_with_env(client, pub_headers, "NoMatch-Publisher")
    write_skill_with_credentials(
        pub_env, "no-match-skill", [{"slot": "slot-empty", "type": "api_token"}],
    )
    revision = publish_skill(
        client, pub_headers, pub_agent, "no-match-skill", visibility="public",
    )
    package_uuid = revision["package_id"]

    b, b_headers = make_developer(client, superuser_token_headers)
    b_agent, _ = make_agent_with_env(client, b_headers, "NoMatch-B1")
    adapter = _install_adapter(patch_environment_adapter)

    result = install_skill(client, b_headers, b_agent, package_uuid)
    prov = result["credential_provisioning"][0]
    assert prov["outcome"] == "placeholder_created"
    assert prov["needs_setup"] is True
    placeholder_id = prov["credential_id"]

    placeholder = get_credential(client, b_headers, placeholder_id)
    assert placeholder["name"] == "slot-empty"
    assert placeholder["service_uri"] == "slot-empty"
    assert placeholder["is_placeholder"] is True

    row = _addon_row_for_skill(client, b_headers, b_agent, adapter, "no-match-skill")
    assert row["status"] == "warning"
    assert row["status_code"] == "credential_missing"
    assert row["credential_issues"] == [
        {"slot": "slot-empty", "type": "api_token", "reason": "not_configured"}
    ]

    # D1: a skill-only placeholder never trips setup-status.
    setup = _setup_status(client, b_headers, b_agent)
    assert setup["status"] == "ready"

    # A second install (another of B's agents) reuses the SAME credential (I9).
    b_agent_2, _ = make_agent_with_env(client, b_headers, "NoMatch-B2")
    preview = get_skill_install_preview(client, b_headers, b_agent_2, package_uuid)
    assert preview["credentials"][0]["outcome"] == "linked_existing"
    assert preview["credentials"][0]["needs_setup"] is True
    result2 = install_skill(client, b_headers, b_agent_2, package_uuid)
    prov2 = result2["credential_provisioning"][0]
    assert prov2["outcome"] == "linked_existing"
    assert prov2["credential_id"] == placeholder_id
    assert prov2["needs_setup"] is True

    # A different skill reusing that slot on the same agent also needs setup.
    write_skill_with_credentials(
        pub_env, "same-slot-skill", [{"slot": "slot-empty", "type": "api_token"}],
    )
    other_revision = publish_skill(
        client, pub_headers, pub_agent, "same-slot-skill", visibility="public",
    )
    other_package = other_revision["package_id"]
    preview = get_skill_install_preview(client, b_headers, b_agent_2, other_package)
    assert preview["credentials"][0]["outcome"] == "already_linked"
    assert preview["credentials"][0]["needs_setup"] is True
    reused = install_skill(client, b_headers, b_agent_2, other_package)
    assert reused["credential_provisioning"][0]["outcome"] == "already_linked"
    assert reused["credential_provisioning"][0]["needs_setup"] is True

    # A filled replacement may coexist with the placeholder. Preview must
    # agree with Addons and the runtime: the filled slot makes this agent ready.
    replacement = create_random_credential(client, b_headers, credential_type="api_token")
    update_credential(client, b_headers, replacement["id"], service_uri="slot-empty")
    link_credential_to_agent(client, b_headers, b_agent_2, replacement["id"])
    preview = get_skill_install_preview(client, b_headers, b_agent_2, other_package)
    assert preview["credentials"][0]["outcome"] == "already_linked"
    assert preview["credentials"][0]["credential_id"] == replacement["id"]
    assert preview["credentials"][0]["needs_setup"] is False

    # Fill the placeholder -> the row becomes ok.
    update_credential(
        client, b_headers, placeholder_id,
        credential_data={
            "api_token_type": "bearer",
            "api_token_template": "Authorization: Bearer {TOKEN}",
            "api_token": "filled-secret",
        },
    )
    filled = get_credential(client, b_headers, placeholder_id)
    assert filled["is_placeholder"] is False

    row2 = _addon_row_for_skill(client, b_headers, b_agent, adapter, "no-match-skill")
    assert row2["status"] == "ok"
    assert row2["credential_issues"] == []
    preview = get_skill_install_preview(client, b_headers, b_agent_2, other_package)
    assert preview["credentials"][0]["needs_setup"] is False


# ---------------------------------------------------------------------------
# Scenario 5: template
# ---------------------------------------------------------------------------


def test_template_materialised_carries_non_private_fields(
    client: TestClient, superuser_token_headers: dict[str, str],
) -> None:
    """
    D12 / I6, post-fix: for ``api_token`` the STORED secret key is
    ``api_token`` itself, not only the computed ``http_header_value`` that
    ``SENSITIVE_FIELDS`` names. Only marking ``http_header_value`` private
    used to still leak the raw token into ``template_data`` -- this is now a
    closed leak (D12 rule mirrors stored keys, D12 §skill_credential_requirements
    ``_template_leaking_secret_fields``). The credential here marks BOTH
    ``api_token`` and ``http_header_value`` private, which is the only shape
    that resolves to ``template`` for this type.
    """
    pub, pub_headers = make_developer(client, superuser_token_headers)
    pub_agent, pub_env = make_agent_with_env(client, pub_headers, "Template-Publisher")
    tmpl_cred = create_random_credential(client, pub_headers, credential_type="api_token")
    update_credential(
        client, pub_headers, tmpl_cred["id"],
        service_uri="slot-template", allow_template_sharing=True,
        template_private_fields=["api_token", "http_header_value"],
    )
    link_credential_to_agent(client, pub_headers, pub_agent, tmpl_cred["id"])

    write_skill_with_credentials(
        pub_env, "template-skill", [{"slot": "slot-template", "type": "api_token"}],
    )
    revision = publish_skill(
        client, pub_headers, pub_agent, "template-skill", visibility="public",
    )
    package_uuid = revision["package_id"]
    assert revision["required_credentials"][0]["provided_by"] == "template"

    # A public catalog read of the published revision never carries the raw
    # secret (or a template_data payload at all -- C3/I6: no public schema
    # exposes it).
    package = get_skill_package(client, pub_headers, package_uuid)
    package_text = json.dumps(package)
    assert "template_data" not in package_text
    assert "sk-test-token-789" not in package_text, (
        "the raw secret must never reach a public catalog read"
    )

    b, b_headers = make_developer(client, superuser_token_headers)
    b_agent, _ = make_agent_with_env(client, b_headers, "Template-Consumer")
    result = install_skill(client, b_headers, b_agent, package_uuid)
    prov = result["credential_provisioning"][0]
    assert prov["outcome"] == "template_materialised"
    materialised_id = prov["credential_id"]

    materialised = get_credential(client, b_headers, materialised_id)
    assert materialised["is_placeholder"] is True
    assert materialised["service_uri"] == "slot-template"
    assert set(materialised["template_private_fields"]) == {"api_token", "http_header_value"}

    with_data = get_credential_with_data(client, b_headers, materialised_id)
    data = with_data["credential_data"]
    assert data.get("api_token_type") == "bearer", (
        "the non-private field must survive materialisation"
    )
    assert data.get("api_token_template") == "Authorization: Bearer {TOKEN}", (
        "the non-private field must survive materialisation"
    )
    assert not data.get("api_token"), (
        "the raw stored secret (api_token) must never reach an installer -- "
        "this is the leak the coordinator flagged: SENSITIVE_FIELDS alone "
        "names only the computed http_header_value, never the stored key"
    )
    assert not data.get("http_header_value"), (
        "the field the owner marked private (http_header_value, the computed "
        "secret header) must never reach the materialised template"
    )


# ---------------------------------------------------------------------------
# Scenario 6: publisher unavailable at install
# ---------------------------------------------------------------------------


def test_publisher_unavailable_at_install(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    pub, pub_headers = make_developer(client, superuser_token_headers)
    pub_agent, pub_env = make_agent_with_env(client, pub_headers, "Unavailable-Publisher")
    pub_cred = create_random_credential(client, pub_headers, credential_type="api_token")
    update_credential(
        client, pub_headers, pub_cred["id"], service_uri="slot-avail", allow_sharing=True,
    )
    link_credential_to_agent(client, pub_headers, pub_agent, pub_cred["id"])

    write_skill_with_credentials(
        pub_env, "avail-skill", [{"slot": "slot-avail", "type": "api_token"}],
    )
    revision = publish_skill(
        client, pub_headers, pub_agent, "avail-skill", visibility="public",
    )
    package_uuid = revision["package_id"]
    assert revision["required_credentials"][0]["provided_by"] == "publisher"

    # The publisher disables sharing before anybody installs.
    set_credential_sharing(client, pub_headers, pub_cred["id"], False)

    b, b_headers = make_developer(client, superuser_token_headers)
    b_agent, _ = make_agent_with_env(client, b_headers, "Unavailable-Consumer")
    adapter = _install_adapter(patch_environment_adapter)

    preview = get_skill_install_preview(client, b_headers, b_agent, package_uuid)
    assert preview["credentials"][0]["outcome"] == "publisher_unavailable"

    result = install_skill(client, b_headers, b_agent, package_uuid)
    prov = result["credential_provisioning"][0]
    assert prov["outcome"] == "publisher_unavailable"
    placeholder = get_credential(client, b_headers, prov["credential_id"])
    assert placeholder["is_placeholder"] is True

    row = _addon_row_for_skill(client, b_headers, b_agent, adapter, "avail-skill")
    assert row["status"] == "warning"
    assert row["status_code"] == "credential_missing"


# ---------------------------------------------------------------------------
# Scenario 7: access revoked after install
# ---------------------------------------------------------------------------


def test_access_revoked_after_install(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    pub, pub_headers = make_developer(client, superuser_token_headers)
    pub_agent, pub_env = make_agent_with_env(client, pub_headers, "Revoke-Publisher")
    pub_cred = create_random_credential(client, pub_headers, credential_type="api_token")
    update_credential(
        client, pub_headers, pub_cred["id"], service_uri="slot-revoke", allow_sharing=True,
    )
    link_credential_to_agent(client, pub_headers, pub_agent, pub_cred["id"])

    write_skill_with_credentials(
        pub_env, "revoke-skill", [{"slot": "slot-revoke", "type": "api_token"}],
    )
    revision = publish_skill(
        client, pub_headers, pub_agent, "revoke-skill", visibility="public",
    )
    package_uuid = revision["package_id"]

    b, b_headers = make_developer(client, superuser_token_headers)
    b_agent, _ = make_agent_with_env(client, b_headers, "Revoke-Consumer")
    adapter = _install_adapter(patch_environment_adapter)

    install_result = install_skill(client, b_headers, b_agent, package_uuid)
    assert install_result["credential_provisioning"][0]["outcome"] == "linked_publisher"

    # The publisher revokes sharing AFTER install. The shares are deleted AND
    # every recipient agent is unlinked, so the credential leaves the consumer's
    # containers instead of living on behind a link nothing re-checks.
    set_credential_sharing(client, pub_headers, pub_cred["id"], False)

    assert pub_cred["id"] not in {
        cred["id"]
        for cred in get_agent_credentials(client, b_headers, b_agent)["data"]
    }

    row = _addon_row_for_skill(client, b_headers, b_agent, adapter, "revoke-skill")
    assert row["status"] == "warning"
    assert row["credential_issues"] == [
        {"slot": "slot-revoke", "type": "api_token", "reason": "not_linked"}
    ]

    # D1: still never blocks the agent's other channels.
    setup = _setup_status(client, b_headers, b_agent)
    assert setup["status"] == "ready"


def test_sharing_off_through_the_generic_update_revokes_access(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """``PUT /credentials/{id}`` disables sharing with full revocation semantics."""
    pub, pub_headers = make_developer(client, superuser_token_headers)
    pub_agent, pub_env = make_agent_with_env(client, pub_headers, "Flag-Publisher")
    pub_cred = create_random_credential(client, pub_headers, credential_type="api_token")
    update_credential(
        client, pub_headers, pub_cred["id"], service_uri="slot-flag", allow_sharing=True,
    )
    link_credential_to_agent(client, pub_headers, pub_agent, pub_cred["id"])

    write_skill_with_credentials(
        pub_env, "flag-skill", [{"slot": "slot-flag", "type": "api_token"}],
    )
    revision = publish_skill(
        client, pub_headers, pub_agent, "flag-skill", visibility="public",
    )

    b, b_headers = make_developer(client, superuser_token_headers)
    b_agent, _ = make_agent_with_env(client, b_headers, "Flag-Consumer")
    adapter = _install_adapter(patch_environment_adapter)
    install_skill(client, b_headers, b_agent, revision["package_id"])

    update_credential(client, pub_headers, pub_cred["id"], allow_sharing=False)

    assert pub_cred["id"] not in {
        cred["id"]
        for cred in get_agent_credentials(client, b_headers, b_agent)["data"]
    }
    row = _addon_row_for_skill(client, b_headers, b_agent, adapter, "flag-skill")
    assert row["credential_issues"] == [
        {"slot": "slot-flag", "type": "api_token", "reason": "not_linked"}
    ]
    assert _setup_status(client, b_headers, b_agent)["status"] == "ready"


# ---------------------------------------------------------------------------
# Scenario 8: upgrade adds a slot
# ---------------------------------------------------------------------------


def test_upgrade_provisions_only_the_added_slot(
    client: TestClient, superuser_token_headers: dict[str, str],
) -> None:
    pub, pub_headers = make_developer(client, superuser_token_headers)
    pub_agent, pub_env = make_agent_with_env(client, pub_headers, "Upgrade-Publisher")
    write_skill_with_credentials(
        pub_env, "upgrade-skill", [{"slot": "slot-v1", "type": "api_token"}],
    )
    rev1 = publish_skill(
        client, pub_headers, pub_agent, "upgrade-skill", version="1.0", visibility="public",
    )
    package_uuid = rev1["package_id"]

    b, b_headers = make_developer(client, superuser_token_headers)
    b_agent, _ = make_agent_with_env(client, b_headers, "Upgrade-Consumer")
    result = install_skill(client, b_headers, b_agent, package_uuid)
    v1_prov = result["credential_provisioning"][0]
    assert v1_prov["outcome"] == "placeholder_created"
    v1_placeholder_id = v1_prov["credential_id"]

    link_id = list_agent_plugins(client, b_headers, b_agent)[0]["id"]
    # The user explicitly unlinks the slot-v1 placeholder before the upgrade.
    unlink_credential_from_agent(client, b_headers, b_agent, v1_placeholder_id)

    write_skill_with_credentials(
        pub_env, "upgrade-skill",
        [
            {"slot": "slot-v1", "type": "api_token"},
            {"slot": "slot-v2", "type": "api_token"},
        ],
    )
    publish_skill(client, pub_headers, pub_agent, "upgrade-skill", version="2.0")

    upgraded = upgrade_agent_plugin(client, b_headers, b_agent, link_id)
    assert upgraded["success"] is True

    creds = get_agent_credentials(client, b_headers, b_agent)["data"]
    names = {c["name"] for c in creds}
    assert "slot-v2" in names, "the newly added slot must be provisioned"
    assert "slot-v1" not in names, (
        "a slot the user unlinked earlier must not be re-linked on upgrade"
    )


# ---------------------------------------------------------------------------
# Scenario 9: uninstall release semantics
# ---------------------------------------------------------------------------


def test_uninstall_releases_orphan_placeholders_keeps_real_credentials(
    client: TestClient, superuser_token_headers: dict[str, str],
) -> None:
    """
    Sub-case A: a skill with one orphan-slot placeholder and one real linked
    credential -- uninstall deletes the placeholder and keeps the real one.

    Sub-case B: two skills declaring the SAME slot on the same agent share one
    placeholder -- uninstalling one keeps the placeholder alive for the other,
    and only the last uninstall releases it.
    """
    # ── Sub-case A ────────────────────────────────────────────────────
    pub, pub_headers = make_developer(client, superuser_token_headers)
    pub_agent, pub_env = make_agent_with_env(client, pub_headers, "Release-Publisher-A")
    write_skill_with_credentials(
        pub_env, "release-skill-a",
        [
            {"slot": "slot-orphan", "type": "api_token"},
            {"slot": "slot-real", "type": "api_token"},
        ],
    )
    revision_a = publish_skill(
        client, pub_headers, pub_agent, "release-skill-a", visibility="public",
    )
    package_a = revision_a["package_id"]

    b, b_headers = make_developer(client, superuser_token_headers)
    b_agent_a, _ = make_agent_with_env(client, b_headers, "Release-Consumer-A")
    real_cred = create_random_credential(client, b_headers, credential_type="api_token")
    update_credential(client, b_headers, real_cred["id"], service_uri="slot-real")
    # Deliberately NOT linked yet -- install must discover it (linked_existing).

    result_a = install_skill(client, b_headers, b_agent_a, package_a)
    by_slot = {p["slot"]: p for p in result_a["credential_provisioning"]}
    assert by_slot["slot-orphan"]["outcome"] == "placeholder_created"
    assert by_slot["slot-real"]["outcome"] == "linked_existing"
    placeholder_id = by_slot["slot-orphan"]["credential_id"]

    link_id_a = list_agent_plugins(client, b_headers, b_agent_a)[0]["id"]
    uninstall_agent_plugin(client, b_headers, b_agent_a, link_id_a)

    _get_credential_404(client, b_headers, placeholder_id)
    remaining = get_agent_credentials(client, b_headers, b_agent_a)["data"]
    assert real_cred["id"] in [c["id"] for c in remaining], (
        "a real linked credential must survive the skill's uninstall"
    )

    # ── Sub-case B: two skills sharing a slot ──────────────────────────
    pub_agent_b, pub_env_b = make_agent_with_env(client, pub_headers, "Release-Publisher-B")
    write_skill_with_credentials(
        pub_env_b, "shared-slot-a", [{"slot": "shared-slot", "type": "api_token"}],
    )
    rev_shared_a = publish_skill(
        client, pub_headers, pub_agent_b, "shared-slot-a", visibility="public",
    )
    write_skill_with_credentials(
        pub_env_b, "shared-slot-b", [{"slot": "shared-slot", "type": "api_token"}],
    )
    rev_shared_b = publish_skill(
        client, pub_headers, pub_agent_b, "shared-slot-b", visibility="public",
    )

    b_agent_shared, _ = make_agent_with_env(client, b_headers, "Release-Consumer-Shared")
    result_shared_a = install_skill(
        client, b_headers, b_agent_shared, rev_shared_a["package_id"],
    )
    shared_placeholder_id = result_shared_a["credential_provisioning"][0]["credential_id"]
    assert result_shared_a["credential_provisioning"][0]["outcome"] == "placeholder_created"

    result_shared_b = install_skill(
        client, b_headers, b_agent_shared, rev_shared_b["package_id"],
    )
    # The second skill reuses the SAME credential -- it is already linked.
    assert result_shared_b["credential_provisioning"][0]["outcome"] == "already_linked"
    assert result_shared_b["credential_provisioning"][0]["credential_id"] == shared_placeholder_id

    plugins = list_agent_plugins(client, b_headers, b_agent_shared)
    link_shared_a = next(
        p["id"] for p in plugins if p["skill_package_id"] == rev_shared_a["package_id"]
    )
    link_shared_b = next(
        p["id"] for p in plugins if p["skill_package_id"] == rev_shared_b["package_id"]
    )

    uninstall_agent_plugin(client, b_headers, b_agent_shared, link_shared_a)
    get_credential(client, b_headers, shared_placeholder_id)  # still 200: still exists
    still_linked = get_agent_credentials(client, b_headers, b_agent_shared)["data"]
    assert shared_placeholder_id in [c["id"] for c in still_linked], (
        "uninstalling one of two skills sharing a slot must keep the placeholder"
    )

    # The LAST skill needing it releases it.
    uninstall_agent_plugin(client, b_headers, b_agent_shared, link_shared_b)
    _get_credential_404(client, b_headers, shared_placeholder_id)


# ---------------------------------------------------------------------------
# Scenario 10: guards
# ---------------------------------------------------------------------------


def test_install_preview_guards(
    client: TestClient, superuser_token_headers: dict[str, str],
) -> None:
    pub, pub_headers = make_developer(client, superuser_token_headers)
    pub_agent, pub_env = make_agent_with_env(client, pub_headers, "Guard-Publisher")
    write_skill_with_credentials(
        pub_env, "guard-skill", [{"slot": "slot-guard", "type": "api_token"}],
    )
    revision = publish_skill(
        client, pub_headers, pub_agent, "guard-skill", visibility="public",
    )
    package_uuid = revision["package_id"]

    other, other_headers = make_developer(client, superuser_token_headers)
    other_agent, _ = make_agent_with_env(client, other_headers, "Guard-Other")

    # Another user's agent.
    get_skill_install_preview(
        client, other_headers, pub_agent, package_uuid, expected_status=404,
    )

    # An invisible package.
    priv_pub, priv_headers = make_developer(client, superuser_token_headers)
    priv_agent, priv_env = make_agent_with_env(
        client, priv_headers, "Guard-Private-Publisher",
    )
    write_skill_with_credentials(
        priv_env, "private-guard-skill", [{"slot": "slot-guard", "type": "api_token"}],
    )
    priv_revision = publish_skill(
        client, priv_headers, priv_agent, "private-guard-skill", visibility="private",
    )
    get_skill_install_preview(
        client, other_headers, other_agent, priv_revision["package_id"],
        expected_status=404,
    )

    # An unknown revision number.
    get_skill_install_preview(
        client, other_headers, other_agent, package_uuid,
        revision_number=999, expected_status=404,
    )


# ---------------------------------------------------------------------------
# Scenario 9: a group-shared credential keeps the slot satisfied (User Groups P3)
# ---------------------------------------------------------------------------


def test_slot_resolution_sees_a_group_shared_credential(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    1. B (a group member) installs the skill → linked_publisher + direct share
    2. The owner removes the direct share: the group still covers B, so the
       link stays and the slot reads satisfied (no credential_issues)
    3. B is removed from the group → link dropped, slot reads not_linked
    """
    from tests.utils.user_group import create_group, remove_member

    su = superuser_token_headers
    pub, pub_headers = make_developer(client, su)
    pub_agent, pub_env = make_agent_with_env(client, pub_headers, "GroupSlot-Publisher")
    cred = create_random_credential(client, pub_headers, credential_type="api_token")
    update_credential(
        client, pub_headers, cred["id"], service_uri="slot-group", allow_sharing=True,
    )
    link_credential_to_agent(client, pub_headers, pub_agent, cred["id"])
    write_skill_with_credentials(
        pub_env, "group-skill", [{"slot": "slot-group", "type": "api_token"}],
    )
    revision = publish_skill(client, pub_headers, pub_agent, "group-skill", visibility="public")

    b, b_headers = make_developer(client, su)
    b_agent, _ = make_agent_with_env(client, b_headers, "GroupSlot-Consumer")
    adapter = _install_adapter(patch_environment_adapter)
    group = create_group(client, su, member_ids=[b["id"]])
    r = client.post(
        f"{API}/credentials/{cred['id']}/group-shares",
        headers=pub_headers, json={"group_id": group["id"]},
    )
    assert r.status_code == 200, r.text

    result = install_skill(client, b_headers, b_agent, revision["package_id"])
    assert result["credential_provisioning"][0]["outcome"] == "linked_publisher"

    shares = client.get(f"{API}/credentials/{cred['id']}/shares", headers=pub_headers).json()["data"]
    share = next(s for s in shares if s["shared_with_user_id"] == b["id"])
    r = client.delete(f"{API}/credentials/{cred['id']}/shares/{share['id']}", headers=pub_headers)
    assert r.status_code == 200, r.text

    assert cred["id"] in {
        c["id"] for c in get_agent_credentials(client, b_headers, b_agent)["data"]
    }
    row = _addon_row_for_skill(client, b_headers, b_agent, adapter, "group-skill")
    assert row["credential_issues"] == []

    remove_member(client, su, group["id"], b["id"])
    assert cred["id"] not in {
        c["id"] for c in get_agent_credentials(client, b_headers, b_agent)["data"]
    }
    row = _addon_row_for_skill(client, b_headers, b_agent, adapter, "group-skill")
    assert row["credential_issues"] == [
        {"slot": "slot-group", "type": "api_token", "reason": "not_linked"}
    ]
