"""Google OAuth credentials for Cinna Desktop, brokered by Core.

Three operations back ``/external/credentials``:

- ``create_oauth_credential`` — create an owned Google OAuth credential
  (local use allowed, Default workspace) and start its authorization; an
  existing never-authorized one of the same type is reused instead.
- ``reauthorize_oauth_credential`` — start a new authorization for an owned one.
- ``access_token`` — a short-lived access token for a credential the caller
  may use locally (same rules as materialize), refreshed when needed; only a
  refresh is audited.

The refresh token never leaves Core. Authorization runs through the regular
credential OAuth flow with ``origin="desktop"``; the browser returns to the
SPA callback page, which completes it. Every call first requires a live,
interactive Desktop client and passes its own rate limit. Errors are
``DesktopCredentialError`` with a stable ``code``; token values are never
logged, audited or put in an error.
"""

import logging
import uuid

from sqlmodel import Session, select

from app.core.config import settings
from app.models.credentials.credential import (
    AgentCredentialAccessTokenRequest,
    AgentCredentialAccessTokenResponse,
    Credential,
    CredentialCreate,
    CredentialType,
)
from app.models.credentials.desktop_credential import (
    DesktopOAuthAuthorizeResponse,
    DesktopOAuthCredentialCreate,
)
from app.models.events.security_event import (
    CREDENTIAL_OAUTH_AUTHORIZE_LOCAL,
    CREDENTIAL_TOKEN_ISSUED_LOCAL,
    SecurityEventCreate,
)
from app.models.users.user import User
from app.services.credentials.credentials_service import CredentialsService
from app.services.credentials.desktop_credential_service import (
    DesktopCredentialError,
    DesktopCredentialService,
)
from app.services.credentials.oauth_credentials_service import OAuthCredentialsService
from app.services.credentials.oauth_refresh_service import (
    GOOGLE_OAUTH_TYPES,
    AgentEnvTokenError,
    OAuthRefreshService,
    oauth_status,
)
from app.services.events.security_event_service import SecurityEventService

logger = logging.getLogger(__name__)

# Create + re-authorize share one bucket; token requests have their own.
AUTHORIZE_LIMIT_PER_MINUTE = 10
TOKEN_LIMIT_PER_MINUTE = 120

_READONLY_SUFFIX = "_readonly"
_DEFAULT_NAMES = {
    "gmail_oauth": "Gmail",
    "gdrive_oauth": "Google Drive",
    "gcalendar_oauth": "Google Calendar",
    "gmeet_oauth": "Google Meet",
}


def default_credential_name(credential_type: CredentialType) -> str:
    """Display name for a new Google OAuth credential ("Gmail (read-only)", ...)."""
    value = credential_type.value
    if value.endswith(_READONLY_SUFFIX):
        return f"{_DEFAULT_NAMES[value.removesuffix(_READONLY_SUFFIX)]} (read-only)"
    return _DEFAULT_NAMES[value]


def _not_found() -> DesktopCredentialError:
    return DesktopCredentialError(
        404, "Credential not found", code="credential_not_found"
    )


def _unsupported_type(credential_type: CredentialType) -> DesktopCredentialError:
    return DesktopCredentialError(
        422,
        f"Credential type {credential_type.value} is not a Google OAuth type",
        code="unsupported_type",
    )


def _require_oauth_configured() -> None:
    if not settings.google_oauth_enabled:
        raise DesktopCredentialError(
            503,
            "Google OAuth is not configured on this platform. Ask an administrator.",
            code="oauth_not_configured",
        )


class DesktopOAuthService:
    @staticmethod
    def _start_request(
        session: Session, user: User, client_id: str, *, scope: str, limit: int
    ) -> None:
        DesktopCredentialService._require_live_client(session, client_id)
        DesktopCredentialService._check_rate_limit(
            user.id, client_id, scope=scope, limit_per_minute=limit
        )

    @staticmethod
    async def create_oauth_credential(
        session: Session,
        user: User,
        client_id: str,
        body: DesktopOAuthCredentialCreate,
    ) -> DesktopOAuthAuthorizeResponse:
        """Create an owned Google OAuth credential and start its authorization.

        Type and configuration are checked before anything is written.
        """
        DesktopOAuthService._start_request(
            session, user, client_id, scope="oauth", limit=AUTHORIZE_LIMIT_PER_MINUTE
        )
        if body.type.value not in GOOGLE_OAUTH_TYPES:
            raise _unsupported_type(body.type)
        _require_oauth_configured()

        credential = DesktopOAuthService._find_pending_credential(
            session, user.id, body.type
        )
        created = credential is None
        if credential is None:
            credential = CredentialsService.create_credential(
                session=session,
                credential_in=CredentialCreate(
                    type=body.type,
                    name=body.name or default_credential_name(body.type),
                    allow_local_use=True,
                    user_workspace_id=None,
                    credential_data={},
                ),
                owner_id=user.id,
            )
        elif body.name and body.name != credential.name:
            credential.name = body.name
            session.add(credential)
            session.commit()
            session.refresh(credential)
        return await DesktopOAuthService._authorize(
            session, user, client_id, credential, created=created
        )

    @staticmethod
    def _find_pending_credential(
        session: Session, owner_id: uuid.UUID, credential_type: CredentialType
    ) -> Credential | None:
        """The caller's never-authorized credential a desktop create would make.

        Same owner and type, Default workspace, local use allowed, not a
        placeholder, and no token stored yet (``pending_authorization``), so an
        abandoned consent does not leave a new empty credential behind each
        retry. When several match, the most recently updated wins (ties by id),
        so repeated creates keep returning the same one.
        """
        candidates = session.exec(
            select(Credential)
            .where(
                Credential.owner_id == owner_id,
                Credential.type == credential_type,
                Credential.user_workspace_id.is_(None),  # type: ignore[union-attr]
                Credential.allow_local_use.is_(True),  # type: ignore[attr-defined]
                Credential.is_placeholder.is_(False),  # type: ignore[attr-defined]
            )
            .order_by(
                Credential.updated_at.desc(),  # type: ignore[attr-defined]
                Credential.id.desc(),  # type: ignore[attr-defined]
            )
        ).all()
        for candidate in candidates:
            try:
                data = CredentialsService.decrypt_credential_data(session, candidate)
            except Exception:
                continue
            if oauth_status(data) == "pending_authorization":
                return candidate
        return None

    @staticmethod
    async def reauthorize_oauth_credential(
        session: Session,
        user: User,
        client_id: str,
        credential_id: uuid.UUID,
    ) -> DesktopOAuthAuthorizeResponse:
        """Start a new authorization for a Google OAuth credential the caller owns.

        A share does not count: only the owner can authorize.
        """
        DesktopOAuthService._start_request(
            session, user, client_id, scope="oauth", limit=AUTHORIZE_LIMIT_PER_MINUTE
        )
        credential = session.get(Credential, credential_id)
        if credential is None or credential.owner_id != user.id:
            raise _not_found()
        if credential.type.value not in GOOGLE_OAUTH_TYPES:
            raise _unsupported_type(credential.type)
        _require_oauth_configured()
        return await DesktopOAuthService._authorize(
            session, user, client_id, credential, created=False
        )

    @staticmethod
    async def _authorize(
        session: Session,
        user: User,
        client_id: str,
        credential: Credential,
        *,
        created: bool,
    ) -> DesktopOAuthAuthorizeResponse:
        flow = OAuthCredentialsService.initiate_oauth_flow(
            session, credential.id, user.id, origin="desktop"
        )
        await SecurityEventService.create_event(
            session=session,
            user_id=user.id,
            data=SecurityEventCreate(
                event_type=CREDENTIAL_OAUTH_AUTHORIZE_LOCAL,
                severity="medium",
                details={
                    "credential_id": str(credential.id),
                    "desktop_client_id": client_id,
                    "credential_type": credential.type.value,
                    "created": created,
                },
            ),
        )
        return DesktopOAuthAuthorizeResponse(
            credential_id=credential.id,
            authorization_url=flow["authorization_url"],
            expires_at=flow["expires_at"],
        )

    @staticmethod
    async def access_token(
        session: Session,
        user: User,
        client_id: str,
        credential_id: uuid.UUID,
        request: AgentCredentialAccessTokenRequest,
    ) -> AgentCredentialAccessTokenResponse:
        """A Google access token valid for ``request.min_ttl`` seconds.

        Access is the materialize rule (``local_access_refusal``); every
        refusal is the same 404, so nothing leaks about existence.
        ``known_expires_at`` is accepted for parity with the env endpoint and
        unused: nothing is pushed to environments from this path.
        """
        DesktopOAuthService._start_request(
            session, user, client_id, scope="token", limit=TOKEN_LIMIT_PER_MINUTE
        )
        credential = session.get(Credential, credential_id)
        shared = DesktopCredentialService._shared_sources(session, user.id)
        categories = (
            DesktopCredentialService._categories(session, user.id, [credential], shared)
            if credential is not None
            else {}
        )
        if DesktopCredentialService.local_access_refusal(
            credential, user.id, shared, categories
        ):
            raise _not_found()
        owner_id = credential.owner_id

        try:
            resp = await OAuthRefreshService.issue_access_token(
                session, credential, min_ttl=request.min_ttl
            )
        except AgentEnvTokenError as e:
            headers = (
                {"Retry-After": str(e.retry_after)} if e.retry_after is not None else None
            )
            raise DesktopCredentialError(
                e.status, e.message, headers=headers, code=e.code
            ) from None

        # Audit only a refresh (a new token minted from the stored refresh
        # token); handing back the still-valid cached token is not an event.
        if resp.refreshed:
            await DesktopOAuthService._audit_token_issued(
                session, user, client_id, credential_id, owner_id
            )
        return resp

    @staticmethod
    async def _audit_token_issued(
        session: Session,
        user: User,
        client_id: str,
        credential_id: uuid.UUID,
        owner_id: uuid.UUID,
    ) -> None:
        """Audit one refreshed token for the requester and, for a share
        recipient, for the owner too (mirrors ``_audit_delivery``). Never
        records the token."""
        await SecurityEventService.create_event(
            session=session,
            user_id=user.id,
            data=SecurityEventCreate(
                event_type=CREDENTIAL_TOKEN_ISSUED_LOCAL,
                severity="medium",
                details={
                    "credential_id": str(credential_id),
                    "desktop_client_id": client_id,
                    "refreshed": True,
                },
            ),
        )
        if owner_id != user.id:
            await SecurityEventService.create_event(
                session=session,
                user_id=owner_id,
                data=SecurityEventCreate(
                    event_type=CREDENTIAL_TOKEN_ISSUED_LOCAL,
                    severity="medium",
                    details={
                        "credential_id": str(credential_id),
                        "recipient_user_id": str(user.id),
                        "recipient_email": user.email,
                        "refreshed": True,
                    },
                ),
            )
