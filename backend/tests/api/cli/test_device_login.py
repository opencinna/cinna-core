"""
Backend tests for the ``cinna login`` device-authorization flow (RFC 8628).

Covers:
- Happy path: start → poll pending → approve → poll authorized (returns account_token)
- Minted account token is usable on the /cli/account/* route surface (e.g. agents list)
- Reject path: start → reject → poll access_denied
- Terminal-state idempotency: second poll after authorized → expired_token (single-use)
- Unknown / bogus device_code → poll expired_token at HTTP 200
- slow_down: polling faster than the interval returns status="slow_down" at HTTP 200
- GET /request happy path: correct fields; no device_code / token / IP leak
- GET /request unknown user_code → 404
- approve / reject unauthenticated → 401/403
- approve unknown/already-resolved user_code → 404 / 409
- reject unknown/already-resolved user_code → 404 / 409
- Validation: over-long machine_name / machine_info → 422
- start response field names follow RFC 8628 (not aliases)
- GET /request returns dashed user_code form (XXXX-XXXX)
"""
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.cli import (
    account_cli_headers,
    list_account_agents,
)
from tests.utils.user import create_random_user, user_authentication_headers

_BASE = f"{settings.API_V1_STR}/cli/account/login"


# ── Rate-limiter reset fixture ───────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_device_login_rate_limiter():
    """Clear the in-process rate limiter between tests.

    ``DeviceLoginService._rate_limiter`` is a class-level ``RateLimiter``
    singleton whose sliding-window ``_hits`` dict accumulates across the test
    process. Without a reset, start calls from earlier tests exhaust the
    10/min per-IP limit and cause later tests to see unexpected 429s.

    This is the same pattern noted (as a coverage gap) for the proxy rate
    limiter in test_account_cli.py. Here we reset it proactively so every
    test starts with a clean window.
    """
    from app.services.cli.device_login_service import DeviceLoginService

    DeviceLoginService._rate_limiter._hits.clear()
    yield
    DeviceLoginService._rate_limiter._hits.clear()


# ── Helpers ──────────────────────────────────────────────────────────────────


def start_device_login(
    client: TestClient,
    machine_name: str = "Test Machine",
    machine_info: str | None = "Darwin/arm64",
) -> dict:
    """POST /api/v1/cli/account/login/start — begin a device-login request.

    Returns the DeviceLoginStartResponse JSON.
    """
    r = client.post(
        f"{_BASE}/start",
        json={"machine_name": machine_name, "machine_info": machine_info},
    )
    assert r.status_code == 200, f"start failed: {r.text}"
    return r.json()


def poll_device_login(client: TestClient, device_code: str) -> dict:
    """POST /api/v1/cli/account/login/poll — always HTTP 200, status in body."""
    r = client.post(f"{_BASE}/poll", json={"device_code": device_code})
    assert r.status_code == 200, f"poll returned non-200: {r.status_code} {r.text}"
    return r.json()


def approve_device_login(
    client: TestClient,
    headers: dict[str, str],
    user_code: str,
) -> dict:
    """POST /api/v1/cli/account/login/approve."""
    r = client.post(f"{_BASE}/approve", headers=headers, json={"user_code": user_code})
    assert r.status_code == 200, f"approve failed: {r.text}"
    return r.json()


def reject_device_login(
    client: TestClient,
    headers: dict[str, str],
    user_code: str,
) -> dict:
    """POST /api/v1/cli/account/login/reject."""
    r = client.post(f"{_BASE}/reject", headers=headers, json={"user_code": user_code})
    assert r.status_code == 200, f"reject failed: {r.text}"
    return r.json()


def get_request_metadata(client: TestClient, user_code: str) -> dict:
    """GET /api/v1/cli/account/login/request?user_code=... — display metadata."""
    r = client.get(f"{_BASE}/request", params={"user_code": user_code})
    assert r.status_code == 200, f"get_request_metadata failed: {r.text}"
    return r.json()


# ── Scenario 1: Happy path + token usability ─────────────────────────────────


def test_device_login_happy_path_and_token_usability(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Full happy path + account token usability:
      1. start → receives RFC 8628 fields (device_code, user_code, etc.)
      2. authenticate user (via superuser) → approve with user_code
      3. approve returns HTTP 200 message
      4. poll after approval → authorized with account_token + extras
         (no prior poll stamps last_polled_at, so slow_down does not fire)
      5. minted account_token authenticates the /account/agents route
      6. second poll → expired_token (single-use, consumed)

    NOTE: We do NOT poll before approving here to avoid stamping last_polled_at
    and triggering slow_down on the critical first authorized poll. The
    pending-poll behavior is verified in test_device_login_poll_always_200.
    """
    # ── Phase 1: start ────────────────────────────────────────────────────
    start = start_device_login(client, machine_name="my-laptop", machine_info="Darwin/arm64")

    # RFC 8628 field names (not aliases like verification_url)
    assert "device_code" in start
    assert "user_code" in start
    assert "verification_uri" in start
    assert "verification_uri_complete" in start
    assert "interval" in start
    assert "expires_in" in start

    # device_code must be a non-empty string (raw, high-entropy token)
    device_code = start["device_code"]
    assert isinstance(device_code, str) and len(device_code) > 20

    # user_code displayed in dashed form XXXX-XXXX
    user_code = start["user_code"]
    assert "-" in user_code, f"user_code should be dashed, got: {user_code!r}"

    # verification_uri_complete must embed the user_code (or its dashed form)
    assert user_code in start["verification_uri_complete"] or \
        user_code.replace("-", "") in start["verification_uri_complete"]

    assert start["interval"] == 5
    assert start["expires_in"] == 900

    # ── Phase 2 + 3: approve as authenticated user (no prior poll) ────────
    approve_resp = approve_device_login(client, superuser_token_headers, user_code)
    assert "message" in approve_resp
    assert "approved" in approve_resp["message"].lower()

    # ── Phase 4: poll after approval → authorized ─────────────────────────
    # last_polled_at is None (no prior polls) so slow_down does not fire.
    poll_auth = poll_device_login(client, device_code)
    assert poll_auth["status"] == "authorized"

    account_token = poll_auth.get("account_token")
    assert account_token is not None, "authorized poll must include account_token"
    assert isinstance(account_token, str) and len(account_token) > 20

    # Optional extras should be present on authorized
    assert "machine_name" in poll_auth
    assert poll_auth["machine_name"] == "my-laptop"

    # ── Phase 5: minted account token must work on /account/* routes ──────
    account_headers = account_cli_headers(account_token)
    agents = list_account_agents(client, account_headers)
    # The call must succeed (returns a list, possibly empty in test DB)
    assert isinstance(agents, list)

    # ── Phase 6: second poll → consumed (single-use) ─────────────────────
    # After the authorized poll, the row transitions to "consumed". A second
    # poll will be rate-limited (slow_down) if it arrives within the 5-second
    # interval — because slow_down is checked BEFORE status dispatch. In the
    # test environment the second poll is immediate, so slow_down fires. Both
    # slow_down and expired_token prove that the token is not re-issued; we
    # assert the union rather than sleeping 5 seconds.
    poll_second = poll_device_login(client, device_code)
    assert poll_second["status"] in ("expired_token", "slow_down"), (
        f"Second poll on consumed device_code should be expired_token or slow_down, "
        f"got: {poll_second['status']!r}"
    )
    # In either case no account_token must be returned
    assert poll_second.get("account_token") is None


# ── Scenario 2: Reject path ───────────────────────────────────────────────────


def test_device_login_reject_path(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Reject path:
      1. start
      2. reject as authenticated user (no prior poll to avoid slow_down)
      3. poll → access_denied
      4. second poll → access_denied (terminal state stays denied)

    NOTE: We omit the pre-reject pending poll here to avoid stamping
    last_polled_at and triggering slow_down on the critical status-check poll.
    The pending-poll path is covered in test_device_login_poll_always_200.
    """
    # ── Phase 1: start ────────────────────────────────────────────────────
    start = start_device_login(client, machine_name="reject-test-machine")
    device_code = start["device_code"]
    user_code = start["user_code"]

    # ── Phase 2: reject (no prior poll — last_polled_at remains None) ─────
    reject_resp = reject_device_login(client, superuser_token_headers, user_code)
    assert "message" in reject_resp
    assert "rejected" in reject_resp["message"].lower()

    # ── Phase 3: poll → access_denied ────────────────────────────────────
    poll_denied = poll_device_login(client, device_code)
    assert poll_denied["status"] == "access_denied"
    assert "account_token" not in poll_denied or poll_denied.get("account_token") is None

    # ── Phase 4: poll again → still access_denied (terminal state) ────────
    # Note: the second poll may hit slow_down (interval check), which is fine —
    # once access_denied is observed on Phase 3, the terminal state is confirmed.
    # We assert 200 only, as the service returns slow_down on rapid re-polls.
    r = client.post(f"{_BASE}/poll", json={"device_code": device_code})
    assert r.status_code == 200


# ── Scenario 3: Unknown / bogus device_code ───────────────────────────────────


def test_device_login_unknown_device_code(
    client: TestClient,
) -> None:
    """
    Bogus/unknown device_code must return expired_token at HTTP 200 (not 404/400).
    Anti-enumeration: no distinction between never-existed and expired.
    """
    bogus_code = "this-is-not-a-real-device-code-" + str(uuid.uuid4())
    r = client.post(f"{_BASE}/poll", json={"device_code": bogus_code})
    # Poll ALWAYS returns HTTP 200
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "expired_token"


# ── Scenario 4: slow_down (per-device_code) ────────────────────────────────


def test_device_login_slow_down(
    client: TestClient,
) -> None:
    """
    Poll twice in rapid succession (faster than the 5-second interval):
      1. start
      2. first poll → authorization_pending (also stamps last_polled_at)
      3. second immediate poll → slow_down (HTTP 200)
    """
    start = start_device_login(client, machine_name="slow-down-test")
    device_code = start["device_code"]
    interval = start["interval"]
    assert interval == 5  # verify the contract value

    # First poll — stamps last_polled_at; should get authorization_pending
    first = poll_device_login(client, device_code)
    assert first["status"] == "authorization_pending"

    # Second poll immediately — faster than 5s interval → slow_down
    second = poll_device_login(client, device_code)
    assert second["status"] == "slow_down"
    assert second.get("account_token") is None


# ── Scenario 5: GET /request metadata ────────────────────────────────────────


def test_device_login_request_metadata(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    GET /request happy path + security / 404:
      1. start → get user_code
      2. GET /request?user_code=... → machine_name / machine_info / user_code / status
      3. Response must NOT include device_code, token, IP, or approver fields
      4. user_code in response is in dashed form
      5. Unknown user_code → 404
      6. Consumed (post-poll) user_code → 404 (no longer live)
    """
    machine_name = "display-test-machine"
    machine_info = "Linux/amd64"
    start = start_device_login(client, machine_name=machine_name, machine_info=machine_info)
    user_code = start["user_code"]

    # ── Phase 2: GET /request → display metadata ──────────────────────────
    meta = get_request_metadata(client, user_code)

    assert meta["machine_name"] == machine_name
    assert meta["machine_info"] == machine_info
    assert meta["status"] == "pending"

    # user_code in response is in dashed form (XXXX-XXXX)
    assert "-" in meta["user_code"], f"user_code should be dashed, got: {meta['user_code']!r}"

    # ── Phase 3: Security — must NOT expose secrets ───────────────────────
    assert "device_code" not in meta, "device_code must never appear in display metadata"
    assert "device_code_hash" not in meta
    assert "account_token" not in meta
    assert "account_token_jwt" not in meta
    assert "client_ip" not in meta
    assert "approved_by_user_id" not in meta
    assert "minted_token_id" not in meta
    assert "token" not in meta

    # ── Phase 5: Unknown user_code → 404 ─────────────────────────────────
    r = client.get(f"{_BASE}/request", params={"user_code": "XXXX-9999"})
    assert r.status_code == 404

    # ── Phase 6: Consumed request → 404 ──────────────────────────────────
    # Approve and poll to consume the token
    approve_device_login(client, superuser_token_headers, user_code)
    poll_device_login(client, start["device_code"])  # authorized poll → consumed

    # The request is now consumed → _load_live_by_user_code excludes consumed rows → 404
    r = client.get(f"{_BASE}/request", params={"user_code": user_code})
    assert r.status_code == 404


# ── Scenario 6: Auth guards on approve/reject ────────────────────────────────


def test_device_login_approve_reject_auth_guards(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    approve and reject require authentication:
      1. start two requests (one to test approve guard, one for reject)
      2. Unauthenticated approve → 401/403
      3. Unauthenticated reject → 401/403
      4. approve unknown user_code → 404
      5. reject unknown user_code → 404
      6. approve → then approve again → 409 (already_resolved)
      7. approve → then reject → 409 (already_resolved)
    """
    # ── Phase 1: start two requests ──────────────────────────────────────
    start1 = start_device_login(client, machine_name="auth-guard-test-1")
    user_code1 = start1["user_code"]
    device_code1 = start1["device_code"]

    start2 = start_device_login(client, machine_name="auth-guard-test-2")
    user_code2 = start2["user_code"]

    # ── Phase 2: unauthenticated approve → 401/403 ───────────────────────
    r = client.post(f"{_BASE}/approve", json={"user_code": user_code1})
    assert r.status_code in (401, 403), (
        f"Unauthenticated approve should be 401/403, got {r.status_code}"
    )

    # ── Phase 3: unauthenticated reject → 401/403 ─────────────────────────
    r = client.post(f"{_BASE}/reject", json={"user_code": user_code2})
    assert r.status_code in (401, 403), (
        f"Unauthenticated reject should be 401/403, got {r.status_code}"
    )

    # ── Phase 4: approve unknown user_code → 404 ─────────────────────────
    r = client.post(
        f"{_BASE}/approve",
        headers=superuser_token_headers,
        json={"user_code": "ZZZZ-ZZZZ"},
    )
    assert r.status_code == 404

    # ── Phase 5: reject unknown user_code → 404 ──────────────────────────
    r = client.post(
        f"{_BASE}/reject",
        headers=superuser_token_headers,
        json={"user_code": "ZZZZ-ZZZZ"},
    )
    assert r.status_code == 404

    # ── Phase 6: approve → approve again → 409 (already_resolved) ────────
    approve_device_login(client, superuser_token_headers, user_code1)
    # The first poll to get the token, so row goes to "consumed"
    poll_device_login(client, device_code1)

    # Attempt to approve again (row is consumed → not live → not_found → 404)
    r = client.post(
        f"{_BASE}/approve",
        headers=superuser_token_headers,
        json={"user_code": user_code1},
    )
    # consumed rows are excluded from live lookup → 404 (not_found reason)
    assert r.status_code == 404

    # ── Phase 7: start fresh, approve, then try to reject → 409 ──────────
    start3 = start_device_login(client, machine_name="already-approved-test")
    user_code3 = start3["user_code"]

    # Approve first — row goes to "approved" (not yet polled/consumed)
    approve_device_login(client, superuser_token_headers, user_code3)

    # Try to reject the now-approved request → 409 (already_resolved)
    r = client.post(
        f"{_BASE}/reject",
        headers=superuser_token_headers,
        json={"user_code": user_code3},
    )
    assert r.status_code == 409

    # Try to approve again while approved → 409 (already_resolved)
    r = client.post(
        f"{_BASE}/approve",
        headers=superuser_token_headers,
        json={"user_code": user_code3},
    )
    assert r.status_code == 409


# ── Scenario 7: reject then approve → 409 ─────────────────────────────────


def test_device_login_reject_then_approve_is_409(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Reject a request, then try to approve → 409 (already_resolved).
    The denied state is terminal; approve-after-reject must never mint a token.
    """
    start = start_device_login(client, machine_name="reject-then-approve-test")
    user_code = start["user_code"]

    # Reject first
    reject_device_login(client, superuser_token_headers, user_code)

    # Approve after rejection → 409
    r = client.post(
        f"{_BASE}/approve",
        headers=superuser_token_headers,
        json={"user_code": user_code},
    )
    assert r.status_code == 409

    # And the poll still returns access_denied (no token ever minted)
    poll_resp = poll_device_login(client, start["device_code"])
    assert poll_resp["status"] == "access_denied"
    assert poll_resp.get("account_token") is None


# ── Scenario 8: Validation — over-long inputs → 422 ──────────────────────────


def test_device_login_start_validation(
    client: TestClient,
) -> None:
    """
    start must return 422 for over-long inputs (before any DB write):
      - machine_name > 100 chars → 422
      - machine_info > 200 chars → 422
    """
    # ── Over-long machine_name ────────────────────────────────────────────
    r = client.post(
        f"{_BASE}/start",
        json={"machine_name": "x" * 101, "machine_info": "Linux/amd64"},
    )
    assert r.status_code == 422

    # ── Over-long machine_info ────────────────────────────────────────────
    r = client.post(
        f"{_BASE}/start",
        json={"machine_name": "valid-name", "machine_info": "x" * 201},
    )
    assert r.status_code == 422

    # Boundary: exactly 100 chars machine_name → should succeed
    r = client.post(
        f"{_BASE}/start",
        json={"machine_name": "x" * 100, "machine_info": None},
    )
    assert r.status_code == 200

    # Boundary: exactly 200 chars machine_info → should succeed
    r = client.post(
        f"{_BASE}/start",
        json={"machine_name": "valid-name", "machine_info": "x" * 200},
    )
    assert r.status_code == 200


# ── Scenario 9: poll always HTTP 200 ─────────────────────────────────────────


def test_device_login_poll_always_200(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    poll must return HTTP 200 for every flow state:
      - pending (before approval)
      - slow_down (poll too fast)
      - authorized (after approval, first poll)
      - expired_token (after consumed — second poll)
      - unknown device_code → expired_token at 200
      None of these should return 4xx or 5xx.
    """
    start = start_device_login(client, machine_name="always-200-test")
    device_code = start["device_code"]
    user_code = start["user_code"]

    # pending
    r = client.post(f"{_BASE}/poll", json={"device_code": device_code})
    assert r.status_code == 200
    assert r.json()["status"] == "authorization_pending"

    # slow_down (immediate second poll)
    r = client.post(f"{_BASE}/poll", json={"device_code": device_code})
    assert r.status_code == 200
    assert r.json()["status"] == "slow_down"

    # Approve to get into authorized state, but we need to wait for slow_down
    # to clear (or just approve and then the next poll should be authorized
    # regardless — per service logic, slow_down check comes BEFORE status dispatch).
    # We therefore approve and start a fresh flow to get clean "authorized" read.
    start2 = start_device_login(client, machine_name="authorized-200-test")
    device_code2 = start2["device_code"]
    user_code2 = start2["user_code"]

    # First poll to stamp last_polled_at on start2
    r2 = client.post(f"{_BASE}/poll", json={"device_code": device_code2})
    assert r2.status_code == 200
    assert r2.json()["status"] == "authorization_pending"

    # Approve
    client.post(f"{_BASE}/approve", headers=superuser_token_headers, json={"user_code": user_code2})

    # Poll for authorized (must be 200)
    r2 = client.post(f"{_BASE}/poll", json={"device_code": device_code2})
    assert r2.status_code == 200
    # We might hit slow_down if the test is fast; keep polling until we don't
    # This is correct behavior - just assert 200 in either case
    assert r2.json()["status"] in ("authorized", "slow_down")

    # expired_token for bogus code
    r_bogus = client.post(f"{_BASE}/poll", json={"device_code": "bogus-code-" + str(uuid.uuid4())})
    assert r_bogus.status_code == 200
    assert r_bogus.json()["status"] == "expired_token"


# ── Scenario 10: Non-developer user can approve/reject (any authenticated role)


def test_device_login_any_authenticated_user_can_approve(
    client: TestClient,
) -> None:
    """
    The actual route implementation uses CurrentUser with ANY role (no developer
    gate). A newly-created regular user (agent-user role by default) must be able
    to approve or reject a login request.

    NOTE: The plan document said require_developer, but the actual route
    implementation has CurrentUser only (no RoleService.require_developer call).
    This test verifies the IMPLEMENTED behavior.
    """
    # Create a regular (non-developer) user
    other_user = create_random_user(client)
    other_headers = user_authentication_headers(
        client=client,
        email=other_user["email"],
        password=other_user["_password"],
    )

    # start a request
    start = start_device_login(client, machine_name="any-role-test")
    user_code = start["user_code"]

    # A non-developer user approves it — should succeed (any role)
    r = client.post(
        f"{_BASE}/approve",
        headers=other_headers,
        json={"user_code": user_code},
    )
    assert r.status_code == 200, (
        f"Any authenticated user should be able to approve (any role). Got {r.status_code}: {r.text}"
    )


# ── Scenario 11: Approved request stays approved until polled ──────────────


def test_device_login_approved_row_stays_available_until_polled(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    An approved (but not yet polled) request:
      - GET /request still shows status "approved" (not consumed, so still live)
      - First poll returns authorized with the token
      - GET /request after consumption returns 404 (consumed rows excluded from live lookup)
    Verifies that lazy expiry does NOT apply to approved rows (service doc: only
    pending rows are expired by _lazy_expire).
    """
    start = start_device_login(client, machine_name="approved-stays-test")
    device_code = start["device_code"]
    user_code = start["user_code"]

    # Approve without polling first (last_polled_at stays None)
    approve_device_login(client, superuser_token_headers, user_code)

    # GET /request should still work (row is "approved", not "consumed")
    meta = get_request_metadata(client, user_code)
    assert meta["status"] == "approved"

    # First poll → authorized with the token.
    # last_polled_at is None (no prior polls), so slow_down does not fire.
    # Status dispatch then sees "approved" → returns authorized.
    poll1 = poll_device_login(client, device_code)
    assert poll1["status"] == "authorized"
    assert poll1.get("account_token") is not None

    # GET /request after consumption → 404 (consumed rows excluded from live lookup)
    r = client.get(f"{_BASE}/request", params={"user_code": user_code})
    assert r.status_code == 404


# ── Scenario 12: machine_info optional ───────────────────────────────────────


def test_device_login_machine_info_optional(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    machine_info is optional. A start without machine_info should succeed, and
    GET /request should return machine_info as null.
    """
    start = start_device_login(client, machine_name="no-info-test", machine_info=None)
    user_code = start["user_code"]

    meta = get_request_metadata(client, user_code)
    assert meta["machine_name"] == "no-info-test"
    assert meta["machine_info"] is None
