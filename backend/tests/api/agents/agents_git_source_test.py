"""Integration tests: Git-backed agent versioning — checkout / pull / push / GitOps webhook.

Git network isolation strategy:
  All git network primitives (clone, ls_remote, push, commit) are patched at their
  import sites inside ``app.services.bundles.git_source_service`` — the module-local
  names created by the ``from app.services.knowledge.git_operations import <name>``
  statements. This eliminates real network calls while exercising every layer above
  the git transport: manifest parsing, bundle/revision creation, workspace seeding,
  prompt-sync, etc.

  The egress guard is tested WITHOUT patching git operations:
  ``test_git_checkout_success_and_source_read`` passes a private-IP HTTPS URL
  (``http://192.168.1.1/repo.git``); the egress guard inside ``clone_repository``
  raises ``EgressBlockedError`` before any git network call is issued, mapping to 400.

  GIT_SOURCE_ALLOW_PRIVATE_HOSTS is not overridden in happy-path tests: the egress guard
  never runs when the git primitives themselves are mocked out.

Unit tests for ``RevisionFormat`` live in ``tests/unit/test_revision_format.py``.
Workspace denylist invariants (credentials/logs/databases never committed, symlinks
skipped) are tested by ``tests/unit/test_bundle_workspace_snapshot.py`` and
``tests/unit/test_workspace_classification.py``.
"""
import json
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.cli import (
    cli_auth_headers,
    create_setup_token,
    exchange_setup_token,
)
from tests.utils.bundle import publish_bundle
from tests.utils.ssh_key import generate_random_ssh_key
from tests.utils.user import (
    create_random_user,
    create_random_user_with_headers,
    promote_to_developer,
    user_authentication_headers,
)
from tests.utils.utils import random_lower_string
from tests.utils.webhook import list_webhook_logs

API = settings.API_V1_STR


# ── Workspace seeding helpers ─────────────────────────────────────────────────


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
    """Create workspace files inside the env instance directory.

    Writes into ``ENV_INSTANCES_DIR/<env_id>/app/workspace/<tree>``.
    Returns the workspace root path.
    """
    ws_root = Path(settings.ENV_INSTANCES_DIR) / env_id / "app" / "workspace"
    _write_tree(ws_root, tree)
    return ws_root


# ── Patch targets (module-local import sites in git_source_service.py) ─────

_CLONE_CTX = "app.services.bundles.git_source_service.clone_repository_context"
_GET_HASH = "app.services.bundles.git_source_service.get_current_commit_hash"
_LS_REMOTE = "app.services.bundles.git_source_service.ls_remote_head"
_COMMIT_ALL = "app.services.bundles.git_source_service.commit_all"
_FF_PUSH = "app.services.bundles.git_source_service.fast_forward_push"
_INIT_REPO = "app.services.bundles.git_source_service.init_repo_with_remote"
_GIT_LOG = "app.services.bundles.git_source_service.git_log_subdir"
# Subdir-aware push precheck bug fix: `_push_locked` now delegates its
# fast-forward precheck to `_remote_change_is_relevant`, which — for a
# subdir-scoped agent with a baseline — calls `subdir_changed_between` to
# decide whether a remote HEAD advance is actually relevant to this agent.
_SUBDIR_CHANGED = "app.services.bundles.git_source_service.subdir_changed_between"

# ── Test git SHAs ─────────────────────────────────────────────────────────────

_SHA_V1 = "a" * 40  # initial checkout commit
_SHA_V2 = "b" * 40  # new remote commit (for pull/push advance)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_agent_repo_tree(
    base_dir: Path,
    bundle_id: str,
    *,
    workflow_prompt: str = "Test workflow prompt",
    subdir: str | None = None,
) -> Path:
    """Build a minimal v2 agent snapshot tree that passes checkout validation.

    Creates:
      <base_dir>[/<subdir>]/
        cinna.agent.json   (schema_version=2, bundle_id=<bundle_id>)
        workspace/
          scripts/
            run.sh
    Returns the effective source root (base_dir/<subdir> or base_dir).
    """
    src = base_dir / subdir if subdir else base_dir
    src.mkdir(parents=True, exist_ok=True)
    (src / "workspace" / "scripts").mkdir(parents=True, exist_ok=True)
    (src / "workspace" / "scripts" / "run.sh").write_text("#!/bin/bash\necho hello")
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
    (src / "cinna.agent.json").write_text(json.dumps(manifest))
    return src


def _fake_clone_ctx(repo_dir: Path, sha: str = _SHA_V1):
    """Return a context-manager replacement for ``clone_repository_context``.

    The returned callable accepts any arguments (url, branch, ssh_key_path, depth)
    and yields ``(str(repo_dir), mock_repo)`` so the rest of the service sees a
    real directory tree but never touches the network.
    """
    @contextmanager
    def _ctx(*args, **kwargs):
        mock_repo = MagicMock()
        mock_repo.head.commit.hexsha = sha
        yield str(repo_dir), mock_repo

    return _ctx


def _checkout_url(bundle_id: str = "") -> str:
    return f"{API}/agents/checkout"


def _git_source_url(agent_id: str) -> str:
    return f"{API}/agents/{agent_id}/git"


def _git_pull_url(agent_id: str) -> str:
    return f"{API}/agents/{agent_id}/git/pull"


def _git_push_url(agent_id: str) -> str:
    return f"{API}/agents/{agent_id}/git/push"


def _check_updates_url(agent_id: str) -> str:
    return f"{API}/agents/{agent_id}/git/check-updates"


def _git_connect_url(agent_id: str) -> str:
    return f"{API}/agents/{agent_id}/git/connect"


def _git_commits_url(agent_id: str) -> str:
    return f"{API}/agents/{agent_id}/git/commits"


def _git_dirty_url(agent_id: str) -> str:
    return f"{API}/agents/{agent_id}/git/dirty"


def _git_status_url(agent_id: str) -> str:
    return f"{API}/agents/{agent_id}/git/status"


def _webhooks_git_url(agent_id: str) -> str:
    return f"{API}/agents/{agent_id}/webhooks/git-source"


def _webhook_logs_url(agent_id: str, webhook_pk: str) -> str:
    return f"{API}/agents/{agent_id}/webhooks/{webhook_pk}/logs"


def _do_checkout(
    client: TestClient,
    headers: dict,
    *,
    repo_url: str = "https://github.com/example/agent.git",
    clone_dir: Path,
    sha: str = _SHA_V1,
    subdir: str | None = None,
    ssh_key_id: str | None = None,
    sync_direction: str = "bidirectional",
) -> dict:
    """Execute a checkout with mocked git ops. Returns response JSON (200 asserted)."""
    body = {
        "repo_url": repo_url,
        "ref": "main",
        "sync_direction": sync_direction,
    }
    if subdir is not None:
        body["subdir"] = subdir
    if ssh_key_id is not None:
        body["ssh_key_id"] = ssh_key_id

    with (
        patch(_CLONE_CTX, _fake_clone_ctx(clone_dir, sha)),
        patch(_GET_HASH, return_value=sha),
        patch(_LS_REMOTE, return_value=sha),
    ):
        r = client.post(_checkout_url(), headers=headers, json=body)

    assert r.status_code == 200, f"checkout failed: {r.text}"
    return r.json()


# ── Scenario 1: Happy-path checkout + source read + error cases ────────────────


def test_git_checkout_success_and_source_read(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    """
    Full checkout happy path and all stateless error cases:
      1. Public repo checkout → 200, correct response shape
      2. GET /agents/{id}/git → source fields persisted
      3. GET /agents/{id}/git/check-updates → update_available=False (same SHA)
      4. update_available=True when ls_remote returns a different SHA
      5. Developer gate: non-developer user → 403
      6. Same user re-checkout → 409
      7. Egress-blocked URL → 400 (no git patch; guard fires before network)
      8. Missing cinna.agent.json (no manifest in workspace-only dir) → 422
      9. Oversized file in workspace → 400
    """
    headers = superuser_token_headers
    bundle_id = f"io.test.git.{random_lower_string()[:8]}"

    # ── Phase 1: Happy-path public repo checkout ───────────────────────────────
    clone_dir = tmp_path / "repo_v1"
    _make_agent_repo_tree(clone_dir, bundle_id=bundle_id)

    result = _do_checkout(client, headers, clone_dir=clone_dir)

    agent_id = result["agent"]["id"]
    git_source = result["git_source"]

    assert result["agent"]["id"]
    assert git_source["repo_url"] == "https://github.com/example/agent.git"
    assert git_source["ref"] == "main"
    assert git_source["sync_direction"] == "bidirectional"
    assert git_source["status"] == "connected"
    assert git_source["last_synced_commit"] == _SHA_V1
    assert git_source["last_synced_commit"] is not None
    assert git_source["last_error"] is None
    # ssh_key_id not set
    assert git_source["ssh_key_id"] is None

    # ── Phase 2: GET /agents/{id}/git → source persisted ─────────────────────
    # ls_remote returns same SHA → update_available=False
    with patch(_LS_REMOTE, return_value=_SHA_V1):
        r = client.get(_git_source_url(agent_id), headers=headers)
    assert r.status_code == 200
    src_data = r.json()
    assert src_data["agent_id"] == agent_id
    assert src_data["status"] == "connected"
    assert src_data["last_synced_commit"] == _SHA_V1
    assert src_data["update_available"] is False
    # GitHub HTTPS remote, no subdir → commit-history + tree web URLs generated.
    assert (
        src_data["web_history_url"]
        == "https://github.com/example/agent/commits/main"
    )
    assert (
        src_data["web_tree_url"]
        == "https://github.com/example/agent/tree/main"
    )

    # ── Phase 3: check-updates → no update ────────────────────────────────────
    with patch(_LS_REMOTE, return_value=_SHA_V1):
        r = client.get(_check_updates_url(agent_id), headers=headers)
    assert r.status_code == 200
    upd = r.json()
    assert upd["update_available"] is False
    assert upd["remote_commit"] == _SHA_V1
    assert upd["last_synced_commit"] == _SHA_V1

    # ── Phase 4: check-updates → update available when remote advanced ─────────
    with patch(_LS_REMOTE, return_value=_SHA_V2):
        r = client.get(_check_updates_url(agent_id), headers=headers)
    assert r.status_code == 200
    upd2 = r.json()
    assert upd2["update_available"] is True
    assert upd2["remote_commit"] == _SHA_V2

    # ── Phase 5: Developer gate — non-developer user → 403 ────────────────────
    non_dev_user = create_random_user(client)
    non_dev_headers = user_authentication_headers(
        client=client, email=non_dev_user["email"], password=non_dev_user["_password"]
    )
    other_bundle_id = f"io.test.git.{random_lower_string()[:8]}"
    other_clone_dir = tmp_path / "repo_other_user"
    _make_agent_repo_tree(other_clone_dir, bundle_id=other_bundle_id)
    body = {"repo_url": "https://github.com/example/agent.git", "ref": "main"}
    with (
        patch(_CLONE_CTX, _fake_clone_ctx(other_clone_dir)),
        patch(_GET_HASH, return_value=_SHA_V1),
        patch(_LS_REMOTE, return_value=_SHA_V1),
    ):
        r = client.post(_checkout_url(), headers=non_dev_headers, json=body)
    assert r.status_code == 403, f"Expected 403 for non-developer, got {r.status_code}: {r.text}"

    # ── Phase 6: Same user re-checkout of same repo → 409 ────────────────────
    with (
        patch(_CLONE_CTX, _fake_clone_ctx(clone_dir)),
        patch(_GET_HASH, return_value=_SHA_V1),
        patch(_LS_REMOTE, return_value=_SHA_V1),
    ):
        r = client.post(
            _checkout_url(),
            headers=headers,
            json={"repo_url": "https://github.com/example/agent.git", "ref": "main"},
        )
    assert r.status_code == 409, f"Expected 409 for re-checkout, got {r.status_code}: {r.text}"

    # ── Phase 7: Egress-blocked URL → 400 (no git patches; guard fires first) ──
    # 192.168.1.1 is a private IP — egress guard rejects it inside clone_repository
    # before any real git network call.
    r = client.post(
        _checkout_url(),
        headers=headers,
        json={"repo_url": "http://192.168.1.1/repo.git", "ref": "main"},
    )
    assert r.status_code == 400, f"Expected 400 for blocked URL, got {r.status_code}: {r.text}"

    # ── Phase 8: No manifest (workspace/ exists but no cinna.agent.json) → 422 ─
    no_manifest_dir = tmp_path / "no_manifest"
    (no_manifest_dir / "workspace").mkdir(parents=True)
    # workspace/ exists (passes snapshot_layout check) but no cinna.agent.json
    # → RevisionFormat.read_manifest raises RevisionFormatError → 422
    with (
        patch(_CLONE_CTX, _fake_clone_ctx(no_manifest_dir)),
        patch(_GET_HASH, return_value=_SHA_V1),
    ):
        r = client.post(
            _checkout_url(),
            headers=headers,
            json={
                "repo_url": "https://github.com/example/agent.git",
                "ref": "main",
            },
        )
    assert r.status_code == 422, (
        f"Expected 422 for missing manifest, got {r.status_code}: {r.text}"
    )

    # ── Phase 9: Oversized file → 400 ────────────────────────────────────────
    oversize_dir = tmp_path / "oversize"
    oversize_bundle_id = f"io.test.git.oversize.{random_lower_string()[:6]}"
    _make_agent_repo_tree(oversize_dir, bundle_id=oversize_bundle_id)
    big_file = oversize_dir / "workspace" / "big.bin"
    big_file.write_bytes(b"x" * (settings.GIT_SOURCE_MAX_FILE_BYTES + 1))

    with (
        patch(_CLONE_CTX, _fake_clone_ctx(oversize_dir)),
        patch(_GET_HASH, return_value=_SHA_V1),
    ):
        r = client.post(
            _checkout_url(),
            headers=headers,
            json={"repo_url": "https://github.com/example/agent.git", "ref": "main"},
        )
    assert r.status_code == 400, (
        f"Expected 400 for oversized file, got {r.status_code}: {r.text}"
    )


# ── Scenario 2: Multi-user isolation + SSH key + subdir ──────────────────────


def test_git_checkout_isolation_subdir_and_ssh_key(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    """
    Checkout isolation and credential-path tests:
      1. Checkout with a subdir — correct tree is resolved
      2. Different user checks out the same repo/bundle_id → gets their own install
      3. Checkout with an SSH key ID → _resolve_ssh_key resolves key, clone proceeds
      4. Catalog bundle_id collision → 409 (another user's real publisher bundle)
    """
    headers = superuser_token_headers

    # ── Phase 1: Checkout with subdir ─────────────────────────────────────────
    subdir_bundle_id = f"io.test.git.subdir.{random_lower_string()[:6]}"
    # The clone root; the manifest lives under "myagent/" subdir.
    clone_root = tmp_path / "subdir_repo"
    _make_agent_repo_tree(clone_root, bundle_id=subdir_bundle_id, subdir="myagent")

    with (
        patch(_CLONE_CTX, _fake_clone_ctx(clone_root)),
        patch(_GET_HASH, return_value=_SHA_V1),
        patch(_LS_REMOTE, return_value=_SHA_V1),
    ):
        r = client.post(
            _checkout_url(),
            headers=headers,
            json={
                "repo_url": "https://github.com/example/agent.git",
                "ref": "main",
                "subdir": "myagent",
            },
        )
    assert r.status_code == 200, f"Subdir checkout failed: {r.text}"
    subdir_result = r.json()
    assert subdir_result["git_source"]["subdir"] == "myagent"
    subdir_agent_id = subdir_result["agent"]["id"]

    # ── Phase 2: Different user checks out the same repo ─────────────────────
    # Both users get their own Agent install (consumer install); neither is the publisher.
    user_b = create_random_user(client)
    user_b_headers = user_authentication_headers(
        client=client, email=user_b["email"], password=user_b["_password"]
    )
    promote_to_developer(client, superuser_token_headers, user_b["id"])

    # User B needs a default AI credential so environment provisioning passes.
    from tests.utils.ai_credential import create_random_ai_credential
    create_random_ai_credential(client, user_b_headers, set_default=True)

    # Same bundle_id, same "repo URL" — user B gets their own checkout.
    user_b_clone = tmp_path / "user_b_repo"
    _make_agent_repo_tree(user_b_clone, bundle_id=subdir_bundle_id, subdir="myagent")

    with (
        patch(_CLONE_CTX, _fake_clone_ctx(user_b_clone)),
        patch(_GET_HASH, return_value=_SHA_V1),
        patch(_LS_REMOTE, return_value=_SHA_V1),
    ):
        r_b = client.post(
            _checkout_url(),
            headers=user_b_headers,
            json={
                "repo_url": "https://github.com/example/agent.git",
                "ref": "main",
                "subdir": "myagent",
            },
        )
    assert r_b.status_code == 200, (
        f"Different-user checkout of same repo failed: {r_b.text}"
    )
    user_b_result = r_b.json()
    # User B gets a DISTINCT agent install.
    assert user_b_result["agent"]["id"] != subdir_agent_id
    # Both share the same bundle_id string from the manifest.
    assert user_b_result["git_source"]["status"] == "connected"

    # ── Phase 3: SSH key checkout ─────────────────────────────────────────────
    # Generate an SSH key via the API so it is stored in the DB.
    # _resolve_ssh_key will decrypt it and create a temp key file;
    # our mocked clone_repository_context ignores the key_path.
    ssh_key = generate_random_ssh_key(client, headers)
    ssh_key_id = ssh_key["id"]

    ssh_bundle_id = f"io.test.git.ssh.{random_lower_string()[:6]}"
    ssh_clone_dir = tmp_path / "ssh_repo"
    _make_agent_repo_tree(ssh_clone_dir, bundle_id=ssh_bundle_id)

    with (
        patch(_CLONE_CTX, _fake_clone_ctx(ssh_clone_dir)),
        patch(_GET_HASH, return_value=_SHA_V1),
        patch(_LS_REMOTE, return_value=_SHA_V1),
    ):
        r = client.post(
            _checkout_url(),
            headers=headers,
            json={
                "repo_url": "git@github.com:example/private-agent.git",
                "ref": "main",
                "ssh_key_id": ssh_key_id,
            },
        )
    assert r.status_code == 200, f"SSH key checkout failed: {r.text}"
    ssh_result = r.json()
    assert ssh_result["git_source"]["ssh_key_id"] == ssh_key_id

    # ── Phase 4: Catalog bundle_id collision → 409 ────────────────────────────
    # Create an agent, publish it → creates an AgentBundle with publisher_user_id.
    # Then a different developer tries to checkout a repo whose manifest has the SAME bundle_id.
    # The collision guard should reject this to prevent cross-tenant injection.
    catalog_agent = create_agent_via_api(client, headers, name="Catalog Publisher")
    drain_tasks()
    # Get the agent's bundle_id (auto-generated on creation).
    catalog_agent_data = get_agent(client, headers, catalog_agent["id"])
    catalog_bundle_id = catalog_agent_data.get("bundle_id")
    assert catalog_bundle_id, "Agent must have a bundle_id"

    # Publish to create the catalog bundle.
    publish_bundle(client, headers, catalog_agent["id"])

    # Now user_b (developer, not superuser) tries to checkout a repo whose manifest
    # uses the same bundle_id as the just-published catalog bundle.
    collision_clone = tmp_path / "collision_repo"
    _make_agent_repo_tree(collision_clone, bundle_id=catalog_bundle_id)

    with (
        patch(_CLONE_CTX, _fake_clone_ctx(collision_clone)),
        patch(_GET_HASH, return_value=_SHA_V1),
        patch(_LS_REMOTE, return_value=_SHA_V1),
    ):
        r = client.post(
            _checkout_url(),
            headers=user_b_headers,
            json={
                "repo_url": "https://github.com/attacker/agent.git",
                "ref": "main",
            },
        )
    assert r.status_code == 409, (
        f"Expected 409 for catalog bundle_id collision, got {r.status_code}: {r.text}"
    )


# ── Scenario 3: Pull scenarios ────────────────────────────────────────────────


def test_git_pull_scenarios(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    """
    All pull scenarios, exercised on a single checked-out agent:
      1. Setup: checkout with SHA_V1
      2. No-op pull (remote SHA == last_synced → CONNECTED, no revision created)
      3. Pull with new remote commit → advances last_synced_commit to SHA_V2
      4. Dirty workspace (prompt changed since last sync) → 409 on pull with new SHA
      5. Wrong sync_direction (push-only) → 400
      6. Pull failure → source status=ERROR, last_error stamped
    """
    headers = superuser_token_headers
    bundle_id = f"io.test.git.pull.{random_lower_string()[:6]}"

    # ── Phase 1: Checkout ─────────────────────────────────────────────────────
    v1_dir = tmp_path / "pull_repo_v1"
    _make_agent_repo_tree(v1_dir, bundle_id=bundle_id, workflow_prompt="Workflow V1")

    result = _do_checkout(client, headers, clone_dir=v1_dir, sha=_SHA_V1)
    agent_id = result["agent"]["id"]

    # ── Phase 2: No-op pull (remote == last_synced) ───────────────────────────
    with (
        patch(_LS_REMOTE, return_value=_SHA_V1),
        patch(_CLONE_CTX, _fake_clone_ctx(v1_dir, _SHA_V1)),
        patch(_GET_HASH, return_value=_SHA_V1),
    ):
        r = client.post(_git_pull_url(agent_id), headers=headers)
    assert r.status_code == 200, f"No-op pull failed: {r.text}"
    # last_synced_commit unchanged
    with patch(_LS_REMOTE, return_value=_SHA_V1):
        src_data = client.get(_git_source_url(agent_id), headers=headers).json()
    assert src_data["last_synced_commit"] == _SHA_V1
    assert src_data["status"] == "connected"

    # ── Phase 3: Pull with new remote commit → advances SHA ───────────────────
    v2_dir = tmp_path / "pull_repo_v2"
    _make_agent_repo_tree(v2_dir, bundle_id=bundle_id, workflow_prompt="Workflow V2")

    with (
        patch(_LS_REMOTE, return_value=_SHA_V2),
        patch(_CLONE_CTX, _fake_clone_ctx(v2_dir, _SHA_V2)),
        patch(_GET_HASH, return_value=_SHA_V2),
    ):
        r = client.post(_git_pull_url(agent_id), headers=headers)
    assert r.status_code == 200, f"Advance pull failed: {r.text}"

    # Verify last_synced_commit advanced.
    with patch(_LS_REMOTE, return_value=_SHA_V2):
        src_after = client.get(_git_source_url(agent_id), headers=headers).json()
    assert src_after["last_synced_commit"] == _SHA_V2
    assert src_after["status"] == "connected"
    assert src_after["last_error"] is None

    # The pulled workflow_prompt should now reflect V2 manifest.
    agent_after = client.get(f"{API}/agents/{agent_id}", headers=headers).json()
    assert agent_after["workflow_prompt"] == "Workflow V2"

    # ── Phase 5 + 6 pre-setup: checkout agents before Phase 4 ─────────────────
    # Create the Phase 5 and 6 agents BEFORE Phase 4's dirty-pull so they are
    # committed to the outer test transaction.  _mark_source_error now uses
    # session.get_nested_transaction().rollback() (ROLLBACK TO SAVEPOINT) instead
    # of session.rollback() (full ROLLBACK), so Phase 4's error path no longer
    # destroys prior-committed test data.

    push_only_bundle_id = f"io.test.git.pushonly.{random_lower_string()[:6]}"
    push_only_dir = tmp_path / "push_only_repo"
    _make_agent_repo_tree(push_only_dir, bundle_id=push_only_bundle_id)

    with (
        patch(_CLONE_CTX, _fake_clone_ctx(push_only_dir)),
        patch(_GET_HASH, return_value=_SHA_V1),
        patch(_LS_REMOTE, return_value=_SHA_V1),
    ):
        push_only_result = client.post(
            _checkout_url(),
            headers=headers,
            json={
                "repo_url": "https://github.com/example/pushonly.git",
                "ref": "main",
                "sync_direction": "push",
            },
        )
    assert push_only_result.status_code == 200, (
        f"Push-only checkout failed: {push_only_result.text}"
    )
    push_only_agent_id = push_only_result.json()["agent"]["id"]

    fail_bundle_id = f"io.test.git.fail.{random_lower_string()[:6]}"
    fail_dir = tmp_path / "fail_repo"
    _make_agent_repo_tree(fail_dir, bundle_id=fail_bundle_id)
    fail_result = _do_checkout(client, headers, clone_dir=fail_dir, sha=_SHA_V1,
                               repo_url="https://github.com/example/fail.git")
    fail_agent_id = fail_result["agent"]["id"]

    # ── Phase 4: Dirty workspace → 409 on pull with new SHA ───────────────────
    # Modify the workflow_prompt via PUT to create a DB-side dirty divergence.
    r = client.put(
        f"{API}/agents/{agent_id}",
        headers=headers,
        json={"workflow_prompt": "Local modification"},
    )
    assert r.status_code == 200, f"Agent update failed: {r.text}"

    # ls_remote returns a different SHA → pull is NOT a no-op → dirty guard fires.
    _SHA_V3 = "c" * 40
    v3_dir = tmp_path / "pull_repo_v3"
    _make_agent_repo_tree(v3_dir, bundle_id=bundle_id, workflow_prompt="Workflow V3")

    with (
        patch(_LS_REMOTE, return_value=_SHA_V3),
        patch(_CLONE_CTX, _fake_clone_ctx(v3_dir, _SHA_V3)),
        patch(_GET_HASH, return_value=_SHA_V3),
    ):
        r = client.post(_git_pull_url(agent_id), headers=headers)
    assert r.status_code == 409, (
        f"Expected 409 for dirty workspace pull, got {r.status_code}: {r.text}"
    )

    # ── Phase 5: Wrong sync_direction (push-only) → 400 ─────────────────────
    # push_only_agent_id was checked out above, before Phase 4.
    # The sync_direction guard fires before any workspace or credential check.
    with patch(_LS_REMOTE, return_value=_SHA_V2):
        r = client.post(_git_pull_url(push_only_agent_id), headers=headers)
    assert r.status_code == 400, (
        f"Expected 400 for pull on push-only source, got {r.status_code}: {r.text}"
    )

    # ── Phase 6: Pull failure → status=ERROR, last_error stamped ─────────────
    # fail_agent_id was checked out above, before Phase 4's rollback.
    from app.services.knowledge.git_operations import GitOperationError

    # ls_remote returns a new SHA (so we're NOT on the no-op path).
    # Then clone_repository_context raises GitOperationError (simulating a network failure).
    _FAIL_SHA = "f" * 40

    @contextmanager
    def _failing_clone(*args, **kwargs):
        raise GitOperationError("Simulated clone failure")
        yield  # unreachable, needed to make it a generator

    with (
        patch(_LS_REMOTE, return_value=_FAIL_SHA),
        patch(_CLONE_CTX, _failing_clone),
    ):
        r = client.post(_git_pull_url(fail_agent_id), headers=headers)

    # Service maps GitOperationError → 400.
    assert r.status_code == 400, (
        f"Expected 400 for pull failure, got {r.status_code}: {r.text}"
    )

    # Source status should now be ERROR with last_error populated.
    # ls_remote on GET /git would also fail for a real remote, so patch it.
    with patch(_LS_REMOTE, side_effect=GitOperationError("network down")):
        # get_source swallows ls_remote failures (best-effort), returns False.
        src_fail = client.get(_git_source_url(fail_agent_id), headers=headers).json()
    assert src_fail["status"] == "error", (
        f"Expected source status=error after pull failure, got {src_fail['status']}"
    )
    assert src_fail["last_error"] is not None and len(src_fail["last_error"]) > 0, (
        "Expected last_error to be stamped after pull failure"
    )


# ── Scenario 4: Push scenarios ────────────────────────────────────────────────


def test_git_push_scenarios(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    """
    All push scenarios, exercised on a single checked-out agent:
      1. Setup: checkout
      2. Push success (fast-forward) → last_synced_commit advances
      3. Non-FF: remote advanced since last sync → 409 "pull first"
      4. Developer gate: non-developer user → 403
      5. Wrong sync_direction (pull-only) → 400
    """
    headers = superuser_token_headers
    bundle_id = f"io.test.git.push.{random_lower_string()[:6]}"

    # ── Phase 1: Checkout ─────────────────────────────────────────────────────
    v1_dir = tmp_path / "push_repo_v1"
    _make_agent_repo_tree(v1_dir, bundle_id=bundle_id)

    result = _do_checkout(
        client, headers, clone_dir=v1_dir, sha=_SHA_V1,
        repo_url="https://github.com/example/pushtest.git",
    )
    agent_id = result["agent"]["id"]

    # Seed the agent's active environment workspace on disk so that the push
    # service's _assert_workspace_readable check passes.  The push route reads
    # the workspace from ENV_INSTANCES_DIR/<env_id>/app/workspace/ before
    # building the commit tree.
    agent_data = client.get(f"{API}/agents/{agent_id}", headers=headers).json()
    env_id = str(agent_data["active_environment_id"])
    _seed_env_workspace(
        env_id, {"docs": {"workflow.md": "# Workflow\nTest"}, "scripts": {"run.sh": "#!/bin/bash\necho hello"}}
    )

    # ── Phase 2: Push success (FF) ────────────────────────────────────────────
    # ls_remote returns _SHA_V1 (same as last_synced → no advance, can push).
    # commit_all returns _SHA_V2 (the new commit after writing the tree).
    # fast_forward_push is a no-op (returns None on success).
    with (
        patch(_LS_REMOTE, return_value=_SHA_V1),   # remote == last_synced → FF allowed
        patch(_CLONE_CTX, _fake_clone_ctx(v1_dir, _SHA_V1)),
        patch(_GET_HASH, return_value=_SHA_V1),
        patch(_COMMIT_ALL, return_value=_SHA_V2),
        patch(_FF_PUSH, return_value=None),
    ):
        r = client.post(
            _git_push_url(agent_id),
            headers=headers,
            json={"commit_message": "Test push commit"},
        )
    assert r.status_code == 200, f"Push failed: {r.text}"
    push_result = r.json()
    assert push_result["status"] == "connected"
    assert push_result["last_synced_commit"] == _SHA_V2

    # Verify via GET /agents/{id}/git
    with patch(_LS_REMOTE, return_value=_SHA_V2):
        src_after = client.get(_git_source_url(agent_id), headers=headers).json()
    assert src_after["last_synced_commit"] == _SHA_V2

    # ── Phase 3: Non-FF: remote advanced since last sync → 409 ────────────────
    # ls_remote returns a DIFFERENT SHA than last_synced (_SHA_V2) → non-FF error.
    _SHA_REMOTE_AHEAD = "9" * 40

    with (
        patch(_LS_REMOTE, return_value=_SHA_REMOTE_AHEAD),  # remote advanced
        patch(_CLONE_CTX, _fake_clone_ctx(v1_dir, _SHA_V2)),
        patch(_GET_HASH, return_value=_SHA_V2),
    ):
        r = client.post(
            _git_push_url(agent_id),
            headers=headers,
            json={"commit_message": "Should fail"},
        )
    assert r.status_code == 409, (
        f"Expected 409 for non-FF push, got {r.status_code}: {r.text}"
    )
    assert "pull" in r.json()["detail"].lower() or "advance" in r.json()["detail"].lower(), (
        f"Expected 'pull' in 409 detail, got: {r.json()['detail']}"
    )

    # ── Phase 4: Developer gate — non-developer user → 403 ────────────────────
    non_dev = create_random_user(client)
    non_dev_headers = user_authentication_headers(
        client=client, email=non_dev["email"], password=non_dev["_password"]
    )
    r = client.post(
        _git_push_url(agent_id),
        headers=non_dev_headers,
        json={"commit_message": "gate test"},
    )
    assert r.status_code == 403, (
        f"Expected 403 for non-developer push, got {r.status_code}: {r.text}"
    )

    # ── Phase 5: Pull-only source → 400 on push ───────────────────────────────
    pull_only_bundle_id = f"io.test.git.pullonly.{random_lower_string()[:6]}"
    pull_only_dir = tmp_path / "pull_only_repo"
    _make_agent_repo_tree(pull_only_dir, bundle_id=pull_only_bundle_id)

    pull_only_result = _do_checkout(
        client, headers, clone_dir=pull_only_dir, sha=_SHA_V1,
        repo_url="https://github.com/example/pullonly.git",
        sync_direction="pull",
    )
    pull_only_agent_id = pull_only_result["agent"]["id"]

    with (
        patch(_LS_REMOTE, return_value=_SHA_V1),
        patch(_CLONE_CTX, _fake_clone_ctx(pull_only_dir, _SHA_V1)),
        patch(_GET_HASH, return_value=_SHA_V1),
    ):
        r = client.post(
            _git_push_url(pull_only_agent_id),
            headers=headers,
            json={"commit_message": "Forbidden push"},
        )
    assert r.status_code == 400, (
        f"Expected 400 for push on pull-only source, got {r.status_code}: {r.text}"
    )


# ── Scenario 4b: Push fast-forward precheck is subdir-aware (bug fix) ───────


def test_git_push_subdir_scoped_precheck(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    """
    Regression coverage for the subdir-aware push precheck fix.

    Before the fix, ``_push_locked``'s fast-forward precheck compared the
    remote HEAD directly against ``last_synced_commit``: ANY advance raised
    409 "Remote has advanced since the last sync — pull first.", even when the
    advance was a commit to an unrelated folder of a shared repo that never
    touched this agent's ``subdir``. This stranded subdir-scoped agents that
    could never push, and disagreed with the update-check banner (which was
    already subdir-scoped), since the two now share a single decision function
    (``_remote_change_is_relevant``).

      1. Checkout a subdir-scoped agent ("myagent"), last_synced_commit=SHA_V1
      2. Remote HEAD advanced to an unrelated commit; subdir_changed_between
         returns False → push now SUCCEEDS (200), not the naive 409
      3. Remote HEAD advanced again; subdir_changed_between returns True
         (the advance DOES touch this agent's subdir) → push still 409

    Root (no-subdir) agents keep raising 409 on ANY advance without ever
    calling ``subdir_changed_between`` — already covered by
    ``test_git_push_scenarios`` Scenario 3, which never patches
    ``subdir_changed_between``; if the fix regressed into calling it
    unconditionally for root agents, that scenario would fail immediately
    (unpatched call attempts a real clone).
    """
    headers = superuser_token_headers
    bundle_id = f"io.test.git.push.subdir.{random_lower_string()[:6]}"

    # ── Phase 1: Checkout a subdir-scoped agent ───────────────────────────────
    v1_dir = tmp_path / "push_subdir_repo_v1"
    _make_agent_repo_tree(v1_dir, bundle_id=bundle_id, subdir="myagent")

    result = _do_checkout(
        client, headers, clone_dir=v1_dir, sha=_SHA_V1, subdir="myagent",
        repo_url="https://github.com/example/push-subdir.git",
    )
    agent_id = result["agent"]["id"]
    assert result["git_source"]["subdir"] == "myagent"
    assert result["git_source"]["last_synced_commit"] == _SHA_V1

    # Seed the agent's active environment workspace on disk so the push
    # service's _assert_workspace_readable check passes.
    agent_data = client.get(f"{API}/agents/{agent_id}", headers=headers).json()
    env_id = str(agent_data["active_environment_id"])
    _seed_env_workspace(
        env_id,
        {"docs": {"workflow.md": "# Workflow\nTest"}, "scripts": {"run.sh": "#!/bin/bash\necho hello"}},
    )

    # ── Phase 2: Remote advanced but subdir UNCHANGED → push succeeds ─────────
    # ls_remote returns a SHA different from last_synced_commit (an advance),
    # but subdir_changed_between says the advance never touched "myagent/" —
    # the precheck must fall through and let the push proceed.
    _SHA_UNRELATED = "c" * 40

    with (
        patch(_LS_REMOTE, return_value=_SHA_UNRELATED),
        patch(_SUBDIR_CHANGED, return_value=False),
        patch(_CLONE_CTX, _fake_clone_ctx(v1_dir, _SHA_V1)),
        patch(_GET_HASH, return_value=_SHA_V1),
        patch(_COMMIT_ALL, return_value=_SHA_V2),
        patch(_FF_PUSH, return_value=None),
    ):
        r = client.post(
            _git_push_url(agent_id),
            headers=headers,
            json={"commit_message": "Push despite unrelated remote advance"},
        )
    assert r.status_code == 200, (
        "Expected push to succeed when the remote advance does not touch "
        f"this agent's subdir, got {r.status_code}: {r.text}"
    )
    push_result = r.json()
    assert push_result["status"] == "connected"
    assert push_result["last_synced_commit"] == _SHA_V2

    # Verify the source really advanced via GET /agents/{id}/git (remote-free).
    src_after = client.get(_git_source_url(agent_id), headers=headers).json()
    assert src_after["last_synced_commit"] == _SHA_V2

    # ── Phase 3: Remote advanced AND subdir CHANGED → still 409 ───────────────
    # subdir_changed_between now says the advance DOES touch "myagent/" — the
    # precheck must still block with the conflict error.
    _SHA_RELATED = "d" * 40

    with (
        patch(_LS_REMOTE, return_value=_SHA_RELATED),
        patch(_SUBDIR_CHANGED, return_value=True),
        patch(_CLONE_CTX, _fake_clone_ctx(v1_dir, _SHA_V2)),
        patch(_GET_HASH, return_value=_SHA_V2),
    ):
        r = client.post(
            _git_push_url(agent_id),
            headers=headers,
            json={"commit_message": "Should fail - subdir-relevant advance"},
        )
    assert r.status_code == 409, (
        "Expected 409 when the remote advance touches this agent's subdir, "
        f"got {r.status_code}: {r.text}"
    )
    assert "pull" in r.json()["detail"].lower() or "advance" in r.json()["detail"].lower(), (
        f"Expected 'pull'/'advance' in 409 detail, got: {r.json()['detail']}"
    )


# ── Scenario 5: GitOps webhook lifecycle ─────────────────────────────────────


def test_git_source_webhook_lifecycle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    """
    GitOps webhook (AgentWebhookType.GIT_SOURCE) full lifecycle:
      1. Setup: checkout an agent
      2. Create a git-source webhook (developer-gated)
      3. Fire with a bad token → 401
      4. Fire with valid token → triggers pull_update → log created with status
      5. Verify invocation logged via GET /agents/{id}/webhooks/{pk}/logs
    """
    headers = superuser_token_headers
    bundle_id = f"io.test.git.webhook.{random_lower_string()[:6]}"

    # ── Phase 1: Checkout ─────────────────────────────────────────────────────
    clone_dir = tmp_path / "webhook_repo"
    _make_agent_repo_tree(clone_dir, bundle_id=bundle_id)

    result = _do_checkout(
        client, headers, clone_dir=clone_dir, sha=_SHA_V1,
        repo_url="https://github.com/example/gitops-agent.git",
    )
    agent_id = result["agent"]["id"]

    # ── Phase 2: Create git-source webhook ────────────────────────────────────
    r = client.post(
        _webhooks_git_url(agent_id),
        headers=headers,
        json={"name": "GitHub Push Trigger", "type": "git_source"},
    )
    assert r.status_code == 200, f"Create git-source webhook failed: {r.text}"
    webhook_data = r.json()
    webhook_pk = webhook_data["id"]
    webhook_id = webhook_data["webhook_id"]   # public URL slug
    webhook_token = webhook_data["webhook_token"]  # one-time plaintext token

    assert webhook_data["type"] == "git_source"
    assert webhook_id
    assert webhook_token

    # ── Phase 3: Fire with bad token → 401 ────────────────────────────────────
    r = client.post(
        f"/agent-hooks/{webhook_id}",
        headers={"Authorization": "Bearer invalid_token_xyz"},
    )
    assert r.status_code == 401, (
        f"Expected 401 for bad token, got {r.status_code}: {r.text}"
    )

    # ── Phase 4: Fire with valid token → pull_update triggered ────────────────
    # The webhook fires pull_update for the agent. Since the remote SHA matches
    # last_synced_commit, the pull is a no-op (CONNECTED), but the invocation
    # is logged regardless of pull outcome.
    with (
        patch(_LS_REMOTE, return_value=_SHA_V1),   # same SHA → no-op pull
        patch(_CLONE_CTX, _fake_clone_ctx(clone_dir, _SHA_V1)),
        patch(_GET_HASH, return_value=_SHA_V1),
    ):
        r = client.post(
            f"/agent-hooks/{webhook_id}",
            headers={"Authorization": f"Bearer {webhook_token}"},
            content=b'{"ref": "refs/heads/main"}',
        )
    assert r.status_code == 200, f"Webhook fire failed: {r.status_code}: {r.text}"
    fire_result = r.json()
    assert fire_result["success"] is True
    assert fire_result["webhook_type"] == "git_source"
    assert fire_result["log_id"]

    # ── Phase 5: Verify invocation logged ────────────────────────────────────
    r = client.get(_webhook_logs_url(agent_id, webhook_pk), headers=headers)
    assert r.status_code == 200
    logs_data = r.json()
    assert logs_data["count"] >= 1, "Expected at least one webhook log after fire"
    latest_log = logs_data["data"][0]  # newest first
    assert latest_log["webhook_type"] == "git_source"
    # No-op pull → status="success" (pull returned without error)
    assert latest_log["status"] == "success"
    assert latest_log["duration_ms"] is not None

    # Also verify the source is still connected after the no-op pull.
    with patch(_LS_REMOTE, return_value=_SHA_V1):
        src_data = client.get(_git_source_url(agent_id), headers=headers).json()
    assert src_data["status"] == "connected"


# ── Connect / disconnect helpers ──────────────────────────────────────────────


def _ensure_default_ai_credential(client: TestClient, headers: dict) -> None:
    """Give the caller a default AI credential so ``POST /agents/`` can provision.

    ``create_agent_via_api`` provisions an env which requires a default AI
    credential; the checkout flow seeds from a bundle revision instead, so only
    the connect-based tests (which create a normal agent) need this.
    """
    from tests.utils.ai_credential import create_random_ai_credential

    create_random_ai_credential(client, headers, set_default=True)


def _create_agent_with_env(
    client: TestClient,
    headers: dict,
    *,
    tree: dict | None = None,
) -> tuple[str, str]:
    """Create a normal agent and return ``(agent_id, env_id)``.

    Optionally seeds the active env's ``app/workspace/`` with ``tree`` so the
    connect export (and the dirty workspace digest) has real files on disk.
    """
    agent = create_agent_via_api(client, headers)
    drain_tasks()
    data = get_agent(client, headers, agent["id"])
    env_id = data.get("active_environment_id")
    assert env_id, "created agent must have an active environment"
    if tree is not None:
        _seed_env_workspace(str(env_id), tree)
    return agent["id"], str(env_id)


def _connect(
    client: TestClient,
    headers: dict,
    agent_id: str,
    *,
    repo_url: str = "https://github.com/example/connect.git",
    subdir: str | None = None,
    ref: str = "main",
    sync_direction: str = "bidirectional",
    ssh_key_id: str | None = None,
    commit_message: str | None = None,
    adopt_existing: bool | None = None,
):
    body: dict = {"repo_url": repo_url, "ref": ref, "sync_direction": sync_direction}
    if subdir is not None:
        body["subdir"] = subdir
    if ssh_key_id is not None:
        body["ssh_key_id"] = ssh_key_id
    if commit_message is not None:
        body["commit_message"] = commit_message
    if adopt_existing is not None:
        body["adopt_existing"] = adopt_existing
    return client.post(_git_connect_url(agent_id), headers=headers, json=body)


# ── Scenario 6: Connect (enable-on-existing-agent) + disconnect ───────────────


def test_git_connect_and_disconnect_scenarios(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    """
    Connect/disconnect flow:
      1. Connect onto an EMPTY remote (init path) → 200, source connected
      2. Connect when a source already exists → 409
      3. Disconnect removes the row; second disconnect → 404
      4. Connect onto a remote with prior history, empty subdir (ff path) → 200
      5. Connect with sync_direction='pull' → 400
      6. Connect with an unreadable workspace (env not seeded) → 400
      7. Connect onto a subdir that already holds an agent → 409
      8. Connect with a non-owned ssh_key_id → 400
    """
    from app.services.knowledge.git_operations import GitOperationError

    headers = superuser_token_headers

    # ── Phase 1: Connect onto an empty remote (init path) ─────────────────────
    agent_id, env_id = _create_agent_with_env(
        client, headers, tree={"docs": {"workflow.md": "# Workflow"}}
    )

    with (
        patch(_LS_REMOTE, side_effect=GitOperationError("Ref 'main' not found")),
        patch(_INIT_REPO, return_value=MagicMock()),
        patch(_GET_HASH, return_value=_SHA_V1),
        patch(_COMMIT_ALL, return_value=_SHA_V2),
        patch(_FF_PUSH, return_value=None),
    ):
        r = _connect(client, headers, agent_id, commit_message="Initial export")
    assert r.status_code == 200, f"Connect (init path) failed: {r.text}"
    source = r.json()
    assert source["status"] == "connected"
    assert source["agent_id"] == agent_id
    assert source["last_synced_commit"] == _SHA_V2
    assert source["sync_direction"] == "bidirectional"
    assert source["update_available"] is False
    assert source["ssh_key_id"] is None

    # The agent payload now reflects git versioning enabled (drives the card
    # toggle before its own git-source query resolves).
    agent_after_connect = get_agent(client, headers, agent_id)
    assert agent_after_connect["git_versioning_enabled"] is True

    # ── Phase 2: Connect again (source already exists) → 409 ──────────────────
    with (
        patch(_LS_REMOTE, side_effect=GitOperationError("Ref 'main' not found")),
        patch(_INIT_REPO, return_value=MagicMock()),
        patch(_GET_HASH, return_value=_SHA_V1),
        patch(_COMMIT_ALL, return_value=_SHA_V2),
        patch(_FF_PUSH, return_value=None),
    ):
        r = _connect(client, headers, agent_id)
    assert r.status_code == 409, f"Expected 409 for double connect, got {r.text}"

    # ── Phase 3: Disconnect removes the row; second disconnect → 404 ──────────
    r = client.delete(_git_source_url(agent_id), headers=headers)
    assert r.status_code == 200, f"Disconnect failed: {r.text}"
    # The source is gone now.
    r = client.get(_git_source_url(agent_id), headers=headers)
    assert r.status_code == 404, f"Expected 404 after disconnect, got {r.text}"
    r = client.delete(_git_source_url(agent_id), headers=headers)
    assert r.status_code == 404, f"Expected 404 for second disconnect, got {r.text}"
    # The agent payload now reflects git versioning disabled again.
    agent_after_disconnect = get_agent(client, headers, agent_id)
    assert agent_after_disconnect["git_versioning_enabled"] is False

    # ── Phase 4: Connect onto a remote with prior history, empty subdir (ff) ──
    ff_agent_id, _ = _create_agent_with_env(
        client, headers, tree={"scripts": {"run.sh": "echo hi"}}
    )
    # Clone dir exists but has NO cinna.agent.json at the (root) checkout path.
    ff_clone = tmp_path / "ff_remote"
    ff_clone.mkdir()
    with (
        patch(_LS_REMOTE, return_value=_SHA_V1),  # remote exists
        patch(_CLONE_CTX, _fake_clone_ctx(ff_clone, _SHA_V1)),
        patch(_GET_HASH, return_value=_SHA_V1),
        patch(_COMMIT_ALL, return_value=_SHA_V2),
        patch(_FF_PUSH, return_value=None),
    ):
        r = _connect(client, headers, ff_agent_id)
    assert r.status_code == 200, f"Connect (ff path) failed: {r.text}"
    assert r.json()["last_synced_commit"] == _SHA_V2

    # ── Phase 5: sync_direction='pull' → 400 (initial export is a write) ──────
    pull_agent_id, _ = _create_agent_with_env(client, headers, tree={"a": {"b": "c"}})
    r = _connect(client, headers, pull_agent_id, sync_direction="pull")
    assert r.status_code == 400, f"Expected 400 for pull-only connect, got {r.text}"

    # ── Phase 6: Unreadable workspace (workspace dir absent on disk) → 400 ────
    import shutil as _shutil

    no_ws_agent_id, no_ws_env = _create_agent_with_env(client, headers, tree=None)
    # Env creation leaves an empty app/workspace; remove it so the readable guard
    # (which only allows an empty-but-present dir) fails as for a stopped env.
    ws_dir = Path(settings.ENV_INSTANCES_DIR) / no_ws_env / "app" / "workspace"
    if ws_dir.exists():
        _shutil.rmtree(ws_dir)
    r = _connect(client, headers, no_ws_agent_id)
    assert r.status_code == 400, f"Expected 400 for unreadable workspace, got {r.text}"

    # ── Phase 7: Subdir already holds an agent → 409 ─────────────────────────
    occupied_agent_id, _ = _create_agent_with_env(
        client, headers, tree={"docs": {"x.md": "y"}}
    )
    occupied_clone = tmp_path / "occupied_remote"
    # The remote already has an agent tree under "myagent/".
    _make_agent_repo_tree(occupied_clone, bundle_id="io.test.occupied", subdir="myagent")
    with (
        patch(_LS_REMOTE, return_value=_SHA_V1),
        patch(_CLONE_CTX, _fake_clone_ctx(occupied_clone, _SHA_V1)),
        patch(_GET_HASH, return_value=_SHA_V1),
    ):
        r = _connect(client, headers, occupied_agent_id, subdir="myagent")
    assert r.status_code == 409, (
        f"Expected 409 for subdir-already-has-agent, got {r.text}"
    )
    # The 409 is recoverable: it carries a machine-readable code so the UI can
    # offer to adopt the existing folder.
    assert r.json()["detail"]["code"] == "existing_agent_folder"

    # ── Phase 8: Non-owned / unknown ssh_key_id → 400 (validation) ────────────
    key_agent_id, _ = _create_agent_with_env(client, headers, tree={"d": {"e": "f"}})
    r = _connect(client, headers, key_agent_id, ssh_key_id=str(uuid.uuid4()))
    assert r.status_code == 400, f"Expected 400 for foreign ssh key, got {r.text}"


# ── Scenario 6b: Adopt an existing remote folder on connect ───────────────────


def test_git_connect_adopt_existing_folder(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    """
    Connect onto a subdir that already holds an agent:
      1. Without adopt_existing → recoverable 409, code "existing_agent_folder"
      2. With adopt_existing=True → 200, source connected at the remote HEAD,
         NO push (commit_all / fast_forward_push are never called)
      3. The adopted remote becomes the dirty-check baseline (GET /git/dirty 200)
    """
    headers = superuser_token_headers

    agent_id, _env_id = _create_agent_with_env(
        client, headers, tree={"docs": {"workflow.md": "# Local workflow"}}
    )

    # Remote already has an agent tree under "myagent/".
    remote_clone = tmp_path / "adopt_remote"
    _make_agent_repo_tree(
        remote_clone, bundle_id="io.test.adopt", subdir="myagent"
    )

    # ── Phase 1: plain connect → recoverable 409 with structured code ─────────
    with (
        patch(_LS_REMOTE, return_value=_SHA_V1),
        patch(_CLONE_CTX, _fake_clone_ctx(remote_clone, _SHA_V1)),
        patch(_GET_HASH, return_value=_SHA_V1),
    ):
        r = _connect(client, headers, agent_id, subdir="myagent")
    assert r.status_code == 409, f"Expected 409, got {r.text}"
    assert r.json()["detail"]["code"] == "existing_agent_folder"

    # ── Phase 2: adopt_existing=True → 200, no push ───────────────────────────
    # commit_all / fast_forward_push must NOT be called on the adopt path; we
    # patch them to raise so the test fails loudly if they are.
    def _must_not_push(*args, **kwargs):
        raise AssertionError("adopt must not commit/push")

    with (
        patch(_LS_REMOTE, return_value=_SHA_V1),
        patch(_CLONE_CTX, _fake_clone_ctx(remote_clone, _SHA_V1)),
        patch(_GET_HASH, return_value=_SHA_V1),
        patch(_COMMIT_ALL, side_effect=_must_not_push),
        patch(_FF_PUSH, side_effect=_must_not_push),
    ):
        r = _connect(
            client, headers, agent_id, subdir="myagent", adopt_existing=True
        )
    assert r.status_code == 200, f"Adopt connect failed: {r.text}"
    source = r.json()
    assert source["status"] == "connected"
    assert source["subdir"] == "myagent"
    assert source["last_synced_commit"] == _SHA_V1
    assert source["update_available"] is False

    # ── Phase 3: the adopted remote is now the dirty-check baseline ───────────
    r = client.get(_git_dirty_url(agent_id), headers=headers)
    assert r.status_code == 200, f"Dirty check failed after adopt: {r.text}"
    assert r.json()["has_env"] is True


# ── Scenario 7: Commit history + dirty check ──────────────────────────────────


def test_git_commits_and_dirty(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    """
    Commit history + dirty endpoints:
      1. Connect, then GET /git/commits → mocked log returned
      2. GET /git/dirty immediately after connect → dirty=False, has_env=True
      3. Edit a workspace file in the env → workspace_dirty=True
      4. prompts_dirty=True after a prompt edit on a checked-out agent
      5. Non-owner → 404 on both endpoints
    """
    from app.services.knowledge.git_operations import GitOperationError

    headers = superuser_token_headers

    # ── Phase 1+2: Connect a fresh agent (init path), commits + clean dirty ───
    agent_id, env_id = _create_agent_with_env(
        client, headers, tree={"docs": {"workflow.md": "# Workflow\nv1"}}
    )
    with (
        patch(_LS_REMOTE, side_effect=GitOperationError("Ref 'main' not found")),
        patch(_INIT_REPO, return_value=MagicMock()),
        patch(_GET_HASH, return_value=_SHA_V1),
        patch(_COMMIT_ALL, return_value=_SHA_V2),
        patch(_FF_PUSH, return_value=None),
    ):
        r = _connect(client, headers, agent_id, commit_message="Initial export")
    assert r.status_code == 200, f"Connect failed: {r.text}"

    fake_commits = [
        {
            "sha": _SHA_V2,
            "short_sha": _SHA_V2[:7],
            "author_name": "Tester",
            "author_email": "tester@example.com",
            "date": "2024-01-01T00:00:00+00:00",
            "message": "Initial export",
        }
    ]
    with patch(_GIT_LOG, return_value=fake_commits):
        r = client.get(_git_commits_url(agent_id), headers=headers)
    assert r.status_code == 200, f"List commits failed: {r.text}"
    commits = r.json()["commits"]
    assert len(commits) == 1
    assert commits[0]["short_sha"] == _SHA_V2[:7]
    assert commits[0]["message"] == "Initial export"
    # GitHub remote → each commit carries a single-commit browser URL.
    assert (
        commits[0]["commit_url"]
        == f"https://github.com/example/connect/commit/{_SHA_V2}"
    )

    # Dirty immediately after connect — live workspace matches the baseline.
    r = client.get(_git_dirty_url(agent_id), headers=headers)
    assert r.status_code == 200, f"Dirty check failed: {r.text}"
    dirty = r.json()
    assert dirty["has_env"] is True
    assert dirty["dirty"] is False
    assert dirty["workspace_dirty"] is False
    assert dirty["prompts_dirty"] is False
    assert dirty["last_synced_commit"] == _SHA_V2

    # ── Phase 3: Edit a workspace file → workspace_dirty=True ──────────────────
    ws_root = Path(settings.ENV_INSTANCES_DIR) / env_id / "app" / "workspace"
    (ws_root / "docs" / "new_file.md").write_text("locally added")
    r = client.get(_git_dirty_url(agent_id), headers=headers)
    assert r.status_code == 200
    dirty2 = r.json()
    assert dirty2["workspace_dirty"] is True
    assert dirty2["dirty"] is True

    # ── Phase 4: prompts_dirty on a checked-out agent ─────────────────────────
    co_bundle_id = f"io.test.git.dirty.{random_lower_string()[:6]}"
    co_dir = tmp_path / "dirty_checkout"
    _make_agent_repo_tree(co_dir, bundle_id=co_bundle_id, workflow_prompt="Synced")
    co_agent_id = _do_checkout(
        client, headers, clone_dir=co_dir, sha=_SHA_V1,
        repo_url="https://github.com/example/dirty-checkout.git",
    )["agent"]["id"]

    # Clean right after checkout (prompts match the installed revision).
    r = client.get(_git_dirty_url(co_agent_id), headers=headers)
    assert r.status_code == 200
    assert r.json()["prompts_dirty"] is False

    # Diverge a prompt → prompts_dirty flips.
    r = client.put(
        f"{API}/agents/{co_agent_id}",
        headers=headers,
        json={"workflow_prompt": "Locally edited"},
    )
    assert r.status_code == 200
    r = client.get(_git_dirty_url(co_agent_id), headers=headers)
    assert r.status_code == 200
    co_dirty = r.json()
    assert co_dirty["prompts_dirty"] is True
    assert co_dirty["dirty"] is True

    # ── Phase 5: Non-owner → 404 on both endpoints ────────────────────────────
    other = create_random_user(client)
    other_headers = user_authentication_headers(
        client=client, email=other["email"], password=other["_password"]
    )
    promote_to_developer(client, superuser_token_headers, other["id"])
    r = client.get(_git_commits_url(agent_id), headers=other_headers)
    assert r.status_code == 404, f"Expected 404 for non-owner commits, got {r.text}"
    r = client.get(_git_dirty_url(agent_id), headers=other_headers)
    assert r.status_code == 404, f"Expected 404 for non-owner dirty, got {r.text}"


# ── Scenario 7b: Commit-status preview (file/prompt-level diff) ───────────────


def test_git_status_preview(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    """
    GET /git/status — the "git status" commit preview:
      1. Clean right after connect → dirty=False, empty change lists
      2. Add / modify / delete workspace files → file_changes reflect each type
      3. Edit a prompt → prompt_changes lists the changed field as 'modified'
      4. Non-owner → 404
    """
    from app.services.knowledge.git_operations import GitOperationError

    headers = superuser_token_headers

    # ── Phase 1: Connect a fresh agent (init path) with two seeded files ──────
    agent_id, env_id = _create_agent_with_env(
        client,
        headers,
        tree={"docs": {"workflow.md": "# Workflow\nv1", "keep.md": "keep"}},
    )
    with (
        patch(_LS_REMOTE, side_effect=GitOperationError("Ref 'main' not found")),
        patch(_INIT_REPO, return_value=MagicMock()),
        patch(_GET_HASH, return_value=_SHA_V1),
        patch(_COMMIT_ALL, return_value=_SHA_V2),
        patch(_FF_PUSH, return_value=None),
    ):
        r = _connect(client, headers, agent_id, commit_message="Initial export")
    assert r.status_code == 200, f"Connect failed: {r.text}"

    # Clean immediately after connect — capture matches the synced revision.
    r = client.get(_git_status_url(agent_id), headers=headers)
    assert r.status_code == 200, f"Status failed: {r.text}"
    status = r.json()
    assert status["has_env"] is True
    assert status["dirty"] is False
    assert status["prompt_changes"] == []
    assert status["file_changes"] == []
    assert status["last_synced_commit"] == _SHA_V2

    # ── Phase 2: Add + modify + delete workspace files ────────────────────────
    ws_root = Path(settings.ENV_INSTANCES_DIR) / env_id / "app" / "workspace"
    (ws_root / "docs" / "added.md").write_text("brand new")
    (ws_root / "docs" / "workflow.md").write_text("# Workflow\nMODIFIED")
    (ws_root / "docs" / "keep.md").unlink()

    r = client.get(_git_status_url(agent_id), headers=headers)
    assert r.status_code == 200
    status2 = r.json()
    assert status2["dirty"] is True
    changes = {c["path"]: c["change_type"] for c in status2["file_changes"]}
    assert changes.get("docs/added.md") == "added"
    assert changes.get("docs/workflow.md") == "modified"
    assert changes.get("docs/keep.md") == "deleted"

    # ── Phase 3: Edit a prompt → prompt_changes lists it ──────────────────────
    r = client.put(
        f"{API}/agents/{agent_id}",
        headers=headers,
        json={"workflow_prompt": "Locally edited prompt"},
    )
    assert r.status_code == 200
    r = client.get(_git_status_url(agent_id), headers=headers)
    assert r.status_code == 200
    status3 = r.json()
    prompt_fields = {c["field"]: c["change_type"] for c in status3["prompt_changes"]}
    assert "Workflow prompt" in prompt_fields
    # Connect captured an empty workflow prompt, so adding text reads as 'added'.
    assert prompt_fields["Workflow prompt"] in ("added", "modified")

    # ── Phase 4: Non-owner → 404 ──────────────────────────────────────────────
    other = create_random_user(client)
    other_headers = user_authentication_headers(
        client=client, email=other["email"], password=other["_password"]
    )
    promote_to_developer(client, superuser_token_headers, other["id"])
    r = client.get(_git_status_url(agent_id), headers=other_headers)
    assert r.status_code == 404, f"Expected 404 for non-owner status, got {r.text}"


# ── Scenario 8: GET /git is remote-free; check-updates probes remote ──────────


def test_git_source_get_is_remote_free(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    """
    Contract regression tests for the remote-free GET /git endpoint (bug fix):
      1. Checkout an agent (last_synced_commit=SHA_V1)
      2. GET /git with ls_remote configured to return SHA_V2 (remote advanced):
         - response update_available=False (endpoint is remote-free, never calls ls_remote)
         - ls_remote_head was NOT called (assert_not_called proves no network call is made)
      3. GET /git/check-updates with ls_remote returning SHA_V2:
         - response update_available=True (strict remote probe returns the advanced HEAD)
         - ls_remote_head WAS called (proves the two endpoints have distinct contracts)

    This test distinguishes the two endpoints' contracts after the bug fix that made
    GET /git release the DB connection before any git network call and removed its
    best-effort ls_remote probe. Freshness (update_available) is now owned solely by
    GET /git/check-updates.
    """
    headers = superuser_token_headers
    bundle_id = f"io.test.git.remotefree.{random_lower_string()[:8]}"

    # ── Phase 1: Checkout ─────────────────────────────────────────────────────
    clone_dir = tmp_path / "remotefree_repo"
    _make_agent_repo_tree(clone_dir, bundle_id=bundle_id)
    result = _do_checkout(client, headers, clone_dir=clone_dir, sha=_SHA_V1)
    agent_id = result["agent"]["id"]

    # ── Phase 2: GET /git does NOT call ls_remote even when remote advanced ────
    # ls_remote is wired to return SHA_V2 (remote has advanced beyond last_synced).
    # If GET /git were to call ls_remote it would compute update_available=True.
    # After the fix the endpoint never calls ls_remote, so update_available is
    # always False regardless of remote state.
    with patch(_LS_REMOTE, return_value=_SHA_V2) as mock_ls_remote:
        r = client.get(_git_source_url(agent_id), headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["update_available"] is False, (
        "GET /agents/{id}/git is remote-free and must always return "
        "update_available=False regardless of what ls_remote would return"
    )
    mock_ls_remote.assert_not_called()

    # ── Phase 3: check-updates DOES call ls_remote → correct update verdict ────
    # The same scenario (remote at SHA_V2) exercised against check-updates must
    # return update_available=True — the strict endpoint probes the remote and
    # surfaces the real freshness state.
    with patch(_LS_REMOTE, return_value=_SHA_V2) as mock_check_ls:
        r = client.get(_check_updates_url(agent_id), headers=headers)
    assert r.status_code == 200
    upd = r.json()
    assert upd["update_available"] is True, (
        "GET /agents/{id}/git/check-updates must return update_available=True "
        "when the remote HEAD has advanced beyond last_synced_commit"
    )
    assert upd["remote_commit"] == _SHA_V2
    assert upd["last_synced_commit"] == _SHA_V1
    mock_check_ls.assert_called()


# ── Scenario 9: CLI git-coordinates discovery endpoint ────────────────────────


def test_cli_git_coordinates(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    """
    CLI git-coordinates discovery (per-agent CLI token):
      1. VCS-enabled agent → vcs_enabled=True, coordinates correct, auth_hint
      2. The serialized response carries NO key material
      3. Non-VCS agent (its own token) → vcs_enabled=False, fields None
         (scope is enforced by the agent-bound CLI token)
    """
    from app.services.knowledge.git_operations import GitOperationError

    headers = superuser_token_headers

    # ── Agent A: connect a git source over an SSH remote ──────────────────────
    agent_a, _ = _create_agent_with_env(client, headers, tree={"docs": {"a.md": "a"}})
    with (
        patch(_LS_REMOTE, side_effect=GitOperationError("Ref 'main' not found")),
        patch(_INIT_REPO, return_value=MagicMock()),
        patch(_GET_HASH, return_value=_SHA_V1),
        patch(_COMMIT_ALL, return_value=_SHA_V2),
        patch(_FF_PUSH, return_value=None),
    ):
        r = _connect(
            client, headers, agent_a, repo_url="git@github.com:example/a.git"
        )
    assert r.status_code == 200, f"Connect failed: {r.text}"

    setup_a = create_setup_token(client, headers, agent_a)
    cli_a = exchange_setup_token(client, setup_a["token"])["cli_token"]

    r = client.get(f"{API}/cli/git-coordinates", headers=cli_auth_headers(cli_a))
    assert r.status_code == 200, f"git-coordinates failed: {r.text}"
    coord = r.json()
    assert coord["vcs_enabled"] is True
    assert coord["repo_url"] == "git@github.com:example/a.git"
    assert coord["ref"] == "main"
    assert coord["sync_direction"] == "bidirectional"
    assert coord["last_synced_commit"] == _SHA_V2
    assert coord["auth_hint"] == "ssh"
    # No deploy-key / private-key material is ever exposed.
    assert "ssh_key_id" not in coord
    assert "ssh_key" not in coord

    # ── Agent B: no git source — its own token returns vcs_enabled=False ──────
    agent_b, _ = _create_agent_with_env(client, headers, tree=None)
    setup_b = create_setup_token(client, headers, agent_b)
    cli_b = exchange_setup_token(client, setup_b["token"])["cli_token"]

    r = client.get(f"{API}/cli/git-coordinates", headers=cli_auth_headers(cli_b))
    assert r.status_code == 200, f"git-coordinates (no source) failed: {r.text}"
    cb = r.json()
    assert cb["vcs_enabled"] is False
    assert cb["repo_url"] is None
    assert cb["ref"] is None
    assert cb["auth_hint"] is None
