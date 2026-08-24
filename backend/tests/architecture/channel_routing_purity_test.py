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
The four numbered structural facts in the module docstring of
``channel_routing_service.py``, one parameterized case each, numbered to match
that docstring so a failure names the fact a reader can go and read:

1. **No caller session crosses the boundary.** ``ChannelRoutingService.decide``
   takes no database session parameter. A caller session crossing that boundary
   is what would let the decision commit, roll back, or add to somebody else's
   transaction; keeping ids and text on the signature is the same rule
   ``run_in_thread`` already enforces one level down.
2. **The sessions it opens are read-only in practice.** The module calls no
   session-writing method (``add``/``commit``/``delete``/...), so a direct write
   cannot slip past the name blocklist in #3, which is a list of nouns and blind
   to verbs.
3. **Nothing effectful is imported here.** The module references none of the
   effect-performing names — not as imports, not as bare names, not as
   attributes.
4. **It returns plain data.** ``decide`` returns a ``RoutingDecisionResult``,
   and every field on it is plain data — an id, a recorder, or a flag. Never an
   ORM instance, whose session is closed by the time the caller sees it.

HOW IT IS STRUCTURED, AND WHY THAT SHAPE
----------------------------------------
Each fact is a ``_StructuralFact`` record in ``_FACTS`` below — number, id,
description, and the callable that checks it — and a single parameterized test
runs one fact per case. They are **not** collapsed into one shared assertion
body: the four use genuinely different mechanisms (a name/import blocklist, a
call-verb blocklist, a signature check, a return-annotation allowlist), and a
common body would have to be generic enough to describe none of them. What is
shared is the parameter list, the module parse, and the pytest id — not the
reasoning. Each checker keeps its own failure message, naming which guarantee
broke and where the code belongs instead.

**``_FACTS`` is the list of facts**, and its length is asserted against the
count of numbered facts parsed out of ``channel_routing_service.py``'s module
docstring (``test_fact_list_matches_the_service_module_docstring``). That test
is the meta-check, not a fifth fact. It also asserts the checkers are distinct:
parameterization costs the property that a missing checker was a missing test,
because a record whose ``check=`` was copied from its neighbour still collects,
still passes, and still counts — so that is checked rather than assumed.

The count is asserted rather than merely asserted-in-prose because this file's
own history is the argument for it: it claimed four structural facts while
enforcing three, from the day both were written, and a claim about coverage is
the one claim a reader has no way to spot as false. The service docstring's
list is a flat, numbered, bold-titled markdown list, which parses in one regex
and fails loudly rather than silently if the shape ever changes — so the
stronger of the two options in the plan (§2.3 of
``docs/plans/channels_identity_unification/phase_0_land_scope_split.md``) was
available and is what is used. Adding a fifth fact over there now fails here
until a fifth checker lands in the same change, which is exactly the coupling
both docstrings say they want.

WHY AST AND NOT A GREP
----------------------
The module's own docstring names ``ChannelThreadBinding`` and
``ChannelOutboundService`` in prose, explaining why they are absent. A text
scan would match that prose and either fail permanently or have to be weakened
with exclusions until it stopped meaning anything. An AST walk over ``Name`` /
``Attribute`` / import nodes sees identifiers and not comments, so the
docstring can say the thing the test enforces without the two fighting.

The one place this file *does* read prose is the fact-count check, and it reads
it from the docstring deliberately: there the prose is the subject, not an
obstacle.

SEE ALSO
--------
``channel_routing_scope_test.py``, a sibling rather than an extension of this
file. It guards a different property — that channel routing does not import the
App MCP candidate set — over a module set that includes the candidate provider.
The "four facts, four cases" pairing above is load-bearing, so a fifth fact in
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
import re
from collections.abc import Callable
from typing import NamedTuple

import pytest

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

#: A numbered, bold-titled item in the service module's structural-fact list —
#: ``1. **No caller session crosses the boundary.** ...``. Anchored at column 0,
#: so prose elsewhere in the docstring that happens to start with a digit cannot
#: join the list, and a fact that loses its number or its bold title fails the
#: count check loudly instead of dropping out of it quietly.
_DOCSTRING_FACT_RE = re.compile(r"^(\d+)\.\s+\*\*(.+?)\*\*")


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


def _function_def(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    found = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ),
        None,
    )
    assert found is not None, f"ChannelRoutingService.{name} not found"
    return found


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


def _docstring_facts(tree: ast.Module) -> list[tuple[int, str]]:
    """The ``(number, title)`` of every numbered fact the module docstring claims.

    Scoped to the **first contiguous run** numbered from 1, not to every match in
    the docstring. That docstring is long and leans on bold lead-ins
    (``**Callers must account for that:**``), so a second numbered list added to
    it later would otherwise inflate the count and make this file report
    "you added a fact, add a checker" about a paragraph that is not a fact. The
    run is defined by its own numbering rather than by a prose anchor, because a
    prose anchor is the thing a docstring edit changes.
    """
    docstring = ast.get_docstring(tree)
    assert docstring, f"{_ROUTING_MODULE.name} has no module docstring"
    facts: list[tuple[int, str]] = []
    for line in docstring.splitlines():
        match = _DOCSTRING_FACT_RE.match(line)
        if match is None:
            continue
        number = int(match.group(1))
        if number != len(facts) + 1:
            if facts:
                break  # a separate, later numbered list — not the fact list
            continue  # numbering that never started at 1 — not the fact list
        facts.append((number, match.group(2)))
    return facts


# --------------------------------------------------------------------------
# The four checkers. Same mechanism-per-fact as before parameterization: a name
# blocklist, a call-verb blocklist, a signature check and an annotation
# allowlist are four different questions, and each keeps its own answer.
# --------------------------------------------------------------------------


def _check_no_caller_session_crosses_the_boundary(tree: ast.Module) -> None:
    """Fact 1 — ``decide``'s signature carries ids and text, never a session.

    A ``Session`` argument is how a 'pure' function starts committing: it can
    add to, flush, commit or roll back a transaction the caller is holding, and
    none of that is visible at the call site. Without one, the decision's only
    database access is through short-lived sessions its own worker threads open
    and close — which is also what lets the callers release their pooled
    connection for the whole cascade.
    """
    args = _function_def(tree, "decide").args
    names = {a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)}
    offenders = names & _FORBIDDEN_DECIDE_PARAMS
    assert not offenders, (
        f"ChannelRoutingService.decide takes {sorted(offenders)}. It must not: "
        "a caller session crossing that boundary is what would let a routing "
        "decision move somebody else's transaction. Pass ids and text; the "
        "worker targets open their own sessions."
    )


def _check_sessions_it_opens_are_read_only(tree: ast.Module) -> None:
    """Fact 2 — no ``add``/``commit``/``delete``/``flush``/``merge`` in the module.

    "The sessions it opens are read-only in practice" stated as an assertion
    instead of as a claim. A routing pass that starts committing through its
    own short-lived session is still a side effect, and is exactly the shape a
    name-based blocklist cannot see.

    Deliberately a blunt whole-module check rather than a dataflow analysis: the
    only session in this file *is* a routing pass's, so any of these verbs on
    any receiver is the thing being forbidden. If a legitimate write ever
    belongs here, it belongs in ``ChannelInboundService`` instead.
    """
    called = {
        node.func.attr
        for node in ast.walk(tree)
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


def _check_nothing_effectful_is_imported(tree: ast.Module) -> None:
    """Fact 3 — no effect-performing name appears anywhere in the routing module."""
    referenced = _referenced_identifiers(tree)
    offenders = {name: why for name, why in _FORBIDDEN.items() if name in referenced}
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


def _check_decide_returns_only_plain_data(tree: ast.Module) -> None:
    """Fact 4 — ids, recorders and flags, never an ORM row.

    The passes run in worker threads that open a session, load rows and close
    it. An ORM instance handed back from there is attached to a dead session,
    so the caller's next attribute read is a lazy reload that raises — and
    ``RoutingDecisionResult`` says so in prose. This is that claim executed.

    Two halves, because either alone is a hole: ``decide`` must return the
    result type (so this check governs what callers actually receive), and
    every field on that type must be annotated with a plain-data type. An
    ``agent: Agent`` field added later is the failure being bought — the module
    already imports ``Agent`` in order to read it, so nothing but this check
    stands between "loaded a row" and "returned it".
    """
    returns = _function_def(tree, "decide").returns
    assert isinstance(returns, ast.Name) and returns.id == "RoutingDecisionResult", (
        "ChannelRoutingService.decide must be annotated as returning "
        "RoutingDecisionResult. It is the only return type whose fields are "
        "pinned as plain data below, so annotating decide with anything else "
        "silently drops structural fact #4 of the module docstring."
    )

    result_cls = _class_def(tree, "RoutingDecisionResult")
    offenders: dict[str, list[str]] = {}
    for stmt in result_cls.body:
        if not isinstance(stmt, ast.AnnAssign) or not isinstance(stmt.target, ast.Name):
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


class _StructuralFact(NamedTuple):
    """One numbered structural fact from the service module's docstring.

    ``number`` is that docstring's numbering, not this file's ordering, so a
    failure in ``-v`` output points straight at the paragraph to go and read.
    """

    number: int
    id: str
    description: str
    check: Callable[[ast.Module], None]


#: **The list of facts.** Not a convenience grouping of four tests that happen
#: to be related — the length of this tuple is checked against the service
#: module docstring's own numbered list, so adding a fact there without adding
#: a checker here (or the reverse) fails.
_FACTS: tuple[_StructuralFact, ...] = (
    _StructuralFact(
        number=1,
        id="no-caller-session-crosses-the-boundary",
        description="decide() takes no database session parameter",
        check=_check_no_caller_session_crosses_the_boundary,
    ),
    _StructuralFact(
        number=2,
        id="sessions-it-opens-are-read-only",
        description="the module calls no session-writing method",
        check=_check_sessions_it_opens_are_read_only,
    ),
    _StructuralFact(
        number=3,
        id="nothing-effectful-is-imported",
        description="no effect-performing name is in the module's namespace",
        check=_check_nothing_effectful_is_imported,
    ),
    _StructuralFact(
        number=4,
        id="returns-only-plain-data",
        description="decide() returns RoutingDecisionResult, all fields plain data",
        check=_check_decide_returns_only_plain_data,
    ),
)

_FACT_IDS = [f"fact-{fact.number}-{fact.id}" for fact in _FACTS]


@pytest.mark.parametrize("fact", _FACTS, ids=_FACT_IDS)
def test_routing_decision_module_has_no_side_effects(fact: _StructuralFact) -> None:
    """Run one structural fact's checker over ``channel_routing_service.py``.

    One case per fact the service docstring claims. The checkers are separate
    functions on purpose — each has its own mechanism and its own failure
    message, and what is shared here is the parse and the parametrization, not
    the reasoning.
    """
    fact.check(_module_tree())


def test_fact_list_matches_the_service_module_docstring() -> None:
    """``_FACTS`` and the service docstring's numbered list are the same list.

    The meta-check, not a fifth fact. ``channel_routing_service.py`` states its
    structural facts in prose and says they are each executed by this file; it
    was once written claiming four while this file enforced three, so that
    pairing is asserted rather than trusted. A fifth fact over there fails here
    until a fifth checker lands in the same change.
    """
    claimed = _docstring_facts(_module_tree())
    assert claimed, (
        f"No numbered structural fact parsed out of {_ROUTING_MODULE.name}'s "
        "module docstring. That most likely means the list changed shape rather "
        "than that the facts were deleted: this file looks for "
        "``N. **Title.** ...`` starting at column 0, numbered contiguously from "
        "1. Restore that shape, or teach _DOCSTRING_FACT_RE the new one — but do "
        "not delete this check, which is the only thing tying the prose over "
        "there to the checkers here."
    )
    assert len(claimed) == len(_FACTS), (
        f"{_ROUTING_MODULE.name}'s module docstring claims {len(claimed)} "
        f"structural facts but _FACTS has {len(_FACTS)} checkers.\n\n"
        "claimed: "
        + "; ".join(f"{n}. {title}" for n, title in claimed)
        + "\nchecked: "
        + "; ".join(f"{f.number}. {f.description}" for f in _FACTS)
        + "\n\nThe two lists are one list stated twice — prose over there, "
        "executable here. A fact stated only in the docstring is a claim, not "
        "a guarantee, which is the exact defect this pairing exists to prevent. "
        "Add the missing checker to _FACTS (or drop the fact from the docstring "
        "if it no longer holds)."
    )
    assert len({fact.check for fact in _FACTS}) == len(_FACTS), (
        "Two _FACTS entries share a checker, so one fact is unenforced while the "
        "count still matches the docstring — the exact defect this pairing "
        "exists to catch, one level up from where it happened last time. Before "
        "parameterization a missing checker was a missing test; now it is a "
        "duplicated `check=`, which is what copying a _StructuralFact block "
        "produces. Give each fact its own _check_* function."
    )
    assert [n for n, _ in claimed] == [f.number for f in _FACTS], (
        "The docstring's fact numbering no longer lines up with _FACTS: "
        f"docstring {[n for n, _ in claimed]} vs _FACTS "
        f"{[f.number for f in _FACTS]}. The numbers are how a failure here "
        "points a reader at the right paragraph, so keep them in step."
    )
