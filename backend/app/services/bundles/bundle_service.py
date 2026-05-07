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
import uuid
from datetime import datetime, UTC

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
                raise ValueError(f"Invalid visibility: {update_dict['visibility']}")
        if "default_install_mode" in update_dict and update_dict["default_install_mode"] is not None:
            if update_dict["default_install_mode"] not in _VALID_INSTALL_MODES:
                raise ValueError(
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
    def delete_bundle(session: Session, bundle: AgentBundle) -> None:
        """Delete the bundle (cascades revisions + grants).

        Refuses if any foreign install (non-publisher) still references the
        bundle. The publisher install's ``bundle_uuid`` is set to NULL by the
        FK ``ON DELETE SET NULL`` automatically.
        """
        foreign = BundleService.foreign_install_count(session, bundle)
        if foreign > 0:
            raise ValueError(
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
            raise ValueError("Grant not found for this bundle")
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
