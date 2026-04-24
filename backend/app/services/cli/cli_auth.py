"""
CLI Authentication Service.

Handles JWT creation/decoding and token hashing for CLI tokens.
"""
import hashlib
import logging
import uuid
from datetime import datetime

import jwt

from app.core.config import settings
from app.core.security import ALGORITHM

logger = logging.getLogger(__name__)


class CLIAuthService:
    """Handles JWT creation, decoding, and hashing for CLI token authentication."""

    @staticmethod
    def create_cli_jwt(
        cli_token_id: uuid.UUID,
        agent_id: uuid.UUID,
        owner_id: uuid.UUID,
        expires_at: datetime,
    ) -> str:
        """
        Create a JWT for CLI authentication.

        Args:
            cli_token_id: UUID of the CLIToken record (used as JWT `sub`)
            agent_id: UUID of the agent this token is scoped to
            owner_id: UUID of the token owner
            expires_at: Expiration datetime (timezone-aware)

        Returns:
            Encoded JWT string
        """
        payload = {
            "sub": str(cli_token_id),
            "agent_id": str(agent_id),
            "owner_id": str(owner_id),
            "token_type": "cli",
            "exp": int(expires_at.timestamp()),
        }
        return jwt.encode(payload, settings.SECRET_KEY, algorithm=ALGORITHM)

    @staticmethod
    def decode_cli_jwt(token_str: str) -> dict:
        """
        Decode and validate a CLI JWT.

        Args:
            token_str: The JWT string to decode

        Returns:
            Decoded payload dict

        Raises:
            ValueError: If the token is invalid, expired, or not a CLI token
        """
        try:
            payload = jwt.decode(
                token_str,
                settings.SECRET_KEY,
                algorithms=[ALGORITHM],
            )
        except jwt.ExpiredSignatureError:
            raise ValueError("CLI token has expired")
        except jwt.InvalidTokenError as e:
            raise ValueError(f"Invalid CLI token: {e}")

        if payload.get("token_type") != "cli":
            raise ValueError("Token is not a CLI token")

        return payload

    @staticmethod
    def hash_token(token_str: str) -> str:
        """
        Create a SHA-256 hash of a token string for secure storage.

        Args:
            token_str: The token string to hash

        Returns:
            Hex-encoded SHA-256 hash
        """
        return hashlib.sha256(token_str.encode()).hexdigest()

    @staticmethod
    def decode_cli_jwt_from_websocket(websocket) -> str:
        """
        Extract the raw CLI JWT string from a WebSocket connection.

        Priority order:
        1. Authorization header: "Bearer <token>"
        2. Query parameter: ?token=<token>

        Args:
            websocket: FastAPI WebSocket connection object

        Returns:
            Raw JWT string

        Raises:
            ValueError: If no token is found in the connection
        """
        # 1. Check Authorization header
        auth_header = websocket.headers.get("authorization") or websocket.headers.get("Authorization")
        if auth_header:
            parts = auth_header.split(" ", 1)
            if len(parts) == 2 and parts[0].lower() == "bearer":
                return parts[1].strip()

        # 2. Fall back to query parameter
        token_param = websocket.query_params.get("token")
        if token_param:
            return token_param.strip()

        raise ValueError("No CLI token found in WebSocket connection (checked Authorization header and ?token= query param)")
