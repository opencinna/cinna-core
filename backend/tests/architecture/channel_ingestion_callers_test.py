"""
§9.4 Architecture contract test — ChannelIngestionService caller count.

Enforces the ≥2-callers rule from the channel ingestion service plan
(docs/drafts/channel-ingestion-service_plan.md §1.1 and §9.4).

CURRENT THRESHOLD: caller_modules >= 2 — steady state, production callers only.

After the Phase 1 debug route was removed at the end of Phase 5, the
service is exercised exclusively by production code:
    * `ingest_inbound_message`: A2A handler, cron scheduler, input-task
      executor (3 modules).
    * `resolve_or_create_session`: sessions route, A2A handler, App MCP
      handler (3 modules).
    * `assert_access`: A2A handler, App MCP handler (2 modules). The
      scheduler and task executor reach `assert_access` transitively via
      `ingest_inbound_message`, but the contract test counts direct
      `ChannelIngestionService.<method>(` calls only.

The threshold can be tightened further if every method gains a third
direct caller; until then, >= 2 is the §9.4 production guardrail.

How it works:
  1. Walk backend/app/ recursively (excludes the definition site and test files).
  2. For each .py file, scan the text for calls to the target method:
         ChannelIngestionService.<method>(
     or for an import-then-call pattern (the file imports ChannelIngestionService
     and calls the method by bare name). Simple substring scanning is used
     intentionally — no AST. The patterns are stable enough that regex / substring
     matching is reliable and fast.
  3. Collect the set of distinct MODULE file paths that contain at least one call.
     Two calls in the same file count as one module.
  4. Assert len(caller_modules) >= EXPECTED_CALLER_THRESHOLD.

Parametrize over the three public methods so failures are per-method and
the output clearly names which method has too few callers.
"""
from __future__ import annotations

import pathlib
import re

import pytest


# ── Constants ──────────────────────────────────────────────────────────────────

# The app source root to search.
APP_ROOT = pathlib.Path(__file__).parent.parent.parent / "app"

# The definition site — excluded so the service does not count as its own caller.
DEFINITION_SITE = APP_ROOT / "services" / "sessions" / "channel_ingestion_service.py"

# Phase 3 steady state. A2A + App MCP are both live callers; this is the
# §9.4 production guardrail enforcing the ≥2-callers rule.
EXPECTED_CALLER_THRESHOLD = 2

# Public methods to check.  Add / remove here if the service surface changes.
PUBLIC_METHODS = [
    "ingest_inbound_message",
    "resolve_or_create_session",
    "assert_access",
]

# Patterns that indicate a file calls a given method. We match both:
#   ChannelIngestionService.<method>(   — direct class.method call
#   .<method>(                          — may occur after "from ... import ChannelIngestionService"
#                                         and then method call as an attribute access
#
# We use the simple unambiguous form: look for `ChannelIngestionService.<method>`
# in the file text. If a file imports the class and then calls its methods
# that way, it will match. If it renames on import we'd miss it, but that
# would itself be a code-style violation in this project.
def _pattern_for_method(method: str) -> re.Pattern[str]:
    return re.compile(
        r"\bChannelIngestionService\." + re.escape(method) + r"\s*\(",
        re.MULTILINE,
    )


# ── File-walker ───────────────────────────────────────────────────────────────


def _collect_caller_modules(method: str) -> list[pathlib.Path]:
    """Return the list of distinct .py files under APP_ROOT that call `method`.

    Excludes:
      - The definition site itself.
      - Files ending in _test.py (defense-in-depth against scope drift).
      - Any file not under APP_ROOT (tests/ and alembic/ are outside APP_ROOT).
    """
    pattern = _pattern_for_method(method)
    callers: list[pathlib.Path] = []

    for path in sorted(APP_ROOT.rglob("*.py")):
        # Always exclude the definition site.
        if path.resolve() == DEFINITION_SITE.resolve():
            continue
        # Exclude test files (belt-and-suspenders: tests/ is outside APP_ROOT anyway).
        if path.name.endswith("_test.py") or path.name.startswith("test_"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if pattern.search(text):
            callers.append(path)

    return callers


# ── Parametrized test ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("method", PUBLIC_METHODS)
def test_channel_ingestion_service_has_enough_callers(method: str) -> None:
    """
    Each public method on ChannelIngestionService must have at least
    EXPECTED_CALLER_THRESHOLD distinct caller modules under backend/app/.

    Phase 3 steady-state threshold = 2 (A2A and App MCP are both live callers).

    On failure the error message lists the modules found so diagnosis is easy.
    """
    caller_modules = _collect_caller_modules(method)
    n = len(caller_modules)

    # Build a human-readable module list for failure output.
    found_str = "\n  ".join(str(p.relative_to(APP_ROOT.parent)) for p in caller_modules)
    not_found_msg = (
        f"ChannelIngestionService.{method} has {n} caller module(s) under "
        f"backend/app/ (excluding the definition site); "
        f"expected >= {EXPECTED_CALLER_THRESHOLD}.\n"
        f"Found modules:\n  {found_str if found_str else '(none)'}\n\n"
        f"Expected callers (Phase 3 steady state):\n"
        f"  backend/app/services/a2a/a2a_request_handler.py\n"
        f"  backend/app/services/app_mcp/app_mcp_request_handler.py\n"
        f"plus the debug route backend/app/api/routes/_test_channel_ingestion.py.\n"
        f"If a real caller is missing, restore the migration. If the threshold "
        f"is wrong, update EXPECTED_CALLER_THRESHOLD."
    )

    assert n >= EXPECTED_CALLER_THRESHOLD, not_found_msg
