"""
User Service - Business logic for user management operations.
"""
import secrets
from typing import Any

from sqlmodel import Session, select

from app.core.security import get_password_hash, verify_password
from app.models import User, UserCreate, UserUpdate
from app.services.users.auth_service import AuthService
from app.utils import (
    generate_password_reset_token,
    generate_reset_password_email,
    send_email,
    verify_password_reset_token,
)


class UserService:
    """
    Service for user CRUD and password management operations.

    Raises ValueError on domain/business rule failures.
    Routes translate ValueError to HTTPException.
    """

    @staticmethod
    def create_user(*, session: Session, user_create: UserCreate) -> User:
        """Create a new user with hashed password."""
        db_obj = User.model_validate(
            user_create,
            update={"hashed_password": get_password_hash(user_create.password)},
        )
        session.add(db_obj)
        session.commit()
        session.refresh(db_obj)
        return db_obj

    @staticmethod
    def update_user(*, session: Session, db_user: User, user_in: UserUpdate) -> Any:
        """Update an existing user, hashing password if provided."""
        user_data = user_in.model_dump(exclude_unset=True)
        extra_data = {}
        if "password" in user_data:
            password = user_data["password"]
            hashed_password = get_password_hash(password)
            extra_data["hashed_password"] = hashed_password
        db_user.sqlmodel_update(user_data, update=extra_data)
        session.add(db_user)
        session.commit()
        session.refresh(db_user)
        return db_user

    @staticmethod
    def get_user_by_email(*, session: Session, email: str) -> User | None:
        """Look up a user by email address."""
        statement = select(User).where(User.email == email)
        return session.exec(statement).first()

    @staticmethod
    def authenticate(*, session: Session, email: str, password: str) -> User | None:
        """Authenticate a user by email and password. Returns None on failure."""
        db_user = UserService.get_user_by_email(session=session, email=email)
        if not db_user:
            return None
        if not db_user.hashed_password:
            return None
        if not verify_password(password, db_user.hashed_password):
            return None
        return db_user

    @staticmethod
    def register_user(
        *, session: Session, email: str, password: str, full_name: str | None = None
    ) -> User:
        """
        Register a new user with domain whitelist and duplicate checks.

        Raises:
            ValueError: If domain not allowed or email already exists.
        """
        if not AuthService.is_email_domain_allowed(email):
            raise ValueError("Registration is restricted to specific email domains")

        existing = UserService.get_user_by_email(session=session, email=email)
        if existing:
            raise ValueError(
                "The user with this email already exists in the system"
            )

        user_create = UserCreate(email=email, password=password, full_name=full_name)
        return UserService.create_user(session=session, user_create=user_create)

    @staticmethod
    def create_email_user(*, session: Session, email: str) -> User:
        """
        Create or return a user from email only (for email integration).

        - Generates a random password (user doesn't receive it)
        - Sets is_active=True
        - Does NOT enforce AUTH_WHITELIST_DOMAINS
        - Returns existing user if email already exists
        """
        existing = UserService.get_user_by_email(session=session, email=email)
        if existing:
            return existing

        random_password = secrets.token_urlsafe(32)
        user_create = UserCreate(
            email=email,
            password=random_password,
            is_active=True,
        )
        return UserService.create_user(session=session, user_create=user_create)

    @staticmethod
    def update_password(
        *, session: Session, user: User, current_password: str, new_password: str
    ) -> None:
        """
        Update password for a user who already has one set.

        Raises:
            ValueError: If no password set, current password wrong, or same password.
        """
        if not user.hashed_password:
            raise ValueError("No password set. Use set-password endpoint first.")
        if not verify_password(current_password, user.hashed_password):
            raise ValueError("Incorrect password")
        if current_password == new_password:
            raise ValueError(
                "New password cannot be the same as the current one"
            )
        user.hashed_password = get_password_hash(new_password)
        session.add(user)
        session.commit()

    @staticmethod
    def set_password(*, session: Session, user: User, new_password: str) -> None:
        """
        Set password for an OAuth user who doesn't have one yet.

        Raises:
            ValueError: If password already set.
        """
        if user.hashed_password:
            raise ValueError(
                "Password already set. Use update password endpoint instead."
            )
        user.hashed_password = get_password_hash(new_password)
        session.add(user)
        session.commit()

    @staticmethod
    def reset_password(*, session: Session, token: str, new_password: str) -> None:
        """
        Reset password using a password-reset token.

        Raises:
            ValueError: If token invalid, user not found, or user inactive.
        """
        email = verify_password_reset_token(token=token)
        if not email:
            raise ValueError("Invalid token")
        user = UserService.get_user_by_email(session=session, email=email)
        if not user:
            raise ValueError(
                "The user with this email does not exist in the system."
            )
        if not user.is_active:
            raise ValueError("Inactive user")
        user.hashed_password = get_password_hash(password=new_password)
        session.add(user)
        session.commit()

    @staticmethod
    def recover_password(*, session: Session, email: str) -> None:
        """
        Send a password recovery email.

        Raises:
            ValueError: If user not found.
        """
        user = UserService.get_user_by_email(session=session, email=email)
        if not user:
            raise ValueError(
                "The user with this email does not exist in the system."
            )
        password_reset_token = generate_password_reset_token(email=email)
        email_data = generate_reset_password_email(
            email_to=user.email, email=email, token=password_reset_token
        )
        send_email(
            email_to=user.email,
            subject=email_data.subject,
            html_content=email_data.html_content,
        )
