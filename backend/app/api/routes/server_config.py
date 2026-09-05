"""
Server configuration API.

Exposes the singleton server-wide config in three tiers:

* the **access policy** projection — public, unauthenticated, rate-limited;
  it is what the login and signup pages render themselves from;
* the **disclaimer** projection — any authenticated user;
* the **full config** — superuser only, read and update.

The access-policy projection is deliberately the narrowest of the three: it
says what this instance offers (may anyone register, which sign-in methods
exist), never who may do what. Patterns and the default role stay behind the
superuser endpoint.
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
# identity there is. The read is cheap, but it is one of the few endpoints an
# unauthenticated caller can reach at all, and it touches the database.
_access_policy_limiter = RateLimiter()


def _access_policy_rate_limit(request: Request) -> None:
    """Per-caller backstop on the one anonymous, DB-touching endpoint here.

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
