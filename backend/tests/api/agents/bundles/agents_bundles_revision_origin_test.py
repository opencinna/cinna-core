"""Tests for the AgentBundleRevision.origin discriminator (FIX 2) and
publish off-loop snapshot (FIX 1).

FIX 1 (asyncio.to_thread):
  PublishService moved the workspace filesystem snapshot off the event loop via
  ``asyncio.to_thread``. The ``patch_asyncio_to_thread`` autouse fixture in the
  agents conftest already runs ``asyncio.to_thread`` synchronously (to keep the
  operation on the test transaction), so test (d) exercises the BEHAVIORAL
  invariants: publish succeeds, the revision appears in the listing, and a
  consumer can install it.

FIX 2 (revision origin discriminator):
  ``AgentBundleRevision`` gained an ``origin`` column (module constants
  ``REVISION_ORIGIN_PUBLISH = "publish"`` / ``REVISION_ORIGIN_GIT = "git"``).
  Catalog publishes write ``origin="publish"``; git operations (checkout / pull
  / push / connect) write ``origin="git"`` via ``GitSourceService._persist_revision``.
  Two service filters rely on this:

  * ``BundleService.list_revisions_with_install_counts`` (backing
    ``GET /bundles/{uuid}/revisions``) — filters to ``origin="publish"`` only.
  * ``BundleService.delete_revision``'s replacement-search — also filters to
    ``origin="publish"`` so a git baseline can never become ``latest_revision_id``.

  ``revision_number`` is a GLOBAL monotonic counter shared across publish and git
  revisions (enforced by the ``uq_revision_bundle_number`` unique constraint).
  Publish-only numbering therefore shows gaps when git revisions are interleaved —
  that is expected and asserted in test (c).

Git network isolation strategy (mirrors agents_git_source_test.py):
  All git network primitives are patched at their import sites inside
  ``app.services.bundles.git_source_service``. The tests use a git-connect init
  path (ls_remote raises GitOperationError → init_repo_with_remote) to add a
  git revision to an already-published bundle without any real network calls.

Coverage:
  test_git_revisions_filtered_from_listing_and_version_suggestion — (a) + (b)
  test_revision_number_global_monotonic_across_origins              — (c)
  test_publish_sanity_after_to_thread_refactor                      — (d)
  test_delete_latest_publish_repoints_to_previous_publish_not_git   — (e)

Unit tests for RevisionFormat live in tests/unit/test_revision_format.py.
"""
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import (
    make_bundle_public,
    make_user_and_headers,
    publish_bundle_revision,
)

API = settings.API_V1_STR

# ── Git op patch targets (module-local import sites in git_source_service.py) ─

_LS_REMOTE = "app.services.bundles.git_source_service.ls_remote_head"
_INIT_REPO = "app.services.bundles.git_source_service.init_repo_with_remote"
_GET_HASH = "app.services.bundles.git_source_service.get_current_commit_hash"
_COMMIT_ALL = "app.services.bundles.git_source_service.commit_all"
_FF_PUSH = "app.services.bundles.git_source_service.fast_forward_push"

_SHA_BEFORE = "a" * 40  # "head" on the freshly-inited repo (no prior commits)
_SHA_AFTER = "b" * 40   # new SHA after commit_all


# ── Private helpers ───────────────────────────────────────────────────────────


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


def _seed_env_workspace(env_id: str, tree: dict) -> None:
    """Write files into ENV_INSTANCES_DIR/<env_id>/app/workspace/."""
    ws_root = Path(settings.ENV_INSTANCES_DIR) / env_id / "app" / "workspace"
    _write_tree(ws_root, tree)


def _create_agent_with_workspace(
    client: TestClient,
    headers: dict,
    *,
    tree: dict | None = None,
) -> tuple[str, str]:
    """Create a normal agent, drain tasks, optionally seed workspace.

    Returns ``(agent_id, env_id)``.
    """
    agent = create_agent_via_api(client, headers)
    drain_tasks()
    data = get_agent(client, headers, agent["id"])
    env_id = data.get("active_environment_id")
    assert env_id, "created agent must have an active environment"
    if tree is not None:
        _seed_env_workspace(str(env_id), tree)
    return agent["id"], str(env_id)


def _git_connect_init_path(
    client: TestClient,
    headers: dict,
    agent_id: str,
    *,
    repo_url: str = "https://github.com/example/agent-origin-test.git",
) -> dict:
    """Connect an agent to a fresh empty remote (init path) with mocked git ops.

    Uses the init path — ``ls_remote`` raises ``GitOperationError`` (empty
    remote / absent branch) → ``init_repo_with_remote`` is called. All network
    primitives are mocked so no real git traffic is issued.

    Returns the response JSON (200 asserted).
    """
    from app.services.knowledge.git_operations import GitOperationError

    with (
        patch(_LS_REMOTE, side_effect=GitOperationError("Ref 'main' not found")),
        patch(_INIT_REPO, return_value=MagicMock()),
        patch(_GET_HASH, return_value=_SHA_BEFORE),
        patch(_COMMIT_ALL, return_value=_SHA_AFTER),
        patch(_FF_PUSH, return_value=None),
    ):
        r = client.post(
            f"{API}/agents/{agent_id}/git/connect",
            headers=headers,
            json={
                "repo_url": repo_url,
                "ref": "main",
                "sync_direction": "bidirectional",
                "commit_message": "Initial export",
            },
        )
    assert r.status_code == 200, f"git connect failed: {r.text}"
    return r.json()


def _list_bundle_revisions(
    client: TestClient,
    headers: dict,
    bundle_uuid: str,
) -> list[dict]:
    """GET /bundles/{uuid}/revisions → list of revision dicts (publish-only)."""
    r = client.get(f"{API}/bundles/{bundle_uuid}/revisions", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _get_bundle_uuid(client: TestClient, headers: dict, agent_id: str) -> str:
    """Refresh the agent row and return its bundle_uuid (assert non-null)."""
    fresh = client.get(f"{API}/agents/{agent_id}", headers=headers).json()
    bundle_uuid = fresh.get("bundle_uuid")
    assert bundle_uuid, "agent must have a bundle_uuid after publish"
    return bundle_uuid


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_git_revisions_filtered_from_listing_and_version_suggestion(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Tests (a) and (b): git revisions are hidden from GET /bundles/{uuid}/revisions;
    data[0] (newest-first) is always the latest catalog publish, never a git baseline.

    Scenario:
      1. Publish rev 1 → origin=publish, appears in listing
      2. Git connect → hidden rev 2 (origin=git, higher revision_number)
      3. GET /bundles/{uuid}/revisions → exactly 1 entry (rev 1), no git revision
      4. Publish rev 3 → origin=publish
      5. GET /bundles/{uuid}/revisions → exactly 2 entries (rev 1, rev 3)
         — the git revision (rev 2) is absent from the listing
      6. data[0] is rev 3 (newest publish), not the git revision (rev 2)
    """
    headers = superuser_token_headers

    # ── Phase 1: Create agent + first catalog publish ─────────────────────────
    agent_id, _ = _create_agent_with_workspace(
        client, headers, tree={"docs": {"readme.md": "# Agent v1"}}
    )
    rev1 = publish_bundle_revision(client, headers, agent_id, notes="initial")
    assert rev1["revision_number"] >= 1
    first_pub_num = rev1["revision_number"]

    bundle_uuid = _get_bundle_uuid(client, headers, agent_id)

    # ── Phase 2: Git connect → inserts a hidden git revision ─────────────────
    git_src = _git_connect_init_path(client, headers, agent_id)
    assert git_src["status"] == "connected"

    # ── Phase 3: Listing contains only the publish revision ───────────────────
    revisions_after_git = _list_bundle_revisions(client, headers, bundle_uuid)

    assert len(revisions_after_git) == 1, (
        f"Expected 1 publish revision after git connect, "
        f"got {len(revisions_after_git)}: "
        f"{[r['revision_number'] for r in revisions_after_git]}"
    )
    assert revisions_after_git[0]["revision_number"] == first_pub_num, (
        f"The only listed revision should be the publish rev {first_pub_num}"
    )

    # ── Phase 4: Second catalog publish ───────────────────────────────────────
    r = client.post(
        f"{API}/agents/{agent_id}/publish",
        headers=headers,
        json={"release_notes": "second publish"},
    )
    assert r.status_code == 200, r.text
    rev3 = r.json()
    drain_tasks()
    second_pub_num = rev3["revision_number"]

    # The git connect consumed one revision_number slot; the second publish
    # must have a higher number than first_pub_num + 1.
    assert second_pub_num > first_pub_num + 1, (
        f"Expected second publish revision_number > {first_pub_num + 1} "
        f"(git connect took slot {first_pub_num + 1}), got {second_pub_num}"
    )

    # ── Phase 5: Listing shows exactly the two publish revisions ──────────────
    revisions_final = _list_bundle_revisions(client, headers, bundle_uuid)
    nums_final = [r["revision_number"] for r in revisions_final]

    assert len(revisions_final) == 2, (
        f"Expected 2 publish revisions after second publish, "
        f"got {len(revisions_final)}: {nums_final}"
    )

    # The git revision_number (first_pub_num + 1) must NOT appear.
    git_rev_num = first_pub_num + 1
    assert git_rev_num not in nums_final, (
        f"Git revision_number {git_rev_num} must not appear in the listing: {nums_final}"
    )

    # Both publish revision numbers must be present.
    assert first_pub_num in nums_final
    assert second_pub_num in nums_final

    # ── Phase 6: (b) data[0] is the most recent publish, not the git rev ──────
    # Listing is ordered newest-first (revision_number DESC).
    assert revisions_final[0]["revision_number"] == second_pub_num, (
        f"data[0] should be the newest publish revision ({second_pub_num}), "
        f"got {revisions_final[0]['revision_number']}"
    )
    assert revisions_final[1]["revision_number"] == first_pub_num, (
        f"data[1] should be the first publish revision ({first_pub_num}), "
        f"got {revisions_final[1]['revision_number']}"
    )


def test_revision_number_global_monotonic_across_origins(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Test (c): revision_number is globally unique and strictly increasing across
    publish and git origins.

    Scenario:
      1. First publish → revision_number = N (e.g. 1)
      2. Git connect → takes revision_number = N+1 (consumed from the shared counter)
      3. Second publish → revision_number = N+2 (evidence: git took one slot)

    Assertions:
      - All revision numbers are unique.
      - Publish revision numbers are strictly increasing in creation order.
      - The second publish revision_number equals first + 2 (proving the global
        counter is shared: publish_1 < git < publish_2 with no contiguity guarantee).
      - GET /bundles/{uuid}/revisions lists both publish revisions, newest-first,
        with IDs that are distinct.
    """
    headers = superuser_token_headers

    # ── Phase 1: Create agent + first publish ─────────────────────────────────
    agent_id, _ = _create_agent_with_workspace(
        client, headers, tree={"scripts": {"run.sh": "#!/bin/bash\necho hello"}}
    )
    rev1 = publish_bundle_revision(client, headers, agent_id)
    first_num = rev1["revision_number"]
    rev1_id = rev1["id"]

    bundle_uuid = _get_bundle_uuid(client, headers, agent_id)

    # ── Phase 2: Git connect — consumes one revision_number slot ──────────────
    _git_connect_init_path(client, headers, agent_id)

    # ── Phase 3: Second publish ───────────────────────────────────────────────
    rev3 = publish_bundle_revision(client, headers, agent_id, notes="v2")
    second_num = rev3["revision_number"]
    rev3_id = rev3["id"]

    # ── Phase 4: Revision numbers are unique and strictly increasing ──────────
    assert first_num != second_num, "Publish revision numbers must be unique"
    assert first_num < second_num, (
        f"Publish revision numbers must be increasing: {first_num} < {second_num}"
    )

    # ── Phase 5: Gap confirms git occupies the intermediate slot ──────────────
    # Without the gap the second number would equal first_num + 1. With a git
    # revision between them, it equals first_num + 2.
    assert second_num == first_num + 2, (
        f"Expected second publish at {first_num + 2} "
        f"(git took slot {first_num + 1}), got {second_num}"
    )

    # ── Phase 6: Listing shows both publish revisions, newest-first ───────────
    revisions = _list_bundle_revisions(client, headers, bundle_uuid)
    nums = [r["revision_number"] for r in revisions]
    ids = [r["id"] for r in revisions]

    assert len(revisions) == 2, (
        f"Expected 2 publish revisions in listing, got {len(revisions)}: {nums}"
    )
    assert nums == [second_num, first_num], (
        f"Expected newest-first [{second_num}, {first_num}], got {nums}"
    )
    # IDs are distinct (uniqueness constraint on the DB rows).
    assert len(ids) == len(set(ids)), "Revision IDs in listing must be unique"
    assert rev1_id in ids and rev3_id in ids


def test_publish_sanity_after_to_thread_refactor(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Test (d): normal publish produces a complete, installable revision after
    the asyncio.to_thread refactor (FIX 1).

    The ``patch_asyncio_to_thread`` autouse fixture in the agents conftest.py
    already runs ``asyncio.to_thread`` synchronously so it stays on the test
    transaction — the refactor is exercised transparently. This test verifies
    the observable behavior:

    Scenario:
      1. Create agent + publish with explicit version + release notes
      2. Publish response carries correct revision fields (number, hash, notes)
      3. Agent row is updated: bundle_uuid, installed_revision_id
      4. GET /bundles/{uuid}/revisions lists the revision with matching fields
      5. Flip bundle to public/listed
      6. Foreign consumer installs from catalog
      7. Consumer's installed_revision_id links to the correct revision
    """
    headers = superuser_token_headers

    # ── Phase 1: Create agent + publish ──────────────────────────────────────
    agent_id, _ = _create_agent_with_workspace(
        client, headers,
        tree={"docs": {"workflow.md": "# Workflow\nDoes something useful"}}
    )

    r = client.post(
        f"{API}/agents/{agent_id}/publish",
        headers=headers,
        json={"version": "1.0", "release_notes": "First release (to_thread guard)"},
    )
    assert r.status_code == 200, f"Publish failed: {r.text}"
    revision = r.json()
    drain_tasks()

    # ── Phase 2: Publish response has correct fields ──────────────────────────
    assert revision["revision_number"] >= 1
    assert revision["content_hash"], "Revision must carry a content_hash"
    assert revision["release_notes"] == "First release (to_thread guard)"
    rev_id = revision["id"]

    # ── Phase 3: Agent row reflects the publish ────────────────────────────────
    fresh_agent = client.get(f"{API}/agents/{agent_id}", headers=headers).json()
    bundle_uuid = fresh_agent["bundle_uuid"]
    bundle_id = fresh_agent["bundle_id"]
    assert bundle_uuid is not None, "Agent must have bundle_uuid after publish"
    assert fresh_agent["installed_revision_id"] == rev_id
    assert fresh_agent["installed_revision_number"] == revision["revision_number"]

    # ── Phase 4: Listing shows the revision ───────────────────────────────────
    revisions = _list_bundle_revisions(client, headers, bundle_uuid)
    assert len(revisions) == 1
    listed = revisions[0]
    assert listed["id"] == rev_id
    assert listed["revision_number"] == revision["revision_number"]
    assert listed["content_hash"] == revision["content_hash"]
    assert listed["release_notes"] == "First release (to_thread guard)"

    # ── Phase 5: Flip to public/listed ────────────────────────────────────────
    make_bundle_public(client, headers, bundle_uuid)

    # ── Phase 6: Foreign consumer installs ────────────────────────────────────
    _, consumer_headers = make_user_and_headers(client)
    install_r = client.post(
        f"{API}/catalog/{bundle_id}/install",
        headers=consumer_headers,
        json={},
    )
    assert install_r.status_code == 200, f"Install failed: {install_r.text}"
    consumer_install = install_r.json()
    drain_tasks()

    # ── Phase 7: Consumer's install links to the correct revision ────────────
    assert consumer_install["installed_revision_id"] == rev_id, (
        "Consumer install must link to the published revision"
    )
    assert consumer_install["installed_revision_number"] == revision["revision_number"]
    assert consumer_install["is_publisher_install"] is False


def test_delete_latest_publish_repoints_to_previous_publish_not_git(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Test (e): deleting the latest PUBLISH revision re-points
    bundle.latest_revision_id to the previous PUBLISH revision, NOT to the
    interleaved git revision that has a higher revision_number.

    Without FIX 2: delete_revision's replacement search found the git revision
    (higher revision_number, but origin="git") and set latest_revision_id to
    the git baseline — an invisible revision from the consumer's perspective.
    A consumer on the previous publish would then see pending_update=True
    forever with no actionable update available.

    With FIX 2: the replacement search filters to origin="publish", so it
    correctly falls back to the previous catalog publish.

    Scenario:
      1. Publish rev 1 → bundle created; consumer installs rev 1
      2. Git connect → rev 2 (origin=git, higher revision_number than rev 1)
         bundle.latest_revision_id is NOT updated by git connect
      3. Publish rev 3 → bundle.latest_revision_id = rev3
         Consumer sees pending_update=True (rev3 > rev1)
      4. Delete rev 3 (latest publish)
         FIX 2: bundle.latest_revision_id → rev1 (not rev2 git)
      5. Consumer check-updates: pending_update=False (latest=rev1=installed)
      6. GET /bundles/{uuid}/revisions: only rev 1 remains (rev 2 hidden, rev 3 deleted)
    """
    headers = superuser_token_headers

    # ── Phase 1: Create + first publish + consumer installs rev 1 ────────────
    agent_id, _ = _create_agent_with_workspace(
        client, headers, tree={"docs": {"guide.md": "# Guide\nVersion one"}}
    )
    rev1 = publish_bundle_revision(client, headers, agent_id, notes="first")
    rev1_id = rev1["id"]
    first_pub_num = rev1["revision_number"]

    fresh = client.get(f"{API}/agents/{agent_id}", headers=headers).json()
    bundle_uuid = fresh["bundle_uuid"]
    bundle_id = fresh["bundle_id"]

    # Make bundle public so the consumer can install.
    make_bundle_public(client, headers, bundle_uuid)

    _, consumer_headers = make_user_and_headers(client)
    consumer_install = client.post(
        f"{API}/catalog/{bundle_id}/install",
        headers=consumer_headers,
        json={},
    ).json()
    drain_tasks()
    consumer_agent_id = consumer_install["id"]
    assert consumer_install["installed_revision_id"] == rev1_id

    # Sanity: immediately after install → no pending update.
    check0 = client.post(
        f"{API}/agents/{consumer_agent_id}/check-updates",
        headers=consumer_headers,
    ).json()
    assert check0["pending_update"] is False
    assert check0["installed_revision_number"] == first_pub_num
    assert check0["latest_revision_number"] == first_pub_num

    # ── Phase 2: Git connect → rev 2 (origin=git) ───────────────────────────
    # bundle.latest_revision_id is NOT changed by git connect (stays at rev1).
    _git_connect_init_path(client, headers, agent_id)

    # ── Phase 3: Second catalog publish → consumer has a pending update ───────
    rev3 = publish_bundle_revision(client, headers, agent_id, notes="second")
    rev3_id = rev3["id"]
    third_pub_num = rev3["revision_number"]

    # The git revision must have consumed a slot between the two publishes.
    assert third_pub_num == first_pub_num + 2, (
        f"Expected second publish at {first_pub_num + 2}, got {third_pub_num}"
    )

    check1 = client.post(
        f"{API}/agents/{consumer_agent_id}/check-updates",
        headers=consumer_headers,
    ).json()
    assert check1["pending_update"] is True, (
        "Consumer should see pending_update=True after second publish"
    )
    assert check1["latest_revision_number"] == third_pub_num

    # ── Phase 4: Delete the latest publish revision (rev 3) ───────────────────
    # Consumer is still on rev 1 (not rev 3), so foreign_count=0 and delete succeeds.
    del_r = client.delete(
        f"{API}/bundles/{bundle_uuid}/revisions/{rev3_id}",
        headers=headers,
    )
    assert del_r.status_code == 200, f"Delete revision failed: {del_r.text}"

    # ── Phase 5: Consumer check-updates → pending_update=False ───────────────
    # FIX 2: delete_revision re-pointed latest_revision_id to rev1 (publish),
    # NOT rev2 (git). Consumer is on rev1 → no pending update.
    check2 = client.post(
        f"{API}/agents/{consumer_agent_id}/check-updates",
        headers=consumer_headers,
    ).json()
    assert check2["pending_update"] is False, (
        "After deleting the latest publish revision, bundle.latest_revision_id "
        "should point to the PREVIOUS PUBLISH revision (rev 1), not the git "
        f"revision (rev 2). Got: pending_update={check2['pending_update']}, "
        f"latest_revision_number={check2.get('latest_revision_number')}"
    )
    assert check2["installed_revision_number"] == first_pub_num, (
        "Consumer should still be installed on rev 1"
    )
    assert check2["latest_revision_number"] == first_pub_num, (
        f"Bundle's latest should have been re-pointed to publish rev {first_pub_num}, "
        f"not to git rev {first_pub_num + 1}. "
        f"Got latest_revision_number={check2.get('latest_revision_number')}"
    )

    # ── Phase 6: Listing shows only rev 1 (rev 2 never shown; rev 3 deleted) ──
    final_revisions = _list_bundle_revisions(client, headers, bundle_uuid)
    final_nums = [r["revision_number"] for r in final_revisions]
    assert final_nums == [first_pub_num], (
        f"After deleting rev {third_pub_num}, listing should contain only "
        f"[{first_pub_num}]. Got: {final_nums}"
    )
    assert final_revisions[0]["id"] == rev1_id
