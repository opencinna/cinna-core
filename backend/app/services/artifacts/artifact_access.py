"""The one access rule for artifacts.

Every route and the content route decide access here, so a change to who can
see an artifact lands in one place:

- **view**: the owner; or ``visibility='instance'`` and a valid signed-in
  account; or a grant to that user. A public link is checked separately
  (:meth:`ArtifactAccess.link_serves`) because it has no user; it also dies
  with the owner's account.
- **manage** (republish, edit, share, delete): the owner only.
- **agent publish** (A8): :func:`agent_publish_policy` — an agent (and the
  owner's workspace publish, D20) creates private ``core_agent`` artifacts and
  republishes only those its own agent made; no visibility, grant, link or
  delete operation is reachable from that path.

Account validity goes through ``AccessPolicyService.is_account_valid`` — the
same seam ``get_current_user`` uses — so a deactivated owner's caps stop
working on the next content request.
"""
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlmodel import Session, select

from app.models.artifacts.artifact import (
    Artifact,
    ArtifactSourceKind,
    ArtifactVisibility,
)
from app.models.artifacts.artifact_grant import ArtifactGrant
from app.models.artifacts.artifact_link import ArtifactLink
from app.models.users.user import User
from app.services.users.access_policy_service import AccessPolicyService


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class ArtifactAccess:
    @staticmethod
    def can_view(db: Session, artifact: Artifact, user: User | None) -> bool:
        if user is None or not AccessPolicyService.is_account_valid(user):
            return False
        if artifact.owner_id == user.id:
            return True
        if artifact.visibility == ArtifactVisibility.INSTANCE:
            return True
        grant_id = db.exec(
            select(ArtifactGrant.id).where(
                ArtifactGrant.artifact_id == artifact.id,
                ArtifactGrant.user_id == user.id,
            )
        ).first()
        return grant_id is not None

    @staticmethod
    def can_manage(artifact: Artifact, user: User | None) -> bool:
        return (
            user is not None
            and AccessPolicyService.is_account_valid(user)
            and artifact.owner_id == user.id
        )

    @staticmethod
    def link_usable(link: ArtifactLink, artifact: Artifact | None, now: datetime | None = None) -> bool:
        """Not revoked, not expired, and its artifact still exists."""
        if artifact is None or link.artifact_id != artifact.id:
            return False
        if link.revoked_at is not None:
            return False
        if link.expires_at is not None:
            if _aware(link.expires_at) <= (now or datetime.now(UTC)):
                return False
        return True

    @staticmethod
    def owner_valid(db: Session, artifact: Artifact) -> bool:
        """The owner's account is still valid (a deactivated owner's links die too)."""
        owner = db.get(User, artifact.owner_id)
        return owner is not None and AccessPolicyService.is_account_valid(owner)

    @classmethod
    def link_serves(
        cls, db: Session, link: ArtifactLink, artifact: Artifact | None, now: datetime | None = None
    ) -> bool:
        """``link_usable`` and the owner account is valid: may this link show content?"""
        return (
            artifact is not None
            and cls.link_usable(link, artifact, now)
            and cls.owner_valid(db, artifact)
        )


@dataclass(frozen=True)
class AgentPublishPolicy:
    """A8, the one gate for publishing out of an agent environment.

    The publish path offers create and republish only: its request models
    forbid any other field, and nothing on it calls a visibility, grant, link
    or delete operation, so a new artifact keeps the model default
    ``visibility='private'`` until a person shares it. New artifacts take
    ``source_kind`` (``core_agent``) from here.
    """

    source_kind: str = ArtifactSourceKind.CORE_AGENT

    def may_republish(
        self, artifact: Artifact, owner_id: uuid.UUID, agent_id: uuid.UUID
    ) -> bool:
        """Only a ``core_agent`` artifact of this owner, published from this agent."""
        return (
            artifact.owner_id == owner_id
            and artifact.source_kind == self.source_kind
            and artifact.source_agent_id == agent_id
        )


_AGENT_PUBLISH_POLICY = AgentPublishPolicy()


def agent_publish_policy() -> AgentPublishPolicy:
    return _AGENT_PUBLISH_POLICY
