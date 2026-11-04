"""Dashboard chat (D-C5): one conversation session per (dashboard, agent).

The panel on ``/dashboards/$dashboardId`` talks to the focused tile's agent in
the latest session tagged with that dashboard and agent (no time window);
**New chat** starts another. A prompt action's session also carries the
dashboard (``SessionService.create_session``) but is not this chat. The chat
is marked explicitly with ``session_metadata.dashboard_chat = true`` (no new
column; ``integration_type`` stays NULL so it remains a web-UI session),
because a prompt action's ``dashboard_block_id`` is SET NULL when its tile is
deleted while its ``dashboard_id`` stays.
"""
import uuid

from sqlmodel import Session, col, select

from app.models.agents.agent import Agent
from app.models.sessions.session import Session as ChatSession
from app.models.sessions.session import SessionCreate
from app.models.users.user import User
from app.models.users.user_dashboard import UserDashboard
from app.services.sessions.session_service import SessionService

CHAT_MARKER_KEY = "dashboard_chat"


class DashboardChatError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(message)


class DashboardChatService:
    @staticmethod
    def _owned_dashboard(db: Session, dashboard_id: uuid.UUID, user: User) -> UserDashboard:
        dashboard = db.get(UserDashboard, dashboard_id)
        if dashboard is None or dashboard.owner_id != user.id:
            raise DashboardChatError(404, "Dashboard not found.")
        return dashboard

    @staticmethod
    def _owned_agent(db: Session, agent_id: uuid.UUID, user: User) -> Agent:
        agent = db.get(Agent, agent_id)
        if agent is None or agent.owner_id != user.id:
            raise DashboardChatError(404, "Agent not found.")
        return agent

    @staticmethod
    def _latest(db: Session, dashboard_id: uuid.UUID, agent_id: uuid.UUID, user_id: uuid.UUID) -> ChatSession | None:
        return db.exec(
            select(ChatSession)
            .where(
                ChatSession.dashboard_id == dashboard_id,
                ChatSession.agent_id == agent_id,
                ChatSession.user_id == user_id,
                ChatSession.mode == "conversation",
                ChatSession.session_metadata[CHAT_MARKER_KEY].as_boolean() == True,  # noqa: E712
            )
            .order_by(col(ChatSession.created_at).desc())
            .limit(1)
        ).first()

    @staticmethod
    def get(db: Session, user: User, dashboard_id: uuid.UUID, agent_id: uuid.UUID) -> ChatSession | None:
        """The current chat, or None before the first one (never creates)."""
        dashboard = DashboardChatService._owned_dashboard(db, dashboard_id, user)
        agent = DashboardChatService._owned_agent(db, agent_id, user)
        return DashboardChatService._latest(db, dashboard.id, agent.id, user.id)

    @staticmethod
    def get_or_create(
        db: Session, user: User, dashboard_id: uuid.UUID, agent_id: uuid.UUID, *, new: bool = False
    ) -> ChatSession:
        """The current chat, or a new one when there is none or ``new`` is set."""
        dashboard = DashboardChatService._owned_dashboard(db, dashboard_id, user)
        agent = DashboardChatService._owned_agent(db, agent_id, user)
        if not new:
            existing = DashboardChatService._latest(db, dashboard.id, agent.id, user.id)
            if existing is not None:
                return existing
        if not agent.active_environment_id:
            raise DashboardChatError(
                400,
                "Agent has no active environment. Please create and activate an environment first.",
            )
        created = SessionService.create_session(
            db,
            user.id,
            SessionCreate(agent_id=agent.id, mode="conversation"),
            dashboard_id=dashboard.id,
        )
        if created is None:
            raise DashboardChatError(400, "The chat could not be started.")
        created.session_metadata = {**(created.session_metadata or {}), CHAT_MARKER_KEY: True}
        db.add(created)
        db.commit()
        db.refresh(created)
        return created
