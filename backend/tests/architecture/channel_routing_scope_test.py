"""Architecture drift test — channel routing must not re-converge on App MCP.

WHY THIS EXISTS
---------------
``docs/plans/channel_routing_scope_split_plan.md`` §3: each routing surface owns
its **candidate provider**. A Server Channel routes over the agents the sender
*owns*; the App MCP server routes over
``AppAgentRouteService.get_effective_routes_for_user``, which answers a
different question — "what can this user address through the App MCP server" —
and gates it on ``channel_app_mcp``, ``AppAgentRoute.is_active`` and
``AppAgentRouteAssignment.is_enabled``. The only things legitimately shared
across the two surfaces are ``AgentClassifier.classify`` and ``RoutingTrace``.

Channel Pass 1 used to call ``AppMCPRoutingService.route_message`` and then try
to correct the answer subtractively. That is the reported bug (§1): the sender's
own standalone agent was absent from the ballot because standalone agents never
get an auto-route, while an admin route and an identity contact pointing at
*somebody else's* agents were on it and could win. Three App MCP enablement
toggles silently governed what the owner could reach from their own chat app
(§2.2).

The fix was structural — a provider of its own — and its whole value is that it
cannot silently come back. "Nothing here imports App MCP routing" is a property
of a diff, and a property of a diff is a property nobody checks on the Friday
somebody adds ``from ...app_mcp_routing_service import AppMCPRoutingService``
to reuse "just the pattern matching part". Plan §5, Phase 5: make it a test, not
a convention.

WHAT IT ENFORCES
----------------
Neither ``channel_routing_service.py`` nor ``channel_candidate_provider.py``
imports ``AppMCPRoutingService`` or ``AppAgentRouteService`` — by symbol, by
module path, at module level or nested inside a function.

RELATIONSHIP TO ``channel_routing_purity_test.py``
--------------------------------------------------
Sibling, not an extension of it, and deliberately so. That file executes the
four *structural facts* ``channel_routing_service``'s own docstring claims about
``decide()`` having no side effects, and its docstring makes "four facts, four
tests" load-bearing — a fifth test in there about something else would blunt
exactly the invariant it is holding. This is a different guarantee (where the
candidate set comes from), from a different plan, over a module set that
includes a file the purity test has no business parsing.

WHY AST AND NOT A GREP
----------------------
Following the section of the same name in ``channel_routing_purity_test.py``,
and here it bites immediately rather than hypothetically: **both** guarded
modules name both guarded services in prose, precisely to explain why they are
absent. ``_route_installed``'s docstring walks through what
``AppMCPRoutingService.route_message`` used to return and why its ``only_one``
short-circuit came back in a different, conditional form;
``channel_candidate_provider``'s module docstring opens by contrasting itself
with ``AppAgentRouteService.get_effective_routes_for_user``. (Cited by symbol
rather than by line number on purpose — a line citation in a test that exists to
survive refactors is the first thing a refactor invalidates.) A text scan would fail
on correct code — and would then be "fixed" by deleting the explanations, which
is the worst possible outcome for a seam whose only defence is that somebody
understood it.

So this asserts on **import nodes only**, not on ``Name``/``Attribute`` the way
the purity test's name blocklist does. That is narrower on purpose. A bare
``AppMCPRoutingService`` reference cannot exist without an import in the same
module, so the narrower check loses nothing real, and it states the guarantee in
the words the plan states it in: these modules do not import that.

Nested imports are covered explicitly. They are not an edge case here: this
codebase breaks cycles with function-level imports as a matter of course, and
the call this test exists to forbid — the one Phase 3 deleted — *was* a nested
``from app.services.app_mcp.app_mcp_routing_service import AppMCPRoutingService``
inside ``_route_installed``. A module-level-only walk would have missed the
exact regression.
"""
from __future__ import annotations

import ast
import pathlib

_HERE = pathlib.Path(__file__).resolve()
_BACKEND_ROOT = _HERE.parent.parent.parent  # backend/

#: The two modules that make up the channel candidate path.
_GUARDED_MODULES: tuple[pathlib.Path, ...] = (
    _BACKEND_ROOT
    / "app"
    / "services"
    / "server_channels"
    / "channel_routing_service.py",
    _BACKEND_ROOT / "app" / "services" / "routing" / "channel_candidate_provider.py",
)

#: Imported *symbols* that would put an App MCP candidate set back in reach.
#: Each maps to what importing it would undo, so a failure says which property
#: just broke rather than only "forbidden import".
_FORBIDDEN_SYMBOLS: dict[str, str] = {
    "AppMCPRoutingService": (
        "is the App MCP surface's whole Pass 1. Calling it hands channel "
        "routing a ballot built from AppAgentRoute rows — which omits the "
        "sender's own standalone agents and includes other people's agents by "
        "way of admin routes and identity contacts. That is plan §1's reported "
        "incident, exactly"
    ),
    "AppAgentRouteService": (
        "owns get_effective_routes_for_user and the App MCP enablement "
        "toggles (channel_app_mcp, is_active, assignment is_enabled). A "
        "channel must not inherit them: what a user chooses to expose over MCP "
        "is not a statement about what they can reach from their own chat app "
        "(plan §2.2)"
    ),
}

#: The modules those symbols live in, so ``import ... as svc`` followed by
#: ``svc.AppAgentRouteService`` cannot slip past a symbol-name check.
_FORBIDDEN_MODULES: dict[str, str] = {
    "app.services.app_mcp.app_mcp_routing_service": (
        "is the App MCP router module; importing it at all puts "
        "AppMCPRoutingService one attribute access away"
    ),
    "app.services.app_mcp.app_agent_route_service": (
        "is the App MCP route module; importing it at all puts "
        "AppAgentRouteService one attribute access away"
    ),
}

_REMEDY = (
    "\n\nEach routing surface owns its candidate provider (plan §3). Channel "
    "candidates come from ChannelCandidateProvider.build, which selects the "
    "sender's OWN agents and reads no route, no assignment and no "
    "channel_app_mcp flag. The only things shared across surfaces are "
    "AgentClassifier.classify and RoutingTrace — if what you need is "
    "classification, import AgentClassifier; if it is route data, it belongs "
    "on the App MCP path, not here.\n\n"
    "Both modules mention these names in prose on purpose, explaining why they "
    "are absent. This test reads import nodes, not text, so leave the "
    "explanations alone."
)


def _imports(tree: ast.Module) -> tuple[set[str], set[str]]:
    """``(symbols, modules)`` this file imports, top-level or nested.

    Symbols are what a ``from X import Y`` binds (``Y``, plus any ``as`` alias);
    modules are the dotted paths on both statement forms. Both are collected
    because either alone is a hole: a symbol check misses
    ``import app...app_agent_route_service as svc``, and a module check misses
    a re-export imported from somewhere else.
    """
    symbols: set[str] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
                symbols.add(alias.asname or alias.name.rsplit(".", 1)[-1])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(node.module)
            for alias in node.names:
                symbols.add(alias.name)
                if alias.asname:
                    symbols.add(alias.asname)
    return symbols, modules


def test_channel_routing_never_imports_the_app_mcp_candidate_set() -> None:
    """Neither channel module imports AppMCPRoutingService/AppAgentRouteService."""
    offenders: list[str] = []
    for path in _GUARDED_MODULES:
        assert path.is_file(), f"Expected {path} to exist"
        symbols, modules = _imports(ast.parse(path.read_text(encoding="utf-8")))
        rel = path.relative_to(_BACKEND_ROOT)
        for name, why in _FORBIDDEN_SYMBOLS.items():
            if name in symbols:
                offenders.append(f"  - {rel} imports {name}, which {why}.")
        for name, why in _FORBIDDEN_MODULES.items():
            if name in modules:
                offenders.append(f"  - {rel} imports {name}, which {why}.")

    assert not offenders, (
        "Channel routing has re-converged on the App MCP candidate set:\n"
        + "\n".join(sorted(offenders))
        + _REMEDY
    )
