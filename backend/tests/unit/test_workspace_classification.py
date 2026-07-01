"""Unit tests for the workspace classification single-source-of-truth module.

Pure predicate + FS-shape logic — no DB, no HTTP. Covers:
  - denylist membership (bundle-owned vs excluded vs runtime-denylisted)
  - custom agent-authored top-level dirs are bundle-owned by default
  - ``plugins`` is bundle-owned (special handling lives at the call sites)
  - ENV_MIGRATION superset (credentials + uploads included, logs/databases not)
  - ``iter_bundle_toplevel`` / ``iter_env_migration_toplevel`` over a tmp tree
  - ``snapshot_layout`` v1-flat vs v2-workspace detection on temp dirs
"""
from pathlib import Path

from app.services.environments.workspace_classification import (
    BUNDLE_EXCLUDED_TOPLEVEL,
    ENV_MIGRATION_EXTRA,
    NESTED_EXCLUDED_DIRS,
    NESTED_EXCLUDED_FILE_GLOBS,
    PLUGIN_DERIVED_FILES,
    PLUGINS_DIRNAME,
    RUNTIME_NAME_DENYLIST,
    WORKSPACE_ROOT_REL,
    is_bundle_owned_toplevel,
    is_env_migration_toplevel,
    is_nested_excluded,
    is_runtime_denylisted,
    iter_bundle_toplevel,
    iter_env_migration_toplevel,
    safe_copytree,
    snapshot_layout,
)


# ── Constants sanity ────────────────────────────────────────────────


def test_workspace_root_rel_value() -> None:
    assert WORKSPACE_ROOT_REL == "app/workspace"


def test_plugins_dirname_and_derived_files() -> None:
    assert PLUGINS_DIRNAME == "plugins"
    assert PLUGIN_DERIVED_FILES == frozenset({"settings.json", "manifest.json"})


def test_env_migration_extra_is_credentials_and_uploads() -> None:
    assert ENV_MIGRATION_EXTRA == frozenset({"credentials", "uploads"})


# ── is_bundle_owned_toplevel ────────────────────────────────────────


def test_excluded_toplevel_not_bundle_owned() -> None:
    for name in BUNDLE_EXCLUDED_TOPLEVEL:
        assert is_bundle_owned_toplevel(name) is False, name


def test_specific_excluded_names() -> None:
    for name in (
        "app-data",
        "credentials",
        "logs",
        "databases",
        "uploads",
        "__init__.py",
    ):
        assert is_bundle_owned_toplevel(name) is False, name


def test_known_bundle_owned_names() -> None:
    for name in (
        "scripts",
        "docs",
        "knowledge",
        "files",
        "webapp",
        "agent_api",
        "plugins",
        "workspace_requirements.txt",
        "workspace_system_packages.txt",
    ):
        assert is_bundle_owned_toplevel(name) is True, name


def test_custom_agent_dir_is_bundle_owned() -> None:
    # The whole point of the denylist: a dir the agent invented is captured.
    for name in ("templates", "prompts", "my_custom_dir", "notes.md", ".env.example"):
        assert is_bundle_owned_toplevel(name) is True, name


def test_plugins_is_bundle_owned() -> None:
    assert is_bundle_owned_toplevel(PLUGINS_DIRNAME) is True


def test_runtime_denylisted_not_bundle_owned() -> None:
    for name in RUNTIME_NAME_DENYLIST:
        assert is_runtime_denylisted(name) is True, name
        assert is_bundle_owned_toplevel(name) is False, name


def test_cinna_plugin_ref_not_denylisted() -> None:
    # Per-plugin marker is kept verbatim in snapshots.
    assert is_runtime_denylisted(".cinna_plugin_ref") is False
    assert is_bundle_owned_toplevel(".cinna_plugin_ref") is True


# ── is_nested_excluded (recursive cache-artifact denylist) ──────────


def test_pycache_dir_is_nested_excluded() -> None:
    assert is_nested_excluded("__pycache__") is True
    # Also rejected at the workspace root.
    assert is_bundle_owned_toplevel("__pycache__") is False
    assert is_env_migration_toplevel("__pycache__") is False


def test_compiled_python_files_are_nested_excluded() -> None:
    for name in ("module.pyc", "thing.pyo"):
        assert is_nested_excluded(name) is True, name


def test_tooling_caches_are_nested_excluded() -> None:
    for name in NESTED_EXCLUDED_DIRS:
        assert is_nested_excluded(name) is True, name


def test_normal_names_are_not_nested_excluded() -> None:
    for name in ("scripts", "agent_api", "main.py", "notes.md", "data.json"):
        assert is_nested_excluded(name) is False, name
    # Sanity: the glob set is the pyc/pyo pair.
    assert set(NESTED_EXCLUDED_FILE_GLOBS) == {"*.pyc", "*.pyo"}


# ── is_env_migration_toplevel ───────────────────────────────────────


def test_env_migration_includes_credentials_and_uploads() -> None:
    assert is_env_migration_toplevel("credentials") is True
    assert is_env_migration_toplevel("uploads") is True


def test_env_migration_excludes_runtime_and_appdata_and_marker() -> None:
    for name in ("logs", "databases", "app-data", "__init__.py"):
        assert is_env_migration_toplevel(name) is False, name


def test_env_migration_includes_bundle_owned() -> None:
    for name in ("scripts", "webapp", "agent_api", "plugins", "custom_dir"):
        assert is_env_migration_toplevel(name) is True, name


def test_env_migration_excludes_runtime_denylisted() -> None:
    for name in RUNTIME_NAME_DENYLIST:
        assert is_env_migration_toplevel(name) is False, name


# ── iter_bundle_toplevel ────────────────────────────────────────────


def _seed_ws(root: Path, names: list[str]) -> None:
    """Create a fake workspace dir with the given top-level entries.

    Names ending in an extension become files; otherwise directories.
    """
    root.mkdir(parents=True, exist_ok=True)
    for name in names:
        target = root / name
        if "." in name and not name.startswith("."):
            target.write_text("x")
        elif name.endswith(".example") or name in ("__init__.py",):
            target.write_text("x")
        else:
            target.mkdir(parents=True, exist_ok=True)


def test_iter_bundle_toplevel_filters_denylist(tmp_path: Path) -> None:
    ws = tmp_path / "app" / "workspace"
    _seed_ws(
        ws,
        [
            "scripts",
            "docs",
            "webapp",
            "agent_api",
            "templates",  # custom
            "notes.md",  # custom file
            "plugins",
            "app-data",  # excluded
            "credentials",  # excluded
            "logs",  # excluded
            "databases",  # excluded
            "uploads",  # excluded from bundle
            "__init__.py",  # excluded marker
            ".cache",  # runtime denylisted
        ],
    )
    got = {p.name for p in iter_bundle_toplevel(ws)}
    assert got == {
        "scripts",
        "docs",
        "webapp",
        "agent_api",
        "templates",
        "notes.md",
        "plugins",
    }


def test_iter_bundle_toplevel_missing_root_is_empty(tmp_path: Path) -> None:
    assert list(iter_bundle_toplevel(tmp_path / "does_not_exist")) == []


def test_iter_env_migration_toplevel_includes_extra(tmp_path: Path) -> None:
    ws = tmp_path / "app" / "workspace"
    _seed_ws(
        ws,
        [
            "scripts",
            "webapp",
            "credentials",  # env-migration extra
            "uploads",  # env-migration extra
            "logs",  # never
            "databases",  # never
            "app-data",  # never
            "__init__.py",  # never
        ],
    )
    got = {p.name for p in iter_env_migration_toplevel(ws)}
    assert got == {"scripts", "webapp", "credentials", "uploads"}


# ── snapshot_layout ─────────────────────────────────────────────────


def test_snapshot_layout_v2_when_workspace_subtree(tmp_path: Path) -> None:
    snap = tmp_path / "rev"
    (snap / "workspace" / "scripts").mkdir(parents=True)
    (snap / "manifest.json").write_text("{}")
    assert snapshot_layout(snap) == "v2_workspace"


def test_snapshot_layout_v1_when_flat(tmp_path: Path) -> None:
    snap = tmp_path / "rev"
    (snap / "scripts").mkdir(parents=True)
    (snap / "docs").mkdir(parents=True)
    (snap / "manifest.json").write_text("{}")
    assert snapshot_layout(snap) == "v1_flat"


def test_snapshot_layout_v1_when_workspace_is_a_file(tmp_path: Path) -> None:
    # A flat snapshot that happens to contain a file literally named
    # "workspace" must NOT be misclassified as v2.
    snap = tmp_path / "rev"
    snap.mkdir(parents=True)
    (snap / "workspace").write_text("not a dir")
    assert snapshot_layout(snap) == "v1_flat"


def test_snapshot_layout_v1_when_empty(tmp_path: Path) -> None:
    snap = tmp_path / "rev"
    snap.mkdir(parents=True)
    assert snapshot_layout(snap) == "v1_flat"


# ── Symlink safety (denylist bypass / host-file exfiltration guard) ──


def test_iter_bundle_toplevel_skips_toplevel_symlink_to_dir(tmp_path: Path) -> None:
    """A top-level symlink (even pointing at a real dir) is NOT yielded.

    A symlink ``notsecret -> ../../credentials`` is a dir per ``is_dir()`` so
    without the guard it would be classified bundle-owned and dereferenced.
    """
    outside = tmp_path / "outside" / "credentials"
    outside.mkdir(parents=True)
    (outside / "secret.json").write_text("TOPSECRET")

    ws = tmp_path / "app" / "workspace"
    ws.mkdir(parents=True)
    (ws / "scripts").mkdir()
    # Symlink with a non-excluded NAME so only the symlink check can stop it.
    (ws / "notsecret").symlink_to(outside, target_is_directory=True)

    got = {p.name for p in iter_bundle_toplevel(ws)}
    assert got == {"scripts"}, got
    assert "notsecret" not in got


def test_iter_bundle_toplevel_skips_absolute_symlink(tmp_path: Path) -> None:
    """An absolute symlink (e.g. ``-> /etc/passwd``) is skipped."""
    ws = tmp_path / "app" / "workspace"
    ws.mkdir(parents=True)
    (ws / "docs").mkdir()
    (ws / "leak").symlink_to(Path("/etc/passwd"))

    got = {p.name for p in iter_bundle_toplevel(ws)}
    assert got == {"docs"}, got


def test_iter_env_migration_toplevel_skips_symlink(tmp_path: Path) -> None:
    ws = tmp_path / "app" / "workspace"
    ws.mkdir(parents=True)
    (ws / "scripts").mkdir()
    (ws / "credentials").mkdir()
    (ws / "evil").symlink_to(tmp_path)

    got = {p.name for p in iter_env_migration_toplevel(ws)}
    assert got == {"scripts", "credentials"}, got


def test_safe_copytree_drops_nested_symlink(tmp_path: Path) -> None:
    """``safe_copytree`` never follows/recreates a NESTED symlink.

    A captured dir ``scripts/`` containing ``leak.json -> <outside file>`` must
    not exfiltrate the target's content (nor recreate the dangling link).
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "host_secret.txt").write_text("HOSTSECRET")

    src = tmp_path / "scripts"
    src.mkdir()
    (src / "run.sh").write_text("#!/bin/bash")
    (src / "leak.json").symlink_to(outside / "host_secret.txt")
    (src / "sublink").symlink_to(outside, target_is_directory=True)

    dst = tmp_path / "dest" / "scripts"
    safe_copytree(src, dst)

    assert (dst / "run.sh").read_text() == "#!/bin/bash"
    # Neither the symlink itself nor its dereferenced content lands in dest.
    assert not (dst / "leak.json").exists()
    assert not (dst / "sublink").exists()


def test_safe_copytree_drops_nested_pycache(tmp_path: Path) -> None:
    """``safe_copytree`` strips nested ``__pycache__``/``*.pyc`` at every depth.

    Regression: agent code dirs (e.g. ``agent_api/``) carry a nested
    ``__pycache__/`` with compiled ``*.pyc`` files that must never reach a
    bundle snapshot or git commit.
    """
    src = tmp_path / "agent_api"
    (src / "__pycache__").mkdir(parents=True)
    (src / "__pycache__" / "main.cpython-312.pyc").write_bytes(b"\x00cached")
    (src / "main.py").write_text("print('hi')")
    # A stray compiled file outside __pycache__ is dropped too.
    (src / "legacy.pyc").write_bytes(b"\x00old")
    # A nested cache dir deeper in the tree.
    nested = src / "sub"
    (nested / ".mypy_cache").mkdir(parents=True)
    (nested / ".mypy_cache" / "cache.json").write_text("{}")
    (nested / "real.py").write_text("x = 1")

    dst = tmp_path / "dest" / "agent_api"
    safe_copytree(src, dst)

    assert (dst / "main.py").read_text() == "print('hi')"
    assert (dst / "sub" / "real.py").read_text() == "x = 1"
    assert not (dst / "__pycache__").exists()
    assert not (dst / "legacy.pyc").exists()
    assert not (dst / "sub" / ".mypy_cache").exists()
