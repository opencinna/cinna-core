"""
Integration tests for component references: ``POST /dashboards/component-refs/resolve``.

``cinna://component/bundle:<bundle_id>/[<skill>/]<name>`` becomes a card. The
env's component list is stubbed (persistent adapter); rendering never wakes an
environment.

Test scenarios:
  1. bundle ref resolves to the viewer's install, with skill and placements
  2. Chat agent wins among installs; without a chat the two installs are ambiguous
  3. A catalog-visible bundle the viewer has not installed is not_installed
  4. env_asleep for a stopped environment, with nothing woken or listed
  5. invalid / gone / id: and slug refs without a matching chat agent
  6. At most 20 refs per call; results keep the order
"""
import uuid

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import (
    install_bundle,
    make_user_and_headers,
    publish_bundle_and_make_public,
)
from tests.utils.dashboard import create_dashboard
from tests.utils.dashboard_components import (  # noqa: F401 — patch_environment_adapter is an autouse override
    add_component_block,
    make_component,
    open_chat,
    patch_environment_adapter,
    setup_component_agent,
)
from tests.utils.environment import set_environment_status

API = settings.API_V1_STR
URL = f"{API}/dashboards/component-refs/resolve"


def _ref(bundle_id: str, name: str = "sales", skill: str | None = None) -> str:
    return f"cinna://component/bundle:{bundle_id}/{skill + '/' if skill else ''}{name}"


def _resolve(client, headers, refs, session_id=None, expect=200):
    body: dict = {"refs": refs}
    if session_id:
        body["session_id"] = session_id
    r = client.post(URL, headers=headers, json=body)
    assert r.status_code == expect, r.text
    return r.json()["results"] if expect == 200 else r.json()


def _published_agent(client, db, headers, adapter, components):
    """A component agent published as a public bundle (the publisher is its first install)."""
    agent = setup_component_agent(client, db, headers, adapter, components)
    publish_bundle_and_make_public(client, headers, agent["id"])
    return client.get(f"{API}/agents/{agent['id']}", headers=headers).json()


# ── Scenario 1: bundle resolve ──────────────────────────────────────────────

def test_bundle_ref_resolves_to_the_viewers_agent_with_skill_and_placements(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    adapter = patch_environment_adapter._test_adapter
    h = superuser_token_headers
    comps = [
        make_component("sales", view="table"),
        make_component("report", skill="reporting", view="list", title="Weekly"),
    ]
    agent = _published_agent(client, db, h, adapter, comps)
    bid = agent["bundle_id"]
    dash = create_dashboard(client, h, name="Ops board")
    assert add_component_block(client, h, dash["id"], agent["id"], "sales").status_code == 200

    plain, skilled = _resolve(client, h, [_ref(bid), _ref(bid, "report", "reporting")])

    assert plain["status"] == "ok" and plain.get("message") is None
    assert plain["agent"] == {"id": agent["id"], "name": agent["name"]}
    assert plain["component"]["name"] == "sales" and plain["component"]["view"] == "table"
    assert plain["component"]["title"] == "Sales" and plain["from_skill"] is None
    assert plain["findings"] == []
    assert plain["placements"] == [{"dashboard_id": dash["id"], "name": "Ops board", "origin": "web"}]

    assert skilled["status"] == "ok"
    assert skilled["from_skill"] == "reporting" and skilled["component"]["skill"] == "reporting"
    assert skilled["placements"] == []

    # A component the env no longer has is gone; a skill mismatch too
    gone = _resolve(client, h, [_ref(bid, "missing"), _ref(bid, "sales", "reporting")])
    assert [g["status"] for g in gone] == ["gone", "gone"]
    assert all(g["message"] for g in gone)


# ── Scenario 2: chat agent wins, ambiguity ──────────────────────────────────

def test_chat_agent_wins_among_installs_and_two_installs_without_chat_are_ambiguous(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    adapter = patch_environment_adapter._test_adapter
    h = superuser_token_headers
    first = _published_agent(client, db, h, adapter, [make_component("sales")])
    bid = first["bundle_id"]

    # One install: no chat needed
    [res] = _resolve(client, h, [_ref(bid)])
    assert res["status"] == "ok" and res["agent"]["id"] == first["id"]

    # The viewer installs the bundle again: a second agent with the same bundle
    second = install_bundle(client, h, bid)
    agent2 = client.get(f"{API}/agents/{second['id']}", headers=h).json()
    assert agent2["bundle_id"] == bid and agent2["id"] != first["id"]
    set_environment_status(db, agent2["active_environment_id"], "running")

    [res] = _resolve(client, h, [_ref(bid)])
    assert res["status"] == "ambiguous" and res["message"]
    assert "component" not in res or res["component"] is None

    dash = create_dashboard(client, h)
    chat1 = open_chat(client, h, dash["id"], first["id"])
    chat2 = open_chat(client, h, dash["id"], agent2["id"])
    [res1] = _resolve(client, h, [_ref(bid)], session_id=chat1["id"])
    [res2] = _resolve(client, h, [_ref(bid)], session_id=chat2["id"])
    assert res1["status"] == "ok" and res1["agent"]["id"] == first["id"]
    assert res2["status"] == "ok" and res2["agent"]["id"] == agent2["id"]


# ── Scenario 3: not installed ───────────────────────────────────────────────

def test_catalog_visible_bundle_the_viewer_lacks_is_not_installed_with_its_name(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    adapter = patch_environment_adapter._test_adapter
    publisher_h = superuser_token_headers
    agent = _published_agent(client, db, publisher_h, adapter, [make_component("sales")])
    bid = agent["bundle_id"]
    _, viewer_h = make_user_and_headers(client)

    [res] = _resolve(client, viewer_h, [_ref(bid)])
    assert res["status"] == "not_installed"
    assert res["catalog_name"]
    assert res["message"] and res.get("agent") is None

    # Not in the catalog at all: gone, with no name leaked
    [res] = _resolve(client, viewer_h, [_ref("no.such-bundle.x1")])
    assert res["status"] == "gone" and res.get("catalog_name") is None

    # Once installed it resolves for the viewer
    installed = install_bundle(client, viewer_h, bid)
    env_id = client.get(f"{API}/agents/{installed['id']}", headers=viewer_h).json()["active_environment_id"]
    set_environment_status(db, env_id, "running")
    [res] = _resolve(client, viewer_h, [_ref(bid)])
    assert res["status"] == "ok" and res["agent"]["id"] == installed["id"]


# ── Scenario 4: env asleep ──────────────────────────────────────────────────

def test_stopped_environment_is_env_asleep_and_nothing_is_woken_or_listed(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    adapter = patch_environment_adapter._test_adapter
    h = superuser_token_headers
    agent = _published_agent(client, db, h, adapter, [make_component("sales")])
    bid = agent["bundle_id"]
    set_environment_status(db, agent["active_environment_id"], "suspended")

    listed: list[int] = []
    original = adapter.list_dashboard_components

    async def counting():
        listed.append(1)
        return await original()

    adapter.list_dashboard_components = counting
    try:
        [res] = _resolve(client, h, [_ref(bid)])
    finally:
        adapter.list_dashboard_components = original

    assert res["status"] == "env_asleep" and res["message"]
    assert res["agent"] == {"id": agent["id"], "name": agent["name"]}
    assert listed == []
    # Still asleep afterwards: nothing activated it
    from app.models.environments.environment import AgentEnvironment

    db.expire_all()
    assert db.get(AgentEnvironment, uuid.UUID(agent["active_environment_id"])).status == "suspended"


# ── Scenario 5: invalid, id: and slug refs ──────────────────────────────────

def test_malformed_refs_are_invalid_and_id_or_slug_refs_need_a_matching_chat_agent(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    adapter = patch_environment_adapter._test_adapter
    h = superuser_token_headers
    agent = _published_agent(client, db, h, adapter, [make_component("sales")])
    dash = create_dashboard(client, h)
    chat = open_chat(client, h, dash["id"], agent["id"])

    refs = [
        "https://example.com/x",
        "cinna://component/bundle:abc",             # no name
        "cinna://component/bundle:abc/Bad_Name",    # name grammar
        "cinna://component/a/b/c/d",                # too many parts
        f"cinna://component/id:{uuid.uuid4()}/sales",
        "cinna://component/some-slug/sales",
    ]
    results = _resolve(client, h, refs)
    assert [r["ref"] for r in results] == refs
    assert [r["status"] for r in results[:4]] == ["invalid"] * 4
    # No chat agent: id: / slug cannot match anything
    assert [r["status"] for r in results[4:]] == ["gone", "gone"]

    # With a chat agent whose workspace has no matching manifest they are still gone
    results = _resolve(client, h, refs[4:], session_id=chat["id"])
    assert [r["status"] for r in results] == ["gone", "gone"]


# ── Scenario 6: the 20-ref cap ──────────────────────────────────────────────

def test_at_most_20_refs_per_call_and_order_is_kept(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    adapter = patch_environment_adapter._test_adapter
    h = superuser_token_headers
    agent = _published_agent(client, db, h, adapter, [make_component("sales")])
    bid = agent["bundle_id"]

    refs = [_ref(bid) if i % 2 == 0 else f"nonsense-{i}" for i in range(20)]
    results = _resolve(client, h, refs)
    assert [r["ref"] for r in results] == refs
    assert [r["status"] for r in results] == ["ok" if i % 2 == 0 else "invalid" for i in range(20)]

    _resolve(client, h, refs + [_ref(bid)], expect=422)
    assert _resolve(client, h, []) == []
    # Unauthenticated
    assert client.post(URL, json={"refs": []}).status_code in (401, 403)
