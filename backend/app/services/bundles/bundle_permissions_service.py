"""Bundle permissions overview orchestrator.

Assembles the read-only aggregate that backs the unified **Permissions
management** card on the Bundle tab. It unions two already-modeled,
independently owner-gated systems:

- Bundle catalog access (``BundleAccessGrant``) — active when
  ``bundle.visibility == "users"``.
- Producer Agent REST API per-user capability scopes
  (``agent_api_access_grant``) — one column per identity-enabled connected
  producer.

The cross-domain assembly lives here so the route stays thin.

Security invariants (mirrored from the plan §4):
- The owner-gated reads (``AgentApiGrantService.list_grants`` /
  ``.get_scope_catalog``) run **only** for producers where ``can_manage`` is
  true (caller owns the producer or is a superuser). For non-manageable
  producers the overview returns ``grants=[]`` / ``scope_catalog=[]`` and
  surfaces a read-only "Managed by <owner_email>" entry — so a publisher can
  never learn another owner's scope state through this surface.
- The response carries no secrets; the ``agent_api`` credential ``token`` is
  never read (only ``producer_agent_id`` is decrypted, inside
  ``list_connected_producers``).
"""
import logging
import uuid

from sqlmodel import Session, select

from app.models import (
    Agent,
    BundlePermissionGrant,
    BundlePermissionProducer,
    BundlePermissionScopeCatalogEntry,
    BundlePermissionsOverview,
    BundlePermissionUser,
    User,
)
from app.services.agent_api.agent_api_grant_service import AgentApiGrantService
from app.services.agent_api.agent_api_token_service import AgentApiTokenService
from app.services.bundles.bundle_service import BundleService

logger = logging.getLogger(__name__)


class BundlePermissionsService:
    """Read-only aggregator for the unified Permissions management card."""

    @staticmethod
    def build_overview(
        session: Session,
        install: Agent,
        current_user: User,
    ) -> BundlePermissionsOverview:
        """Assemble the full permissions overview for a publisher install."""
        # 1. Resolve the bundle + visibility.
        bundle = (
            BundleService.get_bundle_by_uuid(session, install.bundle_uuid)
            if install.bundle_uuid is not None
            else None
        )
        visibility = bundle.visibility if bundle is not None else None
        bundle_access_applicable = visibility == "users"

        # 2. Bundle grant rows — only meaningful under "users" visibility.
        #    Projection is deferred until the user union is batch-resolved.
        bundle_grant_rows = (
            BundleService.list_grants(session, bundle)
            if bundle_access_applicable and bundle is not None
            else []
        )
        # Map of user_id → bundle_grant_id, used to stamp the users union.
        bundle_grant_by_user: dict[uuid.UUID, uuid.UUID] = {
            grant.user_id: grant.id for grant in bundle_grant_rows
        }

        # 3. Connected, identity-enabled producers.
        producer_rows = AgentApiTokenService.list_connected_producers(
            session,
            install.id,
            current_user.id,
            current_user.is_superuser,
        )

        # Users referenced by any manageable producer grant — resolved once at
        # the end into the union.
        producer_grant_user_ids: set[uuid.UUID] = set()

        producers: list[BundlePermissionProducer] = []
        for row in producer_rows:
            grants: list[BundlePermissionGrant] = []
            scope_catalog: list[BundlePermissionScopeCatalogEntry] = []

            if row.can_manage:
                # Owner-gated reads — only run because can_manage implies the
                # caller owns the producer (or is a superuser).
                for grant in AgentApiGrantService.list_grants(
                    session,
                    row.producer_agent_id,
                    current_user.id,
                    current_user.is_superuser,
                ):
                    grants.append(
                        BundlePermissionGrant(
                            user_id=grant.user_id,
                            grant_id=grant.id,
                            scopes=[str(s) for s in (grant.scopes or [])],
                        )
                    )
                    producer_grant_user_ids.add(grant.user_id)

                catalog = AgentApiGrantService.get_scope_catalog(
                    session,
                    row.producer_agent_id,
                    current_user.id,
                    current_user.is_superuser,
                )
                scope_catalog = [
                    BundlePermissionScopeCatalogEntry(
                        name=scope.name, description=scope.description
                    )
                    for scope in catalog.scopes
                ]

            producers.append(
                BundlePermissionProducer(
                    producer_agent_id=row.producer_agent_id,
                    producer_agent_name=row.producer_agent_name,
                    producer_ui_color_preset=row.producer_ui_color_preset,
                    credential_id=row.credential_id,
                    credential_name=row.credential_name,
                    identity_enabled=row.identity_enabled,
                    can_manage=row.can_manage,
                    owner_email=row.owner_email,
                    scope_catalog=scope_catalog,
                    grants=grants,
                )
            )

        # 4. Users union — bundle grant users + every manageable producer's
        #    grant users. Resolve all referenced User rows in one query.
        union_user_ids = set(bundle_grant_by_user) | producer_grant_user_ids
        users_by_id: dict[uuid.UUID, User] = {}
        if union_user_ids:
            users_by_id = {
                user.id: user
                for user in session.exec(
                    select(User).where(User.id.in_(union_user_ids))
                ).all()
            }

        users = [
            BundlePermissionUser(
                user_id=user_id,
                email=users_by_id[user_id].email if user_id in users_by_id else None,
                full_name=(
                    users_by_id[user_id].full_name if user_id in users_by_id else None
                ),
                bundle_grant_id=bundle_grant_by_user.get(user_id),
            )
            for user_id in union_user_ids
        ]

        # Project bundle grants from the same resolved-user cache (no extra
        # lookups). The shared helper keeps the projection identical to the
        # bundles route.
        bundle_grants = [
            BundleService.grant_to_public(
                session, grant, user=users_by_id.get(grant.user_id)
            )
            for grant in bundle_grant_rows
        ]

        return BundlePermissionsOverview(
            bundle_uuid=install.bundle_uuid,
            visibility=visibility,
            bundle_access_applicable=bundle_access_applicable,
            bundle_grants=bundle_grants,
            producers=producers,
            users=users,
            show_card=bundle_access_applicable or len(producers) > 0,
        )
