"""
OAuth Credentials Service - Business logic for OAuth credential operations.

This service handles the OAuth flow for Google service credentials (Gmail, Drive, Calendar, Meet).
"""
import secrets
import uuid
import json
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlmodel import Session
from fastapi import HTTPException

from app.core.config import settings
from app.core import security
from app.models import Credential

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

# In-memory state storage for OAuth flows (use Redis in production)
_oauth_states: dict[str, dict[str, Any]] = {}

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
        user_id: uuid.UUID
    ) -> dict[str, str]:
        """
        Initiate OAuth flow for a credential.

        Generates a state token with credential context and builds the Google authorization URL.

        Args:
            session: Database session
            credential_id: Credential ID to authorize
            user_id: User ID initiating the flow (for ownership verification)

        Returns:
            Dictionary with "authorization_url" and "state" keys

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

        # Generate CSRF state token with credential context
        state = secrets.token_urlsafe(32)
        _oauth_states[state] = {
            "credential_id": str(credential_id),
            "user_id": str(user_id),
            "expires": datetime.now(timezone.utc).timestamp() + 600  # 10 minutes
        }

        # Clean up expired states
        now = datetime.now(timezone.utc).timestamp()
        expired_states = [k for k, v in _oauth_states.items() if v["expires"] < now]
        for k in expired_states:
            del _oauth_states[k]

        logger.info(f"Initiating OAuth flow for credential {credential_id} (type: {credential.type.value})")

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
            "state": state
        }

    @staticmethod
    async def handle_oauth_callback(
        session: Session,
        code: str,
        state: str
    ) -> Credential:
        """
        Handle OAuth callback from Google.

        Validates state token, exchanges authorization code for tokens,
        and stores them in the credential's encrypted_data.

        Args:
            session: Database session
            code: Authorization code from Google
            state: State token from authorization request

        Returns:
            Updated Credential object

        Raises:
            HTTPException: If OAuth is not configured, state is invalid, or token exchange fails
        """
        if not settings.google_oauth_enabled:
            raise HTTPException(
                status_code=501,
                detail="Google OAuth is not configured"
            )

        # Validate state token (CSRF protection)
        if state not in _oauth_states:
            raise HTTPException(status_code=400, detail="Invalid or expired state parameter")

        state_data = _oauth_states[state]
        credential_id = uuid.UUID(state_data["credential_id"])
        user_id = uuid.UUID(state_data["user_id"])

        # Clean up used state
        del _oauth_states[state]

        # Get credential and verify ownership
        credential = session.get(Credential, credential_id)
        if not credential:
            raise HTTPException(status_code=404, detail="Credential not found")
        if credential.owner_id != user_id:
            raise HTTPException(status_code=403, detail="Not authorized to access this credential")

        logger.info(f"Processing OAuth callback for credential {credential_id}")

        try:
            # Exchange authorization code for tokens
            logger.info(f"Exchanging code for tokens with redirect_uri: {settings.GOOGLE_CREDENTIALS_REDIRECT_URI}")
            async with httpx.AsyncClient() as client:
                token_response = await client.post(
                    "https://oauth2.googleapis.com/token",
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

            # Extract token information
            token_data = token_response.json()
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

            return credential

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
