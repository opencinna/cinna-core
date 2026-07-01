"""Tests for the publish-time bundle display_name/description refresh fix.

Bug: ``PublishService._publish_locked``'s step-5 "Update bundle metadata"
block used to guard the assignment with ``if display_name:`` —

    if display_name:
        bundle.display_name = display_name
        bundle.description = description

so a re-publish that did NOT pass an explicit ``display_name`` override (the
normal "Publish" button flow) never refreshed ``AgentBundle.display_name``
from the publisher install's current ``Agent.name``. If the publisher
renamed their agent and republished, the catalog entry and any brand-new
install kept showing the stale name forever.

Fix: every publish now unconditionally refreshes the bundle row from the
publisher install (mirroring the first-publish-only fallback that already
existed):

    bundle.display_name = display_name or install.name
    bundle.description = description if description is not None else install.description

Coverage (single end-to-end scenario):
  1. First publish still derives ``bundle.display_name`` from the agent's
     name when no explicit override is passed (pre-existing behavior,
     guarded against regression).
  2. A consumer installs before the rename — gets the original name.
  3. Publisher renames the agent (``PUT /agents/{id}``) and republishes
     (still no explicit ``display_name``/``description`` override) — the
     bundle's ``display_name``/``description`` (visible via both
     ``GET /bundles/{uuid}`` and ``GET /catalog/{bundle_id}``) now reflect
     the new name/description. This is the regression the fix targets.
  4. A brand-new consumer installing AFTER the republish gets the new name
     (``InstallService._install_from_revision`` seeds
     ``name=bundle.display_name``).
  5. The pre-existing (foreign) install does NOT get silently renamed by
     ``apply-update`` — ``_apply_revision_metadata`` does not carry ``name``
     in its field set (the revision snapshot doesn't capture it), so the
     old install keeps its original ``Agent.name`` even after applying the
     latest revision. Its ``description`` (which IS part of
     ``_apply_revision_metadata``) does get refreshed, to make the
     name-is-special-cased distinction explicit rather than implied.
"""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api, get_agent, update_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import (
    install_bundle,
    make_bundle_public,
    make_user_and_headers,
)

API = settings.API_V1_STR


def _get_bundle(client: TestClient, headers: dict, bundle_uuid: str) -> dict:
    r = client.get(f"{API}/bundles/{bundle_uuid}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _get_catalog_entry(client: TestClient, headers: dict, bundle_id: str) -> dict:
    r = client.get(f"{API}/catalog/{bundle_id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _publish(
    client: TestClient,
    headers: dict,
    agent_id: str,
    *,
    notes: str | None = None,
) -> dict:
    """Publish WITHOUT an explicit display_name/description override.

    This is the normal "Publish" button flow and the exact path the bug
    affected — an override would have masked the ``if display_name:`` guard
    entirely, so deliberately exercising the no-override path is the point.
    """
    r = client.post(
        f"{API}/agents/{agent_id}/publish",
        headers=headers,
        json={"release_notes": notes} if notes else {},
    )
    assert r.status_code == 200, f"Publish failed: {r.text}"
    revision = r.json()
    drain_tasks()
    return revision


def _apply_update(client: TestClient, headers: dict, agent_id: str) -> dict:
    r = client.post(f"{API}/agents/{agent_id}/apply-update", headers=headers)
    assert r.status_code == 200, f"apply-update failed: {r.text}"
    drain_tasks()
    return r.json()


def test_republish_after_rename_refreshes_display_name_without_renaming_existing_installs(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    headers = superuser_token_headers

    # ── Phase 1: First publish derives display_name from the agent name ───────
    original_name = "Acme Original Agent"
    pub_agent = create_agent_via_api(client, headers, name=original_name)
    drain_tasks()
    pub_id = pub_agent["id"]

    revision_1 = _publish(client, headers, pub_id, notes="v1")
    assert revision_1["revision_number"] == 1

    pub_fresh = get_agent(client, headers, pub_id)
    bundle_id = pub_fresh["bundle_id"]
    bundle_uuid = pub_fresh["bundle_uuid"]
    assert bundle_uuid is not None, "First publish must create the bundle row"

    bundle_v1 = _get_bundle(client, headers, bundle_uuid)
    assert bundle_v1["display_name"] == original_name, (
        "First publish must derive display_name from the agent's name "
        f"when no override is passed; got {bundle_v1['display_name']!r}"
    )

    # ── Phase 2: Make bundle public; a consumer installs BEFORE the rename ────
    make_bundle_public(client, headers, bundle_uuid)

    _, consumer_headers = make_user_and_headers(client)
    install_before = install_bundle(client, consumer_headers, bundle_id)
    before_id = install_before["id"]
    assert install_before["name"] == original_name
    assert install_before["installed_revision_number"] == 1

    # ── Phase 3: Publisher renames the agent + edits description ──────────────
    renamed = "Acme Renamed Agent"
    new_description = "A substantially rewritten description"
    update_agent(
        client, headers, pub_id,
        name=renamed, description=new_description,
    )
    pub_renamed = get_agent(client, headers, pub_id)
    assert pub_renamed["name"] == renamed
    assert pub_renamed["description"] == new_description

    # ── Phase 4: Republish (still no display_name/description override) ───────
    revision_2 = _publish(client, headers, pub_id, notes="v2 — renamed")
    assert revision_2["revision_number"] == 2

    bundle_v2 = _get_bundle(client, headers, bundle_uuid)
    assert bundle_v2["display_name"] == renamed, (
        "BUG REGRESSION: republish after a rename must refresh "
        f"bundle.display_name; got {bundle_v2['display_name']!r}, "
        f"expected {renamed!r}"
    )
    assert bundle_v2["description"] == new_description, (
        "BUG REGRESSION: republish after a description edit must refresh "
        f"bundle.description; got {bundle_v2['description']!r}"
    )

    # Catalog projection (what a browsing user sees) must agree.
    catalog_entry = _get_catalog_entry(client, consumer_headers, bundle_id)
    assert catalog_entry["display_name"] == renamed, (
        f"Catalog entry still shows the stale name: {catalog_entry['display_name']!r}"
    )
    assert catalog_entry["description"] == new_description

    # ── Phase 5: A brand-new install AFTER the republish gets the new name ────
    _, new_consumer_headers = make_user_and_headers(client)
    install_after = install_bundle(client, new_consumer_headers, bundle_id)
    assert install_after["name"] == renamed, (
        "New install must be seeded with the bundle's refreshed display_name "
        f"(InstallService seeds name=bundle.display_name); got "
        f"{install_after['name']!r}"
    )
    assert install_after["installed_revision_number"] == 2

    # ── Phase 6: Pre-existing install is NOT silently renamed by apply-update ─
    before_pending = get_agent(client, consumer_headers, before_id)
    assert before_pending["pending_update"] is True
    assert before_pending["name"] == original_name, (
        "Sanity: install must still carry its original name before applying "
        "the update"
    )

    updated = _apply_update(client, consumer_headers, before_id)
    assert updated["installed_revision_number"] == 2, (
        "apply-update must move the install onto the new revision"
    )
    assert updated["name"] == original_name, (
        "apply-update must NOT rename a pre-existing foreign install — the "
        "revision snapshot does not carry 'name', so "
        "_apply_revision_metadata must leave Agent.name untouched even "
        "though the publisher renamed + republished"
    )
    # Description IS part of _apply_revision_metadata's field set (unlike
    # name), so it DOES get refreshed — asserting this makes the
    # name-is-special-cased distinction explicit rather than merely implied.
    assert updated["description"] == new_description, (
        "description is expected to be revision-synced by apply-update "
        "(unlike name)"
    )

    # Re-fetch via GET to confirm the persisted row (not just the apply-update
    # response) reflects the same name-preserved / description-synced split.
    before_after_update = get_agent(client, consumer_headers, before_id)
    assert before_after_update["name"] == original_name
    assert before_after_update["description"] == new_description
    assert before_after_update["pending_update"] is False
