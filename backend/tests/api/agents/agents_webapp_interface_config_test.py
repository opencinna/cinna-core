"""
Integration tests: Agent Webapp Interface Config feature.

Covers two route groups:
  A. Owner config management  (GET/PUT /agents/{id}/webapp-interface-config/)
  B. Interface config in share info  (GET /webapp-share/{token}/info)

Business rules tested:
  1. GET auto-creates a default record (show_header=True, chat_mode=None) on first call
  2. PUT updates only the fields provided; omitted fields are unchanged (including empty body)
  3. PUT advances updated_at on the config record
  4. Both GET and PUT require authentication (401/403 when unauthenticated)
  5. Only the owning user can access or mutate the config; other users get 404
  6. Non-existent agent returns 404 from both GET and PUT
  7. The share info endpoint embeds interface_config reflecting the current state
  8. Changes to the config are immediately visible in the share info response
  9. chat_mode accepts "conversation", "building", or null (disabled)
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api, update_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.user import create_random_user_with_headers
from tests.utils.webapp_interface_config import (
    get_webapp_interface_config,
    update_webapp_interface_config,
)
from tests.utils.webapp_share import get_webapp_share_info, setup_webapp_agent

API = settings.API_V1_STR


# ── A. Owner config management ────────────────────────────────────────────


def test_webapp_interface_config_full_lifecycle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Full lifecycle for webapp interface config:
      1. Create agent
      2. GET config → returns defaults (show_header=True, chat_mode=None), auto-creates record
      3. Verify response shape (id, agent_id, created_at, updated_at present)
      4. PUT show_header=False → verify response reflects change; updated_at advances
      5. GET → verify show_header=False persisted
      6. PUT chat_mode="conversation" → verify chat_mode set, show_header still False
      7. PUT both fields at once → verify both updated
      8. PUT with empty body → both fields unchanged (no-op partial update)
      9. Auth guard: unauthenticated GET → 401/403
     10. Auth guard: unauthenticated PUT → 401/403
     11. Ownership guard: other user GET → 404
     12. Ownership guard: other user PUT → 404
     13. Non-existent agent → 404 for both GET and PUT
    """
    # ── Phase 1: Create agent ─────────────────────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers, name="Interface Config Agent")
    drain_tasks()
    agent_id = agent["id"]

    # ── Phase 2: GET config → defaults, record auto-created ───────────────
    config = get_webapp_interface_config(client, superuser_token_headers, agent_id)
    assert config["show_header"] is True
    assert config["chat_mode"] is None

    # ── Phase 3: Verify response shape ────────────────────────────────────
    assert "id" in config
    assert "agent_id" in config
    assert "created_at" in config
    assert "updated_at" in config
    assert config["agent_id"] == agent_id

    # ── Phase 4: PUT show_header=False; updated_at advances ──────────────
    updated_at_before = config["updated_at"]
    updated = update_webapp_interface_config(
        client, superuser_token_headers, agent_id, show_header=False
    )
    assert updated["show_header"] is False
    assert updated["chat_mode"] is None  # unchanged
    assert updated["agent_id"] == agent_id
    assert updated["updated_at"] >= updated_at_before  # timestamp advanced or equal

    # ── Phase 5: GET → show_header=False persisted ────────────────────────
    fetched = get_webapp_interface_config(client, superuser_token_headers, agent_id)
    assert fetched["show_header"] is False
    assert fetched["chat_mode"] is None

    # ── Phase 6: PUT chat_mode="conversation" → show_header still False ──
    updated = update_webapp_interface_config(
        client, superuser_token_headers, agent_id, chat_mode="conversation"
    )
    assert updated["chat_mode"] == "conversation"
    assert updated["show_header"] is False  # unchanged from phase 4

    # ── Phase 7: PUT both fields at once ──────────────────────────────────
    updated = update_webapp_interface_config(
        client, superuser_token_headers, agent_id, show_header=True, chat_mode=None
    )
    assert updated["show_header"] is True
    assert updated["chat_mode"] is None

    fetched = get_webapp_interface_config(client, superuser_token_headers, agent_id)
    assert fetched["show_header"] is True
    assert fetched["chat_mode"] is None

    # ── Phase 8: PUT with empty body → no-op, both fields unchanged ───────
    r = client.put(
        f"{API}/agents/{agent_id}/webapp-interface-config/",
        headers=superuser_token_headers,
        json={},
    )
    assert r.status_code == 200
    noop = r.json()
    assert noop["show_header"] is True   # still True from phase 7
    assert noop["chat_mode"] is None     # still None from phase 7

    # ── Phase 9 & 10: Auth guard — unauthenticated requests rejected ──────
    assert client.get(
        f"{API}/agents/{agent_id}/webapp-interface-config/"
    ).status_code in (401, 403)
    assert client.put(
        f"{API}/agents/{agent_id}/webapp-interface-config/",
        json={"show_header": False},
    ).status_code in (401, 403)

    # ── Phase 11 & 12: Ownership guard — other user denied ────────────────
    _, other_headers = create_random_user_with_headers(client)
    r = client.get(
        f"{API}/agents/{agent_id}/webapp-interface-config/",
        headers=other_headers,
    )
    assert r.status_code == 404

    r = client.put(
        f"{API}/agents/{agent_id}/webapp-interface-config/",
        headers=other_headers,
        json={"show_header": False},
    )
    assert r.status_code == 404

    # ── Phase 13: Non-existent agent → 404 ───────────────────────────────
    ghost_id = str(uuid.uuid4())
    r = client.get(
        f"{API}/agents/{ghost_id}/webapp-interface-config/",
        headers=superuser_token_headers,
    )
    assert r.status_code == 404

    r = client.put(
        f"{API}/agents/{ghost_id}/webapp-interface-config/",
        headers=superuser_token_headers,
        json={"show_header": False},
    )
    assert r.status_code == 404


def test_webapp_interface_config_chat_mode_values(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Verify chat_mode accepts valid values and transitions:
      1. Default is None (disabled)
      2. Set to "conversation" → persists
      3. Set to "building" → persists
      4. Set back to None → disables chat
    """
    agent = create_agent_via_api(client, superuser_token_headers, name="Chat Mode Agent")
    drain_tasks()
    agent_id = agent["id"]

    # Default
    config = get_webapp_interface_config(client, superuser_token_headers, agent_id)
    assert config["chat_mode"] is None

    # Set conversation
    updated = update_webapp_interface_config(
        client, superuser_token_headers, agent_id, chat_mode="conversation"
    )
    assert updated["chat_mode"] == "conversation"

    # Set building
    updated = update_webapp_interface_config(
        client, superuser_token_headers, agent_id, chat_mode="building"
    )
    assert updated["chat_mode"] == "building"

    # Disable
    updated = update_webapp_interface_config(
        client, superuser_token_headers, agent_id, chat_mode=None
    )
    assert updated["chat_mode"] is None


# ── B. Interface config in share info ─────────────────────────────────────


def test_webapp_interface_config_in_share_info(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Interface config is embedded in the webapp share info endpoint:
      1. Create agent with webapp_enabled, create a webapp share
      2. GET /webapp-share/{token}/info → has interface_config with defaults
      3. PUT config to change show_header=False, chat_mode="conversation"
      4. GET /webapp-share/{token}/info → interface_config reflects the change
      5. Inactive/invalid share info does not include interface_config
    """
    # ── Phase 1: Create agent with webapp share ───────────────────────────
    agent, share = setup_webapp_agent(
        client, superuser_token_headers,
        name="Share Info Config Agent",
        share_label="Config Test Share",
    )
    agent_id = agent["id"]
    token = share["token"]

    # ── Phase 2: Share info → defaults in interface_config ────────────────
    info = get_webapp_share_info(client, token)
    assert info["is_valid"] is True
    assert "interface_config" in info
    interface_config = info["interface_config"]
    assert interface_config["show_header"] is True
    assert interface_config["chat_mode"] is None

    # ── Phase 3: Update interface config ──────────────────────────────────
    update_webapp_interface_config(
        client, superuser_token_headers, agent_id,
        show_header=False,
        chat_mode="conversation",
    )

    # ── Phase 4: Share info → interface_config reflects change ────────────
    info = get_webapp_share_info(client, token)
    assert info["is_valid"] is True
    interface_config = info["interface_config"]
    assert interface_config["show_header"] is False
    assert interface_config["chat_mode"] == "conversation"

    # ── Phase 5: Inactive share → no interface_config in response ─────────
    r = client.patch(
        f"{API}/agents/{agent_id}/webapp-shares/{share['id']}",
        headers=superuser_token_headers,
        json={"is_active": False},
    )
    assert r.status_code == 200

    info_inactive = get_webapp_share_info(client, token)
    assert info_inactive["is_valid"] is False
    # interface_config is not present (or not meaningful) for invalid shares
    assert info_inactive.get("interface_config") is None
