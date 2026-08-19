"""Archive download — the audited chokepoint shared by the web and CLI routes.

Two transports hand an improvement archive to a human (the REST route and the
account-CLI route), and there is exactly one rule about auditing it: a
``SecurityEvent`` is written when, and only when, the row is *cross-user*
(``owner_user_id != requester_user_id``) — the one path in the platform where
user A's conversation content is read by user B.

Building the ZIP, stamping the download headers, and writing that event
therefore live in one function that hands back a finished ``Response``, so the
two transports cannot drift on either the audit or the ``Content-Disposition``.

See ``docs/plans/agent_improvement_requests_plan.md`` §4.6.
"""
import logging

from fastapi import Request, Response
from sqlmodel import Session as DBSession

from app.models.events.security_event import (
    IMPROVEMENT_ARCHIVE_DOWNLOADED,
    SecurityEventCreate,
)
from app.models.improvement.agent_improvement_request import AgentImprovementRequest
from app.models.users.user import User
from app.services.events.security_event_service import SecurityEventService
from app.services.improvement.improvement_request_service import (
    ImprovementRequestService,
)
from app.utils import client_ip

logger = logging.getLogger(__name__)

ZIP_MEDIA_TYPE = "application/zip"


async def archive_response(
    db: DBSession,
    request: AgentImprovementRequest,
    actor: User,
    http_request: Request | None = None,
) -> Response:
    """Build the archive, audit a cross-user read, and return it as a download.

    The caller has already been authorized by
    :meth:`ImprovementRequestService.get_authorized`; this adds the audit and
    the transport headers. The filename is server-derived from the request id
    (plan §4.5) — never from user input, so it cannot inject a header.
    """
    payload, filename = ImprovementRequestService.build_archive(db, request)
    if request.owner_user_id != request.requester_user_id:
        await _audit(db, request, actor, http_request)
    return Response(
        content=payload,
        media_type=ZIP_MEDIA_TYPE,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _audit(
    db: DBSession,
    request: AgentImprovementRequest,
    actor: User,
    http_request: Request | None,
) -> None:
    """Write the cross-user download event.

    Records identities and ids only — never a byte of the shared conversation.

    **Fail-open is deliberate**, matching the ``environment_console_service``
    audit precedent: a database hiccup while writing the event must not deny a
    recipient the feedback they were legitimately handed. The failure is logged
    with a stack trace so it is visible. The transaction is rolled back because
    ``create_event`` commits — a mid-flush failure would otherwise leave the
    request-scoped session unusable for anything the route does afterwards.
    """
    agent_context = (request.context or {}).get("agent") or {}
    try:
        await SecurityEventService.create_event(
            session=db,
            user_id=actor.id,
            data=SecurityEventCreate(
                agent_id=request.target_agent_id,
                event_type=IMPROVEMENT_ARCHIVE_DOWNLOADED,
                severity="medium",
                details={
                    "request_id": str(request.id),
                    "target_agent_id": str(request.target_agent_id),
                    "bundle_id": agent_context.get("bundle_id"),
                    "bundle_uuid": (
                        str(request.bundle_uuid) if request.bundle_uuid else None
                    ),
                    "requester_user_id": str(request.requester_user_id),
                    "acting_user_id": str(actor.id),
                    "source_ip": client_ip(http_request),
                },
            ),
        )
    except Exception:
        db.rollback()
        logger.exception(
            "Failed to write SecurityEvent %s for improvement request %s",
            IMPROVEMENT_ARCHIVE_DOWNLOADED,
            request.id,
        )
