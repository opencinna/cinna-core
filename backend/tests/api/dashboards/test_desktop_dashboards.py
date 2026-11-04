"""
Integration tests for desktop-owned dashboards (Dashboards D6).

Cinna Desktop backs a desktop dashboard's cloud tiles with a core dashboard
created with ``origin: "desktop"``. Such a dashboard is left out of the web
list by default, has its own per-user limit, and otherwise behaves exactly
like a web dashboard.

Test scenarios:
  1. origin: default web, desktop on create, never changed by update; the web
     list omits desktop dashboards unless ?origin=desktop|all; GET by id works
  2. Limits: desktop dashboards do not count toward the 10 web ones and stop
     at 50; the 20-block limit applies to them
  3. End to end on a desktop dashboard: add block -> data -> refresh -> params
     -> the agent's tile tool sees the block and its params; placements say
     desktop
"""
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from app.services.users.user_dashboard_service import (
    MAX_BLOCKS_PER_DASHBOARD,
    MAX_DASHBOARDS_PER_USER,
    MAX_DESKTOP_DASHBOARDS_PER_USER,
)
from tests.utils.agent import create_agent_via_api
from tests.utils.agent_env import create_env_with_token
from tests.utils.background_tasks import drain_tasks
from tests.utils.dashboard import (
    add_block,
    create_dashboard,
    get_dashboard,
    list_dashboards,
    update_dashboard,
)
from tests.utils.dashboard_components import (  # noqa: F401 — patch_environment_adapter is an autouse override
    add_component_block,
    make_component,
    open_chat,
    patch_environment_adapter,
    setup_component_agent,
)
from tests.utils.user import create_random_user_with_headers

API = settings.API_V1_STR

_PARAMS = {
    "region": {"type": "enum", "values": ["eu", "us", "apac"], "default": "eu"},
    "limit": {"type": "number", "default": 10},
}


def _ids(dashboards: list[dict]) -> set[str]:
    return {d["id"] for d in dashboards}


# ── Scenario 1: origin and the web list ─────────────────────────────────────

def test_origin_is_set_on_create_and_desktop_dashboards_are_hidden_from_the_web_list(
    client: TestClient,
) -> None:
    _, h = create_random_user_with_headers(client)
    web = create_dashboard(client, h, name="Ops")
    desktop = create_dashboard(client, h, name="Desk", origin="desktop")
    assert web["origin"] == "web"
    assert desktop["origin"] == "desktop"

    # The web list (default) has the web one only
    assert _ids(list_dashboards(client, h)) == {web["id"]}
    assert _ids(list_dashboards(client, h, origin="web")) == {web["id"]}
    assert _ids(list_dashboards(client, h, origin="desktop")) == {desktop["id"]}
    both = list_dashboards(client, h, origin="all")
    assert _ids(both) == {web["id"], desktop["id"]}
    assert {d["id"]: d["origin"] for d in both} == {web["id"]: "web", desktop["id"]: "desktop"}
    r = client.get(f"{API}/dashboards/", headers=h, params={"origin": "phone"})
    assert r.status_code == 422

    # Direct GET works for the owner, and only for the owner
    assert get_dashboard(client, h, desktop["id"])["origin"] == "desktop"
    _, other_h = create_random_user_with_headers(client)
    assert client.get(f"{API}/dashboards/{desktop['id']}", headers=other_h).status_code == 403
    assert _ids(list_dashboards(client, other_h, origin="all")) == set()

    # An update never changes the origin (rename is what the desktop does)
    renamed = update_dashboard(client, h, desktop["id"], name="Desk 2", origin="web")
    assert renamed["name"] == "Desk 2" and renamed["origin"] == "desktop"

    # Unknown origin on create is refused
    r = client.post(f"{API}/dashboards/", headers=h, json={"name": "x", "origin": "phone"})
    assert r.status_code == 422


# ── Scenario 2: limits ──────────────────────────────────────────────────────

def test_desktop_dashboards_have_their_own_limit(client: TestClient) -> None:
    _, h = create_random_user_with_headers(client)
    for i in range(MAX_DASHBOARDS_PER_USER):
        create_dashboard(client, h, name=f"Web {i}")
    r = client.post(f"{API}/dashboards/", headers=h, json={"name": "Web extra"})
    assert r.status_code == 409

    # The web limit is full; desktop ones still fit, up to their own bound
    for i in range(MAX_DESKTOP_DASHBOARDS_PER_USER):
        create_dashboard(client, h, name=f"Desk {i}", origin="desktop")
    r = client.post(f"{API}/dashboards/", headers=h, json={"name": "Desk extra", "origin": "desktop"})
    assert r.status_code == 409
    assert str(MAX_DESKTOP_DASHBOARDS_PER_USER) in r.json()["detail"]

    assert len(list_dashboards(client, h)) == MAX_DASHBOARDS_PER_USER
    assert len(list_dashboards(client, h, origin="desktop")) == MAX_DESKTOP_DASHBOARDS_PER_USER


def test_block_limit_applies_to_a_desktop_dashboard(
    client: TestClient, superuser_token_headers: dict
) -> None:
    h = superuser_token_headers
    agent = create_agent_via_api(client, h)
    drain_tasks()
    desk = create_dashboard(client, h, origin="desktop")
    # Grid fields mean nothing here: every block at 0/0, overlapping
    for _ in range(MAX_BLOCKS_PER_DASHBOARD):
        add_block(client, h, desk["id"], agent["id"], grid_x=0, grid_y=0, grid_w=4, grid_h=3)
    r = client.post(
        f"{API}/dashboards/{desk['id']}/blocks",
        headers=h,
        json={"agent_id": agent["id"], "view_type": "latest_session"},
    )
    assert r.status_code == 409


# ── Scenario 3: end to end on a desktop dashboard ───────────────────────────

def test_component_block_on_a_desktop_dashboard_works_end_to_end(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    h = superuser_token_headers
    adapter = patch_environment_adapter._test_adapter
    adapter.dashboard_run_results = {("sales", "get_data"): {"ok": True, "value": {"rows": [{"a": 1}]}}}
    agent = setup_component_agent(client, db, h, adapter, [make_component("sales", params=_PARAMS)])
    desk = create_dashboard(client, h, name="My desktop board", origin="desktop")

    # Add block: the desktop sends grid 0/0 and a size
    r = add_component_block(client, h, desk["id"], agent["id"], "sales", params={"limit": 3})
    assert r.status_code == 200, r.text
    block = r.json()
    base = f"{API}/dashboards/{desk['id']}/blocks/{block['id']}/component"

    # data: nothing cached yet, nothing ran
    r = client.get(f"{base}/data", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["data"] is None and r.json()["stale"] is True
    assert getattr(adapter, "dashboard_run_calls", []) == []

    # open computes; data then serves the cache
    r = client.post(f"{base}/refresh", headers=h, json={"reason": "open"})
    assert r.status_code == 200, r.text
    assert r.json()["data"] == {"rows": [{"a": 1}]}
    assert client.get(f"{base}/data", headers=h).json()["data"] == {"rows": [{"a": 1}]}

    # params: whole set, recomputed
    r = client.put(f"{base}/params", headers=h, json={"params": {"region": "us"}})
    assert r.status_code == 200, r.text
    assert r.json()["params_resolved"] == {"region": "us", "limit": 10}

    # The agent's tile tool, from this dashboard's chat, sees the block and its params
    chat = open_chat(client, h, desk["id"], agent["id"])
    assert chat["dashboard_id"] == desk["id"]
    me = client.get(f"{API}/users/me", headers=h).json()
    _, env_h = create_env_with_token(db, agent_id=agent["id"], owner_id=me["id"])
    r = client.post(f"{API}/agent/dashboards/tiles", headers=env_h, json={"session_id": chat["id"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dashboard"] == {"id": desk["id"], "name": "My desktop board"}
    assert [(t["tile_id"], t["params"]) for t in body["tiles"]] == [
        (block["id"], {"region": "us", "limit": 10})
    ]

    # Placements name the desktop dashboard and say where it lives
    r = client.get(f"{API}/agents/{agent['id']}/dashboards/", headers=h)
    assert r.status_code == 200, r.text
    sales = next(c for c in r.json()["components"] if c["name"] == "sales")
    assert sales["placements"] == [{
        "dashboard_id": desk["id"],
        "dashboard_name": "My desktop board",
        "block_id": block["id"],
        "origin": "desktop",
    }]

    # It is still not on the web list
    assert desk["id"] not in _ids(list_dashboards(client, h))
