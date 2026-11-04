"""
Integration tests for agent_component tile params and the per-tile run budget.

Test scenarios:
  1. Params set is a full replace; a cleared key goes back to the default
  2. `current` / `today` resolve in the owner's timezone
  3. A refused value leaves the stored set untouched
  4. Echo (set to what it already resolves to) runs nothing
  5. Budget — the 31st counted run is rate_limited with retry_after_s
  6. Budget is shared by page calls and params changes
  7. Not counted: refresh, highlight-only page state, empty-params preview
  8. Agent-scoped preview budget is per tile_key
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.utils.dashboard_components import (  # noqa: F401 — patch_environment_adapter is an autouse override
    create_component_tile,
    make_component,
    patch_environment_adapter,
    setup_component_agent,
)

API = settings.API_V1_STR

_PARAMS = {
    "region": {"type": "enum", "values": ["eu", "us", "apac"], "default": "eu"},
    "limit": {"type": "number", "default": 10},
    "month": {"type": "month", "default": "current"},
    "day": {"type": "date", "default": "today"},
    "note": {"type": "text"},
}


def _adapter(lm):
    return lm._test_adapter


def _tile(client, db, headers, lm, params=None, **kw):
    adapter = _adapter(lm)
    comp = make_component(params=_PARAMS, **kw)
    t = create_component_tile(client, db, headers, adapter, [comp], params=params)
    t["adapter"] = adapter
    return t


def _put(client, t, headers, params):
    return client.put(f"{t['base']}/params", headers=headers, json={"params": params})


def _set_timezone(client: TestClient, headers: dict, tz: str) -> None:
    r = client.patch(f"{API}/users/me", headers=headers, json={"timezone": tz})
    assert r.status_code == 200, r.text


# ── Scenario 1: full replace, cleared key ───────────────────────────────────

def test_params_put_is_a_full_set_and_cleared_key_returns_to_default(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    h = superuser_token_headers
    t = _tile(client, db, h, patch_environment_adapter, params={"region": "us", "limit": 5})
    adapter = t["adapter"]

    r = _put(client, t, h, {"region": "apac", "note": "hello"})
    assert r.status_code == 200, r.text
    resolved = r.json()["params_resolved"]
    # `limit` was left out: back to its default; `region` and `note` as sent
    assert resolved["region"] == "apac" and resolved["note"] == "hello"
    assert resolved["limit"] == 10
    assert adapter.dashboard_run_calls[-1]["params"] == resolved

    # Clearing with "" or null removes the key: default applies
    r = _put(client, t, h, {"region": "", "note": None})
    assert r.status_code == 200, r.text
    resolved = r.json()["params_resolved"]
    assert resolved["region"] == "eu" and "note" not in resolved
    # Persisted on the block
    stored = client.get(
        f"{API}/dashboards/{t['dashboard']['id']}", headers=h
    ).json()["blocks"][0]["config"]["params"]
    assert stored == {}


def test_initial_block_params_are_resolved_into_the_first_run(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    h = superuser_token_headers
    t = _tile(client, db, h, patch_environment_adapter, params={"region": "us"})
    r = client.post(f"{t['base']}/refresh", headers=h, json={"reason": "manual"})
    assert r.json()["params_resolved"]["region"] == "us"
    assert t["adapter"].dashboard_run_calls[-1]["params"]["region"] == "us"


# ── Scenario 2: timezone resolution ─────────────────────────────────────────

def test_current_and_today_resolve_in_the_owners_timezone(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    h = superuser_token_headers
    _set_timezone(client, h, "Pacific/Kiritimati")
    t = _tile(client, db, h, patch_environment_adapter)
    r = client.post(f"{t['base']}/refresh", headers=h, json={"reason": "manual"})
    assert r.status_code == 200, r.text
    east = datetime.now(ZoneInfo("Pacific/Kiritimati"))
    resolved = r.json()["params_resolved"]
    assert resolved["day"] == east.date().isoformat()
    assert resolved["month"] == resolved["day"][:7]
    sent = t["adapter"].dashboard_run_calls[-1]["params"]
    assert sent["day"] == resolved["day"]  # the endpoint never sees the words
    kiritimati_day = resolved["day"]

    _set_timezone(client, h, "Pacific/Pago_Pago")
    r = client.post(f"{t['base']}/refresh", headers=h, json={"reason": "manual"})
    west_day = r.json()["params_resolved"]["day"]
    assert west_day == datetime.now(ZoneInfo("Pacific/Pago_Pago")).date().isoformat()
    # 25 hours apart: the calendar day always differs
    assert west_day != kiritimati_day


def test_explicit_current_and_today_values_are_accepted_and_resolved(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    h = superuser_token_headers
    t = _tile(client, db, h, patch_environment_adapter)
    r = _put(client, t, h, {"month": "2024-02", "day": "today"})
    assert r.status_code == 200, r.text
    resolved = r.json()["params_resolved"]
    assert resolved["month"] == "2024-02"
    assert resolved["day"] != "today" and len(resolved["day"]) == 10


# ── Scenario 3: refused values ──────────────────────────────────────────────

def test_refused_value_leaves_the_stored_set_and_runs_nothing(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    h = superuser_token_headers
    t = _tile(client, db, h, patch_environment_adapter, params={"region": "us"})
    adapter = t["adapter"]

    r = _put(client, t, h, {"region": "mars"})
    assert r.status_code == 400, r.text
    err = r.json()["error"]
    assert err["code"] == "invalid_input" and err["field"] == "params.region"
    assert err["message"] == "`params.region` does not fit type enum."

    r = _put(client, t, h, {"region": "eu", "bogus": 1})
    assert r.status_code == 400
    assert r.json()["error"]["message"] == "`params.bogus` is not a param of this component."

    r = _put(client, t, h, {"limit": "ten"})
    assert r.status_code == 400 and r.json()["error"]["field"] == "params.limit"
    r = _put(client, t, h, {"month": "2024-13"})
    assert r.status_code == 400
    r = _put(client, t, h, {"limit": True})  # booleans are not numbers
    assert r.status_code == 400

    assert getattr(adapter, "dashboard_run_calls", []) == []
    stored = client.get(
        f"{API}/dashboards/{t['dashboard']['id']}", headers=h
    ).json()["blocks"][0]["config"]["params"]
    assert stored == {"region": "us"}
    assert client.get(f"{t['base']}/data", headers=h).json()["params_resolved"]["region"] == "us"


# ── Scenario 4: echo ────────────────────────────────────────────────────────

def test_setting_values_the_tile_already_resolves_to_runs_nothing(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    h = superuser_token_headers
    t = _tile(client, db, h, patch_environment_adapter, params={"region": "us"})
    adapter = t["adapter"]
    client.post(f"{t['base']}/refresh", headers=h, json={"reason": "manual"})
    runs = len(adapter.dashboard_run_calls)

    # Same set (and a default given back): nothing changes, nothing runs
    r = _put(client, t, h, {"region": "us"})
    assert r.status_code == 200 and r.json()["stale"] is False
    r = _put(client, t, h, {"region": "us", "limit": 10})  # limit 10 is the default
    assert r.status_code == 200
    assert len(adapter.dashboard_run_calls) == runs

    # A real change runs once
    _put(client, t, h, {"region": "eu"})
    assert len(adapter.dashboard_run_calls) == runs + 1


# ── Scenario 5: budget ──────────────────────────────────────────────────────

def test_31st_counted_params_change_is_rate_limited_with_retry_after(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    h = superuser_token_headers
    t = _tile(client, db, h, patch_environment_adapter)
    adapter = t["adapter"]

    for i in range(30):
        r = _put(client, t, h, {"limit": 100 + i})
        assert r.status_code == 200, f"run {i}: {r.text}"
    runs = len(adapter.dashboard_run_calls)

    r = _put(client, t, h, {"limit": 999})
    assert r.status_code == 429, r.text
    err = r.json()["error"]
    assert err["code"] == "rate_limited"
    assert 1 <= err["retry_after_s"] <= 60
    assert r.headers["Retry-After"] == str(err["retry_after_s"])
    # A refusal changes nothing
    assert len(adapter.dashboard_run_calls) == runs
    assert client.get(f"{t['base']}/data", headers=h).json()["params_resolved"]["limit"] == 129

    # Another tile has its own budget
    t2 = _tile(client, db, h, patch_environment_adapter)
    assert _put(client, t2, h, {"limit": 1}).status_code == 200


# ── Scenario 6: shared budget ───────────────────────────────────────────────

def test_page_calls_and_params_changes_share_one_budget(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    h = superuser_token_headers
    t = _tile(client, db, h, patch_environment_adapter)

    for i in range(20):
        assert _put(client, t, h, {"limit": 100 + i}).status_code == 200
    for _ in range(10):
        r = client.post(f"{t['base']}/call", headers=h, json={"endpoint": "lookup", "params": {}})
        assert r.status_code == 200, r.text

    r = client.post(f"{t['base']}/call", headers=h, json={"endpoint": "lookup", "params": {}})
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"
    assert r.json()["error"]["retry_after_s"] >= 1
    assert _put(client, t, h, {"limit": 5000}).status_code == 429


# ── Scenario 7: what is not counted ─────────────────────────────────────────

def test_refresh_and_page_state_do_not_count_against_the_budget(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    h = superuser_token_headers
    t = _tile(client, db, h, patch_environment_adapter)

    for i in range(29):
        assert _put(client, t, h, {"limit": 100 + i}).status_code == 200
    for reason in ("open", "reshow", "manual", "after_turn", "interval"):
        for _ in range(3):
            r = client.post(f"{t['base']}/refresh", headers=h, json={"reason": reason})
            assert r.status_code == 200, r.text
    for ready in (True, False, True):
        r = client.put(f"{t['base']}/page-state", headers=h, json={"page_doc": "d1", "highlight_ready": ready})
        assert r.status_code == 204
    # Budget still has exactly one counted run left
    assert _put(client, t, h, {"limit": 777}).status_code == 200
    assert _put(client, t, h, {"limit": 778}).status_code == 429
    # ...and a refresh is still never refused
    r = client.post(f"{t['base']}/refresh", headers=h, json={"reason": "manual"})
    assert r.status_code == 200, r.text


# ── Scenario 8: agent-scoped preview budget ─────────────────────────────────

def test_preview_with_empty_params_is_not_counted_but_params_and_endpoints_are(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    h = superuser_token_headers
    t = _tile(client, db, h, patch_environment_adapter)
    run = f"{API}/agents/{t['agent']['id']}/dashboards/sales/run"

    for _ in range(35):  # empty-params source preview: never counted
        r = client.post(run, headers=h, json={"params": {}})
        assert r.status_code == 200, r.text

    for i in range(30):
        r = client.post(run, headers=h, json={"params": {"limit": i}, "tile_key": "desk-1"})
        assert r.status_code == 200, f"run {i}: {r.text}"
        assert r.json()["params_resolved"]["limit"] == i
    r = client.post(run, headers=h, json={"params": {"limit": 99}, "tile_key": "desk-1"})
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"
    assert r.json()["error"]["retry_after_s"] >= 1

    # Other tile key, the keyless preview, and an empty-params source run: unaffected
    assert client.post(run, headers=h, json={"params": {"limit": 1}, "tile_key": "desk-2"}).status_code == 200
    assert client.post(run, headers=h, json={"params": {"limit": 1}}).status_code == 200
    assert client.post(run, headers=h, json={"params": {}, "tile_key": "desk-1"}).status_code == 200
    # An endpoint run (even with no params) counts toward its own tile key
    assert client.post(run, headers=h, json={"endpoint": "lookup", "tile_key": "desk-1"}).status_code == 429
    # Block budget is separate from the preview budget
    assert _put(client, t, h, {"limit": 3}).status_code == 200


def test_preview_run_validates_params_against_the_spec(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    h = superuser_token_headers
    t = _tile(client, db, h, patch_environment_adapter)
    run = f"{API}/agents/{t['agent']['id']}/dashboards/sales/run"
    r = client.post(run, headers=h, json={"params": {"region": "mars"}})
    assert r.status_code == 400 and r.json()["error"]["field"] == "params.region"
    r = client.post(run, headers=h, json={"params": {"bogus": 1}})
    assert r.status_code == 400
    # An endpoint run passes the page's own (undeclared) arguments through
    r = client.post(run, headers=h, json={"endpoint": "lookup", "params": {"q": "x", "region": "us"}})
    assert r.status_code == 200, r.text
    assert t["adapter"].dashboard_run_calls[-1]["params"]["q"] == "x"
    assert t["adapter"].dashboard_run_calls[-1]["params"]["region"] == "us"


# ── Review fixes ────────────────────────────────────────────────────────────

def test_undeclared_endpoint_does_not_use_budget(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    h = superuser_token_headers
    t = _tile(client, db, h, patch_environment_adapter)
    run = f"{API}/agents/{t['agent']['id']}/dashboards/sales/run"
    for _ in range(35):
        r = client.post(f"{t['base']}/call", headers=h, json={"endpoint": "nope", "params": {}})
        assert r.status_code == 404
        r = client.post(run, headers=h, json={"endpoint": "nope", "tile_key": "k1"})
        assert r.status_code == 404
    # Full budgets remain on both targets
    for i in range(30):
        assert _put(client, t, h, {"limit": 100 + i}).status_code == 200
        assert client.post(run, headers=h, json={"endpoint": "lookup", "tile_key": "k1"}).status_code == 200


def test_preview_run_with_long_skill_component_and_tile_key_does_not_500(
    client: TestClient, db: Session, superuser_token_headers: dict, patch_environment_adapter
) -> None:
    h = superuser_token_headers
    adapter = _adapter(patch_environment_adapter)
    skill, name = "s" * 100, "c" * 60
    comp = make_component(name, skill=skill)
    agent = setup_component_agent(client, db, h, adapter, [comp])
    r = client.post(
        f"{API}/agents/{agent['id']}/dashboards/{name}/run", headers=h,
        json={"skill": skill, "endpoint": "lookup", "params": {}, "tile_key": "t" * 64},
    )
    assert r.status_code == 200, r.text
