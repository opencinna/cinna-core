import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import col, delete, func, select

from app import crud
from app.api.deps import (
    CurrentUser,
    SessionDep,
    get_current_active_superuser,
)
from app.core.config import settings
from app.core.security import get_password_hash, verify_password
from app.models import (
    Item,
    Message,
    SetPassword,
    UpdatePassword,
    User,
    UserCreate,
    UserPublic,
    UserRegister,
    UsersPublic,
    UserUpdate,
    UserUpdateMe,
)
from app.models.user import (
    AIServiceCredentials,
    AIServiceCredentialsUpdate,
    UserPublicWithAICredentials,
    VALID_SDK_OPTIONS,
)
from app.services.auth_service import AuthService
from app.utils import generate_new_account_email, send_email

router = APIRouter(prefix="/users", tags=["users"])


@router.get(
    "/",
    dependencies=[Depends(get_current_active_superuser)],
    response_model=UsersPublic,
)
def read_users(session: SessionDep, skip: int = 0, limit: int = 100) -> Any:
    """
    Retrieve users.
    """

    count_statement = select(func.count()).select_from(User)
    count = session.exec(count_statement).one()

    statement = select(User).offset(skip).limit(limit)
    users = session.exec(statement).all()

    return UsersPublic(data=users, count=count)


@router.post(
    "/", dependencies=[Depends(get_current_active_superuser)], response_model=UserPublic
)
def create_user(*, session: SessionDep, user_in: UserCreate) -> Any:
    """
    Create new user.
    """
    user = crud.get_user_by_email(session=session, email=user_in.email)
    if user:
        raise HTTPException(
            status_code=400,
            detail="The user with this email already exists in the system.",
        )

    user = crud.create_user(session=session, user_create=user_in)
    if settings.emails_enabled and user_in.email:
        email_data = generate_new_account_email(
            email_to=user_in.email, username=user_in.email, password=user_in.password
        )
        send_email(
            email_to=user_in.email,
            subject=email_data.subject,
            html_content=email_data.html_content,
        )
    return user


@router.patch("/me", response_model=UserPublic)
def update_user_me(
    *, session: SessionDep, user_in: UserUpdateMe, current_user: CurrentUser
) -> Any:
    """
    Update own user.
    """

    if user_in.email:
        # Block email change if not allowed (domain whitelist is active)
        if not settings.allow_user_email_change:
            raise HTTPException(
                status_code=403,
                detail="Email changes are not allowed",
            )
        existing_user = crud.get_user_by_email(session=session, email=user_in.email)
        if existing_user and existing_user.id != current_user.id:
            raise HTTPException(
                status_code=409, detail="User with this email already exists"
            )
    # Validate SDK values if provided
    if user_in.default_sdk_conversation and user_in.default_sdk_conversation not in VALID_SDK_OPTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid SDK for conversation mode. Must be one of: {VALID_SDK_OPTIONS}",
        )
    if user_in.default_sdk_building and user_in.default_sdk_building not in VALID_SDK_OPTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid SDK for building mode. Must be one of: {VALID_SDK_OPTIONS}",
        )
    user_data = user_in.model_dump(exclude_unset=True)
    current_user.sqlmodel_update(user_data)
    session.add(current_user)
    session.commit()
    session.refresh(current_user)
    return current_user


@router.patch("/me/password", response_model=Message)
def update_password_me(
    *, session: SessionDep, body: UpdatePassword, current_user: CurrentUser
) -> Any:
    """
    Update own password.
    """
    if not current_user.hashed_password:
        raise HTTPException(
            status_code=400,
            detail="No password set. Use set-password endpoint first.",
        )
    if not verify_password(body.current_password, current_user.hashed_password):
        raise HTTPException(status_code=400, detail="Incorrect password")
    if body.current_password == body.new_password:
        raise HTTPException(
            status_code=400, detail="New password cannot be the same as the current one"
        )
    hashed_password = get_password_hash(body.new_password)
    current_user.hashed_password = hashed_password
    session.add(current_user)
    session.commit()
    return Message(message="Password updated successfully")


@router.post("/me/set-password", response_model=Message)
def set_password_me(
    *, session: SessionDep, body: SetPassword, current_user: CurrentUser
) -> Any:
    """
    Set password for user (for OAuth users who don't have one).
    """
    if current_user.hashed_password:
        raise HTTPException(
            status_code=400,
            detail="Password already set. Use update password endpoint instead.",
        )

    hashed_password = get_password_hash(body.new_password)
    current_user.hashed_password = hashed_password
    session.add(current_user)
    session.commit()
    return Message(message="Password set successfully")


@router.get("/me", response_model=UserPublic)
def read_user_me(current_user: CurrentUser) -> Any:
    """
    Get current user.
    """
    return UserPublic(
        **current_user.model_dump(),
        has_google_account=bool(current_user.google_id),
        has_password=bool(current_user.hashed_password),
    )


@router.delete("/me", response_model=Message)
def delete_user_me(session: SessionDep, current_user: CurrentUser) -> Any:
    """
    Delete own user.
    """
    if current_user.is_superuser:
        raise HTTPException(
            status_code=403, detail="Super users are not allowed to delete themselves"
        )
    session.delete(current_user)
    session.commit()
    return Message(message="User deleted successfully")


@router.post("/signup", response_model=UserPublic)
def register_user(session: SessionDep, user_in: UserRegister) -> Any:
    """
    Create new user without the need to be logged in.
    """
    # Check domain whitelist for new user registration
    if not AuthService.is_email_domain_allowed(user_in.email):
        raise HTTPException(
            status_code=403,
            detail="Registration is restricted to specific email domains",
        )

    user = crud.get_user_by_email(session=session, email=user_in.email)
    if user:
        raise HTTPException(
            status_code=400,
            detail="The user with this email already exists in the system",
        )
    user_create = UserCreate.model_validate(user_in)
    user = crud.create_user(session=session, user_create=user_create)
    return user


@router.get("/{user_id}", response_model=UserPublic)
def read_user_by_id(
    user_id: uuid.UUID, session: SessionDep, current_user: CurrentUser
) -> Any:
    """
    Get a specific user by id.
    """
    user = session.get(User, user_id)
    if user == current_user:
        return user
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=403,
            detail="The user doesn't have enough privileges",
        )
    return user


@router.patch(
    "/{user_id}",
    dependencies=[Depends(get_current_active_superuser)],
    response_model=UserPublic,
)
def update_user(
    *,
    session: SessionDep,
    user_id: uuid.UUID,
    user_in: UserUpdate,
) -> Any:
    """
    Update a user.
    """

    db_user = session.get(User, user_id)
    if not db_user:
        raise HTTPException(
            status_code=404,
            detail="The user with this id does not exist in the system",
        )
    if user_in.email:
        existing_user = crud.get_user_by_email(session=session, email=user_in.email)
        if existing_user and existing_user.id != user_id:
            raise HTTPException(
                status_code=409, detail="User with this email already exists"
            )

    db_user = crud.update_user(session=session, db_user=db_user, user_in=user_in)
    return db_user


@router.delete("/{user_id}", dependencies=[Depends(get_current_active_superuser)])
def delete_user(
    session: SessionDep, current_user: CurrentUser, user_id: uuid.UUID
) -> Message:
    """
    Delete a user.
    """
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user == current_user:
        raise HTTPException(
            status_code=403, detail="Super users are not allowed to delete themselves"
        )
    statement = delete(Item).where(col(Item.owner_id) == user_id)
    session.exec(statement)  # type: ignore
    session.delete(user)
    session.commit()
    return Message(message="User deleted successfully")


# AI Service Credentials endpoints
@router.get("/me/ai-credentials/status", response_model=UserPublicWithAICredentials)
def get_ai_credentials_status(
    current_user: CurrentUser,
) -> UserPublicWithAICredentials:
    """
    Get AI credentials status (which keys are set, without revealing the keys).
    """
    credentials = crud.get_user_ai_credentials(user=current_user)

    return UserPublicWithAICredentials(
        **current_user.model_dump(),
        has_google_account=bool(current_user.google_id),
        has_password=bool(current_user.hashed_password),
        has_anthropic_api_key=bool(credentials and credentials.anthropic_api_key),
        has_openai_api_key=bool(credentials and credentials.openai_api_key),
        has_google_ai_api_key=bool(credentials and credentials.google_ai_api_key),
        has_minimax_api_key=bool(credentials and credentials.minimax_api_key),
    )


@router.get("/me/ai-credentials", response_model=AIServiceCredentials)
def get_ai_credentials(
    current_user: CurrentUser,
) -> AIServiceCredentials:
    """
    Get decrypted AI service credentials.
    SECURITY: Only returns to the credential owner.
    """
    credentials = crud.get_user_ai_credentials(user=current_user)
    if not credentials:
        return AIServiceCredentials()
    return credentials


@router.patch("/me/ai-credentials", response_model=Message)
def update_ai_credentials(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    credentials_in: AIServiceCredentialsUpdate,
) -> Message:
    """
    Update AI service credentials (partial update).
    Only updates the fields provided.
    """
    crud.update_user_ai_credentials(
        session=session, user=current_user, credentials_update=credentials_in
    )
    return Message(message="AI credentials updated successfully")


@router.delete("/me/ai-credentials", response_model=Message)
def delete_ai_credentials(
    *,
    session: SessionDep,
    current_user: CurrentUser,
) -> Message:
    """Delete all AI service credentials"""
    crud.delete_user_ai_credentials(session=session, user=current_user)
    return Message(message="AI credentials deleted successfully")
