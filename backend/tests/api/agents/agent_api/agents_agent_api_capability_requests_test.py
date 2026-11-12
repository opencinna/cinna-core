"""
Agent REST API capability requests (``/agent-api/{id}/_cinna/improvement-requests``).

A consumer asks the producer's owner for an API capability that does not exist.
The routes are platform-owned: they never wake the producer env, ignore
``policy.yaml`` and need an identified caller.

Test scenarios:
  1. Full journey: anonymous 401, identified 201 without waking the producer env,
     the caller's own GET, the owner's inbox (kind/title), closing note back to the
     caller, archive without ``session/``
  2. Issued (EXTERNAL) key: identity is the key's subject, a forged header is ignored
  3. Validation: 413 oversize, 422 extra field / short title
  4. Rate limits: 10 per producer (429 + Retry-After), 30 per caller overall
  5. Secret scrubbing of caller-authored text
  6. Forged ``agent_id`` claim for another user's agent is not attributed
  7. Disabled producer, invalid token
"""
import json
import zipfile
from io import BytesIO

from fastapi.testclient import TestClient

from app.core.config import settings
from app.services.agent_api.agent_api_identity_service import AgentApiIdentityService
from app.services.environments.environment_service import EnvironmentService
from tests.utils.agent import create_agent_via_api, update_agent
from tests.utils.agent_api_requests import (
    bearer,
    connect,
    connected_consumer,
    file_request,
    requests_url,
    valid_body,
)
from tests.utils.agent_api_keys import make_developer, setup_producer
from tests.utils.background_tasks import drain_tasks

API = settings.API_V1_STR


class _CountingAdapterFactory:
    """Replaces the lifecycle manager's ``get_adapter``; counts env lookups."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, env):
        self.calls += 1
        raise AssertionError("the producer env must not be touched by _cinna/ routes")


def _me(client, headers) -> dict:
    return client.get(f"{API}/users/me", headers=headers).json()


def _issue_external_key(client, owner_h, producer_id, recipient, recipient_h) -> str:
    r = client.post(
        f"{API}/agents/{producer_id}/agent-api/keys",
        headers=owner_h,
        json={"issued_to_user_id": recipient["id"], "label": "t-key"},
    )
    assert r.status_code == 200, r.text
    rv = client.post(
        f"{API}/credentials/{r.json()['credential_id']}/agent-api-key/reveal",
        headers=recipient_h,
    )
    assert rv.status_code == 200, rv.text
    return rv.json()["token"]


def test_capability_request_journey_from_filing_to_closing_note_and_archive(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    1. Anonymous (no identity header) -> 401.
    2. Identified POST -> 201, producer env never resolved.
    3. Caller's GET shows it; the owner's inbox shows kind=agent_api + title.
    4. Owner PATCHes completed + note -> caller's GET shows the note.
    5. Archive has request.md and no session/ member.
    """
    h = superuser_token_headers
    producer = setup_producer(client, h, name="Cap Req Producer")
    pid = producer["id"]
    c = connected_consumer(client, h, pid)

    r = client.post(requests_url(pid), headers=bearer(c["connection"]["token"]), json=valid_body())
    assert r.status_code == 401
    assert r.json()["detail"] == "Caller identity required to file capability requests"

    lm = EnvironmentService._lifecycle_manager
    original = lm.get_adapter
    counter = _CountingAdapterFactory()
    lm.get_adapter = counter
    try:
        r = client.post(requests_url(pid), headers=c["headers"], json=valid_body())
    finally:
        lm.get_adapter = original
    assert r.status_code == 201, r.text
    created = r.json()
    assert counter.calls == 0
    assert created["title"] == "Need a bulk export endpoint"
    assert created["status"] == "new"
    assert created["spec_version_seen"] == "1.0.0"
    assert created["resolution_note"] is None
    assert "context" not in created and "comment" not in created

    listing = client.get(requests_url(pid), headers=c["headers"])
    assert listing.status_code == 200, listing.text
    assert listing.json()["count"] == 1
    assert [x["id"] for x in listing.json()["data"]] == [created["id"]]

    inbox = client.get(f"{API}/agents/{pid}/improvement-requests", headers=h)
    assert inbox.status_code == 200
    row = next(x for x in inbox.json()["data"] if x["id"] == created["id"])
    assert row["kind"] == "agent_api"
    assert row["title"] == "Need a bulk export endpoint"

    detail = client.get(f"{API}/improvement-requests/{created['id']}", headers=h).json()
    assert detail["kind"] == "agent_api"
    assert detail["context"]["consumer"]["agent_name"] == "Req Consumer"
    assert detail["context"]["api_request"]["need"].startswith("I need to export")
    assert detail["snapshot_message_count"] == 0

    patched = client.patch(
        f"{API}/improvement-requests/{created['id']}",
        headers=h,
        json={"status": "completed", "resolution_note": "Added GET /orders/export."},
    )
    assert patched.status_code == 200, patched.text
    after = client.get(requests_url(pid), headers=c["headers"]).json()["data"][0]
    assert after["status"] == "completed"
    assert after["resolution_note"] == "Added GET /orders/export."
    assert after["status_changed_at"] is not None

    arch = client.get(f"{API}/improvement-requests/{created['id']}/archive", headers=h)
    assert arch.status_code == 200, arch.text
    zf = zipfile.ZipFile(BytesIO(arch.content))
    names = zf.namelist()
    assert "request.md" in names and "README.md" in names
    assert "metadata.json" in names and "context.json" in names
    assert not any(n.startswith("session/") for n in names)
    assert "bulk export" in zf.read("request.md").decode().lower() or "export" in zf.read("request.md").decode()
    assert json.loads(zf.read("metadata.json"))["kind"] == "agent_api"


def test_each_caller_lists_only_their_own_requests(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    h = superuser_token_headers
    pid = setup_producer(client, h, name="Isolation Producer")["id"]
    other_producer = setup_producer(client, h, name="Isolation Producer 2")["id"]
    t, t_h = make_developer(client, h)
    t_key = _issue_external_key(client, h, pid, t, t_h)
    c = connected_consumer(client, h, pid)

    mine = file_request(client, pid, c["headers"], title="Owner request")
    theirs = file_request(client, pid, bearer(t_key), title="T request")
    c2 = connected_consumer(client, h, other_producer, name="Other consumer")
    file_request(client, other_producer, c2["headers"], title="Other producer request")

    s_list = client.get(requests_url(pid), headers=c["headers"]).json()
    assert [x["id"] for x in s_list["data"]] == [mine["id"]]
    t_list = client.get(requests_url(pid), headers=bearer(t_key)).json()
    assert [x["id"] for x in t_list["data"]] == [theirs["id"]]
    assert t_list["count"] == 1


def test_issued_key_identity_is_its_subject_and_forged_header_is_ignored(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    h = superuser_token_headers
    pid = setup_producer(client, h, name="External Producer")["id"]
    t, t_h = make_developer(client, h)
    t_key = _issue_external_key(client, h, pid, t, t_h)

    # No identity header: the key's subject is the caller.
    created = file_request(client, pid, bearer(t_key), title="From issued key")
    # A VALID identity token of another user (the owner) must not override it.
    forged = AgentApiIdentityService.mint(_me(client, h)["id"])
    r = client.post(
        requests_url(pid),
        headers={**bearer(t_key), "X-Cinna-Caller-Identity": forged},
        json=valid_body(title="Forged header attempt"),
    )
    assert r.status_code == 201, r.text

    mine = client.get(requests_url(pid), headers=bearer(t_key)).json()
    assert {x["title"] for x in mine["data"]} == {"From issued key", "Forged header attempt"}
    assert created["id"] in {x["id"] for x in mine["data"]}
    # The owner (the forged identity) filed nothing.
    c = connected_consumer(client, h, pid)
    assert client.get(requests_url(pid), headers=c["headers"]).json()["count"] == 0


def test_invalid_payloads_are_refused(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    h = superuser_token_headers
    pid = setup_producer(client, h, name="Validation Producer")["id"]
    c = connected_consumer(client, h, pid)

    big = client.post(
        requests_url(pid), headers=c["headers"],
        json=valid_body(example_request="x" * (70 * 1024)),
    )
    assert big.status_code == 413

    extra = client.post(requests_url(pid), headers=c["headers"], json=valid_body(bogus="x"))
    assert extra.status_code == 422

    short = client.post(requests_url(pid), headers=c["headers"], json=valid_body(title="ab"))
    assert short.status_code == 422

    bad_nested = client.post(
        requests_url(pid), headers=c["headers"],
        json=valid_body(proposed_endpoint={"method": "GET", "nope": 1}),
    )
    assert bad_nested.status_code == 422

    assert client.get(requests_url(pid), headers=c["headers"]).json()["count"] == 0


def test_per_producer_rate_limit_returns_429_with_retry_after(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    h = superuser_token_headers
    pid = setup_producer(client, h, name="Rate Producer")["id"]
    c = connected_consumer(client, h, pid)
    for i in range(10):
        file_request(client, pid, c["headers"], title=f"Request number {i}")

    r = client.post(requests_url(pid), headers=c["headers"], json=valid_body(title="One too many"))
    assert r.status_code == 429, r.text
    assert int(r.headers["Retry-After"]) >= 1
    assert client.get(requests_url(pid), headers=c["headers"]).json()["count"] == 10

    # Another caller is unaffected.
    t, t_h = make_developer(client, h)
    t_key = _issue_external_key(client, h, pid, t, t_h)
    file_request(client, pid, bearer(t_key), title="Different caller")


def test_overall_rate_limit_across_producers(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    h = superuser_token_headers
    producers = [setup_producer(client, h, name=f"Overall Producer {i}")["id"] for i in range(4)]
    consumers = [connected_consumer(client, h, p, name=f"Overall Consumer {i}")
                 for i, p in enumerate(producers)]
    for p, c in zip(producers[:3], consumers[:3]):
        for i in range(10):
            file_request(client, p, c["headers"], title=f"Request number {i}")

    r = client.post(
        requests_url(producers[3]), headers=consumers[3]["headers"],
        json=valid_body(title="Thirty first"),
    )
    assert r.status_code == 429, r.text
    assert int(r.headers["Retry-After"]) >= 1


def test_bearer_token_in_payload_is_scrubbed(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    h = superuser_token_headers
    pid = setup_producer(client, h, name="Scrub Producer")["id"]
    c = connected_consumer(client, h, pid)
    secret = c["connection"]["token"]
    created = file_request(
        client, pid, c["headers"],
        example_request=f"curl -H 'Authorization: Bearer {secret}' /orders",
        need=f"Token {secret} does not work for exports",
    )
    detail = client.get(f"{API}/improvement-requests/{created['id']}", headers=h)
    assert detail.status_code == 200
    assert secret not in json.dumps(detail.json())
    assert c["identity"] not in json.dumps(detail.json())
    assert "REDACTED" in detail.json()["context"]["api_request"]["example_request"]
    assert detail.json()["context"]["platform"]["scrubbed_hits"] >= 1


def test_forged_agent_id_claim_for_another_users_agent_is_not_attributed(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    h = superuser_token_headers
    pid = setup_producer(client, h, name="Claim Producer")["id"]
    c = connected_consumer(client, h, pid)
    t, t_h = make_developer(client, h)
    others_agent = create_agent_via_api(client, t_h, name="Not Yours")
    drain_tasks()

    forged = AgentApiIdentityService.mint(_me(client, h)["id"], others_agent["id"])
    r = client.post(
        requests_url(pid),
        headers={**bearer(c["connection"]["token"]), "X-Cinna-Caller-Identity": forged},
        json=valid_body(),
    )
    assert r.status_code == 201, r.text
    detail = client.get(f"{API}/improvement-requests/{r.json()['id']}", headers=h).json()
    assert detail["context"]["consumer"]["agent_id"] is None
    assert detail["context"]["consumer"]["agent_name"] is None
    assert detail.get("source_agent_id") in (None, "")

    # An own agent id is attributed.
    own = AgentApiIdentityService.mint(_me(client, h)["id"], c["agent"]["id"])
    r2 = client.post(
        requests_url(pid),
        headers={**bearer(c["connection"]["token"]), "X-Cinna-Caller-Identity": own},
        json=valid_body(title="Attributed request"),
    )
    d2 = client.get(f"{API}/improvement-requests/{r2.json()['id']}", headers=h).json()
    assert d2["context"]["consumer"]["agent_id"] == c["agent"]["id"]


def test_disabled_producer_and_bad_token_are_refused(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    h = superuser_token_headers
    pid = setup_producer(client, h, name="Disabled Producer")["id"]
    c = connected_consumer(client, h, pid)

    bad = client.post(
        requests_url(pid), headers={**bearer("not-a-real-token")}, json=valid_body()
    )
    assert bad.status_code in (401, 403)
    assert client.get(requests_url(pid)).status_code in (401, 403)

    update_agent(client, h, pid, agent_api_enabled=False)
    r = client.post(requests_url(pid), headers=c["headers"], json=valid_body())
    assert r.status_code == 404, r.text
    assert client.get(requests_url(pid), headers=c["headers"]).status_code == 404


def test_reserved_cinna_prefix_never_reaches_the_producer(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Unknown `_cinna/` paths -> 404, wrong methods -> 405, never the producer env."""
    h = superuser_token_headers
    pid = setup_producer(client, h, name="Reserved Prefix Producer")["id"]
    c = connected_consumer(client, h, pid)
    base = requests_url(pid)
    root = base.rsplit("/improvement-requests", 1)[0]

    lm = EnvironmentService._lifecycle_manager
    original = lm.get_adapter
    counter = _CountingAdapterFactory()
    lm.get_adapter = counter
    try:
        unknown = client.get(f"{root}/something-else", headers=c["headers"])
        unknown_post = client.post(f"{root}/other/deeper", headers=c["headers"], json={})
        wrong_put = client.put(base, headers=c["headers"], json=valid_body())
        wrong_delete = client.delete(base, headers=c["headers"])
        slash_get = client.get(base + "/", headers=c["headers"])
        slash_put = client.put(base + "/", headers=c["headers"], json={})
        bare = client.get(root, headers=c["headers"])
        bare_slash = client.get(root + "/", headers=c["headers"])
    finally:
        lm.get_adapter = original
    assert counter.calls == 0
    assert bare.status_code == 404
    assert bare_slash.status_code == 404
    assert unknown.status_code == 404
    assert unknown_post.status_code == 404
    assert wrong_put.status_code == 405
    assert wrong_delete.status_code == 405
    # A trailing slash is handed to the real handlers; other methods are 405 + Allow.
    assert slash_get.status_code == 200, slash_get.text
    assert slash_put.status_code == 405
    assert "allow" in {k.lower() for k in slash_put.headers}
    assert "allow" in {k.lower() for k in wrong_put.headers}
    slash_post = client.post(base + "/", headers=c["headers"], json=valid_body(title="Slash post"))
    assert slash_post.status_code == 201, slash_post.text
    assert slash_post.json()["title"] == "Slash post"


def test_oversized_content_length_is_rejected_before_parsing(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    h = superuser_token_headers
    pid = setup_producer(client, h, name="Content Length Producer")["id"]
    c = connected_consumer(client, h, pid)
    r = client.post(
        requests_url(pid),
        headers={**c["headers"], "Content-Type": "application/json"},
        content=b'{"title": "' + b"x" * (70 * 1024) + b'"',  # not even valid JSON
    )
    assert r.status_code == 413
