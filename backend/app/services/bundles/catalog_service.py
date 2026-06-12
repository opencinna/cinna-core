"""CatalogService — visibility-aware listing of installable bundles.

Returns ``CatalogEntryPublic`` rows for the current user. A bundle is
visible when:

- ``visibility = 'public' AND is_listed = true``, OR
- ``visibility = 'users' AND is_listed = true AND BundleAccessGrant exists
  for (bundle, user)``, OR
- the user is the publisher (always sees their own bundles even when not
  listed — the publisher manages from the bundle CRUD API, but having the
  publisher catch a glimpse of their own bundle in the catalog is harmless).

The publisher's working install row (``is_publisher_install=True``) is the
dev / source copy and is NOT counted as a consumer install — the catalog
reports ``is_installed=False`` for the publisher's own bundle until they
install it as a consumer to dogfood it. Publishers still see their own
bundles in the catalog listing (so they can install them); the
``is_installed`` flag and the "Open" button only flip on for the consumer
install row.
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
from app.models.bundles.catalog import (
    CatalogEntryPublic,
    CatalogInstallContext,
    InstallContextAIPublisherSummaries,
    InstallContextPublisherSummary,
    InstallContextSpec,
)
from app.models.credentials.ai_credential import AICredential
from app.models.credentials.credential import Credential
from app.models.users.user import User
from app.services.bundles.credential_spec import parse_credential_spec

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

        # Install count — how many distinct users currently have a consumer
        # install. Publisher installs (the publisher's dev / source copy) are
        # excluded so the count matches the semantics of ``is_installed``
        # below: the catalog represents consumer installs only.
        install_count_stmt = (
            select(func.count())
            .select_from(Agent)
            .where(
                Agent.bundle_uuid == bundle.id,
                Agent.is_publisher_install == False,  # noqa: E712
            )
        )
        install_count = session.exec(install_count_stmt).one() or 0

        # User's own install of this bundle (if any). The publisher install
        # (``is_publisher_install=True``) is the dev / source copy and is
        # explicitly excluded — the catalog represents consumer installs,
        # so the publisher's own bundle should report ``is_installed=False``
        # until they install it as a consumer to dogfood it.
        user_install_stmt = select(Agent).where(
            Agent.bundle_uuid == bundle.id,
            Agent.owner_id == user.id,
            Agent.is_publisher_install == False,  # noqa: E712
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
            user_install_pending_update=(
                bool(user_install.pending_update) if user_install else False
            ),
            required_credential_specs=cred_specs,
            publisher_ai_credential_conversation_id=(
                bundle.publisher_ai_credential_conversation_id
            ),
            publisher_ai_credential_building_id=(
                bundle.publisher_ai_credential_building_id
            ),
        )

    @staticmethod
    def build_install_context(
        session: Session, bundle: AgentBundle, user: User
    ) -> CatalogInstallContext:
        """Build the per-user install context for the install page.

        Resolves:
          - The catalog entry (visibility-aware; assumes the caller has
            already gated on ``user_can_install``).
          - Whether the publisher provides AI credentials and a friendly
            ``{name, type}`` summary of those rows (no secrets).
          - For every entry of ``required_credential_specs``: the spec
            metadata plus an auto-prefill suggestion picked from the
            user's owned + shared credentials (case-insensitive
            ``(name, type)`` match — pure suggestion, never auto-commit).

        The publisher-provides-AI summaries are best-effort: a missing AI
        credential row (race against deletion, FK ``SET NULL``) yields
        ``None`` for that side of the summary; the bundle FKs themselves
        already nulled out via ``ON DELETE SET NULL``.
        """
        from app.services.credentials.credentials_service import CredentialsService

        entry = CatalogService._bundle_to_entry(session, bundle, user)

        # Publisher AI credential summaries.
        ai_provided_by_publisher = (
            bundle.publisher_ai_credential_conversation_id is not None
            or bundle.publisher_ai_credential_building_id is not None
        )
        ai_summaries = InstallContextAIPublisherSummaries()
        if bundle.publisher_ai_credential_conversation_id is not None:
            conv = session.get(
                AICredential, bundle.publisher_ai_credential_conversation_id
            )
            if conv is not None:
                ai_summaries.conversation = InstallContextPublisherSummary(
                    name=conv.name,
                    type=conv.type.value if hasattr(conv.type, "value") else str(conv.type),
                )
        if bundle.publisher_ai_credential_building_id is not None:
            build = session.get(
                AICredential, bundle.publisher_ai_credential_building_id
            )
            if build is not None:
                ai_summaries.building = InstallContextPublisherSummary(
                    name=build.name,
                    type=build.type.value if hasattr(build.type, "value") else str(build.type),
                )

        # Resolve the latest revision specs (already mirrored on the
        # entry, but we want full spec dicts to read provided_by /
        # publisher_credential_id). Read off the revision row directly.
        revision = (
            session.get(AgentBundleRevision, bundle.latest_revision_id)
            if bundle.latest_revision_id
            else None
        )
        raw_specs: list = []
        if revision is not None:
            raw_specs = revision.required_credential_specs or []

        service_specs: list[InstallContextSpec] = []
        for raw_spec in raw_specs:
            parsed = parse_credential_spec(raw_spec)
            if parsed is None:
                continue

            publisher_summary: InstallContextPublisherSummary | None = None
            suggested_id: uuid.UUID | None = None
            suggested_name: str | None = None

            # A publisher-marked-private service_uri is treated as not-shared:
            # it is neither used for matching nor shown to the installer,
            # mirroring _materialise_template_credential's gating. Only PBT
            # specs ever populate template_private_fields, so PBU/publisher
            # specs keep their full service_uri (empty list → unchanged).
            effective_service_uri = (
                None
                if "service_uri" in parsed.template_private_fields
                else parsed.service_uri
            )

            if (
                parsed.provided_by == "publisher"
                and parsed.publisher_credential_id is not None
            ):
                pub_cred = session.get(Credential, parsed.publisher_credential_id)
                if pub_cred is not None:
                    cred_type_value = (
                        pub_cred.type.value
                        if hasattr(pub_cred.type, "value")
                        else str(pub_cred.type)
                    )
                    publisher_summary = InstallContextPublisherSummary(
                        name=pub_cred.name,
                        type=cred_type_value,
                    )
            else:
                # PBU and PBT specs both run the auto-prefill matcher.
                # For PBT this lets a reinstall reuse the previously-
                # materialised template credential (which has the spec's
                # name/type) instead of creating a duplicate every time;
                # the installer can still opt to materialise a fresh
                # template-derived row by picking "Create from template"
                # in the UI. PBT also carries the private-field list so
                # the form can render the post-install setup hint.
                if parsed.provided_by == "template":
                    match = CredentialsService.find_match_for_spec(
                        session=session,
                        user_id=user.id,
                        name=parsed.name,
                        credential_type=parsed.type,
                        template_data=parsed.template_data,
                        template_private_fields=parsed.template_private_fields,
                        service_uri=effective_service_uri,
                    )
                else:
                    match = CredentialsService.find_match_for_spec(
                        session=session,
                        user_id=user.id,
                        name=parsed.name,
                        credential_type=parsed.type,
                        service_uri=effective_service_uri,
                    )
                if match is not None:
                    suggested_id = match.id
                    suggested_name = match.name

            service_specs.append(
                InstallContextSpec(
                    name=parsed.name,
                    type=parsed.type,
                    description=parsed.description,
                    provided_by=parsed.provided_by,
                    publisher_summary=publisher_summary,
                    suggested_credential_id=suggested_id,
                    suggested_credential_name=suggested_name,
                    template_private_fields=parsed.template_private_fields,
                    service_uri=effective_service_uri,
                )
            )

        return CatalogInstallContext(
            bundle=entry,
            ai_provided_by_publisher=ai_provided_by_publisher,
            ai_publisher_credential_summaries=ai_summaries,
            service_specs=service_specs,
        )
