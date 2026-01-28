import sentry_sdk
import logging
from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.middleware.cors import CORSMiddleware

from app.api.main import api_router
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

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    generate_unique_id_function=custom_generate_unique_id,
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

# Mount Socket.IO app for WebSocket support
from app.services.event_service import event_service

# Mount the Socket.IO ASGI app at /ws
socket_app = event_service.get_asgi_app()
app.mount("/ws", socket_app)


# Startup and shutdown events
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


@app.on_event("startup")
def on_startup():
    """Start background services on app startup"""
    start_file_cleanup_scheduler()
    start_suspension_scheduler()
    start_task_trigger_scheduler()

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
    # These handlers update session status based on streaming events
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

    # Session state handlers: activity creation + task feedback delivery
    event_service.register_handler(
        event_type=EventType.SESSION_STATE_UPDATED,
        handler=ActivityService.handle_session_state_updated
    )
    event_service.register_handler(
        event_type=EventType.SESSION_STATE_UPDATED,
        handler=InputTaskService.handle_session_state_updated
    )

    logger.info("Registered backend event handlers (EnvironmentService, ActivityService, SessionService, InputTaskService)")

    logger.info("Application startup complete")


@app.on_event("shutdown")
def on_shutdown():
    """Stop background services on app shutdown"""
    shutdown_file_cleanup_scheduler()
    shutdown_suspension_scheduler()
    shutdown_task_trigger_scheduler()
    event_service.shutdown()
    logger.info("Application shutdown complete")
