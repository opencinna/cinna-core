import uuid
from typing import Any

from fastapi import APIRouter, HTTPException
from sqlmodel import func, select

from app.api.deps import CurrentUser, SessionDep
from app import crud
from app.models import (
    Credential,
    CredentialCreate,
    CredentialPublic,
    CredentialsPublic,
    CredentialUpdate,
    CredentialWithData,
    Message,
)

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
    credential = session.get(Credential, id)
    if not credential:
        raise HTTPException(status_code=404, detail="Credential not found")
    if not current_user.is_superuser and (credential.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    # Decrypt the credential data
    credential_data = crud.get_credential_with_data(session=session, credential=credential)

    # Return credential with decrypted data
    return CredentialWithData(
        id=credential.id,
        name=credential.name,
        type=credential.type,
        notes=credential.notes,
        owner_id=credential.owner_id,
        credential_data=credential_data,
    )


@router.post("/", response_model=CredentialPublic)
def create_credential(
    *, session: SessionDep, current_user: CurrentUser, credential_in: CredentialCreate
) -> Any:
    """
    Create new credential.
    """
    credential = crud.create_credential(
        session=session, credential_in=credential_in, owner_id=current_user.id
    )
    return credential


@router.put("/{id}", response_model=CredentialPublic)
def update_credential(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    credential_in: CredentialUpdate,
) -> Any:
    """
    Update a credential.
    """
    credential = session.get(Credential, id)
    if not credential:
        raise HTTPException(status_code=404, detail="Credential not found")
    if not current_user.is_superuser and (credential.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")

    credential = crud.update_credential(
        session=session, db_credential=credential, credential_in=credential_in
    )
    return credential


@router.delete("/{id}")
def delete_credential(
    session: SessionDep, current_user: CurrentUser, id: uuid.UUID
) -> Message:
    """
    Delete a credential.
    """
    credential = session.get(Credential, id)
    if not credential:
        raise HTTPException(status_code=404, detail="Credential not found")
    if not current_user.is_superuser and (credential.owner_id != current_user.id):
        raise HTTPException(status_code=400, detail="Not enough permissions")
    session.delete(credential)
    session.commit()
    return Message(message="Credential deleted successfully")
