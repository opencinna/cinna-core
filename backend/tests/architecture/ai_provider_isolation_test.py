"""Architecture drift test — ``ai_provider`` is reachable from two modules.

WHY THIS EXISTS
---------------
``ai_provider`` holds secrets of two categorically different kinds: a model API
key for a ``fixed_key`` provider, and an organisation administration secret — the
power to create and destroy keys for a whole provider organisation — for a
``minted`` one. The table is separate from ``ai_credential`` for one reason, and
it is a structural reason rather than an intentional one:

- ``model_discovery_service`` selects **every** row in ``ai_credential`` on a
  cron and calls the vendor with each key;
- ``external_account_config_service`` hands **every** row a user owns,
  decrypted, to the desktop client;
- sharing, environment linking, bundle publisher wiring and the blast-radius
  counts are all foreign keys pinned to ``ai_credential.id``, and the
  environment credential bag is a fixed slot dict with no slot to pour into.

None of them has a bug. They are correct for the table they read. What keeps an
administration secret out of the desktop client is that it lives in a table
those queries cannot name — which stops being true the first time an unrelated
module learns to query ``AIProvider``.

    THE PROPERTY
    ------------
    No module in ``app/`` queries :class:`AIProvider` outside
    ``app/services/credentials/ai_providers_service.py`` (which owns the table)
    and ``app/services/credentials/key_provisioning_service.py`` (which mints
    and revokes with the secret).

Everything else asks the provider service for a row, or
``provisioning_policy.resolve_policy`` for a policy.

WHAT COUNTS AS A QUERY
----------------------
Three syntactic shapes, chosen because they are what actually reaches the table:

* ``select(AIProvider …)`` — including ``select(AIProvider.column)``;
* ``<x>.get(AIProvider, …)`` — the identity-map fetch by primary key;
* any attribute access on the class, ``AIProvider.provider_type``, which is how
  a column reaches a ``where`` / ``order_by`` clause.

A bare ``import`` is **not** a query and is deliberately not flagged: models are
re-exported from ``app/models/__init__.py``, DTOs are constructed by routes, and
``isinstance`` checks are not table access.

WHAT THIS TEST DOES NOT SEE
---------------------------
Static and syntactic, like every other file in this directory. A query built
through ``getattr``, a table named as a string in raw SQL, or a model resolved
out of a registry dict is invisible here. Green means no *statically visible*
query against ``AIProvider`` exists outside the two modules that own one.

THERE ARE NO PENDING VIOLATIONS LEFT
------------------------------------
There used to be a :data:`PENDING` table naming modules that still queried the
table with the phase that would stop each, asserted by **equality** so that a
landed phase failed this file instead of leaving a stale excuse. It worked as
intended three times: ``account_provisioning_service`` went in Phase 3, and
``provider_admin_credentials_service`` and ``admin_provider_credentials`` went in
Phase 4 with the ``/admin/provider-admin-credentials`` route they served. The
table is gone with its last entry rather than left empty — an empty allowlist is
an invitation to add to it — and the property below is now asserted with no
exceptions at all.
"""
from __future__ import annotations

import ast
import pathlib

MODEL = "AIProvider"

#: The two modules that legitimately reach the table. See the module docstring.
ALLOWED = frozenset(
    {
        "app/services/credentials/ai_providers_service.py",
        "app/services/credentials/key_provisioning_service.py",
    }
)

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


def _names_the_model(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id == MODEL


def _queries_in(path: pathlib.Path) -> list[tuple[str, int]]:
    """``(shape, lineno)`` for every statically visible query in one file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            is_select = isinstance(func, ast.Name) and func.id == "select"
            is_get = isinstance(func, ast.Attribute) and func.attr == "get"
            if not (is_select or is_get):
                continue
            args = node.args[:1] if is_get else node.args
            for arg in args:
                if _names_the_model(arg) or (
                    isinstance(arg, ast.Attribute) and _names_the_model(arg.value)
                ):
                    found.append(
                        ("select" if is_select else "get", node.lineno)
                    )
                    break
        elif isinstance(node, ast.Attribute) and _names_the_model(node.value):
            found.append((f"{MODEL}.{node.attr}", node.lineno))
    return found


# ── The walk must find something before it can prove anything ──────────


def test_the_detector_sees_the_queries_in_the_owning_service() -> None:
    """A guard against a silently inert test.

    ``AIProvidersService`` is built around ``session.get(AIProvider, …)`` and
    ``select(AIProvider)``. If the detector stops recognising those — an AST
    change, a rename, a switch to a different query builder — this file would
    report zero violations forever while enforcing nothing.
    """
    owner = _APP / "services/credentials/ai_providers_service.py"
    shapes = {shape for shape, _line in _queries_in(owner)}
    assert "select" in shapes and "get" in shapes, (
        "The detector found no select/get against AIProvider in the service "
        f"that owns the table. It is inert. Saw: {sorted(shapes)}"
    )


# ── The property ───────────────────────────────────────────────────────


def test_only_the_provider_and_mint_services_query_ai_provider() -> None:
    """The property, with no exceptions.

    Every module that used to be excused has been deleted or rewritten, so this
    is the whole gate: a module that learns to query ``AIProvider`` fails here
    on the commit that teaches it.
    """
    offenders = {
        _rel(path): _queries_in(path)
        for path in _source_files()
        if _rel(path) not in ALLOWED and _queries_in(path)
    }
    assert not offenders, (
        "These modules query AIProvider. The table holds organisation "
        "administration secrets and ordinary model keys, and it is isolated "
        "from the ai_credential plumbing structurally — by being a table those "
        "queries cannot name. Ask ai_providers_service for a row, or "
        "provisioning_policy.resolve_policy for a policy:\n"
        + "\n".join(
            f"  {module}: "
            + ", ".join(f"{shape} (line {line})" for shape, line in hits)
            for module, hits in sorted(offenders.items())
        )
    )
