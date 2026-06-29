"""Unit tests for ``RevisionFormat`` — the canonical revision (de)serializer.

Pure filesystem / logic tests — no DB, no HTTP. Covers Phase 1 of the
git-backed agent versioning plan:

  - ``build_manifest`` produces the schema_version-2 manifest shape (with and
    without an active env).
  - ``write_tree`` captures the ``workspace/`` subtree, hashes it, stamps
    ``content_hash``, and writes the manifest under the chosen filename.
  - Round trip: ``write_tree`` → ``read_manifest`` → ``manifest_to_revision_fields``
    reproduces the prompt / SDK / spec inputs.
  - ``read_manifest`` dispatches the filename and rejects missing / malformed /
    unsupported manifests with ``RevisionFormatError``.
  - ``generate_gitignore`` lists every denylisted top-level name.

The underlying snapshot / hash primitives (``_snapshot_workspace_tree``,
``_hash_tree_with_manifest``) are covered separately in
``tests/unit/test_bundle_workspace_snapshot.py``; this file exercises the
``RevisionFormat`` seam that composes them.
"""
import json
import types
from pathlib import Path

import pytest

from app.services.bundles.revision_format import (
    BUNDLE_MANIFEST_FILENAME,
    GIT_MANIFEST_FILENAME,
    REVISION_SCHEMA_VERSION,
    RevisionFormat,
    RevisionFormatError,
)
from app.services.environments.workspace_classification import (
    BUNDLE_EXCLUDED_TOPLEVEL,
    PLUGIN_DERIVED_FILES,
    RUNTIME_NAME_DENYLIST,
    WORKSPACE_ROOT_REL,
)


# ── helpers ────────────────────────────────────────────────────────────────────


def _make_env_workspace(root: Path, tree: dict) -> None:
    """Recursively create a workspace tree from a dict spec (see snapshot test)."""
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


def _fake_install(**overrides):
    base = dict(
        bundle_id="io.test.revision.format",
        workflow_prompt="WORKFLOW",
        entrypoint_prompt="ENTRY",
        refiner_prompt="REFINER",
        router_trigger_prompt="TRIGGER",
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _fake_env(**overrides):
    base = dict(
        agent_sdk_building="opencode/anthropic",
        agent_sdk_conversation="claude_code/anthropic",
        model_override_building="claude-opus-4",
        model_override_conversation="claude-sonnet-4",
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


_CRED_SPECS = [{"name": "api_key", "type": "api_key", "provided_by": "user"}]
_SCHEDULE_SPECS = [{"name": "nightly", "cron_string": "0 0 * * *", "enabled": True}]
_PLUGIN_SPECS = [{"marketplace_name": "mkt", "plugin_name": "p", "disabled": False}]


# ── build_manifest ──────────────────────────────────────────────────────────────


def test_build_manifest_shape_with_env() -> None:
    manifest = RevisionFormat.build_manifest(
        install=_fake_install(),
        env=_fake_env(),
        cred_specs=_CRED_SPECS,
        schedule_specs=_SCHEDULE_SPECS,
        plugin_specs=_PLUGIN_SPECS,
        revision_number=3,
        version="1.2",
        release_notes="notes",
    )
    assert manifest["schema_version"] == REVISION_SCHEMA_VERSION == 2
    assert manifest["bundle_id"] == "io.test.revision.format"
    assert manifest["revision_number"] == 3
    assert manifest["version"] == "1.2"
    assert manifest["release_notes"] == "notes"
    assert "published_at" in manifest
    assert manifest["prompts"] == {
        "workflow": "WORKFLOW",
        "entrypoint": "ENTRY",
        "refiner": "REFINER",
        "router_trigger": "TRIGGER",
    }
    assert manifest["sdk"] == {
        "building": "opencode/anthropic",
        "conversation": "claude_code/anthropic",
        "model_override_building": "claude-opus-4",
        "model_override_conversation": "claude-sonnet-4",
    }
    assert manifest["required_credential_specs"] == _CRED_SPECS
    assert manifest["schedules"] == _SCHEDULE_SPECS
    assert manifest["plugin_specs"] == _PLUGIN_SPECS
    # content_hash is added by write_tree, not build_manifest.
    assert "content_hash" not in manifest


def test_build_manifest_no_env_nulls_sdk_slots() -> None:
    manifest = RevisionFormat.build_manifest(
        install=_fake_install(),
        env=None,
        cred_specs=[],
        schedule_specs=[],
        plugin_specs=[],
        revision_number=1,
        version=None,
        release_notes=None,
    )
    assert manifest["sdk"] == {
        "building": None,
        "conversation": None,
        "model_override_building": None,
        "model_override_conversation": None,
    }


# ── write_tree ────────────────────────────────────────────────────────────────


def test_write_tree_captures_workspace_and_writes_manifest(tmp_path: Path) -> None:
    env_root = tmp_path / "env"
    _make_env_workspace(
        env_root / WORKSPACE_ROOT_REL,
        {
            "scripts": {"run.sh": "#!/bin/bash\necho hi"},
            "credentials": {"secret.json": "{}"},  # denylisted — excluded
        },
    )
    dest = tmp_path / "snapshot"
    dest.mkdir()

    manifest = RevisionFormat.build_manifest(
        install=_fake_install(),
        env=_fake_env(),
        cred_specs=_CRED_SPECS,
        schedule_specs=[],
        plugin_specs=[],
        revision_number=1,
        version="1.0",
        release_notes=None,
    )
    content_hash = RevisionFormat.write_tree(
        env_workspace_root=env_root, dest=dest, manifest=manifest
    )

    # Returned value is the bare hex digest; manifest stamped with the prefix.
    assert content_hash and ":" not in content_hash
    assert manifest["content_hash"] == f"sha256:{content_hash}"

    # workspace/ subtree captured, denylist applied.
    assert (dest / "workspace" / "scripts" / "run.sh").exists()
    assert not (dest / "workspace" / "credentials").exists()

    # manifest.json on disk matches the (mutated) manifest dict.
    written = json.loads((dest / BUNDLE_MANIFEST_FILENAME).read_text())
    assert written == manifest
    assert written["content_hash"] == f"sha256:{content_hash}"


def test_write_tree_no_env_creates_empty_workspace(tmp_path: Path) -> None:
    dest = tmp_path / "snapshot"
    dest.mkdir()
    manifest = RevisionFormat.build_manifest(
        install=_fake_install(),
        env=None,
        cred_specs=[],
        schedule_specs=[],
        plugin_specs=[],
        revision_number=1,
        version=None,
        release_notes=None,
    )
    RevisionFormat.write_tree(
        env_workspace_root=None, dest=dest, manifest=manifest
    )
    assert (dest / "workspace").is_dir()
    assert list((dest / "workspace").iterdir()) == []


def test_write_tree_git_manifest_filename(tmp_path: Path) -> None:
    dest = tmp_path / "git_tree"
    dest.mkdir()
    manifest = RevisionFormat.build_manifest(
        install=_fake_install(),
        env=_fake_env(),
        cred_specs=[],
        schedule_specs=[],
        plugin_specs=[],
        revision_number=1,
        version="1.0",
        release_notes=None,
    )
    RevisionFormat.write_tree(
        env_workspace_root=None,
        dest=dest,
        manifest=manifest,
        manifest_filename=GIT_MANIFEST_FILENAME,
    )
    assert (dest / GIT_MANIFEST_FILENAME).exists()
    assert not (dest / BUNDLE_MANIFEST_FILENAME).exists()


# ── read_manifest ──────────────────────────────────────────────────────────────


def test_read_manifest_round_trip(tmp_path: Path) -> None:
    """write_tree → read_manifest → manifest_to_revision_fields reproduces input."""
    env_root = tmp_path / "env"
    _make_env_workspace(
        env_root / WORKSPACE_ROOT_REL, {"scripts": {"run.sh": "echo hi"}}
    )
    dest = tmp_path / "snapshot"
    dest.mkdir()

    install = _fake_install()
    env = _fake_env()
    manifest = RevisionFormat.build_manifest(
        install=install,
        env=env,
        cred_specs=_CRED_SPECS,
        schedule_specs=_SCHEDULE_SPECS,
        plugin_specs=_PLUGIN_SPECS,
        revision_number=2,
        version="1.1",
        release_notes="changelog",
    )
    RevisionFormat.write_tree(
        env_workspace_root=env_root, dest=dest, manifest=manifest
    )

    loaded = RevisionFormat.read_manifest(dest)
    assert loaded == manifest

    fields = RevisionFormat.manifest_to_revision_fields(loaded)
    assert fields == {
        "workflow_prompt": "WORKFLOW",
        "entrypoint_prompt": "ENTRY",
        "refiner_prompt": "REFINER",
        "router_trigger_prompt": "TRIGGER",
        "agent_sdk_building": "opencode/anthropic",
        "agent_sdk_conversation": "claude_code/anthropic",
        "model_override_building": "claude-opus-4",
        "model_override_conversation": "claude-sonnet-4",
        "required_credential_specs": _CRED_SPECS,
        "schedules": _SCHEDULE_SPECS,
        "plugin_specs": _PLUGIN_SPECS,
        "version": "1.1",
        "release_notes": "changelog",
    }


def test_read_manifest_dispatches_git_filename(tmp_path: Path) -> None:
    dest = tmp_path / "git_tree"
    dest.mkdir()
    (dest / GIT_MANIFEST_FILENAME).write_text(
        json.dumps({"schema_version": 2, "bundle_id": "io.x"})
    )
    loaded = RevisionFormat.read_manifest(dest)
    assert loaded["bundle_id"] == "io.x"


def test_read_manifest_prefers_bundle_filename(tmp_path: Path) -> None:
    dest = tmp_path / "both"
    dest.mkdir()
    (dest / BUNDLE_MANIFEST_FILENAME).write_text(
        json.dumps({"schema_version": 2, "which": "bundle"})
    )
    (dest / GIT_MANIFEST_FILENAME).write_text(
        json.dumps({"schema_version": 2, "which": "git"})
    )
    assert RevisionFormat.read_manifest(dest)["which"] == "bundle"


def test_read_manifest_missing_raises(tmp_path: Path) -> None:
    dest = tmp_path / "empty"
    dest.mkdir()
    with pytest.raises(RevisionFormatError):
        RevisionFormat.read_manifest(dest)


def test_read_manifest_malformed_json_raises(tmp_path: Path) -> None:
    dest = tmp_path / "bad"
    dest.mkdir()
    (dest / BUNDLE_MANIFEST_FILENAME).write_text("{not json")
    with pytest.raises(RevisionFormatError):
        RevisionFormat.read_manifest(dest)


def test_read_manifest_unsupported_schema_raises(tmp_path: Path) -> None:
    dest = tmp_path / "future"
    dest.mkdir()
    (dest / BUNDLE_MANIFEST_FILENAME).write_text(
        json.dumps({"schema_version": 99, "bundle_id": "io.x"})
    )
    with pytest.raises(RevisionFormatError):
        RevisionFormat.read_manifest(dest)


def test_read_manifest_v1_legacy_supported(tmp_path: Path) -> None:
    """Legacy schema_version 1 manifests remain readable."""
    dest = tmp_path / "v1"
    dest.mkdir()
    (dest / BUNDLE_MANIFEST_FILENAME).write_text(
        json.dumps({"schema_version": 1, "bundle_id": "io.legacy"})
    )
    assert RevisionFormat.read_manifest(dest)["bundle_id"] == "io.legacy"


# ── manifest_to_revision_fields defaults ─────────────────────────────────────────


def test_manifest_to_revision_fields_tolerates_missing_sections() -> None:
    fields = RevisionFormat.manifest_to_revision_fields({"schema_version": 2})
    assert fields["workflow_prompt"] is None
    assert fields["agent_sdk_building"] is None
    assert fields["required_credential_specs"] == []
    assert fields["schedules"] == []
    assert fields["plugin_specs"] == []
    assert fields["version"] is None


# ── generate_gitignore ──────────────────────────────────────────────────────────


def test_generate_gitignore_lists_every_denylisted_name() -> None:
    body = RevisionFormat.generate_gitignore()
    # Every bundle + runtime denylist top-level name appears.
    for name in BUNDLE_EXCLUDED_TOPLEVEL | RUNTIME_NAME_DENYLIST:
        assert name in body, f"{name} missing from generated .gitignore"
    # Every plugin-derived file appears, scoped under plugins/.
    for name in PLUGIN_DERIVED_FILES:
        assert f"plugins/{name}" in body, f"plugins/{name} missing"
    # Stable / non-empty.
    assert body.endswith("\n")
    assert body == RevisionFormat.generate_gitignore()
