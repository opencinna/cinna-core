"""
MCP OAuth Service — handles OAuth 2.1 business logic.

Extracted from oauth_routes.py to follow the service layer pattern.
Covers Dynamic Client Registration, authorization, token exchange,
refresh, and revocation.
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
from app.models.mcp.mcp_connector import MCPConnector
from app.models.mcp.mcp_oauth_client import MCPOAuthClient
from app.models.mcp.mcp_auth_code import MCPAuthCode, MCPAuthRequest
from app.models.mcp.mcp_token import MCPToken
from app.services.mcp.mcp_errors import (
    ConnectorNotFoundError,
    ConnectorInactiveError,
    InvalidClientError,
    InvalidGrantError,
    MaxClientsReachedError,
    MCPError,
)

logger = logging.getLogger(__name__)


# ── Helper Functions ──────────────────────────────────────────────────────────


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _verify_secret(plain: str, hashed: str) -> bool:
    return _hash_secret(plain) == hashed


def _generate_opaque_token() -> str:
    return secrets.token_urlsafe(48)


def _verify_pkce(code_verifier: str, code_challenge: str) -> bool:
    """Verify PKCE S256 challenge."""
    if not code_verifier or not code_challenge:
        return False
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return computed == code_challenge


def extract_connector_id_from_resource(resource_url: str) -> str | None:
    """Extract connector_id UUID from resource URL like {base}/connector-id/mcp."""
    if not resource_url or not settings.MCP_SERVER_BASE_URL:
        return None
    base = settings.MCP_SERVER_BASE_URL.rstrip("/")
    if not resource_url.startswith(base + "/"):
        return None
    remainder = resource_url[len(base) + 1:]
    parts = remainder.strip("/").split("/")
    if len(parts) >= 1:
        try:
            uuid.UUID(parts[0])
            return parts[0]
        except ValueError:
            return None
    return None


def extract_connector_id_from_resource_path(resource_path: str) -> str | None:
    """Extract connector_id UUID from a resource path like 'mcp/{connector_id}/mcp'."""
    parts = resource_path.strip("/").split("/")
    for part in parts:
        try:
            uuid.UUID(part)
            return part
        except ValueError:
            continue
    return None


def get_as_metadata_dict() -> dict:
    """Build the RFC 8414 Authorization Server Metadata dict."""
    base = settings.MCP_SERVER_BASE_URL.rstrip("/") if settings.MCP_SERVER_BASE_URL else ""
    return {
        "issuer": f"{base}/oauth",
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "revocation_endpoint": f"{base}/oauth/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": ["mcp:tools", "mcp:resources"],
    }


# ── Data Classes ──────────────────────────────────────────────────────────────


@dataclass
class DCRInput:
    client_name: str = ""
    redirect_uris: list[str] = field(default_factory=list)
    grant_types: list[str] = field(default_factory=lambda: ["authorization_code", "refresh_token"])
    response_types: list[str] = field(default_factory=lambda: ["code"])
    resource: str = ""


@dataclass
class AuthorizeInput:
    response_type: str = ""
    client_id: str = ""
    redirect_uri: str = ""
    scope: str = ""
    state: str = ""
    code_challenge: str = ""
    code_challenge_method: str = "S256"
    resource: str = ""


@dataclass
class TokenExchangeInput:
    code: str = ""
    redirect_uri: str = ""
    client_id: str = ""
    client_secret: str = ""
    code_verifier: str = ""
    resource: str = ""


@dataclass
class RefreshInput:
    client_id: str = ""
    client_secret: str = ""
    refresh_token: str = ""


@dataclass
class DCRResult:
    client_id: str
    client_secret: str
    client_name: str
    redirect_uris: list[str]
    grant_types: list[str]
    response_types: list[str]


@dataclass
class TokenResult:
    access_token: str
    token_type: str = "bearer"
    expires_in: int = 3600
    refresh_token: str | None = None
    scope: str = ""


# ── Service ───────────────────────────────────────────────────────────────────


class MCPOAuthService:
    """Handles OAuth 2.1 business logic for MCP connectors."""

    @staticmethod
    def register_client(
        db_session: DBSession,
        data: DCRInput,
    ) -> DCRResult:
        """Dynamic Client Registration (RFC 7591).

        If resource is provided, links the client to a specific connector
        and enforces max_clients. Otherwise registers a global client.

        Raises:
            ConnectorNotFoundError: if connector from resource URL doesn't exist.
            ConnectorInactiveError: if connector is inactive.
            MaxClientsReachedError: if connector has reached max_clients.
        """
        connector_id: uuid.UUID | None = None
        connector_id_str = extract_connector_id_from_resource(data.resource)

        if connector_id_str:
            connector_id = uuid.UUID(connector_id_str)
            connector = db_session.get(MCPConnector, connector_id)
            if not connector:
                raise ConnectorNotFoundError()
            if not connector.is_active:
                raise ConnectorInactiveError()

            existing_count = db_session.exec(
                select(MCPOAuthClient).where(
                    MCPOAuthClient.connector_id == connector_id
                )
            ).all()
            if len(existing_count) >= connector.max_clients:
                raise MaxClientsReachedError()

        client_id = str(uuid.uuid4())
        client_secret = secrets.token_urlsafe(48)

        oauth_client = MCPOAuthClient(
            client_id=client_id,
            client_secret_hash=_hash_secret(client_secret),
            client_name=data.client_name,
            redirect_uris=data.redirect_uris,
            grant_types=data.grant_types,
            response_types=data.response_types,
            connector_id=connector_id,
        )
        db_session.add(oauth_client)
        db_session.commit()

        return DCRResult(
            client_id=client_id,
            client_secret=client_secret,
            client_name=data.client_name,
            redirect_uris=data.redirect_uris,
            grant_types=data.grant_types,
            response_types=data.response_types,
        )

    @staticmethod
    def create_authorization(
        db_session: DBSession,
        data: AuthorizeInput,
    ) -> str:
        """Create an OAuth authorization request and return the consent page URL.

        Validates the client, stores the full OAuth request server-side
        (keyed by nonce), and returns the frontend consent page URL.

        Args:
            db_session: Database session.
            data: Authorization request parameters.

        Returns:
            The consent page URL with nonce query param.

        Raises:
            MCPError: if response_type is invalid.
            MCPError: if resource URL is invalid.
            InvalidClientError: if client_id is unknown.
        """
        if data.response_type != "code":
            raise MCPError("Unsupported response_type")

        connector_id_str = extract_connector_id_from_resource(data.resource)
        if not connector_id_str:
            raise MCPError("Invalid resource URL")

        connector_id = uuid.UUID(connector_id_str)

        # Validate client exists
        oauth_client = db_session.exec(
            select(MCPOAuthClient).where(
                MCPOAuthClient.client_id == data.client_id
            )
        ).first()
        if not oauth_client:
            raise MCPError("Unknown client_id for this resource")

        # Store auth request
        nonce = secrets.token_urlsafe(32)
        auth_request = MCPAuthRequest(
            nonce=nonce,
            connector_id=connector_id,
            client_id=data.client_id,
            redirect_uri=data.redirect_uri,
            code_challenge=data.code_challenge,
            code_challenge_method=data.code_challenge_method,
            scope=data.scope,
            state=data.state,
            resource=data.resource,
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
        )
        db_session.add(auth_request)
        db_session.commit()

        frontend_url = settings.FRONTEND_HOST.rstrip("/")
        return f"{frontend_url}/oauth/mcp-consent?nonce={nonce}"

    @staticmethod
    def exchange_authorization_code(
        db_session: DBSession,
        data: TokenExchangeInput,
    ) -> TokenResult:
        """Exchange an authorization code for access + refresh tokens.

        Validates client credentials, auth code, PKCE, and resource match.

        Raises:
            InvalidClientError: if client credentials are invalid.
            InvalidGrantError: if auth code is invalid, used, expired,
                              or PKCE/resource mismatch.
        """
        # Validate client credentials
        oauth_client = db_session.exec(
            select(MCPOAuthClient).where(
                MCPOAuthClient.client_id == data.client_id
            )
        ).first()
        if not oauth_client or not _verify_secret(
            data.client_secret, oauth_client.client_secret_hash
        ):
            raise InvalidClientError()

        # Look up auth code
        auth_code = db_session.get(MCPAuthCode, data.code)
        if not auth_code:
            raise InvalidGrantError("Invalid authorization code")
        if auth_code.used:
            raise InvalidGrantError("Authorization code already used")
        now = datetime.now(UTC).replace(tzinfo=None)
        if auth_code.expires_at < now:
            raise InvalidGrantError("Authorization code expired")
        if auth_code.client_id != data.client_id:
            raise InvalidGrantError("Client ID mismatch")

        # Verify PKCE
        if auth_code.code_challenge:
            if not _verify_pkce(data.code_verifier, auth_code.code_challenge):
                raise InvalidGrantError("Invalid code_verifier")

        # Verify resource matches
        if data.resource and auth_code.resource and data.resource != auth_code.resource:
            raise InvalidGrantError("Resource mismatch")

        # Mark code as used
        auth_code.used = True
        db_session.add(auth_code)

        # Generate tokens
        access_token_str = _generate_opaque_token()
        refresh_token_str = _generate_opaque_token()

        access_token = MCPToken(
            token=access_token_str,
            token_type="access",
            client_id=data.client_id,
            user_id=auth_code.user_id,
            connector_id=auth_code.connector_id,
            scope=auth_code.scope,
            resource=auth_code.resource,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        refresh_token = MCPToken(
            token=refresh_token_str,
            token_type="refresh",
            client_id=data.client_id,
            user_id=auth_code.user_id,
            connector_id=auth_code.connector_id,
            scope=auth_code.scope,
            resource=auth_code.resource,
            expires_at=datetime.now(UTC) + timedelta(days=30),
        )
        db_session.add(access_token)
        db_session.add(refresh_token)
        db_session.commit()

        return TokenResult(
            access_token=access_token_str,
            refresh_token=refresh_token_str,
            scope=auth_code.scope or "",
        )

    @staticmethod
    def refresh_access_token(
        db_session: DBSession,
        data: RefreshInput,
    ) -> TokenResult:
        """Exchange a refresh token for a new access token.

        Raises:
            InvalidClientError: if client credentials are invalid.
            InvalidGrantError: if refresh token is invalid, revoked, or expired.
        """
        # Validate client credentials
        oauth_client = db_session.exec(
            select(MCPOAuthClient).where(
                MCPOAuthClient.client_id == data.client_id
            )
        ).first()
        if not oauth_client or not _verify_secret(
            data.client_secret, oauth_client.client_secret_hash
        ):
            raise InvalidClientError()

        # Look up refresh token
        token_record = db_session.exec(
            select(MCPToken).where(
                MCPToken.token == data.refresh_token,
                MCPToken.token_type == "refresh",
            )
        ).first()
        if not token_record:
            raise InvalidGrantError("Invalid refresh token")
        if token_record.revoked:
            raise InvalidGrantError("Refresh token revoked")
        now = datetime.now(UTC).replace(tzinfo=None)
        if token_record.expires_at < now:
            raise InvalidGrantError("Refresh token expired")
        if token_record.client_id != data.client_id:
            raise InvalidGrantError("Client ID mismatch")

        # Generate new access token
        new_access_token_str = _generate_opaque_token()
        new_access_token = MCPToken(
            token=new_access_token_str,
            token_type="access",
            client_id=data.client_id,
            user_id=token_record.user_id,
            connector_id=token_record.connector_id,
            scope=token_record.scope,
            resource=token_record.resource,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        db_session.add(new_access_token)
        db_session.commit()

        return TokenResult(
            access_token=new_access_token_str,
            scope=token_record.scope or "",
        )

    @staticmethod
    def revoke_token(
        db_session: DBSession,
        token: str,
    ) -> None:
        """Revoke an access or refresh token.

        Per RFC 7009, this always succeeds (no error even if token not found).
        """
        token_record = db_session.exec(
            select(MCPToken).where(MCPToken.token == token)
        ).first()
        if token_record:
            token_record.revoked = True
            db_session.add(token_record)
            db_session.commit()
