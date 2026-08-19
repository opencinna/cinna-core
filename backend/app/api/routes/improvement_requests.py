"""
Agent Improvement Requests — the requester and recipient surfaces.

A session owner (*requester*) consents to sharing a frozen snapshot of one
session with the agent's owner (*recipient* — a bundle publisher, or themselves
for a standalone agent). These routes cover both sides of that hand-off.

| Method | Path                                        | Who              |
|--------|---------------------------------------------|------------------|
| GET    | /sessions/{session_id}/improvement-context  | Requester        |
| POST   | /improvement-requests                       | Requester        |
| GET    | /improvement-requests/mine                  | Requester        |
| GET    | /agents/{agent_id}/improvement-requests     | Recipient        |
| GET    | /improvement-requests/{request_id}          | Either party     |
| GET    | /improvement-requests/{request_id}/archive  | Either (audited) |
| PATCH  | /improvement-requests/{request_id}          | Recipient        |
| DELETE | /improvement-requests/{request_id}          | Recipient        |

Every authorization decision is made in ``ImprovementRequestService`` — the
account-CLI routes in ``cli.py`` call the same methods, so the ownership rules
physically cannot drift between the two transports. This module only maps
``ImprovementRequestDenied`` onto HTTP.

Inaccessible ids answer **404, not 403** (plan §4.3): a 403 would confirm that a
given request id exists. The one deliberate exception is a *requester* trying to
mutate a row they submitted — ``get_authorized`` has already established they are
party to it, so 403 there leaks nothing they did not already know.
"""
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from sqlmodel import Session as DBSession

from app.api.deps import CurrentUser, SessionDep
from app.models import (
    ImprovementContextPublic,
    ImprovementRequestCreate,
    ImprovementRequestDetailPublic,
    ImprovementRequestPublic,
    ImprovementRequestUpdate,
    ImprovementRequestsPublic,
    Message,
    Session as ChatSession,
    User,
)
from app.models.improvement.agent_improvement_request import IMPROVEMENT_SOURCE_WEB_UI
from app.services.improvement.improvement_download_service import archive_response
from app.services.improvement.improvement_request_service import (
    ImprovementRequestDenied,
    ImprovementRequestService,
)

router = APIRouter(tags=["improvement-requests"])


def _denied(e: ImprovementRequestDenied) -> HTTPException:
    """Map the service's typed refusal onto HTTP, preserving its status code."""
    return HTTPException(status_code=e.status_code, detail=e.message)


def _load_own_session(
    db: DBSession, session_id: uuid.UUID, current_user: User
) -> ChatSession:
    """Load a session the caller owns, or 404.

    A session belonging to somebody else is indistinguishable from one that does
    not exist. Only ``POST /improvement-requests`` reports the ownership failure
    distinctly (403), and it does so from the service's eligibility gate.
    """
    chat_session = db.get(ChatSession, session_id)
    if chat_session is None or chat_session.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Session not found")
    return chat_session


# ── Requester surfaces ───────────────────────────────────────────────


@router.get(
    "/sessions/{session_id}/improvement-context",
    response_model=ImprovementContextPublic,
)
def get_improvement_context(
    session_id: uuid.UUID,
    db: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """
    Pre-flight payload for the consent modal: who would receive this session,
    which bundle/version it came from, and whether submitting is possible at all.

    Runs the *same* eligibility gate and the *same* target resolution that
    submitting will run, and writes nothing — so the modal's disclosure copy can
    never disagree with what the submit button actually does. An ineligible
    session returns ``eligible=false`` with a machine-readable ``reason`` rather
    than an error status; the modal renders the explanation and hides the form.
    """
    chat_session = _load_own_session(db, session_id, current_user)
    return ImprovementRequestService.build_context_preview(
        db, chat_session, current_user
    )


@router.post(
    "/improvement-requests",
    response_model=ImprovementRequestPublic,
    status_code=status.HTTP_201_CREATED,
)
async def create_improvement_request(
    body: ImprovementRequestCreate,
    db: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """
    Submit an improvement request — the consent action, and the only write path
    into this feature. There is no admin, publisher, or automated way to create
    one.

    Captures a frozen snapshot of the session plus its runtime context — which
    includes the agent's prompt documents and, unless the requester opted out
    via ``include_memory=false``, its personal memory area — scrubs the source
    install's credential values out of both, persists it, and notifies the
    recipient. The source session is never read again
    afterwards: continuing or deleting the conversation does not change what the
    recipient sees, and there is no withdrawal.

    Denials carry the gate's reason: 403 when the caller does not own the
    session, 400 for guest/webapp shares, empty sessions, or a deleted agent,
    and 429 when a rate limit is hit.
    """
    chat_session = db.get(ChatSession, body.session_id)
    if chat_session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        request = await ImprovementRequestService.create_from_session(
            db,
            chat_session,
            current_user,
            comment=body.comment,
            source=IMPROVEMENT_SOURCE_WEB_UI,
            include_memory=body.include_memory,
        )
    except ImprovementRequestDenied as e:
        raise _denied(e)
    return ImprovementRequestService.project_many(db, [request])[0]


# ``/mine`` MUST stay above ``/{request_id}`` — otherwise FastAPI matches the
# path parameter first and answers 422 on a literal that is not a UUID.
@router.get("/improvement-requests/mine", response_model=ImprovementRequestsPublic)
def list_my_improvement_requests(
    db: SessionDep,
    current_user: CurrentUser,
    status_filter: str | None = Query(default=None, alias="status"),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, le=200),
) -> Any:
    """
    The requests the current user has submitted, unhandled first then newest.

    Includes ``resolution_note`` — the recipient's closing note is deliberately
    visible to the person who raised the request.
    """
    data, count = ImprovementRequestService.list_for_requester(
        db, current_user, status=status_filter, skip=skip, limit=limit
    )
    return ImprovementRequestsPublic(data=data, count=count)


# ── Recipient surfaces ───────────────────────────────────────────────


@router.get(
    "/agents/{agent_id}/improvement-requests",
    response_model=ImprovementRequestsPublic,
)
def list_agent_improvement_requests(
    agent_id: uuid.UUID,
    db: SessionDep,
    current_user: CurrentUser,
    status_filter: str | None = Query(default=None, alias="status"),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, le=200),
) -> Any:
    """
    Requests received on one agent the caller owns — the Configuration-tab card.

    An agent the caller does not own answers 404, not 403.
    """
    try:
        data, count = ImprovementRequestService.list_for_agent(
            db, agent_id, current_user, status=status_filter, skip=skip, limit=limit
        )
    except ImprovementRequestDenied as e:
        raise _denied(e)
    return ImprovementRequestsPublic(data=data, count=count)


# ── Single-request surfaces (either party) ───────────────────────────


@router.get(
    "/improvement-requests/{request_id}",
    response_model=ImprovementRequestDetailPublic,
)
def get_improvement_request(
    request_id: uuid.UUID,
    db: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """
    One request, with the frozen runtime-context block. Readable by the
    recipient and by the requester who submitted it; 404 for anyone else.

    Note that this returns the *context*, not the transcript — the messages are
    only ever handed over through the archive, which is audited.
    """
    try:
        request, _role = ImprovementRequestService.get_authorized(
            db, request_id, current_user
        )
    except ImprovementRequestDenied as e:
        raise _denied(e)
    return ImprovementRequestService.to_detail_public(db, request)


@router.get("/improvement-requests/{request_id}/archive")
async def download_improvement_archive(
    request_id: uuid.UUID,
    http_request: Request,
    db: SessionDep,
    current_user: CurrentUser,
) -> Response:
    """
    Download the improvement archive as a ZIP (README, metadata, context, and
    the frozen transcript in both markdown and JSON).

    Raw passthrough with **no** ``response_model`` — mirroring the
    ``/account/api-proxy`` pattern. The generated TypeScript client therefore
    types this as a blob rather than a model, so the frontend must fetch it with
    the shared authenticated-download helper instead of the service method.

    The archive is a pure function of the stored row, so it is built on demand
    and never cached. Every download of a *cross-user* request writes an
    ``IMPROVEMENT_ARCHIVE_DOWNLOADED`` security event; downloads of a request a
    user raised on their own agent are not audited.
    """
    try:
        request, _role = ImprovementRequestService.get_authorized(
            db, request_id, current_user
        )
    except ImprovementRequestDenied as e:
        raise _denied(e)

    return await archive_response(
        db, request, current_user, http_request=http_request
    )


@router.patch(
    "/improvement-requests/{request_id}",
    response_model=ImprovementRequestDetailPublic,
)
async def update_improvement_request(
    request_id: uuid.UUID,
    body: ImprovementRequestUpdate,
    db: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """
    Set the status and/or the resolution note. Recipient only.

    The note is shown to the person who submitted the request, so it is the
    single reply channel this feature offers. Last write wins.
    """
    try:
        request, _role = ImprovementRequestService.get_authorized(
            db, request_id, current_user
        )
        request = await ImprovementRequestService.update_status(
            db,
            request,
            current_user,
            status=body.status,
            note=body.resolution_note,
        )
    except ImprovementRequestDenied as e:
        raise _denied(e)
    return ImprovementRequestService.to_detail_public(db, request)


@router.delete("/improvement-requests/{request_id}", response_model=Message)
def delete_improvement_request(
    request_id: uuid.UUID,
    db: SessionDep,
    current_user: CurrentUser,
) -> Any:
    """
    Delete a request. Recipient only — the requester cannot withdraw one
    (consent is final by product decision, and the modal says so).
    """
    try:
        request, _role = ImprovementRequestService.get_authorized(
            db, request_id, current_user
        )
        ImprovementRequestService.delete(db, request, current_user)
    except ImprovementRequestDenied as e:
        raise _denied(e)
    return Message(message="Improvement request deleted")
