"""Git-backed agents on tree v3 (the kit agent folder as the git wire format).

Tree v3 puts ``cinna-agent.json`` at the repo root with the content beside it,
instead of v2's ``cinna.agent.json`` + ``workspace/``. Readers accept both
layouts always; the exporter writes v3 only when ``GIT_EXPORT_TREE_V3`` is on
or the remote already holds a v3 tree (a v3 remote is never downgraded).

Same stubbing pattern as ``agents_git_source_test.py``: ``clone_repository_context``
yields a real directory, the commit / push primitives are patched, and a
``commit_all`` side effect snapshots what the exporter wrote into the clone.
Unit tests for the reader/writer live in ``tests/unit/test_revision_format.py``.
"""
import json
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.api.agents.git.agents_git_source_test import (
    _CLONE_CTX,
    _COMMIT_ALL,
    _FF_PUSH,
    _INIT_REPO,
    _connect,
    _create_agent_with_env,
    _ensure_default_ai_credential,
    _git_dirty_url,
    _git_status_url,
    _GET_HASH,
    _LS_REMOTE,
    _SHA_V1,
    _SHA_V2,
    _do_checkout,
    _fake_clone_ctx,
    _git_pull_url,
    _git_push_url,
    _git_source_url,
    _make_agent_repo_tree,
    _seed_env_workspace,
    _write_tree,
)
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR
_KIT_ID = "11111111-2222-4333-8444-555555555555"


def _make_v3_tree(
    root: Path,
    bundle_id: str,
    *,
    workflow_prompt: str = "V3 workflow prompt",
    entrypoint_prompt: str | None = "V3 entrypoint prompt",
    description: str = "A shared v3 agent",
    with_bundle: bool = True,
    extra_kit: dict | None = None,
) -> Path:
    """A minimal kit agent folder (tree v3) that passes checkout validation."""
    kit: dict = {
        "contract_version": "1.8.0",
        "id": _KIT_ID,
        "name": "Shared v3 agent",
        "slug": "shared-v3-agent",
        "description": description,
        "example_prompts": ["Run the report"],
        "prompts": {
            "workflow": "docs/WORKFLOW_PROMPT.md",
            "entrypoint": "docs/ENTRYPOINT_PROMPT.md",
        },
        "runtime": {"engine": "claude"},
    }
    if with_bundle:
        kit["bundle"] = {
            "schema_version": 3,
            "bundle_id": bundle_id,
            "revision_number": 1,
            "version": "1.0",
            "published_at": "2024-01-01T00:00:00+00:00",
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
    kit.update(extra_kit or {})
    tree: dict = {
        "scripts": {"run.sh": "#!/bin/bash\necho hello"},
        "docs": {"WORKFLOW_PROMPT.md": workflow_prompt},
        # Paths the reader must not import
        "AGENTS.md": "teammate wrapper",
        "README.md": "teammate readme",
    }
    if entrypoint_prompt is not None:
        tree["docs"]["ENTRYPOINT_PROMPT.md"] = entrypoint_prompt
    _write_tree(root, tree)
    (root / "cinna-agent.json").write_text(json.dumps(kit))
    return root


def _list_tree(root: Path) -> set[str]:
    return {
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if ".git" not in p.relative_to(root).parts
    }


def _push(
    client: TestClient,
    headers: dict,
    agent_id: str,
    clone_dir: Path,
    *,
    flag: bool,
    remote_sha: str,
) -> tuple[dict, set[str]]:
    """Push with git mocked; return (response json, files the exporter wrote)."""
    captured: dict[str, set[str]] = {}

    def _capture(*_a, **_k):
        captured["files"] = _list_tree(clone_dir)
        return _SHA_V2

    with (
        patch.object(settings, "GIT_EXPORT_TREE_V3", flag),
        patch(_LS_REMOTE, return_value=remote_sha),
        patch(_CLONE_CTX, _fake_clone_ctx(clone_dir, remote_sha)),
        patch(_GET_HASH, return_value=remote_sha),
        patch(_COMMIT_ALL, side_effect=_capture),
        patch(_FF_PUSH, return_value=None),
    ):
        r = client.post(
            _git_push_url(agent_id), headers=headers, json={"commit_message": "export"}
        )
    assert r.status_code == 200, f"push failed: {r.text}"
    return r.json(), captured["files"]


def _seed_workspace(client: TestClient, headers: dict, agent_id: str) -> None:
    agent = client.get(f"{API}/agents/{agent_id}", headers=headers).json()
    _seed_env_workspace(
        str(agent["active_environment_id"]),
        {"docs": {"workflow.md": "# Workflow"}, "scripts": {"run.sh": "echo hi"}},
    )


def test_v3_checkout_pull_and_push_layouts(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    """
    Tree v3 through the git source lifecycle:
      1. Checkout of a v3 tree creates the agent from the kit manifest + prompt files
      2. GET git source reflects the checkout
      3. Pull of a v3 tree with edited prompt files updates the agent
      4. Push with the flag OFF on a v3 remote stays v3 (never downgraded)
      5. A second checkout-then-push of a v2 remote with the flag OFF writes v2
      6. Push of a v2 remote with the flag ON writes v3
    """
    headers = superuser_token_headers
    bundle_id = f"io.test.git.v3.{random_lower_string()[:6]}"

    # ── Phase 1: checkout of a v3 tree ───────────────────────────────────────
    v1_dir = tmp_path / "v3_repo_v1"
    _make_v3_tree(v1_dir, bundle_id)
    result = _do_checkout(
        client, headers, clone_dir=v1_dir, sha=_SHA_V1,
        repo_url="https://github.com/example/v3agent.git",
    )
    agent_id = result["agent"]["id"]
    assert result["git_source"]["status"] == "connected"
    assert result["git_source"]["last_synced_commit"] == _SHA_V1

    agent = client.get(f"{API}/agents/{agent_id}", headers=headers).json()
    assert agent["workflow_prompt"] == "V3 workflow prompt"
    assert agent["entrypoint_prompt"] == "V3 entrypoint prompt"
    assert agent["description"] == "A shared v3 agent"
    # The clone itself is never mutated by the reader
    assert (v1_dir / "AGENTS.md").read_text() == "teammate wrapper"
    assert not (v1_dir / "workspace").exists()

    # ── Phase 2: source row ──────────────────────────────────────────────────
    with patch(_LS_REMOTE, return_value=_SHA_V1):
        src = client.get(_git_source_url(agent_id), headers=headers).json()
    assert src["update_available"] is False

    # ── Phase 3: pull of an edited v3 tree ───────────────────────────────────
    v2_dir = tmp_path / "v3_repo_v2"
    _make_v3_tree(
        v2_dir, bundle_id, workflow_prompt="V3 workflow EDITED",
        entrypoint_prompt="V3 entrypoint EDITED",
    )
    with (
        patch(_LS_REMOTE, return_value=_SHA_V2),
        patch(_CLONE_CTX, _fake_clone_ctx(v2_dir, _SHA_V2)),
        patch(_GET_HASH, return_value=_SHA_V2),
    ):
        r = client.post(_git_pull_url(agent_id), headers=headers)
    assert r.status_code == 200, r.text
    agent = client.get(f"{API}/agents/{agent_id}", headers=headers).json()
    assert agent["workflow_prompt"] == "V3 workflow EDITED"
    assert agent["entrypoint_prompt"] == "V3 entrypoint EDITED"
    with patch(_LS_REMOTE, return_value=_SHA_V2):
        src = client.get(_git_source_url(agent_id), headers=headers).json()
    assert src["last_synced_commit"] == _SHA_V2

    # ── Phase 4: flag OFF, remote already v3 → stays v3 ──────────────────────
    _seed_workspace(client, headers, agent_id)
    push_dir = tmp_path / "v3_push_dir"
    _make_v3_tree(push_dir, bundle_id, workflow_prompt="V3 workflow EDITED")
    body, files = _push(
        client, headers, agent_id, push_dir, flag=False, remote_sha=_SHA_V2
    )
    assert body["last_synced_commit"] == _SHA_V2
    assert "cinna-agent.json" in files
    assert "cinna.agent.json" not in files
    assert "workspace" not in files
    assert "docs/WORKFLOW_PROMPT.md" in files
    # A teammate's excluded files in the remote survive our export
    assert (push_dir / "AGENTS.md").read_text() == "teammate wrapper"
    kit = json.loads((push_dir / "cinna-agent.json").read_text())
    assert kit["id"] == _KIT_ID
    assert kit["bundle"]["bundle_id"] == bundle_id
    assert kit["contract_version"] == "1.8.0"

    # ── Phase 5: v2 remote, flag OFF → v2 layout ─────────────────────────────
    v2_bundle = f"io.test.git.v2.{random_lower_string()[:6]}"
    v2_clone = tmp_path / "v2_clone"
    _make_agent_repo_tree(v2_clone, bundle_id=v2_bundle)
    v2_result = _do_checkout(
        client, headers, clone_dir=v2_clone, sha=_SHA_V1,
        repo_url="https://github.com/example/v2agent.git",
    )
    v2_agent_id = v2_result["agent"]["id"]
    _seed_workspace(client, headers, v2_agent_id)
    _, files = _push(
        client, headers, v2_agent_id, v2_clone, flag=False, remote_sha=_SHA_V1
    )
    assert "cinna.agent.json" in files
    assert "workspace" in files
    assert "cinna-agent.json" not in files

    # ── Phase 6: v2 remote, flag ON → exporter switches the tree to v3 ───────
    v2b_bundle = f"io.test.git.v2b.{random_lower_string()[:6]}"
    v2b_clone = tmp_path / "v2b_clone"
    _make_agent_repo_tree(v2b_clone, bundle_id=v2b_bundle)
    v2b_result = _do_checkout(
        client, headers, clone_dir=v2b_clone, sha=_SHA_V1,
        repo_url="https://github.com/example/v2b.git",
    )
    v2b_agent_id = v2b_result["agent"]["id"]
    _seed_workspace(client, headers, v2b_agent_id)
    _, files = _push(
        client, headers, v2b_agent_id, v2b_clone, flag=True, remote_sha=_SHA_V1
    )
    assert "cinna-agent.json" in files
    assert "docs/WORKFLOW_PROMPT.md" in files
    assert "scripts/run.sh" in files
    kit = json.loads((v2b_clone / "cinna-agent.json").read_text())
    assert kit["bundle"]["bundle_id"] == v2b_bundle
    assert kit["id"]  # minted once


def test_v3_checkout_error_cases(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    """
    Checkout refusals specific to tree v3:
      1. A desktop-made tree without a ``bundle`` block has no bundle_id → refused
      2. A directory with neither layout is refused as not a Cinna agent repo
    """
    headers = superuser_token_headers

    # ── Phase 1: v3 tree without bundle block ────────────────────────────────
    desk = tmp_path / "desktop_made"
    _make_v3_tree(desk, "ignored", with_bundle=False)
    with (
        patch(_CLONE_CTX, _fake_clone_ctx(desk, _SHA_V1)),
        patch(_GET_HASH, return_value=_SHA_V1),
        patch(_LS_REMOTE, return_value=_SHA_V1),
    ):
        r = client.post(
            f"{API}/agents/checkout",
            headers=headers,
            json={"repo_url": "https://github.com/example/desk.git", "ref": "main"},
        )
    assert r.status_code in (400, 422), r.text
    assert "bundle_id" in json.dumps(r.json())

    # ── Phase 2: neither layout ──────────────────────────────────────────────
    nothing = tmp_path / "nothing"
    _write_tree(nothing, {"random.txt": "x"})
    with (
        patch(_CLONE_CTX, _fake_clone_ctx(nothing, _SHA_V1)),
        patch(_GET_HASH, return_value=_SHA_V1),
        patch(_LS_REMOTE, return_value=_SHA_V1),
    ):
        r = client.post(
            f"{API}/agents/checkout",
            headers=headers,
            json={"repo_url": "https://github.com/example/nothing.git", "ref": "main"},
        )
    assert r.status_code in (400, 422), r.text


def _v3_connect_then_assert_clean(
    client: TestClient,
    headers: dict,
    *,
    tree: dict,
    example_prompts: list[str] | None = None,
) -> None:
    """Connect an agent with the v3 flag on (new sha -> revision saved) and
    assert dirty / status report nothing changed on any axis."""
    from unittest.mock import MagicMock

    from app.services.knowledge.git_operations import GitOperationError

    _ensure_default_ai_credential(client, headers)
    agent_id, _env_id = _create_agent_with_env(client, headers, tree=tree)
    if example_prompts is not None:
        r = client.put(
            f"{API}/agents/{agent_id}", headers=headers,
            json={"example_prompts": example_prompts},
        )
        assert r.status_code == 200, r.text

    with (
        patch.object(settings, "GIT_EXPORT_TREE_V3", True),
        patch(_LS_REMOTE, side_effect=GitOperationError("Ref 'main' not found")),
        patch(_INIT_REPO, return_value=MagicMock()),
        patch(_GET_HASH, return_value=_SHA_V1),
        patch(_COMMIT_ALL, return_value=_SHA_V2),
        patch(_FF_PUSH, return_value=None),
    ):
        r = _connect(client, headers, agent_id, commit_message="v3 export")
    assert r.status_code == 200, r.text

    dirty = client.get(_git_dirty_url(agent_id), headers=headers).json()
    assert dirty["has_env"] is True
    assert dirty["last_synced_commit"] == _SHA_V2
    for axis in ("dirty", "workspace_dirty", "prompts_dirty", "settings_dirty"):
        if axis in dirty:
            assert dirty[axis] is False, f"{axis}: {dirty}"

    status = client.get(_git_status_url(agent_id), headers=headers).json()
    assert status["dirty"] is False, status
    assert status["file_changes"] == [], status
    assert status["prompt_changes"] == [], status
    for key in ("setting_changes", "settings_changes"):
        if key in status:
            assert status[key] == [], status


def test_v3_export_leaves_no_spurious_dirty_for_cloud_made_agent(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """After a v3 export the live agent matches the saved baseline: nothing is
    reported dirty and /git/status lists no file, setting or prompt change."""
    _v3_connect_then_assert_clean(
        client,
        superuser_token_headers,
        tree={"docs": {"workflow.md": "# Workflow\nv1"}, "scripts": {"run.sh": "echo hi"}},
    )


def test_v3_export_leaves_no_spurious_dirty_when_workspace_holds_excluded_paths(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Workspace files the v3 reader never imports (kit manifest, README,
    CLAUDE.md, temp/, nested .gitignore) must not read as changes after export."""
    _v3_connect_then_assert_clean(
        client,
        superuser_token_headers,
        tree={
            "docs": {"workflow.md": "# Workflow\nv1"},
            "scripts": {"run.sh": "echo hi", ".gitignore": "*.log\n"},
            "cinna-agent.json": json.dumps(
                {"contract_version": "1.8.0", "id": _KIT_ID, "name": "X", "description": "d"}
            ),
            "README.md": "readme",
            "CLAUDE.md": "claude",
            "temp": {"scratch.txt": "tmp"},
        },
    )


def test_v3_export_with_empty_or_many_example_prompts_is_not_settings_dirty(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """The v3 manifest drops empty example prompts and caps them at 20; the live
    agent must still compare equal to the saved baseline afterwards."""
    _v3_connect_then_assert_clean(
        client,
        superuser_token_headers,
        tree={"docs": {"workflow.md": "# Workflow"}},
        example_prompts=["", "keep me"],
    )
    _v3_connect_then_assert_clean(
        client,
        superuser_token_headers,
        tree={"docs": {"workflow.md": "# Workflow"}},
        example_prompts=[f"prompt {i}" for i in range(25)],
    )
