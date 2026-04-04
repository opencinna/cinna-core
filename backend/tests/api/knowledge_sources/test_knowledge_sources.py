"""
Integration tests for the knowledge-sources API.

All knowledge source endpoints are admin-only (superuser).
Three scenario-based tests covering the full surface:
  1. Lifecycle   — CRUD, enable/disable, ownership guards, status transitions
  2. Operational — check-access (success + failure), refresh (success + disabled)
  3. Discovery   — public discovery visibility for admins, non-admin rejection
"""

import uuid
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.knowledge_source import (
    create_knowledge_source,
    delete_knowledge_source,
    disable_knowledge_source,
    enable_knowledge_source,
    get_knowledge_source,
    list_knowledge_sources,
    update_knowledge_source,
)
from tests.utils.user import create_random_user, user_authentication_headers
from tests.utils.utils import random_lower_string

_BASE = f"{settings.API_V1_STR}/knowledge-sources"


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

@contextmanager
def _fake_clone_context(*args, **kwargs):
    """Pretend to clone a repository — yields (repo_path, repo_handle)."""
    yield ("/tmp/fake-repo", MagicMock())


def _mock_parse_settings():
    mock_settings = MagicMock()
    mock_settings.static_articles = [MagicMock(path="articles/test.md")]
    return mock_settings


def _refresh_patches():
    """Stack of patches that prevent real git/embedding work in refresh tests."""
    return (
        patch(
            "app.services.knowledge.knowledge_source_service.clone_repository_context",
            side_effect=_fake_clone_context,
        ),
        patch(
            "app.services.knowledge.knowledge_source_service.parse_settings_json",
            return_value=_mock_parse_settings(),
        ),
        patch(
            "app.services.knowledge.knowledge_source_service.process_repository_articles",
            return_value={"total": 1, "created": 1, "updated": 0, "skipped": 0, "errors": []},
        ),
        patch(
            "app.services.knowledge.knowledge_source_service.delete_orphaned_articles",
            return_value=0,
        ),
        patch(
            "app.services.knowledge.knowledge_source_service.chunk_and_embed_all_articles",
            return_value={
                "articles_processed": 0,
                "articles_failed": 0,
                "total_chunks_created": 0,
                "total_chunks_updated": 0,
            },
        ),
        patch(
            "app.services.knowledge.knowledge_source_service.get_current_commit_hash",
            return_value="abc123",
        ),
    )


# ---------------------------------------------------------------------------
# Scenario 1: Source lifecycle
# ---------------------------------------------------------------------------

def test_knowledge_source_lifecycle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Full source management lifecycle:
      1.  Unauthenticated request is rejected
      2.  Create source — verify initial state (pending, is_enabled, article_count)
      3.  Source appears in list
      4.  GET by ID — fields match
      5.  Update name and description — changes persist
      6.  Non-admin user gets 403 on all endpoints
      7.  Requests for a non-existent ID return 404
      8.  Changing branch resets status to 'pending'
      9.  Disable source — is_enabled=False
      10. Enable source — is_enabled=True
      11. Delete source — gone (404)
    """
    name = f"ks-{random_lower_string()[:12]}"

    # ── Phase 1: No auth ───────────────────────────────────────────────────
    r = client.get(f"{_BASE}/")
    assert r.status_code in (401, 403)

    # ── Phase 2: Create ───────────────────────────────────────────────────
    source = create_knowledge_source(client, superuser_token_headers, name=name)
    source_id = source["id"]

    assert source["name"] == name
    assert source["status"] == "pending"
    assert source["is_enabled"] is True
    assert source["article_count"] == 0
    assert source["public_discovery"] is False
    assert "git_url" in source and "branch" in source
    assert "created_at" in source and "updated_at" in source

    # ── Phase 3: List → source is present ────────────────────────────────
    sources = list_knowledge_sources(client, superuser_token_headers)
    assert any(s["id"] == source_id for s in sources)

    # ── Phase 4: GET by ID ───────────────────────────────────────────────
    fetched = get_knowledge_source(client, superuser_token_headers, source_id)
    assert fetched["id"] == source_id
    assert fetched["name"] == name

    # ── Phase 5: Update name and description ─────────────────────────────
    new_name = f"renamed-{random_lower_string()[:8]}"
    updated = update_knowledge_source(
        client, superuser_token_headers, source_id,
        name=new_name, description="updated desc",
    )
    assert updated["name"] == new_name
    assert updated["description"] == "updated desc"
    assert updated["status"] == "pending"  # non-git change must not affect status

    re_fetched = get_knowledge_source(client, superuser_token_headers, source_id)
    assert re_fetched["name"] == new_name

    # ── Phase 6: Non-admin user gets 403 on all endpoints ────────────────
    other_user = create_random_user(client)
    other_headers = user_authentication_headers(
        client=client, email=other_user["email"], password=other_user["_password"]
    )

    assert client.get(f"{_BASE}/", headers=other_headers).status_code == 403
    assert client.get(f"{_BASE}/{source_id}", headers=other_headers).status_code == 403
    assert client.put(
        f"{_BASE}/{source_id}", headers=other_headers, json={"name": "hacked"}
    ).status_code == 403
    assert client.delete(f"{_BASE}/{source_id}", headers=other_headers).status_code == 403
    assert client.post(
        f"{_BASE}/{source_id}/enable", headers=other_headers
    ).status_code == 403
    assert client.post(
        f"{_BASE}/{source_id}/check-access", headers=other_headers
    ).status_code == 403

    # Original owner's source is still intact
    get_knowledge_source(client, superuser_token_headers, source_id)

    # ── Phase 7: Non-existent ID returns 404 ─────────────────────────────
    ghost = str(uuid.uuid4())
    assert client.get(f"{_BASE}/{ghost}", headers=superuser_token_headers).status_code == 404
    assert client.put(
        f"{_BASE}/{ghost}", headers=superuser_token_headers, json={"name": "x"}
    ).status_code == 404
    assert client.delete(f"{_BASE}/{ghost}", headers=superuser_token_headers).status_code == 404
    assert client.post(
        f"{_BASE}/{ghost}/enable", headers=superuser_token_headers
    ).status_code == 404

    # ── Phase 8: Changing branch resets status to pending ────────────────
    with patch(
        "app.services.knowledge.knowledge_source_service.verify_repository_access",
        return_value=(True, "Repository accessible"),
    ):
        client.post(f"{_BASE}/{source_id}/check-access", headers=superuser_token_headers)

    assert get_knowledge_source(client, superuser_token_headers, source_id)["status"] == "connected"

    branch_updated = update_knowledge_source(
        client, superuser_token_headers, source_id, branch="develop"
    )
    assert branch_updated["branch"] == "develop"
    assert branch_updated["status"] == "pending"

    # ── Phase 9: Disable ─────────────────────────────────────��────────────
    disabled = disable_knowledge_source(client, superuser_token_headers, source_id)
    assert disabled["is_enabled"] is False
    assert get_knowledge_source(client, superuser_token_headers, source_id)["is_enabled"] is False

    # ── Phase 10: Enable ──────────────────────────────────────────────────
    enabled = enable_knowledge_source(client, superuser_token_headers, source_id)
    assert enabled["is_enabled"] is True
    assert get_knowledge_source(client, superuser_token_headers, source_id)["is_enabled"] is True

    # ── Phase 11: Delete → gone ───────────────────────────────────────────
    delete_knowledge_source(client, superuser_token_headers, source_id)
    assert client.get(f"{_BASE}/{source_id}", headers=superuser_token_headers).status_code == 404


# ---------------------------------------------------------------------------
# Scenario 2: Check access and refresh
# ---------------------------------------------------------------------------

def test_check_access_and_refresh(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Operational actions on a source:
      1. Create source
      2. Check access → success → status=connected
      3. Check access → failure → status=error
      4. Check access → success again → status=connected
      5. Articles endpoint returns empty list
      6. Refresh (all internals mocked) → status=success, last_sync_at set
      7. Disable source, then refresh → status=error (disabled guard)
    """
    # ── Phase 1: Create ───────────────────────────────────────────────────
    source = create_knowledge_source(client, superuser_token_headers)
    source_id = source["id"]
    assert source["status"] == "pending"

    # ── Phase 2: Check access success ─────────────────────────────────────
    with patch(
        "app.services.knowledge.knowledge_source_service.verify_repository_access",
        return_value=(True, "Repository accessible"),
    ):
        r = client.post(f"{_BASE}/{source_id}/check-access", headers=superuser_token_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["accessible"] is True
        assert "message" in body

    assert get_knowledge_source(client, superuser_token_headers, source_id)["status"] == "connected"

    # ── Phase 3: Check access failure ─────────────────────────────────────
    with patch(
        "app.services.knowledge.knowledge_source_service.verify_repository_access",
        return_value=(False, "Connection refused"),
    ):
        r = client.post(f"{_BASE}/{source_id}/check-access", headers=superuser_token_headers)
        assert r.status_code == 200
        assert r.json()["accessible"] is False

    assert get_knowledge_source(client, superuser_token_headers, source_id)["status"] == "error"

    # ── Phase 4: Restore to connected ─────────────────────────────────────
    with patch(
        "app.services.knowledge.knowledge_source_service.verify_repository_access",
        return_value=(True, "Repository accessible"),
    ):
        client.post(f"{_BASE}/{source_id}/check-access", headers=superuser_token_headers)

    assert get_knowledge_source(client, superuser_token_headers, source_id)["status"] == "connected"

    # ── Phase 5: Articles → empty ─────────────────────────────────────────
    r = client.get(f"{_BASE}/{source_id}/articles", headers=superuser_token_headers)
    assert r.status_code == 200
    assert r.json() == []

    # ── Phase 6: Refresh → success ────────────────────────────────────────
    p1, p2, p3, p4, p5, p6 = _refresh_patches()
    with p1, p2, p3, p4, p5, p6:
        r = client.post(f"{_BASE}/{source_id}/refresh", headers=superuser_token_headers)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "success"
        assert "message" in body

    after_refresh = get_knowledge_source(client, superuser_token_headers, source_id)
    assert after_refresh["status"] == "connected"
    assert after_refresh["last_sync_at"] is not None

    # ── Phase 7: Disabled source → refresh returns error ────────��─────────
    disable_knowledge_source(client, superuser_token_headers, source_id)

    r = client.post(f"{_BASE}/{source_id}/refresh", headers=superuser_token_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "error"
    assert "disabled" in body["message"].lower() or "enable" in body["message"].lower()


# ---------------------------------------------------------------------------
# Scenario 3: Discoverable sources (admin cross-visibility)
# ---------------------------------------------------------------------------

def test_discoverable_sources_flow(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    normal_user_token_headers: dict[str, str],
) -> None:
    """
    Public discovery workflow (admin-only, read-only):
      1. Superuser creates a source and makes it connected
      2. Superuser enables public_discovery=True
      3. Non-admin user gets 403 on discoverable list
      4. Superuser's own source does not appear in their own discoverable list
      5. Source has no is_enabled_by_user field in response
      6. Superuser disables public_discovery → source no longer discoverable
    """
    # ── Phase 1: Superuser creates and connects a source ──────────────────
    source = create_knowledge_source(client, superuser_token_headers)
    source_id = source["id"]

    with patch(
        "app.services.knowledge.knowledge_source_service.verify_repository_access",
        return_value=(True, "Repository accessible"),
    ):
        client.post(f"{_BASE}/{source_id}/check-access", headers=superuser_token_headers)

    # ── Phase 2: Enable public discovery ──────────────────────────────────
    updated = update_knowledge_source(
        client, superuser_token_headers, source_id, public_discovery=True
    )
    assert updated["public_discovery"] is True

    # ── Phase 3: Non-admin user gets 403 on discoverable list ─────────────
    r = client.get(f"{_BASE}/discoverable/list", headers=normal_user_token_headers)
    assert r.status_code == 403

    # ── Phase 4: Owner does not see own source in discoverable list ────────
    r = client.get(f"{_BASE}/discoverable/list", headers=superuser_token_headers)
    assert r.status_code == 200
    owner_view = r.json()
    assert not any(s["id"] == source_id for s in owner_view)

    # ── Phase 5: Response has no is_enabled_by_user field ──────���──────────
    for s in owner_view:
        assert "is_enabled_by_user" not in s

    # ── Phase 6: Disable public_discovery → no longer discoverable ────────
    update_knowledge_source(
        client, superuser_token_headers, source_id, public_discovery=False
    )
    r = client.get(f"{_BASE}/discoverable/list", headers=superuser_token_headers)
    assert r.status_code == 200
    assert not any(s["id"] == source_id for s in r.json())
