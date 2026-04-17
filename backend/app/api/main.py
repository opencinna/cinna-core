from fastapi import APIRouter

from app.api.routes import (
    a2a,
    access_tokens,
    agent_app_mcp_routes,
    app_agent_routes,
    cli,
    activities,
    agents,
    agent_shares,
    agentic_teams,
    ai_credentials,
    credentials,
    credential_shares,
    desktop_auth,
    email_integration,
    environments,
    identity,
    identity_contacts,
    mcp_connectors,
    mcp_consent,
    events,
    files,
    guest_shares,
    input_tasks,
    knowledge,
    knowledge_sources,
    llm_plugins,
    login,
    mail_servers,
    messages,
    oauth,
    oauth_credentials,
    private,
    security_events,
    sessions,
    shared_workspace,
    ssh_keys,
    task_agent_api,
    task_triggers,
    user_app_agent_routes,
    users,
    user_dashboards,
    user_workspaces,
    utils,
    webhooks,
    webapp,
    webapp_chat,
    webapp_interface_config,
    webapp_public,
    webapp_share,
    workspace,
)
from app.api.routes.guest_shares import guest_router as guest_share_auth_router
from app.api.routes.webapp_share import public_router as webapp_share_public_router
from app.core.config import settings

api_router = APIRouter()
api_router.include_router(login.router)
api_router.include_router(oauth.router)
api_router.include_router(users.router)
api_router.include_router(utils.router)
api_router.include_router(agents.router)
api_router.include_router(agentic_teams.router)
api_router.include_router(agent_shares.router)
api_router.include_router(access_tokens.router)
api_router.include_router(guest_shares.router)
api_router.include_router(guest_share_auth_router)
api_router.include_router(credential_shares.router)  # Must be before credentials.router for /shared-with-me
api_router.include_router(credentials.router)
api_router.include_router(ai_credentials.router)
api_router.include_router(oauth_credentials.router, prefix="/credentials", tags=["credentials"])
api_router.include_router(ssh_keys.router)
api_router.include_router(environments.router)
api_router.include_router(sessions.router)
api_router.include_router(messages.router)
api_router.include_router(workspace.router)
api_router.include_router(user_dashboards.router)
api_router.include_router(user_workspaces.router)
api_router.include_router(activities.router)
api_router.include_router(security_events.router)
api_router.include_router(events.router)
api_router.include_router(knowledge.router)
api_router.include_router(knowledge_sources.router)
api_router.include_router(files.router)
api_router.include_router(llm_plugins.router)
api_router.include_router(input_tasks.router)
api_router.include_router(task_agent_api.router)
api_router.include_router(task_triggers.router, prefix="/tasks", tags=["task-triggers"])
api_router.include_router(webhooks.router, prefix="/hooks", tags=["webhooks"])
api_router.include_router(mail_servers.router)
api_router.include_router(email_integration.router)
api_router.include_router(webapp.router)
api_router.include_router(webapp_interface_config.router)
api_router.include_router(webapp_share.router)
api_router.include_router(webapp_share_public_router)
api_router.include_router(webapp_chat.router)
api_router.include_router(webapp_public.router)
api_router.include_router(shared_workspace.router)
api_router.include_router(a2a.router)        # /a2a/{agent_id}/ (latest / v1.0)
api_router.include_router(a2a.v1_router)     # /a2a/v1.0/{agent_id}/
api_router.include_router(a2a.v03_router)    # /a2a/v0.3/{agent_id}/
api_router.include_router(mcp_connectors.router)
api_router.include_router(mcp_consent.router)
api_router.include_router(agent_app_mcp_routes.router)
api_router.include_router(app_agent_routes.router)
api_router.include_router(user_app_agent_routes.router)
api_router.include_router(identity.router)
api_router.include_router(identity_contacts.router)
api_router.include_router(cli.router)
api_router.include_router(desktop_auth.router)


if settings.ENVIRONMENT == "local":
    api_router.include_router(private.router)
