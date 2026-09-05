"""The one builder for :class:`UserPublic`.

``UserPublic` carries several fields that do not exist on the ``User`` row:
the derived ``has_*`` enrolment flags, the confirmation-resend timestamp, and
``can_change_email`` (an access-policy fact). A route that returns the ORM
object and lets FastAPI coerce it gets whatever defaults the model declares
for those — which for a policy-shaped field means the permissive answer,
silently. So every endpoint answering with a ``UserPublic`` goes through
``user_to_public``.

Split out of ``routes/users.py`` for the same reason ``_mfa_errors`` was:
``routes/login.py`` needs it too, and a private helper imported across route
modules is how two projections of the same object start disagreeing.
"""
from __future__ import annotations

from sqlmodel import Session

from app.models import User, UserPublic
from app.services.users.access_policy_service import AccessPolicyService
from app.services.users.email_confirmation_service import EmailConfirmationService
from app.services.users.mfa_service import MfaService


def user_to_public(
    session: Session, user: User, *, can_change_email: bool | None = None
) -> UserPublic:
    """Build :class:`UserPublic` for ``user``, populating the derived fields.

    ``can_change_email`` is an instance-wide policy fact, identical for every
    user in a response. Pass it in when projecting a list so the policy is
    resolved once instead of once per row.
    """
    if can_change_email is None:
        can_change_email = AccessPolicyService.can_change_email(session)
    return UserPublic(
        **user.model_dump(),
        can_change_email=can_change_email,
        has_google_account=bool(user.google_id),
        has_password=bool(user.hashed_password),
        has_passkey=MfaService.has_passkey(session=session, user_id=user.id),
        has_totp=MfaService.has_totp(session=session, user_id=user.id),
        confirmation_resend_available_at=(
            None
            if user.email_confirmed
            else EmailConfirmationService.resend_available_at(user)
        ),
    )
