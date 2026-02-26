"""
Temporary JWT tokens for MCP file uploads.

MCP clients can't transfer large files through the MCP protocol, so we provide
a `get_file_upload_url` tool that returns a CURL command with a short-lived JWT.
The client executes the CURL to upload directly to the backend's upload endpoint.

These tokens are self-contained (no DB table needed) and purpose-scoped to
prevent reuse as regular access tokens.
"""
import uuid
from datetime import datetime, timedelta, timezone

import jwt

from app.core.config import settings
from app.core.security import ALGORITHM


def create_file_upload_token(connector_id: str, expires_minutes: int = 15) -> str:
    """Create a short-lived JWT for file upload to a specific connector.

    Args:
        connector_id: MCP connector UUID string
        expires_minutes: Token validity in minutes (default 15)

    Returns:
        Signed JWT string
    """
    now = datetime.now(timezone.utc)
    payload = {
        "sub": connector_id,
        "purpose": "mcp_file_upload",
        "exp": now + timedelta(minutes=expires_minutes),
        "iat": now,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)


def verify_file_upload_token(token: str) -> str | None:
    """Verify a file upload JWT and return the connector_id if valid.

    Checks signature, expiry, and purpose claim.

    Returns:
        connector_id string if valid, None otherwise
    """
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("purpose") != "mcp_file_upload":
            return None
        return payload.get("sub")
    except jwt.PyJWTError:
        return None
