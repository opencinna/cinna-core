"""RevisionFormat — the single (de)serializer for the canonical revision layout.

A bundle revision on disk is a directory holding:

* a **manifest** (``manifest.json`` for bundle storage, ``cinna.agent.json`` for
  git trees) — schema_version 2: prompts, SDK + model overrides,
  ``required_credential_specs``, ``schedules``, ``plugin_specs``, ``version``,
  ``release_notes`` and a SHA-256 ``content_hash``;
* a ``workspace/`` subtree — a verbatim copy of the env ``app/workspace/`` minus
  the runtime / per-user / secret denylist (see
  :mod:`app.services.environments.workspace_classification`);
* an optional generated ``.gitignore`` (git trees) listing the same denylist so
  excluded state can never be committed.

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

import json
import logging
from datetime import datetime, UTC
from pathlib import Path
from typing import TYPE_CHECKING

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
SUPPORTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({1, 2})

# Manifest filename used by bundle storage vs. a git tree. This is the ONLY
# git-vs-bundle difference in the layout.
BUNDLE_MANIFEST_FILENAME = "manifest.json"
GIT_MANIFEST_FILENAME = "cinna.agent.json"

# Reader dispatch order: bundle filename first, then the git filename.
_MANIFEST_FILENAMES = (BUNDLE_MANIFEST_FILENAME, GIT_MANIFEST_FILENAME)


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
            "version": manifest.get("version"),
            "release_notes": manifest.get("release_notes"),
        }

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
