"""
Architecture drift test — ``routing_trace`` must not drag in ``app.*``.

WHY THIS EXISTS
---------------
``app/services/routing/__init__.py`` carries a hand-written layering
prohibition: ``app/agents/provider_manager.py`` (and
``app/agents/app_agent_router.py``) import ``routing_trace`` directly, and
``app/agents/`` sits *below* ``app/services/`` in the dependency order —
several services (``ai_functions``, ``identity``, ``agents``) import from
``app/agents/``. That inversion is harmless *today* only because
``routing_trace.py`` imports nothing but the standard library.

Auto Routing Tuning's plan (``docs/plans/auto_routing_tuning_plan.md`` §5)
adds a second module to the same package: ``routing_trace_service.py``, with
models and a database session behind it. It is wired in for real — the
routing hot path reaches ``RoutingTraceService.persist`` through
``channel_routing_service.py`` (Phase 3 moved the call there with the
``decide()`` split; ``channel_inbound_service.py`` no longer imports it at
all), ``routing_tuning_service.py`` reads and writes through it, and
``app/api/routes/admin_routing.py`` reads through it — but it is still
imported by its full module path, never
re-exported from this package's ``__init__`` (see the package docstring). If
anyone ever re-exports that module from ``app/services/routing/__init__.py``,
or ``routing_trace`` starts importing it directly, the harmless inversion
above becomes a real import cycle: ``app.agents`` -> ``app.services.routing``
-> (models / db / settings) -> ... -> back into ``app.services`` /
``app.agents``. This test is the trip-wire for exactly that regression.

WHAT IT ENFORCES
----------------
1. Importing ``app.services.routing.routing_trace`` in a clean interpreter
   must not cause any ``app.*`` module *other than the package path leading
   to it* (``app``, ``app.services``, ``app.services.routing``, and the
   module itself) to appear in ``sys.modules``.
2. ``app/services/routing/__init__.py`` contains no ``import`` /
   ``from ... import`` statements at all — the package's own "must not
   re-export anything" rule, enforced structurally rather than by
   convention.

HOW IT WORKS (enforcement #1)
------------------------------
A regex over the source text for ``^from app\\.`` would pass happily even if
a nested import inside a hot function pulled in the same modules at call
time — the damage (an ``app.agents`` -> ``app.services`` -> back to
``app.agents``-ish cycle) is identical either way, and it would be invisible
to source-text scanning. So instead of scanning text, this test spawns a
**fresh subprocess** (the current interpreter, ``sys.executable``) that does
nothing but import the target module and report which ``app.*`` entries
``sys.modules`` gained. A subprocess is required — not just a bare import in
this test process — because by the time any test in this suite runs, the
root ``conftest.py`` has already done ``from app.main import app``, which
has already pulled in the entire application (including
``app.agents.provider_manager``, which already imports ``routing_trace``).
Importing in *this* process would therefore always report zero new modules,
regardless of whether ``routing_trace`` is actually clean — a false pass.
"""
from __future__ import annotations

import ast
import json
import pathlib
import subprocess
import sys

# ── Roots ──────────────────────────────────────────────────────────────────────

_HERE = pathlib.Path(__file__).resolve()
_BACKEND_ROOT = _HERE.parent.parent.parent  # backend/
_ROUTING_INIT = _BACKEND_ROOT / "app" / "services" / "routing" / "__init__.py"

# The only app.* module names a clean import of routing_trace is allowed to
# introduce: the package path leading to it, plus the module itself. Nothing
# else — in particular, no app.core.*, app.models.*, app.crud, or (once it
# exists as a live import path) app.services.routing.routing_trace_service.
_ALLOWED_INTRODUCED_MODULES = frozenset(
    {
        "app",
        "app.services",
        "app.services.routing",
        "app.services.routing.routing_trace",
    }
)

# Minimal, self-contained probe script run in a brand-new interpreter. Must
# not import anything from the test suite itself — this runs as a standalone
# process, not under pytest.
_PROBE_SCRIPT = """
import json
import sys

before = {m for m in sys.modules if m == "app" or m.startswith("app.")}
import app.services.routing.routing_trace  # noqa: F401
after = {m for m in sys.modules if m == "app" or m.startswith("app.")}

print(json.dumps({
    "before": sorted(before),
    "introduced": sorted(after - before),
}))
"""


def _run_probe() -> dict:
    """Run ``_PROBE_SCRIPT`` in a fresh subprocess and parse its JSON report."""
    result = subprocess.run(
        [sys.executable, "-c", _PROBE_SCRIPT],
        cwd=_BACKEND_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"Probe subprocess failed importing app.services.routing.routing_trace "
        f"in a clean interpreter.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    # The probe prints exactly one JSON line; be defensive about stray output
    # (e.g. a warning) by taking the last non-empty line.
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, f"Probe subprocess produced no output.\nstderr:\n{result.stderr}"
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError as exc:  # pragma: no cover - diagnostic path
        raise AssertionError(
            f"Could not parse probe output as JSON: {lines[-1]!r}\n"
            f"Full stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        ) from exc


# ── Tests ──────────────────────────────────────────────────────────────────────


def test_importing_routing_trace_introduces_no_other_app_modules() -> None:
    """Importing ``app.services.routing.routing_trace`` fresh must not drag
    any ``app.*`` module into ``sys.modules`` beyond its own package path.

    This is the graph-based check: it asserts on what Python's import system
    actually did, not on what the source text of ``routing_trace.py`` looks
    like. A future refactor that moves the same dependency behind a
    function-local import, a lazy property, or an indirection through a
    third module would be caught here exactly as it would be caught by a
    direct top-level ``import app.core.db`` — both land the same modules in
    ``sys.modules``.
    """
    report = _run_probe()

    # Sanity: a genuinely fresh interpreter starts with no `app.*` loaded at
    # all. If this ever fails, the probe script itself is contaminated
    # (e.g. a sitecustomize.py or .pth file eagerly importing the app) and
    # the "introduced" delta below is not trustworthy.
    assert report["before"] == [], (
        f"Fresh interpreter already had app.* modules loaded before the "
        f"import under test: {report['before']}"
    )

    introduced = set(report["introduced"])
    disallowed = introduced - _ALLOWED_INTRODUCED_MODULES

    assert not disallowed, (
        f"Importing app.services.routing.routing_trace pulled in additional "
        f"app.* module(s): {sorted(disallowed)}\n\n"
        f"routing_trace.py must depend on nothing but the standard library — "
        f"see its module docstring and the layering-prohibition comment in "
        f"app/services/routing/__init__.py. app/agents/provider_manager.py "
        f"imports routing_trace directly, and app/agents/ sits BELOW "
        f"app/services/; the only reason that inversion is safe today is "
        f"that routing_trace pulls in nothing else. If Phase 2's "
        f"routing_trace_service.py (models + DB session) leaked into this "
        f"import path — directly, or via a re-export in "
        f"app/services/routing/__init__.py — the import cycle would go "
        f"live. Do not import routing_trace_service from routing_trace, and "
        f"do not add anything to app/services/routing/__init__.py."
    )

    # The package path itself must actually have been introduced (i.e. the
    # probe really did perform a fresh import, not a no-op).
    assert {
        "app",
        "app.services",
        "app.services.routing",
        "app.services.routing.routing_trace",
    } <= introduced


def test_routing_package_init_has_no_imports() -> None:
    """``app/services/routing/__init__.py`` must contain zero import
    statements — the package's documented "must not re-export anything"
    rule, checked structurally so a future contributor can't quietly add a
    re-export (e.g. ``from .routing_trace import RoutingTrace`` for
    convenience) without this test catching it.
    """
    assert _ROUTING_INIT.is_file(), f"Expected {_ROUTING_INIT} to exist"
    tree = ast.parse(_ROUTING_INIT.read_text(encoding="utf-8"))

    import_nodes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert not import_nodes, (
        f"app/services/routing/__init__.py must not import anything (it "
        f"must not re-export routing_trace or routing_trace_service — see "
        f"the package docstring for why), but found: "
        f"{[ast.dump(n) for n in import_nodes]}"
    )
