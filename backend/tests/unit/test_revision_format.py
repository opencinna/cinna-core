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
    # Stands in for an ``Agent`` row. ``build_manifest`` reads the prompts AND the
    # schema_version-2 ``metadata`` block (agent-row definitional columns) directly
    # off this object, so the stub must carry every column those reads touch — in
    # production an Agent always has these columns.
    base = dict(
        bundle_id="io.test.revision.format",
        workflow_prompt="WORKFLOW",
        entrypoint_prompt="ENTRY",
        refiner_prompt="REFINER",
        router_trigger_prompt="TRIGGER",
        # schema_version-2 metadata block (definitional agent-row fields).
        description="A test agent.",
        example_prompts=["do the thing", "do the other thing"],
        status_refresh_command="/run:status",
        agent_api_enabled=True,
        agent_api_identity_enabled=False,
        a2a_config={"enabled": True},
        agent_sdk_config={"foo": "bar"},
        webapp_enabled=True,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


_EXPECTED_METADATA = {
    "description": "A test agent.",
    "example_prompts": ["do the thing", "do the other thing"],
    "status_refresh_command": "/run:status",
    "agent_api_enabled": True,
    "agent_api_identity_enabled": False,
    "a2a_config": {"enabled": True},
    "agent_sdk_config": {"foo": "bar"},
    "webapp_enabled": True,
}


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
_SKILLS_SUMMARY = [
    {"name": "pdf-report", "description": "Build a PDF report.", "has_scripts": True}
]


# ── build_manifest ──────────────────────────────────────────────────────────────


def test_build_manifest_shape_with_env() -> None:
    manifest = RevisionFormat.build_manifest(
        install=_fake_install(),
        env=_fake_env(),
        cred_specs=_CRED_SPECS,
        schedule_specs=_SCHEDULE_SPECS,
        plugin_specs=_PLUGIN_SPECS,
        skills_summary=_SKILLS_SUMMARY,
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
    # Derived at publish from the workspace tree — never authored.
    assert manifest["skills_summary"] == _SKILLS_SUMMARY
    # schema_version-2 agent-row definitional metadata block.
    assert manifest["metadata"] == _EXPECTED_METADATA
    # content_hash is added by write_tree, not build_manifest.
    assert "content_hash" not in manifest


def test_build_manifest_no_env_nulls_sdk_slots() -> None:
    manifest = RevisionFormat.build_manifest(
        install=_fake_install(),
        env=None,
        cred_specs=[],
        schedule_specs=[],
        plugin_specs=[],
        skills_summary=None,
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
    # The metadata block is read off the install (Agent row), not the env, so it
    # is fully populated even for a prompts-only / no-env revision.
    assert manifest["metadata"] == _EXPECTED_METADATA


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
        skills_summary=[],
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
        skills_summary=None,
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
        skills_summary=[],
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
        skills_summary=_SKILLS_SUMMARY,
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
        "skills_summary": _SKILLS_SUMMARY,
        "version": "1.1",
        "release_notes": "changelog",
        "description": "A test agent.",
        "example_prompts": ["do the thing", "do the other thing"],
        "status_refresh_command": "/run:status",
        "agent_api_enabled": True,
        "agent_api_identity_enabled": False,
        "a2a_config": {"enabled": True},
        "agent_sdk_config": {"foo": "bar"},
        "webapp_enabled": True,
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
    # NOT ``[]``: an absent key means the revision predates agent skills, which
    # the restore side must not read as "this bundle ships no skills". This
    # assertion IS the missing-key discipline.
    assert fields["skills_summary"] is None
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


# ── Tree v3 (kit agent folder as git wire format) ──────────────────────────────
# API-observable checkout/pull/push paths live in
# tests/api/agents/git/agents_git_tree_v3_test.py.

_V3_KIT_ID = "11111111-2222-4333-8444-555555555555"


def _v3_manifest(**overrides) -> dict:
    manifest = RevisionFormat.build_manifest(
        install=_fake_install(),
        env=_fake_env(),
        cred_specs=_CRED_SPECS,
        schedule_specs=_SCHEDULE_SPECS,
        plugin_specs=_PLUGIN_SPECS,
        skills_summary=[],
        revision_number=4,
        version="2.0",
        release_notes="shared",
    )
    manifest["sdk"] = {
        "building": "opencode/anthropic",
        "conversation": "claude-code/anthropic",
        "model_override_building": None,
        "model_override_conversation": None,
    }
    manifest["metadata"] = {
        "description": "Shared agent",
        "example_prompts": ["Do the thing"],
    }
    manifest["prompts"] = {
        "workflow": "WF text",
        "entrypoint": "EP text",
        "refiner": None,
        "router_trigger": "route me",
    }
    manifest.update(overrides)
    return manifest


def _write_v3_tree(root: Path, kit: dict, files: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "cinna-agent.json").write_text(json.dumps(kit))
    _make_env_workspace(root, files)


def test_detect_tree_layout_distinguishes_v2_v3_and_unknown(tmp_path: Path) -> None:
    v2 = tmp_path / "v2"
    (v2 / "workspace").mkdir(parents=True)
    (v2 / GIT_MANIFEST_FILENAME).write_text("{}")
    v3 = tmp_path / "v3"
    v3.mkdir()
    (v3 / "cinna-agent.json").write_text("{}")
    empty = tmp_path / "empty"
    empty.mkdir()
    # workspace/ next to the kit manifest is not a v3 tree (that is a v2 shape)
    mixed = tmp_path / "mixed"
    (mixed / "workspace").mkdir(parents=True)
    (mixed / "cinna-agent.json").write_text("{}")

    assert RevisionFormat.detect_tree_layout(v2) == "v2"
    assert RevisionFormat.detect_tree_layout(v3) == "v3"
    assert RevisionFormat.detect_tree_layout(empty) is None
    assert RevisionFormat.detect_tree_layout(mixed) is None


def test_read_v3_tree_applies_excludes_and_reads_prompts_from_files(
    tmp_path: Path,
) -> None:
    src = tmp_path / "v3"
    _write_v3_tree(
        src,
        {
            "contract_version": "1.9.0",
            "id": _V3_KIT_ID,
            "name": "Shared",
            "description": "Shared agent",
            "example_prompts": ["hello"],
            "router_trigger_prompt": "route me",
            "prompts": {
                "workflow": "docs/WORKFLOW_PROMPT.md",
                "entrypoint": "docs/ENTRYPOINT_PROMPT.md",
            },
            "bundle": {
                "bundle_id": "io.test.v3",
                "revision_number": 7,
                "version": "1.3",
                "content_hash": "sha256:abc",
            },
        },
        {
            "docs": {
                "WORKFLOW_PROMPT.md": "workflow body",
                "ENTRYPOINT_PROMPT.md": "entry body",
            },
            "scripts": {"run.sh": "echo hi"},
            "AGENTS.md": "wrapper",
            "README.md": "readme",
            "Makefile": "all:",
            "credentials": {"x.json": "{}", ".env": "K=V"},
            "app-data": {"desktop.json": "{}"},
            ".env": "SECRET=1",
        },
    )
    dest = tmp_path / "view" / "workspace"

    manifest = RevisionFormat.read_v3_tree(src, dest)

    assert manifest["schema_version"] == REVISION_SCHEMA_VERSION
    assert manifest["bundle_id"] == "io.test.v3"
    assert manifest["revision_number"] == 7
    assert manifest["version"] == "1.3"
    assert manifest["metadata"]["description"] == "Shared agent"
    assert manifest["metadata"]["example_prompts"] == ["hello"]
    # Prompt texts come from the files named in `prompts`; absent kinds are None.
    assert manifest["prompts"]["workflow"] == "workflow body"
    assert manifest["prompts"]["entrypoint"] == "entry body"
    assert manifest["prompts"]["refiner"] is None
    assert manifest["prompts"]["router_trigger"] == "route me"

    # cloud_import_excludes applied: content copied, wrapper/secret paths not.
    assert (dest / "scripts" / "run.sh").read_text() == "echo hi"
    assert (dest / "docs" / "WORKFLOW_PROMPT.md").is_file()
    for excluded in (
        "AGENTS.md", "README.md", "Makefile", "credentials", "app-data", ".env",
    ):
        assert not (dest / excluded).exists(), excluded
    # The source clone is never mutated.
    assert (src / "AGENTS.md").is_file()
    assert (src / "credentials" / ".env").is_file()


def test_read_v3_tree_without_bundle_block_has_no_bundle_id(tmp_path: Path) -> None:
    src = tmp_path / "desktop_made"
    _write_v3_tree(
        src,
        {
            "contract_version": "1.9.0",
            "id": _V3_KIT_ID,
            "name": "Desk",
            "description": "Made on the desktop",
        },
        {"docs": {"WORKFLOW_PROMPT.md": "wf"}},
    )
    manifest = RevisionFormat.read_v3_tree(src, tmp_path / "view" / "workspace")
    assert "bundle_id" not in manifest
    assert manifest["prompts"]["workflow"] == "wf"
    assert manifest["metadata"]["description"] == "Made on the desktop"


def test_read_v3_tree_never_follows_symlinked_prompt_or_content(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("TOP SECRET")
    src = tmp_path / "v3"
    _write_v3_tree(
        src,
        {"contract_version": "1.9.0", "id": _V3_KIT_ID, "name": "S", "description": "d"},
        {"docs": {}},
    )
    (src / "docs" / "WORKFLOW_PROMPT.md").symlink_to(outside)
    (src / "link.txt").symlink_to(outside)
    dest = tmp_path / "view" / "workspace"

    manifest = RevisionFormat.read_v3_tree(src, dest)

    assert manifest["prompts"]["workflow"] is None
    assert not (dest / "link.txt").exists()


def test_write_v3_tree_round_trips_through_reader(tmp_path: Path) -> None:
    env_root = tmp_path / "env"
    _make_env_workspace(
        env_root / WORKSPACE_ROOT_REL,
        {
            "scripts": {"run.sh": "echo hi"},
            "credentials": {"secret.json": "{}"},
        },
    )
    manifest = _v3_manifest()
    original = json.loads(json.dumps(manifest))
    dest = tmp_path / "clone"
    dest.mkdir()

    result = RevisionFormat.write_v3_tree(
        env_workspace_root=env_root,
        dest=dest,
        manifest=manifest,
        existing_kit_manifest=None,
        name="Shared Agent",
    )
    content_hash = result.content_hash
    assert result.dropped_paths == []

    assert content_hash and ":" not in content_hash
    assert RevisionFormat.detect_tree_layout(dest) == "v3"
    assert (dest / "scripts" / "run.sh").read_text() == "echo hi"
    assert not (dest / "credentials").exists()
    assert not (dest / "workspace").exists()
    assert (dest / "docs" / "WORKFLOW_PROMPT.md").read_text() == "WF text"
    assert (dest / "docs" / "ENTRYPOINT_PROMPT.md").read_text() == "EP text"
    assert (dest / ".gitignore").is_file()

    kit = json.loads((dest / "cinna-agent.json").read_text())
    assert kit["contract_version"] == "1.9.0"
    assert kit["name"] == "Shared Agent"
    assert kit["slug"] == "shared-agent"
    assert kit["description"] == "Shared agent"
    assert kit["router_trigger_prompt"] == "route me"
    # claude-code/anthropic conversation SDK → "claude" engine
    assert kit["runtime"]["engine"] == "claude"
    assert uuid_like(kit["id"])
    assert kit["bundle"]["bundle_id"] == original["bundle_id"]
    assert "prompts" not in kit["bundle"]

    # Reader yields the input manifest back (minus content_hash / the prompt
    # kinds that were None, which the reader reports as None).
    back = RevisionFormat.read_v3_tree(dest, tmp_path / "view" / "workspace")
    assert manifest["content_hash"] == f"sha256:{content_hash}"
    for key in (
        "bundle_id", "revision_number", "version", "published_at", "sdk",
        "required_credential_specs", "schedules", "plugin_specs", "release_notes",
    ):
        assert back[key] == original[key], key
    assert back["content_hash"] == manifest["content_hash"]
    assert back["metadata"]["description"] == "Shared agent"
    assert back["metadata"]["example_prompts"] == ["Do the thing"]
    assert back["prompts"]["workflow"] == "WF text"
    assert back["prompts"]["entrypoint"] == "EP text"
    assert back["prompts"]["router_trigger"] == "route me"


def uuid_like(value: str) -> bool:
    import uuid

    try:
        uuid.UUID(value)
    except (ValueError, TypeError):
        return False
    return True


def test_write_v3_tree_preserves_unknown_kit_keys_id_and_excluded_files(
    tmp_path: Path,
) -> None:
    env_root = tmp_path / "env"
    _make_env_workspace(
        env_root / WORKSPACE_ROOT_REL,
        {"scripts": {"new.sh": "echo new"}},
    )
    dest = tmp_path / "clone"
    # A desktop teammate's previous export: old content, their AGENTS.md, and a
    # file the reader would import that the new export no longer carries.
    _make_env_workspace(
        dest,
        {
            "AGENTS.md": "teammate wrapper",
            "README.md": "teammate readme",
            "scripts": {"old.sh": "echo old"},
            "stale.txt": "gone after export",
        },
    )
    existing = {
        "contract_version": "1.7.0",
        "id": _V3_KIT_ID,
        "name": "Old name",
        "slug": "kept-slug",
        "x-teammate": {"custom": True},
        "runtime": {"engine": "opencode", "extra": 1},
    }
    manifest = _v3_manifest()

    RevisionFormat.write_v3_tree(
        env_workspace_root=env_root,
        dest=dest,
        manifest=manifest,
        existing_kit_manifest=existing,
        name="New name",
    )

    kit = json.loads((dest / "cinna-agent.json").read_text())
    assert kit["id"] == _V3_KIT_ID
    assert kit["x-teammate"] == {"custom": True}
    assert kit["slug"] == "kept-slug"
    assert kit["name"] == "New name"
    assert kit["contract_version"] == "1.9.0"
    assert kit["runtime"] == {"engine": "claude", "extra": 1}
    # Prune only removed what the reader would import.
    assert (dest / "AGENTS.md").read_text() == "teammate wrapper"
    assert (dest / "README.md").read_text() == "teammate readme"
    assert not (dest / "stale.txt").exists()
    assert not (dest / "scripts" / "old.sh").exists()
    assert (dest / "scripts" / "new.sh").read_text() == "echo new"
    # the existing dict handed in is not mutated
    assert existing["contract_version"] == "1.7.0"


def test_write_v3_tree_engine_opencode_and_invalid_slug_falls_back(
    tmp_path: Path,
) -> None:
    env_root = tmp_path / "env"
    _make_env_workspace(env_root / WORKSPACE_ROOT_REL, {"a.txt": "x"})
    manifest = _v3_manifest()
    manifest["sdk"] = {"building": "opencode/anthropic", "conversation": "opencode/anthropic"}
    dest = tmp_path / "clone"
    dest.mkdir()

    RevisionFormat.write_v3_tree(
        env_workspace_root=env_root, dest=dest, manifest=manifest,
        existing_kit_manifest=None, name="!!!", slug="Bad Slug",
    )
    kit = json.loads((dest / "cinna-agent.json").read_text())
    assert kit["runtime"]["engine"] == "opencode"
    # "!!!" slugifies to nothing usable → the safe fallback, never the bad slug
    assert kit["slug"] == "agent"


def test_kit_path_excluded_semantics() -> None:
    from app.services.bundles.revision_format import kit_path_excluded

    pats = ["credentials/", "**/.env", "AGENTS.md", "**/*.pem", ".git/"]
    # trailing slash covers the dir and everything under it
    assert kit_path_excluded("credentials", pats)
    assert kit_path_excluded("credentials/x.json", pats)
    # ** spans any depth including zero segments
    assert kit_path_excluded(".env", pats)
    assert kit_path_excluded("a/b/.env", pats)
    assert kit_path_excluded("keys/server.pem", pats)
    # patterns without leading ** are anchored at the agent root
    assert kit_path_excluded("AGENTS.md", pats)
    assert not kit_path_excluded("docs/AGENTS.md", pats)
    # `*` does not cross segments; near-misses are kept
    assert not kit_path_excluded(".env.example", pats)
    assert not kit_path_excluded("scripts/run.sh", pats)
    assert not kit_path_excluded("credentials.txt", pats)
    # normalisation: ./ and leading slash tolerated
    assert kit_path_excluded("./credentials/x", pats)
    # empty path / pattern never match
    assert not kit_path_excluded("", pats)
    assert not kit_path_excluded("a", ["", "  "])


def test_v3_write_example_prompts_are_cleaned_and_empty_description_round_trips(
    tmp_path: Path,
) -> None:
    env_root = tmp_path / "env"
    _make_env_workspace(env_root / WORKSPACE_ROOT_REL, {"a.txt": "x"})
    manifest = _v3_manifest()
    manifest["metadata"] = {
        "description": "",
        "example_prompts": ["ok", "", "   ", "y" * 900] + [f"p{i}" for i in range(40)],
    }
    dest = tmp_path / "clone"
    dest.mkdir()
    RevisionFormat.write_v3_tree(
        env_workspace_root=env_root, dest=dest, manifest=manifest,
        existing_kit_manifest=None, name="Named",
    )
    kit = json.loads((dest / "cinna-agent.json").read_text())
    prompts = kit["example_prompts"]
    assert len(prompts) <= 20
    assert all(p.strip() and len(p) <= 500 for p in prompts)
    assert prompts[0] == "ok"

    back = RevisionFormat.read_v3_tree(dest, tmp_path / "view" / "workspace")
    # an empty description stays empty; the name must not leak back as one
    assert back["metadata"].get("description", "") in ("", None)
    assert back["metadata"]["description"] != "Named"


def test_v3_write_preserves_a_custom_prompts_map(tmp_path: Path) -> None:
    env_root = tmp_path / "env"
    _make_env_workspace(env_root / WORKSPACE_ROOT_REL, {"a.txt": "x"})
    dest = tmp_path / "clone"
    dest.mkdir()
    existing = {
        "id": _V3_KIT_ID,
        "prompts": {"workflow": "prompts/main.md", "entrypoint": "prompts/entry.md"},
    }
    RevisionFormat.write_v3_tree(
        env_workspace_root=env_root, dest=dest, manifest=_v3_manifest(),
        existing_kit_manifest=existing, name="N",
    )
    kit = json.loads((dest / "cinna-agent.json").read_text())
    assert kit["prompts"]["workflow"] == "prompts/main.md"
    assert (dest / "prompts" / "main.md").read_text() == "WF text"
    back = RevisionFormat.read_v3_tree(dest, tmp_path / "view" / "workspace")
    assert back["prompts"]["workflow"] == "WF text"
