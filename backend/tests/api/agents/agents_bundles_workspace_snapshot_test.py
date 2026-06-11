"""API-level tests for the full-workspace bundle snapshot (Phase 2-3 of the plan).

Scenarios covered here:
  2. Missing-workspace publish fails loudly: active env, no on-disk workspace
     dir → 400 with a clear error message; no revision row created; bundle's
     latest_revision_id unchanged.
  3. No-env publish still allowed: publish with active_environment_id=None →
     succeeds; revision created; workspace/ subtree empty; prompts present in
     manifest.
  4. Install seeds the full tree from a v2 snapshot: scripts/, webapp/,
     agent_api/, custom dirs land in the install workspace; credentials/ left
     intact; plugins/ merged (consumer's own marketplace plugin dir preserved).

Scenarios 1, 5, 6, 7, 8 (pure FS operations on internal helpers) live in:
``tests/unit/test_bundle_workspace_snapshot.py``

Test-seam note:
  API tests run with a stubbed environment adapter (no Docker). To exercise
  FS capture, files are seeded into ``ENV_INSTANCES_DIR/<env_id>/app/workspace/``
  using ``Path(settings.ENV_INSTANCES_DIR) / env_id / "app" / "workspace"``.
  BUNDLE_STORAGE_DIR is patched to a tmp dir by the conftest's autouse
  ``patch_storage_dirs`` fixture.
"""

from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import (
    install_bundle as _install,
    make_user_and_headers as _make_user_and_headers,
    make_bundle_public,
)
from tests.utils.environment import list_environments

API = settings.API_V1_STR


# ── FS helpers ─────────────────────────────────────────────────────────────────


def _seed_env_workspace(env_id: str, tree: dict) -> Path:
    """Create workspace files inside the publisher env instance directory.

    Writes into ``ENV_INSTANCES_DIR/<env_id>/app/workspace/<tree>``.
    Names with a "." that are not dot-only (e.g. ".txt") become files;
    dicts become directories recursively; string values become file content.
    Returns the workspace root path.
    """
    ws_root = Path(settings.ENV_INSTANCES_DIR) / env_id / "app" / "workspace"
    _write_tree(ws_root, tree)
    return ws_root


def _write_tree(root: Path, tree: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, value in tree.items():
        child = root / name
        if isinstance(value, dict):
            _write_tree(child, value)
        elif value is None:
            child.mkdir(parents=True, exist_ok=True)
        else:
            child.parent.mkdir(parents=True, exist_ok=True)
            child.write_text(str(value))


# ── Scenario 2: Missing-workspace publish fails loudly ────────────────────────


def test_publish_fails_when_active_env_workspace_dir_missing(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Publishing with an active env but no on-disk workspace returns 400
    with a descriptive error message; no revision row is created and the
    bundle's latest_revision_id remains None.

    Scenario:
      1. Create agent and let an env be created (env's instance dir exists
         under the tmp instances dir, but the app/workspace/ subtree may or
         may not exist there — we REMOVE it to simulate a never-materialised
         workspace).
      2. Record the bundle's latest_revision_id before publish.
      3. POST /agents/{id}/publish → expect 400.
      4. Error message references workspace or environment.
      5. Refetch agent: installed_revision_id unchanged (still None or same).
    """
    # ── Phase 1: Create agent (env created automatically) ─────────────────
    agent = create_agent_via_api(
        client, superuser_token_headers, name="MissingWS-Publisher"
    )
    agent_id = agent["id"]
    drain_tasks()

    # Get the active environment id.
    envs = list_environments(client, superuser_token_headers, agent_id)
    assert envs["data"], "Expected at least one environment after agent creation"
    env_id = envs["data"][0]["id"]

    # ── Phase 2: Remove (or never create) the workspace dir ───────────────
    ws_root = Path(settings.ENV_INSTANCES_DIR) / env_id / "app" / "workspace"
    if ws_root.exists():
        import shutil
        shutil.rmtree(ws_root)

    # The workspace dir must NOT exist at this point.
    assert not ws_root.exists(), (
        "Test precondition: workspace dir must not exist to simulate missing workspace"
    )

    # Capture pre-publish state.
    fresh_before = client.get(
        f"{API}/agents/{agent_id}", headers=superuser_token_headers
    ).json()
    rev_id_before = fresh_before.get("installed_revision_id")

    # ── Phase 3+4: Publish → expect 400 with descriptive message ─────────
    r = client.post(
        f"{API}/agents/{agent_id}/publish",
        headers=superuser_token_headers,
        json={},
    )
    assert r.status_code == 400, f"Expected 400, got {r.status_code}: {r.text}"
    detail = r.json().get("detail", "")
    # The error should mention workspace or environment path.
    assert any(kw in detail.lower() for kw in ("workspace", "environment", "cannot publish")), (
        f"Error message should describe missing workspace, got: {detail!r}"
    )

    # ── Phase 5: No revision created; installed_revision_id unchanged ───────
    fresh_after = client.get(
        f"{API}/agents/{agent_id}", headers=superuser_token_headers
    ).json()
    assert fresh_after.get("installed_revision_id") == rev_id_before, (
        "installed_revision_id must remain unchanged after a failed publish"
    )
    # installed_revision_number should still be None (no revision created).
    assert fresh_after.get("installed_revision_number") is None, (
        "installed_revision_number must remain None when publish fails (no revision row created)"
    )


# ── Scenario 3: No-env publish is allowed ────────────────────────────────────


def test_publish_without_active_env_succeeds_prompts_only(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    Publishing an agent with active_environment_id=None succeeds (prompts-only).

    Uses the DB seam to clear active_environment_id (no API endpoint to unset
    it; this is the same pattern as set_environment_status in tests/utils/environment.py).

    Scenario:
      1. Create agent.
      2. Clear active_environment_id via DB seam.
      3. POST /agents/{id}/publish → 200.
      4. Revision created; manifest schema_version=2.
      5. workspace/ subtree in snapshot is empty (prompts-only).
      6. Prompts are present in manifest.
      7. bundle_uuid and installed_revision_id are set on the agent.
    """
    import uuid as _uuid
    from app.models import Agent as AgentModel

    # ── Phase 1: Create agent ─────────────────────────────────────────────
    agent = create_agent_via_api(
        client, superuser_token_headers, name="NoEnv-Publisher"
    )
    agent_id = agent["id"]
    drain_tasks()

    # ── Phase 2: Clear active_environment_id via DB seam ─────────────────
    # There is no API endpoint to unset the active environment; we use the DB
    # seam (same pattern as set_environment_status in tests/utils/environment.py).
    agent_row = db.get(AgentModel, _uuid.UUID(agent_id))
    assert agent_row is not None
    agent_row.active_environment_id = None
    db.add(agent_row)
    db.flush()

    # ── Phase 3: Publish ──────────────────────────────────────────────────
    r = client.post(
        f"{API}/agents/{agent_id}/publish",
        headers=superuser_token_headers,
        json={"release_notes": "prompts-only revision"},
    )
    assert r.status_code == 200, f"Publish without active env should succeed: {r.text}"
    revision = r.json()
    drain_tasks()

    # ── Phase 4: Revision created with schema_version=2 ───────────────────
    assert revision["revision_number"] == 1
    manifest = revision.get("manifest", {})
    assert manifest.get("schema_version") == 2, (
        f"Revision manifest must have schema_version=2, got: {manifest}"
    )

    # ── Phase 5: workspace/ subtree is a dir (empty for no-env case) ─────
    # snapshot_path is not in AgentBundleRevisionPublic; derive it from the
    # deterministic storage layout: BUNDLE_STORAGE_DIR / bundle_id / revision_number.
    refreshed_agent = client.get(
        f"{API}/agents/{agent_id}", headers=superuser_token_headers
    ).json()
    bundle_id_str = refreshed_agent["bundle_id"]
    snapshot_root = (
        Path(settings.BUNDLE_STORAGE_DIR) / bundle_id_str / str(revision["revision_number"])
    )
    assert snapshot_root.exists(), f"Snapshot root must exist on disk: {snapshot_root}"

    workspace_subdir = snapshot_root / "workspace"
    assert workspace_subdir.is_dir(), (
        "Snapshot must contain a workspace/ subtree even for prompts-only publish"
    )
    # No-env publish: workspace/ should be empty.
    assert list(workspace_subdir.iterdir()) == [], (
        "workspace/ must be empty for a prompts-only (no active env) publish"
    )

    # ── Phase 6: Prompts present in manifest ──────────────────────────────
    assert "prompts" in manifest, "Manifest must contain prompts section"

    # ── Phase 7: Agent row updated ────────────────────────────────────────
    refreshed = client.get(
        f"{API}/agents/{agent_id}", headers=superuser_token_headers
    ).json()
    assert refreshed["bundle_uuid"] is not None, "bundle_uuid must be set after publish"
    assert refreshed["installed_revision_id"] == revision["id"], (
        "installed_revision_id must point to the new revision"
    )


# ── Scenario 4: Install seeds the full tree from v2 snapshot ─────────────────


def test_v2_snapshot_captures_full_tree_for_install(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    v2 snapshot captures the full workspace tree; install creates a revision
    that consumer installs can seed from. The API-level assertions verify:
      - Publish produces a revision with schema_version=2.
      - Snapshot workspace/ subtree contains scripts/, webapp/, agent_api/,
        custom templates/ dir, plugins/ (with plugin content), notes.md.
      - credentials/ and other denylist entries absent from snapshot.
      - Consumer can install the bundle successfully (revision consumed).
      - plugins/ in snapshot: bundle plugin present, derived files absent.

    The actual "seed lands files in consumer workspace" behavior is covered
    by unit test ``test_seed_v2_snapshot_plugins_merge_preserves_consumer_plugin``
    (``tests/unit/test_bundle_workspace_snapshot.py``), which calls
    ``seed_workspace_from_bundle_snapshot`` directly with a tmp_path — this
    avoids the FS path mismatch between the stub adapter's tmp instances_dir
    and ``settings.ENV_INSTANCES_DIR`` used by workspace_copy.

    Scenario:
      1. Publisher: create agent + seed env workspace with full tree.
      2. Publisher: POST /agents/{id}/publish.
      3. Verify snapshot workspace/ has correct content.
      4. Make bundle public + consumer installs → 200.
      5. Revision is the one installed by the consumer.
    """
    # ── Phase 1: Publisher creates agent + seeds workspace ────────────────
    publisher_agent = create_agent_via_api(
        client, superuser_token_headers, name="Full-Tree-Publisher"
    )
    pub_agent_id = publisher_agent["id"]
    drain_tasks()

    envs = list_environments(client, superuser_token_headers, pub_agent_id)
    assert envs["data"], "Publisher must have an environment"
    pub_env_id = envs["data"][0]["id"]

    # Seed a rich workspace for the publisher env.
    _seed_env_workspace(
        pub_env_id,
        {
            "scripts": {"run.sh": "#!/bin/bash\necho hello"},
            "docs": {"README.md": "# Docs"},
            "webapp": {"index.html": "<html>Hello</html>"},
            "agent_api": {"app.py": "# FastAPI app"},
            "templates": {"x.txt": "template content"},  # custom dir
            "notes.md": "root level file",
            "plugins": {
                "settings.json": "{}",  # derived — must NOT appear in snapshot
                "bundle-mkt": {
                    "bundle-plugin": {"plugin.json": '{"name":"bundle-plugin"}'}
                },
            },
            # Denylist entries — must NOT appear in snapshot.
            "credentials": {"secret.json": "{}"},
            "app-data": {"db.sqlite": "data"},
            "logs": {"run.log": "log"},
        },
    )

    # ── Phase 2: Publish ──────────────────────────────────────────────────
    r = client.post(
        f"{API}/agents/{pub_agent_id}/publish",
        headers=superuser_token_headers,
        json={"release_notes": "full tree v1"},
    )
    assert r.status_code == 200, f"Publish failed: {r.text}"
    revision = r.json()
    drain_tasks()

    # ── Phase 3: Verify snapshot workspace/ content ───────────────────────
    assert revision.get("manifest", {}).get("schema_version") == 2, (
        "Published revision must be schema_version 2"
    )
    # snapshot_path is not in AgentBundleRevisionPublic; derive from storage layout.
    pub_fresh_for_snap = client.get(
        f"{API}/agents/{pub_agent_id}", headers=superuser_token_headers
    ).json()
    snap_root = (
        Path(settings.BUNDLE_STORAGE_DIR)
        / pub_fresh_for_snap["bundle_id"]
        / str(revision["revision_number"])
    )
    assert snap_root.exists(), "Snapshot root must exist"
    ws_subdir = snap_root / "workspace"
    assert ws_subdir.is_dir(), "Snapshot must have workspace/ subtree"

    snap_ws_entries = {p.name for p in ws_subdir.iterdir()}

    # Required bundle-owned dirs captured.
    for expected in ("scripts", "webapp", "agent_api", "templates", "plugins"):
        assert expected in snap_ws_entries, f"{expected}/ not captured in snapshot"

    # Root-level custom file captured.
    assert (ws_subdir / "notes.md").is_file(), "notes.md not captured in snapshot"

    # Content correct.
    assert (ws_subdir / "webapp" / "index.html").read_text() == "<html>Hello</html>"
    assert (ws_subdir / "scripts" / "run.sh").exists()

    # plugins/ has the bundle plugin.
    assert (ws_subdir / "plugins" / "bundle-mkt" / "bundle-plugin" / "plugin.json").exists(), \
        "bundle plugin must be in snapshot"
    # plugins/settings.json (derived) must NOT be in snapshot.
    assert not (ws_subdir / "plugins" / "settings.json").exists(), \
        "plugins/settings.json (derived) must NOT appear in snapshot"

    # Denylist entries must be absent from snapshot.
    for excluded in ("credentials", "app-data", "logs", "databases", "uploads"):
        assert excluded not in snap_ws_entries, (
            f"{excluded} must NOT appear in snapshot workspace/ subtree"
        )

    # ── Phase 4: Make bundle public + consumer installs ───────────────────
    pub_fresh = client.get(
        f"{API}/agents/{pub_agent_id}", headers=superuser_token_headers
    ).json()
    bundle_id = pub_fresh["bundle_id"]
    bundle_uuid = pub_fresh["bundle_uuid"]
    assert bundle_uuid, "bundle_uuid must be set after publish"
    make_bundle_public(client, superuser_token_headers, bundle_uuid)

    _, consumer_headers = _make_user_and_headers(client)
    install = _install(client, consumer_headers, bundle_id)

    # ── Phase 5: Consumer install references the correct revision ─────────
    assert install["installed_revision_id"] == revision["id"], (
        "Consumer install must reference the published revision"
    )
    assert install["bundle_uuid"] == pub_fresh["bundle_uuid"], (
        "Consumer install must reference the same bundle"
    )
