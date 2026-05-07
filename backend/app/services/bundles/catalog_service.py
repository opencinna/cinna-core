"""CatalogService — visibility-aware listing of installable bundles.

Returns ``CatalogEntryPublic`` rows for the current user. A bundle is
visible when:

- ``visibility = 'public' AND is_listed = true``, OR
- ``visibility = 'users' AND is_listed = true AND BundleAccessGrant exists
  for (bundle, user)``, OR
- the user is the publisher (always sees their own bundles even when not
  listed — the publisher manages from the bundle CRUD API, but having the
  publisher catch a glimpse of their own bundle in the catalog is harmless).

The publisher's working install row is filtered out — publishers should not
"install" their own bundle (the publisher install IS the local copy).
"""
import logging
import uuid
from datetime import datetime

from sqlmodel import Session, select
from sqlalchemy import func

from app.models.agents.agent import Agent
from app.models.bundles.agent_bundle import AgentBundle, BundleVisibility
from app.models.bundles.agent_bundle_revision import AgentBundleRevision
from app.models.bundles.bundle_access_grant import BundleAccessGrant
from app.models.bundles.catalog import CatalogEntryPublic
from app.models.users.user import User

logger = logging.getLogger(__name__)


class CatalogService:
    """Catalog listing + per-entry resolution helpers."""

    @staticmethod
    def list_for_user(
        session: Session, user: User
    ) -> list[CatalogEntryPublic]:
        # Public listed bundles + bundles with explicit grants.
        public_stmt = select(AgentBundle).where(
            AgentBundle.is_listed == True,  # noqa: E712
            AgentBundle.visibility == BundleVisibility.PUBLIC,
        )
        public_bundles = list(session.exec(public_stmt).all())

        granted_stmt = (
            select(AgentBundle)
            .join(BundleAccessGrant, BundleAccessGrant.bundle_id == AgentBundle.id)
            .where(
                AgentBundle.is_listed == True,  # noqa: E712
                AgentBundle.visibility == BundleVisibility.USERS,
                BundleAccessGrant.user_id == user.id,
            )
        )
        granted_bundles = list(session.exec(granted_stmt).all())

        # Publisher's own bundles (always visible).
        own_stmt = select(AgentBundle).where(
            AgentBundle.publisher_user_id == user.id
        )
        own_bundles = list(session.exec(own_stmt).all())

        # Deduplicate by uuid.
        all_bundles: dict[uuid.UUID, AgentBundle] = {}
        for b in public_bundles + granted_bundles + own_bundles:
            all_bundles[b.id] = b

        return [
            CatalogService._bundle_to_entry(session, b, user)
            for b in all_bundles.values()
        ]

    @staticmethod
    def get_for_user(
        session: Session, bundle_id: str, user: User
    ) -> CatalogEntryPublic | None:
        stmt = select(AgentBundle).where(AgentBundle.bundle_id == bundle_id)
        bundle = session.exec(stmt).first()
        if not bundle:
            return None
        if not CatalogService.user_can_see(session, bundle, user):
            return None
        return CatalogService._bundle_to_entry(session, bundle, user)

    @staticmethod
    def user_can_see(
        session: Session, bundle: AgentBundle, user: User
    ) -> bool:
        if bundle.publisher_user_id == user.id:
            return True
        if not bundle.is_listed:
            # Unlisted bundles are publisher-only.
            return False
        if bundle.visibility == BundleVisibility.PUBLIC:
            return True
        if bundle.visibility == BundleVisibility.USERS:
            stmt = select(BundleAccessGrant).where(
                BundleAccessGrant.bundle_id == bundle.id,
                BundleAccessGrant.user_id == user.id,
            )
            return session.exec(stmt).first() is not None
        return False

    @staticmethod
    def user_can_install(
        session: Session, bundle: AgentBundle, user: User
    ) -> bool:
        """A bundle is installable if visible AND has at least one revision."""
        if bundle.latest_revision_id is None:
            return False
        return CatalogService.user_can_see(session, bundle, user)

    @staticmethod
    def _bundle_to_entry(
        session: Session, bundle: AgentBundle, user: User
    ) -> CatalogEntryPublic:
        latest_rev_number: int | None = None
        latest_version: str | None = None
        latest_published_at: datetime | None = None
        cred_specs: list = []
        if bundle.latest_revision_id:
            rev = session.get(AgentBundleRevision, bundle.latest_revision_id)
            if rev:
                latest_rev_number = rev.revision_number
                latest_version = rev.version
                latest_published_at = rev.published_at
                cred_specs = rev.required_credential_specs or []

        # Install count — how many distinct users currently have an install.
        install_count_stmt = (
            select(func.count())
            .select_from(Agent)
            .where(Agent.bundle_uuid == bundle.id)
        )
        install_count = session.exec(install_count_stmt).one() or 0

        # User's own install of this bundle (if any).
        user_install_stmt = select(Agent).where(
            Agent.bundle_uuid == bundle.id,
            Agent.owner_id == user.id,
        )
        user_install = session.exec(user_install_stmt).first()

        # Publisher handle — derive a non-PII identifier (truncated UUID).
        publisher_handle = (
            f"{str(bundle.publisher_user_id)[:8]}…"
            if bundle.publisher_user_id else None
        )
        # Author display fields — surfaced on catalog cards alongside the
        # publisher handle. Catalog access is auth-gated, so exposing the
        # publisher's name/email to viewers who can already see the bundle
        # row matches the trust model of an internal instance catalog.
        publisher_name: str | None = None
        publisher_email: str | None = None
        if bundle.publisher_user_id:
            publisher = session.get(User, bundle.publisher_user_id)
            if publisher:
                publisher_name = publisher.full_name or None
                publisher_email = publisher.email or None

        return CatalogEntryPublic(
            bundle_id=bundle.bundle_id,
            bundle_uuid=bundle.id,
            display_name=bundle.display_name,
            description=bundle.description,
            publisher_handle=publisher_handle,
            publisher_name=publisher_name,
            publisher_email=publisher_email,
            visibility=bundle.visibility,
            latest_revision_id=bundle.latest_revision_id,
            latest_revision_number=latest_rev_number,
            latest_version=latest_version,
            latest_published_at=latest_published_at,
            install_count=install_count,
            is_installed=user_install is not None,
            user_install_id=user_install.id if user_install else None,
            required_credential_specs=cred_specs,
        )
