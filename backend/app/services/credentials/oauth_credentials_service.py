"""
OAuth Credentials Service - Business logic for OAuth credential operations.

This service handles the OAuth flow for Google service credentials (Gmail, Drive, Calendar).
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
                "granted_at": int | None
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
                "granted_at": credential_data.get("granted_at")
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
    async def refresh_oauth_token(
        session: Session,
        credential: Credential
    ) -> Credential:
        """
        Refresh OAuth token for a credential.

        Uses the refresh token to obtain a new access token from Google.

        Args:
            session: Database session
            credential: Credential object

        Returns:
            Updated Credential object

        Raises:
            HTTPException: If OAuth is not configured or refresh fails
            ValueError: If credential has no refresh token
        """
        if not settings.google_oauth_enabled:
            raise HTTPException(
                status_code=501,
                detail="Google OAuth is not configured"
            )

        if not credential.encrypted_data:
            raise ValueError("Credential has no OAuth data")

        try:
            # Decrypt credential data
            decrypted_data = security.decrypt_field(credential.encrypted_data)
            credential_data = json.loads(decrypted_data)

            refresh_token = credential_data.get("refresh_token")
            if not refresh_token:
                raise ValueError("Credential has no refresh token")

            logger.info(f"Refreshing OAuth token for credential {credential.id}")

            # Request new access token using refresh token
            async with httpx.AsyncClient() as client:
                token_response = await client.post(
                    "https://oauth2.googleapis.com/token",
                    data={
                        "client_id": settings.GOOGLE_CLIENT_ID,
                        "client_secret": settings.GOOGLE_CLIENT_SECRET,
                        "refresh_token": refresh_token,
                        "grant_type": "refresh_token",
                    },
                )

            if token_response.status_code != 200:
                logger.error(f"Token refresh failed for credential {credential.id}: {token_response.text}")
                raise HTTPException(
                    status_code=400,
                    detail="Failed to refresh OAuth token"
                )

            token_data = token_response.json()
            logger.info(f"Successfully refreshed token for credential {credential.id}")

            # Extract new token information
            new_access_token = token_data.get("access_token")
            expires_in = token_data.get("expires_in", 3600)  # Default 1 hour

            if not new_access_token:
                raise HTTPException(status_code=400, detail="No access token received")

            # Calculate new expiration timestamp
            new_expires_at = int(datetime.now(timezone.utc).timestamp() + expires_in)

            # Update credential data with new access token and expiration
            credential_data["access_token"] = new_access_token
            credential_data["expires_at"] = new_expires_at

            # Note: refresh_token typically stays the same, but if Google returns a new one, update it
            new_refresh_token = token_data.get("refresh_token")
            if new_refresh_token:
                credential_data["refresh_token"] = new_refresh_token
                logger.info(f"Received new refresh token for credential {credential.id}")

            # Encrypt and store updated credential data
            encrypted_data = security.encrypt_field(json.dumps(credential_data))
            credential.encrypted_data = encrypted_data
            session.add(credential)
            session.commit()
            session.refresh(credential)

            logger.info(f"Successfully updated credential {credential.id} with refreshed token")

            return credential

        except HTTPException:
            raise
        except ValueError:
            raise
        except Exception as e:
            logger.error(f"Token refresh error for credential {credential.id}: {e}")
            raise HTTPException(status_code=400, detail=f"Token refresh error: {str(e)}")
