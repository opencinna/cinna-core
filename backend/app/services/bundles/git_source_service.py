"""GitSourceService — checkout / pull / push an agent install against a git remote.

Git is an external storage / transport / interchange backend for the thing the
platform already versions: an ``AgentBundleRevision``. A git tree
(``cinna.agent.json`` + ``workspace/`` + ``.gitignore``) is byte-for-byte the
schema_version-2 bundle snapshot layout, so each git operation reduces to an
operation the platform already performs:

* **checkout** — clone ``<repo>[/subdir]@<ref>``, validate + parse
  ``cinna.agent.json``, persist the cloned tree into bundle storage as an
  ``AgentBundleRevision`` (the internal SSOT), then install from that revision
  via the existing :meth:`InstallService._install_from_revision` seeding path.
* **pull** — ``git pull``, persist a new revision, then reuse
  :func:`replace_bundle_content` **verbatim** (its denylist merge/prune
  preserves App Data, credentials, consumer plugins). Advance
  ``last_synced_commit``.
* **push** — capture the live env workspace via
  :meth:`RevisionFormat.write_tree` (which reuses
  ``PublishService._snapshot_workspace_tree`` — credentials / app-data / logs /
  databases / uploads can never reach the tree), commit, fast-forward-push.

Security invariants (honoured here, enforced by reused primitives):

* **Egress guard** runs on every clone / pull / push / ls-remote
  (``git_operations.assert_git_url_allowed``).
* **SSH key host-side only** — :meth:`SSHKeyService.get_decrypted_private_key`
  (ownership-checked) → chmod-600 temp file (``create_ssh_key_file``) → deleted
  in ``finally``. Never copied into the container.
* **Inbound sanitisation** — checkout / pull route the cloned ``workspace/``
  tree through :func:`iter_bundle_toplevel` + :func:`safe_copytree`, so an
  untrusted repo cannot inject ``credentials/`` / ``app-data/`` / runtime state
  into the install (the same denylist publish applies on the way out).
* **Per-agent lock** serialises pull/push for one agent (mirrors the per-bundle
  publish lock).
"""
from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import logging
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterator

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.config import settings
from app.models.agents.agent import Agent
from app.models.bundles.agent_bundle import AgentBundle
from app.models.bundles.agent_bundle_revision import (
    AgentBundleRevision,
    REVISION_ORIGIN_GIT,
)
from app.models.bundles.agent_git_source import (
    AgentGitSource,
    GitSourceStatus,
    GitSyncDirection,
)
from app.models.environments.environment import AgentEnvironment
from app.models.users.user import User
from app.services.bundles.bundle_service import BundleService
from app.services.bundles.install_service import InstallError, InstallService
from app.services.bundles.publish_service import PublishService
from app.services.bundles.revision_format import (
    GIT_MANIFEST_FILENAME,
    RevisionFormat,
    RevisionFormatError,
)
from app.services.environments.workspace_classification import (
    PLUGINS_DIRNAME,
    WORKSPACE_ROOT_REL,
    is_bundle_owned_toplevel,
    is_nested_excluded,
    iter_bundle_toplevel,
    safe_copytree,
    snapshot_layout,
)
from app.services.knowledge.git_operations import (
    GitAuthenticationError,
    GitConnectionError,
    GitOperationError,
    build_web_commit_url,
    clone_repository_context,
    commit_all,
    create_ssh_key_file,
    fast_forward_push,
    get_current_commit_hash,
    git_log_subdir,
    init_repo_with_remote,
    ls_remote_head,
    subdir_changed_between,
)
from app.services.users.ssh_key_service import SSHKeyService

logger = logging.getLogger(__name__)


# DB prompt fields compared against the synced revision, with their UI labels.
# Single source of truth shared by the dirty check (:meth:`_prompts_changed`),
# the pull guard's blocking set (:meth:`_pull_blocking_changes`), the
# commit-status preview and the ``keep_local`` restore narrowing in
# :meth:`_apply_revision_to_install`, so they cannot disagree.
_PROMPT_FIELDS: tuple[tuple[str, str], ...] = (
    ("workflow_prompt", "Workflow prompt"),
    ("entrypoint_prompt", "Entrypoint prompt"),
    ("refiner_prompt", "Refiner prompt"),
    ("router_trigger_prompt", "Router trigger prompt"),
)


# ── cinna.agent.json settings fields (everything in the manifest but the
#    prompts and the captured ``workspace/`` tree) ────────────────────────
#
# The workspace digest covers ``workspace/`` and ``_PROMPT_FIELDS`` covers the
# manifest's ``prompts`` block — but the rest of ``cinna.agent.json`` (the agent's
# definitional settings) was invisible to the change check, so editing e.g. the
# example prompts or adding a schedule reported "no local changes" even though
# the next commit would rewrite the manifest. These registries close that gap.
#
# Every attribute name below is deliberately identical on the live row and on
# ``AgentBundleRevision``, so one ``getattr`` pair covers both sides.

# ``manifest["metadata"]`` — agent-row definitional fields (live: ``Agent``).
_METADATA_FIELDS: tuple[tuple[str, str], ...] = (
    ("description", "Description"),
    ("example_prompts", "Example prompts"),
    ("status_refresh_command", "Status refresh command"),
    ("agent_api_enabled", "Agent REST API enabled"),
    ("agent_api_identity_enabled", "Agent REST API caller identity"),
    ("a2a_config", "A2A configuration"),
    ("agent_sdk_config", "SDK tool configuration"),
    ("webapp_enabled", "Webapp enabled"),
)

# ``manifest["sdk"]`` — per-mode engine + model overrides (live: the active
# ``AgentEnvironment``). Skipped entirely when the install has no env row.
_SDK_FIELDS: tuple[tuple[str, str], ...] = (
    ("agent_sdk_building", "SDK engine (building mode)"),
    ("agent_sdk_conversation", "SDK engine (conversation mode)"),
    ("model_override_building", "Model override (building mode)"),
    ("model_override_conversation", "Model override (conversation mode)"),
)

# Top-level manifest spec lists. The live side is re-collected with the SAME
# helpers ``_capture_and_push`` uses to build the manifest, so the diff can
# never disagree with what a commit would actually write.
#
# ``required_credential_specs`` is deliberately NOT here — do not re-add it.
# Its live collector (``PublishService._collect_credential_specs``) reads the
# install-local ``Credential`` rows, and on any install that did not author the
# baseline (a ``checkout``, or a ``pull`` from a repo another install pushed)
# those rows are placeholders: ``name="<spec> (placeholder)"``,
# ``notes="Placeholder for required bundle credential."``, ``allow_sharing=False``
# and a locally re-resolved ``provided_by="user"``. They can never reproduce the
# publisher's spec values, so the comparison would report drift permanently with
# no user edit behind it — not a trustworthy signal. Credential-spec staleness
# already has a purpose-built detector: ``PublishService.compute_credential_spec_drift``
# behind ``GET /bundle-credential-drift``, surfaced as the republish nudge on the
# Bundle tab.
_SPEC_FIELDS: tuple[tuple[str, str, Callable[[Session, "Agent"], list]], ...] = (
    ("schedules", "Schedules", PublishService._collect_schedule_specs),
    ("plugin_specs", "Plugins", PublishService._collect_plugin_specs),
)

# Section keys of :meth:`GitSourceService._settings_changes`.
_SETTING_SECTIONS: tuple[str, ...] = ("metadata", "sdk", "specs")

# Sections a *pull* would overwrite on the install — the only ones the pull
# dirty guard may block on (see :meth:`GitSourceService._assert_not_dirty`).
_PULL_OVERWRITTEN_SECTIONS: tuple[str, ...] = ("metadata",)

# Sections addressable by the per-field diff endpoint: the three settings
# registries plus the two axes that live outside them.
_DIFF_SECTIONS: tuple[str, ...] = ("prompt", "file", *_SETTING_SECTIONS)

# Per-side content cap for a diff. Generous for prose and code, small enough
# that a stray large file can't turn a read-only drill-down into a memory event.
_DIFF_MAX_BYTES = 512 * 1024
# Output cap. A rewritten file legitimately diffs to thousands of lines; the
# modal is a review aid, not an archive, so truncate and say so.
_DIFF_MAX_LINES = 2_000

# Accepted ``conflict_resolution`` values on :meth:`GitSourceService.pull_update`.
#
# ``None`` (omitted) keeps the historical fail-loud behavior — a pull onto an
# install with blocking local changes 409s. That is what the GitOps webhook
# dispatcher relies on, so it must never gain an implicit default.
GIT_PULL_TAKE_REMOTE = "take_remote"
GIT_PULL_KEEP_LOCAL = "keep_local"
GIT_PULL_RESOLUTIONS: tuple[str, ...] = (GIT_PULL_TAKE_REMOTE, GIT_PULL_KEEP_LOCAL)

# The 409 message for a pull blocked by local drift. Deliberately does NOT tell
# the user to "push or discard first": pushing is impossible in exactly this
# state (the push precheck demands a pull), and there is no discard action
# outside the resolution modes below. It is the fallback for any client that
# does not understand the structured ``local_changes`` detail, so it stays a
# complete, self-contained sentence.
_PULL_LOCAL_CHANGES_MESSAGE = (
    "This agent has local changes that a pull would overwrite. Review them to "
    "choose whether to keep or discard them."
)

# Manifest lists whose element ORDER carries no meaning: they are snapshots of
# unordered DB row sets, so a re-query can legitimately return them in a
# different order. Compared as multisets to avoid a false "modified".
_UNORDERED_LIST_FIELDS: frozenset[str] = frozenset({"schedules", "plugin_specs"})

# Dicts whose list values are semantically SETS. ``agent_sdk_config`` holds
# ``sdk_tools`` / ``allowed_tools``, both written via ``list(set(...))`` by the
# tool-discovery path — their order is genuinely non-deterministic.
_SET_LIKE_DICT_FIELDS: frozenset[str] = frozenset({"agent_sdk_config"})


def _canonical_json_value(value: object) -> object:
    """Normalize a JSON-ish value so equivalent "empty" shapes compare equal.

    ``None`` / ``""`` / ``[]`` / ``{}`` all collapse to ``None`` (they all mean
    "unset"), and dict entries whose value is empty are dropped — so a manifest
    that omits a key and a live row that holds an empty default do not read as a
    change. Recurses into containers; scalars pass through untouched (``False``
    and ``0`` are values, not emptiness).
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        normalized = {}
        for key, item in value.items():
            item = _canonical_json_value(item)
            if item is not None:
                normalized[str(key)] = item
        return normalized or None
    if isinstance(value, (list, tuple)):
        items = [_canonical_json_value(item) for item in value]
        return items or None
    return value


def _sorted_json(items: list) -> list:
    """Order a list deterministically by its canonical JSON encoding."""
    return sorted(
        items, key=lambda item: json.dumps(item, sort_keys=True, default=str)
    )


def _normalize_setting_value(field: str, value: object) -> object:
    """Canonicalize one settings field for comparison (see the registries)."""
    canonical = _canonical_json_value(value)
    if field in _UNORDERED_LIST_FIELDS and isinstance(canonical, list):
        return _sorted_json(canonical)
    if field in _SET_LIKE_DICT_FIELDS and isinstance(canonical, dict):
        return {
            key: _sorted_json(item) if isinstance(item, list) else item
            for key, item in canonical.items()
        }
    return canonical


def _classify_change(live: object, baseline: object) -> str | None:
    """``added`` / ``modified`` / ``deleted``, or ``None`` when unchanged."""
    if live == baseline:
        return None
    if baseline is None:
        return "added"
    if live is None:
        return "deleted"
    return "modified"


# ── Typed errors ───────────────────────────────────────────────────────
#
# These are mapped to HTTP status codes by the route layer and to webhook error
# logs by the GitOps dispatcher — so the service NEVER raises ``HTTPException``.


class GitSourceError(Exception):
    """Base error for git-source operations."""


class GitSourceNotFoundError(GitSourceError):
    """No git source for the agent / not visible to the caller (→ 404)."""


class GitSourceValidationError(GitSourceError):
    """Bad input / unusable state — bad tree, wrong direction, no env, oversize (→ 400)."""


class GitSourceConflictError(GitSourceError):
    """Dirty workspace or non-fast-forward remote (→ 409)."""


class GitSourceExistingAgentError(GitSourceConflictError):
    """Connect target subdir already holds an agent (→ 409, recoverable).

    Distinct from the generic conflict so the route can surface a machine-
    readable code: the UI offers to *adopt* the existing remote folder (set the
    remote on the existing agent and re-check status) instead of failing. Pass
    ``adopt_existing=True`` to :meth:`GitSourceService.connect` to take that path.
    """


class GitSourceLocalChangesError(GitSourceConflictError):
    """Pull blocked by local drift (→ 409, recoverable).

    Distinct from the generic conflict so the route can surface a machine-
    readable code plus the blocking field list: the UI opens the pull-conflict
    dialog and offers ``conflict_resolution`` instead of showing a dead-end
    toast. Subclasses :class:`GitSourceConflictError`, so the route must branch
    on it BEFORE the generic conflict branch (same ordering rule as
    :class:`GitSourceExistingAgentError` — see ``agent_git.py``).

    ``blocking`` is the :meth:`GitSourceService._pull_blocking_changes` list —
    ``[{section, field, label, change_type}]``.
    """

    def __init__(self, message: str, blocking: list[dict]) -> None:
        super().__init__(message)
        self.blocking = blocking


class GitBaselineUnavailableError(GitSourceError):
    """The last-synced baseline snapshot is lost AND could not be rebuilt (→ 5xx).

    A synced ``AgentBundleRevision`` row exists but its on-disk snapshot was wiped
    (e.g. an ephemeral ``BUNDLE_STORAGE_DIR`` cleared on a backend redeploy) and
    re-materializing it from git also failed (remote unreachable, the pinned
    commit was GC'd / rewritten, auth failure, bad tree). This is a server-side
    storage-integrity failure — deliberately distinct from the user-actionable
    :class:`GitSourceValidationError` (400) so the dirty / status checks fail loud
    with a "baseline check failed" signal instead of silently reporting a clean,
    non-dirty workspace. Never raised when NO baseline was ever synced (that case
    stays legitimately non-dirty).
    """


# ── Per-agent locks (mirror PublishService._publish_locks) ──────────────

_git_locks: dict[str, asyncio.Lock] = {}


def _lock_for(agent_id: str) -> asyncio.Lock:
    lock = _git_locks.get(agent_id)
    if lock is None:
        lock = asyncio.Lock()
        _git_locks[agent_id] = lock
    return lock


class GitSourceService:
    """Checkout / pull / push for an agent install backed by a git remote."""

    # ── Checkout (read path) ─────────────────────────────────────────

    @staticmethod
    async def checkout(
        *,
        session: Session,
        user: User,
        repo_url: str,
        subdir: str | None,
        ref: str,
        ssh_key_id: uuid.UUID | None,
        sync_direction: str,
        name_override: str | None = None,
    ) -> tuple[Agent, AgentGitSource]:
        """Clone a repo/subdir, import its ``cinna.agent.json`` as a bundle
        revision, create an install + env, seed the workspace, record the source.

        Returns ``(install, git_source)``.
        """
        bundle: AgentBundle
        bundle_created: bool
        revision: AgentBundleRevision
        last_synced_commit: str

        with _resolve_ssh_key(session, ssh_key_id, user.id) as ssh_key_path:
            with clone_repository_context(
                repo_url, branch=ref, ssh_key_path=ssh_key_path
            ) as (repo_path, repo):
                last_synced_commit = get_current_commit_hash(repo)
                src = _resolve_subdir(repo_path, subdir)
                manifest = _read_and_validate_tree(src)
                bundle_id = _require_bundle_id(manifest)
                _assert_no_oversized_files(src / "workspace")

                # Friendly up-front 409 for a same-user re-checkout — the
                # consumer-install unique constraint (owner_id, bundle_id,
                # is_publisher_install) would otherwise raise IntegrityError
                # mid-install. Detect before creating any bundle/revision row so
                # the common duplicate path strands no half-state.
                GitSourceService._assert_not_already_checked_out(
                    session, user_id=user.id, bundle_id=bundle_id
                )

                bundle, bundle_created = GitSourceService._resolve_or_create_bundle(
                    session,
                    bundle_id=bundle_id,
                    user=user,
                    display_name=name_override or bundle_id,
                )
                revision = GitSourceService._persist_revision(
                    session,
                    bundle=bundle,
                    src=src,
                    manifest=manifest,
                    published_by_user_id=user.id,
                )

        # Clone temp tree is gone here; the snapshot lives in bundle storage.
        # Install + source-record together: if anything fails after the bundle
        # and revision are committed, clean up the orphaned rows so a failed
        # checkout never strands a half-imported bundle (N6). The bundle row is
        # only dropped when THIS call created it (a reused/shared row stays).
        try:
            install = await InstallService._install_from_revision(
                session=session,
                user=user,
                bundle=bundle,
                revision=revision,
                request=None,
            )

            if name_override:
                install.name = await InstallService._ensure_unique_name(
                    session, user.id, name_override
                )
                session.add(install)
                session.commit()
                session.refresh(install)

            source = AgentGitSource(
                agent_id=install.id,
                owner_id=user.id,
                bundle_uuid=bundle.id,
                repo_url=repo_url,
                subdir=subdir,
                ref=ref,
                ssh_key_id=ssh_key_id,
                sync_direction=sync_direction,
                last_synced_commit=last_synced_commit,
                last_sync_at=datetime.now(UTC),
                status=GitSourceStatus.CONNECTED,
            )
            session.add(source)
            session.commit()
            session.refresh(source)
        except IntegrityError as exc:
            # Race backstop: a concurrent checkout slipped past the up-front
            # dedupe and hit the consumer-install unique constraint.
            session.rollback()
            GitSourceService._cleanup_orphan_import(
                session, revision, bundle if bundle_created else None
            )
            raise GitSourceConflictError(
                "This repository is already checked out for your account."
            ) from exc
        except InstallError as exc:
            # Env provisioning failed; InstallService rolled back its own Agent
            # row — drop the orphaned bundle/revision and surface a 400.
            session.rollback()
            GitSourceService._cleanup_orphan_import(
                session, revision, bundle if bundle_created else None
            )
            raise GitSourceValidationError(str(exc)) from exc
        except Exception:
            session.rollback()
            GitSourceService._cleanup_orphan_import(
                session, revision, bundle if bundle_created else None
            )
            raise

        logger.info(
            "git checkout: created install %s + git source %s from %s@%s",
            install.id, source.id, repo_url, ref,
        )
        return install, source

    # ── Connect (enable-on-existing-agent) ───────────────────────────

    @staticmethod
    async def connect(
        *,
        session: Session,
        agent_id: uuid.UUID,
        user: User,
        repo_url: str,
        subdir: str | None,
        ref: str,
        ssh_key_id: uuid.UUID | None,
        sync_direction: str,
        commit_message: str = "Initial export from Cinna",
        adopt_existing: bool = False,
    ) -> tuple[AgentGitSource, Agent]:
        """Attach a git source to an EXISTING owned install + initial export push.

        Unlike :meth:`checkout` (which *imports a foreign repo into a new
        install*), connect attaches a brand-new git destination to an agent the
        user already has and performs the first commit from the current live
        workspace. Per-agent locked (mirrors push). Returns ``(source, install)``.

        When the target subdir already holds an agent, connect raises
        :class:`GitSourceExistingAgentError` — unless ``adopt_existing`` is
        ``True``, in which case it *adopts* that remote folder: it records the
        remote's current content as the synced baseline (no push) so the dirty
        check then compares the live install against it. This is the "set the
        remote on an existing folder and re-check status" path.
        """
        async with _lock_for(str(agent_id)):
            return GitSourceService._connect_locked(
                session,
                agent_id=agent_id,
                user=user,
                repo_url=repo_url,
                subdir=subdir,
                ref=ref,
                ssh_key_id=ssh_key_id,
                sync_direction=sync_direction,
                commit_message=commit_message,
                adopt_existing=adopt_existing,
            )

    @staticmethod
    def _connect_locked(
        session: Session,
        *,
        agent_id: uuid.UUID,
        user: User,
        repo_url: str,
        subdir: str | None,
        ref: str,
        ssh_key_id: uuid.UUID | None,
        sync_direction: str,
        commit_message: str,
        adopt_existing: bool = False,
    ) -> tuple[AgentGitSource, Agent]:
        # 1. Resolve the install owned by the caller (404 — no existence leak).
        install = session.get(Agent, agent_id)
        if install is None or (
            install.owner_id != user.id and not user.is_superuser
        ):
            raise GitSourceNotFoundError("Agent not found")

        # 2. One git source per install — reject a second connect (→ 409). The
        #    route also catches the unique-constraint IntegrityError as a race
        #    backstop.
        existing = session.exec(
            select(AgentGitSource).where(AgentGitSource.agent_id == agent_id)
        ).first()
        if existing is not None:
            raise GitSourceConflictError(
                "A git source is already configured for this agent; "
                "disconnect it first."
            )

        # 3. Direction guard — the initial export is a write.
        if sync_direction not in (
            GitSyncDirection.PUSH,
            GitSyncDirection.BIDIRECTIONAL,
        ):
            raise GitSourceValidationError(
                "Connect performs an initial export push; sync_direction must "
                "be 'push' or 'bidirectional'."
            )

        # 4. Env-readable guard — we export the live workspace.
        env = (
            session.get(AgentEnvironment, install.active_environment_id)
            if install.active_environment_id else None
        )
        if env is None:
            raise GitSourceValidationError(
                "Start the environment before connecting so its workspace can "
                "be exported."
            )
        env_workspace_root = Path(settings.ENV_INSTANCES_DIR) / str(env.id)
        try:
            PublishService._assert_workspace_readable(env, env_workspace_root)
        except ValueError as exc:
            raise GitSourceValidationError(str(exc)) from exc

        with _resolve_ssh_key(session, ssh_key_id, user.id) as key:
            # 5. Resolve the backing bundle WITHOUT mutating Agent.bundle_uuid —
            #    the git source gets its own bundle_uuid (Q2), keeping
            #    publisher/consumer semantics on the Agent row intact.
            bundle_created = False
            if install.bundle_uuid is not None:
                bundle = session.get(AgentBundle, install.bundle_uuid)
                if bundle is None:
                    raise GitSourceValidationError(
                        "The agent's backing bundle is missing."
                    )
            else:
                bundle, bundle_created = GitSourceService._resolve_or_create_bundle(
                    session,
                    bundle_id=install.bundle_id,
                    user=user,
                    display_name=install.name,
                )

            # In-memory (unsaved) source carrying the repo coordinates the shared
            # capture helper needs. Persisted only on success (no half-state).
            source = AgentGitSource(
                agent_id=install.id,
                owner_id=user.id,
                bundle_uuid=bundle.id,
                repo_url=repo_url,
                subdir=subdir,
                ref=ref,
                ssh_key_id=ssh_key_id,
                sync_direction=sync_direction,
                status=GitSourceStatus.CONNECTED,
            )

            try:
                new_sha = GitSourceService._connect_capture(
                    session,
                    install=install,
                    env=env,
                    source=source,
                    owner=user,
                    key=key,
                    repo_url=repo_url,
                    ref=ref,
                    subdir=subdir,
                    commit_message=commit_message,
                    adopt_existing=adopt_existing,
                )
            except Exception:
                # No source row is written until success, so the only DB side
                # effect to undo is a bundle row THIS connect created.
                if bundle_created:
                    GitSourceService._cleanup_orphan_bundle(session, bundle.id)
                raise

        # 6. Persist the source row (success only).
        source.last_synced_commit = new_sha
        source.last_sync_at = datetime.now(UTC)
        session.add(source)
        session.commit()
        session.refresh(source)
        session.refresh(install)

        logger.info(
            "git connect: agent %s linked to %s@%s (subdir=%s), pushed %s",
            agent_id, repo_url, ref, subdir, new_sha,
        )
        return source, install

    @staticmethod
    def _connect_capture(
        session: Session,
        *,
        install: Agent,
        env: AgentEnvironment,
        source: AgentGitSource,
        owner: User,
        key: str | None,
        repo_url: str,
        ref: str,
        subdir: str | None,
        commit_message: str,
        adopt_existing: bool = False,
    ) -> str:
        """Probe the remote and run the initial export through the right path.

        Branches (Decision C — connect targets a *fresh* destination):

        * **empty remote / ref absent** → :func:`init_repo_with_remote`, capture,
          first push (creates the ref).
        * **ref exists, subdir empty/absent** → full-history clone, capture,
          ff-push on top of the remote history.
        * **ref exists and subdir already holds a ``cinna.agent.json``**:
          - ``adopt_existing=False`` → :class:`GitSourceExistingAgentError`
            (→ 409, recoverable: the UI offers to adopt).
          - ``adopt_existing=True`` → :meth:`_connect_adopt_existing` records the
            remote folder as the synced baseline (no push) so the dirty check
            then compares the live install against it.
        """
        # Remote-state probe (egress-guarded). A missing ref ⇒ empty remote /
        # absent branch (init path); genuine auth / connection failures surface.
        remote_exists = True
        try:
            ls_remote_head(repo_url, ref, key)
        except (GitAuthenticationError, GitConnectionError):
            raise
        except GitOperationError:
            remote_exists = False

        if not remote_exists:
            temp_dir = tempfile.mkdtemp(prefix="git_connect_")
            try:
                repo = init_repo_with_remote(
                    workdir=temp_dir, repo_url=repo_url, ref=ref, ssh_key_path=key
                )
                return GitSourceService._capture_and_push(
                    session,
                    install=install,
                    env=env,
                    source_like=source,
                    owner=owner,
                    key=key,
                    repo=repo,
                    repo_path=temp_dir,
                    commit_message=commit_message,
                    version=None,
                )
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)

        # Ref exists — full-history clone, guard against overwriting an agent.
        with clone_repository_context(
            repo_url, branch=ref, ssh_key_path=key, depth=None
        ) as (repo_path, repo):
            src = _resolve_subdir(repo_path, subdir)
            if (src / GIT_MANIFEST_FILENAME).exists():
                if adopt_existing:
                    # Adopt the existing remote folder: record its current
                    # content as the synced baseline (no push). The dirty check
                    # then surfaces local-vs-remote differences, and the user
                    # commits (push) or pulls from there.
                    return GitSourceService._connect_adopt_existing(
                        session, source=source, owner=owner, src=src, repo=repo
                    )
                raise GitSourceExistingAgentError(
                    "This repository/subdir already holds an agent — use "
                    "checkout to import it instead of connect."
                )
            return GitSourceService._capture_and_push(
                session,
                install=install,
                env=env,
                source_like=source,
                owner=owner,
                key=key,
                repo=repo,
                repo_path=repo_path,
                commit_message=commit_message,
                version=None,
            )

    @staticmethod
    def _connect_adopt_existing(
        session: Session,
        *,
        source: AgentGitSource,
        owner: User,
        src: Path,
        repo,
    ) -> str:
        """Adopt an existing remote agent folder as the synced baseline (no push).

        Used by connect when ``adopt_existing=True`` and the target subdir already
        holds a ``cinna.agent.json``. Reads + validates the remote folder's tree
        and records it as an :class:`AgentBundleRevision` on the source's bundle —
        the baseline :meth:`_resolve_synced_revision` returns — so the dirty check
        then compares the live install against the remote. NOTHING is committed or
        pushed: this only sets the platform-side link (the git analog of pointing
        a remote at an existing local folder). Returns the remote HEAD SHA, which
        becomes ``last_synced_commit`` (so ``update_available`` is ``False`` —
        local-vs-remote drift surfaces through the dirty check, not the banner).
        """
        manifest = _read_and_validate_tree(src)
        remote_bundle_id = _require_bundle_id(manifest)
        bundle = session.get(AgentBundle, source.bundle_uuid)
        if bundle is None:
            raise GitSourceValidationError(
                "The agent's backing bundle is missing."
            )
        # Adoption is an explicit, user-opted-in action (the UI confirms before
        # adopting a remote folder), so a bundle_id mismatch is permitted: the
        # user may deliberately point an agent at a folder published under a
        # different bundle_id. Surface the mismatch in logs but continue and
        # record the remote tree as this agent's baseline (the agent's own
        # bundle_id is bundle.bundle_id, the value the connect flow resolved
        # this source onto).
        if remote_bundle_id != bundle.bundle_id:
            logger.warning(
                "git adopt: adopting remote folder with bundle_id %s onto agent "
                "bundle %s (mismatch) — recording as baseline by user request",
                remote_bundle_id, bundle.bundle_id,
            )
        _assert_no_oversized_files(src / "workspace")
        GitSourceService._persist_revision(
            session,
            bundle=bundle,
            src=src,
            manifest=manifest,
            published_by_user_id=owner.id,
        )
        return get_current_commit_hash(repo)

    # ── Disconnect (disable) ─────────────────────────────────────────

    @staticmethod
    def disconnect(
        session: Session, agent_id: uuid.UUID, owner: User
    ) -> None:
        """Sever the platform's git link by deleting the ``AgentGitSource`` row.

        Does NOT touch the remote — the external repo is the durable record;
        disconnect only removes the platform-side link (Q1: delete, not a soft
        ``disconnected`` flag). 404 for a missing source / non-owner.
        """
        source = GitSourceService._resolve_source_owned(session, agent_id, owner)
        session.delete(source)
        session.commit()
        logger.info("git disconnect: removed git source for agent %s", agent_id)

    # ── Status / update check ────────────────────────────────────────

    @staticmethod
    def _remote_change_is_relevant(
        *,
        repo_url: str,
        ref: str,
        subdir: str | None,
        last_synced_commit: str | None,
        remote_sha: str,
        ssh_key_path: str | None,
    ) -> bool:
        """Whether a remote HEAD advance actually concerns this agent's subdir.

        Single source of truth shared by the update-check read path
        (:meth:`_compute_update_available_remote`, which drives the UI's "update
        available" banner) and the push fast-forward precheck (:meth:`_push_locked`,
        which drives the 409 "pull first" guard) so the two can never disagree —
        the failure they previously exhibited was exactly a disagreement (banner
        said "no update", push said "pull first"), stranding a subdir-scoped agent.

        Assumes the caller has already established the remote advanced
        (``remote_sha != last_synced_commit``).

        - No ``subdir`` (repo root) or no ``last_synced_commit`` baseline → every
          advance is relevant (unchanged root-repo behaviour).
        - ``subdir`` + baseline → relevant only when the subdir tree actually
          changed between the baseline and ``remote_sha`` (a commit to an unrelated
          folder of a shared repo is not relevant). The only branch that clones.
        """
        subdir_norm = (subdir or "").strip("/")
        if not subdir_norm or not last_synced_commit:
            return True
        return subdir_changed_between(
            repo_url=repo_url,
            ref=ref,
            subdir=subdir_norm,
            base_commit=last_synced_commit,
            ssh_key_path=ssh_key_path,
        )

    @staticmethod
    def _compute_update_available_remote(
        *,
        repo_url: str,
        ref: str,
        subdir: str | None,
        last_synced_commit: str | None,
        key_material: tuple[str, str | None] | None,
    ) -> tuple[bool, str]:
        """Resolve ``(update_available, remote_head_sha)`` from the remote — no DB.

        The remote-only half of the update check, called by :meth:`check_updates`
        AFTER the DB connection has been released (it takes captured primitives +
        in-memory SSH key material, never a ``Session`` / ORM object) so the
        blocking ``ls-remote`` / subdir clone never runs while a pooled connection
        and transaction are held.

        Two-tier, cheapest-first:

        1. **Cheap path** — a single ``ls-remote`` HEAD (no clone). If HEAD has
           not advanced past ``last_synced_commit`` → no update, no clone.
        2. **Subdir scoping** — when HEAD *has* advanced, the answer depends on
           whether the agent lives in a ``subdir``:
           - No ``subdir`` (repo root) → every commit touches the root, so the
             cheap HEAD-advanced verdict is correct. Stays clone-free.
           - No ``last_synced_commit`` baseline → nothing to scope against; the
             advance is the verdict (clone-free).
           - With both a ``subdir`` and a baseline → only report an update when
             commits beyond the baseline actually touched ``<subdir>/`` (subdir
             tree hash differs). This is the only branch that does a (bounded)
             clone, and only after the cheap path already showed an advance, so a
             commit to an unrelated folder of the same repo no longer raises a
             false "update available" banner.

        The temp SSH key file wraps both the ls-remote and the subdir clone so a
        private repo is authenticated for both calls.
        """
        with _ssh_key_file(key_material) as key:
            remote_sha = ls_remote_head(repo_url, ref, key)
            if remote_sha == last_synced_commit:
                return False, remote_sha

            # HEAD advanced: an update only when the advance is relevant to this
            # agent (repo root / no baseline → always; subdir → only when that
            # subdir tree actually changed). Shared with the push precheck.
            relevant = GitSourceService._remote_change_is_relevant(
                repo_url=repo_url,
                ref=ref,
                subdir=subdir,
                last_synced_commit=last_synced_commit,
                remote_sha=remote_sha,
                ssh_key_path=key,
            )
            return relevant, remote_sha

    @staticmethod
    def get_source(
        session: Session, agent_id: uuid.UUID, owner: User
    ) -> tuple[AgentGitSource, bool]:
        """Return ``(source, update_available)`` for the owner-resolved agent.

        **Remote-free by design.** This is the cheap plain status read; it does
        NO network I/O and always reports ``update_available = False``. Freshness
        is owned exclusively by the explicit ``check-updates`` endpoint (and the
        dirty check), so the plain read never blocks on — nor pins a pooled DB
        connection behind — a slow / hung remote. The frontend polls
        ``check-updates`` / ``dirty`` separately for the update banner.
        """
        source = GitSourceService._resolve_source_owned(session, agent_id, owner)
        return source, False

    @staticmethod
    def check_updates(
        session: Session, agent_id: uuid.UUID, owner: User
    ) -> dict:
        """Strict ``update_available`` check (surfaces auth / network errors).

        Cheap ``ls-remote`` HEAD vs ``last_synced_commit``, then subdir-scoped via
        :meth:`_compute_update_available_remote` so a commit to an unrelated folder
        of the same repo does not report an update.

        Pool-safety: all DB work (owner-resolve + SSH-key decrypt + capturing the
        repo coordinates) happens first, then the connection is released
        (``session.commit()``) BEFORE the blocking remote git calls run — so the
        ls-remote / subdir clone never holds a pooled connection.
        """
        source = GitSourceService._resolve_source_owned(session, agent_id, owner)
        key_material = _read_ssh_key_material(
            session, source.ssh_key_id, source.owner_id
        )
        repo_url = source.repo_url
        ref = source.ref
        subdir = source.subdir
        last_synced_commit = source.last_synced_commit

        # Release the pooled DB connection before any remote git I/O.
        session.commit()

        update_available, remote_sha = (
            GitSourceService._compute_update_available_remote(
                repo_url=repo_url,
                ref=ref,
                subdir=subdir,
                last_synced_commit=last_synced_commit,
                key_material=key_material,
            )
        )
        return {
            "update_available": update_available,
            "remote_commit": remote_sha,
            "last_synced_commit": last_synced_commit,
        }

    # ── Commit history ───────────────────────────────────────────────

    @staticmethod
    def list_commits(
        session: Session, agent_id: uuid.UUID, owner: User, *, limit: int = 50
    ) -> list[dict]:
        """Return up to ``limit`` commits touching the source's subdir, newest first.

        Strict — surfaces auth / network errors (like ``check_updates``).
        Owner-resolved (404 for a missing source / non-owner).

        Pool-safety: resolve the source + decrypt the SSH key + capture the repo
        coordinates first, release the DB connection (``session.commit()``), then
        run the blocking ``git log`` clone — so it never holds a pooled
        connection.
        """
        source = GitSourceService._resolve_source_owned(session, agent_id, owner)
        key_material = _read_ssh_key_material(
            session, source.ssh_key_id, source.owner_id
        )
        repo_url = source.repo_url
        ref = source.ref
        subdir = source.subdir

        # Release the pooled DB connection before the (cloning) git log call.
        session.commit()

        with _ssh_key_file(key_material) as key:
            commits = git_log_subdir(
                repo_url=repo_url,
                ref=ref,
                subdir=subdir,
                ssh_key_path=key,
                max_count=limit,
            )
        # Attach a per-commit browser URL when the host supports it (GitHub
        # today); ``None`` otherwise so the UI renders the SHA as plain text.
        for commit in commits:
            commit["commit_url"] = build_web_commit_url(
                repo_url, commit.get("sha", "")
            )
        return commits

    # ── Dirty check ──────────────────────────────────────────────────

    @staticmethod
    def compute_dirty(
        session: Session, agent_id: uuid.UUID, owner: User
    ) -> dict:
        """Compare the live workspace + manifest against the last synced revision.

        Read-only — NEVER pushes. Returns ``{dirty, prompts_dirty,
        settings_dirty, workspace_dirty, has_env, last_synced_commit}`` — the
        three axes of a git tree: the manifest's ``prompts`` block, the rest of
        ``cinna.agent.json`` (agent settings — see :meth:`_settings_changes`), and
        the captured ``workspace/`` files. Best-effort: with no env or no synced
        revision the relevant flags stay ``False``. Owner-resolved (404 for a
        missing source / non-owner).

        Pool-safety: all DB work (owner-resolve, prompt + settings diff, resolving
        the synced revision's snapshot path + the env workspace path) happens
        first, then the connection is released (``session.commit()``) BEFORE the
        heavy workspace tree copy + hash — so the slow filesystem work never holds
        a pooled connection.
        """
        source = GitSourceService._resolve_source_owned(session, agent_id, owner)
        install = session.get(Agent, agent_id)
        if install is None:
            raise GitSourceNotFoundError("Agent not found")

        env = (
            session.get(AgentEnvironment, install.active_environment_id)
            if install.active_environment_id else None
        )
        # One baseline resolve for all three axes (prompts / settings /
        # workspace) — it is a full-row SELECT carrying the manifest blob, and
        # this endpoint is polled.
        synced_rev = GitSourceService._resolve_synced_revision(
            session, source, install
        )
        prompts_dirty = GitSourceService._prompts_changed(install, synced_rev)
        settings_dirty = bool(
            GitSourceService._settings_changes(
                session, install, synced_rev, env, stop_early=True
            )
        )
        last_synced_commit = source.last_synced_commit

        # Capture the on-disk paths the workspace diff needs while still on the DB
        # connection; the heavy copy/hash then runs after it is released.
        has_env = False
        env_workspace_root: Path | None = None
        synced_workspace: Path | None = None
        rematerialize_ctx: dict | None = None
        if env is not None:
            env_workspace_root = Path(settings.ENV_INSTANCES_DIR) / str(env.id)
            workspace_dir = env_workspace_root / WORKSPACE_ROOT_REL
            if workspace_dir.exists() and workspace_dir.is_dir():
                has_env = True
                if synced_rev is not None and synced_rev.snapshot_path:
                    candidate = Path(synced_rev.snapshot_path) / "workspace"
                    if candidate.exists():
                        synced_workspace = candidate
                    else:
                        # Baseline row exists but its on-disk snapshot was wiped
                        # (e.g. ephemeral BUNDLE_STORAGE_DIR after a redeploy).
                        # This is a lost-baseline condition, NOT a clean workspace.
                        # Capture what a git re-materialization needs while the DB
                        # connection is still open (SSH key decrypt), then rebuild
                        # it AFTER the connection is released.
                        rematerialize_ctx = {
                            "repo_url": source.repo_url,
                            "ref": source.ref,
                            "subdir": source.subdir,
                            "last_synced_commit": source.last_synced_commit,
                            "snapshot_dir": Path(synced_rev.snapshot_path),
                            "key_material": _read_ssh_key_material(
                                session, source.ssh_key_id, source.owner_id
                            ),
                        }

        # Release the pooled DB connection before the heavy workspace copy/hash.
        session.commit()

        # A synced baseline row existed but its snapshot was gone: rebuild it from
        # git now (self-healing) and compare against the fresh copy. If the rebuild
        # itself fails, this raises GitBaselineUnavailableError so the caller fails
        # loud instead of falsely reporting a clean workspace.
        if rematerialize_ctx is not None:
            synced_workspace = GitSourceService._rematerialize_baseline_snapshot(
                **rematerialize_ctx
            )

        workspace_dirty = False
        if (
            has_env
            and env_workspace_root is not None
            and synced_workspace is not None
        ):
            temp_dir = Path(tempfile.mkdtemp(prefix="git_dirty_"))
            try:
                PublishService._snapshot_workspace_tree(
                    env_workspace_root, temp_dir
                )
                live_digest = PublishService.hash_workspace_tree(
                    temp_dir / "workspace"
                )
                synced_digest = PublishService.hash_workspace_tree(
                    synced_workspace
                )
                workspace_dirty = live_digest != synced_digest
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)

        return {
            "dirty": prompts_dirty or settings_dirty or workspace_dirty,
            "prompts_dirty": prompts_dirty,
            "settings_dirty": settings_dirty,
            "workspace_dirty": workspace_dirty,
            "has_env": has_env,
            "last_synced_commit": last_synced_commit,
        }

    @staticmethod
    def compute_status(
        session: Session, agent_id: uuid.UUID, owner: User
    ) -> dict:
        """File/prompt/settings-level preview of what a commit would capture.

        The detailed sibling of :meth:`compute_dirty`: instead of booleans it
        returns the actual changes — a per-prompt list, a per-setting list (the
        rest of ``cinna.agent.json``) and a per-file list of ``{path,
        change_type}`` (``added`` / ``modified`` / ``deleted``) — so the UI can
        render a ``git status`` style preview before the user commits.

        Each prompt / setting change also carries ``blocks_pull``, and the result
        carries ``pull_blocked``, from :meth:`_pull_blocking_changes` — the very
        helper :meth:`_assert_not_dirty` raises from — so this preview and the
        pull 409 it explains can never disagree about what blocks.

        The workspace side compares the SAME post-denylist capture a push would
        produce (via :meth:`PublishService._snapshot_workspace_tree`) against the
        last synced revision's ``workspace/`` snapshot, so the preview matches the
        commit exactly (e.g. ``__pycache__`` never appears). Read-only — never
        pushes. Best-effort: with no env or no synced revision the relevant list
        stays empty. Owner-resolved (404 for a missing source / non-owner).

        Pool-safety: the prompt diff + resolving the synced revision's snapshot
        path + the env workspace path all happen first, then the connection is
        released (``session.commit()``) BEFORE the heavy per-file snapshot + hash
        diff — so the slow filesystem work never holds a pooled connection.
        """
        source = GitSourceService._resolve_source_owned(session, agent_id, owner)
        install = session.get(Agent, agent_id)
        if install is None:
            raise GitSourceNotFoundError("Agent not found")

        synced_rev = GitSourceService._resolve_synced_revision(
            session, source, install
        )
        last_synced_commit = source.last_synced_commit
        env = (
            session.get(AgentEnvironment, install.active_environment_id)
            if install.active_environment_id else None
        )

        # ── What a pull would overwrite (the pull guard's blocking set) ──
        # Computed by the SAME helper the guard raises from, so a change flagged
        # here as blocking is exactly a change the 409 would name.
        #
        # This does diff the settings twice — narrowed here, in full below — but
        # the narrowed pass covers only ``metadata`` (plain columns on the row
        # and the manifest), never the ``specs`` section whose collectors are the
        # expensive part. The duplicated work is a handful of comparisons; the
        # alternative (deriving the blocking set from the full diff) would
        # re-implement the per-field ``skip_null_baseline_metadata`` narrowing
        # here and re-open the drift this helper exists to close.
        blocking = GitSourceService._pull_blocking_changes(
            session, install, synced_rev, env
        )
        blocking_keys = {(c["section"], c["field"]) for c in blocking}

        # ── Prompt changes (DB diff) ─────────────────────────────────
        # Every prompt change blocks a pull, so the blocking list already IS the
        # prompt-change list; project it to the wire shape rather than recomputing.
        prompt_changes = [
            {
                "field": c["label"],
                # Raw attribute name — the stable key the per-field diff
                # endpoint addresses. A label is UI copy and can be reworded.
                "key": c["field"],
                "section": "prompt",
                "change_type": c["change_type"],
                "blocks_pull": True,
            }
            for c in blocking
            if c["section"] == "prompt"
        ]

        # ── Settings changes (rest of cinna.agent.json) ──────────────
        # ``_settings_changes`` emits the human label under ``field`` (the
        # long-standing wire contract of ``GitSettingChange.field``) alongside the
        # raw ``name`` / ``section`` key. The key drives the ``blocking_keys``
        # join here AND reaches the response as ``key``/``section``, which is how
        # the per-field diff endpoint addresses this exact setting.
        setting_changes = [
            {
                "field": c["field"],
                "key": c["name"],
                "section": c["section"],
                "change_type": c["change_type"],
                "blocks_pull": (c["section"], c["name"]) in blocking_keys,
            }
            for c in GitSourceService._settings_changes(
                session, install, synced_rev, env
            )
        ]

        # Capture the on-disk paths the workspace diff needs while still on the DB
        # connection; the heavy snapshot/hash then runs after it is released.
        has_env = False
        env_workspace_root: Path | None = None
        synced_workspace: Path | None = None
        rematerialize_ctx: dict | None = None
        if env is not None:
            env_workspace_root = Path(settings.ENV_INSTANCES_DIR) / str(env.id)
            workspace_dir = env_workspace_root / WORKSPACE_ROOT_REL
            if workspace_dir.exists() and workspace_dir.is_dir():
                has_env = True
                # Mirror compute_dirty. A missing baseline snapshot on disk splits
                # two ways: genuinely no baseline (no synced revision / no
                # snapshot_path) → skip the diff (legitimately non-dirty); a synced
                # revision row whose snapshot dir was wiped → re-materialize it from
                # git (self-healing) rather than fabricating an all-"added" preview
                # against an empty baseline.
                if synced_rev is not None and synced_rev.snapshot_path:
                    candidate = Path(synced_rev.snapshot_path) / "workspace"
                    if candidate.exists():
                        synced_workspace = candidate
                    else:
                        rematerialize_ctx = {
                            "repo_url": source.repo_url,
                            "ref": source.ref,
                            "subdir": source.subdir,
                            "last_synced_commit": source.last_synced_commit,
                            "snapshot_dir": Path(synced_rev.snapshot_path),
                            "key_material": _read_ssh_key_material(
                                session, source.ssh_key_id, source.owner_id
                            ),
                        }

        # Release the pooled DB connection before the heavy per-file diff.
        session.commit()

        # Lost-baseline recovery (mirrors compute_dirty): rebuild the wiped
        # snapshot from git, or fail loud via GitBaselineUnavailableError.
        if rematerialize_ctx is not None:
            synced_workspace = GitSourceService._rematerialize_baseline_snapshot(
                **rematerialize_ctx
            )

        # ── Workspace file changes ──────────────────────────────────
        file_changes: list[dict] = []
        if (
            has_env
            and env_workspace_root is not None
            and synced_workspace is not None
        ):
            synced_files = GitSourceService._file_hashes(synced_workspace)
            temp_dir = Path(tempfile.mkdtemp(prefix="git_status_"))
            try:
                PublishService._snapshot_workspace_tree(
                    env_workspace_root, temp_dir
                )
                live_files = GitSourceService._file_hashes(
                    temp_dir / "workspace"
                )
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)
            for path in sorted(set(live_files) | set(synced_files)):
                in_live = path in live_files
                in_synced = path in synced_files
                if in_live and not in_synced:
                    change_type = "added"
                elif in_synced and not in_live:
                    change_type = "deleted"
                elif live_files[path] != synced_files[path]:
                    change_type = "modified"
                else:
                    continue
                file_changes.append(
                    {"path": path, "change_type": change_type}
                )

        return {
            "dirty": bool(prompt_changes or setting_changes or file_changes),
            "has_env": has_env,
            "last_synced_commit": last_synced_commit,
            # Whether a plain (bodiless) pull would 409 right now. Same source as
            # the guard, so the banner and the error cannot disagree.
            "pull_blocked": bool(blocking),
            "prompt_changes": prompt_changes,
            "setting_changes": setting_changes,
            # Workspace files carry NO blocks_pull flag: they never block a pull,
            # they are REPLACED by it wholesale whenever an env exists. That is a
            # property of the operation, not of a file, so the UI states it once
            # as a section header instead of per row.
            "file_changes": file_changes,
        }

    @staticmethod
    def compute_diff(
        session: Session,
        agent_id: uuid.UUID,
        owner: User,
        *,
        section: str,
        key: str,
    ) -> dict:
        """Unified diff of ONE changed prompt / setting / workspace file.

        The per-item drill-down behind the change lists
        :meth:`compute_status` returns: baseline (last synced revision) on the
        ``-`` side, live install on the ``+`` side, rendered as ``git diff``
        style text the UI shows verbatim in a console-style modal.

        ``section`` is ``"prompt"``, one of :data:`_SETTING_SECTIONS`, or
        ``"file"``; ``key`` is the RAW attribute name (prompt / setting) or the
        workspace-relative POSIX path (file) — the same ``key`` the status
        endpoint emits per change, deliberately not the human label.

        Read-only. Owner-resolved (404 for a missing source / non-owner). Returns
        ``{section, key, label, change_type, diff, binary, truncated}``; ``diff``
        is ``""`` when the two sides are equal or the content is binary.
        """
        if section not in _DIFF_SECTIONS:
            raise GitSourceValidationError(
                f"Unknown diff section {section!r}; expected one of "
                f"{', '.join(_DIFF_SECTIONS)}."
            )

        source = GitSourceService._resolve_source_owned(session, agent_id, owner)
        install = session.get(Agent, agent_id)
        if install is None:
            raise GitSourceNotFoundError("Agent not found")

        synced_rev = GitSourceService._resolve_synced_revision(
            session, source, install
        )
        if synced_rev is None:
            raise GitSourceValidationError(
                "This agent has no synced baseline yet, so there is nothing to "
                "diff against. Commit or pull once to establish one."
            )
        env = (
            session.get(AgentEnvironment, install.active_environment_id)
            if install.active_environment_id else None
        )

        # ── Prompts and settings: both sides are already in the DB ───────
        if section != "file":
            label, live, baseline = GitSourceService._diff_sides_for_field(
                session, install, synced_rev, env, section=section, key=key
            )
            # Release the connection before formatting (cheap, but keeps every
            # read path on the same discipline).
            session.commit()
            live_text = _render_setting_text(key, live)
            baseline_text = _render_setting_text(key, baseline)
            return {
                "section": section,
                "key": key,
                "label": label,
                "change_type": _classify_change(
                    _normalize_setting_value(key, live),
                    _normalize_setting_value(key, baseline),
                ) or "unchanged",
                **_unified_diff(baseline_text, live_text, key),
            }

        # ── Workspace file: live env tree vs. the baseline snapshot ──────
        rel = _resolve_diff_file_key(key)
        env_workspace_root = (
            Path(settings.ENV_INSTANCES_DIR) / str(env.id) if env is not None
            else None
        )
        snapshot_path = (
            Path(synced_rev.snapshot_path) if synced_rev.snapshot_path else None
        )
        # Lost-baseline recovery, same contract as compute_dirty / compute_status:
        # a synced revision whose snapshot dir was wiped is re-cloned rather than
        # silently diffed against nothing (which would render every file as new).
        rematerialize_ctx: dict | None = None
        synced_workspace: Path | None = None
        if snapshot_path is not None:
            candidate = snapshot_path / "workspace"
            if candidate.exists():
                synced_workspace = candidate
            else:
                rematerialize_ctx = {
                    "repo_url": source.repo_url,
                    "ref": source.ref,
                    "subdir": source.subdir,
                    "last_synced_commit": source.last_synced_commit,
                    "snapshot_dir": snapshot_path,
                    "key_material": _read_ssh_key_material(
                        session, source.ssh_key_id, source.owner_id
                    ),
                }

        # Release the pooled connection before any filesystem / network work.
        session.commit()

        if rematerialize_ctx is not None:
            synced_workspace = GitSourceService._rematerialize_baseline_snapshot(
                **rematerialize_ctx
            )

        live_text, live_binary = _read_diff_side(
            (env_workspace_root / WORKSPACE_ROOT_REL)
            if env_workspace_root is not None else None,
            rel,
        )
        baseline_text, baseline_binary = _read_diff_side(synced_workspace, rel)
        if live_binary or baseline_binary:
            return {
                "section": section,
                "key": key,
                "label": key,
                "change_type": "modified",
                "diff": "",
                "binary": True,
                "truncated": False,
            }
        change_type = (
            "unchanged" if live_text == baseline_text
            else "added" if baseline_text is None
            else "deleted" if live_text is None
            else "modified"
        )
        return {
            "section": section,
            "key": key,
            "label": key,
            "change_type": change_type,
            **_unified_diff(baseline_text or "", live_text or "", key),
        }

    @staticmethod
    def _diff_sides_for_field(
        session: Session,
        install: Agent,
        rev: AgentBundleRevision,
        env: AgentEnvironment | None,
        *,
        section: str,
        key: str,
    ) -> tuple[str, object, object]:
        """Resolve ``(label, live, baseline)`` for one prompt / setting field.

        The live side is read through the SAME registries and collectors
        :meth:`_settings_changes` uses, so a diff can never disagree with the
        change row the user clicked to open it. Raises
        :class:`GitSourceValidationError` for a key outside the registries —
        this is user-supplied input reaching ``getattr``, so the allowlist is
        the security boundary, not a convenience.
        """
        if section == "prompt":
            labels = dict(_PROMPT_FIELDS)
            if key not in labels:
                raise GitSourceValidationError(f"Unknown prompt field {key!r}.")
            return (
                labels[key],
                getattr(install, key, None),
                getattr(rev, key, None),
            )

        if section == "metadata":
            labels = dict(_METADATA_FIELDS)
            if key not in labels:
                raise GitSourceValidationError(f"Unknown settings field {key!r}.")
            return (
                labels[key],
                getattr(install, key, None),
                getattr(rev, key, None),
            )

        if section == "sdk":
            labels = dict(_SDK_FIELDS)
            if key not in labels:
                raise GitSourceValidationError(f"Unknown SDK field {key!r}.")
            return (
                labels[key],
                getattr(env, key, None) if env is not None else None,
                getattr(rev, key, None),
            )

        # specs — the live side is re-collected with the publish helpers.
        for field, label, collect in _SPEC_FIELDS:
            if field != key:
                continue
            try:
                live_specs = collect(session, install)
            except Exception as exc:  # noqa: BLE001
                # Same conservative-on-indeterminate rule _settings_changes
                # applies: a collector that cannot snapshot the live state must
                # not 500 a read-only drill-down. Clear the poisoned transaction
                # and show the baseline against an explicit marker.
                logger.warning(
                    "git diff: could not collect %s for agent %s (%s)",
                    field, install.id, exc,
                )
                GitSourceService._clear_poisoned_transaction(
                    session, context=f"diff ({field}) for agent {install.id}"
                )
                raise GitSourceValidationError(
                    f"Could not read the live {label.lower()} for this agent."
                ) from exc
            return label, live_specs, getattr(rev, key, None)

        raise GitSourceValidationError(f"Unknown settings field {key!r}.")

    @staticmethod
    def _file_hashes(workspace_root: Path) -> dict[str, str]:
        """Map each file under a ``workspace/`` subtree to its SHA-256 digest.

        Relative POSIX path → hex digest. Symlinks and non-files are skipped
        (mirroring :meth:`PublishService.hash_workspace_tree`); a missing root
        yields an empty map. The per-file analogue of the whole-tree digest, used
        by :meth:`compute_status` to diff the live capture against the baseline.
        """
        hashes: dict[str, str] = {}
        if not workspace_root.exists() or not workspace_root.is_dir():
            return hashes
        for f in workspace_root.rglob("*"):
            if f.is_symlink() or not f.is_file():
                continue
            rel = f.relative_to(workspace_root).as_posix()
            try:
                hashes[rel] = hashlib.sha256(f.read_bytes()).hexdigest()
            except OSError:
                continue
        return hashes

    # ── Pull (update) ────────────────────────────────────────────────

    @staticmethod
    async def pull_update(
        *,
        session: Session,
        agent_id: uuid.UUID,
        owner: User,
        conflict_resolution: str | None = None,
    ) -> Agent:
        """Pull the latest remote revision onto the install. Per-agent locked.

        ``conflict_resolution`` (one of :data:`GIT_PULL_RESOLUTIONS`) decides what
        happens when local state a pull would overwrite has drifted:

        * ``None`` — fail loud with a structured 409 (today's behavior, and what
          the GitOps webhook dispatcher relies on). Never give this a default.
        * ``"keep_local"`` — pull, but leave the drifted prompt / metadata fields
          at their local value. **Post-condition, by construction: the install is
          then dirty on exactly those fields.** That is correct and git-like
          (pull, then commit on top) but must be said in the UI, or it reads as a
          failed pull.
        * ``"take_remote"`` — discard the local drift.

        Neither mode preserves workspace FILES: the workspace is one tree with
        one baseline and ``replace_bundle_content`` replaces it either way.
        Precisely because that is true of BOTH modes, both first persist the live
        agent as a backup revision (:meth:`_capture_backup_revision`) — it is the
        only record of a locally edited workspace file after either resolution.
        """
        async with _lock_for(str(agent_id)):
            return await GitSourceService._pull_locked(
                session, agent_id, owner, conflict_resolution=conflict_resolution
            )

    @staticmethod
    async def _pull_locked(
        session: Session,
        agent_id: uuid.UUID,
        owner: User,
        *,
        conflict_resolution: str | None = None,
    ) -> Agent:
        source = GitSourceService._resolve_source_owned(session, agent_id, owner)
        # On any failure below, stamp ERROR + last_error on the source so the UI
        # can surface it (N2), then re-raise the original error unchanged.
        try:
            # Validated service-side (not by a route enum) so the service stays
            # the sole enforcement point for every caller — route, webhook, CLI.
            if (
                conflict_resolution is not None
                and conflict_resolution not in GIT_PULL_RESOLUTIONS
            ):
                raise GitSourceValidationError(
                    f"Unknown conflict_resolution '{conflict_resolution}'. "
                    f"Expected one of: {', '.join(GIT_PULL_RESOLUTIONS)}."
                )

            if source.sync_direction not in (
                GitSyncDirection.PULL,
                GitSyncDirection.BIDIRECTIONAL,
            ):
                raise GitSourceValidationError(
                    "This git source is push-only; pull is not allowed."
                )

            install = session.get(Agent, agent_id)
            if install is None:
                raise GitSourceNotFoundError("Agent not found")
            env = (
                session.get(AgentEnvironment, install.active_environment_id)
                if install.active_environment_id else None
            )
            if env is None:
                raise GitSourceValidationError(
                    "Start the environment before pulling so its workspace exists."
                )

            revision: AgentBundleRevision
            pulled_sha: str
            blocking: list[dict] = []
            backup_revision: AgentBundleRevision | None = None
            # Deliberate scope decision: unlike the polled read paths (which release
            # the pooled connection before the remote git I/O), this write path holds
            # it across the network work. It is a low-frequency POST that legitimately
            # mutates the source afterwards, so the read-path split is intentionally
            # not applied; the hold is bounded by the GIT_HTTP_LOW_SPEED_* idle guard
            # and SSH ConnectTimeout/keepalive.
            with _resolve_ssh_key(session, source.ssh_key_id, source.owner_id) as key:
                remote_sha = ls_remote_head(source.repo_url, source.ref, key)
                if remote_sha == source.last_synced_commit:
                    # Already up to date — no-op. Idempotent webhook fires land
                    # here and stay quiet even on an install with local edits:
                    # the dirty guard is checked BELOW, only once there is
                    # actually something to pull.
                    source.last_sync_at = datetime.now(UTC)
                    source.status = GitSourceStatus.CONNECTED
                    source.last_error = None
                    session.add(source)
                    session.commit()
                    return install

                # Dirty guard (manifest/DB side): local prompt / metadata edits
                # since the last sync block the pull unless the caller picked a
                # resolution. Runs only after the no-op short-circuit so an
                # unchanged remote never trips it.
                if conflict_resolution is None:
                    GitSourceService._assert_not_dirty(
                        session, source, install, env
                    )
                else:
                    # Same helper, same narrowing — but the caller has already
                    # chosen, so compute the set (keep_local needs it, the audit
                    # log wants it) instead of raising on it.
                    blocking = GitSourceService._pull_blocking_changes(
                        session,
                        install,
                        GitSourceService._resolve_synced_revision(
                            session, source, install
                        ),
                        env,
                    )

                with clone_repository_context(
                    source.repo_url, branch=source.ref, ssh_key_path=key
                ) as (repo_path, repo):
                    pulled_sha = get_current_commit_hash(repo)
                    src = _resolve_subdir(repo_path, source.subdir)
                    manifest = _read_and_validate_tree(src)
                    _assert_no_oversized_files(src / "workspace")

                    bundle = (
                        session.get(AgentBundle, source.bundle_uuid)
                        if source.bundle_uuid else None
                    )
                    if bundle is None:
                        raise GitSourceValidationError(
                            "The backing bundle is missing; re-checkout the agent."
                        )

                    if conflict_resolution is not None:
                        # Capture the live agent before anything mutates it, but
                        # as LATE as possible: everything that can still abort
                        # the pull (clone, tree validation, oversize check,
                        # bundle resolve) has already run.
                        #
                        # Taken on BOTH resolutions, deliberately widening plan
                        # §3.4's "no backup for keep_local: nothing is discarded
                        # on that path". That premise is false for FILES:
                        # ``replace_bundle_content`` replaces the whole workspace
                        # identically either way, so ``keep_local`` preserves
                        # prompt/metadata FIELDS while still destroying a locally
                        # edited ``scripts/foo.py``. This snapshot is the only
                        # record of those files, so it belongs on both paths.
                        #
                        # Ordering is load-bearing. The dirty-check baseline is
                        # the HIGHEST revision_number on the bundle
                        # (``_resolve_synced_revision``), so the backup must land
                        # BELOW the incoming revision — otherwise a snapshot of
                        # the live agent becomes its own baseline, the install
                        # reports clean while holding unpushed work, and the next
                        # (possibly webhook-driven, unguarded) pull discards it
                        # silently. Hence: backup first, incoming second, and the
                        # backup is rolled back if the incoming persist fails.
                        #
                        # Fail loud: a failed backup aborts the discard. Silently
                        # discarding the user's work after promising a backup is
                        # the worst outcome available on this path.
                        backup_revision = (
                            GitSourceService._capture_backup_revision(
                                session,
                                install=install,
                                env=env,
                                bundle=bundle,
                                owner=owner,
                                release_notes=(
                                    "Automatic backup of the live agent before "
                                    f"git pull ({conflict_resolution})"
                                ),
                            )
                        )

                    try:
                        revision = GitSourceService._persist_revision(
                            session,
                            bundle=bundle,
                            src=src,
                            manifest=manifest,
                            published_by_user_id=source.owner_id,
                        )
                    except Exception:
                        # Without this the just-committed backup would be left as
                        # the newest revision — i.e. the baseline — on a pull that
                        # never happened. See the ordering note above.
                        GitSourceService._discard_backup_revision(
                            session, backup_revision
                        )
                        raise

            # Apply the new revision onto the live env (reuses replace_bundle_content).
            preserve_fields = (
                {change["field"] for change in blocking}
                if conflict_resolution == GIT_PULL_KEEP_LOCAL else None
            )
            await GitSourceService._apply_revision_to_install(
                session, install, revision, preserve_fields=preserve_fields
            )

            source.last_synced_commit = pulled_sha
            source.last_sync_at = datetime.now(UTC)
            source.status = GitSourceStatus.CONNECTED
            source.last_error = None
            session.add(source)
            session.commit()
            session.refresh(install)
        except (
            GitSourceConflictError,
            GitSourceValidationError,
            GitSourceNotFoundError,
        ):
            # Expected, user-actionable outcomes (dirty/non-ff → 409, wrong
            # direction / unreadable workspace / missing env → 400, missing
            # agent → 404). These guards fire before any DB mutation, so there
            # is nothing to roll back and the source is NOT in an error state —
            # leave its status untouched and let the route map the exception.
            raise
        except Exception as exc:
            # Genuine operational failure (egress-blocked, clone/fetch/network,
            # filesystem/IO, or an unexpected error after a mutation may have
            # started): stamp ERROR + last_error, then re-raise unchanged.
            GitSourceService._mark_source_error(session, source.id, exc)
            raise

        logger.info(
            "git pull: agent %s advanced to %s (rev %s)",
            agent_id, pulled_sha, revision.revision_number,
        )
        if conflict_resolution is not None:
            # Audit trail for a resolution the user explicitly chose. Not a
            # SecurityEvent: this is an owner acting on their own agent, and no
            # other git operation emits one either.
            blocked_labels = [change["label"] for change in blocking]
            logger.info(
                "git pull: agent %s conflict_resolution=%s %s=%s backup_revision=%s",
                agent_id,
                conflict_resolution,
                (
                    "preserved"
                    if conflict_resolution == GIT_PULL_KEEP_LOCAL else "discarded"
                ),
                blocked_labels or "none",
                backup_revision.revision_number if backup_revision else "none",
            )
        return install

    # ── Push (publish) ───────────────────────────────────────────────

    @staticmethod
    async def push(
        *,
        session: Session,
        agent_id: uuid.UUID,
        owner: User,
        commit_message: str,
        version: str | None = None,
        also_publish_bundle: bool = False,
    ) -> AgentGitSource:
        """Capture the live workspace and fast-forward-push it. Per-agent locked."""
        async with _lock_for(str(agent_id)):
            return await GitSourceService._push_locked(
                session,
                agent_id,
                owner,
                commit_message=commit_message,
                version=version,
                also_publish_bundle=also_publish_bundle,
            )

    @staticmethod
    async def _push_locked(
        session: Session,
        agent_id: uuid.UUID,
        owner: User,
        *,
        commit_message: str,
        version: str | None,
        also_publish_bundle: bool,
    ) -> AgentGitSource:
        source = GitSourceService._resolve_source_owned(session, agent_id, owner)
        # On any failure below, stamp ERROR + last_error on the source so the UI
        # can surface it (N2), then re-raise the original error unchanged.
        try:
            if source.sync_direction not in (
                GitSyncDirection.PUSH,
                GitSyncDirection.BIDIRECTIONAL,
            ):
                raise GitSourceValidationError(
                    "This git source is pull-only; push is not allowed."
                )

            install = session.get(Agent, agent_id)
            if install is None:
                raise GitSourceNotFoundError("Agent not found")
            env = (
                session.get(AgentEnvironment, install.active_environment_id)
                if install.active_environment_id else None
            )
            if env is None:
                raise GitSourceValidationError(
                    "Start the environment before pushing so its workspace is readable."
                )
            env_workspace_root = Path(settings.ENV_INSTANCES_DIR) / str(env.id)
            try:
                PublishService._assert_workspace_readable(env, env_workspace_root)
            except ValueError as exc:
                raise GitSourceValidationError(str(exc)) from exc

            # also_publish_bundle only makes sense for a publisher install —
            # reject early (before the push) so we don't push then 400.
            if also_publish_bundle and not install.is_publisher_install:
                raise GitSourceValidationError(
                    "also_publish_bundle requires this agent to be a publisher "
                    "install (publish it from the bundle UI first)."
                )

            new_sha: str
            # Deliberate scope decision: unlike the polled read paths (which release
            # the pooled connection before the remote git I/O), this write path holds
            # it across the network work. It is a low-frequency POST that legitimately
            # mutates the source afterwards, so the read-path split is intentionally
            # not applied; the hold is bounded by the GIT_HTTP_LOW_SPEED_* idle guard
            # and SSH ConnectTimeout/keepalive.
            with _resolve_ssh_key(session, source.ssh_key_id, source.owner_id) as key:
                # ff precheck: only block when the remote advance is relevant to
                # this agent's subdir (matching the update-check banner via the
                # shared helper); an advance touching only an unrelated folder of a
                # shared repo falls through, and a genuine non-ff push is still
                # caught loudly by fast_forward_push's merge-base check below.
                remote_sha = ls_remote_head(source.repo_url, source.ref, key)
                if (
                    source.last_synced_commit
                    and remote_sha != source.last_synced_commit
                    and GitSourceService._remote_change_is_relevant(
                        repo_url=source.repo_url,
                        ref=source.ref,
                        subdir=source.subdir,
                        last_synced_commit=source.last_synced_commit,
                        remote_sha=remote_sha,
                        ssh_key_path=key,
                    )
                ):
                    raise GitSourceConflictError(
                        "Remote has advanced since the last sync — pull first."
                    )

                # Full-history clone so fast_forward_push's merge-base check is sound.
                with clone_repository_context(
                    source.repo_url, branch=source.ref, ssh_key_path=key, depth=None
                ) as (repo_path, repo):
                    new_sha = GitSourceService._capture_and_push(
                        session,
                        install=install,
                        env=env,
                        source_like=source,
                        owner=owner,
                        key=key,
                        repo=repo,
                        repo_path=repo_path,
                        commit_message=commit_message,
                        version=version,
                    )

            source.last_synced_commit = new_sha
            source.last_sync_at = datetime.now(UTC)
            source.status = GitSourceStatus.CONNECTED
            source.last_error = None
            session.add(source)
            session.commit()
            session.refresh(source)
        except (
            GitSourceConflictError,
            GitSourceValidationError,
            GitSourceNotFoundError,
        ):
            # Expected, user-actionable outcomes (non-ff remote → 409, wrong
            # direction / unreadable workspace / also_publish precondition /
            # missing env → 400, missing agent → 404). These guards fire before
            # any DB mutation, so there is nothing to roll back and the source
            # is NOT in an error state — leave its status untouched and let the
            # route map the exception.
            raise
        except Exception as exc:
            # Genuine operational failure (egress-blocked, clone/fetch/network,
            # non-fast-forward push, filesystem/IO, or an unexpected error after
            # a mutation may have started): stamp ERROR + last_error, then
            # re-raise unchanged.
            GitSourceService._mark_source_error(session, source.id, exc)
            raise

        if also_publish_bundle:
            # Best-effort parallel bundle revision — the git push already
            # succeeded, so a publish hiccup must not fail the push.
            try:
                await PublishService.publish(
                    session=session,
                    install=install,
                    publisher_user_id=owner.id,
                    release_notes=commit_message,
                    version=version,
                )
            except Exception as exc:  # noqa: BLE001 — publish is the secondary action
                logger.warning(
                    "git push: also_publish_bundle failed for agent %s: %s",
                    agent_id, exc,
                )

        logger.info("git push: agent %s pushed %s to %s", agent_id, new_sha, source.ref)
        return source

    # ── Internal helpers ─────────────────────────────────────────────

    @staticmethod
    def _assert_not_already_checked_out(
        session: Session, *, user_id: uuid.UUID, bundle_id: str
    ) -> None:
        """Reject (409) a second checkout of the same repo for the same user.

        A git checkout always creates a consumer install (``is_publisher_install
        = False``); the partial unique constraint ``(owner_id, bundle_id,
        is_publisher_install)`` would otherwise raise ``IntegrityError`` partway
        through the install. Detect it up front so the common duplicate path is a
        clean 409 and never strands a half-imported bundle/revision.
        """
        existing = session.exec(
            select(Agent).where(
                Agent.owner_id == user_id,
                Agent.bundle_id == bundle_id,
                Agent.is_publisher_install == False,  # noqa: E712
            )
        ).first()
        if existing is not None:
            raise GitSourceConflictError(
                "This repository is already checked out for your account."
            )

    @staticmethod
    def _cleanup_orphan_import(
        session: Session,
        revision: AgentBundleRevision,
        bundle: AgentBundle | None,
    ) -> None:
        """Remove a bundle/revision/snapshot stranded by a failed checkout (N6).

        Called after the bundle + revision are committed but the install fails.
        Deletes the revision row + its on-disk snapshot, and the bundle row only
        when this checkout created it (``bundle`` is None for a reused/shared
        row). Best-effort: a cleanup hiccup must never mask the original error.
        """
        try:
            snapshot_path = getattr(revision, "snapshot_path", None)
            rev = session.get(AgentBundleRevision, revision.id)
            if rev is not None:
                session.delete(rev)
            if bundle is not None:
                bundle_row = session.get(AgentBundle, bundle.id)
                if bundle_row is not None:
                    session.delete(bundle_row)
            session.commit()
            if snapshot_path:
                shutil.rmtree(snapshot_path, ignore_errors=True)
        except Exception as exc:  # noqa: BLE001 — never mask the original error
            session.rollback()
            logger.warning(
                "git checkout: failed to clean up orphaned import (revision %s): %s",
                getattr(revision, "id", None), exc,
            )

    @staticmethod
    def _clear_poisoned_transaction(session: Session, *, context: str) -> None:
        """Best-effort rollback of a transaction poisoned by a swallowed DB error.

        Once SQLAlchemy raises inside a transaction, every later statement on
        that session — including the ``session.commit()`` the pool-safe read
        paths use to release their connection — fails with
        ``PendingRollbackError``. Any handler that swallows a DB error must
        therefore clear the transaction, or it merely relocates the failure.

        Implementation note: SQLAlchemy 2.0's ``session.rollback()`` always
        rolls back to the root transaction (``_to_root=True``), which in the
        test framework (savepoint-based isolation) destroys all previously
        committed test data. We use ``get_nested_transaction().rollback()``
        when inside a savepoint so only the current (likely empty) savepoint
        is rolled back — this issues ``ROLLBACK TO SAVEPOINT`` rather than a
        full ``ROLLBACK``, preserving the outer transaction's committed rows.
        In production there are no active nested transactions so the fallback
        ``session.rollback()`` is used unchanged.

        NEVER throws: a stale/inactive savepoint object (already released by a
        prior commit) would otherwise raise out of a handler whose whole job is
        to not raise. The common genuine-failure case (a non-DB clone/egress
        failure) leaves the session clean, so this is a no-op.
        """
        try:
            nested = session.get_nested_transaction()
            if nested is not None and nested.is_active:
                nested.rollback()
            elif nested is None:
                # No active savepoint (the production case): only a full
                # rollback can clear a poisoned transaction. Guarded so a
                # stale/inactive state is a harmless no-op.
                session.rollback()
        except Exception as rb_exc:  # noqa: BLE001 — rollback must never propagate
            logger.debug("git sync: rollback skipped for %s: %s", context, rb_exc)

    @staticmethod
    def _mark_source_error(
        session: Session, source_id: uuid.UUID, exc: Exception
    ) -> None:
        """Stamp ``status=ERROR`` + ``last_error`` on a git source (N2).

        Called from the pull/push failure path so the UI can surface why the
        last sync failed. Rolls back any poisoned transaction first, re-fetches
        the row, and swallows its own errors so it never masks (or replaces) the
        original exception the caller is about to re-raise.
        """
        # Step 1: best-effort rollback of any poisoned transaction, or the error
        # stamp below would itself fail with PendingRollbackError.
        GitSourceService._clear_poisoned_transaction(
            session, context=f"error stamp on source {source_id}"
        )

        # Step 2: always attempt the stamp. Swallows + logs its own errors so it
        # never masks (or replaces) the original exception the caller re-raises.
        try:
            source = session.get(AgentGitSource, source_id)
            if source is None:
                return
            source.status = GitSourceStatus.ERROR
            source.last_error = str(exc)
            session.add(source)
            session.commit()
        except Exception as inner:  # noqa: BLE001 — never mask the original error
            logger.warning(
                "git sync: failed to stamp error status on source %s: %s",
                source_id, inner,
            )

    @staticmethod
    def _resolve_source_owned(
        session: Session, agent_id: uuid.UUID, owner: User
    ) -> AgentGitSource:
        """Resolve the agent's git source, enforcing per-agent ownership.

        Returns 404-equivalent (``GitSourceNotFoundError``) for a missing source
        AND for a source owned by another user — never leaking existence to a
        non-owner.
        """
        source = session.exec(
            select(AgentGitSource).where(AgentGitSource.agent_id == agent_id)
        ).first()
        if source is None:
            raise GitSourceNotFoundError("No git source configured for this agent")
        if source.owner_id != owner.id and not owner.is_superuser:
            raise GitSourceNotFoundError("No git source configured for this agent")
        return source

    @staticmethod
    def _resolve_or_create_bundle(
        session: Session,
        *,
        bundle_id: str,
        user: User,
        display_name: str,
    ) -> tuple[AgentBundle, bool]:
        """Resolve (or create) the shared bundle row for a git-sourced import.

        A git checkout is a *consumer-style* import: the checking-out user is
        NOT the publisher. The manifest's ``bundle_id`` is globally unique on
        the instance, so all users who check out the same repo share ONE
        ``AgentBundle`` row — exactly how catalog installs share one bundle
        across many consumers — and per-user App Data (keyed on the
        ``bundle_id`` string) reattaches across checkouts of the same agent.

        The row is created OWNERLESS (``publisher_user_id = NULL``) and
        private/unlisted (the model defaults), so it is a local import — never a
        catalog publish and never visible in the public catalog.

        Reuse rules for an existing row:
        * ownerless (git-shared) — reuse; every user shares it.
        * owned by THIS user (their own catalog bundle) — reuse (dogfooding the
          git mirror of an agent they also published locally).
        * owned by ANOTHER real publisher — reject (409). Reusing it would
          inject a git revision into someone else's catalog bundle; the
          cross-tenant injection guard stays for genuinely-owned rows.

        Returns ``(bundle, created)`` where ``created`` is True only when this
        call inserted a new row (used by the caller to clean up an orphaned
        bundle if the subsequent install fails).
        """
        existing = BundleService.get_bundle_by_id(session, bundle_id)
        if existing is not None:
            if (
                existing.publisher_user_id is not None
                and existing.publisher_user_id != user.id
                and not user.is_superuser
            ):
                raise GitSourceConflictError(
                    f"bundle_id '{bundle_id}' is a published catalog bundle "
                    "owned by another user on this instance."
                )
            return existing, False
        bundle = BundleService.create_bundle(
            session=session,
            bundle_id=bundle_id,
            publisher_user_id=None,
            display_name=display_name,
        )
        return bundle, True

    @staticmethod
    def _next_revision_number(session: Session, bundle_uuid: uuid.UUID) -> int:
        stmt = select(
            func.coalesce(func.max(AgentBundleRevision.revision_number), 0)
        ).where(AgentBundleRevision.bundle_id == bundle_uuid)
        return (session.exec(stmt).one() or 0) + 1

    @staticmethod
    def _persist_revision(
        session: Session,
        *,
        bundle: AgentBundle,
        src: Path,
        manifest: dict,
        published_by_user_id: uuid.UUID,
    ) -> AgentBundleRevision:
        """Persist a cloned tree as an ``AgentBundleRevision`` (internal SSOT).

        The cloned ``workspace/`` subtree is copied into bundle storage through
        the SAME denylist + symlink guards publish applies (``iter_bundle_toplevel``
        / ``safe_copytree``) so an untrusted repo cannot inject ``credentials/`` /
        ``app-data/`` / runtime state into the install on the way in.
        """
        rev_number = GitSourceService._next_revision_number(session, bundle.id)
        snapshot_dir = (
            Path(settings.BUNDLE_STORAGE_DIR)
            / bundle.bundle_id
            / str(rev_number)
        )
        content_hash = _persist_clone_as_snapshot(src, snapshot_dir, manifest)

        revision = AgentBundleRevision(
            bundle_id=bundle.id,
            revision_number=rev_number,
            # Internal SSOT / dirty-check baseline — NOT a catalog publish, so it
            # is excluded from the Revisions UI and version suggestion.
            origin=REVISION_ORIGIN_GIT,
            manifest=manifest,
            snapshot_path=str(snapshot_dir),
            content_hash=content_hash,
            published_by_user_id=published_by_user_id,
            **RevisionFormat.manifest_to_revision_fields(manifest),
        )
        session.add(revision)
        session.commit()
        session.refresh(revision)
        return revision

    @staticmethod
    def _build_live_manifest(
        session: Session,
        *,
        install: Agent,
        env: AgentEnvironment,
        bundle: AgentBundle | None,
        version: str | None,
        release_notes: str | None,
    ) -> dict:
        """Build the ``cinna.agent.json`` manifest describing the LIVE agent.

        The non-git half of a capture: allocate the next revision number, run the
        three ``PublishService._collect_*`` spec helpers, and hand them to
        :meth:`RevisionFormat.build_manifest`. Shared by :meth:`_capture_and_push`
        (which then writes it into the clone and commits) and
        :meth:`_capture_backup_revision` (which persists it straight to bundle
        storage) so a backup can never describe the live agent differently from
        the way a push would — a backup built off a divergent manifest shape
        would be a broken restore point, which is the whole reason it exists.

        The git half of ``_capture_and_push`` is deliberately NOT extracted with
        it: that body writes the tree into the clone directory precisely so the
        commit picks it up, and the revision it persists is the tree that was
        just pushed. Reusing it for a backup would mean either a second full
        workspace snapshot on the push path or a callback-shaped seam — more
        disruption than the duplication it removes.
        """
        rev_number = (
            GitSourceService._next_revision_number(session, bundle.id)
            if bundle else 1
        )
        return RevisionFormat.build_manifest(
            install=install,
            env=env,
            cred_specs=PublishService._collect_credential_specs(session, install),
            schedule_specs=PublishService._collect_schedule_specs(session, install),
            plugin_specs=PublishService._collect_plugin_specs(session, install),
            revision_number=rev_number,
            version=version,
            release_notes=release_notes,
        )

    @staticmethod
    def _capture_backup_revision(
        session: Session,
        *,
        install: Agent,
        env: AgentEnvironment,
        bundle: AgentBundle,
        owner: User,
        release_notes: str,
    ) -> AgentBundleRevision:
        """Persist the live agent as an ``AgentBundleRevision`` — no git, no push.

        The safety net behind BOTH pull resolutions: each replaces the whole
        workspace, and ``take_remote`` additionally discards the drifted fields,
        so the live state is first captured as an internal (``origin="git"``,
        Revisions-tab-invisible) revision that can be restored from.
        ``_capture_and_push`` minus the git half — snapshot
        the live workspace into a temp dir, build the manifest from the live row
        (:meth:`_build_live_manifest`), persist both through the same
        :meth:`_persist_revision` every sync uses (denylist + symlink guards
        included).

        Taken unconditionally on any resolution, even when no FIELD blocks the
        pull: ``replace_bundle_content`` replaces the whole workspace either way,
        so this snapshot is also the only record of locally edited workspace
        files. It costs one ``revision_number`` from the shared counter, widening
        the numbering gaps the Revisions tab already tolerates.

        **Never swallows.** Any failure propagates to the caller, which must call
        this BEFORE it mutates anything: silently discarding the user's work
        after promising a backup is the worst outcome available on this path, so
        this is the one place in the feature where best-effort is wrong.

        The caller also owns the ORDERING contract — the backup must end up
        BELOW the incoming revision, and must be removed via
        :meth:`_discard_backup_revision` if the pull does not complete. See the
        note at the call site in :meth:`_pull_locked`.
        """
        env_workspace_root = Path(settings.ENV_INSTANCES_DIR) / str(env.id)
        # MUST run before the snapshot, exactly as connect and push do.
        # ``iter_bundle_toplevel`` yields nothing — and does NOT raise — when the
        # workspace root is missing or unreadable, so without this the capture
        # below "succeeds" with an EMPTY ``workspace/`` and the pull then
        # replaces the real one: silent data loss behind a promised backup. A
        # lost env instance dir is a state this codebase already knows about
        # (it is why ``_rematerialize_baseline_snapshot`` exists).
        try:
            PublishService._assert_workspace_readable(env, env_workspace_root)
        except ValueError as exc:
            raise GitSourceValidationError(str(exc)) from exc

        manifest = GitSourceService._build_live_manifest(
            session,
            install=install,
            env=env,
            bundle=bundle,
            version=None,
            release_notes=release_notes,
        )
        temp_dir = Path(tempfile.mkdtemp(prefix="git_pull_backup_"))
        try:
            # Same post-denylist capture compute_dirty / compute_status make, so
            # the backup holds exactly what a commit would have preserved.
            PublishService._snapshot_workspace_tree(env_workspace_root, temp_dir)
            return GitSourceService._persist_revision(
                session,
                bundle=bundle,
                src=temp_dir,
                manifest=manifest,
                published_by_user_id=owner.id,
            )
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    @staticmethod
    def _discard_backup_revision(
        session: Session, revision: AgentBundleRevision | None
    ) -> None:
        """Roll back a pre-discard backup whose pull then failed.

        A backup revision is only safe to keep once the incoming revision lands
        above it: it is a snapshot of the LIVE agent, so if it is left as the
        newest revision on the bundle it becomes the dirty-check baseline
        (:meth:`_resolve_synced_revision`) and the install reports clean while
        still holding unpushed work — after which the next pull, including an
        unguarded webhook one, discards that work silently. Removing it restores
        the pre-pull baseline exactly.

        Best-effort **only because it runs while an exception is already in
        flight** and must never mask it — not because a leftover row is harmless.
        It is not: a surviving backup is exactly the broken-baseline state
        described above. That is why the failure path logs at WARNING with the
        operational consequence spelled out; it is the only signal that an agent
        may now be reporting clean while holding unpushed work.
        """
        if revision is None:
            return
        revision_number: object = "?"
        snapshot_path: str | None = None
        try:
            # FIRST — before touching a single attribute. The failure that
            # brought us here may have poisoned the transaction, and ``revision``
            # is expired (its own ``_persist_revision`` committed), so a bare
            # ``revision.snapshot_path`` would lazy-load and raise
            # ``PendingRollbackError`` out of a handler whose job is to not raise.
            GitSourceService._clear_poisoned_transaction(
                session, context="pull backup revision rollback"
            )
            revision_number = revision.revision_number
            snapshot_path = revision.snapshot_path
            session.delete(revision)
            session.commit()
        except Exception as exc:  # noqa: BLE001 — must not mask the real failure
            logger.warning(
                "git pull: could not roll back backup revision %s (%s) — it is "
                "now the newest revision on the bundle, so this agent will "
                "report NO local changes while still holding unpushed work, and "
                "the next pull will not be guarded. Delete the revision row to "
                "restore the previous baseline.",
                revision_number, exc,
            )
            return
        if snapshot_path:
            shutil.rmtree(Path(snapshot_path), ignore_errors=True)
        logger.info(
            "git pull: rolled back backup revision %s (pull did not complete)",
            revision_number,
        )

    @staticmethod
    def _capture_and_push(
        session: Session,
        *,
        install: Agent,
        env: AgentEnvironment,
        source_like: AgentGitSource,
        owner: User,
        key: str | None,
        repo,
        repo_path: str,
        commit_message: str,
        version: str | None,
    ) -> str:
        """Capture the live env workspace into ``repo``, commit, ff-push, snapshot.

        The single capture+commit+push body shared by :meth:`_push_locked` and
        :meth:`connect` (so the two paths cannot drift). ``source_like`` carries
        ``repo_url`` / ``subdir`` / ``ref`` / ``bundle_uuid`` — the persisted row
        for push, the in-memory unsaved source for connect. ``repo`` is either a
        full-history clone (push / connect-onto-existing) or a freshly
        :func:`init_repo_with_remote`-d tree (connect-onto-empty). Returns the
        pushed commit SHA.

        On a real push (the tree changed / was newly created) it ALSO persists
        the captured tree as an ``AgentBundleRevision`` — the immutable internal
        record of what was pushed and the stable baseline the dirty check
        compares the live workspace against.
        """
        env_workspace_root = Path(settings.ENV_INSTANCES_DIR) / str(env.id)

        # A freshly-initialized repo (connect-onto-empty) has no commits yet —
        # there is no HEAD to read, so there is no "unchanged" short-circuit.
        try:
            head_before: str | None = get_current_commit_hash(repo)
        except Exception:  # noqa: BLE001 — unborn HEAD (init path) has no commit yet
            head_before = None

        src = _resolve_subdir(repo_path, source_like.subdir)
        src.mkdir(parents=True, exist_ok=True)

        bundle = (
            session.get(AgentBundle, source_like.bundle_uuid)
            if source_like.bundle_uuid else None
        )
        manifest = GitSourceService._build_live_manifest(
            session,
            install=install,
            env=env,
            bundle=bundle,
            version=version,
            release_notes=commit_message,
        )

        # Remove the existing captured workspace so deletions propagate
        # (write_tree only overwrites/creates; it never prunes).
        stale_workspace = src / "workspace"
        if stale_workspace.exists():
            shutil.rmtree(stale_workspace)

        RevisionFormat.write_tree(
            env_workspace_root=env_workspace_root,
            dest=src,
            manifest=manifest,
            manifest_filename=GIT_MANIFEST_FILENAME,
        )
        (src / ".gitignore").write_text(RevisionFormat.generate_gitignore())

        # Binary-in-git hygiene: reject oversized captured assets before commit.
        _assert_no_oversized_files(src / "workspace")

        author_name = owner.full_name or owner.email
        new_sha = commit_all(repo, commit_message, author_name, owner.email)
        if head_before is not None and new_sha == head_before:
            # Nothing changed (existing remote, identical tree): no push, no new
            # revision — the latest revision already reflects this tree.
            logger.info(
                "git capture: agent %s — working tree unchanged, nothing to push",
                install.id,
            )
            return new_sha

        # ff-only; raises GitNonFastForwardError on a non-ff remote. On the
        # first push (absent remote ref) this creates the branch.
        #
        # Two-system gap (acceptable, fail-loud): the commit is on the remote
        # once this returns. If the revision persist or the caller's source-row
        # commit then fails, the remote is ahead of the platform record. The
        # external repo is the durable record (Decision 2); a retry re-detects
        # the now-non-empty subdir (→ 409 "use checkout") rather than
        # double-pushing, and the developer reconciles via their own git client.
        fast_forward_push(repo, source_like.ref, key)

        # Persist the captured tree as the internal SSOT + dirty baseline.
        if bundle is not None:
            GitSourceService._persist_revision(
                session,
                bundle=bundle,
                src=src,
                manifest=manifest,
                published_by_user_id=owner.id,
            )
        return new_sha

    @staticmethod
    def _cleanup_orphan_bundle(session: Session, bundle_uuid: uuid.UUID) -> None:
        """Remove an ownerless bundle row stranded by a failed connect.

        Connect creates the backing bundle (ownerless, keyed on ``bundle_id``)
        before the capture/push; if that fails and THIS connect created the row,
        drop it so a failed connect leaves no half-state. Best-effort: a cleanup
        hiccup must never mask the original error.

        Like :meth:`_mark_source_error`, a poisoned transaction is cleared via
        ``get_nested_transaction().rollback()`` when inside a savepoint
        (``ROLLBACK TO SAVEPOINT`` — preserves the outer transaction's committed
        rows, which a full ``session.rollback()`` would destroy under the
        savepoint-based test isolation) and a full ``session.rollback()`` only in
        production where there is no active savepoint.
        """
        try:
            nested = session.get_nested_transaction()
            if nested is not None and nested.is_active:
                nested.rollback()
            elif nested is None:
                session.rollback()
        except Exception as rb_exc:  # noqa: BLE001 — rollback must never block cleanup
            logger.debug(
                "git connect: pre-cleanup rollback skipped for bundle %s: %s",
                bundle_uuid, rb_exc,
            )

        try:
            row = session.get(AgentBundle, bundle_uuid)
            if row is not None:
                session.delete(row)
                session.commit()
        except Exception as exc:  # noqa: BLE001 — never mask the original error
            logger.warning(
                "git connect: failed to clean up orphaned bundle %s: %s",
                bundle_uuid, exc,
            )

    @staticmethod
    def _resolve_synced_revision(
        session: Session, source: AgentGitSource, install: Agent
    ) -> AgentBundleRevision | None:
        """Resolve the revision representing the last sync (dirty-check baseline).

        Prefers the **latest** revision on the source's bundle — every sync
        (checkout / pull / push / connect) appends one, so the newest is always
        the most recent sync (a checkout-then-push install's
        ``installed_revision_id`` would otherwise point at the stale checkout).
        Falls back to ``installed_revision_id``; ``None`` when no baseline exists.

        ``.limit(1)`` matters: ``.first()`` alone only truncates client-side, so
        without it every revision row of the bundle (each carrying a full
        ``manifest`` JSON blob) is materialized on a polled read path.
        """
        if source.bundle_uuid is not None:
            rev = session.exec(
                select(AgentBundleRevision)
                .where(AgentBundleRevision.bundle_id == source.bundle_uuid)
                .order_by(AgentBundleRevision.revision_number.desc())
                .limit(1)
            ).first()
            if rev is not None:
                return rev
        if install.installed_revision_id is not None:
            return session.get(AgentBundleRevision, install.installed_revision_id)
        return None

    @staticmethod
    def _rematerialize_baseline_snapshot(
        *,
        repo_url: str,
        ref: str,
        subdir: str | None,
        last_synced_commit: str | None,
        snapshot_dir: Path,
        key_material: tuple[str, str | None] | None,
    ) -> Path:
        """Re-clone the last-synced baseline tree into ``snapshot_dir``; return its ``workspace/``.

        Recovery path for a lost on-disk baseline: a synced
        ``AgentBundleRevision`` row exists but its ``snapshot_path`` was wiped
        (e.g. an ephemeral ``BUNDLE_STORAGE_DIR`` after a backend redeploy). Clones
        the remote at ``last_synced_commit`` (full history so the pinned commit is
        reachable), validates the tree, and persists it back to the expected
        ``snapshot_dir`` opportunistically so subsequent checks hit disk. Reuses
        the same ``clone_repository_context`` helper the pull / push paths use, so
        the egress / SSRF guard runs on the network call as everywhere else.

        NO DB access — the caller decrypts the SSH key material and releases the
        pooled connection first (pool-safety, mirroring the other read paths).

        Raises :class:`GitBaselineUnavailableError` when the baseline cannot be
        reproduced (no pin recorded, remote unreachable, commit GC'd / rewritten,
        auth failure, malformed tree). The caller MUST NOT collapse back to a
        clean / non-dirty result on this error.
        """
        if not last_synced_commit:
            raise GitBaselineUnavailableError(
                "The last-synced baseline snapshot is missing and no commit is "
                "recorded to rebuild it from. Pull or commit to restore the "
                "baseline."
            )
        # A peer re-materialization (or a pull / push) may have restored the
        # baseline between the caller's stale existence check and now — reuse it
        # and skip a redundant clone.
        workspace = snapshot_dir / "workspace"
        if workspace.exists():
            return workspace

        # Concurrency: compute_dirty / compute_status are SYNC handlers that
        # FastAPI runs in a threadpool, so two dirty checks for the same
        # lost-baseline agent can reach here at once. The per-agent ``_git_locks``
        # pull / push use are ``asyncio.Lock``s and cannot be acquired from this
        # synchronous path, so instead of serializing we make publication atomic:
        # build into a private temp sibling on the SAME filesystem, then swap it
        # into place with ``os.replace`` (``Path.replace``). The destination is
        # only ever created by an atomic rename onto a MISSING dir; a rename onto
        # an already-published (non-empty) dir fails, so a peer that already won
        # is simply kept. This removes the in-place ``rmtree`` + rewrite whose race
        # against a concurrent baseline hash is the exact bug class this fix
        # targets — no reader ever observes a half-written baseline.
        #
        # ``subdir`` is read from the current ``source`` row (fixed at checkout),
        # not from the synced revision — matching how pull / push resolve it.
        tmp_snapshot = snapshot_dir.parent / (
            f".rematerialize-{snapshot_dir.name}-{uuid.uuid4().hex}"
        )
        try:
            with _ssh_key_file(key_material) as key:
                # Full-history clone (``depth=None``) so the pinned commit is
                # reachable. No ``kill_after_timeout``: like the pull / push
                # write-path clones, this relies on the progress-based low-speed
                # guard so a slow-but-healthy large clone is not hard-killed and
                # mis-surfaced as a false 503.
                with clone_repository_context(
                    repo_url,
                    branch=ref,
                    ssh_key_path=key,
                    depth=None,
                ) as (repo_path, repo):
                    repo.git.checkout(last_synced_commit)
                    src = _resolve_subdir(repo_path, subdir)
                    manifest = _read_and_validate_tree(src)
                    _persist_clone_as_snapshot(src, tmp_snapshot, manifest)
            try:
                tmp_snapshot.replace(snapshot_dir)
            except OSError:
                # os.replace only succeeds onto a MISSING or EMPTY destination; it
                # raises ENOTEMPTY onto a non-empty one. Two shapes land here:
                #   1. Peer win — a concurrent re-materialization already published
                #      a complete baseline (snapshot_dir has workspace/). Keep it.
                #   2. Stale partial — snapshot_dir exists but its workspace/ is
                #      gone, yet it still holds manifest.json (the headline trigger
                #      for this whole path), so it is non-empty and the rename
                #      failed. Blindly keeping it would wedge the agent in a
                #      permanent 503; instead move the stale dir aside atomically,
                #      then rename the freshly-built rebuild into the now-missing
                #      slot. Each rename stays atomic (same parent); the only new
                #      window is a brief "dest missing" between them, during which a
                #      concurrent reader simply re-enters re-materialization into
                #      its own private temp — never observing a half-written tree.
                if (snapshot_dir / "workspace").exists():
                    shutil.rmtree(tmp_snapshot, ignore_errors=True)
                else:
                    aside = snapshot_dir.parent / (
                        f".stale-{snapshot_dir.name}-{uuid.uuid4().hex}"
                    )
                    snapshot_dir.replace(aside)
                    tmp_snapshot.replace(snapshot_dir)
                    shutil.rmtree(aside, ignore_errors=True)
        except Exception as exc:  # noqa: BLE001 — any failure here = lost baseline
            shutil.rmtree(tmp_snapshot, ignore_errors=True)
            raise GitBaselineUnavailableError(
                "Failed to re-materialize the last-synced baseline snapshot from "
                f"git (baseline check failed): {exc}"
            ) from exc

        if not workspace.exists():
            raise GitBaselineUnavailableError(
                "Re-materialized baseline is missing its 'workspace/' subtree "
                "(baseline check failed)."
            )
        return workspace

    @staticmethod
    def _prompts_changed(install: Agent, rev: AgentBundleRevision | None) -> bool:
        """Whether the install's DB prompts diverge from a resolved baseline.

        Pure diff against an ALREADY-resolved revision so the callers that also
        need the baseline for the workspace / settings diff can resolve it once
        per request instead of once per check (each resolve is a full-row SELECT
        pulling the revision's ``manifest`` blob). SDK lives on the env and is not
        compared here (see :meth:`_settings_changes`). ``False`` when there is no
        synced revision baseline.
        """
        if rev is None:
            return False
        for field, _label in _PROMPT_FIELDS:
            if (getattr(install, field) or "") != (getattr(rev, field) or ""):
                return True
        return False

    @staticmethod
    def _settings_changes(
        session: Session,
        install: Agent,
        rev: AgentBundleRevision | None,
        env: AgentEnvironment | None = None,
        *,
        sections: tuple[str, ...] = _SETTING_SECTIONS,
        skip_null_baseline_metadata: bool = False,
        stop_early: bool = False,
    ) -> list[dict]:
        """Per-field diff of the non-prompt ``cinna.agent.json`` fields.

        The manifest half of the change check that the workspace digest and
        :meth:`_prompts_changed` do not cover: the ``metadata`` block (description,
        example prompts, status refresh command, feature flags, A2A / SDK-tool
        config), the ``sdk`` block (per-mode engine + model overrides, read off
        the active env) and the ``schedules`` / ``plugin_specs`` lists. Without
        this, editing an agent setting that lives only in the manifest reported
        "no local changes" even though the next commit would rewrite
        ``cinna.agent.json``.

        ``required_credential_specs`` is intentionally out of scope — see the
        note on :data:`_SPEC_FIELDS`.

        Returns ``[{field, change_type, name, section}]`` (``added`` /
        ``modified`` / ``deleted``); empty when there is no synced baseline
        (``rev is None``).

        ``field`` is the human LABEL — it is what the wire contract
        (``GitSettingChange.field``) has always carried, so it is deliberately
        not renamed. ``name`` is the raw attribute name and ``section`` the
        registry it came from (one of :data:`_SETTING_SECTIONS`); together they
        are the stable key :meth:`_pull_blocking_changes` joins on and the
        ``preserve_fields`` set ``keep_local`` passes to
        :meth:`_apply_revision_to_install`. A label is not a usable key: it is
        UI copy and can be reworded.

        ``rev`` is the ALREADY-resolved sync baseline
        (:meth:`_resolve_synced_revision`) so a caller that also needs it for the
        workspace / prompt diff resolves it once per request.

        **Absent sections are never compared.** A baseline manifest that predates
        a block (or a whole key) yields no changes for it — the same
        missing-key-tolerant rule :meth:`InstallService._apply_revision_metadata`
        applies on the restore side, so an old snapshot can't fabricate drift.

        Flags (all pull-guard / hot-path narrowing; the default is the full,
        report-everything comparison the indicator endpoints use):

        - ``sections`` narrows which blocks are compared; the pull guard passes
          :data:`_PULL_OVERWRITTEN_SECTIONS` (see :meth:`_assert_not_dirty`).
        - ``skip_null_baseline_metadata`` additionally skips any metadata field
          whose RAW baseline column is ``None`` — mirroring
          :meth:`InstallService._apply_revision_metadata`'s PER-FIELD
          ``is not None`` guard. Pull-guard only.
        - ``stop_early`` returns as soon as the first change is found. Only valid
          for callers that reduce the result to a bool (:meth:`compute_dirty`);
          the returned list is then partial by design.

        Touches the DB (spec collectors), so callers on the pool-safe read paths
        must call it BEFORE releasing their connection.
        """
        if rev is None:
            return []
        manifest = rev.manifest if isinstance(rev.manifest, dict) else {}
        changes: list[dict] = []

        def _record(
            section: str, field: str, label: str, live: object, baseline: object
        ) -> None:
            change_type = _classify_change(
                _normalize_setting_value(field, live),
                _normalize_setting_value(field, baseline),
            )
            if change_type is not None:
                changes.append(
                    {
                        "field": label,
                        "change_type": change_type,
                        "name": field,
                        "section": section,
                    }
                )

        def _satisfied() -> bool:
            """Whether a ``stop_early`` caller already has its answer."""
            return stop_early and bool(changes)

        if "metadata" in sections and isinstance(manifest.get("metadata"), dict):
            for field, label in _METADATA_FIELDS:
                baseline = getattr(rev, field, None)
                # Per-field missing-value tolerance for the pull guard.
                # ``_apply_revision_metadata`` only assigns when the revision
                # column ``is not None``, so a NULL baseline means a pull leaves
                # the local value ALONE — blocking on it would wedge the user
                # (pull refuses, and push demands a pull first) while protecting
                # nothing. Matched on the RAW column, deliberately NOT on
                # ``change_type == "added"``: a baseline of ``[]`` / ``""``
                # normalizes to ``None`` (so it classifies as "added") yet passes
                # the ``is not None`` check, i.e. the pull DOES overwrite it and
                # the guard must still block.
                if skip_null_baseline_metadata and baseline is None:
                    continue
                _record(
                    "metadata", field, label,
                    getattr(install, field, None), baseline,
                )
            if _satisfied():
                return changes

        # The SDK block is env-derived; with no env there is nothing live to
        # compare against (a commit isn't possible in that state either).
        if "sdk" in sections and env is not None and isinstance(
            manifest.get("sdk"), dict
        ):
            for field, label in _SDK_FIELDS:
                _record(
                    "sdk", field, label,
                    getattr(env, field, None),
                    getattr(rev, field, None),
                )
            if _satisfied():
                return changes

        if "specs" in sections:
            for field, label, collect in _SPEC_FIELDS:
                if field not in manifest:
                    continue
                # Each collector is a real query burst; skip the rest once a
                # bool-only caller already knows the answer.
                if _satisfied():
                    return changes
                try:
                    live_specs = collect(session, install)
                except Exception as exc:  # noqa: BLE001 — see below
                    # Conservative-on-indeterminate, the same rule
                    # ``subdir_changed_between`` applies: a collector that cannot
                    # snapshot the live state (e.g. an undecryptable credential)
                    # must not silently report "clean" — that would grey out the
                    # commit button on a genuinely drifted agent — nor 500 a
                    # polled read. Report it as changed and let the push surface
                    # the real error.
                    logger.warning(
                        "git dirty: could not collect %s for agent %s (%s) — "
                        "reporting it as changed",
                        field, install.id, exc,
                    )
                    # A DB-level failure leaves the transaction poisoned, and
                    # swallowing the error here would only defer the blow-up to
                    # the caller's ``session.commit()`` (PendingRollbackError →
                    # 500 on the very endpoint that must degrade, not fail).
                    # Clear it with the same nested-transaction-aware discipline
                    # ``_mark_source_error`` uses so savepoint-based test
                    # isolation survives. ORM instances are merely expired
                    # afterwards, so the remaining ``getattr``s re-load.
                    GitSourceService._clear_poisoned_transaction(
                        session,
                        context=f"settings diff ({field}) for agent {install.id}",
                    )
                    changes.append(
                        {
                            "field": label,
                            "change_type": "modified",
                            "name": field,
                            "section": "specs",
                        }
                    )
                    continue
                _record(
                    "specs", field, label, live_specs, getattr(rev, field, None)
                )

        return changes

    @staticmethod
    def _pull_blocking_changes(
        session: Session,
        install: Agent,
        rev: AgentBundleRevision | None,
        env: AgentEnvironment | None = None,
    ) -> list[dict]:
        """The changes a pull would OVERWRITE — the pull guard's blocking set.

        Prompts (always, they are rewritten wholesale by
        :meth:`_apply_revision_to_install`) plus the manifest sections a pull
        actually rewrites (:data:`_PULL_OVERWRITTEN_SECTIONS`), narrowed
        per-field by ``skip_null_baseline_metadata`` to mirror
        :meth:`InstallService._apply_revision_metadata`'s ``is not None`` guard.

        Each entry: ``{section, field, label, change_type}`` where ``section`` is
        ``"prompt"`` or one of :data:`_SETTING_SECTIONS`, ``field`` is the RAW
        attribute name (the stable key — also what ``keep_local`` passes as
        ``preserve_fields``) and ``label`` the human string the UI renders.

        **Single source of truth** for both the pull guard
        (:meth:`_assert_not_dirty`) and the ``blocks_pull`` flags on
        :meth:`compute_status`, so the 409 and the preview it explains can never
        disagree — the same rule ``_remote_change_is_relevant`` enforces for the
        update banner vs. the push guard.

        Deliberately narrower than the dirty indicator: schedules, plugin links,
        credential links and env SDK selections survive a pull untouched, so
        blocking on them would only deadlock the user (pull refuses, and a pull
        is exactly what an advanced remote demands first) while protecting
        nothing. They still light up the dirty / commit-preview endpoints with
        ``blocks_pull: false``. Do NOT widen :data:`_PULL_OVERWRITTEN_SECTIONS`.

        There is deliberately NO ``stop_early`` passthrough. ``_settings_changes``
        offers one, but a partial list is unusable here: the guard needs the full
        set for its 409 payload and the preview needs it for the ``blocks_pull``
        join key set. Only a caller that reduces to a bool may narrow, and this
        helper never is one.

        **Caveat on the ``is not None`` mirroring.** ``skip_null_baseline_metadata``
        tests the BASELINE revision's column, while the overwrite is driven by the
        INCOMING one. They are the same revision on the common path, but when the
        baseline is NULL and the incoming carries a value the field is (correctly)
        absent from this set, yet ``_apply_revision_metadata`` still assigns it —
        so ``keep_local`` does not preserve it. The alternative (blocking on a NULL
        baseline) is the documented deadlock this narrowing exists to prevent, so
        the gap is accepted, not fixed here.

        Touches the DB via :meth:`_settings_changes`, so pool-safe read paths
        must call it BEFORE releasing their connection.
        """
        blocking: list[dict] = []
        if rev is None:
            return blocking

        # Prompt half — always blocking: a pull rewrites all four columns.
        for field, label in _PROMPT_FIELDS:
            current = getattr(install, field) or ""
            baseline = getattr(rev, field) or ""
            change_type = (
                None if current == baseline
                else "added" if not baseline
                else "deleted" if not current
                else "modified"
            )
            if change_type is None:
                continue
            blocking.append(
                {
                    "section": "prompt",
                    "field": field,
                    "label": label,
                    "change_type": change_type,
                }
            )

        # Settings half — only the sections a pull rewrites, per-field narrowed.
        for change in GitSourceService._settings_changes(
            session,
            install,
            rev,
            env,
            sections=_PULL_OVERWRITTEN_SECTIONS,
            skip_null_baseline_metadata=True,
        ):
            blocking.append(
                {
                    "section": change["section"],
                    "field": change["name"],
                    "label": change["field"],
                    "change_type": change["change_type"],
                }
            )
        return blocking

    @staticmethod
    def _assert_not_dirty(
        session: Session,
        source: AgentGitSource,
        install: Agent,
        env: AgentEnvironment | None = None,
    ) -> None:
        """Block pull when local state a pull would OVERWRITE has diverged.

        Thin wrapper over :meth:`_pull_blocking_changes` (the shared blocking-set
        computation — see its docstring for what is in scope and why). Raises
        :class:`GitSourceLocalChangesError` (→ a structured, recoverable 409)
        carrying the blocking list, so the UI can offer ``conflict_resolution``
        instead of a dead-end toast.
        """
        blocking = GitSourceService._pull_blocking_changes(
            session,
            install,
            GitSourceService._resolve_synced_revision(session, source, install),
            env,
        )
        if blocking:
            raise GitSourceLocalChangesError(_PULL_LOCAL_CHANGES_MESSAGE, blocking)

    @staticmethod
    async def _apply_revision_to_install(
        session: Session,
        install: Agent,
        revision: AgentBundleRevision,
        *,
        preserve_fields: set[str] | None = None,
    ) -> None:
        """Apply a revision's snapshot onto the install's active env.

        Mirrors :meth:`InstallService.apply_update` (stop → ``replace_bundle_content``
        → reset prompt-sync baselines → DB prompts from manifest → restart) but
        targets a specific git-imported revision and never touches the catalog's
        ``bundle.latest_revision_id`` / install-notify machinery.

        ``preserve_fields`` (the ``keep_local`` resolution) names RAW attributes
        — prompt columns and/or ``metadata`` fields — to leave at their current
        local value instead of restoring them from ``revision``. It narrows the
        two DB-assignment groups ONLY; the workspace is still replaced wholesale
        by ``replace_bundle_content`` (one tree, one baseline — per-file
        resolution is out of scope), and the prompt-sync baseline reset below
        still runs, which is what makes a preserved DB value actually win over
        the file the snapshot just wrote.
        """
        preserve = preserve_fields or set()
        from app.services.environments.environment_service import EnvironmentService
        from app.services.environments.workspace_copy import replace_bundle_content

        env = (
            session.get(AgentEnvironment, install.active_environment_id)
            if install.active_environment_id else None
        )
        lifecycle = EnvironmentService.get_lifecycle_manager()
        was_running = env is not None and env.status == "running"

        if was_running:
            try:
                await lifecycle.stop_environment(session, env)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "git pull: failed to stop env %s before update: %s — continuing",
                    env.id, exc,
                )

        if env is not None:
            replace_bundle_content(Path(revision.snapshot_path), env.id)
            # The snapshot just overwrote the env prompt files; reset the
            # prompt-sync baselines so the next start treats the DB as
            # authoritative and does not pull the fresh env files back.
            env.workflow_prompt_synced_hash = None
            env.entrypoint_prompt_synced_hash = None
            env.refiner_prompt_synced_hash = None
            session.add(env)

        for field, _label in _PROMPT_FIELDS:
            if field in preserve:
                continue
            setattr(install, field, getattr(revision, field))
        # Overwrite the agent-row definitional metadata from the pulled revision
        # (publisher-authoritative), only for fields the revision carries — same
        # missing-key-tolerant rule as catalog apply-update.
        InstallService._apply_revision_metadata(
            install, revision, skip_fields=preserve or None
        )
        prompt_now = datetime.now(UTC)
        install.workflow_prompt_updated_at = prompt_now
        install.entrypoint_prompt_updated_at = prompt_now
        install.refiner_prompt_updated_at = prompt_now
        install.installed_revision_id = revision.id
        install.last_sync_at = prompt_now
        install.last_update_status = "synced"
        session.add(install)
        session.commit()
        session.refresh(install)

        if env is not None and was_running:
            try:
                await lifecycle.start_environment(session, env, install)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "git pull: failed to restart env %s after update: %s",
                    env.id, exc,
                )


# ── Module-level filesystem / git helpers ───────────────────────────────


def _render_setting_text(field: str, value: object) -> str:
    """Render one prompt / setting value as the text side of a diff.

    Strings (prompts, description, a shell command) are shown verbatim — JSON
    quoting a multi-line prompt would make every line of the diff unreadable.
    Everything else is pretty-printed JSON with sorted keys so the diff is
    stable across re-serializations rather than churning on dict ordering.

    Both sides go through :func:`_normalize_setting_value` FIRST, the same
    normalization :meth:`GitSourceService._settings_changes` classifies on — so
    a row reported as changed always renders a non-empty diff, and an "unset"
    shape (``None`` / ``""`` / ``[]`` / ``{}``) never diffs against its twin.
    """
    normalized = _normalize_setting_value(field, value)
    if normalized is None:
        return ""
    if isinstance(normalized, str):
        return normalized
    return json.dumps(normalized, indent=2, sort_keys=True, default=str)


def _unified_diff(baseline_text: str, live_text: str, display: str) -> dict:
    """Build the ``git diff`` style body for two already-resolved sides.

    Returns the ``{diff, binary, truncated}`` slice of the response. ``diff`` is
    ``""`` when the sides are equal. ``a/`` is the last synced revision and
    ``b/`` the live agent, matching git's own convention (baseline → working
    copy), so a ``+`` line is always "what this agent has now".
    """
    truncated = False
    if len(baseline_text) > _DIFF_MAX_BYTES:
        baseline_text = baseline_text[:_DIFF_MAX_BYTES]
        truncated = True
    if len(live_text) > _DIFF_MAX_BYTES:
        live_text = live_text[:_DIFF_MAX_BYTES]
        truncated = True
    if baseline_text == live_text:
        return {"diff": "", "binary": False, "truncated": truncated}

    lines = list(
        difflib.unified_diff(
            baseline_text.splitlines(),
            live_text.splitlines(),
            fromfile=f"a/{display}",
            tofile=f"b/{display}",
            lineterm="",
        )
    )
    if len(lines) > _DIFF_MAX_LINES:
        lines = lines[:_DIFF_MAX_LINES]
        truncated = True
    return {"diff": "\n".join(lines), "binary": False, "truncated": truncated}


def _resolve_diff_file_key(key: str) -> str:
    """Validate a caller-supplied workspace-relative path for the diff read.

    This is untrusted input addressing the filesystem, so it is allowlisted
    rather than sanitized. Enforced, in order: relative-only, no traversal, and
    the SAME denylist the capture walk applies — first segment must be
    bundle-owned (so ``credentials/``, ``app-data/``, ``logs/`` are unreachable)
    and no segment may be a nested cache artifact. Reusing
    ``workspace_classification``'s helpers rather than re-listing the rules is
    what keeps this endpoint from drifting away from what a commit captures.

    Containment against symlink escape is enforced separately, at read time in
    :func:`_read_diff_side`, where the resolved path is known.
    """
    rel = (key or "").strip().replace("\\", "/")
    if not rel or rel.startswith("/"):
        raise GitSourceValidationError("File path must be workspace-relative.")
    parts = [p for p in rel.split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        raise GitSourceValidationError(f"Invalid file path {key!r}.")
    if not is_bundle_owned_toplevel(parts[0]):
        raise GitSourceValidationError(
            f"{parts[0]!r} is not a versioned workspace folder."
        )
    if any(is_nested_excluded(p) for p in parts):
        raise GitSourceValidationError(f"{key!r} is not a versioned file.")
    return "/".join(parts)


def _read_diff_side(root: Path | None, rel: str) -> tuple[str | None, bool]:
    """Read one side of a file diff under ``root`` → ``(text, is_binary)``.

    ``(None, False)`` means the file is absent on that side — which is what
    makes a change classify as added or deleted rather than modified, so it must
    stay distinct from an empty file (``("", False)``).

    **Containment is enforced here, on the fully resolved path**, not on the
    path string: ``_resolve_diff_file_key`` can only reject literal traversal,
    but a symlinked intermediate DIRECTORY (``scripts/x -> /etc``) escapes with
    no ``..`` anywhere and with the final component a perfectly ordinary file.
    Resolving both sides and requiring containment is the check that actually
    holds; the per-component denylist above it is defense in depth, not the
    boundary. A symlink is refused outright either way — the capture walk drops
    symlinks, so one here would never be committed.

    Undecodable bytes report as binary rather than raising.
    """
    if root is None:
        return None, False
    try:
        root_resolved = root.resolve()
        path = (root / rel).resolve()
        if not path.is_relative_to(root_resolved):
            logger.warning(
                "git diff: refusing path %r — resolves outside %s", rel, root
            )
            return None, False
        if (root / rel).is_symlink() or not path.is_file():
            return None, False
        raw = path.read_bytes()[: _DIFF_MAX_BYTES + 1]
    except OSError:
        return None, False
    try:
        return raw.decode("utf-8"), False
    except UnicodeDecodeError:
        return None, True


def _read_ssh_key_material(
    session: Session, ssh_key_id: uuid.UUID | None, owner_id: uuid.UUID
) -> tuple[str, str | None] | None:
    """Decrypt the SSH key material (private key + passphrase) under the DB scope.

    The DB-touching half of :func:`_resolve_ssh_key`: it decrypts the key
    host-side via the ownership-checked
    :meth:`SSHKeyService.get_decrypted_private_key` and returns the material
    **in memory**. The status-read paths call this while still holding a pooled
    DB connection, then release the connection BEFORE writing the temp key file
    and running the (blocking, network) git call via :func:`_ssh_key_file` — so
    no remote git I/O ever runs with a connection / transaction checked out.

    Returns ``None`` when no key is configured (public repo). Raises
    :class:`GitSourceValidationError` when the key is missing / not owned.
    """
    if ssh_key_id is None:
        return None
    result = SSHKeyService.get_decrypted_private_key(session, ssh_key_id, owner_id)
    if result is None:
        raise GitSourceValidationError(
            "SSH key not found or not owned by you."
        )
    return result


@contextmanager
def _ssh_key_file(
    key_material: tuple[str, str | None] | None,
) -> Iterator[str | None]:
    """Write decrypted key material to a chmod-600 temp file for the git call.

    The non-DB half of :func:`_resolve_ssh_key`: it takes the in-memory material
    from :func:`_read_ssh_key_material` and yields a chmod-600 temp key path
    (deleted in ``finally``), or ``None`` for a public repo. The temp-key
    lifetime wraps ONLY the git network call, never a DB query — letting the
    status reads release their pooled connection before the blocking git I/O.
    The key material never reaches the container.
    """
    if key_material is None:
        yield None
        return
    private_key, passphrase = key_material
    with create_ssh_key_file(private_key, passphrase) as key_path:
        yield key_path


@contextmanager
def _resolve_ssh_key(
    session: Session, ssh_key_id: uuid.UUID | None, owner_id: uuid.UUID
) -> Iterator[str | None]:
    """Yield a chmod-600 temp SSH key path (deleted in ``finally``), or ``None``.

    The private key is decrypted host-side via the ownership-checked
    :meth:`SSHKeyService.get_decrypted_private_key` and never copied into the
    container. Convenience wrapper over :func:`_read_ssh_key_material` +
    :func:`_ssh_key_file` for the write paths (checkout / connect / pull / push),
    which legitimately interleave DB and git work inside one transaction. The
    status-read paths instead call the two halves separately so they can release
    the DB connection between the decrypt and the git call.
    """
    key_material = _read_ssh_key_material(session, ssh_key_id, owner_id)
    with _ssh_key_file(key_material) as key_path:
        yield key_path


def _resolve_subdir(repo_path: str, subdir: str | None) -> Path:
    """Resolve ``<repo>/<subdir>``, rejecting any path that escapes the clone."""
    base = Path(repo_path).resolve()
    if not subdir:
        return base
    target = (base / subdir).resolve()
    if target != base and base not in target.parents:
        raise GitSourceValidationError(
            f"subdir '{subdir}' escapes the repository root."
        )
    return target


def _read_and_validate_tree(src: Path) -> dict:
    """Validate ``src`` is a v2 snapshot and return its parsed manifest.

    Raises :class:`GitSourceValidationError` for a non-v2 layout and
    :class:`RevisionFormatError` for a malformed / unsupported manifest.
    """
    if snapshot_layout(src) != "v2_workspace":
        raise GitSourceValidationError(
            "Not a Cinna agent repo: expected a 'workspace/' directory and a "
            f"'{GIT_MANIFEST_FILENAME}' manifest at the checkout path."
        )
    return RevisionFormat.read_manifest(src)


def _require_bundle_id(manifest: dict) -> str:
    bundle_id = manifest.get("bundle_id")
    if not bundle_id or not isinstance(bundle_id, str):
        raise GitSourceValidationError(
            f"{GIT_MANIFEST_FILENAME} is missing a 'bundle_id'."
        )
    return bundle_id


def _assert_no_oversized_files(workspace_root: Path) -> None:
    """Reject any individual file under ``workspace_root`` over the size cap."""
    max_bytes = settings.GIT_SOURCE_MAX_FILE_BYTES
    if not workspace_root.exists():
        return
    for f in workspace_root.rglob("*"):
        if f.is_symlink() or not f.is_file():
            continue
        try:
            size = f.stat().st_size
        except OSError:
            continue
        if size > max_bytes:
            rel = f.relative_to(workspace_root)
            raise GitSourceValidationError(
                f"File 'workspace/{rel}' ({size} bytes) exceeds the maximum "
                f"allowed size of {max_bytes} bytes."
            )


def _persist_clone_as_snapshot(src: Path, snapshot_dir: Path, manifest: dict) -> str:
    """Copy a cloned tree's ``workspace/`` into bundle storage + write the manifest.

    Reuses the publish denylist (``iter_bundle_toplevel``) and symlink guards
    (``safe_copytree`` / ``_copy_plugins_tree``) so credentials / app-data /
    runtime state in an untrusted repo never reach the install. Mutates
    ``manifest`` in place with the recomputed ``content_hash`` and returns the
    bare hex digest.
    """
    if snapshot_dir.exists():
        shutil.rmtree(snapshot_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
    dest_workspace = snapshot_dir / "workspace"
    dest_workspace.mkdir(parents=True, exist_ok=True, mode=0o755)

    clone_workspace = src / "workspace"
    for entry in iter_bundle_toplevel(clone_workspace):
        target = dest_workspace / entry.name
        if entry.name == PLUGINS_DIRNAME and entry.is_dir():
            target.mkdir(parents=True, exist_ok=True, mode=0o755)
            PublishService._copy_plugins_tree(entry, target)
        elif entry.is_dir():
            safe_copytree(entry, target)
        else:
            shutil.copy2(entry, target)

    content_hash = PublishService._hash_tree_with_manifest(snapshot_dir, manifest)
    manifest["content_hash"] = f"sha256:{content_hash}"
    (snapshot_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return content_hash
