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

from sqlalchemy.orm.util import object_state
from sqlmodel import Session

from app.models import User, UserPublic
from app.services.users.access_policy_service import AccessPolicyService
from app.services.users.email_confirmation_service import EmailConfirmationService
from app.services.users.mfa_service import MfaService


def _load_missing_columns(user: User) -> None:
    """Reload any mapped column ``model_dump()`` would otherwise omit.

    ``model_dump()`` is the one way of reading a SQLModel row that does **not**
    go through SQLAlchemy's instrumented descriptors: it reads ``__dict__``.
    Ordinary attribute access on an expired instance silently emits a SELECT
    and returns the value; ``model_dump()`` just returns the keys that happen
    to still be in ``__dict__`` — which, right after a ``commit()``, is none of
    them. The row's ``id`` and ``email`` are then simply absent from the
    payload and ``UserPublic`` fails validation with a 500.

    That is not a hypothetical. ``UserService.create_account`` refreshes the
    new row and then runs auto-provisioning, which commits a child credential
    and an audit row; the account-creating routes hand the now-expired instance
    straight to this builder. It was invisible for as long as it was, because
    with outbound email configured the routes call
    ``EmailConfirmationService.send_confirmation_email(user=user)`` in between,
    and that touches an attribute, which un-expires the instance. Turn SMTP off
    — the default for a fresh install — and creating an account 500s *after*
    the row is committed: the person exists and is told they do not.

    The repair belongs here rather than at that one call site because the trap
    belongs to ``model_dump()``, not to account creation. Every caller of this
    builder hands over a row it did not necessarily load itself, and any commit
    anywhere upstream arms the same failure. One reload at the single builder
    retires the class; a ``session.refresh`` in ``create_account`` would only
    have retired today's instance of it.

    THE CONDITION IS "A COLUMN IS MISSING", NOT "THE ROW IS EXPIRED"
    ----------------------------------------------------------------
    Expiry is only the common *cause*. What ``model_dump()`` actually cares
    about is whether a mapped column key is absent from ``__dict__``, and a
    ``load_only()`` / deferred load produces exactly that without ever setting
    the expired flag. So the set is computed directly and the reload asks for
    only the keys that are missing.

    ``state.unloaded`` is the tempting shortcut and the wrong one: it always
    contains the relationship keys, which ``model_dump()`` never reads, so
    using it would force a full-row SELECT for every row of the users list.
    Restricting to ``mapper.column_attrs`` keeps the list path free — those
    rows come straight off a ``select`` with every column loaded, so the set is
    empty and no SQL is emitted.

    Reloads through the instance's *own* session (``state.session``) rather
    than the one passed in: they are the same everywhere today, and a refresh
    through a foreign session raises.

    ONLY A PERSISTENT ROW CAN BE REPAIRED
    -------------------------------------
    ``state.persistent`` gates the whole thing, and the three states it
    excludes are excluded for different reasons. A *pending* row has a session
    but no database row yet — ``refresh`` on it raises, and its ``__dict__`` is
    already the authoritative answer. A *transient* row has neither. A
    *detached* row is the uncomfortable one: if it is also expired, this
    function cannot fix it and the caller still gets the 500. That is not a
    silently tolerated failure mode so much as a statement of the builder's
    contract — it projects a live row belonging to ``session``, and no call
    site passes anything else. If one ever does, the 500 it gets is the same
    one it would have got before this function existed.
    """
    state = object_state(user)
    owning_session = state.session
    if owning_session is None or not state.persistent:
        return
    missing = {
        attr.key for attr in state.mapper.column_attrs
    } - user.__dict__.keys()
    if missing:
        owning_session.refresh(user, missing)


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
    # Before the ``model_dump()`` below, never after: see
    # :func:`_load_missing_columns`.
    _load_missing_columns(user)
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
