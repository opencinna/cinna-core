"""
CLI building-context — bundle_id pass-through and platform skills.

Covers ``GET /api/v1/cli/agents/{id}/building-context``:
- core asks env-core for ``/prompt/building`` with the agent's ``bundle_id``;
- ``platform_skills`` from the env pass through unchanged;
- an env that predates ``platform_skills`` falls back to the skills core
  installs (derived from ``PROTOCOL_FILES``);
- an unreachable env yields the minimal context with ``platform_skills: []``.

The env-core side (Dashboards line, skill scan) is covered in
``tests/unit/test_building_prompt_route.py``.
"""
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from app.core.config import settings
from app.services.environments.platform_protocol_files import PROTOCOL_FILES
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.cli import cli_auth_headers, create_setup_token, exchange_setup_token

_BASE = f"{settings.API_V1_STR}/cli"


class _FakeAsyncClient:
    """Stands in for ``httpx.AsyncClient``; records the env-core GET."""

    calls: list[dict] = []
    payload: dict = {}

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, headers=None):
        type(self).calls.append({"url": url, "params": params})
        request = httpx.Request("GET", url)
        return httpx.Response(200, json=type(self).payload, request=request)


def _cli_agent(client: TestClient, superuser_token_headers: dict) -> tuple[dict, dict]:
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    token = create_setup_token(client, superuser_token_headers, agent["id"])
    exchange = exchange_setup_token(client, token["token"], machine_name="Building Ctx Machine")
    return agent, cli_auth_headers(exchange["cli_token"])


def _fetch(client: TestClient, agent: dict, headers: dict, payload: dict) -> dict:
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.payload = payload
    with patch("app.services.cli.cli_service.httpx.AsyncClient", _FakeAsyncClient):
        r = client.get(f"{_BASE}/agents/{agent['id']}/building-context", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_building_context_sends_bundle_id_and_passes_env_platform_skills(
    client: TestClient, superuser_token_headers: dict
) -> None:
    agent, headers = _cli_agent(client, superuser_token_headers)
    env_skills = [
        {"name": "cinna-dashboards", "kit_path": "skills/cinna-dashboards"},
        {"name": "cinna-tasks", "kit_path": "skills/cinna-tasks"},
    ]
    body = _fetch(client, agent, headers, {
        "building_prompt": f"## Dashboards\ncinna://component/bundle:{agent['bundle_id']}/name",
        "building_prompt_parts": {},
        "prompt_files": {},
        "settings": {},
        "platform_skills": env_skills,
    })

    assert len(_FakeAsyncClient.calls) == 1
    call = _FakeAsyncClient.calls[0]
    assert call["url"].endswith("/prompt/building")
    assert str(call["params"]["bundle_id"]) == str(agent["bundle_id"])
    assert f"cinna://component/bundle:{agent['bundle_id']}/" in body["building_prompt"]
    assert body["platform_skills"] == env_skills


def test_building_context_old_env_without_platform_skills_falls_back_to_core_list(
    client: TestClient, superuser_token_headers: dict
) -> None:
    agent, headers = _cli_agent(client, superuser_token_headers)
    body = _fetch(client, agent, headers, {
        "building_prompt": "old env prompt",
        "building_prompt_parts": {},
        "prompt_files": {},
        "settings": {},
    })

    expected = sorted(p for p in PROTOCOL_FILES if p.startswith("skills/"))
    assert expected, "PROTOCOL_FILES must install at least one platform skill"
    got = sorted(s["kit_path"] for s in body["platform_skills"])
    assert got == expected
    for skill in body["platform_skills"]:
        assert skill["kit_path"] == f"skills/{skill['name']}"
    names = {s["name"] for s in body["platform_skills"]}
    assert {"cinna-dashboards", "cinna-tasks", "cinna-artifacts"} <= names


def test_building_context_env_with_empty_platform_skills_is_not_replaced(
    client: TestClient, superuser_token_headers: dict
) -> None:
    agent, headers = _cli_agent(client, superuser_token_headers)
    body = _fetch(client, agent, headers, {
        "building_prompt": "p", "building_prompt_parts": {}, "prompt_files": {},
        "settings": {}, "platform_skills": [],
    })
    assert body["platform_skills"] == []


def test_building_context_unreachable_env_returns_minimal_with_no_platform_skills(
    client: TestClient, superuser_token_headers: dict
) -> None:
    agent, headers = _cli_agent(client, superuser_token_headers)
    r = client.get(f"{_BASE}/agents/{agent['id']}/building-context", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["platform_skills"] == []
    assert "settings" in body and "building_prompt" in body
