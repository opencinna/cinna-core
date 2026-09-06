"""Architecture drift test — one door into existence, one attributable grant.

WHY THIS EXISTS
---------------
Zero-touch onboarding phase 2 collapsed three hand-rolled ``User(...)``
constructions — the signup path, the Google path, and the passwordless branch
of ``create_external_user`` — into one function,
``UserService.create_account(origin=…)``. The three had already drifted: only
one normalised the address, only two derived the default role through
``RoleService``, none agreed about ``email_confirmed``. Auto-provisioning of
the company's managed AI credentials now hangs off that one function, so a
fourth construction added later would produce an account that exists, can log
in, and silently has none of the company keys every other account gets.

That failure is invisible in the diff that causes it. The new code looks
correct in isolation; what is wrong is everything it *doesn't* do, in a file
the reviewer is not reading. Only an enumeration catches that, which is why
this is a test and not a convention. The behavioural half — that each of the
five reachable origins really is provisioned — lives in
``tests/api/users/users_auto_provision_origins_test.py``; this file is what
stops a *sixth* path from being added without joining it.

The second rule here guards the audit trail rather than the feature.
``ManagedAICredentialsService.add_members`` takes a keyword-only ``actor``
with no default, precisely so "who did this" is a decision at every call site
instead of an omission. ``actor=None`` means *the system did it, at account
creation* — legitimate exactly once, in ``AccountProvisioningService``. A route
reaching it would be writing an unattributed grant of a company API key, which
is what the audit trail exists to prevent.

HOW THE SETS ARE DERIVED
------------------------
By parsing ``backend/app/``, not from a hand-written list — a list is the
document that is green on the day the next offender is added. Two directories
are excluded and both exclusions are deliberate:

* ``app/env-templates/`` — a separate application copied into agent
  containers. It has its own models and is not this backend.
* ``app/alembic/versions/`` — migrations run outside the application and
  legitimately manipulate rows directly. A data migration is not an account
  arrival path.

WHAT THIS TEST DOES NOT SEE
---------------------------
Static and syntactic. A row built through ``SQLModel.model_validate``, through
a helper aliased under another name, or by raw SQL is invisible here — the
first of those is checked separately below because it is the shape the old
``create_user`` actually used; the others are not. Green means no *statically
visible* second constructor exists.
"""
from __future__ import annotations

import ast
import inspect
import pathlib

import pytest

from app.models.users.user import AccountOrigin, User
from app.services.credentials.managed_ai_credentials_service import (
    ManagedAICredentialsService,
)
from app.services.users.user_service import UserService

# The one place a ``User`` row may be constructed, as (path suffix, function).
CHOKEPOINT_FILE = "services/users/user_service.py"
CHOKEPOINT_FUNCTION = "create_account"

_EXCLUDED_DIRS = ("env-templates", "alembic")


def _app_dir() -> pathlib.Path:
    import app

    return pathlib.Path(app.__file__).parent


def _app_modules() -> list[pathlib.Path]:
    """Every backend source file, minus the two deliberate exclusions."""
    root = _app_dir()
    return [
        path
        for path in sorted(root.rglob("*.py"))
        if not any(part in _EXCLUDED_DIRS for part in path.relative_to(root).parts)
    ]


def _relative(path: pathlib.Path) -> str:
    return str(path.relative_to(_app_dir()))


def _enclosing_function(tree: ast.AST, node: ast.AST) -> str | None:
    """The name of the function ``node`` sits inside, if any."""
    for candidate in ast.walk(tree):
        if not isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(candidate):
            if inner is node:
                return candidate.name
    return None


def _called_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _calls_named(name: str) -> list[tuple[str, str | None, ast.Call, ast.AST]]:
    """``(module, enclosing function, call node, tree)`` for every call to ``name``."""
    found: list[tuple[str, str | None, ast.Call, ast.AST]] = []
    for path in _app_modules():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _called_name(node) == name:
                found.append((_relative(path), _enclosing_function(tree, node), node, tree))
    return found


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


# ── The anchors ────────────────────────────────────────────────────────


def test_the_anchors_this_derivation_rests_on_exist() -> None:
    """Every hand-written name is real, so a rename cannot empty the walk."""
    modules = _app_modules()
    assert len(modules) >= 100, (
        f"Only {len(modules)} source files were walked — the tree moved out "
        f"from under this test and every rule below is inert."
    )
    assert CHOKEPOINT_FILE in {_relative(path) for path in modules}

    # The chokepoint, and the two things that make it one: it demands an
    # origin, and the origin is an enum rather than a string a caller can
    # invent.
    signature = inspect.signature(UserService.create_account)
    assert "origin" in signature.parameters
    assert signature.parameters["origin"].default is inspect.Parameter.empty
    assert {member.value for member in AccountOrigin} == {
        "signup", "google", "invite", "admin", "external", "seed",
    }

    # ``User`` really is the table model, so counting its constructions is
    # counting account rows.
    assert User.model_config.get("table") is True or hasattr(User, "__tablename__")


# ── Rule 1: one constructor ────────────────────────────────────────────


def test_a_user_row_is_constructed_in_exactly_one_place() -> None:
    """``User(...)`` appears once in the backend, at the chokepoint.

    A second construction is an account-arrival path that skips address
    normalisation, the ``is_superuser ⇒ admin`` invariant, the policy-derived
    default role and — the reason phase 2 exists — auto-provisioning of the
    company's managed AI credentials. The account would work; it would just
    quietly have no keys.
    """
    constructions = [
        (module, function)
        for module, function, _, _ in _calls_named("User")
    ]

    assert constructions == [(CHOKEPOINT_FILE, CHOKEPOINT_FUNCTION)], (
        f"A User row is constructed outside the chokepoint: {constructions}. "
        f"Every arrival path must go through "
        f"UserService.create_account(origin=…) — see "
        f"app/services/users/user_service.py."
    )


def test_no_user_row_is_built_by_validating_another_model_into_it() -> None:
    """The other way to build the row, and the one the old code used.

    ``create_user`` used to be ``User.model_validate(user_create, update=…)``.
    That is a construction as much as ``User(...)`` is, and it bypasses the
    chokepoint identically — so it is enumerated too rather than left as the
    obvious way around rule 1.
    """
    offenders = []
    for path in _app_modules():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute):
                continue
            if func.attr not in {"model_validate", "model_construct"}:
                continue
            if isinstance(func.value, ast.Name) and func.value.id == "User":
                offenders.append((_relative(path), _enclosing_function(tree, node)))

    assert offenders == [], (
        f"A User row is built by validation outside the chokepoint: "
        f"{offenders}. Use UserService.create_account(origin=…)."
    )


def test_every_call_into_the_chokepoint_names_an_origin() -> None:
    """``origin`` is what makes "which path is this" expressible.

    It selects whether the access-policy gate applies at all and it is stamped
    on the auto-provisioning audit event. A call that omitted it would fail at
    runtime (the parameter has no default, asserted in the anchors) — this
    catches the near-miss instead: a call that passes it positionally or under
    another name would be a refactor nobody meant.
    """
    calls = _calls_named("create_account")
    assert calls, "No call to create_account was found — the walk is inert."

    offenders = [
        (module, function)
        for module, function, call, _ in calls
        if _keyword(call, "origin") is None
    ]
    assert offenders == [], (
        f"create_account called without a named origin= from: {offenders}"
    )


def test_the_first_superuser_bootstrap_uses_the_seed_origin() -> None:
    """``core.db.init_db`` is the only caller carrying ``AccountOrigin.SEED``.

    ``seed`` is ungated by the access policy for a reason that has no HTTP
    equivalent: the first superuser must be creatable on an instance whose
    policy row does not exist yet. Pinned statically because the runtime path
    runs once per installation and can never be replayed by a request — the
    behavioural side of that origin is covered in
    ``tests/api/users/users_auto_provision_origins_test.py`` through the
    documented Rule-1 exemption.
    """
    seed_calls = [
        (module, function)
        for module, function, call, _ in _calls_named("create_user")
        if (origin := _keyword(call, "origin")) is not None
        and "SEED" in ast.unparse(origin)
    ]
    assert seed_calls == [("core/db.py", "init_db")], (
        f"Expected exactly the first-superuser bootstrap to use the seed "
        f"origin; found {seed_calls}. An ungated origin anywhere else is a "
        f"path around the access policy."
    )


# ── Rule 2: no unattributed grant from a route ─────────────────────────


def test_add_members_forces_every_caller_to_name_an_actor() -> None:
    """``actor`` is keyword-only with no default, and that is load-bearing.

    A default of ``None`` would make an unattributed grant the thing you get by
    forgetting, which is the opposite of what an audit trail is for.
    """
    signature = inspect.signature(ManagedAICredentialsService.add_members)
    actor = signature.parameters.get("actor")
    assert actor is not None, "add_members lost its actor parameter."
    assert actor.kind is inspect.Parameter.KEYWORD_ONLY
    assert actor.default is inspect.Parameter.empty, (
        "actor acquired a default — 'who did this' is now something a caller "
        "can omit rather than decide."
    )


def test_no_route_grants_a_managed_credential_without_an_actor() -> None:
    """``actor=None`` means *the system*, and a route is never the system.

    Routes always act as the signed-in superuser; the one legitimate
    ``actor=None`` is ``AccountProvisioningService``, granting at account
    creation where there is no admin in the story. A route reaching it would
    write a grant of a company API key that the security feed attributes to
    nobody.
    """
    calls = _calls_named("add_members")
    assert calls, "No call to add_members was found — the walk is inert."

    system_callers = {
        module
        for module, _, call, _ in calls
        if (actor := _keyword(call, "actor")) is not None
        and isinstance(actor, ast.Constant)
        and actor.value is None
    }
    assert system_callers == {"services/users/account_provisioning_service.py"}, (
        f"Unattributed add_members grants come from: {system_callers}. Only "
        f"the account-creation path may pass actor=None."
    )

    route_calls = [
        (module, function)
        for module, function, _, _ in calls
        if module.startswith("api/routes/")
    ]
    assert route_calls == [], (
        f"A route module calls add_members directly: {route_calls}. Routes go "
        f"through the service methods that pass the acting superuser."
    )


# ── Rule 3: the creation path imports no provider client ───────────────


@pytest.mark.parametrize(
    "module_path",
    [
        "services/users/user_service.py",
        "services/users/account_provisioning_service.py",
    ],
)
def test_the_creation_path_does_not_import_a_provider_client(
    module_path: str,
) -> None:
    """A cheap static echo of the runtime guard, on the two files phase 2 owns.

    The real proof is behavioural — ``tests/api/users/
    users_auto_provision_resilience_test.py`` runs every creation path under
    an httpx-transport tripwire, which no import can dodge. This adds the
    earliest possible warning for the most likely regression: somebody
    reaching for a provider SDK in the chokepoint or the provisioning service
    itself, where phase 5's key minting will be tempting to inline.

    Scoped to these two files on purpose. ``auth_service`` legitimately
    imports ``httpx`` for the Google *identity* exchange, which is a different
    thing from an AI provider and is already awaited on the login path today;
    widening this rule to it would make the test wrong rather than strict.
    """
    forbidden = {"httpx", "requests", "aiohttp", "urllib3", "openai", "anthropic"}
    tree = ast.parse((_app_dir() / module_path).read_text())

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    offenders = imported & forbidden
    assert offenders == set(), (
        f"{module_path} imports {sorted(offenders)}. Account creation runs "
        f"inline on the signup and OAuth-callback requests, where a provider "
        f"timeout becomes a failed login. Phase 5's minting goes in a "
        f"background task (zero-touch-onboarding invariant 1)."
    )
