from fastapi import APIRouter

from app.api.routes import (
    activities,
    agents,
    credentials,
    environments,
    events,
    files,
    items,
    knowledge,
    knowledge_sources,
    llm_plugins,
    login,
    messages,
    oauth,
    oauth_credentials,
    private,
    sessions,
    ssh_keys,
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
api_router.include_router(oauth_credentials.router, prefix="/credentials", tags=["credentials"])
api_router.include_router(ssh_keys.router)
api_router.include_router(environments.router)
api_router.include_router(sessions.router)
api_router.include_router(messages.router)
api_router.include_router(workspace.router)
api_router.include_router(user_workspaces.router)
api_router.include_router(activities.router)
api_router.include_router(events.router)
api_router.include_router(knowledge.router)
api_router.include_router(knowledge_sources.router)
api_router.include_router(files.router)
api_router.include_router(llm_plugins.router)


if settings.ENVIRONMENT == "local":
    api_router.include_router(private.router)
