"""
Integration tests: ``GET /credentials/issued`` for the A2A and ACP halves
(the agent_api half lives in ``tests/api/agents/agent_api/agents_agent_api_issued_keys_test.py``).

Scenarios:
  1. A2A row shape, no token value in the payload; revoked hidden by default,
     shown with ``include_ended`` as ``revoked``.
  2. ACP row shape; paused when the connector is deactivated; revoked via route.
  3. ``kind`` filter (repeatable, 422 on unknown value).
  4. Isolation: another user sees none of these rows.
  5. Mixed list ordering by kind on one agent; workspace filter.
"""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.a2a import create_access_token, update_access_token
from tests.utils.acp import acp_connector_url, create_acp_connector, create_acp_token
from tests.utils.agent import create_agent_via_api
from tests.utils.agent_api_keys import make_developer, setup_producer
from tests.utils.background_tasks import drain_tasks
from tests.utils.user import create_random_user_with_headers
from tests.utils.workspace import create_random_workspace

API = settings.API_V1_STR
ISSUED_URL = f"{API}/credentials/issued"


def _issued(client: TestClient, headers: dict[str, str], **params) -> dict:
    r = client.get(ISSUED_URL, headers=headers, params=params)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == len(body["data"])
    return body


def _agent(client: TestClient, headers: dict[str, str], name: str) -> dict:
    agent = create_agent_via_api(client, headers, name=name)
    drain_tasks()
    return agent


# -- 1. A2A -------------------------------------------------------------------


def test_a2a_row_shape_and_no_token_value(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    _, headers = make_developer(client, superuser_token_headers)
    agent = _agent(client, headers, "A2A Issuer Agent")
    tok = create_access_token(client, headers, agent["id"], mode="conversation", scope="limited")

    body = _issued(client, headers, kind="a2a_token")
    assert [r["id"] for r in body["data"]] == [tok["id"]]
    row = body["data"][0]
    assert row["kind"] == "a2a_token"
    assert row["label"] == tok["name"]
    assert row["holder"] is None
    assert row["holder_kind"] == "external_client"
    assert row["status"] == "active"
    assert row["raw_status"] == "active"
    assert row["revoked_at"] is None
    assert row["details"] == {"kind": "a2a_token", "mode": "conversation", "scope": "limited"}
    assert row["agent"]["id"] == agent["id"]
    assert row["agent"]["name"] == "A2A Issuer Agent"
    assert "user_workspace_id" in row["agent"]
    assert row["created_at"]
    assert row["token_prefix"] is None or isinstance(row["token_prefix"], str)
    assert tok["token"] not in str(body)


def test_a2a_revoked_hidden_by_default_shown_with_include_ended(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    _, headers = make_developer(client, superuser_token_headers)
    agent = _agent(client, headers, "A2A Revoke Agent")
    keep = create_access_token(client, headers, agent["id"])
    gone = create_access_token(client, headers, agent["id"])
    update_access_token(client, headers, agent["id"], gone["id"], is_revoked=True)

    default = _issued(client, headers, kind="a2a_token")
    assert [r["id"] for r in default["data"]] == [keep["id"]]

    ended = _issued(client, headers, kind="a2a_token", include_ended="true")
    by_id = {r["id"]: r for r in ended["data"]}
    assert set(by_id) == {keep["id"], gone["id"]}
    assert by_id[gone["id"]]["status"] == "revoked"
    assert by_id[gone["id"]]["raw_status"] == "revoked"
    assert by_id[keep["id"]]["status"] == "active"


# -- 2. ACP -------------------------------------------------------------------


def test_acp_row_shape_paused_and_revoked(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    _, headers = make_developer(client, superuser_token_headers)
    agent = _agent(client, headers, "ACP Issuer Agent")
    connector = create_acp_connector(client, headers, agent["id"], name="Desk client")
    tok = create_acp_token(client, headers, agent["id"], connector["id"], label="laptop")

    body = _issued(client, headers, kind="acp_token")
    assert len(body["data"]) == 1
    row = body["data"][0]
    assert row["kind"] == "acp_token"
    assert row["id"] == tok["id"]
    assert row["label"] == "laptop"
    assert row["holder"] is None
    assert row["holder_kind"] == "external_client"
    assert row["status"] == "active"
    assert row["raw_status"] == "active"
    assert row["revoked_at"] is None
    assert row["expires_at"]
    assert row["agent"]["id"] == agent["id"]
    assert row["details"]["kind"] == "acp_token"
    assert row["details"]["connector_id"] == connector["id"]
    assert row["details"]["connector_name"] == "Desk client"
    assert row["details"]["mode"] == connector["mode"]
    assert "token" not in row
    if tok.get("token"):
        assert tok["token"] not in str(body)

    # Deactivate the connector -> paused (still listed by default).
    r = client.put(
        acp_connector_url(agent["id"], connector["id"]),
        headers=headers,
        json={"is_active": False},
    )
    assert r.status_code == 200, r.text
    paused = _issued(client, headers, kind="acp_token")["data"]
    assert [(x["id"], x["status"], x["raw_status"]) for x in paused] == [
        (tok["id"], "paused", "paused")
    ]

    # Revoke via route -> revoked beats paused; hidden by default.
    r = client.post(
        f"{acp_connector_url(agent['id'], connector['id'])}/tokens/{tok['id']}/revoke",
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert _issued(client, headers, kind="acp_token")["data"] == []
    ended = _issued(client, headers, kind="acp_token", include_ended="true")["data"]
    assert [(x["id"], x["status"]) for x in ended] == [(tok["id"], "revoked")]
    assert ended[0]["revoked_at"] is None


def test_acp_building_connector_pauses_when_owner_loses_developer_role(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    user, headers = make_developer(client, superuser_token_headers)
    agent = _agent(client, headers, "ACP Building Agent")
    connector = create_acp_connector(client, headers, agent["id"], mode="building")
    tok = create_acp_token(client, headers, agent["id"], connector["id"])
    assert _issued(client, headers, kind="acp_token")["data"][0]["status"] == "active"

    r = client.patch(
        f"{API}/users/{user['id']}/role",
        headers=superuser_token_headers,
        json={"role": "agent-user"},
    )
    assert r.status_code == 200, r.text
    rows = _issued(client, headers, kind="acp_token")["data"]
    assert [(x["id"], x["status"]) for x in rows] == [(tok["id"], "paused")]


# -- 3. kind filter -----------------------------------------------------------


def test_kind_filter_selects_only_requested_kinds_and_rejects_unknown(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    _, headers = make_developer(client, superuser_token_headers)
    agent = _agent(client, headers, "Kind Filter Agent")
    a2a = create_access_token(client, headers, agent["id"])
    connector = create_acp_connector(client, headers, agent["id"])
    acp = create_acp_token(client, headers, agent["id"], connector["id"])

    assert {r["id"] for r in _issued(client, headers)["data"]} == {a2a["id"], acp["id"]}
    assert [r["id"] for r in _issued(client, headers, kind="a2a_token")["data"]] == [a2a["id"]]
    assert [r["id"] for r in _issued(client, headers, kind="acp_token")["data"]] == [acp["id"]]
    both = _issued(client, headers, kind=["a2a_token", "acp_token"])
    assert {r["id"] for r in both["data"]} == {a2a["id"], acp["id"]}
    assert _issued(client, headers, kind="agent_api_key")["data"] == []

    r = client.get(ISSUED_URL, headers=headers, params={"kind": "bogus"})
    assert r.status_code == 422, r.text


# -- 4. Isolation -------------------------------------------------------------


def test_other_user_sees_none_of_the_a2a_or_acp_rows(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    _, a_headers = make_developer(client, superuser_token_headers)
    _, other_headers = make_developer(client, superuser_token_headers)
    _, plain_headers = create_random_user_with_headers(client)
    agent = _agent(client, a_headers, "Private Issuer Agent")
    create_access_token(client, a_headers, agent["id"])
    connector = create_acp_connector(client, a_headers, agent["id"])
    create_acp_token(client, a_headers, agent["id"], connector["id"])

    assert len(_issued(client, a_headers)["data"]) == 2
    assert _issued(client, other_headers)["data"] == []
    assert _issued(client, plain_headers)["data"] == []


# -- 5. Mixed ordering and workspace filter -----------------------------------


def test_mixed_list_is_ordered_by_kind_on_one_agent(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    _, a_headers = make_developer(client, superuser_token_headers)
    b, _ = create_random_user_with_headers(client)
    producer = setup_producer(client, a_headers, name="Mixed Agent")
    r = client.post(
        f"{API}/agents/{producer['id']}/agent-api/keys",
        headers=a_headers,
        json={"issued_to_user_id": b["id"]},
    )
    assert r.status_code == 200, r.text
    key = r.json()
    # Created after the key, so newest-first alone would put them ahead of it.
    connector = create_acp_connector(client, a_headers, producer["id"])
    acp = create_acp_token(client, a_headers, producer["id"], connector["id"])
    a2a = create_access_token(client, a_headers, producer["id"])

    rows = _issued(client, a_headers)["data"]
    assert [(x["kind"], x["id"]) for x in rows] == [
        ("agent_api_key", key["id"]),
        ("a2a_token", a2a["id"]),
        ("acp_token", acp["id"]),
    ]


def test_workspace_filter_applies_to_a2a_and_acp_rows(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    _, headers = make_developer(client, superuser_token_headers)
    ws = create_random_workspace(client, headers)
    default_agent = _agent(client, headers, "Default WS Agent")
    r = client.post(
        f"{API}/agents/", headers=headers,
        json={"name": "Scoped WS Agent", "user_workspace_id": ws["id"]},
    )
    assert r.status_code == 200, r.text
    scoped = r.json()
    drain_tasks()
    ids = {}
    for label, agent in (("default", default_agent), ("scoped", scoped)):
        ids[f"a2a_{label}"] = create_access_token(client, headers, agent["id"])["id"]
        conn = create_acp_connector(client, headers, agent["id"])
        ids[f"acp_{label}"] = create_acp_token(client, headers, agent["id"], conn["id"])["id"]

    assert {x["id"] for x in _issued(client, headers)["data"]} == set(ids.values())
    scoped_rows = _issued(client, headers, user_workspace_id=ws["id"])["data"]
    assert {x["id"] for x in scoped_rows} == {ids["a2a_scoped"], ids["acp_scoped"]}
    assert all(x["agent"]["user_workspace_id"] == ws["id"] for x in scoped_rows)
    default_rows = _issued(client, headers, user_workspace_id="")["data"]
    assert {x["id"] for x in default_rows} == {ids["a2a_default"], ids["acp_default"]}

    r = client.get(ISSUED_URL, headers=headers, params={"user_workspace_id": "nope"})
    assert r.status_code == 400
