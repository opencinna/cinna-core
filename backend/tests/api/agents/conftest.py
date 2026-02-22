"""
Agent-specific test fixtures.

Provides:
- Session proxy so service code uses the test DB session
- Environment adapter stub so agent creation uses real service logic without Docker
- Background task collector so fire-and-forget tasks can be drained by test utils
- External service mocks to prevent real calls
"""
import pytest
from unittest.mock import patch, AsyncMock

from app.core.config import settings
from app.services.environment_service import EnvironmentService
from app.services.environment_lifecycle import EnvironmentLifecycleManager
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.stubs.socketio_stub import StubSocketIOConnector
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import BackgroundTaskCollector, set_collector
from tests.utils.db_proxy import NonClosingSessionProxy


@pytest.fixture(autouse=True)
def patch_create_session(db):
    """All internal service session creation returns the test session.

    Must patch at every import site because `from app.core.db import create_session`
    binds a local reference that isn't updated by patching the source module alone.
    """
    factory = lambda: NonClosingSessionProxy(db)
    with (
        patch("app.core.db.create_session", factory),
        patch("app.services.email.processing_service.create_session", factory),
        patch("app.services.session_service.create_session", factory),
        patch("app.services.environment_service.create_session", factory),
        patch("app.services.commands.files_command.create_session", factory),
    ):
        yield


@pytest.fixture(autouse=True)
def patch_asyncio_to_thread():
    """Run asyncio.to_thread synchronously to avoid cross-thread session issues."""
    async def _run_sync(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    with patch("asyncio.to_thread", _run_sync):
        yield


@pytest.fixture(autouse=True)
def patch_environment_adapter(tmp_path_factory):
    """Patch lifecycle manager to use EnvironmentTestAdapter instead of Docker.

    Creates minimal template directory structure so real file I/O works
    on temp paths without Docker.
    """
    tmp = tmp_path_factory.mktemp("env")
    templates_dir = tmp / "templates"
    instances_dir = tmp / "instances"
    templates_dir.mkdir()
    instances_dir.mkdir()

    # Create minimal template with docker-compose.template.yml
    template_dir = templates_dir / settings.DEFAULT_AGENT_ENV_NAME
    template_dir.mkdir(parents=True)
    (template_dir / "docker-compose.template.yml").write_text(
        "version: '3'\nservices:\n  agent:\n    image: test\n    ports:\n      - '${AGENT_PORT}:8000'\n"
    )

    # Build lifecycle manager with tmp dirs
    lm = EnvironmentLifecycleManager()
    lm.templates_dir = templates_dir
    lm.instances_dir = instances_dir

    # Patch get_adapter to return test adapter
    def _test_get_adapter(environment):
        return EnvironmentTestAdapter()
    lm.get_adapter = _test_get_adapter

    # Install as singleton
    EnvironmentService._lifecycle_manager = lm
    yield lm
    EnvironmentService._lifecycle_manager = None


@pytest.fixture(autouse=True)
def background_tasks():
    """Collect background tasks for deferred execution.

    Replaces create_task_with_error_logging at every import site so that
    fire-and-forget coroutines are captured instead of scheduled.
    Test utilities (e.g. process_emails_with_stub) drain them automatically
    via drain_tasks().
    """
    collector = BackgroundTaskCollector()
    set_collector(collector)
    with (
        patch(
            "app.services.session_service.create_task_with_error_logging",
            collector,
        ),
        patch(
            "app.services.event_service.create_task_with_error_logging",
            collector,
        ),
        patch(
            "app.services.environment_service.create_task_with_error_logging",
            collector,
        ),
    ):
        yield
        collector.cleanup()
    set_collector(None)


@pytest.fixture(autouse=True)
def patch_external_services():
    """Mock external service calls (OAuth refresh, Socket.IO)."""
    with (
        patch(
            "app.services.credentials_service.CredentialsService.refresh_expiring_credentials_for_agent",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "app.services.event_service.socketio_connector",
            StubSocketIOConnector(),
        ),
    ):
        yield


@pytest.fixture(autouse=True)
def setup_default_credentials(client, superuser_token_headers):
    """Create a default anthropic AI credential so create_environment validation passes."""
    cred = create_random_ai_credential(
        client, superuser_token_headers,
        credential_type="anthropic",
        api_key="sk-ant-api03-test-default-key",
        name="test-default-credential",
        set_default=True,
    )
    yield cred
