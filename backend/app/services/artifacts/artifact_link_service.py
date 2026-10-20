"""Public links to artifacts (D22).

The token (``secrets.token_urlsafe(32)``) is returned once, at creation; only
its SHA-256 and its first 8 characters are stored. The optional 4-digit code
is Fernet-encrypted so the owner can read it back. Code checks run under
``SELECT … FOR UPDATE`` on the link row, so parallel guesses serialise; the
link is blocked once ``failed_attempts`` reaches
``ARTIFACT_LINK_MAX_CODE_ATTEMPTS`` (derived, no column) and then refuses even
the right code.

Opening a link mints a cap with ``via = l:<link_id>``; the content route
re-checks that the link is still live on every request, so a revoke, an
expiry or a deleted artifact stops an open viewer at its next file load.
"""
import hashlib
import secrets
import uuid
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.config import settings
from app.core.security import decrypt_field, encrypt_field
from app.models.artifacts.artifact import Artifact, ArtifactViewPublic
from app.models.artifacts.artifact_link import (
    ArtifactLink,
    ArtifactLinkCodeRefused,
    ArtifactLinkCreate,
    ArtifactLinkCreated,
    ArtifactLinkInfo,
)
from app.models.artifacts.artifact_revision import ArtifactRevision
from app.models.users.user import User
from app.services.artifacts import artifact_cap
from app.services.artifacts.artifact_access import ArtifactAccess
from app.services.artifacts.artifact_errors import ArtifactError, not_found
from app.services.artifacts.artifact_service import (
    ArtifactService,
    link_page_url,
    owner_display_name,
)

TOKEN_PREFIX_LENGTH = 8


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _is_expired(link: ArtifactLink, now: datetime) -> bool:
    return link.expires_at is not None and _aware(link.expires_at) <= now


def _is_blocked(link: ArtifactLink) -> bool:
    return (
        link.code_encrypted is not None
        and link.failed_attempts >= settings.ARTIFACT_LINK_MAX_CODE_ATTEMPTS
    )


def _refused(message: str, link: ArtifactLink) -> ArtifactError:
    left = max(settings.ARTIFACT_LINK_MAX_CODE_ATTEMPTS - link.failed_attempts, 0)
    return ArtifactError(
        403,
        message,
        detail=ArtifactLinkCodeRefused(
            message=message, attempts_left=left, blocked=_is_blocked(link)
        ).model_dump(),
    )


BLOCKED_MESSAGE = "This link has been blocked after too many wrong codes. Contact the owner."


class ArtifactLinkService:
    # ── Owner side ─────────────────────────────────────────────────

    @staticmethod
    def create(
        db: Session, user: User, artifact_id: uuid.UUID, data: ArtifactLinkCreate
    ) -> ArtifactLinkCreated:
        artifact = ArtifactService.get_managed(db, artifact_id, user)
        now = datetime.now(UTC)
        pinned_number: int | None = None
        if data.pinned_revision_id is not None:
            pinned = db.get(ArtifactRevision, data.pinned_revision_id)
            # A pin must name a revision of this very artifact.
            if pinned is None or pinned.artifact_id != artifact.id:
                raise ArtifactError(400, "Pinned revision not found for this artifact")
            pinned_number = pinned.number
        if data.expires_at is not None and _aware(data.expires_at) <= now:
            raise ArtifactError(422, "Expiry must be in the future")

        token = secrets.token_urlsafe(32)
        code = f"{secrets.randbelow(10_000):04d}" if data.with_code else None
        link = ArtifactLink(
            artifact_id=artifact.id,
            token_hash=hash_token(token),
            token_prefix=token[:TOKEN_PREFIX_LENGTH],
            token_encrypted=encrypt_field(token),
            label=(data.label or "").strip() or None,
            expires_at=data.expires_at,
            code_encrypted=encrypt_field(code) if code else None,
            pinned_revision_id=data.pinned_revision_id,
            created_by=user.id,
            created_at=now,
        )
        db.add(link)
        try:
            db.commit()
        except IntegrityError as exc:
            # The pinned revision was deleted between the check and the insert.
            db.rollback()
            raise ArtifactError(409, "The pinned revision no longer exists") from exc
        db.refresh(link)
        public = ArtifactService.link_public(link, pinned_number, now)
        return ArtifactLinkCreated(
            **public.model_dump(exclude={"url"}), token=token, url=link_page_url(token)
        )

    @staticmethod
    def revoke(db: Session, user: User, artifact_id: uuid.UUID, link_id: uuid.UUID) -> None:
        artifact = ArtifactService.get_managed(db, artifact_id, user)
        link = db.get(ArtifactLink, link_id)
        if link is None or link.artifact_id != artifact.id:
            raise not_found("Link")
        if link.revoked_at is None:
            link.revoked_at = datetime.now(UTC)
            db.add(link)
            db.commit()

    # ── Public side ────────────────────────────────────────────────

    @staticmethod
    def _find(db: Session, token: str, *, lock: bool = False) -> tuple[ArtifactLink, Artifact]:
        """The link and its artifact; 404 for unknown, revoked, or a deleted artifact."""
        if not token or len(token) > 256:
            raise not_found("Link")
        stmt = select(ArtifactLink).where(ArtifactLink.token_hash == hash_token(token))
        if lock:
            stmt = stmt.with_for_update()
        link = db.exec(stmt).first()
        if link is None or link.revoked_at is not None:
            raise not_found("Link")
        artifact = db.get(Artifact, link.artifact_id)
        if artifact is None or not ArtifactAccess.owner_valid(db, artifact):
            raise not_found("Link")
        return link, artifact

    @classmethod
    def info(cls, db: Session, token: str) -> ArtifactLinkInfo:
        link, artifact = cls._find(db, token)
        return ArtifactLinkInfo(
            title=artifact.title,
            description=artifact.description,
            owner_name=owner_display_name(db.get(User, artifact.owner_id)),
            source_kind=artifact.source_kind,
            needs_code=link.code_encrypted is not None,
            expired=_is_expired(link, datetime.now(UTC)),
            blocked=_is_blocked(link),
        )

    @classmethod
    def open(cls, db: Session, token: str, code: str | None) -> ArtifactViewPublic:
        link, artifact = cls._find(db, token, lock=True)
        if not ArtifactAccess.link_serves(db, link, artifact):
            raise ArtifactError(410, "This link has expired")
        if link.code_encrypted is not None:
            if _is_blocked(link):
                raise _refused(BLOCKED_MESSAGE, link)
            if not code:
                raise _refused("A code is required to open this link", link)
            # Anything that is not the code — including non-ASCII or malformed
            # input — is a counted wrong attempt (compare bytes, never raise).
            if not secrets.compare_digest(
                code.strip().encode(), decrypt_field(link.code_encrypted).encode()
            ):
                link.failed_attempts += 1
                db.add(link)
                db.commit()
                if _is_blocked(link):
                    raise _refused(BLOCKED_MESSAGE, link)
                raise _refused("Wrong code", link)
        revision_id = link.pinned_revision_id or artifact.current_revision_id
        revision = db.get(ArtifactRevision, revision_id) if revision_id else None
        if revision is None:
            raise not_found("Link")
        # Release the row lock before returning.
        db.commit()
        return ArtifactService.build_view(artifact, revision, artifact_cap.via_link(link.id))
