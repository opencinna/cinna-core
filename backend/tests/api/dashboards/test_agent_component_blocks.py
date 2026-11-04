"""
Integration tests for agent_component dashboard blocks and their owner routes.

Test scenarios:
  1. Block creation — valid config stored, validation failures, ownership
  2. Tile data lifecycle — empty cache, open fills it, fresh open does not rerun
  3. Failure keeps cached data and records a mark; success clears it
  4. needs_credentials — nothing runs, missing names reported
  5. interval refresh with a sleeping environment — skipped, no wake
  6. Schedule-fed tile recomputes once per settled schedule run
  7. Page calls and frames (html view vs native view)
  8. adapter_unsupported mapping (pre-feature container)
  9. Agent-scoped routes — list, get, run, frame
 10. Ownership — another user's agent, dashboard and block are 404
 11. Page-state PUT
"""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.utils.dashboard import create_dashboard
from tests.utils.dashboard_components import (  # noqa: F401 — patch_environment_adapter is an autouse override
    add_component_block,
    create_component_tile,
    make_component,
    patch_environment_adapter,
    setup_component_agent,
)
from tests.utils.environment import get_environment
from tests.utils.schedule import create_schedule, run_schedule_now
from tests.utils.background_tasks import drain_tasks
from tests.utils.user import create_random_user_with_headers

API = settings.API_V1_STR


def _adapter(lm):
    return lm._test_adapter


def _err(r) -> dict:
    return r.json()["error"]


# ── Scenario 1: block creation ──────────────────────────────────────────────

def test_create_component_block_stores_config(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    adapter = _adapter(patch_environment_adapter)
    comp = make_component(params={"region": {"type": "enum", "values": ["eu", "us"], "default": "eu"}})
    agent = setup_component_agent(client, db, superuser_token_headers, adapter, [comp])
    dashboard = create_dashboard(client, superuser_token_headers)

    r = add_component_block(
        client, superuser_token_headers, dashboard["id"], agent["id"], "sales",
        params={"region": "us"}, refresh={"max_age": "5m", "while_open_every": "manual"},
    )
    assert r.status_code == 200, r.text
    block = r.json()
    assert block["view_type"] == "agent_component"
    assert block["config"]["component"] == "sales"
    assert block["config"]["params"] == {"region": "us"}
    assert block["config"]["refresh"]["max_age"] == "5m"

    fetched = client.get(f"{API}/dashboards/{dashboard['id']}", headers=superuser_token_headers).json()
    assert [b["id"] for b in fetched["blocks"]] == [block["id"]]


def test_create_component_block_rejects_bad_input(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    adapter = _adapter(patch_environment_adapter)
    comp = make_component(params={"region": {"type": "enum", "values": ["eu", "us"]}})
    agent = setup_component_agent(client, db, superuser_token_headers, adapter, [comp])
    dashboard = create_dashboard(client, superuser_token_headers)
    h = superuser_token_headers

    # Component the agent does not have
    r = add_component_block(client, h, dashboard["id"], agent["id"], "missing")
    assert r.status_code == 400, r.text
    # Param value that does not fit the declared enum
    r = add_component_block(client, h, dashboard["id"], agent["id"], "sales", params={"region": "mars"})
    assert r.status_code == 400, r.text
    # Undeclared param
    r = add_component_block(client, h, dashboard["id"], agent["id"], "sales", params={"nope": 1})
    assert r.status_code == 400, r.text
    # Bad component name pattern / unknown config key
    r = add_component_block(client, h, dashboard["id"], agent["id"], "../etc")
    assert r.status_code in (400, 422), r.text
    r = client.post(
        f"{API}/dashboards/{dashboard['id']}/blocks", headers=h,
        json={"agent_id": agent["id"], "view_type": "agent_component",
              "config": {"component": "sales", "bogus": 1}},
    )
    assert r.status_code in (400, 422), r.text
    # Nothing was stored
    fetched = client.get(f"{API}/dashboards/{dashboard['id']}", headers=h).json()
    assert fetched["blocks"] == []


def test_create_component_block_with_sleeping_environment_is_refused(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    adapter = _adapter(patch_environment_adapter)
    agent = setup_component_agent(
        client, db, superuser_token_headers, adapter, [make_component()], env_status="suspended"
    )
    dashboard = create_dashboard(client, superuser_token_headers)
    r = add_component_block(client, superuser_token_headers, dashboard["id"], agent["id"])
    assert r.status_code == 400, r.text


def test_create_component_block_on_another_users_agent_is_refused(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    adapter = _adapter(patch_environment_adapter)
    agent = setup_component_agent(client, db, superuser_token_headers, adapter, [make_component()])
    _, other_headers = create_random_user_with_headers(client)
    dashboard = create_dashboard(client, other_headers)
    r = add_component_block(client, other_headers, dashboard["id"], agent["id"])
    assert r.status_code in (400, 403, 404), r.text


def test_update_component_block_rechecks_config(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    adapter = _adapter(patch_environment_adapter)
    t = create_component_tile(client, db, superuser_token_headers, adapter)
    h = superuser_token_headers
    url = f"{API}/dashboards/{t['dashboard']['id']}/blocks/{t['block']['id']}"
    r = client.put(url, headers=h, json={"config": {"component": "gone", "params": {}}})
    assert r.status_code == 400, r.text
    r = client.put(url, headers=h, json={"config": {"component": "sales", "params": {}, "refresh": {"max_age": "1h"}}})
    assert r.status_code == 200, r.text
    assert r.json()["config"]["refresh"]["max_age"] == "1h"


# ── Scenario 2: tile data lifecycle ─────────────────────────────────────────

def test_tile_cache_served_stale_then_fresh_and_open_does_not_rerun(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    adapter = _adapter(patch_environment_adapter)
    adapter.dashboard_run_results = {("sales", "get_data"): {"ok": True, "value": {"markdown": "v1"}}}
    t = create_component_tile(client, db, superuser_token_headers, adapter)
    h = superuser_token_headers

    # Nothing computed yet: empty, stale, nothing ran (data never wakes or runs)
    r = client.get(f"{t['base']}/data", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["data"] is None and r.json()["stale"] is True
    assert getattr(adapter, "dashboard_run_calls", []) == []

    # open: computes
    r = client.post(f"{t['base']}/refresh", headers=h, json={"reason": "open"})
    assert r.status_code == 200, r.text
    assert r.json()["data"] == {"markdown": "v1"}
    assert r.json()["stale"] is False and r.json()["fetched_at"] is not None
    assert len(adapter.dashboard_run_calls) == 1

    # open / reshow while fresh: served from cache, no run
    for reason in ("open", "reshow"):
        r = client.post(f"{t['base']}/refresh", headers=h, json={"reason": reason})
        assert r.status_code == 200 and r.json()["stale"] is False
    assert len(adapter.dashboard_run_calls) == 1

    # data GET returns the cache
    assert client.get(f"{t['base']}/data", headers=h).json()["data"] == {"markdown": "v1"}

    # manual / after_turn always recompute
    adapter.dashboard_run_results = {("sales", "get_data"): {"ok": True, "value": {"markdown": "v2"}}}
    r = client.post(f"{t['base']}/refresh", headers=h, json={"reason": "manual"})
    assert r.json()["data"] == {"markdown": "v2"}
    client.post(f"{t['base']}/refresh", headers=h, json={"reason": "after_turn"})
    assert len(adapter.dashboard_run_calls) == 3

    # Unknown reason is rejected
    r = client.post(f"{t['base']}/refresh", headers=h, json={"reason": "bogus"})
    assert r.status_code == 422


def test_file_source_is_read_without_running_an_endpoint(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    adapter = _adapter(patch_environment_adapter)
    adapter.dashboard_source_files = {("report", None): ("text/csv", b"a,b\n1,2\n3,4\n")}
    comp = make_component("report", source={"file": "data/report.csv"}, endpoints={})
    t = create_component_tile(client, db, superuser_token_headers, adapter, [comp])
    r = client.post(f"{t['base']}/refresh", headers=superuser_token_headers, json={"reason": "manual"})
    assert r.status_code == 200, r.text
    assert r.json()["data"] == {
        "columns": [{"key": "a"}, {"key": "b"}],
        "rows": [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}],
    }
    assert getattr(adapter, "dashboard_run_calls", []) == []


# ── Scenario 3: failure keeps data plus mark ────────────────────────────────

def test_failed_run_keeps_cached_data_and_marks_then_success_clears(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    adapter = _adapter(patch_environment_adapter)
    state = {"fail": False}

    def result(_params):
        if state["fail"]:
            return {"ok": False, "code": "call_failed", "message": "upstream\nexploded"}
        return {"ok": True, "value": {"markdown": "good"}}

    adapter.dashboard_run_results = {("sales", "get_data"): result}
    t = create_component_tile(client, db, superuser_token_headers, adapter)
    h = superuser_token_headers
    client.post(f"{t['base']}/refresh", headers=h, json={"reason": "manual"})

    state["fail"] = True
    r = client.post(f"{t['base']}/refresh", headers=h, json={"reason": "manual"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["data"] == {"markdown": "good"}
    assert body["mark"] == "upstream exploded"
    assert body["mark_at"] is not None

    # The mark survives a plain read
    assert client.get(f"{t['base']}/data", headers=h).json()["mark"] == "upstream exploded"

    state["fail"] = False
    body = client.post(f"{t['base']}/refresh", headers=h, json={"reason": "manual"}).json()
    assert body["mark"] is None and body["data"] == {"markdown": "good"}


# ── Scenario 4: credentials ─────────────────────────────────────────────────

def test_needs_credentials_runs_nothing(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    adapter = _adapter(patch_environment_adapter)
    comp = make_component(requires_credentials=["Stripe"])
    t = create_component_tile(client, db, superuser_token_headers, adapter, [comp])
    h = superuser_token_headers

    r = client.post(f"{t['base']}/refresh", headers=h, json={"reason": "manual"})
    assert r.status_code == 409, r.text
    assert _err(r)["code"] == "needs_credentials"
    assert "Stripe" in _err(r)["message"]

    r = client.post(f"{t['base']}/call", headers=h, json={"endpoint": "lookup", "params": {}})
    assert r.status_code == 409 and _err(r)["code"] == "needs_credentials"

    r = client.post(
        f"{API}/agents/{t['agent']['id']}/dashboards/sales/run", headers=h,
        json={"endpoint": "lookup", "params": {}},
    )
    assert r.status_code == 409 and _err(r)["code"] == "needs_credentials"

    assert getattr(adapter, "dashboard_run_calls", []) == []
    data = client.get(f"{t['base']}/data", headers=h).json()
    assert data["missing_credentials"] == ["Stripe"]


# ── Scenario 5: interval with a sleeping environment ────────────────────────

def test_interval_refresh_with_sleeping_environment_is_skipped_and_does_not_wake(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    from tests.utils.environment import set_environment_status

    adapter = _adapter(patch_environment_adapter)
    t = create_component_tile(client, db, superuser_token_headers, adapter)
    h = superuser_token_headers
    env_id = t["agent"]["active_environment_id"]
    set_environment_status(db, env_id, "suspended")
    start_calls = adapter.start_calls

    r = client.post(f"{t['base']}/refresh", headers=h, json={"reason": "interval"})
    assert r.status_code == 200, r.text
    assert r.json()["skipped"] == "env_asleep"
    assert getattr(adapter, "dashboard_run_calls", []) == []
    assert get_environment(client, h, env_id)["status"] == "suspended"
    assert adapter.start_calls == start_calls

    # A cached read never wakes it either
    assert client.get(f"{t['base']}/data", headers=h).status_code == 200
    assert get_environment(client, h, env_id)["status"] == "suspended"

    # A manual refresh asks the environment to wake and tells the page to retry
    r = client.post(f"{t['base']}/refresh", headers=h, json={"reason": "manual"})
    assert r.status_code == 503, r.text
    assert _err(r)["code"] == "env_unavailable"
    assert getattr(adapter, "dashboard_run_calls", []) == []


# ── Scenario 6: schedule-fed tile ───────────────────────────────────────────

def test_schedule_fed_tile_recomputes_once_per_settled_schedule_run(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    adapter = _adapter(patch_environment_adapter)
    h = superuser_token_headers
    comp = make_component(refresh={"schedule": "nightly"})
    t = create_component_tile(client, db, h, adapter, [comp])
    agent_id = t["agent"]["id"]
    # Fill the cache first so only the schedule can make it recompute
    client.post(f"{t['base']}/refresh", headers=h, json={"reason": "manual"})
    runs_before = len(adapter.dashboard_run_calls)

    # No schedule run yet: reads do not recompute
    client.get(f"{t['base']}/data", headers=h)
    assert len(adapter.dashboard_run_calls) == runs_before

    schedule = create_schedule(client, h, agent_id, name="nightly")
    run_schedule_now(client, h, agent_id, schedule["id"])
    drain_tasks()

    client.get(f"{t['base']}/data", headers=h)
    after_first = len(adapter.dashboard_run_calls)
    assert after_first == runs_before + 1
    # Same settled run: not again
    client.get(f"{t['base']}/data", headers=h)
    client.get(f"{t['base']}/data", headers=h)
    assert len(adapter.dashboard_run_calls) == after_first


# ── Scenario 7: page call and frame ─────────────────────────────────────────

def test_call_runs_endpoint_and_unknown_endpoint_is_404(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    adapter = _adapter(patch_environment_adapter)
    adapter.dashboard_run_results = {("sales", "lookup"): lambda p: {"ok": True, "value": {"echo": p}}}
    t = create_component_tile(client, db, superuser_token_headers, adapter)
    h = superuser_token_headers

    r = client.post(f"{t['base']}/call", headers=h, json={"endpoint": "lookup", "params": {"q": "abc"}})
    assert r.status_code == 200, r.text
    assert r.json()["value"] == {"echo": {"q": "abc"}}
    assert adapter.dashboard_run_calls[-1]["endpoint"] == "lookup"

    r = client.post(f"{t['base']}/call", headers=h, json={"endpoint": "nope", "params": {}})
    assert r.status_code == 404 and _err(r)["code"] == "not_found"

    # The page never caches a call
    assert client.get(f"{t['base']}/data", headers=h).json()["data"] is None


def test_endpoint_result_failure_maps_to_call_failed_502(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    adapter = _adapter(patch_environment_adapter)
    adapter.dashboard_run_results = {
        ("sales", "lookup"): {"ok": False, "code": "boom", "message": "it broke"},
    }
    t = create_component_tile(client, db, superuser_token_headers, adapter)
    r = client.post(
        f"{t['base']}/call", headers=superuser_token_headers, json={"endpoint": "lookup", "params": {}}
    )
    assert r.status_code == 502, r.text
    assert _err(r) == {"code": "call_failed", "message": "it broke"}


def test_frame_for_html_view_returns_url_and_native_view_is_refused(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    adapter = _adapter(patch_environment_adapter)
    h = superuser_token_headers
    html = make_component("page", view="html", entry="index.html", endpoints={})
    t = create_component_tile(client, db, h, adapter, [html])
    r = client.post(f"{t['base']}/frame", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["frame_url"] and r.json()["expires_at"]
    assert "index.html" in r.json()["frame_url"]

    native = make_component("grid", view="table")
    t2 = create_component_tile(client, db, h, adapter, [native])
    r = client.post(f"{t2['base']}/frame", headers=h)
    assert r.status_code == 400 and _err(r)["code"] == "invalid_input"


# ── Scenario 8: adapter_unsupported ─────────────────────────────────────────

def test_pre_feature_container_maps_to_adapter_unsupported(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    adapter = _adapter(patch_environment_adapter)
    h = superuser_token_headers
    # Block exists, then the container turns out not to have the run route
    t = create_component_tile(client, db, h, adapter)
    adapter.unsupported_endpoints.add("/dashboards/run")
    r = client.post(f"{t['base']}/refresh", headers=h, json={"reason": "manual"})
    assert r.status_code == 409, r.text
    assert _err(r)["code"] == "adapter_unsupported"
    assert "Rebuild the environment" in _err(r)["message"]
    r = client.post(f"{t['base']}/call", headers=h, json={"endpoint": "lookup", "params": {}})
    assert r.status_code == 409 and _err(r)["code"] == "adapter_unsupported"

    # A container without the list route: a fresh environment, nothing cached
    adapter.unsupported_endpoints.add("/dashboards/components")
    agent = setup_component_agent(client, db, h, adapter, [make_component()])
    r = client.get(f"{API}/agents/{agent['id']}/dashboards/", headers=h)
    assert r.status_code == 409, r.text
    assert _err(r)["code"] == "adapter_unsupported"
    dashboard = create_dashboard(client, h)
    r = add_component_block(client, h, dashboard["id"], agent["id"])
    assert r.status_code == 400, r.text


# ── Scenario 9: agent-scoped routes ─────────────────────────────────────────

def test_agent_scoped_list_get_run_and_frame(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    adapter = _adapter(patch_environment_adapter)
    h = superuser_token_headers
    adapter.dashboard_run_results = {("sales", "get_data"): {"ok": True, "value": {"markdown": "preview"}}}
    comps = [make_component("sales"), make_component("page", view="html", endpoints={}, entry="main.html")]
    t = create_component_tile(client, db, h, adapter, comps)
    agent_id = t["agent"]["id"]
    base = f"{API}/agents/{agent_id}/dashboards"

    # list: both components, the placed one carries its placement
    r = client.get(f"{base}/", headers=h)
    assert r.status_code == 200, r.text
    by_name = {c["name"]: c for c in r.json()["components"]}
    assert set(by_name) == {"sales", "page"}
    assert by_name["sales"]["title"] == "Sales" and by_name["sales"]["view"] == "table"
    assert by_name["sales"]["has_errors"] is False
    assert by_name["sales"]["placements"] == [{
        "dashboard_id": t["dashboard"]["id"],
        "dashboard_name": t["dashboard"]["name"],
        "block_id": t["block"]["id"],
        "origin": "web",
    }]
    assert by_name["page"]["placements"] == []

    # get
    r = client.get(f"{base}/sales", headers=h)
    assert r.status_code == 200 and r.json()["component_json"]["title"] == "Sales"
    assert client.get(f"{base}/missing", headers=h).status_code == 404
    assert client.get(f"{base}/sales", headers=h, params={"skill": "other"}).status_code == 404

    # run: source with empty params
    r = client.post(f"{base}/sales/run", headers=h, json={"params": {}})
    assert r.status_code == 200, r.text
    assert r.json()["value"] == {"markdown": "preview"}
    assert r.json()["params_resolved"] == {} and r.json()["fetched_at"]

    # run: unknown endpoint, bad tile key
    r = client.post(f"{base}/sales/run", headers=h, json={"endpoint": "nope"})
    assert r.status_code == 404 and _err(r)["code"] == "not_found"
    r = client.post(f"{base}/sales/run", headers=h, json={"tile_key": "bad key!"})
    assert r.status_code == 400 and _err(r)["field"] == "tile_key"

    # frame
    r = client.post(f"{base}/page/frame", headers=h, json={"tile_key": "t1"})
    assert r.status_code == 200, r.text
    assert "main.html" in r.json()["frame_url"] and r.json()["expires_at"]
    r = client.post(f"{base}/sales/frame", headers=h, json={})
    assert r.status_code == 400 and _err(r)["code"] == "invalid_input"
    assert client.post(f"{base}/ghost/frame", headers=h, json={}).status_code == 404


def test_agent_scoped_list_with_stopped_environment_is_503(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    adapter = _adapter(patch_environment_adapter)
    agent = setup_component_agent(
        client, db, superuser_token_headers, adapter, [make_component()], env_status="stopped"
    )
    r = client.get(f"{API}/agents/{agent['id']}/dashboards/", headers=superuser_token_headers)
    assert r.status_code == 503, r.text
    assert _err(r)["code"] == "env_unavailable"


# ── Scenario 10: ownership ──────────────────────────────────────────────────

def test_other_users_agent_dashboard_and_block_are_not_found(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    adapter = _adapter(patch_environment_adapter)
    t = create_component_tile(client, db, superuser_token_headers, adapter)
    _, other = create_random_user_with_headers(client)
    agent_base = f"{API}/agents/{t['agent']['id']}/dashboards"

    for method, url, body in [
        ("get", f"{agent_base}/", None),
        ("get", f"{agent_base}/sales", None),
        ("post", f"{agent_base}/sales/run", {"params": {}}),
        ("post", f"{agent_base}/sales/frame", {}),
        ("get", f"{t['base']}/data", None),
        ("post", f"{t['base']}/refresh", {"reason": "manual"}),
        ("put", f"{t['base']}/params", {"params": {}}),
        ("post", f"{t['base']}/call", {"endpoint": "lookup", "params": {}}),
        ("post", f"{t['base']}/frame", None),
        ("put", f"{t['base']}/page-state", {"page_doc": "d", "highlight_ready": True}),
    ]:
        kwargs = {"headers": other}
        if body is not None:
            kwargs["json"] = body
        r = getattr(client, method)(url, **kwargs)
        assert r.status_code == 404, f"{method} {url}: {r.status_code} {r.text}"
        assert _err(r)["code"] == "not_found"

    # The owner's own agent but a wrong dashboard/block combination, and random ids
    h = superuser_token_headers
    other_dash = create_dashboard(client, h)
    for url in (
        f"{API}/dashboards/{other_dash['id']}/blocks/{t['block']['id']}/component/data",
        f"{API}/dashboards/{t['dashboard']['id']}/blocks/{uuid.uuid4()}/component/data",
        f"{API}/dashboards/{uuid.uuid4()}/blocks/{t['block']['id']}/component/data",
        f"{API}/agents/{uuid.uuid4()}/dashboards/",
    ):
        r = client.get(url, headers=h)
        assert r.status_code == 404, f"{url}: {r.text}"
        assert _err(r)["code"] == "not_found"

    # Unauthenticated
    assert client.get(f"{t['base']}/data").status_code in (401, 403)
    assert client.get(f"{agent_base}/").status_code in (401, 403)


def test_non_component_block_is_not_a_tile(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    from tests.utils.dashboard import add_block

    adapter = _adapter(patch_environment_adapter)
    h = superuser_token_headers
    agent = setup_component_agent(client, db, h, adapter, [make_component()])
    dashboard = create_dashboard(client, h)
    block = add_block(client, h, dashboard["id"], agent["id"], view_type="latest_session")
    r = client.get(
        f"{API}/dashboards/{dashboard['id']}/blocks/{block['id']}/component/data", headers=h
    )
    assert r.status_code == 404 and _err(r)["code"] == "not_found"


# ── Scenario 11: page state ─────────────────────────────────────────────────

def test_page_state_put_returns_204_and_validates(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    adapter = _adapter(patch_environment_adapter)
    t = create_component_tile(client, db, superuser_token_headers, adapter)
    h = superuser_token_headers
    url = f"{t['base']}/page-state"

    r = client.put(url, headers=h, json={"page_doc": "doc-1", "highlight_ready": True})
    assert r.status_code == 204 and r.content == b""
    # Replaces the previous report; null document and defaults accepted
    assert client.put(url, headers=h, json={"page_doc": None, "highlight_ready": False}).status_code == 204
    assert client.put(url, headers=h, json={}).status_code == 204
    # Document id over 64 chars is refused
    assert client.put(url, headers=h, json={"page_doc": "x" * 65}).status_code == 422
    # No run happened
    assert getattr(adapter, "dashboard_run_calls", []) == []


# ── Review fixes ────────────────────────────────────────────────────────────

def test_changing_block_component_drops_the_cached_result(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    adapter = _adapter(patch_environment_adapter)
    h = superuser_token_headers
    adapter.dashboard_run_results = {("sales", "get_data"): {"ok": True, "value": {"markdown": "sales data"}}}
    t = create_component_tile(
        client, db, h, adapter, [make_component("sales"), make_component("other")]
    )
    client.post(f"{t['base']}/refresh", headers=h, json={"reason": "manual"})
    assert client.get(f"{t['base']}/data", headers=h).json()["data"] == {"markdown": "sales data"}

    url = f"{API}/dashboards/{t['dashboard']['id']}/blocks/{t['block']['id']}"
    r = client.put(url, headers=h, json={"config": {"component": "other", "params": {}}})
    assert r.status_code == 200, r.text
    data = client.get(f"{t['base']}/data", headers=h).json()
    assert data["data"] is None and data["stale"] is True and data["fetched_at"] is None
