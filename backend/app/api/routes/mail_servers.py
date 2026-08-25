"""Mail server configuration API — superuser-only.

A ``MailServerConfig`` is server-owned infrastructure: an email
``ServerChannel`` references one by id the way a Google Chat channel
references its service account. It holds an encrypted mailbox credential and
decides where the platform's mail goes, so — exactly like channel
administration — it is superuser-or-nothing. There is no per-row ownership to
check any more; the dependency is the whole gate.
"""
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import SessionDep, get_current_active_superuser
from app.models import (
    MailServerConfigCreate,
    MailServerConfigUpdate,
    MailServerConfigPublic,
    MailServerConfigsPublic,
    MailServerType,
    Message,
    User,
)
from app.services.email.mail_server_service import (
    MailServerInUseError,
    MailServerService,
)

router = APIRouter(prefix="/mail-servers", tags=["mail-servers"])

SuperUser = Annotated[User, Depends(get_current_active_superuser)]


@router.get("/", response_model=MailServerConfigsPublic)
def list_mail_servers(
    session: SessionDep,
    current_user: SuperUser,
    server_type: MailServerType | None = None,
    skip: int = 0,
    limit: int = 100,
) -> Any:
    """List the server's mail server configurations."""
    return MailServerService.list_mail_servers(
        session=session,
        server_type=server_type,
        skip=skip,
        limit=limit,
    )


@router.post("/", response_model=MailServerConfigPublic)
def create_mail_server(
    *,
    session: SessionDep,
    current_user: SuperUser,
    server_in: MailServerConfigCreate,
) -> Any:
    """Create a new mail server configuration."""
    server = MailServerService.create_mail_server(
        session=session,
        data=server_in,
    )
    return MailServerService._to_public(server)


@router.get("/{server_id}", response_model=MailServerConfigPublic)
def get_mail_server(
    session: SessionDep,
    current_user: SuperUser,
    server_id: uuid.UUID,
) -> Any:
    """Get a mail server configuration by ID."""
    server = MailServerService.get_mail_server(session, server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Mail server not found")
    return MailServerService._to_public(server)


@router.put("/{server_id}", response_model=MailServerConfigPublic)
def update_mail_server(
    *,
    session: SessionDep,
    current_user: SuperUser,
    server_id: uuid.UUID,
    server_in: MailServerConfigUpdate,
) -> Any:
    """Update a mail server configuration."""
    server = MailServerService.get_mail_server(session, server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Mail server not found")

    server = MailServerService.update_mail_server(
        session=session,
        server=server,
        data=server_in,
    )
    return MailServerService._to_public(server)


@router.delete("/{server_id}")
def delete_mail_server(
    session: SessionDep,
    current_user: SuperUser,
    server_id: uuid.UUID,
) -> Message:
    """Delete a mail server configuration.

    Blocked with HTTP 409 while any channel still references the server: the
    reference is a plain id in ``ServerChannel.config`` with no FK behind it,
    so deleting would leave that channel silently unable to send or receive.
    The 409 body carries the referencing channels so the admin can detach them
    first.
    """
    server = MailServerService.get_mail_server(session, server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Mail server not found")

    try:
        MailServerService.delete_mail_server(session=session, server=server)
    except MailServerInUseError as e:
        raise HTTPException(
            status_code=409, detail=e.impact.model_dump(mode="json")
        )
    return Message(message="Mail server deleted successfully")


@router.post("/{server_id}/test-connection")
def test_mail_server_connection(
    session: SessionDep,
    current_user: SuperUser,
    server_id: uuid.UUID,
) -> Message:
    """Test connection to a mail server."""
    server = MailServerService.get_mail_server(session, server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Mail server not found")

    try:
        message = MailServerService.test_connection(session=session, server_id=server_id)
        return Message(message=message)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
