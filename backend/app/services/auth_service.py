"""
Auth Service - Business logic for authentication and OAuth operations.
"""
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlmodel import Session, select

from app.core import security
from app.core.config import settings
from app.models import User


class AuthService:
    """
    Service for authentication operations including Google OAuth.

    Responsibilities:
    - OAuth state management (CSRF tokens)
    - Google OAuth flow (authorization URL, token exchange)
    - User lookup/creation for OAuth
    - JWT token generation
    """

    # In-memory state storage (use Redis in production)
    _oauth_states: dict[str, dict[str, Any]] = {}

    # Google OAuth constants
    GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
    GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
    GOOGLE_SCOPES = "openid%20email%20profile"

    @classmethod
    def is_google_oauth_enabled(cls) -> bool:
        """Check if Google OAuth is configured."""
        return settings.google_oauth_enabled

    @classmethod
    def is_email_domain_allowed(cls, email: str) -> bool:
        """
        Check if email domain is allowed for registration.

        Returns True if:
        - No whitelist is configured (all domains allowed)
        - Email domain is in the whitelist
        """
        whitelist = settings.auth_whitelist_domains
        if not whitelist:
            return True

        # Extract domain from email
        if "@" not in email:
            return False
        domain = email.split("@")[1].lower()
        return domain in whitelist

    @classmethod
    def generate_oauth_state(cls) -> str:
        """
        Generate a CSRF state token for OAuth flow.

        Returns:
            State token string
        """
        state = secrets.token_urlsafe(32)
        cls._oauth_states[state] = {
            "expires": datetime.now(timezone.utc).timestamp() + 600  # 10 minutes
        }
        cls._cleanup_expired_states()
        return state

    @classmethod
    def _cleanup_expired_states(cls) -> None:
        """Remove expired state tokens from storage."""
        now = datetime.now(timezone.utc).timestamp()
        expired_states = [k for k, v in cls._oauth_states.items() if v["expires"] < now]
        for k in expired_states:
            del cls._oauth_states[k]

    @classmethod
    def consume_oauth_state(cls, state: str) -> None:
        """
        Consume (remove) a state token after use.

        Note: State validation skipped for popup flow (@react-oauth/google)
        The popup flow has built-in CSRF protection via browser same-origin policy.
        """
        cls._oauth_states.pop(state, None)

    @classmethod
    def build_google_authorization_url(cls, state: str) -> str:
        """
        Build Google OAuth authorization URL.

        Args:
            state: CSRF state token

        Returns:
            Full authorization URL
        """
        return (
            f"{cls.GOOGLE_AUTH_URL}?"
            f"client_id={settings.GOOGLE_CLIENT_ID}&"
            f"redirect_uri={settings.GOOGLE_REDIRECT_URI}&"
            "response_type=code&"
            f"scope={cls.GOOGLE_SCOPES}&"
            f"state={state}"
        )

    @classmethod
    async def exchange_google_code(cls, code: str) -> dict[str, Any]:
        """
        Exchange authorization code for tokens with Google.

        Args:
            code: Authorization code from Google

        Returns:
            Token response data containing id_token, access_token, etc.

        Raises:
            ValueError: If token exchange fails
        """
        async with httpx.AsyncClient() as client:
            token_response = await client.post(
                cls.GOOGLE_TOKEN_URL,
                data={
                    "code": code,
                    "client_id": settings.GOOGLE_CLIENT_ID,
                    "client_secret": settings.GOOGLE_CLIENT_SECRET,
                    "redirect_uri": "postmessage",  # Required for popup flow
                    "grant_type": "authorization_code",
                },
            )

        if token_response.status_code != 200:
            raise ValueError("Failed to exchange authorization code")

        return token_response.json()

    @classmethod
    async def verify_and_decode_google_token(
        cls, id_token: str
    ) -> dict[str, Any] | None:
        """
        Verify and decode Google ID token.

        Args:
            id_token: Google ID token (JWT)

        Returns:
            Token claims or None if invalid
        """
        return await security.verify_google_token(id_token, settings.GOOGLE_CLIENT_ID)

    @classmethod
    def get_user_by_google_id(cls, *, session: Session, google_id: str) -> User | None:
        """Get user by Google ID."""
        statement = select(User).where(User.google_id == google_id)
        return session.exec(statement).first()

    @classmethod
    def get_user_by_email(cls, *, session: Session, email: str) -> User | None:
        """Get user by email address."""
        statement = select(User).where(User.email == email)
        return session.exec(statement).first()

    @classmethod
    def create_user_from_google(
        cls,
        *,
        session: Session,
        email: str,
        google_id: str,
        full_name: str | None,
    ) -> User:
        """
        Create a new user from Google OAuth (no password).

        Args:
            session: Database session
            email: User's email from Google
            google_id: Google's unique user ID
            full_name: User's full name from Google

        Returns:
            Created User

        Raises:
            ValueError: If email domain is not in whitelist
        """
        # Check domain whitelist for new user registration
        if not cls.is_email_domain_allowed(email):
            raise ValueError(
                "Registration is restricted to specific email domains"
            )

        db_obj = User(
            email=email,
            google_id=google_id,
            full_name=full_name,
            hashed_password=None,
            is_active=True,
            is_superuser=False,
        )
        session.add(db_obj)
        session.commit()
        session.refresh(db_obj)

        return db_obj

    @classmethod
    def link_google_account(
        cls, *, session: Session, user: User, google_id: str
    ) -> User:
        """
        Link a Google account to an existing user.

        Args:
            session: Database session
            user: User to link
            google_id: Google's unique user ID

        Returns:
            Updated User
        """
        user.google_id = google_id
        session.add(user)
        session.commit()
        session.refresh(user)
        return user

    @classmethod
    def unlink_google_account(cls, *, session: Session, user: User) -> User:
        """
        Unlink Google account from user.

        Args:
            session: Database session
            user: User to unlink

        Returns:
            Updated User
        """
        user.google_id = None
        session.add(user)
        session.commit()
        session.refresh(user)
        return user

    @classmethod
    def create_access_token(cls, user_id: Any) -> str:
        """
        Create a JWT access token for a user.

        Args:
            user_id: User's ID

        Returns:
            JWT access token string
        """
        access_token_expires = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        return security.create_access_token(user_id, expires_delta=access_token_expires)

    @classmethod
    async def authenticate_with_google(
        cls, *, session: Session, code: str, state: str
    ) -> tuple[User, str]:
        """
        Complete Google OAuth authentication flow.

        Handles:
        - State cleanup
        - Code exchange
        - Token verification
        - User lookup/creation/linking

        Args:
            session: Database session
            code: Authorization code from Google
            state: CSRF state token

        Returns:
            Tuple of (User, access_token)

        Raises:
            ValueError: If authentication fails
        """
        # Clean up state (for backwards compatibility with redirect flow)
        cls.consume_oauth_state(state)

        # Exchange code for tokens
        token_data = await cls.exchange_google_code(code)

        id_token = token_data.get("id_token")
        if not id_token:
            raise ValueError("No ID token received")

        # Verify and decode ID token
        claims = await cls.verify_and_decode_google_token(id_token)
        if not claims:
            raise ValueError("Invalid Google token")

        google_id = claims["sub"]
        email = claims["email"]
        full_name = claims.get("name")

        # Find or create user
        user = cls.get_user_by_google_id(session=session, google_id=google_id)

        if not user:
            # Check if user exists by email (auto-link)
            user = cls.get_user_by_email(session=session, email=email)
            if user:
                # Auto-link Google account to existing email user
                user = cls.link_google_account(
                    session=session, user=user, google_id=google_id
                )
            else:
                # Create new user
                user = cls.create_user_from_google(
                    session=session,
                    email=email,
                    google_id=google_id,
                    full_name=full_name,
                )

        if not user.is_active:
            raise ValueError("Inactive user")

        # Generate JWT access token
        access_token = cls.create_access_token(user.id)

        return user, access_token

    @classmethod
    async def link_google_account_for_user(
        cls, *, session: Session, user: User, code: str, state: str
    ) -> None:
        """
        Link Google account to an existing authenticated user.

        Args:
            session: Database session
            user: Current authenticated user
            code: Authorization code from Google
            state: CSRF state token

        Raises:
            ValueError: If linking fails
        """
        if user.google_id:
            raise ValueError("Google account already linked")

        # Clean up state
        cls.consume_oauth_state(state)

        # Exchange code for tokens
        token_data = await cls.exchange_google_code(code)

        # Verify and decode ID token
        claims = await cls.verify_and_decode_google_token(token_data["id_token"])
        if not claims:
            raise ValueError("Invalid Google token")

        google_id = claims["sub"]

        # Check if Google ID is already used by another user
        existing_user = cls.get_user_by_google_id(session=session, google_id=google_id)
        if existing_user:
            raise ValueError("This Google account is already linked to another user")

        # Link account
        cls.link_google_account(session=session, user=user, google_id=google_id)

    @classmethod
    def unlink_google_account_for_user(cls, *, session: Session, user: User) -> None:
        """
        Unlink Google account from an authenticated user.

        Args:
            session: Database session
            user: Current authenticated user

        Raises:
            ValueError: If unlinking fails
        """
        if not user.google_id:
            raise ValueError("No Google account linked")

        # Prevent unlinking if no password set (would lock out user)
        if not user.hashed_password:
            raise ValueError(
                "Cannot unlink Google account without setting a password first"
            )

        cls.unlink_google_account(session=session, user=user)
