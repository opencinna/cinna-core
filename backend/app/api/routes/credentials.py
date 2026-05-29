import uuid
import xmlrpc.client
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import func, select

from app.api.deps import CurrentUser, SessionDep
from app.models.credentials.credential import CredentialType
from app.models import (
    AgentApiConnectionInfo,
    Credential,
    CredentialBundleUsages,
    CredentialDeletionImpact,
    CredentialCreate,
    CredentialPublic,
    CredentialsPublic,
    CredentialUpdate,
    CredentialWithData,
    Message,
    UserWorkspace,
)
from app.services.credentials.credentials_service import (
    CredentialsService,
    CredentialInUseError,
)
from app.services.credentials.credential_share_service import CredentialShareService
from app.services.agent_api.agent_api_token_service import (
    AgentApiTokenError,
    AgentApiTokenService,
)


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
    credential_data = CredentialsService.decrypt_credential_data(session=session, credential=credential)
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
        allow_template_sharing=credential.allow_template_sharing,
        template_private_fields=list(credential.template_private_fields or []),
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
    from app.models.users.user import User

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


@router.get("/{id}/agent-api-connection", response_model=AgentApiConnectionInfo)
def read_agent_api_connection(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Any:
    """
    Connection details for an ``agent_api`` credential: the producer agent it
    proxies, the consumer agents it is linked to, and the spec/base URLs.
    Drives the credential's detail view (View Spec + connected agents).
    """
    try:
        return AgentApiTokenService.get_connection_info(
            session=session,
            credential_id=id,
            user_id=current_user.id,
            is_superuser=current_user.is_superuser,
        )
    except AgentApiTokenError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)


@router.post("/", response_model=CredentialPublic)
def create_credential(
    *, session: SessionDep, current_user: CurrentUser, credential_in: CredentialCreate
) -> Any:
    """
    Create new credential.
    """
    # Validate workspace ownership if workspace_id is provided
    if credential_in.user_workspace_id is not None:
        workspace = session.get(UserWorkspace, credential_in.user_workspace_id)
        if not workspace:
            raise HTTPException(status_code=400, detail="Workspace not found")
        if workspace.user_id != current_user.id:
            raise HTTPException(status_code=403, detail="Not allowed to use this workspace")

    # Validate service account JSON on create
    if credential_in.type == CredentialType.GOOGLE_SERVICE_ACCOUNT and credential_in.credential_data:
        try:
            CredentialsService.validate_service_account_json(credential_in.credential_data)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))

    # SSH key credentials: generate or validate the pair server-side before persist.
    # On success, credential_data is replaced with the normalised blob so the
    # standard create path simply Fernet-encrypts and stores.
    if credential_in.type == CredentialType.SSH_KEY:
        try:
            credential_in.credential_data = CredentialsService.process_ssh_key_credential_input(
                credential_in.credential_data or {},
                credential_name=credential_in.name,
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))

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
        # Type-specific validation on update (only if credential_data is supplied).
        # Each branch normalises `credential_in.credential_data` in place; the
        # standard update path then Fernet-encrypts and persists.
        if credential_in.credential_data:
            credential = session.get(Credential, id)
            if credential and credential.type == CredentialType.GOOGLE_SERVICE_ACCOUNT:
                try:
                    CredentialsService.validate_service_account_json(credential_in.credential_data)
                except ValueError as e:
                    raise HTTPException(status_code=422, detail=str(e))
            elif credential and credential.type == CredentialType.SSH_KEY:
                # Delegate to the service — handles both key rotation (mode
                # present) and metadata-only updates (host_aliases).
                try:
                    credential_in.credential_data = CredentialsService.prepare_ssh_key_update_data(
                        session=session,
                        credential=credential,
                        raw_data=credential_in.credential_data,
                        credential_name=credential_in.name,
                    )
                except ValueError as e:
                    raise HTTPException(status_code=422, detail=str(e))

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


@router.get("/{id}/deletion-impact", response_model=CredentialDeletionImpact)
def get_credential_deletion_impact(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Any:
    """Classify the blast radius of deleting this credential.

    Returns a graduated impact tier (0 self-only, 1 direct shares, 2 PBP in a
    published bundle with active foreign installs) plus the supporting detail
    (own agents, share count, affected bundles, active install count). The
    frontend renders tier-specific delete confirmation UI; Tier 2 blocks the
    normal delete and offers a force escape hatch.

    Returns 404 when the credential does not exist or the requester does not
    own it.
    """
    try:
        return CredentialsService.get_deletion_impact(
            session=session, credential_id=id, requester_id=current_user.id
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/{id}")
async def delete_credential(
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    force: bool = False,
) -> Message:
    """
    Delete a credential.

    This will trigger automatic sync to all running environments of agents
    that had this credential linked.

    Blocked with HTTP 409 when the credential is publisher-provided in a
    published bundle with active foreign installs (Tier 2), unless ``force`` is
    passed — in which case the owner explicitly accepts breaking those installs.
    """
    try:
        await CredentialsService.delete_credential(
            session=session,
            credential_id=id,
            owner_id=current_user.id,
            is_superuser=current_user.is_superuser,
            force=force,
        )
        return Message(message="Credential deleted successfully")
    except CredentialInUseError as e:
        # Tier 2 block: surface the structured impact so the frontend can
        # render affected bundles + install counts and offer force delete.
        raise HTTPException(
            status_code=409, detail=e.impact.model_dump(mode="json")
        )
    except ValueError as e:
        # Service raises ValueError for not found or permission errors
        status_code = 404 if "not found" in str(e).lower() else 400
        raise HTTPException(status_code=status_code, detail=str(e))


@router.get("/{id}/bundles", response_model=CredentialBundleUsages)
def list_credential_bundle_usages(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Any:
    """List bundles whose publisher install has this credential linked.

    Surfaced on the credential detail page so the owner can see at a
    glance which of their bundles ship this credential (PBP / PBT / PBU
    spec — all three modes start by linking the credential to the
    publisher install). Each entry exposes the publisher install id so
    the frontend can deep-link into the agent's Bundle tab.

    Returns 404 when the credential does not exist or the requester does
    not own it.
    """
    try:
        usages = CredentialsService.list_bundle_usages(
            session=session, credential_id=id, requester_id=current_user.id
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return CredentialBundleUsages(data=usages, count=len(usages))


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
