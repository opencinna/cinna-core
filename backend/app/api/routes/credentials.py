import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
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

router = APIRouter(prefix="/credentials", tags=["credentials"])


@router.get("/", response_model=CredentialsPublic)
def read_credentials(
    session: SessionDep, current_user: CurrentUser, skip: int = 0, limit: int = 100
) -> Any:
    """
    Retrieve credentials (without decrypted data).
    """
    if current_user.is_superuser:
        count_statement = select(func.count()).select_from(Credential)
        count = session.exec(count_statement).one()
        statement = select(Credential).offset(skip).limit(limit)
        credentials = session.exec(statement).all()
    else:
        count_statement = (
            select(func.count())
            .select_from(Credential)
            .where(Credential.owner_id == current_user.id)
        )
        count = session.exec(count_statement).one()
        statement = (
            select(Credential)
            .where(Credential.owner_id == current_user.id)
            .offset(skip)
            .limit(limit)
        )
        credentials = session.exec(statement).all()

    return CredentialsPublic(data=credentials, count=count)


@router.get("/{id}", response_model=CredentialPublic)
def read_credential(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Any:
    """
    Get credential by ID (without decrypted data).
    """
    credential = session.get(Credential, id)
    if not credential:
        raise HTTPException(status_code=404, detail="Credential not found")
    if not current_user.is_superuser and (credential.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")
    return credential


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
    return credential


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
        return credential
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
