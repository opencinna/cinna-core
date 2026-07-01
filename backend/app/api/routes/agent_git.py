"""Git-backed agent versioning — checkout / pull / push routes.

Thin controllers over :class:`~app.services.bundles.git_source_service.GitSourceService`.
Git is an external transport over the platform's existing ``AgentBundleRevision``
SSOT, so these endpoints reduce to install / apply-update / publish operations
with git as the wire.

DI / authorization:

* ``POST /agents/checkout``, ``POST /agents/{id}/git/pull``,
  ``POST /agents/{id}/git/push`` are **developer-gated**
  (``dependencies=[Depends(require_developer)]``) — they mutate / create installs.
* ``GET /agents/{id}/git`` and ``GET /agents/{id}/git/check-updates`` are
  owner-resolved read endpoints (the service enforces per-agent ownership and
  returns 404 for non-owners — no existence leak).

Error mapping (the service never raises ``HTTPException``):

* ``EgressBlockedError`` → 400        ``GitNonFastForwardError`` → 409
* ``GitSourceConflictError`` → 409    ``GitSourceValidationError`` → 400
* ``GitSourceNotFoundError`` → 404    ``RevisionFormatError`` → 422
* ``GitAuthenticationError`` → 401    ``GitConnectionError`` → 400
* ``GitBaselineUnavailableError`` → 503 (lost baseline snapshot, rebuild failed)
* other ``GitOperationError`` → 400
"""
import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel

from app.api.deps import CurrentUser, SessionDep, require_developer
from app.models import Message
from app.models.agents.agent import AgentPublic
from app.models.bundles.agent_git_source import (
    AgentGitSourcePublic,
    GitSyncDirection,
)
from app.services.agents.agent_service import AgentService
from app.services.bundles.git_source_service import (
    GitBaselineUnavailableError,
    GitSourceConflictError,
    GitSourceExistingAgentError,
    GitSourceNotFoundError,
    GitSourceService,
    GitSourceValidationError,
)
from app.services.bundles.revision_format import RevisionFormatError
from app.services.common.egress_guard import EgressBlockedError
from app.services.knowledge.git_operations import (
    GitAuthenticationError,
    GitConnectionError,
    GitNonFastForwardError,
    GitOperationError,
    build_web_history_url,
    build_web_tree_url,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agent-git"])


# ── Request / response models ───────────────────────────────────────────


class AgentCheckoutRequest(SQLModel):
    """Body of ``POST /agents/checkout``."""

    repo_url: str
    subdir: str | None = None
    ref: str = "main"
    ssh_key_id: uuid.UUID | None = None
    sync_direction: str = GitSyncDirection.BIDIRECTIONAL
    name_override: str | None = None


class AgentCheckoutResponse(SQLModel):
    """Combined response for checkout: the created install + its git source."""

    agent: AgentPublic
    git_source: AgentGitSourcePublic


class GitPushRequest(SQLModel):
    """Body of ``POST /agents/{agent_id}/git/push``."""

    commit_message: str
    version: str | None = None
    # Opt-in: also cut a parallel catalog bundle revision so internal installs
    # stay in lockstep with the git mirror. Default off — see route docstring.
    also_publish_bundle: bool = False


class GitUpdateStatus(SQLModel):
    """Response of ``GET /agents/{agent_id}/git/check-updates``."""

    update_available: bool
    remote_commit: str | None = None
    last_synced_commit: str | None = None


class AgentGitConnectRequest(SQLModel):
    """Body of ``POST /agents/{agent_id}/git/connect``."""

    repo_url: str
    subdir: str | None = None
    ref: str = "main"
    ssh_key_id: uuid.UUID | None = None
    sync_direction: str = GitSyncDirection.BIDIRECTIONAL
    commit_message: str = "Initial export from Cinna"
    # When the target subdir already holds an agent, the first connect returns a
    # recoverable 409 (code "existing_agent_folder"). Re-send with this True to
    # adopt that remote folder as the synced baseline instead of failing.
    adopt_existing: bool = False


class GitCommit(SQLModel):
    """One commit in the source's subdir history."""

    sha: str
    short_sha: str
    author_name: str
    author_email: str
    date: datetime
    message: str
    # Browser URL to this commit on the host, when supported (GitHub today);
    # None otherwise so the UI renders the SHA as plain text.
    commit_url: str | None = None


class GitCommitList(SQLModel):
    """Response of ``GET /agents/{agent_id}/git/commits``."""

    commits: list[GitCommit]


class GitDirtyStatus(SQLModel):
    """Response of ``GET /agents/{agent_id}/git/dirty``."""

    dirty: bool
    prompts_dirty: bool
    workspace_dirty: bool
    has_env: bool
    last_synced_commit: str | None = None


class GitPromptChange(SQLModel):
    """One changed prompt field in the commit preview."""

    field: str
    change_type: str  # "added" | "modified" | "deleted"


class GitFileChange(SQLModel):
    """One changed workspace file in the commit preview."""

    path: str
    change_type: str  # "added" | "modified" | "deleted"


class GitStatus(SQLModel):
    """Response of ``GET /agents/{agent_id}/git/status`` — commit preview."""

    dirty: bool
    has_env: bool
    last_synced_commit: str | None = None
    prompt_changes: list[GitPromptChange] = []
    file_changes: list[GitFileChange] = []


# ── Error mapping ────────────────────────────────────────────────────────


def _map_git_error(exc: Exception) -> HTTPException:
    """Translate a service / git exception into the right HTTP status."""
    if isinstance(exc, GitSourceNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, GitBaselineUnavailableError):
        # Server-side storage-integrity failure: the last-synced baseline
        # snapshot was lost and could not be rebuilt from git. A 5xx (not a
        # false 200 "no changes") lets the UI show an explicit "baseline check
        # failed" state and keep the commit action blocked.
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, (GitSourceConflictError, GitNonFastForwardError)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, RevisionFormatError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, GitAuthenticationError):
        return HTTPException(status_code=401, detail=str(exc))
    # EgressBlockedError, GitConnectionError, generic GitOperationError and
    # GitSourceValidationError all surface as a user-fixable 400.
    return HTTPException(status_code=400, detail=str(exc))


def _git_source_to_public(source, update_available: bool = False) -> AgentGitSourcePublic:
    public = AgentGitSourcePublic.model_validate(source)
    public.update_available = update_available
    public.web_history_url = build_web_history_url(
        source.repo_url, ref=source.ref, subdir=source.subdir
    )
    public.web_tree_url = build_web_tree_url(
        source.repo_url, ref=source.ref, subdir=source.subdir
    )
    return public


# ── Checkout ─────────────────────────────────────────────────────────────


@router.post(
    "/checkout",
    response_model=AgentCheckoutResponse,
    dependencies=[Depends(require_developer)],
)
async def checkout_agent(
    request: AgentCheckoutRequest,
    session: SessionDep,
    current_user: CurrentUser,
) -> AgentCheckoutResponse:
    """Clone ``<repo>[/subdir]@<ref>`` and import it as a new agent install.

    Parses ``cinna.agent.json``, persists the tree as an internal bundle
    revision (SSOT), creates the install + env, and seeds the workspace from
    the cloned tree. Records an ``AgentGitSource`` so the install can later be
    pulled / pushed. Developer-gated.
    """
    try:
        install, source = await GitSourceService.checkout(
            session=session,
            user=current_user,
            repo_url=request.repo_url,
            subdir=request.subdir,
            ref=request.ref,
            ssh_key_id=request.ssh_key_id,
            sync_direction=request.sync_direction,
            name_override=request.name_override,
        )
    except (
        GitSourceNotFoundError,
        GitSourceConflictError,
        GitSourceValidationError,
        RevisionFormatError,
        EgressBlockedError,
        GitOperationError,
    ) as exc:
        raise _map_git_error(exc)
    except IntegrityError:
        # Race backstop: a DB uniqueness violation (e.g. two concurrent
        # checkouts of the same bundle_id) that the service did not already
        # translate. The common same-user re-checkout is detected up front and
        # surfaces as GitSourceConflictError above.
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="This repository is already checked out for your account.",
        )

    return AgentCheckoutResponse(
        agent=AgentService.to_public_with_clone_info(session, install),
        git_source=_git_source_to_public(source),
    )


# ── Connect / disconnect (enable-on-existing-agent) ──────────────────────


@router.post(
    "/{agent_id}/git/connect",
    response_model=AgentGitSourcePublic,
    dependencies=[Depends(require_developer)],
)
async def connect_git_source(
    agent_id: uuid.UUID,
    request: AgentGitConnectRequest,
    session: SessionDep,
    current_user: CurrentUser,
) -> AgentGitSourcePublic:
    """Attach a git source to an existing owned agent + initial export push.

    Unlike checkout (which imports a foreign repo into a *new* install), connect
    enables versioning on an agent the user already has: it captures the current
    live workspace and pushes it as the first commit. Developer-gated;
    per-agent locked. ``update_available`` is ``False`` immediately after connect
    (the remote now equals ``last_synced_commit``).
    """
    try:
        source, _install = await GitSourceService.connect(
            session=session,
            agent_id=agent_id,
            user=current_user,
            repo_url=request.repo_url,
            subdir=request.subdir,
            ref=request.ref,
            ssh_key_id=request.ssh_key_id,
            sync_direction=request.sync_direction,
            commit_message=request.commit_message,
            adopt_existing=request.adopt_existing,
        )
    except GitSourceExistingAgentError as exc:
        # Recoverable 409: the subdir already holds an agent. Surface a machine-
        # readable code so the UI can offer to adopt the existing folder
        # (re-send with adopt_existing=True) instead of just showing an error.
        raise HTTPException(
            status_code=409,
            detail={"code": "existing_agent_folder", "message": str(exc)},
        )
    except (
        GitSourceNotFoundError,
        GitSourceConflictError,
        GitSourceValidationError,
        RevisionFormatError,
        EgressBlockedError,
        GitOperationError,
    ) as exc:
        raise _map_git_error(exc)
    except IntegrityError:
        # Race backstop: a concurrent connect slipped past the no-existing-source
        # guard and hit the unique agent_id index.
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="A git source is already configured for this agent.",
        )
    return _git_source_to_public(source, update_available=False)


@router.delete(
    "/{agent_id}/git",
    response_model=Message,
    dependencies=[Depends(require_developer)],
)
def disconnect_git_source(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> Message:
    """Disconnect (delete) the agent's git source. Does not touch the remote."""
    try:
        GitSourceService.disconnect(session, agent_id, current_user)
    except GitSourceNotFoundError as exc:
        raise _map_git_error(exc)
    return Message(message="Git source disconnected")


# ── Status / update check ────────────────────────────────────────────────


@router.get("/{agent_id}/git", response_model=AgentGitSourcePublic)
def get_git_source(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> AgentGitSourcePublic:
    """Return the agent's git source — remote-free (cheap plain status read).

    Does NO remote git I/O and always reports ``update_available = False``;
    freshness is owned by ``GET /git/check-updates`` (and the dirty check), so
    this read never blocks on — nor pins a pooled DB connection behind — a slow
    remote. The frontend polls those endpoints for the update banner.
    """
    try:
        source, update_available = GitSourceService.get_source(
            session, agent_id, current_user
        )
    except GitSourceNotFoundError as exc:
        raise _map_git_error(exc)
    return _git_source_to_public(source, update_available)


@router.get("/{agent_id}/git/check-updates", response_model=GitUpdateStatus)
def check_git_updates(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> GitUpdateStatus:
    """Cheap ``ls-remote`` HEAD vs ``last_synced_commit`` (no clone)."""
    try:
        result = GitSourceService.check_updates(session, agent_id, current_user)
    except (
        GitSourceNotFoundError,
        GitSourceValidationError,
        EgressBlockedError,
        GitOperationError,
    ) as exc:
        raise _map_git_error(exc)
    return GitUpdateStatus(**result)


# ── Commit history / dirty state ─────────────────────────────────────────


@router.get("/{agent_id}/git/commits", response_model=GitCommitList)
def list_git_commits(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
    limit: int = 50,
) -> GitCommitList:
    """List recent commits touching the source's subdir (newest first).

    Strict — surfaces auth / network errors (like check-updates). ``limit`` is
    clamped server-side to 1..200. Owner-resolved (404 for a non-owner).
    """
    limit = max(1, min(limit, 200))
    try:
        commits = GitSourceService.list_commits(
            session, agent_id, current_user, limit=limit
        )
    except (
        GitSourceNotFoundError,
        GitSourceValidationError,
        RevisionFormatError,
        EgressBlockedError,
        GitOperationError,
    ) as exc:
        raise _map_git_error(exc)
    return GitCommitList(commits=[GitCommit(**c) for c in commits])


@router.get("/{agent_id}/git/dirty", response_model=GitDirtyStatus)
def get_git_dirty(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> GitDirtyStatus:
    """Whether the live workspace / prompts diverge from the last synced revision.

    Read-only (never pushes); best-effort on the env side. Owner-resolved
    (404 for a missing source / non-owner). Gates the "Commit Agent" action in
    the UI — kept a separate endpoint so the cheap ``GET /git`` status read is
    never slowed by the workspace tree copy this does.
    """
    try:
        result = GitSourceService.compute_dirty(session, agent_id, current_user)
    except (
        GitSourceNotFoundError,
        GitSourceValidationError,
        GitBaselineUnavailableError,
    ) as exc:
        raise _map_git_error(exc)
    return GitDirtyStatus(**result)


@router.get("/{agent_id}/git/status", response_model=GitStatus)
def get_git_status(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> GitStatus:
    """File/prompt-level preview of what the next commit would capture.

    The detailed sibling of ``GET /git/dirty`` — returns the actual per-prompt
    and per-file changes (``added`` / ``modified`` / ``deleted``) so the commit
    dialog can render a ``git status`` style preview. Mirrors the post-denylist
    capture a push produces, so the preview matches the commit exactly.
    Read-only; owner-resolved (404 for a missing source / non-owner).
    """
    try:
        result = GitSourceService.compute_status(session, agent_id, current_user)
    except (
        GitSourceNotFoundError,
        GitSourceValidationError,
        GitBaselineUnavailableError,
    ) as exc:
        raise _map_git_error(exc)
    return GitStatus(**result)


# ── Pull ───────────────────────────────────────────────────────────────


@router.post(
    "/{agent_id}/git/pull",
    response_model=AgentPublic,
    dependencies=[Depends(require_developer)],
)
async def pull_git_source(
    agent_id: uuid.UUID,
    session: SessionDep,
    current_user: CurrentUser,
) -> AgentPublic:
    """Pull the latest remote revision onto the install (reuses apply-update)."""
    try:
        install = await GitSourceService.pull_update(
            session=session, agent_id=agent_id, owner=current_user
        )
    except (
        GitSourceNotFoundError,
        GitSourceConflictError,
        GitSourceValidationError,
        RevisionFormatError,
        EgressBlockedError,
        GitOperationError,
    ) as exc:
        raise _map_git_error(exc)
    return AgentService.to_public_with_clone_info(session, install)


# ── Push ───────────────────────────────────────────────────────────────


@router.post(
    "/{agent_id}/git/push",
    response_model=AgentGitSourcePublic,
    dependencies=[Depends(require_developer)],
)
async def push_git_source(
    agent_id: uuid.UUID,
    request: GitPushRequest,
    session: SessionDep,
    current_user: CurrentUser,
) -> AgentGitSourcePublic:
    """Capture the live workspace and fast-forward-push it to the remote.

    Fast-forward-only: a remote that advanced since the last sync returns 409
    ("pull first"). ``also_publish_bundle`` (default off) additionally cuts a
    parallel catalog bundle revision — only valid on a publisher install.
    Developer-gated.
    """
    try:
        source = await GitSourceService.push(
            session=session,
            agent_id=agent_id,
            owner=current_user,
            commit_message=request.commit_message,
            version=request.version,
            also_publish_bundle=request.also_publish_bundle,
        )
    except (
        GitSourceNotFoundError,
        GitSourceConflictError,
        GitSourceValidationError,
        RevisionFormatError,
        EgressBlockedError,
        GitOperationError,
    ) as exc:
        raise _map_git_error(exc)
    return _git_source_to_public(source)
