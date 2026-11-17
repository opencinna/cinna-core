"""
Account CLI — agent-api connections of a consumer agent.

Plan: ``docs/plans/cli_shared_agent_api_connections_plan.md`` (§3, §4, §6).

Routes under ``/api/v1/cli/account/agents/{agent_id}/agent-api-connections``:
  C2 GET ``''``; C3 GET ``/{credential_id}/spec``;
  C4 POST + GET ``/{credential_id}/capability-requests``.

Scenarios:
  1. C2 listing: ownership 404, slot vs id ``sdk_lookup``, env-origin URLs,
     no token anywhere, shared connection, unusable (paused / revoked).
  2. C3 refusals: 404 unlinked, 409 unusable.
  3. C4 filing: 201 + attribution, 422/413 bodies, 404/409, scrubbing,
     per-requester rate limit shared with the env route, list scoped to caller,
     non-developer 403.
"""
import json
import uuid
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api, update_agent
from tests.utils.agent_api_keys import make_developer, setup_producer
from tests.utils.agent_api_requests import (
    bearer,
    connected_consumer,
    file_request,
    requests_url,
    valid_body,
)
from tests.utils.background_tasks import drain_tasks
from tests.utils.cli import account_cli_headers, bootstrap_account_token
from tests.utils.credential import (
    create_random_credential,
    link_credential_to_agent,
    share_credential_via_api,
    update_credential,
)

API = settings.API_V1_STR
_BASE = f"{API}/cli/account/agents"

_ROW_KEYS = {
    "credential_id", "name", "slot", "is_shared", "owner_email",
    "producer_agent_id", "producer_agent_name", "base_url", "spec_url",
    "improvement_requests_url", "key_status", "scopes", "usable",
    "unavailable_reason", "sdk_lookup",
}


def _acc(client, headers) -> dict[str, str]:
    jwt, _ = bootstrap_account_token(client, headers)
    return account_cli_headers(jwt)


def _list_url(agent_id) -> str:
    return f"{_BASE}/{agent_id}/agent-api-connections"


def _cap_url(agent_id, credential_id) -> str:
    return f"{_list_url(agent_id)}/{credential_id}/capability-requests"


def _rows(client, acc, agent_id) -> dict[str, dict]:
    r = client.get(_list_url(agent_id), headers=acc)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == len(body["data"])
    return {row["credential_id"]: row for row in body["data"]}


def _issue_external_key(client, owner_h, producer_id, recipient, recipient_h):
    r = client.post(
        f"{API}/agents/{producer_id}/agent-api/keys",
        headers=owner_h,
        json={"issued_to_user_id": recipient["id"], "label": "t-key"},
    )
    assert r.status_code == 200, r.text
    key = r.json()
    rv = client.post(
        f"{API}/credentials/{key['credential_id']}/agent-api-key/reveal", headers=recipient_h
    )
    assert rv.status_code == 200, rv.text
    return key, rv.json()["token"]


_AGENT_NOT_FOUND = "Agent not found"


def _not_linked(credential_id: object) -> str:
    return f"No agent API connection {credential_id} is linked to this agent"


def _assert_not_found(r, message: str = _AGENT_NOT_FOUND) -> None:
    assert r.status_code == 404, r.text
    assert r.json()["detail"] == {"code": "not_found", "message": message}


def test_connections_listing_ownership_urls_sdk_lookup_and_no_token(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    _, h = make_developer(client, su)
    producer = setup_producer(client, h, name="Conn Producer")
    pid = producer["id"]
    c = connected_consumer(client, h, pid, name="Conn Consumer")
    consumer_id, cred_id = c["agent"]["id"], c["connection"]["credential_id"]
    acc = _acc(client, h)

    # ── ownership / auth ──
    _, other_h = make_developer(client, su)
    other_acc = _acc(client, other_h)
    _assert_not_found(client.get(_list_url(consumer_id), headers=other_acc))
    _assert_not_found(client.get(_list_url(uuid.uuid4()), headers=acc))
    assert client.get(_list_url(consumer_id), headers=h).status_code == 401

    # ── no slot: sdk_lookup is the credential id ──
    r = client.get(_list_url(consumer_id), headers=acc)
    assert r.status_code == 200, r.text
    row = _rows(client, acc, consumer_id)[cred_id]
    assert set(row) == _ROW_KEYS
    assert row["slot"] is None
    assert row["sdk_lookup"] == cred_id
    assert row["is_shared"] is False and row["owner_email"] is None
    assert row["producer_agent_id"] == pid
    assert row["producer_agent_name"] == "Conn Producer"
    assert row["usable"] is True and row["unavailable_reason"] is None
    assert row["key_status"] == "active"
    assert row["improvement_requests_url"] == (
        row["base_url"].rstrip("/") + "/_cinna/improvement-requests"
    )
    internal = urlsplit(settings.AGENT_ENV_BACKEND_URL).netloc
    for field in ("base_url", "spec_url", "improvement_requests_url"):
        assert urlsplit(row[field]).netloc == internal
    assert pid in row["base_url"]

    # ── no token, neither connection nor identity ──
    assert c["connection"]["token"] not in r.text
    assert c["identity"] not in r.text
    assert '"token"' not in r.text

    # ── slot: sdk_lookup is the slot ──
    update_credential(client, h, cred_id, service_uri="orders-api")
    row = _rows(client, acc, consumer_id)[cred_id]
    assert row["slot"] == "orders-api"
    assert row["sdk_lookup"] == "orders-api"

    # ── unusable: producer API disabled ──
    update_agent(client, h, pid, agent_api_enabled=False)
    row = _rows(client, acc, consumer_id)[cred_id]
    assert row["usable"] is False
    assert row["unavailable_reason"] == "key_paused"
    assert row["key_status"] == "api_disabled"
    assert row["improvement_requests_url"] is None
    update_agent(client, h, pid, agent_api_enabled=True)
    assert _rows(client, acc, consumer_id)[cred_id]["usable"] is True


def test_shared_connection_and_revoked_key_rows(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    owner, oh = make_developer(client, su)
    rec, rh = make_developer(client, su)
    producer = setup_producer(client, oh, name="Shared Producer")
    pid = producer["id"]

    # ── shared (ownerless) connection linked to the recipient's agent ──
    r = client.post(f"{API}/agents/{pid}/agent-api/connect", headers=oh, json={})
    assert r.status_code == 200, r.text
    shared_id = r.json()["credential_id"]
    update_credential(client, oh, shared_id, allow_sharing=True)
    share_credential_via_api(client, oh, shared_id, rec["email"])
    agent = create_agent_via_api(client, rh, name="Recipient Consumer")
    drain_tasks()
    link_credential_to_agent(client, rh, agent["id"], shared_id)
    drain_tasks()

    acc = _acc(client, rh)
    row = _rows(client, acc, agent["id"])[shared_id]
    assert row["is_shared"] is True
    assert row["owner_email"] == owner["email"]
    assert row["producer_agent_id"] == pid
    assert row["producer_agent_name"] == "Shared Producer"
    assert row["usable"] is True

    # ── issued key, then revoked by the issuer -> key_revoked ──
    key, _ = _issue_external_key(client, oh, pid, rec, rh)
    link_credential_to_agent(client, rh, agent["id"], key["credential_id"])
    drain_tasks()
    assert _rows(client, acc, agent["id"])[key["credential_id"]]["usable"] is True
    rv = client.post(f"{API}/agents/{pid}/agent-api/keys/{key['id']}/revoke", headers=oh)
    assert rv.status_code == 200, rv.text
    revoked = _rows(client, acc, agent["id"])[key["credential_id"]]
    assert revoked["usable"] is False
    assert revoked["unavailable_reason"] == "key_revoked"
    assert revoked["key_status"] == "revoked_by_issuer"
    assert revoked["improvement_requests_url"] is None

    # ── C3 / C4 refusals: unusable -> 409, not linked -> 404 ──
    spec = f"{_list_url(agent['id'])}/{key['credential_id']}/spec"
    r = client.get(spec, headers=acc)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "connection_unusable"
    r = client.post(
        _cap_url(agent["id"], key["credential_id"]), headers=acc, json=valid_body()
    )
    assert r.status_code == 409 and r.json()["detail"]["code"] == "connection_unusable"
    # Listing stays available on an unusable connection (earlier requests).
    r = client.get(_cap_url(agent["id"], key["credential_id"]), headers=acc)
    assert r.status_code == 200, r.text

    unlinked = create_random_credential(client, rh, credential_type="api_token")
    ghost_id = uuid.uuid4()
    for url, missing in (
        (f"{_list_url(agent['id'])}/{unlinked['id']}/spec", unlinked["id"]),
        (_cap_url(agent["id"], unlinked["id"]), unlinked["id"]),
        (f"{_list_url(agent['id'])}/{ghost_id}/spec", ghost_id),
    ):
        _assert_not_found(client.get(url, headers=acc), _not_linked(missing))
    _assert_not_found(
        client.post(_cap_url(agent["id"], unlinked["id"]), headers=acc, json=valid_body()),
        _not_linked(unlinked["id"]),
    )
    # Another user's consumer agent: 404 on every route.
    _, other_h = make_developer(client, su)
    other_acc = _acc(client, other_h)
    _assert_not_found(client.get(_cap_url(agent["id"], shared_id), headers=other_acc))
    _assert_not_found(
        client.post(_cap_url(agent["id"], shared_id), headers=other_acc, json=valid_body())
    )


def test_capability_request_filing_attribution_validation_scrub_and_listing(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    _, h = make_developer(client, su)
    pid = setup_producer(client, h, name="Cap Producer")["id"]
    c = connected_consumer(client, h, pid, name="Cap Consumer")
    consumer_id, cred_id = c["agent"]["id"], c["connection"]["credential_id"]
    acc = _acc(client, h)
    url = _cap_url(consumer_id, cred_id)

    # ── 201 + attribution ──
    r = client.post(url, headers=acc, json=valid_body(title="First via CLI"))
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["status"] == "new"
    assert created["title"] == "First via CLI"
    inbox = client.get(f"{API}/agents/{pid}/improvement-requests", headers=h).json()["data"]
    row = next(x for x in inbox if x["id"] == created["id"])
    assert row["kind"] == "agent_api"
    assert row["source_agent_id"] == consumer_id
    detail = client.get(f"{API}/improvement-requests/{created['id']}", headers=h).json()
    assert detail["context"]["consumer"]["agent_name"] == "Cap Consumer"

    # ── 422: errors + expected, same as the public route ──
    bad = client.post(url, headers=acc, json=valid_body(bogus="x"))
    assert bad.status_code == 422, bad.text
    d = bad.json()["detail"]
    assert d["code"] == "invalid_capability_request"
    assert any(err["loc"] == ["bogus"] for err in d["errors"])
    public = client.post(requests_url(pid), headers=c["headers"], json=valid_body(bogus="x"))
    assert public.status_code == 422
    assert d["expected"] == public.json()["detail"]["expected"]
    assert d["message"]

    # ── 413 ──
    big = client.post(url, headers=acc, json=valid_body(example_request="x" * (70 * 1024)))
    assert big.status_code == 413, big.text
    assert big.json()["detail"]["code"] == "too_large"

    # ── scrub: the connection token in free text is redacted ──
    secret = c["connection"]["token"]
    scrubbed = client.post(
        url, headers=acc, json=valid_body(title="Scrub me", need=f"Token {secret} fails")
    )
    assert scrubbed.status_code == 201, scrubbed.text
    assert secret not in scrubbed.text
    producer_view = client.get(
        f"{API}/improvement-requests/{scrubbed.json()['id']}", headers=h
    )
    assert secret not in json.dumps(producer_view.json())

    # ── list: own requests only, newest first ──
    t, t_h = make_developer(client, su)
    _, t_key = _issue_external_key(client, h, pid, t, t_h)
    theirs = file_request(client, pid, bearer(t_key), title="Someone else")
    listing = client.get(url, headers=acc)
    assert listing.status_code == 200, listing.text
    ids = [x["id"] for x in listing.json()["data"]]
    assert listing.json()["count"] == len(ids) == 2
    assert theirs["id"] not in ids
    assert ids == [scrubbed.json()["id"], created["id"]]

    # ── non-developer account -> 403 on POST; reads stay open ──
    demote = client.patch(
        f"{API}/users/{client.get(f'{API}/users/me', headers=h).json()['id']}/role",
        headers=su,
        json={"role": "agent-user"},
    )
    assert demote.status_code == 200, demote.text
    r = client.post(url, headers=acc, json=valid_body(title="Demoted attempt"))
    assert r.status_code == 403, r.text
    assert isinstance(r.json()["detail"], str)
    assert client.get(url, headers=acc).status_code == 200


def test_cli_filing_shares_the_per_requester_rate_limit_with_the_env_route(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    _, h = make_developer(client, su)
    pid = setup_producer(client, h, name="CLI Rate Producer")["id"]
    c = connected_consumer(client, h, pid, name="CLI Rate Consumer")
    acc = _acc(client, h)
    url = _cap_url(c["agent"]["id"], c["connection"]["credential_id"])

    for i in range(5):
        file_request(client, pid, c["headers"], title=f"Env request {i}")
    for i in range(5):
        r = client.post(url, headers=acc, json=valid_body(title=f"CLI request {i}"))
        assert r.status_code == 201, r.text

    r = client.post(url, headers=acc, json=valid_body(title="One too many"))
    assert r.status_code == 429, r.text
    assert int(r.headers["Retry-After"]) >= 1
    assert r.json()["detail"]["code"] == "rate_limited"
    assert client.get(url, headers=acc).json()["count"] == 10


def _spec_url(agent_id, credential_id) -> str:
    return f"{_list_url(agent_id)}/{credential_id}/spec"


def test_connection_spec_success_and_coded_producer_refusals(
    client: TestClient, superuser_token_headers: dict[str, str], monkeypatch
) -> None:
    """C3: raw spec on success; each producer-side refusal maps to its code."""
    from app.services.agent_api.agent_api_service import (
        AgentApiDisabledError,
        AgentApiError,
        AgentApiNotRunningError,
        AgentApiService,
    )

    su = superuser_token_headers
    _, h = make_developer(client, su)
    pid = setup_producer(client, h, name="Spec Producer")["id"]
    c = connected_consumer(client, h, pid, name="Spec Consumer")
    url = _spec_url(c["agent"]["id"], c["connection"]["credential_id"])
    acc = _acc(client, h)

    env = object()
    state: dict = {"policy": {"expose_spec": True}, "spec": {}, "resolve_exc": None}
    spec = {"openapi": "3.1.0", "info": {"title": "Producer", "version": "9.9"}, "paths": {}}
    state["spec"] = spec

    async def fake_resolve(db, producer):
        if state["resolve_exc"]:
            raise state["resolve_exc"]
        return env

    async def fake_policy(db, environment):
        return state["policy"]

    async def fake_spec(db, environment, **kw):
        if isinstance(state["spec"], Exception):
            raise state["spec"]
        return state["spec"]

    monkeypatch.setattr(AgentApiService, "resolve_running_producer_env", staticmethod(fake_resolve))
    monkeypatch.setattr(AgentApiService, "load_policy", staticmethod(fake_policy))
    monkeypatch.setattr(AgentApiService, "get_spec", staticmethod(fake_spec))
    monkeypatch.setattr(
        AgentApiService, "update_last_activity", staticmethod(lambda db, e: None)
    )

    # ── success: raw producer spec, no token ──
    r = client.get(url, headers=acc)
    assert r.status_code == 200, r.text
    assert r.json() == spec
    assert c["connection"]["token"] not in r.text

    # ── 403 spec_not_exposed ──
    state["policy"] = {"expose_spec": False}
    r = client.get(url, headers=acc)
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["code"] == "spec_not_exposed"
    assert r.json()["detail"]["message"]
    state["policy"] = {"expose_spec": True}

    # ── 400 api_disabled ──
    state["resolve_exc"] = AgentApiDisabledError()
    r = client.get(url, headers=acc)
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "api_disabled"
    assert r.json()["detail"]["message"]

    # ── 503 env_unavailable ──
    state["resolve_exc"] = AgentApiNotRunningError()
    r = client.get(url, headers=acc)
    assert r.status_code == 503, r.text
    assert r.json()["detail"]["code"] == "env_unavailable"
    assert r.json()["detail"]["message"]
    state["resolve_exc"] = None

    # ── 502 producer_error ──
    state["spec"] = AgentApiError("Spec harvest failed", status_code=502)
    r = client.get(url, headers=acc)
    assert r.status_code == 502, r.text
    assert r.json()["detail"] == {"code": "producer_error", "message": "Spec harvest failed"}

    # ── auth still required ──
    assert client.get(url, headers=h).status_code == 401


def test_capability_request_malformed_inputs_400_vs_422(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """C4: only an invalid Content-Length is 400 invalid_request; bad bodies are 422."""
    su = superuser_token_headers
    _, h = make_developer(client, su)
    pid = setup_producer(client, h, name="Malformed Producer")["id"]
    c = connected_consumer(client, h, pid, name="Malformed Consumer")
    acc = _acc(client, h)
    url = _cap_url(c["agent"]["id"], c["connection"]["credential_id"])

    # ── invalid Content-Length header -> 400 invalid_request ──
    r = client.post(
        url,
        headers={**acc, "Content-Type": "application/json", "Content-Length": "abc"},
        content=json.dumps(valid_body()),
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "invalid_request"
    assert r.json()["detail"]["message"]

    # ── non-JSON and non-object bodies -> 422 invalid_capability_request ──
    for content in (b"not json", b"[1, 2]", b'"str"', b""):
        r = client.post(
            url, headers={**acc, "Content-Type": "application/json"}, content=content
        )
        assert r.status_code == 422, (content, r.text)
        assert r.json()["detail"]["code"] == "invalid_capability_request"

    # nothing was filed by the refused attempts
    assert client.get(url, headers=acc).json()["count"] == 0


def test_ambiguous_slot_falls_back_to_credential_id_for_sdk_lookup(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """C2: a slot carried by more than one linked credential is not a usable lookup."""
    su = superuser_token_headers
    _, oh = make_developer(client, su)
    rec, rh = make_developer(client, su)
    owner_pid = setup_producer(client, oh, name="Slot Owner Producer")["id"]
    rec_pid = setup_producer(client, rh, name="Slot Recipient Producer")["id"]

    # A: owner's connection, shared with the recipient, carrying a slot.
    r = client.post(f"{API}/agents/{owner_pid}/agent-api/connect", headers=oh, json={})
    assert r.status_code == 200, r.text
    a_id = r.json()["credential_id"]
    update_credential(client, oh, a_id, allow_sharing=True, service_uri="shared-slot")
    share_credential_via_api(client, oh, a_id, rec["email"])

    agent = create_agent_via_api(client, rh, name="Slot Consumer")
    drain_tasks()
    link_credential_to_agent(client, rh, agent["id"], a_id)
    drain_tasks()
    acc = _acc(client, rh)

    # ── control: a unique slot is the lookup ──
    row = _rows(client, acc, agent["id"])[a_id]
    assert row["slot"] == "shared-slot"
    assert row["sdk_lookup"] == "shared-slot"

    # ── B: recipient's own connection with the same slot -> both fall back to ids ──
    r = client.post(f"{API}/agents/{rec_pid}/agent-api/connect", headers=rh, json={})
    assert r.status_code == 200, r.text
    b_id = r.json()["credential_id"]
    update_credential(client, rh, b_id, service_uri="shared-slot")
    link_credential_to_agent(client, rh, agent["id"], b_id)
    drain_tasks()
    rows = _rows(client, acc, agent["id"])
    for cid in (a_id, b_id):
        assert rows[cid]["slot"] == "shared-slot"
        assert rows[cid]["sdk_lookup"] == cid

    # ── a distinct slot on B makes both unambiguous again ──
    update_credential(client, rh, b_id, service_uri="own-slot")
    rows = _rows(client, acc, agent["id"])
    assert rows[a_id]["sdk_lookup"] == "shared-slot"
    assert rows[b_id]["sdk_lookup"] == "own-slot"

    # ── a non-agent_api credential sharing the slot also triggers the fallback ──
    other = create_random_credential(client, rh, credential_type="api_token")
    update_credential(client, rh, other["id"], service_uri="own-slot")
    link_credential_to_agent(client, rh, agent["id"], other["id"])
    drain_tasks()
    rows = _rows(client, acc, agent["id"])
    assert other["id"] not in rows
    assert rows[b_id]["slot"] == "own-slot"
    assert rows[b_id]["sdk_lookup"] == b_id
    assert rows[a_id]["sdk_lookup"] == "shared-slot"


def test_capability_request_listing_survives_unusable_connection_but_post_is_409(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """C4 GET keeps earlier requests visible once the connection is unusable."""
    su = superuser_token_headers
    _, oh = make_developer(client, su)
    rec, rh = make_developer(client, su)
    pid = setup_producer(client, oh, name="Unusable List Producer")["id"]
    agent = create_agent_via_api(client, rh, name="Unusable List Consumer")
    drain_tasks()
    key, _ = _issue_external_key(client, oh, pid, rec, rh)
    cred_id = key["credential_id"]
    link_credential_to_agent(client, rh, agent["id"], cred_id)
    drain_tasks()
    acc = _acc(client, rh)
    url = _cap_url(agent["id"], cred_id)

    r = client.post(url, headers=acc, json=valid_body(title="Filed while usable"))
    assert r.status_code == 201, r.text
    filed_id = r.json()["id"]

    def _assert_listing_visible_post_refused() -> None:
        listing = client.get(url, headers=acc)
        assert listing.status_code == 200, listing.text
        assert listing.json()["count"] == 1
        assert [x["id"] for x in listing.json()["data"]] == [filed_id]
        refused = client.post(url, headers=acc, json=valid_body(title="Too late"))
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["code"] == "connection_unusable"

    # ── producer API disabled ──
    update_agent(client, oh, pid, agent_api_enabled=False)
    assert _rows(client, acc, agent["id"])[cred_id]["usable"] is False
    _assert_listing_visible_post_refused()
    update_agent(client, oh, pid, agent_api_enabled=True)
    assert _rows(client, acc, agent["id"])[cred_id]["usable"] is True

    # ── revoked key ──
    rv = client.post(f"{API}/agents/{pid}/agent-api/keys/{key['id']}/revoke", headers=oh)
    assert rv.status_code == 200, rv.text
    row = _rows(client, acc, agent["id"])[cred_id]
    assert row["usable"] is False and row["unavailable_reason"] == "key_revoked"
    _assert_listing_visible_post_refused()

    # ── an unlinked credential is still a 404, listing or not ──
    unlinked = create_random_credential(client, rh, credential_type="api_token")
    _assert_not_found(
        client.get(_cap_url(agent["id"], unlinked["id"]), headers=acc),
        _not_linked(unlinked["id"]),
    )


def test_not_found_messages_distinguish_agent_from_connection_but_not_foreign_from_missing(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    su = superuser_token_headers
    _, h = make_developer(client, su)
    pid = setup_producer(client, h, name="NF Producer")["id"]
    c = connected_consumer(client, h, pid, name="NF Consumer")
    consumer_id, cred_id = c["agent"]["id"], c["connection"]["credential_id"]
    _, other_h = make_developer(client, su)
    other_acc = _acc(client, other_h)
    acc = _acc(client, h)

    # ── foreign agent and random uuid answer byte-identically ──
    foreign = client.get(_list_url(consumer_id), headers=other_acc)
    missing = client.get(_list_url(uuid.uuid4()), headers=other_acc)
    _assert_not_found(foreign)
    _assert_not_found(missing)
    assert foreign.json() == missing.json()
    # also on the connection routes, even for a credential that is linked
    for url in (_spec_url(consumer_id, cred_id), _cap_url(consumer_id, cred_id)):
        _assert_not_found(client.get(url, headers=other_acc))
    _assert_not_found(
        client.get(_cap_url(uuid.uuid4(), cred_id), headers=other_acc)
    )

    # ── own agent, unlinked credential: message carries the lowercase uuid ──
    unlinked = create_random_credential(client, h, credential_type="api_token")
    for url in (_spec_url(consumer_id, unlinked["id"]), _cap_url(consumer_id, unlinked["id"])):
        r = client.get(url, headers=acc)
        _assert_not_found(r, _not_linked(unlinked["id"]))
        assert unlinked["id"] == unlinked["id"].lower()
        assert unlinked["id"] in r.json()["detail"]["message"]
