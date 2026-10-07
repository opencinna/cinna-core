"""
Integration tests: Agent REST API **issued keys** (P1 + P2 of
``docs/plans/agent_api_issued_keys_plan.md``; supersedes the "acts as" external
key model of ``agent_api_external_keys_plan.md``).

Complements ``agents_agent_api_test.py`` (L1 connection token, connect helper,
policy enforcement, consumer proxy) and ``agents_agent_api_grants_test.py`` (L2
caller identity + per-user scopes for CONNECTIONS). Here we cover the second
product behind the proxy: a key a human copies into a laptop script, server, or
cron job.

Cast: **A** = the issuer (producer owner, usually the superuser fixture);
**B** = the recipient (a plain signed-up user). The key's ``agent_api``
credential is OWNED BY THE RECIPIENT, so only B can reveal / rotate / read it.

Business rules tested (plan D1-D6, D13, D23, §12):
  1. Issuing needs both opt-ins (400); routes are owner-gated (404, no leak);
     the old ``GET /keys`` and ``DELETE /keys/{id}`` routes are gone (D33).
  2. Self-issue: credential owned by the issuer, ``token`` in the response,
     reveal works, ``service_uri`` defaults to ``<slug>-api`` (``CRM Agent`` ->
     ``crm-agent-api``). Issued to B: ``token`` is null, the credential is B's.
  3. An explicit ``service_uri`` is stored as given; the recipient edits it via
     the credential update and rotate keeps it.
  4. The key's identity is its recipient (never the issuer, never a forged
     identity header); scopes come from the live ``(producer, recipient)`` grant.
  5. Soft revoke: 401, recipient's credential survives, ``revoked_by_issuer``,
     reveal / rotate / spec 409, idempotent, grant untouched.
  6. Status vocabulary at the proxy + holder view: producer deleted, issuer
     deleted, kill switch (``external_access_paused``), ``api_disabled``,
     ``expired`` (all 401, no ``last_used_at`` bump while paused).
  7. Rotation: same credential and links, old value valid for the grace window
     only, a second rotate replaces the first previous hash, recipient only.
  8. The recipient deleting their credential removes the key and audits
     ``DELETED_BY_HOLDER`` on the issuer.
  9. Sharing / template sharing refused; ``/access`` groups grants + keys per
     person; connections are not touched by any of it.
 10. Security events: CREATED, REVOKED (issuer + recipient), REVEALED, ROTATED.

API-only except one documented DB seam: there is no API to mint an already
expired key (``expires_in_days >= 1``), so ``_expire_key`` rewrites
``expires_at`` (same pattern as ``_set_policy_cache`` in the sibling test).
"""
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from app.models import AgentApiToken
from app.services.environments.environment_service import EnvironmentService
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.agent import create_agent_via_api, update_agent
from tests.utils.agent_api_keys import (
    make_developer as _make_developer,
    setup_producer as _setup_producer,
)
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.credential import (
    create_random_credential,
    get_credential_with_data,
    link_credential_to_agent,
    update_credential,
)
from tests.utils.mfa import find_security_events
from tests.utils.utils import random_email, random_lower_string
from tests.utils.user import (
    user_authentication_headers,
    create_random_user,
    create_random_user_with_headers,
    promote_to_developer,
)

API = settings.API_V1_STR

GRACE_PATCH_TARGET = "app.services.agent_api.agent_api_key_service.AGENT_API_KEY_ROTATION_GRACE"


# ── URL helpers ───────────────────────────────────────────────────────────────


def _owner_base(agent_id: str) -> str:
    return f"{API}/agents/{agent_id}/agent-api"


def _keys_url(agent_id: str) -> str:
    return f"{_owner_base(agent_id)}/keys"


def _revoke_url(agent_id: str, key_id: str) -> str:
    return f"{_keys_url(agent_id)}/{key_id}/revoke"


def _access_url(agent_id: str) -> str:
    return f"{_owner_base(agent_id)}/access"


def _grants_url(agent_id: str) -> str:
    return f"{_owner_base(agent_id)}/grants"


def _grant_url(agent_id: str, grant_id: str) -> str:
    return f"{_grants_url(agent_id)}/{grant_id}"


def _connect_url(agent_id: str) -> str:
    return f"{_owner_base(agent_id)}/connect"


def _consumer_proxy_url(agent_id: str, path: str) -> str:
    return f"{API}/agent-api/{agent_id}/{path}"


def _credential_url(credential_id: str) -> str:
    return f"{API}/credentials/{credential_id}"


def _holder_url(credential_id: str) -> str:
    return f"{_credential_url(credential_id)}/agent-api-key"


def _reveal_url(credential_id: str) -> str:
    return f"{_holder_url(credential_id)}/reveal"


def _rotate_url(credential_id: str) -> str:
    return f"{_holder_url(credential_id)}/rotate"


def _spec_url(credential_id: str) -> str:
    return f"{_holder_url(credential_id)}/spec"


def _bearer(token_value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token_value}"}


# ── Setup helpers ─────────────────────────────────────────────────────────────
# ``setup_producer`` / ``make_developer`` live in ``tests/utils/agent_api_keys.py``.


def _issue_key(
    client: TestClient,
    headers: dict[str, str],
    producer_id: str,
    issued_to_user_id: str | None = None,
    label: str | None = None,
    scopes: list[str] | None = None,
    read_only_override: bool = False,
    expires_in_days: int | None = None,
    service_uri: str | None = None,
) -> dict:
    """Issue a key via POST .../agent-api/keys. Returns the response body."""
    body: dict = {"read_only_override": read_only_override}
    if issued_to_user_id is not None:
        body["issued_to_user_id"] = issued_to_user_id
    if label is not None:
        body["label"] = label
    if scopes is not None:
        body["scopes"] = scopes
    if expires_in_days is not None:
        body["expires_in_days"] = expires_in_days
    if service_uri is not None:
        body["service_uri"] = service_uri
    r = client.post(_keys_url(producer_id), headers=headers, json=body)
    assert r.status_code == 200, f"Issue key failed: {r.text}"
    return r.json()


def _issue_to_recipient(
    client: TestClient,
    issuer_headers: dict[str, str],
    producer_id: str,
    recipient: dict,
    recipient_headers: dict[str, str],
    **kwargs,
) -> dict:
    """Issue to ``recipient`` and let them reveal the value.

    Returns the issue response with ``token`` filled from the recipient's reveal
    (the issue response itself carries ``token = null``).
    """
    created = _issue_key(
        client, issuer_headers, producer_id, issued_to_user_id=recipient["id"], **kwargs
    )
    assert created["token"] is None, "a key issued to someone else must not return its value"
    created["token"] = _reveal_key(client, recipient_headers, created["credential_id"])
    return created


def _access(client: TestClient, headers: dict[str, str], producer_id: str) -> dict:
    r = client.get(_access_url(producer_id), headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _access_keys(client: TestClient, headers: dict[str, str], producer_id: str) -> list[dict]:
    """Every key in the access list, flattened across people."""
    return [k for entry in _access(client, headers, producer_id)["data"] for k in entry["keys"]]


def _revoke_key(
    client: TestClient, headers: dict[str, str], producer_id: str, key_id: str
) -> dict:
    r = client.post(_revoke_url(producer_id, key_id), headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _holder(client: TestClient, headers: dict[str, str], credential_id: str) -> dict:
    r = client.get(_holder_url(credential_id), headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _rotate(client: TestClient, headers: dict[str, str], credential_id: str) -> dict:
    r = client.post(_rotate_url(credential_id), headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _reveal_key(client: TestClient, headers: dict[str, str], credential_id: str) -> str:
    r = client.post(_reveal_url(credential_id), headers=headers)
    assert r.status_code == 200, f"Reveal failed: {r.text}"
    return r.json()["token"]


def _create_grant(
    client: TestClient, headers: dict[str, str], agent_id: str, user_id: str, scopes: list[str]
) -> dict:
    r = client.post(
        _grants_url(agent_id), headers=headers, json={"user_id": user_id, "scopes": scopes}
    )
    assert r.status_code == 200, f"Create grant failed: {r.text}"
    return r.json()


def _list_grants(client: TestClient, headers: dict[str, str], agent_id: str) -> list[dict]:
    r = client.get(_grants_url(agent_id), headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _connect(
    client: TestClient,
    headers: dict[str, str],
    producer_id: str,
    label: str | None = None,
    consumer_agent_id: str | None = None,
) -> dict:
    """Connect helper (L1 connection, not an issued key)."""
    body: dict = {}
    if label is not None:
        body["credential_label"] = label
    if consumer_agent_id is not None:
        body["consumer_agent_id"] = consumer_agent_id
    r = client.post(_connect_url(producer_id), headers=headers, json=body)
    assert r.status_code == 200, f"Connect failed: {r.text}"
    conn = r.json()
    cred = get_credential_with_data(client, headers, conn["credential_id"])
    return {
        "id": conn["token_id"],
        "credential_id": conn["credential_id"],
        "token": cred["credential_data"]["token"],
    }


def _proxy_status(producer_id: str, token: str, client: TestClient, path: str = "ping") -> int:
    return client.get(_consumer_proxy_url(producer_id, path), headers=_bearer(token)).status_code


def _me(client: TestClient, headers: dict[str, str]) -> dict:
    return client.get(f"{API}/users/me", headers=headers).json()


def _expire_key(db: Session, key_id: str) -> None:
    """Force a key's ``expires_at`` into the past (documented DB seam)."""
    token = db.get(AgentApiToken, uuid.UUID(key_id))
    assert token is not None, f"Token {key_id} not found"
    token.expires_at = datetime.now(UTC) - timedelta(days=1)
    db.add(token)
    db.commit()


class _TrackingAdapter(EnvironmentTestAdapter):
    """Adapter that records the forwarded headers of each proxy call."""

    def __init__(self) -> None:
        super().__init__()
        self.forwarded_headers: list[dict] = []

    async def proxy_agent_api(
        self, method, path, headers=None, body=None, stream=False, timeout=60.0, query_string=None
    ):
        self.forwarded_headers.append({k.lower(): v for k, v in (headers or {}).items()})
        return await super().proxy_agent_api(
            method, path, headers, body, stream, timeout, query_string
        )


def _read_owner_identity_token(
    client: TestClient,
    headers: dict[str, str],
    consumer_id: str,
    credential_id: str,
) -> str:
    """Obtain a VALID L2 ``owner_identity_token`` for whoever owns ``consumer_id``.

    API-only, mirroring ``agents_agent_api_grants_test.py._read_owner_identity_token``.
    """
    persistent = EnvironmentTestAdapter()
    lm = EnvironmentService._lifecycle_manager
    original = lm.get_adapter
    lm.get_adapter = lambda env: persistent
    try:
        link_credential_to_agent(client, headers, consumer_id, credential_id)
        drain_tasks()
    finally:
        lm.get_adapter = original

    captured = persistent.credentials_set
    assert captured, "credential sync never reached the env adapter"
    creds = captured["credentials_json"]
    identity = next((c for c in creds if c["type"] == "owner_identity_token"), None)
    assert identity is not None, (
        f"owner_identity_token not injected into credentials.json: "
        f"{[c['type'] for c in creds]}"
    )
    token = identity["credential_data"]["token"]
    assert token, "owner_identity_token entry has no token"
    return token


# ── A. Issue gating + self-issue ──────────────────────────────────────────────


def test_issue_requires_both_toggles_and_self_issue_returns_value_and_default_slug(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Issuing needs TWO opt-ins, each independently 400 while off; a self-issue
    then returns the value and the credential is the issuer's own:
      1. Both off -> 400. external_access ON but agent_api_enabled OFF -> 400.
      2. Both ON -> 200 with ``token``, 8-char prefix, urls, ``status=active``,
         ``issued_to`` = the issuer.
      3. The credential is owned by the issuer, ``service_uri`` is the default
         slug (``CRM Agent`` -> ``crm-agent-api``), reveal returns the same value.
      4. The access list never carries the value.
    """
    agent = create_agent_via_api(client, superuser_token_headers, name="CRM Agent")
    drain_tasks()
    agent_id = agent["id"]
    me = _me(client, superuser_token_headers)

    # ── Phase 1: both opt-ins off, then only one ──────────────────────────
    r = client.post(_keys_url(agent_id), headers=superuser_token_headers, json={})
    assert r.status_code == 400, r.text
    update_agent(
        client, superuser_token_headers, agent_id, agent_api_external_access_enabled=True
    )
    r = client.post(_keys_url(agent_id), headers=superuser_token_headers, json={})
    assert r.status_code == 400, r.text

    # ── Phase 2: both on -> issue succeeds ────────────────────────────────
    update_agent(client, superuser_token_headers, agent_id, agent_api_enabled=True)
    created = _issue_key(client, superuser_token_headers, agent_id, label="my-key")

    assert created["token"], "a self-issue must return the raw token value"
    assert len(created["token_prefix"]) == 8
    assert created["token"].startswith(created["token_prefix"])
    assert agent_id in created["base_url"]
    assert created["spec_url"].endswith("/openapi.json")
    assert created["is_active"] is True
    assert created["is_usable"] is True
    assert created["status"] == "active"
    assert created["issued_to"]["id"] == me["id"]
    assert created["issued_to"]["email"] == me["email"]

    # ── Phase 3: credential owned by the issuer, default slug, reveal ─────
    cred = get_credential_with_data(
        client, superuser_token_headers, created["credential_id"]
    )
    assert cred["owner_id"] == me["id"]
    assert cred["service_uri"] == "crm-agent-api"
    assert "token" not in cred["credential_data"]
    assert _reveal_key(client, superuser_token_headers, created["credential_id"]) == created["token"]
    assert _proxy_status(agent_id, created["token"], client) == 200

    # ── Phase 4: the access list never carries the value ──────────────────
    keys = _access_keys(client, superuser_token_headers, agent_id)
    assert len(keys) == 1
    assert "token" not in keys[0]
    assert keys[0]["id"] == created["id"]
    assert keys[0]["token_prefix"] == created["token_prefix"]
    assert keys[0]["status"] == "active"


# ── B. Owner gating, no existence leak, removed routes ───────────────────────


def test_key_routes_owner_gated_no_existence_leak_and_old_routes_removed(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Every key route is owner-gated exactly like ``/grants*``:
      1. Unauthenticated -> 401/403.
      2. Non-owner -> 404 on issue / access / revoke (no existence leak).
      3. Ghost agent id -> 404; unknown recipient -> 404.
      4. The removed ``GET /keys`` list and ``DELETE /keys/{id}`` no longer
         serve (D33).
    """
    producer = _setup_producer(client, superuser_token_headers, name="Key Gate Producer")
    agent_id = producer["id"]
    me = _me(client, superuser_token_headers)
    key = _issue_key(client, superuser_token_headers, agent_id, label="gate-key")

    # ── Phase 1: unauthenticated ───────────────────────────────────────────
    assert client.get(_access_url(agent_id)).status_code in (401, 403)
    assert client.post(_keys_url(agent_id), json={}).status_code in (401, 403)
    assert client.post(_revoke_url(agent_id, key["id"])).status_code in (401, 403)

    # ── Phase 2: non-owner -> 404 ──────────────────────────────────────────
    _, other_headers = create_random_user_with_headers(client)
    assert client.get(_access_url(agent_id), headers=other_headers).status_code == 404
    assert (
        client.post(
            _keys_url(agent_id), headers=other_headers, json={"issued_to_user_id": me["id"]}
        ).status_code
        == 404
    )
    assert (
        client.post(_revoke_url(agent_id, key["id"]), headers=other_headers).status_code == 404
    )

    # ── Phase 3: ghost agent / unknown recipient / ghost key -> 404 ────────
    ghost = str(uuid.uuid4())
    assert client.get(_access_url(ghost), headers=superuser_token_headers).status_code == 404
    r = client.post(
        _keys_url(agent_id),
        headers=superuser_token_headers,
        json={"issued_to_user_id": ghost},
    )
    assert r.status_code == 404, r.text
    assert (
        client.post(
            _revoke_url(agent_id, ghost), headers=superuser_token_headers
        ).status_code
        == 404
    )

    # ── Phase 4: the replaced routes are gone ──────────────────────────────
    assert client.get(_keys_url(agent_id), headers=superuser_token_headers).status_code in (404, 405)
    assert (
        client.delete(
            f"{_keys_url(agent_id)}/{key['id']}", headers=superuser_token_headers
        ).status_code
        in (404, 405)
    )
    # ... and the key was not touched by the failed delete.
    assert _access_keys(client, superuser_token_headers, agent_id)[0]["status"] == "active"


# ── C. Issue to someone else: the recipient owns the credential ──────────────


def test_issue_to_other_user_returns_no_token_and_only_recipient_can_read_it(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    D1/D2: a key issued to B belongs to B.
      1. ``token`` is null in the response; the default ``service_uri`` is set.
      2. The credential is owned by B and listed for B with status ``active``;
         it is not in A's credential list.
      3. A cannot reveal, rotate, read the holder view or the credential data
         (404, no leak); B can reveal and read the holder view (producer,
         issuer, scopes, urls).
    """
    producer = _setup_producer(client, superuser_token_headers, name="Issued To Producer")
    producer_id = producer["id"]
    me = _me(client, superuser_token_headers)
    b, b_headers = create_random_user_with_headers(client)

    created = _issue_key(
        client, superuser_token_headers, producer_id,
        issued_to_user_id=b["id"], label="for-b", scopes=["orders.read"],
    )
    cred_id = created["credential_id"]

    # ── Phase 1: no value for the issuer ───────────────────────────────────
    assert created["token"] is None
    assert created["issued_to"]["id"] == b["id"]
    assert created["linked_agent_count"] == 0

    # ── Phase 2: B owns it ─────────────────────────────────────────────────
    b_cred = get_credential_with_data(client, b_headers, cred_id)
    assert b_cred["owner_id"] == b["id"]
    assert b_cred["service_uri"] == "issued-to-producer-api"
    r = client.get(f"{API}/credentials/", headers=b_headers)
    assert r.status_code == 200, r.text
    row = next(c for c in r.json()["data"] if c["id"] == cred_id)
    assert row["agent_api_key_status"] == "active"
    r = client.get(f"{API}/credentials/", headers=superuser_token_headers)
    assert all(c["id"] != cred_id for c in r.json()["data"]), "A must not hold B's credential"

    # ── Phase 3: A is locked out, B is in ─────────────────────────────────
    assert client.post(_reveal_url(cred_id), headers=superuser_token_headers).status_code == 404
    assert client.post(_rotate_url(cred_id), headers=superuser_token_headers).status_code == 404
    assert client.get(_holder_url(cred_id), headers=superuser_token_headers).status_code == 404
    assert client.get(_spec_url(cred_id), headers=superuser_token_headers).status_code == 404
    # The issuer reading the recipient's credential data hits the generic
    # permission guard (route unchanged): 400 "Not enough permissions".
    r = client.get(f"{_credential_url(cred_id)}/with-data", headers=superuser_token_headers)
    assert r.status_code == 400, r.text

    token = _reveal_key(client, b_headers, cred_id)
    assert token.startswith(created["token_prefix"])
    assert _proxy_status(producer_id, token, client) == 200

    info = _holder(client, b_headers, cred_id)
    assert info["credential_id"] == cred_id
    assert info["status"] == "active"
    assert info["producer"]["id"] == producer_id
    assert info["producer"]["name"] == "Issued To Producer"
    assert info["issuer"]["id"] == me["id"]
    assert info["scopes"] == ["orders.read"]
    assert info["token_prefix"] == created["token_prefix"]
    assert producer_id in info["base_url"]
    assert info["spec_url"].endswith("/openapi.json")
    assert info["issuer_saw_value"] is False
    assert info["linked_agents"] == []


def test_explicit_service_uri_is_stored_edited_by_recipient_and_kept_by_rotate(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    D23: the issuer may override ``service_uri``; the recipient edits it via
    the existing credential update; rotating the key leaves it alone.
    """
    producer = _setup_producer(client, superuser_token_headers, name="Slot Producer")
    b, b_headers = create_random_user_with_headers(client)

    created = _issue_key(
        client, superuser_token_headers, producer["id"],
        issued_to_user_id=b["id"], service_uri="my-custom-slot",
    )
    cred_id = created["credential_id"]
    assert _holder(client, b_headers, cred_id)["service_uri"] == "my-custom-slot"

    updated = update_credential(client, b_headers, cred_id, service_uri="b-renamed-slot")
    assert updated["service_uri"] == "b-renamed-slot"
    assert _holder(client, b_headers, cred_id)["service_uri"] == "b-renamed-slot"

    _rotate(client, b_headers, cred_id)
    assert _holder(client, b_headers, cred_id)["service_uri"] == "b-renamed-slot"
    assert get_credential_with_data(client, b_headers, cred_id)["service_uri"] == "b-renamed-slot"


# ── D. Identity + scopes ──────────────────────────────────────────────────────


def test_issued_key_identity_is_recipient_overrides_forged_header_and_scopes_without_identity_flag(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The heart of the feature:
      1. The key's identity is its RECIPIENT B — never the issuer A — with NO
         identity header presented at all.
      2. Scopes come from the ``(producer, B)`` grant even though the
         producer's ``agent_api_identity_enabled`` is OFF.
      3. A forged ``X-Cinna-Caller-Identity`` (a VALID token of a genuinely
         different real user, A) is never consulted.
      4. Authorization + the identity header are stripped downstream.
    """
    producer = _setup_producer(
        client, superuser_token_headers, name="Identity Override Producer", identity_enabled=False
    )
    producer_id = producer["id"]
    b, b_headers = create_random_user_with_headers(client)
    key = _issue_to_recipient(
        client, superuser_token_headers, producer_id, b, b_headers,
        label="b-key", scopes=["orders.read"],
    )

    consumer = create_agent_via_api(
        client, superuser_token_headers, name="Identity Override Consumer"
    )
    drain_tasks()
    connection = _connect(
        client, superuser_token_headers, producer_id, consumer_agent_id=consumer["id"]
    )
    forged_identity_token = _read_owner_identity_token(
        client, superuser_token_headers, consumer["id"], connection["credential_id"]
    )
    me = _me(client, superuser_token_headers)

    tracking = _TrackingAdapter()
    lm = EnvironmentService._lifecycle_manager
    original = lm.get_adapter
    lm.get_adapter = lambda env: tracking
    try:
        r = client.get(
            _consumer_proxy_url(producer_id, "orders"),
            headers={
                **_bearer(key["token"]),
                "X-Cinna-Caller-Identity": forged_identity_token,
            },
        )
        assert r.status_code == 200, r.text
        fwd = tracking.forwarded_headers[0]

        assert fwd.get("x-cinna-caller-user-id") == b["id"]
        assert fwd.get("x-cinna-caller-user-id") != me["id"]
        assert fwd.get("x-cinna-caller-email") == b["email"]
        assert set(fwd.get("x-cinna-caller-scopes", "").split()) == {"orders.read"}
        assert "authorization" not in fwd
        assert "x-cinna-caller-identity" not in fwd
    finally:
        lm.get_adapter = original


def test_live_scope_edit_takes_effect_without_rekey(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Editing the (producer, recipient) grant changes the NEXT call's scopes
    for the SAME key value, and the recipient's holder view follows."""
    producer = _setup_producer(client, superuser_token_headers, name="Live Scope Producer")
    producer_id = producer["id"]
    b, b_headers = create_random_user_with_headers(client)
    key = _issue_to_recipient(
        client, superuser_token_headers, producer_id, b, b_headers,
        label="live-scope", scopes=["a"],
    )

    tracking = _TrackingAdapter()
    lm = EnvironmentService._lifecycle_manager
    original = lm.get_adapter
    lm.get_adapter = lambda env: tracking

    def _call() -> dict:
        tracking.forwarded_headers.clear()
        r = client.get(_consumer_proxy_url(producer_id, "orders"), headers=_bearer(key["token"]))
        assert r.status_code == 200, r.text
        return tracking.forwarded_headers[0]

    try:
        assert _call().get("x-cinna-caller-scopes") == "a"

        grants = _list_grants(client, superuser_token_headers, producer_id)
        assert len(grants) == 1
        r = client.put(
            _grant_url(producer_id, grants[0]["id"]),
            headers=superuser_token_headers,
            json={"scopes": ["b", "c"]},
        )
        assert r.status_code == 200, r.text

        assert set(_call().get("x-cinna-caller-scopes", "").split()) == {"b", "c"}
        assert set(_holder(client, b_headers, key["credential_id"])["scopes"]) == {"b", "c"}
    finally:
        lm.get_adapter = original


# ── E. Revoke (soft) ──────────────────────────────────────────────────────────


def test_revoke_is_soft_keeps_recipient_credential_and_blocks_reveal_and_rotate(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    D3: the issuer revoking stops the key at once but never deletes B's credential.
      1. 401 at the proxy; the issuer's response says ``revoked_by_issuer``.
      2. B's credential survives and the holder view explains why; the credential
         list row carries the status.
      3. Reveal, rotate and spec -> 409. The grant is untouched.
      4. Revoking again is idempotent (200, ``revoked_at`` unchanged).
    """
    producer = _setup_producer(client, superuser_token_headers, name="Revoke Producer")
    producer_id = producer["id"]
    b, b_headers = create_random_user_with_headers(client)
    key = _issue_to_recipient(
        client, superuser_token_headers, producer_id, b, b_headers,
        label="revoke-me", scopes=["x"],
    )
    cred_id = key["credential_id"]
    assert _proxy_status(producer_id, key["token"], client) == 200

    # ── Phase 1: revoke ────────────────────────────────────────────────────
    revoked = _revoke_key(client, superuser_token_headers, producer_id, key["id"])
    assert revoked["status"] == "revoked_by_issuer"
    assert revoked["is_active"] is False
    assert revoked["is_usable"] is False
    assert revoked["revoked_at"] is not None
    assert _proxy_status(producer_id, key["token"], client) == 401

    # ── Phase 2: B's credential survives with the reason ──────────────────
    assert get_credential_with_data(client, b_headers, cred_id)["id"] == cred_id
    assert _holder(client, b_headers, cred_id)["status"] == "revoked_by_issuer"
    r = client.get(f"{API}/credentials/", headers=b_headers)
    row = next(c for c in r.json()["data"] if c["id"] == cred_id)
    assert row["agent_api_key_status"] == "revoked_by_issuer"
    access_keys = _access_keys(client, superuser_token_headers, producer_id)
    assert [k["status"] for k in access_keys] == ["revoked_by_issuer"]

    # ── Phase 3: terminal -> 409 on reveal / rotate / spec; grant intact ──
    assert client.post(_reveal_url(cred_id), headers=b_headers).status_code == 409
    assert client.post(_rotate_url(cred_id), headers=b_headers).status_code == 409
    assert client.get(_spec_url(cred_id), headers=b_headers).status_code == 409
    grants = _list_grants(client, superuser_token_headers, producer_id)
    assert len(grants) == 1 and grants[0]["scopes"] == ["x"]

    # ── Phase 4: idempotent ────────────────────────────────────────────────
    again = _revoke_key(client, superuser_token_headers, producer_id, key["id"])
    assert again["status"] == "revoked_by_issuer"
    assert again["revoked_at"] == revoked["revoked_at"]


# ── F. Other statuses at the proxy ───────────────────────────────────────────


def test_producer_deleted_marks_key_producer_removed_and_401(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Deleting the producer keeps B's credential: status ``producer_removed``
    (terminal, 409 on reveal / rotate), and the value stops authenticating."""
    producer = _setup_producer(client, superuser_token_headers, name="Doomed Producer")
    producer_id = producer["id"]
    b, b_headers = create_random_user_with_headers(client)
    key = _issue_to_recipient(client, superuser_token_headers, producer_id, b, b_headers)
    cred_id = key["credential_id"]
    assert _proxy_status(producer_id, key["token"], client) == 200

    r = client.delete(f"{API}/agents/{producer_id}", headers=superuser_token_headers)
    assert r.status_code == 200, r.text

    info = _holder(client, b_headers, cred_id)
    assert info["status"] == "producer_removed"
    assert info["producer"] is None
    assert _proxy_status(producer_id, key["token"], client) in (401, 404)
    assert client.post(_reveal_url(cred_id), headers=b_headers).status_code == 409
    assert client.post(_rotate_url(cred_id), headers=b_headers).status_code == 409
    r = client.get(f"{API}/credentials/", headers=b_headers)
    row = next(c for c in r.json()["data"] if c["id"] == cred_id)
    assert row["agent_api_key_status"] == "producer_removed"


def test_issuer_deleted_marks_key_issuer_removed_and_401(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    A second superuser S issues a key on the superuser's producer, then S is
    deleted (D32): the producer survives, B's key becomes ``issuer_removed``
    (terminal) and 401s at once.
    """
    producer = _setup_producer(client, superuser_token_headers, name="Issuer Gone Producer")
    producer_id = producer["id"]
    s_email, s_password = random_email(), random_lower_string()
    r = client.post(
        f"{API}/users/",
        headers=superuser_token_headers,
        json={"email": s_email, "password": s_password, "is_superuser": True},
    )
    assert r.status_code == 200, r.text
    s = r.json()
    s_headers = user_authentication_headers(client=client, email=s_email, password=s_password)
    b, b_headers = create_random_user_with_headers(client)

    key = _issue_to_recipient(client, s_headers, producer_id, b, b_headers, label="s-issued")
    assert _proxy_status(producer_id, key["token"], client) == 200

    r = client.delete(f"{API}/users/{s['id']}", headers=superuser_token_headers)
    assert r.status_code == 200, r.text

    info = _holder(client, b_headers, key["credential_id"])
    assert info["status"] == "issuer_removed"
    assert info["issuer"] is None
    assert info["producer"]["id"] == producer_id
    assert _proxy_status(producer_id, key["token"], client) == 401
    assert client.post(_reveal_url(key["credential_id"]), headers=b_headers).status_code == 409
    assert client.post(_rotate_url(key["credential_id"]), headers=b_headers).status_code == 409


def test_kill_switch_pauses_keys_without_last_used_bump_and_expiry_is_terminal(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """
    Self-recovering vs terminal statuses (D4):
      1. ``agent_api_external_access_enabled`` off -> 401, status
         ``external_access_paused``, no ``last_used_at`` bump, connections
         unaffected; back on -> the same key works again.
      2. ``agent_api_enabled`` off -> ``api_disabled``; on -> works.
      3. Past ``expires_at`` -> 401 and ``expired`` (reveal / rotate 409).
    """
    producer = _setup_producer(client, superuser_token_headers, name="Kill Switch Producer")
    producer_id = producer["id"]
    b, b_headers = create_random_user_with_headers(client)
    key = _issue_to_recipient(client, superuser_token_headers, producer_id, b, b_headers)
    cred_id = key["credential_id"]
    connection = _connect(client, superuser_token_headers, producer_id, label="kill-switch-conn")

    assert _proxy_status(producer_id, key["token"], client) == 200
    before = _access_keys(client, superuser_token_headers, producer_id)[0]
    assert before["last_used_at"] is not None

    # ── Phase 1: kill switch ───────────────────────────────────────────────
    update_agent(
        client, superuser_token_headers, producer_id, agent_api_external_access_enabled=False
    )
    assert _proxy_status(producer_id, key["token"], client) == 401
    assert _proxy_status(producer_id, connection["token"], client) == 200
    assert _holder(client, b_headers, cred_id)["status"] == "external_access_paused"
    assert client.get(_spec_url(cred_id), headers=b_headers).status_code == 409
    after = _access_keys(client, superuser_token_headers, producer_id)[0]
    assert after["last_used_at"] == before["last_used_at"]
    assert after["status"] == "external_access_paused"
    assert after["is_usable"] is False

    update_agent(
        client, superuser_token_headers, producer_id, agent_api_external_access_enabled=True
    )
    assert _proxy_status(producer_id, key["token"], client) == 200
    assert _holder(client, b_headers, cred_id)["status"] == "active"

    # ── Phase 2: whole API off ─────────────────────────────────────────────
    update_agent(client, superuser_token_headers, producer_id, agent_api_enabled=False)
    assert _proxy_status(producer_id, key["token"], client) in (401, 404)
    assert _holder(client, b_headers, cred_id)["status"] == "api_disabled"
    assert client.get(_spec_url(cred_id), headers=b_headers).status_code == 409
    update_agent(client, superuser_token_headers, producer_id, agent_api_enabled=True)
    assert _proxy_status(producer_id, key["token"], client) == 200

    # ── Phase 3: expiry ────────────────────────────────────────────────────
    expiring = _issue_to_recipient(
        client, superuser_token_headers, producer_id, b, b_headers,
        label="expiring", expires_in_days=1,
    )
    assert _proxy_status(producer_id, expiring["token"], client) == 200
    _expire_key(db, expiring["id"])
    assert _proxy_status(producer_id, expiring["token"], client) == 401
    assert _holder(client, b_headers, expiring["credential_id"])["status"] == "expired"
    assert client.post(_reveal_url(expiring["credential_id"]), headers=b_headers).status_code == 409
    assert client.post(_rotate_url(expiring["credential_id"]), headers=b_headers).status_code == 409


# ── G. Rotation ──────────────────────────────────────────────────────────────


def test_rotate_swaps_value_in_place_with_grace_window_and_second_rotate_kills_first_previous(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    D6: recipient-only rotate.
      1. The new value works, the old one still works inside the grace window,
         the holder view reports ``previous_valid_until`` and ``rotated_at``.
      2. Credential id, key id and linked agents are unchanged;
         ``issuer_saw_value`` is false; reveal returns the new value.
      3. A second rotate inside the window replaces the previous hash: the
         first old value is dead, the second old value works.
      4. With the grace window closed (constant patched) the old value 401s.
    """
    producer = _setup_producer(client, superuser_token_headers, name="Rotate Producer")
    producer_id = producer["id"]
    b, b_headers = _make_developer(client, superuser_token_headers)
    key = _issue_to_recipient(client, superuser_token_headers, producer_id, b, b_headers)
    cred_id = key["credential_id"]
    v0 = key["token"]

    # B links the key to one of their own agents; rotate must keep the link.
    b_agent = create_agent_via_api(client, b_headers, name="Rotate Consumer")
    drain_tasks()
    link_credential_to_agent(client, b_headers, b_agent["id"], cred_id)
    drain_tasks()
    assert _access_keys(client, superuser_token_headers, producer_id)[0]["linked_agent_count"] == 1

    # ── Phase 1: first rotate ──────────────────────────────────────────────
    rot1 = _rotate(client, b_headers, cred_id)
    v1 = rot1["token"]
    assert v1 and v1 != v0
    assert v1.startswith(rot1["token_prefix"])
    assert rot1["previous_valid_until"] is not None
    assert _proxy_status(producer_id, v1, client) == 200
    assert _proxy_status(producer_id, v0, client) == 200, "old value valid inside grace"

    info = _holder(client, b_headers, cred_id)
    assert info["rotated_at"] is not None
    assert info["previous_valid_until"] is not None
    assert info["issuer_saw_value"] is False
    assert info["token_prefix"] == rot1["token_prefix"]
    assert _reveal_key(client, b_headers, cred_id) == v1

    # ── Phase 2: same credential, same key, same links ────────────────────
    access = _access_keys(client, superuser_token_headers, producer_id)
    assert len(access) == 1
    assert access[0]["id"] == key["id"]
    assert access[0]["credential_id"] == cred_id
    assert access[0]["linked_agent_count"] == 1
    assert access[0]["rotated_at"] is not None

    # ── Phase 3: second rotate replaces the previous hash ─────────────────
    v2 = _rotate(client, b_headers, cred_id)["token"]
    assert _proxy_status(producer_id, v2, client) == 200
    assert _proxy_status(producer_id, v1, client) == 200, "latest previous value still in grace"
    assert _proxy_status(producer_id, v0, client) == 401, "first old value dies on the second rotate"

    # ── Phase 4: grace closed -> the previous value is dead at once ───────
    with patch(GRACE_PATCH_TARGET, timedelta(seconds=-1)):
        v3 = _rotate(client, b_headers, cred_id)["token"]
    assert _proxy_status(producer_id, v3, client) == 200
    assert _proxy_status(producer_id, v2, client) == 401
    assert _holder(client, b_headers, cred_id)["previous_valid_until"] is None


def test_rotate_and_reveal_are_recipient_only_no_issuer_or_superuser_bypass(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    A (a non-superuser developer issuer) cannot rotate, reveal or read B's key
    (404); neither can a superuser; a non-key credential is 400 for its owner.
    """
    a, a_headers = _make_developer(client, superuser_token_headers)
    producer = _setup_producer(client, a_headers, name="Recipient Only Producer")
    b, b_headers = create_random_user_with_headers(client)
    key = _issue_to_recipient(client, a_headers, producer["id"], b, b_headers)
    cred_id = key["credential_id"]

    for headers in (a_headers, superuser_token_headers):
        assert client.post(_rotate_url(cred_id), headers=headers).status_code == 404
        assert client.post(_reveal_url(cred_id), headers=headers).status_code == 404
        assert client.get(_holder_url(cred_id), headers=headers).status_code == 404
    # ... and the failed attempts changed nothing for B.
    assert _holder(client, b_headers, cred_id)["rotated_at"] is None
    assert _reveal_key(client, b_headers, cred_id) == key["token"]

    # Unauthenticated, ghost id, and a credential that is not a key.
    assert client.post(_rotate_url(cred_id)).status_code in (401, 403)
    assert client.post(_rotate_url(str(uuid.uuid4())), headers=b_headers).status_code == 404
    other = create_random_credential(client, b_headers, credential_type="api_token")
    assert client.post(_rotate_url(other["id"]), headers=b_headers).status_code == 400
    assert client.get(_holder_url(other["id"]), headers=b_headers).status_code == 400


# ── H. Recipient deletes their credential ────────────────────────────────────


def test_recipient_deleting_credential_removes_key_and_audits_on_issuer(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """D26: B deleting the credential cascades the token (401), A's access
    list drops the key (the grant stays) and A gets DELETED_BY_HOLDER."""
    producer = _setup_producer(client, superuser_token_headers, name="Holder Delete Producer")
    producer_id = producer["id"]
    b, b_headers = create_random_user_with_headers(client)
    key = _issue_to_recipient(
        client, superuser_token_headers, producer_id, b, b_headers, scopes=["s"]
    )
    assert _proxy_status(producer_id, key["token"], client) == 200
    assert len(_access_keys(client, superuser_token_headers, producer_id)) == 1

    r = client.delete(_credential_url(key["credential_id"]), headers=b_headers)
    assert r.status_code == 200, r.text

    assert _proxy_status(producer_id, key["token"], client) == 401
    assert _access_keys(client, superuser_token_headers, producer_id) == []
    grants = _list_grants(client, superuser_token_headers, producer_id)
    assert [g["user_id"] for g in grants] == [b["id"]]

    events = find_security_events(
        client, superuser_token_headers, "AGENT_API_EXTERNAL_KEY_DELETED_BY_HOLDER"
    )
    assert len(events) == 1


def test_deleted_recipient_user_removes_key(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Deleting the recipient user cascades the token row away (subject FK)
    and the value stops authenticating."""
    producer = _setup_producer(client, superuser_token_headers, name="Orphan Key Producer")
    b, b_headers = create_random_user_with_headers(client)
    key = _issue_to_recipient(client, superuser_token_headers, producer["id"], b, b_headers)
    assert _proxy_status(producer["id"], key["token"], client) == 200

    r = client.delete(f"{API}/users/{b['id']}", headers=superuser_token_headers)
    assert r.status_code == 200, r.text

    assert _access_keys(client, superuser_token_headers, producer["id"]) == []
    assert _proxy_status(producer["id"], key["token"], client) == 401


# ── I. Sharing ban + /access grouping ────────────────────────────────────────


def test_sharing_refused_for_issued_key_and_access_groups_grants_and_keys_per_person(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    D9 + D28: B cannot share the key (direct or template) and ``/access``
    groups grants + keys per person:
      * B: two keys (one revoked) + the shared grant.
      * C: a grant with no key.
      * the issuer: a self-key.
    """
    producer = _setup_producer(client, superuser_token_headers, name="Access Producer")
    producer_id = producer["id"]
    me = _me(client, superuser_token_headers)
    b, b_headers = create_random_user_with_headers(client)
    c = create_random_user(client)

    k1 = _issue_to_recipient(
        client, superuser_token_headers, producer_id, b, b_headers, label="b1", scopes=["a"]
    )
    k2 = _issue_to_recipient(
        client, superuser_token_headers, producer_id, b, b_headers, label="b2", scopes=["a", "b"]
    )
    _revoke_key(client, superuser_token_headers, producer_id, k1["id"])
    _create_grant(client, superuser_token_headers, producer_id, c["id"], ["z"])
    own = _issue_key(client, superuser_token_headers, producer_id, label="mine")

    # ── Sharing refused ────────────────────────────────────────────────────
    r = client.patch(
        f"{_credential_url(k2['credential_id'])}/sharing",
        headers=b_headers,
        json={"allow_sharing": True},
    )
    assert r.status_code in (400, 403), r.text
    r = client.put(
        _credential_url(k2["credential_id"]),
        headers=b_headers,
        json={"allow_template_sharing": True},
    )
    assert r.status_code in (400, 403), r.text

    # ── /access grouping ───────────────────────────────────────────────────
    access = _access(client, superuser_token_headers, producer_id)
    assert access["external_access_enabled"] is True
    assert access["identity_enabled"] is False
    assert access["count"] == 3
    by_user = {e["user"]["id"]: e for e in access["data"]}
    assert set(by_user) == {b["id"], c["id"], me["id"]}

    b_entry = by_user[b["id"]]
    assert b_entry["user"]["email"] == b["email"]
    assert b_entry["grant_id"] is not None
    assert set(b_entry["scopes"]) == {"a", "b"}
    statuses = {k["id"]: k["status"] for k in b_entry["keys"]}
    assert statuses == {k1["id"]: "revoked_by_issuer", k2["id"]: "active"}

    c_entry = by_user[c["id"]]
    assert c_entry["keys"] == [] and c_entry["scopes"] == ["z"]

    me_entry = by_user[me["id"]]
    assert [k["id"] for k in me_entry["keys"]] == [own["id"]]
    assert me_entry["grant_id"] is None


# ── J. Grant / key independence ──────────────────────────────────────────────


def test_grant_and_key_lifecycles_independent_and_issue_upserts_scopes(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Scopes live on the grant, never the key.
      1. Issuing key1 with scopes upserts the (producer, B) grant.
      2. Issuing key2 for the SAME recipient with DIFFERENT scopes upserts the
         SAME row (not a 409) — both keys share that scope set.
      3. Revoking key1 does NOT delete the grant; key2 is unaffected.
      4. Deleting the GRANT does NOT kill key2 — it keeps authenticating with
         zero scopes.
    """
    producer = _setup_producer(client, superuser_token_headers, name="Independence Producer")
    producer_id = producer["id"]
    b, b_headers = create_random_user_with_headers(client)

    key1 = _issue_to_recipient(
        client, superuser_token_headers, producer_id, b, b_headers,
        label="key-1", scopes=["orders.read"],
    )
    grants = _list_grants(client, superuser_token_headers, producer_id)
    assert len(grants) == 1
    assert grants[0]["user_id"] == b["id"]
    assert grants[0]["scopes"] == ["orders.read"]

    key2 = _issue_to_recipient(
        client, superuser_token_headers, producer_id, b, b_headers,
        label="key-2", scopes=["orders.write"],
    )
    grants = _list_grants(client, superuser_token_headers, producer_id)
    assert len(grants) == 1, "issuing a second key for the same recipient must upsert"
    assert grants[0]["scopes"] == ["orders.write"]

    tracking = _TrackingAdapter()
    lm = EnvironmentService._lifecycle_manager
    original = lm.get_adapter
    lm.get_adapter = lambda env: tracking
    try:
        for key in (key1, key2):
            tracking.forwarded_headers.clear()
            r = client.get(
                _consumer_proxy_url(producer_id, "orders"), headers=_bearer(key["token"])
            )
            assert r.status_code == 200, r.text
            assert tracking.forwarded_headers[0].get("x-cinna-caller-scopes") == "orders.write"

        _revoke_key(client, superuser_token_headers, producer_id, key1["id"])
        grants = _list_grants(client, superuser_token_headers, producer_id)
        assert len(grants) == 1, "revoking a key must not delete the grant"
        assert _proxy_status(producer_id, key1["token"], client, "orders") == 401

        tracking.forwarded_headers.clear()
        r = client.get(_consumer_proxy_url(producer_id, "orders"), headers=_bearer(key2["token"]))
        assert r.status_code == 200, "key2 must be unaffected by key1's revocation"
        assert tracking.forwarded_headers[0].get("x-cinna-caller-scopes") == "orders.write"

        r = client.delete(_grant_url(producer_id, grants[0]["id"]), headers=superuser_token_headers)
        assert r.status_code == 200, r.text
        tracking.forwarded_headers.clear()
        r = client.get(_consumer_proxy_url(producer_id, "orders"), headers=_bearer(key2["token"]))
        assert r.status_code == 200, "deleting the grant must not revoke the key"
        assert "x-cinna-caller-scopes" not in tracking.forwarded_headers[0]
    finally:
        lm.get_adapter = original


# ── K. Reveal endpoint + credential projections ──────────────────────────────


def test_reveal_returns_token_authenticates_and_audits_once_per_call(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    ``POST /credentials/{id}/agent-api-key/reveal`` (recipient):
      1. Returns a value that authenticates a proxy call.
      2. One ``AGENT_API_EXTERNAL_KEY_REVEALED`` event per call on B's account.
      3. ``GET /with-data`` never carries ``token`` and writes no event.
    """
    producer = _setup_producer(client, superuser_token_headers, name="Reveal Producer")
    producer_id = producer["id"]
    b, b_headers = create_random_user_with_headers(client)
    created = _issue_key(client, superuser_token_headers, producer_id, issued_to_user_id=b["id"])
    cred_id = created["credential_id"]

    revealed = _reveal_key(client, b_headers, cred_id)
    assert _proxy_status(producer_id, revealed, client) == 200
    assert len(find_security_events(client, b_headers, "AGENT_API_EXTERNAL_KEY_REVEALED")) == 1

    with_data = get_credential_with_data(client, b_headers, cred_id)
    assert "token" not in with_data["credential_data"]
    assert len(find_security_events(client, b_headers, "AGENT_API_EXTERNAL_KEY_REVEALED")) == 1

    assert _reveal_key(client, b_headers, cred_id) == revealed
    assert len(find_security_events(client, b_headers, "AGENT_API_EXTERNAL_KEY_REVEALED")) == 2
    # The issuer was never shown the value, so no REVEALED event on their account.
    assert find_security_events(
        client, superuser_token_headers, "AGENT_API_EXTERNAL_KEY_REVEALED"
    ) == []


def test_with_data_unchanged_for_agent_api_connection_credential(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """No blast radius: an ``agent_api`` CONNECTION credential still returns
    ``token`` from ``with-data`` and writes no REVEALED event."""
    producer = _setup_producer(
        client, superuser_token_headers, name="Connection With-Data Producer"
    )
    connection = _connect(client, superuser_token_headers, producer["id"], label="conn-with-data")

    with_data = get_credential_with_data(
        client, superuser_token_headers, connection["credential_id"]
    )
    assert with_data["credential_data"]["token"] == connection["token"]
    assert find_security_events(
        client, superuser_token_headers, "AGENT_API_EXTERNAL_KEY_REVEALED"
    ) == []


def test_reveal_owner_gated_no_existence_leak_and_400_for_non_keys(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Reveal 401/403 unauthenticated, 404 (never 403) for a non-owner or a
    ghost id, 400 for the owner's own connection / non-key credential."""
    producer = _setup_producer(client, superuser_token_headers, name="Reveal Gate Producer")
    producer_id = producer["id"]
    key = _issue_key(client, superuser_token_headers, producer_id, label="gate-reveal")

    assert client.post(_reveal_url(key["credential_id"])).status_code in (401, 403)
    _, other_headers = create_random_user_with_headers(client)
    assert client.post(_reveal_url(key["credential_id"]), headers=other_headers).status_code == 404
    assert (
        client.post(_reveal_url(str(uuid.uuid4())), headers=superuser_token_headers).status_code
        == 404
    )

    connection = _connect(client, superuser_token_headers, producer_id, label="wrong-type-conn")
    r = client.post(_reveal_url(connection["credential_id"]), headers=superuser_token_headers)
    assert r.status_code == 400, r.text
    other = create_random_credential(
        client, superuser_token_headers, credential_type="api_token"
    )
    r = client.post(_reveal_url(other["id"]), headers=superuser_token_headers)
    assert r.status_code == 400, r.text


def test_update_credential_through_with_data_seed_does_not_blank_stored_token(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The Edit dialog seeds its form from ``with-data`` (no ``token`` for an
    issued key). Round-tripping it through ``PUT /credentials/{id}`` as the
    recipient must NOT wipe the stored token."""
    producer = _setup_producer(client, superuser_token_headers, name="Edit Preserve Producer")
    b, b_headers = create_random_user_with_headers(client)
    key = _issue_to_recipient(client, superuser_token_headers, producer["id"], b, b_headers)

    with_data = get_credential_with_data(client, b_headers, key["credential_id"])
    assert "token" not in with_data["credential_data"]

    updated = update_credential(
        client,
        b_headers,
        key["credential_id"],
        name="Renamed External Key",
        credential_data=with_data["credential_data"],
    )
    assert updated["name"] == "Renamed External Key"
    assert _reveal_key(client, b_headers, key["credential_id"]) == key["token"]
    assert _proxy_status(producer["id"], key["token"], client) == 200


def test_issued_key_is_classified_mine_and_never_synced_into_agent_env(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A connection credential classifies as "automatic"; an issued key is
    "mine" for its recipient. (Env sync of keys is P3 — covered elsewhere; here
    only the classification, owner-side.)"""
    producer = _setup_producer(client, superuser_token_headers, name="Classify Producer")
    producer_id = producer["id"]
    connection = _connect(client, superuser_token_headers, producer_id, label="conn-cred")
    key = _issue_key(client, superuser_token_headers, producer_id, label="key-cred")

    r = client.get(f"{API}/credentials/", headers=superuser_token_headers)
    assert r.status_code == 200, r.text
    rows = {c["id"]: c for c in r.json()["data"]}
    assert rows[connection["credential_id"]]["category"] == "automatic"
    assert rows[connection["credential_id"]]["agent_api_key_status"] is None
    assert rows[key["credential_id"]]["category"] == "mine"
    assert rows[key["credential_id"]]["agent_api_key_status"] == "active"


# ── L. Connections are unaffected by the issued-key lifecycle ────────────────


def test_connection_unaffected_by_key_revoke_kill_switch_and_holder_routes(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A connection on the same producer keeps authenticating through a key
    revoke; the key-only holder routes refuse a connection credential (400)."""
    producer = _setup_producer(client, superuser_token_headers, name="Mixed Producer")
    producer_id = producer["id"]
    connection = _connect(client, superuser_token_headers, producer_id, label="conn")
    key = _issue_key(client, superuser_token_headers, producer_id, label="key")

    _revoke_key(client, superuser_token_headers, producer_id, key["id"])
    assert _proxy_status(producer_id, key["token"], client) == 401
    assert _proxy_status(producer_id, connection["token"], client) == 200

    conn_cred = connection["credential_id"]
    assert client.get(_holder_url(conn_cred), headers=superuser_token_headers).status_code == 400
    assert client.post(_rotate_url(conn_cred), headers=superuser_token_headers).status_code == 400
    assert [k["id"] for k in _access_keys(client, superuser_token_headers, producer_id)] == [
        key["id"]
    ], "connections never appear in the issued-key access list"


# ── M. Security events ───────────────────────────────────────────────────────


def test_security_events_cover_issue_revoke_reveal_and_rotate(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """D13: CREATED on A, REVEALED + ROTATED on B, REVOKED on both A and B."""
    producer = _setup_producer(client, superuser_token_headers, name="Audit Producer")
    producer_id = producer["id"]
    b, b_headers = create_random_user_with_headers(client)

    created = _issue_key(client, superuser_token_headers, producer_id, issued_to_user_id=b["id"])
    _reveal_key(client, b_headers, created["credential_id"])
    _rotate(client, b_headers, created["credential_id"])
    _revoke_key(client, superuser_token_headers, producer_id, created["id"])

    def count(headers: dict[str, str], event_type: str) -> int:
        return len(find_security_events(client, headers, event_type))

    assert count(superuser_token_headers, "AGENT_API_EXTERNAL_KEY_CREATED") == 1
    assert count(b_headers, "AGENT_API_EXTERNAL_KEY_CREATED") == 0
    assert count(b_headers, "AGENT_API_EXTERNAL_KEY_REVEALED") == 1
    assert count(b_headers, "AGENT_API_EXTERNAL_KEY_ROTATED") == 1
    assert count(superuser_token_headers, "AGENT_API_EXTERNAL_KEY_ROTATED") == 0
    assert count(superuser_token_headers, "AGENT_API_EXTERNAL_KEY_REVOKED") == 1
    assert count(b_headers, "AGENT_API_EXTERNAL_KEY_REVOKED") == 1


# ── N. Review-fix behaviours ─────────────────────────────────────────────────


def test_put_with_token_in_credential_data_leaves_issued_key_value_unchanged(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A recipient cannot overwrite an issued key's stored value through the
    generic credential update: a ``token`` in ``credential_data`` is ignored,
    reveal still returns the working value, and it still authenticates."""
    producer = _setup_producer(client, superuser_token_headers, name="Token Overwrite Producer")
    b, b_headers = create_random_user_with_headers(client)
    key = _issue_to_recipient(client, superuser_token_headers, producer["id"], b, b_headers)

    update_credential(
        client,
        b_headers,
        key["credential_id"],
        credential_data={"token": "not-the-real-value"},
    )

    assert _reveal_key(client, b_headers, key["credential_id"]) == key["token"]
    assert _proxy_status(producer["id"], key["token"], client) == 200
    assert _proxy_status(producer["id"], "not-the-real-value", client) == 401


def test_default_service_uri_never_contains_double_dash_after_slug_cut(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """The slug is capped at 40 chars and the trailing ``-`` trimmed after the
    cut, so the default never reads ``...--api``."""
    name = "a" * 39 + " bbb"
    producer = _setup_producer(client, superuser_token_headers, name=name)
    key = _issue_key(client, superuser_token_headers, producer["id"])
    cred = get_credential_with_data(client, superuser_token_headers, key["credential_id"])
    assert cred["service_uri"] == "a" * 39 + "-api"
    assert "--" not in cred["service_uri"]
