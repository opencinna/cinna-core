"""Tests for the user locale preferences + conversation style feature.

Covers:
  1. PATCH /users/me with each valid conversation_style value persists and
     is returned by GET /users/me.
  2. PATCH /users/me with an invalid conversation_style → 400.
  3. PATCH /users/me setting timezone/language/locale persists; clearing to
     null works for those three nullable fields.
  4. PATCH /users/me with an explicit null conversation_style → 400
     (the column is NOT NULL; null must be rejected before the DB sees it).
  5. PATCH /users/me/locale-defaults fills fields when NULL; does NOT
     overwrite when already set (the load-bearing NULL-only guard).
  6. PATCH /users/me/locale-defaults no-op (all already set) returns 200
     and changes nothing.
  7. Over-length value (>64 chars on timezone/language/locale) → 422.
  8. credentials.json current_user block carries the four new keys.
  9. conversation_style defaults to 'ai_default' for a freshly created user.
 10. Auth contract — unauthenticated requests rejected.
 11. User isolation — one user's preferences do not bleed into another's.

Unit tests for the pure ConversationStyle enum live alongside the model in
backend/app/models/users/user.py and are not separately tested here; this
file covers the API-observable surface only.
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

_ME = f"{settings.API_V1_STR}/users/me"
_LOCALE_DEFAULTS = f"{settings.API_V1_STR}/users/me/locale-defaults"


# ---------------------------------------------------------------------------
# Small inline helpers
# ---------------------------------------------------------------------------


def _get_me(client: TestClient, headers: dict[str, str]) -> dict:
    """GET /users/me, assert 200, return parsed JSON."""
    r = client.get(_ME, headers=headers)
    assert r.status_code == 200, f"GET /users/me failed: {r.text}"
    return r.json()


def _patch_me(
    client: TestClient,
    headers: dict[str, str],
    payload: dict,
) -> "TestClient":  # type: ignore[return]
    """PATCH /users/me, return the raw response (caller checks status)."""
    return client.patch(_ME, headers=headers, json=payload)


def _patch_me_ok(
    client: TestClient,
    headers: dict[str, str],
    payload: dict,
) -> dict:
    """PATCH /users/me, assert 200, return parsed JSON."""
    r = _patch_me(client, headers, payload)
    assert r.status_code == 200, f"PATCH /users/me failed: {r.text}"
    return r.json()


def _patch_locale_defaults(
    client: TestClient,
    headers: dict[str, str],
    payload: dict,
) -> "TestClient":  # type: ignore[return]
    """PATCH /users/me/locale-defaults, return the raw response."""
    return client.patch(_LOCALE_DEFAULTS, headers=headers, json=payload)


def _patch_locale_defaults_ok(
    client: TestClient,
    headers: dict[str, str],
    payload: dict,
) -> dict:
    """PATCH /users/me/locale-defaults, assert 200, return parsed JSON."""
    r = _patch_locale_defaults(client, headers, payload)
    assert r.status_code == 200, f"PATCH /users/me/locale-defaults failed: {r.text}"
    return r.json()


def _create_agent_with_running_env(
    client: TestClient,
    headers: dict[str, str],
    patch_environment_adapter,
    db,
) -> tuple[dict, EnvironmentTestAdapter]:
    """Create AI credential + agent, drain env-init tasks, force env to running.

    Returns ``(agent, shared_adapter)`` ready for credentials-sync tests.
    This mirrors the helper pattern in ``user_details_test.py``.
    """
    create_random_ai_credential(client, headers, set_default=True)
    agent = create_agent_via_api(client, headers)
    drain_tasks()
    agent = get_agent(client, headers, agent["id"])
    assert agent["active_environment_id"] is not None

    shared_adapter = EnvironmentTestAdapter()
    patch_environment_adapter.get_adapter = lambda env: shared_adapter
    set_environment_status(db, agent["active_environment_id"], "running")
    return agent, shared_adapter


# ---------------------------------------------------------------------------
# Scenario 1: All valid conversation_style values persist and round-trip
# ---------------------------------------------------------------------------


def test_conversation_style_all_valid_values_persist(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    PATCH /users/me with each valid conversation_style value:
      1. Sets 'concise_direct' → GET /users/me returns it.
      2. Sets 'friendly_chatty' → GET /users/me returns it.
      3. Sets 'ai_default' → GET /users/me returns it (explicit reset works).
    """
    _, headers = create_random_user_with_headers(client)

    for style in ("concise_direct", "friendly_chatty", "ai_default"):
        # ── PATCH ──────────────────────────────────────────────────────────
        body = _patch_me_ok(client, headers, {"conversation_style": style})
        assert body["conversation_style"] == style, (
            f"Response for style={style!r} must echo back the value; got: {body['conversation_style']}"
        )

        # ── GET to confirm persistence ─────────────────────────────────────
        fetched = _get_me(client, headers)
        assert fetched["conversation_style"] == style, (
            f"GET /users/me must return persisted style={style!r}; got: {fetched['conversation_style']}"
        )


# ---------------------------------------------------------------------------
# Scenario 2 + 4: Validation errors on PATCH /users/me
# ---------------------------------------------------------------------------


def test_conversation_style_invalid_value_returns_400(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    PATCH /users/me validation contract:
      1. An unknown conversation_style string → 400 with a helpful detail.
      2. An explicit null conversation_style → 400 (column is NOT NULL).
      3. Valid styles pass (no spurious 400 from the validator).
    """
    _, headers = create_random_user_with_headers(client)

    # ── Phase 1: Unknown style string ─────────────────────────────────────
    r = _patch_me(client, headers, {"conversation_style": "robot_voice"})
    assert r.status_code == 400, (
        f"Unknown style must return 400; got {r.status_code}: {r.text}"
    )
    detail = r.json().get("detail", "")
    assert "conversation_style" in detail.lower() or "invalid" in detail.lower(), (
        f"400 detail must mention conversation_style or 'invalid'; got: {detail!r}"
    )

    # ── Phase 2: Explicit null (not just omitting the field) ───────────────
    r = _patch_me(client, headers, {"conversation_style": None})
    assert r.status_code == 400, (
        f"Explicit null conversation_style must return 400; got {r.status_code}: {r.text}"
    )

    # ── Phase 3: Valid styles still accepted (regression guard) ────────────
    _patch_me_ok(client, headers, {"conversation_style": "concise_direct"})


# ---------------------------------------------------------------------------
# Scenario 3: timezone / language / locale persist and can be cleared
# ---------------------------------------------------------------------------


def test_locale_fields_persist_and_clear(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    PATCH /users/me with timezone/language/locale:
      1. Set all three to non-null values → persist.
      2. GET /users/me returns the set values.
      3. Clear each to null one at a time → persists.
      4. GET after each clear returns null for that field.
    """
    _, headers = create_random_user_with_headers(client)

    # ── Phase 1: Set all three ─────────────────────────────────────────────
    body = _patch_me_ok(client, headers, {
        "timezone": "Europe/Berlin",
        "language": "de",
        "locale": "de-DE",
    })
    assert body["timezone"] == "Europe/Berlin"
    assert body["language"] == "de"
    assert body["locale"] == "de-DE"

    # ── Phase 2: GET confirms persistence ─────────────────────────────────
    fetched = _get_me(client, headers)
    assert fetched["timezone"] == "Europe/Berlin"
    assert fetched["language"] == "de"
    assert fetched["locale"] == "de-DE"

    # ── Phase 3: Clear timezone to null ───────────────────────────────────
    body = _patch_me_ok(client, headers, {"timezone": None})
    assert body["timezone"] is None
    fetched = _get_me(client, headers)
    assert fetched["timezone"] is None
    # Other fields unchanged
    assert fetched["language"] == "de"
    assert fetched["locale"] == "de-DE"

    # ── Phase 4: Clear language to null ───────────────────────────────────
    body = _patch_me_ok(client, headers, {"language": None})
    assert body["language"] is None
    fetched = _get_me(client, headers)
    assert fetched["language"] is None

    # ── Phase 5: Clear locale to null ─────────────────────────────────────
    body = _patch_me_ok(client, headers, {"locale": None})
    assert body["locale"] is None
    fetched = _get_me(client, headers)
    assert fetched["locale"] is None


# ---------------------------------------------------------------------------
# Scenario 5: locale-defaults fills NULL fields, does NOT overwrite set ones
# ---------------------------------------------------------------------------


def test_locale_defaults_fills_null_does_not_overwrite(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    PATCH /users/me/locale-defaults — the load-bearing NULL-only guard:
      1. Start with fresh user (all three fields NULL).
      2. locale-defaults call fills them with detected values.
      3. GET confirms values are now set.
      4. Explicitly set timezone via PATCH /users/me to a different value.
      5. locale-defaults call with a different timezone → field unchanged
         (the explicit user choice is preserved).
      6. locale-defaults with different language/locale → those also stay
         unchanged (all three are now non-NULL).
    """
    _, headers = create_random_user_with_headers(client)

    # ── Phase 1: Confirm all three start as NULL ───────────────────────────
    initial = _get_me(client, headers)
    assert initial["timezone"] is None
    assert initial["language"] is None
    assert initial["locale"] is None

    # ── Phase 2: locale-defaults fills them ───────────────────────────────
    body = _patch_locale_defaults_ok(client, headers, {
        "timezone": "America/New_York",
        "language": "en",
        "locale": "en-US",
    })
    assert body["timezone"] == "America/New_York"
    assert body["language"] == "en"
    assert body["locale"] == "en-US"

    # ── Phase 3: GET confirms persistence ─────────────────────────────────
    fetched = _get_me(client, headers)
    assert fetched["timezone"] == "America/New_York"
    assert fetched["language"] == "en"
    assert fetched["locale"] == "en-US"

    # ── Phase 4: Explicitly override timezone via /users/me ────────────────
    _patch_me_ok(client, headers, {"timezone": "Asia/Tokyo"})
    fetched = _get_me(client, headers)
    assert fetched["timezone"] == "Asia/Tokyo"

    # ── Phase 5: locale-defaults with different timezone → no overwrite ────
    body = _patch_locale_defaults_ok(client, headers, {
        "timezone": "Europe/Paris",
        "language": "fr",
        "locale": "fr-FR",
    })
    # All three are already set → none overwritten
    assert body["timezone"] == "Asia/Tokyo", (
        "locale-defaults must NOT overwrite non-NULL timezone; "
        f"expected 'Asia/Tokyo', got {body['timezone']!r}"
    )
    assert body["language"] == "en", (
        "locale-defaults must NOT overwrite non-NULL language; "
        f"expected 'en', got {body['language']!r}"
    )
    assert body["locale"] == "en-US", (
        "locale-defaults must NOT overwrite non-NULL locale; "
        f"expected 'en-US', got {body['locale']!r}"
    )

    # ── Phase 6: GET confirms state unchanged after no-overwrite call ──────
    fetched = _get_me(client, headers)
    assert fetched["timezone"] == "Asia/Tokyo"
    assert fetched["language"] == "en"
    assert fetched["locale"] == "en-US"


# ---------------------------------------------------------------------------
# Scenario 6: locale-defaults no-op returns 200
# ---------------------------------------------------------------------------


def test_locale_defaults_noop_returns_200(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    When all three fields are already set, PATCH /users/me/locale-defaults:
      1. Returns 200.
      2. Response body carries the unchanged values.
      3. A second GET confirms no mutation.
    """
    _, headers = create_random_user_with_headers(client)

    # Set all three explicitly first
    _patch_me_ok(client, headers, {
        "timezone": "Pacific/Auckland",
        "language": "en",
        "locale": "en-NZ",
    })

    # ── Phase 1+2: no-op call returns 200 with the pre-existing values ─────
    r = _patch_locale_defaults(client, headers, {
        "timezone": "Europe/London",
        "language": "fr",
        "locale": "fr-FR",
    })
    assert r.status_code == 200, f"no-op locale-defaults must return 200; got {r.status_code}: {r.text}"
    body = r.json()
    assert body["timezone"] == "Pacific/Auckland"
    assert body["language"] == "en"
    assert body["locale"] == "en-NZ"

    # ── Phase 3: GET confirms nothing changed ──────────────────────────────
    fetched = _get_me(client, headers)
    assert fetched["timezone"] == "Pacific/Auckland"
    assert fetched["language"] == "en"
    assert fetched["locale"] == "en-NZ"


# ---------------------------------------------------------------------------
# Scenario 7: Over-length values → 422
# ---------------------------------------------------------------------------


def test_over_length_locale_field_returns_422(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Values exceeding 64 characters are rejected by Pydantic max_length=64:
      1. timezone > 64 chars via PATCH /users/me → 422.
      2. language > 64 chars via PATCH /users/me → 422.
      3. locale > 64 chars via PATCH /users/me → 422.
      4. timezone > 64 chars via PATCH /users/me/locale-defaults → 422.
    """
    _, headers = create_random_user_with_headers(client)
    too_long = "Z" * 65  # 65 chars, one over the max_length=64 limit

    # ── Via PATCH /users/me ────────────────────────────────────────────────
    for field in ("timezone", "language", "locale"):
        r = _patch_me(client, headers, {field: too_long})
        assert r.status_code == 422, (
            f"Over-length {field} must return 422; got {r.status_code}: {r.text}"
        )

    # ── Via PATCH /users/me/locale-defaults ───────────────────────────────
    r = _patch_locale_defaults(client, headers, {"timezone": too_long})
    assert r.status_code == 422, (
        f"Over-length timezone via locale-defaults must return 422; got {r.status_code}: {r.text}"
    )


# ---------------------------------------------------------------------------
# Scenario 8: credentials.json current_user block carries the four new keys
# ---------------------------------------------------------------------------


def test_current_user_block_carries_locale_and_style_fields(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    patch_environment_adapter,
    db,
) -> None:
    """
    After setting locale preferences, the credentials.json ``current_user``
    block sent to the environment adapter includes the four new keys:

      1. Create agent; drain env-init tasks; force env to 'running'.
      2. Set timezone/language/locale/conversation_style via PATCH /users/me.
      3. Trigger a credentials sync by linking a real credential.
      4. Inspect adapter.credentials_set['credentials_json']:
         - current_user entry present with id='current_user'.
         - credential_data carries timezone/language/locale/conversation_style.
         - The values match what was set in step 2.
      5. Set two fields to null and re-trigger sync; confirm null is present.
    """
    agent, shared_adapter = _create_agent_with_running_env(
        client, superuser_token_headers, patch_environment_adapter, db
    )
    agent_id = agent["id"]

    # ── Phase 2: Set all four personalization fields ───────────────────────
    _patch_me_ok(client, superuser_token_headers, {
        "timezone": "Europe/Berlin",
        "language": "de",
        "locale": "de-DE",
        "conversation_style": "concise_direct",
    })

    # ── Phase 3: Link a credential to trigger a full credentials sync ──────
    cred = create_random_credential(
        client, superuser_token_headers, credential_type="email_imap"
    )
    link_credential_to_agent(client, superuser_token_headers, agent_id, cred["id"])

    # ── Phase 4: Inspect the synced credentials payload ───────────────────
    env_data = shared_adapter.credentials_set
    assert env_data, "Adapter must have received credentials after link"

    creds_json: list[dict] = env_data["credentials_json"]
    entry_by_id = {c["id"]: c for c in creds_json}
    assert "current_user" in entry_by_id, (
        f"Synthetic 'current_user' entry missing; found ids: {list(entry_by_id)}"
    )

    cd = entry_by_id["current_user"]["credential_data"]
    # All four new keys must be present
    assert "timezone" in cd, f"timezone key missing from current_user.credential_data: {cd}"
    assert "language" in cd, f"language key missing from current_user.credential_data: {cd}"
    assert "locale" in cd, f"locale key missing from current_user.credential_data: {cd}"
    assert "conversation_style" in cd, (
        f"conversation_style key missing from current_user.credential_data: {cd}"
    )

    # Values must match what was set
    assert cd["timezone"] == "Europe/Berlin"
    assert cd["language"] == "de"
    assert cd["locale"] == "de-DE"
    assert cd["conversation_style"] == "concise_direct"

    # ── Phase 5: Clear two fields; re-trigger sync; confirm null in payload ─
    _patch_me_ok(client, superuser_token_headers, {
        "timezone": None,
        "language": None,
    })

    # Unlink + relink the same credential to force another sync
    from tests.utils.credential import unlink_credential_from_agent
    unlink_credential_from_agent(
        client, superuser_token_headers, agent_id, cred["id"]
    )
    link_credential_to_agent(
        client, superuser_token_headers, agent_id, cred["id"]
    )

    env_data2 = shared_adapter.credentials_set
    cd2 = {c["id"]: c["credential_data"] for c in env_data2["credentials_json"]}["current_user"]
    assert cd2["timezone"] is None, (
        f"Cleared timezone must appear as null in credentials block; got: {cd2['timezone']!r}"
    )
    assert cd2["language"] is None, (
        f"Cleared language must appear as null in credentials block; got: {cd2['language']!r}"
    )
    # conversation_style and locale unchanged
    assert cd2["conversation_style"] == "concise_direct"
    assert cd2["locale"] == "de-DE"


# ---------------------------------------------------------------------------
# Scenario 9: Fresh user default conversation_style = 'ai_default'
# ---------------------------------------------------------------------------


def test_fresh_user_default_conversation_style(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    A newly created user's conversation_style must default to 'ai_default'.
    Verified via GET /users/me right after signup + login.
    """
    _, headers = create_random_user_with_headers(client)

    profile = _get_me(client, headers)
    assert profile["conversation_style"] == "ai_default", (
        f"Fresh user must have conversation_style='ai_default'; got: {profile['conversation_style']!r}"
    )
    # The three nullable fields must be NULL for a fresh user
    assert profile["timezone"] is None
    assert profile["language"] is None
    assert profile["locale"] is None


# ---------------------------------------------------------------------------
# Scenario 10: Auth contract — unauthenticated requests rejected
# ---------------------------------------------------------------------------


def test_locale_auth_contract(client: TestClient) -> None:
    """
    Both locale-related endpoints must reject unauthenticated requests:
      1. PATCH /users/me without auth → 401/403.
      2. PATCH /users/me/locale-defaults without auth → 401/403.
    """
    r = client.patch(_ME, json={"conversation_style": "ai_default"})
    assert r.status_code in (401, 403), (
        f"Unauthenticated PATCH /users/me must be rejected; got {r.status_code}"
    )

    r = client.patch(_LOCALE_DEFAULTS, json={"timezone": "UTC"})
    assert r.status_code in (401, 403), (
        f"Unauthenticated PATCH /users/me/locale-defaults must be rejected; got {r.status_code}"
    )


# ---------------------------------------------------------------------------
# Scenario 11: User isolation — preferences do not bleed between users
# ---------------------------------------------------------------------------


def test_locale_user_isolation(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    User A's locale preferences must not appear in User B's GET /users/me:
      1. User A sets timezone/language/locale/conversation_style.
      2. User B (freshly created) has all fields at default / NULL.
      3. User B sets different preferences.
      4. User A's preferences are unchanged.
    """
    _, headers_a = create_random_user_with_headers(client)
    _, headers_b = create_random_user_with_headers(client)

    # ── Phase 1: User A sets preferences ──────────────────────────────────
    _patch_me_ok(client, headers_a, {
        "timezone": "America/Chicago",
        "language": "en",
        "locale": "en-US",
        "conversation_style": "friendly_chatty",
    })

    # ── Phase 2: User B starts at defaults ────────────────────────────────
    profile_b = _get_me(client, headers_b)
    assert profile_b["timezone"] is None, "User B must not see User A's timezone"
    assert profile_b["conversation_style"] == "ai_default", (
        "User B must not be affected by User A's conversation_style"
    )

    # ── Phase 3: User B sets different preferences ─────────────────────────
    _patch_me_ok(client, headers_b, {
        "timezone": "Asia/Singapore",
        "conversation_style": "concise_direct",
    })

    # ── Phase 4: User A's preferences are unchanged ────────────────────────
    profile_a = _get_me(client, headers_a)
    assert profile_a["timezone"] == "America/Chicago", (
        "User A's timezone must be unchanged by User B's PATCH"
    )
    assert profile_a["conversation_style"] == "friendly_chatty", (
        "User A's conversation_style must be unchanged by User B's PATCH"
    )

    # User B's preferences match what was set
    profile_b = _get_me(client, headers_b)
    assert profile_b["timezone"] == "Asia/Singapore"
    assert profile_b["conversation_style"] == "concise_direct"
