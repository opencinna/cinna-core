from fastapi import APIRouter

from app.api.routes import (
    a2a,
    access_tokens,
    activities,
    agents,
    agent_shares,
    ai_credentials,
    credentials,
    credential_shares,
    environments,
    events,
    files,
    input_tasks,
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
    task_triggers,
    users,
    user_workspaces,
    utils,
    webhooks,
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
api_router.include_router(agent_shares.router)
api_router.include_router(access_tokens.router)
api_router.include_router(credential_shares.router)  # Must be before credentials.router for /shared-with-me
api_router.include_router(credentials.router)
api_router.include_router(ai_credentials.router)
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
api_router.include_router(input_tasks.router)
api_router.include_router(task_triggers.router, prefix="/tasks", tags=["task-triggers"])
api_router.include_router(webhooks.router, prefix="/hooks", tags=["webhooks"])
api_router.include_router(a2a.router)


if settings.ENVIRONMENT == "local":
    api_router.include_router(private.router)
