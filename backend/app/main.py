import sentry_sdk
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.middleware.cors import CORSMiddleware

from app.api.main import api_router
from app.api.routes.agent_hooks import router as agent_hooks_router
from app.api.routes.cli import setup_router as cli_setup_router
from app.api.routes.desktop_auth import router as desktop_auth_router  # noqa: F401 (used below)
from app.mcp.oauth_routes import router as mcp_oauth_router, wellknown_router as mcp_wellknown_router
from app.mcp.upload_routes import router as mcp_upload_router
from app.mcp.server import mcp_registry
from app.core.config import settings

# Configure logging
# Note: basicConfig is a no-op when uvicorn already configured root logger,
# so we also attach a handler to the "app" logger explicitly below.
logging.basicConfig(
    level=logging.DEBUG if settings.ENVIRONMENT == "local" else logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Ensure all app.* loggers can emit to console even when uvicorn owns root.
_app_logger = logging.getLogger("app")
if not _app_logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
    _app_logger.addHandler(_handler)
_app_logger.setLevel(logging.DEBUG if settings.ENVIRONMENT == "local" else logging.INFO)

# Set DEBUG level for specific modules we want to debug
if settings.ENVIRONMENT == "local":
    logging.getLogger("app.services.environments.adapters.docker_adapter").setLevel(logging.DEBUG)
    logging.getLogger("app.services.environments.environment_lifecycle").setLevel(logging.DEBUG)


def custom_generate_unique_id(route: APIRoute) -> str:
    return f"{route.tags[0]}-{route.name}"


if settings.SENTRY_DSN and settings.ENVIRONMENT != "local":
    sentry_sdk.init(dsn=str(settings.SENTRY_DSN), enable_tracing=True)


# ── Desktop app instance discovery endpoint ────────────────────────────────
# Registered at root level (not under /api/v1) per RFC 8615 /.well-known/
from fastapi import APIRouter as _APIRouter

_desktop_wellknown_router = _APIRouter(tags=["desktop-auth"])


@_desktop_wellknown_router.get("/.well-known/cinna-desktop")
def cinna_desktop_discovery() -> dict:
    """Return instance metadata for Cinna Desktop app discovery.

    Endpoint field names follow RFC 8414 (OAuth 2.0 Authorization Server
    Metadata): ``authorization_endpoint``, ``token_endpoint``,
    ``userinfo_endpoint``.
    """
    base = f"{settings.FRONTEND_HOST}{settings.API_V1_STR}/desktop-auth"
    return {
        "instance_name": settings.PROJECT_NAME,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "userinfo_endpoint": f"{base}/userinfo",
        "version": "1.0",
        "desktop_auth_enabled": settings.DESKTOP_AUTH_ENABLED,
    }


_app_wellknown_router = _APIRouter(tags=["app-auth"])


@_app_wellknown_router.get("/.well-known/cinna-app")
def cinna_app_discovery() -> dict:
    """Return instance metadata for Cinna Mobile app discovery.

    Mirrors /.well-known/cinna-desktop but points at the parallel /app-auth
    surface. Endpoint field names follow RFC 8414 (OAuth 2.0 Authorization
    Server Metadata): ``authorization_endpoint``, ``token_endpoint``,
    ``userinfo_endpoint``.
    """
    base = f"{settings.FRONTEND_HOST}{settings.API_V1_STR}/app-auth"
    return {
        "instance_name": settings.PROJECT_NAME,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "userinfo_endpoint": f"{base}/userinfo",
        "version": "1.0",
        "app_auth_enabled": settings.APP_AUTH_ENABLED,
    }


# Startup and shutdown imports
from app.services.events.event_service import event_service
from app.services.files.file_cleanup_scheduler import (
    start_scheduler as start_file_cleanup_scheduler,
    shutdown_scheduler as shutdown_file_cleanup_scheduler
)
from app.services.environments.environment_suspension_scheduler import (
    start_scheduler as start_suspension_scheduler,
    shutdown_scheduler as shutdown_suspension_scheduler
)
from app.services.environments.bundle_auto_update_scheduler import (
    start_scheduler as start_bundle_auto_update_scheduler,
    shutdown_scheduler as shutdown_bundle_auto_update_scheduler,
)
from app.services.tasks.task_trigger_scheduler import (
    start_scheduler as start_task_trigger_scheduler,
    shutdown_scheduler as shutdown_task_trigger_scheduler
)
from app.services.agents.agent_schedule_scheduler import (
    start_scheduler as start_agent_schedule_scheduler,
    shutdown_scheduler as shutdown_agent_schedule_scheduler
)
from app.services.email.polling_scheduler import (
    start_scheduler as start_email_polling_scheduler,
    shutdown_scheduler as shutdown_email_polling_scheduler
)
from app.services.email.sending_scheduler import (
    start_scheduler as start_email_sending_scheduler,
    shutdown_scheduler as shutdown_email_sending_scheduler
)
from app.services.environments.environment_status_scheduler import (
    start_scheduler as start_env_status_scheduler,
    shutdown_scheduler as shutdown_env_status_scheduler
)
from app.services.cli.cli_setup_token_scheduler import (
    start_scheduler as start_cli_cleanup_scheduler,
    shutdown_scheduler as shutdown_cli_cleanup_scheduler
)
from app.services.cli.device_login_scheduler import (
    start_scheduler as start_device_login_cleanup_scheduler,
    shutdown_scheduler as shutdown_device_login_cleanup_scheduler
)
from app.services.desktop_auth.desktop_auth_scheduler import (
    start_scheduler as start_desktop_auth_cleanup_scheduler,
    shutdown_scheduler as shutdown_desktop_auth_cleanup_scheduler
)
from app.services.bundles.app_data_orphan_scheduler import (
    start_scheduler as start_app_data_orphan_scheduler,
    shutdown_scheduler as shutdown_app_data_orphan_scheduler,
)
from app.services.bundles.app_data_gc_scheduler import (
    start_scheduler as start_app_data_gc_scheduler,
    shutdown_scheduler as shutdown_app_data_gc_scheduler,
)
from app.services.users.mfa_cleanup_service import (
    start_scheduler as start_mfa_cleanup_scheduler,
    shutdown_scheduler as shutdown_mfa_cleanup_scheduler,
)
from app.services.credentials.model_discovery_scheduler import (
    start_scheduler as start_model_discovery_scheduler,
    shutdown_scheduler as shutdown_model_discovery_scheduler,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application startup and shutdown."""
    # --- Startup ---
    # Background schedulers run real APScheduler BackgroundSchedulers whose jobs
    # bind to the application DB engine. The pytest harness sets
    # ``settings.TESTING`` so they never start under test (an isolation escape:
    # a job firing mid-run would mutate the dev DB from inside a test). In
    # production ``TESTING`` is False, so every scheduler starts as before.
    if not settings.TESTING:
        start_file_cleanup_scheduler()
        start_suspension_scheduler()
        start_bundle_auto_update_scheduler()
        start_task_trigger_scheduler()
        start_agent_schedule_scheduler()
        start_email_polling_scheduler()
        start_email_sending_scheduler()
        start_env_status_scheduler()
        start_cli_cleanup_scheduler()
        start_device_login_cleanup_scheduler()
        start_desktop_auth_cleanup_scheduler()
        start_app_data_orphan_scheduler()
        start_app_data_gc_scheduler()
        start_mfa_cleanup_scheduler()
        start_model_discovery_scheduler()

    # Register backend event handlers
    from app.models.events.event import EventType
    from app.services.environments.environment_service import EnvironmentService
    from app.services.events.activity_service import ActivityService
    from app.services.sessions.session_service import SessionService

    # --- Synced Workspace File Registry-driven event wiring ---
    #
    # All auto-synced workspace files are declared once in
    # ``app.services.environments.synced_files.SYNCED_FILES``. We derive the
    # handler registrations from that registry so adding a synced file is a
    # one-line change there (no hand-editing several blocks here):
    #   - bidirectional entries  → EnvironmentService reconcile handlers
    #   - pull_only entries       → their cache-refresh post-action handler
    from app.services.environments.synced_files import (
        bidirectional_files,
        pull_only_files,
    )
    from app.services.agents.agent_status_service import AgentStatusService
    from app.services.agents.cli_commands_service import CLICommandsService

    # Bidirectional prompt files: reconcile after a building session completes
    # (STREAM_COMPLETED) and after a watched-file change (WORKSPACE_FILES_CHANGED,
    # e.g. a Mutagen sync from the CLI). Both reconcile handlers are env⇄DB.
    if bidirectional_files():
        event_service.register_handler(
            event_type=EventType.STREAM_COMPLETED,
            handler=EnvironmentService.handle_stream_completed_event,
        )
        event_service.register_handler(
            event_type=EventType.WORKSPACE_FILES_CHANGED,
            handler=EnvironmentService.handle_workspace_files_changed_event,
        )

    # Pull-only env-authoritative caches: refresh after every backend-triggered
    # action that touched the agent-env. The full post-action event set now
    # INCLUDES ``ENVIRONMENT_ACTIVATED`` for every pull-only file — this closes
    # Gap 1 (STATUS.md was previously not pulled on activation; CLI commands
    # already was). Per-env rate limiting inside refresh_after_action dedupes
    # bursts. Each event carries ``environment_id`` in meta.
    _POST_ACTION_EVENTS = (
        EventType.ENVIRONMENT_ACTIVATED,
        EventType.STREAM_COMPLETED,
        EventType.STREAM_ERROR,
        EventType.CRON_COMPLETED_OK,
        EventType.CRON_TRIGGER_SESSION,
        EventType.CRON_ERROR,
        EventType.WORKSPACE_FILES_CHANGED,
    )
    _PULL_ONLY_HANDLERS = {
        "status": AgentStatusService.handle_post_action_event,
        "cli_commands": CLICommandsService.handle_post_action_event,
    }
    for synced_file in pull_only_files():
        handler = _PULL_ONLY_HANDLERS.get(synced_file.key)
        if handler is None:
            logger.warning(
                "No post-action handler registered for pull-only synced file %r",
                synced_file.key,
            )
            continue
        for _event_type in _POST_ACTION_EVENTS:
            event_service.register_handler(
                event_type=_event_type,
                handler=handler,
            )

    # Activity service handlers for streaming lifecycle
    event_service.register_handler(
        event_type=EventType.STREAM_STARTED,
        handler=ActivityService.handle_stream_started
    )
    event_service.register_handler(
        event_type=EventType.STREAM_COMPLETED,
        handler=ActivityService.handle_stream_completed
    )
    event_service.register_handler(
        event_type=EventType.STREAM_ERROR,
        handler=ActivityService.handle_stream_error
    )
    event_service.register_handler(
        event_type=EventType.STREAM_INTERRUPTED,
        handler=ActivityService.handle_stream_interrupted
    )

    # Session service handlers for session status management
    event_service.register_handler(
        event_type=EventType.STREAM_STARTED,
        handler=SessionService.handle_stream_started
    )
    event_service.register_handler(
        event_type=EventType.STREAM_COMPLETED,
        handler=SessionService.handle_stream_completed
    )
    event_service.register_handler(
        event_type=EventType.STREAM_ERROR,
        handler=SessionService.handle_stream_error
    )
    event_service.register_handler(
        event_type=EventType.STREAM_INTERRUPTED,
        handler=SessionService.handle_stream_interrupted
    )
    # Session service handler for processing pending messages when environment activates
    event_service.register_handler(
        event_type=EventType.ENVIRONMENT_ACTIVATED,
        handler=SessionService.handle_environment_activated
    )

    # Input task service handlers for task status sync from sessions
    from app.services.tasks.input_task_service import InputTaskService

    event_service.register_handler(
        event_type=EventType.STREAM_STARTED,
        handler=InputTaskService.handle_stream_started
    )
    event_service.register_handler(
        event_type=EventType.STREAM_COMPLETED,
        handler=InputTaskService.handle_stream_completed
    )
    event_service.register_handler(
        event_type=EventType.STREAM_ERROR,
        handler=InputTaskService.handle_stream_error
    )
    # To-do progress tracking: propagate session to-do updates to tasks
    event_service.register_handler(
        event_type=EventType.TODO_LIST_UPDATED,
        handler=InputTaskService.handle_todo_list_updated
    )

    # Email task activity handlers
    event_service.register_handler(
        event_type=EventType.TASK_CREATED,
        handler=ActivityService.handle_task_created
    )
    event_service.register_handler(
        event_type=EventType.TASK_STATUS_UPDATED,
        handler=ActivityService.handle_task_status_changed
    )

    # Session state handlers: activity creation + task feedback delivery
    event_service.register_handler(
        event_type=EventType.SESSION_STATE_UPDATED,
        handler=ActivityService.handle_session_state_updated
    )
    event_service.register_handler(
        event_type=EventType.SESSION_STATE_UPDATED,
        handler=InputTaskService.handle_session_state_updated
    )

    # Email sending handler: queue outgoing email when agent responds in email session
    from app.services.email.sending_service import EmailSendingService

    event_service.register_handler(
        event_type=EventType.STREAM_COMPLETED,
        handler=EmailSendingService.handle_stream_completed
    )

    logger.info("Registered backend event handlers (EnvironmentService, ActivityService, SessionService, InputTaskService, EmailSendingService)")

    # Availability check for the platform email sender.
    if not settings.emails_enabled:
        logger.warning(
            "Email sending disabled: SMTP_HOST / EMAILS_FROM_EMAIL not set. "
            "Password reset and system notifications will be skipped."
        )

    logger.info("Application startup complete")

    # MCP registry manages per-connector session manager lifecycles.
    # Its run() context creates a parent anyio task group; each connector's
    # session_manager.run() is started within it on first request.
    async with mcp_registry.run():
        yield

    # --- Shutdown ---
    if not settings.TESTING:
        shutdown_file_cleanup_scheduler()
        shutdown_suspension_scheduler()
        shutdown_bundle_auto_update_scheduler()
        shutdown_task_trigger_scheduler()
        shutdown_agent_schedule_scheduler()
        shutdown_email_polling_scheduler()
        shutdown_email_sending_scheduler()
        shutdown_env_status_scheduler()
        shutdown_cli_cleanup_scheduler()
        shutdown_device_login_cleanup_scheduler()
        shutdown_desktop_auth_cleanup_scheduler()
        shutdown_app_data_orphan_scheduler()
        shutdown_app_data_gc_scheduler()
        shutdown_mfa_cleanup_scheduler()
        shutdown_model_discovery_scheduler()
    event_service.shutdown()
    logger.info("Application shutdown complete")


app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    generate_unique_id_function=custom_generate_unique_id,
    lifespan=lifespan,
)

# CORS: MCP OAuth and protocol endpoints are accessed by external MCP clients
# whose origins aren't known ahead of time. Using allow_origin_regex to reflect
# any incoming Origin (CORSMiddleware reflects the actual value, not "*", so it
# works with allow_credentials). The regex is unconditional — MCP requires it in
# both local dev and production. API endpoints remain protected by JWT auth.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.all_cors_origins,
    allow_origin_regex=r".*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["mcp-session-id", "mcp-protocol-version"],
)

app.include_router(api_router, prefix=settings.API_V1_STR)

# Public agent webhook execution endpoint — no /api/v1 prefix, no JWT auth.
# Authenticated via encrypted bearer token inside the service layer.
app.include_router(agent_hooks_router, prefix="/agent-hooks")

# CLI setup bootstrap endpoint (top-level, short URL for curl oneliner)
app.include_router(cli_setup_router)

# Desktop app instance discovery (RFC 8615 /.well-known/ path, no /api/v1 prefix)
app.include_router(_desktop_wellknown_router)
# Mobile app instance discovery (parallel /app-auth surface)
app.include_router(_app_wellknown_router)

# RFC 9728 Protected Resource Metadata (must be at root level)
app.include_router(mcp_wellknown_router)

# MCP OAuth routes (must be before any /mcp mount)
app.include_router(mcp_oauth_router, prefix="/mcp/oauth")

# MCP file upload route (must be before /mcp ASGI mount — FastAPI routes match first)
app.include_router(mcp_upload_router)

# Per-connector MCP server mount (must be after /mcp/oauth routes)
app.mount("/mcp", mcp_registry)

# Mount the Socket.IO ASGI app at /ws
socket_app = event_service.get_asgi_app()
app.mount("/ws", socket_app)
