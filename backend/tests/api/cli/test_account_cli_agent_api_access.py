"""
Account CLI — issued Agent API keys (``cinna agent-api access``), core side.

Plan: ``docs/plans/agent_api_issued_keys_cli_plan.md`` (§1 contract, §5 tests).

Routes under ``/api/v1/cli/account/agent-api``:
  GET  /access        — grouped grants + keys, scope catalog, manage_url
  POST /keys          — issue (self or another person, by email)
  POST /keys/revoke   — soft revoke, idempotent
  PUT  /access/scopes — set / clear a person's grant

Invariants asserted here:
  * No key value ever crosses the account CLI: no ``token`` key anywhere in any
    response, and the value the owner later reveals on the web never appears.
  * Coded refusals ``{"detail": {"code", "message"}}`` for the new routes.
  * Writes need a developer account; reads are open to any account token.
  * The api-proxy escape hatch refuses ``agents/{id}/agent-api/keys`` (no key
    minted), while the sibling ``agent-api/access`` read stays reachable.

API-only except one documented DB seam: the scope catalog comes from the env's
parsed policy cache, which the stubbed environment never fills
(``_seed_scope_catalog``, same helper shape as the grants test).
"""
import uuid

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import settings
from app.models import AgentEnvironment
from tests.utils.agent_api_keys import make_developer, setup_producer
from tests.utils.cli import (
    account_cli_headers,
    account_create_credential,
    account_list_credentials,
    bootstrap_account_token,
)
from tests.utils.mfa import find_security_events
from tests.utils.skill_catalog import mixed_case
from tests.utils.user import create_random_user_with_headers

API = settings.API_V1_STR
_BASE = f"{API}/cli"
_ACCESS = f"{_BASE}/account/agent-api/access"
_KEYS = f"{_BASE}/account/agent-api/keys"
_REVOKE = f"{_BASE}/account/agent-api/keys/revoke"
_SCOPES = f"{_BASE}/account/agent-api/access/scopes"


# ── helpers ──────────────────────────────────────────────────────────────────


def _account_headers(client: TestClient, user_headers: dict[str, str]) -> dict[str, str]:
    jwt, _ = bootstrap_account_token(client, user_headers)
    return account_cli_headers(jwt)


def _code(response) -> str:
    detail = response.json()["detail"]
    assert isinstance(detail, dict), detail
    return detail["code"]


def _list(client, acc, agent_id):
    return client.get(_ACCESS, headers=acc, params={"agent_id": agent_id})


def _issue(client, acc, agent_id, **fields):
    return client.post(_KEYS, headers=acc, json={"agent_id": agent_id, **fields})


def _revoke(client, acc, agent_id, key_id):
    return client.post(_REVOKE, headers=acc, json={"agent_id": agent_id, "key_id": key_id})


def _set_scopes(client, acc, agent_id, scopes, user_email=None):
    return client.put(
        _SCOPES,
        headers=acc,
        json={"agent_id": agent_id, "user_email": user_email, "scopes": scopes},
    )


def _walk_keys(value) -> set[str]:
    """Every dict key anywhere in a JSON value."""
    found: set[str] = set()
    if isinstance(value, dict):
        for k, v in value.items():
            found.add(k)
            found |= _walk_keys(v)
    elif isinstance(value, list):
        for item in value:
            found |= _walk_keys(item)
    return found


def _seed_scope_catalog(db: Session, agent_id: str, names: list[str]) -> None:
    env = db.exec(
        select(AgentEnvironment).where(AgentEnvironment.agent_id == uuid.UUID(agent_id))
    ).first()
    assert env is not None
    env.agent_api_policy_cache = {
        "read_only": True,
        "auth": "required",
        "max_body_bytes": 10 * 1024 * 1024,
        "rate_limit": "60/min",
        "expose_spec": True,
        "allowed_paths": ["*"],
        "scopes": [{"name": n, "description": f"{n} scope", "requires": []} for n in names],
    }
    db.add(env)
    db.commit()


def _entry_for(access: dict, email: str) -> dict | None:
    for entry in access["data"]:
        if (entry["user"].get("email") or "").lower() == email.lower():
            return entry
    return None


# ── Scenario 1: list ─────────────────────────────────────────────────────────


def test_list_access_shows_grouped_entries_catalog_and_manage_url(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """
    1. Fresh producer: empty list, flags + empty scope catalog + manage_url.
    2. After issuing to a recipient and revoking, the terminal key is still
       listed under that person.
    3. A seeded catalog is surfaced as ``[{name, description}]``.
    4. A non-owner gets 404 ``agent_not_found``; a superuser (not owner) is allowed.
    """
    owner, owner_headers = make_developer(client, superuser_token_headers)
    recipient, _ = make_developer(client, superuser_token_headers)
    producer = setup_producer(client, owner_headers, name="Cli List Producer")
    pid = producer["id"]
    acc = _account_headers(client, owner_headers)

    r = _list(client, acc, pid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["data"] == [] and body["count"] == 0
    assert body["external_access_enabled"] is True
    assert body["scope_catalog"] == []
    assert body["manage_url"].endswith(f"/agent/{pid}#integrations")

    issued = _issue(client, acc, pid, issued_to_email=recipient["email"]).json()
    assert _revoke(client, acc, pid, issued["id"]).status_code == 200

    body = _list(client, acc, pid).json()
    entry = _entry_for(body, recipient["email"])
    assert entry is not None
    assert [k["id"] for k in entry["keys"]] == [issued["id"]]
    assert entry["keys"][0]["status"] == "revoked_by_issuer"
    assert "token" not in _walk_keys(body)

    _seed_scope_catalog(db, pid, ["orders.read", "orders.write"])
    catalog = _list(client, acc, pid).json()["scope_catalog"]
    assert {c["name"] for c in catalog} == {"orders.read", "orders.write"}
    assert all("description" in c for c in catalog)

    _, other_headers = make_developer(client, superuser_token_headers)
    other_acc = _account_headers(client, other_headers)
    r = _list(client, other_acc, pid)
    assert r.status_code == 404 and _code(r) == "agent_not_found"

    su_acc = _account_headers(client, superuser_token_headers)
    assert _list(client, su_acc, pid).status_code == 200


# ── Scenario 2: issue to self ────────────────────────────────────────────────


def test_issue_to_self_returns_credential_url_and_never_the_value(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Self-issue (``issued_to_email`` null): 200, ``issued_to_self``, credential_url
    ends ``/credential/<id>``, default slug service_uri, no ``token`` key anywhere,
    and the value revealed afterwards on the web is absent from the response.
    The key then shows in the credentials list as ``active``.
    """
    owner, owner_headers = make_developer(client, superuser_token_headers)
    producer = setup_producer(client, owner_headers, name="CRM Cli Agent")
    pid = producer["id"]
    acc = _account_headers(client, owner_headers)

    r = _issue(client, acc, pid, label="laptop", expires_in_days=30)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["issued_to_self"] is True
    assert body["credential_id"]
    assert body["credential_url"].endswith(f"/credential/{body['credential_id']}")
    assert body["service_uri"] == "crm-cli-agent-api"
    assert body["label"] == "laptop"
    assert body["status"] == "active" and body["is_usable"] is True
    assert body["expires_at"] is not None
    assert body["base_url"] and body["spec_url"]
    assert body["unknown_scopes"] == []
    assert "token" not in _walk_keys(body)

    revealed = client.post(
        f"{API}/credentials/{body['credential_id']}/agent-api-key/reveal",
        headers=owner_headers,
    )
    assert revealed.status_code == 200, revealed.text
    value = revealed.json()["token"]
    assert value and value not in r.text

    # The list response must not carry it either.
    assert value not in _list(client, acc, pid).text

    rows = {c["id"]: c for c in account_list_credentials(client, acc)}
    assert rows[body["credential_id"]]["agent_api_key_status"] == "active"


# ── Scenario 3: issue to another person ──────────────────────────────────────


def test_issue_to_other_by_mixed_case_email_hands_credential_to_recipient(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Recipient typed mixed-case: resolved case-insensitively; the credential is
    the recipient's (they can read it, the issuer cannot), no credential_url,
    ``issued_to_self`` false, CREATED audit lands on the issuer.
    """
    issuer, issuer_headers = make_developer(client, superuser_token_headers)
    recipient, recipient_headers = make_developer(client, superuser_token_headers)
    producer = setup_producer(client, issuer_headers, name="Cli Other Producer")
    pid = producer["id"]
    acc = _account_headers(client, issuer_headers)

    r = _issue(client, acc, pid, issued_to_email=mixed_case(recipient["email"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["issued_to_self"] is False
    assert body["credential_url"] is None
    assert "token" not in _walk_keys(body)

    cred_id = body["credential_id"]
    assert (
        client.get(f"{API}/credentials/{cred_id}", headers=recipient_headers).status_code == 200
    )
    # The issuer cannot read the recipient's credential (generic permission guard).
    assert (
        client.get(f"{API}/credentials/{cred_id}", headers=issuer_headers).status_code != 200
    )
    assert find_security_events(client, issuer_headers, "AGENT_API_EXTERNAL_KEY_CREATED")

    entry = _entry_for(_list(client, acc, pid).json(), recipient["email"])
    assert entry is not None and entry["keys"][0]["id"] == body["id"]


# ── Scenario 4: issue refusals ───────────────────────────────────────────────


def test_issue_refusals_recipient_toggles_role_and_validation(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """
    unknown email -> 404 recipient_not_found; deactivated user -> 404; external
    access off -> 400 external_access_disabled; API off -> 400 api_disabled;
    non-developer account -> 403; expires_in_days=0 -> 422; unknown scopes are
    reported (never refused) once the producer declares a catalog.
    """
    owner, owner_headers = make_developer(client, superuser_token_headers)
    producer = setup_producer(client, owner_headers, name="Cli Refusal Producer")
    pid = producer["id"]
    acc = _account_headers(client, owner_headers)

    r = _issue(client, acc, pid, issued_to_email=f"nobody-{uuid.uuid4().hex[:8]}@example.com")
    assert r.status_code == 404 and _code(r) == "recipient_not_found"

    gone, _ = create_random_user_with_headers(client)
    r = client.patch(
        f"{API}/users/{gone['id']}",
        headers=superuser_token_headers,
        json={"is_active": False},
    )
    assert r.status_code == 200, r.text
    r = _issue(client, acc, pid, issued_to_email=gone["email"])
    assert r.status_code == 404 and _code(r) == "recipient_not_found"

    r = _issue(client, acc, pid, expires_in_days=0)
    assert r.status_code == 422

    # Unknown scopes: only meaningful against a non-empty catalog.
    _seed_scope_catalog(db, pid, ["orders.read"])
    r = _issue(client, acc, pid, scopes=["orders.read", "bogus.scope"])
    assert r.status_code == 200, r.text
    assert r.json()["unknown_scopes"] == ["bogus.scope"]

    # Non-developer: token minted while a developer, then demoted. Writes are
    # refused (403); reads stay open to the owner.
    r = client.patch(
        f"{API}/users/{owner['id']}/role",
        headers=superuser_token_headers,
        json={"role": "agent-user"},
    )
    assert r.status_code == 200, r.text
    r = _issue(client, acc, pid)
    assert r.status_code == 403, r.text
    assert _revoke(client, acc, pid, str(uuid.uuid4())).status_code == 403
    assert _set_scopes(client, acc, pid, ["x"]).status_code == 403
    assert _list(client, acc, pid).status_code == 200
    r = client.patch(
        f"{API}/users/{owner['id']}/role",
        headers=superuser_token_headers,
        json={"role": "agent-developer"},
    )
    assert r.status_code == 200, r.text

    # Toggles.
    off = setup_producer(
        client, owner_headers, name="Cli External Off", external_access_enabled=False
    )
    r = _issue(client, acc, off["id"])
    assert r.status_code == 400 and _code(r) == "external_access_disabled"
    assert r.json()["detail"]["message"].endswith(f"/agent/{off['id']}#integrations")

    api_off = setup_producer(client, owner_headers, name="Cli Api Off", agent_api_enabled=False)
    r = _issue(client, acc, api_off["id"])
    assert r.status_code == 400 and _code(r) == "api_disabled"


def test_issue_for_foreign_or_missing_agent_is_404_agent_not_found(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    _, owner_headers = make_developer(client, superuser_token_headers)
    _, other_headers = make_developer(client, superuser_token_headers)
    producer = setup_producer(client, owner_headers, name="Cli Foreign Producer")
    other_acc = _account_headers(client, other_headers)

    for agent_id in (producer["id"], str(uuid.uuid4())):
        r = _issue(client, other_acc, agent_id)
        assert r.status_code == 404 and _code(r) == "agent_not_found"
        # The owner gate runs before recipient resolution: no email-existence leak.
        r = _issue(client, other_acc, agent_id, issued_to_email="ghost-nobody@example.com")
        assert r.status_code == 404 and _code(r) == "agent_not_found"
        r = _set_scopes(client, other_acc, agent_id, ["a"], user_email="ghost-nobody@example.com")
        assert r.status_code == 404 and _code(r) == "agent_not_found"


def test_case_duplicate_accounts_cannot_be_created_through_the_api(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    ``ambiguous_recipient`` needs legacy case-only duplicate accounts. The API
    normalises/refuses them, so the 409 path is covered at the resolver level in
    ``tests/unit/test_account_cli_recipient_resolver.py`` instead.
    """
    user, _ = create_random_user_with_headers(client)
    r = client.post(
        f"{API}/users/",
        headers=superuser_token_headers,
        json={"email": user["email"].upper(), "password": "changethis-123456"},
    )
    assert r.status_code in (400, 409), r.text


# ── Scenario 5: revoke ───────────────────────────────────────────────────────


def test_revoke_is_idempotent_and_keeps_the_recipients_credential(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    revoke -> 200 revoked_by_issuer (+ no token key); again -> 200 same status;
    unknown key id -> 404 key_not_found; the recipient's credential survives;
    non-owner -> 404.
    """
    _, issuer_headers = make_developer(client, superuser_token_headers)
    recipient, recipient_headers = make_developer(client, superuser_token_headers)
    producer = setup_producer(client, issuer_headers, name="Cli Revoke Producer")
    other_producer = setup_producer(client, issuer_headers, name="Cli Revoke Other")
    pid = producer["id"]
    acc = _account_headers(client, issuer_headers)

    key = _issue(client, acc, pid, issued_to_email=recipient["email"]).json()

    r = _revoke(client, acc, pid, key["id"])
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "revoked_by_issuer"
    assert r.json()["is_active"] is False
    assert "token" not in _walk_keys(r.json())

    again = _revoke(client, acc, pid, key["id"])
    assert again.status_code == 200 and again.json()["status"] == "revoked_by_issuer"

    # Recipient keeps the credential (now terminal).
    assert (
        client.get(
            f"{API}/credentials/{key['credential_id']}", headers=recipient_headers
        ).status_code
        == 200
    )

    # A key id that is not an external key of THIS producer.
    r = _revoke(client, acc, other_producer["id"], key["id"])
    assert r.status_code == 404 and _code(r) == "key_not_found"
    r = _revoke(client, acc, pid, str(uuid.uuid4()))
    assert r.status_code == 404 and _code(r) == "key_not_found"

    _, other_headers = make_developer(client, superuser_token_headers)
    other_acc = _account_headers(client, other_headers)
    r = _revoke(client, other_acc, pid, key["id"])
    assert r.status_code == 404 and _code(r) == "agent_not_found"


# ── Scenario 6: scopes ───────────────────────────────────────────────────────


def test_scopes_set_clear_and_none_clear(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    set -> grant scopes exactly the list; empty list is an explicit empty grant;
    ``null`` removes the grant (entry null when nothing else remains) and is
    idempotent; ``scopes`` omitted -> 422 (required-but-nullable); non-owner 404.
    """
    _, owner_headers = make_developer(client, superuser_token_headers)
    person, _ = make_developer(client, superuser_token_headers)
    producer = setup_producer(client, owner_headers, name="Cli Scopes Producer")
    pid = producer["id"]
    acc = _account_headers(client, owner_headers)

    r = _set_scopes(client, acc, pid, ["a", "b"], user_email=mixed_case(person["email"]))
    assert r.status_code == 200, r.text
    assert sorted(r.json()["entry"]["scopes"]) == ["a", "b"]
    assert r.json()["unknown_scopes"] == []

    r = _set_scopes(client, acc, pid, ["c"], user_email=person["email"])
    assert r.json()["entry"]["scopes"] == ["c"]
    entry = _entry_for(_list(client, acc, pid).json(), person["email"])
    assert entry["scopes"] == ["c"]

    r = _set_scopes(client, acc, pid, [], user_email=person["email"])
    assert r.status_code == 200 and r.json()["entry"]["scopes"] == []

    r = _set_scopes(client, acc, pid, None, user_email=person["email"])
    assert r.status_code == 200, r.text
    assert r.json()["entry"] is None
    assert _entry_for(_list(client, acc, pid).json(), person["email"]) is None

    # Removing when none exists is still 200.
    r = _set_scopes(client, acc, pid, None, user_email=person["email"])
    assert r.status_code == 200 and r.json()["entry"] is None

    # Caller (null email) is a valid target.
    r = _set_scopes(client, acc, pid, ["self"])
    assert r.status_code == 200 and r.json()["entry"]["scopes"] == ["self"]

    # Unknown recipient.
    r = _set_scopes(client, acc, pid, ["a"], user_email=f"x-{uuid.uuid4().hex[:8]}@example.com")
    assert r.status_code == 404 and _code(r) == "recipient_not_found"

    # `scopes` omitted entirely -> 422.
    r = client.put(_SCOPES, headers=acc, json={"agent_id": pid})
    assert r.status_code == 422, r.text

    _, other_headers = make_developer(client, superuser_token_headers)
    other_acc = _account_headers(client, other_headers)
    r = _set_scopes(client, other_acc, pid, ["a"])
    assert r.status_code == 404 and _code(r) == "agent_not_found"


# ── Scenario 7: credentials list status + draft refusal ──────────────────────


def test_credentials_list_reports_key_status_and_agent_api_draft_is_refused(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Issued key row: ``agent_api_key_status`` active -> revoked_by_issuer after
    revoke. A connection's agent_api row carries null. Drafting type=agent_api
    -> 400 and no row is created.
    """
    _, headers = make_developer(client, superuser_token_headers)
    producer = setup_producer(client, headers, name="Cli Status Producer")
    pid = producer["id"]
    acc = _account_headers(client, headers)

    key = _issue(client, acc, pid).json()
    conn = client.post(f"{API}/agents/{pid}/agent-api/connect", headers=headers, json={})
    assert conn.status_code == 200, conn.text
    conn_cred_id = conn.json()["credential_id"]

    rows = {c["id"]: c for c in account_list_credentials(client, acc)}
    assert rows[key["credential_id"]]["agent_api_key_status"] == "active"
    assert rows[conn_cred_id]["agent_api_key_status"] is None

    assert _revoke(client, acc, pid, key["id"]).status_code == 200
    rows = {c["id"]: c for c in account_list_credentials(client, acc)}
    assert rows[key["credential_id"]]["agent_api_key_status"] == "revoked_by_issuer"
    assert rows[conn_cred_id]["agent_api_key_status"] is None

    before = len(account_list_credentials(client, acc))
    r = client.post(
        f"{_BASE}/account/credentials",
        headers=acc,
        json={"name": "Draft Agent API", "type": "agent_api", "allow_sharing": False},
    )
    assert r.status_code == 400, r.text
    assert "agent-api access issue" in r.text
    assert len(account_list_credentials(client, acc)) == before

    # A normal draft still works (the refusal is type-specific).
    ok = account_create_credential(client, acc, name="Draft Token", cred_type="api_token")
    assert ok["credential"]["id"]


# ── Scenario 8: api-proxy escape hatch ───────────────────────────────────────


def test_api_proxy_refuses_issuing_keys_but_still_reads_access(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    ``POST agents/{id}/agent-api/keys`` through the hatch -> 403 excluded_path and
    no key is minted (access list still empty); ``GET agents/{id}/agent-api/access``
    through the hatch is unaffected.
    """
    _, headers = make_developer(client, superuser_token_headers)
    producer = setup_producer(client, headers, name="Cli Proxy Producer")
    pid = producer["id"]
    acc = _account_headers(client, headers)

    r = client.post(
        f"{_BASE}/account/api-proxy",
        headers=acc,
        json={"method": "POST", "path": f"agents/{pid}/agent-api/keys", "body": {}},
    )
    assert r.status_code == 403, r.text
    assert "cinna agent-api access" in r.json()["detail"]
    assert _list(client, acc, pid).json()["data"] == []
    assert client.get(f"{API}/agents/{pid}/agent-api/access", headers=headers).json()["data"] == []

    r = client.post(
        f"{_BASE}/account/api-proxy",
        headers=acc,
        json={"method": "POST", "path": f"agents/{pid}/agent-api/keys%0A", "body": {}},
    )
    # Refused before dispatch (the proxy service answers malformed paths 400).
    assert r.status_code in (400, 403), r.text
    assert _list(client, acc, pid).json()["data"] == []

    for method in ("GET", "POST"):
        r = client.post(
            f"{_BASE}/account/api-proxy",
            headers=acc,
            json={"method": method, "path": f"agents/{pid}/guest-shares"},
        )
        assert r.status_code == 403, r.text
        r = client.post(
            f"{_BASE}/account/api-proxy",
            headers=acc,
            json={"method": method, "path": f"agents/{pid}/webapp-shares"},
        )
        assert r.status_code == 403, r.text

    r = client.post(
        f"{_BASE}/account/api-proxy",
        headers=acc,
        json={"method": "GET", "path": f"agents/{pid}/agent-api/access"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"] == []

