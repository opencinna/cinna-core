"""
Integration tests: Agent REST API **external keys** (Phase 3 of the external-keys
plan — `docs/plans/agent_api_external_keys_plan.md`).

Complements ``agents_agent_api_test.py`` (L1 connection token, connect helper,
policy enforcement, consumer proxy) and ``agents_agent_api_grants_test.py`` (L2
caller identity + per-user scopes for CONNECTIONS). Here we cover the second
product behind the proxy (plan §2): a key a human copies into a laptop script,
server, or cron job, bound to a platform user (``subject_user_id``) at mint time.

Business rules tested (plan §6 + two implementation behaviours beyond the
letter of the plan):
  1. Minting is blocked (400) while ``agent_api_external_access_enabled`` is
     off; ALSO blocked (400) while ``agent_api_enabled`` is off even with
     external access on; allowed (200) once both are on.
  2. Key routes (mint/list/revoke) are owner-gated exactly like ``/grants*``:
     404 (never 403) for a non-owner or a ghost agent id — no existence leak.
  3. An external key authenticates a proxy call with NO identity header at all
     (the identity rides the token, not a header) and the producer receives
     authoritative ``X-Cinna-Caller-*`` headers for the key's SUBJECT, never
     the issuer who minted it.
  4. A forged ``X-Cinna-Caller-Identity`` header — even a cryptographically
     VALID identity token belonging to a genuinely different real user — is
     never consulted for an external key call (D2 precedence).
  5. Scopes are injected for an external key even though the producer's
     ``agent_api_identity_enabled`` is OFF (D3 — a key is self-evidently
     intentional, unlike an anonymous connection).
  6. Live scope edits on the ``(producer, subject)`` grant take effect on the
     NEXT call — no key re-issue.
  7. Expired key → 401. Revoked key → 401. Deleting the underlying credential
     directly cascades the token → 401 (same shape, no distinction leaked).
  8. A key credential classifies as "mine"; a connection credential stays
     "automatic" (D4) — and regardless of category, an external key is NEVER
     written into a consumer env's ``credentials.json`` (D4).
  9. Grant and key lifecycles are independent in both directions (D5):
     revoking a key leaves the grant intact; deleting the grant leaves the key
     authenticating with zero scopes.
  10. Minting a key with scopes for a subject that already has a grant UPSERTS
      that grant (shared by both keys, D7) rather than 409ing.
  11. Beyond the plan letter: ``agent_api_external_access_enabled`` is enforced
      AT THE PROXY, not just at mint — flipping it off 401s an already-issued
      key immediately (without touching agent-to-agent connections) and never
      bumps ``last_used_at`` on the rejected call.
  12. ``POST /credentials/{id}/agent-api-key/reveal`` is the ONLY path that
      returns an external key's value after mint: it returns the token
      (matching the mint-time value, and it authenticates a proxy call) and
      writes exactly one ``AGENT_API_EXTERNAL_KEY_REVEALED`` event per call.
      It is owner-gated (404, no leak) and 400s for a credential that is not
      an external key.
  13. ``GET /credentials/{id}/with-data`` strips ``token`` and writes no event
      for an external key, but is completely unchanged for an ``agent_api``
      *connection* credential and every other credential type (no blast
      radius). Editing an external-key credential through the generic
      ``PUT /credentials/{id}`` — the way the Edit dialog does, seeding from
      the now-token-less ``with-data`` payload — must not blank the stored
      token; a subsequent reveal still returns the original value.

API-only: keys are minted via ``POST .../agent-api/keys`` and read back from
the created ``agent_api`` credential / the key list projection — the raw value
is never re-derived. Where an identity token from a genuinely different real
user is needed (rule 4), it is obtained via the SAME API-only path
``agents_agent_api_grants_test.py`` uses (link a connection credential to a
superuser-owned consumer, drain the sync, read the synthetic
``owner_identity_token`` the env adapter captured) — never a second full
account, since a freshly-signed-up user has no default AI credential and its
environment never reaches "running" in this suite (see that file's docstring).
"""
import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlmodel import Session

from app.core.config import settings
from app.models import AgentApiToken
from app.services.environments.environment_service import EnvironmentService
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.agent import create_agent_via_api, update_agent
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.credential import (
    create_random_credential,
    get_credential_with_data,
    link_credential_to_agent,
    update_credential,
)
from tests.utils.mfa import find_security_events
from tests.utils.user import (
    create_random_user,
    create_random_user_with_headers,
    promote_to_developer,
)

API = settings.API_V1_STR


# ── URL helpers ───────────────────────────────────────────────────────────────


def _owner_base(agent_id: str) -> str:
    return f"{API}/agents/{agent_id}/agent-api"


def _keys_url(agent_id: str) -> str:
    return f"{_owner_base(agent_id)}/keys"


def _key_url(agent_id: str, key_id: str) -> str:
    return f"{_keys_url(agent_id)}/{key_id}"


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


def _reveal_url(credential_id: str) -> str:
    return f"{_credential_url(credential_id)}/agent-api-key/reveal"


def _bearer(token_value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token_value}"}


# ── Setup helpers ─────────────────────────────────────────────────────────────


def _setup_producer(
    client: TestClient,
    headers: dict[str, str],
    name: str = "External Key Producer",
    agent_api_enabled: bool = True,
    external_access_enabled: bool = True,
    identity_enabled: bool = False,
) -> dict:
    """Create an agent with the agent_api toggles this file needs."""
    agent = create_agent_via_api(client, headers, name=name)
    drain_tasks()
    update_agent(
        client,
        headers,
        agent["id"],
        agent_api_enabled=agent_api_enabled,
        agent_api_identity_enabled=identity_enabled,
        agent_api_external_access_enabled=external_access_enabled,
    )
    return agent


def _mint_key(
    client: TestClient,
    headers: dict[str, str],
    producer_id: str,
    subject_user_id: str,
    label: str | None = None,
    scopes: list[str] | None = None,
    read_only_override: bool = False,
    expires_in_days: int | None = None,
) -> dict:
    """Mint an external key via POST .../agent-api/keys. Returns the raw response."""
    body: dict = {
        "subject_user_id": subject_user_id,
        "read_only_override": read_only_override,
    }
    if label is not None:
        body["label"] = label
    if scopes is not None:
        body["scopes"] = scopes
    if expires_in_days is not None:
        body["expires_in_days"] = expires_in_days
    r = client.post(_keys_url(producer_id), headers=headers, json=body)
    assert r.status_code == 200, f"Mint key failed: {r.text}"
    return r.json()


def _list_keys(client: TestClient, headers: dict[str, str], producer_id: str) -> list[dict]:
    r = client.get(_keys_url(producer_id), headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _revoke_key(
    client: TestClient, headers: dict[str, str], producer_id: str, key_id: str
) -> dict:
    r = client.delete(_key_url(producer_id, key_id), headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


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
    """Connect helper (L1 connection, not an external key). Mirrors the private
    ``_mint_token`` helper in the sibling agent_api test files — the connection
    product this file must NOT regress or confuse with a key."""
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


def _reveal_key(
    client: TestClient, headers: dict[str, str], credential_id: str
) -> str:
    """Reveal a key's value via POST .../agent-api-key/reveal. Returns the raw response."""
    r = client.post(_reveal_url(credential_id), headers=headers)
    assert r.status_code == 200, f"Reveal failed: {r.text}"
    return r.json()["token"]


def _expire_key(db: Session, key_id: str) -> None:
    """Force a key's ``expires_at`` into the past.

    There is no API surface to mint an already-expired key (``expires_in_days``
    requires >= 1), so this mirrors the documented DB-seam pattern used for the
    env policy cache (``_set_policy_cache`` in ``agents_agent_api_test.py``) —
    internal state with no API mutation path.
    """
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

    API-only, mirroring ``agents_agent_api_grants_test.py._read_owner_identity_token``:
    link an ``agent_api`` connection credential to the consumer, drain the sync,
    and read the synthetic ``owner_identity_token`` entry the env adapter
    captured via ``set_credentials`` — exactly the value a consumer agent would
    send on ``X-Cinna-Caller-Identity``.
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


# ── A. Mint gating (two independent 400 opt-ins) ──────────────────────────────


def test_mint_requires_both_agent_api_enabled_and_external_access_toggle(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Minting requires TWO opt-ins, each independently 400 while off:
      1. Both off (fresh agent defaults) → 400.
      2. external_access ON, agent_api_enabled still OFF → 400 (proves the
         second gate independently, not just the first).
      3. Both ON → 200, with the raw token, an 8-char prefix, base_url/spec_url,
         is_active/is_usable True, and the correct subject. The value never
         reappears on GET /keys.
    """
    agent = create_agent_via_api(client, superuser_token_headers, name="Toggle Gate Agent")
    drain_tasks()
    agent_id = agent["id"]
    me = client.get(f"{API}/users/me", headers=superuser_token_headers).json()

    # ── Phase 1: both opt-ins off ─────────────────────────────────────────
    r = client.post(
        _keys_url(agent_id), headers=superuser_token_headers, json={"subject_user_id": me["id"]}
    )
    assert r.status_code == 400, r.text

    # ── Phase 2: external_access ON, agent_api_enabled still OFF ──────────
    update_agent(
        client, superuser_token_headers, agent_id, agent_api_external_access_enabled=True
    )
    r = client.post(
        _keys_url(agent_id), headers=superuser_token_headers, json={"subject_user_id": me["id"]}
    )
    assert r.status_code == 400, r.text

    # ── Phase 3: agent_api_enabled ON too → mint succeeds ─────────────────
    update_agent(client, superuser_token_headers, agent_id, agent_api_enabled=True)
    created = _mint_key(client, superuser_token_headers, agent_id, me["id"], label="my-key")

    assert created["token"], "mint must return the raw token value"
    assert len(created["token_prefix"]) == 8
    assert created["token"].startswith(created["token_prefix"])
    assert agent_id in created["base_url"]
    assert created["spec_url"].endswith("/openapi.json")
    assert created["is_active"] is True
    assert created["is_usable"] is True
    assert created["subject"]["id"] == me["id"]
    assert created["subject"]["email"] == me["email"]

    keys = _list_keys(client, superuser_token_headers, agent_id)
    assert len(keys) == 1
    assert "token" not in keys[0], "the list projection must never carry the value"
    assert keys[0]["id"] == created["id"]
    assert keys[0]["token_prefix"] == created["token_prefix"]


# ── B. Owner gating, no existence leak ────────────────────────────────────────


def test_key_routes_owner_gated_no_existence_leak(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Every key route is owner-gated exactly like ``/grants*``:
      1. Unauthenticated → 401/403.
      2. Non-owner → 404 on mint / list / revoke (no existence leak).
      3. Ghost agent id → 404 on list.
    """
    producer = _setup_producer(client, superuser_token_headers, name="Key Gate Producer")
    agent_id = producer["id"]
    me = client.get(f"{API}/users/me", headers=superuser_token_headers).json()
    key = _mint_key(client, superuser_token_headers, agent_id, me["id"], label="gate-key")

    # ── Phase 1: unauthenticated ───────────────────────────────────────────
    assert client.get(_keys_url(agent_id)).status_code in (401, 403)
    assert client.post(
        _keys_url(agent_id), json={"subject_user_id": me["id"]}
    ).status_code in (401, 403)
    assert client.delete(_key_url(agent_id, key["id"])).status_code in (401, 403)

    # ── Phase 2: non-owner → 404 ──────────────────────────────────────────
    _, other_headers = create_random_user_with_headers(client)
    assert client.get(_keys_url(agent_id), headers=other_headers).status_code == 404
    assert (
        client.post(
            _keys_url(agent_id), headers=other_headers, json={"subject_user_id": me["id"]}
        ).status_code
        == 404
    )
    assert (
        client.delete(_key_url(agent_id, key["id"]), headers=other_headers).status_code == 404
    )

    # ── Phase 3: ghost agent id → 404 ─────────────────────────────────────
    ghost = str(uuid.uuid4())
    assert client.get(_keys_url(ghost), headers=superuser_token_headers).status_code == 404


# ── C. The core security property: D2 precedence + D3 scopes ─────────────────


def test_external_key_identity_overrides_forged_header_and_scopes_without_identity_flag(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    D2 + D3, the heart of the feature:
      1. The key's identity is the SUBJECT bound at mint — never the issuer who
         minted it — with NO identity header presented at all (unlike a
         connection, the identity rides the token itself).
      2. This holds even though the producer's ``agent_api_identity_enabled``
         is OFF (D3 — an explicitly issued key is self-evidently intentional).
      3. A forged ``X-Cinna-Caller-Identity`` header — even a cryptographically
         VALID identity token belonging to a genuinely different real user (the
         superuser, via their own connection) — is never consulted; the
         token-bound subject still wins (D2 precedence).
      4. Authorization + the (ignored) identity header are stripped downstream.
    """
    producer = _setup_producer(
        client, superuser_token_headers, name="Identity Override Producer", identity_enabled=False
    )
    producer_id = producer["id"]
    subject = create_random_user(client)  # a real user, distinct from the issuer

    key = _mint_key(
        client,
        superuser_token_headers,
        producer_id,
        subject["id"],
        label="subject-key",
        scopes=["orders.read"],
    )

    # A cryptographically VALID identity token belonging to a genuinely
    # different real user (the superuser), obtained the API-only way via their
    # own connection to this same producer.
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
    me = client.get(f"{API}/users/me", headers=superuser_token_headers).json()

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

        # Subject wins, never the superuser (a genuinely different, validly
        # identified real user whose token was supplied on the header).
        assert fwd.get("x-cinna-caller-user-id") == subject["id"]
        assert fwd.get("x-cinna-caller-user-id") != me["id"]
        assert fwd.get("x-cinna-caller-email") == subject["email"]

        # Scopes injected despite agent_api_identity_enabled=False (D3).
        assert set(fwd.get("x-cinna-caller-scopes", "").split()) == {"orders.read"}

        # Secrets stripped.
        assert "authorization" not in fwd
        assert "x-cinna-caller-identity" not in fwd
    finally:
        lm.get_adapter = original


# ── D. Live scope edits, no re-issue ──────────────────────────────────────────


def test_live_scope_edit_takes_effect_without_rekey(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Editing the (producer, subject) grant changes the NEXT call's scopes for
    the SAME key value — no re-mint needed (D5/D7)."""
    producer = _setup_producer(client, superuser_token_headers, name="Live Scope Producer")
    producer_id = producer["id"]
    subject = create_random_user(client)
    key = _mint_key(
        client, superuser_token_headers, producer_id, subject["id"],
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
        fwd = _call()
        assert fwd.get("x-cinna-caller-scopes") == "a"

        grants = _list_grants(client, superuser_token_headers, producer_id)
        assert len(grants) == 1
        gid = grants[0]["id"]
        r = client.put(
            _grant_url(producer_id, gid),
            headers=superuser_token_headers,
            json={"scopes": ["b", "c"]},
        )
        assert r.status_code == 200, r.text

        fwd = _call()  # same key value, not re-minted
        assert set(fwd.get("x-cinna-caller-scopes", "").split()) == {"b", "c"}
    finally:
        lm.get_adapter = original


# ── E. Expiry, revocation, cascade ────────────────────────────────────────────


def test_expired_revoked_and_credential_deleted_key_all_401(
    client: TestClient, superuser_token_headers: dict[str, str], db: Session
) -> None:
    """
    Three independent ways a key stops authenticating, each 401 (no distinction
    leaked between them or from an invalid token):
      1. Past ``expires_at``.
      2. Explicit revoke (``DELETE /keys/{id}``).
      3. Deleting the underlying credential directly (cascades the token — the
         same revocation path a connection's disconnect uses).
    """
    producer = _setup_producer(client, superuser_token_headers, name="Expiry Producer")
    producer_id = producer["id"]
    me = client.get(f"{API}/users/me", headers=superuser_token_headers).json()

    # ── Phase 1: expired ───────────────────────────────────────────────────
    expiring = _mint_key(
        client, superuser_token_headers, producer_id, me["id"],
        label="expiring", expires_in_days=1,
    )
    r = client.get(_consumer_proxy_url(producer_id, "ping"), headers=_bearer(expiring["token"]))
    assert r.status_code == 200, "not yet expired"

    _expire_key(db, expiring["id"])
    r = client.get(_consumer_proxy_url(producer_id, "ping"), headers=_bearer(expiring["token"]))
    assert r.status_code == 401, "a key past its expires_at must 401"

    # ── Phase 2: revoked ───────────────────────────────────────────────────
    revocable = _mint_key(
        client, superuser_token_headers, producer_id, me["id"], label="revocable"
    )
    _revoke_key(client, superuser_token_headers, producer_id, revocable["id"])
    r = client.get(_consumer_proxy_url(producer_id, "ping"), headers=_bearer(revocable["token"]))
    assert r.status_code == 401, "a revoked key must 401"

    # ── Phase 3: credential deleted directly cascades the token ───────────
    cascading = _mint_key(
        client, superuser_token_headers, producer_id, me["id"], label="cascading"
    )
    r = client.delete(_credential_url(cascading["credential_id"]), headers=superuser_token_headers)
    assert r.status_code == 200, r.text
    r = client.get(_consumer_proxy_url(producer_id, "ping"), headers=_bearer(cascading["token"]))
    assert r.status_code == 401, "deleting the credential must cascade-revoke the key"


# ── F. Credential classification + env-sync exclusion ─────────────────────────


def test_key_credential_classifies_mine_and_is_excluded_from_env_sync(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    D4: a connection credential classifies as "automatic"; an external key
    classifies as "mine" (same split as ``mcp_provider``). Regardless of
    category, an external key is NEVER written into a consumer env's
    ``credentials.json`` — only the connection is.
    """
    producer = _setup_producer(client, superuser_token_headers, name="Classify Producer")
    producer_id = producer["id"]
    me = client.get(f"{API}/users/me", headers=superuser_token_headers).json()

    connection = _connect(client, superuser_token_headers, producer_id, label="conn-cred")
    key = _mint_key(
        client, superuser_token_headers, producer_id, me["id"], label="key-cred"
    )

    def _category(cred_id: str) -> str:
        r = client.get(f"{API}/credentials/", headers=superuser_token_headers)
        assert r.status_code == 200, r.text
        row = next(c for c in r.json()["data"] if c["id"] == cred_id)
        return row["category"]

    assert _category(connection["credential_id"]) == "automatic"
    assert _category(key["credential_id"]) == "mine"

    # Link the KEY credential (only) to a consumer agent and sync.
    consumer = create_agent_via_api(client, superuser_token_headers, name="Classify Consumer")
    drain_tasks()

    persistent = EnvironmentTestAdapter()
    lm = EnvironmentService._lifecycle_manager
    original = lm.get_adapter
    lm.get_adapter = lambda env: persistent
    try:
        link_credential_to_agent(
            client, superuser_token_headers, consumer["id"], key["credential_id"]
        )
        drain_tasks()
    finally:
        lm.get_adapter = original

    captured = persistent.credentials_set
    assert captured is not None, "credential sync never reached the env adapter"
    agent_api_entries = [c for c in captured["credentials_json"] if c["type"] == "agent_api"]
    assert agent_api_entries == [], (
        f"an external key must never be synced into credentials.json: {agent_api_entries}"
    )


# ── G. Grant/key independence + upsert-not-409 ────────────────────────────────


def test_grant_and_key_lifecycles_independent_and_mint_upserts_scopes(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    D5/D7: scopes live on the grant, never the key.
      1. Minting key1 with scopes upserts the (producer, subject) grant.
      2. Minting key2 for the SAME subject with DIFFERENT scopes upserts the
         SAME row (not a 409) — both keys then share that one scope set.
      3. Revoking key1 does NOT delete the grant; key2 is unaffected.
      4. Deleting the GRANT does NOT kill key2 — it keeps authenticating with
         zero scopes.
    """
    producer = _setup_producer(client, superuser_token_headers, name="Independence Producer")
    producer_id = producer["id"]
    subject = create_random_user(client)

    # ── Phase 1: mint key1 with scopes → upserts a new grant ──────────────
    key1 = _mint_key(
        client, superuser_token_headers, producer_id, subject["id"],
        label="key-1", scopes=["orders.read"],
    )
    grants = _list_grants(client, superuser_token_headers, producer_id)
    assert len(grants) == 1
    assert grants[0]["user_id"] == subject["id"]
    assert grants[0]["scopes"] == ["orders.read"]

    # ── Phase 2: mint key2, SAME subject, DIFFERENT scopes → upsert, no 409 ─
    key2 = _mint_key(
        client, superuser_token_headers, producer_id, subject["id"],
        label="key-2", scopes=["orders.write"],
    )
    grants = _list_grants(client, superuser_token_headers, producer_id)
    assert len(grants) == 1, "minting a second key for the same subject must upsert, not duplicate"
    assert grants[0]["scopes"] == ["orders.write"], "the shared row reflects the latest write"

    tracking = _TrackingAdapter()
    lm = EnvironmentService._lifecycle_manager
    original = lm.get_adapter
    lm.get_adapter = lambda env: tracking
    try:
        # Both keys share the one scope set (D7).
        for key in (key1, key2):
            tracking.forwarded_headers.clear()
            r = client.get(
                _consumer_proxy_url(producer_id, "orders"), headers=_bearer(key["token"])
            )
            assert r.status_code == 200, r.text
            assert tracking.forwarded_headers[0].get("x-cinna-caller-scopes") == "orders.write"

        # ── Phase 3: revoke key1 → grant remains; key2 unaffected ──────────
        _revoke_key(client, superuser_token_headers, producer_id, key1["id"])
        grants = _list_grants(client, superuser_token_headers, producer_id)
        assert len(grants) == 1, "revoking a key must not delete the (producer, subject) grant"

        r = client.get(_consumer_proxy_url(producer_id, "orders"), headers=_bearer(key1["token"]))
        assert r.status_code == 401, "revoked key1 must no longer authenticate"

        tracking.forwarded_headers.clear()
        r = client.get(_consumer_proxy_url(producer_id, "orders"), headers=_bearer(key2["token"]))
        assert r.status_code == 200, "key2 must be unaffected by key1's revocation"
        assert tracking.forwarded_headers[0].get("x-cinna-caller-scopes") == "orders.write"

        # ── Phase 4: delete the GRANT → key2 keeps authenticating, 0 scopes ─
        grant_id = grants[0]["id"]
        r = client.delete(_grant_url(producer_id, grant_id), headers=superuser_token_headers)
        assert r.status_code == 200, r.text

        tracking.forwarded_headers.clear()
        r = client.get(_consumer_proxy_url(producer_id, "orders"), headers=_bearer(key2["token"]))
        assert r.status_code == 200, "deleting the grant must not revoke the key"
        assert "x-cinna-caller-scopes" not in tracking.forwarded_headers[0]
    finally:
        lm.get_adapter = original


# ── H. Beyond the plan: proxy-level kill switch, no last_used_at leak ─────────


def test_disabling_external_access_stops_keys_but_not_connections_no_last_used_bump(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    ``agent_api_external_access_enabled`` is enforced AT THE PROXY, not merely
    at mint — a live kill switch:
      1. Turning it off 401s an already-issued key immediately.
      2. Agent-to-agent CONNECTIONS are unaffected (the switch is key-only).
      3. The rejected call never bumps ``last_used_at`` (a disabled key must
         never look like it "still works"), and ``is_usable`` folds it in.
    """
    producer = _setup_producer(client, superuser_token_headers, name="Kill Switch Producer")
    producer_id = producer["id"]
    me = client.get(f"{API}/users/me", headers=superuser_token_headers).json()

    key = _mint_key(client, superuser_token_headers, producer_id, me["id"], label="kill-switch")
    connection = _connect(client, superuser_token_headers, producer_id, label="kill-switch-conn")

    # Establish a real "last used" timestamp.
    r = client.get(_consumer_proxy_url(producer_id, "ping"), headers=_bearer(key["token"]))
    assert r.status_code == 200, r.text
    before = _list_keys(client, superuser_token_headers, producer_id)[0]
    assert before["last_used_at"] is not None

    # ── Kill switch off ────────────────────────────────────────────────────
    update_agent(
        client, superuser_token_headers, producer_id, agent_api_external_access_enabled=False
    )

    r = client.get(_consumer_proxy_url(producer_id, "ping"), headers=_bearer(key["token"]))
    assert r.status_code == 401, "an external key must stop authenticating once the switch is off"

    r = client.get(
        _consumer_proxy_url(producer_id, "ping"), headers=_bearer(connection["token"])
    )
    assert r.status_code == 200, "agent-to-agent connections must be unaffected by the switch"

    after = _list_keys(client, superuser_token_headers, producer_id)[0]
    assert after["last_used_at"] == before["last_used_at"], (
        "a rejected (disabled) call must never bump last_used_at"
    )
    assert after["is_usable"] is False, "is_usable must fold in the kill switch"


# ── I. Reveal endpoint: the only path back to the value, D4 blast-radius ──────


def test_reveal_endpoint_returns_token_authenticates_and_audits_once_per_call(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    ``POST /credentials/{id}/agent-api-key/reveal`` (plan D4/D9):
      1. Returns the same value ``POST /keys`` minted, and that value actually
         authenticates a proxy call.
      2. Writes exactly one ``AGENT_API_EXTERNAL_KEY_REVEALED`` event per call
         — two calls, two events, not one.
      3. ``GET /with-data`` on the same credential never bumps that count and
         never carries ``token`` (the pairing in D4: strip + move the audit).
    """
    producer = _setup_producer(client, superuser_token_headers, name="Reveal Producer")
    producer_id = producer["id"]
    me = client.get(f"{API}/users/me", headers=superuser_token_headers).json()
    key = _mint_key(client, superuser_token_headers, producer_id, me["id"], label="reveal-me")

    # ── Phase 1: reveal returns the same value, and it authenticates ─────
    revealed = _reveal_key(client, superuser_token_headers, key["credential_id"])
    assert revealed == key["token"]

    r = client.get(_consumer_proxy_url(producer_id, "ping"), headers=_bearer(revealed))
    assert r.status_code == 200, "the revealed value must authenticate a proxy call"

    events = find_security_events(
        client, superuser_token_headers, "AGENT_API_EXTERNAL_KEY_REVEALED"
    )
    assert len(events) == 1, "one reveal call must write exactly one REVEALED event"

    # ── Phase 2: with-data on the same credential — no event, no token ────
    with_data = get_credential_with_data(client, superuser_token_headers, key["credential_id"])
    assert "token" not in with_data["credential_data"], (
        "with-data must strip the token for an external-key credential"
    )
    events = find_security_events(
        client, superuser_token_headers, "AGENT_API_EXTERNAL_KEY_REVEALED"
    )
    assert len(events) == 1, "GET /with-data must never write a REVEALED event"

    # ── Phase 3: a second reveal call writes a second event ───────────────
    revealed_again = _reveal_key(client, superuser_token_headers, key["credential_id"])
    assert revealed_again == key["token"]
    events = find_security_events(
        client, superuser_token_headers, "AGENT_API_EXTERNAL_KEY_REVEALED"
    )
    assert len(events) == 2, "each reveal call must write its own REVEALED event"


def test_with_data_unchanged_for_agent_api_connection_credential(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    D4 no-blast-radius guard: an ``agent_api`` **connection** credential (the
    L1 machine-to-machine token) is untouched by the external-key strip —
    ``with-data`` still returns ``token`` for it, exactly as before this
    feature landed.
    """
    producer = _setup_producer(
        client, superuser_token_headers, name="Connection With-Data Producer"
    )
    producer_id = producer["id"]

    connection = _connect(client, superuser_token_headers, producer_id, label="conn-with-data")

    with_data = get_credential_with_data(
        client, superuser_token_headers, connection["credential_id"]
    )
    assert with_data["credential_data"]["token"] == connection["token"], (
        "with-data must be unchanged for a connection credential"
    )

    events = find_security_events(
        client, superuser_token_headers, "AGENT_API_EXTERNAL_KEY_REVEALED"
    )
    assert events == [], "a connection's with-data read must never write a REVEALED event"


def test_reveal_owner_gated_no_existence_leak(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Reveal is owner-gated exactly like the other key routes: 404 (never
    403) for a non-owner or a ghost credential id, and 401/403 unauthenticated."""
    producer = _setup_producer(client, superuser_token_headers, name="Reveal Gate Producer")
    producer_id = producer["id"]
    me = client.get(f"{API}/users/me", headers=superuser_token_headers).json()
    key = _mint_key(client, superuser_token_headers, producer_id, me["id"], label="gate-reveal")

    # ── Unauthenticated ────────────────────────────────────────────────────
    assert client.post(_reveal_url(key["credential_id"])).status_code in (401, 403)

    # ── Non-owner → 404, no existence leak ─────────────────────────────────
    _, other_headers = create_random_user_with_headers(client)
    r = client.post(_reveal_url(key["credential_id"]), headers=other_headers)
    assert r.status_code == 404, r.text

    # ── Ghost credential id → 404 ──────────────────────────────────────────
    ghost = str(uuid.uuid4())
    r = client.post(_reveal_url(ghost), headers=superuser_token_headers)
    assert r.status_code == 404, r.text


def test_orphaned_key_no_longer_authenticates(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Deleting the subject user kills the key at the proxy.

    `AgentApiToken.subject_user_id` is `ON DELETE CASCADE`, so removing the
    subject drops the token row while the credential — owned by the minter, not
    the subject — survives. This pins the property that actually matters about
    that orphaned state: `validate_token` matches nothing, so the stored value
    is inert.

    That is also what bounds the known gap documented on
    `AgentApiKeyService.is_external_key_credential`: an orphaned credential does
    still hand its stored value back through `with-data` (it is
    indistinguishable from a hand-created `agent_api` credential, whose owner
    must be able to read back what they typed), but what leaks is a string that
    can no longer authenticate. Closing that properly means deleting the
    credential when the token cascades, not widening the predicate.
    """
    producer = _setup_producer(client, superuser_token_headers, name="Orphan Key Producer")

    # Throwaway subject, so deleting them cascades the token without touching
    # the credential.
    subject, _ = create_random_user_with_headers(client)
    key = _mint_key(
        client, superuser_token_headers, producer["id"], subject["id"], label="orphan-me"
    )

    # Works before the cascade, so a 401 afterwards can only be the cascade.
    r = client.get(
        _consumer_proxy_url(producer["id"], "ping"), headers=_bearer(key["token"])
    )
    assert r.status_code == 200, r.text

    r = client.delete(f"{API}/users/{subject['id']}", headers=superuser_token_headers)
    assert r.status_code == 200, r.text

    # Prove the cascade actually fired, so the 401 below cannot pass for an
    # unrelated reason and leave this test silently guarding nothing.
    assert _list_keys(client, superuser_token_headers, producer["id"]) == [], (
        "deleting the subject user must cascade the token row away; if it "
        "survives, this test is not exercising the orphan path at all"
    )

    r = client.get(
        _consumer_proxy_url(producer["id"], "ping"), headers=_bearer(key["token"])
    )
    assert r.status_code == 401, (
        f"an orphaned key must stop authenticating, got {r.status_code}: {r.text}"
    )


def test_reveal_not_readable_by_non_owner_superuser(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Reveal has **no superuser bypass** — a platform admin who is not the owner
    gets 404, exactly like any other non-owner.

    This is deliberate asymmetry, not an oversight: ``revoke_key`` *does* honour
    ``is_superuser``, because revoke is containment (an admin who suspects a key
    is compromised must be able to kill it) while reveal is disclosure (that same
    admin has no need to read the secret). The endpoint reveal replaced —
    ``GET /credentials/{id}/with-data`` — is hard owner-only, so honouring
    superuser here would silently widen who can read another user's secret as a
    by-product of the refactor. This test exists to fail loudly if someone later
    "makes it consistent with revoke".
    """
    # The key is owned by a regular developer, NOT the superuser. The owner
    # needs their own default AI credential — environment creation validates
    # one per owner, and this user is not the superuser the other tests use.
    owner, owner_headers = create_random_user_with_headers(client)
    promote_to_developer(client, superuser_token_headers, owner["id"])
    create_random_ai_credential(
        client, owner_headers, credential_type="anthropic", set_default=True
    )
    producer = _setup_producer(client, owner_headers, name="Admin Reveal Producer")
    key = _mint_key(
        client, owner_headers, producer["id"], owner["id"], label="not-for-admins"
    )
    credential_id = key["credential_id"]

    # ── Superuser is not the owner → 404, no existence leak ────────────────
    r = client.post(_reveal_url(credential_id), headers=superuser_token_headers)
    assert r.status_code == 404, (
        f"a non-owner superuser must not be able to reveal another user's key: {r.text}"
    )

    # ── Control: the actual owner still can, so the 404 above is about
    #    identity and not a broken fixture ───────────────────────────────────
    r = client.post(_reveal_url(credential_id), headers=owner_headers)
    assert r.status_code == 200, r.text
    assert r.json()["token"] == key["token"]

    # ── Exactly one REVEALED event — the owner's. The denied admin attempt
    #    must not be audited as a reveal, since nothing was disclosed.
    #    Queried as the owner: GET /security-events is scoped to the current
    #    user, so the superuser cannot see another user's events. ────────────
    events = find_security_events(
        client, owner_headers, "AGENT_API_EXTERNAL_KEY_REVEALED"
    )
    assert len(events) == 1, f"expected only the owner's reveal to be audited: {events}"


def test_reveal_returns_400_for_credential_that_is_not_an_external_key(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Reveal 400s for a credential that exists and is owned by the caller but is
    not an external key: an ``agent_api`` **connection** credential, and any
    other credential type entirely.
    """
    producer = _setup_producer(client, superuser_token_headers, name="Wrong Type Producer")
    producer_id = producer["id"]

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
    """
    The data-loss path the token strip created (plan §3 D4 note): the generic
    Edit dialog seeds its form from ``with-data``, which no longer carries
    ``token`` for an external key. Round-tripping that payload through
    ``PUT /credentials/{id}`` must NOT wipe the stored token — a subsequent
    reveal must still return the original value.
    """
    producer = _setup_producer(client, superuser_token_headers, name="Edit Preserve Producer")
    producer_id = producer["id"]
    me = client.get(f"{API}/users/me", headers=superuser_token_headers).json()
    key = _mint_key(client, superuser_token_headers, producer_id, me["id"], label="edit-me")

    with_data = get_credential_with_data(client, superuser_token_headers, key["credential_id"])
    assert "token" not in with_data["credential_data"]

    updated = update_credential(
        client,
        superuser_token_headers,
        key["credential_id"],
        name="Renamed External Key",
        credential_data=with_data["credential_data"],
    )
    assert updated["name"] == "Renamed External Key"

    still_revealed = _reveal_key(client, superuser_token_headers, key["credential_id"])
    assert still_revealed == key["token"], (
        "editing the credential via the with-data-seeded payload must not blank the stored token"
    )
