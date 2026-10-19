"""Artifacts: publish, republish, list, detail, update, delete, grants, views.

Every read goes through :class:`ArtifactAccess`; a caller who cannot view an
artifact gets 404 (never 403), and every write is owner-only (also 404 for
everyone else). Errors are :class:`ArtifactError`; routes map them to HTTP.

Publish order (a failed publish leaves nothing behind):

1. quota pre-check on the declared upload size (D7);
2. ingest into a scratch folder (:mod:`.artifact_ingest`);
3. lock the owner's ``user`` row (``SELECT … FOR UPDATE``) and re-check the
   quota against the actual size — serialises one user's concurrent publishes;
   a republish also locks the artifact row and takes ``max(number) + 1``;
4. write the rows, promote the scratch folder to ``<artifact>/<revision_id>/``,
   commit. Any failure after promotion removes the promoted folder.
"""
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import exists, func, or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.security import decrypt_field
from app.models.artifacts.artifact import (
    Artifact,
    ArtifactDetailPublic,
    ArtifactKind,
    ArtifactPublic,
    ArtifactPublished,
    ArtifactsCapabilities,
    ArtifactsLimits,
    ArtifactsPublic,
    ArtifactUpdate,
    ArtifactViewPublic,
    ArtifactVisibility,
)
from app.models.artifacts.artifact_grant import (
    ArtifactGrant,
    ArtifactGrantIn,
    ArtifactGrantPublic,
    ArtifactGroupGrant,
)
from app.models.artifacts.artifact_link import ArtifactLink, ArtifactLinkPublic
from app.models.artifacts.artifact_revision import (
    ArtifactFileEntry,
    ArtifactRevision,
    ArtifactRevisionPublic,
)
from app.models.catalog.catalog_access import (
    CatalogPerson,
    CatalogPrincipalGroup,
    CatalogPrincipalUser,
)
from app.models.users.user import User
from app.models.users.user_group import UserGroup
from app.models.users.user_workspace import UserWorkspace
from app.services.artifacts import artifact_cap
from app.services.artifacts.artifact_access import ArtifactAccess
from app.services.artifacts.artifact_content import (
    ArtifactContentService,
    download_name,
)
from app.services.artifacts.artifact_errors import ArtifactError, not_found
from app.services.artifacts.artifact_ingest import (
    ArtifactIngestError,
    ArtifactIngestService,
    FilesSource,
    IngestResult,
    ZipSource,
    normalize_path,
)
from app.services.artifacts.artifact_mime import is_html_path
from app.services.artifacts.artifact_storage import ArtifactStorage
from app.services.users.user_group_access import UserGroupAccess

LIST_MAX_LIMIT = 200


def _now() -> datetime:
    return datetime.now(UTC)


def _quota_error() -> ArtifactError:
    return ArtifactError(
        413,
        "Artifact storage quota exceeded "
        f"({settings.ARTIFACT_USER_QUOTA_BYTES} bytes across all your artifacts)",
    )


def owner_display_name(user: User | None) -> str | None:
    if user is None:
        return None
    return user.full_name or user.email


def artifact_page_url(artifact_id: uuid.UUID) -> str:
    return f"{settings.FRONTEND_HOST.rstrip('/')}/artifact/{artifact_id}"


def live_link_clause(now: datetime):
    """SQL condition: the link is neither revoked nor expired."""
    return (col(ArtifactLink.revoked_at).is_(None)) & (
        col(ArtifactLink.expires_at).is_(None) | (col(ArtifactLink.expires_at) > now)
    )


def _merge_excluded(result: IngestResult, extra: list[str] | None) -> None:
    """Prepend what the source left out before the ingest saw it (env artifact zips)."""
    if extra:
        seen = set(result.excluded)
        result.excluded = [p for p in extra if p not in seen] + result.excluded


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@dataclass(frozen=True)
class ArtifactDownload:
    """A revision's cached zip and the name to download it as."""

    path: Path
    filename: str


class ArtifactService:
    # ── Lookups ────────────────────────────────────────────────────

    @staticmethod
    def get_viewable(db: Session, artifact_id: uuid.UUID, user: User | None) -> Artifact:
        artifact = db.get(Artifact, artifact_id)
        if artifact is None or not ArtifactAccess.can_view(db, artifact, user):
            raise not_found()
        return artifact

    @staticmethod
    def get_managed(db: Session, artifact_id: uuid.UUID, user: User) -> Artifact:
        artifact = db.get(Artifact, artifact_id)
        if artifact is None or not ArtifactAccess.can_manage(artifact, user):
            raise not_found()
        return artifact

    @staticmethod
    def current_revision(db: Session, artifact: Artifact) -> ArtifactRevision | None:
        if artifact.current_revision_id is None:
            return None
        return db.get(ArtifactRevision, artifact.current_revision_id)

    @staticmethod
    def revision_by_number(db: Session, artifact: Artifact, number: int) -> ArtifactRevision | None:
        return db.exec(
            select(ArtifactRevision).where(
                ArtifactRevision.artifact_id == artifact.id,
                ArtifactRevision.number == number,
            )
        ).first()

    @classmethod
    def _revision_for_viewer(
        cls, db: Session, artifact: Artifact, user: User, number: int | None
    ) -> ArtifactRevision:
        """The requested revision; non-owners only ever reach the current one."""
        current = cls.current_revision(db, artifact)
        if number is None:
            revision = current
        elif artifact.owner_id == user.id:
            revision = cls.revision_by_number(db, artifact, number)
        else:
            revision = current if current is not None and current.number == number else None
        if revision is None:
            raise not_found("Revision")
        return revision

    # ── Quota / capabilities ───────────────────────────────────────

    @staticmethod
    def used_bytes(db: Session, owner_id: uuid.UUID) -> int:
        total = db.exec(
            select(func.coalesce(func.sum(ArtifactRevision.size_bytes), 0))
            .join(Artifact, col(Artifact.id) == col(ArtifactRevision.artifact_id))
            .where(Artifact.owner_id == owner_id)
        ).one()
        return int(total or 0)

    @staticmethod
    def capabilities() -> ArtifactsCapabilities:
        return ArtifactsCapabilities(
            version=1,
            limits=ArtifactsLimits(
                max_revision_bytes=settings.ARTIFACT_MAX_REVISION_BYTES,
                max_revision_files=settings.ARTIFACT_MAX_REVISION_FILES,
                max_file_bytes=settings.ARTIFACT_MAX_FILE_BYTES,
                user_quota_bytes=settings.ARTIFACT_USER_QUOTA_BYTES,
            ),
        )

    # ── Publish ────────────────────────────────────────────────────

    @classmethod
    def _ingest(
        cls,
        db: Session,
        user: User,
        source: ZipSource | FilesSource,
        declared_bytes: int | None,
        entry_path: str | None,
    ) -> IngestResult:
        if declared_bytes and (
            cls.used_bytes(db, user.id) + declared_bytes > settings.ARTIFACT_USER_QUOTA_BYTES
        ):
            raise _quota_error()
        try:
            return ArtifactIngestService.ingest(source, entry_path)
        except ArtifactIngestError as exc:
            raise ArtifactError(exc.status, exc.message) from exc

    @classmethod
    def _commit_revision(
        cls,
        db: Session,
        user: User,
        result: IngestResult,
        artifact_id: uuid.UUID,
        *,
        is_new: bool,
        note: str | None,
        apply: Callable[[datetime, uuid.UUID], tuple[Artifact, int]],
    ) -> tuple[Artifact, int]:
        """Lock, re-check the quota, write rows, promote the files, commit.

        ``apply(now, revision_id)`` adds or updates the artifact row (flushed)
        and returns it with the new revision's number.
        """
        revision_id = uuid.uuid4()
        promoted = False
        try:
            db.exec(select(User.id).where(User.id == user.id).with_for_update()).one()
            if cls.used_bytes(db, user.id) + result.size_bytes > settings.ARTIFACT_USER_QUOTA_BYTES:
                raise _quota_error()
            now = _now()
            artifact, number = apply(now, revision_id)
            db.add(
                ArtifactRevision(
                    id=revision_id,
                    artifact_id=artifact_id,
                    number=number,
                    size_bytes=result.size_bytes,
                    file_count=result.file_count,
                    files=result.files,
                    excluded=result.excluded,
                    created_by=user.id,
                    note=(note or "").strip() or None,
                    created_at=now,
                )
            )
            db.flush()
            ArtifactStorage.promote(result.tmp_dir, artifact_id, revision_id)
            promoted = True
            db.commit()
        except BaseException:
            db.rollback()
            ArtifactStorage.discard_tmp(result.tmp_dir)
            if promoted:
                if is_new:
                    ArtifactStorage.remove_artifact(artifact_id)
                else:
                    ArtifactStorage.remove_revision(artifact_id, revision_id)
            raise
        db.refresh(artifact)
        return artifact, number

    @staticmethod
    def _check_workspace(db: Session, user: User, workspace_id: uuid.UUID | None) -> None:
        if workspace_id is None:
            return
        workspace = db.get(UserWorkspace, workspace_id)
        if workspace is None or workspace.user_id != user.id:
            raise ArtifactError(400, "Workspace not found")

    @classmethod
    def create_from_upload(
        cls,
        db: Session,
        user: User,
        *,
        source: ZipSource | FilesSource,
        declared_bytes: int | None,
        title: str,
        description: str | None,
        entry_path: str | None,
        source_kind: str,
        source_label: str | None,
        user_workspace_id: uuid.UUID | None = None,
        source_agent_id: uuid.UUID | None = None,
        source_key: str | None = None,
        source_session_id: uuid.UUID | None = None,
        extra_excluded: list[str] | None = None,
    ) -> ArtifactPublished:
        cls._check_workspace(db, user, user_workspace_id)
        result = cls._ingest(db, user, source, declared_bytes, entry_path)
        _merge_excluded(result, extra_excluded)
        artifact_id = uuid.uuid4()

        def apply(now: datetime, revision_id: uuid.UUID) -> tuple[Artifact, int]:
            artifact = Artifact(
                id=artifact_id,
                owner_id=user.id,
                user_workspace_id=user_workspace_id,
                title=title.strip(),
                description=(description or "").strip() or None,
                kind=result.kind,
                entry_path=result.entry_path,
                current_revision_id=revision_id,
                source_kind=source_kind,
                source_agent_id=source_agent_id,
                source_session_id=source_session_id,
                source_label=(source_label or "").strip() or None,
                source_key=source_key,
                created_at=now,
                updated_at=now,
            )
            db.add(artifact)
            db.flush()
            return artifact, 1

        artifact, number = cls._commit_revision(
            db, user, result, artifact_id, is_new=True, note=None, apply=apply
        )
        return cls._published(db, artifact, user, number, result)

    @classmethod
    def republish(
        cls,
        db: Session,
        user: User,
        artifact_id: uuid.UUID,
        *,
        source: ZipSource | FilesSource,
        declared_bytes: int | None,
        entry_path: str | None,
        note: str | None,
        title: str | None = None,
        description: str | None = None,
        extra_excluded: list[str] | None = None,
    ) -> ArtifactPublished:
        """Add a revision; the id and page URL stay, the new revision becomes current.

        Without an explicit ``entry_path`` the artifact keeps its start page
        when the new files still contain it, else the ingest's detection wins.
        """
        existing = cls.get_managed(db, artifact_id, user)
        previous_entry = existing.entry_path
        result = cls._ingest(db, user, source, declared_bytes, entry_path)
        _merge_excluded(result, extra_excluded)
        kind, entry = result.kind, result.entry_path
        if not (entry_path or "").strip() and previous_entry:
            if previous_entry in {f["path"] for f in result.files}:
                kind, entry = ArtifactKind.SITE, previous_entry

        def apply(now: datetime, revision_id: uuid.UUID) -> tuple[Artifact, int]:
            artifact = db.exec(
                select(Artifact).where(Artifact.id == artifact_id).with_for_update()
            ).first()
            if artifact is None:  # deleted while the upload was being ingested
                raise not_found()
            last = db.exec(
                select(func.max(ArtifactRevision.number)).where(
                    ArtifactRevision.artifact_id == artifact_id
                )
            ).one()
            artifact.current_revision_id = revision_id
            artifact.kind = kind
            artifact.entry_path = entry
            if title is not None and title.strip():
                artifact.title = title.strip()
            if description is not None:
                artifact.description = description.strip() or None
            artifact.updated_at = now
            db.add(artifact)
            db.flush()
            return artifact, int(last or 0) + 1

        artifact, number = cls._commit_revision(
            db, user, result, artifact_id, is_new=False, note=note, apply=apply
        )
        return cls._published(db, artifact, user, number, result)

    @classmethod
    def _published(
        cls, db: Session, artifact: Artifact, user: User, number: int, result: IngestResult
    ) -> ArtifactPublished:
        public = cls.to_public(db, artifact, user)
        return ArtifactPublished(
            **public.model_dump(), revision_number=number, excluded=result.excluded
        )

    # ── Projection ─────────────────────────────────────────────────

    @classmethod
    def to_public(cls, db: Session, artifact: Artifact, viewer: User | None) -> ArtifactPublic:
        return cls.to_public_many(db, [artifact], viewer)[0]

    @staticmethod
    def to_public_many(
        db: Session, artifacts: list[Artifact], viewer: User | None
    ) -> list[ArtifactPublic]:
        """Project a page of artifacts with a fixed number of queries."""
        if not artifacts:
            return []
        ids = [a.id for a in artifacts]
        totals = {
            row[0]: (int(row[1]), int(row[2] or 0))
            for row in db.exec(
                select(
                    ArtifactRevision.artifact_id,
                    func.count(ArtifactRevision.id),
                    func.coalesce(func.sum(ArtifactRevision.size_bytes), 0),
                )
                .where(col(ArtifactRevision.artifact_id).in_(ids))
                .group_by(ArtifactRevision.artifact_id)
            ).all()
        }
        current_ids = [a.current_revision_id for a in artifacts if a.current_revision_id]
        current = {
            row[0]: (row[1], row[2])
            for row in db.exec(
                select(ArtifactRevision.id, ArtifactRevision.number, ArtifactRevision.file_count)
                .where(col(ArtifactRevision.id).in_(current_ids))
            ).all()
        } if current_ids else {}
        owned = [a.id for a in artifacts if viewer is not None and a.owner_id == viewer.id]
        grant_counts: dict[uuid.UUID, int] = {}
        link_counts: dict[uuid.UUID, int] = {}
        if owned:
            # A share count (people + groups), not a person count.
            for model in (ArtifactGrant, ArtifactGroupGrant):
                for artifact_id, n in db.exec(
                    select(model.artifact_id, func.count(model.id))
                    .where(col(model.artifact_id).in_(owned))
                    .group_by(model.artifact_id)
                ).all():
                    grant_counts[artifact_id] = grant_counts.get(artifact_id, 0) + int(n)
            link_counts = {
                row[0]: int(row[1])
                for row in db.exec(
                    select(ArtifactLink.artifact_id, func.count(ArtifactLink.id))
                    .where(col(ArtifactLink.artifact_id).in_(owned), live_link_clause(_now()))
                    .group_by(ArtifactLink.artifact_id)
                ).all()
            }
        owner_ids = {a.owner_id for a in artifacts}
        owners = {
            u.id: u for u in db.exec(select(User).where(col(User.id).in_(owner_ids))).all()
        }

        out: list[ArtifactPublic] = []
        for a in artifacts:
            revision_count, size_bytes = totals.get(a.id, (0, 0))
            number, file_count = current.get(a.current_revision_id, (None, 0))
            is_owner = viewer is not None and a.owner_id == viewer.id
            out.append(
                ArtifactPublic(
                    id=a.id,
                    title=a.title,
                    description=a.description,
                    kind=a.kind,
                    entry_path=a.entry_path,
                    visibility=a.visibility,
                    source_kind=a.source_kind,
                    source_label=a.source_label,
                    # Owner-private context: other viewers never see these.
                    source_agent_id=a.source_agent_id if is_owner else None,
                    source_session_id=a.source_session_id if is_owner else None,
                    user_workspace_id=a.user_workspace_id if is_owner else None,
                    current_revision_id=a.current_revision_id,
                    current_revision_number=number,
                    revision_count=revision_count,
                    file_count=file_count,
                    size_bytes=size_bytes,
                    owner_id=a.owner_id,
                    owner_name=owner_display_name(owners.get(a.owner_id)),
                    is_owner=is_owner,
                    grant_count=grant_counts.get(a.id, 0),
                    live_link_count=link_counts.get(a.id, 0),
                    url=artifact_page_url(a.id),
                    created_at=a.created_at,
                    updated_at=a.updated_at,
                )
            )
        return out

    # ── List / detail ──────────────────────────────────────────────

    @classmethod
    def list_artifacts(
        cls,
        db: Session,
        user: User,
        *,
        scope: str = "mine",
        user_workspace_id: str | None = None,
        q: str | None = None,
        source_key: str | None = None,
        skip: int = 0,
        limit: int = 100,
    ) -> ArtifactsPublic:
        """``mine``: owned (workspace / source_key filters apply);
        ``shared_with_me``: not mine, and instance-visible, granted to me, or
        granted to a group I belong to.

        ``user_workspace_id``: None = every workspace, ``""`` = the default
        workspace (NULL), a UUID = that workspace (credentials semantics).
        """
        stmt = select(Artifact)
        if scope == "shared_with_me":
            granted = exists().where(
                ArtifactGrant.artifact_id == Artifact.id, ArtifactGrant.user_id == user.id
            )
            group_granted = exists().where(
                ArtifactGroupGrant.artifact_id == Artifact.id,
                col(ArtifactGroupGrant.group_id).in_(
                    UserGroupAccess.member_group_ids_subquery(user.id)
                ),
            )
            stmt = stmt.where(
                Artifact.owner_id != user.id,
                or_(
                    Artifact.visibility == ArtifactVisibility.INSTANCE,
                    granted,
                    group_granted,
                ),
            )
        else:
            stmt = stmt.where(Artifact.owner_id == user.id)
            if user_workspace_id is not None:
                if user_workspace_id == "":
                    stmt = stmt.where(col(Artifact.user_workspace_id).is_(None))
                else:
                    try:
                        workspace = uuid.UUID(user_workspace_id)
                    except ValueError as exc:
                        raise ArtifactError(400, "Invalid workspace ID format") from exc
                    stmt = stmt.where(Artifact.user_workspace_id == workspace)
            if source_key is not None:
                stmt = stmt.where(Artifact.source_key == source_key)
        if q and q.strip():
            stmt = stmt.where(
                col(Artifact.title).ilike(f"%{_escape_like(q.strip())}%", escape="\\")
            )
        count = db.exec(select(func.count()).select_from(stmt.subquery())).one()
        rows = db.exec(
            stmt.order_by(col(Artifact.updated_at).desc(), col(Artifact.id))
            .offset(max(skip, 0))
            .limit(min(max(limit, 1), LIST_MAX_LIMIT))
        ).all()
        return ArtifactsPublic(data=cls.to_public_many(db, list(rows), user), count=int(count))

    @classmethod
    def get_detail(cls, db: Session, user: User, artifact_id: uuid.UUID) -> ArtifactDetailPublic:
        artifact = cls.get_viewable(db, artifact_id, user)
        public = cls.to_public(db, artifact, user)
        is_owner = public.is_owner
        if is_owner:
            revisions = db.exec(
                select(ArtifactRevision)
                .where(ArtifactRevision.artifact_id == artifact.id)
                .order_by(col(ArtifactRevision.number).desc())
            ).all()
            pinned = set(
                db.exec(
                    select(ArtifactLink.pinned_revision_id).where(
                        ArtifactLink.artifact_id == artifact.id,
                        col(ArtifactLink.pinned_revision_id).is_not(None),
                        live_link_clause(_now()),
                    )
                ).all()
            )
        else:
            current = cls.current_revision(db, artifact)
            revisions = [current] if current else []
            pinned = set()
        creator_ids = {r.created_by for r in revisions if r.created_by}
        creators = (
            {u.id: u for u in db.exec(select(User).where(col(User.id).in_(creator_ids))).all()}
            if creator_ids
            else {}
        )
        detail = ArtifactDetailPublic(
            **public.model_dump(),
            revisions=[
                ArtifactRevisionPublic(
                    id=r.id,
                    number=r.number,
                    size_bytes=r.size_bytes,
                    file_count=r.file_count,
                    files=[ArtifactFileEntry(**f) for f in r.files],
                    excluded=list(r.excluded),
                    note=r.note,
                    created_by=r.created_by,
                    created_by_name=owner_display_name(creators.get(r.created_by)),
                    created_at=r.created_at,
                    is_current=r.id == artifact.current_revision_id,
                    pinned=r.id in pinned,
                )
                for r in revisions
            ],
        )
        if is_owner:
            detail.grants = cls.list_grants(db, artifact)
            detail.links = cls.list_links(db, artifact, revisions)
        return detail

    @staticmethod
    def list_grants(db: Session, artifact: Artifact) -> list[ArtifactGrantPublic]:
        """People first, then groups; each set in grant order."""
        grants = db.exec(
            select(ArtifactGrant)
            .where(ArtifactGrant.artifact_id == artifact.id)
            .order_by(col(ArtifactGrant.created_at), col(ArtifactGrant.id))
        ).all()
        group_grants = db.exec(
            select(ArtifactGroupGrant)
            .where(ArtifactGroupGrant.artifact_id == artifact.id)
            .order_by(col(ArtifactGroupGrant.created_at), col(ArtifactGroupGrant.id))
        ).all()
        user_ids = (
            {g.user_id for g in grants}
            | {g.granted_by for g in grants if g.granted_by}
            | {g.granted_by for g in group_grants}
        )
        users = (
            {u.id: u for u in db.exec(select(User).where(col(User.id).in_(user_ids))).all()}
            if user_ids
            else {}
        )
        group_ids = {g.group_id for g in group_grants}
        groups = (
            {
                grp.id: grp
                for grp in db.exec(
                    select(UserGroup).where(col(UserGroup.id).in_(group_ids))
                ).all()
            }
            if group_ids
            else {}
        )
        member_counts = UserGroupAccess.member_counts(db, group_ids)

        def granter_person(granter_id: uuid.UUID | None) -> CatalogPerson | None:
            granter = users.get(granter_id) if granter_id else None
            if granter is None:
                return None
            return CatalogPerson(
                user_id=granter.id, display_name=owner_display_name(granter) or ""
            )

        out: list[ArtifactGrantPublic] = []
        for g in grants:
            out.append(
                ArtifactGrantPublic(
                    principal=CatalogPrincipalUser(user_id=g.user_id),
                    access="view",
                    display_name=owner_display_name(users.get(g.user_id)) or str(g.user_id),
                    email=users[g.user_id].email if g.user_id in users else None,
                    granted_by=granter_person(g.granted_by),
                    created_at=g.created_at,
                )
            )
        for gg in group_grants:
            group = groups.get(gg.group_id)
            out.append(
                ArtifactGrantPublic(
                    principal=CatalogPrincipalGroup(group_id=gg.group_id),
                    access="view",
                    display_name=group.name if group else str(gg.group_id),
                    email=None,
                    member_count=member_counts.get(gg.group_id, 0),
                    granted_by=granter_person(gg.granted_by),
                    created_at=gg.created_at,
                )
            )
        return out

    @staticmethod
    def link_public(link: ArtifactLink, pinned_number: int | None, now: datetime) -> ArtifactLinkPublic:
        expires_at = link.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        return ArtifactLinkPublic(
            id=link.id,
            token_prefix=link.token_prefix,
            label=link.label,
            created_at=link.created_at,
            expires_at=link.expires_at,
            revoked_at=link.revoked_at,
            pinned_revision_id=link.pinned_revision_id,
            pinned_revision_number=pinned_number,
            needs_code=link.code_encrypted is not None,
            code=decrypt_field(link.code_encrypted) if link.code_encrypted else None,
            failed_attempts=link.failed_attempts,
            expired=expires_at is not None and expires_at <= now,
            blocked=link.code_encrypted is not None
            and link.failed_attempts >= settings.ARTIFACT_LINK_MAX_CODE_ATTEMPTS,
        )

    @classmethod
    def list_links(
        cls, db: Session, artifact: Artifact, revisions: list[ArtifactRevision]
    ) -> list[ArtifactLinkPublic]:
        numbers = {r.id: r.number for r in revisions}
        now = _now()
        links = db.exec(
            select(ArtifactLink)
            .where(ArtifactLink.artifact_id == artifact.id)
            .order_by(col(ArtifactLink.created_at).desc(), col(ArtifactLink.id))
        ).all()
        return [
            cls.link_public(link, numbers.get(link.pinned_revision_id), now) for link in links
        ]

    # ── Update / delete ────────────────────────────────────────────

    @classmethod
    def update(
        cls, db: Session, user: User, artifact_id: uuid.UUID, data: ArtifactUpdate
    ) -> ArtifactPublic:
        artifact = cls.get_managed(db, artifact_id, user)
        fields = data.model_fields_set
        if "title" in fields and data.title is not None:
            if not data.title.strip():
                raise ArtifactError(422, "Title must not be blank")
            artifact.title = data.title.strip()
        if "description" in fields:
            artifact.description = (data.description or "").strip() or None
        if "visibility" in fields and data.visibility is not None:
            artifact.visibility = data.visibility
        if "entry_path" in fields:
            if data.entry_path is None or not data.entry_path.strip():
                artifact.kind, artifact.entry_path = ArtifactKind.FILES, None
            else:
                try:
                    entry, is_dir = normalize_path(data.entry_path.strip())
                except ArtifactIngestError as exc:
                    raise ArtifactError(400, exc.message) from exc
                current = cls.current_revision(db, artifact)
                paths = {f["path"] for f in current.files} if current else set()
                if is_dir or entry not in paths or not is_html_path(entry):
                    raise ArtifactError(
                        400, f"Start page {data.entry_path!r} must be an .html file of the current revision"
                    )
                artifact.kind, artifact.entry_path = ArtifactKind.SITE, entry
        artifact.updated_at = _now()
        db.add(artifact)
        db.commit()
        db.refresh(artifact)
        return cls.to_public(db, artifact, user)

    @classmethod
    def delete(cls, db: Session, user: User, artifact_id: uuid.UUID) -> None:
        """Rows go at once (revisions, grants, links cascade); then the files."""
        artifact = cls.get_managed(db, artifact_id, user)
        db.delete(artifact)
        db.commit()
        ArtifactStorage.remove_artifact(artifact_id)

    @classmethod
    def delete_revision(cls, db: Session, user: User, artifact_id: uuid.UUID, number: int) -> None:
        """D17: never the current revision, never one a live link pins.

        Dead links (revoked or expired) that pin it are unpinned first, so the
        foreign key does not refuse the delete.
        """
        artifact = cls.get_managed(db, artifact_id, user)
        revision = cls.revision_by_number(db, artifact, number)
        if revision is None:
            raise not_found("Revision")
        if revision.id == artifact.current_revision_id:
            raise ArtifactError(409, "The current revision cannot be deleted")
        now = _now()
        pinning = db.exec(
            select(ArtifactLink).where(ArtifactLink.pinned_revision_id == revision.id)
        ).all()
        live = [
            link for link in pinning
            if ArtifactAccess.link_usable(link, artifact, now)
        ]
        if live:
            raise ArtifactError(
                409, "A public link is pinned to this revision; revoke it before deleting"
            )
        for link in pinning:
            link.pinned_revision_id = None
            db.add(link)
        revision_id = revision.id
        db.delete(revision)
        db.commit()
        ArtifactStorage.remove_revision(artifact.id, revision_id)

    # ── Grants ─────────────────────────────────────────────────────

    @classmethod
    def replace_grants(
        cls, db: Session, user: User, artifact_id: uuid.UUID, grants: list[ArtifactGrantIn]
    ) -> list[ArtifactGrantPublic]:
        """Full replace (D21) of both the person and the group grants.

        The owner in the list is dropped; unknown users or groups ⇒ 400.
        """
        artifact = cls.get_managed(db, artifact_id, user)
        wanted_users = {
            g.principal.user_id
            for g in grants
            if isinstance(g.principal, CatalogPrincipalUser)
        } - {artifact.owner_id}
        wanted_groups = {
            g.principal.group_id
            for g in grants
            if isinstance(g.principal, CatalogPrincipalGroup)
        }
        if wanted_users:
            found = set(db.exec(select(User.id).where(col(User.id).in_(wanted_users))).all())
            missing = wanted_users - found
            if missing:
                raise ArtifactError(400, f"User not found: {sorted(map(str, missing))[0]}")
        if wanted_groups:
            found_groups = set(
                db.exec(select(UserGroup.id).where(col(UserGroup.id).in_(wanted_groups))).all()
            )
            missing_groups = wanted_groups - found_groups
            if missing_groups:
                raise ArtifactError(
                    400, f"Group not found: {sorted(map(str, missing_groups))[0]}"
                )

        existing = {
            g.user_id: g
            for g in db.exec(
                select(ArtifactGrant).where(ArtifactGrant.artifact_id == artifact.id)
            ).all()
        }
        for user_id, grant in existing.items():
            if user_id not in wanted_users:
                db.delete(grant)
        for user_id in wanted_users - existing.keys():
            db.add(ArtifactGrant(artifact_id=artifact.id, user_id=user_id, granted_by=user.id))

        existing_groups = {
            g.group_id: g
            for g in db.exec(
                select(ArtifactGroupGrant).where(ArtifactGroupGrant.artifact_id == artifact.id)
            ).all()
        }
        for group_id, group_grant in existing_groups.items():
            if group_id not in wanted_groups:
                db.delete(group_grant)
        for group_id in wanted_groups - existing_groups.keys():
            db.add(
                ArtifactGroupGrant(
                    artifact_id=artifact.id, group_id=group_id, granted_by=user.id
                )
            )
        try:
            db.commit()
        except IntegrityError as exc:
            # A concurrent replace inserted the same grant (or the user/group went away).
            db.rollback()
            raise ArtifactError(409, "The grants changed concurrently; reload and retry") from exc
        return cls.list_grants(db, artifact)

    # ── Views / downloads ──────────────────────────────────────────

    @staticmethod
    def build_view(artifact: Artifact, revision: ArtifactRevision, via: str) -> ArtifactViewPublic:
        """Mint a cap for ``revision`` and describe where its files are served."""
        cap, expires_at = artifact_cap.mint(artifact.id, revision.id, via)
        paths = {f["path"] for f in revision.files}
        entry = artifact.entry_path if artifact.entry_path in paths else None
        return ArtifactViewPublic(
            content_base=artifact_cap.content_base(cap),
            entry=entry,
            files=[ArtifactFileEntry(**f) for f in revision.files],
            revision_number=revision.number,
            expires_at=expires_at,
            download_url=artifact_cap.zip_url(cap),
        )

    @classmethod
    def view_for_user(
        cls,
        db: Session,
        artifact_id: uuid.UUID,
        user: User,
        revision_number: int | None = None,
    ) -> ArtifactViewPublic:
        artifact = cls.get_viewable(db, artifact_id, user)
        revision = cls._revision_for_viewer(db, artifact, user, revision_number)
        return cls.build_view(artifact, revision, artifact_cap.via_user(user.id))

    @classmethod
    def download(
        cls, db: Session, user: User, artifact_id: uuid.UUID, revision_number: int | None = None
    ) -> ArtifactDownload:
        """The revision's cached zip (built on first download)."""
        artifact = cls.get_viewable(db, artifact_id, user)
        revision = cls._revision_for_viewer(db, artifact, user, revision_number)
        path = ArtifactContentService.revision_zip(
            artifact.id, revision.id, [f["path"] for f in revision.files]
        )
        return ArtifactDownload(
            path=path, filename=download_name(artifact.title, f"-r{revision.number}.zip")
        )
