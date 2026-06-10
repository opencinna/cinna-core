"""
Tests for the default_ai_functions_sdk user preference.

Covers:
  1. New users get "system" as the default value
  2. Valid values ("system", "personal:anthropic") are accepted and persisted (personal: prefix for user credentials)
  3. Invalid values are rejected with HTTP 400
  4. The preference is returned in GET /users/me

Tests for the default_ai_functions_credential_id user preference.

Covers:
  5. Successfully setting a valid Anthropic API key credential — 200, persisted
  6. Auto-clearing credential_id when switching SDK to "system"
  7. Setting a non-existent UUID — 404
  8. Setting another user's credential — 404
  9. Setting a non-Anthropic credential — 400 with "Only Anthropic credentials"
  10. Setting an OAuth token credential — 400 with "OAuth tokens cannot be used"

Tests for OpenAI provider support (personal:openai).

Covers:
  11. Setting SDK to "personal:openai" is accepted and persisted
  12. Pinning an OpenAI credential when SDK is "personal:openai" — 200, persisted
  13. Pinning an Anthropic credential when SDK is "personal:openai" — 400 (wrong type)
  14. Pinning an OpenAI credential when SDK is "personal:anthropic" — 400 (wrong type)
  15. Switching SDK to "system" from "personal:openai" auto-clears credential_id
  16. Auto-set to "personal:openai" when first OpenAI credential created and no personal preference set
  17. Auto-set is skipped when personal preference already configured (Anthropic takes priority)
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.user import create_random_user_with_headers


def test_default_ai_functions_sdk_full_lifecycle(
    client: TestClient,
) -> None:
    """
    Full lifecycle for the default_ai_functions_sdk user preference:
      1. New user is created — default value is "system"
      2. Update to "personal:anthropic" — accepted, persisted
      3. Update back to "system" — accepted, persisted
      4. Invalid value is rejected with HTTP 400
      5. None/unset does not clear the value (partial update semantics)
    """
    user, headers = create_random_user_with_headers(client)

    # ── Phase 1: New user has default "system" ─────────────────────────
    r = client.get(f"{settings.API_V1_STR}/users/me", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert data["default_ai_functions_sdk"] == "system", (
        f"Expected 'system' for new user, got {data['default_ai_functions_sdk']!r}"
    )

    # ── Phase 2: Update to "personal:anthropic" ────────────────────────────────
    r = client.patch(
        f"{settings.API_V1_STR}/users/me",
        headers=headers,
        json={"default_ai_functions_sdk": "personal:anthropic"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["default_ai_functions_sdk"] == "personal:anthropic"

    # Verify persistence via GET
    r = client.get(f"{settings.API_V1_STR}/users/me", headers=headers)
    assert r.status_code == 200
    assert r.json()["default_ai_functions_sdk"] == "personal:anthropic"

    # ── Phase 3: Update back to "system" ─────────────────────────────
    r = client.patch(
        f"{settings.API_V1_STR}/users/me",
        headers=headers,
        json={"default_ai_functions_sdk": "system"},
    )
    assert r.status_code == 200
    assert r.json()["default_ai_functions_sdk"] == "system"

    # ── Phase 4: Invalid value is rejected ───────────────────────────
    r = client.patch(
        f"{settings.API_V1_STR}/users/me",
        headers=headers,
        json={"default_ai_functions_sdk": "openai"},
    )
    assert r.status_code == 400
    detail = r.json().get("detail", "")
    assert "invalid" in detail.lower() or "ai functions sdk" in detail.lower(), (
        f"Expected error message about invalid SDK, got: {detail!r}"
    )

    # Verify value was not changed despite the failed update
    r = client.get(f"{settings.API_V1_STR}/users/me", headers=headers)
    assert r.status_code == 200
    assert r.json()["default_ai_functions_sdk"] == "system"

    # ── Phase 5: Omitting the field does not clear the value ─────────
    # First set to anthropic, then do an unrelated update without the field
    client.patch(
        f"{settings.API_V1_STR}/users/me",
        headers=headers,
        json={"default_ai_functions_sdk": "personal:anthropic"},
    )
    r = client.patch(
        f"{settings.API_V1_STR}/users/me",
        headers=headers,
        json={"full_name": "Test User"},
    )
    assert r.status_code == 200
    r = client.get(f"{settings.API_V1_STR}/users/me", headers=headers)
    assert r.status_code == 200
    assert r.json()["default_ai_functions_sdk"] == "personal:anthropic", (
        "Unrelated PATCH should not clear default_ai_functions_sdk"
    )


def test_ai_functions_sdk_invalid_values(
    client: TestClient,
) -> None:
    """
    Verify several invalid values are all rejected with HTTP 400.

    This is a focused validation test — invalid values cannot be part of the
    main lifecycle because they leave no side-effects.
    """
    _, headers = create_random_user_with_headers(client)

    # Only non-empty invalid strings are rejected — empty string is treated as "not provided"
    invalid_values = ["openai", "gemini", "system2", "ANTHROPIC", "System", "anthropic"]
    for value in invalid_values:
        r = client.patch(
            f"{settings.API_V1_STR}/users/me",
            headers=headers,
            json={"default_ai_functions_sdk": value},
        )
        assert r.status_code == 400, (
            f"Expected 400 for value {value!r}, got {r.status_code}: {r.text}"
        )


def test_ai_functions_sdk_unauthenticated(
    client: TestClient,
) -> None:
    """Unauthenticated requests to update user preferences are rejected."""
    r = client.patch(
        f"{settings.API_V1_STR}/users/me",
        json={"default_ai_functions_sdk": "personal:anthropic"},
    )
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# default_ai_functions_credential_id tests
# ---------------------------------------------------------------------------


def test_default_ai_functions_credential_id_full_lifecycle(
    client: TestClient,
) -> None:
    """
    Full lifecycle for the default_ai_functions_credential_id user preference:
      1. Create a user and a valid Anthropic API key credential
      2. Pin the credential via PATCH /users/me — 200, credential_id persisted
      3. Verify the credential_id is returned in GET /users/me
      4. Switch SDK to "system" — credential_id is auto-cleared to null
      5. Verify credential_id is null after SDK switch to "system"
      6. Pin credential again, then attempt to set a non-existent UUID — 404
      7. Attempt to set another user's credential — 404
      8. Attempt to set a non-Anthropic credential — 400
      9. Attempt to set an OAuth token credential — 400
    """
    _, headers = create_random_user_with_headers(client)

    # ── Phase 1: Create a valid Anthropic API key credential ──────────────
    cred = create_random_ai_credential(
        client,
        headers,
        credential_type="anthropic",
        api_key="sk-ant-api03-valid-key-for-ai-functions",
    )
    cred_id = cred["id"]

    # ── Phase 2: Pin the credential ───────────────────────────────────────
    # First set SDK to personal:anthropic so the credential pin is not cleared
    r = client.patch(
        f"{settings.API_V1_STR}/users/me",
        headers=headers,
        json={"default_ai_functions_sdk": "personal:anthropic"},
    )
    assert r.status_code == 200

    r = client.patch(
        f"{settings.API_V1_STR}/users/me",
        headers=headers,
        json={"default_ai_functions_credential_id": cred_id},
    )
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    data = r.json()
    assert data["default_ai_functions_credential_id"] == cred_id

    # ── Phase 3: Verify persistence via GET ───────────────────────────────
    r = client.get(f"{settings.API_V1_STR}/users/me", headers=headers)
    assert r.status_code == 200
    assert r.json()["default_ai_functions_credential_id"] == cred_id

    # ── Phase 4: Switch SDK to "system" — credential_id is auto-cleared ──
    r = client.patch(
        f"{settings.API_V1_STR}/users/me",
        headers=headers,
        json={"default_ai_functions_sdk": "system"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["default_ai_functions_sdk"] == "system"
    # The credential_id must have been cleared automatically
    assert data["default_ai_functions_credential_id"] is None, (
        "Switching to 'system' SDK should auto-clear default_ai_functions_credential_id"
    )

    # ── Phase 5: Verify cleared state via GET ─────────────────────────────
    r = client.get(f"{settings.API_V1_STR}/users/me", headers=headers)
    assert r.status_code == 200
    assert r.json()["default_ai_functions_credential_id"] is None

    # ── Phase 6: Non-existent credential UUID → 404 ───────────────────────
    ghost_id = str(uuid.uuid4())
    r = client.patch(
        f"{settings.API_V1_STR}/users/me",
        headers=headers,
        json={"default_ai_functions_credential_id": ghost_id},
    )
    assert r.status_code == 404, (
        f"Expected 404 for non-existent credential, got {r.status_code}: {r.text}"
    )
    assert "not found" in r.json().get("detail", "").lower()

    # ── Phase 7: Another user's credential → 404 ─────────────────────────
    _, other_headers = create_random_user_with_headers(client)
    other_cred = create_random_ai_credential(
        client,
        other_headers,
        credential_type="anthropic",
        api_key="sk-ant-api03-other-user-key",
    )
    r = client.patch(
        f"{settings.API_V1_STR}/users/me",
        headers=headers,
        json={"default_ai_functions_credential_id": other_cred["id"]},
    )
    assert r.status_code == 404, (
        f"Expected 404 for another user's credential, got {r.status_code}: {r.text}"
    )
    assert "not found" in r.json().get("detail", "").lower()

    # ── Phase 8: Non-Anthropic credential → 400 ──────────────────────────
    minimax_cred = create_random_ai_credential(
        client,
        headers,
        credential_type="minimax",
        api_key="mm-test-key-not-anthropic",
    )
    r = client.patch(
        f"{settings.API_V1_STR}/users/me",
        headers=headers,
        json={"default_ai_functions_credential_id": minimax_cred["id"]},
    )
    assert r.status_code == 400, (
        f"Expected 400 for non-Anthropic credential, got {r.status_code}: {r.text}"
    )
    detail = r.json().get("detail", "")
    assert "anthropic" in detail.lower(), (
        f"Expected 'Anthropic' in error detail, got: {detail!r}"
    )

    # ── Phase 9: OAuth token credential → 400 ────────────────────────────
    oauth_cred = create_random_ai_credential(
        client,
        headers,
        credential_type="anthropic",
        api_key="sk-ant-oat01-this-is-an-oauth-token",
    )
    r = client.patch(
        f"{settings.API_V1_STR}/users/me",
        headers=headers,
        json={"default_ai_functions_credential_id": oauth_cred["id"]},
    )
    assert r.status_code == 400, (
        f"Expected 400 for OAuth token credential, got {r.status_code}: {r.text}"
    )
    detail = r.json().get("detail", "")
    assert "oauth" in detail.lower(), (
        f"Expected 'OAuth' in error detail, got: {detail!r}"
    )


# ---------------------------------------------------------------------------
# OpenAI provider support (personal:openai)
# ---------------------------------------------------------------------------


def test_personal_openai_sdk_lifecycle(
    client: TestClient,
) -> None:
    """
    Full lifecycle for the personal:openai AI functions SDK preference:
      1. Set SDK to "personal:openai" — accepted, persisted
      2. Create an OpenAI credential and pin it — 200, credential_id persisted
      3. Verify the pinned credential_id is returned in GET /users/me
      4. Switch SDK to "system" — credential_id is auto-cleared to null
      5. Set SDK back to "personal:openai" and pin an Anthropic credential — 400 (wrong type)
      6. Pin the OpenAI credential again — 200 (correct type)
      7. Switch SDK to "personal:anthropic" and attempt to pin the OpenAI credential — 400 (wrong type)
    """
    _, headers = create_random_user_with_headers(client)

    # ── Phase 1: Set SDK to "personal:openai" ─────────────────────────────
    r = client.patch(
        f"{settings.API_V1_STR}/users/me",
        headers=headers,
        json={"default_ai_functions_sdk": "personal:openai"},
    )
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
    data = r.json()
    assert data["default_ai_functions_sdk"] == "personal:openai"

    # Verify persistence via GET
    r = client.get(f"{settings.API_V1_STR}/users/me", headers=headers)
    assert r.status_code == 200
    assert r.json()["default_ai_functions_sdk"] == "personal:openai"

    # ── Phase 2: Create an OpenAI credential and pin it ───────────────────
    openai_cred = create_random_ai_credential(
        client,
        headers,
        credential_type="openai",
        api_key="sk-openai-test-key-for-ai-functions",
    )
    openai_cred_id = openai_cred["id"]

    r = client.patch(
        f"{settings.API_V1_STR}/users/me",
        headers=headers,
        json={"default_ai_functions_credential_id": openai_cred_id},
    )
    assert r.status_code == 200, f"Expected 200 pinning OpenAI cred, got {r.status_code}: {r.text}"
    data = r.json()
    assert data["default_ai_functions_credential_id"] == openai_cred_id

    # ── Phase 3: Verify pinned credential_id via GET ──────────────────────
    r = client.get(f"{settings.API_V1_STR}/users/me", headers=headers)
    assert r.status_code == 200
    assert r.json()["default_ai_functions_credential_id"] == openai_cred_id

    # ── Phase 4: Switch SDK to "system" — credential_id is auto-cleared ───
    r = client.patch(
        f"{settings.API_V1_STR}/users/me",
        headers=headers,
        json={"default_ai_functions_sdk": "system"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["default_ai_functions_sdk"] == "system"
    assert data["default_ai_functions_credential_id"] is None, (
        "Switching to 'system' SDK should auto-clear default_ai_functions_credential_id"
    )

    # Verify cleared state via GET
    r = client.get(f"{settings.API_V1_STR}/users/me", headers=headers)
    assert r.status_code == 200
    assert r.json()["default_ai_functions_credential_id"] is None

    # ── Phase 5: Re-enable personal:openai; attempt to pin Anthropic cred → 400
    r = client.patch(
        f"{settings.API_V1_STR}/users/me",
        headers=headers,
        json={"default_ai_functions_sdk": "personal:openai"},
    )
    assert r.status_code == 200

    anthropic_cred = create_random_ai_credential(
        client,
        headers,
        credential_type="anthropic",
        api_key="sk-ant-api03-wrong-type-for-openai-sdk",
    )
    r = client.patch(
        f"{settings.API_V1_STR}/users/me",
        headers=headers,
        json={"default_ai_functions_credential_id": anthropic_cred["id"]},
    )
    assert r.status_code == 400, (
        f"Expected 400 pinning Anthropic cred with personal:openai SDK, got {r.status_code}: {r.text}"
    )
    detail = r.json().get("detail", "")
    assert "openai" in detail.lower(), (
        f"Expected 'openai' in error detail, got: {detail!r}"
    )

    # ── Phase 6: Pin the OpenAI credential again — 200 ───────────────────
    r = client.patch(
        f"{settings.API_V1_STR}/users/me",
        headers=headers,
        json={"default_ai_functions_credential_id": openai_cred_id},
    )
    assert r.status_code == 200, f"Expected 200 re-pinning OpenAI cred, got {r.status_code}: {r.text}"
    assert r.json()["default_ai_functions_credential_id"] == openai_cred_id

    # ── Phase 7: Switch to personal:anthropic; OpenAI cred now invalid → 400
    r = client.patch(
        f"{settings.API_V1_STR}/users/me",
        headers=headers,
        json={
            "default_ai_functions_sdk": "personal:anthropic",
            "default_ai_functions_credential_id": openai_cred_id,
        },
    )
    assert r.status_code == 400, (
        f"Expected 400 pinning OpenAI cred with personal:anthropic SDK, got {r.status_code}: {r.text}"
    )
    detail = r.json().get("detail", "")
    assert "anthropic" in detail.lower(), (
        f"Expected 'anthropic' in error detail, got: {detail!r}"
    )


def test_personal_openai_sdk_is_valid_option(
    client: TestClient,
) -> None:
    """
    Verify that "personal:openai" is accepted as a valid AI functions SDK value
    alongside "system" and "personal:anthropic", and that it round-trips correctly
    through the API without being rejected as invalid.
    """
    _, headers = create_random_user_with_headers(client)

    for valid_sdk in ["system", "personal:anthropic", "personal:openai"]:
        r = client.patch(
            f"{settings.API_V1_STR}/users/me",
            headers=headers,
            json={"default_ai_functions_sdk": valid_sdk},
        )
        assert r.status_code == 200, (
            f"Expected 200 for valid SDK {valid_sdk!r}, got {r.status_code}: {r.text}"
        )
        assert r.json()["default_ai_functions_sdk"] == valid_sdk


def test_auto_set_personal_openai_on_first_openai_credential(
    client: TestClient,
) -> None:
    """
    When the first OpenAI (type="openai") credential is created via the onboarding
    AI credentials update endpoint (PATCH /users/me/ai-credentials), and the user
    has no personal AI functions preference (still "system"), the system auto-sets
    the SDK preference to "personal:openai".

      1. New user starts with "system" AI functions SDK
      2. Submit openai_api_key via PATCH /users/me/ai-credentials
      3. Verify default_ai_functions_sdk is now "personal:openai"
    """
    _, headers = create_random_user_with_headers(client)

    # ── Phase 1: New user starts with "system" ────────────────────────────
    r = client.get(f"{settings.API_V1_STR}/users/me", headers=headers)
    assert r.status_code == 200
    assert r.json()["default_ai_functions_sdk"] == "system"

    # ── Phase 2: Submit OpenAI key via onboarding endpoint ───────────────
    r = client.patch(
        f"{settings.API_V1_STR}/users/me/ai-credentials",
        headers=headers,
        json={"openai_api_key": "sk-openai-auto-set-test-key"},
    )
    assert r.status_code == 200, f"Expected 200 from ai-credentials update, got {r.status_code}: {r.text}"

    # ── Phase 3: Verify auto-set to "personal:openai" ────────────────────
    r = client.get(f"{settings.API_V1_STR}/users/me", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert data["default_ai_functions_sdk"] == "personal:openai", (
        f"Expected auto-set to 'personal:openai' after first OpenAI credential, "
        f"got {data['default_ai_functions_sdk']!r}"
    )


def test_auto_set_anthropic_takes_priority_over_openai_when_both_submitted(
    client: TestClient,
) -> None:
    """
    When both anthropic_api_key and openai_api_key are submitted in the same
    onboarding request, the Anthropic auto-set fires first and sets the SDK to
    "personal:anthropic". The OpenAI auto-set check then sees a personal preference
    already set and skips, so the final value remains "personal:anthropic".

      1. New user starts with "system" SDK
      2. Submit both anthropic_api_key and openai_api_key in one request
      3. Verify default_ai_functions_sdk is "personal:anthropic" (Anthropic wins)
    """
    _, headers = create_random_user_with_headers(client)

    # ── Phase 1: Verify initial state ────────────────────────────────────
    r = client.get(f"{settings.API_V1_STR}/users/me", headers=headers)
    assert r.status_code == 200
    assert r.json()["default_ai_functions_sdk"] == "system"

    # ── Phase 2: Submit both keys in one request ──────────────────────────
    r = client.patch(
        f"{settings.API_V1_STR}/users/me/ai-credentials",
        headers=headers,
        json={
            "anthropic_api_key": "sk-ant-api03-both-keys-test",
            "openai_api_key": "sk-openai-both-keys-test",
        },
    )
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"

    # ── Phase 3: Anthropic preference wins ───────────────────────────────
    r = client.get(f"{settings.API_V1_STR}/users/me", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert data["default_ai_functions_sdk"] == "personal:anthropic", (
        f"Expected 'personal:anthropic' to win when both keys submitted, "
        f"got {data['default_ai_functions_sdk']!r}"
    )


def test_auto_set_openai_skipped_when_personal_preference_already_set(
    client: TestClient,
) -> None:
    """
    When the user already has a personal AI functions preference set (e.g.,
    "personal:anthropic"), submitting an openai_api_key via onboarding should NOT
    override the existing preference.

      1. User sets SDK to "personal:anthropic" manually
      2. Submit openai_api_key via onboarding endpoint
      3. Verify default_ai_functions_sdk remains "personal:anthropic"
    """
    _, headers = create_random_user_with_headers(client)

    # ── Phase 1: Set personal preference to "personal:anthropic" ─────────
    r = client.patch(
        f"{settings.API_V1_STR}/users/me",
        headers=headers,
        json={"default_ai_functions_sdk": "personal:anthropic"},
    )
    assert r.status_code == 200
    assert r.json()["default_ai_functions_sdk"] == "personal:anthropic"

    # ── Phase 2: Submit openai_api_key via onboarding ─────────────────────
    r = client.patch(
        f"{settings.API_V1_STR}/users/me/ai-credentials",
        headers=headers,
        json={"openai_api_key": "sk-openai-should-not-override"},
    )
    assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"

    # ── Phase 3: Personal preference unchanged ────────────────────────────
    r = client.get(f"{settings.API_V1_STR}/users/me", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert data["default_ai_functions_sdk"] == "personal:anthropic", (
        f"Expected personal:anthropic to remain unchanged, "
        f"got {data['default_ai_functions_sdk']!r}"
    )
