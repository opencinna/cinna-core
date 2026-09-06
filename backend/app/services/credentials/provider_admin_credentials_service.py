"""Superuser CRUD over the provider administration secrets.

The secret this service stores can create and destroy API keys for a whole
provider organisation. Three rules follow from that, and they are the reason this
is a service of its own rather than a few more branches in the AI-credential one:

1. **The secret leaves this module only as an argument to a provider call.** No
   projection carries it, no endpoint returns it, nothing links it to an
   environment, and it is absent from ``/external/account-config`` — structurally,
   because every one of those paths is keyed to ``ai_credential.id`` and this is a
   different table.
2. **Deleting it is gated on what it can still revoke.** Losing the secret does
   not only stop new keys being minted; it strands every key already minted with
   it, since the only way to destroy one is an administration call authenticated
   by this secret. So the delete is refused while anything still depends on it,
   and forcing it is an explicit, audited act.
3. **Verify answers both questions at once.** "Is the secret good" and "is the
   project capped" fail independently, and an admin who fixes one wants to see the
   other without a second round trip.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlmodel import Session, col, func, select

from app.core.security import decrypt_field, encrypt_field
from app.models.credentials.managed_ai_credential import ManagedAICredential
from app.models.credentials.managed_ai_credential_membership import (
    ManagedAICredentialMembership,
    holds_provider_key_clause,
)
from app.models.credentials.provider_admin_credential import (
    ProviderAdminCredential,
    ProviderAdminCredentialConfig,
    ProviderAdminCredentialCreate,
    ProviderAdminCredentialPublic,
    ProviderAdminCredentialUpdate,
    ProviderAdminCredentialVerifyResult,
)
from app.models.users.user import User
from app.services.ai_providers import registry
from app.services.ai_providers.base import KeyProvisioner, ProviderAdminError

logger = logging.getLogger(__name__)


class ProviderAdminCredentialInUseError(Exception):
    """Deleting this credential would strand keys only it can revoke."""

    def __init__(
        self, *, minting_credential_count: int, live_minted_key_count: int
    ) -> None:
        self.minting_credential_count = minting_credential_count
        self.live_minted_key_count = live_minted_key_count
        super().__init__(
            f"{minting_credential_count} managed credential(s) mint through "
            f"this secret and {live_minted_key_count} live key(s) can only be "
            "revoked with it."
        )


class ProviderAdminCredentialsService:
    """Superuser-only CRUD + verification for provider administration secrets."""

    def _get_or_404(
        self, session: Session, credential_id: uuid.UUID
    ) -> ProviderAdminCredential:
        record = session.get(ProviderAdminCredential, credential_id)
        if record is None:
            raise HTTPException(
                status_code=404, detail="Provider admin credential not found"
            )
        return record

    def decrypt_secret(self, record: ProviderAdminCredential) -> str:
        """The administration secret, in plaintext.

        The only door out of this table. Deliberately not a projection field and
        deliberately not reachable from a route: every caller is a provider call
        in ``KeyProvisioningService`` or in this service's own verify.
        """
        return decrypt_field(record.encrypted_secret)

    def _provisioner(self, record: ProviderAdminCredential) -> KeyProvisioner:
        adapter = registry.find_adapter(record.provider_type)
        provisioner = adapter.key_provisioner if adapter is not None else None
        if provisioner is None:
            label = adapter.label if adapter is not None else record.provider_type
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{label} cannot create API keys through its administration "
                    "API."
                ),
            )
        return provisioner

    # ------------------------------------------------------------------ #
    # Dependency counts — the delete gate's input
    # ------------------------------------------------------------------ #

    def usage_counts(
        self, session: Session, credential_id: uuid.UUID
    ) -> tuple[int, int]:
        """``(managed records minting through it, live keys only it can revoke)``.

        Two counts rather than one because they mean different things to the
        admin pressing Delete: the first is configuration they would have to
        redo, the second is keys that would stay live in the provider forever.
        """
        parent_ids = session.exec(
            select(ManagedAICredential.id).where(
                ManagedAICredential.provider_admin_credential_id == credential_id
            )
        ).all()
        if not parent_ids:
            return 0, 0
        live = session.exec(
            select(func.count()).select_from(ManagedAICredentialMembership).where(
                col(ManagedAICredentialMembership.managed_credential_id).in_(
                    parent_ids
                ),
                # The one predicate, defined on the row it describes. Account
                # deletion and account deactivation ask the same question and
                # call the same thing; a second copy here is how they came to
                # disagree about ``failed`` members.
                holds_provider_key_clause(),
            )
        ).one()
        return len(parent_ids), int(live)

    # ------------------------------------------------------------------ #
    # CRUD
    # ------------------------------------------------------------------ #

    def create(
        self,
        session: Session,
        admin: User,
        data: ProviderAdminCredentialCreate,
    ) -> ProviderAdminCredentialPublic:
        adapter = registry.find_adapter(data.provider_type)
        if adapter is None or not adapter.supports_minting:
            label = adapter.label if adapter is not None else str(data.provider_type)
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{label} cannot create API keys through its administration "
                    "API, so there is nothing an administration secret would be "
                    "used for."
                ),
            )
        now = datetime.now(timezone.utc)
        record = ProviderAdminCredential(
            name=data.name,
            provider_type=data.provider_type,
            encrypted_secret=encrypt_field(data.secret),
            config=data.config.model_dump(),
            created_by_id=admin.id,
            created_at=now,
            updated_at=now,
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        return self.to_public(session, record)

    def list(
        self, session: Session, admin: User
    ) -> list[ProviderAdminCredentialPublic]:
        records = session.exec(
            select(ProviderAdminCredential).order_by(
                col(ProviderAdminCredential.created_at).desc()
            )
        ).all()
        return [self.to_public(session, record) for record in records]

    def get(
        self, session: Session, admin: User, credential_id: uuid.UUID
    ) -> ProviderAdminCredentialPublic:
        return self.to_public(session, self._get_or_404(session, credential_id))

    def update(
        self,
        session: Session,
        admin: User,
        credential_id: uuid.UUID,
        data: ProviderAdminCredentialUpdate,
    ) -> ProviderAdminCredentialPublic:
        """Partial update. Every omitted field is left alone.

        Replacing the secret **or the config** clears ``last_verified_at``. A
        verification is of a (secret, project) pair, not of a secret: changing
        ``project_id`` points the record at a project whose spend limit nobody has
        checked, and a surviving stamp would report "verified, capped" about it.
        The two clears are one clear for that reason — an asymmetry here is a
        stamp that quietly means something different depending on which field was
        edited last.
        """
        record = self._get_or_404(session, credential_id)
        if data.name is not None:
            record.name = data.name
        config_changed = (
            data.config is not None and data.config.model_dump() != record.config
        )
        if data.config is not None:
            record.config = data.config.model_dump()
        if data.secret is not None:
            record.encrypted_secret = encrypt_field(data.secret)
        if data.secret is not None or config_changed:
            record.last_verified_at = None
            record.last_verify_error = None
        record.updated_at = datetime.now(timezone.utc)
        session.add(record)
        session.commit()
        session.refresh(record)
        return self.to_public(session, record)

    def delete(
        self,
        session: Session,
        admin: User,
        credential_id: uuid.UUID,
        *,
        force: bool = False,
    ) -> tuple[int, int]:
        """Delete the record, refusing while anything still depends on it.

        Returns the ``(minting records, live keys)`` counts as they were at the
        moment of deletion, so a forced disconnect can be audited with **what it
        overrode** rather than merely that it was forced. This is the one action
        on the system that permanently strands live provider keys; "forced: true"
        on its own does not say how many.
        """
        record = self._get_or_404(session, credential_id)
        minting_count, live_count = self.usage_counts(session, credential_id)
        if (minting_count or live_count) and not force:
            raise ProviderAdminCredentialInUseError(
                minting_credential_count=minting_count,
                live_minted_key_count=live_count,
            )
        session.delete(record)
        session.commit()
        return minting_count, live_count

    # ------------------------------------------------------------------ #
    # Verification
    # ------------------------------------------------------------------ #

    async def verify(
        self, session: Session, admin: User, credential_id: uuid.UUID
    ) -> ProviderAdminCredentialVerifyResult:
        """Check the secret and the project's spend cap, and record the outcome.

        The cap is *read*, not applied: applying one is a setup action
        (:meth:`apply_spend_limit`) and must be a decision an admin makes, not a
        side effect of pressing Verify.
        """
        record = self._get_or_404(session, credential_id)
        provisioner = self._provisioner(record)
        secret = self.decrypt_secret(record)
        try:
            account_ref = await provisioner.verify_admin_access(
                secret, record.config
            )
            limit = await provisioner.verify_spend_limit(secret, record.config)
        except ProviderAdminError as exc:
            self._stamp_verification(session, record, error=exc.code)
            return ProviderAdminCredentialVerifyResult(ok=False, error=exc.code)

        if not limit.is_capped:
            # Not a cap. A limit row with a threshold reads as "capped" to anyone
            # checking the obvious field; ``enforcement.status`` is what says
            # whether it does anything, and minting is refused until it does.
            self._stamp_verification(session, record, error="project_not_capped")
            return ProviderAdminCredentialVerifyResult(
                ok=False,
                account_ref=account_ref,
                spend_limit_enforcing=False,
                spend_limit_cents=limit.threshold_cents,
                error="project_not_capped",
            )

        self._stamp_verification(session, record, error=None)
        return ProviderAdminCredentialVerifyResult(
            ok=True,
            account_ref=account_ref,
            spend_limit_enforcing=True,
            spend_limit_cents=limit.threshold_cents,
        )

    async def apply_spend_limit(
        self, session: Session, admin: User, credential_id: uuid.UUID
    ) -> ProviderAdminCredentialVerifyResult:
        """Set the configured spend limit on the project, at setup time."""
        record = self._get_or_404(session, credential_id)
        provisioner = self._provisioner(record)
        secret = self.decrypt_secret(record)
        try:
            limit = await provisioner.ensure_spend_limit(secret, record.config)
        except ProviderAdminError as exc:
            return ProviderAdminCredentialVerifyResult(ok=False, error=exc.code)
        return ProviderAdminCredentialVerifyResult(
            ok=limit.is_capped,
            spend_limit_enforcing=limit.is_capped,
            spend_limit_cents=limit.threshold_cents,
            error=None if limit.is_capped else "project_not_capped",
        )

    def _stamp_verification(
        self,
        session: Session,
        record: ProviderAdminCredential,
        *,
        error: str | None,
    ) -> None:
        record.last_verify_error = error
        if error is None:
            record.last_verified_at = datetime.now(timezone.utc)
        record.updated_at = datetime.now(timezone.utc)
        session.add(record)
        session.commit()
        session.refresh(record)

    # ------------------------------------------------------------------ #
    # Projection
    # ------------------------------------------------------------------ #

    def to_public(
        self, session: Session, record: ProviderAdminCredential
    ) -> ProviderAdminCredentialPublic:
        minting_count, live_count = self.usage_counts(session, record.id)
        return ProviderAdminCredentialPublic(
            id=record.id,
            name=record.name,
            provider_type=record.provider_type,
            config=ProviderAdminCredentialConfig(**(record.config or {})),
            has_secret=bool(record.encrypted_secret),
            last_verified_at=record.last_verified_at,
            last_verify_error=record.last_verify_error,
            created_by_id=record.created_by_id,
            minting_credential_count=minting_count,
            live_minted_key_count=live_count,
            delete_blocked=bool(minting_count or live_count),
            created_at=record.created_at,
            updated_at=record.updated_at,
        )


provider_admin_credentials_service = ProviderAdminCredentialsService()

__all__ = [
    "ProviderAdminCredentialInUseError",
    "ProviderAdminCredentialsService",
    "provider_admin_credentials_service",
]
