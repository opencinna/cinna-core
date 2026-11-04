"""
Integration tests for the agent's dashboard tile tools (P4).

``POST /agent/dashboards/tiles`` and ``/agent/dashboards/update-tile`` are what
env-core's ``dashboards_tiles`` / ``dashboards_update_tile`` tools call (env
token + ``X-Agent-Env-Id``). Both always answer 200: the result, or
``{error: {code, ...}}``. Socket emits are observed by recording
``event_service.emit_event``.

Test scenarios:
  1. no_dashboard: plain session, deleted dashboard, other agent's session, junk id
  2. Another agent's tile (or an unknown one) is not_found; it is not listed either
  3. tiles: resolved params, param_specs, can_highlight per view
  4. summary / selection only for the focused tile; ``visible`` is never present
  5. update-tile: echo and unchanged set run nothing; a real change runs once,
     stores, and emits dashboard_tile_updated
  6. Refused params (and rate_limited) also emit a highlight clear
  7. highlight: html not ready -> highlighted false; native views true
  8. Budget: highlight-only is free; an agent run shares the budget with page calls
"""
import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.agent_env import create_env_with_token
from tests.utils.background_tasks import drain_tasks
from tests.utils.dashboard import create_dashboard, delete_dashboard, get_dashboard
from tests.utils.dashboard_components import (  # noqa: F401 — patch_environment_adapter is an autouse override
    add_component_block,
    create_component_tile,
    make_component,
    open_chat,
    patch_environment_adapter,
    send_chat_message,
    setup_component_agent,
)

API = settings.API_V1_STR

_PARAMS = {
    "region": {"type": "enum", "values": ["eu", "us", "apac"], "default": "eu"},
    "limit": {"type": "number", "default": 10},
}


# ── Helpers ─────────────────────────────────────────────────────────────────

def _tile(client, db, h, lm, *, view="table", name="sales", params=None):
    adapter = lm._test_adapter
    comp = make_component(name, params=_PARAMS, view=view)
    t = create_component_tile(client, db, h, adapter, [comp], params=params)
    t["adapter"] = adapter
    t["chat"] = open_chat(client, h, t["dashboard"]["id"], t["agent"]["id"])
    me = client.get(f"{API}/users/me", headers=h).json()
    _, t["env_h"] = create_env_with_token(db, agent_id=t["agent"]["id"], owner_id=me["id"])
    return t


def _tiles(client, t, session_id=None):
    r = client.post(
        f"{API}/agent/dashboards/tiles", headers=t["env_h"], json={"session_id": session_id or t["chat"]["id"]}
    )
    assert r.status_code == 200, r.text
    return r.json()


def _update(client, t, tile_id=None, session_id=None, **body):
    r = client.post(
        f"{API}/agent/dashboards/update-tile",
        headers=t["env_h"],
        json={"session_id": session_id or t["chat"]["id"], "tile_id": tile_id or t["block"]["id"], **body},
    )
    assert r.status_code == 200, r.text
    return r.json()


@contextmanager
def _emits():
    """Record event_service.emit_event; yields a getter ``events(type) -> [meta]``."""
    mock = AsyncMock()
    with patch("app.services.events.event_service.event_service.emit_event", mock):
        def events(event_type: str) -> list[dict]:
            return [
                c.kwargs["meta"] for c in mock.call_args_list
                if str(getattr(c.kwargs.get("event_type"), "value", c.kwargs.get("event_type"))) == event_type
            ]
        yield events


def _update_and_drain(client, t, **body):
    """update-tile with emits recorded; background recompute drained inside the patch."""
    with _emits() as events:
        result = _update(client, t, **body)
        drain_tasks()
        return result, events


def _error_code(body: dict) -> str:
    return body["error"]["code"]


# ── Scenario 1: no_dashboard ────────────────────────────────────────────────

def test_plain_session_deleted_dashboard_and_foreign_sessions_are_no_dashboard(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    h = superuser_token_headers
    t = _tile(client, db, h, patch_environment_adapter)

    # Works with the chat session
    assert len(_tiles(client, t)["tiles"]) == 1

    # A plain session of the same agent has no dashboard
    plain = client.post(
        f"{API}/sessions/", headers=h, json={"agent_id": t["agent"]["id"], "mode": "conversation"}
    ).json()
    assert _error_code(_tiles(client, t, plain["id"])) == "no_dashboard"
    assert _error_code(_update(client, t, session_id=plain["id"], highlight=["a"])) == "no_dashboard"

    # A session of another agent (chat on the same dashboard) is not the caller's
    other = setup_component_agent(client, db, h, t["adapter"], [make_component()])
    other_chat = open_chat(client, h, t["dashboard"]["id"], other["id"])
    assert _error_code(_tiles(client, t, other_chat["id"])) == "no_dashboard"

    # Junk and unknown ids
    assert _error_code(_tiles(client, t, "not-a-uuid")) == "no_dashboard"
    assert _error_code(_tiles(client, t, "00000000-0000-0000-0000-000000000000")) == "no_dashboard"

    # Deleted dashboard
    delete_dashboard(client, h, t["dashboard"]["id"])
    body = _tiles(client, t)
    assert _error_code(body) == "no_dashboard" and body["error"]["message"]
    assert _error_code(_update(client, t, highlight=["a"])) == "no_dashboard"


def test_tool_routes_need_the_env_token(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    t = _tile(client, db, superuser_token_headers, patch_environment_adapter)
    r = client.post(f"{API}/agent/dashboards/tiles", json={"session_id": t["chat"]["id"]})
    assert r.status_code in (401, 403)
    # A user JWT is not an environment token
    r = client.post(
        f"{API}/agent/dashboards/tiles", headers=superuser_token_headers, json={"session_id": t["chat"]["id"]}
    )
    assert r.status_code in (401, 403)


# ── Scenario 2: another agent's tile ────────────────────────────────────────

def test_another_agents_tile_or_unknown_id_is_not_found_and_not_listed(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    h = superuser_token_headers
    t = _tile(client, db, h, patch_environment_adapter)
    other = setup_component_agent(client, db, h, t["adapter"], [make_component("sales", params=_PARAMS), make_component("ops", params=_PARAMS)],
    )
    r = add_component_block(client, h, t["dashboard"]["id"], other["id"], "ops")
    assert r.status_code == 200, r.text
    foreign_id = r.json()["id"]

    tiles = _tiles(client, t)["tiles"]
    assert [x["tile_id"] for x in tiles] == [t["block"]["id"]]

    for bad in (foreign_id, "00000000-0000-0000-0000-000000000000", "nope", "x" * 200):
        body = _update(client, t, tile_id=bad, params={"limit": 5})
        assert _error_code(body) == "not_found", bad
        assert _error_code(_update(client, t, tile_id=bad, highlight=["a"])) == "not_found"
    # Nothing ran for the foreign tile
    assert getattr(t["adapter"], "dashboard_run_calls", []) == []


def test_non_component_block_is_not_found(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    from tests.utils.dashboard import add_block

    h = superuser_token_headers
    t = _tile(client, db, h, patch_environment_adapter)
    plain = add_block(client, h, t["dashboard"]["id"], t["agent"]["id"])
    assert _error_code(_update(client, t, tile_id=plain["id"], highlight=["a"])) == "not_found"
    assert [x["tile_id"] for x in _tiles(client, t)["tiles"]] == [t["block"]["id"]]


# ── Scenario 3: tiles ───────────────────────────────────────────────────────

def test_tiles_list_resolved_params_specs_and_can_highlight_per_view(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    h = superuser_token_headers
    adapter = patch_environment_adapter._test_adapter
    comps = [
        make_component("grid", params=_PARAMS, view="table"),
        make_component("feed", view="list"),
        make_component("page", view="html"),
        make_component("notes", view="markdown"),
        make_component("flawed", view="table"),
    ]
    comps[4]["findings"] = [{"severity": "error", "code": "bad", "message": "broken"}]
    agent = setup_component_agent(client, db, h, adapter, comps)
    dash = create_dashboard(client, h)
    ids = {}
    for c in comps:
        r = add_component_block(client, h, dash["id"], agent["id"], c["name"],
                                params={"limit": 3} if c["name"] == "grid" else None)
        assert r.status_code == 200, r.text
        ids[c["name"]] = r.json()["id"]
    chat = open_chat(client, h, dash["id"], agent["id"])
    me = client.get(f"{API}/users/me", headers=h).json()
    _, env_h = create_env_with_token(db, agent_id=agent["id"], owner_id=me["id"])
    t = {"env_h": env_h, "chat": chat}

    body = _tiles(client, t)
    assert body["dashboard"] == {"id": dash["id"], "name": dash["name"]}
    by_name = {x["component"]: x for x in body["tiles"]}
    assert set(by_name) == set(ids)
    grid = by_name["grid"]
    assert grid["tile_id"] == ids["grid"] and grid["view"] == "table"
    assert grid["params"] == {"region": "eu", "limit": 3}
    assert grid["param_specs"]["region"]["values"] == ["eu", "us", "apac"]
    assert grid["can_highlight"] is True
    assert by_name["feed"]["can_highlight"] is True
    assert by_name["page"]["can_highlight"] is False  # html, page not ready
    assert by_name["notes"]["can_highlight"] is False
    assert by_name["flawed"]["can_highlight"] is False  # component with an error finding
    assert all("summary" not in x and "selection" not in x and "visible" not in x for x in body["tiles"])

    # A component that disappeared from the environment is omitted
    adapter.dashboard_components = [c for c in comps if c["name"] != "feed"]
    from app.services.dashboards.component_service import ComponentService
    ComponentService.clear_cache()
    assert "feed" not in {x["component"] for x in _tiles(client, t)["tiles"]}


# ── Scenario 4: focused tile only ───────────────────────────────────────────

def test_summary_and_selection_only_for_the_focused_tile_and_visible_never(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    h = superuser_token_headers
    t = _tile(client, db, h, patch_environment_adapter)
    r = add_component_block(client, h, t["dashboard"]["id"], t["agent"]["id"], "sales")
    second = r.json()["id"]

    # No context sent yet: nothing extra
    first_body = _tiles(client, t)
    assert all("summary" not in x for x in first_body["tiles"])

    ctx = json.dumps({
        "dashboard": {"id": t["dashboard"]["id"]},
        "focused": {
            "tile_id": t["block"]["id"], "component": "sales", "title": "Sales",
            "summary": "3 rows", "selection": ["r1", "r2"], "visible": ["r1", "r2", "r3"],
        },
        "other_tiles": [{"tile_id": second, "summary": "not focused"}],
    })
    send_chat_message(client, h, t["chat"]["id"], "look", ctx)

    tiles = {x["tile_id"]: x for x in _tiles(client, t)["tiles"]}
    focused = tiles[t["block"]["id"]]
    assert focused["summary"] == "3 rows" and focused["selection"] == ["r1", "r2"]
    assert "summary" not in tiles[second] and "selection" not in tiles[second]
    assert all("visible" not in x for x in tiles.values())

    # Focus moves to the other tile with the next context
    ctx2 = json.dumps({"focused": {"tile_id": second, "component": "sales", "summary": "other"}})
    send_chat_message(client, h, t["chat"]["id"], "now this", ctx2)
    tiles = {x["tile_id"]: x for x in _tiles(client, t)["tiles"]}
    assert tiles[second]["summary"] == "other"
    assert "summary" not in tiles[t["block"]["id"]]


# ── Scenario 5: update-tile ─────────────────────────────────────────────────

def test_echo_and_unchanged_params_run_nothing_and_a_change_runs_once(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    h = superuser_token_headers
    t = _tile(client, db, h, patch_environment_adapter, params={"region": "us"})
    adapter = t["adapter"]
    client.post(f"{t['base']}/refresh", headers=h, json={"reason": "manual"})
    runs = len(adapter.dashboard_run_calls)

    # Echo: a default given back, and the same value
    result, events = _update_and_drain(client, t, params={"region": "us", "limit": 10})
    assert result == {
        "tile_id": t["block"]["id"], "params": {"region": "us", "limit": 10},
        "recomputing": False, "highlighted": False,
    }
    assert events("dashboard_tile_updated") == [] and events("dashboard_tile_highlight") == []
    assert len(adapter.dashboard_run_calls) == runs

    # An empty params object is a no-op too
    result, _ = _update_and_drain(client, t, params={}, highlight=[])
    assert result["recomputing"] is False
    assert len(adapter.dashboard_run_calls) == runs

    # A real change: merged over the stored set, runs once, emits the refresh event
    result, events = _update_and_drain(client, t, params={"limit": 25})
    assert result["recomputing"] is True
    assert result["params"] == {"region": "us", "limit": 25}
    assert len(adapter.dashboard_run_calls) == runs + 1
    assert adapter.dashboard_run_calls[-1]["params"] == {"region": "us", "limit": 25}
    assert events("dashboard_tile_updated") == [
        {"dashboard_id": t["dashboard"]["id"], "block_id": t["block"]["id"]}
    ]
    stored = get_dashboard(client, h, t["dashboard"]["id"])["blocks"][0]["config"]["params"]
    assert stored == {"region": "us", "limit": 25}
    assert _tiles(client, t)["tiles"][0]["params"] == {"region": "us", "limit": 25}


def test_update_tile_needs_params_or_highlight_and_validates_shapes(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    t = _tile(client, db, superuser_token_headers, patch_environment_adapter)
    assert _error_code(_update(client, t)) == "invalid_input"
    body = _update(client, t, params=["limit"])
    assert _error_code(body) == "invalid_input" and body["error"]["field"] == "params"
    body = _update(client, t, highlight="r1")
    assert _error_code(body) == "invalid_input" and body["error"]["field"] == "highlight"
    assert _error_code(_update(client, t, highlight=[1, 2])) == "invalid_input"
    assert _error_code(_update(client, t, highlight=[""])) == "invalid_input"
    assert _error_code(_update(client, t, highlight=["a"] * 501)) == "invalid_input"


# ── Scenario 6: refused params clear the highlight ──────────────────────────

def test_refused_params_return_the_error_and_emit_a_highlight_clear(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    h = superuser_token_headers
    t = _tile(client, db, h, patch_environment_adapter, params={"region": "us"})
    adapter = t["adapter"]

    for bad in ({"region": "mars"}, {"bogus": 1}, {"limit": "ten"}):
        result, events = _update_and_drain(client, t, params=bad)
        assert _error_code(result) == "invalid_input"
        assert result["error"]["field"].startswith("params")
        assert events("dashboard_tile_highlight") == [
            {"dashboard_id": t["dashboard"]["id"], "block_id": t["block"]["id"], "ids": []}
        ]
        assert events("dashboard_tile_updated") == []
    assert getattr(adapter, "dashboard_run_calls", []) == []
    stored = get_dashboard(client, h, t["dashboard"]["id"])["blocks"][0]["config"]["params"]
    assert stored == {"region": "us"}


def test_rate_limited_params_also_emit_a_highlight_clear(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    t = _tile(client, db, superuser_token_headers, patch_environment_adapter)
    for i in range(30):
        assert "error" not in _update(client, t, params={"limit": 100 + i})

    result, events = _update_and_drain(client, t, params={"limit": 999}, highlight=["a"])
    assert _error_code(result) == "rate_limited"
    assert result["error"]["retry_after_s"] >= 1
    # The cleared highlight is the only one emitted: the requested one is not
    assert events("dashboard_tile_highlight") == [
        {"dashboard_id": t["dashboard"]["id"], "block_id": t["block"]["id"], "ids": []}
    ]


# ── Scenario 7: highlight ───────────────────────────────────────────────────

def test_highlight_is_relayed_and_highlighted_follows_can_highlight(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    h = superuser_token_headers
    t = _tile(client, db, h, patch_environment_adapter, view="table")
    result, events = _update_and_drain(client, t, highlight=["r1", "r2"])
    assert result["highlighted"] is True and result["recomputing"] is False
    assert events("dashboard_tile_highlight") == [
        {"dashboard_id": t["dashboard"]["id"], "block_id": t["block"]["id"], "ids": ["r1", "r2"]}
    ]
    # An empty list clears
    result, events = _update_and_drain(client, t, highlight=[])
    assert result["highlighted"] is True
    assert events("dashboard_tile_highlight")[0]["ids"] == []


def test_html_tile_highlight_waits_for_the_page_to_be_ready(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    h = superuser_token_headers
    t = _tile(client, db, h, patch_environment_adapter, view="html")

    result, events = _update_and_drain(client, t, highlight=["r1"])
    assert result["highlighted"] is False
    assert _tiles(client, t)["tiles"][0]["can_highlight"] is False

    page = f"{t['base']}/page-state"
    assert client.put(page, headers=h, json={"page_doc": "d1", "highlight_ready": True}).status_code == 204
    assert _tiles(client, t)["tiles"][0]["can_highlight"] is True
    result, _ = _update_and_drain(client, t, highlight=["r1"])
    assert result["highlighted"] is True

    # The page reports not ready again: false again
    assert client.put(page, headers=h, json={"page_doc": "d1", "highlight_ready": False}).status_code == 204
    result, _ = _update_and_drain(client, t, highlight=["r1"])
    assert result["highlighted"] is False


# ── Scenario 8: budget ──────────────────────────────────────────────────────

def test_highlight_only_calls_are_not_counted_against_the_budget(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    t = _tile(client, db, superuser_token_headers, patch_environment_adapter)
    for _ in range(45):
        assert _update(client, t, highlight=["r1"])["recomputing"] is False
    # Echoes are free too
    for _ in range(5):
        assert _update(client, t, params={"limit": 10})["recomputing"] is False
    # All 30 counted runs are still available, the 31st is refused
    for i in range(30):
        result = _update(client, t, params={"limit": 100 + i})
        assert "error" not in result, f"run {i}: {result}"
    assert _error_code(_update(client, t, params={"limit": 999})) == "rate_limited"


def test_an_agent_run_shares_the_tile_budget_with_page_calls(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    h = superuser_token_headers
    t = _tile(client, db, h, patch_environment_adapter)
    for i in range(20):
        assert "error" not in _update(client, t, params={"limit": 100 + i})
    for _ in range(10):
        r = client.post(f"{t['base']}/call", headers=h, json={"endpoint": "lookup", "params": {}})
        assert r.status_code == 200, r.text

    # 30 runs used, by both sides: the agent is refused ...
    body = _update(client, t, params={"limit": 5000})
    assert _error_code(body) == "rate_limited" and body["error"]["retry_after_s"] >= 1
    # ... and so is the page
    r = client.post(f"{t['base']}/call", headers=h, json={"endpoint": "lookup", "params": {}})
    assert r.status_code == 429
    # A refused change stored nothing
    assert _tiles(client, t)["tiles"][0]["params"]["limit"] == 119


# ── Review fixes ────────────────────────────────────────────────────────────

def test_bad_or_missing_tile_id_is_200_not_found_never_422(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    t = _tile(client, db, superuser_token_headers, patch_environment_adapter)
    sid = t["chat"]["id"]
    for body in (
        {"session_id": sid},
        {"session_id": sid, "tile_id": None, "highlight": ["a"]},
        {"session_id": sid, "tile_id": 42, "highlight": ["a"]},
        {"session_id": sid, "tile_id": "x" * 400, "params": {"limit": 5}},
    ):
        r = client.post(f"{API}/agent/dashboards/update-tile", headers=t["env_h"], json=body)
        assert r.status_code == 200, f"{body}: {r.text}"
        assert _error_code(r.json()) == "not_found", body


def test_every_params_refusal_emits_a_highlight_clear(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    h = superuser_token_headers
    t = _tile(client, db, h, patch_environment_adapter)
    clear = [{"dashboard_id": t["dashboard"]["id"], "block_id": t["block"]["id"], "ids": []}]

    # params that is not an object
    result, events = _update_and_drain(client, t, params=["limit"])
    assert _error_code(result) == "invalid_input"
    assert events("dashboard_tile_highlight") == clear

    # component gone from the environment, params given
    t["adapter"].dashboard_components = []
    from app.services.dashboards.component_service import ComponentService
    ComponentService.clear_cache()
    result, events = _update_and_drain(client, t, params={"limit": 5})
    assert _error_code(result) == "not_found"
    assert events("dashboard_tile_highlight") == clear


def test_tile_updated_is_emitted_even_when_the_recompute_fails(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    h = superuser_token_headers
    t = _tile(client, db, h, patch_environment_adapter)
    adapter = t["adapter"]

    async def boom(*args, **kwargs):
        raise RuntimeError("env exploded")

    adapter.run_dashboard_endpoint = boom
    result, events = _update_and_drain(client, t, params={"limit": 77})
    assert result["recomputing"] is True
    assert events("dashboard_tile_updated") == [
        {"dashboard_id": t["dashboard"]["id"], "block_id": t["block"]["id"]}
    ]
    # The params were stored before the recompute
    stored = get_dashboard(client, h, t["dashboard"]["id"])["blocks"][0]["config"]["params"]
    assert stored == {"limit": 77}


# ── Seam fixes ──────────────────────────────────────────────────────────────

def test_page_state_with_a_lower_seq_is_ignored(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    h = superuser_token_headers
    t = _tile(client, db, h, patch_environment_adapter, view="html")
    page = f"{t['base']}/page-state"
    put = lambda ready, seq: client.put(  # noqa: E731
        page, headers=h, json={"page_doc": "d1", "highlight_ready": ready, "seq": seq}
    ).status_code
    assert put(True, 10) == 204
    assert _tiles(client, t)["tiles"][0]["can_highlight"] is True
    # A late, older write does not flip it
    assert put(False, 5) == 204
    assert _tiles(client, t)["tiles"][0]["can_highlight"] is True
    # A newer one does; a write without seq always applies
    assert put(False, 11) == 204
    assert _tiles(client, t)["tiles"][0]["can_highlight"] is False
    assert client.put(page, headers=h, json={"page_doc": "d1", "highlight_ready": True}).status_code == 204
    assert _tiles(client, t)["tiles"][0]["can_highlight"] is True


def test_readiness_older_than_the_ttl_counts_as_not_ready(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    from datetime import timedelta

    h = superuser_token_headers
    t = _tile(client, db, h, patch_environment_adapter, view="html")
    page = f"{t['base']}/page-state"
    assert client.put(page, headers=h, json={"page_doc": "d1", "highlight_ready": True}).status_code == 204
    assert _tiles(client, t)["tiles"][0]["can_highlight"] is True

    # The last report is now "older than 15 minutes"
    with patch("app.services.dashboards.run_budget.PAGE_STATE_TTL", timedelta(seconds=-1)):
        assert _tiles(client, t)["tiles"][0]["can_highlight"] is False
        result, _ = _update_and_drain(client, t, highlight=["r1"])
        assert result["highlighted"] is False
    # A fresh report makes it ready again
    result, _ = _update_and_drain(client, t, highlight=["r1"])
    assert result["highlighted"] is True


def test_interval_tick_within_60s_of_the_last_fetch_returns_the_cache(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    h = superuser_token_headers
    t = _tile(client, db, h, patch_environment_adapter)
    adapter = t["adapter"]
    r = client.post(f"{t['base']}/refresh", headers=h, json={"reason": "manual"})
    assert r.status_code == 200, r.text
    runs = len(adapter.dashboard_run_calls)
    for _ in range(3):
        r = client.post(f"{t['base']}/refresh", headers=h, json={"reason": "interval"})
        assert r.status_code == 200, r.text
    assert len(adapter.dashboard_run_calls) == runs
    # A manual refresh still runs
    client.post(f"{t['base']}/refresh", headers=h, json={"reason": "manual"})
    assert len(adapter.dashboard_run_calls) == runs + 1


def test_tool_calls_see_a_changed_component_spec_without_clearing_the_cache(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    h = superuser_token_headers
    t = _tile(client, db, h, patch_environment_adapter)
    assert _tiles(client, t)["tiles"][0]["view"] == "table"
    assert "extra" not in _tiles(client, t)["tiles"][0]["param_specs"]

    changed = make_component("sales", params={**_PARAMS, "extra": {"type": "text"}}, view="list")
    t["adapter"].dashboard_components = [changed]
    tile = _tiles(client, t)["tiles"][0]
    assert tile["view"] == "list" and "extra" in tile["param_specs"]
    # update-tile validates against the new spec too
    assert "error" not in _update(client, t, params={"extra": "hello"})
