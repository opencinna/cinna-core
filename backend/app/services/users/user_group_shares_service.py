"""UserGroupSharesService — the group page's view of what is shared with a group.

D3: new grants are made only in each resource's own share dialog. The group
page lists every resource shared with the group, lets a superuser revoke one
share (overriding the owner check), previews the cost of a revoke, a member
removal or a group delete, and deletes groups.

Loss of access is computed, not assumed (D8, §10). A preview asks, per
credential / AI credential the group is shared with, which of the affected
members would be left with no other path once this group's shares stop
applying (``exclude_group_id``). A delete snapshots the group's coverage
before the rows cascade and releases exactly those pairs after the commit.
"""
from __future__ import annotations

import json
import logging
import uuid
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, get_args

from fastapi import HTTPException
from sqlmodel import Session, col, delete, select

from app.models.agent_api.agent_api_access_group_grant import AgentApiAccessGroupGrant
from app.models.agents.agent import Agent
from app.models.artifacts.artifact import Artifact
from app.models.artifacts.artifact_grant import ArtifactGroupGrant
from app.models.bundles.agent_bundle import AgentBundle
from app.models.bundles.bundle_access_grant import BundleAccessGrant
from app.models.catalog.catalog_access import CatalogPerson
from app.models.credentials.ai_credential import AICredential
from app.models.credentials.ai_credential_group_share import AICredentialGroupShare
from app.models.credentials.credential import Credential
from app.models.credentials.credential_group_share import CredentialGroupShare
from app.models.events.security_event import (
    AGENT_API_GRANT_DELETED,
    CATALOG_GRANTS_CHANGED,
    SecurityEvent,
    USER_GROUP_DELETED,
    USER_GROUP_SHARE_REVOKED,
)
from app.models.knowledge.knowledge import (
    AIKnowledgeGitRepo,
    AIKnowledgeGitRepoGroupShare,
)
from app.models.skills.skill_package import SkillPackage
from app.models.skills.skill_package_access_grant import SkillPackageAccessGrant
from app.models.users.user import User
from app.models.users.user_group import (
    UserGroupImpact,
    UserGroupShareResourceType,
    UserGroupSharePublic,
    UserGroupSharesPublic,
)
from app.services.catalog.catalog_access_service import (
    BUNDLE,
    SKILL,
    CatalogAccessService,
    CatalogKindSpec,
)
from app.services.catalog.catalog_events import add_audit_event
from app.services.credentials.ai_credentials_service import ai_credentials_service
from app.services.credentials.credential_share_service import (
    CredentialGroupShareError,
    CredentialShareService,
)
from app.services.users.user_group_access import UserGroupAccess
from app.services.users.user_group_service import (
    UserGroupNotFoundError,
    UserGroupService,
)

logger = logging.getLogger(__name__)

SHARE_NOT_FOUND_DETAIL = "This resource is not shared with the group"

# List order of the families on the group page.
_TYPE_ORDER: dict[str, int] = {
    t: i for i, t in enumerate(get_args(UserGroupShareResourceType))
}

# Fixed levels of the families whose group share has no level column.
_LEVEL_CREDENTIAL = "read"
_LEVEL_AI_CREDENTIAL = "use"
_LEVEL_KNOWLEDGE = "query"
_LEVEL_NO_SCOPES = "no scopes"

_BUNDLE_RUNS_ON = {"agent_bundle": "cloud", "desktop_agent": "desktop"}


@dataclass(frozen=True)
class _ShareRow:
    """A share before people are resolved."""

    resource_type: str
    resource_id: uuid.UUID
    resource_name: str
    level: str
    owner_id: uuid.UUID | None
    granted_by_id: uuid.UUID | None
    granted_at: Any


@dataclass
class _Loss:
    """Accumulated credential / AI credential loss for an impact preview."""

    users: set[uuid.UUID]
    links: int = 0
    envs_cleared: int = 0
    envs_restarted: int = 0


class UserGroupSharesService:
    """Static entry points; callers own the session. Superuser-only (routes)."""

    # ── List ─────────────────────────────────────────────────────────

    @staticmethod
    def list_shares(session: Session, group_id: uuid.UUID) -> UserGroupSharesPublic:
        """Every resource shared with the group, sorted by type then name."""
        UserGroupService.get_group(session, group_id)
        rows = UserGroupSharesService._share_rows(session, group_id)
        people = UserGroupSharesService._people(
            session,
            {r.owner_id for r in rows} | {r.granted_by_id for r in rows},
        )
        data = [
            UserGroupSharePublic(
                resource_type=r.resource_type,
                resource_id=r.resource_id,
                resource_name=r.resource_name,
                level=r.level,
                owner=people.get(r.owner_id) if r.owner_id else None,
                granted_by=people.get(r.granted_by_id) if r.granted_by_id else None,
                granted_at=r.granted_at,
            )
            for r in rows
        ]
        return UserGroupSharesPublic(data=data, count=len(data))

    @staticmethod
    def _share_rows(session: Session, group_id: uuid.UUID) -> list[_ShareRow]:
        """One query per family."""
        rows: list[_ShareRow] = []
        for grant, bundle in session.exec(
            select(BundleAccessGrant, AgentBundle)
            .join(AgentBundle, col(AgentBundle.id) == col(BundleAccessGrant.bundle_id))
            .where(BundleAccessGrant.group_id == group_id)
        ).all():
            rows.append(
                _ShareRow(
                    "desktop_agent" if bundle.runs_on == "desktop" else "agent_bundle",
                    bundle.id,
                    bundle.display_name,
                    grant.access,
                    bundle.publisher_user_id,
                    grant.granted_by_user_id,
                    grant.created_at,
                )
            )
        for grant, package in session.exec(
            select(SkillPackageAccessGrant, SkillPackage)
            .join(
                SkillPackage,
                col(SkillPackage.id) == col(SkillPackageAccessGrant.package_id),
            )
            .where(SkillPackageAccessGrant.group_id == group_id)
        ).all():
            rows.append(
                _ShareRow(
                    "skill_package",
                    package.id,
                    package.display_name,
                    grant.access,
                    package.publisher_user_id,
                    grant.granted_by_user_id,
                    grant.created_at,
                )
            )
        for share, credential in session.exec(
            select(CredentialGroupShare, Credential)
            .join(Credential, col(Credential.id) == col(CredentialGroupShare.credential_id))
            .where(CredentialGroupShare.group_id == group_id)
        ).all():
            rows.append(
                _ShareRow(
                    "credential",
                    credential.id,
                    credential.name,
                    _LEVEL_CREDENTIAL,
                    credential.owner_id,
                    share.shared_by_user_id,
                    share.shared_at,
                )
            )
        for share, ai_credential in session.exec(
            select(AICredentialGroupShare, AICredential)
            .join(
                AICredential,
                col(AICredential.id) == col(AICredentialGroupShare.ai_credential_id),
            )
            .where(AICredentialGroupShare.group_id == group_id)
        ).all():
            rows.append(
                _ShareRow(
                    "ai_credential",
                    ai_credential.id,
                    ai_credential.name,
                    _LEVEL_AI_CREDENTIAL,
                    ai_credential.owner_id,
                    share.shared_by_user_id,
                    share.shared_at,
                )
            )
        for grant, artifact in session.exec(
            select(ArtifactGroupGrant, Artifact)
            .join(Artifact, col(Artifact.id) == col(ArtifactGroupGrant.artifact_id))
            .where(ArtifactGroupGrant.group_id == group_id)
        ).all():
            rows.append(
                _ShareRow(
                    "artifact",
                    artifact.id,
                    artifact.title,
                    grant.level,
                    artifact.owner_id,
                    grant.granted_by,
                    grant.created_at,
                )
            )
        for share, repo in session.exec(
            select(AIKnowledgeGitRepoGroupShare, AIKnowledgeGitRepo)
            .join(
                AIKnowledgeGitRepo,
                col(AIKnowledgeGitRepo.id) == col(AIKnowledgeGitRepoGroupShare.git_repo_id),
            )
            .where(AIKnowledgeGitRepoGroupShare.group_id == group_id)
        ).all():
            rows.append(
                _ShareRow(
                    "knowledge_source",
                    repo.id,
                    repo.name,
                    _LEVEL_KNOWLEDGE,
                    repo.user_id,
                    share.granted_by_user_id,
                    share.created_at,
                )
            )
        for grant, agent in session.exec(
            select(AgentApiAccessGroupGrant, Agent)
            .join(Agent, col(Agent.id) == col(AgentApiAccessGroupGrant.producer_agent_id))
            .where(AgentApiAccessGroupGrant.group_id == group_id)
        ).all():
            rows.append(
                _ShareRow(
                    "agent_api_scopes",
                    agent.id,
                    agent.name,
                    ", ".join(str(s) for s in (grant.scopes or [])) or _LEVEL_NO_SCOPES,
                    agent.owner_id,
                    grant.created_by,
                    grant.created_at,
                )
            )
        rows.sort(
            key=lambda r: (_TYPE_ORDER[r.resource_type], r.resource_name.lower(), r.resource_id)
        )
        return rows

    @staticmethod
    def _people(
        session: Session, user_ids: Iterable[uuid.UUID | None]
    ) -> dict[uuid.UUID, CatalogPerson]:
        ids = {uid for uid in user_ids if uid is not None}
        if not ids:
            return {}
        return {
            u.id: CatalogPerson(user_id=u.id, display_name=u.full_name or u.email)
            for u in session.exec(select(User).where(col(User.id).in_(ids))).all()
        }

    # ── Revoke (superuser override of the owner check) ───────────────

    @staticmethod
    async def revoke(
        session: Session,
        actor: User,
        group_id: uuid.UUID,
        resource_type: str,
        resource_id: uuid.UUID,
    ) -> None:
        """Remove one share from the group. 404 for an unknown group or pair."""
        group = UserGroupService.get_group(session, group_id)
        group_name = group.name
        handlers: dict[str, Callable[..., Any]] = {
            "agent_bundle": UserGroupSharesService._revoke_catalog,
            "desktop_agent": UserGroupSharesService._revoke_catalog,
            "skill_package": UserGroupSharesService._revoke_catalog,
            "artifact": UserGroupSharesService._revoke_artifact,
            "knowledge_source": UserGroupSharesService._revoke_knowledge,
            "agent_api_scopes": UserGroupSharesService._revoke_agent_api_scopes,
        }
        if resource_type not in handlers and resource_type not in (
            "credential",
            "ai_credential",
        ):
            raise UserGroupNotFoundError(SHARE_NOT_FOUND_DETAIL)

        # Staged first so it lands in the same commit as the share delete:
        # the credential families commit inside ``remove_group_share`` and
        # release afterwards.
        add_audit_event(
            session,
            actor.id,
            USER_GROUP_SHARE_REVOKED,
            {
                "group_id": str(group_id),
                "name": group_name,
                "resource_type": resource_type,
                "resource_id": str(resource_id),
            },
        )
        try:
            if resource_type == "credential":
                await CredentialShareService.remove_group_share(
                    session, resource_id, group_id, actor_id=actor.id
                )
            elif resource_type == "ai_credential":
                await ai_credentials_service.remove_group_share(
                    session, resource_id, group_id, actor_id=actor.id
                )
            else:
                # Stages the delete (and any family audit) with the audit row.
                handlers[resource_type](
                    session, actor, group_id, resource_type, resource_id
                )
                session.commit()
        except (CredentialGroupShareError, HTTPException) as exc:
            # 404 from the credential families: nothing was committed.
            session.rollback()
            raise UserGroupNotFoundError(SHARE_NOT_FOUND_DETAIL) from exc
        except UserGroupNotFoundError:
            session.rollback()
            raise

    @staticmethod
    def _catalog_spec(resource_type: str) -> CatalogKindSpec:
        return SKILL if resource_type == "skill_package" else BUNDLE

    @staticmethod
    def _revoke_catalog(
        session: Session,
        actor: User,
        group_id: uuid.UUID,
        resource_type: str,
        resource_id: uuid.UUID,
    ) -> None:
        spec = UserGroupSharesService._catalog_spec(resource_type)
        # Row lock on the item, as a grant replacement takes, so the two
        # serialise instead of racing.
        item = session.exec(
            select(spec.item_model)
            .where(spec.item_model.id == resource_id)
            .with_for_update()
        ).first()
        if item is None or (
            spec is BUNDLE and item.runs_on != _BUNDLE_RUNS_ON[resource_type]
        ):
            raise UserGroupNotFoundError(SHARE_NOT_FOUND_DETAIL)
        grant_model = spec.grant_model
        row = session.exec(
            select(grant_model).where(
                spec.fk_col == resource_id, grant_model.group_id == group_id
            )
        ).first()
        if row is None:
            raise UserGroupNotFoundError(SHARE_NOT_FOUND_DETAIL)
        session.delete(row)
        session.flush()
        remaining = CatalogAccessService.grant_rows(session, spec, resource_id)
        add_audit_event(
            session,
            actor.id,
            CATALOG_GRANTS_CHANGED,
            {
                "kind": spec.wire_kind,
                "uuid": str(resource_id),
                "grants": [
                    {"principal": f"{g.principal_kind}:{g.principal_id}", "access": g.access}
                    for g in remaining
                ],
            },
        )

    @staticmethod
    def _revoke_artifact(
        session: Session,
        actor: User,
        group_id: uuid.UUID,
        resource_type: str,
        resource_id: uuid.UUID,
    ) -> None:
        UserGroupSharesService._delete_one(
            session,
            delete(ArtifactGroupGrant).where(
                ArtifactGroupGrant.artifact_id == resource_id,
                ArtifactGroupGrant.group_id == group_id,
            ),
        )

    @staticmethod
    def _revoke_knowledge(
        session: Session,
        actor: User,
        group_id: uuid.UUID,
        resource_type: str,
        resource_id: uuid.UUID,
    ) -> None:
        UserGroupSharesService._delete_one(
            session,
            delete(AIKnowledgeGitRepoGroupShare).where(
                AIKnowledgeGitRepoGroupShare.git_repo_id == resource_id,
                AIKnowledgeGitRepoGroupShare.group_id == group_id,
            ),
        )

    @staticmethod
    def _revoke_agent_api_scopes(
        session: Session,
        actor: User,
        group_id: uuid.UUID,
        resource_type: str,
        resource_id: uuid.UUID,
    ) -> None:
        """Delete the group's scope grant on producer ``resource_id`` and stage
        the same ``AGENT_API_GRANT_DELETED`` audit the owner's delete writes."""
        grant = session.exec(
            select(AgentApiAccessGroupGrant).where(
                AgentApiAccessGroupGrant.producer_agent_id == resource_id,
                AgentApiAccessGroupGrant.group_id == group_id,
            )
        ).first()
        if grant is None:
            raise UserGroupNotFoundError(SHARE_NOT_FOUND_DETAIL)
        # Same details shape as AgentApiGrantService.remove_group_grant /
        # _audit_group; keep the two in step.
        details = {
            "grant_id": str(grant.id),
            "granted_group_id": str(group_id),
            "scopes": list(grant.scopes or []),
        }
        session.delete(grant)
        session.add(
            SecurityEvent(
                user_id=actor.id,
                agent_id=resource_id,
                event_type=AGENT_API_GRANT_DELETED,
                severity="medium",
                details=json.dumps(details),
            )
        )

    @staticmethod
    def _delete_one(session: Session, statement: Any) -> None:
        if session.execute(statement).rowcount == 0:
            raise UserGroupNotFoundError(SHARE_NOT_FOUND_DETAIL)

    # ── Impact previews (read-only) ──────────────────────────────────

    @staticmethod
    def impact(
        session: Session,
        group_id: uuid.UUID,
        user_ids: Iterable[uuid.UUID] | None = None,
    ) -> UserGroupImpact:
        """Cost of this group's shares ceasing to apply to ``user_ids``
        (every member when None): a group delete or a member removal."""
        UserGroupService.get_group(session, group_id)
        coverage = UserGroupAccess.coverage(session, group_id, user_ids)
        shares_by_type = Counter(
            r.resource_type for r in UserGroupSharesService._share_rows(session, group_id)
        )
        loss = UserGroupSharesService._loss(
            session,
            group_id,
            set(coverage.user_ids),
            coverage.credential_ids,
            coverage.ai_credential_ids,
        )
        active = UserGroupAccess.active_member_ids(session, [group_id])
        return UserGroupSharesService._impact_public(
            len(active & coverage.user_ids), dict(shares_by_type), loss
        )

    @staticmethod
    def member_removal_impact(
        session: Session, group_id: uuid.UUID, user_id: uuid.UUID
    ) -> UserGroupImpact:
        """Cost of removing one member. 404 when they are not a member."""
        UserGroupService.get_group(session, group_id)
        if user_id not in UserGroupAccess.member_ids(session, group_id):
            raise UserGroupNotFoundError("User is not a member of this group")
        return UserGroupSharesService.impact(session, group_id, {user_id})

    @staticmethod
    def share_revoke_impact(
        session: Session,
        group_id: uuid.UUID,
        resource_type: str,
        resource_id: uuid.UUID,
    ) -> UserGroupImpact:
        """Cost of revoking one share from the group (every member)."""
        UserGroupService.get_group(session, group_id)
        if not any(
            r.resource_type == resource_type and r.resource_id == resource_id
            for r in UserGroupSharesService._share_rows(session, group_id)
        ):
            raise UserGroupNotFoundError(SHARE_NOT_FOUND_DETAIL)
        members = UserGroupAccess.member_ids(session, group_id)
        loss = UserGroupSharesService._loss(
            session,
            group_id,
            members,
            [resource_id] if resource_type == "credential" else [],
            [resource_id] if resource_type == "ai_credential" else [],
        )
        active = UserGroupAccess.active_member_ids(session, [group_id])
        return UserGroupSharesService._impact_public(
            len(active), {resource_type: 1}, loss
        )

    @staticmethod
    def _loss(
        session: Session,
        group_id: uuid.UUID,
        user_ids: set[uuid.UUID],
        credential_ids: Iterable[uuid.UUID],
        ai_credential_ids: Iterable[uuid.UUID],
    ) -> _Loss:
        loss = _Loss(users=set())
        if not user_ids:
            return loss
        for credential_id in credential_ids:
            lost, links = CredentialShareService.lost_links(
                session, credential_id, user_ids, exclude_group_id=group_id
            )
            loss.users |= lost
            loss.links += links
        for ai_credential_id in ai_credential_ids:
            lost, envs = ai_credentials_service.lost_environments(
                session, ai_credential_id, user_ids, exclude_group_id=group_id
            )
            loss.users |= lost
            loss.envs_cleared += len(envs)
            loss.envs_restarted += sum(1 for env in envs if env.status == "running")
        return loss

    @staticmethod
    def _impact_public(
        member_count: int, shares_by_type: dict[str, int], loss: _Loss
    ) -> UserGroupImpact:
        return UserGroupImpact(
            member_count=member_count,
            shares_by_type=shares_by_type,
            users_losing_credential_access=len(loss.users),
            credential_links_removed=loss.links,
            ai_environments_cleared=loss.envs_cleared,
            ai_environments_restarted=loss.envs_restarted,
        )

    # ── Delete ───────────────────────────────────────────────────────

    @staticmethod
    async def delete_group(session: Session, actor: User, group_id: uuid.UUID) -> None:
        """Delete the group; members and every share row go by CASCADE.

        The group's credential / AI credential coverage is snapshotted first
        and released after the commit, so only pairs the group covered are
        considered, and anyone who still holds them another way keeps them.
        """
        # Lock first: a share made between the snapshot and the commit would
        # otherwise cascade away unreleased (share_with_group read-locks).
        group = UserGroupService.lock_group(session, group_id)
        coverage = UserGroupAccess.coverage(session, group_id)
        shares_by_type = Counter(
            r.resource_type for r in UserGroupSharesService._share_rows(session, group_id)
        )
        add_audit_event(
            session,
            actor.id,
            USER_GROUP_DELETED,
            {
                "group_id": str(group.id),
                "name": group.name,
                "member_count": len(
                    UserGroupAccess.active_member_ids(session, [group_id])
                ),
                "shares_by_type": dict(shares_by_type),
            },
        )
        session.delete(group)
        session.commit()
        await UserGroupAccess.release(session, coverage)
