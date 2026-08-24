"""Architecture drift test — ``decide()`` must stay unable to have side effects.

WHY THIS EXISTS
---------------
Phase 3 of Auto Routing Tuning (``docs/plans/auto_routing_tuning_plan.md`` §6)
turns ``POST /admin/routing/simulate`` into a route that runs the real router
over a real user's real routing state and then does nothing with the answer: no
thread binding, no session, no bundle install, no outbound reply.

The plan is explicit that this must hold **by construction, not by a flag
threaded through 200 lines**. So there is no ``simulate=True`` parameter
anywhere: the decision was split out into
``app/services/server_channels/channel_routing_service.py``, and the effects
stayed in ``channel_inbound_service.py``. Simulate calls ``decide`` and stops.

That is only a structural guarantee for as long as the routing module cannot
*reach* an effect. It cannot today because the names are not in its namespace —
binding it would require adding an import, which is visible in a diff. This
test is the trip-wire that makes "visible in a diff" into "fails in CI",
because a plausible-looking future change ("while we're here, let's record the
binding in decide so the trace can link to it") would restore the exact hazard
the split removed, and every no-side-effects test in
``tests/api/routing/routing_simulate_no_side_effects_test.py`` would then be
asserting something that had quietly stopped being true of the design and was
only still true of today's branches.

WHAT IT ENFORCES
----------------
1. ``channel_routing_service.py`` references none of the effect-performing
   names — not as imports, not as bare names, not as attributes.
2. It calls no session-writing method (``add``/``commit``/``delete``/...), so a
   direct write cannot slip past the name blocklist in #1, which is a list of
   nouns and blind to verbs.
3. ``ChannelRoutingService.decide`` takes no database session. A caller session
   crossing that boundary is what would let the decision commit, roll back, or
   add to somebody else's transaction; keeping ids and text on the signature is
   the same rule ``run_in_thread`` already enforces one level down.
4. ``decide`` returns a ``RoutingDecisionResult``, and every field on it is
   plain data — an id, a recorder, or a flag. Never an ORM instance, whose
   session is closed by the time the caller sees it.

That list is four items long because the module docstring claims four
structural facts, and it claimed four while this file enforced three from the
day both were written. A guarantee stated in prose is unverified until
something executes it, so the two are kept the same length on purpose: if a
fifth fact is added over there, it comes with a test here in the same change.

WHY AST AND NOT A GREP
----------------------
The module's own docstring names ``ChannelThreadBinding`` and
``ChannelOutboundService`` in prose, explaining why they are absent. A text
scan would match that prose and either fail permanently or have to be weakened
with exclusions until it stopped meaning anything. An AST walk over ``Name`` /
``Attribute`` / import nodes sees identifiers and not comments, so the
docstring can say the thing the test enforces without the two fighting.

SEE ALSO
--------
``channel_routing_scope_test.py``, a sibling rather than an extension of this
file. It guards a different property — that channel routing does not import the
App MCP candidate set — over a module set that includes the candidate provider.
The "four facts, four tests" pairing above is load-bearing, so a fifth test in
here about something else would blunt exactly the invariant it holds.

It does deliberately NOT try to be a reachability analysis. ``decide`` calls
``ChannelCandidateProvider`` and ``CatalogService``, which are read-only today
but are not pinned as such by anything here; a transitive check would have to
model the whole service graph. This is the cheap structural half — the
behavioural half is the API-level no-side-effects suite, which is
mutation-checked.
"""
from __future__ import annotations

import ast
import pathlib

_HERE = pathlib.Path(__file__).resolve()
_BACKEND_ROOT = _HERE.parent.parent.parent  # backend/
_ROUTING_MODULE = (
    _BACKEND_ROOT / "app" / "services" / "server_channels" / "channel_routing_service.py"
)

#: Names that perform, or are the gateway to, an effect the routing decision
#: must not have. Each maps to the effect it would introduce, so a failure says
#: which of the four guarantees just broke rather than only "forbidden name".
_FORBIDDEN: dict[str, str] = {
    # binding
    "ChannelThreadBinding": "creates a channel thread binding",
    "_upsert_binding": "creates a channel thread binding",
    "_park_message": "parks a message on a binding",
    # session / ingest
    "ChannelIngestionService": "opens an agent session and ingests a message",
    "SessionService": "creates a session",
    "MessageService": "sends a message into a session",
    # install
    "InstallService": "installs a bundle for the user",
    # outbound reply
    "ChannelOutboundService": "sends an outbound reply",
    "GoogleChatAdapter": "sends an outbound reply through the channel adapter",
    "get_adapter": "resolves a channel adapter, whose purpose is sending",
    # the effectful half of the pipeline, which owns all of the above
    "ChannelInboundService": "is the effect half of the pipeline; importing it "
    "would make every effect above reachable in one hop, and create a cycle",
}

#: Parameter names that would mean a caller's transaction crossed into the
#: decision. ``decide`` takes ids and text.
_FORBIDDEN_DECIDE_PARAMS = {"db", "session", "db_session"}

#: Types a ``RoutingDecisionResult`` field is allowed to hold. An **allowlist**,
#: not a blocklist of ORM model names: the module imports ``Agent``, ``User``,
#: ``AgentBundle``, ``AgentBundleRevision`` and ``ServerAutoInstallBundle`` to
#: read them, so an ORM row is genuinely within reach of a future field, and
#: "did we list every model" is the question that goes stale. "Is this type
#: plain data" is answerable by whoever adds the field.
#:
#: ``RoutingTrace`` is on the list because it is a plain recorder object built
#: by ``capture()`` — not a table, and not attached to any session. ``uuid`` and
#: ``UUID`` are both here because ``uuid.UUID`` reaches the walk below as a Name
#: plus an Attribute.
_PLAIN_DATA_ANNOTATIONS = {
    "uuid",
    "UUID",
    "RoutingTrace",
    "bool",
    "str",
    "int",
    "float",
}

#: Session methods that write. ``_FORBIDDEN`` above catches effect-performing
#: *names*, which is a blocklist of nouns and therefore blind to verbs: a bare
#: ``db.add(row)`` / ``db.commit()`` dropped into ``_route_catalog`` performs an
#: effect without naming anything forbidden, and would have sailed through this
#: file. The sessions this module opens are read-only — ``db.get`` and
#: ``db.exec`` only — and that is a property worth pinning rather than
#: re-establishing by hand every time somebody edits the passes.
#:
#: ``persist`` is the deliberate exception and does not appear here: it writes
#: through a session of its own that it opens and closes itself, so it is
#: invisible to this check by construction rather than by exemption.
_FORBIDDEN_SESSION_WRITES = {"add", "add_all", "commit", "delete", "flush", "merge"}


def _module_tree() -> ast.Module:
    assert _ROUTING_MODULE.is_file(), f"Expected {_ROUTING_MODULE} to exist"
    return ast.parse(_ROUTING_MODULE.read_text(encoding="utf-8"))


def _referenced_identifiers(tree: ast.Module) -> set[str]:
    """Every identifier the module actually uses — never anything in a string.

    Covers the three ways a forbidden name can arrive: a bare reference, an
    attribute access (``mod.ChannelThreadBinding``), and an import (top-level
    *or* nested inside a function, which is this codebase's normal way of
    breaking import cycles and would otherwise slip straight past).
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.asname or alias.name.rsplit(".", 1)[-1])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                found.add(alias.name)
                if alias.asname:
                    found.add(alias.asname)
    return found


def test_routing_decision_module_cannot_reach_any_effect() -> None:
    """No effect-performing name appears anywhere in the routing module."""
    referenced = _referenced_identifiers(_module_tree())
    offenders = {
        name: why for name, why in _FORBIDDEN.items() if name in referenced
    }
    assert not offenders, (
        "app/services/server_channels/channel_routing_service.py references "
        f"{sorted(offenders)}, which breaks the guarantee that a routing "
        "decision has no side effects.\n\n"
        + "\n".join(f"  - {name}: {why}" for name, why in sorted(offenders.items()))
        + "\n\nPOST /api/v1/admin/routing/simulate calls ChannelRoutingService."
        "decide() and nothing else, and that is the ONLY thing making simulate "
        "side-effect-free — there is deliberately no simulate=True flag to "
        "suppress effects downstream (the plan rejects that design explicitly, "
        "§6). If the effect genuinely belongs in the pipeline, put it in "
        "ChannelInboundService._route_new_thread, which is the bind-and-ingest "
        "half and is where every one of these names already lives."
    )


def test_routing_module_never_writes_through_a_session_it_opens() -> None:
    """No ``add``/``commit``/``delete``/``flush``/``merge`` in the module.

    Structural fact #2 from the module docstring — "the sessions it opens are
    read-only in practice" — stated as an assertion instead of as a claim. A
    routing pass that starts committing through its own short-lived session is
    still a side effect, and is exactly the shape a name-based blocklist cannot
    see.

    Deliberately a blunt whole-module check rather than a dataflow analysis: the
    only session in this file *is* a routing pass's, so any of these verbs on
    any receiver is the thing being forbidden. If a legitimate write ever
    belongs here, it belongs in ``ChannelInboundService`` instead.
    """
    called = {
        node.func.attr
        for node in ast.walk(_module_tree())
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    offenders = called & _FORBIDDEN_SESSION_WRITES
    assert not offenders, (
        f"app/services/server_channels/channel_routing_service.py calls "
        f"{sorted(offenders)} on a session. A routing decision must not write: "
        "POST /api/v1/admin/routing/simulate runs this code over another "
        "account's routing state and is documented as having no effects. The "
        "sessions opened here are for reading (db.get / db.exec) and are closed "
        "without committing. Put the write in "
        "ChannelInboundService._route_new_thread, which is the effect half."
    )


def test_decide_takes_no_database_session() -> None:
    """``decide``'s signature carries ids and text, never a session.

    A ``Session`` argument is how a 'pure' function starts committing: it can
    add to, flush, commit or roll back a transaction the caller is holding, and
    none of that is visible at the call site. Without one, the decision's only
    database access is through short-lived sessions its own worker threads open
    and close — which is also what lets the callers release their pooled
    connection for the whole cascade.
    """
    tree = _module_tree()
    decide = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "decide"
        ),
        None,
    )
    assert decide is not None, "ChannelRoutingService.decide not found"

    args = decide.args
    names = {
        a.arg
        for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)
    }
    offenders = names & _FORBIDDEN_DECIDE_PARAMS
    assert not offenders, (
        f"ChannelRoutingService.decide takes {sorted(offenders)}. It must not: "
        "a caller session crossing that boundary is what would let a routing "
        "decision move somebody else's transaction. Pass ids and text; the "
        "worker targets open their own sessions."
    )


def _class_def(tree: ast.Module, name: str) -> ast.ClassDef:
    found = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and node.name == name
        ),
        None,
    )
    assert found is not None, f"{name} not found in {_ROUTING_MODULE.name}"
    return found


def test_decide_returns_only_plain_data() -> None:
    """Structural fact #4 — ids, recorders and flags, never an ORM row.

    The passes run in worker threads that open a session, load rows and close
    it. An ORM instance handed back from there is attached to a dead session,
    so the caller's next attribute read is a lazy reload that raises — and
    ``RoutingDecisionResult`` says so in prose. This is that claim executed.

    Two halves, because either alone is a hole: ``decide`` must return the
    result type (so this check governs what callers actually receive), and
    every field on that type must be annotated with a plain-data type. An
    ``agent: Agent`` field added later is the failure being bought — the module
    already imports ``Agent`` in order to read it, so nothing but this test
    stands between "loaded a row" and "returned it".
    """
    tree = _module_tree()

    decide = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "decide"
        ),
        None,
    )
    assert decide is not None, "ChannelRoutingService.decide not found"
    returns = decide.returns
    assert isinstance(returns, ast.Name) and returns.id == "RoutingDecisionResult", (
        "ChannelRoutingService.decide must be annotated as returning "
        "RoutingDecisionResult. It is the only return type whose fields are "
        "pinned as plain data below, so annotating decide with anything else "
        "silently drops structural fact #4 of the module docstring."
    )

    result_cls = _class_def(tree, "RoutingDecisionResult")
    offenders: dict[str, list[str]] = {}
    for stmt in result_cls.body:
        if not isinstance(stmt, ast.AnnAssign) or not isinstance(
            stmt.target, ast.Name
        ):
            continue
        used = {
            n.id if isinstance(n, ast.Name) else n.attr
            for n in ast.walk(stmt.annotation)
            if isinstance(n, (ast.Name, ast.Attribute))
        }
        disallowed = sorted(used - _PLAIN_DATA_ANNOTATIONS)
        if disallowed:
            offenders[stmt.target.id] = disallowed

    assert not offenders, (
        "RoutingDecisionResult carries a field that is not plain data: "
        + ", ".join(
            f"{field} ({', '.join(types)})" for field, types in sorted(offenders.items())
        )
        + ".\n\nThe routing passes load their rows in worker threads whose "
        "sessions are closed before decide() returns, so an ORM instance "
        "reaching a caller turns its next attribute read into a lazy reload "
        "against a dead session. Return the id and let the caller load it in "
        "its own session -- that is what agent_id and bundle_uuid are. If the "
        "new field really is plain data (an id, a flag, a recorder, a string), "
        "add its type to _PLAIN_DATA_ANNOTATIONS above."
    )

