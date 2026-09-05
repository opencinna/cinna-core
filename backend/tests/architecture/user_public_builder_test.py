"""
Architecture drift test — every ``UserPublic`` answer goes through one builder.

WHY THIS EXISTS
---------------
``UserPublic`` is not a projection of a ``User`` row. It carries fields that
exist nowhere on the row: the derived enrolment flags (``has_passkey``,
``has_totp``), the confirmation-resend timestamp, and ``can_change_email`` — an
instance-wide access-policy fact resolved from ``server_config``.
``app/api/routes/_user_public.py`` exists to assemble them, and its docstring
states the rule this file makes executable: *"every endpoint answering with a
``UserPublic`` goes through ``user_to_public``."*

A route that instead returns the ORM row and lets FastAPI coerce it does not
fail at the seam. It fails one of two ways, both of them quiet at the time the
route is written:

* For a field **with** a default, coercion fills the default. A route returning
  the raw row reports ``has_passkey=False`` for a user who has one — a wrong
  answer, no error, no test failure unless someone happened to assert on that
  field through that particular endpoint.
* For a field with **no** default, coercion raises ``ResponseValidationError``
  and the endpoint 500s. That is what ``can_change_email`` (deliberately
  required-with-no-default, precisely so the permissive answer is not the
  silent one) did to ``POST /private/users/`` the day it was added: the route
  had shipped years earlier returning ``user``, nobody touched it, and it broke.
  Its router only registers when ``ENVIRONMENT == "local"``, so it is dev and
  test that carry it.

Both failures are invisible to a reviewer reading the diff that adds the field,
because the producer that breaks is in a file the diff does not touch. Only an
enumeration catches that — which is why this is a test and not a convention.

HOW THE SET IS DERIVED
----------------------
Nothing here is a hand-written route list; a list is exactly the document that
is green on the day the next producer is added.

1. **The model family** is read from ``app.models``: ``UserPublic``, every
   subclass of it, and every model declaring a field whose annotation mentions
   ``UserPublic`` (the list wrapper ``UsersPublic``, which has the identical
   trap one level down).
2. **The producers** are found by parsing ``app/api/routes/`` and keeping every
   router-decorated function whose ``response_model=`` names a family member.
   The *source tree* rather than the live app on purpose: a conditionally
   registered router — ``private``, again — is absent from ``app.routes``
   whenever the suite runs with a different ``ENVIRONMENT``, and a test that
   silently stops covering the one route that already broke is worse than no
   test. The live app is then cross-checked against the walk (see
   ``test_the_route_walk_sees_every_registered_producer``) so a routes package
   moved out from under the walk fails loudly instead of emptying it.
3. **The rule** — every ``return`` in such a handler must be an expression with
   a call to the builder somewhere inside it. That accepts all three shapes in
   use (the bare call, a ``**builder(...).model_dump()`` spread into a richer
   model, and a comprehension inside a list wrapper) and rejects ``return
   user``.

WHAT THIS TEST DOES NOT SEE
---------------------------
Static and syntactic, so: a handler delegating to a helper that itself returns a
raw row, a ``response_model`` computed rather than named, a route registered by
``add_api_route`` instead of a decorator, and anything reaching the client
outside a ``response_model`` (a hand-built ``JSONResponse``). Green here means
no *statically visible* producer bypasses the builder.
"""
from __future__ import annotations

import ast
import pathlib
import sys
import typing

import pytest
from fastapi.routing import APIRoute

from app.api.routes import _user_public
from app.main import app
from app.models import User, UserPublic

# The builder, and the alias ``routes/users.py`` imports it under. Both are
# asserted to exist below, so a rename fails here instead of quietly making
# every producer look non-compliant (or, worse, compliant).
BUILDER_NAMES = {"user_to_public", "_user_to_public"}


def _routes_package_dir() -> pathlib.Path:
    """The on-disk ``app/api/routes`` directory."""
    from app.api import routes

    return pathlib.Path(routes.__file__).parent


def _subclasses_of(cls: type) -> set[type]:
    """``cls`` and every class transitively deriving from it."""
    found = {cls}
    for subclass in cls.__subclasses__():
        found |= _subclasses_of(subclass)
    return found


def _user_public_family() -> set[str]:
    """Names of the response models that carry a ``UserPublic`` payload.

    Derived, not listed: a new subclass or a new list wrapper joins the
    enumeration by existing.

    Two sources, because neither alone is complete. Subclasses come from
    ``__subclasses__`` rather than from ``app.models``'s namespace —
    ``UserPublicWithAICredentials`` is *not* re-exported there, so reading the
    package namespace silently dropped the one producer that hand-builds its
    response. Containers are found by scanning the loaded model modules, since
    a wrapper is not a subclass of anything.
    """
    family = {cls.__name__ for cls in _subclasses_of(UserPublic)}

    model_modules = [
        module
        for name, module in list(sys.modules.items())
        if name == "app.models" or name.startswith("app.models.")
    ]
    for module in model_modules:
        for name, obj in vars(module).items():
            if not isinstance(obj, type) or name in family:
                continue
            fields = getattr(obj, "model_fields", None)
            if not fields:
                continue
            # A container of them — ``UsersPublic.data: list[UserPublic]``.
            for field in fields.values():
                if _annotation_carries_user_public(field.annotation):
                    family.add(name)
                    break
    return family


def _annotation_carries_user_public(annotation: object) -> bool:
    """Whether ``annotation`` resolves to (or wraps) a ``UserPublic``.

    Resolved through ``typing.get_args`` rather than matched as text: a
    substring test also fires on the unrelated ``SharedUserPublic``, which
    would drag half the credentials routes into the enumeration and fail them
    for not calling a builder that has nothing to do with them.
    """
    if isinstance(annotation, type):
        return issubclass(annotation, UserPublic)
    return any(
        _annotation_carries_user_public(arg) for arg in typing.get_args(annotation)
    )


def _decorator_response_model_names(node: ast.AST) -> set[str]:
    """Every class name appearing in a router decorator's ``response_model=``.

    A **set**, and gathered by walking the expression, because the annotation is
    not always a bare name: ``list[UserPublic]`` and ``UserPublic | None`` are
    both written literally in this codebase (20-odd routes use the former shape
    today) and both parse to a ``Subscript`` / ``BinOp``. Matching only
    ``ast.Name`` would drop such a producer out of the enumeration silently —
    and the live-app cross-check below would miss it in exactly the same
    direction, so the two halves would fail open together.
    """
    names: set[str] = set()
    for decorator in getattr(node, "decorator_list", []):
        if not isinstance(decorator, ast.Call):
            continue
        for keyword in decorator.keywords:
            if keyword.arg != "response_model":
                continue
            for inner in ast.walk(keyword.value):
                if isinstance(inner, ast.Name):
                    names.add(inner.id)
    return names


def _calls_builder(expression: ast.AST) -> bool:
    """Whether the builder is called anywhere inside ``expression``."""
    for node in ast.walk(expression):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else None
        )
        if name in BUILDER_NAMES:
            return True
    return False


def _returns_of(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.AST]:
    """Every value-bearing ``return`` in ``func``'s own body.

    Nested function definitions are skipped: a closure's return is not this
    handler's response.
    """
    returns: list[ast.AST] = []
    stack: list[ast.AST] = list(func.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Return):
            if node.value is not None:
                returns.append(node.value)
            continue
        stack.extend(ast.iter_child_nodes(node))
    return returns


def _producers() -> list[tuple[str, str, str, ast.FunctionDef]]:
    """``(module, function, response_model, node)`` for every family producer."""
    family = _user_public_family()
    found: list[tuple[str, str, str, ast.FunctionDef]] = []
    for path in sorted(_routes_package_dir().rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            declared = _decorator_response_model_names(node) & family
            if declared:
                found.append((path.stem, node.name, ", ".join(sorted(declared)), node))
    return found


# ── The anchors ────────────────────────────────────────────────────────


def test_the_anchors_this_derivation_rests_on_exist() -> None:
    """Every hand-written name is real, so a rename cannot empty the walk."""
    assert callable(_user_public.user_to_public)
    assert "can_change_email" in UserPublic.model_fields

    # The trap is only worth a test while it is real: a field that the row
    # cannot supply and the model will not default. If this ever stops being
    # true, coercion becomes merely wrong rather than fatal — still a bug, but
    # the failure mode this file describes has changed and the prose above
    # needs rewriting before the assertion is relaxed.
    assert UserPublic.model_fields["can_change_email"].is_required()
    assert "can_change_email" not in User.model_fields

    family = _user_public_family()
    assert {"UserPublic", "UserPublicWithAICredentials", "UsersPublic"} <= family

    # An empty parametrize list is *skipped* by pytest, not failed — it would
    # read as benign in a green summary while enforcing nothing. Pin the two
    # producers this file was written for by name, and a floor besides.
    walked = {(module, function) for module, function, _, _ in _producers()}
    assert {("private", "create_user"), ("login", "test_token")} <= walked
    assert len(walked) >= 10, walked


def test_the_route_walk_sees_every_registered_producer() -> None:
    """The source walk is a superset of what the live app actually serves.

    Guards the derivation itself: a routes module relocated out of the walked
    package would otherwise leave this file green while covering nothing.
    """
    walked = {(module, function) for module, function, _, _ in _producers()}
    family = _user_public_family()

    registered = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        # The same resolver the model fields use, not ``isinstance(model, type)``:
        # a live ``response_model=list[UserPublic]`` is a generic alias, not a
        # class, and an identity test would skip it here just as a bare-``Name``
        # match would skip it in the source walk.
        if _annotation_carries_user_public(getattr(route, "response_model", None)):
            registered.add(
                (route.endpoint.__module__.rsplit(".", 1)[-1], route.endpoint.__name__)
            )

    assert family, "The model family is empty — the derivation is inert."
    assert registered, "No live route answers with a UserPublic — walk is inert."
    assert registered <= walked, f"Not covered by the source walk: {registered - walked}"


# ── The rule ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("module", "function", "model", "node"),
    [pytest.param(*p, id=f"{p[0]}.{p[1]}") for p in _producers()],
)
def test_every_user_public_producer_goes_through_the_builder(
    module: str, function: str, model: str, node: ast.FunctionDef
) -> None:
    """A handler answering with a ``UserPublic`` must not hand back a raw row.

    The enrolment flags, the resend timestamp and ``can_change_email`` are not
    on the row; only ``user_to_public`` knows how to produce them.
    """
    returns = _returns_of(node)
    assert returns, (
        f"{module}.{function} declares response_model={model} but returns "
        f"nothing — check the walk, not the route."
    )
    offenders = [
        ast.unparse(expression)
        for expression in returns
        if not _calls_builder(expression)
    ]
    assert not offenders, (
        f"{module}.{function} answers with {model} without going through "
        f"user_to_public: {offenders}. Returning the ORM row lets FastAPI "
        f"coerce it, which fills the derived fields from their defaults and "
        f"500s on can_change_email, which has none. Use "
        f"`user_to_public(session, user)` (see app/api/routes/_user_public.py)."
    )
