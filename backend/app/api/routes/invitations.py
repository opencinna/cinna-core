"""The public half of the invitation lifecycle: look one up, and accept it.

Two anonymous endpoints, and everything unusual about this module follows from
that one fact.

ONE ANSWER FOR EVERY REFUSAL
----------------------------
The caller supplies the token, so any difference in the response to a *forged*
token and a *real* one is a direct answer to "does this account exist and is it
invited". So there is exactly one:

* ``POST /invitations/lookup`` answers ``200 {"valid": false}`` — and nothing
  else — for a malformed token, a wrong signature, a cross-purpose token, a
  rotated-away ``jti``, an expired, revoked or already-accepted invitation, a
  deleted or deactivated user, and an address the admin has since changed.
  ``response_model_exclude_none=True`` is what makes "nothing else" literal:
  every other field is *absent*, not null. ``password_accepted`` in particular
  must be absent rather than ``false``, because a field that is always present
  with a boolean value is itself a channel — on an instance that allows
  password auth, ``true`` versus absent would separate a real token from a
  forged one.

* ``POST /invitations/accept`` answers ``400`` with one fixed detail string for
  every one of those, **including** "password auth is not available to you".
  There is no 403 branch on this route at all: the service raises a single
  :class:`InvitationInvalidError` for every reason and this module maps that
  one type. Nobody is surprised by the password case, because the page they
  came from asked ``lookup`` first, got ``password_accepted=false`` from the
  same predicate the service enforces, and did not render a form.

The corollary is that ``InvitationInvalidError`` must be the *only* exception
type these handlers translate. Adding a second — for a reason that seemed
harmless to disclose — reopens the oracle.

WHAT IS DELIBERATELY NOT DEFENDED
---------------------------------
A forged token is refused before any database access; a server-signed one
costs one indexed SELECT. That timing difference separates "forged" from
"signed by this server", which is not a secret: an attacker who can produce a
server-signed token already holds one. No constant-time floor is added for it,
on purpose.

NEITHER ENDPOINT SENDS EMAIL, EVER
----------------------------------
Both are anonymous. An anonymous endpoint that sends mail keyed on
attacker-chosen input is both a timing oracle and an outbound-mail amplifier.
Resending an invitation is superuser-only and lives in ``routes/users.py``.
"""
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.deps import SessionDep
from app.core.config import settings
from app.models import (
    AcceptInvitationRequest,
    InvitationLookupPublic,
    InvitationLookupRequest,
    LoginResponse,
    LoginToken,
)
from app.services.common.rate_limiter import RateLimiter, anonymous_caller_key
from app.services.users.auth_service import AuthService
from app.services.users.invitation_service import (
    InvitationInvalidError,
    InvitationService,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/invitations", tags=["invitations"])

# Keyed by source IP — the caller is anonymous, so their address is the only
# identity there is. Both endpoints take an attacker-chosen token and each one
# costs an indexed SELECT, which is why the budget is lower than the
# access-policy projection's: that answers one cached row identical for every
# caller.
#
# Process-local and unaffected by transaction rollback, so tests that exercise
# these routes need to reset it between cases.
_limiter = RateLimiter()


def _invitation_rate_limit(request: Request) -> None:
    """Per-caller backstop on the two anonymous invitation endpoints.

    Declared before ``SessionDep`` resolves so a throttled request costs no
    pool connection and no query.
    """
    retry_after = _limiter.check(
        anonymous_caller_key(request),
        settings.INVITATION_RATE_LIMIT_PER_MIN,
    )
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded",
            headers={"Retry-After": str(int(retry_after))},
        )


@router.post(
    "/lookup",
    response_model=InvitationLookupPublic,
    # Load-bearing, not a tidiness setting: it is what makes the invalid path
    # answer ``{"valid": false}`` with every other key *absent* rather than
    # null. See the module docstring.
    response_model_exclude_none=True,
    dependencies=[Depends(_invitation_rate_limit)],
)
def lookup_invitation(
    session: SessionDep, body: InvitationLookupRequest
) -> Any:
    """What the accept page may know before anyone has signed in.

    The token travels in the body rather than the path: a JWT in a URL is
    written to every proxy access log, kept in browser history and leaked
    through ``Referer``. Same shape, and same reason, as ``NewPassword.token``.

    Read-only. No row is written and no mail is sent on either path — a
    side effect here would be observable and would therefore be an oracle of
    its own.
    """
    return InvitationService.lookup(session, body.token)


@router.post(
    "/accept",
    response_model=LoginResponse,
    dependencies=[Depends(_invitation_rate_limit)],
)
async def accept_invitation(
    session: SessionDep, body: AcceptInvitationRequest
) -> Any:
    """Redeem an invitation by setting a password, and sign the person in.

    Answers with the same discriminated :class:`LoginResponse` as
    ``POST /login/access-token`` so the frontend narrows it with the code it
    already has. Only the ``LoginToken`` arm is reachable: the account is
    being given its first password in this very request, so it cannot have
    enrolled a second factor. Do not try to populate ``MfaChallenge`` here.

    Every token failure — bad signature, wrong purpose, unknown or rotated
    ``jti``, expired, revoked, already accepted, address mismatch, deactivated
    account, and password auth not being available to this person — is the
    same 400 with the same detail. A too-short password is a 422 from the
    request schema instead, which is fine: it does not depend on whether the
    token is real.
    """
    try:
        user = await InvitationService.accept_with_password(
            session,
            token=body.token,
            password=body.password,
            full_name=body.full_name,
        )
    except InvitationInvalidError as exc:
        # The single mapping. One exception type, one status, one body — see
        # the module docstring on why a second branch here is an oracle.
        raise HTTPException(status_code=400, detail=str(exc))
    return LoginToken(access_token=AuthService.create_access_token(user.id))
