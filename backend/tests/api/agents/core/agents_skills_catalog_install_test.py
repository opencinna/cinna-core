"""Skills catalog (Phase 3) — installing a catalog skill into an agent.

Scenarios:
  1. Install → upgrade → uninstall, end to end: a ``source=catalog`` plugin
     link, the archive coordinates that reach the container in the manifest,
     the ``has_update`` signal when the publisher cuts a new revision, the
     re-pin on upgrade, and the prune when the link goes away.
  2. ``install_count`` is a projection, not a column: it counts DISTINCT
     consumer agents and excludes the publisher's own installs, and it drops
     when an install disappears with its agent.
  3. ``already_installed`` (the same package twice) and ``name_conflict`` (a
     DIFFERENT publisher's package of the same skill name) are two different
     409s — an agent has one ``plugins/cinna-skills/<name>/`` directory, so the
     refusal has to name the incumbent package rather than claim this one is
     already there.
  4. ``resolve_revision``: no revision number means "the latest at install
     time", an explicit one pins that revision, and an unknown one is a 404.
  5. The archive route: served to an environment of an agent that holds a link
     for THAT revision, refused (403) to one that does not, and 410 once the
     snapshot is gone.
  6. A container-side failure (checksum mismatch) surfaces as
     ``partial_failures`` with a ``failed`` plugin result — the op itself still
     succeeds, which is what drives the amber banner rather than an error page.
  7. Bundle publish of an agent holding a catalog skill snapshots it as an
     ordinary bundle plugin, and a consumer installing that bundle receives it
     as ``source=bundle``. This is also the regression guard for
     ``_resolve_link_identity`` testing ``!= marketplace`` rather than
     ``== bundle``: the ``== bundle`` form sends a catalog link down the
     marketplace branch, where it resolves to nothing and hard-blocks publish.

Test seam:
  ``patch_environment_adapter`` (the agent-domain lifecycle-manager fixture) is
  re-pointed at one ``EnvironmentTestAdapter`` the test owns, so the manifest
  that would reach a container is readable as ``adapter.plugins_set``. The
  container's own handling of that manifest is covered in
  ``tests/unit/test_skill_catalog_container_install.py``.
"""
import hashlib
import shutil
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.agent_env import create_env_with_token
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import install_bundle, make_bundle_public
from tests.utils.environment import list_environments
from tests.utils.skill_catalog import (
    entry_for,
    error_code,
    install_skill,
    list_agent_plugins,
    list_skill_catalog,
    make_agent_with_env,
    make_developer,
    patched_skill_storage,
    publish_skill,
    uninstall_agent_plugin,
    upgrade_agent_plugin,
    workspace_root,
    write_skill,
)

API = settings.API_V1_STR
CATALOG_MARKETPLACE = "cinna-skills"


@pytest.fixture(autouse=True)
def skill_storage(tmp_path: Path):
    """Redirect ``SKILL_STORAGE_DIR`` at a tmp tree for every test here."""
    with patched_skill_storage(tmp_path / "skill-storage") as root:
        yield root


# ── Helpers ────────────────────────────────────────────────────────────────


def _capture_adapter(lifecycle_manager) -> EnvironmentTestAdapter:
    """Route every ``get_adapter`` call at one adapter the test can inspect."""
    adapter = EnvironmentTestAdapter()
    lifecycle_manager.get_adapter = lambda env: adapter
    return adapter


def _publish_package(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    *,
    skill: str = "pdf-report",
    agent_name: str = "Publisher",
    version: str = "1.0",
    visibility: str = "public",
) -> tuple[dict, dict[str, str], str, str, dict, dict]:
    """A developer who has published ``skill`` publicly.

    Returns ``(user, headers, agent_id, env_id, revision, entry)``.
    """
    user, headers = make_developer(client, superuser_token_headers)
    agent_id, env_id = make_agent_with_env(client, headers, agent_name)
    write_skill(env_id, skill, description=f"Publishes {skill}.")
    revision = publish_skill(
        client, headers, agent_id, skill, version=version, visibility=visibility
    )
    entry = next(
        e
        for e in list_skill_catalog(client, headers)
        if e["latest_revision_id"] == revision["id"]
    )
    return user, headers, agent_id, env_id, revision, entry


def _catalog_manifest_entry(adapter: EnvironmentTestAdapter, name: str) -> dict:
    """The one manifest entry for ``cinna-skills/<name>`` last pushed."""
    entries = [
        e
        for e in (adapter.plugins_set.get("plugins") or [])
        if e.get("marketplace_name") == CATALOG_MARKETPLACE
        and e.get("plugin_name") == name
    ]
    assert len(entries) == 1, adapter.plugins_set
    return entries[0]


# ── Scenario 1: install → upgrade → uninstall ──────────────────────────────


def test_install_upgrade_and_uninstall_a_catalog_skill(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    The full consumer lifecycle:
      1. A publisher publishes ``pdf-report`` publicly (revision 1).
      2. A consumer installs it → a ``source=catalog`` link with no
         ``plugin_id``, under the synthetic ``cinna-skills`` marketplace.
      3. The manifest pushed to the environment carries archive coordinates —
         URL, sha256 and the revision id as the idempotency ref — and no git.
      4. The plugins list projects the package's identity, ``skill_package_id``
         and ``has_update=False``.
      5. The publisher cuts revision 2 → ``has_update`` flips and
         ``latest_version`` names the new one, without the link moving.
      6. Upgrade re-pins the link and the manifest re-fetches a different
         archive.
      7. Uninstall removes the row, and the next manifest no longer mentions
         the skill — which is what makes the container prune its directory.
    """
    # ── Phase 1: a published package ─────────────────────────────────────
    _pub, pub_headers, pub_agent, pub_env, revision1, entry = _publish_package(
        client, superuser_token_headers, agent_name="Catalog-Publisher"
    )
    package_uuid = entry["id"]

    # ── Phase 2: a consumer installs it ──────────────────────────────────
    _consumer, con_headers = make_developer(client, superuser_token_headers)
    con_agent, _con_env = make_agent_with_env(client, con_headers, "Consumer")
    adapter = _capture_adapter(patch_environment_adapter)

    response = install_skill(client, con_headers, con_agent, package_uuid)
    assert response["success"] is True
    assert response["partial_failures"] is False
    assert response["message"].startswith("Skill added.")
    link_public = response["plugin_link"]
    assert link_public["source"] == "catalog"
    assert link_public["plugin_id"] is None
    assert link_public["snapshot_marketplace_name"] == CATALOG_MARKETPLACE
    assert link_public["snapshot_plugin_name"] == "pdf-report"
    assert link_public["installed_version"] == "1.0"
    assert link_public["installed_commit_hash"] is None
    assert link_public["skill_package_revision_id"] == revision1["id"], (
        "the install response is the one place that names the revision just "
        "pinned — the projection must not drop it"
    )

    # ── Phase 3: what would reach the container ──────────────────────────
    manifest_entry = _catalog_manifest_entry(adapter, "pdf-report")
    assert manifest_entry["source"] == "catalog"
    assert manifest_entry["git"] is None
    assert manifest_entry["commit_hash"] is None
    archive = manifest_entry["archive"]
    assert archive is not None
    assert archive["url"].endswith(
        f"/api/v1/skills/packages/{package_uuid}/revisions/1/archive"
    )
    assert len(archive["sha256"]) == 64
    assert archive["ref"] == revision1["id"]

    # ── Phase 4: the row the Plugins tab renders ─────────────────────────
    plugins = list_agent_plugins(client, con_headers, con_agent)
    assert len(plugins) == 1
    row = plugins[0]
    link_id = row["id"]
    assert row["source"] == "catalog"
    assert row["skill_package_id"] == package_uuid
    assert row["skill_package_revision_id"] == revision1["id"]
    assert row["plugin_name"] == entry["display_name"]
    assert row["plugin_category"] == "skill"
    assert row["marketplace_name"] == CATALOG_MARKETPLACE
    assert row["has_update"] is False
    assert row["latest_version"] == "1.0"

    # ── Phase 5: the publisher cuts revision 2 ───────────────────────────
    write_skill(
        pub_env, "pdf-report", description="Publishes pdf-report.", body="v2 body"
    )
    revision2 = publish_skill(
        client, pub_headers, pub_agent, "pdf-report", version="2.0"
    )
    row = list_agent_plugins(client, con_headers, con_agent)[0]
    assert row["has_update"] is True
    assert row["latest_version"] == "2.0"
    assert row["skill_package_revision_id"] == revision1["id"], (
        "an available update must not move the pinned revision on its own"
    )

    # ── Phase 6: upgrade re-pins and re-fetches ──────────────────────────
    upgraded = upgrade_agent_plugin(client, con_headers, con_agent, link_id)
    assert upgraded["success"] is True
    assert upgraded["plugin_link"]["skill_package_revision_id"] == revision2["id"]
    row = list_agent_plugins(client, con_headers, con_agent)[0]
    assert row["skill_package_revision_id"] == revision2["id"]
    assert row["installed_version"] == "2.0"
    assert row["has_update"] is False

    upgraded_entry = _catalog_manifest_entry(adapter, "pdf-report")
    assert upgraded_entry["archive"]["ref"] == revision2["id"]
    assert upgraded_entry["archive"]["url"].endswith("/revisions/2/archive")
    assert upgraded_entry["archive"]["sha256"] != archive["sha256"]

    # A second upgrade is a no-op, not an error.
    upgrade_agent_plugin(client, con_headers, con_agent, link_id)
    assert (
        list_agent_plugins(client, con_headers, con_agent)[0][
            "skill_package_revision_id"
        ]
        == revision2["id"]
    )

    # ── Phase 7: uninstall prunes ────────────────────────────────────────
    uninstall_agent_plugin(client, con_headers, con_agent, link_id)
    assert list_agent_plugins(client, con_headers, con_agent) == []
    assert [
        e
        for e in (adapter.plugins_set.get("plugins") or [])
        if e.get("marketplace_name") == CATALOG_MARKETPLACE
    ] == [], "the pruned skill must be absent from the next manifest"


# ── Scenario 2: install_count is computed, publisher excluded ──────────────


def test_install_count_counts_distinct_consumer_agents_only(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    ``install_count`` is derived at projection time, not stored:
      1. The publisher installs their own skill into two of their agents →
         the count stays 0 (dogfooding must not inflate the catalog).
      2. Two consumer agents owned by one consumer → 2, and that consumer's
         ``installed_in_agent_ids`` names both.
      3. A second consumer adds one more → 3, and their own
         ``installed_in_agent_ids`` names only their agent.
      4. Uninstalling one drops the count back to 2 — a stored counter could
         not do that, which is why there is no column.
    """
    _pub, pub_headers, pub_agent, _pub_env, _rev, entry = _publish_package(
        client, superuser_token_headers, agent_name="Count-Publisher"
    )
    package_uuid = entry["id"]
    _capture_adapter(patch_environment_adapter)

    # ── Phase 1: the publisher's own installs do not count ───────────────
    install_skill(client, pub_headers, pub_agent, package_uuid)
    pub_agent_2, _ = make_agent_with_env(client, pub_headers, "Publisher-Second")
    install_skill(client, pub_headers, pub_agent_2, package_uuid)

    pub_entry = entry_for(list_skill_catalog(client, pub_headers), package_uuid)
    assert pub_entry["install_count"] == 0
    assert sorted(pub_entry["installed_in_agent_ids"]) == sorted(
        [pub_agent, pub_agent_2]
    )

    # ── Phase 2: one consumer, two agents ────────────────────────────────
    _c1, c1_headers = make_developer(client, superuser_token_headers)
    c1_agent_a, _ = make_agent_with_env(client, c1_headers, "C1-A")
    c1_agent_b, _ = make_agent_with_env(client, c1_headers, "C1-B")
    install_skill(client, c1_headers, c1_agent_a, package_uuid)
    install_skill(client, c1_headers, c1_agent_b, package_uuid)

    c1_entry = entry_for(list_skill_catalog(client, c1_headers), package_uuid)
    assert c1_entry["install_count"] == 2
    assert sorted(c1_entry["installed_in_agent_ids"]) == sorted(
        [c1_agent_a, c1_agent_b]
    )
    assert c1_entry["can_manage"] is False

    # ── Phase 3: a second consumer ───────────────────────────────────────
    _c2, c2_headers = make_developer(client, superuser_token_headers)
    c2_agent, _ = make_agent_with_env(client, c2_headers, "C2-A")
    install_skill(client, c2_headers, c2_agent, package_uuid)

    c2_entry = entry_for(list_skill_catalog(client, c2_headers), package_uuid)
    assert c2_entry["install_count"] == 3
    assert c2_entry["installed_in_agent_ids"] == [c2_agent]
    # The publisher sees the same total from their own vantage point.
    assert (
        entry_for(list_skill_catalog(client, pub_headers), package_uuid)[
            "install_count"
        ]
        == 3
    )

    # ── Phase 4: removing an install lowers the count ────────────────────
    link_id = list_agent_plugins(client, c1_headers, c1_agent_b)[0]["id"]
    uninstall_agent_plugin(client, c1_headers, c1_agent_b, link_id)
    assert (
        entry_for(list_skill_catalog(client, pub_headers), package_uuid)[
            "install_count"
        ]
        == 2
    )


# ── Scenario 3: already_installed vs name_conflict ─────────────────────────


def test_a_second_install_is_a_duplicate_but_a_rival_name_is_a_conflict(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    Two publishers may both publish ``pdf-report`` (§9), but one agent has a
    single ``plugins/cinna-skills/pdf-report/`` directory:
      1. Consumer installs publisher A's package → ok.
      2. The same package again → 409 ``already_installed``.
      3. Publisher B's SAME-NAMED package → 409 ``name_conflict``, naming the
         package that is actually in the way.
      4. Removing A's install clears the way for B's.
    """
    _a, _a_headers, _a_agent, _a_env, _a_rev, entry_a = _publish_package(
        client, superuser_token_headers, agent_name="Rival-A"
    )
    _b, _b_headers, _b_agent, _b_env, _b_rev, entry_b = _publish_package(
        client, superuser_token_headers, agent_name="Rival-B"
    )
    assert entry_a["name"] == entry_b["name"] == "pdf-report"
    assert entry_a["package_id"] != entry_b["package_id"]

    _consumer, con_headers = make_developer(client, superuser_token_headers)
    con_agent, _ = make_agent_with_env(client, con_headers, "Rival-Consumer")
    _capture_adapter(patch_environment_adapter)

    install_skill(client, con_headers, con_agent, entry_a["id"])

    duplicate = install_skill(
        client, con_headers, con_agent, entry_a["id"], expected_status=409
    )
    assert error_code(duplicate) == "already_installed"

    conflict = install_skill(
        client, con_headers, con_agent, entry_b["id"], expected_status=409
    )
    assert error_code(conflict) == "name_conflict"
    # The refusal names the INCUMBENT, which is the package the consumer has
    # to remove — not the one they just asked for.
    assert entry_a["package_id"] in conflict["detail"]["message"]
    assert entry_b["package_id"] not in conflict["detail"]["message"]

    assert len(list_agent_plugins(client, con_headers, con_agent)) == 1

    # ── Phase 4: the conflict is about the slot, not the package ─────────
    link_id = list_agent_plugins(client, con_headers, con_agent)[0]["id"]
    uninstall_agent_plugin(client, con_headers, con_agent, link_id)
    install_skill(client, con_headers, con_agent, entry_b["id"])
    row = list_agent_plugins(client, con_headers, con_agent)[0]
    assert row["skill_package_id"] == entry_b["id"]


# ── Scenario 4: resolve_revision ───────────────────────────────────────────


def test_install_resolves_the_latest_revision_unless_one_is_named(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    Which revision an install pins:
      1. No ``revision_number`` → the package's latest AT INSTALL TIME.
      2. An explicit number pins that revision, even when a newer one exists.
      3. An unknown number is a 404 ``revision_not_found``, and nothing is
         installed.
      4. A package the caller cannot see is a 404 ``package_not_found``, and an
         agent they do not own is a 404 from the shared access helper.
    """
    _pub, pub_headers, pub_agent, pub_env, rev1, entry = _publish_package(
        client, superuser_token_headers, agent_name="Rev-Publisher"
    )
    package_uuid = entry["id"]
    write_skill(pub_env, "pdf-report", description="Publishes pdf-report.", body="v2")
    rev2 = publish_skill(
        client, pub_headers, pub_agent, "pdf-report", version="2.0"
    )

    _consumer, con_headers = make_developer(client, superuser_token_headers)
    latest_agent, _ = make_agent_with_env(client, con_headers, "Rev-Latest")
    pinned_agent, _ = make_agent_with_env(client, con_headers, "Rev-Pinned")
    _capture_adapter(patch_environment_adapter)

    # ── Phase 1: default → latest ────────────────────────────────────────
    install_skill(client, con_headers, latest_agent, package_uuid)
    assert (
        list_agent_plugins(client, con_headers, latest_agent)[0][
            "skill_package_revision_id"
        ]
        == rev2["id"]
    )

    # ── Phase 2: explicit → that one, and it reads as behind ─────────────
    install_skill(
        client, con_headers, pinned_agent, package_uuid, revision_number=1
    )
    pinned_row = list_agent_plugins(client, con_headers, pinned_agent)[0]
    assert pinned_row["skill_package_revision_id"] == rev1["id"]
    assert pinned_row["installed_version"] == "1.0"
    assert pinned_row["has_update"] is True

    # ── Phase 3: an unknown revision installs nothing ────────────────────
    third_agent, _ = make_agent_with_env(client, con_headers, "Rev-Ghost")
    unknown = install_skill(
        client,
        con_headers,
        third_agent,
        package_uuid,
        revision_number=99,
        expected_status=404,
    )
    assert error_code(unknown) == "revision_not_found"
    assert list_agent_plugins(client, con_headers, third_agent) == []

    # ── Phase 4: visibility and ownership guards ─────────────────────────
    _priv, priv_headers, priv_agent, priv_env, _priv_rev, _e = _publish_package(
        client,
        superuser_token_headers,
        skill="secret-skill",
        agent_name="Private-Publisher",
        visibility="private",
    )
    private_uuid = _e["id"]
    hidden = install_skill(
        client,
        con_headers,
        third_agent,
        private_uuid,
        expected_status=404,
    )
    assert error_code(hidden) == "package_not_found"

    # An agent the caller does not own answers 404 from the shared
    # ``verify_agent_access`` helper, before the catalog service is reached —
    # indistinguishable from an agent id that does not exist, so installing
    # into a guessed id cannot confirm that it belongs to somebody.
    r = client.post(
        f"{API}/agents/{priv_agent}/skills/install",
        headers=con_headers,
        json={"package_id": package_uuid},
    )
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Agent not found"


# ── Scenario 5: the archive route ──────────────────────────────────────────


def test_archive_is_served_only_to_an_environment_holding_the_link(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
    db: Session,
    skill_storage: Path,
) -> None:
    """
    The container-facing download, whose whole authorisation is the install:
      1. The consumer's environment fetches its pinned revision → 200, gzip
         bytes, and ``X-Content-SHA256`` matching the manifest's digest.
      2. An environment of an agent with no such link → 403, not 404: it
         authenticated fine, it simply has no install.
      3. A revision the environment did NOT install (the publisher's newer
         one) → 403, so an env can only ever fetch what its own manifest named.
      4. No credentials at all → 401/403.
      5. An unknown package → 404.
      6. Snapshot and archive cache deleted → 410 ``snapshot_missing`` on both
         the archive and the content preview: a revision is immutable, so a
         retry would never help.
    """
    _pub, pub_headers, pub_agent, pub_env, rev1, entry = _publish_package(
        client, superuser_token_headers, agent_name="Archive-Publisher"
    )
    package_uuid = entry["id"]

    _consumer, con_headers = make_developer(client, superuser_token_headers)
    con_agent, _ = make_agent_with_env(client, con_headers, "Archive-Consumer")
    other_agent, _ = make_agent_with_env(client, con_headers, "Archive-Bystander")
    adapter = _capture_adapter(patch_environment_adapter)
    install_skill(client, con_headers, con_agent, package_uuid)
    manifest_sha = _catalog_manifest_entry(adapter, "pdf-report")["archive"]["sha256"]

    _env, env_headers = create_env_with_token(db, con_agent, _consumer["id"])
    _other_env, other_env_headers = create_env_with_token(
        db, other_agent, _consumer["id"]
    )
    archive_url = (
        f"{API}/skills/packages/{package_uuid}/revisions/1/archive"
    )

    # ── Phase 1: the holder downloads ────────────────────────────────────
    r = client.get(archive_url, headers=env_headers)
    assert r.status_code == 200, r.text
    assert r.headers["x-content-sha256"] == manifest_sha
    assert r.headers["content-type"] == "application/gzip"
    assert "pdf-report-1.tar.gz" in r.headers["content-disposition"]
    assert r.content[:2] == b"\x1f\x8b", "body must be a gzip stream"
    assert hashlib.sha256(r.content).hexdigest() == manifest_sha

    # ── Phase 2: an environment with no install ──────────────────────────
    assert client.get(archive_url, headers=other_env_headers).status_code == 403

    # ── Phase 3: a revision this environment never installed ─────────────
    write_skill(pub_env, "pdf-report", description="Publishes pdf-report.", body="v2")
    publish_skill(client, pub_headers, pub_agent, "pdf-report", version="2.0")
    assert (
        client.get(
            f"{API}/skills/packages/{package_uuid}/revisions/2/archive",
            headers=env_headers,
        ).status_code
        == 403
    )

    # ── Phase 4: no credentials, and a user token is not an env token ────
    assert client.get(archive_url).status_code in (401, 403)
    assert client.get(archive_url, headers=con_headers).status_code in (401, 403)

    # ── Phase 5: an unknown package ──────────────────────────────────────
    ghost = uuid.uuid4()
    assert (
        client.get(
            f"{API}/skills/packages/{ghost}/revisions/1/archive",
            headers=env_headers,
        ).status_code
        == 404
    )

    # ── Phase 6: the snapshot is gone ────────────────────────────────────
    shutil.rmtree(skill_storage / package_uuid)
    gone = client.get(archive_url, headers=env_headers)
    assert gone.status_code == 410, gone.text
    assert error_code(gone.json()) == "snapshot_missing"

    content = client.get(
        f"{API}/skills/packages/{package_uuid}/revisions/1/content",
        headers=con_headers,
    )
    assert content.status_code == 410, content.text
    assert error_code(content.json()) == "snapshot_missing"


# ── Scenario 6: a container-side failure is reported, not raised ───────────


def test_a_checksum_mismatch_in_the_container_is_a_partial_failure(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    The container refuses an archive whose sha256 does not match (that refusal
    itself is unit-tested against the real code in
    ``tests/unit/test_skill_catalog_container_install.py``). What the API does
    with it:
      1. The install still answers 200 with ``success=True`` — the link exists,
         and the environment was reached.
      2. ``partial_failures`` is True and the failed plugin is reported with
         its message, which is what the amber banner renders.
      3. The link is created regardless, so the next sync can retry it.
    """
    _pub, _pub_headers, _pub_agent, _pub_env, _rev, entry = _publish_package(
        client, superuser_token_headers, agent_name="Mismatch-Publisher"
    )
    _consumer, con_headers = make_developer(client, superuser_token_headers)
    con_agent, _ = make_agent_with_env(client, con_headers, "Mismatch-Consumer")

    adapter = _capture_adapter(patch_environment_adapter)

    async def _reject_catalog_plugins(manifest: dict) -> list[dict]:
        adapter.plugins_set = manifest
        return [
            {
                "marketplace_name": item.get("marketplace_name", ""),
                "plugin_name": item.get("plugin_name", ""),
                "source": item.get("source", "marketplace"),
                "status": (
                    "failed" if item.get("source") == "catalog" else "installed"
                ),
                "error_message": (
                    "Archive checksum mismatch — files were not installed"
                    if item.get("source") == "catalog"
                    else None
                ),
            }
            for item in (manifest.get("plugins") or [])
        ]

    adapter.set_plugins = _reject_catalog_plugins

    response = install_skill(client, con_headers, con_agent, entry["id"])
    assert response["success"] is True
    assert response["partial_failures"] is True
    failures = [r for r in response["plugin_results"] if r["status"] == "failed"]
    assert len(failures) == 1
    assert failures[0]["plugin_name"] == "pdf-report"
    assert failures[0]["source"] == "catalog"
    assert "checksum mismatch" in failures[0]["error_message"]

    # The link survives the failed materialisation — the retry path is the
    # next sync, not a re-install.
    assert len(list_agent_plugins(client, con_headers, con_agent)) == 1


# ── Scenario 7: bundle publish of an agent holding a catalog skill ─────────


def test_bundle_publish_snapshots_a_catalog_skill_as_a_bundle_plugin(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
) -> None:
    """
    Catalog skills travel to bundle consumers as ordinary bundle plugins:
      1. A consumer installs a catalog skill and its files exist in the
         workspace (the container writes them; the test seeds the directory).
      2. That consumer publishes their agent as a bundle → 200. A regression in
         ``_resolve_link_identity`` (testing ``== bundle`` instead of
         ``!= marketplace``) sends the catalog link down the marketplace branch,
         where it resolves to nothing and hard-blocks this publish with a 400 —
         so the status code alone is the guard.
      3. The revision's ``plugin_specs`` carry the catalog skill under
         ``cinna-skills`` with its snapshot subdir.
      4. A third user installing the bundle receives it as ``source=bundle`` —
         no independent catalog upgrade, exactly as §5.3 documents.
    """
    _pub, _pub_headers, _pub_agent, _pub_env, _rev, entry = _publish_package(
        client, superuser_token_headers, agent_name="Skill-Author"
    )

    # ── Phase 1: a consumer with the skill installed AND materialised ────
    _consumer, con_headers = make_developer(client, superuser_token_headers)
    con_agent, con_env = make_agent_with_env(client, con_headers, "Bundle-Publisher")
    _capture_adapter(patch_environment_adapter)
    install_skill(client, con_headers, con_agent, entry["id"])

    plugin_dir = (
        workspace_root(con_env) / "plugins" / CATALOG_MARKETPLACE / "pdf-report"
    )
    (plugin_dir / "skills" / "pdf-report").mkdir(parents=True, exist_ok=True)
    (plugin_dir / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
        '{"name": "pdf-report", "version": "1.0"}', encoding="utf-8"
    )
    (plugin_dir / "skills" / "pdf-report" / "SKILL.md").write_text(
        "---\nname: pdf-report\ndescription: Publishes pdf-report.\n---\n",
        encoding="utf-8",
    )

    # ── Phase 2 + 3: publish the bundle ──────────────────────────────────
    r = client.post(
        f"{API}/agents/{con_agent}/publish", headers=con_headers, json={}
    )
    assert r.status_code == 200, (
        "a catalog link must not block bundle publish: " + r.text
    )
    revision = r.json()
    drain_tasks()

    specs = {
        (s["marketplace_name"], s["plugin_name"]): s
        for s in (revision.get("plugin_specs") or [])
    }
    spec = specs.get((CATALOG_MARKETPLACE, "pdf-report"))
    assert spec is not None, revision.get("plugin_specs")
    assert spec["snapshot_subdir"] == f"plugins/{CATALOG_MARKETPLACE}/pdf-report"

    # ── Phase 4: the bundle consumer gets it as a bundle plugin ──────────
    fresh = get_agent(client, con_headers, con_agent)
    make_bundle_public(client, con_headers, fresh["bundle_uuid"])

    _installer, inst_headers = make_developer(client, superuser_token_headers)
    installed = install_bundle(client, inst_headers, fresh["bundle_id"])
    installed_agent_id = installed["id"]

    rows = list_agent_plugins(client, inst_headers, installed_agent_id)
    catalog_rows = [
        row for row in rows if row["snapshot_plugin_name"] == "pdf-report"
    ]
    assert len(catalog_rows) == 1, rows
    assert catalog_rows[0]["source"] == "bundle"
    assert catalog_rows[0]["skill_package_revision_id"] is None
    assert catalog_rows[0]["marketplace_name"] == CATALOG_MARKETPLACE


def test_upgrading_a_non_catalog_link_never_reaches_the_catalog_service(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """A link id that does not exist is still a plain 404 on the shared route.

    The catalog branch of ``upgrade_agent_plugin`` is only entered for
    ``source=catalog``; everything else keeps the old answer, so a wrong id
    cannot start answering with a catalog code.
    """
    agent = create_agent_via_api(client, superuser_token_headers, name="Upgrade-404")
    drain_tasks()
    assert list_environments(client, superuser_token_headers, agent["id"])["data"]
    r = client.post(
        f"{API}/llm-plugins/agents/{agent['id']}/plugins/{uuid.uuid4()}/upgrade",
        headers=superuser_token_headers,
    )
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == "Plugin link not found"
