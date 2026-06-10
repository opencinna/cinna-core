"""
Architecture drift test — create_session / create_task_with_error_logging patch targets.

WHY THIS EXISTS
---------------
The agent-domain test fixtures patch ``create_session`` (and the background-task
scheduler ``create_task_with_error_logging``) at every *import site* so that
service code opens sessions on the rolled-back test transaction instead of the
real engine, and so fire-and-forget coroutines are collected instead of
scheduled. The patch target is a *module attribute* path — e.g.
``app.services.sessions.session_service.create_session`` — because
``unittest.mock.patch`` rebinds the name in the importing module, not the
definition site.

If a module does ``from app.core.db import create_session`` at module level but
is NOT listed in the patch-target lists, then during a test its
``create_session`` still points at the real factory: it silently opens a
connection on the real engine, can't see test data, and the handler "silently
returns" (see ``backend/tests/README.md`` → *Source Code Invariants* #3). This
is exactly the isolation-escape failure mode this test guards against.

WHAT IT ENFORCES
----------------
Every module under ``backend/app/`` that imports ``create_session`` (from
``app.core.db``) or ``create_task_with_error_logging`` (from ``app.utils``)
**at module level** must either:
  * appear in the union of patch-target lists (``tests/utils/fixtures.py``
    constants + the inline extensions in domain ``conftest.py`` files), or
  * be explicitly allowlisted below with a justifying comment.

IMPORTANT — module-level vs. function-local imports
---------------------------------------------------
Only **module-level** imports are checkable here. A *function-local* import
(``from app.core.db import create_session`` inside a function body) never
becomes a module attribute, so it cannot be patched at
``app.<module>.create_session``. Those call sites are instead covered by the
base patch on ``app.core.db.create_session`` itself (which every patch-target
list includes via ``CREATE_SESSION_TARGETS_BASE``), because the function-local
``from app.core.db import create_session`` resolves the name from the (patched)
``app.core.db`` module at call time. We therefore deliberately scan ONLY
top-level ``ImportFrom`` nodes — function-local importers are out of scope and
require no target entry.

HOW IT WORKS
------------
  1. AST-walk every ``.py`` under ``backend/app/``; collect top-level
     ``from app.core.db import create_session [as X]`` and
     ``from app.utils import create_task_with_error_logging [as X]`` as the
     target string ``"<module>.<bound_name>"`` (honoring ``as`` aliases).
  2. Build the union of patch targets: the four constants in
     ``tests/utils/fixtures.py`` plus every inline ``"app.….create_session"`` /
     ``"app.….create_task_with_error_logging"`` string literal in the domain
     ``conftest.py`` files and the two test modules that extend the lists.
  3. Assert each importer target is in the union or in the allowlist below.
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

from tests.utils.fixtures import (
    CREATE_SESSION_TARGETS_BASE,
    CREATE_SESSION_TARGETS_AGENT,
    BACKGROUND_TASK_TARGETS_BASE,
    BACKGROUND_TASK_TARGETS_FULL,
)


# ── Roots ──────────────────────────────────────────────────────────────────────

_HERE = pathlib.Path(__file__).resolve()
_BACKEND_ROOT = _HERE.parent.parent.parent          # backend/
APP_ROOT = _BACKEND_ROOT / "app"
TESTS_ROOT = _BACKEND_ROOT / "tests"

# Definition sites — the import *source*, not an import site to patch.
_SESSION_SOURCE_MODULE = "app.core.db"
_SESSION_NAME = "create_session"
_BG_SOURCE_MODULE = "app.utils"
_BG_NAME = "create_task_with_error_logging"


# ── Allowlist for intentional exclusions ───────────────────────────────────────
#
# Each entry is a module-level import target that is intentionally NOT patched.
# Every entry MUST carry a comment explaining why the module is never exercised
# under the patched agent-domain fixtures (so a real isolation escape can't hide
# here). Format: dotted "<module>.<bound_name>" exactly as the importer binds it.

ALLOWED_UNPATCHED_SESSION_TARGETS = {
    # APScheduler jobs — disabled in tests via settings.TESTING (Phase 1). Their
    # create_session call only fires from a scheduler thread, which never starts
    # under the test app lifespan, so no test transaction is involved.
    "app.services.bundles.app_data_gc_scheduler.create_session",
    "app.services.bundles.app_data_orphan_scheduler.create_session",
    "app.services.credentials.model_discovery_scheduler.create_session",
    "app.services.environments.environment_status_scheduler.create_session",
    # Admin-only env service: its module-level create_session is used only inside
    # _rebuild_env_background, a Docker-backed admin bulk-rebuild background job
    # that no agent/credential/env test drives (those go through the lifecycle
    # adapter stub, not the admin bulk-rebuild path).
    "app.services.environments.admin_environment_service.create_session",
}

# (none currently — every module-level create_task_with_error_logging importer
# that an agent-domain test can reach is in BACKGROUND_TASK_TARGETS_FULL.)
ALLOWED_UNPATCHED_BG_TARGETS: set[str] = set()


# ── Importer discovery (AST, module-level only) ─────────────────────────────────


def _module_dotted_path(path: pathlib.Path) -> str:
    """`backend/app/services/x/y.py` -> `app.services.x.y`."""
    rel = path.resolve().relative_to(_BACKEND_ROOT).with_suffix("")
    return ".".join(rel.parts)


def _collect_module_level_importers(source_module: str, name: str) -> set[str]:
    """Return ``{"<module>.<bound_name>"}`` for top-level ``from <source_module> import <name> [as X]``.

    Function-local imports are intentionally ignored (see module docstring): we
    only inspect ``tree.body`` (module scope), never nested function bodies.
    """
    targets: set[str] = set()
    for path in sorted(APP_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        module = _module_dotted_path(path)
        for node in tree.body:  # module scope only
            if not isinstance(node, ast.ImportFrom):
                continue
            if (node.module or "") != source_module:
                continue
            for alias in node.names:
                if alias.name == name:
                    bound = alias.asname or alias.name
                    targets.add(f"{module}.{bound}")
    return targets


# ── Patch-target union (fixtures constants + conftest inline literals) ──────────

# Matches the dotted patch-target string literals embedded in conftests / tests.
_LITERAL_RE = re.compile(
    r'"(app\.[A-Za-z0-9_.]*\.(?:create_session|create_db_session|create_task_with_error_logging))"'
)

# Test modules (besides conftest.py) that extend the patch-target lists inline.
_EXTRA_TARGET_FILES = (
    TESTS_ROOT / "api" / "agents" / "agents_message_attachments_test.py",
    TESTS_ROOT / "api" / "users" / "users_search_test.py",
)


def _scan_inline_targets() -> tuple[set[str], set[str]]:
    """Scan conftests + extender test files for inline patch-target literals.

    Returns ``(session_targets, bg_targets)``.
    """
    session: set[str] = set()
    bg: set[str] = set()
    files = list(TESTS_ROOT.rglob("conftest.py")) + list(_EXTRA_TARGET_FILES)
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for literal in _LITERAL_RE.findall(text):
            if literal.endswith(_BG_NAME):
                bg.add(literal)
            else:
                session.add(literal)
    return session, bg


def _session_target_union() -> set[str]:
    inline_session, _ = _scan_inline_targets()
    return (
        set(CREATE_SESSION_TARGETS_BASE)
        | set(CREATE_SESSION_TARGETS_AGENT)
        | inline_session
    )


def _bg_target_union() -> set[str]:
    _, inline_bg = _scan_inline_targets()
    return (
        set(BACKGROUND_TASK_TARGETS_BASE)
        | set(BACKGROUND_TASK_TARGETS_FULL)
        | inline_bg
    )


# ── Tests ──────────────────────────────────────────────────────────────────────


def _format_failure(kind: str, missing: set[str], allowlist_name: str) -> str:
    listed = "\n  ".join(sorted(missing))
    return (
        f"{len(missing)} module(s) import {kind} at module level but are NOT in "
        f"any patch-target list (fixtures.py constants + conftest inline "
        f"extensions) and NOT allowlisted:\n  {listed}\n\n"
        f"During agent-domain tests these modules open sessions / schedule "
        f"background tasks on the REAL engine instead of the rolled-back test "
        f"transaction (see backend/tests/README.md → Source Code Invariants #3).\n\n"
        f"Fix by adding each target to the appropriate list (CREATE_SESSION_* / "
        f"BACKGROUND_TASK_* in tests/utils/fixtures.py, or the inline extension in "
        f"the domain conftest that exercises it). Only add to "
        f"{allowlist_name} (with a justifying comment) if the module is genuinely "
        f"never reached under the patched fixtures (e.g. a scheduler gated off in "
        f"tests)."
    )


def test_create_session_importers_are_patched() -> None:
    """Every module-level ``create_session`` importer is patched or allowlisted."""
    importers = _collect_module_level_importers(_SESSION_SOURCE_MODULE, _SESSION_NAME)
    covered = _session_target_union() | ALLOWED_UNPATCHED_SESSION_TARGETS
    missing = importers - covered
    assert not missing, _format_failure(
        "create_session", missing, "ALLOWED_UNPATCHED_SESSION_TARGETS"
    )


def test_create_task_importers_are_patched() -> None:
    """Every module-level ``create_task_with_error_logging`` importer is patched or allowlisted."""
    importers = _collect_module_level_importers(_BG_SOURCE_MODULE, _BG_NAME)
    covered = _bg_target_union() | ALLOWED_UNPATCHED_BG_TARGETS
    missing = importers - covered
    assert not missing, _format_failure(
        "create_task_with_error_logging", missing, "ALLOWED_UNPATCHED_BG_TARGETS"
    )


def test_allowlist_entries_are_real_importers() -> None:
    """Allowlist entries must correspond to actual module-level importers.

    Prevents the allowlist from rotting: if an allowlisted module stops importing
    create_session / create_task_with_error_logging (or is patched after all),
    the stale entry must be removed.
    """
    session_importers = _collect_module_level_importers(
        _SESSION_SOURCE_MODULE, _SESSION_NAME
    )
    bg_importers = _collect_module_level_importers(_BG_SOURCE_MODULE, _BG_NAME)

    stale_session = ALLOWED_UNPATCHED_SESSION_TARGETS - session_importers
    stale_bg = ALLOWED_UNPATCHED_BG_TARGETS - bg_importers
    assert not stale_session, (
        f"Stale ALLOWED_UNPATCHED_SESSION_TARGETS entries (no longer module-level "
        f"create_session importers): {sorted(stale_session)}"
    )
    assert not stale_bg, (
        f"Stale ALLOWED_UNPATCHED_BG_TARGETS entries (no longer module-level "
        f"create_task_with_error_logging importers): {sorted(stale_bg)}"
    )
