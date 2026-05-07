"""BundleService — CRUD for ``AgentBundle`` rows.

The bundle row is the canonical metadata record for a published agent.
Creation happens implicitly on first publish (see ``PublishService``);
this service handles list/read/update/delete only.

Per the plan, deleting a bundle is allowed only when no foreign installs
reference it (the publisher's own install is the only remaining install).
A future "force delete" admin path will orphan all installs, but Phase 2
ships with the safe behaviour.
"""
import logging
import shutil
import uuid
from datetime import datetime, UTC
from pathlib import Path

from sqlmodel import Session, select
from sqlalchemy import func

from app.models.agents.agent import Agent
from app.models.bundles.agent_bundle import (
    AgentBundle,
    AgentBundleUpdate,
    BundleVisibility,
    BundleInstallMode,
)
from app.models.bundles.agent_bundle_revision import AgentBundleRevision
from app.models.bundles.bundle_access_grant import BundleAccessGrant
from app.models.users.user import User
from app.services.bundles.exceptions import (
    BundleAccessDeniedError,
    BundleConflictError,
    BundleNotFoundError,
    BundleValidationError,
    GrantNotFoundError,
    RevisionInUseError,
    RevisionNotFoundError,
)

logger = logging.getLogger(__name__)


_VALID_VISIBILITIES = {
    BundleVisibility.PRIVATE,
    BundleVisibility.USERS,
    BundleVisibility.PUBLIC,
}
_VALID_INSTALL_MODES = {BundleInstallMode.MANUAL, BundleInstallMode.AUTOMATIC}


class BundleService:
    """CRUD operations for ``AgentBundle`` rows."""

    # ── Read ───────────────────────────────────────────────────────

    @staticmethod
    def get_bundle_by_uuid(session: Session, bundle_uuid: uuid.UUID) -> AgentBundle | None:
        return session.get(AgentBundle, bundle_uuid)

    @staticmethod
    def get_bundle_by_id(session: Session, bundle_id: str) -> AgentBundle | None:
        stmt = select(AgentBundle).where(AgentBundle.bundle_id == bundle_id)
        return session.exec(stmt).first()

    @staticmethod
    def get_for_publisher(
        session: Session, bundle_uuid: uuid.UUID, user: User
    ) -> AgentBundle:
        """Resolve a bundle for the given publisher (or any superuser).

        Raises:
            BundleNotFoundError: bundle does not exist.
            BundleAccessDeniedError: caller is neither the publisher nor a
                superuser.
        """
        bundle = BundleService.get_bundle_by_uuid(session, bundle_uuid)
        if bundle is None:
            raise BundleNotFoundError("Bundle not found")
        if bundle.publisher_user_id != user.id and not user.is_superuser:
            raise BundleAccessDeniedError("Not your bundle")
        return bundle

    @staticmethod
    def list_publisher_bundles(
        session: Session, publisher_user_id: uuid.UUID
    ) -> list[AgentBundle]:
        stmt = (
            select(AgentBundle)
            .where(AgentBundle.publisher_user_id == publisher_user_id)
            .order_by(AgentBundle.created_at.desc())
        )
        return list(session.exec(stmt).all())

    @staticmethod
    def install_count(session: Session, bundle_uuid: uuid.UUID) -> int:
        """Count Agent rows currently linked to the bundle (including publisher)."""
        stmt = (
            select(func.count())
            .select_from(Agent)
            .where(Agent.bundle_uuid == bundle_uuid)
        )
        return session.exec(stmt).one()

    @staticmethod
    def foreign_install_count(session: Session, bundle: AgentBundle) -> int:
        """Count installs **other than** the publisher's working install."""
        stmt = (
            select(func.count())
            .select_from(Agent)
            .where(
                Agent.bundle_uuid == bundle.id,
                Agent.is_publisher_install == False,  # noqa: E712
            )
        )
        return session.exec(stmt).one()

    @staticmethod
    def latest_revision(
        session: Session, bundle: AgentBundle
    ) -> AgentBundleRevision | None:
        if not bundle.latest_revision_id:
            return None
        return session.get(AgentBundleRevision, bundle.latest_revision_id)

    @staticmethod
    def revision_install_count(
        session: Session, revision_id: uuid.UUID
    ) -> int:
        """Count installs whose ``installed_revision_id`` is this revision."""
        stmt = (
            select(func.count())
            .select_from(Agent)
            .where(Agent.installed_revision_id == revision_id)
        )
        return session.exec(stmt).one()

    @staticmethod
    def list_revisions_with_install_counts(
        session: Session, bundle: AgentBundle
    ) -> list[tuple[AgentBundleRevision, int]]:
        """Return ``(revision, install_count)`` pairs ordered newest-first.

        Single aggregated query — replaces the per-revision count loop.
        """
        revisions_stmt = (
            select(AgentBundleRevision)
            .where(AgentBundleRevision.bundle_id == bundle.id)
            .order_by(AgentBundleRevision.revision_number.desc())
        )
        revisions = list(session.exec(revisions_stmt).all())
        if not revisions:
            return []
        counts_stmt = (
            select(Agent.installed_revision_id, func.count())
            .where(
                Agent.installed_revision_id.in_(  # type: ignore[union-attr]
                    [r.id for r in revisions]
                )
            )
            .group_by(Agent.installed_revision_id)
        )
        counts: dict = dict(session.exec(counts_stmt).all())
        return [(r, counts.get(r.id, 0)) for r in revisions]

    # ── Write ──────────────────────────────────────────────────────

    @staticmethod
    def create_bundle(
        session: Session,
        bundle_id: str,
        publisher_user_id: uuid.UUID,
        display_name: str,
        description: str | None = None,
    ) -> AgentBundle:
        """Create a new ``AgentBundle`` row.

        Used by ``PublishService`` on first publish. Callers are responsible
        for re-using an existing bundle (looked up by ``bundle_id``) before
        invoking this — duplicate ``bundle_id`` raises an integrity error.
        """
        bundle = AgentBundle(
            bundle_id=bundle_id,
            publisher_user_id=publisher_user_id,
            display_name=display_name,
            description=description,
        )
        session.add(bundle)
        session.commit()
        session.refresh(bundle)
        logger.info(
            "Created AgentBundle id=%s bundle_id=%s publisher=%s",
            bundle.id, bundle_id, publisher_user_id,
        )
        return bundle

    @staticmethod
    def update_bundle(
        session: Session, bundle: AgentBundle, data: AgentBundleUpdate
    ) -> AgentBundle:
        update_dict = data.model_dump(exclude_unset=True)

        if "visibility" in update_dict and update_dict["visibility"] is not None:
            if update_dict["visibility"] not in _VALID_VISIBILITIES:
                raise BundleValidationError(
                    f"Invalid visibility: {update_dict['visibility']}"
                )
        if "default_install_mode" in update_dict and update_dict["default_install_mode"] is not None:
            if update_dict["default_install_mode"] not in _VALID_INSTALL_MODES:
                raise BundleValidationError(
                    f"Invalid default_install_mode: {update_dict['default_install_mode']}"
                )

        for k, v in update_dict.items():
            if v is not None:
                setattr(bundle, k, v)
        bundle.updated_at = datetime.now(UTC)
        session.add(bundle)
        session.commit()
        session.refresh(bundle)
        return bundle

    @staticmethod
    def delete_revision(
        session: Session,
        bundle: AgentBundle,
        revision_id: uuid.UUID,
    ) -> None:
        """Delete a single revision when no foreign install references it.

        Refuses if any non-publisher install has ``installed_revision_id``
        pointing at this revision. The publisher's working install is allowed
        to be on the revision: its ``installed_revision_id`` is set to NULL
        (it can be re-snapshotted on the next publish). The on-disk snapshot
        tree at ``revision.snapshot_path`` is removed best-effort after the
        DB transaction commits.

        When the last revision is removed, the empty ``AgentBundle`` row is
        also deleted in the same transaction. The publisher install's
        ``bundle_uuid`` reverts to NULL via the FK ``ON DELETE SET NULL``
        cascade — re-publishing creates a fresh bundle, and (until a new
        revision is published) the bundle ID is editable again.

        Raises:
            RevisionNotFoundError: revision missing or not on this bundle.
            RevisionInUseError: at least one foreign install still references
                the revision.
        """
        revision = session.get(AgentBundleRevision, revision_id)
        if revision is None or revision.bundle_id != bundle.id:
            raise RevisionNotFoundError("Revision not found for this bundle")

        foreign_stmt = (
            select(func.count())
            .select_from(Agent)
            .where(
                Agent.installed_revision_id == revision.id,
                Agent.is_publisher_install == False,  # noqa: E712
            )
        )
        foreign_count = session.exec(foreign_stmt).one()
        if foreign_count > 0:
            raise RevisionInUseError(
                f"Cannot delete revision {revision.revision_number}: "
                f"{foreign_count} foreign install(s) still reference it."
            )

        # Snapshot identifying fields before the ORM row is invalidated by
        # the commit below — we use them for filesystem cleanup and logging.
        snapshot_path = revision.snapshot_path
        revision_number = revision.revision_number
        bundle_id_str = bundle.bundle_id
        bundle_uuid = bundle.id

        # Detach any publisher install(s) sitting on this revision.
        publisher_installs_stmt = select(Agent).where(
            Agent.installed_revision_id == revision.id
        )
        for install in session.exec(publisher_installs_stmt).all():
            install.installed_revision_id = None
            session.add(install)

        # Re-point bundle.latest_revision_id only when there is a replacement.
        # When there isn't, the FK ``ON DELETE SET NULL`` on
        # ``agent_bundle.latest_revision_id`` will clear the pointer — and the
        # bundle row is about to be deleted below anyway.
        if bundle.latest_revision_id == revision.id:
            replacement_stmt = (
                select(AgentBundleRevision)
                .where(
                    AgentBundleRevision.bundle_id == bundle.id,
                    AgentBundleRevision.id != revision.id,
                )
                .order_by(AgentBundleRevision.revision_number.desc())
                .limit(1)
            )
            replacement = session.exec(replacement_stmt).first()
            if replacement is not None:
                bundle.latest_revision_id = replacement.id
                bundle.updated_at = datetime.now(UTC)
                session.add(bundle)

        session.delete(revision)

        # Auto-unpublish: when this is the last revision, drop the bundle row
        # in the same transaction. ``session.flush()`` lets the revision
        # delete clear ``bundle.latest_revision_id`` via the FK first, so the
        # bundle delete doesn't trip integrity errors.
        session.flush()
        remaining = session.exec(
            select(func.count())
            .select_from(AgentBundleRevision)
            .where(AgentBundleRevision.bundle_id == bundle_uuid)
        ).one()
        bundle_was_unpublished = remaining == 0
        if bundle_was_unpublished:
            session.delete(bundle)

        session.commit()

        # Best-effort filesystem cleanup; failure is logged but not raised so
        # the DB delete still wins.
        if snapshot_path:
            try:
                shutil.rmtree(Path(snapshot_path), ignore_errors=True)
            except Exception as e:  # pragma: no cover — defensive
                logger.warning(
                    "Failed to clean up snapshot dir %s: %s", snapshot_path, e
                )

        logger.info(
            "Deleted AgentBundleRevision id=%s number=%s bundle_id=%s",
            revision_id,
            revision_number,
            bundle_id_str,
        )
        if bundle_was_unpublished:
            logger.info(
                "Auto-deleted empty AgentBundle id=%s bundle_id=%s "
                "(last revision removed)",
                bundle_uuid,
                bundle_id_str,
            )

    @staticmethod
    def delete_bundle(session: Session, bundle: AgentBundle) -> None:
        """Delete the bundle (cascades revisions + grants).

        Refuses if any foreign install (non-publisher) still references the
        bundle. The publisher install's ``bundle_uuid`` is set to NULL by the
        FK ``ON DELETE SET NULL`` automatically.
        """
        foreign = BundleService.foreign_install_count(session, bundle)
        if foreign > 0:
            raise BundleConflictError(
                f"Cannot delete bundle with {foreign} dependent install(s). "
                "Have those users uninstall first."
            )
        session.delete(bundle)
        session.commit()
        logger.info("Deleted AgentBundle id=%s bundle_id=%s", bundle.id, bundle.bundle_id)

    # ── Access grants ──────────────────────────────────────────────

    @staticmethod
    def list_grants(session: Session, bundle: AgentBundle) -> list[BundleAccessGrant]:
        stmt = (
            select(BundleAccessGrant)
            .where(BundleAccessGrant.bundle_id == bundle.id)
            .order_by(BundleAccessGrant.created_at.desc())
        )
        return list(session.exec(stmt).all())

    @staticmethod
    def grant_access(
        session: Session,
        bundle: AgentBundle,
        target_user: User,
        granted_by_user_id: uuid.UUID,
    ) -> BundleAccessGrant:
        # Idempotent: return existing grant if already present.
        stmt = select(BundleAccessGrant).where(
            BundleAccessGrant.bundle_id == bundle.id,
            BundleAccessGrant.user_id == target_user.id,
        )
        existing = session.exec(stmt).first()
        if existing:
            return existing
        grant = BundleAccessGrant(
            bundle_id=bundle.id,
            user_id=target_user.id,
            granted_by_user_id=granted_by_user_id,
        )
        session.add(grant)
        session.commit()
        session.refresh(grant)
        return grant

    @staticmethod
    def revoke_grant(
        session: Session, bundle: AgentBundle, grant_id: uuid.UUID
    ) -> None:
        grant = session.get(BundleAccessGrant, grant_id)
        if not grant or grant.bundle_id != bundle.id:
            raise GrantNotFoundError("Grant not found for this bundle")
        session.delete(grant)
        session.commit()

    @staticmethod
    def user_has_grant(
        session: Session, bundle: AgentBundle, user_id: uuid.UUID
    ) -> bool:
        stmt = select(BundleAccessGrant).where(
            BundleAccessGrant.bundle_id == bundle.id,
            BundleAccessGrant.user_id == user_id,
        )
        return session.exec(stmt).first() is not None
