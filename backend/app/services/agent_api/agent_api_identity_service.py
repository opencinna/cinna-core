"""Agent-API caller-identity (L2) token service.

Mints and verifies the narrow ``owner_identity_token`` JWT that lets the
agent-api proxy attribute a consumer call to the cinna-core user who owns the
calling install — without handing the agent a master credential.

Design (plan D3):
- ``aud = "agent_api_caller"`` — useless as a general backend credential. The
  backend honours it ONLY at the agent-api proxy identity check, so a leak is
  bounded to "I am owner-E" assertions on agent-api calls (and capability is
  still gated by the live grant, Phase 2).
- ``sub = owner_user_id``.
- ``type = "agent_api_identity"`` marks it an identity assertion.
- Moderate TTL (config ``AGENT_API_IDENTITY_TOKEN_EXPIRE_DAYS``); re-minted on
  every credential sync, so running envs always carry a fresh one (plan D7).

HS256 / ``SECRET_KEY`` — minted and verified by the same backend process, so no
asymmetric crypto is needed (plan D1). The token NEVER reaches the producer:
the proxy strips it after verifying (see ``agent_api_public.consumer_proxy``).
"""
import logging
import uuid
from datetime import timedelta

import jwt
from sqlmodel import Session

from app.core import security
from app.core.config import settings
from app.models import User

logger = logging.getLogger(__name__)

# Audience that restricts this token to the agent-api identity check. The token
# is rejected as a credential anywhere else.
IDENTITY_AUDIENCE = "agent_api_caller"

# Marker claim distinguishing this from other backend-minted JWTs.
IDENTITY_TYPE = "agent_api_identity"

# The wire header the agent sends the L2 token on. Self-described in the
# synthetic credentials.json entry (so the building agent reads it, not memorises
# it) and stripped by the proxy after verification.
IDENTITY_HEADER = "X-Cinna-Caller-Identity"

# Fixed sentinel markers for the synthetic credentials.json entry (mirrors the
# reserved ``current_user`` markers in user_details_service).
OWNER_IDENTITY_ID = "owner_identity"
OWNER_IDENTITY_TYPE = "owner_identity_token"
OWNER_IDENTITY_NAME = "Owner Identity Token"
OWNER_IDENTITY_NOTES = (
    "Auto-generated. Send the header below on agent_api calls so the producer "
    "can identify you. Not a real credential."
)
OWNER_IDENTITY_USAGE = (
    "Send `<header>: <token>` on every agent_api request, alongside the "
    "Authorization bearer from the paired agent_api credential."
)

# Trusted attribution headers the proxy injects into the forwarded request after
# resolving the identity token. The proxy STRIPS any inbound copies of these (the
# identity token is the only accepted identity input) and sets them
# authoritatively, so the producer can trust them.
CALLER_HEADER_PREFIX = "x-cinna-caller-"
CALLER_USER_ID_HEADER = "X-Cinna-Caller-User-Id"
CALLER_EMAIL_HEADER = "X-Cinna-Caller-Email"
CALLER_USERNAME_HEADER = "X-Cinna-Caller-Username"
# Per-user scopes (Phase 2), resolved LIVE from the grant table at the proxy and
# injected only when the producer opted in (agent_api_identity_enabled). Encoded
# as a SPACE-separated list (OAuth-style); scope names are opaque tokens without
# spaces. Covered by the CALLER_HEADER_PREFIX strip like the others.
CALLER_SCOPES_HEADER = "X-Cinna-Caller-Scopes"


class AgentApiIdentityService:
    """Mint/verify the L2 ``owner_identity_token``. Stateless (no table)."""

    @staticmethod
    def mint(owner_user_id: uuid.UUID | str) -> str:
        """Mint a signed identity token asserting ``sub = owner_user_id``.

        Computed host-side from the install owner during credential prep; never
        stored. Re-minted for free on every credential sync.
        """
        return security.create_access_token(
            subject=str(owner_user_id),
            expires_delta=timedelta(
                days=settings.AGENT_API_IDENTITY_TOKEN_EXPIRE_DAYS
            ),
            extra_claims={
                "aud": IDENTITY_AUDIENCE,
                "type": IDENTITY_TYPE,
            },
        )

    @staticmethod
    def build_owner_identity_block(owner_user_id: uuid.UUID | str) -> dict:
        """Build the synthetic ``owner_identity_token`` credentials.json entry.

        Mirrors the shape of a real credential entry
        (``{id, name, type, notes, credential_data}``) with reserved sentinel
        ``id``/``type`` markers. Self-describing (``header`` + ``usage``) so the
        building agent reads the entry and uses it without prior knowledge.
        Computed host-side from the install owner, never stored, never redacted,
        never user-editable (plan D4).
        """
        return {
            "id": OWNER_IDENTITY_ID,
            "name": OWNER_IDENTITY_NAME,
            "type": OWNER_IDENTITY_TYPE,
            "notes": OWNER_IDENTITY_NOTES,
            "credential_data": {
                "token": AgentApiIdentityService.mint(owner_user_id),
                "header": IDENTITY_HEADER,
                "usage": OWNER_IDENTITY_USAGE,
            },
        }

    @staticmethod
    def verify(token: str | None) -> uuid.UUID | None:
        """Verify signature + ``aud`` + expiry; return the owner ``user_id`` or None.

        NEVER raises. Returns ``None`` on any failure (missing/malformed/expired
        token, wrong audience, bad signature, non-UUID subject). The proxy treats
        ``None`` as "anonymous caller" — never an error (plan SECURITY rule 4).
        The raw token must never be logged.
        """
        if not token:
            return None
        try:
            claims = jwt.decode(
                token,
                settings.SECRET_KEY,
                algorithms=[security.ALGORITHM],
                audience=IDENTITY_AUDIENCE,
            )
        except Exception:
            # Bad signature, expired, wrong audience, malformed — all anonymous.
            return None

        if claims.get("type") != IDENTITY_TYPE:
            return None

        sub = claims.get("sub")
        if not sub:
            return None
        try:
            return uuid.UUID(str(sub))
        except (ValueError, TypeError):
            return None

    @staticmethod
    def resolve_caller_headers(
        session: Session, identity_token: str | None
    ) -> dict[str, str]:
        """Resolve the trusted ``X-Cinna-Caller-*`` headers from an L2 token.

        Verifies the token, resolves ``sub`` to a live ``User``, and returns the
        attribution headers the proxy must inject authoritatively. Returns an
        EMPTY dict for an anonymous caller (no token, invalid/expired token, or a
        user that no longer exists) — the proxy then injects NO caller headers and
        lets the producer decide (plan SECURITY rule 4). NEVER raises; the token
        is never logged.

        Scopes (``X-Cinna-Caller-Scopes``) are intentionally NOT set here — that
        is Phase 2 (live grant lookup).
        """
        user_id = AgentApiIdentityService.verify(identity_token)
        if user_id is None:
            return {}
        user = session.get(User, user_id)
        if user is None:
            # Token verified but the owner is gone — treat as anonymous.
            return {}
        # Always set the user-id (the authoritative attribution key); only emit
        # email/username when present so we never rely on empty-header transport
        # semantics (some HTTP stacks drop empty-valued headers).
        headers = {CALLER_USER_ID_HEADER: str(user.id)}
        if user.email:
            headers[CALLER_EMAIL_HEADER] = user.email
        if user.username:
            headers[CALLER_USERNAME_HEADER] = user.username
        return headers
