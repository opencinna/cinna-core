"""Architecture drift test — the shadowed columns have exactly one reader.

WHY THIS EXISTS
---------------
``managed_ai_credential`` still carries every wiring-policy column it ever had:
``set_as_default``, ``set_user_sdk_defaults``, ``sdk_default_modes``,
``default_model``, ``available_models``, both ``model_override_*`` and
``expiry_notification_date``. For a **manual** record those are the real values.
For a **provider-owned** one they are *shadowed*: present, no longer read, and
holding whatever they held before the provider took ownership.

The values are deliberately not mirrored down from the provider, because a
second copy of a number the provider owns goes stale the moment the provider is
edited, and a stale copy displayed as the active policy is worse than no copy.
What replaces the copy is a resolver, and the resolver is only load-bearing
while it is the *only* reader:

    THE PROPERTY
    ------------
    No module outside ``app/services/credentials/provisioning_policy.py`` reads
    one of those attributes off a ``ManagedAICredential`` instance (or off the
    class).

A read that slips past it is silent by construction. Nothing raises, no type is
wrong, and the value that comes back is a real number — just the wrong one, for
every provider-owned record, until somebody notices that editing a provider did
not change what a member got.

WHAT COUNTS AS A READ
---------------------
A ``Load``-context attribute access whose receiver this file has *inferred* to
be a ``ManagedAICredential``. Writes (``Store``) are not reads and are allowed:
a manual record's columns are still written by ``ManagedAICredentialsService``,
and the provider-owned path refuses those writes at the edge instead
(``_refuse_shadowed_writes``).

HOW A RECEIVER IS INFERRED
--------------------------
Three shapes, all local to one function, and deliberately no further:

1. a parameter annotated ``ManagedAICredential`` (or the string form);
2. an ``AnnAssign`` with that annotation;
3. an assignment from ``<x>.get(ManagedAICredential, …)``.

Plus the class name itself, scanned over the **whole module** rather than only
inside functions, so a module-level or class-body
``select(ManagedAICredential.default_model)`` is caught as well.

**This is inference, not typing, and it under-approximates on purpose.** A read
off a receiver none of the three shapes reaches — a value pulled out of a dict,
a return value with no annotation — is invisible here. That is stated rather
than papered over, and it is why the walk asserts it found something before it
asserts it found nothing wrong: an inference that silently stopped binding
names would otherwise enforce nothing and stay green forever.

SCOPE OF THE SOURCE WALK
------------------------
``app/`` only, with ``app/alembic/`` and ``app/env-templates/`` pruned. The
first holds migrations, which read and write these columns by definition — that
is what a migration is. The second carries an installed virtualenv inside the
backend container, thousands of vendored files that do not exist in a fresh
checkout, and it runs in a separate process that cannot reach a database
session at all.
"""
from __future__ import annotations

import ast
import pathlib

from app.models.credentials.managed_ai_credential import ManagedAICredential
from app.services.credentials.managed_ai_credentials_service import (
    ManagedAICredentialsService,
)

MODEL = "ManagedAICredential"

#: §3.2 of the ai-credential-providers plan, spelled out here rather than
#: imported, so this file states the property independently of the code it
#: guards. ``test_the_service_and_this_file_name_the_same_columns`` is what
#: keeps the two honest about each other.
SHADOWED_FIELDS = frozenset(
    {
        "set_as_default",
        "set_user_sdk_defaults",
        "sdk_default_modes",
        "default_model",
        "available_models",
        "model_override_conversation",
        "model_override_building",
        "expiry_notification_date",
    }
)

#: The one module allowed to read them. It is where the manual-versus-provider
#: branch lives, and having the branch in one place is the entire point.
ALLOWED = frozenset({"app/services/credentials/provisioning_policy.py"})

_APP = pathlib.Path(__file__).resolve().parents[2] / "app"
_PRUNED = {"alembic", "env-templates", "__pycache__", ".venv", "node_modules"}


def _source_files() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for path in _APP.rglob("*.py"):
        if any(part in _PRUNED for part in path.relative_to(_APP).parts):
            continue
        out.append(path)
    return out


def _rel(path: pathlib.Path) -> str:
    return "app/" + str(path.relative_to(_APP)).replace("\\", "/")


def _annotation_names(node: ast.AST | None) -> set[str]:
    """Every bare name in an annotation, including the string form."""
    if node is None:
        return set()
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            node = ast.parse(node.value, mode="eval").body
        except SyntaxError:  # pragma: no cover - malformed annotation
            return set()
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _bound_names(func: ast.AST) -> set[str]:
    """Local names this function binds to a ``ManagedAICredential``."""
    names: set[str] = set()
    args = getattr(func, "args", None)
    if args is not None:
        every = list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
        if args.vararg:
            every.append(args.vararg)
        if args.kwarg:
            every.append(args.kwarg)
        for arg in every:
            if MODEL in _annotation_names(arg.annotation):
                names.add(arg.arg)

    for node in ast.walk(func):
        if isinstance(node, ast.AnnAssign):
            if MODEL in _annotation_names(node.annotation) and isinstance(
                node.target, ast.Name
            ):
                names.add(node.target.id)
        elif isinstance(node, ast.Assign):
            value = node.value
            if (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and value.func.attr == "get"
                and value.args
                and isinstance(value.args[0], ast.Name)
                and value.args[0].id == MODEL
            ):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
    return names


def _scan(scope: ast.AST, receivers: set[str]) -> list[tuple[str, str, int]]:
    found: list[tuple[str, str, int]] = []
    for node in ast.walk(scope):
        if not isinstance(node, ast.Attribute):
            continue
        if not isinstance(node.ctx, ast.Load):
            continue
        if node.attr not in SHADOWED_FIELDS:
            continue
        if isinstance(node.value, ast.Name) and node.value.id in receivers:
            found.append((node.value.id, node.attr, node.lineno))
    return found


def _reads_in(path: pathlib.Path) -> list[tuple[str, str, int]]:
    """``(receiver, attribute, lineno)`` for every shadowed read in one file.

    The module is scanned twice over: once whole, with only the class name as a
    receiver — which is what catches a module-level or class-body
    ``select(ManagedAICredential.default_model)``, neither of which sits inside
    a function — and once per function, where a parameter annotation or a
    ``session.get`` can bind an instance name as well. Hits are de-duplicated by
    position, since the whole-module pass sees the function bodies too.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = list(_scan(tree, {MODEL}))
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        found.extend(_scan(func, _bound_names(func)))
    seen: set[tuple[str, str, int]] = set()
    return [hit for hit in found if not (hit in seen or seen.add(hit))]


# ── The walk must find something before it can prove anything ──────────


def test_the_inference_binds_names_where_it_obviously_should() -> None:
    """A guard against a silently inert test.

    ``managed_ai_credentials_service`` annotates ``parent: ManagedAICredential``
    on most of its methods. If the inference stops binding those — a rename, a
    switch to a type alias, an AST change — this file would report zero
    violations forever while enforcing nothing.
    """
    service = _APP / "services/credentials/managed_ai_credentials_service.py"
    tree = ast.parse(service.read_text(encoding="utf-8"))
    bound = set()
    for func in ast.walk(tree):
        if isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            bound |= _bound_names(func)
    assert "parent" in bound, (
        "No local name was inferred as a ManagedAICredential in the service "
        "that owns the record. The inference is inert and this file proves "
        f"nothing. Bound: {sorted(bound)}"
    )


def test_every_named_column_still_exists_on_the_model() -> None:
    """A field renamed out from under this list would empty it in silence."""
    columns = set(ManagedAICredential.model_fields)
    missing = SHADOWED_FIELDS - columns
    assert not missing, (
        f"{sorted(missing)} are named here but are not fields of "
        "ManagedAICredential any more. Update this list with the rename."
    )


def test_the_service_and_this_file_name_the_same_columns() -> None:
    """The refusal and the property must be about one set of fields.

    ``ManagedAICredentialsService.SHADOWED_FIELDS`` is what a provider-owned
    record refuses writes to; this file's list is what nothing may read. A field
    in one and not the other is either a write nobody refuses or a read nobody
    guards.
    """
    assert (
        set(ManagedAICredentialsService.SHADOWED_FIELDS) == SHADOWED_FIELDS
    ), (
        "The service's refused-write set and this file's forbidden-read set "
        "have drifted: "
        f"{set(ManagedAICredentialsService.SHADOWED_FIELDS) ^ SHADOWED_FIELDS}"
    )


# ── The property ───────────────────────────────────────────────────────


def test_only_the_policy_resolver_reads_the_shadowed_columns() -> None:
    offenders: dict[str, list[tuple[str, str, int]]] = {}
    for path in _source_files():
        rel = _rel(path)
        if rel in ALLOWED:
            continue
        reads = _reads_in(path)
        if reads:
            offenders[rel] = reads

    assert not offenders, (
        "These modules read a shadowed ManagedAICredential column directly. "
        "For a provider-owned record those columns are stale, and the value "
        "returned is wrong with nothing raising. Go through "
        "provisioning_policy.resolve_policy(session, parent) instead:\n"
        + "\n".join(
            f"  {module}: "
            + ", ".join(f"{recv}.{attr} (line {line})" for recv, attr, line in reads)
            for module, reads in sorted(offenders.items())
        )
    )


def test_the_resolver_itself_does_read_them() -> None:
    """The allowlist entry is not a formality.

    ``policy_from_manual`` is where a manual record's own columns are read, and
    it is the reason the allowlist has one member rather than none. A resolver
    that stopped reading them would answer with defaults for every manual
    record — every admin-pasted company key silently losing its wiring.
    """
    resolver = _APP / "services/credentials/provisioning_policy.py"
    read_attrs = {attr for _recv, attr, _line in _reads_in(resolver)}
    assert read_attrs == SHADOWED_FIELDS, (
        "The resolver does not read every shadowed column: missing "
        f"{sorted(SHADOWED_FIELDS - read_attrs)}"
    )
