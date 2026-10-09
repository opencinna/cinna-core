"""RevisionFormat — the single (de)serializer for the canonical revision layout.

A bundle revision on disk is a directory holding:

* a **manifest** (``manifest.json`` for bundle storage, ``cinna.agent.json`` for
  git trees) — schema_version 2: prompts, SDK + model overrides,
  ``required_credential_specs``, ``schedules``, ``plugin_specs``, a derived
  ``skills_summary``, a ``metadata``
  block of agent-row definitional fields (``description``, ``example_prompts``,
  ``status_refresh_command``, ``agent_api_enabled``,
  ``agent_api_identity_enabled``, ``a2a_config``, ``agent_sdk_config``,
  ``webapp_enabled``), ``version``, ``release_notes`` and a SHA-256
  ``content_hash``;
* a ``workspace/`` subtree — a verbatim copy of the env ``app/workspace/`` minus
  the runtime / per-user / secret denylist (see
  :mod:`app.services.environments.workspace_classification`);
* an optional generated ``.gitignore`` (git trees) listing the same denylist so
  excluded state can never be committed.

**Tree v3** (kit contract 1.8.0) is a git wire format only: the kit agent folder
as-is — ``cinna-agent.json`` at the tree root, the workspace content beside it,
the prompts in the files the kit manifest names, and the revision block under the
manifest's ``bundle`` key. :meth:`RevisionFormat.read_v3_tree` turns such a tree
into a v2 view (``workspace/`` + a v2-equivalent manifest) so every install /
checkout path stays v2; :meth:`RevisionFormat.write_v3_tree` is the export side.
Bundle storage never holds v3.

This module is the one place that knows that layout. The write side
(:meth:`RevisionFormat.build_manifest` + :meth:`RevisionFormat.write_tree`) is
what :class:`~app.services.bundles.publish_service.PublishService` delegates to;
the read side (:meth:`RevisionFormat.read_manifest` +
:meth:`RevisionFormat.manifest_to_revision_fields`) maps an on-disk manifest back
into :class:`~app.models.bundles.agent_bundle_revision.AgentBundleRevision`
constructor kwargs. ``manifest.json`` is byte-for-byte the schema_version-2
bundle snapshot layout, so git checkout / pull / push (later phases) reduce to
the same operations the platform already performs.

The actual filesystem primitives (``_snapshot_workspace_tree``,
``_copy_plugins_tree``, ``_hash_tree_with_manifest``) and the denylist / symlink
guards (``iter_bundle_toplevel`` / ``safe_copytree``) are **reused verbatim** —
this module composes them, it does not reimplement them.
"""
from __future__ import annotations

import copy
import functools
import json
import logging
import re
import shutil
import tempfile
import uuid
from datetime import datetime, UTC
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NamedTuple

from app.services.environments.workspace_classification import (
    BUNDLE_EXCLUDED_TOPLEVEL,
    NESTED_EXCLUDED_DIRS,
    NESTED_EXCLUDED_FILE_GLOBS,
    PLUGIN_DERIVED_FILES,
    PLUGINS_DIRNAME,
    RUNTIME_NAME_DENYLIST,
)

if TYPE_CHECKING:
    from app.models.agents.agent import Agent
    from app.models.environments.environment import AgentEnvironment

logger = logging.getLogger(__name__)


# Canonical schema version written by every new revision.
REVISION_SCHEMA_VERSION = 2

# Schema versions the reader accepts. Legacy v1 (flat allowlist) snapshots still
# exist on disk and remain readable; the on-disk layout is dispatched separately
# by ``workspace_classification.snapshot_layout``.
SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({1, 2, 3})

# Stamped on the ``bundle`` block of a tree-v3 manifest; the v2 view the reader
# returns carries :data:`REVISION_SCHEMA_VERSION` again.
TREE_V3_SCHEMA_VERSION = 3

# Manifest filename used by bundle storage vs. a git tree. This is the ONLY
# git-vs-bundle difference in the layout.
BUNDLE_MANIFEST_FILENAME = "manifest.json"
GIT_MANIFEST_FILENAME = "cinna.agent.json"

# Reader dispatch order: bundle filename first, then the git filename.
_MANIFEST_FILENAMES = (BUNDLE_MANIFEST_FILENAME, GIT_MANIFEST_FILENAME)

# Tree v3: the kit agent folder manifest, at the tree root.
KIT_MANIFEST_FILENAME = "cinna-agent.json"

# Marker on a v2 manifest read from (or persisted for) a tree v3, kept outside
# ``metadata`` so no settings diff or restore ever reads it. The workspace diff
# uses it to project the live side the way a v3 tree sees it
# (:meth:`RevisionFormat.project_v3_view`).
TREE_LAYOUT_KEY = "tree_layout"

# Contract version a v3 export stamps into ``cinna-agent.json``.
KIT_CONTRACT_VERSION = "1.8.0"

# Prompt kinds stored as files in a kit folder (router_trigger is a manifest field).
_KIT_PROMPT_KINDS = ("workflow", "entrypoint", "refiner")
_KIT_DEFAULT_PROMPT_FILES = {
    "workflow": "docs/WORKFLOW_PROMPT.md",
    "entrypoint": "docs/ENTRYPOINT_PROMPT.md",
    "refiner": "docs/REFINER_PROMPT.md",
}
# Kit top-level fields that mirror the v2 ``metadata`` block. The kit value wins
# on read, so a teammate editing the folder on the desktop is honoured.
_KIT_METADATA_FIELDS = ("description", "example_prompts", "status_refresh_command")
_KIT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# Kit schema limits on ``example_prompts`` (``cinna-agent.schema.json``); an
# export outside them would be refused by the library's push policy.
_KIT_EXAMPLE_PROMPTS_MAX_ITEMS = 20
_KIT_EXAMPLE_PROMPT_MAX_CHARS = 500

TreeLayout = Literal["v2", "v3"]


class V3WriteResult(NamedTuple):
    """What :meth:`RevisionFormat.write_v3_tree` produced."""

    #: Bare hex digest, also stamped as ``manifest["content_hash"]``.
    content_hash: str
    #: Workspace paths (top-most, agent-root-relative) that do not travel in a
    #: v3 tree because ``cloud_import_excludes`` drops them, e.g. ``README.md``.
    dropped_paths: list[str]


class RevisionFormatError(Exception):
    """Raised when a revision manifest is missing, malformed, or unsupported."""


class RevisionFormat:
    """(De)serializer for the canonical bundle-revision layout.

    All methods are static — the format is stateless. Write side: publish /
    git push. Read side: install / git checkout / git pull.
    """

    # ── Write side ─────────────────────────────────────────────────

    @staticmethod
    def build_manifest(
        *,
        install: "Agent",
        env: "AgentEnvironment | None",
        cred_specs: list[dict],
        schedule_specs: list[dict],
        plugin_specs: list[dict],
        skills_summary: list[dict] | None,
        revision_number: int,
        version: str | None,
        release_notes: str | None,
    ) -> dict:
        """Build the canonical schema_version-2 manifest dict.

        Single source of the manifest schema. ``content_hash`` is added later
        by :meth:`write_tree` (it depends on the captured tree). When ``env`` is
        ``None`` (no active env / prompts-only revision) the SDK + model-override
        slots are ``None``.
        """
        return {
            # schema_version 2: the workspace lives under a ``workspace/``
            # subtree (full-tree capture minus the denylist). v1 revisions
            # used a flat allowlist layout; the consumer reader dispatches
            # on this value / the on-disk shape.
            "schema_version": REVISION_SCHEMA_VERSION,
            "bundle_id": install.bundle_id,
            "revision_number": revision_number,
            "version": version,
            "published_at": datetime.now(UTC).isoformat(),
            "prompts": {
                "workflow": install.workflow_prompt,
                "entrypoint": install.entrypoint_prompt,
                "refiner": install.refiner_prompt,
                "router_trigger": install.router_trigger_prompt,
            },
            "sdk": {
                "building": env.agent_sdk_building if env else None,
                "conversation": env.agent_sdk_conversation if env else None,
                "model_override_building": (
                    getattr(env, "model_override_building", None) if env else None
                ),
                "model_override_conversation": (
                    getattr(env, "model_override_conversation", None) if env else None
                ),
            },
            "required_credential_specs": cred_specs,
            "schedules": schedule_specs,
            "plugin_specs": plugin_specs,
            # Derived, never authored — same discipline as ``content_hash``.
            # ``[]`` means "this agent has no skills"; the key being ABSENT
            # (only possible on a revision published before the feature) is what
            # ``manifest_to_revision_fields`` maps to a NULL column.
            "skills_summary": skills_summary if skills_summary is not None else [],
            # Agent-row definitional metadata (additive, schema_version 2). These
            # are plain columns read off the publisher's ``Agent`` row — they
            # define the agent beyond its prompts/SDK and must survive a snapshot
            # round-trip. Enablement flags (``agent_api_enabled`` /
            # ``a2a_config.enabled``) DO travel; the per-install tokens / grants
            # / UI prefs they gate do NOT (see plan exclusions).
            "metadata": {
                "description": install.description,
                "example_prompts": install.example_prompts,
                "status_refresh_command": install.status_refresh_command,
                "agent_api_enabled": install.agent_api_enabled,
                "agent_api_identity_enabled": install.agent_api_identity_enabled,
                "a2a_config": install.a2a_config,
                "agent_sdk_config": install.agent_sdk_config,
                "webapp_enabled": install.webapp_enabled,
            },
            "release_notes": release_notes,
        }

    @staticmethod
    def write_tree(
        *,
        env_workspace_root: Path | None,
        dest: Path,
        manifest: dict,
        manifest_filename: str = BUNDLE_MANIFEST_FILENAME,
    ) -> str:
        """Serialize a revision tree into ``dest`` and return its content hash.

        Steps (identical to the inline publish logic this replaces):

        1. Capture the full ``app/workspace/`` tree into ``dest/workspace/`` via
           :meth:`PublishService._snapshot_workspace_tree` (denylist + symlink
           guards apply). When ``env_workspace_root`` is ``None`` the
           ``workspace/`` subtree is created empty (prompts-only revision).
        2. Compute the SHA-256 ``content_hash`` over the captured tree + the
           manifest body via :meth:`PublishService._hash_tree_with_manifest`.
        3. Stamp ``manifest["content_hash"] = "sha256:<hash>"`` (mutates the
           passed dict in place, so the caller's manifest matches what is
           written to disk) and write ``dest/<manifest_filename>``.

        Returns the bare hex digest (no ``sha256:`` prefix) — the value the
        revision row stores in its ``content_hash`` column. ``manifest_filename``
        is the only git-vs-bundle difference (``cinna.agent.json`` for git trees,
        ``manifest.json`` for bundle storage).
        """
        from app.services.bundles.publish_service import PublishService

        PublishService._snapshot_workspace_tree(env_workspace_root, dest)
        content_hash = PublishService._hash_tree_with_manifest(dest, manifest)
        manifest["content_hash"] = f"sha256:{content_hash}"
        (dest / manifest_filename).write_text(json.dumps(manifest, indent=2))
        return content_hash

    # ── Read side ──────────────────────────────────────────────────

    @staticmethod
    def read_manifest(snapshot_path: Path) -> dict:
        """Load + validate a revision manifest from ``snapshot_path``.

        Dispatches the filename (``manifest.json`` then ``cinna.agent.json``) and
        validates ``schema_version`` against :data:`SUPPORTED_SCHEMA_VERSIONS`.
        Raises :class:`RevisionFormatError` when no manifest is present, it is not
        valid JSON / not an object, or its ``schema_version`` is unsupported.
        """
        for filename in _MANIFEST_FILENAMES:
            candidate = snapshot_path / filename
            if not candidate.exists():
                continue
            try:
                raw = candidate.read_text()
                manifest = json.loads(raw)
            except (OSError, json.JSONDecodeError) as exc:
                raise RevisionFormatError(
                    f"Manifest {candidate} could not be read as JSON: {exc}"
                ) from exc
            if not isinstance(manifest, dict):
                raise RevisionFormatError(
                    f"Manifest {candidate} is not a JSON object"
                )
            schema_version = manifest.get("schema_version")
            if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
                raise RevisionFormatError(
                    f"Manifest {candidate} has unsupported schema_version "
                    f"{schema_version!r} (supported: "
                    f"{sorted(SUPPORTED_SCHEMA_VERSIONS)})"
                )
            return manifest
        raise RevisionFormatError(
            f"No revision manifest found at {snapshot_path} "
            f"(looked for {', '.join(_MANIFEST_FILENAMES)})"
        )

    @staticmethod
    def manifest_to_revision_fields(manifest: dict) -> dict:
        """Map a manifest dict to ``AgentBundleRevision`` constructor kwargs.

        The inverse of :meth:`build_manifest`: the prompts / SDK / model-override
        / spec fields a revision row carries. ``bundle_id`` (FK uuid),
        ``revision_number``, ``snapshot_path``, ``content_hash``,
        ``published_by_user_id`` and the raw ``manifest`` are NOT produced here —
        they are caller-supplied (they are not derivable from the manifest body
        alone). This is the shape both publish (revision-row construction) and
        Phase 3 checkout consume.
        """
        prompts = manifest.get("prompts") or {}
        sdk = manifest.get("sdk") or {}
        # Agent-row metadata block (additive). ``.get()`` yields ``None`` when the
        # key is absent — and these revision columns are nullable — so a snapshot
        # that predates a field maps to a NULL column, which the restore side
        # treats as "do not overwrite" (missing-key-tolerant). Never coerce to an
        # empty default here: that would erase the absent-vs-present distinction.
        metadata = manifest.get("metadata") or {}
        return {
            "workflow_prompt": prompts.get("workflow"),
            "entrypoint_prompt": prompts.get("entrypoint"),
            "refiner_prompt": prompts.get("refiner"),
            "router_trigger_prompt": prompts.get("router_trigger"),
            "agent_sdk_building": sdk.get("building"),
            "agent_sdk_conversation": sdk.get("conversation"),
            "model_override_building": sdk.get("model_override_building"),
            "model_override_conversation": sdk.get("model_override_conversation"),
            "required_credential_specs": manifest.get("required_credential_specs") or [],
            "schedules": manifest.get("schedules") or [],
            "plugin_specs": manifest.get("plugin_specs") or [],
            # ``.get()`` with NO ``or []``: absent means "predates agent skills"
            # and must stay NULL, while a published-with-none revision carries a
            # real empty list. Collapsing the two would tell a consumer that an
            # old bundle definitely ships no skills, which we do not know.
            "skills_summary": manifest.get("skills_summary"),
            "version": manifest.get("version"),
            "release_notes": manifest.get("release_notes"),
            "description": metadata.get("description"),
            "example_prompts": metadata.get("example_prompts"),
            "status_refresh_command": metadata.get("status_refresh_command"),
            "agent_api_enabled": metadata.get("agent_api_enabled"),
            "agent_api_identity_enabled": metadata.get("agent_api_identity_enabled"),
            "a2a_config": metadata.get("a2a_config"),
            "agent_sdk_config": metadata.get("agent_sdk_config"),
            "webapp_enabled": metadata.get("webapp_enabled"),
        }

    # ── Tree v3 (kit agent folder) ─────────────────────────────────

    @staticmethod
    def detect_tree_layout(src: Path) -> TreeLayout | None:
        """``"v2"`` (``workspace/`` + ``cinna.agent.json``), ``"v3"``
        (``cinna-agent.json`` at the root, no ``workspace/``) or ``None``."""
        has_workspace = (src / "workspace").is_dir()
        if has_workspace and (src / GIT_MANIFEST_FILENAME).is_file():
            return "v2"
        if not has_workspace and (src / KIT_MANIFEST_FILENAME).is_file():
            return "v3"
        return None

    @staticmethod
    def read_v3_tree(src: Path, dest_workspace: Path) -> dict:
        """Materialise a v3 tree as a v2 workspace and return a v2 manifest.

        Copies ``src`` into ``dest_workspace`` minus the kit's
        ``cloud_import_excludes`` (the rule ``cinna agent import`` applies;
        symlinks are never followed) and builds the v2-equivalent manifest: the
        ``bundle`` block, overlaid with the kit's top-level metadata fields and
        the prompt texts read from the files named in ``prompts``. A tree without
        ``bundle`` (made on the desktop) yields a manifest without ``bundle_id``.
        ``src`` is never modified.
        """
        kit_manifest = _load_kit_manifest(src)
        layout = _kit_layout()
        excludes = _cloud_import_excludes(layout)

        dest_workspace.mkdir(parents=True, exist_ok=True, mode=0o755)
        # The root manifest is metadata (it becomes the v2 manifest below), the
        # way ``cinna.agent.json`` sits outside a v2 tree's ``workspace/``.
        _copy_importable(src, dest_workspace, excludes, skip={KIT_MANIFEST_FILENAME})

        bundle = kit_manifest.get("bundle")
        manifest: dict = copy.deepcopy(bundle) if isinstance(bundle, dict) else {}
        manifest["schema_version"] = REVISION_SCHEMA_VERSION

        metadata = dict(manifest.get("metadata") or {})
        for field in _KIT_METADATA_FIELDS:
            if field not in kit_manifest:
                continue
            # The writer falls back to the name for the schema-required
            # description; that placeholder is not a real description.
            if (
                field == "description"
                and not metadata.get("description")
                and kit_manifest.get("description") == kit_manifest.get("name")
                and isinstance(bundle, dict)
            ):
                continue
            metadata[field] = kit_manifest[field]
        if metadata or "metadata" in manifest:
            manifest["metadata"] = metadata

        prompt_files = kit_manifest.get("prompts")
        if not isinstance(prompt_files, dict):
            prompt_files = _layout_prompt_files(layout)
        prompts: dict = {
            kind: _read_tree_text(src, prompt_files.get(kind))
            for kind in _KIT_PROMPT_KINDS
        }
        prompts["router_trigger"] = kit_manifest.get("router_trigger_prompt")
        manifest["prompts"] = prompts
        manifest[TREE_LAYOUT_KEY] = "v3"
        return manifest

    @staticmethod
    def is_v3_manifest(manifest: object) -> bool:
        """Whether a stored revision manifest came from / went to a tree v3."""
        return isinstance(manifest, dict) and manifest.get(TREE_LAYOUT_KEY) == "v3"

    @staticmethod
    def project_v3_view(workspace: Path) -> None:
        """Reduce a captured workspace (in place) to what a tree v3 carries.

        Deletes every path ``cloud_import_excludes`` drops and the root
        ``cinna-agent.json`` — exactly what :meth:`read_v3_tree` leaves out —
        so a live capture compares equal to a v3 baseline. Only ever call it on
        a temporary copy, never on an env workspace.
        """
        excludes = _cloud_import_excludes(_kit_layout())
        doomed = [workspace / rel for rel in _excluded_paths(workspace, excludes)]
        doomed.append(workspace / KIT_MANIFEST_FILENAME)
        for path in doomed:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists() or path.is_symlink():
                path.unlink(missing_ok=True)

    @staticmethod
    def write_v3_tree(
        *,
        env_workspace_root: Path | None,
        dest: Path,
        manifest: dict,
        existing_kit_manifest: dict | None,
        name: str,
        slug: str | None = None,
    ) -> V3WriteResult:
        """Serialize a revision as a v3 tree into ``dest``.

        * Prunes from ``dest`` only paths the reader would import: anything
          matching ``cloud_import_excludes`` (``.git/``, ``AGENTS.md``,
          ``README.md``, ``Makefile``, ``publications.json``, ...) is left alone,
          so a desktop teammate's files survive a cloud export.
        * Captures the workspace with ``PublishService._snapshot_workspace_tree``
          into a private temp stage and copies its importable paths to the
          ``dest`` root. Workspace paths the excludes drop do not travel; they
          are returned as ``dropped_paths`` so the caller can say so. A caller
          that needs the tree as a later checkout sees it reads ``dest`` back
          with :meth:`read_v3_tree`.
        * Writes the prompt files (to the paths the existing kit manifest names,
          else the kit defaults), ``cinna-agent.json`` (``existing_kit_manifest``
          with unknown keys preserved, ``id`` kept or minted once, ``bundle`` =
          the rest of the v2 manifest) and the kit agent ``.gitignore``.

        The hash is computed over the captured workspace + the v2 manifest, the
        same inputs as :meth:`write_tree`; ``manifest["content_hash"]`` is
        stamped in place.
        """
        from app.services.bundles.publish_service import PublishService

        layout = _kit_layout()
        excludes = _cloud_import_excludes(layout)
        stage_root = Path(tempfile.mkdtemp(prefix="v3stage-"))
        try:
            PublishService._snapshot_workspace_tree(env_workspace_root, stage_root)
            content_hash = PublishService._hash_tree_with_manifest(stage_root, manifest)
            manifest["content_hash"] = f"sha256:{content_hash}"

            dest.mkdir(parents=True, exist_ok=True)
            _prune_importable(dest, dest, excludes)
            _copy_importable(stage_root / "workspace", dest, excludes)
            dropped = _excluded_paths(stage_root / "workspace", excludes)
        finally:
            shutil.rmtree(stage_root, ignore_errors=True)

        prompts = manifest.get("prompts") or {}
        prompt_files = _kit_prompt_files(existing_kit_manifest, dest)
        for kind in _KIT_PROMPT_KINDS:
            text = prompts.get(kind)
            if text is None:
                continue
            target = dest / prompt_files[kind]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")

        kit_manifest = _build_kit_manifest(
            manifest=manifest,
            existing=existing_kit_manifest,
            name=name,
            slug=slug,
            prompt_files=prompt_files,
        )
        (dest / KIT_MANIFEST_FILENAME).write_text(
            json.dumps(kit_manifest, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (dest / ".gitignore").write_text(_kit_agent_gitignore(), encoding="utf-8")
        return V3WriteResult(content_hash=content_hash, dropped_paths=dropped)

    # ── Git ignore ─────────────────────────────────────────────────

    @staticmethod
    def generate_gitignore() -> str:
        """Emit the ``.gitignore`` body for a git-backed revision tree.

        Single source of truth for what can never be committed: the bundle
        top-level denylist (:data:`BUNDLE_EXCLUDED_TOPLEVEL`), the runtime
        dotfile denylist (:data:`RUNTIME_NAME_DENYLIST`), the per-consumer
        plugin-derived files (:data:`PLUGIN_DERIVED_FILES`, scoped under
        ``plugins/``), and the recursive cache-artifact denylist
        (:data:`NESTED_EXCLUDED_DIRS` / :data:`NESTED_EXCLUDED_FILE_GLOBS`,
        e.g. ``__pycache__/`` and ``*.pyc`` at any depth). Derived from the same
        constants the snapshot copy walk uses, so the ``.gitignore`` and the copy
        denylist can never disagree.

        Top-level entries are scoped to the ``workspace/`` subtree (where the
        captured tree lives); cache patterns are unscoped so they match at any
        depth (also helping developers who clone and run the agent locally).
        Sorted for a stable, diff-friendly output.
        """
        # A pattern without a trailing slash matches both files and directories
        # (and a matched directory's contents), so we deliberately do NOT try to
        # classify each denylist name as file-vs-dir: a bare ``workspace/<name>``
        # fails safe for any future denylist entry regardless of type.
        toplevel_names = BUNDLE_EXCLUDED_TOPLEVEL | RUNTIME_NAME_DENYLIST
        lines: list[str] = [
            "# Auto-generated by Cinna RevisionFormat — do not edit.",
            "# Runtime / per-user / secret state that is never part of a bundle.",
        ]
        for name in sorted(toplevel_names):
            lines.append(f"workspace/{name}")
        # Per-consumer plugin-derived files live at the plugins root and are
        # regenerated per install — never committed.
        for name in sorted(PLUGIN_DERIVED_FILES):
            lines.append(f"workspace/{PLUGINS_DIRNAME}/{name}")
        # Regenerated language / tooling caches at ANY depth. A bare,
        # leading-slash-free pattern matches recursively in every directory.
        for name in sorted(NESTED_EXCLUDED_DIRS):
            lines.append(f"{name}/")
        for pattern in sorted(NESTED_EXCLUDED_FILE_GLOBS):
            lines.append(pattern)
        return "\n".join(lines) + "\n"


# ── Tree v3 helpers ────────────────────────────────────────────────


def _kit_dir() -> Path:
    from app.services.cli.platform_knowledge_assets import local_kit_dir

    return local_kit_dir()


def _kit_layout() -> dict:
    """The kit's ``layout.json``; a missing or unreadable one is an error."""
    path = _kit_dir() / "layout.json"
    try:
        layout = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RevisionFormatError(f"Kit layout {path} could not be read: {exc}") from exc
    if not isinstance(layout, dict):
        raise RevisionFormatError(f"Kit layout {path} is not a JSON object")
    return layout


def _cloud_import_excludes(layout: dict) -> list[str]:
    patterns = layout.get("cloud_import_excludes")
    if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
        raise RevisionFormatError("Kit layout has no usable 'cloud_import_excludes'")
    return [p for p in patterns if p.strip()]


def _layout_prompt_files(layout: dict) -> dict:
    agent = layout.get("agent")
    files = agent.get("prompt_files") if isinstance(agent, dict) else None
    return files if isinstance(files, dict) else dict(_KIT_DEFAULT_PROMPT_FILES)


def _kit_agent_gitignore() -> str:
    path = _kit_dir() / "templates" / "agent" / "gitignore"
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RevisionFormatError(f"Kit agent gitignore {path} is missing: {exc}") from exc


def _load_kit_manifest(src: Path) -> dict:
    path = src / KIT_MANIFEST_FILENAME
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RevisionFormatError(
            f"Manifest {path} could not be read as JSON: {exc}"
        ) from exc
    if not isinstance(manifest, dict):
        raise RevisionFormatError(f"Manifest {path} is not a JSON object")
    return manifest


def _read_tree_text(root: Path, rel: object) -> str | None:
    """Text of ``root/rel`` when it is a regular file inside ``root``, else None."""
    if not isinstance(rel, str) or not rel.strip():
        return None
    candidate = root / rel
    try:
        resolved = candidate.resolve()
        base = root.resolve()
    except OSError:
        return None
    if base not in resolved.parents or candidate.is_symlink() or not candidate.is_file():
        return None
    try:
        return candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:63].strip("-")
    return slug if _KIT_SLUG_RE.match(slug) else "agent"


def _engine_from_sdk(sdk: dict) -> str | None:
    value = sdk.get("conversation") or sdk.get("building")
    if not isinstance(value, str) or not value:
        return None
    return "claude" if value.startswith("claude-code") else "opencode"


def _build_kit_manifest(
    *,
    manifest: dict,
    existing: dict | None,
    name: str,
    slug: str | None,
    prompt_files: dict,
) -> dict:
    """``existing`` updated with the revision's fields; unknown keys preserved."""
    kit: dict = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    metadata = manifest.get("metadata") or {}
    prompts = manifest.get("prompts") or {}

    kit["contract_version"] = KIT_CONTRACT_VERSION
    if not (isinstance(kit.get("id"), str) and _UUID_RE.match(kit["id"])):
        kit["id"] = str(uuid.uuid4())
    kit["name"] = name
    if slug and _KIT_SLUG_RE.match(slug):
        kit["slug"] = slug
    elif not (isinstance(kit.get("slug"), str) and _KIT_SLUG_RE.match(kit["slug"])):
        kit["slug"] = _slugify(name)
    # The schema requires a non-empty description.
    kit["description"] = metadata.get("description") or kit.get("description") or name
    if metadata.get("example_prompts") is not None:
        kit["example_prompts"] = _kit_example_prompts(metadata["example_prompts"])
    if "status_refresh_command" in metadata:
        kit["status_refresh_command"] = metadata["status_refresh_command"]
    kit["router_trigger_prompt"] = prompts.get("router_trigger")
    kit["prompts"] = dict(prompt_files)

    engine = _engine_from_sdk(manifest.get("sdk") or {})
    if engine is not None:
        runtime = kit.get("runtime")
        runtime = dict(runtime) if isinstance(runtime, dict) else {}
        runtime["engine"] = engine
        kit["runtime"] = runtime

    bundle = {
        k: copy.deepcopy(v)
        for k, v in manifest.items()
        if k not in ("prompts", TREE_LAYOUT_KEY)
    }
    bundle["schema_version"] = TREE_V3_SCHEMA_VERSION
    kit["bundle"] = bundle
    return kit


def _kit_example_prompts(value: object) -> list[str]:
    """``example_prompts`` within the kit schema: non-empty strings, at most 20,
    each clipped to 500 characters."""
    if not isinstance(value, list):
        return []
    prompts = [
        item.strip()[:_KIT_EXAMPLE_PROMPT_MAX_CHARS]
        for item in value
        if isinstance(item, str) and item.strip()
    ]
    return prompts[:_KIT_EXAMPLE_PROMPTS_MAX_ITEMS]


def _kit_prompt_files(existing: dict | None, dest: Path) -> dict:
    """Prompt file paths: the existing kit manifest's when each is a safe
    relative path inside ``dest``, else the kit default for that kind."""
    files = dict(_KIT_DEFAULT_PROMPT_FILES)
    custom = existing.get("prompts") if isinstance(existing, dict) else None
    if not isinstance(custom, dict):
        return files
    base = dest.resolve()
    for kind in _KIT_PROMPT_KINDS:
        rel = custom.get(kind)
        if not isinstance(rel, str) or not rel.strip():
            continue
        parts = Path(rel).parts
        if Path(rel).is_absolute() or ".." in parts:
            continue
        if base not in (dest / rel).resolve().parents:
            continue
        files[kind] = rel
    return files


def _excluded_paths(root: Path, excludes: list[str], base: Path | None = None) -> list[str]:
    """Top-most paths under ``root`` that ``excludes`` drop (sorted)."""
    base = base or root
    dropped: list[str] = []
    try:
        children = sorted(root.iterdir())
    except OSError:
        return dropped
    for child in children:
        rel = child.relative_to(base).as_posix()
        if kit_path_excluded(rel, excludes):
            dropped.append(rel)
        elif child.is_dir() and not child.is_symlink():
            dropped.extend(_excluded_paths(child, excludes, base))
    return dropped


def _iter_tree(root: Path, base: Path, excludes: list[str]):
    """Yield ``(path, rel)`` for importable entries under ``root``, depth-first,
    children before their directory. Excluded directories are never opened."""
    try:
        children = sorted(root.iterdir())
    except OSError:
        return
    for child in children:
        rel = child.relative_to(base).as_posix()
        if kit_path_excluded(rel, excludes):
            continue
        if child.is_dir() and not child.is_symlink():
            yield from _iter_tree(child, base, excludes)
        yield child, rel


def _copy_importable(
    src_root: Path,
    dest_root: Path,
    excludes: list[str],
    skip: set[str] | None = None,
) -> None:
    """Copy every importable regular file of ``src_root`` into ``dest_root``,
    except the root-relative paths in ``skip``."""
    if not src_root.is_dir():
        return
    for path, rel in _iter_tree(src_root, src_root, excludes):
        if skip and rel in skip:
            continue
        if path.is_symlink():
            logger.warning("Skipping symlink in agent tree (never followed): %s", rel)
            continue
        target = dest_root / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True, mode=0o755)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            shutil.copy2(path, target)


def _prune_importable(root: Path, base: Path, excludes: list[str]) -> None:
    """Remove from ``root`` what the reader would import; keep excluded paths
    and any directory that still holds one."""
    for path, _rel in _iter_tree(root, base, excludes):
        if path.is_dir() and not path.is_symlink():
            try:
                path.rmdir()  # only succeeds when nothing excluded remains
            except OSError:
                pass
        else:
            path.unlink(missing_ok=True)


# ── cloud_import_excludes matching (the kit contract's semantics) ──
# Same rules as cinna-cli ``kit_contract.matches_pattern`` and kit.py: a trailing
# ``/`` covers the directory and everything beneath it, ``*`` / ``?`` stay inside
# one segment, ``**`` spans whole segments, and a pattern without a leading
# ``**`` is anchored at the agent root.


def _normalize_rel_path(rel_path: str) -> str:
    text = rel_path.replace("\\", "/")
    if text.startswith("./"):
        text = text[2:]
    return text.lstrip("/").rstrip("/")


@functools.lru_cache(maxsize=512)
def _segment_matcher(pattern: str) -> "re.Pattern[str]":
    parts = []
    for character in pattern:
        if character == "*":
            parts.append("[^/]*")
        elif character == "?":
            parts.append("[^/]")
        else:
            parts.append(re.escape(character))
    return re.compile("".join(parts))


def _match_segments(pattern: tuple[str, ...], path: tuple[str, ...]) -> bool:
    if not pattern:
        return not path
    if pattern[0] == "**":
        rest = pattern[1:]
        return any(_match_segments(rest, path[skip:]) for skip in range(len(path) + 1))
    if not path or not _segment_matcher(pattern[0]).fullmatch(path[0]):
        return False
    return _match_segments(pattern[1:], path[1:])


def kit_path_matches(pattern: str, rel_path: str) -> bool:
    """One ``cloud_import_excludes`` pattern against one agent-root-relative path."""
    stripped = pattern.strip()
    body = _normalize_rel_path(stripped)
    path = _normalize_rel_path(rel_path)
    if not body or not path:
        return False
    pattern_segments = tuple(body.split("/"))
    path_segments = tuple(path.split("/"))
    if stripped.endswith("/"):
        return any(
            _match_segments(pattern_segments, path_segments[:end])
            for end in range(len(pattern_segments), len(path_segments) + 1)
        )
    return _match_segments(pattern_segments, path_segments)


def kit_path_excluded(rel_path: str, patterns: list[str]) -> bool:
    """True when any ``cloud_import_excludes`` pattern drops ``rel_path``."""
    return any(kit_path_matches(pattern, rel_path) for pattern in patterns)
