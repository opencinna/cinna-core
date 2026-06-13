"""
Native Account-Config API.

A single endpoint — ``GET /api/v1/external/account-config`` — that returns the
authenticated user's own usable AI credentials *including the decrypted
api_key*, so Cinna Desktop/Mobile can auto-create local "LLM providers" + a
default chat mode on login.

This is the ONE endpoint that returns decrypted key bytes: a deliberate,
product-approved relaxation of the platform's "keys never exposed" invariant.
It is isolated in its own module to keep that security boundary auditable.

Security boundary (all enforced here):
  - Native-token gated: only JWTs carrying ``client_kind in {desktop, mobile}``.
    A plain web-session JWT (no client_kind) is rejected 403. ``get_current_user``
    already enforces live desktop-client revocation, so a revoked device's stale
    token is rejected 401 upstream.
  - Strictly self-scoped: only the caller's OWN credentials (owner_id == user.id);
    shares are not included.
  - Audited: every successful call writes a high-severity SecurityEvent with
    counts + ids but NO key material.
  - No caching: ``Cache-Control: no-store`` on the response.
"""
from typing import Any

from fastapi import APIRouter, HTTPException, Response

from app.api.deps import CurrentClientClaims, CurrentUser, SessionDep
from app.models.events.security_event import SecurityEventCreate
from app.models.external.account_config import AccountConfigResponse
from app.services.events.security_event_service import SecurityEventService
from app.services.external.external_account_config_service import (
    external_account_config_service,
)

router = APIRouter(prefix="/external", tags=["external"])

_NATIVE_CLIENT_KINDS = {"desktop", "mobile"}


@router.get("/account-config", response_model=AccountConfigResponse)
async def get_account_config(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    client_claims: CurrentClientClaims,
    response: Response,
) -> Any:
    """Return the caller's own AI providers with decrypted keys (native only).

    Rejects non-native tokens with 403. Sets ``Cache-Control: no-store`` and
    writes a high-severity audit event (no key material).
    """
    client_kind, external_client_id = client_claims
    if client_kind not in _NATIVE_CLIENT_KINDS:
        raise HTTPException(
            status_code=403,
            detail="This endpoint is only available to native clients",
        )

    config = external_account_config_service.build_config(session, current_user)

    # Secrets in the body — never cache.
    response.headers["Cache-Control"] = "no-store"

    await SecurityEventService.create_event(
        session=session,
        user_id=current_user.id,
        data=SecurityEventCreate(
            event_type="external.account_config.read",
            severity="high",
            details={
                "client_kind": client_kind,
                "external_client_id": external_client_id,
                "provider_count": len(config.providers),
                "credential_ids": [
                    str(p.credential_id) for p in config.providers
                ],
            },
        ),
    )

    return config
