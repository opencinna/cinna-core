"""
App MCP OAuth Service — OAuth 2.1 business logic for the App MCP Server.

Mirrors MCPOAuthService but without connector_id coupling.
Stores tokens in app_mcp_token table (SHA256 hashed).
"""
import base64
import hashlib
import secrets
import uuid
import logging
from datetime import datetime, timedelta, UTC
from dataclasses import dataclass, field

from sqlmodel import Session as DBSession, select

from app.core.config import settings
from app.models.app_mcp.app_mcp_oauth_client import AppMCPOAuthClient
from app.models.app_mcp.app_mcp_auth_code import AppMCPAuthCode, AppMCPAuthRequest
from app.models.app_mcp.app_mcp_token import AppMCPToken

logger = logging.getLogger(__name__)

APP_MCP_RESOURCE_SUFFIX = "/app/mcp"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _verify_secret(plain: str, hashed: str) -> bool:
    return _hash_secret(plain) == hashed


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _generate_opaque_token() -> str:
    return secrets.token_urlsafe(48)


def _verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    if not code_verifier or not code_challenge:
        return False
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return computed == code_challenge


def is_app_mcp_resource(resource_url: str) -> bool:
    """Return True if the resource URL points to the App MCP Server."""
    if not resource_url or not settings.MCP_SERVER_BASE_URL:
        return False
    base = settings.MCP_SERVER_BASE_URL.rstrip("/")
    return resource_url == f"{base}{APP_MCP_RESOURCE_SUFFIX}"


# ---------------------------------------------------------------------------
# Data classes (mirrors MCPOAuthService dataclasses)
# ---------------------------------------------------------------------------


@dataclass
class AppDCRInput:
    client_name: str = ""
    redirect_uris: list[str] = field(default_factory=list)
    grant_types: list[str] = field(default_factory=lambda: ["authorization_code", "refresh_token"])
    response_types: list[str] = field(default_factory=lambda: ["code"])
    resource: str = ""


@dataclass
class AppTokenResult:
    access_token: str
    token_type: str = "bearer"
    expires_in: int = 3600
    refresh_token: str | None = None
    scope: str = ""


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class AppMCPOAuthService:
    """Handles OAuth 2.1 business logic for the App MCP Server."""

    @staticmethod
    def register_client(
        db_session: DBSession,
        data: AppDCRInput,
    ) -> dict:
        """Dynamic Client Registration for the App MCP Server.

        No max_clients limit. The route that calls this (``POST /mcp/oauth/register``
        in ``app/mcp/oauth_routes.py``) enforces **no authentication** — RFC 7591
        dynamic registration is open — so any caller, signed in or not, can
        register a client; the binding to a user happens later, at consent.
        Returns dict with client_id and client_secret.
        """
        client_id = str(uuid.uuid4())
        client_secret = secrets.token_urlsafe(48)

        oauth_client = AppMCPOAuthClient(
            client_id=client_id,
            client_secret_hash=_hash_secret(client_secret),
            client_name=data.client_name,
            redirect_uris=data.redirect_uris,
            grant_types=data.grant_types,
            response_types=data.response_types,
        )
        db_session.add(oauth_client)
        db_session.commit()

        return {
            "client_id": client_id,
            "client_secret": client_secret,
            "client_name": data.client_name,
            "redirect_uris": data.redirect_uris,
            "grant_types": data.grant_types,
            "response_types": data.response_types,
        }

    @staticmethod
    def create_authorization(
        db_session: DBSession,
        client_id: str,
        redirect_uri: str,
        scope: str,
        state: str,
        code_challenge: str,
        code_challenge_method: str,
        resource: str,
    ) -> str:
        """Store an OAuth auth request and return the consent page URL.

        Shows "Application MCP Server" on the consent page (no specific agent).
        """
        # Validate client — check App MCP table first, then fall back to
        # per-connector table (clients may register globally without resource
        # during DCR and only specify resource during authorize)
        oauth_client = db_session.exec(
            select(AppMCPOAuthClient).where(AppMCPOAuthClient.client_id == client_id)
        ).first()
        if not oauth_client:
            from app.models.mcp.mcp_oauth_client import MCPOAuthClient
            per_connector_client = db_session.exec(
                select(MCPOAuthClient).where(MCPOAuthClient.client_id == client_id)
            ).first()
            if not per_connector_client:
                raise ValueError("Unknown client_id")

        nonce = secrets.token_urlsafe(32)
        auth_request = AppMCPAuthRequest(
            nonce=nonce,
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            scope=scope,
            state=state,
            resource=resource,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        db_session.add(auth_request)
        db_session.commit()

        frontend_url = settings.FRONTEND_HOST.rstrip("/")
        # Use a dedicated consent page param to indicate app-level consent
        return f"{frontend_url}/oauth/mcp-consent?nonce={nonce}&app_mcp=true"

    @staticmethod
    def exchange_authorization_code(
        db_session: DBSession,
        code: str,
        redirect_uri: str,
        client_id: str,
        client_secret: str,
        code_verifier: str,
        resource: str,
    ) -> AppTokenResult:
        """Exchange an authorization code for access + refresh tokens."""
        # Validate client — check App MCP table first, fall back to per-connector
        oauth_client = db_session.exec(
            select(AppMCPOAuthClient).where(AppMCPOAuthClient.client_id == client_id)
        ).first()
        if oauth_client:
            if not _verify_secret(client_secret, oauth_client.client_secret_hash):
                raise ValueError("Invalid client credentials")
        else:
            from app.models.mcp.mcp_oauth_client import MCPOAuthClient
            per_connector_client = db_session.exec(
                select(MCPOAuthClient).where(MCPOAuthClient.client_id == client_id)
            ).first()
            if not per_connector_client or not _verify_secret(client_secret, per_connector_client.client_secret_hash):
                raise ValueError("Invalid client credentials")

        # Look up auth code
        auth_code = db_session.get(AppMCPAuthCode, code)
        if not auth_code:
            raise ValueError("Invalid authorization code")
        if auth_code.used:
            raise ValueError("Authorization code already used")
        now = datetime.now(UTC).replace(tzinfo=None)
        if auth_code.expires_at < now:
            raise ValueError("Authorization code expired")
        if auth_code.client_id != client_id:
            raise ValueError("Client ID mismatch")

        # Verify PKCE
        if auth_code.code_challenge:
            if not _verify_pkce(code_verifier, auth_code.code_challenge):
                raise ValueError("Invalid code_verifier")

        # Mark code as used
        auth_code.used = True
        db_session.add(auth_code)

        # Generate tokens (stored as SHA256 hashes)
        access_token_str = _generate_opaque_token()
        refresh_token_str = _generate_opaque_token()

        access_token = AppMCPToken(
            user_id=auth_code.user_id,
            client_id=client_id,
            token_hash=_hash_token(access_token_str),
            token_type="access",
            scope=auth_code.scope,
            resource=auth_code.resource,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        refresh_token_record = AppMCPToken(
            user_id=auth_code.user_id,
            client_id=client_id,
            token_hash=_hash_token(refresh_token_str),
            token_type="refresh",
            scope=auth_code.scope,
            resource=auth_code.resource,
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        db_session.add(access_token)
        db_session.add(refresh_token_record)
        db_session.commit()

        return AppTokenResult(
            access_token=access_token_str,
            refresh_token=refresh_token_str,
            scope=auth_code.scope or "",
        )

    @staticmethod
    def refresh_access_token(
        db_session: DBSession,
        client_id: str,
        client_secret: str,
        refresh_token: str,
    ) -> AppTokenResult:
        """Exchange a refresh token for a new access token."""
        # Validate client — check App MCP table first, fall back to per-connector
        oauth_client = db_session.exec(
            select(AppMCPOAuthClient).where(AppMCPOAuthClient.client_id == client_id)
        ).first()
        if oauth_client:
            if not _verify_secret(client_secret, oauth_client.client_secret_hash):
                raise ValueError("Invalid client credentials")
        else:
            from app.models.mcp.mcp_oauth_client import MCPOAuthClient
            per_connector_client = db_session.exec(
                select(MCPOAuthClient).where(MCPOAuthClient.client_id == client_id)
            ).first()
            if not per_connector_client or not _verify_secret(client_secret, per_connector_client.client_secret_hash):
                raise ValueError("Invalid client credentials")

        # Look up refresh token by hash
        refresh_hash = _hash_token(refresh_token)
        token_record = db_session.exec(
            select(AppMCPToken).where(
                AppMCPToken.token_hash == refresh_hash,
                AppMCPToken.token_type == "refresh",
                AppMCPToken.client_id == client_id,
            )
        ).first()
        if not token_record:
            raise ValueError("Invalid refresh token")
        if token_record.is_revoked:
            raise ValueError("Refresh token revoked")
        now = datetime.now(UTC).replace(tzinfo=None)
        if token_record.expires_at < now:
            raise ValueError("Refresh token expired")

        # Revoke old access tokens for this client/user
        old_tokens = db_session.exec(
            select(AppMCPToken).where(
                AppMCPToken.user_id == token_record.user_id,
                AppMCPToken.client_id == client_id,
                AppMCPToken.token_type == "access",
                AppMCPToken.is_revoked == False,  # noqa: E712
            )
        ).all()
        for t in old_tokens:
            t.is_revoked = True
            db_session.add(t)

        # Issue new access token
        new_access_token_str = _generate_opaque_token()
        new_access_token = AppMCPToken(
            user_id=token_record.user_id,
            client_id=client_id,
            token_hash=_hash_token(new_access_token_str),
            token_type="access",
            scope=token_record.scope,
            resource=token_record.resource,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        db_session.add(new_access_token)
        db_session.commit()

        return AppTokenResult(
            access_token=new_access_token_str,
            scope=token_record.scope or "",
        )

    @staticmethod
    def revoke_token(db_session: DBSession, token: str) -> None:
        """Revoke a token by its hash. No-op if not found."""
        token_hash = _hash_token(token)
        token_record = db_session.exec(
            select(AppMCPToken).where(AppMCPToken.token_hash == token_hash)
        ).first()
        if token_record:
            token_record.is_revoked = True
            db_session.add(token_record)
            db_session.commit()

    @staticmethod
    def create_auth_code_from_request(
        db_session: DBSession,
        nonce: str,
        user_id: uuid.UUID,
    ) -> tuple[str, str]:
        """Create an auth code after user consents.

        Used by the consent page API to issue the code after user approves.
        Returns (code, redirect_uri_with_params).
        """
        auth_request = db_session.get(AppMCPAuthRequest, nonce)
        if not auth_request or auth_request.used:
            raise ValueError("Invalid or expired consent request")
        now = datetime.now(UTC).replace(tzinfo=None)
        if auth_request.expires_at < now:
            raise ValueError("Consent request expired")

        # Mark request as used
        auth_request.used = True
        db_session.add(auth_request)

        # Create auth code
        code = secrets.token_urlsafe(32)
        auth_code = AppMCPAuthCode(
            code=code,
            client_id=auth_request.client_id,
            user_id=user_id,
            redirect_uri=auth_request.redirect_uri,
            code_challenge=auth_request.code_challenge,
            code_challenge_method=auth_request.code_challenge_method,
            scope=auth_request.scope,
            resource=auth_request.resource,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        db_session.add(auth_code)
        db_session.commit()

        # Build redirect URI
        redirect_uri = auth_request.redirect_uri
        sep = "&" if "?" in redirect_uri else "?"
        redirect_uri += f"{sep}code={code}"
        if auth_request.state:
            redirect_uri += f"&state={auth_request.state}"

        return code, redirect_uri

    @staticmethod
    def get_auth_request(db_session: DBSession, nonce: str) -> AppMCPAuthRequest | None:
        """Look up a pending auth request by nonce."""
        return db_session.get(AppMCPAuthRequest, nonce)
