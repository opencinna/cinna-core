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

import functools
import os
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

# Directory names pruned from the walk. `app/env-templates/` ships bundled
# virtualenvs (.venv/site-packages) with ~10k vendored .py files — reading all
# of them per method made this test take seconds. None of them are production
# callers, so skip the whole subtree. __pycache__ is byte-compiled noise.
EXCLUDED_DIR_NAMES = frozenset({"env-templates", ".venv", "site-packages", "__pycache__", "node_modules"})

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


@functools.lru_cache(maxsize=1)
def _source_files() -> tuple[tuple[pathlib.Path, str], ...]:
    """Read every candidate .py file under APP_ROOT exactly once.

    Cached so the three parametrized methods share a single filesystem pass —
    reads over the macOS Docker bind mount are the dominant cost (~3ms/file),
    so reading once instead of per-method is a ~3x speedup.

    Excludes:
      - The definition site itself (so the service is not its own caller).
      - Files ending in _test.py / starting with test_ (scope-drift guard).
      - Vendored / generated subtrees (see EXCLUDED_DIR_NAMES) — most notably
        the bundled virtualenvs under app/env-templates/ (~10k files).
    """
    definition_site = DEFINITION_SITE.resolve()
    files: list[tuple[pathlib.Path, str]] = []

    for dirpath, dirnames, filenames in os.walk(APP_ROOT):
        # Prune excluded subtrees in place so os.walk never descends into them
        # (avoids stat-ing / reading ~10k vendored site-package files).
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIR_NAMES]

        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            # Exclude test files (belt-and-suspenders: tests/ is outside APP_ROOT anyway).
            if filename.endswith("_test.py") or filename.startswith("test_"):
                continue
            path = pathlib.Path(dirpath) / filename
            # Always exclude the definition site.
            if path.resolve() == definition_site:
                continue
            try:
                files.append((path, path.read_text(encoding="utf-8")))
            except (OSError, UnicodeDecodeError):
                continue

    return tuple(sorted(files, key=lambda pt: pt[0]))


def _collect_caller_modules(method: str) -> list[pathlib.Path]:
    """Return the list of distinct .py files under APP_ROOT that call `method`."""
    pattern = _pattern_for_method(method)
    return [path for path, text in _source_files() if pattern.search(text)]


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
        f"Expected production callers (steady state):\n"
        f"  backend/app/services/a2a/a2a_request_handler.py\n"
        f"  backend/app/services/app_mcp/app_mcp_request_handler.py\n"
        f"(the cron scheduler and input-task executor reach ingest_inbound_message "
        f"as additional direct callers).\n"
        f"If a real caller was removed, that is the regression — restore it. If the "
        f"threshold is wrong, update EXPECTED_CALLER_THRESHOLD."
    )

    assert n >= EXPECTED_CALLER_THRESHOLD, not_found_msg
