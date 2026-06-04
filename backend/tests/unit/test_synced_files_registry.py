"""Unit test: Synced Workspace File Registry drift guard.

Asserts that the env-core ``_WATCHED_FILES`` list (``app_core_base/core/main.py``)
exactly equals the set of ``rel_path``s declared in
``backend/app/services/environments/synced_files.py:SYNCED_FILES``.

If the two lists diverge (e.g., someone adds a file to env-core but forgets to
register it in SYNCED_FILES, or vice-versa) this test will fail, making the
omission visible at CI time rather than at runtime.

This is the test required by the plan (section "Synced Workspace File Registry",
line ~101/378): "add a unit test asserting the env-core watched list equals the
registry's rel_path s, so drift is caught."

The env-core ``main.py`` is imported by adding ``app_core_base`` to ``sys.path``
inside the unit-test conftest.py, which this file reuses.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Import env-core main.py (available via sys.path set by unit/conftest.py)
# ---------------------------------------------------------------------------

def _load_env_core_main() -> object:
    """Import the env-core main module and return it.

    The ``app_core_base`` directory is already on sys.path thanks to the
    unit-test conftest, so ``core.main`` is importable like any package.
    """
    if "core.main" in sys.modules:
        return sys.modules["core.main"]

    # Locate the file directly for a clean import with a unique name.
    _app_core_base = (
        Path(__file__).parents[2]
        / "app"
        / "env-templates"
        / "app_core_base"
    )
    main_path = _app_core_base / "core" / "main.py"
    assert main_path.exists(), (
        f"env-core main.py not found at {main_path!r}. "
        "The test assumes the env-template lives at "
        "backend/app/env-templates/app_core_base/core/main.py"
    )
    spec = importlib.util.spec_from_file_location("_env_core_main", main_path)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    # env-core main imports FastAPI and other packages that are not installed
    # in the backend test environment. We only need the module-level constant
    # ``_WATCHED_FILES`` defined before the imports that might fail. Use
    # source parsing as the fallback when exec fails.
    try:
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
    except Exception:
        # Fall back to AST-based extraction so the test is robust even if
        # env-core has optional dependencies not installed in the test env.
        import ast

        src = main_path.read_text()
        tree = ast.parse(src)
        watched: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "_WATCHED_FILES":
                        if isinstance(node.value, ast.List):
                            watched = [
                                elt.s  # type: ignore[attr-defined]
                                for elt in node.value.elts
                                if isinstance(elt, ast.Constant)
                                and isinstance(elt.s, str)
                            ]
        mod = type(sys)("_env_core_main")
        mod._WATCHED_FILES = watched  # type: ignore[attr-defined]
    return mod


# ---------------------------------------------------------------------------
# Import the registry
# ---------------------------------------------------------------------------

from app.services.environments.synced_files import SYNCED_FILES, watched_rel_paths


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSyncedFilesRegistryDriftGuard:

    def test_watched_files_equals_registry_rel_paths(self):
        """env-core _WATCHED_FILES must equal registry rel_paths — no drift allowed."""
        env_core_main = _load_env_core_main()
        env_core_watched: set[str] = set(env_core_main._WATCHED_FILES)  # type: ignore[attr-defined]
        registry_paths: set[str] = set(watched_rel_paths())

        missing_from_registry = env_core_watched - registry_paths
        missing_from_env_core = registry_paths - env_core_watched

        assert not missing_from_registry, (
            f"Files present in env-core _WATCHED_FILES but NOT in SYNCED_FILES registry: "
            f"{sorted(missing_from_registry)!r}. "
            "Add a SyncedFile entry for each."
        )
        assert not missing_from_env_core, (
            f"Files present in SYNCED_FILES registry but NOT in env-core _WATCHED_FILES: "
            f"{sorted(missing_from_env_core)!r}. "
            "Add each rel_path to _WATCHED_FILES in app_core_base/core/main.py."
        )

    def test_registry_has_expected_bidirectional_files(self):
        """The three prompt docs must be bidirectional (reconcile + LWW)."""
        bidir = {f.rel_path for f in SYNCED_FILES if f.sync_class == "bidirectional"}
        assert "docs/WORKFLOW_PROMPT.md" in bidir
        assert "docs/ENTRYPOINT_PROMPT.md" in bidir
        assert "docs/REFINER_PROMPT.md" in bidir

    def test_registry_has_expected_pull_only_files(self):
        """STATUS.md and CLI_COMMANDS.yaml must be pull-only."""
        pull_only = {f.rel_path for f in SYNCED_FILES if f.sync_class == "pull_only"}
        assert "app-data/storage/STATUS.md" in pull_only
        assert "docs/CLI_COMMANDS.yaml" in pull_only

    def test_registry_keys_are_unique(self):
        """Every SyncedFile.key must be unique across the registry."""
        keys = [f.key for f in SYNCED_FILES]
        assert len(keys) == len(set(keys)), (
            f"Duplicate keys in SYNCED_FILES: {[k for k in keys if keys.count(k) > 1]!r}"
        )

    def test_registry_rel_paths_are_unique(self):
        """Every SyncedFile.rel_path must be unique across the registry."""
        paths = [f.rel_path for f in SYNCED_FILES]
        assert len(paths) == len(set(paths)), (
            f"Duplicate rel_paths in SYNCED_FILES: "
            f"{[p for p in paths if paths.count(p) > 1]!r}"
        )

    def test_watched_rel_paths_helper_returns_all_registry_paths(self):
        """watched_rel_paths() returns rel_path for every registry entry."""
        all_paths = watched_rel_paths()
        for entry in SYNCED_FILES:
            assert entry.rel_path in all_paths, (
                f"SyncedFile(key={entry.key!r}).rel_path {entry.rel_path!r} "
                "not returned by watched_rel_paths()"
            )
