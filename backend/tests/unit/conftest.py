"""
Unit test conftest — overrides the root conftest's DB-dependent fixtures
so that pure unit tests can run without a database connection.

Also sets up sys.path for importing from the env-template tree
(adapters, MCP bridge servers, etc.) so individual test files
don't need to repeat the path manipulation.
"""

import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup — env-template modules live outside the normal backend package.
# Add app_core_base to sys.path so imports like
#   from core.server.adapters.tool_name_registry import ...
# work without --noconftest or per-file sys.path hacks.
# ---------------------------------------------------------------------------

_APP_CORE_BASE = (
    Path(__file__).parents[2]
    / "app"
    / "env-templates"
    / "app_core_base"
)

if str(_APP_CORE_BASE) not in sys.path:
    sys.path.insert(0, str(_APP_CORE_BASE))


# ---------------------------------------------------------------------------
# Override root conftest's DB fixtures — unit tests don't need a database.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def setup_db():
    """No-op override: unit tests skip DB migrations and seeding."""
    yield
