"""
Credential Share Service - Business logic for credential sharing operations.
"""
import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy import union
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, delete, func, select, and_

from app.models.agents.agent import Agent
from app.models.bundles.agent_bundle import AgentBundle
from app.models.catalog.catalog_local_install import CatalogLocalInstall
from app.models.credentials.credential import Credential, CredentialSkillUsage
from app.models.credentials.credential_share import (
    CredentialShare,
    CredentialShareAgentUsage,
    CredentialShareLocalSkillUsage,
    CredentialSharePublic,
    CredentialShareRevocation,
    SharedCredentialPublic,
)
from app.models.credentials.credential_group_share import (
    CredentialGroupShare,
    CredentialGroupSharePublic,
)
from app.models.credentials.link_models import AgentCredentialLink
from app.models.plugins.llm_plugin import AgentPluginLink, PluginSource
from app.models.skills.skill_package_revision import SkillPackageRevision
from app.models.users.user import User
from app.models.users.user_group import UserGroup, UserGroupMember, UserGroupRef
from app.services.credentials.credentials_service import CredentialsService
from app.services.users.user_group_access import UserGroupAccess

logger = logging.getLogger(__name__)


class CredentialGroupShareError(Exception):
    """A refused group-share operation; ``status_code`` is the HTTP answer."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class CredentialShareService:
    """
    Service for managing credential sharing between users.

    Responsibilities:
    - Share credentials with other users
    - Revoke credential shares
    - Query shares for credentials and users
    - Validate sharing permissions
    """

    @staticmethod
    def share_credential(
        session: Session,
        credential_id: UUID,
        owner_id: UUID,
        shared_with_email: str,
        source: str = "direct",
    ) -> CredentialSharePublic:
        """
        Share a credential with another user.

        Validations:
        - Credential must exist and be owned by owner_id
        - Credential must have allow_sharing=true
        - Target user must exist (by email)
        - Cannot share with yourself
        - Cannot create duplicate share

        ``source`` is the provenance marker stamped on the new
        ``CredentialShare`` ("direct" for user→user shares). It is set by the
        server at the call site, never from client input.

        Returns:
            CredentialSharePublic with resolved user info

        Raises:
            ValueError for validation failures
        """
        # Get credential and verify ownership
        credential = session.get(Credential, credential_id)
        if not credential:
            raise ValueError("Credential not found")
        if credential.owner_id != owner_id:
            raise ValueError("Not enough permissions to share this credential")

        # Check if sharing is allowed
        if not credential.allow_sharing:
            raise ValueError("This credential does not allow sharing. Enable sharing first.")

        # Find target user by email
        statement = select(User).where(User.email == shared_with_email)
        target_user = session.exec(statement).first()
        if not target_user:
            raise ValueError(f"User with email '{shared_with_email}' not found")

        # Cannot share with yourself
        if target_user.id == owner_id:
            raise ValueError("Cannot share a credential with yourself")

        # Check for duplicate share
        existing_share = session.exec(
            select(CredentialShare).where(
                and_(
                    CredentialShare.credential_id == credential_id,
                    CredentialShare.shared_with_user_id == target_user.id
                )
            )
        ).first()
        if existing_share:
            raise ValueError(f"Credential is already shared with {shared_with_email}")

        # Get owner info
        owner = session.get(User, owner_id)

        # Create the share
        share = CredentialShare(
            credential_id=credential_id,
            shared_with_user_id=target_user.id,
            shared_by_user_id=owner_id,
            access_level="read",
            source=source,
        )
        session.add(share)
        # The owner sharing again is the one thing that lifts their earlier
        # removal of this person's share.
        CredentialShareService.clear_revocation(
            session, credential_id=credential_id, user_id=target_user.id
        )
        session.commit()
        session.refresh(share)

        logger.info(
            f"Credential {credential_id} shared with user {target_user.id} "
            f"(email: {shared_with_email}) by owner {owner_id}"
        )

        return CredentialSharePublic(
            id=share.id,
            credential_id=share.credential_id,
            credential_name=credential.name,
            credential_type=credential.type.value,
            shared_with_user_id=target_user.id,
            shared_with_email=target_user.email,
            shared_by_user_id=owner_id,
            shared_by_email=owner.email if owner else "",
            shared_at=share.shared_at,
            access_level=share.access_level,
            source=share.source,
        )

    @staticmethod
    async def revoke_credential_share(
        session: Session,
        share_id: UUID,
        owner_id: UUID
    ) -> None:
        """
        Revoke a credential share.

        Validations:
        - Share must exist
        - Credential must be owned by owner_id

        Deletes the CredentialShare record, then unlinks the credential from the
        recipient's agents and re-syncs their running environments — a link
        outlives the share it was made under, and env materialisation reads the
        link table only, so leaving it would keep a working secret inside the
        recipient's containers.
        """
        share = session.get(CredentialShare, share_id)
        if not share:
            raise ValueError("Share not found")

        # Verify credential ownership
        credential = session.get(Credential, share.credential_id)
        if not credential:
            raise ValueError("Credential not found")
        if credential.owner_id != owner_id:
            raise ValueError("Not enough permissions to revoke this share")

        credential_id = share.credential_id
        recipient_id = share.shared_with_user_id

        session.delete(share)
        # Remember the owner's decision so no automatic path (provision-local,
        # a cloud bundle or skill install) shares it with this person again.
        CredentialShareService.record_revocation(
            session,
            credential_id=credential_id,
            user_id=recipient_id,
            revoked_by_user_id=owner_id,
        )
        session.commit()

        logger.info(
            f"Credential share {share_id} revoked by owner {owner_id}"
        )

        await CredentialsService.unlink_credential_from_revoked_recipients(
            session=session,
            credential_id=credential_id,
            recipient_user_ids=[recipient_id],
        )

    @staticmethod
    def get_shares_by_credential(
        session: Session,
        credential_id: UUID,
        owner_id: UUID
    ) -> list[CredentialSharePublic]:
        """
        Get all shares for a credential (for credential owner).

        Validations:
        - Credential must be owned by owner_id

        Returns list of shares with resolved user emails.
        """
        # Verify credential ownership
        credential = session.get(Credential, credential_id)
        if not credential:
            raise ValueError("Credential not found")
        if credential.owner_id != owner_id:
            raise ValueError("Not enough permissions to view shares for this credential")

        # Get owner info
        owner = session.get(User, owner_id)

        # Query all shares for this credential
        statement = select(CredentialShare).where(
            CredentialShare.credential_id == credential_id
        )
        shares = session.exec(statement).all()

        (
            agent_usages,
            direct_agent_counts,
            local_skill_usages,
        ) = CredentialShareService._recipient_usages(
            session,
            credential=credential,
            recipient_ids=[share.shared_with_user_id for share in shares],
        )

        result = []
        for share in shares:
            # Get shared_with user info
            shared_with_user = session.get(User, share.shared_with_user_id)
            result.append(CredentialSharePublic(
                id=share.id,
                credential_id=share.credential_id,
                credential_name=credential.name,
                credential_type=credential.type.value,
                shared_with_user_id=share.shared_with_user_id,
                shared_with_email=shared_with_user.email if shared_with_user else "",
                shared_by_user_id=share.shared_by_user_id,
                shared_by_email=owner.email if owner else "",
                shared_at=share.shared_at,
                access_level=share.access_level,
                source=share.source,
                agent_usages=agent_usages.get(share.shared_with_user_id, []),
                direct_agent_use_count=direct_agent_counts.get(
                    share.shared_with_user_id, 0
                ),
                local_skill_usages=local_skill_usages.get(
                    share.shared_with_user_id, []
                ),
            ))

        return result

    # ── Owner revocations ────────────────────────────────────────────────

    @staticmethod
    def record_revocation(
        session: Session,
        *,
        credential_id: UUID,
        user_id: UUID,
        revoked_by_user_id: UUID,
    ) -> None:
        """Record that the owner removed ``user_id``'s share. Idempotent and
        race-safe (``ON CONFLICT DO NOTHING``); never commits."""
        session.exec(
            pg_insert(CredentialShareRevocation)
            .values(
                id=uuid4(),
                credential_id=credential_id,
                user_id=user_id,
                revoked_by_user_id=revoked_by_user_id,
                created_at=datetime.now(timezone.utc),
            )
            .on_conflict_do_nothing(
                constraint="uq_credential_share_revocation_credential_user"
            )
        )

    @staticmethod
    def clear_revocation(
        session: Session, *, credential_id: UUID, user_id: UUID
    ) -> None:
        """Lift the owner's removal for ``user_id``. Never commits."""
        session.exec(
            delete(CredentialShareRevocation).where(
                col(CredentialShareRevocation.credential_id) == credential_id,
                col(CredentialShareRevocation.user_id) == user_id,
            )
        )

    @staticmethod
    def is_revoked(session: Session, *, credential_id: UUID, user_id: UUID) -> bool:
        """The credential's owner removed ``user_id``'s share and has not
        shared it with them again."""
        return (
            session.exec(
                select(CredentialShareRevocation.id).where(
                    CredentialShareRevocation.credential_id == credential_id,
                    CredentialShareRevocation.user_id == user_id,
                )
            ).first()
            is not None
        )

    # ── What a share is used for (owner warnings) ────────────────────────

    @staticmethod
    def owner_skill_usages(
        session: Session, *, credential: Credential
    ) -> tuple[list[CredentialSkillUsage], list[UUID]]:
        """The owner's catalog skill packages providing ``credential``, and
        every revision id of those packages (see
        ``SkillCredentialRequirements.publisher_usages_of_credential``)."""
        from app.services.skills.skill_credential_requirements import (
            SkillCredentialRequirements,
        )

        return SkillCredentialRequirements.publisher_usages_of_credential(
            session,
            credential_id=credential.id,
            publisher_user_id=credential.owner_id,
        )

    @staticmethod
    def local_skill_installs(
        session: Session,
        *,
        skill_usages: list[CredentialSkillUsage],
        user_ids: list[UUID],
    ) -> list[CatalogLocalInstall]:
        """Recorded Desktop installs, by ``user_ids``, of any package in
        ``skill_usages``. Any revision counts: a later version that stops
        providing the credential keeps the share."""
        if not skill_usages or not user_ids:
            return []
        return list(
            session.exec(
                select(CatalogLocalInstall)
                .where(
                    CatalogLocalInstall.item_kind == "skill",
                    col(CatalogLocalInstall.item_uuid).in_(
                        [usage.package_uuid for usage in skill_usages]
                    ),
                    col(CatalogLocalInstall.user_id).in_(user_ids),
                )
                .order_by(col(CatalogLocalInstall.created_at))
            ).all()
        )

    @staticmethod
    def _recipient_usages(
        session: Session, *, credential: Credential, recipient_ids: list[UUID]
    ) -> tuple[
        dict[UUID, list[CredentialShareAgentUsage]],
        dict[UUID, int],
        dict[UUID, list[CredentialShareLocalSkillUsage]],
    ]:
        """Per recipient: their cloud agents linking ``credential`` through
        one of the owner's providing skills (named), the count of the others
        (not named: the owner has no business knowing them), and their Desktop
        installs of the owner's providing skills. A fixed number of queries."""
        agent_usages: dict[UUID, list[CredentialShareAgentUsage]] = {}
        direct_counts: dict[UUID, int] = {}
        local_usages: dict[UUID, list[CredentialShareLocalSkillUsage]] = {}
        if not recipient_ids:
            return agent_usages, direct_counts, local_usages

        skill_usages, revision_ids = CredentialShareService.owner_skill_usages(
            session, credential=credential
        )
        names_by_package = {u.package_uuid: u.display_name for u in skill_usages}

        agent_rows = session.exec(
            select(Agent.id, Agent.name, Agent.owner_id)
            .join(AgentCredentialLink, AgentCredentialLink.agent_id == Agent.id)
            .where(
                AgentCredentialLink.credential_id == credential.id,
                col(Agent.owner_id).in_(recipient_ids),
            )
            .order_by(col(Agent.name))
        ).all()
        skills_by_agent: dict[UUID, list[str]] = {}
        if agent_rows and revision_ids:
            link_rows = session.exec(
                select(AgentPluginLink.agent_id, SkillPackageRevision.package_id)
                .join(
                    SkillPackageRevision,
                    SkillPackageRevision.id == AgentPluginLink.skill_package_revision_id,
                )
                .where(
                    col(AgentPluginLink.agent_id).in_([row[0] for row in agent_rows]),
                    AgentPluginLink.source == PluginSource.catalog,
                    col(AgentPluginLink.skill_package_revision_id).in_(revision_ids),
                )
            ).all()
            for agent_id, package_uuid in link_rows:
                name = names_by_package.get(package_uuid)
                names = skills_by_agent.setdefault(agent_id, [])
                if name is not None and name not in names:
                    names.append(name)
        for agent_id, agent_name, owner_id in agent_rows:
            if not skills_by_agent.get(agent_id):
                direct_counts[owner_id] = direct_counts.get(owner_id, 0) + 1
                continue
            agent_usages.setdefault(owner_id, []).append(
                CredentialShareAgentUsage(
                    id=agent_id,
                    name=agent_name,
                    skill_display_names=skills_by_agent.get(agent_id, []),
                )
            )

        installs = CredentialShareService.local_skill_installs(
            session, skill_usages=skill_usages, user_ids=recipient_ids
        )
        host_ids = {row.host_manifest_id for row in installs if row.host_manifest_id}
        # (manifest id, publisher) → name: a host agent is named only when the
        # recipient themselves shared it through core.
        host_names: dict[tuple[UUID, UUID], str] = {}
        if host_ids:
            host_names = {
                (manifest_id, publisher_id): display_name
                for manifest_id, publisher_id, display_name in session.exec(
                    select(
                        AgentBundle.manifest_id,
                        AgentBundle.publisher_user_id,
                        AgentBundle.display_name,
                    ).where(
                        AgentBundle.runs_on == "desktop",
                        col(AgentBundle.manifest_id).in_(host_ids),
                        col(AgentBundle.publisher_user_id).in_(recipient_ids),
                    )
                ).all()
                if manifest_id is not None and publisher_id is not None
            }
        usage_by_package = {u.package_uuid: u for u in skill_usages}
        for row in installs:
            usage = usage_by_package[row.item_uuid]
            local_usages.setdefault(row.user_id, []).append(
                CredentialShareLocalSkillUsage(
                    package_uuid=usage.package_uuid,
                    package_id=usage.package_id,
                    display_name=usage.display_name,
                    revision_number=row.revision_number,
                    host_agent_name=(
                        host_names.get((row.host_manifest_id, row.user_id))
                        if row.host_manifest_id
                        else None
                    ),
                )
            )
        return agent_usages, direct_counts, local_usages

    @staticmethod
    def get_credentials_shared_with_me(
        session: Session,
        user_id: UUID
    ) -> list[SharedCredentialPublic]:
        """
        Get all credentials shared with a user.

        Returns one SharedCredentialPublic per credential the user holds through
        a direct ``CredentialShare`` and/or a group share. When both exist the
        direct row's ``source`` / ``shared_at`` wins; a group-only row has
        ``source=None`` (classifies as "mine") and the earliest group share's
        ``shared_at``. ``via_groups`` names the groups either way.
        """
        # Query all shares where user is the recipient
        statement = select(CredentialShare).where(
            CredentialShare.shared_with_user_id == user_id
        )
        shares = session.exec(statement).all()

        # Resolve all credentials first, then batch the agent-usage counts so we
        # never issue a per-row count query (avoids the N+1 flagged in the plan).
        # A row is (source, shared_at, access_level, credential).
        resolved: list[tuple[str | None, datetime, str, Credential]] = []
        seen: set[UUID] = set()
        for share in shares:
            credential = session.get(Credential, share.credential_id)
            if not credential:
                continue  # Skip if credential was deleted
            seen.add(credential.id)
            resolved.append(
                (share.source, share.shared_at, share.access_level, credential)
            )

        group_rows = session.exec(
            select(Credential, func.min(CredentialGroupShare.shared_at))
            .join(
                CredentialGroupShare,
                col(CredentialGroupShare.credential_id) == col(Credential.id),
            )
            .where(
                col(CredentialGroupShare.group_id).in_(
                    UserGroupAccess.member_group_ids_subquery(user_id)
                ),
                Credential.owner_id != user_id,
            )
            .group_by(col(Credential.id))
        ).all()
        for credential, shared_at in group_rows:
            if credential.id in seen:
                continue
            seen.add(credential.id)
            resolved.append((None, shared_at, "read", credential))

        credential_ids = [credential.id for *_, credential in resolved]
        groups_by_credential = CredentialShareService.via_groups(
            session, user_id=user_id, credential_ids=credential_ids
        )
        usage_counts = CredentialsService.get_agent_usage_counts(
            session=session,
            credential_ids=credential_ids,
            owner_scope=user_id,  # recipient-scoped: agents *of mine* using it
        )

        result = []
        for source, shared_at, access_level, credential in resolved:
            owner = session.get(User, credential.owner_id)
            # `agent_api_kind` is deliberately omitted: this projection is for
            # credentials shared WITH the caller, and a shared row's category
            # never depends on its type (the is_owned=False branch decides on
            # share_source alone: "bundle_install" → bundle, "skill_install" →
            # automatic, anything else → mine). An external key can never
            # reach here anyway — sharing one is refused at the write boundary
            # (`assert_sharing_allowed`).
            category = CredentialsService.classify_credential_category(
                is_owned=False,
                credential_type=credential.type,
                share_source=source,
            )
            result.append(SharedCredentialPublic(
                id=credential.id,
                name=credential.name,
                type=credential.type.value,
                notes=credential.notes,
                owner_id=credential.owner_id,
                owner_email=owner.email if owner else "",
                shared_at=shared_at,
                access_level=access_level,
                category=category,
                source=source,
                agent_usage_count=usage_counts.get(credential.id, 0),
                used_in_bundle=False,
                via_groups=groups_by_credential.get(credential.id, []),
            ))

        return result

    @staticmethod
    def get_share_count_for_credential(
        session: Session,
        credential_id: UUID
    ) -> int:
        """
        Get the count of shares for a credential.

        Returns the number of users this credential is shared with.
        """
        from sqlmodel import func

        statement = select(func.count()).select_from(CredentialShare).where(
            CredentialShare.credential_id == credential_id
        )
        count = session.exec(statement).one()
        return count

    @staticmethod
    async def update_credential_sharing(
        session: Session,
        credential_id: UUID,
        owner_id: UUID,
        allow_sharing: bool
    ) -> Credential:
        """
        Enable or disable sharing for a credential.

        If disabling (allow_sharing=false):
        - All existing CredentialShare records are DELETED
        - Every recipient agent is unlinked and re-synced, so the value leaves
          their running containers too
        - Users who had access lose it immediately

        Returns updated Credential.
        """
        credential = session.get(Credential, credential_id)
        if not credential:
            raise ValueError("Credential not found")
        if credential.owner_id != owner_id:
            raise ValueError("Not enough permissions to update this credential")
        if allow_sharing:
            # Some credentials must never become shareable (agent_api external
            # keys — identity-bound). Same guard the generic update path applies.
            CredentialsService.assert_sharing_allowed(session, credential)

        # If disabling sharing, delete all existing shares.  Do not condition
        # this on the stored flag: an older generic PUT could already have
        # flipped it while leaving its shares behind, and PATCH must repair
        # that stale state as well.
        revoked_recipient_ids: list[UUID] = []
        if not allow_sharing:
            statement = select(CredentialShare).where(
                CredentialShare.credential_id == credential_id
            )
            shares = session.exec(statement).all()
            revoked_recipient_ids = [share.shared_with_user_id for share in shares]
            for share in shares:
                session.delete(share)
            revoked_recipient_ids.extend(
                CredentialShareService.delete_group_shares_for_credential(
                    session, credential_id
                )
            )
            logger.info(
                f"Disabled sharing for credential {credential_id}, "
                f"deleted {len(shares)} share(s)"
            )

        credential.allow_sharing = allow_sharing
        session.add(credential)
        session.commit()
        session.refresh(credential)

        await CredentialsService.unlink_credential_from_revoked_recipients(
            session=session,
            credential_id=credential_id,
            recipient_user_ids=revoked_recipient_ids,
        )

        return credential

    @staticmethod
    def can_user_access_credential(
        session: Session,
        credential_id: UUID,
        user_id: UUID
    ) -> bool:
        """
        Check if user can access a credential.

        Returns True if:
        - User owns the credential, OR
        - User has a direct CredentialShare or belongs to a group it is shared with
        """
        credential = session.get(Credential, credential_id)
        if not credential:
            return False

        # Check if user is the owner
        if credential.owner_id == user_id:
            return True

        return CredentialShareService.has_share(session, credential_id, user_id)

    # ── Group-aware access (user groups: D4 live membership, D8) ─────────

    @staticmethod
    def group_shared_credential_ids_subquery(user_id: UUID):
        """Credential ids group-shared with any group ``user_id`` belongs to."""
        return select(CredentialGroupShare.credential_id).where(
            col(CredentialGroupShare.group_id).in_(
                UserGroupAccess.member_group_ids_subquery(user_id)
            )
        )

    @staticmethod
    def shared_credential_ids_subquery(user_id: UUID):
        """Ids of every credential shared with ``user_id``, directly or by group.

        Use as ``col(Credential.id).in_(CredentialShareService.shared_credential_ids_subquery(uid))``.
        """
        direct = select(CredentialShare.credential_id).where(
            CredentialShare.shared_with_user_id == user_id
        )
        return union(direct, CredentialShareService.group_shared_credential_ids_subquery(user_id))

    @staticmethod
    def has_share(session: Session, credential_id: UUID, user_id: UUID) -> bool:
        """Whether ``user_id`` holds a direct or a group share of the credential.

        Ownership is not checked; callers test it first.
        """
        direct = session.exec(
            select(CredentialShare.id).where(
                CredentialShare.credential_id == credential_id,
                CredentialShare.shared_with_user_id == user_id,
            )
        ).first()
        if direct is not None:
            return True
        group = session.exec(
            select(CredentialGroupShare.id).where(
                CredentialGroupShare.credential_id == credential_id,
                col(CredentialGroupShare.group_id).in_(
                    UserGroupAccess.member_group_ids_subquery(user_id)
                ),
            )
        ).first()
        return group is not None

    @staticmethod
    def group_shared_ids(
        session: Session, *, user_id: UUID, credential_ids: list[UUID]
    ) -> set[UUID]:
        """Ids among ``credential_ids`` group-shared with ``user_id``. No query when empty."""
        if not credential_ids:
            return set()
        return set(
            session.exec(
                select(CredentialGroupShare.credential_id).where(
                    col(CredentialGroupShare.credential_id).in_(credential_ids),
                    col(CredentialGroupShare.group_id).in_(
                        UserGroupAccess.member_group_ids_subquery(user_id)
                    ),
                )
            ).all()
        )

    @staticmethod
    def via_groups(
        session: Session, *, user_id: UUID, credential_ids: list[UUID]
    ) -> dict[UUID, list[UserGroupRef]]:
        """``{credential_id: [groups]}`` through which ``user_id`` holds each one."""
        if not credential_ids:
            return {}
        rows = session.exec(
            select(CredentialGroupShare.credential_id, UserGroup.id, UserGroup.name)
            .join(UserGroup, col(UserGroup.id) == col(CredentialGroupShare.group_id))
            .where(
                col(CredentialGroupShare.credential_id).in_(credential_ids),
                col(CredentialGroupShare.group_id).in_(
                    UserGroupAccess.member_group_ids_subquery(user_id)
                ),
            )
            .order_by(col(UserGroup.name))
        ).all()
        result: dict[UUID, list[UserGroupRef]] = {}
        for credential_id, group_id, group_name in rows:
            result.setdefault(credential_id, []).append(
                UserGroupRef(id=group_id, name=group_name)
            )
        return result

    @staticmethod
    def users_without_access(
        session: Session,
        credential_id: UUID,
        user_ids: set[UUID] | list[UUID],
        *,
        exclude_group_id: UUID | None = None,
    ) -> set[UUID]:
        """The users among ``user_ids`` with no remaining path to the credential.

        Excludes the owner, direct share holders and members of any group the
        credential is shared with (D8). ``exclude_group_id`` ignores one group
        share, to preview what revoking it would cost.
        """
        remaining = {uid for uid in user_ids if uid is not None}
        if not remaining:
            return set()
        credential = session.get(Credential, credential_id)
        if credential is None:
            return remaining
        remaining.discard(credential.owner_id)
        if not remaining:
            return set()
        direct = set(
            session.exec(
                select(CredentialShare.shared_with_user_id).where(
                    CredentialShare.credential_id == credential_id,
                    col(CredentialShare.shared_with_user_id).in_(remaining),
                )
            ).all()
        )
        group_stmt = (
            select(UserGroupMember.user_id)
            .join(
                CredentialGroupShare,
                col(CredentialGroupShare.group_id) == col(UserGroupMember.group_id),
            )
            .where(
                CredentialGroupShare.credential_id == credential_id,
                col(UserGroupMember.user_id).in_(remaining),
            )
        )
        if exclude_group_id is not None:
            group_stmt = group_stmt.where(
                CredentialGroupShare.group_id != exclude_group_id
            )
        via_group = set(session.exec(group_stmt).all())
        return remaining - direct - via_group

    @staticmethod
    def share_counts_by_credential(
        session: Session, credential_ids: list[UUID]
    ) -> tuple[dict[UUID, int], dict[UUID, int]]:
        """``(direct share counts, group share counts)`` per credential, two
        grouped queries for a whole page. Missing ids count 0."""
        if not credential_ids:
            return {}, {}
        direct = dict(
            session.exec(
                select(CredentialShare.credential_id, func.count())
                .where(col(CredentialShare.credential_id).in_(credential_ids))
                .group_by(col(CredentialShare.credential_id))
            ).all()
        )
        group = dict(
            session.exec(
                select(CredentialGroupShare.credential_id, func.count())
                .where(col(CredentialGroupShare.credential_id).in_(credential_ids))
                .group_by(col(CredentialGroupShare.credential_id))
            ).all()
        )
        return direct, group

    @staticmethod
    def get_group_share_count_for_credential(
        session: Session, credential_id: UUID
    ) -> int:
        """Number of user groups the credential is shared with."""
        return session.exec(
            select(func.count())
            .select_from(CredentialGroupShare)
            .where(CredentialGroupShare.credential_id == credential_id)
        ).one()

    @staticmethod
    def get_group_member_count_for_credential(
        session: Session, credential_id: UUID
    ) -> int:
        """Distinct active members reached through the credential's group shares."""
        group_ids = session.exec(
            select(CredentialGroupShare.group_id).where(
                CredentialGroupShare.credential_id == credential_id
            )
        ).all()
        return len(UserGroupAccess.active_member_ids(session, group_ids))

    @staticmethod
    def delete_group_shares_for_credential(
        session: Session, credential_id: UUID
    ) -> list[UUID]:
        """Delete every group share of a credential (sharing turned off).

        Does not commit. Returns every member of the groups, inactive ones
        included (they may still own agents linking the credential), for the
        caller to pass to ``unlink_credential_from_revoked_recipients`` after
        its commit.
        No revocation rows are written (D7).
        """
        group_shares = session.exec(
            select(CredentialGroupShare).where(
                CredentialGroupShare.credential_id == credential_id
            )
        ).all()
        if not group_shares:
            return []
        member_ids = set(
            session.exec(
                select(UserGroupMember.user_id).where(
                    col(UserGroupMember.group_id).in_(
                        [share.group_id for share in group_shares]
                    )
                )
            ).all()
        )
        for share in group_shares:
            session.delete(share)
        logger.info(
            "Deleted %s group share(s) of credential %s",
            len(group_shares),
            credential_id,
        )
        return list(member_ids)

    # ── Group share operations (owner) ───────────────────────────────────

    @staticmethod
    def _owned_credential(
        session: Session, credential_id: UUID, owner_id: UUID
    ) -> Credential:
        credential = session.get(Credential, credential_id)
        if credential is None:
            raise CredentialGroupShareError(404, "Credential not found")
        if credential.owner_id != owner_id:
            raise CredentialGroupShareError(
                403, "Only the owner can manage this credential's group shares"
            )
        return credential

    @staticmethod
    def _links_at_risk(
        session: Session, credential_id: UUID, group_id: UUID, member_ids: set[UUID]
    ) -> int:
        """Agent links revoking the group share would drop (members left with no path)."""
        _, links = CredentialShareService.lost_links(
            session, credential_id, member_ids, exclude_group_id=group_id
        )
        return links

    @staticmethod
    def lost_links(
        session: Session,
        credential_id: UUID,
        user_ids: set[UUID],
        *,
        exclude_group_id: UUID | None = None,
    ) -> tuple[set[UUID], int]:
        """``(users who would lose the credential, agent links they would lose)``.

        Read-only; ``exclude_group_id`` previews the loss of one group share.
        """
        lost = CredentialShareService.users_without_access(
            session, credential_id, user_ids, exclude_group_id=exclude_group_id
        )
        if not lost:
            return set(), 0
        links = session.exec(
            select(func.count())
            .select_from(AgentCredentialLink)
            .join(Agent, col(Agent.id) == col(AgentCredentialLink.agent_id))
            .where(
                AgentCredentialLink.credential_id == credential_id,
                col(Agent.owner_id).in_(lost),
            )
        ).one()
        return lost, links

    @staticmethod
    def _members_by_group(
        session: Session, group_ids: list[UUID]
    ) -> dict[UUID, tuple[set[UUID], int]]:
        """``{group_id: (all member ids, active member count)}`` in one query.

        Every member feeds ``links_at_risk`` (an inactive one may still own
        linking agents); ``member_count`` counts active members only.
        """
        result: dict[UUID, tuple[set[UUID], int]] = {
            gid: (set(), 0) for gid in group_ids
        }
        if not group_ids:
            return result
        rows = session.exec(
            select(UserGroupMember.group_id, UserGroupMember.user_id, User.is_active)
            .join(User, col(User.id) == col(UserGroupMember.user_id))
            .where(col(UserGroupMember.group_id).in_(group_ids))
        ).all()
        for gid, uid, is_active in rows:
            members, active = result[gid]
            members.add(uid)
            result[gid] = (members, active + (1 if is_active else 0))
        return result

    @staticmethod
    def _group_share_public(
        session: Session,
        share: CredentialGroupShare,
        group: UserGroup,
        members: tuple[set[UUID], int] | None = None,
    ) -> CredentialGroupSharePublic:
        if members is None:
            members = CredentialShareService._members_by_group(session, [group.id])[
                group.id
            ]
        member_ids, active_count = members
        return CredentialGroupSharePublic(
            group_id=group.id,
            group_name=group.name,
            member_count=active_count,
            shared_by_user_id=share.shared_by_user_id,
            shared_at=share.shared_at,
            links_at_risk=CredentialShareService._links_at_risk(
                session, share.credential_id, group.id, member_ids
            ),
        )

    @staticmethod
    def share_with_group(
        session: Session, credential_id: UUID, owner_id: UUID, group_id: UUID
    ) -> CredentialGroupSharePublic:
        """Share a credential with a user group (owner only).

        Raises CredentialGroupShareError: 404 credential / group, 403 not the
        owner, 400 sharing off or not allowed for this credential, 409 duplicate.
        """
        credential = CredentialShareService._owned_credential(
            session, credential_id, owner_id
        )
        if not credential.allow_sharing:
            raise CredentialGroupShareError(
                400, "This credential does not allow sharing. Enable sharing first."
            )
        try:
            CredentialsService.assert_sharing_allowed(session, credential)
        except ValueError as exc:
            raise CredentialGroupShareError(400, str(exc)) from exc
        # Read lock: a concurrent group delete / member removal (FOR UPDATE)
        # waits for this share, so its coverage snapshot includes it.
        group = session.exec(
            select(UserGroup).where(UserGroup.id == group_id).with_for_update(read=True)
        ).first()
        if group is None:
            raise CredentialGroupShareError(404, "User group not found")
        existing = session.exec(
            select(CredentialGroupShare.id).where(
                CredentialGroupShare.credential_id == credential_id,
                CredentialGroupShare.group_id == group_id,
            )
        ).first()
        if existing is not None:
            raise CredentialGroupShareError(
                409, f"Credential is already shared with group '{group.name}'"
            )
        share = CredentialGroupShare(
            credential_id=credential_id,
            group_id=group_id,
            shared_by_user_id=owner_id,
        )
        session.add(share)
        try:
            session.commit()
        except IntegrityError as exc:
            # A concurrent request created the same share after our check.
            session.rollback()
            raise CredentialGroupShareError(
                409, f"Credential is already shared with group '{group.name}'"
            ) from exc
        session.refresh(share)
        logger.info(
            "Credential %s shared with user group %s by owner %s",
            credential_id,
            group_id,
            owner_id,
        )
        return CredentialShareService._group_share_public(session, share, group)

    @staticmethod
    def list_group_shares(
        session: Session, credential_id: UUID, owner_id: UUID
    ) -> list[CredentialGroupSharePublic]:
        """Every group share of an owned credential, by group name."""
        CredentialShareService._owned_credential(session, credential_id, owner_id)
        rows = session.exec(
            select(CredentialGroupShare, UserGroup)
            .join(UserGroup, col(UserGroup.id) == col(CredentialGroupShare.group_id))
            .where(CredentialGroupShare.credential_id == credential_id)
            .order_by(col(UserGroup.name))
        ).all()
        members = CredentialShareService._members_by_group(
            session, [group.id for _, group in rows]
        )
        return [
            CredentialShareService._group_share_public(
                session, share, group, members[group.id]
            )
            for share, group in rows
        ]

    @staticmethod
    async def revoke_group_share(
        session: Session, credential_id: UUID, owner_id: UUID, group_id: UUID
    ) -> None:
        """Owner revoke of a group share; see :meth:`remove_group_share`."""
        CredentialShareService._owned_credential(session, credential_id, owner_id)
        await CredentialShareService.remove_group_share(
            session, credential_id, group_id, actor_id=owner_id
        )

    @staticmethod
    async def remove_group_share(
        session: Session, credential_id: UUID, group_id: UUID, *, actor_id: UUID
    ) -> None:
        """Remove a group share, then unlink + re-sync members who lost every path.

        No ownership check: callers authorise (the owner route, or the
        superuser group page). Writes no revocation rows (D7): the members
        never held a per-person share. Raises CredentialGroupShareError 404.
        """
        share = session.exec(
            select(CredentialGroupShare).where(
                CredentialGroupShare.credential_id == credential_id,
                CredentialGroupShare.group_id == group_id,
            )
        ).first()
        if share is None:
            raise CredentialGroupShareError(404, "Group share not found")
        # Every member, active or not: an inactive one may still own agents
        # linking the credential.
        member_ids = set(
            session.exec(
                select(UserGroupMember.user_id).where(
                    UserGroupMember.group_id == group_id
                )
            ).all()
        )
        session.delete(share)
        session.commit()
        logger.info(
            "Credential %s group share with %s revoked by %s",
            credential_id,
            group_id,
            actor_id,
        )
        await CredentialShareService.release_lost(
            session, credential_ids={credential_id}, user_ids=member_ids
        )

    @staticmethod
    async def release_lost(
        session: Session, *, credential_ids: Iterable[UUID], user_ids: Iterable[UUID]
    ) -> None:
        """Drop the links of ``user_ids`` to ``credential_ids`` they lost.

        The caller passes exactly the pairs a group change covered (the
        group's credential shares × the affected members), snapshotted before
        the change. Runs after the change is committed, so the D8 filter inside
        ``unlink_credential_from_revoked_recipients`` keeps whatever the users
        still hold through the owner, a direct share or another group.
        """
        targets = [uid for uid in user_ids if uid is not None]
        if not targets:
            return
        for credential_id in dict.fromkeys(credential_ids):
            await CredentialsService.unlink_credential_from_revoked_recipients(
                session=session,
                credential_id=credential_id,
                recipient_user_ids=targets,
            )

    @staticmethod
    def delete_all_shares_for_credential(
        session: Session,
        credential_id: UUID
    ) -> int:
        """
        Delete all shares for a credential (called when credential is deleted).

        Returns the number of shares deleted.
        """
        statement = select(CredentialShare).where(
            CredentialShare.credential_id == credential_id
        )
        shares = session.exec(statement).all()
        count = len(shares)
        for share in shares:
            session.delete(share)
        session.commit()
        return count
