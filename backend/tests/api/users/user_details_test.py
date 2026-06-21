"""Tests for the GET/PATCH /users/me/details endpoints and the
credentials.json current_user block injection.

Covers:
  1. Parser normalization — happy path (KEY=value, comments, blank lines,
     quoted values, value-containing-=, empty value).
  2. Parser error cases → 422 with the specific ``detail`` message (no =,
     key normalizing to empty, duplicate normalized key, >100 keys, >10 KB,
     over-long key, over-long value).
  3. Empty raw → 200, details cleared.
  4. GET reflects saved details (details_raw, details_parsed).
  5. credentials.json shape — the synthetic ``current_user`` entry is present,
     has the correct id/type/credential_data shape, and the dict-comprehension
     consumer pattern works; real credentials appear alongside it unchanged.
  6. credentials/README contains the ``## Current User`` section.
  7. Re-sync fan-out — PATCH /users/me/details triggers set_credentials on
     every running environment's adapter for every agent owned by the user.
  8. No running envs → save succeeds, no error.
  9. Save still returns 200 when downstream sync raises (best-effort).
 10. Auth / me-scoped contract — unauthenticated requests rejected.

Unit tests for the pure parser logic live in
tests/unit/test_user_details_service.py (cross-reference).
"""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.credential import create_random_credential, link_credential_to_agent
from tests.utils.environment import set_environment_status
from tests.utils.user import create_random_user_with_headers
from tests.utils.utils import random_lower_string

_DETAILS = f"{settings.API_V1_STR}/users/me/details"


# ---------------------------------------------------------------------------
# Small inline helpers
# ---------------------------------------------------------------------------


def _patch_details(
    client: TestClient,
    headers: dict[str, str],
    details_raw: str,
) -> dict:
    """PATCH /users/me/details and return the response dict.

    Asserts nothing — callers check the status code themselves when
    testing errors, or call this via the happy-path helper below.
    """
    return client.patch(_DETAILS, headers=headers, json={"details_raw": details_raw})


def _patch_details_ok(
    client: TestClient,
    headers: dict[str, str],
    details_raw: str,
) -> dict:
    """PATCH /users/me/details, assert 200, return parsed JSON."""
    r = _patch_details(client, headers, details_raw)
    assert r.status_code == 200, f"PATCH /users/me/details failed: {r.text}"
    return r.json()


def _get_details(
    client: TestClient,
    headers: dict[str, str],
) -> dict:
    """GET /users/me/details, assert 200, return parsed JSON."""
    r = client.get(_DETAILS, headers=headers)
    assert r.status_code == 200, f"GET /users/me/details failed: {r.text}"
    return r.json()


def _create_agent_with_shared_adapter(
    client: TestClient,
    headers: dict[str, str],
    patch_environment_adapter,
) -> tuple[dict, EnvironmentTestAdapter]:
    """Create AI credential + agent, drain env-init tasks, install a shared adapter.

    ``create_agent_via_api`` triggers environment creation which validates that
    a default AI credential exists.  Since ``tests/api/users/conftest.py`` does
    not import ``setup_default_credentials``, each test that creates agents must
    call ``create_random_ai_credential(..., set_default=True)`` itself.  This
    helper centralises that setup so the three credential/sync scenario tests
    don't duplicate it.  (Mirrors the pattern in ``users_roles_test.py``.)
    """
    create_random_ai_credential(client, headers, set_default=True)
    agent = create_agent_via_api(client, headers)
    drain_tasks()
    agent = get_agent(client, headers, agent["id"])
    assert agent["active_environment_id"] is not None

    shared_adapter = EnvironmentTestAdapter()
    patch_environment_adapter.get_adapter = lambda env: shared_adapter
    return agent, shared_adapter


# ---------------------------------------------------------------------------
# Scenario 1 + 2: Parser normalization — happy path
# ---------------------------------------------------------------------------


def test_parser_normalization_happy_path(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Full parser normalization happy-path contract:
      1. KEY=value / comments (#) / blank lines / quoted values /
         value-containing-= / empty value all parse correctly.
      2. GET returns the normalized details_raw and details_parsed.
      3. details_parsed keys are UPPER_SNAKE, values are stripped.
    """
    _, headers = create_random_user_with_headers(client)

    raw = (
        "real name = Master of the universe\n"
        "favorite food = hotdogs\n"
        "# this is a comment\n"
        "\n"
        "  \n"
        'quoted_val = "hello world"\n'
        "single_quoted = 'foo bar'\n"
        "with_equals = key=value=pair\n"
        "empty_val = \n"
    )

    # ── Phase 1: PATCH → 200 ─────────────────────────────────────────────
    body = _patch_details_ok(client, headers, raw)

    assert body["details_raw"] is not None
    assert body["details_parsed"] is not None

    parsed = body["details_parsed"]

    # Key normalization: spaces → underscores, uppercase
    assert parsed["REAL_NAME"] == "Master of the universe"
    assert parsed["FAVORITE_FOOD"] == "hotdogs"

    # Quote stripping (one layer, matching)
    assert parsed["QUOTED_VAL"] == "hello world"
    assert parsed["SINGLE_QUOTED"] == "foo bar"

    # Value-containing-= splits on first = only
    assert parsed["WITH_EQUALS"] == "key=value=pair"

    # Empty value is allowed
    assert parsed["EMPTY_VAL"] == ""

    # Comments and blank lines are NOT keys
    assert "#" not in str(parsed)

    # ── Phase 2: GET reflects saved state ────────────────────────────────
    fetched = _get_details(client, headers)
    assert fetched["details_parsed"] == parsed
    assert fetched["details_raw"] is not None


# ---------------------------------------------------------------------------
# Scenario 3: Parser error cases → 422 with specific detail string
# ---------------------------------------------------------------------------


def test_parser_errors_return_422_with_line_references(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Every parse-error path returns HTTP 422 with a specific ``detail`` string.
    Asserts the exact shape and a substring of the expected message.

      1. Line with no '=' → "Line N: expected 'key = value'"
      2. Key normalizing to empty → "key is empty after normalization"
      3. Duplicate normalized key → "Duplicate key: REAL_NAME"
      4. >100 keys → "Too many keys"
      5. >10 KB raw text → "too large"
      6. Over-long key (>64 chars) → "exceeds 64"
      7. Over-long value (>1 KB) → "exceeds 1024"
    """
    _, headers = create_random_user_with_headers(client)

    def _expect_422(details_raw: str, detail_substring: str):
        r = _patch_details(client, headers, details_raw)
        assert r.status_code == 422, (
            f"Expected 422 for input {details_raw!r:.80}, got {r.status_code}: {r.text}"
        )
        body = r.json()
        assert "detail" in body, f"Expected 'detail' in 422 body, got: {body}"
        msg = body["detail"]
        assert detail_substring.lower() in msg.lower(), (
            f"Expected {detail_substring!r} in detail msg {msg!r}"
        )

    # 1. No '=' on a non-comment, non-blank line
    _expect_422("no equals sign here", "expected 'key = value'")

    # 2. Key normalizing to empty (a key that is only special chars)
    _expect_422("--- = value", "empty after normalization")

    # 3. Duplicate normalized key
    _expect_422(
        "real name = Alice\nreal_name = Bob",
        "Duplicate key",
    )

    # 4. More than 100 keys
    many_keys = "\n".join(f"KEY{i} = value{i}" for i in range(101))
    _expect_422(many_keys, "Too many keys")

    # 5. Raw text exceeds 10 KB
    big_value = "X" * 600
    # 18 keys × ~640 chars each ≈ 11.5 KB > 10 KB
    oversized = "\n".join(f"KEY{i} = {big_value}" for i in range(18))
    _expect_422(oversized, "too large")

    # 6. Over-long key (>64 chars after normalization)
    long_key = "A" * 65  # 65 uppercase letters → 65-char key
    _expect_422(f"{long_key} = value", "exceeds 64")

    # 7. Over-long value (>1024 chars)
    long_value = "v" * 1025
    _expect_422(f"MYKEY = {long_value}", "exceeds 1024")


# ---------------------------------------------------------------------------
# Scenario 4: Empty raw → 200, details cleared
# ---------------------------------------------------------------------------


def test_empty_raw_clears_details(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Saving an empty string:
      1. First saves some details.
      2. Then saves empty raw → 200.
      3. GET returns details_raw=None, details_parsed=None.
    """
    _, headers = create_random_user_with_headers(client)

    # ── Phase 1: Set some details ─────────────────────────────────────────
    _patch_details_ok(client, headers, "REAL_NAME = Alice")

    first_get = _get_details(client, headers)
    assert first_get["details_parsed"] is not None

    # ── Phase 2: Clear with empty string ──────────────────────────────────
    cleared = _patch_details_ok(client, headers, "")

    assert cleared["details_raw"] is None
    assert cleared["details_parsed"] is None

    # ── Phase 3: Subsequent GET also shows cleared state ──────────────────
    second_get = _get_details(client, headers)
    assert second_get["details_raw"] is None
    assert second_get["details_parsed"] is None


# ---------------------------------------------------------------------------
# Scenario 5: credentials.json synthetic current_user entry shape
# ---------------------------------------------------------------------------


def test_credentials_json_current_user_entry_shape(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
    db,
) -> None:
    """
    After saving details, the synthetic ``current_user`` entry appears in the
    credentials payload sent to the running environment adapter:

      1. Create agent; drain env-init tasks.
      2. Force env status to "running" so sync_credentials finds it.
      3. PATCH details with known values.
      4. Link a real credential to trigger adapter.set_credentials.
      5. Inspect adapter.credentials_set["credentials_json"]:
         - Entry with id="current_user" and type="current_user" exists.
         - credential_data carries username/full_name/email/email_confirmed
           and custom_details with normalized keys.
         - Dict-comprehension pattern:
           {c["id"]: c["credential_data"] for c in payload}
           yields creds["current_user"]["custom_details"]["REAL_NAME"].
         - Real credentials also present.
      6. credentials_readme contains the "## Current User" section.
    """
    # ── Phase 1: Create agent + shared adapter ────────────────────────────
    agent, shared_adapter = _create_agent_with_shared_adapter(
        client, superuser_token_headers, patch_environment_adapter
    )
    agent_id = agent["id"]
    env_id = agent["active_environment_id"]

    # ── Phase 2: Force env to "running" ───────────────────────────────────
    set_environment_status(db, env_id, "running")

    # ── Phase 3: Save details with known values ───────────────────────────
    tag = random_lower_string()[:8]
    raw = f"real name = Master of the universe {tag}\nfavorite food = hotdogs"
    _patch_details_ok(client, superuser_token_headers, raw)

    # ── Phase 4: Link a real credential to trigger a full sync ────────────
    imap_cred = create_random_credential(
        client, superuser_token_headers, credential_type="email_imap"
    )
    link_credential_to_agent(
        client, superuser_token_headers, agent_id, imap_cred["id"]
    )

    # ── Phase 5: Inspect the synced credentials payload ───────────────────
    env_data = shared_adapter.credentials_set
    assert env_data, "Adapter must have received credentials after link"

    creds_json: list[dict] = env_data["credentials_json"]
    assert creds_json, "credentials_json must be non-empty"

    # Find the synthetic entry
    entry_by_id = {c["id"]: c for c in creds_json}
    assert "current_user" in entry_by_id, (
        f"Synthetic 'current_user' entry missing; found ids: {list(entry_by_id)}"
    )

    cu_entry = entry_by_id["current_user"]
    assert cu_entry["type"] == "current_user"
    assert "credential_data" in cu_entry

    cd = cu_entry["credential_data"]
    # Identity fields from the owner's User row
    assert "email" in cd
    assert cd["email"] == settings.FIRST_SUPERUSER
    assert "email_confirmed" in cd
    assert isinstance(cd["email_confirmed"], bool)
    assert "username" in cd
    assert "full_name" in cd

    # custom_details carries the parsed key/values
    assert "custom_details" in cd
    custom = cd["custom_details"]
    assert "REAL_NAME" in custom, f"REAL_NAME not in custom_details: {custom}"
    assert f"Master of the universe {tag}" == custom["REAL_NAME"]
    assert custom["FAVORITE_FOOD"] == "hotdogs"

    # Dict-comprehension consumer pattern
    by_id = {c["id"]: c["credential_data"] for c in creds_json}
    assert by_id["current_user"]["custom_details"]["REAL_NAME"] == f"Master of the universe {tag}"

    # Real credential also present
    real_ids = [c["id"] for c in creds_json if c["id"] != "current_user"]
    assert imap_cred["id"] in real_ids, (
        f"Real credential {imap_cred['id']} not found alongside current_user"
    )

    # ── Phase 6: README contains the ## Current User section ─────────────
    readme = env_data.get("credentials_readme", "")
    assert readme, "credentials_readme must be non-empty"
    assert "## Current User" in readme, (
        f"README must contain '## Current User' section; got: {readme[:500]}"
    )


# ---------------------------------------------------------------------------
# Scenario 6: current_user entry present even with no real credentials
# ---------------------------------------------------------------------------


def test_credentials_json_current_user_present_without_real_credentials(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
    db,
) -> None:
    """
    Even when the agent has NO linked credentials (only the synthetic entry),
    the current_user block is present.

      1. Create agent; force env to "running".
      2. Link then immediately unlink a real credential so we get a fresh sync
         with only the current_user entry in credentials_json.
      3. Verify current_user is present and real cred is absent.
    """
    agent, shared_adapter = _create_agent_with_shared_adapter(
        client, superuser_token_headers, patch_environment_adapter
    )
    agent_id = agent["id"]
    env_id = agent["active_environment_id"]
    set_environment_status(db, env_id, "running")

    # Link + unlink so the last sync happens with zero real creds
    from tests.utils.credential import unlink_credential_from_agent
    cred = create_random_credential(
        client, superuser_token_headers, credential_type="email_imap"
    )
    link_credential_to_agent(client, superuser_token_headers, agent_id, cred["id"])
    unlink_credential_from_agent(client, superuser_token_headers, agent_id, cred["id"])

    env_data = shared_adapter.credentials_set
    assert env_data, "Adapter must have received credentials after unlink"

    creds_json = env_data["credentials_json"]
    ids = [c["id"] for c in creds_json]
    assert "current_user" in ids, (
        f"current_user must appear even with no real credentials; got ids: {ids}"
    )
    # The unlinked real credential is gone
    assert cred["id"] not in ids


# ---------------------------------------------------------------------------
# Scenario 7: Re-sync fan-out — 2 agents, 2 running envs
# ---------------------------------------------------------------------------


def test_resync_fanout_reaches_all_running_envs(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
    db,
) -> None:
    """
    PATCH /users/me/details triggers a re-sync to EVERY running environment
    owned by the user.

      1. Create two agents for the same user, each with a running env.
      2. Install separate shared adapters on each.
      3. PATCH /users/me/details.
      4. Both adapters must have received credentials_set after drain_tasks.
      5. Both payloads contain the updated custom_details.
    """
    # ── Phase 1: Two agents with shared adapters ──────────────────────────
    agent_a, adapter_a = _create_agent_with_shared_adapter(
        client, superuser_token_headers, patch_environment_adapter
    )
    env_a_id = agent_a["active_environment_id"]
    set_environment_status(db, env_a_id, "running")

    # Create a second agent using a new shared adapter.
    # We replace the lifecycle manager's get_adapter per-env.
    agent_b = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent_b = get_agent(client, superuser_token_headers, agent_b["id"])
    assert agent_b["active_environment_id"] is not None
    env_b_id = agent_b["active_environment_id"]
    set_environment_status(db, env_b_id, "running")

    adapter_b = EnvironmentTestAdapter()

    # Map each env to its own adapter using the env's string id from the API.
    # We compare via str(env.id) so we do not import app.models here.
    def _env_adapter(env) -> EnvironmentTestAdapter:
        env_id_str = str(env.id)
        if env_id_str == env_a_id:
            return adapter_a
        if env_id_str == env_b_id:
            return adapter_b
        return EnvironmentTestAdapter()

    patch_environment_adapter.get_adapter = _env_adapter

    # ── Phase 2: PATCH details ────────────────────────────────────────────
    tag = random_lower_string()[:8]
    _patch_details_ok(
        client, superuser_token_headers, f"project = fanout test {tag}"
    )

    # ── Phase 3: Both adapters received credentials ────────────────────────
    assert adapter_a.credentials_set, (
        "Adapter A must have received credentials after PATCH /users/me/details"
    )
    assert adapter_b.credentials_set, (
        "Adapter B must have received credentials after PATCH /users/me/details"
    )

    # Both payloads contain the updated custom_details
    for adapter, label in [(adapter_a, "A"), (adapter_b, "B")]:
        creds_json = adapter.credentials_set["credentials_json"]
        by_id = {c["id"]: c["credential_data"] for c in creds_json}
        assert "current_user" in by_id, f"Adapter {label} missing current_user"
        custom = by_id["current_user"]["custom_details"]
        assert "PROJECT" in custom, (
            f"Adapter {label} current_user.custom_details missing PROJECT: {custom}"
        )
        assert f"fanout test {tag}" == custom["PROJECT"]


# ---------------------------------------------------------------------------
# Scenario 8: No running envs — save succeeds without error
# ---------------------------------------------------------------------------


def test_patch_details_no_running_envs_succeeds(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A user with no running environments (or no agents at all) saves details
    successfully — the best-effort fan-out is a no-op.

    Using a fresh user who owns no agents.
    """
    _, headers = create_random_user_with_headers(client)

    r = _patch_details(client, headers, "HELLO = world")
    assert r.status_code == 200, (
        f"Expected 200 for user with no running envs; got {r.status_code}: {r.text}"
    )
    body = r.json()
    assert body["details_parsed"] == {"HELLO": "world"}


# ---------------------------------------------------------------------------
# Scenario 9: Sync raises — save still returns 200, DB reflects new details
# ---------------------------------------------------------------------------


def test_patch_details_returns_200_when_sync_fails(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    If the downstream env-sync fan-out raises an exception, the route must
    still return 200 and the DB must reflect the new details.

      1. Mock event_user_details_updated to raise.
      2. PATCH /users/me/details → 200.
      3. GET /users/me/details → reflects the saved values.
    """
    _, headers = create_random_user_with_headers(client)

    with patch(
        "app.services.users.user_details_service.event_user_details_updated",
        new=AsyncMock(side_effect=RuntimeError("simulated sync failure")),
    ):
        r = _patch_details(client, headers, "SYNC_FAIL = yes")
    assert r.status_code == 200, (
        f"Save must succeed even when sync raises; got {r.status_code}: {r.text}"
    )
    body = r.json()
    assert body["details_parsed"] == {"SYNC_FAIL": "yes"}

    # ── Verify persistence via GET ────────────────────────────────────────
    fetched = _get_details(client, headers)
    assert fetched["details_parsed"] == {"SYNC_FAIL": "yes"}


# ---------------------------------------------------------------------------
# Scenario 10: Auth / me-scoped contract
# ---------------------------------------------------------------------------


def test_details_endpoint_auth_contract(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Auth and me-scope contract:
      1. Unauthenticated GET → 401/403.
      2. Unauthenticated PATCH → 401/403.
      3. Two distinct users each read/write only their own details — user A's
         details do not appear in user B's response (the endpoint is /me/details,
         there is no cross-user route).
      4. Saving details for user A does not affect user B's state.
    """
    # ── Phase 1+2: Unauthenticated access rejected ─────────────────────
    r = client.get(_DETAILS)
    assert r.status_code in (401, 403), (
        f"Unauthenticated GET must be rejected; got {r.status_code}"
    )

    r = client.patch(_DETAILS, json={"details_raw": "KEY = val"})
    assert r.status_code in (401, 403), (
        f"Unauthenticated PATCH must be rejected; got {r.status_code}"
    )

    # ── Phase 3+4: Two users, isolated details ─────────────────────────
    _, headers_a = create_random_user_with_headers(client)
    _, headers_b = create_random_user_with_headers(client)

    unique_a = f"user_a_{random_lower_string()[:8]}"
    _patch_details_ok(client, headers_a, f"IDENTITY = {unique_a}")

    # B has no details yet
    b_state = _get_details(client, headers_b)
    assert b_state["details_parsed"] is None or (
        "IDENTITY" not in (b_state["details_parsed"] or {})
    ), "User B must not see user A's details"

    # B saves different details
    unique_b = f"user_b_{random_lower_string()[:8]}"
    _patch_details_ok(client, headers_b, f"IDENTITY = {unique_b}")

    # A's details are unchanged
    a_state = _get_details(client, headers_a)
    assert a_state["details_parsed"]["IDENTITY"] == unique_a, (
        "User A's IDENTITY must be unaffected by user B's PATCH"
    )

    # B's details are correct
    b_state = _get_details(client, headers_b)
    assert b_state["details_parsed"]["IDENTITY"] == unique_b
