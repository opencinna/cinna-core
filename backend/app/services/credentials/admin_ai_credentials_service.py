"""
Admin AI Credentials Service.

Lets a superuser provision an :class:`AICredential` *for* a target user
(``owner_id = target.id``) — a per-user credential, NOT a shared key. Because
each row is owned by the user, it automatically participates in every existing
per-user plumbing path (default resolution, profile auto-sync, environment
linking, listing). The only behavioral divergence is that admin-managed rows
are read-only through the user-facing CRUD (enforced in
:class:`AICredentialsService`).

This service does NOT build a parallel resolution path: it delegates to the
existing :data:`ai_credentials_service` with ``user_id = owner_id`` so all
per-user invariants (one-default-per-type, profile sync, ``default_sdk_*``
wiring) run for the *target* user.
"""
import logging
import uuid

from fastapi import HTTPException
from sqlmodel import Session, select

from app.models.credentials.ai_credential import (
    AICredential,
    AICredentialCreate,
    AICredentialUpdate,
    AICredentialType,
    AdminAICredentialCreate,
    AdminAICredentialProvisionResult,
    AdminAICredentialPublic,
    AdminProvisionSkip,
)
from app.models.users.user import User
from app.services.credentials.ai_credentials_service import (
    ai_credentials_service,
)
from app.services.environments.sdk_constants import (
    is_credential_compatible_with_sdk,
)

logger = logging.getLogger(__name__)


# Which SDK engine string to compose for a credential type, per mode, mirroring
# the AddEnvironment SDK composition: claude-code for anthropic/minimax,
# opencode/<provider> for the OpenCode-only providers.
_TYPE_TO_SDK_ENGINE: dict[AICredentialType, str] = {
    AICredentialType.ANTHROPIC: "claude-code/anthropic",
    AICredentialType.MINIMAX: "claude-code/minimax",
    AICredentialType.OPENAI: "opencode/openai",
    AICredentialType.GOOGLE: "opencode/google",
    AICredentialType.OPENAI_COMPATIBLE: "opencode/openai_compatible",
}


class AdminAICredentialService:
    """Superuser-only provisioning surface over AI credentials."""

    # ------------------------------------------------------------------ #
    # Projection
    # ------------------------------------------------------------------ #

    def _to_admin_public(
        self, session: Session, credential: AICredential
    ) -> AdminAICredentialPublic:
        """Project a credential into the admin-facing model (adds owner_id +
        managed_by_id on top of the shared public projection)."""
        public = ai_credentials_service._to_public(credential, session)
        return AdminAICredentialPublic(
            **public.model_dump(),
            owner_id=credential.owner_id,
            managed_by_id=credential.managed_by_id,
        )

    # ------------------------------------------------------------------ #
    # Provisioning
    # ------------------------------------------------------------------ #

    def provision_for_users(
        self, session: Session, admin: User, data: AdminAICredentialCreate
    ) -> AdminAICredentialProvisionResult:
        """Create one admin-managed AICredential per valid target user.

        Genuinely invalid targets (unknown / inactive user) are recorded in the
        ``skipped`` list rather than failing the whole call. Each created row is
        owned by its target user with ``is_admin_managed=True`` and
        ``managed_by_id=admin.id``. Optionally sets the row as the user's
        default and wires the user's ``default_sdk_*`` preferences.

        Per-type field validation is delegated to the existing
        :meth:`AICredentialsService.create_credential` (HTTP 400 on a bad type
        payload) — that fails the whole call, since a malformed key payload is
        not a per-target condition.
        """
        created: list[AdminAICredentialPublic] = []
        skipped: list[AdminProvisionSkip] = []

        # De-duplicate target ids while preserving order.
        seen: set[uuid.UUID] = set()
        target_ids = [
            uid for uid in data.target_user_ids
            if not (uid in seen or seen.add(uid))
        ]

        for target_id in target_ids:
            target = session.get(User, target_id)
            if target is None:
                skipped.append(
                    AdminProvisionSkip(user_id=target_id, reason="user_not_found")
                )
                continue
            if not target.is_active:
                skipped.append(
                    AdminProvisionSkip(user_id=target_id, reason="user_inactive")
                )
                continue

            credential = self._provision_one(session, admin, target, data)
            created.append(self._to_admin_public(session, credential))

        return AdminAICredentialProvisionResult(created=created, skipped=skipped)

    def _provision_one(
        self,
        session: Session,
        admin: User,
        target: User,
        data: AdminAICredentialCreate,
    ) -> AICredential:
        """Create a single admin-managed credential for ``target`` and apply
        the optional default / SDK-default wiring. Returns the DB row."""
        # 1. Create the row through the existing per-user pipeline (validates
        #    type-specific fields, encrypts, etc.).
        public = ai_credentials_service.create_credential(
            session,
            target.id,
            AICredentialCreate(
                name=data.name,
                type=data.type,
                api_key=data.api_key,
                base_url=data.base_url,
                model=data.model,
                expiry_notification_date=data.expiry_notification_date,
            ),
        )

        # 2. Stamp the admin-managed markers.
        credential = session.get(AICredential, public.id)
        credential.is_admin_managed = True
        credential.managed_by_id = admin.id
        session.add(credential)
        session.commit()
        session.refresh(credential)

        # 3. Optionally set as the owner's default (reuses set_default → profile
        #    auto-sync). Never auto-sets two defaults for the same type.
        if data.set_as_default:
            ai_credentials_service.set_default(session, credential.id, target.id)
            session.refresh(credential)

        # 4. Optionally wire the user's default SDK preferences.
        if data.set_user_sdk_defaults:
            self._apply_sdk_defaults(session, target, credential, data)

        logger.info(
            "Admin %s provisioned AI credential %s (type=%s) for user %s",
            admin.id, credential.id, credential.type, target.id,
        )
        return credential

    def _apply_sdk_defaults(
        self,
        session: Session,
        target: User,
        credential: AICredential,
        data: AdminAICredentialCreate,
    ) -> None:
        """Set the target user's ``default_sdk_*`` + ``default_ai_credential_*_id``
        for the requested modes, mirroring the AddEnvironment SDK composition.

        A mode whose composed engine is incompatible with the credential type is
        skipped (not a hard error) so a partial provision still succeeds.
        """
        sdk_engine = _TYPE_TO_SDK_ENGINE.get(credential.type)
        if not sdk_engine:
            return

        for mode in data.sdk_default_modes:
            if mode not in ("conversation", "building"):
                continue
            if not is_credential_compatible_with_sdk(sdk_engine, credential.type):
                # Should not happen given the static map, but stay defensive.
                continue
            if mode == "conversation":
                target.default_sdk_conversation = sdk_engine
                target.default_ai_credential_conversation_id = credential.id
            else:
                target.default_sdk_building = sdk_engine
                target.default_ai_credential_building_id = credential.id

        session.add(target)
        session.commit()
        session.refresh(target)

    # ------------------------------------------------------------------ #
    # Listing
    # ------------------------------------------------------------------ #

    def list_managed(
        self,
        session: Session,
        admin: User,
        target_user_id: uuid.UUID | None = None,
    ) -> list[AdminAICredentialPublic]:
        """List admin-managed credentials fleet-wide (any superuser sees all
        admin-managed rows), optionally scoped to a single target user."""
        statement = select(AICredential).where(
            AICredential.is_admin_managed == True  # noqa: E712
        )
        if target_user_id is not None:
            statement = statement.where(AICredential.owner_id == target_user_id)
        statement = statement.order_by(AICredential.created_at.desc())
        rows = session.exec(statement).all()
        return [self._to_admin_public(session, row) for row in rows]

    def _get_managed_or_404(
        self, session: Session, credential_id: uuid.UUID
    ) -> AICredential:
        """Fetch an admin-managed credential or 404. A self-created (non
        admin-managed) row of some user is NOT reachable through this surface."""
        credential = session.get(AICredential, credential_id)
        if credential is None or not credential.is_admin_managed:
            raise HTTPException(
                status_code=404, detail="Admin-managed AI credential not found"
            )
        return credential

    def get_managed(
        self, session: Session, admin: User, credential_id: uuid.UUID
    ) -> AdminAICredentialPublic:
        """Fetch a single admin-managed credential."""
        credential = self._get_managed_or_404(session, credential_id)
        return self._to_admin_public(session, credential)

    # ------------------------------------------------------------------ #
    # Mutation (admin bypasses the user read-only guard via admin_override)
    # ------------------------------------------------------------------ #

    def update_managed(
        self,
        session: Session,
        admin: User,
        credential_id: uuid.UUID,
        data: AICredentialUpdate,
    ) -> AdminAICredentialPublic:
        """Admin edits an admin-managed row on behalf of its owner."""
        credential = self._get_managed_or_404(session, credential_id)
        ai_credentials_service.update_credential(
            session, credential.id, credential.owner_id, data, admin_override=True
        )
        session.refresh(credential)
        return self._to_admin_public(session, credential)

    def delete_managed(
        self,
        session: Session,
        admin: User,
        credential_id: uuid.UUID,
        force: bool = False,
    ) -> None:
        """Admin deletes an admin-managed row. Reuses the Tier-2 bundle
        blast-radius check (HTTP 409 unless ``force``)."""
        credential = self._get_managed_or_404(session, credential_id)
        ai_credentials_service.delete_credential(
            session,
            credential.id,
            credential.owner_id,
            force=force,
            admin_override=True,
        )

    def set_managed_default(
        self, session: Session, admin: User, credential_id: uuid.UUID
    ) -> AdminAICredentialPublic:
        """Set the admin-managed row as the owner-user's default for its type."""
        credential = self._get_managed_or_404(session, credential_id)
        ai_credentials_service.set_default(
            session, credential.id, credential.owner_id
        )
        session.refresh(credential)
        return self._to_admin_public(session, credential)


# Singleton instance (matches ai_credentials_service pattern).
admin_ai_credentials_service = AdminAICredentialService()
