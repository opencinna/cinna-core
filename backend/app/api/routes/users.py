import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import col, delete, func, select

from app.api.deps import (
    CurrentUser,
    SessionDep,
    get_current_active_superuser,
    require_developer,
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
    UserRolePublic,
    UserRoleUpdate,
    UsersPublic,
    UserSearchResult,
    UsersSearchPublic,
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
    VALID_USER_ROLES,
)
from app.services.users.role_service import RoleService
from app.services.environments.sdk_constants import is_valid_sdk
from app.services.users.mfa_service import MfaService
from app.models.credentials.ai_credential import AICredentialType
from app.services.credentials.ai_credentials_service import ai_credentials_service
from app.utils import generate_new_account_email, send_email

router = APIRouter(prefix="/users", tags=["users"])


def _user_to_public(session, user: User) -> UserPublic:
    """Build :class:`UserPublic` for ``user``, populating the derived
    ``has_*`` flags including the 2FA factor flags.

    Centralised so every endpoint that returns ``UserPublic`` reflects
    enrolment state without each call site duplicating the query.
    """
    return UserPublic(
        **user.model_dump(),
        has_google_account=bool(user.google_id),
        has_password=bool(user.hashed_password),
        has_passkey=MfaService.has_passkey(session=session, user_id=user.id),
        has_totp=MfaService.has_totp(session=session, user_id=user.id),
    )


@router.get(
    "/",
    dependencies=[Depends(get_current_active_superuser)],
    response_model=UsersPublic,
)
def read_users(
    session: SessionDep,
    skip: int = 0,
    limit: int = 100,
    role: str | None = None,
) -> Any:
    """
    Retrieve users. Admin only.

    Optional ``role`` query parameter filters by ``UserRole`` value
    (``agent-user`` | ``agent-developer`` | ``admin``).  Used by the
    Phase 3 admin Roles tab to show counts per role and drive the
    promote / demote UI.
    """
    if role is not None and role not in VALID_USER_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid role filter. Must be one of: {VALID_USER_ROLES}",
        )

    count_statement = select(func.count()).select_from(User)
    statement = select(User)
    if role is not None:
        count_statement = count_statement.where(User.role == role)
        statement = statement.where(User.role == role)

    count = session.exec(count_statement).one()
    statement = statement.offset(skip).limit(limit)
    users = session.exec(statement).all()

    return UsersPublic(
        data=[_user_to_public(session, u) for u in users], count=count
    )


@router.get("/search", response_model=UsersSearchPublic)
def search_users(
    session: SessionDep,
    current_user: CurrentUser,
    q: str,
    limit: int = 10,
) -> Any:
    """
    Search users by email or name for sharing pickers.

    Available to any authenticated user — returns a minimal projection
    (``id``, ``email``, ``full_name``) only, so it does not leak the full
    ``UserPublic`` payload. The current user is excluded from results.
    Requires a query of at least 2 characters; shorter queries return an
    empty list. ``limit`` is clamped to the range 1-25.
    """
    limit = max(1, min(limit, 25))
    term = (q or "").strip()
    if len(term) < 2:
        return UsersSearchPublic(data=[], count=0)

    users = UserService.search_users(
        session=session,
        query=term,
        exclude_user_id=current_user.id,
        limit=limit,
    )
    results = [
        UserSearchResult(id=u.id, email=u.email, full_name=u.full_name)
        for u in users
    ]
    return UsersSearchPublic(data=results, count=len(results))


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
    return _user_to_public(session, user)


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
    return _user_to_public(session, current_user)


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


@router.post(
    "/me/general-assistant",
    response_model=AgentPublic,
    dependencies=[Depends(require_developer)],
)
async def generate_general_assistant(
    *, session: SessionDep, current_user: CurrentUser
) -> Any:
    """
    Create the General Assistant agent for the current user.

    Phase 3 — restricted to ``agent-developer`` and ``admin`` roles.
    The General Assistant creates a real ``Agent`` row, so the same
    role gate applied to ``POST /agents/`` applies here.

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
def read_user_me(session: SessionDep, current_user: CurrentUser) -> Any:
    """
    Get current user.
    """
    return _user_to_public(session, current_user)


@router.get("/me/role", response_model=UserRolePublic)
def read_my_role(current_user: CurrentUser) -> UserRolePublic:
    """Return the current user's role.

    Lightweight endpoint for the Phase 3 frontend shell so that boot
    code can branch the layout (``AgentUserLayout`` vs the existing
    developer layout) without pulling the full ``UserPublic`` payload.
    The role is also included in ``GET /users/me`` for parity.
    """
    return UserRolePublic(role=current_user.role)


@router.patch(
    "/{user_id}/role",
    dependencies=[Depends(get_current_active_superuser)],
    response_model=UserPublic,
)
async def update_user_role(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    user_id: uuid.UUID,
    body: UserRoleUpdate,
) -> Any:
    """Change a user's role between ``agent-user`` and ``agent-developer``.

    Admin (superuser) only.  Cannot promote or demote into ``admin`` —
    that tier is bound to ``is_superuser`` and managed elsewhere.
    Cannot change one's own role.
    """
    target = session.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        target = await RoleService.set_role(
            session=session,
            target_user=target,
            new_role=body.role,
            changed_by=current_user,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return _user_to_public(session, target)


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

    return _user_to_public(session, user)


@router.get("/{user_id}", response_model=UserPublic)
def read_user_by_id(
    user_id: uuid.UUID, session: SessionDep, current_user: CurrentUser
) -> Any:
    """
    Get a specific user by id.
    """
    user = session.get(User, user_id)
    if user == current_user:
        return _user_to_public(session, user)
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=403,
            detail="The user doesn't have enough privileges",
        )
    return _user_to_public(session, user)


@router.patch(
    "/{user_id}",
    dependencies=[Depends(get_current_active_superuser)],
    response_model=UserPublic,
)
async def update_user(
    *,
    session: SessionDep,
    current_user: CurrentUser,
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

    role_provided = "role" in user_in.model_dump(exclude_unset=True)
    if role_provided and user_in.role not in VALID_USER_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid role. Must be one of: {VALID_USER_ROLES}",
        )

    previous_role = db_user.role
    db_user = UserService.update_user(session=session, db_user=db_user, user_in=user_in)

    if role_provided and db_user.role != previous_role:
        await RoleService._emit_role_changed(
            user_id=db_user.id,
            new_role=db_user.role,
            previous_role=previous_role,
            changed_by_user_id=current_user.id,
        )

    return _user_to_public(session, db_user)


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
