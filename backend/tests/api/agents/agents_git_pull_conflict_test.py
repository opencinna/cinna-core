"""Integration tests: git pull conflict resolution.

Covers the plan at ``docs/plans/git_pull_conflict_resolution_plan.md`` (Test
list in its §5): the structured 409 a blocked pull now raises, the
``blocks_pull`` / ``pull_blocked`` fields on ``GET /agents/{id}/git/status``
(computed by the SAME helper the guard raises from — ``_pull_blocking_changes``
— so the preview and the 409 can never disagree), and the two
``conflict_resolution`` modes on ``POST /agents/{id}/git/pull``
(``keep_local`` / ``take_remote``).

Git network isolation strategy (mirrors ``agents_git_source_test.py``): all git
primitives are patched at their module-local import sites inside
``app.services.bundles.git_source_service``.

Fixture note — the "SDK engine (conversation mode): added" drift: every
checkout in this file uses the default ``_make_agent_repo_tree`` (no explicit
``sdk_building`` / ``sdk_conversation``), so the manifest's ``sdk`` block is
``{"building": None, "conversation": None, ...}``. ``InstallService`` then
provisions the env via ``EnvironmentService.create_environment``, which
normalizes a ``None`` conversation SDK to ``DEFAULT_SDK`` (a non-null string) —
so every checked-out install in this file carries the documented one-time
``sdk`` section drift (baseline ``None`` vs. live ``DEFAULT_SDK``) from the
moment it exists, with no extra setup. This is used deliberately as the
"real", not synthetic, fixture for the plan's required assertion that an
``sdk``-section change is never ``blocks_pull: true`` and never blocks a pull
(``sdk`` is not in ``_PULL_OVERWRITTEN_SECTIONS``) — see Phase 2 of the first
scenario below.
"""
import json
import shutil
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import get_agent
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR

# ── Patch targets (module-local import sites in git_source_service.py) ─────

_CLONE_CTX = "app.services.bundles.git_source_service.clone_repository_context"
_GET_HASH = "app.services.bundles.git_source_service.get_current_commit_hash"
_LS_REMOTE = "app.services.bundles.git_source_service.ls_remote_head"

# ── Test git SHAs ─────────────────────────────────────────────────────────────

_SHA_V1 = "a" * 40  # initial checkout commit
_SHA_V2 = "b" * 40  # new remote commit (advances the pull)


# ── Workspace / manifest helpers (self-contained; mirrors agents_git_source_test.py) ──


def _make_agent_repo_tree(
    base_dir: Path,
    bundle_id: str,
    *,
    workflow_prompt: str = "Test workflow prompt",
) -> Path:
    """Build a minimal v2 agent snapshot tree that passes checkout validation.

    ``sdk`` is deliberately left at its default ``None``/``None`` in the
    manifest (see module docstring) — every test in this file relies on that
    to reproduce the "sdk: added, never blocks" drift automatically.
    """
    base_dir.mkdir(parents=True, exist_ok=True)
    (base_dir / "workspace" / "scripts").mkdir(parents=True, exist_ok=True)
    (base_dir / "workspace" / "scripts" / "run.sh").write_text("#!/bin/bash\necho hello")

    manifest = {
        "schema_version": 2,
        "bundle_id": bundle_id,
        "revision_number": 1,
        "version": "1.0",
        "published_at": "2024-01-01T00:00:00+00:00",
        "prompts": {
            "workflow": workflow_prompt,
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
        "release_notes": "initial",
    }
    (base_dir / "cinna.agent.json").write_text(json.dumps(manifest))
    return base_dir


def _fake_clone_ctx(repo_dir: Path, sha: str = _SHA_V1):
    """Return a context-manager replacement for ``clone_repository_context``.

    Yields ``(str(repo_dir), mock_repo)`` so the service sees a real directory
    tree but never makes a network call.
    """

    @contextmanager
    def _ctx(*args, **kwargs):
        mock_repo = MagicMock()
        mock_repo.head.commit.hexsha = sha
        yield str(repo_dir), mock_repo

    return _ctx


def _checkout_url() -> str:
    return f"{API}/agents/checkout"


def _git_source_url(agent_id: str) -> str:
    return f"{API}/agents/{agent_id}/git"


def _git_pull_url(agent_id: str) -> str:
    return f"{API}/agents/{agent_id}/git/pull"


def _git_status_url(agent_id: str) -> str:
    return f"{API}/agents/{agent_id}/git/status"


def _git_dirty_url(agent_id: str) -> str:
    return f"{API}/agents/{agent_id}/git/dirty"


def _git_diff_url(agent_id: str) -> str:
    return f"{API}/agents/{agent_id}/git/diff"


def _snapshot_dir(bundle_id: str, revision_number: int) -> Path:
    """The on-disk revision snapshot dir — the documented, stable contract at
    ``<BUNDLE_STORAGE_DIR>/<bundle_id>/<revision_number>/`` written by
    ``GitSourceService._persist_revision`` (mirrors the same helper in
    ``agents_git_baseline_recovery_test.py``). Inspecting it is the sanctioned,
    filesystem-only (not DB) way to prove a backup revision was persisted: the
    Revisions API deliberately excludes ``origin="git"`` rows (they are the
    internal dirty-check SSOT, not a catalog publish), so there is no API
    listing that would otherwise surface it.
    """
    return Path(settings.BUNDLE_STORAGE_DIR) / bundle_id / str(revision_number)


def _write_tree(root: Path, tree: dict) -> None:
    """Recursively materialise ``tree`` under ``root`` (mirrors the identical
    helper in ``agents_git_source_test.py`` / ``agents_git_baseline_recovery_test.py``)."""
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
    """Create workspace files inside the env instance directory.

    Writes into ``ENV_INSTANCES_DIR/<env_id>/app/workspace/<tree>``. A bare
    checkout does NOT materialise this dir on its own — the copy-from-template
    step runs in a background task this harness never drains — so any test
    whose pull exercises ``_capture_backup_revision`` (i.e. passes
    ``conflict_resolution``) must seed it explicitly first, exactly as the
    push/connect tests in ``agents_git_source_test.py`` do, or the new B1
    guard (``PublishService._assert_workspace_readable``) 400s.
    """
    ws_root = Path(settings.ENV_INSTANCES_DIR) / env_id / "app" / "workspace"
    _write_tree(ws_root, tree)
    return ws_root


def _do_checkout(
    client: TestClient,
    headers: dict,
    *,
    repo_url: str,
    clone_dir: Path,
    sha: str = _SHA_V1,
) -> dict:
    """Execute a checkout with mocked git ops. Returns response JSON (200 asserted)."""
    body = {"repo_url": repo_url, "ref": "main", "sync_direction": "bidirectional"}
    with (
        patch(_CLONE_CTX, _fake_clone_ctx(clone_dir, sha)),
        patch(_GET_HASH, return_value=sha),
        patch(_LS_REMOTE, return_value=sha),
    ):
        r = client.post(_checkout_url(), headers=headers, json=body)
    assert r.status_code == 200, f"checkout failed: {r.text}"
    return r.json()


# ── Scenario 1: structured 409 + status/guard blocks_pull agreement ─────────


def test_git_pull_local_changes_409_and_status_blocks_pull_agreement(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    """
    1. Checkout an agent — the sdk-section drift ("SDK engine (conversation
       mode): added") is present immediately, ``blocks_pull: false``, and does
       NOT set ``pull_blocked`` (plan §6 risk: sdk is not in
       ``_PULL_OVERWRITTEN_SECTIONS``). This doubles as the "non-blocking
       settings change" case from plan item 2 — schedules/plugins cannot be
       used for it here since a checkout produces a foreign (consumer) install,
       and those definitions are publisher-managed on foreign installs (403 on
       create), but the sdk section is exactly as valid an example of a
       non-blocking settings change.
    2. Unknown ``conflict_resolution`` -> 400 (validated before any remote I/O,
       so no local drift is required to exercise it).
    3. A REAL local edit (the workflow prompt) + remote advance ->
       ``GET /git/status`` agrees with the guard: ``pull_blocked: true``, the
       drifted prompt has ``blocks_pull: true``, the non-blocking sdk change
       still reports ``blocks_pull: false``.
    4. Bodiless ``POST /git/pull`` -> structured 409: ``detail`` is an object,
       ``code == "local_changes"``, non-empty ``blocking`` naming the drifted
       prompt and excluding the non-blocking sdk change. This is also the
       webhook regression case (plan §5 item 6): a bodiless pull — the GitOps
       webhook shape — must still fail loud, never silently resolve.
    5. Regression (plan §5 item 7): a SEPARATE, clean install still pulls with
       no body and no resolution -> 200.
    """
    headers = superuser_token_headers
    bundle_id = f"io.test.git.pullconflict.{random_lower_string()[:6]}"

    # ── Phase 1: Checkout ──────────────────────────────────────────────────
    v1_dir = tmp_path / "v1"
    _make_agent_repo_tree(v1_dir, bundle_id=bundle_id, workflow_prompt="Workflow V1")
    result = _do_checkout(
        client, headers, clone_dir=v1_dir, sha=_SHA_V1,
        repo_url="https://github.com/example/pull-conflict.git",
    )
    agent_id = result["agent"]["id"]

    r = client.get(_git_status_url(agent_id), headers=headers)
    assert r.status_code == 200, f"status failed: {r.text}"
    status0 = r.json()
    sdk_changes = [
        c for c in status0["setting_changes"]
        if c["field"] == "SDK engine (conversation mode)"
    ]
    assert len(sdk_changes) == 1, (
        f"Expected the one-time sdk drift, got setting_changes={status0['setting_changes']}"
    )
    assert sdk_changes[0]["change_type"] == "added"
    assert sdk_changes[0]["blocks_pull"] is False, (
        "An sdk-section change must never block a pull (sdk is not in "
        "_PULL_OVERWRITTEN_SECTIONS)"
    )
    assert status0["pull_blocked"] is False, (
        "The sdk drift alone must not flip pull_blocked"
    )
    # It IS dirty (the sdk drift is a real, visible change) — just not blocking.
    assert status0["dirty"] is True

    # ── Phase 2: Unknown conflict_resolution -> 400 ───────────────────────
    # Validated service-side before any remote fetch, so this needs no git
    # patches and no local drift.
    r = client.post(
        _git_pull_url(agent_id),
        headers=headers,
        json={"conflict_resolution": "not_a_real_value"},
    )
    assert r.status_code == 400, f"Expected 400 for unknown conflict_resolution, got {r.text}"

    # ── Phase 3: A REAL local edit — the drifted prompt ───────────────────
    r = client.put(
        f"{API}/agents/{agent_id}",
        headers=headers,
        json={"workflow_prompt": "Local modification"},
    )
    assert r.status_code == 200, f"Agent update failed: {r.text}"

    v2_dir = tmp_path / "v2"
    _make_agent_repo_tree(v2_dir, bundle_id=bundle_id, workflow_prompt="Workflow V2 remote")

    # ── Phase 4: GET /git/status now agrees with the (about to fire) guard ─
    r = client.get(_git_status_url(agent_id), headers=headers)
    assert r.status_code == 200
    status2 = r.json()
    assert status2["pull_blocked"] is True
    prompt_by_field = {c["field"]: c for c in status2["prompt_changes"]}
    assert "Workflow prompt" in prompt_by_field
    assert prompt_by_field["Workflow prompt"]["blocks_pull"] is True

    still_sdk = [
        c for c in status2["setting_changes"]
        if c["field"] == "SDK engine (conversation mode)"
    ]
    assert still_sdk[0]["blocks_pull"] is False, (
        "sdk drift must still report blocks_pull: false even once the "
        "install is genuinely pull_blocked by the prompt"
    )

    # ── Phase 5: Bodiless pull -> structured 409 (+ webhook regression) ───
    with (
        patch(_LS_REMOTE, return_value=_SHA_V2),
        patch(_CLONE_CTX, _fake_clone_ctx(v2_dir, _SHA_V2)),
        patch(_GET_HASH, return_value=_SHA_V2),
    ):
        r = client.post(_git_pull_url(agent_id), headers=headers)
    assert r.status_code == 409, f"Expected structured 409, got {r.status_code}: {r.text}"
    detail = r.json()["detail"]
    assert isinstance(detail, dict), f"Expected an object detail, got {detail!r}"
    assert detail["code"] == "local_changes"
    assert detail["message"], "Expected a non-empty fallback message"
    blocking = detail["blocking"]
    assert blocking, "Expected a non-empty blocking list"
    blocking_fields = {c["field"] for c in blocking}
    assert "Workflow prompt" in blocking_fields
    assert "SDK engine (conversation mode)" not in blocking_fields
    for change in blocking:
        assert change["section"] in ("prompt", "metadata")
        assert change["change_type"]

    # ── Phase 6 (regression, plan item 7): a clean install still pulls fine
    #    with no body and no resolution ─────────────────────────────────────
    clean_bundle_id = f"io.test.git.pullconflict.clean.{random_lower_string()[:6]}"
    clean_v1_dir = tmp_path / "clean_v1"
    _make_agent_repo_tree(clean_v1_dir, bundle_id=clean_bundle_id, workflow_prompt="Clean V1")
    clean_result = _do_checkout(
        client, headers, clone_dir=clean_v1_dir, sha=_SHA_V1,
        repo_url="https://github.com/example/pull-conflict-clean.git",
    )
    clean_agent_id = clean_result["agent"]["id"]

    clean_v2_dir = tmp_path / "clean_v2"
    _make_agent_repo_tree(clean_v2_dir, bundle_id=clean_bundle_id, workflow_prompt="Clean V2")
    with (
        patch(_LS_REMOTE, return_value=_SHA_V2),
        patch(_CLONE_CTX, _fake_clone_ctx(clean_v2_dir, _SHA_V2)),
        patch(_GET_HASH, return_value=_SHA_V2),
    ):
        r = client.post(_git_pull_url(clean_agent_id), headers=headers)
    assert r.status_code == 200, f"Expected a clean install to pull fine, got {r.text}"
    clean_agent_after = client.get(f"{API}/agents/{clean_agent_id}", headers=headers).json()
    assert clean_agent_after["workflow_prompt"] == "Clean V2"


# ── Scenario 2: conflict_resolution="keep_local" ─────────────────────────────


def test_git_pull_keep_local_resolution_preserves_local_value(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    """
    1. Checkout v1 (-> revision #1 on the bundle, on-disk snapshot present)
    2. Dirty the workflow prompt locally
    3. Remote advances to v2 with a DIFFERENT prompt
    4. ``POST /git/pull`` with ``conflict_resolution="keep_local"`` -> 200
    5. The prompt still holds the LOCAL value (not v2's remote value)
    5b. S2 — the backup capture condition is ``conflict_resolution is not
        None``, so a backup revision (#2) is now taken on keep_local too, not
        just take_remote: ``replace_bundle_content`` replaces the whole
        workspace identically on both resolutions, so the snapshot is the
        only record of a locally edited workspace file even when the DB-side
        prompt/metadata fields are preserved. Revision allocation on this
        path is therefore checkout=1, backup=2, incoming=3 — verified via the
        on-disk snapshot layout, same sanctioned filesystem-only check
        scenario 3 uses (the Revisions API excludes ``origin="git"`` rows).
    6. The pull genuinely advanced ``last_synced_commit`` to v2's SHA
    7. ``GET /git/dirty`` still reports dirty afterwards — the documented
       post-condition: keep_local leaves the install dirty on exactly the
       preserved field (pull, then commit on top). The backup landing BELOW
       the incoming revision (#3) is what keeps ``_resolve_synced_revision``
       pointed at the pulled tree as baseline, so this still holds.
    """
    headers = superuser_token_headers
    bundle_id = f"io.test.git.pullconflict.keep.{random_lower_string()[:6]}"

    # ── Phase 1: Checkout ──────────────────────────────────────────────────
    v1_dir = tmp_path / "keep_v1"
    _make_agent_repo_tree(v1_dir, bundle_id=bundle_id, workflow_prompt="Workflow V1")
    result = _do_checkout(
        client, headers, clone_dir=v1_dir, sha=_SHA_V1,
        repo_url="https://github.com/example/pull-conflict-keep.git",
    )
    agent_id = result["agent"]["id"]

    agent_data = get_agent(client, headers, agent_id)
    real_bundle_id = agent_data["bundle_id"]
    assert real_bundle_id
    assert (_snapshot_dir(real_bundle_id, 1) / "workspace").exists(), (
        "Sanity: checkout must persist an on-disk revision #1 snapshot"
    )

    # The keep_local pull below now takes a backup too (S2), which needs a
    # readable env instance workspace (B1 guard) — seed it as the push/connect
    # tests do (see ``_seed_env_workspace`` docstring).
    env_id = str(agent_data["active_environment_id"])
    _seed_env_workspace(env_id, {"scripts": {"run.sh": "#!/bin/bash\necho hello"}})

    # ── Phase 2: Dirty the prompt locally ─────────────────────────────────
    r = client.put(
        f"{API}/agents/{agent_id}",
        headers=headers,
        json={"workflow_prompt": "My local edit"},
    )
    assert r.status_code == 200, f"Agent update failed: {r.text}"

    # ── Phase 3: Remote advances with a DIFFERENT prompt ──────────────────
    v2_dir = tmp_path / "keep_v2"
    _make_agent_repo_tree(v2_dir, bundle_id=bundle_id, workflow_prompt="Workflow V2 remote")

    # ── Phase 4: keep_local pull -> 200 ────────────────────────────────────
    with (
        patch(_LS_REMOTE, return_value=_SHA_V2),
        patch(_CLONE_CTX, _fake_clone_ctx(v2_dir, _SHA_V2)),
        patch(_GET_HASH, return_value=_SHA_V2),
    ):
        r = client.post(
            _git_pull_url(agent_id),
            headers=headers,
            json={"conflict_resolution": "keep_local"},
        )
    assert r.status_code == 200, f"keep_local pull failed: {r.text}"
    pulled_agent = r.json()

    # ── Phase 5: Local prompt value retained ──────────────────────────────
    assert pulled_agent["workflow_prompt"] == "My local edit", (
        "keep_local must preserve the local prompt value, not overwrite it "
        "with the remote's"
    )

    # ── Phase 5b (S2): keep_local now also takes a backup revision (#2),
    #    landing BELOW the incoming revision (#3) ─────────────────────────
    assert (_snapshot_dir(real_bundle_id, 2) / "workspace").exists(), (
        "keep_local must also persist a backup revision of the live agent "
        "before the pull replaces the workspace on disk (conflict_resolution "
        "is not None on both resolutions, not just take_remote)"
    )

    # ── Phase 6: The pull genuinely advanced last_synced_commit ───────────
    with patch(_LS_REMOTE, return_value=_SHA_V2):
        src = client.get(_git_source_url(agent_id), headers=headers).json()
    assert src["last_synced_commit"] == _SHA_V2

    # ── Phase 7: Still dirty afterwards (documented post-condition) ───────
    r = client.get(_git_dirty_url(agent_id), headers=headers)
    assert r.status_code == 200
    dirty = r.json()
    assert dirty["dirty"] is True
    assert dirty["prompts_dirty"] is True


# ── Scenario 3: conflict_resolution="take_remote" ────────────────────────────


def test_git_pull_take_remote_resolution_discards_local_and_backs_up(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    """
    1. Checkout v1 (-> revision #1 on the bundle, on-disk snapshot present)
    2. Dirty the workflow prompt locally
    3. Remote advances to v2 with a DIFFERENT prompt
    4. ``POST /git/pull`` with ``conflict_resolution="take_remote"`` -> 200
    5. The prompt now matches the REMOTE (v2) value — the local edit is
       discarded
    6. A backup revision was persisted BEFORE the incoming one: revision #2 is
       the pre-discard snapshot of the live (dirty) agent, revision #3 is the
       pulled remote tree that becomes the new baseline (plan §3.4 ordering:
       "backup first, incoming second"). Verified via the on-disk snapshot
       layout — the sanctioned filesystem-only way to observe this, since the
       Revisions API deliberately excludes ``origin="git"`` rows.
    """
    headers = superuser_token_headers
    bundle_id = f"io.test.git.pullconflict.remote.{random_lower_string()[:6]}"

    # ── Phase 1: Checkout -> revision #1 ──────────────────────────────────
    v1_dir = tmp_path / "remote_v1"
    _make_agent_repo_tree(v1_dir, bundle_id=bundle_id, workflow_prompt="Workflow V1")
    result = _do_checkout(
        client, headers, clone_dir=v1_dir, sha=_SHA_V1,
        repo_url="https://github.com/example/pull-conflict-remote.git",
    )
    agent_id = result["agent"]["id"]

    agent_data = get_agent(client, headers, agent_id)
    real_bundle_id = agent_data["bundle_id"]
    assert real_bundle_id
    assert (_snapshot_dir(real_bundle_id, 1) / "workspace").exists(), (
        "Sanity: checkout must persist an on-disk revision #1 snapshot"
    )

    # take_remote's backup capture needs a readable env instance workspace
    # (B1 guard) — seed it as the push/connect tests do.
    env_id = str(agent_data["active_environment_id"])
    _seed_env_workspace(env_id, {"scripts": {"run.sh": "#!/bin/bash\necho hello"}})

    # ── Phase 2: Dirty the prompt locally ─────────────────────────────────
    r = client.put(
        f"{API}/agents/{agent_id}",
        headers=headers,
        json={"workflow_prompt": "My local edit"},
    )
    assert r.status_code == 200, f"Agent update failed: {r.text}"

    # ── Phase 3: Remote advances with a DIFFERENT prompt ──────────────────
    v2_dir = tmp_path / "remote_v2"
    _make_agent_repo_tree(v2_dir, bundle_id=bundle_id, workflow_prompt="Workflow V2 remote")

    # ── Phase 4: take_remote pull -> 200 ───────────────────────────────────
    with (
        patch(_LS_REMOTE, return_value=_SHA_V2),
        patch(_CLONE_CTX, _fake_clone_ctx(v2_dir, _SHA_V2)),
        patch(_GET_HASH, return_value=_SHA_V2),
    ):
        r = client.post(
            _git_pull_url(agent_id),
            headers=headers,
            json={"conflict_resolution": "take_remote"},
        )
    assert r.status_code == 200, f"take_remote pull failed: {r.text}"
    pulled_agent = r.json()

    # ── Phase 5: Prompt now matches the remote ─────────────────────────────
    assert pulled_agent["workflow_prompt"] == "Workflow V2 remote", (
        "take_remote must discard the local edit and adopt the remote's value"
    )

    # ── Phase 6: Backup revision (#2) exists BELOW the incoming one (#3) ──
    assert (_snapshot_dir(real_bundle_id, 2) / "workspace").exists(), (
        "take_remote must persist a backup revision of the live agent BEFORE "
        "discarding it"
    )
    assert (_snapshot_dir(real_bundle_id, 3) / "workspace").exists(), (
        "the incoming pulled revision must land ABOVE the backup"
    )


# ── Scenario 4: B1 — the backup guard fails loud on a missing workspace ──────


def test_git_pull_backup_guard_fails_loud_on_missing_env_workspace(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    """
    B1 — ``_capture_backup_revision`` (``git_source_service.py``) now calls
    ``PublishService._assert_workspace_readable(env, env_workspace_root)``
    inside a ``try/except ValueError -> GitSourceValidationError``, BEFORE
    ``_build_live_manifest`` and before the workspace snapshot. Rationale:
    ``iter_bundle_toplevel`` yields nothing — and does NOT raise — when the
    env instance's workspace root is missing or not a directory, so the
    unguarded version "succeeded" with an empty ``workspace/`` backup and the
    pull then went on to replace the real one: silent partial data loss
    behind a promised backup. This is the SAME assertion the push path
    already asserts 200 through with a readable workspace (see
    ``agents_git_source_test.py`` at :792, :1686, :1861 for that shape) —
    here the workspace is made unreadable instead, so the guard must instead
    fail the pull loud with 400, before anything is mutated or persisted.

    1. Checkout v1, then seed the env instance's ``app/workspace`` on disk
       (a bare checkout does not materialise it — see ``_seed_env_workspace``
       docstring) so this starts readable, like a genuinely live install.
    2. Remote advances to v2 — no local drift is needed to reach the backup
       capture: it now runs whenever ``conflict_resolution is not None``
       (scenario 2's S2), regardless of whether any field actually blocks
       the pull.
    3. The env instance's ``app/workspace`` dir is removed from disk —
       mirrors the "unreadable workspace" pattern used for connect/push
       elsewhere in ``agents_git_source_test.py``.
    4. ``POST /git/pull`` with ``conflict_resolution="take_remote"`` -> 400
       (``GitSourceValidationError``), not a 200 with a silently empty
       backup and a discarded local prompt.
    """
    headers = superuser_token_headers
    bundle_id = f"io.test.git.pullconflict.b1.{random_lower_string()[:6]}"

    # ── Phase 1: Checkout ──────────────────────────────────────────────────
    v1_dir = tmp_path / "b1_v1"
    _make_agent_repo_tree(v1_dir, bundle_id=bundle_id, workflow_prompt="Workflow V1")
    result = _do_checkout(
        client, headers, clone_dir=v1_dir, sha=_SHA_V1,
        repo_url="https://github.com/example/pull-conflict-b1.git",
    )
    agent_id = result["agent"]["id"]

    agent_data = get_agent(client, headers, agent_id)
    env_id = str(agent_data["active_environment_id"])
    assert env_id, "Checkout must provision an active environment"

    # A bare checkout does NOT materialise the per-instance workspace on its
    # own (the template->instance copy is a background task this harness
    # never drains) — seed it first so there is something real to remove,
    # exactly like a live, previously-running agent whose instance dir has
    # since disappeared (see ``_seed_env_workspace`` docstring).
    ws_dir = _seed_env_workspace(env_id, {"scripts": {"run.sh": "#!/bin/bash\necho hello"}})

    # ── Phase 2: Remote advances (no local drift needed — see docstring) ──
    v2_dir = tmp_path / "b1_v2"
    _make_agent_repo_tree(v2_dir, bundle_id=bundle_id, workflow_prompt="Workflow V2 remote")

    # ── Phase 3: The env instance workspace dir goes missing ──────────────
    assert ws_dir.exists(), (
        "Sanity: the seeded workspace dir must exist so this test genuinely "
        "removes something"
    )
    shutil.rmtree(ws_dir)

    # ── Phase 4: take_remote pull -> 400, fail loud, not a silent empty backup ─
    with (
        patch(_LS_REMOTE, return_value=_SHA_V2),
        patch(_CLONE_CTX, _fake_clone_ctx(v2_dir, _SHA_V2)),
        patch(_GET_HASH, return_value=_SHA_V2),
    ):
        r = client.post(
            _git_pull_url(agent_id),
            headers=headers,
            json={"conflict_resolution": "take_remote"},
        )
    assert r.status_code == 400, (
        "Expected the backup guard to fail loud on a missing env workspace "
        f"dir, got {r.status_code}: {r.text}"
    )
    detail = r.json()["detail"]
    assert isinstance(detail, str), f"Expected a plain string detail, got {detail!r}"
    assert "workspace" in detail.lower()

    # ── Phase 5: The pull must not have mutated the live agent ────────────
    agent_after = get_agent(client, headers, agent_id)
    assert agent_after["workflow_prompt"] == "Workflow V1", (
        "A failed backup guard must abort the pull before anything is "
        "applied to the live install"
    )


# ── Scenario 5: per-item diff drill-down (GET /git/diff) ───────────────────


def test_git_diff_prompt_setting_and_file(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    """The drill-down behind every change row.

    1. A drifted PROMPT diffs baseline -> live, with real ``-``/``+`` lines.
    2. The status rows carry the RAW ``key`` / ``section`` the endpoint needs,
       and feeding them straight back in resolves (the contract the UI relies
       on: it never constructs a key, it echoes the one it was given).
    3. A WORKSPACE FILE modified since the baseline diffs the same way.
    4. An unchanged field returns an empty diff rather than 404 — a row can go
       clean between the status read and the click.
    5. Denylisted / traversing / unknown keys are refused (400), including the
       ``credentials/`` path that must never be readable through this endpoint.
    """
    headers = superuser_token_headers
    bundle_id = f"io.test.git.diff.{random_lower_string()[:6]}"

    v1_dir = tmp_path / "v1"
    _make_agent_repo_tree(v1_dir, bundle_id=bundle_id, workflow_prompt="Workflow V1")
    result = _do_checkout(
        client, headers, clone_dir=v1_dir, sha=_SHA_V1,
        repo_url="https://github.com/example/diff-drilldown.git",
    )
    agent_id = result["agent"]["id"]
    env_id = result["agent"]["active_environment_id"]

    # ── Phase 1: drift the workflow prompt, then diff it ──────────────────
    r = client.put(
        f"{API}/agents/{agent_id}",
        headers=headers,
        json={"workflow_prompt": "Workflow V1\nplus a local line"},
    )
    assert r.status_code == 200, f"Agent update failed: {r.text}"

    r = client.get(
        _git_diff_url(agent_id),
        headers=headers,
        params={"section": "prompt", "key": "workflow_prompt"},
    )
    assert r.status_code == 200, f"prompt diff failed: {r.text}"
    diff = r.json()
    assert diff["section"] == "prompt"
    assert diff["key"] == "workflow_prompt"
    assert diff["label"] == "Workflow prompt"
    assert diff["change_type"] == "modified"
    assert diff["binary"] is False
    assert "+plus a local line" in diff["diff"], (
        f"Expected the added line on the b/ side, got:\n{diff['diff']}"
    )
    assert diff["diff"].startswith("--- a/workflow_prompt"), (
        "a/ must be the baseline and b/ the live agent (git's own convention)"
    )

    # ── Phase 2: the status row's key/section round-trip back in ──────────
    r = client.get(_git_status_url(agent_id), headers=headers)
    assert r.status_code == 200, f"status failed: {r.text}"
    status = r.json()
    prompt_row = next(
        c for c in status["prompt_changes"] if c["field"] == "Workflow prompt"
    )
    assert prompt_row["key"] == "workflow_prompt"
    assert prompt_row["section"] == "prompt"
    r = client.get(
        _git_diff_url(agent_id),
        headers=headers,
        params={"section": prompt_row["section"], "key": prompt_row["key"]},
    )
    assert r.status_code == 200, (
        f"A key echoed straight from the status row must resolve: {r.text}"
    )

    # Settings rows carry the pair too (the sdk drift is always present here).
    sdk_row = next(
        c for c in status["setting_changes"]
        if c["field"] == "SDK engine (conversation mode)"
    )
    assert sdk_row["key"] == "agent_sdk_conversation"
    assert sdk_row["section"] == "sdk"
    r = client.get(
        _git_diff_url(agent_id),
        headers=headers,
        params={"section": sdk_row["section"], "key": sdk_row["key"]},
    )
    assert r.status_code == 200, f"sdk diff failed: {r.text}"
    assert r.json()["label"] == "SDK engine (conversation mode)"

    # ── Phase 3: a modified workspace file ────────────────────────────────
    # The checkout seeded scripts/run.sh into the baseline snapshot; overwrite
    # the live copy so the two sides differ.
    _seed_env_workspace(env_id, {"scripts": {"run.sh": "#!/bin/bash\necho CHANGED"}})
    r = client.get(
        _git_diff_url(agent_id),
        headers=headers,
        params={"section": "file", "key": "scripts/run.sh"},
    )
    assert r.status_code == 200, f"file diff failed: {r.text}"
    file_diff = r.json()
    assert file_diff["change_type"] == "modified"
    assert "+echo CHANGED" in file_diff["diff"], (
        f"Expected the live file content on the b/ side, got:\n{file_diff['diff']}"
    )

    # ── Phase 4: an unchanged field is empty, not an error ────────────────
    r = client.get(
        _git_diff_url(agent_id),
        headers=headers,
        params={"section": "prompt", "key": "refiner_prompt"},
    )
    assert r.status_code == 200, f"unchanged prompt diff failed: {r.text}"
    assert r.json()["diff"] == ""
    assert r.json()["change_type"] == "unchanged"

    # ── Phase 5: rejected keys ────────────────────────────────────────────
    for section, key, why in [
        ("prompt", "hashed_password", "an arbitrary Agent attribute"),
        ("metadata", "id", "an attribute outside the metadata registry"),
        ("file", "../../etc/passwd", "path traversal"),
        ("file", "/etc/passwd", "an absolute path"),
        ("file", "credentials/creds.json", "a denylisted top-level dir"),
        ("file", "scripts/__pycache__/x.pyc", "a nested cache artifact"),
        ("nonsense", "whatever", "an unknown section"),
    ]:
        r = client.get(
            _git_diff_url(agent_id),
            headers=headers,
            params={"section": section, "key": key},
        )
        assert r.status_code == 400, (
            f"Expected 400 for {why} ({section}/{key}), got {r.status_code}: {r.text}"
        )


def test_git_diff_requires_ownership_and_baseline(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    normal_user_token_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    """Owner-resolved, with no existence leak; and no baseline -> 400, not 500."""
    headers = superuser_token_headers
    bundle_id = f"io.test.git.diffacl.{random_lower_string()[:6]}"

    v1_dir = tmp_path / "v1"
    _make_agent_repo_tree(v1_dir, bundle_id=bundle_id, workflow_prompt="Workflow V1")
    result = _do_checkout(
        client, headers, clone_dir=v1_dir, sha=_SHA_V1,
        repo_url="https://github.com/example/diff-acl.git",
    )
    agent_id = result["agent"]["id"]

    # A different user gets 404 (not 403) — the same no-existence-leak contract
    # every other git read path holds.
    r = client.get(
        _git_diff_url(agent_id),
        headers=normal_user_token_headers,
        params={"section": "prompt", "key": "workflow_prompt"},
    )
    assert r.status_code == 404, (
        f"Expected 404 for a non-owner, got {r.status_code}: {r.text}"
    )

    # An agent with no git source at all is likewise 404.
    r = client.post(
        f"{API}/agents/",
        headers=headers,
        json={"name": f"no-git-{random_lower_string()[:6]}"},
    )
    assert r.status_code == 200, f"agent create failed: {r.text}"
    r = client.get(
        _git_diff_url(r.json()["id"]),
        headers=headers,
        params={"section": "prompt", "key": "workflow_prompt"},
    )
    assert r.status_code == 404, (
        f"Expected 404 for an agent with no git source, got {r.status_code}: {r.text}"
    )
