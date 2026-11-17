"""
Agent-API connection scopes: owner-chosen scopes carried by a shareable
connection (``agent_api`` credential), plan ``docs/plans/connection_scopes_plan.md``.

Scenarios:
  1. Enforcement through a shared connection (V12/V13): recipient refused, owner
     sets a scope, recipient passes with the scope header forwarded, clear -> refused
     again, owner's own agent still passes through its grant; token unchanged.
  2. Union with the personal grant (grant first, no duplicates); identity off
     injects connection scopes only; anonymous caller; forged header stripped;
     external keys unaffected.
  3. Create with scopes (web) and refusals leaving nothing behind; sanitization.
  4. Update route guards (V14): not_owner 403, stranger 404, wrong producer 404,
     not_a_connection 400, unknown_scope 400, superuser allowed; audit event
     without the token value.
  5. Read projections on credentials, shared-with-me, connection info, producer list.
"""
import json
import uuid

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from tests.utils.agent import create_agent_via_api, update_agent
from tests.utils.agent_api_keys import make_developer, setup_producer
from tests.utils.agent_api_requests import (
    bearer,
    capture_proxy,
    connected_consumer,
    read_owner_identity_token,
    scoped_policy,
    set_policy_cache,
)
from tests.utils.background_tasks import drain_tasks
from tests.utils.credential import (
    create_random_credential,
    get_credential,
    get_credential_with_data,
    share_credential_via_api,
    update_credential,
)
from tests.utils.mfa import find_security_events
from tests.utils.user_group import create_group

API = settings.API_V1_STR
_EVENT = "AGENT_API_CONNECTION_SCOPES_UPDATED"


def _base(pid: str) -> str:
    return f"{API}/agents/{pid}/agent-api"


def _scopes_url(pid: str, cred_id: str) -> str:
    return f"{_base(pid)}/connections/{cred_id}/scopes"


def _put_scopes(client, headers, pid, cred_id, scopes):
    return client.put(_scopes_url(pid, cred_id), headers=headers, json={"scopes": scopes})


def _world(client, su, db: Session, identity: bool = True, scopes=None) -> dict:
    """Owner + producer (policy declared) + shared connection + recipient B with an
    agent linked to it and B's identity token."""
    owner, oh = make_developer(client, su)
    rec, rh = make_developer(client, su)
    producer = setup_producer(client, oh, name="Scoped Producer", identity_enabled=identity)
    pid = producer["id"]
    set_policy_cache(db, pid, scoped_policy())
    body: dict = {}
    if scopes is not None:
        body["scopes"] = scopes
    r = client.post(f"{_base(pid)}/connect", headers=oh, json=body)
    assert r.status_code == 200, r.text
    conn = r.json()
    cred_id = conn["credential_id"]
    update_credential(client, oh, cred_id, allow_sharing=True)
    share_credential_via_api(client, oh, cred_id, rec["email"])
    agent_b = create_agent_via_api(client, rh, name="Recipient Consumer")
    drain_tasks()
    ident = read_owner_identity_token(client, rh, agent_b["id"], cred_id)
    token = get_credential_with_data(client, oh, cred_id)["credential_data"]["token"]
    return {
        "owner": owner, "oh": oh, "rec": rec, "rh": rh, "pid": pid, "conn": conn,
        "cred_id": cred_id, "agent_b": agent_b, "ident": ident, "token": token,
        "b_headers": {**bearer(token), "X-Cinna-Caller-Identity": ident},
    }


def _call(client, adapter, pid, headers, path="btc-rate"):
    adapter.agent_api_proxy_calls.clear()
    r = client.get(f"{API}/agent-api/{pid}/{path}", headers=headers)
    fwd = {}
    if adapter.agent_api_proxy_calls:
        fwd = {k.lower(): v for k, v in (adapter.agent_api_proxy_calls[-1]["headers"] or {}).items()}
    return r, fwd


def _create_grant(client, headers, pid, user_id, scopes):
    r = client.post(
        f"{_base(pid)}/grants", headers=headers, json={"user_id": user_id, "scopes": scopes}
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_shared_connection_scope_enforced_for_recipient_and_cleared(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """
    V12 + V13:
      1. Recipient B via a scope-less shared connection -> 403 on the gated path.
      2. Owner PUTs ["btc.read"] -> B passes, forwarded scopes header = btc.read;
         token and credential untouched (no rotation).
      3. Owner PUTs [] -> B refused again; owner's own agent passes via its grant.
    """
    w = _world(client, superuser_token_headers, db)
    pid, cred_id, oh = w["pid"], w["cred_id"], w["oh"]
    with capture_proxy() as adapter:
        # ── Phase 1: refused ──
        r, _ = _call(client, adapter, pid, w["b_headers"])
        assert r.status_code == 403, r.text
        assert "btc.read" in r.text

        # ── Phase 2: owner sets the scope ──
        r = _put_scopes(client, oh, pid, cred_id, ["btc.read"])
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["credential_id"] == cred_id and body["producer_agent_id"] == pid
        assert body["token_id"] == w["conn"]["token_id"]
        assert body["scopes"] == ["btc.read"]
        r, fwd = _call(client, adapter, pid, w["b_headers"])
        assert r.status_code == 200, r.text
        assert fwd["x-cinna-caller-scopes"] == "btc.read"
        # same bearer value still works (no rotation)
        again = get_credential_with_data(client, oh, cred_id)["credential_data"]["token"]
        assert again == w["token"]

        # ── Phase 3: clear -> refused; owner's agent passes through its grant ──
        r = _put_scopes(client, oh, pid, cred_id, [])
        assert r.status_code == 200 and r.json()["scopes"] == []
        r, _ = _call(client, adapter, pid, w["b_headers"])
        assert r.status_code == 403, r.text

        own = connected_consumer(client, oh, pid, name="Owner Consumer")
        me = client.get(f"{API}/users/me", headers=oh).json()
        r, _ = _call(client, adapter, pid, own["headers"])
        assert r.status_code == 403, r.text
        _create_grant(client, oh, pid, me["id"], ["btc.read"])
        r, fwd = _call(client, adapter, pid, own["headers"])
        assert r.status_code == 200, r.text
        assert fwd["x-cinna-caller-scopes"] == "btc.read"


def test_grant_and_connection_scopes_union_and_identity_modes(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """
    Union and gating details:
      1. Grant btc.admin + connection btc.read -> header "btc.admin btc.read".
      2. Grant holds both -> no duplicates, grant order first.
      3. Identity off: connection scopes only (grant not injected), gated path passes.
      4. Identity on, no identity header: anonymous, connection scopes still apply.
      5. Forged inbound scopes header on a scope-less connection is stripped -> 403.
    """
    w = _world(client, superuser_token_headers, db, scopes=["btc.read"])
    pid, oh = w["pid"], w["oh"]
    grant = _create_grant(client, oh, pid, w["rec"]["id"], ["btc.admin"])
    with capture_proxy() as adapter:
        # ── 1 ──
        r, fwd = _call(client, adapter, pid, w["b_headers"])
        assert r.status_code == 200, r.text
        assert fwd["x-cinna-caller-scopes"] == "btc.admin btc.read"

        # ── 2 ──
        r = client.put(
            f"{_base(pid)}/grants/{grant['id']}", headers=oh,
            json={"scopes": ["btc.read", "btc.admin"]},
        )
        assert r.status_code == 200, r.text
        r, fwd = _call(client, adapter, pid, w["b_headers"])
        assert fwd["x-cinna-caller-scopes"] == "btc.read btc.admin"

        # ── 3 ──
        update_agent(client, oh, pid, agent_api_identity_enabled=False)
        r, fwd = _call(client, adapter, pid, w["b_headers"])
        assert r.status_code == 200, r.text
        assert fwd["x-cinna-caller-scopes"] == "btc.read"
        update_agent(client, oh, pid, agent_api_identity_enabled=True)

        # ── 4 ──
        r, fwd = _call(client, adapter, pid, bearer(w["token"]))
        assert r.status_code == 200, r.text
        assert "x-cinna-caller-user-id" not in fwd
        assert fwd["x-cinna-caller-scopes"] == "btc.read"

        # ── 5 ──
        assert _put_scopes(client, oh, pid, w["cred_id"], []).status_code == 200
        forged = {**bearer(w["token"]), "X-Cinna-Caller-Scopes": "btc.read"}
        r, fwd = _call(client, adapter, pid, forged)
        assert r.status_code == 403, r.text
        assert "x-cinna-caller-scopes" not in fwd


def test_external_key_ignores_connection_scopes(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """An issued key with no grant is refused on the gated path even though another
    connection on the same producer carries btc.read."""
    w = _world(client, superuser_token_headers, db, scopes=["btc.read"])
    pid, oh = w["pid"], w["oh"]
    r = client.post(
        f"{_base(pid)}/keys", headers=oh,
        json={"issued_to_user_id": w["rec"]["id"], "label": "k"},
    )
    assert r.status_code == 200, r.text
    key = r.json()
    rv = client.post(
        f"{API}/credentials/{key['credential_id']}/agent-api-key/reveal", headers=w["rh"]
    )
    assert rv.status_code == 200, rv.text
    with capture_proxy() as adapter:
        r, _ = _call(client, adapter, pid, bearer(rv.json()["token"]))
        assert r.status_code == 403, r.text
        # the connection itself still passes
        r, _ = _call(client, adapter, pid, w["b_headers"])
        assert r.status_code == 200, r.text

        # ── Update route refuses an issued key credential ──
        r = _put_scopes(client, oh, pid, key["credential_id"], ["btc.read"])
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["code"] == "not_a_connection"


def test_connect_with_scopes_and_refusals_leave_nothing_behind(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """
    Create (web) with scopes:
      1. Unknown scope -> 400 unknown_scope; connection and credential counts unchanged.
      2. Sanitized scopes stored; response carries them; enforced on the first call.
      3. Producer with no catalog: any scope 400, [] fine.
    """
    _, oh = make_developer(client, superuser_token_headers)
    producer = setup_producer(client, oh, name="Create Scopes Producer", identity_enabled=True)
    pid = producer["id"]
    set_policy_cache(db, pid, scoped_policy())

    def counts():
        conns = client.get(f"{_base(pid)}/connections", headers=oh).json()["count"]
        creds = client.get(f"{API}/credentials/", headers=oh).json()["count"]
        return conns, creds

    before = counts()
    # ── Phase 1 ──
    r = client.post(f"{_base(pid)}/connect", headers=oh, json={"scopes": ["nope"]})
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "unknown_scope"
    assert counts() == before

    # ── Phase 2 ──
    r = client.post(
        f"{_base(pid)}/connect", headers=oh,
        json={"scopes": [" btc.read ", "btc.read", "", "a b"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["scopes"] == ["btc.read"]
    token = get_credential_with_data(client, oh, r.json()["credential_id"])["credential_data"]["token"]
    with capture_proxy() as adapter:
        resp, fwd = _call(client, adapter, pid, bearer(token))
        assert resp.status_code == 200, resp.text
        assert fwd["x-cinna-caller-scopes"] == "btc.read"
    # default is []
    r = client.post(f"{_base(pid)}/connect", headers=oh, json={})
    assert r.status_code == 200 and r.json()["scopes"] == []

    # ── Phase 3: empty catalog ──
    bare = setup_producer(client, oh, name="Bare Producer", identity_enabled=True)
    r = client.post(f"{_base(bare['id'])}/connect", headers=oh, json={"scopes": ["btc.read"]})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "unknown_scope"
    r = client.post(f"{_base(bare['id'])}/connect", headers=oh, json={"scopes": []})
    assert r.status_code == 200, r.text


def test_update_scopes_guards_and_audit_event(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """
    V14 on the update route:
      1. Recipient (holds the credential, not the producer) -> 403 not_owner.
      2. Stranger -> 404 not_found; ghost credential -> 404; wrong producer path -> 404.
      3. Unknown scope -> 400 unknown_scope, scopes unchanged.
      4. Superuser may update.
      5. Owner update writes an audit event with before/after, prefix, no token value.
    """
    w = _world(client, superuser_token_headers, db)
    pid, cred_id, oh = w["pid"], w["cred_id"], w["oh"]

    r = _put_scopes(client, w["rh"], pid, cred_id, ["btc.read"])
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "not_owner"

    _, stranger_h = make_developer(client, superuser_token_headers)
    for cid in (cred_id, str(uuid.uuid4())):
        r = _put_scopes(client, stranger_h, pid, cid, ["btc.read"])
        assert r.status_code == 404, r.text
        assert r.json()["detail"]["code"] == "not_found"

    other = setup_producer(client, oh, name="Other Producer", identity_enabled=True)
    r = _put_scopes(client, oh, other["id"], cred_id, [])
    assert r.status_code == 404, r.text
    assert r.json()["detail"]["code"] == "not_found"

    r = _put_scopes(client, oh, pid, cred_id, ["nope"])
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "unknown_scope"
    assert client.get(f"{API}/credentials/{cred_id}/agent-api-connection", headers=oh).json()[
        "connection_scopes"
    ] == []
    # body is required
    assert client.put(_scopes_url(pid, cred_id), headers=oh, json={}).status_code == 422

    r = _put_scopes(client, superuser_token_headers, pid, cred_id, ["btc.admin"])
    assert r.status_code == 200, r.text
    assert r.json()["scopes"] == ["btc.admin"]

    r = _put_scopes(client, oh, pid, cred_id, ["btc.read"])
    assert r.status_code == 200, r.text
    events = find_security_events(client, oh, _EVENT)
    assert events, "no audit event for the owner's update"
    ev = events[0]
    raw = json.dumps(ev)
    assert w["token"] not in raw
    details = ev["details"]
    if isinstance(details, str):
        details = json.loads(details)
    assert details["producer_agent_id"] == pid
    assert details["credential_id"] == cred_id
    assert details["scopes_before"] == ["btc.admin"]
    assert details["scopes_after"] == ["btc.read"]
    assert details["token_prefix"] and details["token_prefix"] != w["token"]
    assert w["token"].startswith(details["token_prefix"])


def test_connection_scope_read_projections(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """Scopes surface on credential detail, shared-with-me, connection info and the
    producer's connection list; non-connection credentials report null."""
    w = _world(client, superuser_token_headers, db, scopes=["btc.read"])
    pid, cred_id, oh, rh = w["pid"], w["cred_id"], w["oh"], w["rh"]

    assert get_credential(client, oh, cred_id)["agent_api_connection_scopes"] == ["btc.read"]
    assert get_credential(client, rh, cred_id)["agent_api_connection_scopes"] == ["btc.read"]
    plain = create_random_credential(client, oh, credential_type="api_token")
    assert get_credential(client, oh, plain["id"])["agent_api_connection_scopes"] is None

    r = client.get(f"{API}/credentials/shared-with-me", headers=rh)
    assert r.status_code == 200, r.text
    row = next(x for x in r.json()["data"] if x["id"] == cred_id)
    assert row["agent_api_connection_scopes"] == ["btc.read"]

    r = client.get(f"{API}/credentials/{cred_id}/agent-api-connection", headers=oh)
    assert r.status_code == 200, r.text
    assert r.json()["connection_scopes"] == ["btc.read"]
    assert r.json()["can_edit_scopes"] is True
    # connection info is the credential owner's view (a recipient gets 404 today)
    assert client.get(
        f"{API}/credentials/{cred_id}/agent-api-connection", headers=rh
    ).status_code == 404

    r = client.get(f"{_base(pid)}/connections", headers=oh)
    assert r.status_code == 200, r.text
    prow = next(x for x in r.json()["data"] if x["credential_id"] == cred_id)
    assert prow["connection_scopes"] == ["btc.read"]


def test_group_recipient_gets_connection_scopes_d10(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """
    D10: connection shared with a group. A member links it to their agent; the call
    is refused without the connection scope and passes (scope forwarded) with it.
    """
    owner, oh = make_developer(client, superuser_token_headers)
    member, mh = make_developer(client, superuser_token_headers)
    producer = setup_producer(client, oh, name="Group Scoped Producer", identity_enabled=True)
    pid = producer["id"]
    set_policy_cache(db, pid, scoped_policy())
    r = client.post(f"{_base(pid)}/connect", headers=oh, json={})
    assert r.status_code == 200, r.text
    cred_id = r.json()["credential_id"]
    update_credential(client, oh, cred_id, allow_sharing=True)
    group = create_group(client, superuser_token_headers, member_ids=[member["id"]])
    r = client.post(
        f"{API}/credentials/{cred_id}/group-shares", headers=oh, json={"group_id": group["id"]}
    )
    assert r.status_code == 200, r.text
    agent = create_agent_via_api(client, mh, name="Group Member Consumer")
    drain_tasks()
    ident = read_owner_identity_token(client, mh, agent["id"], cred_id)
    token = get_credential_with_data(client, oh, cred_id)["credential_data"]["token"]
    headers = {**bearer(token), "X-Cinna-Caller-Identity": ident}
    with capture_proxy() as adapter:
        r, _ = _call(client, adapter, pid, headers)
        assert r.status_code == 403, r.text
        assert _put_scopes(client, oh, pid, cred_id, ["btc.read"]).status_code == 200
        r, fwd = _call(client, adapter, pid, headers)
        assert r.status_code == 200, r.text
        assert fwd["x-cinna-caller-scopes"] == "btc.read"
        # the member holds the credential through the group but cannot edit scopes
        r = _put_scopes(client, mh, pid, cred_id, [])
        assert r.status_code == 403, r.text
        assert r.json()["detail"]["code"] == "not_owner"


def test_update_scopes_after_producer_deleted_is_409(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """Producer agent deleted: credential holder gets 409 producer_removed, stranger 404."""
    w = _world(client, superuser_token_headers, db)
    pid, cred_id = w["pid"], w["cred_id"]
    r = client.delete(f"{API}/agents/{pid}", headers=w["oh"])
    assert r.status_code == 200, r.text
    drain_tasks()
    for h in (w["oh"], w["rh"]):
        r = _put_scopes(client, h, pid, cred_id, ["btc.read"])
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "producer_removed"
    _, stranger_h = make_developer(client, superuser_token_headers)
    r = _put_scopes(client, stranger_h, pid, cred_id, ["btc.read"])
    assert r.status_code == 404, r.text


def test_invalid_identity_header_keeps_connection_scopes(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """A garbage (not absent) identity token: connection scopes still apply, no crash."""
    w = _world(client, superuser_token_headers, db, scopes=["btc.read"])
    pid = w["pid"]
    bad = {**bearer(w["token"]), "X-Cinna-Caller-Identity": "not-a-valid-token"}
    with capture_proxy() as adapter:
        r, fwd = _call(client, adapter, pid, bad)
        assert r.status_code == 200, r.text
        assert fwd["x-cinna-caller-scopes"] == "btc.read"
        assert "x-cinna-caller-user-id" not in fwd


def test_noop_scopes_update_returns_200_and_emits_no_event(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """Same scopes again: 200, same body, no additional audit event."""
    w = _world(client, superuser_token_headers, db)
    pid, cred_id, oh = w["pid"], w["cred_id"], w["oh"]
    assert _put_scopes(client, oh, pid, cred_id, ["btc.read"]).status_code == 200
    n = len(find_security_events(client, oh, _EVENT))
    assert n >= 1
    r = _put_scopes(client, oh, pid, cred_id, ["btc.read"])
    assert r.status_code == 200, r.text
    assert r.json()["scopes"] == ["btc.read"]
    assert len(find_security_events(client, oh, _EVENT)) == n
