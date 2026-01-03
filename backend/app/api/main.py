from fastapi import APIRouter

from app.api.routes import (
    activities,
    agents,
    credentials,
    environments,
    events,
    items,
    knowledge,
    login,
    messages,
    oauth,
    private,
    sessions,
    users,
    user_workspaces,
    utils,
    workspace,
)
from app.core.config import settings

api_router = APIRouter()
api_router.include_router(login.router)
api_router.include_router(oauth.router)
api_router.include_router(users.router)
api_router.include_router(utils.router)
api_router.include_router(items.router)
api_router.include_router(agents.router)
api_router.include_router(credentials.router)
api_router.include_router(environments.router)
api_router.include_router(sessions.router)
api_router.include_router(messages.router)
api_router.include_router(workspace.router)
api_router.include_router(user_workspaces.router)
api_router.include_router(activities.router)
api_router.include_router(events.router)
api_router.include_router(knowledge.router)


if settings.ENVIRONMENT == "local":
    api_router.include_router(private.router)
