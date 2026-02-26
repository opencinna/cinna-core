import sentry_sdk
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.middleware.cors import CORSMiddleware

from app.api.main import api_router
from app.mcp.oauth_routes import router as mcp_oauth_router, wellknown_router as mcp_wellknown_router
from app.mcp.upload_routes import router as mcp_upload_router
from app.mcp.server import mcp_registry
from app.core.config import settings

# Configure logging
logging.basicConfig(
    level=logging.DEBUG if settings.ENVIRONMENT == "local" else logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Set DEBUG level for specific modules we want to debug
if settings.ENVIRONMENT == "local":
    logging.getLogger("app.services.adapters.docker_adapter").setLevel(logging.DEBUG)
    logging.getLogger("app.services.environment_lifecycle").setLevel(logging.DEBUG)


def custom_generate_unique_id(route: APIRoute) -> str:
    return f"{route.tags[0]}-{route.name}"


if settings.SENTRY_DSN and settings.ENVIRONMENT != "local":
    sentry_sdk.init(dsn=str(settings.SENTRY_DSN), enable_tracing=True)


# Startup and shutdown imports
from app.services.event_service import event_service
from app.services.file_cleanup_scheduler import (
    start_scheduler as start_file_cleanup_scheduler,
    shutdown_scheduler as shutdown_file_cleanup_scheduler
)
from app.services.environment_suspension_scheduler import (
    start_scheduler as start_suspension_scheduler,
    shutdown_scheduler as shutdown_suspension_scheduler
)
from app.services.task_trigger_scheduler import (
    start_scheduler as start_task_trigger_scheduler,
    shutdown_scheduler as shutdown_task_trigger_scheduler
)
from app.services.agent_schedule_scheduler import (
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application startup and shutdown."""
    # --- Startup ---
    start_file_cleanup_scheduler()
    start_suspension_scheduler()
    start_task_trigger_scheduler()
    start_agent_schedule_scheduler()
    start_email_polling_scheduler()
    start_email_sending_scheduler()

    # Register backend event handlers
    from app.models.event import EventType
    from app.services.environment_service import EnvironmentService
    from app.services.activity_service import ActivityService
    from app.services.session_service import SessionService

    # Environment service handlers
    event_service.register_handler(
        event_type=EventType.STREAM_COMPLETED,
        handler=EnvironmentService.handle_stream_completed_event
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
    from app.services.input_task_service import InputTaskService

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
    logger.info("Application startup complete")

    # MCP registry manages per-connector session manager lifecycles.
    # Its run() context creates a parent anyio task group; each connector's
    # session_manager.run() is started within it on first request.
    async with mcp_registry.run():
        yield

    # --- Shutdown ---
    shutdown_file_cleanup_scheduler()
    shutdown_suspension_scheduler()
    shutdown_task_trigger_scheduler()
    shutdown_agent_schedule_scheduler()
    shutdown_email_polling_scheduler()
    shutdown_email_sending_scheduler()
    event_service.shutdown()
    logger.info("Application shutdown complete")


app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    generate_unique_id_function=custom_generate_unique_id,
    lifespan=lifespan,
)

# Set all CORS enabled origins
if settings.all_cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.all_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

app.include_router(api_router, prefix=settings.API_V1_STR)

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
