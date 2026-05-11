"""End-to-end tests for the App Data tab.

Walks the lifecycle of a per-(user, bundle) ``AppDataVolume`` from auto-creation
through size recompute to wipe. The volume is created indirectly: when a new
agent is provisioned through the regular ``POST /agents/`` flow,
``EnvironmentLifecycleManager`` resolves an app-data path during compose
generation, which triggers ``AppDataService.get_or_create_volume``.

Phase 2 — agent delete now also flips ``is_orphaned=true`` (via
``AgentService.delete_agent``'s call to ``AppDataService.mark_orphaned``)
so the user can wipe right after uninstalling. Phase 1's separate
"orphan-then-wipe" two-step is no longer required.
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.user import (
    create_random_user,
    promote_to_developer,
    user_authentication_headers,
)


_BASE = f"{settings.API_V1_STR}/users/me/app-data"


def test_app_data_volume_lifecycle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """
    App-data lifecycle:
      1. Create agent → bundle_id surfaces on AgentPublic
      2. List app-data → volume auto-provisioned with the agent's bundle_id
      3. Recompute size → returns updated row
      4. Wipe while install attached → 409
      5. Delete agent → volume becomes orphaned (Phase 2)
      6. Wipe orphaned volume → 204 → listing empty
    """
    # ── Phase 1: Create agent ──────────────────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers, name="App Data Test")
    drain_tasks()
    fresh = client.get(
        f"{settings.API_V1_STR}/agents/{agent['id']}",
        headers=superuser_token_headers,
    ).json()

    assert fresh["bundle_id"], "bundle_id must be auto-generated on create"
    bundle_id = fresh["bundle_id"]
    # Bundle id format: <reversed-host>.<8-hex-suffix>
    assert "." in bundle_id and len(bundle_id) > 8

    # ── Phase 2: Volume auto-provisioned ───────────────────────────────
    r = client.get(_BASE, headers=superuser_token_headers)
    assert r.status_code == 200
    listing = r.json()
    matched = [v for v in listing["data"] if v["bundle_id"] == bundle_id]
    assert len(matched) == 1, f"Expected one volume for {bundle_id}, got: {listing}"
    volume = matched[0]
    assert volume["is_orphaned"] is False
    assert volume["current_install_id"] == fresh["id"]
    assert volume["current_install_name"] == fresh["name"]
    volume_id = volume["id"]

    # ── Phase 3: Recompute size ────────────────────────────────────────
    r = client.post(
        f"{_BASE}/{volume_id}/recompute-size",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200, r.text
    refreshed = r.json()
    assert refreshed["id"] == volume_id
    assert refreshed["last_size_check_at"] is not None

    # ── Phase 4: Wipe refused while attached (not orphaned) ────────────
    r = client.delete(f"{_BASE}/{volume_id}", headers=superuser_token_headers)
    assert r.status_code == 409
    assert "install" in r.json()["detail"].lower()

    # ── Phase 5: Agent delete → volume becomes orphaned (Phase 2) ──────
    # Phase 2: ``AgentService.delete_agent`` calls ``AppDataService.mark_orphaned``
    # before the row delete, so the user can wipe immediately afterwards.
    client.delete(
        f"{settings.API_V1_STR}/agents/{fresh['id']}",
        headers=superuser_token_headers,
    )
    drain_tasks()

    # Listing should still surface the volume — orphaned, not deleted.
    r = client.get(_BASE, headers=superuser_token_headers)
    found = next(
        (v for v in r.json()["data"] if v["id"] == volume_id), None
    )
    assert found is not None, "Orphaned volume should still appear in listing"
    assert found["is_orphaned"] is True
    assert found["current_install_id"] is None

    # ── Phase 6: Wipe orphaned volume succeeds ─────────────────────────
    r = client.delete(f"{_BASE}/{volume_id}", headers=superuser_token_headers)
    assert r.status_code == 204, r.text

    # Listing no longer contains the volume
    r = client.get(_BASE, headers=superuser_token_headers)
    remaining_ids = [v["id"] for v in r.json()["data"]]
    assert volume_id not in remaining_ids


def test_app_data_owner_isolation(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Ownership guards:
      1. User A creates agent → owns the auto-provisioned volume
      2. User B cannot see User A's volumes (each user gets only their own)
      3. User B cannot recompute / delete User A's volume — 404
    """
    # ── Phase 1: User A's agent ────────────────────────────────────────
    user_a = create_random_user(client)
    headers_a = user_authentication_headers(
        client=client, email=user_a["email"], password=user_a["_password"]
    )
    promote_to_developer(client, superuser_token_headers, user_a["id"])
    # Each user needs an AI credential to create an environment.
    create_random_ai_credential(
        client, headers_a, credential_type="anthropic",
        api_key="sk-ant-api03-test-A", set_default=True,
    )
    agent_a = create_agent_via_api(client, headers_a, name="A's Agent")
    drain_tasks()

    # Force volume creation by reading the agent (compose generation runs
    # at create time inside the env lifecycle manager).
    r = client.get(_BASE, headers=headers_a)
    a_volumes = r.json()["data"]
    assert len(a_volumes) >= 1
    a_volume_id = next(
        v["id"] for v in a_volumes if v["bundle_id"] == agent_a["bundle_id"]
    )

    # ── Phase 2: User B sees nothing ──────────────────────────────────
    user_b = create_random_user(client)
    headers_b = user_authentication_headers(
        client=client, email=user_b["email"], password=user_b["_password"]
    )
    r = client.get(_BASE, headers=headers_b)
    assert r.status_code == 200
    b_volumes = r.json()["data"]
    assert all(v["id"] != a_volume_id for v in b_volumes)

    # ── Phase 3: User B's mutations are 404 ───────────────────────────
    assert client.post(
        f"{_BASE}/{a_volume_id}/recompute-size", headers=headers_b
    ).status_code == 404
    assert client.delete(f"{_BASE}/{a_volume_id}", headers=headers_b).status_code == 404

    # Ghost id is also 404
    ghost = uuid.uuid4()
    assert client.delete(f"{_BASE}/{ghost}", headers=headers_a).status_code == 404


def test_bundle_id_unique_per_agent(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Bundle ids derive from the agent UUID so two fresh agents differ."""
    a1 = create_agent_via_api(client, superuser_token_headers, name="Bundle A")
    a2 = create_agent_via_api(client, superuser_token_headers, name="Bundle B")
    drain_tasks()
    assert a1["bundle_id"] and a2["bundle_id"]
    assert a1["bundle_id"] != a2["bundle_id"]
