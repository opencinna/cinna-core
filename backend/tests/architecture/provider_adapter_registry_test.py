"""
Architecture drift test — per-provider tables live in the adapter package.

WHY THIS EXISTS
---------------
"Which SDK engine does this provider use", "what is it called", "which bag slot
holds its key", "which fields does it require" were each answered independently
in several modules — one of them byte-identically in two files, one of them
twice inside a single module. Adding a sixth provider meant finding all of them.
``app/services/ai_providers/`` exists so each of those questions has exactly one
answer, and this file makes the property executable.

THE PROPERTY
------------
    No dictionary keyed on ``AICredentialType`` members may be declared outside
    ``app/services/ai_providers/``.

**The allowlist is empty and is meant to stay empty.** If a new table is
genuinely provider-shaped, it belongs on the adapter; if it is not
provider-shaped, it is not keyed on ``AICredentialType``.

WHY THIS PROPERTY AND NOT "NO AICredentialType COMPARISONS"
-----------------------------------------------------------
The obvious alternative — forbidding ``cred.type == AICredentialType.X`` outside
the package — was measured and rejected. At the time this landed there were 26
such comparisons across nine modules against a one-module forbidden zone, so the
test would have needed a six-file allowlist: not a property, just a list of the
files someone had not got to. Worse, it would have been the wrong property. A
scattered ``== ANTHROPIC`` inside a feature is provider-specific *logic*, which
legitimately lives with its feature. A per-provider **table** is the thing that
forces an edit in N files when a provider is added, and it is a table precisely
because it enumerates every provider — so it is exactly what must be centralised.

It also misses what it most needs to catch: passing ``AICredentialType.OPENAI``
as an *argument* is not a comparison, so five hardcoded provider constants in one
route handler would have sailed through a comparison-only check.

WHAT THIS TEST DOES NOT SEE
---------------------------
Static and syntactic. A dict built by a comprehension over the registry is
allowed on purpose — it is derived from the one declaration, not a second one.

**The known hole: dicts keyed on the enum's string values.** ``{"anthropic": …,
"openai": …}`` is a per-provider table, and no AST check can tell it from any
other string-keyed dict without guessing. Two such tables exist today, both in
``environment_lifecycle._write_opencode_config``: the map from our provider name
to OpenCode's provider id (``openai_compatible`` → ``custom``), and the dispatch
from a provider name to the already-extracted local key variable. The first is
genuinely provider-shaped and would belong on the adapter; the second is
dispatch over locals and would not. Neither was absorbed in the pass that
created this package, and widening the predicate to catch them would mean the
allowlist stops being empty — so this is recorded here rather than silently
tolerated. Green means no per-provider table keyed on the enum *members* is
declared outside the package.
"""
from __future__ import annotations

import ast
import os
import pathlib

import pytest

from app.models.credentials.ai_credential import AICredentialType
from app.models.credentials.provider_admin_credential import (
    ProviderAdminCredentialConfig,
)
from app.services.ai_providers import registry

_HERE = pathlib.Path(__file__).resolve()
_BACKEND_ROOT = _HERE.parent.parent.parent          # backend/
APP_ROOT = _BACKEND_ROOT / "app"
ADAPTER_PACKAGE = APP_ROOT / "services" / "ai_providers"

# Vendored subtrees — full agent-runtime virtualenvs, ~10k files that can never
# hold a first-party provider table. Pruned at the directory level so the scan
# stays sub-second (same reasoning as patch_target_drift_test).
_PRUNED_DIR_NAMES = frozenset(
    {"env-templates", ".venv", "site-packages", "node_modules", "__pycache__"}
)

# Modules outside the adapter package that may still declare an
# AICredentialType-keyed dict. EMPTY BY DESIGN — see the module docstring before
# adding anything here.
ALLOWED_PROVIDER_TABLE_MODULES: set[str] = set()


def _iter_source_files(root: pathlib.Path) -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _PRUNED_DIR_NAMES]
        for filename in filenames:
            if filename.endswith(".py"):
                files.append(pathlib.Path(dirpath) / filename)
    return files


def _module_dotted_path(path: pathlib.Path, base: pathlib.Path) -> str:
    rel = path.resolve().relative_to(base.resolve()).with_suffix("")
    return ".".join(rel.parts)


def _is_credential_type_member(node: ast.expr | None) -> bool:
    """``AICredentialType.ANTHROPIC`` and friends, however the module aliases it."""
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "AICredentialType"
    )


def _find_provider_tables(
    root: pathlib.Path | None = None,
    base: pathlib.Path | None = None,
    exclude: pathlib.Path | None = None,
    allowlist: set[str] | None = None,
) -> list[tuple[str, int, int]]:
    """Every dict literal with an ``AICredentialType`` member as a key.

    Returns ``(module, lineno, key_count)`` for each. Parameterised on its roots
    only so the meta-test can point the whole scan — pruning, exclusion and
    allowlist included — at a planted fixture tree; production callers pass
    nothing.
    """
    root = root or APP_ROOT
    base = base or _BACKEND_ROOT
    exclude = exclude if exclude is not None else ADAPTER_PACKAGE
    allowlist = allowlist if allowlist is not None else ALLOWED_PROVIDER_TABLE_MODULES

    found: list[tuple[str, int, int]] = []
    for path in sorted(_iter_source_files(root)):
        if exclude in path.resolve().parents:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        module = _module_dotted_path(path, base)
        if module in allowlist:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            if any(_is_credential_type_member(key) for key in node.keys):
                found.append((module, node.lineno, len(node.keys)))
    return found


def test_no_provider_table_is_declared_outside_the_adapter_package() -> None:
    """The property. See the module docstring."""
    offenders = _find_provider_tables()
    detail = "\n  ".join(
        f"{module}:{lineno} ({keys} keys)" for module, lineno, keys in offenders
    )
    assert not offenders, (
        f"{len(offenders)} dictionary/-ies keyed on AICredentialType are declared "
        f"outside app/services/ai_providers/:\n  {detail}\n\n"
        f"A table that enumerates every provider is the thing that forces an edit "
        f"in N files when a provider is added. Put the fact on the adapter "
        f"(app/services/ai_providers/<provider>.py) and read it back through "
        f"registry.get_adapter(...) / registry.all_adapters(). If the consumer "
        f"genuinely needs a mapping, DERIVE it with a comprehension over "
        f"all_adapters() — that is one declaration read twice, not two "
        f"declarations.\n\n"
        f"ALLOWED_PROVIDER_TABLE_MODULES is empty by design; adding to it is "
        f"conceding the property, not satisfying it."
    )


_PLANTED_TABLE = (
    "TABLE = {AICredentialType.ANTHROPIC: 1, AICredentialType.OPENAI: 2}\n"
)


def test_the_scan_finds_a_planted_table(tmp_path: pathlib.Path) -> None:
    """The whole scan — walk, exclusion and allowlist — is exercised end to end.

    Without this, a scan that silently stopped walking (a pruned directory name
    that starts matching everything, a root that moved, an exclusion that
    swallowed the tree) would report zero offenders forever and read as a pass.
    Asserting the AST predicate alone would not catch any of those.
    """
    pkg = tmp_path / "pkg"
    (pkg / "sub").mkdir(parents=True)
    (pkg / "offender.py").write_text(_PLANTED_TABLE)
    (pkg / "sub" / "nested.py").write_text(_PLANTED_TABLE)
    (pkg / "innocent.py").write_text("TABLE = {'anthropic': 1}\n")

    found = _find_provider_tables(
        root=tmp_path, base=tmp_path, exclude=tmp_path / "nowhere", allowlist=set()
    )
    assert sorted(module for module, _, _ in found) == [
        "pkg.offender",
        "pkg.sub.nested",
    ], f"the scan did not find both planted tables (and only those): {found}"


def test_the_scan_honours_its_exclusion_and_allowlist(tmp_path: pathlib.Path) -> None:
    """Both escape hatches work — so "zero offenders" is not one of them firing."""
    (tmp_path / "adapters").mkdir()
    (tmp_path / "adapters" / "a.py").write_text(_PLANTED_TABLE)
    (tmp_path / "allowed.py").write_text(_PLANTED_TABLE)
    (tmp_path / "offender.py").write_text(_PLANTED_TABLE)

    found = _find_provider_tables(
        root=tmp_path,
        base=tmp_path,
        exclude=tmp_path / "adapters",
        allowlist={"allowed"},
    )
    assert [module for module, _, _ in found] == ["offender"]


def test_the_real_scan_walks_a_non_empty_tree() -> None:
    """The production roots resolve to actual source files."""
    files = _iter_source_files(APP_ROOT)
    assert files, "the app source walk returned nothing"
    assert ADAPTER_PACKAGE.is_dir(), "the adapter package path no longer exists"


@pytest.fixture(autouse=True)
def registry_override_leaks_nothing():
    """No test may leave an adapter override installed.

    ``override_for_tests`` restores the previous mapping in a ``finally``, so
    nothing leaks today. The point is what the rest of this file would do if
    something did: every property here is asserted against
    ``registry.all_adapters()`` and ``registry.find_adapter(...)``, both of which
    return the *override* when one is installed. A leaked stub that happened to
    satisfy the contract would make these tests pass while asserting nothing
    about the real adapters — assurance that is not merely absent but
    affirmatively false.

    Checked before as well as after, so a leak from an earlier module is
    attributed to the module that caused it rather than to the next one.
    """
    assert not registry._overrides, (
        f"a provider adapter override leaked into this test from elsewhere: "
        f"{sorted(t.value for t in registry._overrides)}"
    )
    yield
    assert not registry._overrides, (
        f"this test left a provider adapter override installed: "
        f"{sorted(t.value for t in registry._overrides)}. Every override must go "
        f"through registry.override_for_tests, which restores on exit."
    )


def test_registry_serves_every_credential_type() -> None:
    """Every enum member resolves to an adapter — derived from the enum.

    Deliberately not a literal list of the five types we happen to have: a
    literal list is green on the day a sixth is added, which is the exact moment
    this needs to fail.
    """
    missing = [
        cred_type.value
        for cred_type in AICredentialType
        if registry.find_adapter(cred_type) is None
    ]
    assert not missing, (
        f"AICredentialType members with no adapter: {missing}. Add a module under "
        f"app/services/ai_providers/ and register it in registry._ADAPTERS."
    )


def test_registry_order_follows_the_enum() -> None:
    """``_ADAPTERS`` iteration order is load-bearing, so pin it.

    ``make_empty_credential_bag`` derives the credential bag's key order from
    ``all_adapters()``. Reordering the registry would silently reorder the bag,
    which anything comparing two bags would notice and nothing else would.
    """
    assert registry.supported_types() == list(AICredentialType), (
        "registry._ADAPTERS must be declared in AICredentialType order — the "
        "credential bag's key order is derived from it."
    )


def test_every_adapter_declares_the_full_contract() -> None:
    """An adapter missing a field fails here, not at the call site that reads it.

    A ``BaseProviderAdapter`` subclass that forgets to set ``sdk_engine`` raises
    ``AttributeError`` only when something asks for it — possibly in a code path
    only one provider takes.
    """
    required = (
        "type",
        "label",
        "account_config_display_name",
        "account_config_slug",
        "sdk_engine",
        "bag_key_api_key",
        "requires_base_url",
        "requires_model",
        "supports_model_listing",
        "issues_oauth_tokens",
        "key_provisioner",
    )
    problems: list[str] = []
    for adapter in registry.all_adapters():
        name = type(adapter).__name__
        for field in required:
            if not hasattr(adapter, field):
                problems.append(f"{name} is missing '{field}'")
        # Derived answers must not raise either.
        try:
            engine, provider = adapter.catalog_engine_provider
            if not engine or not provider:
                problems.append(
                    f"{name}.sdk_engine={adapter.sdk_engine!r} does not split into "
                    f"an (engine, provider) pair"
                )
        except AttributeError as exc:
            problems.append(f"{name}.catalog_engine_provider raised {exc!r}")
        if not adapter.bag_keys:
            problems.append(f"{name} declares no credential-bag slots")
    assert not problems, "\n".join(problems)


def test_adapter_types_are_unique_and_match_their_registry_slot() -> None:
    """No two adapters claim the same type, and none is filed under the wrong one."""
    mismatched = [
        f"{type(adapter).__name__} declares type={adapter.type!r} but is registered "
        f"under {cred_type!r}"
        for cred_type, adapter in zip(
            registry.supported_types(), registry.all_adapters(), strict=True
        )
        if adapter.type != cred_type
    ]
    assert not mismatched, "\n".join(mismatched)


def test_a_declared_provisioner_implements_the_whole_contract() -> None:
    """``supports_minting`` must not be able to promise a half-written provisioner.

    Replaces the earlier "no adapter claims minting in this pass" pin, which was
    a placeholder for exactly this test and said so. The property it was
    protecting is unchanged — ``supports_minting`` must never be True for
    something that cannot actually mint — but it can now be stated positively
    instead of by forbidding the feature.
    """
    problems: list[str] = []
    for adapter in registry.all_adapters():
        provisioner = adapter.key_provisioner
        if provisioner is None:
            assert not adapter.supports_minting
            continue
        for method in (
            "mint",
            "revoke",
            "verify_admin_access",
            "verify_spend_limit",
            "config_schema",
        ):
            if not callable(getattr(provisioner, method, None)):
                problems.append(
                    f"{type(adapter).__name__}'s provisioner has no {method}()"
                )
    assert not problems, "\n".join(problems)


def test_anthropic_never_declares_a_key_provisioner() -> None:
    """Anthropic's administration API cannot create keys, in any version.

    Pinned rather than left to a comment because the consequence is user-visible:
    it is why adding a key by hand is a first-class path rather than a fallback.
    An adapter that quietly grew a provisioner here would offer an admin a minted
    mode that fails at the first mint, for every member at once.
    """
    anthropic = registry.get_adapter("anthropic")
    assert anthropic.key_provisioner is None
    assert not anthropic.supports_minting


def test_provisioner_config_fields_fit_the_stored_config_model() -> None:
    """A provisioner's ``config_schema`` may only name fields the config model has.

    ``admin_config_schema`` is rendered as a form by the admin UI: the browser
    builds the request body from the field *names* the adapter declares, and
    seeds the form from the *stored* config object's keys. Those two key sets are
    the same set today and nothing has been making them so — a field the adapter
    names but ``ProviderAdminCredentialConfig`` has no slot for is silently
    dropped on save, and a stored key the adapter stops naming is silently
    nulled on the next one.

    Both failures are invisible: no exception, no 4xx, just configuration that
    quietly stops round-tripping. The containment is what makes the schema safe
    to render generically, so it is pinned here rather than restated as a
    hardcoded field list in the frontend.
    """
    allowed = set(ProviderAdminCredentialConfig.model_fields)
    problems: list[str] = []
    for adapter in registry.all_adapters():
        provisioner = adapter.key_provisioner
        if provisioner is None:
            continue
        schema = provisioner.config_schema()
        fields = schema.get("fields")
        assert isinstance(fields, list), (
            f"{type(adapter).__name__}'s config_schema has no 'fields' list"
        )
        for field in fields:
            name = field.get("name")
            assert isinstance(name, str) and name, (
                f"{type(adapter).__name__} declares a config field with no name"
            )
            if name not in allowed:
                problems.append(
                    f"{type(adapter).__name__} declares config field "
                    f"'{name}', which ProviderAdminCredentialConfig cannot store"
                )
    assert not problems, "\n".join(problems)
