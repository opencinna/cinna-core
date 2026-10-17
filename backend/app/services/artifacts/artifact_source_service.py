"""Publish a file or folder out of an agent environment's workspace as an artifact.

Shared by the agent's ``artifacts_publish`` tool (``POST /agent/artifacts/publish``,
env token) and the owner's "Publish as artifact" in the workspace file view
(``POST /artifacts/from-environment``, user JWT, D20). Both produce
``source_kind='core_agent'`` artifacts with ``source_key = "<agent_id>:<path>"``
(A12 is per agent and path, so it survives the agent's environment being
recreated or switched).

The entry points are synchronous (the routes run in the threadpool); only the
pull from the environment runs on the event loop.

Flow:

1. normalise the workspace path (``/app/workspace/x``, ``./x``, ``x/`` → ``x``)
   and refuse, 400, a path that is itself excluded by A10 or has a dot-named
   segment — the env's zip is relative to the published folder and a file
   arrives under its basename, so the ingest cannot see that the folder itself
   is ``credentials/`` or ``.claude/``;
2. ``artifact_id`` given ⇒ it must pass :func:`agent_publish_policy` (owner,
   ``core_agent``, this agent), else 404;
3. the environment must be running, else 409;
4. pull the item through the environment adapter in artifact mode (D5): a
   folder arrives as a zip (``application/zip``) without dotfiles and
   outside-pointing symlinks, which the env reports (``meta.skipped``, merged
   into ``excluded``; an env built before the report sends none), a file as
   itself; streamed to a scratch file under a hard byte cap (413);
5. republish ``artifact_id``, else the owner's artifact with the same
   ``source_key`` (A12), else create a new private artifact.
"""
import logging
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

import anyio.from_thread
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.config import settings
from app.models.agents.agent import Agent
from app.models.artifacts.artifact import Artifact, ArtifactPublished
from app.models.artifacts.artifact_revision import ArtifactRevision
from app.models.artifacts.artifact_source import (
    AgentArtifactPublished,
    AgentArtifactPublishRequest,
    ArtifactFromEnvironmentRequest,
)
from app.models.environments.environment import AgentEnvironment
from app.models.sessions.session import Session as ChatSession
from app.models.users.user import User
from app.services.artifacts.artifact_access import agent_publish_policy
from app.services.artifacts.artifact_errors import ArtifactError, not_found
from app.services.artifacts.artifact_ingest import (
    ArtifactIngestError,
    FilesSource,
    SourceFile,
    ZipSource,
    excluded_prefix,
    normalize_path,
)
from app.services.artifacts.artifact_service import ArtifactService
from app.services.artifacts.artifact_storage import ArtifactStorage
from app.services.environments.environment_service import EnvironmentService

logger = logging.getLogger(__name__)

_WORKSPACE_PREFIX = "/app/workspace/"
_FOLDER_CONTENT_TYPE = "application/zip"

WorkspacePublishRequest = AgentArtifactPublishRequest | ArtifactFromEnvironmentRequest


@dataclass(frozen=True)
class _Pulled:
    """A workspace item copied to a scratch folder."""

    scratch: Path
    file: Path
    is_folder: bool
    name: str
    size: int
    # What the env's artifact zipper left out (relative to the folder).
    skipped: list[str] = field(default_factory=list)


def normalize_workspace_path(raw: str) -> str:
    """Workspace-relative path of a publishable file or folder; 400 otherwise."""
    path = (raw or "").strip()
    if path.startswith(_WORKSPACE_PREFIX):
        path = path[len(_WORKSPACE_PREFIX):]
    while path.startswith("./"):
        path = path[2:]
    path = path.rstrip("/")
    if not path or path == ".":
        raise ArtifactError(400, "Give a file or folder inside the workspace, not the workspace itself")
    try:
        normalized, _is_dir = normalize_path(path)
    except ArtifactIngestError as exc:
        raise ArtifactError(400, f"Invalid workspace path: {exc.message}") from exc
    _refuse_excluded_path(normalized)
    return normalized


def _refuse_excluded_path(path: str) -> None:
    """A10 on the published path itself (as a file and as a folder), plus dot-names."""
    if any(segment.startswith(".") for segment in path.split("/")):
        raise ArtifactError(400, f"{path} is a hidden (dot-named) path; it is never published")
    if excluded_prefix(path) is not None or excluded_prefix(f"{path}/_") is not None:
        raise ArtifactError(
            400,
            f"{path} is never published (credentials, keys, .env, .git, app-data, node_modules)",
        )


def source_key(agent_id: uuid.UUID, path: str) -> str:
    """A12 matching key: the same path of the same agent updates one artifact."""
    return f"{agent_id}:{path}"


class ArtifactSourceService:
    # ── Entry points (sync; called from threadpool routes) ─────────

    @classmethod
    def publish_from_agent(
        cls,
        db: Session,
        *,
        owner: User,
        agent: Agent,
        environment: AgentEnvironment,
        data: AgentArtifactPublishRequest,
    ) -> AgentArtifactPublished:
        """The ``artifacts_publish`` tool (env token)."""
        if data.source_session_id is not None:
            cls._check_session(db, owner, agent, data.source_session_id)
        published = cls._publish(
            db,
            owner=owner,
            agent=agent,
            environment=environment,
            data=data,
            source_session_id=data.source_session_id,
        )
        revision = db.get(ArtifactRevision, published.current_revision_id)
        return AgentArtifactPublished(
            artifact_id=published.id,
            revision=published.revision_number,
            url=published.url,
            kind=published.kind,
            file_count=revision.file_count if revision else published.file_count,
            size_bytes=revision.size_bytes if revision else 0,
            excluded=published.excluded,
        )

    @classmethod
    def publish_from_environment(
        cls, db: Session, user: User, data: ArtifactFromEnvironmentRequest
    ) -> ArtifactPublished:
        """The owner's workspace publish (user JWT, D20): agent owner only, else 404."""
        environment = db.get(AgentEnvironment, data.environment_id)
        agent = db.get(Agent, environment.agent_id) if environment is not None else None
        if environment is None or agent is None or agent.owner_id != user.id:
            raise not_found("Environment")
        return cls._publish(
            db, owner=user, agent=agent, environment=environment, data=data, source_session_id=None
        )

    # ── Shared publish ─────────────────────────────────────────────

    @classmethod
    def _publish(
        cls,
        db: Session,
        *,
        owner: User,
        agent: Agent,
        environment: AgentEnvironment,
        data: WorkspacePublishRequest,
        source_session_id: uuid.UUID | None,
    ) -> ArtifactPublished:
        path = normalize_workspace_path(data.path)
        policy = agent_publish_policy()
        target_id: uuid.UUID | None = None
        if data.artifact_id is not None:
            target = db.get(Artifact, data.artifact_id)
            if target is None or not policy.may_republish(target, owner.id, agent.id):
                raise not_found()
            target_id = target.id
        if environment.status != "running":
            raise ArtifactError(
                409,
                f"The agent's environment is {environment.status}; start it and publish again",
            )

        pulled = anyio.from_thread.run(cls._pull, environment, path)
        try:
            return cls._ingest_and_commit(
                db,
                owner=owner,
                agent=agent,
                data=data,
                pulled=pulled,
                key=source_key(agent.id, path),
                target_id=target_id,
                source_session_id=source_session_id,
            )
        finally:
            ArtifactStorage.discard_tmp(pulled.scratch)

    @classmethod
    def _ingest_and_commit(
        cls,
        db: Session,
        *,
        owner: User,
        agent: Agent,
        data: WorkspacePublishRequest,
        pulled: _Pulled,
        key: str,
        target_id: uuid.UUID | None,
        source_session_id: uuid.UUID | None,
    ) -> ArtifactPublished:
        policy = agent_publish_policy()
        if target_id is None:
            matched = db.exec(
                select(Artifact).where(Artifact.owner_id == owner.id, Artifact.source_key == key)
            ).first()
            if matched is not None:
                if not policy.may_republish(matched, owner.id, agent.id):
                    raise ArtifactError(409, "This path is already published from another source")
                target_id = matched.id

        with open(pulled.file, "rb") as handle:
            if pulled.is_folder:
                source: ZipSource | FilesSource = ZipSource(handle)
            else:
                source = FilesSource([SourceFile(pulled.name, handle)])
            if target_id is not None:
                return ArtifactService.republish(
                    db,
                    owner,
                    target_id,
                    source=source,
                    declared_bytes=pulled.size,
                    entry_path=data.entry,
                    note=None,
                    title=data.title,
                    description=data.description,
                    extra_excluded=pulled.skipped,
                )
            try:
                return ArtifactService.create_from_upload(
                    db,
                    owner,
                    source=source,
                    declared_bytes=pulled.size,
                    title=data.title,
                    description=data.description,
                    entry_path=data.entry,
                    source_kind=policy.source_kind,
                    source_label=agent.name,
                    user_workspace_id=agent.user_workspace_id,
                    source_agent_id=agent.id,
                    source_key=key,
                    source_session_id=source_session_id,
                    extra_excluded=pulled.skipped,
                )
            except IntegrityError as exc:
                # uq_artifact_owner_source_key: a concurrent publish of this path won.
                raise ArtifactError(
                    409, "This path is being published right now; publish again to add a revision"
                ) from exc

    # ── Helpers ────────────────────────────────────────────────────

    @staticmethod
    def _check_session(db: Session, owner: User, agent: Agent, session_id: uuid.UUID) -> None:
        """The calling conversation must belong to this environment's owner and agent."""
        session = db.get(ChatSession, session_id)
        if session is None or session.user_id != owner.id or session.agent_id != agent.id:
            raise ArtifactError(403, "Session does not belong to this environment")

    @staticmethod
    async def _pull(environment: AgentEnvironment, path: str) -> _Pulled:
        """Copy the workspace item into a scratch folder under a hard byte cap (D5)."""
        adapter = EnvironmentService.get_lifecycle_manager().get_adapter(environment)
        try:
            meta, stream = await adapter.fetch_workspace_item_for_artifact(path)
        except ValueError as exc:
            # The env refused the path (e.g. "Invalid path: Symlinks are not published: x").
            raise ArtifactError(400, str(exc) or f"Invalid workspace path: {path}") from exc
        except Exception as exc:  # noqa: BLE001 — adapter/transport failure
            logger.warning("Artifact pull failed for env %s path %s: %s", environment.id, path, exc)
            raise ArtifactError(502, "Could not read the workspace; is the environment healthy?") from exc
        if not meta.exists:
            raise ArtifactError(404, f"{path} was not found in the workspace")

        is_folder = (meta.content_type or "").split(";")[0].strip().lower() == _FOLDER_CONTENT_TYPE
        cap = (
            settings.ARTIFACT_MAX_REVISION_BYTES + settings.ARTIFACT_UPLOAD_OVERHEAD_BYTES
            if is_folder
            else settings.ARTIFACT_MAX_FILE_BYTES
        )
        if meta.size is not None and meta.size > cap:
            await _close(stream)
            raise _too_large(path, cap)

        scratch = ArtifactStorage.tmp_dir()
        target = scratch / ("pulled.zip" if is_folder else "pulled")
        size = 0
        try:
            with open(target, "wb") as out:
                async for chunk in stream:
                    size += len(chunk)
                    if size > cap:
                        raise _too_large(path, cap)
                    out.write(chunk)
        except BaseException:
            await _close(stream)
            ArtifactStorage.discard_tmp(scratch)
            raise
        return _Pulled(
            scratch=scratch,
            file=target,
            is_folder=is_folder,
            name=os.path.basename(path),
            size=size,
            skipped=list(meta.skipped or []) if is_folder else [],
        )


def _too_large(path: str, cap: int) -> ArtifactError:
    return ArtifactError(413, f"{path} is larger than the {cap} byte limit for one publish")


async def _close(stream: AsyncIterator[bytes]) -> None:
    aclose = getattr(stream, "aclose", None)
    if aclose is not None:
        try:
            await aclose()
        except Exception:  # noqa: BLE001
            pass
