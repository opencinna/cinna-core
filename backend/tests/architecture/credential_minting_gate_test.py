"""
Architecture drift test — the credential-minting gate for CLI-exchanged sessions.

WHY THIS EXISTS
---------------
``POST /cli/account/desktop-token`` converts an account CLI token into an
ordinary user JWT. ``ensure_not_cli_exchanged_session`` (``app/api/deps.py``,
with its ``Depends`` adapter ``forbid_cli_exchanged_desktop_session`` exposed as
``NoCliExchangedSession``) refuses that session at the platform's
credential-minting surfaces, so it cannot produce a *fresh* credential carrying
no ``minted_by_account_token_id`` link back to the account token — a credential
that would outlive the revoke cascade and leave a user who revoked their leaked
``account.json`` having ended nothing.

The gate's docstring states the property and then says the thing this file
exists to obey: *"Do not apply it by route list learned from a document —
re-derive the set against the property, because a new minting route added later
inherits the loop silently."* A test carrying a hardcoded list of expected
routes would be exactly the document it warns against: green on the day the next
minting route is added without the gate. So this module **derives** the set
mechanically from the source tree and the live FastAPI app, and asserts every
derived member carries the gate.

The only hand-written inputs are the two cascade-function names, the gate's
three identifiers, and the auth-annotation names. Every one is asserted to exist
first, so a rename fails loudly here instead of silently emptying the derivation
and leaving a green test that enforces nothing.

HOW THE SET IS DERIVED
----------------------
1. **Sinks** — parse the bodies of ``AccountCLIService.revoke_account_token``
   and ``DesktopAuthService.revoke_clients_for_account_token`` and collect every
   model named in ``select(X)`` / ``db.get(X, ...)``. These are the credential
   classes the revoke cascade is built to reach. ``AccountCLIService`` is the
   authority the gate's docstring names; reading it instead of restating it is
   what keeps this test honest as the cascade grows.
2. **Proxies** — a redeemable artefact is not itself a cascade class but is
   exchangeable for one without further authentication, so minting it is minting
   the credential. Derived, not listed: for each **auth-free** route whose call
   closure constructs a sink, take the other models it reads and keep those whose
   class declares (``is_used`` or ``used_at``) **and** ``expires_at`` — the
   single-use-with-expiry shape of a redemption artefact.
3. **Selection** — a route reachable with ``CurrentUser`` whose call closure
   constructs a sink or a proxy must carry the gate.
4. **Gate detection**, two shapes OR'd, matching the two application forms the
   gate's docstring describes:
   * the live route's dependency tree contains
     ``forbid_cli_exchanged_desktop_session`` — read from ``route.dependant``,
     not from a decorator's AST, so router-level and nested dependencies count; or
   * the handler's **own** body calls ``ensure_not_cli_exchanged_session``
     directly. For that shape the call's nearest enclosing ``if`` is additionally
     required to test ``!= "deny"``: the docstring's fail-closed rule made
     executable, so a rewrite to ``== "approve"`` is reported.

The call graph is **receiver-resolved**: ``X.method()`` resolves to the method of
class ``X``, and a bare ``f()`` only to a module-level function. A simple-name
graph instead over-selects three unrelated routes in
``app/api/routes/mcp_providers.py`` through a ``register_client`` name clash. The
closure runs to a **fixpoint**, not a fixed depth; it is computed once over the
whole graph via Tarjan SCC condensation, so the fixpoint costs about a second
rather than exhausting memory per route.

SCOPE OF THE SOURCE WALK
------------------------
Only the API process's own source is indexed. ``app/env-templates/`` is pruned
along with ``__pycache__`` / ``.venv`` / ``node_modules`` / ``site-packages`` and
``app/alembic/``. This is not a convenience: inside the backend container
``env-templates`` carries an installed virtualenv — over ten thousand vendored
``.py`` files that do not exist in a fresh checkout. Indexing it would make the
derivation depend on whether a venv happens to be materialised (indexing 79k
methods instead of 2.2k) and would inject spurious call edges wherever a vendored
class name collides with an application one. The env template runs in a separate
process and is not reachable from a route handler, so pruning it loses nothing.

WHAT THIS TEST DOES **NOT** SEE
-------------------------------
Stated plainly, because a green run here is not a proof of the property — it is a
proof that no *statically visible* minting route lost its gate.

* **Dynamic dispatch.** ``getattr(svc, name)()``, dispatch dictionaries,
  ``functools.partial``, callables handed to background tasks or event handlers:
  none produce a resolvable call edge. A minting path reached only that way is
  invisible.
* **Non-``Name`` receivers.** ``self.svc.method()``, ``mod.sub.method()``, or a
  local alias (``svc = DeviceLoginService; svc.approve()``) are not resolved. The
  handlers in this tree call services as ``ClassName.method(...)``, which is why
  the derivation works; a handler written in another style drops out silently
  rather than failing.
* **A cascade class that ought to be cascaded but is not.** Sinks are read *from*
  the cascade functions, so a credential class the cascade forgot is not a sink
  here and its minting route is never selected. That gap is real, but it belongs
  to the cascade's own tests — here it is invisible by construction.
* **Proxy-shape drift.** A future redeemable artefact expressing consumption some
  other way (a ``consumed_at``, a ``status`` enum, a delete-on-redeem row) has
  neither ``is_used``/``used_at`` nor necessarily ``expires_at``, so it is not
  recognised as a proxy and a route minting only it is not selected.
* **Auth-annotation drift, in both directions.** Selection keys on the
  ``CurrentUser`` annotation; proxy derivation keys on the absence of
  ``CurrentUser`` / ``CLIContext`` / ``AccountCLIContext``. A route
  authenticating some new way is neither selected nor treated as auth-free. The
  names are existence-checked, so a *rename* is loud; a genuinely *new*
  annotation is not.
* **Branch-insensitivity.** The closure cannot tell that a sink is constructed
  only on a branch that does not mint. The two consent routes are over-selected
  for exactly this reason (their deny branch mints nothing) and pass via the
  in-handler shape. Conversely, a future two-action route that gates the *wrong*
  branch would still pass: the ``!= "deny"`` assertion narrows that hole — it
  rejects the ``== "approve"`` spelling the docstring warns about — but does not
  close it.

KNOWN RESIDUE THE PROPERTY DELIBERATELY EXCLUDES
------------------------------------------------
Recorded so nobody reads their absence as a bug in the derivation: agent-API
external keys and A2A access tokens mint credentials that outlive the cascade but
are listed and revocable through their own routes, and are excluded by decision.
The MCP OAuth consent (``app/api/routes/mcp_consent.py``) mints a credential that
outlives the cascade with *no* owner-reachable revocation — a revocation gap
tracked separately on the MCP side, and explicitly not a member of this set. This
test asserts that route is **not** selected. If it ever is, the fix belongs on the
MCP side; this test must not be "fixed" by gating it.
"""

from __future__ import annotations

import ast
import inspect
import os
import textwrap
from dataclasses import dataclass, field
from functools import lru_cache

from fastapi.routing import APIRoute

import app as app_package
from app.api import deps
from app.main import app

# ── Hand-written inputs (all existence-checked in the first test) ──────────

# The revoke cascade. Its bodies name the credential classes the gate protects;
# we read them rather than restating them.
CASCADE_FUNCTIONS: tuple[tuple[str, str], ...] = (
    ("AccountCLIService", "revoke_account_token"),
    ("DesktopAuthService", "revoke_clients_for_account_token"),
)

# The gate's three identifiers in app/api/deps.py.
GATE_FUNCTION = "ensure_not_cli_exchanged_session"
GATE_DEPENDENCY_ADAPTER = "forbid_cli_exchanged_desktop_session"
GATE_DEPENDENCY_ALIAS = "NoCliExchangedSession"

# Annotation names marking a route as authenticated. Matched as substrings of the
# unparsed annotation, so the ``*Dep`` aliases (``CLIContextDep``,
# ``AccountCLIContextDep``) are covered by their base names.
AUTH_ANNOTATIONS: tuple[str, ...] = ("CurrentUser", "CLIContext", "AccountCLIContext")
# The subset marking a route reachable by an exchanged desktop session.
USER_ANNOTATION = "CurrentUser"

# The non-minting action the in-handler branch must be written against.
NON_MINTING_ACTION = "deny"

# Route module that must never be selected — see "KNOWN RESIDUE" above.
EXCLUDED_RESIDUE_MODULE = "mcp_consent.py"

# Directories that are not the API process's own source — see "SCOPE" above.
PRUNED_DIRS = frozenset(
    {
        "env-templates",
        "alembic",
        "__pycache__",
        ".venv",
        "node_modules",
        "site-packages",
        ".git",
    }
)


# ── Source index over backend/app ─────────────────────────────────────────


@dataclass
class _Index:
    """Receiver-resolved function index over the application source tree."""

    by_qualified: dict[tuple[str, str], ast.AST] = field(default_factory=dict)
    by_bare: dict[str, list[ast.AST]] = field(default_factory=dict)
    class_fields: dict[str, list[str]] = field(default_factory=dict)


@lru_cache(maxsize=1)
def _index() -> _Index:
    assert app_package.__file__ is not None
    app_dir = os.path.dirname(os.path.abspath(app_package.__file__))
    idx = _Index()
    for dirpath, dirnames, filenames in os.walk(app_dir):
        dirnames[:] = [d for d in dirnames if d not in PRUNED_DIRS]
        for filename in sorted(filenames):
            if not filename.endswith(".py"):
                continue
            path = os.path.join(dirpath, filename)
            try:
                with open(path, encoding="utf-8") as handle:
                    tree = ast.parse(handle.read())
            except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    idx.class_fields[node.name] = [
                        stmt.target.id
                        for stmt in node.body
                        if isinstance(stmt, ast.AnnAssign)
                        and isinstance(stmt.target, ast.Name)
                    ]
                    for member in node.body:
                        if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                            idx.by_qualified[(node.name, member.name)] = member
            # Only module-level functions are callable by a bare name.
            for node in tree.body:
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    idx.by_bare.setdefault(node.name, []).append(node)
    return idx


# ── Local facts read off a single function body ───────────────────────────


def _call_edges(node: ast.AST):
    """Yield receiver-resolved call edges out of ``node``'s whole subtree."""
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if isinstance(func, ast.Name):
            yield ("bare", func.id)
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            yield ("qualified", (func.value.id, func.attr))


def _local_constructions(node: ast.AST) -> frozenset[str]:
    """Capitalised bare calls in this body — i.e. object constructions."""
    return frozenset(
        sub.func.id
        for sub in ast.walk(node)
        if isinstance(sub, ast.Call)
        and isinstance(sub.func, ast.Name)
        and sub.func.id[:1].isupper()
    )


def _local_model_reads(node: ast.AST) -> frozenset[str]:
    """Models named in ``select(X)`` or ``<session>.get(X, ...)`` in this body."""
    found: set[str] = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        if isinstance(sub.func, ast.Name) and sub.func.id == "select":
            for arg in sub.args:
                if isinstance(arg, ast.Name) and arg.id[:1].isupper():
                    found.add(arg.id)
        if (
            isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "get"
            and sub.args
            and isinstance(sub.args[0], ast.Name)
            and sub.args[0].id[:1].isupper()
        ):
            found.add(sub.args[0].id)
    return frozenset(found)


# ── Call graph + fixpoint closure (Tarjan SCC condensation) ───────────────


@dataclass
class _Graph:
    node_index: dict[int, int]
    adjacency: list[set[int]]
    component_of: list[int]
    reachable_constructions: list[frozenset[str]]
    reachable_model_reads: list[frozenset[str]]


def _strongly_connected_components(
    adjacency: list[set[int]],
) -> tuple[list[list[int]], list[int]]:
    """Iterative Tarjan. Components come back in reverse topological order."""
    size = len(adjacency)
    order: list[int | None] = [None] * size
    low = [0] * size
    on_stack = [False] * size
    stack: list[int] = []
    components: list[list[int]] = []
    component_of = [-1] * size
    counter = 0

    for root in range(size):
        if order[root] is not None:
            continue
        order[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack[root] = True
        work: list[tuple[int, object]] = [(root, iter(adjacency[root]))]
        while work:
            vertex, iterator = work[-1]
            descended = False
            for neighbour in iterator:  # type: ignore[union-attr]
                if order[neighbour] is None:
                    order[neighbour] = low[neighbour] = counter
                    counter += 1
                    stack.append(neighbour)
                    on_stack[neighbour] = True
                    work.append((neighbour, iter(adjacency[neighbour])))
                    descended = True
                    break
                if on_stack[neighbour]:
                    low[vertex] = min(low[vertex], order[neighbour])  # type: ignore[arg-type]
            if descended:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[vertex])
            if low[vertex] == order[vertex]:
                component: list[int] = []
                while True:
                    popped = stack.pop()
                    on_stack[popped] = False
                    component_of[popped] = len(components)
                    component.append(popped)
                    if popped == vertex:
                        break
                components.append(component)
    return components, component_of


@lru_cache(maxsize=1)
def _graph() -> _Graph:
    idx = _index()
    nodes: list[ast.AST] = []
    node_index: dict[int, int] = {}

    def register(node: ast.AST) -> None:
        if id(node) not in node_index:
            node_index[id(node)] = len(nodes)
            nodes.append(node)

    for node in idx.by_qualified.values():
        register(node)
    for bucket in idx.by_bare.values():
        for node in bucket:
            register(node)

    adjacency = [_successors(node, idx, node_index) for node in nodes]
    local_constructions = [_local_constructions(node) for node in nodes]
    local_model_reads = [_local_model_reads(node) for node in nodes]

    components, component_of = _strongly_connected_components(adjacency)
    # Reverse-topological order means every successor component is already
    # aggregated when we reach the component pointing at it — the fixpoint,
    # computed in one pass instead of by iterating to convergence per route.
    per_component_constructions: list[frozenset[str]] = [frozenset()] * len(components)
    per_component_model_reads: list[frozenset[str]] = [frozenset()] * len(components)
    for component_id, component in enumerate(components):
        constructions: set[str] = set()
        model_reads: set[str] = set()
        for vertex in component:
            constructions |= local_constructions[vertex]
            model_reads |= local_model_reads[vertex]
            for neighbour in adjacency[vertex]:
                other = component_of[neighbour]
                if other != component_id:
                    constructions |= per_component_constructions[other]
                    model_reads |= per_component_model_reads[other]
        per_component_constructions[component_id] = frozenset(constructions)
        per_component_model_reads[component_id] = frozenset(model_reads)

    return _Graph(
        node_index=node_index,
        adjacency=adjacency,
        component_of=component_of,
        reachable_constructions=[
            per_component_constructions[component_of[i]] for i in range(len(nodes))
        ],
        reachable_model_reads=[
            per_component_model_reads[component_of[i]] for i in range(len(nodes))
        ],
    )


def _successors(node: ast.AST, idx: _Index, node_index: dict[int, int]) -> set[int]:
    out: set[int] = set()
    for kind, target in _call_edges(node):
        if kind == "qualified":
            callee = idx.by_qualified.get(target)  # type: ignore[arg-type]
            if callee is not None and id(callee) in node_index:
                out.add(node_index[id(callee)])
        else:
            for callee in idx.by_bare.get(target, ()):  # type: ignore[arg-type]
                if id(callee) in node_index:
                    out.add(node_index[id(callee)])
    return out


def _closure_facts(node: ast.AST) -> tuple[frozenset[str], frozenset[str]]:
    """``(constructions, model reads)`` over ``node``'s transitive call closure."""
    graph = _graph()
    idx = _index()
    constructions = set(_local_constructions(node))
    model_reads = set(_local_model_reads(node))
    for successor in _successors(node, idx, graph.node_index):
        constructions |= graph.reachable_constructions[successor]
        model_reads |= graph.reachable_model_reads[successor]
    return frozenset(constructions), frozenset(model_reads)


# ── Route records, enumerated from the live app ───────────────────────────


@dataclass
class _RouteRecord:
    label: str
    module: str
    node: ast.FunctionDef | ast.AsyncFunctionDef
    dependency_gated: bool


def _handler_ast(endpoint) -> ast.FunctionDef | ast.AsyncFunctionDef:
    tree = ast.parse(textwrap.dedent(inspect.getsource(endpoint)))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == endpoint.__name__
        ):
            return node
    raise AssertionError(f"handler AST not found for {endpoint!r}")


def _dependency_tree_has_gate(dependant) -> bool:
    gate_adapter = getattr(deps, GATE_DEPENDENCY_ADAPTER)
    if getattr(dependant, "call", None) is gate_adapter:
        return True
    return any(
        _dependency_tree_has_gate(child)
        for child in getattr(dependant, "dependencies", [])
    )


@lru_cache(maxsize=1)
def _live_routes() -> tuple[_RouteRecord, ...]:
    records: list[_RouteRecord] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        endpoint = route.endpoint
        module = os.path.basename(inspect.getsourcefile(endpoint) or "<unknown>")
        methods = ",".join(sorted(route.methods or ()))
        records.append(
            _RouteRecord(
                label=f"{methods} {route.path} ({module}::{endpoint.__name__})",
                module=module,
                node=_handler_ast(endpoint),
                dependency_gated=_dependency_tree_has_gate(route.dependant),
            )
        )
    return tuple(records)


def _annotation_text(node) -> str:
    return " ".join(
        ast.unparse(arg.annotation)
        for arg in list(node.args.args) + list(node.args.kwonlyargs)
        if arg.annotation is not None
    )


def _has_any_auth_annotation(node) -> bool:
    text = _annotation_text(node)
    return any(name in text for name in AUTH_ANNOTATIONS)


def _is_user_reachable(node) -> bool:
    return USER_ANNOTATION in _annotation_text(node)


# ── Derivation ────────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def _sinks() -> frozenset[str]:
    idx = _index()
    found: set[str] = set()
    for class_name, method_name in CASCADE_FUNCTIONS:
        found |= _local_model_reads(idx.by_qualified[(class_name, method_name)])
    return frozenset(found)


@lru_cache(maxsize=1)
def _proxies() -> frozenset[str]:
    idx = _index()
    sinks = _sinks()
    found: set[str] = set()
    for record in _live_routes():
        if _has_any_auth_annotation(record.node):
            continue
        constructions, model_reads = _closure_facts(record.node)
        if not (constructions & sinks):
            continue
        for model in model_reads - sinks:
            fields = idx.class_fields.get(model, [])
            consumable = "is_used" in fields or "used_at" in fields
            if consumable and "expires_at" in fields:
                found.add(model)
    return frozenset(found)


def _mints(node) -> bool:
    """Does this handler's call closure construct a cascade class or a proxy?"""
    constructions, _model_reads = _closure_facts(node)
    return bool(constructions & _sinks() or constructions & _proxies())


# ── Gate detection ────────────────────────────────────────────────────────


def _in_handler_gate_calls(node) -> list[ast.Call]:
    """Direct ``ensure_not_cli_exchanged_session(...)`` calls in the OWN body."""
    return [
        sub
        for sub in ast.walk(node)
        if isinstance(sub, ast.Call)
        and isinstance(sub.func, ast.Name)
        and sub.func.id == GATE_FUNCTION
    ]


def _nearest_enclosing_if(node, target: ast.Call) -> ast.If | None:
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(node):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    current: ast.AST | None = target
    while current is not None:
        current = parents.get(id(current))
        if isinstance(current, ast.If):
            return current
    return None


def _is_fail_closed_branch(test: ast.expr) -> bool:
    """``<something> != "deny"`` — the fail-closed spelling the docstring demands."""
    return (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.NotEq)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == NON_MINTING_ACTION
    )


def _audit(records) -> tuple[list[_RouteRecord], list[str], list[str]]:
    """Return ``(selected, ungated labels, branch-shape problems)``."""
    selected: list[_RouteRecord] = []
    ungated: list[str] = []
    problems: list[str] = []
    for record in records:
        if not _is_user_reachable(record.node):
            continue
        if not _mints(record.node):
            continue
        selected.append(record)
        gated = record.dependency_gated
        for call in _in_handler_gate_calls(record.node):
            gated = True
            enclosing = _nearest_enclosing_if(record.node, call)
            shape = (
                "no enclosing if"
                if enclosing is None
                else f"if {ast.unparse(enclosing.test)}"
            )
            if enclosing is None or not _is_fail_closed_branch(enclosing.test):
                problems.append(
                    f"{record.label}: the in-handler gate must sit on a "
                    f'`!= "{NON_MINTING_ACTION}"` branch so a third action is '
                    f"gated by default; found {shape}"
                )
        if not gated:
            ungated.append(record.label)
    return selected, ungated, problems


class _StripGateCalls(ast.NodeTransformer):
    """Delete direct gate calls from a handler body — in memory, never on disk."""

    def visit_Expr(self, node: ast.Expr):  # noqa: N802
        if (
            isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == GATE_FUNCTION
        ):
            return None
        return node


def _without_gate_calls(node):
    rewritten = _StripGateCalls().visit(ast.parse(ast.unparse(node)))
    ast.fix_missing_locations(rewritten)
    return rewritten.body[0]


# ── Tests ─────────────────────────────────────────────────────────────────


def test_hand_written_inputs_all_exist() -> None:
    """
    Every hand-written name this module keys on must resolve:
      1. The gate function, its Depends adapter and the alias.
      2. The two revoke-cascade functions the sink set is read from.
      3. The auth annotations selection keys on.
      4. The derivation itself is non-empty.
    A rename that slipped past this test would leave the rest of the file green
    while it enforced nothing.
    """
    # ── Phase 1: the gate's three identifiers ─────────────────────────────
    for name in (GATE_FUNCTION, GATE_DEPENDENCY_ADAPTER, GATE_DEPENDENCY_ALIAS):
        assert hasattr(deps, name), (
            f"app.api.deps.{name} is gone or renamed — this test keys on it by "
            "name and would otherwise silently stop enforcing the gate"
        )
    assert callable(getattr(deps, GATE_FUNCTION))
    assert callable(getattr(deps, GATE_DEPENDENCY_ADAPTER))

    # ── Phase 2: the revoke cascade functions ─────────────────────────────
    index = _index()
    for class_name, method_name in CASCADE_FUNCTIONS:
        assert (class_name, method_name) in index.by_qualified, (
            f"{class_name}.{method_name} not found — the sink set is derived "
            "from its body; a rename must not silently yield an empty set"
        )

    # ── Phase 3: the auth annotations ─────────────────────────────────────
    for name in AUTH_ANNOTATIONS:
        assert hasattr(deps, name), f"app.api.deps.{name} is gone or renamed"
    assert USER_ANNOTATION in AUTH_ANNOTATIONS

    # ── Phase 4: the source walk and derivation are non-empty ─────────────
    assert index.by_qualified and index.by_bare, "source index is empty"
    assert _live_routes(), "no routes enumerated from the live app"
    assert _sinks(), "sink derivation collapsed to nothing"
    assert _proxies(), "proxy derivation collapsed to nothing"


def test_every_derived_minting_route_carries_the_gate() -> None:
    """
    Derive the gated set and enforce it:
      1. Sinks come from the revoke cascade's own bodies.
      2. Proxies are redeemable artefacts reachable auth-free on a sink path.
      3. Every CurrentUser-reachable route minting one carries the gate.
      4. In-handler gates sit on a `!= "deny"` branch (fail closed).
      5. Both application shapes are still in use.
      6. The MCP OAuth consent residue is NOT selected.
    """
    # ── Phase 1: sinks ────────────────────────────────────────────────────
    sinks = _sinks()
    assert sinks, "no cascade classes derived"
    for model in sinks:
        assert model in _index().class_fields, f"derived sink {model} is not a class"

    # ── Phase 2: proxies ──────────────────────────────────────────────────
    proxies = _proxies()
    assert proxies, "no redemption proxies derived"
    assert not (proxies & sinks), "a proxy must not also be a cascade class"

    # ── Phase 3: selection + gate enforcement ─────────────────────────────
    selected, ungated, problems = _audit(_live_routes())
    assert selected, (
        "no minting routes selected at all — the derivation broke; "
        f"sinks={sorted(sinks)} proxies={sorted(proxies)}"
    )
    assert not ungated, (
        "route(s) mint a credential the account-token revoke cascade is built to "
        f"reach, but carry no CLI-exchanged-session gate ({GATE_DEPENDENCY_ALIAS} "
        f"as a route dependency, or {GATE_FUNCTION} called on the minting "
        "branch):\n  "
        + "\n  ".join(ungated)
        + f"\n(derived sinks={sorted(sinks)}, proxies={sorted(proxies)})"
    )

    # ── Phase 4: in-handler gates fail closed ─────────────────────────────
    assert not problems, "\n".join(problems)

    # ── Phase 5: both application shapes are still exercised ──────────────
    # If either shape disappears from real code, that half of the detector stops
    # being exercised and would rot unnoticed.
    assert any(r.dependency_gated for r in selected), (
        "no selected route uses the route-dependency shape any more"
    )
    assert any(_in_handler_gate_calls(r.node) for r in selected), (
        "no selected route uses the in-handler shape any more"
    )

    # ── Phase 6: the excluded residue is not selected ─────────────────────
    # The MCP OAuth consent mints a credential outliving the cascade with no
    # owner-reachable revocation. That is a separate, tracked gap on the MCP
    # side and deliberately outside this property — do not "fix" this by gating
    # it; see the module docstring.
    assert not any(r.module == EXCLUDED_RESIDUE_MODULE for r in selected), (
        f"{EXCLUDED_RESIDUE_MODULE} was selected by the derivation, but the "
        "gate's docstring names it as known residue outside this property"
    )


def test_detector_reports_a_new_ungated_minting_route() -> None:
    """
    Red proof — the detector is not vacuous:
      1. The real tree reports zero violations.
      2. A synthetic CurrentUser handler that mints and carries no gate IS
         reported as a violation.
      3. The same handler WITH an in-handler gate on `!= "deny"` is accepted,
         so the detector is not simply reporting everything.
      4. The same gate written `== "approve"` is reported as a branch-shape
         problem — the docstring's fail-closed rule is load-bearing.
    """
    # ── Phase 1: real tree is clean ───────────────────────────────────────
    real_selected, real_ungated, real_problems = _audit(_live_routes())
    assert real_selected
    assert real_ungated == []
    assert real_problems == []

    # ── Phase 2: a new minting route added without the gate is reported ───
    ungated_source = textwrap.dedent(
        '''
        async def device_login_approve_synthetic(
            body: DeviceLoginResolveBody,
            request: Request,
            db: SessionDep,
            current_user: CurrentUser,
        ) -> Message:
            """Synthetic minting route added without the gate."""
            await DeviceLoginService.approve(
                db=db, user=current_user, user_code=body.user_code, request=request
            )
            return Message(message="ok")
        '''
    )
    ungated_record = _RouteRecord(
        label="POST /synthetic/account/login/approve (synthetic)",
        module="<synthetic>",
        node=ast.parse(ungated_source).body[0],
        dependency_gated=False,
    )
    assert _is_user_reachable(ungated_record.node)
    assert _mints(ungated_record.node), (
        "the synthetic handler is not recognised as minting, so this red proof "
        "would prove nothing"
    )
    selected, ungated, _problems = _audit([ungated_record])
    assert [r.label for r in selected] == [ungated_record.label]
    assert ungated == [ungated_record.label]

    # ── Phase 3: the same route WITH the gate is accepted ─────────────────
    gated_source = textwrap.dedent(
        f'''
        async def device_login_approve_synthetic_gated(
            body: DeviceLoginResolveBody,
            request: Request,
            db: SessionDep,
            current_user: CurrentUser,
            client_claims: CurrentClientClaims,
        ) -> Message:
            """Synthetic minting route carrying the in-handler gate."""
            if body.action != "{NON_MINTING_ACTION}":
                _client_kind, external_client_id = client_claims
                {GATE_FUNCTION}(db, external_client_id)
            await DeviceLoginService.approve(
                db=db, user=current_user, user_code=body.user_code, request=request
            )
            return Message(message="ok")
        '''
    )
    gated_record = _RouteRecord(
        label="POST /synthetic/account/login/approve-gated (synthetic)",
        module="<synthetic>",
        node=ast.parse(gated_source).body[0],
        dependency_gated=False,
    )
    selected, ungated, problems = _audit([gated_record])
    assert [r.label for r in selected] == [gated_record.label]
    assert ungated == []
    assert problems == []

    # ── Phase 4: the fail-closed branch shape is load-bearing ─────────────
    wrong_branch_record = _RouteRecord(
        label="POST /synthetic/wrong-branch (synthetic)",
        module="<synthetic>",
        node=ast.parse(
            gated_source.replace(f'!= "{NON_MINTING_ACTION}"', '== "approve"')
        ).body[0],
        dependency_gated=False,
    )
    _selected, ungated, problems = _audit([wrong_branch_record])
    assert ungated == []  # the call is present, so the route counts as gated...
    assert problems, '...but the `== "approve"` branch shape must be reported'


def test_removing_the_gate_from_a_real_route_is_reported() -> None:
    """
    Red proof, second half — the green result above is caused by the gates
    actually being there, not by the derivation quietly selecting nothing:
      1. Every real selected route goes red once its gate is stripped in memory.
      2. Stripping the gate must not change whether the route is selected.
      3. Each route's own mechanism is individually load-bearing.
    Nothing on disk is touched — the strip is an AST rewrite of a copy.
    """
    selected, ungated, _problems = _audit(_live_routes())
    assert selected and ungated == []

    for record in selected:
        # ── Phase 1: full strip → the route is reported ───────────────────
        fully_stripped = _RouteRecord(
            label=record.label,
            module=record.module,
            node=_without_gate_calls(record.node),
            dependency_gated=False,
        )
        still_selected, now_ungated, _p = _audit([fully_stripped])
        assert [r.label for r in still_selected] == [record.label], (
            f"{record.label} stopped being selected once its gate was removed — "
            "selection must depend on what the route mints, not on the gate"
        )
        assert now_ungated == [record.label], (
            f"{record.label} is not reported when its gate is removed; the "
            "detector is vacuous for this route"
        )

        # ── Phase 2: this route's own mechanism is load-bearing ───────────
        has_in_handler = bool(_in_handler_gate_calls(record.node))
        if record.dependency_gated and not has_in_handler:
            # Dependency-only route: dropping the dependency alone must report.
            _s, reported, _p = _audit(
                [
                    _RouteRecord(
                        label=record.label,
                        module=record.module,
                        node=record.node,
                        dependency_gated=False,
                    )
                ]
            )
            assert reported == [record.label]
        elif has_in_handler and not record.dependency_gated:
            # In-handler-only route: deleting the calls alone must report.
            _s, reported, _p = _audit([fully_stripped])
            assert reported == [record.label]
