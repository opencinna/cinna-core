"""Shared test fixture helpers.

Provides importable pytest fixtures, context managers, and helper functions
for the common patterns used across domain-specific conftest.py files.

Usage in a conftest.py:
    # Identical fixtures — just import (pytest discovers them automatically)
    from tests.utils.fixtures import patch_asyncio_to_thread, setup_default_credentials

    # Parameterized fixtures — wrap context managers in @pytest.fixture
    @pytest.fixture(autouse=True)
    def patch_create_session(db):
        with patched_create_sessions(db, CREATE_SESSION_TARGETS_AGENT):
            yield
"""

import pytest
from contextlib import contextmanager, ExitStack
from unittest.mock import patch, AsyncMock

from app.core.config import settings
from app.services.environments.environment_service import EnvironmentService
from app.services.environments.environment_lifecycle import EnvironmentLifecycleManager, APP_CORE_BASE_DIR_NAME
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.stubs.socketio_stub import StubSocketIOConnector
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import BackgroundTaskCollector, set_collector
from tests.utils.db_proxy import NonClosingSessionProxy


# ── Patch target constants ──────────────────────────────────────────────────

CREATE_SESSION_TARGETS_BASE = [
    "app.core.db.create_session",
    "app.services.environments.environment_service.create_session",
]

CREATE_SESSION_TARGETS_AGENT = CREATE_SESSION_TARGETS_BASE + [
    "app.services.email.processing_service.create_session",
    "app.services.sessions.session_service.create_session",
    "app.services.tasks.input_task_service.create_session",
    "app.services.agents.commands.files_command.create_session",
]

BACKGROUND_TASK_TARGETS_BASE = [
    "app.services.events.event_service.create_task_with_error_logging",
    "app.services.environments.environment_service.create_task_with_error_logging",
]

BACKGROUND_TASK_TARGETS_FULL = BACKGROUND_TASK_TARGETS_BASE + [
    "app.services.sessions.session_service.create_task_with_error_logging",
    "app.services.tasks.input_task_service.create_task_with_error_logging",
    "app.services.tasks.task_comment_service.create_task_with_error_logging",
    "app.services.tasks.task_attachment_service.create_task_with_error_logging",
    "app.utils.create_task_with_error_logging",
]


# ── Importable fixtures (identical across domains) ──────────────────────────

@pytest.fixture(autouse=True)
def patch_asyncio_to_thread():
    """Run asyncio.to_thread synchronously to avoid cross-thread session issues."""
    async def _run_sync(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    with patch("asyncio.to_thread", _run_sync):
        yield


@pytest.fixture(autouse=True)
def setup_default_credentials(client, superuser_token_headers):
    """Create a default anthropic AI credential so create_environment validation passes."""
    yield create_default_ai_credential(client, superuser_token_headers)


# ── Context managers (for parameterized conftest fixtures) ──────────────────

@contextmanager
def patched_create_sessions(db, targets=None):
    """Patch create_session at the given import sites to return a NonClosingSessionProxy.

    Args:
        db: The test database session.
        targets: List of dotted module paths to patch. Defaults to CREATE_SESSION_TARGETS_BASE.
    """
    if targets is None:
        targets = CREATE_SESSION_TARGETS_BASE
    factory = lambda: NonClosingSessionProxy(db)
    with ExitStack() as stack:
        for target in targets:
            stack.enter_context(patch(target, factory))
        yield


@contextmanager
def patched_background_tasks(targets=None):
    """Collect background tasks for deferred execution.

    Replaces create_task_with_error_logging at the given import sites so that
    fire-and-forget coroutines are captured instead of scheduled.

    Args:
        targets: List of dotted module paths to patch. Defaults to BACKGROUND_TASK_TARGETS_BASE.
    """
    if targets is None:
        targets = BACKGROUND_TASK_TARGETS_BASE
    collector = BackgroundTaskCollector()
    set_collector(collector)
    with ExitStack() as stack:
        for target in targets:
            stack.enter_context(patch(target, collector))
        yield
        collector.cleanup()
    set_collector(None)


@contextmanager
def patched_external_services(
    mock_ai_functions=False,
    mock_a2a_skills=False,
):
    """Mock external service calls (OAuth refresh, Socket.IO, optionally LLM/A2A).

    Always patches: credentials refresh, Socket.IO connector.
    Optionally patches: AIFunctionsService.is_available, generate_a2a_skills.
    """
    with ExitStack() as stack:
        stack.enter_context(patch(
            "app.services.credentials.credentials_service.CredentialsService.refresh_expiring_credentials_for_agent",
            new=AsyncMock(return_value=False),
        ))
        stack.enter_context(patch(
            "app.services.events.event_service.socketio_connector",
            StubSocketIOConnector(),
        ))
        if mock_ai_functions:
            stack.enter_context(patch(
                "app.services.ai_functions.ai_functions_service.AIFunctionsService.is_available",
                return_value=False,
            ))
        if mock_a2a_skills:
            stack.enter_context(patch(
                "app.services.agents.agent_service.generate_a2a_skills",
                return_value=[],
            ))
        yield


# ── Helper functions ────────────────────────────────────────────────────────

def setup_environment_adapter(tmp_path_factory, *, persistent_adapter=False, extra_template_dirs=None):
    """Create and configure a test environment lifecycle manager.

    Sets up temp directories with a minimal docker-compose template and installs
    the lifecycle manager as the EnvironmentService singleton.

    Args:
        tmp_path_factory: pytest tmp_path_factory fixture.
        persistent_adapter: If True, reuses a single adapter instance (available
            as lm._test_adapter) so tests can inspect call history.
        extra_template_dirs: Additional subdirectories to create inside the template
            (e.g. ["app/core"]).

    Returns:
        The configured EnvironmentLifecycleManager.
        Caller must call teardown_environment_adapter() after the test.
    """
    tmp = tmp_path_factory.mktemp("env")
    templates_dir = tmp / "templates"
    instances_dir = tmp / "instances"
    templates_dir.mkdir()
    instances_dir.mkdir()

    template_dir = templates_dir / settings.DEFAULT_AGENT_ENV_NAME
    template_dir.mkdir(parents=True)
    (template_dir / "docker-compose.template.yml").write_text(
        "version: '3'\nservices:\n  agent:\n    image: test\n    ports:\n      - '${AGENT_PORT}:8000'\n"
    )

    # Create shared app_core_base/core directory (used during rebuild)
    app_core_base_dir = templates_dir / APP_CORE_BASE_DIR_NAME / "core"
    app_core_base_dir.mkdir(parents=True)

    if extra_template_dirs:
        for d in extra_template_dirs:
            (template_dir / d).mkdir(parents=True, exist_ok=True)

    lm = EnvironmentLifecycleManager()
    lm.templates_dir = templates_dir
    lm.instances_dir = instances_dir

    if persistent_adapter:
        adapter = EnvironmentTestAdapter()
        def _test_get_adapter(environment):
            return adapter
        lm.get_adapter = _test_get_adapter
        lm._test_adapter = adapter
    else:
        def _test_get_adapter(environment):
            return EnvironmentTestAdapter()
        lm.get_adapter = _test_get_adapter

    EnvironmentService._lifecycle_manager = lm
    return lm


def teardown_environment_adapter():
    """Reset the environment service singleton."""
    EnvironmentService._lifecycle_manager = None


def create_default_ai_credential(client, superuser_token_headers):
    """Create a default anthropic AI credential for tests."""
    return create_random_ai_credential(
        client, superuser_token_headers,
        credential_type="anthropic",
        api_key="sk-ant-api03-test-default-key",
        name="test-default-credential",
        set_default=True,
    )
