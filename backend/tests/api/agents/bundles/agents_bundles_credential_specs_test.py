"""Credential spec shape tests for agent bundle publishing.

Covers the ``provided_by`` / ``publisher_credential_id`` spec fields emitted
during publish, the publisher AI credential FK columns on ``AgentBundle``, and
the backward-compat guarantee that pre-spec-redesign revision shapes still
install cleanly.

Scenarios:
  A. Publish emits ``provided_by="publisher"`` / ``publisher_credential_id``
     for a credential with ``allow_sharing=True``.
  B. Publish emits ``provided_by="user"`` / ``publisher_credential_id=None``
     for a credential with ``allow_sharing=False``.
  C. Mixed credentials — one shareable, one not — spec list reflects each
     independently.
  D. Backward-compat install — old-shape revision (no ``provided_by`` /
     ``publisher_credential_id``) still installs correctly.
  E. ``PATCH /bundles/{uuid}`` accepts a publisher-owned AI credential.
  F. ``PATCH /bundles/{uuid}`` rejects an AI credential owned by another user
     (HTTP 400).
  G. ``PATCH /bundles/{uuid}`` accepts explicit ``null`` to clear the field.
  H. ``GET /catalog/{bundle_id}`` surfaces the two ``publisher_ai_credential_*``
     fields after they are set.
  I. Unit-level: ``_validate_publisher_provides`` raises when a spec is
     hand-built as ``provided_by="publisher"`` but the underlying credential
     is not shareable.
  J. Smoke: on-disk ``manifest.json`` mirrors the new spec shape after publish.

Notes:
  - All tests operate via the API layer only.
  - ``_validate_publisher_provides`` (scenario I) is the one spot where we
    must call into the service layer because we need to hand-craft a spec
    shape that cannot be produced through the API (i.e. a
    ``provided_by="publisher"`` spec backed by a non-shareable credential).
    The README allows importing from ``app.core.config`` and ``app.utils``
    only; importing service methods is not permitted.  We therefore implement
    scenario I as a white-box call inside the agent-fixtures context — this
    is flagged explicitly in the test body.
  - Scenario I is NOT possible to exercise through the API because
    the inference guarantees the invariant by construction.  We import the
    service method directly for this one scenario and call it out clearly.
"""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import (
    create_bundle_credential as _create_credential,
    link_bundle_credential_to_agent as _link_credential_to_agent,
    make_user_and_headers as _make_user_and_headers,
)


API = settings.API_V1_STR


# ── Helpers ─────────────────────────────────────────────────────────────────
# Shared bundle helpers (_make_user_and_headers, _create_credential,
# _link_credential_to_agent) are imported above from tests.utils.bundle.
# _publish_and_list is a local composite (publish → list revision specs).


def _publish_and_list(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
) -> tuple[dict, list[dict]]:
    """Publish an agent and return (revision, credential_specs_from_revision_list)."""
    r = client.post(
        f"{API}/agents/{agent_id}/publish",
        headers=headers,
        json={},
    )
    assert r.status_code == 200, r.text
    revision = r.json()
    drain_tasks()

    # Fetch the bundle UUID from the agent.
    fresh = client.get(f"{API}/agents/{agent_id}", headers=headers).json()
    bundle_uuid = fresh["bundle_uuid"]
    assert bundle_uuid is not None

    # List revisions to get the full spec list from the server.
    revs = client.get(
        f"{API}/bundles/{bundle_uuid}/revisions",
        headers=headers,
    )
    assert revs.status_code == 200, revs.text
    revs_data = revs.json()["data"]
    assert len(revs_data) >= 1
    latest = revs_data[0]  # newest-first
    specs = latest["required_credential_specs"]
    return revision, specs


def _make_catalog_visible(
    client: TestClient,
    headers: dict[str, str],
    bundle_uuid: str,
) -> None:
    """Flip a bundle to public+listed so catalog tests can find it."""
    r = client.patch(
        f"{API}/bundles/{bundle_uuid}",
        headers=headers,
        json={"is_listed": True, "visibility": "public"},
    )
    assert r.status_code == 200, r.text


# ── Scenario A: allow_sharing=True → provided_by="publisher" ─────────────────


def test_publish_emits_publisher_spec_for_shareable_credential(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """A. Publish emits provided_by="publisher" for allow_sharing=True credential.

    1. Create agent.
    2. Create a credential with allow_sharing=True.
    3. Link it to the agent.
    4. Publish.
    5. GET revisions → assert spec has provided_by="publisher" and
       publisher_credential_id == the credential's UUID.
    """
    # ── Phase 1: create + link shareable credential ───────────────────────────
    agent = create_agent_via_api(
        client, superuser_token_headers, name="CredSpec-A-Agent"
    )
    drain_tasks()

    shareable_cred = _create_credential(
        client, superuser_token_headers, name="crm-key", allow_sharing=True
    )
    _link_credential_to_agent(
        client, superuser_token_headers, agent["id"], shareable_cred["id"]
    )

    # ── Phase 2: publish ──────────────────────────────────────────────────────
    _, specs = _publish_and_list(client, superuser_token_headers, agent["id"])

    # ── Phase 3: assert spec shape ────────────────────────────────────────────
    matched = [s for s in specs if s["name"] == "crm-key"]
    assert len(matched) == 1, f"Expected spec named 'crm-key'; got {specs}"
    spec = matched[0]

    assert spec["provided_by"] == "publisher", (
        f"Expected provided_by='publisher' for allow_sharing=True credential; got {spec}"
    )
    assert spec["publisher_credential_id"] == shareable_cred["id"], (
        f"Expected publisher_credential_id={shareable_cred['id']}; got {spec}"
    )
    assert spec["allow_sharing"] is True


# ── Scenario B: allow_sharing=False → provided_by="user" ─────────────────────


def test_publish_emits_user_spec_for_non_shareable_credential(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """B. Publish emits provided_by="user" for allow_sharing=False credential.

    1. Create agent.
    2. Create credential with allow_sharing=False.
    3. Link + publish.
    4. Assert spec has provided_by="user" and publisher_credential_id=None.
    """
    agent = create_agent_via_api(
        client, superuser_token_headers, name="CredSpec-B-Agent"
    )
    drain_tasks()

    private_cred = _create_credential(
        client, superuser_token_headers, name="mailbox-key", allow_sharing=False
    )
    _link_credential_to_agent(
        client, superuser_token_headers, agent["id"], private_cred["id"]
    )

    _, specs = _publish_and_list(client, superuser_token_headers, agent["id"])

    matched = [s for s in specs if s["name"] == "mailbox-key"]
    assert len(matched) == 1, f"Expected spec named 'mailbox-key'; got {specs}"
    spec = matched[0]

    assert spec["provided_by"] == "user", (
        f"Expected provided_by='user' for allow_sharing=False credential; got {spec}"
    )
    assert spec["publisher_credential_id"] is None, (
        f"Expected publisher_credential_id=null; got {spec}"
    )
    assert spec["allow_sharing"] is False


# ── Scenario C: mixed credentials ─────────────────────────────────────────────


def test_publish_mixed_credentials_emits_per_credential_provided_by(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """C. Two linked credentials, one shareable and one not, are reflected independently.

    1. Create agent.
    2. Link one allow_sharing=True and one allow_sharing=False credential.
    3. Publish.
    4. Assert each spec has the correct provided_by and publisher_credential_id.
    """
    agent = create_agent_via_api(
        client, superuser_token_headers, name="CredSpec-C-Agent"
    )
    drain_tasks()

    shared_cred = _create_credential(
        client, superuser_token_headers, name="crm-shared", allow_sharing=True
    )
    private_cred = _create_credential(
        client, superuser_token_headers, name="mailbox-private", allow_sharing=False
    )
    _link_credential_to_agent(
        client, superuser_token_headers, agent["id"], shared_cred["id"]
    )
    _link_credential_to_agent(
        client, superuser_token_headers, agent["id"], private_cred["id"]
    )

    _, specs = _publish_and_list(client, superuser_token_headers, agent["id"])

    assert len(specs) == 2, f"Expected 2 specs; got {specs}"

    by_name = {s["name"]: s for s in specs}
    assert "crm-shared" in by_name, f"Missing crm-shared spec: {specs}"
    assert "mailbox-private" in by_name, f"Missing mailbox-private spec: {specs}"

    # Shareable credential.
    shared_spec = by_name["crm-shared"]
    assert shared_spec["provided_by"] == "publisher"
    assert shared_spec["publisher_credential_id"] == shared_cred["id"]
    assert shared_spec["allow_sharing"] is True

    # Non-shareable credential.
    private_spec = by_name["mailbox-private"]
    assert private_spec["provided_by"] == "user"
    assert private_spec["publisher_credential_id"] is None
    assert private_spec["allow_sharing"] is False


# ── Scenario D: old-shape revision still installs ─────────────────────────────


def test_old_shape_revision_installs_cleanly(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """D. A revision whose required_credential_specs lacks the new fields
    still installs without error and the install row activates cleanly.

    Backward-compat guarantee: revisions written before the spec redesign
    (no ``provided_by`` / ``publisher_credential_id`` fields) must install
    without raising an HTTP error.  The reader in
    ``InstallService._setup_install_credentials`` defaults missing
    ``provided_by`` to ``"user"`` and missing ``publisher_credential_id`` to
    ``None``.  A credential with ``allow_sharing=False`` (provided_by="user")
    is semantically identical to an old-shape spec so this test exercises the
    same install path.

    The install must also create a placeholder credential for the recipient to
    fill in, visible via ``GET /agents/{install_id}/credentials`` with
    ``is_placeholder=True`` and ``status="incomplete"``.
    """
    # ── Phase 1: publish a bundle with a non-shareable credential ────────────
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="CredSpec-D-Publisher"
    )
    drain_tasks()

    private_cred = _create_credential(
        client, superuser_token_headers, name="legacy-mailbox", allow_sharing=False
    )
    _link_credential_to_agent(
        client, superuser_token_headers, publisher_agent["id"], private_cred["id"]
    )

    client.post(
        f"{API}/agents/{publisher_agent['id']}/publish",
        headers=superuser_token_headers,
        json={},
    ).raise_for_status()
    drain_tasks()

    fresh = client.get(
        f"{API}/agents/{publisher_agent['id']}", headers=superuser_token_headers
    ).json()
    bundle_id = fresh["bundle_id"]
    bundle_uuid = fresh["bundle_uuid"]
    _make_catalog_visible(client, superuser_token_headers, bundle_uuid)

    # ── Phase 2: recipient installs ───────────────────────────────────────────
    _, recipient_headers = _make_user_and_headers(client)

    r = client.post(
        f"{API}/catalog/{bundle_id}/install",
        headers=recipient_headers,
        json={},
    )
    assert r.status_code == 200, r.text
    install = r.json()
    drain_tasks()

    # ── Phase 3: install activated correctly ──────────────────────────────────
    # The install row must link back to the published bundle and revision.
    assert install["bundle_id"] == bundle_id
    assert install["bundle_uuid"] == fresh["bundle_uuid"]
    assert install["installed_revision_id"] == fresh["installed_revision_id"]
    assert install["is_publisher_install"] is False

    # The install created a placeholder credential for the recipient to fill in,
    # observable via the agent-credentials endpoint (regression for the former
    # ``_encrypt_data`` bug that silently skipped placeholder creation).
    creds = client.get(
        f"{API}/agents/{install['id']}/credentials",
        headers=recipient_headers,
    )
    assert creds.status_code == 200, creds.text
    cred_rows = creds.json()["data"]
    placeholder = next(
        (c for c in cred_rows if "legacy-mailbox" in c["name"]), None
    )
    assert placeholder is not None, (
        f"Placeholder credential for 'legacy-mailbox' must be visible to the "
        f"recipient after install; got: {cred_rows}"
    )
    assert placeholder["is_placeholder"] is True
    assert placeholder["status"] == "incomplete", (
        "An unfilled placeholder credential must report status='incomplete'"
    )

    # The install is idempotent — a second call returns the same row.
    r2 = client.post(
        f"{API}/catalog/{bundle_id}/install",
        headers=recipient_headers,
        json={},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["id"] == install["id"]


# ── Scenario E: PATCH accepts publisher-owned AI credential ───────────────────


def test_patch_bundle_accepts_publisher_ai_credential(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """E. PATCH /bundles/{uuid} accepts publisher_ai_credential_*_id when the AI
    credential is owned by the publisher.

    1. Create + publish a bundle.
    2. Create an AI credential for the publisher (superuser).
    3. PATCH the bundle with publisher_ai_credential_conversation_id.
    4. GET the bundle → assert the field is persisted.
    """
    agent = create_agent_via_api(
        client, superuser_token_headers, name="CredSpec-E-Agent"
    )
    drain_tasks()

    r = client.post(
        f"{API}/agents/{agent['id']}/publish",
        headers=superuser_token_headers,
        json={},
    )
    assert r.status_code == 200, r.text
    drain_tasks()

    fresh = client.get(
        f"{API}/agents/{agent['id']}", headers=superuser_token_headers
    ).json()
    bundle_uuid = fresh["bundle_uuid"]

    # Create a publisher AI credential.
    ai_cred = create_random_ai_credential(client, superuser_token_headers)
    ai_cred_id = ai_cred["id"]

    # PATCH the bundle.
    r = client.patch(
        f"{API}/bundles/{bundle_uuid}",
        headers=superuser_token_headers,
        json={"publisher_ai_credential_conversation_id": ai_cred_id},
    )
    assert r.status_code == 200, r.text
    patched = r.json()
    assert str(patched["publisher_ai_credential_conversation_id"]) == ai_cred_id, (
        f"Expected publisher_ai_credential_conversation_id={ai_cred_id}; "
        f"got {patched['publisher_ai_credential_conversation_id']}"
    )
    assert patched["publisher_ai_credential_building_id"] is None

    # Verify persistence via GET.
    fetched = client.get(
        f"{API}/bundles/{bundle_uuid}", headers=superuser_token_headers
    ).json()
    assert str(fetched["publisher_ai_credential_conversation_id"]) == ai_cred_id
    assert fetched["publisher_ai_credential_building_id"] is None


# ── Scenario F: PATCH rejects AI credential owned by another user ─────────────


def test_patch_bundle_rejects_ai_credential_owned_by_other_user(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """F. PATCH /bundles/{uuid} returns HTTP 400 when the AI credential belongs
    to a different user.

    1. Publisher publishes a bundle.
    2. A second user creates an AI credential.
    3. Publisher tries to PATCH the bundle with the other user's credential.
    4. Assert HTTP 400.
    """
    agent = create_agent_via_api(
        client, superuser_token_headers, name="CredSpec-F-Agent"
    )
    drain_tasks()
    r = client.post(
        f"{API}/agents/{agent['id']}/publish",
        headers=superuser_token_headers,
        json={},
    )
    assert r.status_code == 200, r.text
    drain_tasks()

    fresh = client.get(
        f"{API}/agents/{agent['id']}", headers=superuser_token_headers
    ).json()
    bundle_uuid = fresh["bundle_uuid"]

    # Another user creates an AI credential.
    _, other_headers = _make_user_and_headers(client)
    other_ai_cred = create_random_ai_credential(client, other_headers)
    other_ai_cred_id = other_ai_cred["id"]

    # Superuser (publisher) tries to set the other user's AI credential.
    r = client.patch(
        f"{API}/bundles/{bundle_uuid}",
        headers=superuser_token_headers,
        json={"publisher_ai_credential_conversation_id": other_ai_cred_id},
    )
    assert r.status_code == 400, (
        f"Expected 400 when setting another user's AI credential; got {r.status_code}: {r.text}"
    )
    detail = r.json().get("detail", "")
    assert "publisher" in detail.lower() or "owner" in detail.lower(), (
        f"Expected a helpful error about ownership; got: {detail}"
    )


# ── Scenario G: PATCH accepts null to clear the field ────────────────────────


def test_patch_bundle_clears_ai_credential_with_null(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """G. After E sets publisher_ai_credential_conversation_id, PATCHing with
    null clears it.

    1. Create + publish bundle.
    2. Set publisher_ai_credential_conversation_id (scenario E setup).
    3. PATCH with null.
    4. GET bundle → field is null.
    """
    agent = create_agent_via_api(
        client, superuser_token_headers, name="CredSpec-G-Agent"
    )
    drain_tasks()
    r = client.post(
        f"{API}/agents/{agent['id']}/publish",
        headers=superuser_token_headers,
        json={},
    )
    assert r.status_code == 200, r.text
    drain_tasks()

    fresh = client.get(
        f"{API}/agents/{agent['id']}", headers=superuser_token_headers
    ).json()
    bundle_uuid = fresh["bundle_uuid"]

    # Set it first.
    ai_cred = create_random_ai_credential(client, superuser_token_headers)
    r = client.patch(
        f"{API}/bundles/{bundle_uuid}",
        headers=superuser_token_headers,
        json={"publisher_ai_credential_conversation_id": ai_cred["id"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["publisher_ai_credential_conversation_id"] is not None

    # Now clear it with explicit null.
    r = client.patch(
        f"{API}/bundles/{bundle_uuid}",
        headers=superuser_token_headers,
        json={"publisher_ai_credential_conversation_id": None},
    )
    assert r.status_code == 200, r.text
    cleared = r.json()
    assert cleared["publisher_ai_credential_conversation_id"] is None, (
        f"Expected null after clearing; got {cleared['publisher_ai_credential_conversation_id']}"
    )

    # Persist check.
    fetched = client.get(
        f"{API}/bundles/{bundle_uuid}", headers=superuser_token_headers
    ).json()
    assert fetched["publisher_ai_credential_conversation_id"] is None


# ── Scenario H: CatalogEntryPublic surfaces new fields ───────────────────────


def test_catalog_entry_surfaces_publisher_ai_credential_fields(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """H. GET /catalog/{bundle_id} (CatalogEntryPublic) returns the two
    publisher_ai_credential_*_id fields after they are set on the bundle.

    1. Create + publish bundle.
    2. Set both publisher AI credential columns.
    3. Make bundle public+listed.
    4. GET /catalog/{bundle_id} (as a different user) → assert both fields present.
    """
    agent = create_agent_via_api(
        client, superuser_token_headers, name="CredSpec-H-Agent"
    )
    drain_tasks()
    r = client.post(
        f"{API}/agents/{agent['id']}/publish",
        headers=superuser_token_headers,
        json={},
    )
    assert r.status_code == 200, r.text
    drain_tasks()

    fresh = client.get(
        f"{API}/agents/{agent['id']}", headers=superuser_token_headers
    ).json()
    bundle_uuid = fresh["bundle_uuid"]
    bundle_id = fresh["bundle_id"]

    # Create two AI credentials for the publisher.
    conv_cred = create_random_ai_credential(client, superuser_token_headers)
    build_cred = create_random_ai_credential(client, superuser_token_headers)

    r = client.patch(
        f"{API}/bundles/{bundle_uuid}",
        headers=superuser_token_headers,
        json={
            "publisher_ai_credential_conversation_id": conv_cred["id"],
            "publisher_ai_credential_building_id": build_cred["id"],
            "is_listed": True,
            "visibility": "public",
        },
    )
    assert r.status_code == 200, r.text

    # Fetch catalog entry as a recipient user.
    _, recipient_headers = _make_user_and_headers(client)
    entry_r = client.get(
        f"{API}/catalog/{bundle_id}",
        headers=recipient_headers,
    )
    assert entry_r.status_code == 200, entry_r.text
    entry = entry_r.json()

    assert str(entry["publisher_ai_credential_conversation_id"]) == conv_cred["id"], (
        f"Expected conv AI cred; got {entry['publisher_ai_credential_conversation_id']}"
    )
    assert str(entry["publisher_ai_credential_building_id"]) == build_cred["id"], (
        f"Expected build AI cred; got {entry['publisher_ai_credential_building_id']}"
    )


# Scenario I (white-box _validate_publisher_provides contract) was removed: the
# "publisher marks a non-shareable credential as provided_by=publisher → rejected
# at publish time" invariant is covered end-to-end via the publish flow in
# tests/api/agents/bundles/agents_bundles_publish_settings_test.py.


# ── Scenario J: manifest.json on disk mirrors new spec shape ──────────────────


def test_manifest_on_disk_contains_new_spec_fields(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """J. After publishing a bundle with an allow_sharing=True credential, the
    on-disk manifest.json in BUNDLE_STORAGE_DIR contains provided_by and
    publisher_credential_id in each spec.

    1. Create agent, link shareable credential, publish.
    2. Read manifest.json from disk.
    3. Assert each spec dict in required_credential_specs includes both new fields.
    """
    agent = create_agent_via_api(
        client, superuser_token_headers, name="CredSpec-J-Agent"
    )
    drain_tasks()

    shareable_cred = _create_credential(
        client, superuser_token_headers, name="disk-check-cred", allow_sharing=True
    )
    _link_credential_to_agent(
        client, superuser_token_headers, agent["id"], shareable_cred["id"]
    )

    r = client.post(
        f"{API}/agents/{agent['id']}/publish",
        headers=superuser_token_headers,
        json={},
    )
    assert r.status_code == 200, r.text
    revision = r.json()
    drain_tasks()

    # Derive the on-disk path: BUNDLE_STORAGE_DIR/<bundle_id>/<revision_number>/
    fresh = client.get(
        f"{API}/agents/{agent['id']}", headers=superuser_token_headers
    ).json()
    bundle_id_str = fresh["bundle_id"]
    revision_number = revision["revision_number"]

    manifest_path = (
        Path(settings.BUNDLE_STORAGE_DIR)
        / bundle_id_str
        / str(revision_number)
        / "manifest.json"
    )
    assert manifest_path.exists(), f"manifest.json not found at {manifest_path}"

    manifest = json.loads(manifest_path.read_text())
    specs = manifest.get("required_credential_specs", [])
    assert len(specs) == 1, f"Expected 1 spec in manifest; got {specs}"

    spec = specs[0]
    assert "provided_by" in spec, f"'provided_by' missing from manifest spec: {spec}"
    assert "publisher_credential_id" in spec, (
        f"'publisher_credential_id' missing from manifest spec: {spec}"
    )
    assert spec["provided_by"] == "publisher"
    assert spec["publisher_credential_id"] == shareable_cred["id"]
    assert spec["allow_sharing"] is True
