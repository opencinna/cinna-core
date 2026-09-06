"""
Server configuration API.

Exposes the singleton server-wide config in tiers:

* the **access policy** projection — public, unauthenticated, rate-limited;
  it is what the login, signup and landing pages render themselves from;
* the **landing** projection — public, unauthenticated, rate-limited on the
  same budget; the admin-authored welcome copy `/start` renders;
* the **disclaimer** projection — any authenticated user;
* the **full config** — superuser only, read and update.

The access-policy projection is deliberately the narrowest of these: it says
what this instance offers (may anyone register, which sign-in methods exist),
never who may do what. Patterns and the default role stay behind the superuser
endpoint. The landing copy is split out rather than folded into it because it
is *content*, and every login page load reads the policy.
"""
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.deps import CurrentUser, SessionDep, get_current_active_superuser
from app.core.config import settings
from app.models import User
from app.models.server_config.server_config import (
    AccessPolicyPublic,
    DisclaimerPublic,
    LandingPagePublic,
    ServerConfig,
    ServerConfigUpdate,
)
from app.services.common.rate_limiter import RateLimiter, anonymous_caller_key
from app.services.server_config.server_config_service import ServerConfigService
from app.services.users.access_policy_service import (
    AccessPolicyService,
    AccessPolicyValidationError,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["server-config"])

SuperUser = Annotated[User, Depends(get_current_active_superuser)]

# Keyed by source IP — the caller is anonymous, so their address is the only
# identity there is. The reads are cheap, but they are among the few endpoints
# an unauthenticated caller can reach at all, and they touch the database.
#
# One limiter object shared by every anonymous endpoint in this module, so a
# caller gets one budget rather than one per route.
_access_policy_limiter = RateLimiter()


def _access_policy_rate_limit(request: Request) -> None:
    """Per-caller backstop on the anonymous, DB-touching endpoints here.

    Declared before ``SessionDep`` resolves so a throttled request costs no
    pool connection and no query.
    """
    retry_after = _access_policy_limiter.check(
        anonymous_caller_key(request),
        settings.ACCESS_POLICY_RATE_LIMIT_PER_MIN,
    )
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded",
            headers={"Retry-After": str(int(retry_after))},
        )


@router.get(
    "/server-config/access-policy",
    response_model=AccessPolicyPublic,
    dependencies=[Depends(_access_policy_rate_limit)],
)
def get_access_policy(session: SessionDep) -> Any:
    """Return the public access-policy projection.

    Unauthenticated on purpose: the login and signup pages must render the
    right front door *before* anyone has a token.
    """
    policy = AccessPolicyService.resolve(session)
    return AccessPolicyService.to_public(policy)


@router.get(
    "/server-config/landing",
    response_model=LandingPagePublic,
    dependencies=[Depends(_access_policy_rate_limit)],
)
def get_landing_page(session: SessionDep) -> Any:
    """Return the admin-authored welcome copy for the public `/start` page.

    A separate endpoint from the access-policy projection on purpose. The
    policy is a handful of small scalars, cached under one key that the login
    and signup pages both read on every load; this is an admin-pasted document
    that only `/start` renders. Folding it into that projection would put a
    landing page on the wire for every login page view.

    Shares the *same* limiter object as the policy read — a second
    ``RateLimiter`` would hand one anonymous caller two budgets.
    """
    config = ServerConfigService.get_or_create(session)
    return LandingPagePublic(landing_markdown=config.landing_markdown)


@router.get("/server-config/disclaimer", response_model=DisclaimerPublic)
def get_disclaimer(
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """Return the disclaimer projection for any authenticated user."""
    config = ServerConfigService.get_or_create(session)
    return ServerConfigService.to_disclaimer_public(config)


@router.get("/admin/server-config", response_model=ServerConfig)
def get_server_config(
    session: SessionDep,
    current_user: SuperUser,
) -> Any:
    """Return the full server configuration (superuser only)."""
    return ServerConfigService.get_or_create(session)


@router.put("/admin/server-config", response_model=ServerConfig)
def update_server_config(
    *,
    session: SessionDep,
    current_user: SuperUser,
    data: ServerConfigUpdate,
) -> Any:
    """Update the server configuration (superuser only).

    Access-policy changes are validated first; a rejection returns 400 with
    the machine-readable reason code as ``detail`` so the admin card can show
    its own wording next to the offending control.
    """
    try:
        return ServerConfigService.update(session, data, current_user)
    except AccessPolicyValidationError as exc:
        # ``reason`` is the stable code the card branches on; ``message`` is
        # the human explanation, logged rather than returned so the wording
        # stays in the UI where it can be translated.
        logger.info(
            "Rejected server-config update (%s) for %s: %s",
            exc.reason,
            current_user.id,
            exc.message,
        )
        raise HTTPException(status_code=400, detail=exc.reason)
