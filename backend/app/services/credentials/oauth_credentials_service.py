"""
OAuth Credentials Service - Business logic for OAuth credential operations.

This service handles the OAuth flow for Google service credentials (Gmail, Drive, Calendar, Meet).
"""
import hashlib
import secrets
import uuid
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import delete
from sqlmodel import Session
from fastapi import HTTPException

from app.core.config import settings
from app.core import security
from app.models import Credential, User
from app.models.credentials.credential_oauth_state import (
    CredentialOAuthOrigin,
    CredentialOAuthState,
)

logger = logging.getLogger(__name__)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_TOKEN_TIMEOUT_SECONDS = 10.0
# Google OAuth error codes meaning the refresh token is no longer usable.
GOOGLE_REAUTH_ERRORS = {"invalid_grant", "unauthorized_client"}

# Refresh error kinds (stored as ``refresh_error_kind`` in the encrypted blob).
REFRESH_ERROR_REAUTH_REQUIRED = "reauth_required"
REFRESH_ERROR_PROVIDER = "provider_error"
REFRESH_ERROR_NOT_CONFIGURED = "not_configured"

# Refresh bookkeeping fields kept in the encrypted credential blob. Never
# reach the container: AGENT_ENV_ALLOWED_FIELDS whitelists what is pushed.
REFRESH_ERROR_FIELDS = ("refresh_error", "refresh_error_kind", "refresh_error_at")


class OAuthRefreshError(Exception):
    """A Google token refresh failed. ``message`` never contains token values."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message

# Lifetime of a pending authorization state (``credential_oauth_state`` row).
OAUTH_STATE_TTL_SECONDS = 600
# Prefix of a state started from Cinna Desktop. The SPA callback page reads it
# to pick its terminal view even when Google redirects back with ``?error=``.
DESKTOP_STATE_PREFIX = "desktop."


def _hash_state(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


@dataclass(frozen=True)
class ConsumedOAuthState:
    credential_id: uuid.UUID
    user_id: uuid.UUID
    origin: CredentialOAuthOrigin


@dataclass(frozen=True)
class OAuthCallbackResult:
    credential: Credential
    origin: CredentialOAuthOrigin
    owner_email_masked: str | None


def mask_email(email: str | None) -> str | None:
    """``evgeny@example.com`` -> ``ev***@example.com``; the domain stays whole.

    Enough for the owner to recognise their own account, not enough to hand a
    stranger who completes the link a usable address.
    """
    if not email or "@" not in email:
        return None
    local, domain = email.rsplit("@", 1)
    keep = 1 if len(local) <= 3 else 2
    return f"{local[:keep]}***@{domain}"

# Scope mapping for each OAuth credential type
OAUTH_SCOPES = {
    "gmail_oauth": [
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
        "openid"
    ],
    "gmail_oauth_readonly": [
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
        "openid"
    ],
    "gdrive_oauth": [
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
        "openid"
    ],
    "gdrive_oauth_readonly": [
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
        "openid"
    ],
    "gcalendar_oauth": [
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
        "openid"
    ],
    "gcalendar_oauth_readonly": [
        "https://www.googleapis.com/auth/calendar.readonly",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
        "openid"
    ],
    # Google Meet: create and manage meeting spaces, plus read every space,
    # conference record and participant the user can access
    # (meetings.space.readonly), so it covers the read-only type's Meet
    # metadata. It does not carry drive.meet.readonly: downloading recordings
    # and transcripts still needs gmeet_oauth_readonly. No Calendar coupling —
    # meetings with invitees go through gcalendar_oauth (conferenceData).
    "gmeet_oauth": [
        "https://www.googleapis.com/auth/meetings.space.created",
        "https://www.googleapis.com/auth/meetings.space.settings",
        "https://www.googleapis.com/auth/meetings.space.readonly",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
        "openid"
    ],
    # Google Meet read-only: conference records, participants, recordings,
    # transcripts and smart notes. drive.meet.readonly (a restricted scope) is
    # needed to download the recording/transcript artifacts stored in Drive.
    "gmeet_oauth_readonly": [
        "https://www.googleapis.com/auth/meetings.space.readonly",
        "https://www.googleapis.com/auth/drive.meet.readonly",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
        "openid"
    ],
}


class OAuthCredentialsService:
    """Service for managing OAuth credential operations."""

    @staticmethod
    def get_oauth_scopes_for_type(credential_type: str) -> list[str]:
        """
        Get the OAuth scopes required for a credential type.

        Args:
            credential_type: Type of credential (e.g., "gmail_oauth", "gdrive_oauth")

        Returns:
            List of OAuth scope URLs

        Raises:
            ValueError: If credential type is not an OAuth type
        """
        scopes = OAUTH_SCOPES.get(credential_type)
        if not scopes:
            raise ValueError(f"Unknown or non-OAuth credential type: {credential_type}")
        return scopes

    @staticmethod
    def initiate_oauth_flow(
        session: Session,
        credential_id: uuid.UUID,
        user_id: uuid.UUID,
        origin: CredentialOAuthOrigin = "web",
    ) -> dict[str, Any]:
        """
        Initiate OAuth flow for a credential.

        Stores a single-use state (hashed) in ``credential_oauth_state`` and
        builds the Google authorization URL. Desktop-started states carry the
        ``DESKTOP_STATE_PREFIX``.

        Args:
            session: Database session
            credential_id: Credential ID to authorize
            user_id: User ID initiating the flow (for ownership verification)
            origin: "web" (credential settings page) or "desktop"

        Returns:
            ``{"authorization_url", "state", "expires_at"}`` (``expires_at`` =
            state expiry, epoch seconds)

        Raises:
            HTTPException: If Google OAuth is not configured
            ValueError: If credential type is not OAuth-compatible
        """
        if not settings.google_oauth_enabled:
            raise HTTPException(
                status_code=501,
                detail="Google OAuth is not configured"
            )

        # Get credential to determine type and verify ownership
        credential = session.get(Credential, credential_id)
        if not credential:
            raise ValueError("Credential not found")
        if credential.owner_id != user_id:
            raise ValueError("Not authorized to access this credential")

        # Get scopes for this credential type
        try:
            scopes = OAuthCredentialsService.get_oauth_scopes_for_type(
                credential.type.value
            )
        except ValueError as e:
            raise ValueError(f"Invalid credential type for OAuth: {str(e)}")

        # Generate CSRF state token; only its hash is stored.
        state = secrets.token_urlsafe(32)
        if origin == "desktop":
            state = DESKTOP_STATE_PREFIX + state
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=OAUTH_STATE_TTL_SECONDS)

        # Opportunistic purge of abandoned flows, then record this one.
        session.execute(
            delete(CredentialOAuthState).where(CredentialOAuthState.expires_at < now)
        )
        session.add(
            CredentialOAuthState(
                state_hash=_hash_state(state),
                credential_id=credential_id,
                user_id=user_id,
                origin=origin,
                expires_at=expires_at,
            )
        )
        session.commit()

        logger.info(
            "Initiating OAuth flow for credential %s (type: %s, origin: %s)",
            credential_id, credential.type.value, origin,
        )

        # Build authorization URL
        # Use separate redirect URI for credential OAuth to differentiate from user OAuth
        params = {
            "client_id": settings.GOOGLE_CLIENT_ID,
            "redirect_uri": settings.GOOGLE_CREDENTIALS_REDIRECT_URI,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
            "access_type": "offline",  # Request refresh token
            "prompt": "consent"  # Always show consent screen to get refresh token
        }
        auth_url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"

        return {
            "authorization_url": auth_url,
            "state": state,
            "expires_at": int(expires_at.timestamp()),
        }

    @staticmethod
    def consume_state(session: Session, state: str) -> ConsumedOAuthState | None:
        """Delete the state's row in one statement and commit; return its context.

        Single-use: the row is gone (committed) before the caller talks to
        Google, so a failed exchange cannot be replayed. None when the state is
        unknown, already used, or expired.
        """
        row = session.execute(
            delete(CredentialOAuthState)
            .where(CredentialOAuthState.state_hash == _hash_state(state))
            .returning(
                CredentialOAuthState.credential_id,
                CredentialOAuthState.user_id,
                CredentialOAuthState.origin,
                CredentialOAuthState.expires_at,
            )
            .execution_options(synchronize_session=False)
        ).first()
        session.commit()
        if row is None:
            return None
        credential_id, user_id, origin, expires_at = row
        if expires_at <= datetime.now(timezone.utc):
            return None
        return ConsumedOAuthState(
            credential_id=credential_id,
            user_id=user_id,
            origin="desktop" if origin == "desktop" else "web",
        )

    @staticmethod
    async def handle_oauth_callback(
        session: Session,
        code: str,
        state: str,
        signed_in_user_id: uuid.UUID | None = None,
    ) -> OAuthCallbackResult:
        """
        Handle OAuth callback from Google.

        Consumes the state (CSRF protection, single-use), exchanges the
        authorization code for tokens, and stores them in the credential's
        encrypted_data.

        Args:
            session: Database session
            code: Authorization code from Google
            state: State token from authorization request
            signed_in_user_id: The Cinna account the browser is signed in as,
                or None for a sessionless browser (the usual desktop case).
                When set it must be the state's user, else 403. The state is
                consumed either way, so a phishing link dies on first use.

        Returns:
            The updated credential and the origin the flow was started from

        Raises:
            HTTPException: If OAuth is not configured, state is invalid, or token exchange fails
        """
        if not settings.google_oauth_enabled:
            raise HTTPException(
                status_code=501,
                detail="Google OAuth is not configured"
            )

        consumed = OAuthCredentialsService.consume_state(session, state)
        if consumed is None:
            raise HTTPException(status_code=400, detail="Invalid or expired state parameter")
        if signed_in_user_id is not None and signed_in_user_id != consumed.user_id:
            # Generic on purpose: never say whose link it is.
            logger.warning(
                "OAuth callback rejected: signed-in account does not own the state"
            )
            raise HTTPException(
                status_code=403,
                detail=(
                    "This authorization link was started from a different Cinna "
                    "account than the one signed in here. The link is no longer "
                    "valid; start the connection again from your own account."
                ),
            )
        credential_id = consumed.credential_id

        # Get credential and verify ownership
        credential = session.get(Credential, credential_id)
        if not credential:
            raise HTTPException(status_code=404, detail="Credential not found")
        if credential.owner_id != consumed.user_id:
            raise HTTPException(status_code=403, detail="Not authorized to access this credential")

        logger.info(f"Processing OAuth callback for credential {credential_id}")

        try:
            token_data = await OAuthCredentialsService._exchange_authorization_code(code)

            # Extract token information
            access_token = token_data.get("access_token")
            refresh_token = token_data.get("refresh_token")
            expires_in = token_data.get("expires_in", 3600)  # Default 1 hour
            token_type = token_data.get("token_type", "Bearer")
            scope = token_data.get("scope", "")

            if not access_token:
                raise HTTPException(status_code=400, detail="No access token received")
            if not refresh_token:
                logger.warning(f"No refresh token received for credential {credential_id}")

            # Calculate expiration timestamp
            expires_at = int(datetime.now(timezone.utc).timestamp() + expires_in)
            granted_at = int(datetime.now(timezone.utc).timestamp())

            # Get user information from ID token or userinfo endpoint
            id_token = token_data.get("id_token")
            granted_user_email = None
            granted_user_name = None

            if id_token:
                # Verify and decode ID token
                claims = await security.verify_google_token(
                    id_token,
                    settings.GOOGLE_CLIENT_ID  # type: ignore
                )
                if claims:
                    granted_user_email = claims.get("email")
                    granted_user_name = claims.get("name")
                    logger.info(f"Extracted user info from ID token: {granted_user_email}")

            # If ID token verification failed or didn't have info, try userinfo endpoint
            if not granted_user_email:
                try:
                    async with httpx.AsyncClient() as client:
                        userinfo_response = await client.get(
                            "https://www.googleapis.com/oauth2/v3/userinfo",
                            headers={
                                "Authorization": f"Bearer {access_token}",
                                "Accept": "application/json"
                            }
                        )
                    if userinfo_response.status_code == 200:
                        userinfo = userinfo_response.json()
                        granted_user_email = userinfo.get("email")
                        granted_user_name = userinfo.get("name")
                        logger.info(f"Extracted user info from userinfo endpoint: {granted_user_email}")
                except Exception as e:
                    logger.warning(f"Failed to get user info from userinfo endpoint: {e}")

            # Prepare credential data for storage
            credential_data = {
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_type": token_type,
                "expires_at": expires_at,
                "scope": scope,
                "granted_user_email": granted_user_email,
                "granted_user_name": granted_user_name,
                "granted_at": granted_at
            }

            # Encrypt and store credential data
            encrypted_data = security.encrypt_field(json.dumps(credential_data))
            credential.encrypted_data = encrypted_data
            session.add(credential)
            session.commit()
            session.refresh(credential)

            logger.info(f"Successfully stored OAuth tokens for credential {credential_id}")

            owner = session.get(User, credential.owner_id)
            return OAuthCallbackResult(
                credential=credential,
                origin=consumed.origin,
                owner_email_masked=mask_email(owner.email if owner else None),
            )

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"OAuth callback error for credential {credential_id}: {e}")
            raise HTTPException(status_code=400, detail=f"OAuth error: {str(e)}")

    @staticmethod
    def get_oauth_metadata(
        session: Session,
        credential: Credential
    ) -> dict[str, Any]:
        """
        Extract OAuth metadata from a credential for display.

        Returns non-sensitive information like user email, scopes, and expiration.

        Args:
            session: Database session
            credential: Credential object

        Returns:
            Dictionary with metadata:
            {
                "user_email": str | None,
                "user_name": str | None,
                "scopes": list[str] | None,
                "expires_at": int | None,
                "granted_at": int | None,
                "refresh_error": str | None,      # last automatic/manual refresh failure
                "needs_reauthorization": bool     # Google rejected the refresh token
            }
        """
        if not credential.encrypted_data:
            # No OAuth data yet
            return {
                "user_email": None,
                "user_name": None,
                "scopes": None,
                "expires_at": None,
                "granted_at": None
            }

        try:
            # Decrypt credential data
            decrypted_data = security.decrypt_field(credential.encrypted_data)
            credential_data = json.loads(decrypted_data)

            # Extract non-sensitive metadata
            scopes_str = credential_data.get("scope", "")
            scopes = scopes_str.split() if scopes_str else None

            return {
                "user_email": credential_data.get("granted_user_email"),
                "user_name": credential_data.get("granted_user_name"),
                "scopes": scopes,
                "expires_at": credential_data.get("expires_at"),
                "granted_at": credential_data.get("granted_at"),
                "refresh_error": credential_data.get("refresh_error"),
                "needs_reauthorization": (
                    credential_data.get("refresh_error_kind") == REFRESH_ERROR_REAUTH_REQUIRED
                ),
            }
        except Exception as e:
            logger.error(f"Failed to extract OAuth metadata from credential {credential.id}: {e}")
            return {
                "user_email": None,
                "user_name": None,
                "scopes": None,
                "expires_at": None,
                "granted_at": None
            }

    @staticmethod
    async def _exchange_authorization_code(code: str) -> dict[str, Any]:
        """
        Exchange an authorization code for Google tokens (the callback's single
        token-endpoint call, and its test seam).

        Returns:
            The decoded Google token response.

        Raises:
            HTTPException: 400 when Google does not answer 200.
        """
        logger.info(
            "Exchanging code for tokens with redirect_uri: %s",
            settings.GOOGLE_CREDENTIALS_REDIRECT_URI,
        )
        async with httpx.AsyncClient() as client:
            token_response = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": settings.GOOGLE_CLIENT_ID,
                    "client_secret": settings.GOOGLE_CLIENT_SECRET,
                    "redirect_uri": settings.GOOGLE_CREDENTIALS_REDIRECT_URI,
                    "grant_type": "authorization_code",
                },
            )
        if token_response.status_code != 200:
            logger.error(f"Token exchange failed: {token_response.text}")
            raise HTTPException(
                status_code=400,
                detail="Failed to exchange authorization code"
            )
        return token_response.json()

    @staticmethod
    async def _exchange_refresh_token(refresh_token: str) -> dict[str, Any]:
        """
        Exchange a Google refresh token for a new access token.

        This is the single network call of the refresh flow (and the test seam).
        Error messages never contain token values.

        Returns:
            The decoded Google token response (contains ``access_token``).

        Raises:
            OAuthRefreshError: ``not_configured`` when Google OAuth is disabled,
                ``reauth_required`` when Google rejects the grant or no refresh
                token is given, ``provider_error`` for any other failure.
        """
        if not settings.google_oauth_enabled:
            raise OAuthRefreshError(REFRESH_ERROR_NOT_CONFIGURED, "Google OAuth is not configured")
        if not refresh_token:
            raise OAuthRefreshError(REFRESH_ERROR_REAUTH_REQUIRED, "Credential has no refresh token")

        try:
            async with httpx.AsyncClient(timeout=GOOGLE_TOKEN_TIMEOUT_SECONDS) as client:
                token_response = await client.post(
                    GOOGLE_TOKEN_URL,
                    data={
                        "client_id": settings.GOOGLE_CLIENT_ID,
                        "client_secret": settings.GOOGLE_CLIENT_SECRET,
                        "refresh_token": refresh_token,
                        "grant_type": "refresh_token",
                    },
                )
        except httpx.TimeoutException:
            raise OAuthRefreshError(REFRESH_ERROR_PROVIDER, "Google token endpoint timed out")
        except httpx.HTTPError as e:
            raise OAuthRefreshError(
                REFRESH_ERROR_PROVIDER,
                f"Could not reach Google token endpoint ({type(e).__name__})",
            )

        status = token_response.status_code
        if status != 200:
            error_code = OAuthCredentialsService._google_error_code(token_response)
            if status in (400, 401) and error_code in GOOGLE_REAUTH_ERRORS:
                raise OAuthRefreshError(
                    REFRESH_ERROR_REAUTH_REQUIRED,
                    f"Google rejected the refresh token ({error_code}); re-authorization required",
                )
            detail = f": {error_code}" if error_code else ""
            raise OAuthRefreshError(
                REFRESH_ERROR_PROVIDER,
                f"Google token endpoint returned HTTP {status}{detail}",
            )

        try:
            token_data = token_response.json()
        except ValueError:
            raise OAuthRefreshError(REFRESH_ERROR_PROVIDER, "Google token response is not valid JSON")
        if not isinstance(token_data, dict) or not token_data.get("access_token"):
            raise OAuthRefreshError(REFRESH_ERROR_PROVIDER, "No access token received")
        return token_data

    @staticmethod
    def _google_error_code(response: httpx.Response) -> str | None:
        """Return Google's OAuth ``error`` code from an error response, if any."""
        try:
            body = response.json()
        except ValueError:
            return None
        if isinstance(body, dict) and isinstance(body.get("error"), str):
            return body["error"]
        return None

    @staticmethod
    def apply_token_response(
        credential_data: dict[str, Any],
        token_data: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Merge a Google token response into decrypted credential data.

        Sets the new access token and expiry, stores a rotated refresh token if
        Google returned one, and clears the refresh-error bookkeeping fields.
        Mutates and returns ``credential_data``.
        """
        expires_in = token_data.get("expires_in") or 3600
        credential_data["access_token"] = token_data["access_token"]
        credential_data["expires_at"] = int(datetime.now(timezone.utc).timestamp() + expires_in)
        new_refresh_token = token_data.get("refresh_token")
        if new_refresh_token:
            credential_data["refresh_token"] = new_refresh_token
        for field in REFRESH_ERROR_FIELDS:
            credential_data.pop(field, None)
        return credential_data

    @staticmethod
    async def refresh_oauth_token(
        session: Session,
        credential: Credential
    ) -> Credential:
        """
        Refresh OAuth token for a credential unconditionally (owner's manual refresh).

        Goes through ``OAuthRefreshService.refresh_if_needed(force=True)`` so it
        shares the per-credential lock with every automatic refresh path, and
        clears a recorded ``reauth_required`` state if Google accepts the grant.

        Raises:
            HTTPException: 501 if Google OAuth is not configured, 400 if the
                refresh fails.
            ValueError: If the credential has no OAuth data.
        """
        from app.services.credentials.oauth_refresh_service import OAuthRefreshService

        if not settings.google_oauth_enabled:
            raise HTTPException(
                status_code=501,
                detail="Google OAuth is not configured"
            )

        if not credential.encrypted_data:
            raise ValueError("Credential has no OAuth data")

        try:
            outcome = await OAuthRefreshService.refresh_if_needed(
                session,
                credential,
                min_valid_seconds=0,
                force=True,
                raise_on_error=True,
            )
        except OAuthRefreshError as e:
            if e.kind == REFRESH_ERROR_NOT_CONFIGURED:
                raise HTTPException(status_code=501, detail=e.message)
            raise HTTPException(status_code=400, detail=f"Failed to refresh OAuth token: {e.message}")

        if outcome.status == "skipped":
            raise HTTPException(status_code=400, detail="Credential has no OAuth data")
        if outcome.status == "busy":
            raise HTTPException(
                status_code=400,
                detail="Failed to refresh OAuth token: a refresh is already in progress, try again shortly",
            )

        session.refresh(outcome.credential)
        return outcome.credential
