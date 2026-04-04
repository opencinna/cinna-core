"""
MCP domain exceptions.

Custom exception hierarchy for MCP-related service errors, following the
same pattern as InputTaskError in input_task_service.py.

Each exception carries a status_code so route handlers can convert them
to HTTPException with a single _handle_mcp_error() helper.
"""


class MCPError(Exception):
    """Base exception for MCP service errors."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class ConnectorNotFoundError(MCPError):
    """Connector not found."""

    def __init__(self, message: str = "Connector not found"):
        super().__init__(message, status_code=404)


class ConnectorInactiveError(MCPError):
    """Connector is inactive."""

    def __init__(self, message: str = "Connector is inactive"):
        super().__init__(message, status_code=404)


class MCPPermissionDeniedError(MCPError):
    """User does not have access to this resource."""

    def __init__(self, message: str = "Not authorized"):
        super().__init__(message, status_code=403)


class AgentNotAvailableError(MCPError):
    """Agent not found or has no active environment."""

    def __init__(self, message: str = "Agent not found or has no active environment"):
        super().__init__(message, status_code=404)


class EnvironmentNotFoundError(MCPError):
    """Agent environment not found."""

    def __init__(self, message: str = "Agent environment not found"):
        super().__init__(message, status_code=404)


class AuthRequestNotFoundError(MCPError):
    """OAuth auth request not found."""

    def __init__(self, message: str = "Auth request not found"):
        super().__init__(message, status_code=404)


class AuthRequestExpiredError(MCPError):
    """OAuth auth request has expired."""

    def __init__(self, message: str = "Auth request expired"):
        super().__init__(message, status_code=400)


class AuthRequestUsedError(MCPError):
    """OAuth auth request has already been used."""

    def __init__(self, message: str = "Auth request already used"):
        super().__init__(message, status_code=400)


class InvalidClientError(MCPError):
    """Invalid OAuth client credentials."""

    def __init__(self, message: str = "Invalid client credentials"):
        super().__init__(message, status_code=401)


class InvalidGrantError(MCPError):
    """Invalid or expired authorization code / refresh token."""

    def __init__(self, message: str = "Invalid grant"):
        super().__init__(message, status_code=400)


class MaxClientsReachedError(MCPError):
    """Maximum number of OAuth clients reached for this connector."""

    def __init__(self, message: str = "Maximum number of clients reached"):
        super().__init__(message, status_code=429)
