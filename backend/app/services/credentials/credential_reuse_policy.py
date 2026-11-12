"""Who may embed a credential as publisher-provided (PBP) in a bundle or skill.

The single trust predicate behind publish, install, update, preview and the
skill publish candidates. The owner may always provide their own credential.
A recipient may provide a credential shared with them only when the owner
allowed re-use inside bundles, the credential is still shareable, its type is
on the re-use allowlist (connections only) and the recipient still holds a
share path (direct or group).

Evaluated live on every call: a publisher that lost the right falls through to
``publisher_unavailable`` on the next install or update, as an owner who
turned sharing off does today.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from sqlmodel import Session

from app.models.credentials.credential import Credential, CredentialType
from app.models.users.user import User

#: Credential types a recipient may re-use inside their own bundles (D1).
#: Connections only: an ``agent_api`` row must also not be restricted
#: (external keys and orphans are identity-bound or dead).
BUNDLE_REUSABLE_TYPES: frozenset[CredentialType] = frozenset(
    {CredentialType.AGENT_API}
)

ReuseReason = Literal[
    "owned",
    "reuse_allowed",
    "not_owned",
    "reuse_off",
    "type_not_reusable",
    "no_share_path",
    "sharing_off",
]


@dataclass(frozen=True)
class ReuseVerdict:
    ok: bool
    reason: ReuseReason


def _is_restricted(session: Session, credential: Credential) -> bool:
    if credential.type != CredentialType.AGENT_API:
        return False
    from app.services.agent_api.agent_api_token_service import (
        AgentApiTokenService,
    )

    return AgentApiTokenService.is_restricted_agent_api_credential(
        session, credential.id
    )


def is_reusable_type(session: Session, credential: Credential) -> bool:
    """The credential's type is allowlisted and it is not a restricted row."""
    return credential.type in BUNDLE_REUSABLE_TYPES and not _is_restricted(
        session, credential
    )


def reuse_effective(session: Session, credential: Credential) -> bool:
    """The owner's consent is live: sharing on, re-use on, type re-usable.

    Says nothing about a particular publisher's share path.
    """
    return (
        bool(credential.allow_sharing)
        and bool(credential.allow_bundle_reuse)
        and is_reusable_type(session, credential)
    )


def reuse_effective_ids(
    session: Session, credentials: list[Credential]
) -> set[uuid.UUID]:
    """Batched :func:`reuse_effective`: one token-kind query for the page."""
    candidates = [
        c
        for c in credentials
        if c.allow_sharing and c.allow_bundle_reuse and c.type in BUNDLE_REUSABLE_TYPES
    ]
    if not candidates:
        return set()
    from app.models.agent_api.agent_api_token import AgentApiTokenKind
    from app.services.agent_api.agent_api_token_service import (
        AgentApiTokenService,
    )

    kinds = AgentApiTokenService.get_kinds_by_credential(
        session, [c.id for c in candidates]
    )
    return {
        c.id
        for c in candidates
        if c.type != CredentialType.AGENT_API
        or kinds.get(c.id) == AgentApiTokenKind.CONNECTION.value
    }


def publisher_may_provide(
    session: Session,
    credential: Credential,
    publisher_user_id: uuid.UUID | None,
) -> ReuseVerdict:
    """Whether ``publisher_user_id`` may embed ``credential`` as PBP (D3).

    Does not check ``allow_sharing`` for the owner: callers that need the
    owner's sharing consent (publish inference, install) test it themselves,
    as they did before re-use existed.
    """
    if publisher_user_id is None:
        return ReuseVerdict(False, "not_owned")
    if credential.owner_id == publisher_user_id:
        return ReuseVerdict(True, "owned")
    if not credential.allow_sharing:
        return ReuseVerdict(False, "sharing_off")
    if credential.type not in BUNDLE_REUSABLE_TYPES:
        return ReuseVerdict(False, "type_not_reusable")
    if not credential.allow_bundle_reuse:
        return ReuseVerdict(False, "reuse_off")
    if _is_restricted(session, credential):
        return ReuseVerdict(False, "type_not_reusable")
    from app.services.credentials.credential_share_service import (
        CredentialShareService,
    )

    # Owner-rooted paths only: a recipient of a re-share may not re-share
    # (one level deep from the owner).
    if not CredentialShareService.has_owner_rooted_share(
        session, credential, publisher_user_id
    ):
        return ReuseVerdict(False, "no_share_path")
    return ReuseVerdict(True, "reuse_allowed")


def user_display(user: User | None) -> str:
    """``full_name`` when set, else the email; ``"the owner"`` when unknown.

    The name is whitespace-normalised (trimmed, inner runs collapsed): it is
    interpolated into sentences, and a stored ``"Ada  Lovelace   "`` must not
    leak its padding into the copy. A blank name falls back to the email.
    """
    if user is None:
        return "the owner"
    name = " ".join((user.full_name or "").split())
    return name or user.email


def owner_display(session: Session, credential: Credential) -> str:
    """The credential owner's display name, for error copy."""
    return user_display(session.get(User, credential.owner_id))
