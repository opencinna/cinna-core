import uuid
import xmlrpc.client
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import func, select

from app.api.deps import CurrentUser, SessionDep
from app.models import (
    Credential,
    CredentialCreate,
    CredentialPublic,
    CredentialsPublic,
    CredentialUpdate,
    CredentialWithData,
    Message,
)
from app.services.credentials_service import CredentialsService
from app.services.credential_share_service import CredentialShareService
from app import crud


# Request/Response models for credential verification
class OdooVerifyRequest(BaseModel):
    url: str
    database_name: str
    login: str
    api_token: str


class OdooVerifyResponse(BaseModel):
    success: bool
    message: str
    user_id: int | None = None

router = APIRouter(prefix="/credentials", tags=["credentials"])


def _credential_to_public(
    session,
    credential: Credential,
    is_shared: bool = False,
    owner_email: str | None = None
) -> CredentialPublic:
    """Convert a Credential model to CredentialPublic with share_count and status."""
    share_count = 0
    if not is_shared:
        # Only show share_count to owners
        share_count = CredentialShareService.get_share_count_for_credential(
            session=session, credential_id=credential.id
        )

    # Decrypt credential data to check completeness
    credential_data = crud.get_credential_with_data(session=session, credential=credential)
    status = CredentialsService.check_credential_completeness(
        credential_type=credential.type.value,
        credential_data=credential_data
    )

    return CredentialPublic(
        id=credential.id,
        name=credential.name,
        type=credential.type,
        notes=credential.notes,
        allow_sharing=credential.allow_sharing,
        owner_id=credential.owner_id,
        user_workspace_id=credential.user_workspace_id,
        share_count=share_count,
        is_shared=is_shared,
        owner_email=owner_email,
        is_placeholder=credential.is_placeholder,
        placeholder_source_id=credential.placeholder_source_id,
        status=status
    )


@router.get("/", response_model=CredentialsPublic)
def read_credentials(
    session: SessionDep,
    current_user: CurrentUser,
    skip: int = 0,
    limit: int = 100,
    user_workspace_id: str | None = None,
) -> Any:
    """
    Retrieve credentials (without decrypted data).
    - If user_workspace_id is not provided (None): returns all credentials
    - If user_workspace_id is empty string (""): filters for default workspace (NULL)
    - If user_workspace_id is a UUID string: filters for that workspace
    """
    # Parse workspace filter
    workspace_filter: uuid.UUID | None = None
    apply_filter = False

    if user_workspace_id is None:
        # Parameter not provided - return all credentials
        apply_filter = False
    elif user_workspace_id == "":
        # Empty string means default workspace (NULL in database)
        workspace_filter = None
        apply_filter = True
    else:
        # Parse as UUID
        try:
            workspace_filter = uuid.UUID(user_workspace_id)
            apply_filter = True
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid workspace ID format")

    # Credentials are always private - only return credentials owned by current user
    count_statement = (
        select(func.count())
        .select_from(Credential)
        .where(Credential.owner_id == current_user.id)
    )
    statement = (
        select(Credential)
        .where(Credential.owner_id == current_user.id)
    )

    if apply_filter:
        count_statement = count_statement.where(Credential.user_workspace_id == workspace_filter)
        statement = statement.where(Credential.user_workspace_id == workspace_filter)

    count = session.exec(count_statement).one()
    credentials = session.exec(statement.offset(skip).limit(limit)).all()

    # Convert to public models with share_count
    credentials_public = [_credential_to_public(session, c) for c in credentials]

    return CredentialsPublic(data=credentials_public, count=count)


@router.get("/{id}", response_model=CredentialPublic)
def read_credential(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Any:
    """
    Get credential by ID (without decrypted data).

    Returns credential if user owns it OR has it shared with them.
    For shared credentials, is_shared=True and owner_email is set.
    """
    from app.models.user import User

    credential = session.get(Credential, id)
    if not credential:
        raise HTTPException(status_code=404, detail="Credential not found")

    # Check if user owns the credential
    if credential.owner_id == current_user.id:
        return _credential_to_public(session, credential, is_shared=False)

    # Check if credential is shared with user
    if CredentialShareService.can_user_access_credential(session, id, current_user.id):
        owner = session.get(User, credential.owner_id)
        owner_email = owner.email if owner else None
        return _credential_to_public(session, credential, is_shared=True, owner_email=owner_email)

    raise HTTPException(status_code=400, detail="Not enough permissions")


@router.get("/{id}/with-data", response_model=CredentialWithData)
def read_credential_with_data(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Any:
    """
    Get credential by ID with decrypted data.
    """
    try:
        credential_data_dict = CredentialsService.get_credential_with_data(
            session=session,
            credential_id=id,
            owner_id=current_user.id,
            is_superuser=current_user.is_superuser
        )
        # Add share_count
        share_count = CredentialShareService.get_share_count_for_credential(
            session=session, credential_id=id
        )
        credential_data_dict["share_count"] = share_count
        return CredentialWithData(**credential_data_dict)
    except ValueError as e:
        # Service raises ValueError for not found or permission errors
        status_code = 404 if "not found" in str(e).lower() else 400
        raise HTTPException(status_code=status_code, detail=str(e))


@router.post("/", response_model=CredentialPublic)
def create_credential(
    *, session: SessionDep, current_user: CurrentUser, credential_in: CredentialCreate
) -> Any:
    """
    Create new credential.
    """
    credential = CredentialsService.create_credential(
        session=session,
        credential_in=credential_in,
        owner_id=current_user.id
    )
    return _credential_to_public(session, credential)


@router.put("/{id}", response_model=CredentialPublic)
async def update_credential(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    credential_in: CredentialUpdate,
) -> Any:
    """
    Update a credential.

    This will trigger automatic sync to all running environments of agents
    that have this credential linked.
    """
    try:
        credential = await CredentialsService.update_credential(
            session=session,
            credential_id=id,
            credential_in=credential_in,
            owner_id=current_user.id,
            is_superuser=current_user.is_superuser
        )
        return _credential_to_public(session, credential)
    except ValueError as e:
        # Service raises ValueError for not found or permission errors
        status_code = 404 if "not found" in str(e).lower() else 400
        raise HTTPException(status_code=status_code, detail=str(e))


@router.delete("/{id}")
async def delete_credential(
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID
) -> Message:
    """
    Delete a credential.

    This will trigger automatic sync to all running environments of agents
    that had this credential linked.
    """
    try:
        await CredentialsService.delete_credential(
            session=session,
            credential_id=id,
            owner_id=current_user.id,
            is_superuser=current_user.is_superuser
        )
        return Message(message="Credential deleted successfully")
    except ValueError as e:
        # Service raises ValueError for not found or permission errors
        status_code = 404 if "not found" in str(e).lower() else 400
        raise HTTPException(status_code=status_code, detail=str(e))


@router.post("/verify/odoo", response_model=OdooVerifyResponse)
def verify_odoo_credential(
    current_user: CurrentUser,
    verify_data: OdooVerifyRequest,
) -> Any:
    """
    Verify Odoo credentials by attempting to authenticate.

    Makes an XML-RPC call to the Odoo server to verify the credentials are valid.
    Returns the user ID if authentication is successful.
    """
    try:
        # Normalize URL (remove trailing slash)
        url = verify_data.url.rstrip("/")

        # Connect to Odoo's common endpoint for authentication
        common = xmlrpc.client.ServerProxy(
            f"{url}/xmlrpc/2/common",
            allow_none=True
        )

        # Attempt authentication
        uid = common.authenticate(
            verify_data.database_name,
            verify_data.login,
            verify_data.api_token,
            {}
        )

        if uid:
            return OdooVerifyResponse(
                success=True,
                message="Authentication successful",
                user_id=uid
            )
        else:
            return OdooVerifyResponse(
                success=False,
                message="Authentication failed: Invalid credentials or database"
            )

    except xmlrpc.client.Fault as e:
        return OdooVerifyResponse(
            success=False,
            message=f"Odoo error: {e.faultString}"
        )
    except ConnectionRefusedError:
        return OdooVerifyResponse(
            success=False,
            message="Connection refused: Unable to connect to the Odoo server"
        )
    except OSError as e:
        return OdooVerifyResponse(
            success=False,
            message=f"Connection error: {str(e)}"
        )
    except Exception as e:
        return OdooVerifyResponse(
            success=False,
            message=f"Verification failed: {str(e)}"
        )
