import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import col, delete, func, select

from app.api.deps import (
    CurrentUser,
    SessionDep,
    get_current_active_superuser,
)
from app.core.config import settings
from app.services.users.user_service import UserService
from app.models import (
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
from app.models.agents.agent import AgentPublic
from app.models.users.user import (
    AIServiceCredentials,
    AIServiceCredentialsUpdate,
    UserPublicWithAICredentials,
    VALID_SDK_OPTIONS,
    VALID_AI_FUNCTIONS_SDK_OPTIONS,
)
from app.services.environments.sdk_constants import is_valid_sdk
from app.models.credentials.ai_credential import AICredentialType
from app.services.credentials.ai_credentials_service import ai_credentials_service
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
    user = UserService.get_user_by_email(session=session, email=user_in.email)
    if user:
        raise HTTPException(
            status_code=400,
            detail="The user with this email already exists in the system.",
        )

    user = UserService.create_user(session=session, user_create=user_in)
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
        existing_user = UserService.get_user_by_email(session=session, email=user_in.email)
        if existing_user and existing_user.id != current_user.id:
            raise HTTPException(
                status_code=409, detail="User with this email already exists"
            )
    # Validate SDK values if provided
    if user_in.default_sdk_conversation and not is_valid_sdk(user_in.default_sdk_conversation):
        raise HTTPException(
            status_code=400,
            detail="Invalid SDK for conversation mode",
        )
    if user_in.default_sdk_building and not is_valid_sdk(user_in.default_sdk_building):
        raise HTTPException(
            status_code=400,
            detail="Invalid SDK for building mode",
        )
    if user_in.default_ai_functions_sdk and user_in.default_ai_functions_sdk not in VALID_AI_FUNCTIONS_SDK_OPTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid AI functions SDK. Must be one of: {VALID_AI_FUNCTIONS_SDK_OPTIONS}",
        )
    # When switching to "system", clear the credential_id
    if user_in.default_ai_functions_sdk and not user_in.default_ai_functions_sdk.startswith("personal:"):
        user_in.default_ai_functions_credential_id = None
    # Validate AI functions credential_id if provided
    if user_in.default_ai_functions_credential_id is not None:
        from app.models.credentials.ai_credential import AICredential, AICredentialType
        cred = session.get(AICredential, user_in.default_ai_functions_credential_id)
        if not cred or cred.owner_id != current_user.id:
            raise HTTPException(status_code=404, detail="AI credential not found")
        # Determine expected credential type based on the SDK being set in this request
        # or the user's current saved preference if SDK is not being changed here
        sdk = user_in.default_ai_functions_sdk or current_user.default_ai_functions_sdk
        if sdk == "personal:openai":
            expected_type = AICredentialType.OPENAI
        else:
            expected_type = AICredentialType.ANTHROPIC
        if cred.type != expected_type:
            raise HTTPException(
                status_code=400,
                detail=f"Only {expected_type.value} credentials can be used for AI functions when using {sdk}",
            )
        # OAuth token check — only applicable for Anthropic credentials
        if expected_type == AICredentialType.ANTHROPIC:
            from app.services.credentials.ai_credentials_service import ai_credentials_service
            data = ai_credentials_service.decrypt_credential(cred)
            if data.api_key and data.api_key.startswith("sk-ant-oat"):
                raise HTTPException(
                    status_code=400,
                    detail="OAuth tokens cannot be used with the Anthropic API for AI functions. "
                           "Please select a credential with an API key (sk-ant-api*).",
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
    try:
        UserService.update_password(
            session=session,
            user=current_user,
            current_password=body.current_password,
            new_password=body.new_password,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return Message(message="Password updated successfully")


@router.post("/me/set-password", response_model=Message)
def set_password_me(
    *, session: SessionDep, body: SetPassword, current_user: CurrentUser
) -> Any:
    """
    Set password for user (for OAuth users who don't have one).
    """
    try:
        UserService.set_password(
            session=session, user=current_user, new_password=body.new_password
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return Message(message="Password set successfully")


@router.post("/me/general-assistant", response_model=AgentPublic)
async def generate_general_assistant(
    *, session: SessionDep, current_user: CurrentUser
) -> Any:
    """
    Create the General Assistant agent for the current user.

    Returns HTTP 409 if a General Assistant already exists for this user.
    Returns HTTP 400 if the General Assistant feature is not enabled on the account.
    """
    from app.services.users.general_assistant_service import GeneralAssistantService

    try:
        agent = await GeneralAssistantService.ensure_or_create(session, current_user)
    except GeneralAssistantService.NotEnabledError:
        raise HTTPException(status_code=400, detail="Enable General Assistant in your profile first")
    except GeneralAssistantService.AlreadyExistsError:
        raise HTTPException(status_code=409, detail="General Assistant already exists")

    from app.services.agents.agent_service import AgentService
    return AgentService.to_public_with_clone_info(session, agent)


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
    try:
        user = UserService.register_user(
            session=session,
            email=user_in.email,
            password=user_in.password,
            full_name=user_in.full_name,
        )
    except ValueError as e:
        detail = str(e)
        if "restricted" in detail:
            raise HTTPException(status_code=403, detail=detail)
        raise HTTPException(status_code=400, detail=detail)

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
        existing_user = UserService.get_user_by_email(session=session, email=user_in.email)
        if existing_user and existing_user.id != user_id:
            raise HTTPException(
                status_code=409, detail="User with this email already exists"
            )

    db_user = UserService.update_user(session=session, db_user=db_user, user_in=user_in)
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
    session.delete(user)
    session.commit()
    return Message(message="User deleted successfully")


# AI Service Credentials endpoints
@router.get("/me/ai-credentials/status", response_model=UserPublicWithAICredentials)
def get_ai_credentials_status(
    session: SessionDep,
    current_user: CurrentUser,
) -> UserPublicWithAICredentials:
    """
    Get AI credentials status (which keys are set, without revealing the keys).
    Checks the ai_credential table for default credentials of each type.
    """
    # Check for default credentials in ai_credential table
    anthropic_default = ai_credentials_service.get_default_for_type(
        session, current_user.id, AICredentialType.ANTHROPIC
    )
    minimax_default = ai_credentials_service.get_default_for_type(
        session, current_user.id, AICredentialType.MINIMAX
    )
    openai_compat_default = ai_credentials_service.get_default_for_type(
        session, current_user.id, AICredentialType.OPENAI_COMPATIBLE
    )
    openai_default = ai_credentials_service.get_default_for_type(
        session, current_user.id, AICredentialType.OPENAI
    )
    google_default = ai_credentials_service.get_default_for_type(
        session, current_user.id, AICredentialType.GOOGLE
    )

    return UserPublicWithAICredentials(
        **current_user.model_dump(),
        has_google_account=bool(current_user.google_id),
        has_password=bool(current_user.hashed_password),
        has_anthropic_api_key=anthropic_default is not None,
        has_openai_api_key=openai_default is not None,
        has_google_ai_api_key=google_default is not None,
        has_minimax_api_key=minimax_default is not None,
        has_openai_compatible_api_key=openai_compat_default is not None,
    )


@router.get("/me/ai-credentials", response_model=AIServiceCredentials)
def get_ai_credentials(
    current_user: CurrentUser,
) -> AIServiceCredentials:
    """
    Get decrypted AI service credentials.
    SECURITY: Only returns to the credential owner.
    """
    credentials = ai_credentials_service.get_user_ai_credentials(user=current_user)
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
    Creates AICredential records and sets them as defaults.
    Also syncs to user profile for backward compatibility.
    """
    ai_credentials_service.upsert_onboarding_credentials(
        session, current_user, credentials_in,
    )
    return Message(message="AI credentials updated successfully")


@router.delete("/me/ai-credentials", response_model=Message)
def delete_ai_credentials(
    *,
    session: SessionDep,
    current_user: CurrentUser,
) -> Message:
    """Delete all AI service credentials"""
    ai_credentials_service.delete_user_ai_credentials(session=session, user=current_user)
    return Message(message="AI credentials deleted successfully")
