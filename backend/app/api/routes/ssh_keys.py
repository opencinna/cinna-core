import uuid
from typing import Any

from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUser, SessionDep
from app.models import (
    SSHKeyGenerate,
    SSHKeyImport,
    SSHKeyPublic,
    SSHKeysPublic,
    SSHKeyUpdate,
    Message,
)
from app.services.users.ssh_key_service import SSHKeyService

router = APIRouter(prefix="/ssh-keys", tags=["ssh-keys"])


@router.get("/", response_model=SSHKeysPublic)
def read_ssh_keys(
    session: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """
    Retrieve all SSH keys for the current user (metadata only, no private keys).
    """
    keys = SSHKeyService.get_user_keys(session, current_user.id)
    return SSHKeysPublic(data=keys, count=len(keys))


@router.get("/{id}", response_model=SSHKeyPublic)
def read_ssh_key(
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
) -> Any:
    """
    Get SSH key by ID (metadata only, no private key).
    """
    key = SSHKeyService.get_key_by_id(session, id, current_user.id)
    if not key:
        raise HTTPException(status_code=404, detail="SSH key not found")
    return key


@router.post("/generate", response_model=SSHKeyPublic)
def generate_ssh_key(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    key_in: SSHKeyGenerate,
) -> Any:
    """
    Generate a new RSA 4096-bit SSH key pair.

    Returns the key metadata including the public key.
    The private key is encrypted and stored securely.
    """
    try:
        key = SSHKeyService.generate_key_pair(session, current_user.id, key_in)
        return key
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/", response_model=SSHKeyPublic)
def import_ssh_key(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    key_in: SSHKeyImport,
) -> Any:
    """
    Import an existing SSH key.

    The private key and optional passphrase are encrypted and stored securely.
    Only the public key and metadata are returned.
    """
    try:
        key = SSHKeyService.import_key(session, current_user.id, key_in)
        return key
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/{id}", response_model=SSHKeyPublic)
def update_ssh_key(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
    key_in: SSHKeyUpdate,
) -> Any:
    """
    Update SSH key (only name can be updated).
    """
    key = SSHKeyService.update_key(session, id, current_user.id, key_in)
    if not key:
        raise HTTPException(status_code=404, detail="SSH key not found")
    return key


@router.delete("/{id}")
def delete_ssh_key(
    session: SessionDep,
    current_user: CurrentUser,
    id: uuid.UUID,
) -> Message:
    """
    Delete an SSH key.

    Note: If this key is used by any knowledge sources, those sources will be
    marked as disconnected.
    """
    success = SSHKeyService.delete_key(session, id, current_user.id)
    if not success:
        raise HTTPException(status_code=404, detail="SSH key not found")
    return Message(message="SSH key deleted successfully")
