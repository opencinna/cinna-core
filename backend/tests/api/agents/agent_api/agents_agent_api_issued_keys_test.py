"""
Integration tests: issuer's cross-producer view of issued keys
(``GET /credentials/issued``, ``agent_api_key`` rows) and the optional ``service_uri`` slot on the
connect helper (``POST /agents/{id}/agent-api/connect``).

Cast: **A** = issuer (a developer who owns several producers), **B**/**C** =
recipients (plain users), **D** = another issuer. Revocation goes through the
existing ``POST /agents/{producer}/agent-api/keys/{key}/revoke`` route.

Scenarios:
  1. Connect slot: stamped (whitespace stripped) / blank -> null / absent -> null;
     consumer-less connect works and lands in the caller's credential list.
  2. Issued-keys list: only keys issued to OTHER people (self-issued keys and
     connections excluded), never a token value, only the caller's own keys,
     recipients see nothing for keys they received.
  3. Ended keys hidden by default, shown with ``include_ended``.
  4. Workspace filter (absent / "" / uuid / invalid -> 400), scopes reflect the
     grant, ordering by producer name then recipient email, newest first.
"""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import update_agent
from tests.utils.agent_api_keys import make_developer, setup_producer
from tests.utils.background_tasks import drain_tasks
from tests.utils.credential import get_credential_with_data
from tests.utils.user import create_random_user_with_headers
from tests.utils.workspace import create_random_workspace

API = settings.API_V1_STR
ISSUED_URL = f"{API}/credentials/issued"


def _issue(
    client: TestClient,
    headers: dict[str, str],
    producer_id: str,
    issued_to_user_id: str | None = None,
    label: str | None = None,
    scopes: list[str] | None = None,
) -> dict:
    body: dict = {}
    if issued_to_user_id is not None:
        body["issued_to_user_id"] = issued_to_user_id
    if label is not None:
        body["label"] = label
    if scopes is not None:
        body["scopes"] = scopes
    r = client.post(f"{API}/agents/{producer_id}/agent-api/keys", headers=headers, json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _revoke(client: TestClient, headers: dict[str, str], producer_id: str, key_id: str) -> None:
    r = client.post(
        f"{API}/agents/{producer_id}/agent-api/keys/{key_id}/revoke", headers=headers
    )
    assert r.status_code == 200, r.text


def _issued(client: TestClient, headers: dict[str, str], **params) -> dict:
    r = client.get(ISSUED_URL, headers=headers, params=params)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == len(body["data"])
    return body


def _connect(client: TestClient, headers: dict[str, str], producer_id: str, **body) -> dict:
    r = client.post(f"{API}/agents/{producer_id}/agent-api/connect", headers=headers, json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ── 1. Connect slot ───────────────────────────────────────────────────────────


def test_connect_service_uri_stamped_blank_to_null_and_absent_to_null(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    ``service_uri`` on connect: stripped and stamped; blank / empty -> null;
    absent -> null (a connection has no ``<slug>-api`` default). Consumer-less
    connect works and the credential is in the caller's credential list.
    """
    _, headers = make_developer(client, superuser_token_headers)
    producer = setup_producer(client, headers, name="Slot Producer")

    stamped = _connect(client, headers, producer["id"], service_uri="  my-slot  ")
    blank = _connect(client, headers, producer["id"], service_uri="   ")
    empty = _connect(client, headers, producer["id"], service_uri="")
    absent = _connect(client, headers, producer["id"])

    for conn, expected in (
        (stamped, "my-slot"),
        (blank, None),
        (empty, None),
        (absent, None),
    ):
        cred = get_credential_with_data(client, headers, conn["credential_id"])
        assert cred["service_uri"] == expected

    r = client.get(f"{API}/credentials/", headers=headers)
    assert r.status_code == 200
    listed = {c["id"] for c in r.json()["data"]}
    assert {c["credential_id"] for c in (stamped, blank, empty, absent)} <= listed


# ── 2. Issued-keys list basics ────────────────────────────────────────────────


def test_issued_keys_lists_only_keys_issued_to_others_without_token_value(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    A issues to B and also to self, and has a connection. The list holds only
    the key for B, with the full row shape, producer and recipient blocks, and
    no token value. B (the recipient) and D (another issuer) see nothing.
    """
    a, a_headers = make_developer(client, superuser_token_headers)
    _, d_headers = make_developer(client, superuser_token_headers)
    b, b_headers = create_random_user_with_headers(client)
    producer = setup_producer(client, a_headers, name="Listing Producer")

    to_b = _issue(client, a_headers, producer["id"], issued_to_user_id=b["id"], label="for-b")
    self_key = _issue(client, a_headers, producer["id"], label="mine")
    assert self_key["token"], "self-issue returns a value (sanity)"
    _connect(client, a_headers, producer["id"])

    body = _issued(client, a_headers)
    assert [row["id"] for row in body["data"]] == [to_b["id"]]
    row = body["data"][0]
    assert row["label"] == "for-b"
    assert row["kind"] == "agent_api_key"
    assert row["status"] == "active"
    assert row["raw_status"] == "active"
    assert row["holder_kind"] == "person"
    assert row["token_prefix"] == to_b["token_prefix"]
    assert row["created_at"]
    assert row["revoked_at"] is None
    assert row["agent"]["id"] == producer["id"]
    assert row["agent"]["name"] == "Listing Producer"
    assert row["agent"]["user_workspace_id"] is None
    assert row["holder"]["id"] == b["id"]
    assert row["holder"]["email"] == b["email"]
    assert "token" not in row
    assert self_key["token"] not in str(body)

    # Recipient and another issuer see nothing
    assert _issued(client, b_headers) == {"data": [], "count": 0}
    assert _issued(client, d_headers) == {"data": [], "count": 0}
    # No superuser bypass
    su_ids = {r["id"] for r in _issued(client, superuser_token_headers)["data"]}
    assert to_b["id"] not in su_ids


def test_issued_keys_requires_authentication(client: TestClient) -> None:
    assert client.get(ISSUED_URL).status_code in (401, 403)


# ── 3. Ended keys ─────────────────────────────────────────────────────────────


def test_issued_keys_hides_revoked_by_default_and_shows_with_include_ended(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    a, a_headers = make_developer(client, superuser_token_headers)
    b, _ = create_random_user_with_headers(client)
    producer = setup_producer(client, a_headers, name="Ended Producer")
    keep = _issue(client, a_headers, producer["id"], issued_to_user_id=b["id"], label="keep")
    gone = _issue(client, a_headers, producer["id"], issued_to_user_id=b["id"], label="gone")
    _revoke(client, a_headers, producer["id"], gone["id"])

    default = _issued(client, a_headers)
    assert [r["id"] for r in default["data"]] == [keep["id"]]

    explicit_false = _issued(client, a_headers, include_ended=False)
    assert [r["id"] for r in explicit_false["data"]] == [keep["id"]]

    everything = _issued(client, a_headers, include_ended=True)
    by_id = {r["id"]: r for r in everything["data"]}
    assert set(by_id) == {keep["id"], gone["id"]}
    assert by_id[gone["id"]]["status"] == "revoked"
    assert by_id[gone["id"]]["raw_status"] == "revoked_by_issuer"
    assert by_id[gone["id"]]["revoked_at"] is not None
    assert by_id[keep["id"]]["status"] == "active"


# ── 4. Workspace filter ───────────────────────────────────────────────────────


def test_issued_keys_workspace_filter_semantics_and_invalid_id(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    absent = all producers; "" = producers in the default workspace; uuid =
    producers in that workspace; anything else -> 400.
    """
    a, a_headers = make_developer(client, superuser_token_headers)
    b, _ = create_random_user_with_headers(client)
    ws = create_random_workspace(client, a_headers)

    default_producer = setup_producer(client, a_headers, name="Default WS Producer")
    r = client.post(
        f"{API}/agents/",
        headers=a_headers,
        json={"name": "Scoped WS Producer", "user_workspace_id": ws["id"]},
    )
    assert r.status_code == 200, r.text
    ws_producer = r.json()
    assert ws_producer["user_workspace_id"] == ws["id"]
    drain_tasks()
    update_agent(
        client, a_headers, ws_producer["id"],
        agent_api_enabled=True, agent_api_external_access_enabled=True,
    )
    k_default = _issue(client, a_headers, default_producer["id"], issued_to_user_id=b["id"])
    k_ws = _issue(client, a_headers, ws_producer["id"], issued_to_user_id=b["id"])

    all_rows = _issued(client, a_headers)
    assert {r["id"] for r in all_rows["data"]} == {k_default["id"], k_ws["id"]}

    default_rows = _issued(client, a_headers, user_workspace_id="")
    assert [r["id"] for r in default_rows["data"]] == [k_default["id"]]

    ws_rows = _issued(client, a_headers, user_workspace_id=ws["id"])
    assert [r["id"] for r in ws_rows["data"]] == [k_ws["id"]]
    assert ws_rows["data"][0]["agent"]["user_workspace_id"] == ws["id"]

    r = client.get(ISSUED_URL, headers=a_headers, params={"user_workspace_id": "not-a-uuid"})
    assert r.status_code == 400
    assert r.json()["detail"] == "Invalid workspace ID format"


def test_issued_keys_workspace_filter_excludes_deleted_producer_keys(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A deleted producer's key is agent_removed (raw producer_removed) unfiltered, absent when a workspace filter is set."""
    a, a_headers = make_developer(client, superuser_token_headers)
    b, _ = create_random_user_with_headers(client)
    producer = setup_producer(client, a_headers, name="Soon Deleted Producer")
    key = _issue(client, a_headers, producer["id"], issued_to_user_id=b["id"])

    r = client.delete(f"{API}/agents/{producer['id']}", headers=a_headers)
    assert r.status_code == 200, r.text

    rows = _issued(client, a_headers, include_ended="true")
    match = [row for row in rows["data"] if row["id"] == key["id"]]
    assert len(match) == 1
    assert match[0]["status"] == "agent_removed"
    assert match[0]["raw_status"] == "producer_removed"
    assert match[0]["agent"] is None

    assert _issued(client, a_headers, user_workspace_id="", include_ended="true")["data"] == []


# ── 5. Scopes and ordering ────────────────────────────────────────────────────


def test_issued_keys_scopes_reflect_grant_and_ordering_is_producer_then_email_then_newest(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    a, a_headers = make_developer(client, superuser_token_headers)
    b, _ = create_random_user_with_headers(client)
    c, _ = create_random_user_with_headers(client)
    first, second = sorted([b, c], key=lambda u: u["email"].lower())

    p_zeta = setup_producer(client, a_headers, name="zeta producer")
    p_alpha = setup_producer(client, a_headers, name="Alpha producer")

    k_zeta = _issue(client, a_headers, p_zeta["id"], issued_to_user_id=first["id"])
    k_second_old = _issue(client, a_headers, p_alpha["id"], issued_to_user_id=second["id"])
    k_first = _issue(client, a_headers, p_alpha["id"], issued_to_user_id=first["id"],
                     scopes=["orders.read"])
    k_second_new = _issue(client, a_headers, p_alpha["id"], issued_to_user_id=second["id"])

    rows = _issued(client, a_headers)["data"]
    assert [r["id"] for r in rows] == [
        k_first["id"],
        k_second_new["id"],
        k_second_old["id"],
        k_zeta["id"],
    ]
    by_id = {r["id"]: r for r in rows}
    assert by_id[k_first["id"]]["details"]["scopes"] == ["orders.read"]
