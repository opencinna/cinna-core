"""Integration tests: subdir-scoped update detection in git-backed agent versioning.

These tests cover the bug fix in
``GitSourceService._compute_update_available_remote`` (surfaced by the
``check-updates`` endpoint): a git-versioned agent connected to a repo WITH a
``subdir`` must NOT report ``update_available=True`` when the remote HEAD
advances but the new commits do NOT touch the agent's ``subdir/``. Note the
plain ``GET /git`` read is remote-free and always reports ``False`` — freshness
is owned solely by ``check-updates``.

Before the fix, the service compared remote HEAD SHA against
``last_synced_commit`` — any advance triggered the banner regardless of which
folder changed.  The fix adds a subdir-scoped tree-hash comparison
(``subdir_changed_between``) that only reports an update when commits beyond
the baseline actually touched ``<subdir>/``.

Unit tests for ``subdir_changed_between``'s internal logic (same tree hash,
changed tree hash, conservative fetch-error handling) live in
``tests/unit/test_subdir_changed_between.py``.

Git network isolation strategy (mirrors ``agents_git_source_test.py``):
  All git primitives are patched at their import sites in
  ``app.services.bundles.git_source_service``.  ``subdir_changed_between``
  is also patched there so tests can simulate "subdir unchanged" vs
  "subdir changed" outcomes without a real clone or fetch.
"""

import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.utils import random_lower_string

API = settings.API_V1_STR

# ── Patch targets (module-local import sites in git_source_service.py) ────────

_CLONE_CTX = "app.services.bundles.git_source_service.clone_repository_context"
_GET_HASH = "app.services.bundles.git_source_service.get_current_commit_hash"
_LS_REMOTE = "app.services.bundles.git_source_service.ls_remote_head"
# New in the bug fix: subdir_changed_between is called by
# _compute_update_available_remote only when HEAD advanced AND a subdir +
# baseline are both present.
_SUBDIR_CHANGED = "app.services.bundles.git_source_service.subdir_changed_between"

# ── Test git SHAs ─────────────────────────────────────────────────────────────

_SHA_V1 = "a" * 40  # initial checkout commit (stored as last_synced_commit)
_SHA_V2 = "b" * 40  # simulated remote HEAD advance (an unrelated commit)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_agent_repo_tree(
    base_dir: Path,
    bundle_id: str,
    *,
    subdir: str | None = None,
) -> Path:
    """Build a minimal v2 agent snapshot tree that passes checkout validation.

    Creates ``[base_dir/subdir/]cinna.agent.json`` plus a placeholder
    ``workspace/scripts/run.sh``.  Returns the effective source root.

    Mirrors the same helper in ``agents_git_source_test.py``; duplicated here
    so this file is self-contained (importing from another test file is
    not conventional in this project).
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
            "workflow": "Test workflow prompt",
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


def _check_updates_url(agent_id: str) -> str:
    return f"{API}/agents/{agent_id}/git/check-updates"


def _do_checkout(
    client: TestClient,
    headers: dict,
    *,
    repo_url: str = "https://github.com/example/agent.git",
    clone_dir: Path,
    sha: str = _SHA_V1,
    subdir: str | None = None,
) -> dict:
    """Execute a checkout with mocked git ops. Returns response JSON (200 asserted)."""
    body: dict = {"repo_url": repo_url, "ref": "main", "sync_direction": "bidirectional"}
    if subdir is not None:
        body["subdir"] = subdir

    with (
        patch(_CLONE_CTX, _fake_clone_ctx(clone_dir, sha)),
        patch(_GET_HASH, return_value=sha),
        patch(_LS_REMOTE, return_value=sha),
    ):
        r = client.post(_checkout_url(), headers=headers, json=body)

    assert r.status_code == 200, f"checkout failed: {r.text}"
    return r.json()


# ── Scenario: subdir-scoped update detection ──────────────────────────────────


def test_subdir_scoped_update_detection(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    tmp_path: Path,
) -> None:
    """
    Subdir-scoped update-available detection via both API endpoints:
      1. Checkout an agent in subdir "myagent" with last_synced_commit=SHA_V1
      2. Remote HEAD = SHA_V1 (unchanged) → update_available=False (cheap path,
         subdir_changed_between never called)
      3. Remote HEAD = SHA_V2 (advanced); subdir UNCHANGED →
         update_available=False  ← the regression case the fix targets
      4. Remote HEAD = SHA_V2 (advanced); subdir CHANGED →
         update_available=True
      5. Checkout a root (no-subdir) agent with same SHA_V1 baseline
      6. Root agent, remote advanced to SHA_V2 → update_available=True
         (legacy head-advance verdict, subdir_changed_between NOT called)

    Each update_available assertion is verified on BOTH
      GET /agents/{id}/git          (best-effort, powered by get_source)
      GET /agents/{id}/git/check-updates (strict, powered by check_updates)
    so neither endpoint can regress independently.
    """
    headers = superuser_token_headers

    # ── Phase 1: Checkout a subdir agent ──────────────────────────────────────
    subdir_bundle_id = f"io.test.git.subdir.upd.{random_lower_string()[:6]}"
    clone_dir = tmp_path / "subdir_repo"
    _make_agent_repo_tree(clone_dir, bundle_id=subdir_bundle_id, subdir="myagent")

    result = _do_checkout(
        client,
        headers,
        clone_dir=clone_dir,
        sha=_SHA_V1,
        subdir="myagent",
        repo_url="https://github.com/example/subdir-agent.git",
    )
    agent_id = result["agent"]["id"]
    assert result["git_source"]["subdir"] == "myagent"
    assert result["git_source"]["last_synced_commit"] == _SHA_V1

    # ── Phase 2: Same SHA → update_available=False (no subdir check) ──────────
    # The cheap ls-remote path short-circuits immediately when
    # remote HEAD == last_synced_commit; subdir_changed_between is never called.
    with patch(_LS_REMOTE, return_value=_SHA_V1):
        r = client.get(_git_source_url(agent_id), headers=headers)
    assert r.status_code == 200
    assert r.json()["update_available"] is False

    with patch(_LS_REMOTE, return_value=_SHA_V1):
        r = client.get(_check_updates_url(agent_id), headers=headers)
    assert r.status_code == 200
    phase2 = r.json()
    assert phase2["update_available"] is False
    assert phase2["remote_commit"] == _SHA_V1
    assert phase2["last_synced_commit"] == _SHA_V1

    # ── Phase 3: Remote advanced; subdir UNCHANGED → update_available=False ───
    # GET /git is remote-free, so it always reports False here. The subdir-scoped
    # regression (HEAD advanced but the subdir tree is unchanged → no update) is
    # exercised against check-updates below: before the fix any HEAD advance →
    # True; after the fix subdir_changed_between returns False → update stays
    # False.
    with (
        patch(_LS_REMOTE, return_value=_SHA_V2),
        patch(_SUBDIR_CHANGED, return_value=False),
    ):
        r = client.get(_git_source_url(agent_id), headers=headers)
    assert r.status_code == 200
    assert r.json()["update_available"] is False, (
        "GET /agents/{id}/git is remote-free — always update_available=False"
    )

    with (
        patch(_LS_REMOTE, return_value=_SHA_V2),
        patch(_SUBDIR_CHANGED, return_value=False),
    ):
        r = client.get(_check_updates_url(agent_id), headers=headers)
    assert r.status_code == 200
    phase3 = r.json()
    assert phase3["update_available"] is False, (
        "Regression: a commit that does not touch the agent's subdir must NOT "
        "set update_available=True on GET /agents/{id}/git/check-updates"
    )
    # remote_commit still reflects the actual remote HEAD, even when the
    # subdir is unchanged — so the caller can track the remote ref accurately.
    assert phase3["remote_commit"] == _SHA_V2
    assert phase3["last_synced_commit"] == _SHA_V1

    # ── Phase 4: Remote advanced; subdir CHANGED → update_available=True ──────
    # subdir_changed_between returns True → the banner should show. This is owned
    # solely by check-updates: GET /git is remote-free (it never probes the
    # remote and always reports update_available=False), so the subdir-changed
    # verdict surfaces only on the explicit check-updates endpoint.
    with (
        patch(_LS_REMOTE, return_value=_SHA_V2),
        patch(_SUBDIR_CHANGED, return_value=True),
    ):
        r = client.get(_git_source_url(agent_id), headers=headers)
    assert r.status_code == 200
    assert r.json()["update_available"] is False, (
        "GET /agents/{id}/git is remote-free — it must report "
        "update_available=False regardless of remote state; freshness is owned "
        "by check-updates."
    )

    with (
        patch(_LS_REMOTE, return_value=_SHA_V2),
        patch(_SUBDIR_CHANGED, return_value=True),
    ):
        r = client.get(_check_updates_url(agent_id), headers=headers)
    assert r.status_code == 200
    phase4 = r.json()
    assert phase4["update_available"] is True
    assert phase4["remote_commit"] == _SHA_V2

    # ── Phase 5: Checkout a root (no-subdir) agent ────────────────────────────
    root_bundle_id = f"io.test.git.root.upd.{random_lower_string()[:6]}"
    root_dir = tmp_path / "root_repo"
    _make_agent_repo_tree(root_dir, bundle_id=root_bundle_id)

    root_result = _do_checkout(
        client,
        headers,
        clone_dir=root_dir,
        sha=_SHA_V1,
        repo_url="https://github.com/example/root-agent.git",
    )
    root_agent_id = root_result["agent"]["id"]
    assert root_result["git_source"]["subdir"] is None

    # ── Phase 6: Root agent; remote advanced → update_available=True ──────────
    # With no subdir, the service uses the cheap HEAD-advance verdict directly
    # (every commit touches the repo root).  subdir_changed_between is NOT
    # patched here — if it were called unexpectedly it would try to clone the
    # real remote and fail, surfacing the regression immediately.
    with patch(_LS_REMOTE, return_value=_SHA_V2):
        r = client.get(_check_updates_url(root_agent_id), headers=headers)
    assert r.status_code == 200
    phase6 = r.json()
    assert phase6["update_available"] is True, (
        "Root (no-subdir) agents must still report update_available=True on "
        "any HEAD advance — legacy behavior must be unchanged"
    )
    assert phase6["remote_commit"] == _SHA_V2
