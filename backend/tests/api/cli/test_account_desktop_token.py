"""
Backend tests for POST /api/v1/cli/account/desktop-token (handover §8.2 / D12).

The endpoint Cinna Desktop calls when it finds a CLI ``account.json`` in a
workshop tree: it trades the account CLI token for a desktop access + refresh
pair plus the account email, so the profile links without a browser consent.

Covers:
- Full exchange lifecycle: lazy client registration, the issued access token
  working as an ordinary user session, the client surfacing in
  ``GET /desktop-auth/clients`` with ``origin="cli_exchange"``, the CLI token
  surviving the exchange unspent, rotation through the normal desktop refresh
  endpoint, and revocation killing both the access and the refresh token.
- Binding to a supplied client id, including the laundering case: a client
  registered through a browser consent that is later handed to this endpoint
  must stop reading as ``browser_consent``.
- Auth matrix: per-agent CLI token, plain user JWT, no auth and a revoked
  account token are all rejected; another user's client id is 403.
- Audit: ``CLI_ACCOUNT_DESKTOP_TOKEN_ISSUED`` on success,
  ``CLI_ACCOUNT_DESKTOP_TOKEN_DENIED`` on an authorization failure.
- The minting gate on the exchanged session: which surfaces it closes, and the
  action granularity that keeps ``action="deny"`` on the two consent routes
  reachable — deny mints nothing, so a gate against minting must not touch it.
- The blast radius of the provenance-change revoke: one client row, never the
  owning user, so linking a device does not sign the user's others out.

Note on token expiry: the account-token expiry check lives in the shared
``_resolve_account_cli_context`` dep and has no API surface that can age a token,
so the revoked-token case stands in for it here — the same dep rejects both, and
``test_account_cli.py`` covers the revoked path for every other account route.
"""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.cli import (
    account_cli_headers,
    bootstrap_account_token,
    cli_auth_headers,
    exchange_for_desktop_token,
    mint_child_token,
    revoke_account_token,
)
from tests.utils.desktop_auth import (
    exchange_code_for_tokens,
    generate_pkce_pair,
    get_authorization_code,
    initiate_authorize,
    list_desktop_clients,
    obtain_desktop_tokens,
    refresh_access_token,
    revoke_desktop_client,
)
from tests.utils.user import (
    create_random_user_with_headers,
    promote_to_developer,
)

_BASE = f"{settings.API_V1_STR}/cli"
_DESKTOP = f"{settings.API_V1_STR}/desktop-auth"
_SEC = f"{settings.API_V1_STR}/security-events"


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _me(client: TestClient, headers: dict[str, str]):
    return client.get(f"{settings.API_V1_STR}/users/me", headers=headers)


# ── Scenario 1: full exchange lifecycle ──────────────────────────────────────


def test_desktop_token_exchange_lifecycle(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Exchange lifecycle end to end:
      1. Bootstrap an account CLI token
      2. Exchange it with no client_id → lazy registration, tokens + email
      3. The issued access token authenticates an ordinary user route
      4. The client appears in GET /desktop-auth/clients with origin=cli_exchange
      5. The CLI token is used, not spent — it still works afterwards
      6. The issued refresh token rotates through POST /desktop-auth/token
      7. Revoking the client kills the access token immediately and the
         refresh token with it
      8. The CLI token is still unaffected by that revocation
    """
    account_jwt, _ = bootstrap_account_token(
        client, superuser_token_headers, machine_name="Exchange Machine"
    )
    acc_headers = account_cli_headers(account_jwt)

    me = _me(client, superuser_token_headers).json()

    # ── Phase 2: exchange (lazy registration) ─────────────────────────────
    issued = exchange_for_desktop_token(
        client, acc_headers, device_name="Silent Link Desktop"
    )
    assert issued["access_token"]
    assert issued["refresh_token"]
    assert issued["token_type"] == "bearer"
    assert issued["expires_in"] == settings.DESKTOP_ACCESS_TOKEN_EXPIRE_MINUTES * 60
    assert issued["client_id"]
    # D12: the response carries the account owner's email so the desktop can
    # name the profile without a second round-trip.
    assert issued["email"] == me["email"]

    desktop_headers = _bearer(issued["access_token"])

    # ── Phase 3: the issued token is a real user session ──────────────────
    r = _me(client, desktop_headers)
    assert r.status_code == 200, f"Issued desktop token must authenticate: {r.text}"
    assert r.json()["email"] == me["email"]

    # ── Phase 4: visible + distinguishable in the sessions list ───────────
    clients = list_desktop_clients(client, superuser_token_headers)
    entry = next(c for c in clients if c["client_id"] == issued["client_id"])
    assert entry["origin"] == "cli_exchange", (
        "A CLI-exchanged session must be distinguishable from a browser consent"
    )
    assert entry["device_name"] == "Silent Link Desktop"
    assert entry["platform"] == "macos"
    assert entry["app_version"] == "1.2.3"
    assert entry["is_revoked"] is False

    # ── Phase 5: the CLI token is used, never spent ───────────────────────
    r = client.get(f"{_BASE}/account/agents", headers=acc_headers)
    assert r.status_code == 200, (
        "The exchange must neither consume nor revoke the CLI account token"
    )
    # ...and it can be exchanged again (a desktop that lost its refresh token
    # simply asks a second time).
    again = exchange_for_desktop_token(
        client, acc_headers, client_id=issued["client_id"]
    )
    assert again["client_id"] == issued["client_id"]

    # ── Phase 6: rotation through the ordinary desktop refresh endpoint ───
    rotated = refresh_access_token(
        client, issued["client_id"], again["refresh_token"]
    )
    assert rotated["client_id"] == issued["client_id"]
    r = _me(client, _bearer(rotated["access_token"]))
    assert r.status_code == 200, "Rotated access token must authenticate"

    # ── Phase 7: revocation kills the issued tokens ───────────────────────
    revoke_desktop_client(client, superuser_token_headers, issued["client_id"])

    r = _me(client, _bearer(rotated["access_token"]))
    assert r.status_code == 401, (
        "Revoking the client must kill the access token on its next request, "
        "not only at the next refresh"
    )
    r = client.post(
        f"{_DESKTOP}/token",
        json={
            "grant_type": "refresh_token",
            "client_id": issued["client_id"],
            "refresh_token": rotated["refresh_token"],
        },
    )
    assert r.status_code == 400, "Revoked client's refresh token must not rotate"

    # ── Phase 8: the CLI token is untouched by desktop revocation ─────────
    r = client.get(f"{_BASE}/account/agents", headers=acc_headers)
    assert r.status_code == 200


# ── Scenario 2: binding to a supplied client id ──────────────────────────────


def test_desktop_token_exchange_binds_to_supplied_client(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Binding + origin laundering:
      1. Register a client the normal way (browser consent) → origin=browser_consent
      2. Exchange an account token naming that client id
      3. The issued pair is bound to that same client (no second row)
      4. The entry now reads origin=cli_exchange — reusing a browser-registered
         client id must not launder the provenance of the new session
    """
    browser = obtain_desktop_tokens(
        client, superuser_token_headers, device_name="Consent Desktop"
    )
    clients = list_desktop_clients(client, superuser_token_headers)
    entry = next(c for c in clients if c["client_id"] == browser["client_id"])
    assert entry["origin"] == "browser_consent", (
        "The consent flow must keep stamping browser_consent"
    )

    account_jwt, _ = bootstrap_account_token(
        client, superuser_token_headers, machine_name="Binding Machine"
    )
    acc_headers = account_cli_headers(account_jwt)

    issued = exchange_for_desktop_token(
        client, acc_headers, client_id=browser["client_id"], device_name=None
    )
    assert issued["client_id"] == browser["client_id"], (
        "Issued tokens must be bound to the supplied client id"
    )

    clients = list_desktop_clients(client, superuser_token_headers)
    matching = [c for c in clients if c["client_id"] == browser["client_id"]]
    assert len(matching) == 1, "Binding must not register a second client row"
    assert matching[0]["origin"] == "cli_exchange"
    # The display fields the browser consent registered are untouched — origin
    # is provenance, device_name is the user's label.
    assert matching[0]["device_name"] == "Consent Desktop"


# ── Scenario 3: auth matrix ──────────────────────────────────────────────────


def test_desktop_token_exchange_auth_matrix(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Only a live account CLI token may exchange:
      1. No auth → 401
      2. An ordinary user JWT → 401
      3. A per-agent (child) CLI token → 401
      4. A revoked account token → 401
      5. Another user's desktop client id → 403, and no session is issued
    """
    body = {"device_name": "Rejected Desktop", "platform": "linux"}

    # ── Phase 1: unauthenticated ──────────────────────────────────────────
    r = client.post(f"{_BASE}/account/desktop-token", json=body)
    assert r.status_code == 401

    # ── Phase 2: ordinary user JWT is not an account CLI token ────────────
    r = client.post(
        f"{_BASE}/account/desktop-token",
        headers=superuser_token_headers,
        json=body,
    )
    assert r.status_code == 401

    # ── Phase 3: a per-agent CLI token must not reach an account route ────
    agent = create_agent_via_api(client, superuser_token_headers)
    account_jwt, account_token_id = bootstrap_account_token(
        client, superuser_token_headers, machine_name="Matrix Machine"
    )
    acc_headers = account_cli_headers(account_jwt)
    child = mint_child_token(
        client, acc_headers, agent["id"], machine_name="Matrix Child"
    )
    r = client.post(
        f"{_BASE}/account/desktop-token",
        headers=cli_auth_headers(child["token"]),
        json=body,
    )
    assert r.status_code == 401, (
        "A per-agent CLI token must not be able to mint a desktop session"
    )

    # ── Phase 5 (before revoking the account token): foreign client id ────
    _, other_headers = create_random_user_with_headers(client)
    other = obtain_desktop_tokens(client, other_headers, device_name="Other Desktop")
    r = client.post(
        f"{_BASE}/account/desktop-token",
        headers=acc_headers,
        json={**body, "client_id": other["client_id"]},
    )
    assert r.status_code == 403, "Another user's client id must not be bindable"
    # The rejection issued nothing: the foreign client is untouched and no new
    # client landed for the exchanging user.
    ours = list_desktop_clients(client, superuser_token_headers)
    assert not [c for c in ours if c["client_id"] == other["client_id"]]
    assert not [c for c in ours if c["device_name"] == "Rejected Desktop"]

    # ── Phase 4: revoked account token ────────────────────────────────────
    revoke_account_token(client, superuser_token_headers, account_token_id)
    r = client.post(
        f"{_BASE}/account/desktop-token", headers=acc_headers, json=body
    )
    assert r.status_code == 401, "A revoked account token must not exchange"


# ── Scenario 4: audit trail ──────────────────────────────────────────────────


def test_desktop_token_exchange_is_audited(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    D12 requires a security event naming the exchange:
      1. A successful exchange writes CLI_ACCOUNT_DESKTOP_TOKEN_ISSUED
      2. The event carries the client id and never a token value
      3. A rejected exchange writes CLI_ACCOUNT_DESKTOP_TOKEN_DENIED
    """
    account_jwt, _ = bootstrap_account_token(
        client, superuser_token_headers, machine_name="Audit Exchange Machine"
    )
    acc_headers = account_cli_headers(account_jwt)

    issued = exchange_for_desktop_token(
        client, acc_headers, device_name="Audited Desktop"
    )

    r = client.get(
        f"{_SEC}/",
        headers=superuser_token_headers,
        params={"event_type": "CLI_ACCOUNT_DESKTOP_TOKEN_ISSUED"},
    )
    assert r.status_code == 200
    events = r.json()
    assert events["count"] >= 1, (
        "A desktop-token exchange must write CLI_ACCOUNT_DESKTOP_TOKEN_ISSUED"
    )
    event = events["data"][0]
    assert event["event_type"] == "CLI_ACCOUNT_DESKTOP_TOKEN_ISSUED"
    details = event["details"]
    assert details["client_id"] == issued["client_id"]
    assert details["device_name"] == "Audited Desktop"
    # No token value, in any field, ever.
    serialized = str(details)
    assert issued["access_token"] not in serialized
    assert issued["refresh_token"] not in serialized

    # ── Phase 3: an authorization failure is audited too ──────────────────
    _, other_headers = create_random_user_with_headers(client)
    other = obtain_desktop_tokens(client, other_headers, device_name="Denied Desktop")
    r = client.post(
        f"{_BASE}/account/desktop-token",
        headers=acc_headers,
        json={"client_id": other["client_id"], "device_name": "Denied Desktop"},
    )
    assert r.status_code == 403

    r = client.get(
        f"{_SEC}/",
        headers=superuser_token_headers,
        params={"event_type": "CLI_ACCOUNT_DESKTOP_TOKEN_DENIED"},
    )
    assert r.status_code == 200
    denied = r.json()
    assert denied["count"] >= 1, (
        "A refused exchange must be audited — it is the event a user needs to see"
    )
    assert denied["data"][0]["details"]["requested_client_id"] == other["client_id"]


# ── Scenario 5: an agent-user role may still link a desktop ─────────────────


def test_desktop_token_exchange_is_not_developer_gated(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Linking a desktop is not a build-rights operation.

    A user who holds an account CLI token but has no developer role (they were
    demoted, or their token came from ``cinna login``, which is open to any
    signed-in user) can still exchange it — the resulting session is their own
    account, not build rights over anyone's agents.
    """
    user, user_headers = create_random_user_with_headers(client)
    # The Settings-card path that mints an account token is developer-gated, so
    # promote to obtain one...
    promote_to_developer(client, superuser_token_headers, user["id"])
    account_jwt, _ = bootstrap_account_token(
        client, user_headers, machine_name="Role Machine"
    )
    acc_headers = account_cli_headers(account_jwt)

    # ...then demote before exchanging. Without this the test runs as a
    # developer and would pass identically if the route *were* role-gated —
    # it would prove nothing about its own name. Inline rather than a helper
    # because the demotion is the thing under test, not setup.
    r = client.patch(
        f"{settings.API_V1_STR}/users/{user['id']}/role",
        headers=superuser_token_headers,
        json={"role": "agent-user"},
    )
    assert r.status_code == 200, f"Demotion failed: {r.text}"
    # Sanity-check the demotion took, so the assertion below cannot pass
    # because the role change silently did nothing.
    r = client.post(
        f"{_BASE}/account/setup-tokens", headers=user_headers
    )
    assert r.status_code == 403, (
        "Precondition: an agent-user must not be able to mint an account token"
    )

    issued = exchange_for_desktop_token(
        client, acc_headers, device_name="Role Desktop"
    )
    assert issued["email"] == user["email"]

    r = _me(client, _bearer(issued["access_token"]))
    assert r.status_code == 200
    assert r.json()["email"] == user["email"]


# ── Scenario 6: a later browser consent cannot launder a CLI grant ──────────


def test_browser_consent_supersedes_rather_than_unbadges_a_cli_grant(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The reverse of Scenario 2, and the direction that actually matters.

    Users rely on the badge in the contrapositive — *no badge means no
    CLI-minted session*. So a later ordinary browser consent on the same client
    id must not be able to clear the badge while the CLI-minted session keeps
    working:
      1. Exchange an account token for a session on a lazily registered client
      2. Run a normal browser consent on that same client id
      3. The badge is gone (the current grant really is a browser consent)
      4. ...and the CLI-minted refresh token is dead, so nothing survives
         unbadged
    """
    account_jwt, _ = bootstrap_account_token(
        client, superuser_token_headers, machine_name="Launder Machine"
    )
    acc_headers = account_cli_headers(account_jwt)

    issued = exchange_for_desktop_token(
        client, acc_headers, device_name="Launder Desktop"
    )
    clients = list_desktop_clients(client, superuser_token_headers)
    entry = next(c for c in clients if c["client_id"] == issued["client_id"])
    assert entry["origin"] == "cli_exchange"

    # ── Phase 2: an ordinary browser consent on the SAME client id ────────
    verifier, challenge = generate_pkce_pair()
    code = get_authorization_code(
        client,
        superuser_token_headers,
        code_challenge=challenge,
        existing_client_id=issued["client_id"],
    )
    browser_tokens = exchange_code_for_tokens(
        client, issued["client_id"], code, verifier
    )

    # ── Phase 3: the badge is gone — truthfully, the grant is now a consent ─
    clients = list_desktop_clients(client, superuser_token_headers)
    entry = next(c for c in clients if c["client_id"] == issued["client_id"])
    assert entry["origin"] == "browser_consent"

    # ── Phase 4: ...and the CLI-minted family did not survive it ──────────
    r = client.post(
        f"{_DESKTOP}/token",
        json={
            "grant_type": "refresh_token",
            "client_id": issued["client_id"],
            "refresh_token": issued["refresh_token"],
        },
    )
    assert r.status_code == 400, (
        "A superseded CLI-minted refresh token must not survive the browser "
        "consent that cleared its badge — otherwise the absent badge is a lie"
    )
    # The new browser-granted session is the live one.
    rotated = refresh_access_token(
        client, issued["client_id"], browser_tokens["refresh_token"]
    )
    assert rotated["client_id"] == issued["client_id"]


# ── Scenario 7: revoking the account token cascades into the desktop ────────


def test_revoking_the_account_token_kills_the_desktop_session(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Revoking a CLI account token disconnects the desktop session bought with it.

    The negative half is the one that matters — a revocation path that silently
    fails to revoke is worse than one that never claimed to — so this asserts
    the tokens are *dead against the real endpoints*, not that rows changed:
      1. Exchange for a desktop session; confirm it works
      2. Revoke the account CLI token
      3. The desktop access token is rejected on its next request
      4. The desktop refresh token no longer rotates
      5. The client is gone from the active App Sessions list
      6. A desktop session the user re-authorized in a browser is NOT collateral
    """
    account_jwt, account_token_id = bootstrap_account_token(
        client, superuser_token_headers, machine_name="Cascade Machine"
    )
    acc_headers = account_cli_headers(account_jwt)

    issued = exchange_for_desktop_token(
        client, acc_headers, device_name="Cascade Desktop"
    )
    desktop_headers = _bearer(issued["access_token"])
    assert _me(client, desktop_headers).status_code == 200

    # A second, browser-consented session that must survive the cascade.
    browser = obtain_desktop_tokens(
        client, superuser_token_headers, device_name="Untouched Desktop"
    )

    # ── Phase 2: revoke the account token ─────────────────────────────────
    result = revoke_account_token(
        client, superuser_token_headers, account_token_id
    )
    assert "revoked" in result["message"].lower()

    # ── Phase 3+4: the desktop credentials are actually dead ──────────────
    r = _me(client, desktop_headers)
    assert r.status_code == 401, (
        "Revoking the account token must kill the desktop access token it "
        "bought, on the next request"
    )
    r = client.post(
        f"{_DESKTOP}/token",
        json={
            "grant_type": "refresh_token",
            "client_id": issued["client_id"],
            "refresh_token": issued["refresh_token"],
        },
    )
    assert r.status_code == 400, "The cascaded refresh token must not rotate"

    # ── Phase 5: gone from the active sessions list ───────────────────────
    clients = list_desktop_clients(client, superuser_token_headers)
    assert not [c for c in clients if c["client_id"] == issued["client_id"]]

    # ── Phase 6: the browser-consented session is untouched ───────────────
    assert _me(client, _bearer(browser["access_token"])).status_code == 200
    assert [c for c in clients if c["client_id"] == browser["client_id"]]


# ── Scenario 8: provenance is server-set, and refusals are classified ───────


def test_exchange_ignores_caller_supplied_origin_and_classifies_refusals(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Two properties the docs claim explicitly:
      1. ``origin`` is never accepted from the request body — a caller that
         sends one gets a server-determined value anyway
      2. Re-presenting a client the user disconnected is refused, and audited
         as the benign case rather than as a suspicious one
    """
    account_jwt, _ = bootstrap_account_token(
        client, superuser_token_headers, machine_name="Provenance Machine"
    )
    acc_headers = account_cli_headers(account_jwt)

    # ── Phase 1: a forged origin in the body is ignored ───────────────────
    r = client.post(
        f"{_BASE}/account/desktop-token",
        headers=acc_headers,
        json={
            "device_name": "Forged Desktop",
            "origin": "browser_consent",
        },
    )
    assert r.status_code == 200, r.text
    forged = r.json()
    clients = list_desktop_clients(client, superuser_token_headers)
    entry = next(c for c in clients if c["client_id"] == forged["client_id"])
    assert entry["origin"] == "cli_exchange", (
        "Provenance must be server-set; a body field must not be able to "
        "relabel the session"
    )

    # ── Phase 2: the user disconnects it, the desktop retries ─────────────
    revoke_desktop_client(client, superuser_token_headers, forged["client_id"])
    exchange_for_desktop_token(
        client,
        acc_headers,
        client_id=forged["client_id"],
        expected_status=403,
    )

    r = client.get(
        f"{_SEC}/",
        headers=superuser_token_headers,
        params={"event_type": "CLI_ACCOUNT_DESKTOP_TOKEN_DENIED"},
    )
    assert r.status_code == 200
    events = r.json()["data"]
    mine = [
        e for e in events
        if e["details"].get("requested_client_id") == forged["client_id"]
    ]
    assert mine, "The refusal must be audited"
    # Classified as the routine case, so a desktop retrying a disconnected
    # client id cannot flood the feed with high-severity noise.
    assert mine[0]["details"]["reason"] == "revoked_own"
    assert mine[0]["severity"] == "low"


# ── Scenario 9: rotation must not re-badge or unlink a session ──────────────


def test_rotation_preserves_provenance(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Refreshing is not a new grant, and must leave provenance alone.

    Rotation deliberately does not stamp the grant columns — but that is a
    protection nothing else exercises. If someone later adds a re-stamp call to
    the rotation path "for symmetry", a CLI-exchanged session would silently
    re-badge on its first refresh and drop out of the cascade's filter, and no
    other test in this file would notice: they all assert against a session
    that has never refreshed.

      1. Exchange, then rotate the refresh token twice
      2. The badge is still `cli_exchange` after rotating
      3. The session is still caught by the account-token cascade — the link,
         not just the label, survived
    """
    account_jwt, account_token_id = bootstrap_account_token(
        client, superuser_token_headers, machine_name="Rotation Machine"
    )
    acc_headers = account_cli_headers(account_jwt)

    issued = exchange_for_desktop_token(
        client, acc_headers, device_name="Rotating Desktop"
    )

    # ── Phase 1: rotate twice — one hop could pass by accident ────────────
    first = refresh_access_token(
        client, issued["client_id"], issued["refresh_token"]
    )
    second = refresh_access_token(
        client, issued["client_id"], first["refresh_token"]
    )
    assert _me(client, _bearer(second["access_token"])).status_code == 200

    # ── Phase 2: the badge survived rotation ──────────────────────────────
    clients = list_desktop_clients(client, superuser_token_headers)
    entry = next(c for c in clients if c["client_id"] == issued["client_id"])
    assert entry["origin"] == "cli_exchange", (
        "Rotation is not a new grant and must not re-badge the session"
    )

    # ── Phase 3: and so did the link the cascade filters on ───────────────
    revoke_account_token(client, superuser_token_headers, account_token_id)
    assert _me(client, _bearer(second["access_token"])).status_code == 401, (
        "A rotated session must still be caught by the account-token cascade — "
        "otherwise refreshing is a way to escape revocation"
    )


# ── Scenario 10: the exchanged session cannot re-mint its own input ─────────


def test_exchanged_session_cannot_mint_cli_credentials(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Close the self-replication loop.

    A desktop session bought with a CLI account token is an ordinary user JWT,
    so without a gate it can walk straight back to the credential-minting
    surfaces and produce a *fresh* account CLI token that carries no provenance
    link to the original. Revoking the stolen `account.json` would then not end
    the compromise: the attacker's new token survives the cascade and can
    exchange again.

    The gate is scoped by provenance, not by client kind — a desktop session
    that came from a browser consent is a human-approved session and keeps
    working — so this asserts both directions:
      1. CLI-exchanged session → 403 on every credential-minting surface
      2. Browser-consented desktop session → still allowed
      3. Ordinary web session → still allowed
    """
    agent = create_agent_via_api(client, superuser_token_headers)
    account_jwt, _ = bootstrap_account_token(
        client, superuser_token_headers, machine_name="Replication Machine"
    )
    acc_headers = account_cli_headers(account_jwt)

    issued = exchange_for_desktop_token(
        client, acc_headers, device_name="Replicating Desktop"
    )
    cli_session = _bearer(issued["access_token"])
    # It is a working session — the gate below is about what it may mint, not
    # about whether the token authenticates at all.
    assert _me(client, cli_session).status_code == 200

    # ── Phase 1: every minting surface refuses it ─────────────────────────
    r = client.post(f"{_BASE}/account/setup-tokens", headers=cli_session)
    assert r.status_code == 403, (
        "A CLI-exchanged session must not mint a fresh account CLI token — "
        "that token would carry no link and survive the cascade"
    )

    r = client.post(
        f"{_BASE}/setup-tokens", headers=cli_session, json={"agent_id": agent["id"]}
    )
    assert r.status_code == 403, (
        "Nor a per-agent CLI setup token, which buys sync/exec on an agent"
    )

    start = client.post(
        f"{_BASE}/account/login/start",
        json={"machine_name": "Attacker Machine", "machine_info": None},
    )
    assert start.status_code == 200, start.text
    r = client.post(
        f"{_BASE}/account/login/approve",
        headers=cli_session,
        json={"user_code": start.json()["user_code"]},
    )
    assert r.status_code == 403, (
        "Nor approve a device login it started itself — that mints an account "
        "CLI token with no role gate at all"
    )

    # ── Phase 1b: nor can it approve a consent to mint a *native* session ─
    # This is the same self-replication shape: approving a pending consent
    # mints a new client with clean browser provenance and no cascade link.
    verifier, challenge = generate_pkce_pair()
    nonce = initiate_authorize(
        client, code_challenge=challenge, device_name="Laundered Desktop"
    )
    r = client.post(
        f"{_DESKTOP}/consent",
        headers=cli_session,
        json={"request_nonce": nonce, "action": "approve"},
    )
    assert r.status_code == 403, (
        "Approving a consent would mint a native session with clean provenance"
    )
    r = client.post(
        f"{settings.API_V1_STR}/app-auth/consent",
        headers=cli_session,
        json={"request_nonce": nonce, "action": "approve"},
    )
    assert r.status_code == 403, "The mobile surface shares the loop and the gate"

    # ── Phase 2: a browser-consented desktop session is unaffected ────────
    browser = obtain_desktop_tokens(
        client, superuser_token_headers, device_name="Consented Desktop"
    )
    r = client.post(
        f"{_BASE}/account/setup-tokens", headers=_bearer(browser["access_token"])
    )
    assert r.status_code == 200, (
        "The gate is scoped by provenance: a human-approved desktop session "
        f"must keep working. Got {r.status_code}: {r.text}"
    )

    # ── Phase 3: and so is an ordinary web session ────────────────────────
    r = client.post(f"{_BASE}/account/setup-tokens", headers=superuser_token_headers)
    assert r.status_code == 200, r.text


# ── Scenario 11: a same-provenance re-grant does not disturb live sessions ──


def test_browser_reconsent_does_not_revoke_a_live_session(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The narrowing: superseding is about provenance *changing*, not about
    re-granting.

    A browser consent replacing a browser consent carries the same provenance,
    so both families keep the same badge and the same (absent) cascade link —
    revoking the older one would buy no security property and would break a
    shipped flow on two surfaces, including mobile, where an app suspended
    mid-refresh is exactly the case the reuse-grace window exists for.

      1. Consent once, keep the refresh token
      2. Consent again on the same client id
      3. The first refresh token still rotates — nothing was superseded
      4. But a CLI exchange on that client DOES supersede it (the mixed case
         still fires, which is what the narrowing must not cost us)
    """
    first = obtain_desktop_tokens(
        client, superuser_token_headers, device_name="Re-consent Desktop"
    )

    # ── Phase 2: a second consent on the SAME client id ───────────────────
    verifier, challenge = generate_pkce_pair()
    code = get_authorization_code(
        client,
        superuser_token_headers,
        code_challenge=challenge,
        existing_client_id=first["client_id"],
    )
    second = exchange_code_for_tokens(
        client, first["client_id"], code, verifier
    )
    assert second["client_id"] == first["client_id"]

    # ── Phase 3: the older refresh token is untouched ─────────────────────
    rotated = refresh_access_token(
        client, first["client_id"], first["refresh_token"]
    )
    assert rotated["client_id"] == first["client_id"], (
        "A same-provenance re-consent must not hard-revoke a live family"
    )

    # ── Phase 4: the mixed case still supersedes ──────────────────────────
    account_jwt, _ = bootstrap_account_token(
        client, superuser_token_headers, machine_name="Mixed Machine"
    )
    exchange_for_desktop_token(
        client,
        account_cli_headers(account_jwt),
        client_id=first["client_id"],
    )
    r = client.post(
        f"{_DESKTOP}/token",
        json={
            "grant_type": "refresh_token",
            "client_id": first["client_id"],
            "refresh_token": second["refresh_token"],
        },
    )
    assert r.status_code == 400, (
        "A CLI exchange changes the provenance, so it must still retire the "
        "browser-granted family — narrowing must not cost the laundering defence"
    )


# ── Scenario 12: the gate is action-granular, so DENY stays reachable ───────


def test_cli_exchanged_session_may_deny_the_consent_it_may_not_approve(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    The gate refuses credential *minting*, not the consent surface.

    ``POST /desktop-auth/consent`` and ``POST /app-auth/consent`` are one route
    each carrying two opposite actions in the body. ``action="approve"`` mints a
    native session with clean provenance — the self-replication loop Scenario 10
    closes. ``action="deny"`` mints nothing: it marks the pending request used
    and returns ``error=access_denied``. Refusing deny would let a compromised
    session block its victim from turning down a sign-in it can see pending,
    which is the exact property the gate's own docstring reserves for revocation
    and read routes.

      1. A CLI-exchanged desktop session, with a consent request pending
      2. approve on /desktop-auth/consent → 403 (the loop stays closed)
      3. deny on the SAME request → 200 with error=access_denied. Reusing the
         nonce is load-bearing beyond the stated property: it also pins that
         the 403 fires *before* ``process_consent`` marks the request used, so
         a refused approve does not burn the user's pending request
      4. The same pair on the mobile /app-auth surface
      5. The refusal names what it actually refuses, and fires before the
         nonce is read — so a refused approve does not burn a pending request
      6. An unrecognised action is refused rather than treated as an approval
      7. An ordinary web session can still approve — narrowing costs nothing
    """
    account_jwt, _ = bootstrap_account_token(
        client, superuser_token_headers, machine_name="Deny Machine"
    )
    issued = exchange_for_desktop_token(
        client, account_cli_headers(account_jwt), device_name="Denying Desktop"
    )
    # ── Phase 1: a live, working CLI-exchanged session ───────────────────
    cli_session = _bearer(issued["access_token"])
    assert _me(client, cli_session).status_code == 200

    # ── Phase 2: approve is still refused ─────────────────────────────────
    _verifier, challenge = generate_pkce_pair()
    nonce = initiate_authorize(
        client, code_challenge=challenge, device_name="Pending Desktop"
    )
    r = client.post(
        f"{_DESKTOP}/consent",
        headers=cli_session,
        json={"request_nonce": nonce, "action": "approve"},
    )
    assert r.status_code == 403, (
        "Approving a consent mints a native session with clean provenance and "
        "must stay closed to a CLI-exchanged session"
    )

    # ── Phase 3: deny on the same, still-pending request is allowed ───────
    r = client.post(
        f"{_DESKTOP}/consent",
        headers=cli_session,
        json={"request_nonce": nonce, "action": "deny"},
    )
    assert r.status_code == 200, (
        "Deny mints nothing — it is the safety action, and a gate against "
        f"credential minting must never block it. Got {r.status_code}: {r.text}"
    )
    assert "error=access_denied" in r.json()["redirect_to"], (
        "The deny must actually have been processed, not merely accepted"
    )

    # ── Phase 4: the mobile surface behaves identically ───────────────────
    _verifier, challenge = generate_pkce_pair()
    nonce = initiate_authorize(
        client, code_challenge=challenge, device_name="Pending Phone"
    )
    r = client.post(
        f"{settings.API_V1_STR}/app-auth/consent",
        headers=cli_session,
        json={"request_nonce": nonce, "action": "approve"},
    )
    assert r.status_code == 403, "The mobile surface shares the loop and the gate"
    assert "CLI account token" in r.json()["detail"], (
        "…and refuses for the gate's reason, not for a client-ownership one"
    )
    r = client.post(
        f"{settings.API_V1_STR}/app-auth/consent",
        headers=cli_session,
        json={"request_nonce": nonce, "action": "deny"},
    )
    assert r.status_code == 200, (
        f"…and shares the deny exemption. Got {r.status_code}: {r.text}"
    )
    assert "error=access_denied" in r.json()["redirect_to"]

    # ── Phase 5: the refusal describes the request it refused ────────────
    # A consent approval mints a desktop/mobile session, not a CLI credential,
    # so the message must not tell the user they cannot "create new CLI
    # credentials" — that names a capability this request never exercises. The
    # gate raises before ``process_consent`` reads the nonce, so no pending
    # request is needed here; the point is only the wording.
    r = client.post(
        f"{_DESKTOP}/consent",
        headers=cli_session,
        json={"request_nonce": "no-such-nonce", "action": "approve"},
    )
    assert r.status_code == 403, (
        "The gate must fire before the nonce is looked up, or a refused "
        f"approve would leak whether a request exists. Got {r.text}"
    )
    detail = r.json()["detail"]
    assert "CLI account token" in detail and "browser" in detail, detail
    # The positive half has to name the clause this fix introduced. Without it
    # the phase rests on a negative substring, which passes for *any* rewording
    # — including a worse one — and so cannot fail on a partial revert.
    assert "approve new sign-ins" in detail, (
        f"The refusal must name sign-in approval, not only credentials: {detail}"
    )
    assert "cannot create new CLI credentials" not in detail, (
        "The refusal must not name CLI-credential creation on a route that "
        f"mints a native session: {detail}"
    )

    # ── Phase 5b: an unrecognised action is rejected, never minted ────────
    # ``process_consent`` treats anything that is not "deny" as an approval, so
    # the handler's 400 is what keeps that branch unreachable. Pin it: if the
    # validation were dropped, "Approve" must not become a 200 that mints.
    r = client.post(
        f"{_DESKTOP}/consent",
        headers=cli_session,
        json={"request_nonce": "no-such-nonce", "action": "Approve"},
    )
    assert r.status_code in (400, 403), (
        "An unrecognised action must be refused, not treated as an approval. "
        f"Got {r.status_code}: {r.text}"
    )

    # ── Phase 7: an ordinary web session can still approve ───────────────
    _verifier, challenge = generate_pkce_pair()
    nonce = initiate_authorize(
        client, code_challenge=challenge, device_name="Human Desktop"
    )
    r = client.post(
        f"{_DESKTOP}/consent",
        headers=superuser_token_headers,
        json={"request_nonce": nonce, "action": "approve"},
    )
    assert r.status_code == 200, (
        f"Narrowing the gate must not disturb the human path: {r.text}"
    )


# ── Scenario 13: supersession is client-scoped, not user-scoped ─────────────


def test_supersession_does_not_sign_out_the_users_other_desktops(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Blast radius of the provenance-change revoke.

    When a CLI exchange changes a client's provenance, ``_stamp_grant`` revokes
    the live refresh tokens **on that client row**. Widening that filter to the
    owning user — one word in a `where` clause — would turn "link this device"
    into "sign out of every other device", silently, on a security primitive
    nobody re-reads. Scenario 11 pins that a same-provenance re-grant leaves a
    family alone, but it only ever holds one client, so a user-scoped filter
    passes it. This holds two.

      1. Two browser-consented desktops A and B, same user, both live
      2. A CLI exchange onto B's client id changes B's provenance
      3. B's browser-granted family is retired — the revoke did fire, so a
         green here is not the vacuous kind
      4. A's family still rotates — the revoke stopped at the client row
    """
    bystander = obtain_desktop_tokens(
        client, superuser_token_headers, device_name="Bystander Desktop"
    )
    superseded = obtain_desktop_tokens(
        client, superuser_token_headers, device_name="Superseded Desktop"
    )
    assert bystander["client_id"] != superseded["client_id"]

    # ── Phase 2: change the provenance of ONE of them ────────────────────
    account_jwt, _ = bootstrap_account_token(
        client, superuser_token_headers, machine_name="Blast Radius Machine"
    )
    exchange_for_desktop_token(
        client,
        account_cli_headers(account_jwt),
        client_id=superseded["client_id"],
    )

    # ── Phase 3: that client's browser-granted family is retired ─────────
    r = client.post(
        f"{_DESKTOP}/token",
        json={
            "grant_type": "refresh_token",
            "client_id": superseded["client_id"],
            "refresh_token": superseded["refresh_token"],
        },
    )
    assert r.status_code == 400, (
        "The provenance change must retire the superseded family — if this "
        "passes, the test below proves nothing"
    )

    # ── Phase 4: the user's other desktop is untouched ───────────────────
    r = client.post(
        f"{_DESKTOP}/token",
        json={
            "grant_type": "refresh_token",
            "client_id": bystander["client_id"],
            "refresh_token": bystander["refresh_token"],
        },
    )
    assert r.status_code == 200, (
        "Linking one device must not sign the user out of the others: the "
        "supersession revoke is scoped to the client row, never to the user. "
        f"Got {r.status_code}: {r.text}"
    )
    assert r.json()["client_id"] == bystander["client_id"]
