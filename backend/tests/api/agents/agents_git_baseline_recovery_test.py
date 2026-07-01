"""Regression tests: lost on-disk git-versioning baseline snapshot recovery.

Bug: ``docs/drafts/git_versioning_dirty_false_negative_bug.md``.

Root cause (fixed): ``GitSourceService.compute_dirty`` / ``compute_status``
(backing ``GET /agents/{id}/git/dirty`` and ``GET /agents/{id}/git/status``)
compare the live workspace against the last-synced ``AgentBundleRevision``'s
on-disk snapshot (``snapshot_path/workspace``). That snapshot lives under
``BUNDLE_STORAGE_DIR`` (default ``/app/data/bundles``), a path that is NOT a
durable volume mount in the shipped compose file — a backend container
recreation wipes it while the ``AgentBundleRevision`` **row** (with its now-
dangling ``snapshot_path``) survives in Postgres. Before the fix, a missing
on-disk snapshot silently left ``workspace_dirty=False`` — the UI told the
user "No local changes" even though they had just edited a workspace file.

The fix distinguishes two states when a synced revision row exists:
  * on-disk snapshot present -> unchanged (diff against it as before).
  * on-disk snapshot MISSING -> self-heal: re-clone the remote at
    ``last_synced_commit`` (``GitSourceService._rematerialize_baseline_snapshot``,
    reusing ``clone_repository_context``) and persist it back to
    ``BUNDLE_STORAGE_DIR`` opportunistically, then diff against the rebuilt
    baseline. If the re-clone itself fails (remote unreachable, ref/commit
    gone, auth failure), ``GitBaselineUnavailableError`` is raised instead of
    silently reporting a clean workspace; the route maps it to HTTP 503
    (``agent_git.py`` ``_map_git_error``).
  * genuinely no baseline ever synced (no revision row) is UNCHANGED — stays
    non-dirty, no error.

Git network isolation strategy (mirrors ``agents_git_source_test.py``): all
git primitives are patched at their module-local import sites inside
``app.services.bundles.git_source_service``. The re-materialization path
reuses ``clone_repository_context`` — the SAME patch target the checkout /
connect / pull / push tests already use — so the "remote" content served
during self-heal is whatever directory tree the fake context manager points
at.

Locating the on-disk baseline snapshot without a DB query: the layout is a
documented, stable contract (``core/config.py`` comment at
``BUNDLE_STORAGE_DIR``; ``GitSourceService._persist_revision``):
``<BUNDLE_STORAGE_DIR>/<agent's bundle_id string>/<revision_number>/``. A
newly-created agent's ``bundle_id`` (string) is assigned at creation and
returned by ``GET /agents/{id}`` (already relied on by
``agents_git_source_test.py``); ``connect`` always produces the first
revision (``revision_number == 1``) in the same call. Removing that directory
from disk directly is the filesystem-manipulation equivalent of the existing
``ENV_INSTANCES_DIR`` workspace writes/deletes already used throughout
``agents_git_source_test.py`` (e.g. its Phase 6 "unreadable workspace" case)
— it simulates infrastructure state (an ephemeral volume wipe), not a
database row, so it does not violate the "no direct DB access" rule.

Note on the "genuinely no baseline" case: a literal "``AgentGitSource`` row
exists but zero ``AgentBundleRevision`` rows / no ``installed_revision_id``"
state is NOT independently reachable through the sanctioned API surface —
every successful ``connect`` / ``checkout`` path (init / fast-forward / adopt)
atomically persists an ``AgentBundleRevision`` with a ``snapshot_path`` in the
very same call that creates the connected source
(``GitSourceService._connect_capture`` -> ``_capture_and_push`` /
``_connect_adopt_existing``, both call ``_persist_revision``). Producing the
literal empty state would require inserting an ``AgentGitSource`` row via
direct ORM access, which the test-suite rules forbid. The tests below instead
exercise the *no live environment workspace* branch (``has_env=False``),
which is the other pre-existing "skip the baseline comparison entirely, stay
clean, never error" branch untouched by the fix — giving equivalent coverage
of "unaffected legacy behavior" without violating the API-only rule.
"""
import json
import shutil
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks

API = settings.API_V1_STR

# ── Patch targets (module-local import sites in git_source_service.py) ────────

_CLONE_CTX = "app.services.bundles.git_source_service.clone_repository_context"
_LS_REMOTE = "app.services.bundles.git_source_service.ls_remote_head"
_INIT_REPO = "app.services.bundles.git_source_service.init_repo_with_remote"
_COMMIT_ALL = "app.services.bundles.git_source_service.commit_all"
_FF_PUSH = "app.services.bundles.git_source_service.fast_forward_push"

_SHA_V1 = "a" * 40
_SHA_V2 = "b" * 40


# ── Workspace / manifest helpers ──────────────────────────────────────────────


def _write_tree(root: Path, tree: dict) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, value in tree.items():
        child = root / name
        if isinstance(value, dict):
            _write_tree(child, value)
        elif value is None:
            child.mkdir(parents=True, exist_ok=True)
        else:
            child.parent.mkdir(parents=True, exist_ok=True)
            child.write_text(str(value))


def _seed_env_workspace(env_id: str, tree: dict) -> Path:
    ws_root = Path(settings.ENV_INSTANCES_DIR) / env_id / "app" / "workspace"
    _write_tree(ws_root, tree)
    return ws_root


def _manifest(bundle_id: str) -> dict:
    return {
        "schema_version": 2,
        "bundle_id": bundle_id,
        "revision_number": 1,
        "version": "1.0",
        "published_at": "2024-01-01T00:00:00+00:00",
        "prompts": {
            "workflow": "",
            "entrypoint": None,
            "refiner": None,
            "router_trigger": None,
        },
        "sdk": {
            "building": None,
            "conversation": None,
            "model_override_building": None,
            "model_override_conversation": None,
        },
        "required_credential_specs": [],
        "schedules": [],
        "plugin_specs": [],
        "release_notes": "baseline",
    }


def _snapshot_dir(bundle_id: str, revision_number: int = 1) -> Path:
    """The on-disk baseline snapshot dir for an agent's first synced revision.

    Mirrors the layout documented at ``core/config.py`` (``BUNDLE_STORAGE_DIR``
    comment) and written by ``GitSourceService._persist_revision``:
    ``<BUNDLE_STORAGE_DIR>/<bundle_id>/<revision_number>/``.
    """
    return Path(settings.BUNDLE_STORAGE_DIR) / bundle_id / str(revision_number)


def _prepare_rematerialize_source(tmp_path: Path, name: str, snapshot_dir: Path) -> Path:
    """Snapshot the CURRENT on-disk baseline into a fresh fake-clone source tree.

    Copies ``snapshot_dir/workspace`` verbatim (byte-identical to the true
    last-synced baseline, captured BEFORE any local edits) and adds a valid
    ``cinna.agent.json`` manifest, so patching ``clone_repository_context`` to
    return this directory reproduces exactly what a real ``git clone`` at
    ``last_synced_commit`` would — giving deterministic diff assertions after
    self-heal, rather than merely "some baseline came back".
    """
    src = tmp_path / name
    src.mkdir(parents=True)
    shutil.copytree(snapshot_dir / "workspace", src / "workspace")
    (src / "cinna.agent.json").write_text(json.dumps(_manifest(f"io.test.remat.{name}")))
    return src


def _fake_clone_ctx(repo_dir: Path, sha: str = _SHA_V1):
    @contextmanager
    def _ctx(*args, **kwargs):
        mock_repo = MagicMock()
        mock_repo.head.commit.hexsha = sha
        yield str(repo_dir), mock_repo

    return _ctx


@contextmanager
def _failing_clone_ctx(*args, **kwargs):
    """Simulate a re-materialization that cannot reach the remote at all."""
    from app.services.knowledge.git_operations import GitOperationError

    raise GitOperationError("Simulated: remote unreachable / ref gone")
    yield  # unreachable — keeps this a generator function


# ── URL helpers ────────────────────────────────────────────────────────────────


def _git_dirty_url(agent_id: str) -> str:
    return f"{API}/agents/{agent_id}/git/dirty"


def _git_status_url(agent_id: str) -> str:
    return f"{API}/agents/{agent_id}/git/status"


def _git_connect_url(agent_id: str) -> str:
    return f"{API}/agents/{agent_id}/git/connect"


# ── Setup helpers (mirror agents_git_source_test.py) ──────────────────────────


def _create_agent_with_env(client: TestClient, headers: dict, *, tree: dict) -> tuple[str, str]:
    agent = create_agent_via_api(client, headers)
    drain_tasks()
    data = get_agent(client, headers, agent["id"])
    env_id = data.get("active_environment_id")
    assert env_id, "created agent must have an active environment"
    _seed_env_workspace(str(env_id), tree)
    return agent["id"], str(env_id)


def _connect_init_path(client: TestClient, headers: dict, agent_id: str) -> dict:
    """Connect onto an empty remote (init path) — always persists revision #1."""
    from app.services.knowledge.git_operations import GitOperationError

    with (
        patch(_LS_REMOTE, side_effect=GitOperationError("Ref 'main' not found")),
        patch(_INIT_REPO, return_value=MagicMock()),
        patch(_COMMIT_ALL, return_value=_SHA_V2),
        patch(_FF_PUSH, return_value=None),
    ):
        r = client.post(
            _git_connect_url(agent_id),
            headers=headers,
            json={
                "repo_url": "https://github.com/example/baseline-recovery.git",
                "ref": "main",
                "sync_direction": "bidirectional",
                "commit_message": "Initial export",
            },
        )
    assert r.status_code == 200, f"Connect failed: {r.text}"
    return r.json()


# ── Scenario: compute_dirty / GET /git/dirty lost-baseline recovery ──────────


def test_git_dirty_missing_baseline_snapshot_recovery(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    """
    Lost baseline snapshot handling for ``GET /agents/{id}/git/dirty``:
      1. Setup: connect an agent (revision #1 + on-disk snapshot both exist).
      2. Wipe the on-disk snapshot (simulate an ephemeral BUNDLE_STORAGE_DIR
         wipe) while a real local edit exists in the live workspace ->
         self-heal (re-materialize from git) correctly reports
         workspace_dirty=True — NOT the pre-fix silent False.
      3. The self-heal opportunistically persists the rebuilt snapshot back
         to disk.
      4. Wipe the snapshot again and make re-materialization itself fail
         (remote unreachable) -> GitBaselineUnavailableError -> HTTP 503,
         not a silent clean 200.
      5. No live env workspace at all (has_env=False) — unaffected by the
         fix: stays non-dirty, no error (see module docstring for why this
         stands in for "no baseline row ever synced").
    """
    headers = superuser_token_headers

    # ── Phase 1: Connect a fresh agent; revision #1 + snapshot both exist ────
    agent_id, env_id = _create_agent_with_env(
        client, headers, tree={"docs": {"workflow.md": "# Workflow\nv1"}}
    )
    source = _connect_init_path(client, headers, agent_id)
    assert source["last_synced_commit"] == _SHA_V2

    agent_data = get_agent(client, headers, agent_id)
    bundle_id = agent_data["bundle_id"]
    assert bundle_id
    snapshot_dir = _snapshot_dir(bundle_id)
    assert (snapshot_dir / "workspace").exists(), (
        "Sanity: connect must persist an on-disk baseline snapshot"
    )

    # Sanity: clean immediately after connect (pre-existing, unaffected path).
    r = client.get(_git_dirty_url(agent_id), headers=headers)
    assert r.status_code == 200
    clean = r.json()
    assert clean["has_env"] is True
    assert clean["workspace_dirty"] is False
    assert clean["dirty"] is False

    # ── Phase 2: Snapshot the pristine baseline BEFORE editing anything ──────
    # This is what a real `git clone` at last_synced_commit would faithfully
    # reproduce; using it as the fake-clone source gives an exact-reproduction
    # self-heal instead of merely "some baseline came back".
    remat_ok_src = _prepare_rematerialize_source(tmp_path, "remat_ok", snapshot_dir)

    # ── Phase 3: Wipe the on-disk snapshot (row survives; files gone) ────────
    shutil.rmtree(snapshot_dir)
    assert not snapshot_dir.exists()

    # ── Phase 4: Real local edit to the live workspace ───────────────────────
    ws_root = Path(settings.ENV_INSTANCES_DIR) / env_id / "app" / "workspace"
    (ws_root / "docs" / "workflow.md").write_text("# Workflow\nLOCALLY EDITED")

    # ── Phase 5: dirty check self-heals and detects the real edit ───────────
    with patch(_CLONE_CTX, _fake_clone_ctx(remat_ok_src)):
        r = client.get(_git_dirty_url(agent_id), headers=headers)
    assert r.status_code == 200, f"Expected self-heal to succeed, got: {r.text}"
    healed = r.json()
    assert healed["has_env"] is True
    assert healed["workspace_dirty"] is True, (
        "Missing baseline snapshot must NOT silently report workspace_dirty=False "
        "when the live workspace has genuinely diverged"
    )
    assert healed["dirty"] is True

    # The self-heal persisted a fresh baseline back to disk.
    assert (snapshot_dir / "workspace").exists(), (
        "Re-materialized baseline must be persisted back to BUNDLE_STORAGE_DIR"
    )

    # ── Phase 6: Wipe again; re-materialization itself fails this time ──────
    shutil.rmtree(snapshot_dir)
    assert not snapshot_dir.exists()

    with patch(_CLONE_CTX, _failing_clone_ctx):
        r = client.get(_git_dirty_url(agent_id), headers=headers)
    assert r.status_code == 503, (
        f"Expected 503 when the baseline cannot be rebuilt, got {r.status_code}: {r.text}"
    )
    assert r.json()["detail"], "Expected a non-empty error detail on baseline failure"
    # The failed rebuild must not fabricate a snapshot on disk.
    assert not snapshot_dir.exists()

    # ── Phase 7: No live env workspace at all -> has_env=False, unaffected ──
    no_env_agent_id, no_env_env_id = _create_agent_with_env(
        client, headers, tree={"docs": {"a.md": "a"}}
    )
    _connect_init_path(client, headers, no_env_agent_id)
    ws_dir = Path(settings.ENV_INSTANCES_DIR) / no_env_env_id / "app" / "workspace"
    shutil.rmtree(ws_dir)

    r = client.get(_git_dirty_url(no_env_agent_id), headers=headers)
    assert r.status_code == 200, f"No-env dirty check must not error, got: {r.text}"
    no_env = r.json()
    assert no_env["has_env"] is False
    assert no_env["workspace_dirty"] is False
    assert no_env["dirty"] is False


# ── Scenario: compute_status / GET /git/status lost-baseline recovery ────────


def test_git_status_missing_baseline_snapshot_recovery(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    """
    Lost baseline snapshot handling for ``GET /agents/{id}/git/status`` (the
    commit-preview sibling of ``GET /git/dirty`` — identical guard):
      1. Setup: connect an agent (revision #1 + on-disk snapshot both exist).
      2. Wipe the on-disk snapshot; add/modify/delete workspace files ->
         self-heal correctly reports the exact per-file changes (NOT a
         silent empty ``file_changes`` / ``dirty=False``).
      3. Wipe again + force re-materialization to fail -> HTTP 503.
      4. No live env workspace at all (has_env=False) — unaffected: empty
         change lists, no error.
    """
    headers = superuser_token_headers

    # ── Phase 1: Connect a fresh agent; revision #1 + snapshot both exist ────
    agent_id, env_id = _create_agent_with_env(
        client,
        headers,
        tree={"docs": {"workflow.md": "# Workflow\nv1", "keep.md": "keep"}},
    )
    _connect_init_path(client, headers, agent_id)

    agent_data = get_agent(client, headers, agent_id)
    bundle_id = agent_data["bundle_id"]
    snapshot_dir = _snapshot_dir(bundle_id)
    assert (snapshot_dir / "workspace").exists()

    # Sanity: clean immediately after connect.
    r = client.get(_git_status_url(agent_id), headers=headers)
    assert r.status_code == 200
    clean = r.json()
    assert clean["has_env"] is True
    assert clean["dirty"] is False
    assert clean["file_changes"] == []

    # ── Phase 2: Snapshot the pristine baseline BEFORE editing ───────────────
    remat_ok_src = _prepare_rematerialize_source(tmp_path, "remat_ok_status", snapshot_dir)

    # ── Phase 3: Wipe the on-disk snapshot ───────────────────────────────────
    shutil.rmtree(snapshot_dir)
    assert not snapshot_dir.exists()

    # ── Phase 4: Add / modify / delete workspace files ───────────────────────
    ws_root = Path(settings.ENV_INSTANCES_DIR) / env_id / "app" / "workspace"
    (ws_root / "docs" / "added.md").write_text("brand new")
    (ws_root / "docs" / "workflow.md").write_text("# Workflow\nMODIFIED")
    (ws_root / "docs" / "keep.md").unlink()

    # ── Phase 5: status preview self-heals and reports exact per-file diff ──
    with patch(_CLONE_CTX, _fake_clone_ctx(remat_ok_src)):
        r = client.get(_git_status_url(agent_id), headers=headers)
    assert r.status_code == 200, f"Expected self-heal to succeed, got: {r.text}"
    healed = r.json()
    assert healed["has_env"] is True
    assert healed["dirty"] is True
    changes = {c["path"]: c["change_type"] for c in healed["file_changes"]}
    assert changes.get("docs/added.md") == "added"
    assert changes.get("docs/workflow.md") == "modified"
    assert changes.get("docs/keep.md") == "deleted", (
        "Missing baseline snapshot must NOT silently report an empty diff "
        "(file_changes=[]) when the live workspace has genuinely diverged"
    )

    # Self-heal persisted a fresh baseline back to disk.
    assert (snapshot_dir / "workspace").exists()

    # ── Phase 6: Wipe again; re-materialization itself fails ────────────────
    shutil.rmtree(snapshot_dir)

    with patch(_CLONE_CTX, _failing_clone_ctx):
        r = client.get(_git_status_url(agent_id), headers=headers)
    assert r.status_code == 503, (
        f"Expected 503 when the baseline cannot be rebuilt, got {r.status_code}: {r.text}"
    )
    assert r.json()["detail"]

    # ── Phase 7: No live env workspace at all -> unaffected, no error ──────
    no_env_agent_id, no_env_env_id = _create_agent_with_env(
        client, headers, tree={"docs": {"a.md": "a"}}
    )
    _connect_init_path(client, headers, no_env_agent_id)
    ws_dir = Path(settings.ENV_INSTANCES_DIR) / no_env_env_id / "app" / "workspace"
    shutil.rmtree(ws_dir)

    r = client.get(_git_status_url(no_env_agent_id), headers=headers)
    assert r.status_code == 200, f"No-env status check must not error, got: {r.text}"
    no_env = r.json()
    assert no_env["has_env"] is False
    assert no_env["dirty"] is False
    assert no_env["file_changes"] == []
    assert no_env["prompt_changes"] == []
