"""Users-domain test fixtures.

Two concerns are handled here, both autouse so every file in
``tests/api/users/`` is order-independent and isolated from real
infrastructure:

1. **Agent/environment stubs** — ``users_roles_test.py`` creates real agents
   to exercise role gating.  Without env stubs,
   ``EnvironmentService.create_environment(auto_start=True)`` would schedule a
   real Docker lifecycle build (isolation leak).  We mirror the stub stack from
   ``tests/api/credentials/conftest.py`` (env adapter, background-task
   collector, external-service mocks, ``create_session`` patching) so agent
   creation stays entirely in-process.

   The MFA files never create agents; they opt out of the heavy stubs via the
   module-level flag ``NEEDS_AGENT_STUBS = False`` to keep them cheap.

2. **MFA in-memory rate-limit buckets** — ``app.services.users.mfa_service``
   keeps two module-level dicts (``_verify_rate_limit_log`` keyed by user id,
   ``_anonymous_verify_rate_limit_log`` keyed by client IP — always
   ``"testclient"`` under ``TestClient``).  These survive transaction rollback
   and accumulate across tests in the same process.  This fixture clears both
   before and after every test so all MFA files are order-independent (the
   anonymous cap of 20 / 5 min would otherwise trip after the Nth verify call
   across the session).
"""
import pytest

import app.services.users.mfa_service as _mfa_svc
from tests.utils.fixtures import (
    patched_create_sessions,
    patched_background_tasks,
    patched_external_services,
    setup_environment_adapter,
    teardown_environment_adapter,
    CREATE_SESSION_TARGETS_AGENT,
    BACKGROUND_TASK_TARGETS_FULL,
)


def _needs_agent_stubs(request) -> bool:
    """Whether the requesting test module needs the heavy agent/env stubs.

    Defaults to True; the MFA modules set ``NEEDS_AGENT_STUBS = False``.
    """
    return getattr(request.module, "NEEDS_AGENT_STUBS", True)


@pytest.fixture(autouse=True)
def patch_create_session(request, db):
    """Patch create_session at all service import sites (including session/task services)."""
    if not _needs_agent_stubs(request):
        yield
        return
    with patched_create_sessions(db, CREATE_SESSION_TARGETS_AGENT):
        yield


@pytest.fixture(autouse=True)
def patch_environment_adapter(request, tmp_path_factory):
    """Patch lifecycle manager to use EnvironmentTestAdapter instead of Docker."""
    if not _needs_agent_stubs(request):
        yield None
        return
    lm = setup_environment_adapter(tmp_path_factory)
    yield lm
    teardown_environment_adapter()


@pytest.fixture(autouse=True)
def background_tasks(request):
    """Collect background tasks for deferred execution (never schedule real Docker work)."""
    if not _needs_agent_stubs(request):
        yield
        return
    with patched_background_tasks(BACKGROUND_TASK_TARGETS_FULL):
        yield


@pytest.fixture(autouse=True)
def patch_external_services(request):
    """Mock external service calls (OAuth refresh, Socket.IO, LLM providers)."""
    if not _needs_agent_stubs(request):
        yield
        return
    with patched_external_services(mock_ai_functions=True, mock_a2a_skills=True):
        yield


@pytest.fixture(autouse=True)
def _clear_rate_limit_buckets():
    """Clear the MFA in-memory rate-limit logs before and after each test.

    ``/login/mfa/verify`` has two in-memory guards (``_verify_rate_limit_log``
    keyed by user id, ``_anonymous_verify_rate_limit_log`` keyed by client IP).
    These module-level dicts survive transaction rollback and accumulate across
    tests in the same process; clearing them here keeps all MFA files
    order-independent.
    """
    _mfa_svc._verify_rate_limit_log.clear()
    _mfa_svc._anonymous_verify_rate_limit_log.clear()
    yield
    _mfa_svc._verify_rate_limit_log.clear()
    _mfa_svc._anonymous_verify_rate_limit_log.clear()
