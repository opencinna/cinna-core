"""
Account CLI — credentials shared with the caller (``GET /cli/account/credentials``).

Plan: ``docs/plans/cli_shared_agent_api_connections_plan.md`` (§2, §6).

Scenarios:
  1. Owned only: shared fields default off.
  2. Direct share, group-only share, direct + group (one row), ordering,
     workspace filter (owned only), no ``credential_data`` anywhere.
  3. Shared ``agent_api`` connection: key status as the web list.
  4. PUT / DELETE on a shared row -> 403 ``not_owner``; credential unchanged;
     foreign unshared id keeps the old behaviour; ``share-with-agent`` works.
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.agent_api_keys import make_developer, setup_producer
from tests.utils.background_tasks import drain_tasks
from tests.utils.cli import (
    account_cli_headers,
    account_create_credential,
    account_list_credentials,
    bootstrap_account_token,
)
from tests.utils.credential import (
    create_random_credential,
    get_credential,
    get_agent_credentials,
    share_credential_via_api,
    update_credential,
)
from tests.utils.user_group import create_group
from tests.utils.workspace import create_random_workspace

API = settings.API_V1_STR
_BASE = f"{API}/cli"


def _acc(client, headers) -> dict[str, str]:
    jwt, _ = bootstrap_account_token(client, headers)
    return account_cli_headers(jwt)


def _shareable(client, owner_h, cred_type="api_token") -> dict:
    cred = create_random_credential(client, owner_h, credential_type=cred_type)
    update_credential(client, owner_h, cred["id"], allow_sharing=True)
    return cred


def _by_id(rows: list[dict]) -> dict[str, dict]:
    return {r["id"]: r for r in rows}


def test_owned_rows_have_no_shared_fields(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    _, h = make_developer(client, superuser_token_headers)
    acc = _acc(client, h)
    own = account_create_credential(client, acc, name="Mine")["credential"]
    row = _by_id(account_list_credentials(client, acc))[own["id"]]
    assert row["is_shared"] is False
    assert row["owner_email"] is None
    assert row["access_level"] is None
    assert row["share_source"] is None
    assert row["via_groups"] == []
    assert "credential_data" not in row


def test_direct_group_and_both_shares_list_one_row_each_and_filter_owned_only(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    owner, oh = make_developer(client, su)
    rec, rh = make_developer(client, su)
    acc = _acc(client, rh)

    direct = _shareable(client, oh)
    group_only = _shareable(client, oh)
    both = _shareable(client, oh)
    group = create_group(client, su, member_ids=[rec["id"]])
    share_credential_via_api(client, oh, direct["id"], rec["email"])
    share_credential_via_api(client, oh, both["id"], rec["email"])
    for cid in (group_only["id"], both["id"]):
        r = client.post(
            f"{API}/credentials/{cid}/group-shares", headers=oh, json={"group_id": group["id"]}
        )
        assert r.status_code == 200, r.text

    ws = create_random_workspace(client, rh, name="Recipient WS")
    owned_default = account_create_credential(client, acc, name="Zeta owned")["credential"]
    owned_ws = account_create_credential(
        client, acc, name="Alpha owned ws", user_workspace_id=ws["id"]
    )["credential"]

    rows = account_list_credentials(client, acc)
    by_id = _by_id(rows)
    assert len(rows) == len(by_id) == 5

    # ── direct share ──
    d = by_id[direct["id"]]
    assert d["is_shared"] is True
    assert d["owner_email"] == owner["email"]
    assert d["access_level"] == "read"
    assert d["share_source"] == "direct"
    assert d["user_workspace_id"] is None
    assert d["share_count"] == 0
    assert d["status"] in ("complete", "incomplete")
    assert d["via_groups"] == []
    assert "credential_data" not in d

    # ── group-only share ──
    g = by_id[group_only["id"]]
    assert g["is_shared"] is True
    assert g["share_source"] is None
    assert [v["id"] for v in g["via_groups"]] == [group["id"]]
    assert g["via_groups"][0]["name"] == group["name"]

    # ── direct + group: one row ──
    b = by_id[both["id"]]
    assert b["share_source"] == "direct"
    assert [v["id"] for v in b["via_groups"]] == [group["id"]]

    # ── order: owned block (by name) before shared block ──
    flags = [r["is_shared"] for r in rows]
    assert flags == sorted(flags)
    owned_names = [r["name"] for r in rows if not r["is_shared"]]
    assert owned_names == sorted(owned_names, key=str.lower)
    shared_names = [r["name"] for r in rows if r["is_shared"]]
    assert shared_names == sorted(shared_names, key=str.lower)

    # ── workspace filter hits owned rows only ──
    for ws_filter, kept, dropped in (
        ("", owned_default["id"], owned_ws["id"]),
        (ws["id"], owned_ws["id"], owned_default["id"]),
    ):
        filtered = _by_id(account_list_credentials(client, acc, user_workspace_id=ws_filter))
        assert kept in filtered and dropped not in filtered
        assert {direct["id"], group_only["id"], both["id"]} <= set(filtered)


def test_shared_agent_api_connection_status_and_share_with_agent(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    _, oh = make_developer(client, su)
    rec, rh = make_developer(client, su)
    producer = setup_producer(client, oh, name="Shared Conn Producer")
    r = client.post(f"{API}/agents/{producer['id']}/agent-api/connect", headers=oh, json={})
    assert r.status_code == 200, r.text
    cid = r.json()["credential_id"]
    update_credential(client, oh, cid, allow_sharing=True)
    share_credential_via_api(client, oh, cid, rec["email"])

    acc = _acc(client, rh)
    row = _by_id(account_list_credentials(client, acc))[cid]
    assert row["is_shared"] is True and row["type"] == "agent_api"
    web = _by_id(
        client.get(f"{API}/credentials/shared-with-me", headers=rh).json()["data"]
    )[cid]
    assert row["allow_bundle_reuse_effective"] == web["allow_bundle_reuse_effective"]
    assert row["agent_api_key_status"] is None
    assert "credential_data" not in row

    # share-with-agent accepts a shared connection id and links it.
    agent = create_agent_via_api(client, rh, name="Recipient Agent")
    drain_tasks()
    r = client.post(
        f"{_BASE}/account/credentials/{cid}/share-with-agent",
        headers=acc,
        json={"agent_id": agent["id"]},
    )
    assert r.status_code == 200, r.text
    linked = {c["id"] for c in get_agent_credentials(client, rh, agent["id"])["data"]}
    assert cid in linked


def test_put_and_delete_on_shared_row_are_not_owner_and_foreign_ids_unchanged(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    owner, oh = make_developer(client, su)
    rec, rh = make_developer(client, su)
    _, stranger_h = make_developer(client, su)
    shared = _shareable(client, oh)
    share_credential_via_api(client, oh, shared["id"], rec["email"])
    unshared = create_random_credential(client, oh, credential_type="api_token")
    acc = _acc(client, rh)
    url = f"{_BASE}/account/credentials/{shared['id']}"

    r = client.put(url, headers=acc, json={"name": "Hijacked"})
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "not_owner"
    assert owner["email"] in detail["message"]
    assert "only the owner can change it" in detail["message"]

    r = client.delete(url, headers=acc)
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "not_owner"

    after = get_credential(client, oh, shared["id"])
    assert after["name"] == shared["name"]

    # Foreign unshared id: unchanged refusal (not not_owner); missing id: 404.
    r = client.put(
        f"{_BASE}/account/credentials/{unshared['id']}", headers=acc, json={"name": "x"}
    )
    assert r.status_code == 400, r.text
    assert not isinstance(r.json()["detail"], dict) or r.json()["detail"].get("code") != "not_owner"
    r = client.put(
        f"{_BASE}/account/credentials/{uuid.uuid4()}", headers=acc, json={"name": "x"}
    )
    assert r.status_code == 404, r.text
    assert get_credential(client, oh, unshared["id"])["name"] == unshared["name"]
    assert not account_list_credentials(client, _acc(client, stranger_h))
