"""Unit tests for the full-workspace bundle snapshot (Phase 2-3 of the plan).

Pure filesystem tests — no DB, no HTTP. Covers:

  1. ``_snapshot_workspace_tree`` captures the full tree (scripts, webapp,
     agent_api, custom dirs, root files, plugins minus derived files;
     excludes app-data, credentials, logs, databases, uploads).
  5. ``replace_bundle_content`` prune semantics: stale bundle-owned dirs
     removed; app-data / credentials preserved; new content present;
     consumer marketplace plugin preserved.
  6. v1-flat legacy snapshot still seeds / applies (no-delete behaviour).
  7. Plugins merge preserved across seed and apply-update (explicit
     marketplace-dir survival assertions — folded into scenarios 4/5 per plan).
  8. ``_hash_tree_with_manifest``: identical v2 trees hash equal; changing
     any captured file (webapp/, custom dir) changes the hash.
  9. ``hash_workspace_tree`` (git dirty-check primitive): stable across
     rebuilds, sensitive to content, excludes symlinks, missing root → empty
     digest.

API-observable counterparts (scenarios 2, 3, 4) live in
``tests/api/agents/bundles/agents_bundles_workspace_snapshot_test.py``.
The API-observable dirty-check behavior is covered in
``tests/api/agents/git/agents_git_source_test.py`` (Scenario 7).
"""
import json
import uuid
from pathlib import Path
from unittest.mock import patch

from app.core.config import settings
from app.services.bundles.publish_service import PublishService
from app.services.environments.workspace_classification import WORKSPACE_ROOT_REL


# ── helpers ────────────────────────────────────────────────────────────────────


def _make_env_workspace(root: Path, tree: dict) -> None:
    """Recursively create a workspace tree from a dict spec.

    Values can be:
     - str  → write as file content
     - dict → create as sub-directory with children
     - None → create as empty directory
    """
    root.mkdir(parents=True, exist_ok=True)
    for name, value in tree.items():
        child = root / name
        if isinstance(value, dict):
            _make_env_workspace(child, value)
        elif value is None:
            child.mkdir(parents=True, exist_ok=True)
        else:
            child.parent.mkdir(parents=True, exist_ok=True)
            child.write_text(str(value))


def _make_v2_snapshot(snap_root: Path, workspace_tree: dict) -> None:
    """Build a v2-layout snapshot at ``snap_root``.

    Creates ``workspace/`` subtree from ``workspace_tree`` and writes a
    minimal ``manifest.json`` with ``schema_version: 2``.
    """
    snap_root.mkdir(parents=True, exist_ok=True)
    workspace_dir = snap_root / "workspace"
    _make_env_workspace(workspace_dir, workspace_tree)
    manifest = {
        "schema_version": 2,
        "bundle_id": "io.test.snapshot.unit",
        "revision_number": 1,
        "prompts": {},
    }
    (snap_root / "manifest.json").write_text(json.dumps(manifest))


def _make_v1_snapshot(snap_root: Path, scripts_content: str | None = "#!/bin/bash\necho hi") -> None:
    """Build a v1-flat snapshot at ``snap_root`` with a ``scripts/`` folder."""
    snap_root.mkdir(parents=True, exist_ok=True)
    if scripts_content is not None:
        scripts_dir = snap_root / "scripts"
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / "run.sh").write_text(scripts_content)
    manifest = {
        "schema_version": 1,
        "bundle_id": "io.test.snapshot.unit.v1",
        "revision_number": 1,
        "prompts": {},
    }
    (snap_root / "manifest.json").write_text(json.dumps(manifest))


def _make_install_workspace(root: Path, tree: dict) -> None:
    """Create an install env workspace at ``root / WORKSPACE_ROOT_REL``."""
    ws = root / WORKSPACE_ROOT_REL
    _make_env_workspace(ws, tree)


# ── Scenario 1: _snapshot_workspace_tree captures the full tree ───────────────


def test_snapshot_workspace_tree_captures_full_bundle_tree(tmp_path: Path) -> None:
    """
    _snapshot_workspace_tree captures the full bundle-owned workspace tree:
      - scripts/, docs/, webapp/, agent_api/, custom templates/ dir,
        root notes.md, plugins/<mp>/<p>/… all appear under workspace/.
      - plugins/settings.json absent (derived file excluded).
      - app-data/, credentials/, logs/, databases/, uploads/ absent even
        when seeded.
    """
    env_root = tmp_path / "env"
    ws_root = env_root / WORKSPACE_ROOT_REL

    # Seed the publisher env workspace with a rich tree.
    _make_env_workspace(
        ws_root,
        {
            "scripts": {"run.sh": "#!/bin/bash\necho hi"},
            "docs": {"README.md": "# Docs"},
            "webapp": {"index.html": "<html/>"},
            "agent_api": {"app.py": "from fastapi import FastAPI\napp=FastAPI()"},
            "templates": {"x.txt": "template content"},  # custom dir
            "notes.md": "root-level file",  # custom root file
            "plugins": {
                "settings.json": "{}",  # derived — must be excluded
                "manifest.json": "{}",  # derived — must be excluded
                "test-mkt": {
                    "test-plugin": {
                        "plugin.json": '{"name":"test-plugin"}',
                        ".cinna_plugin_ref": "abc123",
                    }
                },
            },
            # Denylist entries — must be excluded.
            "app-data": {"db.sqlite": "binary"},
            "credentials": {"secret.json": "{}"},
            "logs": {"session.log": "log line"},
            "databases": {"cache.db": "db"},
            "uploads": {"user_file.pdf": "bytes"},
        },
    )

    dest = tmp_path / "snapshot"
    dest.mkdir()

    PublishService._snapshot_workspace_tree(env_root, dest)

    # workspace/ subtree must exist.
    ws_dir = dest / "workspace"
    assert ws_dir.is_dir(), "workspace/ subtree not created"

    ws_entries = {p.name for p in ws_dir.iterdir()}

    # ── Captured ──────────────────────────────────────────────────────────
    assert "scripts" in ws_entries, "scripts/ not captured"
    assert "docs" in ws_entries, "docs/ not captured"
    assert "webapp" in ws_entries, "webapp/ not captured"
    assert "agent_api" in ws_entries, "agent_api/ not captured"
    assert "templates" in ws_entries, "custom templates/ not captured"
    assert "plugins" in ws_entries, "plugins/ not captured"

    # notes.md is a root-level file (not a dir) — should appear as a file.
    assert (ws_dir / "notes.md").is_file(), "root-level notes.md not captured"

    # Nested plugin subdir and .cinna_plugin_ref marker preserved.
    assert (ws_dir / "plugins" / "test-mkt" / "test-plugin" / "plugin.json").exists(), \
        "plugin subdir not captured"
    assert (ws_dir / "plugins" / "test-mkt" / "test-plugin" / ".cinna_plugin_ref").exists(), \
        ".cinna_plugin_ref marker not captured"

    # Content of captured files is correct.
    assert (ws_dir / "webapp" / "index.html").read_text() == "<html/>"
    assert (ws_dir / "agent_api" / "app.py").read_text().startswith("from fastapi")
    assert (ws_dir / "templates" / "x.txt").read_text() == "template content"

    # ── Excluded (plugins/ derived files) ─────────────────────────────────
    assert not (ws_dir / "plugins" / "settings.json").exists(), \
        "plugins/settings.json (derived) must be excluded"
    assert not (ws_dir / "plugins" / "manifest.json").exists(), \
        "plugins/manifest.json (derived) must be excluded"

    # ── Excluded (denylist top-level entries) ─────────────────────────────
    for excluded in ("app-data", "credentials", "logs", "databases", "uploads"):
        assert excluded not in ws_entries, f"{excluded} must be excluded from snapshot"


def test_snapshot_workspace_tree_skips_symlinks(tmp_path: Path) -> None:
    """Snapshot never follows/copies symlinks (denylist bypass + host exfil).

      - top-level symlink ``notsecret -> ../../credentials`` is NOT in the
        snapshot (neither as a link nor as dereferenced content);
      - nested symlink ``scripts/leak.json -> <outside file>`` is NOT captured;
      - absolute symlink ``hostleak -> /etc/passwd`` is NOT captured.
    """
    env_root = tmp_path / "env"
    ws_root = env_root / WORKSPACE_ROOT_REL

    # Real "outside" content the attacker tries to exfiltrate.
    outside = tmp_path / "outside"
    secret_dir = outside / "credentials"
    secret_dir.mkdir(parents=True)
    (secret_dir / "secret.json").write_text("TOPSECRET")
    host_secret = outside / "host_secret.txt"
    host_secret.write_text("HOSTSECRET")

    _make_env_workspace(
        ws_root,
        {
            "scripts": {"run.sh": "#!/bin/bash\necho hi"},
            "docs": {"README.md": "# docs"},
        },
    )
    # Top-level symlink-to-dir with a NON-excluded name.
    (ws_root / "notsecret").symlink_to(secret_dir, target_is_directory=True)
    # Absolute symlink to a host file.
    (ws_root / "hostleak").symlink_to(Path("/etc/passwd"))
    # Nested symlink inside a captured dir.
    (ws_root / "scripts" / "leak.json").symlink_to(host_secret)

    dest = tmp_path / "snapshot"
    dest.mkdir()

    PublishService._snapshot_workspace_tree(env_root, dest)

    ws_dir = dest / "workspace"
    ws_entries = {p.name for p in ws_dir.iterdir()}

    # Real dirs captured.
    assert "scripts" in ws_entries
    assert "docs" in ws_entries

    # Top-level symlinks excluded entirely (neither link nor content).
    assert "notsecret" not in ws_entries, "top-level symlink must not be captured"
    assert "hostleak" not in ws_entries, "absolute symlink must not be captured"
    assert not (ws_dir / "notsecret").exists()

    # Nested symlink not captured (no link, no dereferenced bytes).
    assert not (ws_dir / "scripts" / "leak.json").exists(), \
        "nested symlink must not be captured"
    # Sanity: the secret content never leaked anywhere into the snapshot.
    for f in ws_dir.rglob("*"):
        if f.is_file():
            assert f.read_text() not in ("TOPSECRET", "HOSTSECRET"), \
                f"leaked secret content into {f}"


def test_snapshot_workspace_tree_no_env_creates_empty_workspace_dir(tmp_path: Path) -> None:
    """When env_workspace_root is None (no active env), workspace/ is created but empty."""
    dest = tmp_path / "snapshot"
    dest.mkdir()

    PublishService._snapshot_workspace_tree(None, dest)

    ws_dir = dest / "workspace"
    assert ws_dir.is_dir(), "workspace/ dir must be created even for prompts-only snapshot"
    assert list(ws_dir.iterdir()) == [], "workspace/ must be empty for prompts-only snapshot"


# ── Scenario 5: apply-update prune semantics ──────────────────────────────────


def test_replace_bundle_content_prunes_stale_bundle_owned_dirs(tmp_path: Path) -> None:
    """
    replace_bundle_content (apply-update) prune semantics:
      - stale bundle-owned oldfeature/ removed from install workspace.
      - app-data/ and credentials/ always preserved.
      - new snapshot content (newfeature/) present after update.
      - consumer marketplace plugin under plugins/ preserved (scenario 7).
    """
    from app.services.environments.workspace_copy import replace_bundle_content as _replace

    # Build rev2 snapshot: ships newfeature/ + scripts/ updated, no oldfeature/;
    # plugins/ carries mkt-b/bundle-plugin but NOT mkt-a (consumer owns that).
    snap2 = tmp_path / "snap2"
    _make_v2_snapshot(
        snap2,
        {
            "scripts": {"run.sh": "#!/bin/bash\nnew"},
            "newfeature": {"module.py": "# new module"},
            "plugins": {
                "mkt-b": {"bundle-plugin": {"plugin.json": '{"name":"bundle","version":"2"}'}},
            },
        },
    )

    fake_env_id = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    env_dir = tmp_path / str(fake_env_id)

    # Build install workspace: has oldfeature/ (from old rev), app-data/,
    # credentials/, consumer marketplace plugin in mkt-a, bundle plugin in mkt-b.
    _make_install_workspace(
        env_dir,
        {
            "oldfeature": {"helper.py": "# old"},
            "scripts": {"run.sh": "#!/bin/bash\nold"},
            "app-data": {"db.sqlite": "data"},
            "credentials": {"secret.json": '{"key":"val"}'},
            "plugins": {
                "settings.json": "{}",  # derived — present on consumer
                "mkt-a": {"consumer-plugin": {"plugin.json": '{"name":"consumer"}'}},
                "mkt-b": {"bundle-plugin": {"plugin.json": '{"name":"bundle","version":"1"}'}},
            },
        },
    )

    with patch.object(settings, "ENV_INSTANCES_DIR", str(tmp_path)):
        _replace(snap2, fake_env_id)

    ws = env_dir / WORKSPACE_ROOT_REL

    # ── Stale bundle-owned dir removed ────────────────────────────────────
    assert not (ws / "oldfeature").exists(), "stale oldfeature/ must be pruned"

    # ── New content present ────────────────────────────────────────────────
    assert (ws / "newfeature" / "module.py").exists(), "newfeature/ must be seeded"
    assert (ws / "scripts" / "run.sh").read_text() == "#!/bin/bash\nnew"

    # ── Denylist entries always preserved ─────────────────────────────────
    assert (ws / "app-data" / "db.sqlite").exists(), "app-data/ must not be pruned"
    assert (ws / "credentials" / "secret.json").exists(), "credentials/ must not be pruned"

    # ── plugins/: consumer marketplace dir preserved (scenario 7) ─────────
    assert (ws / "plugins" / "mkt-a" / "consumer-plugin" / "plugin.json").exists(), \
        "consumer marketplace plugin must survive apply-update"
    # Bundle plugin updated to rev2 version.
    bundle_plugin_data = json.loads(
        (ws / "plugins" / "mkt-b" / "bundle-plugin" / "plugin.json").read_text()
    )
    assert bundle_plugin_data.get("version") == "2", \
        "bundle plugin must be updated to rev2 version"


def test_replace_bundle_content_prunes_stale_symlink_without_touching_target(
    tmp_path: Path,
) -> None:
    """Prune pass removes a stale top-level SYMLINK entry by unlinking the link
    itself — never rmtree'ing through it (which would raise and leave it
    forever) and never deleting the link's target.
    """
    from app.services.environments.workspace_copy import (
        replace_bundle_content as _replace,
    )

    # rev2 ships only scripts/ (no "oldlink").
    snap2 = tmp_path / "snap2"
    _make_v2_snapshot(snap2, {"scripts": {"run.sh": "#!/bin/bash\nnew"}})

    # A real target OUTSIDE the workspace that must survive.
    target_dir = tmp_path / "important_target"
    target_dir.mkdir()
    (target_dir / "keep.txt").write_text("KEEP ME")

    fake_env_id = uuid.UUID("99999999-8888-7777-6666-555555555555")
    env_dir = tmp_path / str(fake_env_id)
    _make_install_workspace(
        env_dir,
        {"scripts": {"run.sh": "#!/bin/bash\nold"}},
    )
    ws = env_dir / WORKSPACE_ROOT_REL
    # Stale top-level symlink-to-dir the bundle no longer ships.
    (ws / "oldlink").symlink_to(target_dir, target_is_directory=True)

    with patch.object(settings, "ENV_INSTANCES_DIR", str(tmp_path)):
        _replace(snap2, fake_env_id)

    # The stale symlink is gone.
    assert not (ws / "oldlink").exists()
    assert not (ws / "oldlink").is_symlink()
    # Its target is untouched.
    assert (target_dir / "keep.txt").read_text() == "KEEP ME"
    # New content present.
    assert (ws / "scripts" / "run.sh").read_text() == "#!/bin/bash\nnew"


# ── Scenario 6: v1 legacy snapshot still seeds / applies ─────────────────────


def test_v1_flat_snapshot_seeds_correctly(tmp_path: Path) -> None:
    """v1-flat snapshot seeds scripts/ into an install workspace correctly."""
    from app.services.environments.workspace_copy import seed_workspace_from_bundle_snapshot as _seed

    # Build a v1-flat snapshot.
    snap = tmp_path / "snap_v1"
    snap.mkdir()
    _make_v1_snapshot(snap, scripts_content="#!/bin/bash\necho hello_v1")

    fake_env_id = uuid.UUID("11111111-2222-3333-4444-555555555555")
    env_dir = tmp_path / str(fake_env_id)
    ws = env_dir / WORKSPACE_ROOT_REL
    ws.mkdir(parents=True, exist_ok=True)

    with patch.object(settings, "ENV_INSTANCES_DIR", str(tmp_path)):
        _seed(snap, fake_env_id)

    assert (ws / "scripts" / "run.sh").exists(), "scripts/run.sh must be seeded from v1 snapshot"
    assert (ws / "scripts" / "run.sh").read_text() == "#!/bin/bash\necho hello_v1"


def test_v1_flat_snapshot_apply_update_keeps_no_delete_behaviour(tmp_path: Path) -> None:
    """v1→v1 apply-update keeps no-delete (overwrite-only) behaviour.

    An existing top-level dir not in the v1 allowlist (e.g. a consumer-written
    ``oldfeature/``) must survive because v1 snapshots never prune.
    """
    from app.services.environments.workspace_copy import replace_bundle_content as _replace

    snap = tmp_path / "snap_v1_update"
    snap.mkdir()
    _make_v1_snapshot(snap, scripts_content="#!/bin/bash\nupdated")

    fake_env_id = uuid.UUID("22222222-3333-4444-5555-666666666666")
    env_dir = tmp_path / str(fake_env_id)

    # Pre-existing workspace with an extra dir that is NOT in the v1 allowlist.
    _make_install_workspace(
        env_dir,
        {
            "scripts": {"run.sh": "#!/bin/bash\nold"},
            "oldfeature": {"data.txt": "should survive"},
            "credentials": {"token.json": "{}"},
        },
    )

    with patch.object(settings, "ENV_INSTANCES_DIR", str(tmp_path)):
        _replace(snap, fake_env_id)

    ws = env_dir / WORKSPACE_ROOT_REL

    # Scripts updated.
    assert (ws / "scripts" / "run.sh").read_text() == "#!/bin/bash\nupdated"

    # oldfeature/ NOT pruned (v1 = no-delete).
    assert (ws / "oldfeature" / "data.txt").exists(), \
        "v1 apply-update must NOT prune dirs outside the allowlist (no-delete behaviour)"

    # credentials/ preserved (denylist entry).
    assert (ws / "credentials" / "token.json").exists()


# ── Scenario 7: Plugins merge preserved (explicit assertions) ─────────────────


def test_seed_v2_snapshot_plugins_merge_preserves_consumer_plugin(tmp_path: Path) -> None:
    """seed_workspace_from_bundle_snapshot merges plugins: consumer marketplace
    plugin dir is preserved; bundle plugin dir is seeded/updated.
    """
    from app.services.environments.workspace_copy import seed_workspace_from_bundle_snapshot as _seed

    snap = tmp_path / "snap_v2_plugins"
    _make_v2_snapshot(
        snap,
        {
            "scripts": {"run.sh": "#!/bin/bash\nhello"},
            "plugins": {
                "bundle-mkt": {"bundle-plugin": {"plugin.json": '{"name":"bundle-plugin"}'}},
            },
        },
    )

    fake_env_id = uuid.UUID("33333333-4444-5555-6666-777777777777")
    env_dir = tmp_path / str(fake_env_id)

    # Consumer workspace already has its own marketplace plugin.
    _make_install_workspace(
        env_dir,
        {
            "plugins": {
                "settings.json": "{}",
                "consumer-mkt": {"my-plugin": {"plugin.json": '{"name":"my-plugin"}'}},
            },
        },
    )

    with patch.object(settings, "ENV_INSTANCES_DIR", str(tmp_path)):
        _seed(snap, fake_env_id)

    ws = env_dir / WORKSPACE_ROOT_REL

    # Consumer marketplace plugin preserved.
    assert (ws / "plugins" / "consumer-mkt" / "my-plugin" / "plugin.json").exists(), \
        "consumer marketplace plugin must survive v2 seed"

    # Bundle plugin seeded.
    assert (ws / "plugins" / "bundle-mkt" / "bundle-plugin" / "plugin.json").exists(), \
        "bundle plugin must be seeded from v2 snapshot"

    # plugins/settings.json from consumer workspace is preserved (seed doesn't touch it).
    # (snapshot can't contain settings.json; consumer's stays in place)
    assert (ws / "plugins" / "settings.json").exists(), \
        "consumer plugins/settings.json must not be removed by seed"


# ── Scenario 8: content_hash stability and sensitivity ────────────────────────


def _build_v2_snapshot_with_manifest(dest: Path, workspace_tree: dict) -> dict:
    """Build a v2 snapshot, compute hash, return manifest dict."""
    _make_v2_snapshot(dest, workspace_tree)
    manifest_without_hash = {
        "schema_version": 2,
        "bundle_id": "io.test.hash",
        "revision_number": 1,
        "prompts": {"workflow": "hi"},
    }
    hash_val = PublishService._hash_tree_with_manifest(dest, manifest_without_hash)
    return {"hash": hash_val, "manifest": manifest_without_hash}


def test_content_hash_identical_v2_trees_hash_equal(tmp_path: Path) -> None:
    """Identical v2 trees with the same manifest produce the same hash."""
    tree = {
        "scripts": {"run.sh": "#!/bin/bash\necho hi"},
        "webapp": {"index.html": "<html/>"},
        "agent_api": {"app.py": "# api"},
        "custom_dir": {"data.txt": "data"},
    }

    snap_a = tmp_path / "snap_a"
    snap_b = tmp_path / "snap_b"

    res_a = _build_v2_snapshot_with_manifest(snap_a, tree)
    res_b = _build_v2_snapshot_with_manifest(snap_b, tree)

    assert res_a["hash"] == res_b["hash"], (
        "Identical v2 trees must produce identical content hashes"
    )


def test_content_hash_changes_when_webapp_file_changes(tmp_path: Path) -> None:
    """Changing a file under webapp/ changes the hash."""
    base_tree = {
        "webapp": {"index.html": "<html>v1</html>"},
        "scripts": {"run.sh": "echo hi"},
    }
    changed_tree = {
        "webapp": {"index.html": "<html>v2</html>"},  # content changed
        "scripts": {"run.sh": "echo hi"},
    }

    snap_a = tmp_path / "snap_a"
    snap_b = tmp_path / "snap_b"

    res_a = _build_v2_snapshot_with_manifest(snap_a, base_tree)
    res_b = _build_v2_snapshot_with_manifest(snap_b, changed_tree)

    assert res_a["hash"] != res_b["hash"], \
        "Changing webapp/index.html must change the content hash"


def test_content_hash_changes_when_custom_dir_file_changes(tmp_path: Path) -> None:
    """Changing a file in a custom top-level dir changes the hash."""
    base_tree = {
        "scripts": {"run.sh": "echo hi"},
        "my_custom": {"data.json": '{"v":1}'},
    }
    changed_tree = {
        "scripts": {"run.sh": "echo hi"},
        "my_custom": {"data.json": '{"v":2}'},  # custom dir content changed
    }

    snap_a = tmp_path / "snap_a"
    snap_b = tmp_path / "snap_b"

    res_a = _build_v2_snapshot_with_manifest(snap_a, base_tree)
    res_b = _build_v2_snapshot_with_manifest(snap_b, changed_tree)

    assert res_a["hash"] != res_b["hash"], \
        "Changing a custom-dir file must change the content hash"


def test_content_hash_changes_when_agent_api_file_changes(tmp_path: Path) -> None:
    """Changing a file under agent_api/ changes the hash."""
    snap_a = tmp_path / "snap_a"
    snap_b = tmp_path / "snap_b"

    res_a = _build_v2_snapshot_with_manifest(
        snap_a, {"agent_api": {"app.py": "# v1"}}
    )
    res_b = _build_v2_snapshot_with_manifest(
        snap_b, {"agent_api": {"app.py": "# v2"}}
    )

    assert res_a["hash"] != res_b["hash"], \
        "Changing agent_api/app.py must change the content hash"


def test_content_hash_changes_when_new_file_added(tmp_path: Path) -> None:
    """Adding a new file to the snapshot changes the hash."""
    base_tree = {"scripts": {"run.sh": "echo hi"}}
    extra_tree = {
        "scripts": {"run.sh": "echo hi"},
        "docs": {"guide.md": "# Guide"},  # added
    }

    snap_a = tmp_path / "snap_a"
    snap_b = tmp_path / "snap_b"

    res_a = _build_v2_snapshot_with_manifest(snap_a, base_tree)
    res_b = _build_v2_snapshot_with_manifest(snap_b, extra_tree)

    assert res_a["hash"] != res_b["hash"], \
        "Adding a new file to the workspace must change the content hash"


# ── Scenario 9: hash_workspace_tree (git dirty-check primitive) ───────────────
#
# ``hash_workspace_tree`` is the workspace-only sibling of
# ``_hash_tree_with_manifest``.  It omits the manifest body, making the digest
# stable across rebuilds (revision_number / published_at never affect it).
# The git dirty check compares the digest of the live env workspace against the
# digest of the last-synced snapshot workspace to decide whether files changed.


def test_hash_workspace_tree_identical_content_gives_same_digest(tmp_path: Path) -> None:
    """Two workspace directories with byte-identical content hash equal."""
    tree = {
        "scripts": {"run.sh": "#!/bin/bash\necho hello"},
        "docs": {"README.md": "# Docs"},
    }

    ws_a = tmp_path / "ws_a"
    ws_b = tmp_path / "ws_b"
    _make_env_workspace(ws_a, tree)
    _make_env_workspace(ws_b, tree)

    digest_a = PublishService.hash_workspace_tree(ws_a)
    digest_b = PublishService.hash_workspace_tree(ws_b)

    assert digest_a == digest_b, (
        "Identical workspace content must produce the same hash_workspace_tree digest"
    )


def test_hash_workspace_tree_content_change_gives_different_digest(tmp_path: Path) -> None:
    """Modifying one file's content produces a different digest."""
    ws_a = tmp_path / "ws_a"
    ws_b = tmp_path / "ws_b"
    _make_env_workspace(ws_a, {"scripts": {"run.sh": "#!/bin/bash\necho v1"}})
    _make_env_workspace(ws_b, {"scripts": {"run.sh": "#!/bin/bash\necho v2"}})

    assert PublishService.hash_workspace_tree(ws_a) != PublishService.hash_workspace_tree(ws_b), (
        "Different file content must yield different hash_workspace_tree digests"
    )


def test_hash_workspace_tree_added_file_gives_different_digest(tmp_path: Path) -> None:
    """Adding a new file to the workspace changes the digest."""
    ws_base = tmp_path / "ws_base"
    ws_plus = tmp_path / "ws_plus"
    _make_env_workspace(ws_base, {"scripts": {"run.sh": "echo hi"}})
    _make_env_workspace(ws_plus, {
        "scripts": {"run.sh": "echo hi"},
        "docs": {"new_file.md": "locally added"},  # extra file
    })

    assert PublishService.hash_workspace_tree(ws_base) != PublishService.hash_workspace_tree(ws_plus), (
        "Adding a file must change hash_workspace_tree"
    )


def test_hash_workspace_tree_missing_root_returns_empty_digest(tmp_path: Path) -> None:
    """A non-existent root directory returns the SHA-256 of empty input."""
    import hashlib

    non_existent = tmp_path / "does_not_exist"

    digest = PublishService.hash_workspace_tree(non_existent)

    # SHA-256 of zero bytes — the identity value when no files are fed.
    expected = hashlib.sha256().hexdigest()
    assert digest == expected, (
        "A missing workspace root must return the empty SHA-256 digest"
    )


def test_hash_workspace_tree_symlinks_excluded_from_digest(tmp_path: Path) -> None:
    """Symlinks inside the workspace are skipped; only real files contribute."""
    # Build a workspace with a real file and a symlink that points to a secret.
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET_DATA")

    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "scripts").mkdir()
    (ws / "scripts" / "run.sh").write_text("echo hi")
    (ws / "scripts" / "secret_link").symlink_to(secret)  # symlink to secret

    digest_with_link = PublishService.hash_workspace_tree(ws)

    # Remove the symlink and recompute — digest must be the same.
    (ws / "scripts" / "secret_link").unlink()
    digest_without_link = PublishService.hash_workspace_tree(ws)

    assert digest_with_link == digest_without_link, (
        "Symlinks must be excluded from hash_workspace_tree so they cannot inject "
        "secret content into the digest"
    )


def test_hash_workspace_tree_stable_across_empty_dir(tmp_path: Path) -> None:
    """Empty directories are not files and do not affect the digest."""
    ws_with_empty = tmp_path / "ws_with_empty"
    ws_without = tmp_path / "ws_without"
    _make_env_workspace(ws_with_empty, {"scripts": {"run.sh": "echo hi"}, "empty_dir": None})
    _make_env_workspace(ws_without, {"scripts": {"run.sh": "echo hi"}})

    # Only files contribute; an empty directory changes neither the set of
    # (rel, content) pairs nor the digest.
    assert PublishService.hash_workspace_tree(ws_with_empty) == PublishService.hash_workspace_tree(ws_without), (
        "Empty directories must not affect the hash_workspace_tree digest"
    )
