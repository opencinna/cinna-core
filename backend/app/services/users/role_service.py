"""RoleService — agent-user / agent-developer / admin role management.

Phase 3 of the Agent Bundles & Installs plan.

The role enum is the gate for *building-mode* features (agent CRUD,
publishing, building-mode session start, sync-prompts).  ``admin`` is
the existing superuser tier; ``role == 'admin'`` is kept in sync with
``is_superuser=True``.  Non-superusers default to ``agent-user`` and an
admin can promote to ``agent-developer`` from the admin Roles tab.

Service responsibilities:

* ``set_role`` — validate the requested role, validate the
  ``admin`` invariant (cannot demote a superuser via the role endpoint),
  persist, and emit a ``USER_ROLE_CHANGED`` event scoped to the target
  user.

* ``require_developer`` / ``require_user`` — small helpers used by route
  guards.  ``require_developer`` permits ``agent-developer`` and
  ``admin``; ``require_user`` is a sanity hook that any authenticated
  user satisfies (kept for symmetry — useful for routes that want to
  document "user-facing only, never desktop-token").

Errors are returned as ``ValueError`` so that route handlers can map
them to the right HTTPException codes.  The "you cannot change your
own role" guard is enforced at the route layer, where ``CurrentUser``
is in scope.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlmodel import Session

from app.models import User
from app.models.events.event import EventType
from app.models.users.user import (
    DEVELOPER_OR_ADMIN_ROLES,
    UserRole,
    VALID_USER_ROLES,
)

logger = logging.getLogger(__name__)


class RoleService:
    """Static service for role transitions and guard helpers."""

    # ── Guards ─────────────────────────────────────────────────────

    @staticmethod
    def is_developer(user: User) -> bool:
        """``True`` for ``agent-developer`` or ``admin`` roles.

        Superusers always satisfy the predicate even if their ``role``
        field is mid-migration / out of sync — defense-in-depth.
        """
        if user.is_superuser:
            return True
        return user.role in DEVELOPER_OR_ADMIN_ROLES

    @staticmethod
    def require_developer(user: User) -> None:
        """Raise ``PermissionError`` if the user is not a developer/admin.

        Routes translate this to ``HTTPException(status_code=403, ...)``.
        """
        if not RoleService.is_developer(user):
            raise PermissionError(
                "This action requires the agent-developer role. "
                "Ask an admin to promote your account."
            )

    @staticmethod
    def require_user(user: User) -> None:
        """No-op sanity check — any authenticated, active user passes.

        Provided for symmetry with ``require_developer`` so call sites
        can self-document the intended audience of an endpoint.
        """
        if not user.is_active:
            raise PermissionError("Inactive user")
        # All roles (including ``agent-user``) satisfy this guard.
        return None

    # ── Mutation ───────────────────────────────────────────────────

    @staticmethod
    async def set_role(
        *,
        session: Session,
        target_user: User,
        new_role: str,
        changed_by: User,
    ) -> User:
        """Change ``target_user.role`` and emit a WS event.

        Validation rules:

        * ``new_role`` must be one of the three known enum values.
        * The caller may not change their own role (route should enforce
          this too — second check here is defense-in-depth).
        * Cannot promote / demote into ``admin`` via this endpoint —
          ``admin`` is reserved for superusers and is kept in sync with
          ``is_superuser`` outside this service (e.g., by direct SQL or
          a future user-edit flow).  Concretely: if ``target_user`` is
          a superuser, the role can only be set to ``admin``; if not,
          the role must be ``agent-user`` or ``agent-developer``.

        Raises ``ValueError`` on rule violations.

        Returns the refreshed ``User`` row.
        """
        if new_role not in VALID_USER_ROLES:
            raise ValueError(
                f"Invalid role '{new_role}'. Must be one of {VALID_USER_ROLES}."
            )

        if target_user.id == changed_by.id:
            raise ValueError("Cannot change your own role")

        # Keep the superuser ⇔ admin invariant.
        if target_user.is_superuser and new_role != UserRole.ADMIN.value:
            raise ValueError(
                "Cannot demote a superuser via the role endpoint. "
                "Revoke superuser status first."
            )
        if not target_user.is_superuser and new_role == UserRole.ADMIN.value:
            raise ValueError(
                "Cannot promote to admin via the role endpoint. "
                "Grant superuser status instead."
            )

        previous_role = target_user.role
        if previous_role == new_role:
            return target_user

        target_user.role = new_role
        session.add(target_user)
        session.commit()
        session.refresh(target_user)

        await RoleService._emit_role_changed(
            user_id=target_user.id,
            new_role=new_role,
            previous_role=previous_role,
            changed_by_user_id=changed_by.id,
        )

        return target_user

    # ── Event ──────────────────────────────────────────────────────

    @staticmethod
    async def _emit_role_changed(
        *,
        user_id: uuid.UUID,
        new_role: str,
        previous_role: str,
        changed_by_user_id: uuid.UUID,
    ) -> None:
        """Fire ``USER_ROLE_CHANGED`` to the target user's room.

        Failure to emit is logged but never raised — the role change
        itself has already been persisted, and the user will pick up
        the new role on next ``readUserMe`` regardless of WS delivery.
        """
        try:
            from app.services.events.event_service import event_service

            await event_service.emit_event(
                event_type=EventType.USER_ROLE_CHANGED,
                model_id=user_id,
                user_id=user_id,
                meta={
                    "user_id": str(user_id),
                    "new_role": new_role,
                    "previous_role": previous_role,
                    "changed_by_user_id": str(changed_by_user_id),
                },
            )
        except Exception as e:  # pragma: no cover — best-effort emit
            logger.warning(
                "Failed to emit USER_ROLE_CHANGED for user %s: %s",
                user_id,
                e,
            )

    # ── Bootstrapping ──────────────────────────────────────────────

    @staticmethod
    def derive_default_role(*, is_superuser: bool) -> str:
        """Default role for a freshly created user.

        Mirrors the migration backfill so newly seeded users land in
        the same shape as legacy rows.
        """
        return UserRole.ADMIN.value if is_superuser else UserRole.USER.value


__all__ = ["RoleService"]
