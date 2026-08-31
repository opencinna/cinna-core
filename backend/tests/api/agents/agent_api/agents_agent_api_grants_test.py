"""
Integration tests: Agent-API Caller Identity & Producer Scopes (all 3 phases).

This complements ``agents_agent_api_test.py`` (which covers the L1 connection
token, the connect helper, policy enforcement, and the consumer proxy). Here we
cover the NEW caller-identity (L2) + per-user scopes layer:

  - Per-user GRANT CRUD routes (owner-gated, 404-no-leak, 409-on-dup, scope
    sanitization, scope catalog).
  - The PROXY identity + scopes security model — the heart of the feature:
      * a verified ``X-Cinna-Caller-Identity`` (L2) token → authoritative
        ``X-Cinna-Caller-User-Id/-Email/-Username`` injected on the forwarded
        request; the identity token + Authorization are STRIPPED before
        forwarding.
      * inbound forged ``X-Cinna-Caller-*`` headers are stripped (the identity
        token is the only accepted identity input).
      * missing / invalid / wrong-aud identity → anonymous (no caller headers),
        never an error.
      * scopes injected ONLY when ``agent_api_identity_enabled=True`` AND a live
        grant exists; live resolution (edit grant → next call reflects it).
  - Phase 3 edge enforcement: a ``policy.yaml`` scope with a non-empty
    ``requires:[{method,path}]`` 403s a matching call unless the caller holds the
    scope — but only when identity is enabled; documentation-only scopes never
    edge-deny; segment-accurate path match.
  - A focused identity-service round-trip note (#18): mint→verify round-trips and
    verify() never raises on garbage — covered API-only here via the synthetic
    ``owner_identity_token`` injected into ``credentials.json`` (the same way a
    consumer agent obtains it). A pure unit test for verify() defensive branches
    would live in ``tests/unit/`` (see GAPS in the module docstring of the test
    summary).

API-only: tokens are minted by the connect helper and read back from the created
``agent_api`` credential's decrypted data; the L2 identity token is read back
from the synced ``credentials.json`` captured by the env adapter — never minted
directly (no ``app.core.security`` import). Forwarded headers are captured via a
``_TrackingAdapter`` subclass of ``EnvironmentTestAdapter`` (mirrors the deadline
test in ``agents_agent_api_test.py``).
"""
import uuid

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import settings
from app.models import AgentEnvironment
from app.services.environments.environment_service import EnvironmentService
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.agent import create_agent_via_api, update_agent
from tests.utils.background_tasks import drain_tasks
from tests.utils.credential import (
    get_credential_with_data,
    link_credential_to_agent,
)
from tests.utils.user import create_random_user, create_random_user_with_headers

API = settings.API_V1_STR


# ── URL helpers ───────────────────────────────────────────────────────────────


def _owner_base(agent_id: str) -> str:
    return f"{API}/agents/{agent_id}/agent-api"


def _connect_url(agent_id: str) -> str:
    return f"{_owner_base(agent_id)}/connect"


def _grants_url(agent_id: str) -> str:
    return f"{_owner_base(agent_id)}/grants"


def _grant_url(agent_id: str, grant_id: str) -> str:
    return f"{_owner_base(agent_id)}/grants/{grant_id}"


def _scope_catalog_url(agent_id: str) -> str:
    return f"{_owner_base(agent_id)}/grants/scope-catalog"


def _consumer_proxy_url(agent_id: str, path: str) -> str:
    return f"{API}/agent-api/{agent_id}/{path}"


def _consumer_spec_url(agent_id: str) -> str:
    return f"{API}/agent-api/{agent_id}/openapi.json"


def _credential_url(credential_id: str) -> str:
    return f"{API}/credentials/{credential_id}"


# ── Setup helpers ─────────────────────────────────────────────────────────────


def _setup_api_agent(
    client: TestClient,
    headers: dict[str, str],
    name: str = "API Agent",
    identity_enabled: bool = False,
) -> dict:
    """Create an agent with agent_api_enabled=True (+ optional identity opt-in)."""
    agent = create_agent_via_api(client, headers, name=name)
    drain_tasks()
    update_agent(
        client,
        headers,
        agent["id"],
        agent_api_enabled=True,
        agent_api_identity_enabled=identity_enabled,
    )
    return agent


def _mint_token(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    label: str | None = None,
    consumer_agent_id: str | None = None,
) -> dict:
    """Connect to a producer agent's REST API (mints L1 token + agent_api cred).

    Returns the connection info plus the raw token value (read back from the
    created credential's decrypted data — exactly how a consumer obtains it).
    """
    body: dict = {}
    if label is not None:
        body["credential_label"] = label
    if consumer_agent_id is not None:
        body["consumer_agent_id"] = consumer_agent_id
    r = client.post(_connect_url(agent_id), headers=headers, json=body)
    assert r.status_code == 200, f"Connect failed: {r.text}"
    conn = r.json()
    cred = get_credential_with_data(client, headers, conn["credential_id"])
    return {
        "id": conn["token_id"],
        "credential_id": conn["credential_id"],
        "token": cred["credential_data"]["token"],
        "base_url": conn["base_url"],
        "spec_url": conn["spec_url"],
    }


def _bearer(token_value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token_value}"}


def _create_grant(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    user_id: str,
    scopes: list[str],
) -> dict:
    """Owner creates a per-user grant. Returns the public grant dict."""
    r = client.post(
        _grants_url(agent_id),
        headers=headers,
        json={"user_id": user_id, "scopes": scopes},
    )
    assert r.status_code == 200, f"Create grant failed: {r.text}"
    return r.json()


def _list_grants(
    client: TestClient, headers: dict[str, str], agent_id: str
) -> list[dict]:
    r = client.get(_grants_url(agent_id), headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["data"]


def _set_policy_cache(db: Session, agent_id: str, policy: dict) -> None:
    """Force the producer env's parsed ``agent_api_policy_cache`` on the test DB.

    The policy cache is the env's parsed ``policy.yaml`` — populated by the real
    environment sync, which the suite stubs out. There is no API seam to set it,
    so policy / scope-catalog / edge-enforcement tests inject it here (the same
    documented DB-seam helper used by the policy tests in
    ``agents_agent_api_test.py``). For scopes we write the *canonical parsed*
    shape (a list of ``{name, description, requires}``) — that is exactly what
    ``get_effective_policy`` returns and what both the catalog reader and edge
    enforcement consume.
    """
    env = db.exec(
        select(AgentEnvironment).where(
            AgentEnvironment.agent_id == uuid.UUID(agent_id)
        )
    ).first()
    assert env is not None, f"No environment for agent {agent_id}"
    env.agent_api_policy_cache = policy
    db.add(env)
    db.commit()


def _read_owner_identity_token(
    client: TestClient,
    headers: dict[str, str],
    producer_id: str,
    consumer_id: str,
    credential_id: str,
) -> str:
    """Obtain a VALID L2 ``owner_identity_token`` the API-only way.

    The synthetic ``owner_identity_token`` entry is injected into the consumer
    env's ``credentials.json`` whenever the env has ≥1 linked ``agent_api``
    credential. We link the producer's agent_api credential to the consumer
    agent, trigger the credential sync, and read the minted JWT from the entry
    the env adapter captured via ``set_credentials`` — exactly the value a
    consumer agent would send on ``X-Cinna-Caller-Identity``.
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
    identity = next(
        (c for c in creds if c["type"] == "owner_identity_token"), None
    )
    assert identity is not None, (
        f"owner_identity_token not injected into credentials.json: "
        f"{[c['type'] for c in creds]}"
    )
    token = identity["credential_data"]["token"]
    assert token, "owner_identity_token entry has no token"
    return token


class _TrackingAdapter(EnvironmentTestAdapter):
    """Adapter that records the headers each forwarded proxy call carried."""

    def __init__(self) -> None:
        super().__init__()
        self.forwarded_headers: list[dict] = []

    async def proxy_agent_api(
        self,
        method,
        path,
        headers=None,
        body=None,
        stream=False,
        timeout=60.0,
        query_string=None,
    ):
        self.forwarded_headers.append({k.lower(): v for k, v in (headers or {}).items()})
        return await super().proxy_agent_api(
            method, path, headers, body, stream, timeout, query_string
        )


# ── A. Grant CRUD (routes) ────────────────────────────────────────────────────


def test_grant_crud_lifecycle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Per-user grant CRUD lifecycle (owner-gated):
      1. Empty before any create.
      2. Create a grant (user + scopes) → appears in the list.
      3. Update the grant's scopes → reflected in the list.
      4. Duplicate (producer, user) create → 409.
      5. Delete the grant → gone from the list.
      6. Updating / deleting a ghost grant_id → 404.
    """
    producer = _setup_api_agent(client, superuser_token_headers, name="Grant CRUD Producer")
    agent_id = producer["id"]
    grantee = create_random_user(client)

    # ── Phase 1: empty ────────────────────────────────────────────────────
    assert _list_grants(client, superuser_token_headers, agent_id) == []

    # ── Phase 2: create ───────────────────────────────────────────────────
    grant = _create_grant(
        client, superuser_token_headers, agent_id, grantee["id"],
        scopes=["orders.read", "orders.write"],
    )
    grant_id = grant["id"]
    assert grant["user_id"] == grantee["id"]
    assert grant["scopes"] == ["orders.read", "orders.write"]
    assert grant["user"]["email"] == grantee["email"]

    grants = _list_grants(client, superuser_token_headers, agent_id)
    assert len(grants) == 1
    assert grants[0]["id"] == grant_id

    # ── Phase 3: update scopes → reflected in list ────────────────────────
    r = client.put(
        _grant_url(agent_id, grant_id),
        headers=superuser_token_headers,
        json={"scopes": ["orders.read"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["scopes"] == ["orders.read"]

    grants = _list_grants(client, superuser_token_headers, agent_id)
    assert grants[0]["scopes"] == ["orders.read"]

    # ── Phase 4: duplicate (producer, user) → 409 ─────────────────────────
    r = client.post(
        _grants_url(agent_id),
        headers=superuser_token_headers,
        json={"user_id": grantee["id"], "scopes": ["x"]},
    )
    assert r.status_code == 409, f"Duplicate grant must be 409: {r.text}"

    # ── Phase 5: delete → gone ────────────────────────────────────────────
    r = client.delete(_grant_url(agent_id, grant_id), headers=superuser_token_headers)
    assert r.status_code == 200, r.text
    assert _list_grants(client, superuser_token_headers, agent_id) == []

    # ── Phase 6: ghost grant_id → 404 on update + delete ──────────────────
    ghost = str(uuid.uuid4())
    assert client.put(
        _grant_url(agent_id, ghost),
        headers=superuser_token_headers,
        json={"scopes": []},
    ).status_code == 404
    assert client.delete(
        _grant_url(agent_id, ghost), headers=superuser_token_headers
    ).status_code == 404


def test_grant_create_to_phantom_user_404(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Creating a grant for a non-existent user → 404 (grant would never resolve)."""
    producer = _setup_api_agent(client, superuser_token_headers, name="Phantom Grantee Producer")
    r = client.post(
        _grants_url(producer["id"]),
        headers=superuser_token_headers,
        json={"user_id": str(uuid.uuid4()), "scopes": ["a"]},
    )
    assert r.status_code == 404, r.text


def test_grant_routes_owner_gated_no_existence_leak(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Every grant route is owner-gated and returns 404 (no existence leak) for a
    non-owner — including a cross-producer path-swap of a real grant_id.
      1. Non-owner: list / create / update / delete / scope-catalog → 404.
      2. Path-swap: a grant that belongs to producer A, addressed under
         producer B (also owned by the same owner) → 404 (grant↔producer bond).
    """
    producer = _setup_api_agent(client, superuser_token_headers, name="Owner Gate Producer")
    agent_id = producer["id"]
    grantee = create_random_user(client)
    grant = _create_grant(
        client, superuser_token_headers, agent_id, grantee["id"], scopes=["a"]
    )
    grant_id = grant["id"]

    # ── Phase 1: non-owner hits every route → 404 ─────────────────────────
    _, other_headers = create_random_user_with_headers(client)
    assert client.get(_grants_url(agent_id), headers=other_headers).status_code == 404
    assert client.get(_scope_catalog_url(agent_id), headers=other_headers).status_code == 404
    assert client.post(
        _grants_url(agent_id), headers=other_headers,
        json={"user_id": grantee["id"], "scopes": []},
    ).status_code == 404
    assert client.put(
        _grant_url(agent_id, grant_id), headers=other_headers, json={"scopes": []}
    ).status_code == 404
    assert client.delete(
        _grant_url(agent_id, grant_id), headers=other_headers
    ).status_code == 404

    # ── Phase 1b: unauthenticated → 401/403 ───────────────────────────────
    assert client.get(_grants_url(agent_id)).status_code in (401, 403)

    # ── Phase 2: path-swap a real grant under a DIFFERENT producer → 404 ──
    other_producer = _setup_api_agent(
        client, superuser_token_headers, name="Owner Gate Producer 2"
    )
    # The grant belongs to `agent_id`, not `other_producer["id"]`. Addressing it
    # under the wrong producer must 404 (the grant↔producer bond is enforced).
    r = client.put(
        _grant_url(other_producer["id"], grant_id),
        headers=superuser_token_headers,
        json={"scopes": ["x"]},
    )
    assert r.status_code == 404, f"Cross-producer grant access must 404: {r.text}"
    r = client.delete(
        _grant_url(other_producer["id"], grant_id),
        headers=superuser_token_headers,
    )
    assert r.status_code == 404, f"Cross-producer grant delete must 404: {r.text}"


def test_grant_scope_sanitization(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Scopes are normalized at the write boundary (so they survive the
    space-separated ``X-Cinna-Caller-Scopes`` header transport):
      - leading/trailing whitespace is trimmed,
      - names with inner whitespace are dropped (would be split by the header),
      - empties are dropped,
      - duplicates are removed (order preserved).
    Applies on both create and update.
    """
    producer = _setup_api_agent(client, superuser_token_headers, name="Sanitize Producer")
    agent_id = producer["id"]
    grantee = create_random_user(client)

    messy = [
        "  orders.read  ",   # trimmed → orders.read
        "orders.read",       # duplicate of the trimmed one
        "bad scope name",    # inner whitespace → dropped
        "",                  # empty → dropped
        "   ",               # whitespace-only → dropped
        "orders.write",      # kept
    ]
    grant = _create_grant(client, superuser_token_headers, agent_id, grantee["id"], messy)
    assert grant["scopes"] == ["orders.read", "orders.write"], grant["scopes"]

    # Update applies the same sanitization.
    r = client.put(
        _grant_url(agent_id, grant["id"]),
        headers=superuser_token_headers,
        json={"scopes": ["  audit  ", "audit", "x y", "audit"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["scopes"] == ["audit"], r.json()["scopes"]


def test_scope_catalog_surfaces_declared_scopes(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    GET /grants/scope-catalog surfaces the producer's declared scopes (name +
    description) from the cached, parsed policy. We seed the canonical parsed
    catalog covering all three authoring forms after normalization:
      - bare name (no description, no requires),
      - name → description,
      - rich name → {description, requires:[...]}.
    The catalog must surface name + description for each; the platform-internal
    ``requires`` patterns are NOT leaked to the picker.
      Owner-gated: a non-owner → 404. Empty/absent catalog → empty list.
    """
    producer = _setup_api_agent(client, superuser_token_headers, name="Catalog Producer")
    agent_id = producer["id"]

    # ── Phase 1: no scopes declared yet → empty catalog (graceful) ────────
    r = client.get(_scope_catalog_url(agent_id), headers=superuser_token_headers)
    assert r.status_code == 200, r.text
    assert r.json()["scopes"] == []

    # ── Phase 2: seed canonical parsed scopes (three forms, normalized) ───
    _set_policy_cache(db, agent_id, {
        "read_only": True,
        "auth": "required",
        "max_body_bytes": 10 * 1024 * 1024,
        "rate_limit": "60/min",
        "expose_spec": True,
        "allowed_paths": ["*"],
        "scopes": [
            # bare-list form → documentation-only, no description/requires
            {"name": "orders.read", "description": None, "requires": []},
            # name→description form
            {"name": "orders.list", "description": "List orders", "requires": []},
            # rich form with edge-enforcement requires (must NOT leak to picker)
            {
                "name": "orders.write",
                "description": "Create/modify orders",
                "requires": [{"method": "POST", "path": "/orders"}],
            },
        ],
    })

    r = client.get(_scope_catalog_url(agent_id), headers=superuser_token_headers)
    assert r.status_code == 200, r.text
    catalog = r.json()["scopes"]
    by_name = {s["name"]: s for s in catalog}
    assert set(by_name) == {"orders.read", "orders.list", "orders.write"}
    assert by_name["orders.list"]["description"] == "List orders"
    assert by_name["orders.write"]["description"] == "Create/modify orders"
    assert by_name["orders.read"]["description"] is None
    # The picker model exposes only name + description — no requires leak.
    for entry in catalog:
        assert "requires" not in entry, f"requires must not leak to the catalog: {entry}"

    # ── Phase 3: non-owner → 404 ──────────────────────────────────────────
    _, other_headers = create_random_user_with_headers(client)
    r = client.get(_scope_catalog_url(agent_id), headers=other_headers)
    assert r.status_code == 404


# ── B. Proxy identity + scopes (the security model) ───────────────────────────


def test_proxy_injects_authoritative_caller_headers_and_strips_secrets(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A valid L2 identity token → authoritative attribution headers; the identity
    token + the L1 bearer are STRIPPED before forwarding (SECURITY rules 1 + 2).
      1. Producer + consumer connected; obtain the consumer's owner identity
         token from its synced credentials.json (API-only).
      2. Consumer call with Authorization (L1) + X-Cinna-Caller-Identity (L2).
      3. Forwarded request carries X-Cinna-Caller-User-Id (= owner) + -Email.
      4. Authorization and X-Cinna-Caller-Identity are absent downstream.
    """
    producer = _setup_api_agent(client, superuser_token_headers, name="Identity Producer")
    producer_id = producer["id"]
    consumer = create_agent_via_api(client, superuser_token_headers, name="Identity Consumer")
    drain_tasks()
    consumer_id = consumer["id"]

    token = _mint_token(
        client, superuser_token_headers, producer_id,
        label="identity", consumer_agent_id=consumer_id,
    )
    identity_token = _read_owner_identity_token(
        client, superuser_token_headers, producer_id, consumer_id, token["credential_id"]
    )

    # Resolve the superuser's id (the owner of both agents) for the assertion.
    me = client.get(f"{API}/users/me", headers=superuser_token_headers).json()
    owner_id = me["id"]

    tracking = _TrackingAdapter()
    lm = EnvironmentService._lifecycle_manager
    original = lm.get_adapter
    lm.get_adapter = lambda env: tracking
    try:
        r = client.get(
            _consumer_proxy_url(producer_id, "orders"),
            headers={
                **_bearer(token["token"]),
                "X-Cinna-Caller-Identity": identity_token,
            },
        )
        assert r.status_code == 200, r.text
        assert len(tracking.forwarded_headers) == 1
        fwd = tracking.forwarded_headers[0]

        # Authoritative attribution set from the verified identity token.
        assert fwd.get("x-cinna-caller-user-id") == owner_id
        assert fwd.get("x-cinna-caller-email") == me["email"]

        # Secrets stripped before forwarding (rule 1).
        assert "authorization" not in fwd
        assert "x-cinna-caller-identity" not in fwd
    finally:
        lm.get_adapter = original


def test_proxy_strips_inbound_forged_caller_headers(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    SECURITY rule 2: the identity token is the ONLY accepted identity input.
    Inbound, client-supplied X-Cinna-Caller-User-Id / -Scopes are forged and must
    be discarded; the proxy re-sets attribution authoritatively from the verified
    token (the forged user-id never reaches the producer).
    """
    producer = _setup_api_agent(client, superuser_token_headers, name="Forge Producer")
    producer_id = producer["id"]
    consumer = create_agent_via_api(client, superuser_token_headers, name="Forge Consumer")
    drain_tasks()
    consumer_id = consumer["id"]

    token = _mint_token(
        client, superuser_token_headers, producer_id,
        label="forge", consumer_agent_id=consumer_id,
    )
    identity_token = _read_owner_identity_token(
        client, superuser_token_headers, producer_id, consumer_id, token["credential_id"]
    )
    me = client.get(f"{API}/users/me", headers=superuser_token_headers).json()
    owner_id = me["id"]
    forged_id = str(uuid.uuid4())

    tracking = _TrackingAdapter()
    lm = EnvironmentService._lifecycle_manager
    original = lm.get_adapter
    lm.get_adapter = lambda env: tracking
    try:
        r = client.get(
            _consumer_proxy_url(producer_id, "orders"),
            headers={
                **_bearer(token["token"]),
                "X-Cinna-Caller-Identity": identity_token,
                # Forged attempts — must be stripped, not trusted.
                "X-Cinna-Caller-User-Id": forged_id,
                "X-Cinna-Caller-Email": "attacker@evil.test",
                "X-Cinna-Caller-Scopes": "admin superuser",
            },
        )
        assert r.status_code == 200, r.text
        fwd = tracking.forwarded_headers[0]
        # Authoritative value wins; the forged id never reaches the producer.
        assert fwd.get("x-cinna-caller-user-id") == owner_id
        assert fwd.get("x-cinna-caller-user-id") != forged_id
        assert fwd.get("x-cinna-caller-email") == me["email"]
        # No grant + identity not enabled → no scopes header (forged one dropped).
        assert "x-cinna-caller-scopes" not in fwd
    finally:
        lm.get_adapter = original


def test_proxy_missing_identity_is_anonymous(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    SECURITY rule 4: a call with NO identity header is anonymous — no
    X-Cinna-Caller-* injected — and still succeeds (200, never an error).
    """
    producer = _setup_api_agent(client, superuser_token_headers, name="Anon Producer")
    producer_id = producer["id"]
    token = _mint_token(client, superuser_token_headers, producer_id, label="anon")

    tracking = _TrackingAdapter()
    lm = EnvironmentService._lifecycle_manager
    original = lm.get_adapter
    lm.get_adapter = lambda env: tracking
    try:
        r = client.get(
            _consumer_proxy_url(producer_id, "orders"),
            headers=_bearer(token["token"]),
        )
        assert r.status_code == 200, r.text
        fwd = tracking.forwarded_headers[0]
        assert not any(k.startswith("x-cinna-caller-") for k in fwd), (
            f"anonymous call must inject no caller headers: {fwd}"
        )
    finally:
        lm.get_adapter = original


def test_proxy_invalid_identity_token_is_anonymous(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    SECURITY rule 4 + identity-service robustness: garbage / wrong-aud / wrong
    identity tokens degrade to anonymous (no caller headers), never an error.
    A non-JWT garbage string and a *valid* L1 bearer (wrong aud / type) both
    fail verification → anonymous.
    """
    producer = _setup_api_agent(client, superuser_token_headers, name="BadId Producer")
    producer_id = producer["id"]
    token = _mint_token(client, superuser_token_headers, producer_id, label="badid")

    tracking = _TrackingAdapter()
    lm = EnvironmentService._lifecycle_manager
    original = lm.get_adapter
    lm.get_adapter = lambda env: tracking
    try:
        for bogus_identity in (
            "this-is-not-a-jwt",
            "a.b.c",  # malformed JWT shape
            token["token"],  # a real L1 token — wrong aud/type for identity check
        ):
            tracking.forwarded_headers.clear()
            r = client.get(
                _consumer_proxy_url(producer_id, "orders"),
                headers={
                    **_bearer(token["token"]),
                    "X-Cinna-Caller-Identity": bogus_identity,
                },
            )
            assert r.status_code == 200, f"{bogus_identity!r} → {r.text}"
            fwd = tracking.forwarded_headers[0]
            assert not any(k.startswith("x-cinna-caller-") for k in fwd), (
                f"invalid identity {bogus_identity!r} must be anonymous: {fwd}"
            )
            # And the bogus identity header itself is stripped.
            assert "x-cinna-caller-identity" not in fwd
    finally:
        lm.get_adapter = original


def test_proxy_scopes_injected_only_when_enabled_and_granted(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    X-Cinna-Caller-Scopes is injected ONLY when BOTH hold:
      (a) the producer opted in (agent_api_identity_enabled=True), AND
      (b) a live grant exists for the resolved owner.
    Matrix walked on one producer/consumer pair:
      1. identity OFF + grant exists → no scopes header.
      2. identity ON + no grant      → no scopes header (but still attributed).
      3. identity ON + grant exists  → scopes header = the grant's scopes.
      4. live edit: change the grant → next call reflects new scopes (no re-mint).
    """
    producer = _setup_api_agent(
        client, superuser_token_headers, name="Scopes Producer", identity_enabled=False
    )
    producer_id = producer["id"]
    consumer = create_agent_via_api(client, superuser_token_headers, name="Scopes Consumer")
    drain_tasks()
    consumer_id = consumer["id"]

    token = _mint_token(
        client, superuser_token_headers, producer_id,
        label="scopes", consumer_agent_id=consumer_id,
    )
    identity_token = _read_owner_identity_token(
        client, superuser_token_headers, producer_id, consumer_id, token["credential_id"]
    )
    me = client.get(f"{API}/users/me", headers=superuser_token_headers).json()

    # A grant for the resolved owner (the superuser owns the calling agent).
    grant = _create_grant(
        client, superuser_token_headers, producer_id, me["id"],
        scopes=["orders.read", "orders.write"],
    )

    tracking = _TrackingAdapter()
    lm = EnvironmentService._lifecycle_manager
    original = lm.get_adapter
    lm.get_adapter = lambda env: tracking

    def _call() -> dict:
        tracking.forwarded_headers.clear()
        r = client.get(
            _consumer_proxy_url(producer_id, "orders"),
            headers={
                **_bearer(token["token"]),
                "X-Cinna-Caller-Identity": identity_token,
            },
        )
        assert r.status_code == 200, r.text
        return tracking.forwarded_headers[0]

    try:
        # ── Phase 1: identity OFF + grant exists → no scopes header ────────
        fwd = _call()
        assert "x-cinna-caller-scopes" not in fwd, (
            f"scopes must not leak when identity is disabled: {fwd}"
        )
        # Attribution is still present (Phase 1 behavior, flag-independent).
        assert fwd.get("x-cinna-caller-user-id") == me["id"]

        # ── Phase 2: identity ON + delete grant → no scopes header ────────
        update_agent(
            client, superuser_token_headers, producer_id,
            agent_api_identity_enabled=True,
        )
        client.delete(
            _grant_url(producer_id, grant["id"]), headers=superuser_token_headers
        )
        fwd = _call()
        assert "x-cinna-caller-scopes" not in fwd, (
            f"no grant ⇒ no scopes even when identity is enabled: {fwd}"
        )
        assert fwd.get("x-cinna-caller-user-id") == me["id"]

        # ── Phase 3: identity ON + grant exists → scopes header set ───────
        _create_grant(
            client, superuser_token_headers, producer_id, me["id"],
            scopes=["orders.read", "orders.write"],
        )
        fwd = _call()
        assert set(fwd.get("x-cinna-caller-scopes", "").split()) == {
            "orders.read", "orders.write"
        }, fwd

        # ── Phase 4: live edit → next call reflects new scopes (no re-mint) ─
        grants = _list_grants(client, superuser_token_headers, producer_id)
        gid = grants[0]["id"]
        client.put(
            _grant_url(producer_id, gid),
            headers=superuser_token_headers,
            json={"scopes": ["orders.read"]},
        )
        fwd = _call()  # same identity_token, not re-minted
        assert fwd.get("x-cinna-caller-scopes") == "orders.read", fwd
    finally:
        lm.get_adapter = original


# ── C. Phase 3 edge scope enforcement ─────────────────────────────────────────


def test_edge_enforcement_gates_scope_required_endpoint(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    Phase 3 (D8): a scope declared with a non-empty requires:[{method,path}] in
    policy.yaml edge-gates the matching method/path. With identity ENABLED:
      1. Caller WITHOUT the gating scope POSTing /orders → 403.
      2. Caller WITH the scope POSTing /orders → forwarded (200).
    """
    producer = _setup_api_agent(
        client, superuser_token_headers, name="Edge Gate Producer", identity_enabled=True
    )
    producer_id = producer["id"]
    consumer = create_agent_via_api(client, superuser_token_headers, name="Edge Gate Consumer")
    drain_tasks()
    consumer_id = consumer["id"]

    token = _mint_token(
        client, superuser_token_headers, producer_id,
        label="edge", consumer_agent_id=consumer_id,
    )
    identity_token = _read_owner_identity_token(
        client, superuser_token_headers, producer_id, consumer_id, token["credential_id"]
    )
    me = client.get(f"{API}/users/me", headers=superuser_token_headers).json()

    # Policy: allow POST (read_only=False) + a scope gating POST /orders.
    _set_policy_cache(db, producer_id, {
        "read_only": False,
        "auth": "required",
        "max_body_bytes": 10 * 1024 * 1024,
        "rate_limit": "600/min",
        "expose_spec": True,
        "allowed_paths": ["*"],
        "scopes": [
            {
                "name": "orders.write",
                "description": "Create/modify orders",
                "requires": [{"method": "POST", "path": "/orders"}],
            },
        ],
    })

    headers = {**_bearer(token["token"]), "X-Cinna-Caller-Identity": identity_token}

    # ── Phase 1: caller WITHOUT the scope → 403 ──────────────────────────
    r = client.post(
        _consumer_proxy_url(producer_id, "orders"), headers=headers, json={"x": 1}
    )
    assert r.status_code == 403, f"missing scope must edge-deny POST /orders: {r.text}"

    # ── Phase 2: grant the scope → POST passes through (200) ─────────────
    _create_grant(
        client, superuser_token_headers, producer_id, me["id"], scopes=["orders.write"]
    )
    tracking = _TrackingAdapter()
    lm = EnvironmentService._lifecycle_manager
    original = lm.get_adapter
    lm.get_adapter = lambda env: tracking
    try:
        r = client.post(
            _consumer_proxy_url(producer_id, "orders"), headers=headers, json={"x": 1}
        )
        assert r.status_code == 200, f"holding the scope must pass: {r.text}"
        assert len(tracking.forwarded_headers) == 1
    finally:
        lm.get_adapter = original


def test_edge_enforcement_off_when_identity_disabled(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    Backward-compat (SECURITY rule 4): the SAME scope-gated endpoint is NOT
    edge-denied when the producer has not opted into identity
    (agent_api_identity_enabled=False), even with no grant — it passes through to
    the producer (which remains the sole enforcer).
    """
    producer = _setup_api_agent(
        client, superuser_token_headers, name="Edge Off Producer", identity_enabled=False
    )
    producer_id = producer["id"]
    token = _mint_token(client, superuser_token_headers, producer_id, label="edge-off")

    _set_policy_cache(db, producer_id, {
        "read_only": False,
        "auth": "required",
        "max_body_bytes": 10 * 1024 * 1024,
        "rate_limit": "600/min",
        "expose_spec": True,
        "allowed_paths": ["*"],
        "scopes": [
            {
                "name": "orders.write",
                "description": "Create/modify orders",
                "requires": [{"method": "POST", "path": "/orders"}],
            },
        ],
    })

    # No identity header, identity disabled → never edge-denied.
    r = client.post(
        _consumer_proxy_url(producer_id, "orders"),
        headers=_bearer(token["token"]),
        json={"x": 1},
    )
    assert r.status_code == 200, (
        f"identity-disabled producer must not edge-deny (backward compat): {r.text}"
    )


def test_edge_enforcement_skips_documentation_only_scopes(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    A documentation-only scope (declared with NO requires) is never edge-enforced
    — even with identity enabled and no grant, the endpoint passes through (D6:
    the producer is the sole enforcer for documentation-only scopes).
    """
    producer = _setup_api_agent(
        client, superuser_token_headers, name="DocScope Producer", identity_enabled=True
    )
    producer_id = producer["id"]
    token = _mint_token(client, superuser_token_headers, producer_id, label="doc-scope")

    _set_policy_cache(db, producer_id, {
        "read_only": False,
        "auth": "required",
        "max_body_bytes": 10 * 1024 * 1024,
        "rate_limit": "600/min",
        "expose_spec": True,
        "allowed_paths": ["*"],
        "scopes": [
            # No requires → documentation-only, never edge-enforced.
            {"name": "orders.write", "description": "Create/modify", "requires": []},
        ],
    })

    r = client.post(
        _consumer_proxy_url(producer_id, "orders"),
        headers=_bearer(token["token"]),
        json={"x": 1},
    )
    assert r.status_code == 200, (
        f"documentation-only scope must never edge-deny: {r.text}"
    )


def test_edge_enforcement_path_match_is_segment_accurate(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
) -> None:
    """
    Segment-accurate path match: a scope gating ``/orders`` gates ``/orders`` and
    ``/orders/123`` but NOT ``/orders-archive`` (a naive startswith would
    over-gate the sibling). Identity enabled, caller has NO grant:
      1. POST /orders          → 403 (gated, no scope).
      2. POST /orders/123      → 403 (gated child segment).
      3. POST /orders-archive  → 200 (NOT gated — different segment).
    """
    producer = _setup_api_agent(
        client, superuser_token_headers, name="Segment Producer", identity_enabled=True
    )
    producer_id = producer["id"]
    token = _mint_token(client, superuser_token_headers, producer_id, label="segment")

    _set_policy_cache(db, producer_id, {
        "read_only": False,
        "auth": "required",
        "max_body_bytes": 10 * 1024 * 1024,
        "rate_limit": "600/min",
        "expose_spec": True,
        "allowed_paths": ["*"],
        "scopes": [
            {
                "name": "orders.write",
                "description": "Orders",
                "requires": [{"method": "POST", "path": "/orders"}],
            },
        ],
    })

    headers = _bearer(token["token"])

    # ── Phase 1: exact match gated → 403 ──────────────────────────────────
    r = client.post(_consumer_proxy_url(producer_id, "orders"), headers=headers, json={})
    assert r.status_code == 403, r.text

    # ── Phase 2: child segment gated → 403 ────────────────────────────────
    r = client.post(_consumer_proxy_url(producer_id, "orders/123"), headers=headers, json={})
    assert r.status_code == 403, r.text

    # ── Phase 3: sibling prefix NOT gated → 200 ───────────────────────────
    r = client.post(
        _consumer_proxy_url(producer_id, "orders-archive"), headers=headers, json={}
    )
    assert r.status_code == 200, (
        f"/orders-archive must NOT be gated by an /orders scope: {r.text}"
    )


# ── D. Identity token round-trip (mint via API → verify at proxy) ─────────────


def test_identity_token_minted_via_api_round_trips_to_owner(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    #18 (API-observable round-trip): the synthetic owner_identity_token minted
    host-side during credential prep verifies at the proxy and resolves back to
    the install owner.
      1. The producer+consumer owner's identity token resolves to that owner.
      2. The minted token's authoritative attribution is the resolved owner, not
         any inbound value (cross-check that a forged inbound id is ignored — the
         token is the only identity source, so a different inbound id never wins).

    Pure unit coverage of verify()'s non-raising defensive branches (no-aud /
    wrong-type / expired / None) belongs in tests/unit/ (would import
    AgentApiIdentityService directly — disallowed in tests/api/). The
    invalid-token-is-anonymous behavior is already covered API-only by
    ``test_proxy_invalid_identity_token_is_anonymous`` above; a second owner is
    intentionally NOT built here because a freshly-signed-up user has no AI
    credential, so its env never reaches "running" and the credentials.json
    sync (the only API path to a valid identity token) never fires.
    """
    producer = _setup_api_agent(client, superuser_token_headers, name="RT Producer")
    consumer = create_agent_via_api(client, superuser_token_headers, name="RT Consumer")
    drain_tasks()
    token = _mint_token(
        client, superuser_token_headers, producer["id"],
        label="rt", consumer_agent_id=consumer["id"],
    )
    identity = _read_owner_identity_token(
        client, superuser_token_headers, producer["id"], consumer["id"],
        token["credential_id"],
    )
    me = client.get(f"{API}/users/me", headers=superuser_token_headers).json()
    decoy_id = str(uuid.uuid4())

    tracking = _TrackingAdapter()
    lm = EnvironmentService._lifecycle_manager
    original = lm.get_adapter
    lm.get_adapter = lambda env: tracking
    try:
        r = client.get(
            _consumer_proxy_url(producer["id"], "orders"),
            headers={
                **_bearer(token["token"]),
                "X-Cinna-Caller-Identity": identity,
                # A decoy inbound id — the verified token must win, not this.
                "X-Cinna-Caller-User-Id": decoy_id,
            },
        )
        assert r.status_code == 200, r.text
        resolved = tracking.forwarded_headers[0].get("x-cinna-caller-user-id")
        # Round-trip: the minted sub == the owner; resolves at the proxy.
        assert resolved == me["id"]
        # The token is the only identity source — the decoy never wins.
        assert resolved != decoy_id, "verified token must override inbound id"
    finally:
        lm.get_adapter = original
