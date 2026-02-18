import uuid
from typing import Any

from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUser, SessionDep
from app.models import (
    MailServerConfig,
    MailServerConfigCreate,
    MailServerConfigUpdate,
    MailServerConfigPublic,
    MailServerConfigsPublic,
    MailServerType,
    Message,
)
from app.services.email.mail_server_service import MailServerService

router = APIRouter(prefix="/mail-servers", tags=["mail-servers"])


@router.get("/", response_model=MailServerConfigsPublic)
def list_mail_servers(
    session: SessionDep,
    current_user: CurrentUser,
    server_type: MailServerType | None = None,
    skip: int = 0,
    limit: int = 100,
) -> Any:
    """List user's mail server configurations."""
    return MailServerService.get_user_mail_servers(
        session=session,
        user_id=current_user.id,
        server_type=server_type,
        skip=skip,
        limit=limit,
    )


@router.post("/", response_model=MailServerConfigPublic)
def create_mail_server(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    server_in: MailServerConfigCreate,
) -> Any:
    """Create a new mail server configuration."""
    server = MailServerService.create_mail_server(
        session=session,
        user_id=current_user.id,
        data=server_in,
    )
    return MailServerService._to_public(server)


@router.get("/{server_id}", response_model=MailServerConfigPublic)
def get_mail_server(
    session: SessionDep,
    current_user: CurrentUser,
    server_id: uuid.UUID,
) -> Any:
    """Get a mail server configuration by ID."""
    server = MailServerService.get_mail_server(session, server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Mail server not found")
    if server.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized")
    return MailServerService._to_public(server)


@router.put("/{server_id}", response_model=MailServerConfigPublic)
def update_mail_server(
    *,
    session: SessionDep,
    current_user: CurrentUser,
    server_id: uuid.UUID,
    server_in: MailServerConfigUpdate,
) -> Any:
    """Update a mail server configuration."""
    server = MailServerService.get_mail_server(session, server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Mail server not found")
    if server.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized")

    server = MailServerService.update_mail_server(
        session=session,
        server=server,
        data=server_in,
    )
    return MailServerService._to_public(server)


@router.delete("/{server_id}")
def delete_mail_server(
    session: SessionDep,
    current_user: CurrentUser,
    server_id: uuid.UUID,
) -> Message:
    """Delete a mail server configuration."""
    server = MailServerService.get_mail_server(session, server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Mail server not found")
    if server.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized")

    MailServerService.delete_mail_server(session=session, server=server)
    return Message(message="Mail server deleted successfully")


@router.post("/{server_id}/test-connection")
def test_mail_server_connection(
    session: SessionDep,
    current_user: CurrentUser,
    server_id: uuid.UUID,
) -> Message:
    """Test connection to a mail server."""
    server = MailServerService.get_mail_server(session, server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Mail server not found")
    if server.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not authorized")

    try:
        message = MailServerService.test_connection(session=session, server_id=server_id)
        return Message(message=message)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
