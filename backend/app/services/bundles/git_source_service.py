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
import hashlib
import json
import logging
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

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
# Single source of truth shared by the dirty check (:meth:`_prompts_dirty`) and
# the commit-status preview (:meth:`compute_status`) so they cannot disagree.
_PROMPT_FIELDS: tuple[tuple[str, str], ...] = (
    ("workflow_prompt", "Workflow prompt"),
    ("entrypoint_prompt", "Entrypoint prompt"),
    ("refiner_prompt", "Refiner prompt"),
    ("router_trigger_prompt", "Router trigger prompt"),
)


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
    def _compute_update_available(
        session: Session, source: AgentGitSource
    ) -> tuple[bool, str]:
        """Resolve ``(update_available, remote_head_sha)`` for one git source.

        Single source of truth shared by :meth:`get_source` (best-effort) and
        :meth:`check_updates` (strict) so the banner can never disagree between
        the two endpoints — mirroring how :meth:`_prompts_dirty` is shared.

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

        The SSH key stays resolved for both the ls-remote and the subdir clone so
        a private repo is authenticated for both calls.
        """
        with _resolve_ssh_key(session, source.ssh_key_id, source.owner_id) as key:
            remote_sha = ls_remote_head(source.repo_url, source.ref, key)
            if remote_sha == source.last_synced_commit:
                return False, remote_sha

            subdir = (source.subdir or "").strip("/")
            if not subdir or not source.last_synced_commit:
                # Repo root (every commit touches it) or no baseline to scope
                # against — the cheap HEAD advance is the verdict.
                return True, remote_sha

            # HEAD advanced and a subdir is configured: only a real update when
            # the subdir tree actually changed beyond the synced commit.
            changed = subdir_changed_between(
                repo_url=source.repo_url,
                ref=source.ref,
                subdir=subdir,
                base_commit=source.last_synced_commit,
                ssh_key_path=key,
            )
            return changed, remote_sha

    @staticmethod
    def get_source(
        session: Session, agent_id: uuid.UUID, owner: User
    ) -> tuple[AgentGitSource, bool]:
        """Return ``(source, update_available)`` for the owner-resolved agent.

        ``update_available`` is computed best-effort; any network / auth / clone
        failure leaves it ``False`` (this is a read endpoint — it never mutates
        the source or raises on a transient remote error). When the agent lives
        in a ``subdir`` the check is subdir-scoped (see
        :meth:`_compute_update_available`).
        """
        source = GitSourceService._resolve_source_owned(session, agent_id, owner)
        update_available = False
        try:
            update_available, _ = GitSourceService._compute_update_available(
                session, source
            )
        except Exception as exc:  # noqa: BLE001 — best-effort, never fail the read
            logger.info(
                "git get_source: update check failed for agent %s: %s",
                agent_id, exc,
            )
        return source, update_available

    @staticmethod
    def check_updates(
        session: Session, agent_id: uuid.UUID, owner: User
    ) -> dict:
        """Strict ``update_available`` check (surfaces auth / network errors).

        Cheap ``ls-remote`` HEAD vs ``last_synced_commit``, then subdir-scoped via
        :meth:`_compute_update_available` so a commit to an unrelated folder of the
        same repo does not report an update.
        """
        source = GitSourceService._resolve_source_owned(session, agent_id, owner)
        update_available, remote_sha = GitSourceService._compute_update_available(
            session, source
        )
        return {
            "update_available": update_available,
            "remote_commit": remote_sha,
            "last_synced_commit": source.last_synced_commit,
        }

    # ── Commit history ───────────────────────────────────────────────

    @staticmethod
    def list_commits(
        session: Session, agent_id: uuid.UUID, owner: User, *, limit: int = 50
    ) -> list[dict]:
        """Return up to ``limit`` commits touching the source's subdir, newest first.

        Strict — surfaces auth / network errors (like ``check_updates``).
        Owner-resolved (404 for a missing source / non-owner).
        """
        source = GitSourceService._resolve_source_owned(session, agent_id, owner)
        with _resolve_ssh_key(session, source.ssh_key_id, source.owner_id) as key:
            commits = git_log_subdir(
                repo_url=source.repo_url,
                ref=source.ref,
                subdir=source.subdir,
                ssh_key_path=key,
                max_count=limit,
            )
        # Attach a per-commit browser URL when the host supports it (GitHub
        # today); ``None`` otherwise so the UI renders the SHA as plain text.
        for commit in commits:
            commit["commit_url"] = build_web_commit_url(
                source.repo_url, commit.get("sha", "")
            )
        return commits

    # ── Dirty check ──────────────────────────────────────────────────

    @staticmethod
    def compute_dirty(
        session: Session, agent_id: uuid.UUID, owner: User
    ) -> dict:
        """Compare the live workspace + prompts against the last synced revision.

        Read-only — NEVER pushes. Returns ``{dirty, prompts_dirty,
        workspace_dirty, has_env, last_synced_commit}``. Best-effort: with no env
        or no synced revision the relevant flags stay ``False``. Owner-resolved
        (404 for a missing source / non-owner).
        """
        source = GitSourceService._resolve_source_owned(session, agent_id, owner)
        install = session.get(Agent, agent_id)
        if install is None:
            raise GitSourceNotFoundError("Agent not found")

        prompts_dirty = GitSourceService._prompts_dirty(session, source, install)

        workspace_dirty = False
        has_env = False
        env = (
            session.get(AgentEnvironment, install.active_environment_id)
            if install.active_environment_id else None
        )
        if env is not None:
            env_workspace_root = Path(settings.ENV_INSTANCES_DIR) / str(env.id)
            workspace_dir = env_workspace_root / WORKSPACE_ROOT_REL
            if workspace_dir.exists() and workspace_dir.is_dir():
                has_env = True
                synced_rev = GitSourceService._resolve_synced_revision(
                    session, source, install
                )
                if synced_rev is not None and synced_rev.snapshot_path:
                    synced_workspace = Path(synced_rev.snapshot_path) / "workspace"
                    if synced_workspace.exists():
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
            "dirty": prompts_dirty or workspace_dirty,
            "prompts_dirty": prompts_dirty,
            "workspace_dirty": workspace_dirty,
            "has_env": has_env,
            "last_synced_commit": source.last_synced_commit,
        }

    @staticmethod
    def compute_status(
        session: Session, agent_id: uuid.UUID, owner: User
    ) -> dict:
        """File/prompt-level preview of what a commit would capture.

        The detailed sibling of :meth:`compute_dirty`: instead of booleans it
        returns the actual changes — a per-prompt list and a per-file list of
        ``{path, change_type}`` (``added`` / ``modified`` / ``deleted``) — so the
        UI can render a ``git status`` style preview before the user commits.

        The workspace side compares the SAME post-denylist capture a push would
        produce (via :meth:`PublishService._snapshot_workspace_tree`) against the
        last synced revision's ``workspace/`` snapshot, so the preview matches the
        commit exactly (e.g. ``__pycache__`` never appears). Read-only — never
        pushes. Best-effort: with no env or no synced revision the relevant list
        stays empty. Owner-resolved (404 for a missing source / non-owner).
        """
        source = GitSourceService._resolve_source_owned(session, agent_id, owner)
        install = session.get(Agent, agent_id)
        if install is None:
            raise GitSourceNotFoundError("Agent not found")

        synced_rev = GitSourceService._resolve_synced_revision(
            session, source, install
        )

        # ── Prompt changes ──────────────────────────────────────────
        prompt_changes: list[dict] = []
        if synced_rev is not None:
            for field, label in _PROMPT_FIELDS:
                current = getattr(install, field) or ""
                baseline = getattr(synced_rev, field) or ""
                if current == baseline:
                    continue
                if not baseline:
                    change_type = "added"
                elif not current:
                    change_type = "deleted"
                else:
                    change_type = "modified"
                prompt_changes.append({"field": label, "change_type": change_type})

        # ── Workspace file changes ──────────────────────────────────
        file_changes: list[dict] = []
        has_env = False
        env = (
            session.get(AgentEnvironment, install.active_environment_id)
            if install.active_environment_id else None
        )
        if env is not None:
            env_workspace_root = Path(settings.ENV_INSTANCES_DIR) / str(env.id)
            workspace_dir = env_workspace_root / WORKSPACE_ROOT_REL
            if workspace_dir.exists() and workspace_dir.is_dir():
                has_env = True
                synced_workspace = (
                    Path(synced_rev.snapshot_path) / "workspace"
                    if synced_rev is not None and synced_rev.snapshot_path
                    else None
                )
                # Mirror compute_dirty: with no usable baseline snapshot on disk
                # (no synced revision, no snapshot_path, or the snapshot dir is
                # gone) skip the workspace diff entirely rather than fabricating
                # an all-"added" preview against an empty baseline.
                if synced_workspace is not None and synced_workspace.exists():
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
            "dirty": bool(prompt_changes or file_changes),
            "has_env": has_env,
            "last_synced_commit": source.last_synced_commit,
            "prompt_changes": prompt_changes,
            "file_changes": file_changes,
        }

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
        *, session: Session, agent_id: uuid.UUID, owner: User
    ) -> Agent:
        """Pull the latest remote revision onto the install. Per-agent locked."""
        async with _lock_for(str(agent_id)):
            return await GitSourceService._pull_locked(session, agent_id, owner)

    @staticmethod
    async def _pull_locked(
        session: Session, agent_id: uuid.UUID, owner: User
    ) -> Agent:
        source = GitSourceService._resolve_source_owned(session, agent_id, owner)
        # On any failure below, stamp ERROR + last_error on the source so the UI
        # can surface it (N2), then re-raise the original error unchanged.
        try:
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

                # Fail-loud dirty guard (manifest/DB side): local prompt edits
                # since the last sync block the pull (the file side is protected
                # by the replace_bundle_content denylist). 3-way reconcile is a
                # documented follow-on; for now we fail loud. Runs only after the
                # no-op short-circuit so an unchanged remote never trips it.
                GitSourceService._assert_not_dirty(session, source, install)

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
                    revision = GitSourceService._persist_revision(
                        session,
                        bundle=bundle,
                        src=src,
                        manifest=manifest,
                        published_by_user_id=source.owner_id,
                    )

            # Apply the new revision onto the live env (reuses replace_bundle_content).
            await GitSourceService._apply_revision_to_install(
                session, install, revision
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
            with _resolve_ssh_key(session, source.ssh_key_id, source.owner_id) as key:
                # ff precheck (do not clone/commit if the remote already advanced).
                remote_sha = ls_remote_head(source.repo_url, source.ref, key)
                if source.last_synced_commit and remote_sha != source.last_synced_commit:
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
    def _mark_source_error(
        session: Session, source_id: uuid.UUID, exc: Exception
    ) -> None:
        """Stamp ``status=ERROR`` + ``last_error`` on a git source (N2).

        Called from the pull/push failure path so the UI can surface why the
        last sync failed. Rolls back any poisoned transaction first, re-fetches
        the row, and swallows its own errors so it never masks (or replaces) the
        original exception the caller is about to re-raise.

        Implementation note: SQLAlchemy 2.0's ``session.rollback()`` always
        rolls back to the root transaction (``_to_root=True``), which in the
        test framework (savepoint-based isolation) destroys all previously
        committed test data.  We use ``get_nested_transaction().rollback()``
        when inside a savepoint so only the current (likely empty) savepoint
        is rolled back — this issues ``ROLLBACK TO SAVEPOINT`` rather than a
        full ``ROLLBACK``, preserving the outer transaction's committed rows.
        In production there are no active nested transactions so the fallback
        ``session.rollback()`` is used unchanged.
        """
        # Step 1: best-effort rollback of any poisoned transaction. This must
        # NEVER throw out of its own block — a stale/inactive savepoint object
        # (already released by a prior commit) would otherwise raise and skip
        # the error stamp below. The common genuine-failure case (a non-DB
        # clone/egress failure) leaves the session clean, so this is a no-op.
        try:
            nested = session.get_nested_transaction()
            if nested is not None and nested.is_active:
                nested.rollback()
            elif nested is None:
                # No active savepoint (the production case): only a full
                # rollback can clear a poisoned transaction. Guarded so a
                # stale/inactive state is a harmless no-op.
                session.rollback()
        except Exception as rb_exc:  # noqa: BLE001 — rollback must never block the stamp
            logger.debug(
                "git sync: pre-stamp rollback skipped for source %s: %s",
                source_id, rb_exc,
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
        rev_number = (
            GitSourceService._next_revision_number(session, bundle.id)
            if bundle else 1
        )
        cred_specs = PublishService._collect_credential_specs(session, install)
        schedule_specs = PublishService._collect_schedule_specs(session, install)
        plugin_specs = PublishService._collect_plugin_specs(session, install)
        manifest = RevisionFormat.build_manifest(
            install=install,
            env=env,
            cred_specs=cred_specs,
            schedule_specs=schedule_specs,
            plugin_specs=plugin_specs,
            revision_number=rev_number,
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
        """
        if source.bundle_uuid is not None:
            rev = session.exec(
                select(AgentBundleRevision)
                .where(AgentBundleRevision.bundle_id == source.bundle_uuid)
                .order_by(AgentBundleRevision.revision_number.desc())
            ).first()
            if rev is not None:
                return rev
        if install.installed_revision_id is not None:
            return session.get(AgentBundleRevision, install.installed_revision_id)
        return None

    @staticmethod
    def _prompts_dirty(
        session: Session, source: AgentGitSource, install: Agent
    ) -> bool:
        """Whether the install's DB prompts diverge from the last synced revision.

        Compares the four prompt fields against the SAME baseline the workspace
        digest uses — :meth:`_resolve_synced_revision` (the latest revision on the
        source's bundle, which connect / push / pull / checkout all append to).
        Using ``installed_revision_id`` here instead would disagree with the
        workspace check: it is never advanced by connect or push, so a connected
        agent (``installed_revision_id is None``) would never report a prompt edit
        and a checkout-then-push agent would report a false positive after a
        push. SDK lives on the env and is not compared (env reconfigure is out of
        scope). Returns ``False`` when there is no synced revision baseline.
        Single source of truth shared by the pull guard (:meth:`_assert_not_dirty`)
        and the dirty endpoint (:meth:`compute_dirty`) so they cannot disagree.
        """
        rev = GitSourceService._resolve_synced_revision(session, source, install)
        if rev is None:
            return False
        for field, _label in _PROMPT_FIELDS:
            if (getattr(install, field) or "") != (getattr(rev, field) or ""):
                return True
        return False

    @staticmethod
    def _assert_not_dirty(
        session: Session, source: AgentGitSource, install: Agent
    ) -> None:
        """Block pull when the install's prompts diverge from the last revision.

        Thin raising wrapper over :meth:`_prompts_dirty`. Raises
        :class:`GitSourceConflictError` (→ 409) when the prompts are dirty.
        """
        if GitSourceService._prompts_dirty(session, source, install):
            raise GitSourceConflictError(
                "Local changes detected (prompts differ from the last "
                "synced revision). Push or discard your local changes "
                "before pulling."
            )

    @staticmethod
    async def _apply_revision_to_install(
        session: Session, install: Agent, revision: AgentBundleRevision
    ) -> None:
        """Apply a revision's snapshot onto the install's active env.

        Mirrors :meth:`InstallService.apply_update` (stop → ``replace_bundle_content``
        → reset prompt-sync baselines → DB prompts from manifest → restart) but
        targets a specific git-imported revision and never touches the catalog's
        ``bundle.latest_revision_id`` / install-notify machinery.
        """
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

        install.workflow_prompt = revision.workflow_prompt
        install.entrypoint_prompt = revision.entrypoint_prompt
        install.refiner_prompt = revision.refiner_prompt
        install.router_trigger_prompt = revision.router_trigger_prompt
        # Overwrite the agent-row definitional metadata from the pulled revision
        # (publisher-authoritative), only for fields the revision carries — same
        # missing-key-tolerant rule as catalog apply-update.
        InstallService._apply_revision_metadata(install, revision)
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


@contextmanager
def _resolve_ssh_key(
    session: Session, ssh_key_id: uuid.UUID | None, owner_id: uuid.UUID
) -> Iterator[str | None]:
    """Yield a chmod-600 temp SSH key path (deleted in ``finally``), or ``None``.

    The private key is decrypted host-side via the ownership-checked
    :meth:`SSHKeyService.get_decrypted_private_key` and never copied into the
    container.
    """
    if ssh_key_id is None:
        yield None
        return
    result = SSHKeyService.get_decrypted_private_key(session, ssh_key_id, owner_id)
    if result is None:
        raise GitSourceValidationError(
            "SSH key not found or not owned by you."
        )
    private_key, passphrase = result
    with create_ssh_key_file(private_key, passphrase) as key_path:
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
