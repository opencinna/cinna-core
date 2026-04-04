"""
MCP Consent Service — handles OAuth consent flow business logic.

Extracted from mcp_consent.py routes to follow the service layer pattern.
"""
import secrets
from datetime import datetime, timedelta, UTC
from urllib.parse import urlencode

from sqlmodel import Session as DBSession, select

from app.models import Agent, User
from app.models.mcp.mcp_connector import MCPConnector
from app.models.mcp.mcp_oauth_client import MCPOAuthClient
from app.models.mcp.mcp_auth_code import MCPAuthCode, MCPAuthRequest
from app.services.mcp.mcp_errors import (
    AuthRequestNotFoundError,
    AuthRequestExpiredError,
    AuthRequestUsedError,
    ConnectorNotFoundError,
    MCPPermissionDeniedError,
)


class ConsentDetails:
    """Non-sensitive info displayed on the consent page."""

    __slots__ = (
        "agent_name", "connector_name", "connector_mode",
        "client_name", "scopes", "expires_at",
    )

    def __init__(
        self,
        agent_name: str,
        connector_name: str,
        connector_mode: str,
        client_name: str,
        scopes: list[str],
        expires_at: datetime,
    ):
        self.agent_name = agent_name
        self.connector_name = connector_name
        self.connector_mode = connector_mode
        self.client_name = client_name
        self.scopes = scopes
        self.expires_at = expires_at


class MCPConsentService:
    """Handles OAuth consent flow business logic."""

    @staticmethod
    def _validate_auth_request(
        db_session: DBSession,
        nonce: str,
    ) -> MCPAuthRequest:
        """Validate an auth request by nonce: exists, not used, not expired.

        Returns:
            The validated MCPAuthRequest.

        Raises:
            AuthRequestNotFoundError: if the nonce doesn't exist.
            AuthRequestUsedError: if the auth request was already used.
            AuthRequestExpiredError: if the auth request has expired.
        """
        auth_request = db_session.get(MCPAuthRequest, nonce)
        if not auth_request:
            raise AuthRequestNotFoundError()
        if auth_request.used:
            raise AuthRequestUsedError()
        now = datetime.now(UTC).replace(tzinfo=None)
        if auth_request.expires_at < now:
            raise AuthRequestExpiredError()
        return auth_request

    @staticmethod
    def get_consent_details(
        db_session: DBSession,
        nonce: str,
    ) -> ConsentDetails:
        """Fetch auth request details for the consent page.

        Args:
            db_session: Database session.
            nonce: Auth request nonce.

        Returns:
            ConsentDetails with agent, connector, client info and scopes.

        Raises:
            AuthRequestNotFoundError, AuthRequestUsedError,
            AuthRequestExpiredError, ConnectorNotFoundError.
        """
        auth_request = MCPConsentService._validate_auth_request(db_session, nonce)

        connector = db_session.get(MCPConnector, auth_request.connector_id)
        if not connector:
            raise ConnectorNotFoundError()

        agent = db_session.get(Agent, connector.agent_id)
        agent_name = agent.name if agent else "Unknown Agent"

        oauth_client = db_session.exec(
            select(MCPOAuthClient).where(
                MCPOAuthClient.client_id == auth_request.client_id
            )
        ).first()
        client_name = oauth_client.client_name if oauth_client else "Unknown Client"

        scopes = (
            [s for s in auth_request.scope.split(" ") if s]
            if auth_request.scope
            else []
        )

        return ConsentDetails(
            agent_name=agent_name,
            connector_name=connector.name,
            connector_mode=connector.mode,
            client_name=client_name,
            scopes=scopes,
            expires_at=auth_request.expires_at,
        )

    @staticmethod
    def approve_consent(
        db_session: DBSession,
        nonce: str,
        current_user: User,
    ) -> str:
        """Approve an OAuth consent request.

        Validates the auth request, checks email ACL, creates an auth code,
        and returns the redirect URL with code and state params.

        Args:
            db_session: Database session.
            nonce: Auth request nonce.
            current_user: The authenticated user approving the consent.

        Returns:
            The redirect URL string with code and state query params.

        Raises:
            AuthRequestNotFoundError, AuthRequestUsedError,
            AuthRequestExpiredError, ConnectorNotFoundError,
            MCPPermissionDeniedError.
        """
        auth_request = MCPConsentService._validate_auth_request(db_session, nonce)

        connector = db_session.get(MCPConnector, auth_request.connector_id)
        if not connector:
            raise ConnectorNotFoundError()

        # Check email access: user must be connector owner or in allowed_emails
        is_owner = connector.owner_id == current_user.id
        email_allowed = (
            current_user.email
            and connector.allowed_emails
            and current_user.email.lower()
            in [e.lower() for e in connector.allowed_emails]
        )
        if not is_owner and not email_allowed:
            raise MCPPermissionDeniedError(
                "You don't have access to this connector"
            )

        # Mark auth request as used
        auth_request.used = True
        db_session.add(auth_request)

        # Create auth code
        code = secrets.token_urlsafe(48)
        auth_code = MCPAuthCode(
            code=code,
            client_id=auth_request.client_id,
            user_id=current_user.id,
            connector_id=auth_request.connector_id,
            redirect_uri=auth_request.redirect_uri,
            code_challenge=auth_request.code_challenge,
            scope=auth_request.scope,
            resource=auth_request.resource,
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
        db_session.add(auth_code)
        db_session.commit()

        # Build redirect URL with code and state
        params = {"code": code}
        if auth_request.state:
            params["state"] = auth_request.state
        return f"{auth_request.redirect_uri}?{urlencode(params)}"
