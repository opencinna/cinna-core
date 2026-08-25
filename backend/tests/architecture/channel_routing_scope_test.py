"""Architecture drift test — channel routing must not re-converge on App MCP.

WHY THIS EXISTS
---------------
``docs/plans/channel_routing_scope_split_plan.md`` §3: each routing surface owns
its **candidate provider**. A Server Channel routes over the agents the sender
*owns*, built by ``ChannelCandidateProvider``. Since
``docs/plans/channels_identity_unification/phase_5_app_mcp_channel.md``, App MCP
is itself a channel and composes the *same* ``ChannelCandidateProvider`` (plus
``IdentityCandidateProvider`` when ``allow_identity_routing`` allows it) —
``AppAgentRouteService`` and the whole ``AppAgentRoute`` family it used to gate
on (``channel_app_mcp``, route ``is_active``, assignment ``is_enabled``) are
deleted. What remains a live risk is ``AppMCPRoutingService.route_message``
itself: it is still App MCP's own thin composition layer over the two
candidate providers plus ``AgentClassifier.classify``, and channel routing must
not call it directly — that would hand channel routing App MCP's specific
composition (its own gating, its own trace shape) instead of building its own
candidate set the way it is meant to.

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
imports ``AppMCPRoutingService`` — by symbol, by module path, at module level
or nested inside a function.

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
modules name ``AppMCPRoutingService`` in prose, precisely to explain why it is
absent from the imports. ``_route_installed``'s docstring walks through what
``AppMCPRoutingService.route_message`` used to return and why its ``only_one``
short-circuit came back in a different, conditional form;
``channel_candidate_provider``'s module docstring opens by contrasting itself
with the same service (and, historically, with the now-deleted
``AppAgentRouteService.get_effective_routes_for_user`` it used to call
through). (Cited by symbol rather than by line number on purpose — a line
citation in a test that exists to survive refactors is the first thing a
refactor invalidates.) A text scan would fail on correct code — and would then
be "fixed" by deleting the explanations, which is the worst possible outcome
for a seam whose only defence is that somebody understood it.

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
from typing import NamedTuple

import pytest

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
        "is App MCP's own composition layer (ChannelCandidateProvider + "
        "IdentityCandidateProvider + AgentClassifier.classify, per phase 5 of "
        "docs/plans/channels_identity_unification/). Calling it from channel "
        "routing hands the channel App MCP's specific gating and trace shape "
        "instead of building the channel's own candidate set — the same "
        "cross-surface coupling that produced plan §1's reported incident, in "
        "a new shape"
    ),
}

#: The modules those symbols live in, so ``import ... as svc`` followed by
#: ``svc.AppMCPRoutingService`` cannot slip past a symbol-name check.
_FORBIDDEN_MODULES: dict[str, str] = {
    "app.services.app_mcp.app_mcp_routing_service": (
        "is the App MCP router module; importing it at all puts "
        "AppMCPRoutingService one attribute access away"
    ),
}

_REMEDY = (
    "\n\nEach routing surface owns its candidate provider (plan §3). Channel "
    "candidates come from ChannelCandidateProvider.build, which selects the "
    "sender's OWN agents. App MCP composes the same provider (plus "
    "IdentityCandidateProvider when allow_identity_routing allows it) on its "
    "own path — a channel module must build its own candidate set, not reach "
    "into App MCP's composition for it. The only things shared across "
    "surfaces are AgentClassifier.classify and RoutingTrace — if what you "
    "need is classification, import AgentClassifier.\n\n"
    "Both modules mention AppMCPRoutingService in prose on purpose, "
    "explaining why it is absent. This test reads import nodes, not text, so "
    "leave the explanations alone."
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
    """Neither channel module imports AppMCPRoutingService."""
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


# ===========================================================================
# Phase 7 §2.2 — the other provider/consumer pairs
# ===========================================================================
#
# WHY THIS SECTION EXISTS
# -----------------------
# The test above guards exactly ONE pair, the one the scope-split plan
# produced: channel routing must not import App MCP's composition layer. By the
# end of the channels & identity unification there are five more pairs of the
# same shape, because the refactor's whole direction was to make surfaces
# *compose* shared providers instead of borrowing each other's answers — and
# every new composition is a new place the borrowing can come back.
#
# A cross-phase review (phase 7 §2.2) enumerated them; each was verified
# against the tree as clean before being written down here, so every entry
# below is a property the code has today, not an aspiration.
#
# HOW THIS SECTION IS STRUCTURED, AND WHY
# ---------------------------------------
# One ``_Boundary`` record per pair, parameterized into one case per
# (boundary, file), following master plan §2.14's ruling for the sibling purity
# test: these grow by adding a *record*, never by copying an assertion body.
# The record carries the whole of what differs — which files, which symbols,
# which modules, and the remedy sentence a failure prints — so there is one
# checker and no family of near-identical ones to drift apart.
#
# The original test above is deliberately left as it is rather than folded in.
# It states the pair the plan states, in that plan's words, and it is the one
# guard that has already earned its keep; re-expressing it as a record would
# buy symmetry at the price of the history in its failure message.
#
# STILL AST, FOR THE REASON IN "WHY AST AND NOT A GREP" ABOVE
# -----------------------------------------------------------
# Every addition here reads import nodes and call nodes, never text, and that
# is load-bearing in exactly the same way: ``channel_candidate_provider``'s
# module docstring names ``IdentityAgentBinding`` in prose in order to promise
# it never reads one, and ``external_agent_catalog_service`` names
# ``IdentityCandidateProvider._contact_examples`` in prose to explain that its
# own helper is a near-twin. A text scan fails on both — on *correct* code —
# and the fix a hurried reader reaches for is deleting the explanation, which
# leaves the seam undefended and undocumented at once.


_APP = _BACKEND_ROOT / "app"
_APP_MCP_ROUTING = _APP / "services" / "app_mcp" / "app_mcp_routing_service.py"
_APP_MCP_PROMPTS = _APP / "mcp" / "app_prompts.py"
_IDENTITY_PROVIDER = _APP / "services" / "routing" / "identity_candidate_provider.py"
_IDENTITY_ROUTING = _APP / "services" / "identity" / "identity_routing_service.py"
_CHANNEL_PROVIDER = _APP / "services" / "routing" / "channel_candidate_provider.py"
_EXTERNAL_A2A = _APP / "services" / "external" / "external_a2a_request_handler.py"
_EXTERNAL_CATALOG = _APP / "services" / "external" / "external_agent_catalog_service.py"

_POLICY_MODULE_PATH = "app.services.server_channels.channel_policy_service"


class _Boundary(NamedTuple):
    """One provider/consumer seam, and what crossing it would undo.

    ``symbols`` and ``modules`` are the two halves the original guard above
    already uses, for the reason ``_imports`` gives: neither alone is a fence.

    ``only_from`` is the third, and it exists because one seam is not a plain
    "never touch that module". ``identity_candidate_provider`` legitimately
    imports ``ResolvedChannelPolicy`` — the frozen value it is *handed* — from
    the very module whose ``ChannelPolicyService`` it must never call. The
    distinction is the whole point of the seam (receiving a resolved policy is
    composition; resolving one is the shared toggle read this refactor
    exists to prevent), so it is expressed as an allowlist over that module's
    imported names rather than smudged into a blanket permission. A bare
    ``import <module>`` form is refused outright by the same rule: it binds no
    names to allowlist and puts the service one attribute away.
    """

    id: str
    paths: tuple[pathlib.Path, ...]
    symbols: dict[str, str]
    modules: dict[str, str]
    remedy: str
    only_from: tuple[tuple[str, frozenset[str]], ...] = ()


_BOUNDARIES: tuple[_Boundary, ...] = (
    _Boundary(
        id="app-mcp-routing-does-not-borrow-the-channel-pipeline",
        paths=(_APP_MCP_ROUTING,),
        symbols={
            "ChannelRoutingService": (
                "is the Server Channel router — its passes, its pinned-agent "
                "arm, its Pass-2 catalog auto-install and its two-pass trace "
                "merge. App MCP is a channel by policy (phase 5), not by "
                "pipeline: it composes the same two providers itself and has "
                "no thread binding, no auto-install pass and no outbound reply"
            ),
            "ChannelInboundService": (
                "is the effect half of the channel pipeline — bindings, "
                "sessions, parked messages, outbound replies. App MCP's "
                "handler owns its own session lifecycle; reaching into this "
                "would give one surface two of them"
            ),
        },
        modules={
            "app.services.server_channels.channel_routing_service": (
                "is the channel router module; importing it at all puts "
                "ChannelRoutingService one attribute access away"
            ),
            "app.services.server_channels.channel_inbound_service": (
                "is the channel effect module; importing it at all puts "
                "ChannelInboundService one attribute access away"
            ),
        },
        remedy=(
            "\n\nThis is the mirror image of the guard at the top of this "
            "file, and it became a live risk the moment App MCP became a "
            "ServerChannel: the two surfaces now share ChannelPolicyService "
            "and both candidate providers, which makes 'and the router too' "
            "look like the next obvious step. It is not. Providers compose; "
            "surfaces do not borrow (master plan §3.1). What is legitimately "
            "shared is ChannelPolicyService.resolve, ChannelCandidateProvider, "
            "IdentityCandidateProvider, AgentClassifier.classify and "
            "RoutingTrace — import those."
        ),
    ),
    _Boundary(
        id="app-mcp-prompts-does-not-re-derive-policy",
        paths=(_APP_MCP_PROMPTS,),
        symbols={
            "ChannelUserSetting": (
                "is the per-user override row. Reading it here would re-derive "
                "the caller's channel policy independently of "
                "ChannelPolicyService, which owns every inherit rule — and "
                "'absence of the row means the channel default applies' "
                "(master plan §3.3) is exactly the rule a second reader gets "
                "wrong, silently, in the direction of showing more"
            ),
            "ChannelUserAgent": (
                "is the per-user agent-scope list. Same rule: agent_scope is "
                "already resolved onto ResolvedChannelPolicy, and a discovery "
                "list that re-derives it answers a different question from the "
                "router's — which is the one failure mode a prompts/list "
                "surface has"
            ),
            "ServerChannelUserGrant": (
                "is the restricted-channel allowlist row; availability is "
                "ChannelPolicyService's conjunction and a second copy of an "
                "availability rule is what that service's docstring forbids"
            ),
        },
        modules={
            "app.models.server_channels.channel_user_setting": (
                "holds ChannelUserSetting and ChannelUserAgent; importing it "
                "at all puts a hand-rolled policy read one query away"
            ),
            "app.models.server_channels.server_channel_user_grant": (
                "holds ServerChannelUserGrant; same"
            ),
        },
        remedy=(
            "\n\nprompts/list must ask the router's question or teach the "
            "client a vocabulary the router refuses. It does that by resolving "
            "ONE policy through ChannelPolicyService.resolve and handing that "
            "same object to both candidate providers — including "
            "IdentityCandidateProvider, which since phase 7 enforces the "
            "allow_identity_routing consent gate itself. There is no 'if' "
            "here to drop and no row here to read: pass the policy."
        ),
    ),
    _Boundary(
        id="identity-is-handed-a-policy-and-never-resolves-one",
        paths=(_IDENTITY_PROVIDER, _IDENTITY_ROUTING),
        symbols={
            "ChannelPolicyService": (
                "resolves a channel policy. Identity is now composed by THREE "
                "consumers (channel routing, App MCP routing, App MCP "
                "prompts/list), so a toggle resolved in here is a toggle all "
                "three read at once, whether or not it is the question their "
                "own surface was asking — the exact coupling that made three "
                "App MCP enablement switches govern a Google Chat sender's "
                "own agents (scope-split plan §2.2)"
            ),
            "AppMCPRoutingService": (
                "is one of identity's consumers. A provider that imports one "
                "of its consumers has stopped being a provider, and pins "
                "identity's behaviour to whichever surface got there first"
            ),
            "ChannelRoutingService": (
                "is another of identity's consumers; same, in the other "
                "direction — and it would close an import cycle, since "
                "channel routing composes this provider"
            ),
        },
        modules={
            "app.services.app_mcp.app_mcp_routing_service": (
                "is a consumer's module; importing it at all puts "
                "AppMCPRoutingService one attribute access away"
            ),
            "app.services.server_channels.channel_routing_service": (
                "is a consumer's module; same"
            ),
        },
        only_from=(
            (_POLICY_MODULE_PATH, frozenset({"ResolvedChannelPolicy"})),
        ),
        remedy=(
            "\n\nIdentity takes its policy as an argument: "
            "IdentityCandidateProvider.build(db, caller_user_id, "
            "policy=policy). ResolvedChannelPolicy — the frozen value — is the "
            "only name this seam may import from channel_policy_service; "
            "ChannelPolicyService, the resolver, is the shared toggle read. "
            "If a surface needs a policy identity does not have, resolve it "
            "at that surface and pass it in."
        ),
    ),
    _Boundary(
        id="channel-provider-reads-no-identity",
        paths=(_CHANNEL_PROVIDER,),
        symbols={
            "IdentityAgentBinding": (
                "is an identity row. This module's own docstring promises it "
                "reads none: a channel routes over the agents the sender OWNS, "
                "and an identity binding resolves to somebody else's agent — "
                "which could never produce a usable channel session but could, "
                "and did, win the classification and take the decision with it"
            ),
            "IdentityBindingAssignment": (
                "is the per-caller identity access row; same promise, same "
                "incident"
            ),
            "IdentityCandidateProvider": (
                "is the identity half of the ballot. Composing it HERE would "
                "make every consumer of this provider get identities whether "
                "or not their sender consented — the composition belongs at "
                "the surface, which is the one place that knows the policy"
            ),
            "IdentityService": (
                "is identity's read/write service; importing it puts every "
                "identity row within reach of a module that promises to read "
                "none of them"
            ),
        },
        modules={
            "app.models.identity.identity_models": (
                "holds the identity rows; importing it at all breaks the "
                "docstring's promise"
            ),
            "app.services.routing.identity_candidate_provider": (
                "is the identity provider module; importing it at all puts "
                "IdentityCandidateProvider one attribute access away"
            ),
            "app.services.identity.identity_service": (
                "is identity's service module; same"
            ),
        },
        remedy=(
            "\n\nThe two providers are siblings that COMPOSE at the call site "
            "— owned agents first, identities after — and neither calls the "
            "other. That separation is what lets a surface offer one without "
            "the other, which is what allow_identity_routing switches. This "
            "module names IdentityAgentBinding in prose precisely to promise "
            "it never reads one; this test reads import nodes, not text, so "
            "leave the promise written down."
        ),
    ),
    _Boundary(
        id="external-a2a-does-not-borrow-channel-routing",
        paths=(_EXTERNAL_A2A, _EXTERNAL_CATALOG),
        symbols={
            "ChannelCandidateProvider": (
                "builds the candidate set for a SERVER CHANNEL sender — the "
                "agents that sender owns, narrowed by that channel's resolved "
                "agent scope. An external A2A caller is not a channel sender: "
                "they are authorized by ExternalAccessPolicy against a "
                "published catalog, and borrowing a channel's ballot would "
                "hand them a set assembled from a different authorization "
                "question"
            ),
            "ChannelPolicyService": (
                "answers 'what may this person do on this channel'. There is "
                "no channel here, and ResolvedChannelPolicy.for_no_channel() "
                "is deliberately not a general-purpose permissive default for "
                "surfaces that never had one"
            ),
            "ChannelRoutingService": (
                "is the channel router, complete with thread bindings, "
                "auto-install and outbound replies. The external surface has "
                "its own handler and its own trace story"
            ),
            "ChannelInboundService": (
                "is the channel effect half; same, one hop further"
            ),
        },
        modules={
            "app.services.routing.channel_candidate_provider": (
                "is the channel provider module; importing it at all puts "
                "ChannelCandidateProvider one attribute access away"
            ),
            _POLICY_MODULE_PATH: (
                "is the channel policy module; importing it at all puts "
                "ChannelPolicyService one attribute access away"
            ),
            "app.services.server_channels.channel_routing_service": (
                "is the channel router module; same"
            ),
            "app.services.server_channels.channel_inbound_service": (
                "is the channel effect module; same"
            ),
        },
        remedy=(
            "\n\nThe external A2A surface reaches identity through "
            "IdentityService / IdentityRoutingService, which is the shared "
            "layer, and authorizes through ExternalAccessPolicy. It does not "
            "have, and must not acquire, a channel policy: 'which agents may "
            "this caller address' is answered by the published external "
            "catalog here, not by a ServerChannel row. Note that "
            "external_agent_catalog_service names "
            "IdentityCandidateProvider._contact_examples in prose to flag its "
            "own helper as a near-twin — this test reads import nodes, not "
            "text, so that note is safe and should stay."
        ),
    ),
)

#: One case per (boundary, file), so a failure names the file rather than the
#: group it was checked in.
_BOUNDARY_CASES: tuple[tuple[_Boundary, pathlib.Path], ...] = tuple(
    (boundary, path) for boundary in _BOUNDARIES for path in boundary.paths
)
_BOUNDARY_IDS = [
    f"{boundary.id}::{path.name}" for boundary, path in _BOUNDARY_CASES
]


def _module_imports_of(
    tree: ast.Module, module_path: str
) -> list[ast.Import | ast.ImportFrom]:
    """Every import statement in ``tree`` that touches ``module_path``."""
    hits: list[ast.Import | ast.ImportFrom] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == module_path for alias in node.names):
                hits.append(node)
        elif isinstance(node, ast.ImportFrom) and node.module == module_path:
            hits.append(node)
    return hits


@pytest.mark.parametrize(
    ("boundary", "path"), _BOUNDARY_CASES, ids=_BOUNDARY_IDS
)
def test_routing_surface_boundary_is_not_crossed(
    boundary: _Boundary, path: pathlib.Path
) -> None:
    """One provider/consumer seam, over one file (phase 7 §2.2)."""
    assert path.is_file(), f"Expected {path} to exist"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    symbols, modules = _imports(tree)
    rel = path.relative_to(_BACKEND_ROOT)

    offenders: list[str] = []
    for name, why in boundary.symbols.items():
        if name in symbols:
            offenders.append(f"  - {rel} imports {name}, which {why}.")
    for name, why in boundary.modules.items():
        if name in modules:
            offenders.append(f"  - {rel} imports {name}, which {why}.")

    for module_path, allowed in boundary.only_from:
        for node in _module_imports_of(tree, module_path):
            if isinstance(node, ast.Import):
                offenders.append(
                    f"  - {rel} imports the module {module_path} directly "
                    f"(line {node.lineno}). Only "
                    f"`from {module_path} import {', '.join(sorted(allowed))}` "
                    "is permitted here — a bare module import binds no name to "
                    "check and puts everything on it one attribute away."
                )
                continue
            taken = {alias.name for alias in node.names}
            if not taken <= allowed:
                offenders.append(
                    f"  - {rel} imports {sorted(taken - allowed)} from "
                    f"{module_path} (line {node.lineno}); only "
                    f"{sorted(allowed)} may be imported here."
                )

    assert not offenders, (
        f"Routing surface boundary '{boundary.id}' crossed:\n"
        + "\n".join(sorted(offenders))
        + boundary.remedy
    )


# ===========================================================================
# The consent gate, at every call site there is
# ===========================================================================

#: Directories under ``app/`` that are not this application's own source.
#: ``env-templates`` is a per-environment container template tree copied into
#: agent workspaces; ``alembic`` is migration history, which is allowed to
#: refer to a world that no longer exists.
_NOT_APP_SOURCE = ("env-templates", "alembic")

#: The number of production call sites the gate had when it moved into the
#: provider: channel routing, App MCP routing, App MCP prompts/list. Asserted
#: as a FLOOR, not an exact count — a fourth consumer is expected and is the
#: whole reason the gate moved — so that a walk which silently stops finding
#: calls (a rename, a wrapper, an AST shape this matcher does not model) fails
#: instead of passing over an empty set. A guard that cannot find its subject
#: is indistinguishable from a guard that is satisfied.
_MIN_IDENTITY_BUILD_CALL_SITES = 3


def _app_source_files() -> list[pathlib.Path]:
    return [
        path
        for path in sorted(_APP.rglob("*.py"))
        if not any(part in _NOT_APP_SOURCE for part in path.relative_to(_APP).parts)
    ]


def _identity_build_calls(tree: ast.Module) -> list[ast.Call]:
    """Every ``IdentityCandidateProvider.build(...)`` call in one module.

    Matched on the AST shape ``<anything ending in the class name>.build(...)``
    rather than on text, for this file's usual reason: several modules discuss
    the provider in prose, and one of them (``identity_candidate_provider``
    itself) documents this very call in its docstring.
    """
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "build":
            continue
        owner = func.value
        name = (
            owner.id
            if isinstance(owner, ast.Name)
            else owner.attr
            if isinstance(owner, ast.Attribute)
            else None
        )
        if name == "IdentityCandidateProvider":
            calls.append(node)
    return calls


def test_every_identity_candidate_build_call_passes_a_policy() -> None:
    """No production call site may ask for identity candidates ungated.

    ``allow_identity_routing`` is the sender's own consent that a message of
    theirs may open a session in **somebody else's workspace**, where that
    person can read it (master plan §3.4). Until phase 7 the check was an
    ``if`` written out at each of three surfaces; it now lives inside
    ``IdentityCandidateProvider.build``, which refuses when the policy it is
    handed says the caller has not opted in.

    That relocation only closes the hole if the policy actually arrives.
    ``policy`` is keyword-only with a permissive ``None`` default — kept, and
    argued for, in the provider's own docstring, because the provider's unit
    tests and the App MCP domain's documented "enter at ``build()`` directly"
    convention ask the channel-less question — so a new consumer that simply
    omits ``policy=`` would route a stranger into a stranger's workspace and
    look perfectly ordinary in review. This is the half of the guarantee a
    signature cannot carry: every call under ``app/`` names the policy.

    Deliberately a walk over the whole application tree rather than a fixed
    module list. The property being defended is about the call site that does
    not exist yet — the fourth consumer, added by somebody who never read the
    provider — and a list of today's three consumers cannot see it.
    """
    offenders: list[str] = []
    found = 0
    for path in _app_source_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover - not this test's subject
            continue
        for call in _identity_build_calls(tree):
            found += 1
            if not any(keyword.arg == "policy" for keyword in call.keywords):
                offenders.append(
                    f"  - {path.relative_to(_BACKEND_ROOT)}:{call.lineno} calls "
                    "IdentityCandidateProvider.build without policy=."
                )

    assert found >= _MIN_IDENTITY_BUILD_CALL_SITES, (
        f"Expected at least {_MIN_IDENTITY_BUILD_CALL_SITES} "
        f"IdentityCandidateProvider.build call sites under app/, found "
        f"{found}. This test asserts something about calls it can see, so "
        "finding none would make it pass while defending nothing. Either the "
        "provider is now reached some other way (a wrapper, a rename, an "
        "alias) — in which case teach _identity_build_calls that shape — or "
        "a consumer was deleted and this floor should come down in the same "
        "change that deleted it."
    )
    assert not offenders, (
        "Identity candidates requested without the sender's consent policy:\n"
        + "\n".join(sorted(offenders))
        + "\n\nPass the ResolvedChannelPolicy your surface already resolved: "
        "IdentityCandidateProvider.build(db, user_id, policy=policy). The "
        "provider enforces allow_identity_routing itself and returns an empty "
        "list — recording nothing, not even skips — when consent is off, so "
        "there is no 'if' for you to write and none for you to forget. "
        "Omitting the argument opts your surface OUT of the gate, which is "
        "the one outcome nobody would choose on purpose."
    )
