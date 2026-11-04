"""Dashboard chat (D-C5) and component reference resolution.

- ``GET  /dashboards/{id}/chat?agent_id=`` — the (dashboard, agent) chat, or
  ``session: null`` before the first one.
- ``POST /dashboards/{id}/chat {agent_id, new?}`` — the chat, created when
  there is none or ``new`` ("New chat") is set.
- ``POST /dashboards/component-refs/resolve {refs, session_id?}`` — cards for
  ``cinna://component/…`` references; never wakes an environment.
"""
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException

from app.api.deps import CurrentUser, SessionDep
from app.models.sessions.session import Session as ChatSession
from app.models.sessions.session import SessionPublic
from app.models.dashboards.dashboard_chat import (
    ComponentRefsResolved,
    ComponentRefsResolveRequest,
    DashboardChatCreate,
    DashboardChatPublic,
)
from app.services.dashboards.component_refs_service import ComponentRefsService
from app.services.dashboards.dashboard_chat_service import (
    DashboardChatError,
    DashboardChatService,
)

router = APIRouter(prefix="/dashboards", tags=["DashboardChat"])


def _public(chat: ChatSession | None) -> DashboardChatPublic:
    return DashboardChatPublic(session=SessionPublic.model_validate(chat) if chat else None)


@router.post("/component-refs/resolve", response_model=ComponentRefsResolved)
async def resolve_component_refs(
    body: ComponentRefsResolveRequest, session: SessionDep, current_user: CurrentUser
) -> Any:
    """Resolve up to 20 component references for the viewer (the chat's agent first)."""
    chat_agent = ComponentRefsService.chat_agent(session, current_user, body.session_id)
    results = await ComponentRefsService.resolve(session, current_user, body.refs, chat_agent)
    return ComponentRefsResolved(results=results)


@router.get("/{dashboard_id}/chat", response_model=DashboardChatPublic)
def get_dashboard_chat(
    dashboard_id: uuid.UUID, agent_id: uuid.UUID, session: SessionDep, current_user: CurrentUser
) -> Any:
    try:
        chat = DashboardChatService.get(session, current_user, dashboard_id, agent_id)
    except DashboardChatError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    return _public(chat)


@router.post("/{dashboard_id}/chat", response_model=DashboardChatPublic)
def open_dashboard_chat(
    dashboard_id: uuid.UUID, body: DashboardChatCreate, session: SessionDep, current_user: CurrentUser
) -> Any:
    try:
        chat = DashboardChatService.get_or_create(
            session, current_user, dashboard_id, body.agent_id, new=body.new
        )
    except DashboardChatError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
    return _public(chat)
