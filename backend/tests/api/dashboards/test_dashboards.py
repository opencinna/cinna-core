"""
Integration tests for the User Dashboards API (/api/v1/dashboards).

Test scenarios:
  1. Dashboard CRUD lifecycle — create, list, get, update, delete
  2. Block CRUD lifecycle — add, update, delete, with ownership guards
  3. Bulk layout update — drag-and-drop rearrangement
  4. Authorization — ownership checks, unauthenticated requests
  5. Validation limits — max 10 dashboards, max 20 blocks per dashboard
  6. Agent access validation — agent must be owned by user
  7. Webapp view_type validation — agent must have webapp_enabled
  8. Invalid view_type rejection
  9. Prompt action CRUD lifecycle — create, list, update, delete on a block
  10. Prompt actions returned in block/dashboard responses
  11. Prompt action ownership guards — other user cannot manage actions
  12. Prompt action validation — empty prompt_text rejected
  13. Agent env file block creation and config validation
  14. Agent env file endpoint security and path validation
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api, update_agent
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.dashboard import (
    add_block,
    create_agent_env_file_block,
    create_dashboard,
    create_prompt_action,
    delete_block,
    delete_dashboard,
    delete_prompt_action,
    get_block_env_file,
    get_block_latest_session,
    get_dashboard,
    list_dashboards,
    list_prompt_actions,
    update_block,
    update_block_layout,
    update_dashboard,
    update_prompt_action,
)
from tests.utils.background_tasks import drain_tasks
from tests.utils.environment import list_environments
from tests.utils.message import send_message
from tests.utils.session import create_session_with_block
from tests.utils.user import create_random_user, user_authentication_headers

_BASE = f"{settings.API_V1_STR}/dashboards"


# ── Scenario 1: Dashboard CRUD lifecycle ────────────────────────────────────

def test_dashboard_full_lifecycle(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Full dashboard CRUD lifecycle:
      1. Create dashboard
      2. List dashboards — verify it appears
      3. Get dashboard by ID
      4. Update dashboard (rename + description)
      5. Verify update persisted
      6. Delete dashboard
      7. Verify it's gone
    """
    # ── Phase 1: Create ──────────────────────────────────────────────────────
    dashboard = create_dashboard(
        client, superuser_token_headers, name="My Test Dashboard", description="Test desc"
    )
    dashboard_id = dashboard["id"]
    assert dashboard["name"] == "My Test Dashboard"
    assert dashboard["description"] == "Test desc"
    assert dashboard["blocks"] == []
    assert "sort_order" in dashboard

    # ── Phase 2: List → dashboard is present ────────────────────────────────
    dashboards = list_dashboards(client, superuser_token_headers)
    assert any(d["id"] == dashboard_id for d in dashboards)

    # ── Phase 3: Get by ID ───────────────────────────────────────────────────
    fetched = get_dashboard(client, superuser_token_headers, dashboard_id)
    assert fetched["id"] == dashboard_id
    assert fetched["name"] == "My Test Dashboard"
    assert fetched["blocks"] == []

    # ── Phase 4: Update ──────────────────────────────────────────────────────
    updated = update_dashboard(
        client, superuser_token_headers, dashboard_id, name="Renamed", description="Updated desc"
    )
    assert updated["name"] == "Renamed"
    assert updated["description"] == "Updated desc"

    # ── Phase 5: Verify update persisted ────────────────────────────────────
    refetched = get_dashboard(client, superuser_token_headers, dashboard_id)
    assert refetched["name"] == "Renamed"

    # ── Phase 6: Auth and ownership guards ──────────────────────────────────
    # Unauthenticated
    assert client.get(f"{_BASE}/").status_code in (401, 403)
    assert client.get(f"{_BASE}/{dashboard_id}").status_code in (401, 403)

    # Other user cannot access
    other_user = create_random_user(client)
    other_headers = user_authentication_headers(
        client=client, email=other_user["email"], password=other_user["_password"]
    )
    assert client.get(f"{_BASE}/{dashboard_id}", headers=other_headers).status_code in (403, 404)
    assert client.put(f"{_BASE}/{dashboard_id}", headers=other_headers, json={"name": "X"}).status_code in (403, 404)
    assert client.delete(f"{_BASE}/{dashboard_id}", headers=other_headers).status_code in (403, 404)

    # Non-existent ID returns 404
    ghost = str(uuid.uuid4())
    assert client.get(f"{_BASE}/{ghost}", headers=superuser_token_headers).status_code == 404

    # ── Phase 7: Delete ──────────────────────────────────────────────────────
    delete_dashboard(client, superuser_token_headers, dashboard_id)

    # ── Phase 8: Verify gone ─────────────────────────────────────────────────
    r = client.get(f"{_BASE}/{dashboard_id}", headers=superuser_token_headers)
    assert r.status_code == 404


# ── Scenario 2: Block CRUD lifecycle ────────────────────────────────────────

def test_block_full_lifecycle(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Block CRUD lifecycle on a dashboard:
      1. Create dashboard
      2. Create agent
      3. Add block to dashboard
      4. Verify block appears in dashboard
      5. Update block settings
      6. Verify update persisted
      7. Delete block
      8. Verify block is gone
      9. Ownership guards for block operations
    """
    # ── Phase 1 & 2: Create dashboard and agent ──────────────────────────────
    dashboard = create_dashboard(client, superuser_token_headers, name="Block Test Dashboard")
    dashboard_id = dashboard["id"]
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    # ── Phase 3: Add block ───────────────────────────────────────────────────
    block = add_block(
        client, superuser_token_headers, dashboard_id, agent_id,
        view_type="latest_session", grid_x=0, grid_y=0, grid_w=3, grid_h=2
    )
    block_id = block["id"]
    assert block["agent_id"] == agent_id
    assert block["view_type"] == "latest_session"
    assert block["grid_w"] == 3
    assert block["grid_h"] == 2
    assert block["show_border"] is True
    assert block["show_header"] is False  # default

    # ── Phase 4: Verify block appears in dashboard ───────────────────────────
    fetched = get_dashboard(client, superuser_token_headers, dashboard_id)
    assert len(fetched["blocks"]) == 1
    assert fetched["blocks"][0]["id"] == block_id

    # ── Phase 5: Update block ────────────────────────────────────────────────
    updated_block = update_block(
        client, superuser_token_headers, dashboard_id, block_id,
        view_type="latest_tasks", title="Custom Title", show_border=False, show_header=True
    )
    assert updated_block["view_type"] == "latest_tasks"
    assert updated_block["title"] == "Custom Title"
    assert updated_block["show_border"] is False
    assert updated_block["show_header"] is True

    # ── Phase 6: Verify block update persisted ───────────────────────────────
    refetched = get_dashboard(client, superuser_token_headers, dashboard_id)
    saved_block = refetched["blocks"][0]
    assert saved_block["view_type"] == "latest_tasks"
    assert saved_block["title"] == "Custom Title"
    assert saved_block["show_header"] is True

    # ── Phase 7: Block ownership guards ─────────────────────────────────────
    other_user = create_random_user(client)
    other_headers = user_authentication_headers(
        client=client, email=other_user["email"], password=other_user["_password"]
    )
    # Other user cannot modify blocks on this dashboard
    r = client.put(
        f"{_BASE}/{dashboard_id}/blocks/{block_id}",
        headers=other_headers,
        json={"title": "Hijack"},
    )
    assert r.status_code in (403, 404)

    r = client.delete(
        f"{_BASE}/{dashboard_id}/blocks/{block_id}", headers=other_headers
    )
    assert r.status_code in (403, 404)

    # ── Phase 8: Delete block ────────────────────────────────────────────────
    delete_block(client, superuser_token_headers, dashboard_id, block_id)

    # ── Phase 9: Verify block gone ───────────────────────────────────────────
    refetched2 = get_dashboard(client, superuser_token_headers, dashboard_id)
    assert len(refetched2["blocks"]) == 0

    r = client.delete(
        f"{_BASE}/{dashboard_id}/blocks/{block_id}", headers=superuser_token_headers
    )
    assert r.status_code == 404


# ── Scenario 3: Bulk layout update ──────────────────────────────────────────

def test_bulk_layout_update(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Bulk layout update (drag-and-drop):
      1. Create dashboard with 2 blocks
      2. Call layout update endpoint with new grid positions
      3. Verify positions persisted in dashboard response
    """
    dashboard = create_dashboard(client, superuser_token_headers, name="Layout Test")
    dashboard_id = dashboard["id"]

    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    block1 = add_block(
        client, superuser_token_headers, dashboard_id, agent_id,
        grid_x=0, grid_y=0, grid_w=2, grid_h=2
    )
    block2 = add_block(
        client, superuser_token_headers, dashboard_id, agent_id,
        grid_x=2, grid_y=0, grid_w=2, grid_h=2
    )

    # Move block1 to (4,2) and resize block2
    new_layouts = [
        {"block_id": block1["id"], "grid_x": 4, "grid_y": 2, "grid_w": 3, "grid_h": 3},
        {"block_id": block2["id"], "grid_x": 0, "grid_y": 0, "grid_w": 4, "grid_h": 4},
    ]
    updated_blocks = update_block_layout(
        client, superuser_token_headers, dashboard_id, new_layouts
    )
    assert len(updated_blocks) == 2

    # Verify positions persisted
    fetched = get_dashboard(client, superuser_token_headers, dashboard_id)
    blocks_by_id = {b["id"]: b for b in fetched["blocks"]}

    b1 = blocks_by_id[block1["id"]]
    assert b1["grid_x"] == 4
    assert b1["grid_y"] == 2
    assert b1["grid_w"] == 3
    assert b1["grid_h"] == 3

    b2 = blocks_by_id[block2["id"]]
    assert b2["grid_x"] == 0
    assert b2["grid_w"] == 4

    # Layout update with unknown block_id returns 404
    r = client.put(
        f"{_BASE}/{dashboard_id}/blocks/layout",
        headers=superuser_token_headers,
        json=[{"block_id": str(uuid.uuid4()), "grid_x": 0, "grid_y": 0, "grid_w": 2, "grid_h": 2}],
    )
    assert r.status_code == 404


# ── Scenario 4: Max dashboard limit ─────────────────────────────────────────

def test_max_dashboards_limit(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Max 10 dashboards per user:
      1. Create 10 dashboards (should succeed)
      2. Attempt to create 11th — should get 409
    """
    dashboard_ids = []
    for i in range(10):
        d = create_dashboard(client, superuser_token_headers, name=f"Dashboard {i}")
        dashboard_ids.append(d["id"])

    # 11th should fail
    r = client.post(
        f"{_BASE}/", headers=superuser_token_headers, json={"name": "Over Limit"}
    )
    assert r.status_code == 409

    # Clean up
    for did in dashboard_ids:
        delete_dashboard(client, superuser_token_headers, did)


# ── Scenario 5: Max blocks limit ────────────────────────────────────────────

def test_max_blocks_limit(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Max 20 blocks per dashboard:
      1. Create dashboard and agent
      2. Add 20 blocks (should succeed)
      3. Attempt 21st — should get 409
    """
    dashboard = create_dashboard(client, superuser_token_headers, name="Block Limit Test")
    dashboard_id = dashboard["id"]
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    for _ in range(20):
        add_block(client, superuser_token_headers, dashboard_id, agent_id)

    # 21st should fail
    r = client.post(
        f"{_BASE}/{dashboard_id}/blocks",
        headers=superuser_token_headers,
        json={
            "agent_id": agent_id,
            "view_type": "latest_session",
            "title": None,
            "show_border": True,
            "grid_x": 0,
            "grid_y": 0,
            "grid_w": 2,
            "grid_h": 2,
        },
    )
    assert r.status_code == 409


# ── Scenario 6: Agent access validation ─────────────────────────────────────

def test_block_agent_access_validation(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Cannot add a block for an agent owned by another user.
    """
    dashboard = create_dashboard(client, superuser_token_headers, name="Agent Access Test")
    dashboard_id = dashboard["id"]

    # Create agent under a different user (need AI credentials for that user too)
    other_user = create_random_user(client)
    other_headers = user_authentication_headers(
        client=client, email=other_user["email"], password=other_user["_password"]
    )
    # Set up default AI credential for other user so agent creation works
    create_random_ai_credential(client, other_headers, set_default=True)
    other_agent = create_agent_via_api(client, other_headers)
    other_agent_id = other_agent["id"]

    # Superuser tries to add other user's agent — should fail
    r = client.post(
        f"{_BASE}/{dashboard_id}/blocks",
        headers=superuser_token_headers,
        json={
            "agent_id": other_agent_id,
            "view_type": "latest_session",
            "title": None,
            "show_border": True,
            "grid_x": 0,
            "grid_y": 0,
            "grid_w": 2,
            "grid_h": 2,
        },
    )
    assert r.status_code == 400

    # Non-existent agent_id also returns 400
    r = client.post(
        f"{_BASE}/{dashboard_id}/blocks",
        headers=superuser_token_headers,
        json={
            "agent_id": str(uuid.uuid4()),
            "view_type": "latest_session",
            "title": None,
            "show_border": True,
            "grid_x": 0,
            "grid_y": 0,
            "grid_w": 2,
            "grid_h": 2,
        },
    )
    assert r.status_code == 400


# ── Scenario 7: Webapp view_type validation ──────────────────────────────────

def test_webapp_view_type_validation(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Cannot add webapp block if agent has webapp_enabled=False.
    When webapp_enabled=True, block creation succeeds.
    """
    dashboard = create_dashboard(client, superuser_token_headers, name="Webapp Test")
    dashboard_id = dashboard["id"]
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    # Agent defaults to webapp_enabled=False — should fail
    r = client.post(
        f"{_BASE}/{dashboard_id}/blocks",
        headers=superuser_token_headers,
        json={
            "agent_id": agent_id,
            "view_type": "webapp",
            "title": None,
            "show_border": True,
            "grid_x": 0,
            "grid_y": 0,
            "grid_w": 2,
            "grid_h": 2,
        },
    )
    assert r.status_code == 400

    # Enable webapp on agent
    update_agent(client, superuser_token_headers, agent_id, webapp_enabled=True)

    # Now it should succeed
    block = add_block(
        client, superuser_token_headers, dashboard_id, agent_id, view_type="webapp"
    )
    assert block["view_type"] == "webapp"


# ── Scenario 8: Invalid view_type ────────────────────────────────────────────

def test_invalid_view_type_rejected(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Invalid view_type values are rejected with 422.
    """
    dashboard = create_dashboard(client, superuser_token_headers, name="View Type Test")
    dashboard_id = dashboard["id"]
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    r = client.post(
        f"{_BASE}/{dashboard_id}/blocks",
        headers=superuser_token_headers,
        json={
            "agent_id": agent_id,
            "view_type": "invalid_type_xyz",
            "title": None,
            "show_border": True,
            "grid_x": 0,
            "grid_y": 0,
            "grid_w": 2,
            "grid_h": 2,
        },
    )
    assert r.status_code == 422


# ── Scenario 9: Prompt action CRUD lifecycle ─────────────────────────────────

def test_prompt_action_full_lifecycle(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Full prompt action CRUD lifecycle:
      1. Create a dashboard and block
      2. Create two prompt actions with label and without
      3. List prompt actions — verify both appear, correct fields, sort_order respected
      4. Verify prompt_actions returned in block GET response
      5. Verify prompt_actions returned in dashboard GET response
      6. Update first prompt action (change label and prompt_text)
      7. Verify update persisted
      8. Delete one prompt action
      9. Verify deletion — list returns only 1 action
      10. Delete block — verify actions cascade-delete (block gone)
    """
    # ── Phase 1: Setup dashboard + block ─────────────────────────────────────
    dashboard = create_dashboard(client, superuser_token_headers, name="Prompt Action Test")
    dashboard_id = dashboard["id"]
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]
    block = add_block(client, superuser_token_headers, dashboard_id, agent_id)
    block_id = block["id"]

    # Block starts with no prompt_actions
    assert block["prompt_actions"] == []

    # ── Phase 2: Create two prompt actions ───────────────────────────────────
    action1 = create_prompt_action(
        client, superuser_token_headers, dashboard_id, block_id,
        prompt_text="Check my emails and summarize",
        label="Check emails",
        sort_order=0,
    )
    assert action1["prompt_text"] == "Check my emails and summarize"
    assert action1["label"] == "Check emails"
    assert action1["sort_order"] == 0
    assert action1["block_id"] == block_id
    assert "id" in action1
    assert "created_at" in action1
    action1_id = action1["id"]

    action2 = create_prompt_action(
        client, superuser_token_headers, dashboard_id, block_id,
        prompt_text="Run a full status report",
        sort_order=1,
    )
    assert action2["prompt_text"] == "Run a full status report"
    assert action2["label"] is None
    action2_id = action2["id"]

    # ── Phase 3: List prompt actions ─────────────────────────────────────────
    actions = list_prompt_actions(client, superuser_token_headers, dashboard_id, block_id)
    assert len(actions) == 2
    # Verify ordering by sort_order
    assert actions[0]["id"] == action1_id
    assert actions[1]["id"] == action2_id

    # ── Phase 4: Verify prompt_actions in block GET response ─────────────────
    fetched_dashboard = get_dashboard(client, superuser_token_headers, dashboard_id)
    blocks_by_id = {b["id"]: b for b in fetched_dashboard["blocks"]}
    fetched_block = blocks_by_id[block_id]
    assert len(fetched_block["prompt_actions"]) == 2
    action_ids_in_block = {a["id"] for a in fetched_block["prompt_actions"]}
    assert action1_id in action_ids_in_block
    assert action2_id in action_ids_in_block

    # ── Phase 5: Verify prompt_actions in list dashboards response ────────────
    all_dashboards = list_dashboards(client, superuser_token_headers)
    target = next((d for d in all_dashboards if d["id"] == dashboard_id), None)
    assert target is not None
    target_block = next((b for b in target["blocks"] if b["id"] == block_id), None)
    assert target_block is not None
    assert len(target_block["prompt_actions"]) == 2

    # ── Phase 6: Update first prompt action ──────────────────────────────────
    updated = update_prompt_action(
        client, superuser_token_headers, dashboard_id, block_id, action1_id,
        prompt_text="Check all inboxes and report status",
        label="Check inboxes",
    )
    assert updated["prompt_text"] == "Check all inboxes and report status"
    assert updated["label"] == "Check inboxes"
    assert updated["id"] == action1_id

    # ── Phase 7: Verify update persisted ─────────────────────────────────────
    actions_after_update = list_prompt_actions(
        client, superuser_token_headers, dashboard_id, block_id
    )
    first = next((a for a in actions_after_update if a["id"] == action1_id), None)
    assert first is not None
    assert first["prompt_text"] == "Check all inboxes and report status"

    # ── Phase 8: Delete first prompt action ───────────────────────────────────
    delete_prompt_action(
        client, superuser_token_headers, dashboard_id, block_id, action1_id
    )

    # ── Phase 9: Verify deletion ──────────────────────────────────────────────
    actions_after_delete = list_prompt_actions(
        client, superuser_token_headers, dashboard_id, block_id
    )
    assert len(actions_after_delete) == 1
    assert actions_after_delete[0]["id"] == action2_id

    # ── Phase 10: Block cascade-delete removes actions ────────────────────────
    delete_block(client, superuser_token_headers, dashboard_id, block_id)
    # Dashboard has no blocks now; no way to list the block's actions
    # Verify via dashboard GET that block is gone
    fetched_after_block_delete = get_dashboard(client, superuser_token_headers, dashboard_id)
    assert all(b["id"] != block_id for b in fetched_after_block_delete["blocks"])


# ── Scenario 10: Prompt action ownership guards ──────────────────────────────

def test_prompt_action_ownership_guards(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Prompt action ownership guards:
      1. Create dashboard + block + prompt action as superuser
      2. Another user cannot list, create, update, or delete prompt actions on the block
      3. Non-existent action_id returns 404 for owner
      4. Non-existent block_id returns 404 for list/create
    """
    # ── Phase 1: Setup ────────────────────────────────────────────────────────
    dashboard = create_dashboard(client, superuser_token_headers, name="Ownership Test")
    dashboard_id = dashboard["id"]
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]
    block = add_block(client, superuser_token_headers, dashboard_id, agent_id)
    block_id = block["id"]
    action = create_prompt_action(
        client, superuser_token_headers, dashboard_id, block_id,
        prompt_text="Ownership test action",
    )
    action_id = action["id"]

    # ── Phase 2: Another user cannot access prompt actions ────────────────────
    other_user = create_random_user(client)
    other_headers = user_authentication_headers(
        client=client, email=other_user["email"], password=other_user["_password"]
    )

    # List → 404 (dashboard not found for other user)
    r = client.get(
        f"{_BASE}/{dashboard_id}/blocks/{block_id}/prompt-actions",
        headers=other_headers,
    )
    assert r.status_code in (403, 404)

    # Create → 404/403
    r = client.post(
        f"{_BASE}/{dashboard_id}/blocks/{block_id}/prompt-actions",
        headers=other_headers,
        json={"prompt_text": "Sneaky action", "sort_order": 0},
    )
    assert r.status_code in (403, 404)

    # Update → 404/403
    r = client.put(
        f"{_BASE}/{dashboard_id}/blocks/{block_id}/prompt-actions/{action_id}",
        headers=other_headers,
        json={"prompt_text": "Modified"},
    )
    assert r.status_code in (403, 404)

    # Delete → 404/403
    r = client.delete(
        f"{_BASE}/{dashboard_id}/blocks/{block_id}/prompt-actions/{action_id}",
        headers=other_headers,
    )
    assert r.status_code in (403, 404)

    # Unauthenticated requests → 401
    assert client.get(
        f"{_BASE}/{dashboard_id}/blocks/{block_id}/prompt-actions"
    ).status_code in (401, 403)

    # ── Phase 3: Non-existent action_id → 404 ────────────────────────────────
    ghost_action_id = str(uuid.uuid4())
    r = client.put(
        f"{_BASE}/{dashboard_id}/blocks/{block_id}/prompt-actions/{ghost_action_id}",
        headers=superuser_token_headers,
        json={"prompt_text": "Ghost update"},
    )
    assert r.status_code == 404

    r = client.delete(
        f"{_BASE}/{dashboard_id}/blocks/{block_id}/prompt-actions/{ghost_action_id}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 404

    # ── Phase 4: Non-existent block_id → 404 ─────────────────────────────────
    ghost_block_id = str(uuid.uuid4())
    r = client.get(
        f"{_BASE}/{dashboard_id}/blocks/{ghost_block_id}/prompt-actions",
        headers=superuser_token_headers,
    )
    assert r.status_code == 404

    r = client.post(
        f"{_BASE}/{dashboard_id}/blocks/{ghost_block_id}/prompt-actions",
        headers=superuser_token_headers,
        json={"prompt_text": "Action for ghost block", "sort_order": 0},
    )
    assert r.status_code == 404


# ── Scenario 11: Prompt action validation ────────────────────────────────────

def test_prompt_action_validation(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Prompt action input validation:
      1. Empty prompt_text is rejected with 422
      2. prompt_text exceeding max_length is rejected with 422
      3. label exceeding max_length is rejected with 422
    """
    dashboard = create_dashboard(client, superuser_token_headers, name="Validation Test")
    dashboard_id = dashboard["id"]
    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]
    block = add_block(client, superuser_token_headers, dashboard_id, agent_id)
    block_id = block["id"]

    _actions_url = f"{_BASE}/{dashboard_id}/blocks/{block_id}/prompt-actions"

    # Empty prompt_text → 422
    r = client.post(
        _actions_url,
        headers=superuser_token_headers,
        json={"prompt_text": "", "sort_order": 0},
    )
    assert r.status_code == 422

    # Missing prompt_text → 422
    r = client.post(
        _actions_url,
        headers=superuser_token_headers,
        json={"sort_order": 0},
    )
    assert r.status_code == 422

    # Overly long prompt_text (> 2000 chars) → 422
    r = client.post(
        _actions_url,
        headers=superuser_token_headers,
        json={"prompt_text": "x" * 2001, "sort_order": 0},
    )
    assert r.status_code == 422

    # Overly long label (> 100 chars) → 422
    r = client.post(
        _actions_url,
        headers=superuser_token_headers,
        json={"prompt_text": "Valid prompt text", "label": "L" * 101, "sort_order": 0},
    )
    assert r.status_code == 422

    # Valid creation succeeds
    valid = create_prompt_action(
        client, superuser_token_headers, dashboard_id, block_id,
        prompt_text="Valid action",
        label="Valid",
    )


# ── Scenario 12: Block latest-session endpoint ────────────────────────────────

def test_block_latest_session(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Verify the GET /dashboards/{id}/blocks/{block_id}/latest-session endpoint:
      1. No session exists → 404
      2. Session created with dashboard_block_id but no messages → still 404
         (last_message_at is NULL until a message is sent)
      3. Send a message to the session → last_message_at is set
      4. Endpoint now returns 200 with the correct session
      5. The session response contains dashboard_block_id
      6. Ownership guard: other user gets 403/404
      7. Ghost dashboard_id returns 404
    """
    # ── Phase 1: Setup ────────────────────────────────────────────────────────
    dashboard = create_dashboard(client, superuser_token_headers, name="Block Session Test")
    dashboard_id = dashboard["id"]

    # Create agent, drain background tasks to activate environment, then re-fetch
    # to get active_environment_id (required to create sessions).
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    r = client.get(
        f"{settings.API_V1_STR}/agents/{agent['id']}", headers=superuser_token_headers
    )
    agent = r.json()
    agent_id = agent["id"]
    assert agent["active_environment_id"] is not None

    block = add_block(client, superuser_token_headers, dashboard_id, agent_id)
    block_id = block["id"]

    # ── Phase 2: No session → 404 ─────────────────────────────────────────────
    status, _ = get_block_latest_session(client, superuser_token_headers, dashboard_id, block_id)
    assert status == 404

    # ── Phase 3: Create session tagged with block, no messages → 404 ──────────
    session = create_session_with_block(
        client, superuser_token_headers, agent_id, block_id
    )
    session_id = session["id"]

    # The session must have dashboard_block_id set in the response
    assert session["dashboard_block_id"] == block_id

    # Still 404 because last_message_at is NULL (no messages yet)
    status, _ = get_block_latest_session(client, superuser_token_headers, dashboard_id, block_id)
    assert status == 404

    # ── Phase 4: Send message → last_message_at is set → 200 ─────────────────
    send_message(client, superuser_token_headers, session_id, "Hello from prompt action")

    status, returned_session = get_block_latest_session(
        client, superuser_token_headers, dashboard_id, block_id
    )
    assert status == 200
    assert returned_session is not None
    assert returned_session["id"] == session_id
    assert returned_session["dashboard_block_id"] == block_id

    # ── Phase 5: Ownership guard ──────────────────────────────────────────────
    other_user = create_random_user(client)
    other_headers = user_authentication_headers(
        client=client, email=other_user["email"], password=other_user["_password"]
    )
    status, _ = get_block_latest_session(client, other_headers, dashboard_id, block_id)
    assert status in (403, 404)

    # Unauthenticated → 401/403
    status_unauth = client.get(
        f"{_BASE}/{dashboard_id}/blocks/{block_id}/latest-session"
    ).status_code
    assert status_unauth in (401, 403)

    # ── Phase 6: Ghost dashboard_id → 404 ────────────────────────────────────
    ghost_dashboard = str(uuid.uuid4())
    status, _ = get_block_latest_session(client, superuser_token_headers, ghost_dashboard, block_id)
    assert status == 404


# ── Scenario 13: Agent env file block creation and config validation ──────────

def test_agent_env_file_block_creation(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    agent_env_file block creation and validation:
      1. Create dashboard and agent; drain background tasks so the default environment is created
      2. Create agent_env_file block with file_path config → 200
      3. Create agent_env_file block without config (no file selected yet) → 200
      4. Fetch dashboard → blocks appear with correct config
      5. List env files endpoint → returns list (empty from test adapter)
    """
    # ── Phase 1: Setup dashboard + agent ─────────────────────────────────────
    dashboard = create_dashboard(client, superuser_token_headers, name="Env File Block Test")
    dashboard_id = dashboard["id"]

    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    # Drain background tasks so the agent's default environment is created
    drain_tasks()

    envs = list_environments(client, superuser_token_headers, agent_id)
    assert envs["count"] >= 1, "Agent must have at least one environment after drain_tasks()"

    # ── Phase 2: Create agent_env_file block with file_path ──────────────────
    block = create_agent_env_file_block(
        client, superuser_token_headers, dashboard_id, agent_id,
        file_path="files/report.csv",
    )
    block_id = block["id"]
    assert block["view_type"] == "agent_env_file"
    assert block["config"] is not None
    assert block["config"]["file_path"] == "files/report.csv"

    # ── Phase 3: Create block without config (file not selected yet) ─────────
    block_no_file = create_agent_env_file_block(
        client, superuser_token_headers, dashboard_id, agent_id,
    )
    assert block_no_file["view_type"] == "agent_env_file"

    # ── Phase 4: Verify blocks appear in dashboard GET ────────────────────────
    fetched = get_dashboard(client, superuser_token_headers, dashboard_id)
    blocks_by_id = {b["id"]: b for b in fetched["blocks"]}
    assert block_id in blocks_by_id
    saved = blocks_by_id[block_id]
    assert saved["view_type"] == "agent_env_file"
    assert saved["config"]["file_path"] == "files/report.csv"

    # ── Phase 5: List env files → returns list (empty from test adapter) ──────
    r = client.get(
        f"{_BASE}/{dashboard_id}/blocks/{block_id}/env-files",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# ── Scenario 14: Agent env file endpoint security and path validation ─────────

def test_agent_env_file_endpoint(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Agent env file endpoint security and path validation:
      1. Setup: dashboard + agent + drain tasks
      2. Create agent_env_file block
      3. Call env-file endpoint — EnvironmentTestAdapter does NOT implement
         LocalFilesAccessInterface, so the fallback path is used. After
         drain_tasks() the env is "running", stub yields b"", so 200 is returned.
         Path is now relative to files/ folder (endpoint prefixes files/).
      4. Path traversal attempt → 400
      5. Absolute path attempt → 400
      6. Wrong block type (latest_session block) → 400
      7. Cross-user request → 403/404
      8. Ghost block_id → 404
    """
    # ── Phase 1: Setup dashboard + agent ─────────────────────────────────────
    dashboard = create_dashboard(client, superuser_token_headers, name="Env File Endpoint Test")
    dashboard_id = dashboard["id"]

    agent = create_agent_via_api(client, superuser_token_headers)
    agent_id = agent["id"]

    drain_tasks()

    envs = list_environments(client, superuser_token_headers, agent_id)
    assert envs["count"] >= 1

    # ── Phase 2: Create agent_env_file block ──────────────────────────────────
    block = create_agent_env_file_block(
        client, superuser_token_headers, dashboard_id, agent_id,
        file_path="files/data.txt",
    )
    block_id = block["id"]

    # ── Phase 3: Fallback path — env is running → 200 (empty body from stub) ──
    # Path is workspace-relative (e.g. files/data.txt)
    status, _ = get_block_env_file(
        client, superuser_token_headers, dashboard_id, block_id, path="files/data.txt"
    )
    assert status == 200

    # ── Phase 4: Path traversal → 400 ─────────────────────────────────────────
    status, _ = get_block_env_file(
        client, superuser_token_headers, dashboard_id, block_id, path="../secret"
    )
    assert status == 400

    # ── Phase 5: Absolute path → 400 ──────────────────────────────────────────
    status, _ = get_block_env_file(
        client, superuser_token_headers, dashboard_id, block_id, path="/etc/passwd"
    )
    assert status == 400

    # ── Phase 6: Wrong block type → 400 ──────────────────────────────────────
    wrong_block = add_block(
        client, superuser_token_headers, dashboard_id, agent_id,
        view_type="latest_session",
    )
    wrong_block_id = wrong_block["id"]
    status, _ = get_block_env_file(
        client, superuser_token_headers, dashboard_id, wrong_block_id, path="files/data.txt"
    )
    assert status == 400

    # ── Phase 7: Cross-user request → 403/404 ────────────────────────────────
    other_user = create_random_user(client)
    other_headers = user_authentication_headers(
        client=client, email=other_user["email"], password=other_user["_password"]
    )
    status, _ = get_block_env_file(
        client, other_headers, dashboard_id, block_id, path="files/data.txt"
    )
    assert status in (403, 404)

    # Unauthenticated → 401/403
    r = client.get(
        f"{_BASE}/{dashboard_id}/blocks/{block_id}/env-file",
        params={"path": "files/data.txt"},
    )
    assert r.status_code in (401, 403)

    # ── Phase 8: Ghost block_id → 404 ────────────────────────────────────────
    ghost_block_id = str(uuid.uuid4())
    status, _ = get_block_env_file(
        client, superuser_token_headers, dashboard_id, ghost_block_id, path="files/data.txt"
    )
    assert status == 404
