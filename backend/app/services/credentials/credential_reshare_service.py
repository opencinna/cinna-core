"""Release the shares recipients made by re-using a credential in their packages.

A recipient T who embeds a re-usable shared connection in their own bundle or
skill creates ``CredentialShare`` rows with ``shared_by_user_id = T`` for the
people who install it. Those rows ride on T's own access and on the owner's
re-use consent; when either goes, they go too (D6):

- T lost every path to the credential → ``release_by_sharers({T})``. Hooked
  into ``CredentialsService.unlink_credential_from_revoked_recipients``, the
  one point every loss path reaches (direct revoke, sharing off, group revoke,
  member removal, group delete).
- The owner turned re-use off → ``release_all``.

Releasing is not a revocation: no ``CredentialShareRevocation`` row is
written, so the next install or update re-adds the share once the right is
back. Recipients who still hold another path keep their links
(``users_without_access`` decides).
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable

from sqlmodel import Session, col, select

from app.models.credentials.credential import Credential
from app.models.credentials.credential_share import (
    SHARE_SOURCE_SKILL_LOCAL_INSTALL,
    CredentialShare,
)

logger = logging.getLogger(__name__)

#: Share sources an install writes; only these are released. A direct share
#: is always the owner's, never a re-sharer's.
RELEASABLE_SOURCES: frozenset[str] = frozenset(
    {"bundle_install", "skill_install", SHARE_SOURCE_SKILL_LOCAL_INSTALL}
)


class CredentialReshareService:
    @staticmethod
    async def release_by_sharers(
        session: Session,
        credential_id: uuid.UUID,
        sharer_ids: Iterable[uuid.UUID],
        *,
        _seen: set[uuid.UUID] | None = None,
    ) -> None:
        """Delete the install shares ``sharer_ids`` made as non-owners, then
        unlink the recipients who lost every path (which recurses into the
        re-shares those recipients made). ``_seen`` guards cycles."""
        seen = _seen if _seen is not None else set()
        sharers = {uid for uid in sharer_ids if uid is not None} - seen
        if not sharers:
            return
        seen.update(sharers)

        credential = session.get(Credential, credential_id)
        if credential is None:
            return
        sharers.discard(credential.owner_id)
        if not sharers:
            return

        shares = session.exec(
            select(CredentialShare).where(
                CredentialShare.credential_id == credential_id,
                col(CredentialShare.shared_by_user_id).in_(sharers),
                col(CredentialShare.source).in_(RELEASABLE_SOURCES),
            )
        ).all()
        if not shares:
            return

        recipient_ids = [share.shared_with_user_id for share in shares]
        for share in shares:
            session.delete(share)
        session.commit()
        logger.info(
            "Credential %s: released %s re-share(s) made by %s sharer(s)",
            credential_id,
            len(shares),
            len(sharers),
        )

        from app.services.credentials.credentials_service import (
            CredentialsService,
        )

        await CredentialsService.unlink_credential_from_revoked_recipients(
            session=session,
            credential_id=credential_id,
            recipient_user_ids=recipient_ids,
            _seen=seen,
        )

    @staticmethod
    async def release_all(session: Session, credential_id: uuid.UUID) -> None:
        """Release every re-share of the credential (the owner turned re-use
        off). The re-sharers themselves keep their own shares."""
        credential = session.get(Credential, credential_id)
        if credential is None:
            return
        sharer_ids = session.exec(
            select(CredentialShare.shared_by_user_id)
            .distinct()
            .where(
                CredentialShare.credential_id == credential_id,
                CredentialShare.shared_by_user_id != credential.owner_id,
            )
        ).all()
        await CredentialReshareService.release_by_sharers(
            session, credential_id, list(sharer_ids)
        )

    @staticmethod
    def snapshot_reshares_by(
        session: Session, sharer_id: uuid.UUID
    ) -> dict[uuid.UUID, list[uuid.UUID]]:
        """``{credential_id: [recipient ids]}`` of the shares ``sharer_id``
        made as a non-owner. Read before deleting the sharer's account: the
        ``shared_by_user_id`` cascade removes the rows with it."""
        rows = session.exec(
            select(CredentialShare.credential_id, CredentialShare.shared_with_user_id)
            .join(Credential, col(Credential.id) == col(CredentialShare.credential_id))
            .where(
                CredentialShare.shared_by_user_id == sharer_id,
                Credential.owner_id != sharer_id,
            )
        ).all()
        out: dict[uuid.UUID, list[uuid.UUID]] = {}
        for credential_id, recipient_id in rows:
            out.setdefault(credential_id, []).append(recipient_id)
        return out

    @staticmethod
    async def release_after_sharer_deleted(
        session: Session, snapshot: dict[uuid.UUID, list[uuid.UUID]]
    ) -> None:
        """Unlink and re-sync the recipients of a deleted re-sharer who lost
        every path (their share rows went with the account). Best effort:
        logs and continues, the account deletion has already committed."""
        from app.services.credentials.credentials_service import (
            CredentialsService,
        )

        for credential_id, recipient_ids in snapshot.items():
            try:
                await CredentialsService.unlink_credential_from_revoked_recipients(
                    session=session,
                    credential_id=credential_id,
                    recipient_user_ids=recipient_ids,
                )
            except Exception:
                logger.exception(
                    "Credential %s: releasing re-shares of a deleted user failed",
                    credential_id,
                )
