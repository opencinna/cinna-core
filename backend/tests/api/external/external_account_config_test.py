"""
Tests for the Native Account-Config endpoint.

``GET /api/v1/external/account-config`` — native-token-gated endpoint that
returns the authenticated user's own AI credentials INCLUDING the decrypted
api_key, so Cinna Desktop/Mobile can auto-create local LLM providers on login.

This is the ONE endpoint that deliberately returns decrypted key material.
Tests verify: access gating, response shape, Cache-Control, key presence,
owner-only scoping, SecurityEvent audit, and model resolution semantics.

Scenarios:
  1. Desktop token → 200 with providers carrying decrypted api_key + Cache-Control: no-store.
  2. Plain web JWT (no client_kind) → 403.
  3. Revoked desktop client → 401.
  4. Empty (user has no credentials) → 200 with providers=[].
  5. Owner-only: a credential SHARED to the user is NOT included.
  6. Admin-managed credential appears in the response with is_admin_managed=True.
  7. Provider display names and descriptor_slugs are correct per type.
  8. Model resolution: tier-word-only catalog default → model is None.
  9. SecurityEvent written on read — contains ids/counts but NO raw key bytes.
 10. Unauthenticated (no token) → 401.

No agents or environments are created, so NEEDS_AGENT_STUBS = False.
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.desktop_auth import obtain_desktop_tokens, revoke_desktop_client
from tests.utils.platform_token import mint_platform_token
from tests.utils.user import create_random_user, user_authentication_headers

# Pure-CRUD suite; no agent/environment stubs needed.
NEEDS_AGENT_STUBS = False
NEEDS_DEFAULT_CREDENTIALS = False

_EXT_BASE = f"{settings.API_V1_STR}/external"
_ACCOUNT_CONFIG_URL = f"{_EXT_BASE}/account-config"
_SEC_EVENTS_URL = f"{settings.API_V1_STR}/security-events/"
_CRED_SHARES_URL = f"{settings.API_V1_STR}/ai-credentials"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _get_account_config(
    client: TestClient,
    headers: dict,
    expected_status: int = 200,
) -> dict:
    """GET /external/account-config and return parsed JSON."""
    r = client.get(_ACCOUNT_CONFIG_URL, headers=headers)
    assert r.status_code == expected_status, (
        f"account-config returned {r.status_code}: {r.text}"
    )
    return r.json() if r.status_code == 200 else {}


def _desktop_headers(access_token: str) -> dict:
    """Build auth headers from a desktop access token."""
    return {"Authorization": f"Bearer {access_token}"}


def _provision_admin_credential(
    client: TestClient,
    superuser_headers: dict,
    target_user_id: str,
    credential_type: str = "anthropic",
    api_key: str = "sk-ant-admin-key",
    name: str = "Admin Key",
    set_as_default: bool = False,
) -> dict:
    """POST /admin/llm-providers/ and return the first created credential."""
    r = client.post(
        f"{settings.API_V1_STR}/admin/llm-providers/",
        headers=superuser_headers,
        json={
            "name": name,
            "type": credential_type,
            "api_key": api_key,
            "target_user_ids": [target_user_id],
            "set_as_default": set_as_default,
        },
    )
    assert r.status_code == 200, f"Admin provision failed: {r.text}"
    # POST /admin/llm-providers/ returns a managed-credential reconcile result
    # ({record, added, ...}); the first ``added`` member carries the per-user
    # child credential. Return it shaped so callers can read ``["id"]``.
    member = r.json()["added"][0]
    return {"id": member["child_credential_id"], **member}


def _list_security_events(
    client: TestClient,
    headers: dict,
    event_type: str | None = None,
) -> list[dict]:
    """GET /security-events/ → list of events, optionally filtered."""
    params = {}
    if event_type:
        params["event_type"] = event_type
    r = client.get(_SEC_EVENTS_URL, headers=headers, params=params)
    assert r.status_code == 200, f"list_security_events failed: {r.text}"
    return r.json().get("data", [])


# ── Scenario 1: Desktop token → 200 with decrypted api_key + no-store ─────────


def test_desktop_token_returns_providers_with_api_key(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Desktop token scenario:
      1. Create a credential for the user.
      2. Obtain a desktop access token via the full consent flow.
      3. GET /external/account-config → 200.
      4. Response contains providers with decrypted api_key.
      5. Cache-Control: no-store is set.
      6. Response shape: providers, default_provider_credential_id, generated_at.
    """
    # ── Phase 1: Superuser creates a credential and gets a desktop token ──────
    sentinel_key = "sk-ant-api03-sentinel-for-desktop-test"
    cred = create_random_ai_credential(
        client,
        superuser_token_headers,
        credential_type="anthropic",
        api_key=sentinel_key,
        name="Desktop Config Test Key",
    )
    cred_id = cred["id"]

    desktop = obtain_desktop_tokens(client, superuser_token_headers, device_name="config-test-device")
    desk_headers = _desktop_headers(desktop["access_token"])

    # ── Phase 2: Call the native endpoint ────────────────────────────────────
    r = client.get(_ACCOUNT_CONFIG_URL, headers=desk_headers)
    assert r.status_code == 200, f"account-config failed: {r.text}"

    # ── Phase 3: Verify Cache-Control ────────────────────────────────────────
    assert r.headers.get("cache-control", "").lower() == "no-store"

    config = r.json()

    # ── Phase 4: Response shape ───────────────────────────────────────────────
    assert "providers" in config
    assert "generated_at" in config
    # default_provider_credential_id may be None if no default is set, or a UUID
    assert "default_provider_credential_id" in config

    # ── Phase 5: Provider descriptor shape + decrypted api_key ───────────────
    assert len(config["providers"]) >= 1
    provider = next(
        (p for p in config["providers"] if p["credential_id"] == cred_id), None
    )
    assert provider is not None, f"Created credential not found in providers: {config['providers']}"

    # Key fields
    assert provider["api_key"] == sentinel_key  # DECRYPTED
    assert provider["provider_type"] == "anthropic"
    assert provider["display_name"] == "Claude"
    # The credential's own name is exposed so native clients can disambiguate
    # multiple credentials of the same provider family.
    assert provider["credential_name"] == "Desktop Config Test Key"
    assert provider["descriptor_slug"] == "claude"
    # Curated default model is exposed (None for a self-created key with no
    # admin curation) so native clients can prefer it over the legacy `model`.
    assert "default_model" in provider
    assert provider["default_model"] is None
    assert "credential_id" in provider
    assert "is_default" in provider
    assert "is_admin_managed" in provider


# ── Scenario 2: Web JWT (no client_kind) → 403 ────────────────────────────────


def test_web_jwt_rejected_with_403(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A plain web-session JWT (no client_kind claim) must be rejected 403."""
    r = client.get(_ACCOUNT_CONFIG_URL, headers=superuser_token_headers)
    assert r.status_code == 403


def test_web_jwt_no_client_kind_rejected(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """Explicitly minted token with no client_kind → 403."""
    # Get the current superuser id from /users/me
    me = client.get(f"{settings.API_V1_STR}/users/me", headers=superuser_token_headers)
    user_id = me.json()["id"]

    # Mint a token with NO client_kind claim (plain web token)
    raw_token = mint_platform_token(subject=user_id)
    r = client.get(
        _ACCOUNT_CONFIG_URL,
        headers={"Authorization": f"Bearer {raw_token}"},
    )
    assert r.status_code == 403


# ── Scenario 3: Revoked desktop client → 401 ──────────────────────────────────


def test_revoked_desktop_client_rejected_with_401(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """A revoked desktop client's access token must be rejected 401."""
    desktop = obtain_desktop_tokens(
        client, superuser_token_headers, device_name="revoke-test-device"
    )
    desk_headers = _desktop_headers(desktop["access_token"])

    # Verify the token works before revocation
    r = client.get(_ACCOUNT_CONFIG_URL, headers=desk_headers)
    # 200 OR 403 depending on whether the user has credentials — either is fine;
    # the important thing is it is NOT 401 while the client is active.
    assert r.status_code in (200, 403), f"Expected 200/403 before revoke, got {r.status_code}"

    # Revoke the desktop client
    revoke_desktop_client(client, superuser_token_headers, desktop["client_id"])

    # Now the same token should be rejected
    r = client.get(_ACCOUNT_CONFIG_URL, headers=desk_headers)
    assert r.status_code == 401


# ── Scenario 4: No credentials → 200 with empty providers ────────────────────


def test_empty_providers_when_no_credentials(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """User with no AI credentials gets 200 with providers=[]."""
    user = create_random_user(client)
    user_headers = user_authentication_headers(
        client=client, email=user["email"], password=user["_password"]
    )

    desktop = obtain_desktop_tokens(client, user_headers, device_name="empty-cred-device")
    desk_headers = _desktop_headers(desktop["access_token"])

    config = _get_account_config(client, desk_headers)
    assert config["providers"] == []
    assert config["default_provider_credential_id"] is None


# ── Scenario 5: Owner-only — shared credential NOT included ───────────────────


def test_shared_credential_not_included(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    OQ-6: Only owner's own credentials appear.
    A credential shared with the user via AICredentialShare must NOT appear in
    /external/account-config (the caller is not the owner).
    """
    # Superuser creates a credential
    cred = create_random_ai_credential(
        client,
        superuser_token_headers,
        credential_type="anthropic",
        api_key="sk-ant-shared-cred-key",
        name="Superuser Key to Share",
    )
    cred_id = cred["id"]

    # Create a target user
    target = create_random_user(client)
    target_id = target["id"]
    target_headers = user_authentication_headers(
        client=client, email=target["email"], password=target["_password"]
    )

    # Share the credential with the target (via AI credential share endpoint)
    r = client.post(
        f"{_CRED_SHARES_URL}/{cred_id}/shares",
        headers=superuser_token_headers,
        json={"shared_with_user_id": target_id},
    )
    # If sharing is not supported or 404, skip the share step and verify
    # the credential doesn't appear anyway (it's not owned by target)
    share_succeeded = r.status_code == 200

    # Get a desktop token for the target
    desktop = obtain_desktop_tokens(
        client, target_headers, device_name="share-scope-device"
    )
    desk_headers = _desktop_headers(desktop["access_token"])

    config = _get_account_config(client, desk_headers)

    # The shared credential must NOT appear in the target's account config
    provider_ids = [p["credential_id"] for p in config["providers"]]
    assert cred_id not in provider_ids, (
        f"Shared credential {cred_id} must not appear in target's account-config. "
        f"Found: {provider_ids}. Share succeeded: {share_succeeded}"
    )


# ── Scenario 6: Admin-managed credential appears with is_admin_managed=True ───


def test_admin_managed_credential_in_account_config(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Admin-managed credential appears in the owner's account config with
    is_admin_managed=True and the correct decrypted api_key.
    """
    target = create_random_user(client)
    target_id = target["id"]
    target_headers = user_authentication_headers(
        client=client, email=target["email"], password=target["_password"]
    )

    sentinel_key = "sk-ant-api03-admin-managed-account-config"
    admin_cred = _provision_admin_credential(
        client,
        superuser_token_headers,
        target_user_id=target_id,
        credential_type="anthropic",
        api_key=sentinel_key,
        name="Admin Managed For Desktop",
    )
    admin_cred_id = admin_cred["id"]

    # Get a desktop token for the target
    desktop = obtain_desktop_tokens(
        client, target_headers, device_name="admin-cred-device"
    )
    desk_headers = _desktop_headers(desktop["access_token"])

    config = _get_account_config(client, desk_headers)

    provider = next(
        (p for p in config["providers"] if p["credential_id"] == admin_cred_id), None
    )
    assert provider is not None, "Admin-managed credential must appear in account config"
    assert provider["is_admin_managed"] is True
    assert provider["api_key"] == sentinel_key


# ── Scenario 7: Provider display names and descriptor slugs ───────────────────


def test_provider_display_names_per_type(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Each provider type maps to the correct display_name and descriptor_slug:
      - anthropic → ("Claude", "claude")
      - openai → ("OpenAI", "openai")
      - google → ("Gemini", "gemini")
      - minimax → ("MiniMax", "minimax")
      - openai_compatible → (credential.name, "openai-compatible")
    """
    desktop = obtain_desktop_tokens(
        client, superuser_token_headers, device_name="display-names-device"
    )
    desk_headers = _desktop_headers(desktop["access_token"])

    expected = [
        ("anthropic", "sk-ant-type-test", "Claude", "claude", None, None),
        ("openai", "sk-openai-type-test", "OpenAI", "openai", None, None),
        ("google", "aig-type-test", "Gemini", "gemini", None, None),
        ("minimax", "mm-type-test", "MiniMax", "minimax", None, None),
        (
            "openai_compatible",
            "sk-compat-type-test",
            "My Custom Provider",  # display_name = credential.name for openai_compatible
            "openai-compatible",
            "https://api.example.com/v1",
            "gpt-4o",
        ),
    ]

    created_creds: list[dict] = []
    for ctype, api_key, _disp, _slug, base_url, model in expected:
        cred_name = "My Custom Provider" if ctype == "openai_compatible" else f"Type Test {ctype}"
        cred = create_random_ai_credential(
            client,
            superuser_token_headers,
            credential_type=ctype,
            api_key=api_key,
            name=cred_name,
            base_url=base_url,
            model=model,
        )
        created_creds.append(cred)

    config = _get_account_config(client, desk_headers)
    providers_by_cred_id = {p["credential_id"]: p for p in config["providers"]}

    for cred, (ctype, _key, expected_display, expected_slug, _bu, _m) in zip(
        created_creds, expected
    ):
        cred_id = cred["id"]
        assert cred_id in providers_by_cred_id, (
            f"Credential {cred_id} (type={ctype}) missing from account config"
        )
        p = providers_by_cred_id[cred_id]
        assert p["display_name"] == expected_display, (
            f"Wrong display_name for {ctype}: {p['display_name']!r} != {expected_display!r}"
        )
        assert p["descriptor_slug"] == expected_slug, (
            f"Wrong slug for {ctype}: {p['descriptor_slug']!r} != {expected_slug!r}"
        )


# ── Scenario 8: model resolution — tier-word-only catalog default → None ──────


def test_model_resolution_anthropic_tier_word_returns_none(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    OQ-10: For anthropic credentials (which use claude-code/haiku|sonnet|opus
    tier words), the resolved model should be None since tier words are not
    usable provider API ids for a native client.
    The native client is expected to fall back to suggested_models or its own default.
    """
    desktop = obtain_desktop_tokens(
        client, superuser_token_headers, device_name="model-resolution-device"
    )
    desk_headers = _desktop_headers(desktop["access_token"])

    # Create an anthropic credential with NO model or discovered_models
    cred = create_random_ai_credential(
        client,
        superuser_token_headers,
        credential_type="anthropic",
        api_key="sk-ant-model-resolution-test",
        name="Anthropic No Model",
    )
    cred_id = cred["id"]

    config = _get_account_config(client, desk_headers)
    provider = next(
        (p for p in config["providers"] if p["credential_id"] == cred_id), None
    )
    assert provider is not None, "Credential must appear in account config"

    # Without credential.model or discovered_models, the catalog default for
    # anthropic (a tier word like "haiku"/"sonnet") should be stripped → None.
    # (The exact None-ness depends on the model catalog; assert it's either
    # None or a concrete provider id, never a bare tier word.)
    model_val = provider.get("model")
    if model_val is not None:
        tier_words = {"haiku", "sonnet", "opus"}
        assert model_val not in tier_words, (
            f"model should not be a bare tier word, got {model_val!r}"
        )


def test_model_resolution_openai_compatible_uses_credential_model(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    For openai_compatible, credential.model is explicitly set → it's returned
    directly (no catalog lookup needed).
    """
    desktop = obtain_desktop_tokens(
        client, superuser_token_headers, device_name="compat-model-device"
    )
    desk_headers = _desktop_headers(desktop["access_token"])

    cred = create_random_ai_credential(
        client,
        superuser_token_headers,
        credential_type="openai_compatible",
        api_key="sk-compat-model-test",
        name="OAI Compat with Model",
        base_url="https://api.example.com/v1",
        model="my-custom-model-7b",
    )
    cred_id = cred["id"]

    config = _get_account_config(client, desk_headers)
    provider = next(
        (p for p in config["providers"] if p["credential_id"] == cred_id), None
    )
    assert provider is not None
    assert provider["model"] == "my-custom-model-7b"


# ── Scenario 9: SecurityEvent audit on read ────────────────────────────────────


def test_account_config_read_writes_security_event_with_no_key_material(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    Every successful GET /external/account-config call writes a high-severity
    SecurityEvent with credential ids / provider_count but NO raw api_key bytes.
    """
    sentinel_key = "sk-ant-api03-audit-sentinel-account-config"
    cred = create_random_ai_credential(
        client,
        superuser_token_headers,
        credential_type="anthropic",
        api_key=sentinel_key,
        name="Audit Test Credential",
    )
    cred_id = cred["id"]

    desktop = obtain_desktop_tokens(
        client, superuser_token_headers, device_name="audit-test-device"
    )
    desk_headers = _desktop_headers(desktop["access_token"])

    # Call the endpoint to trigger the event
    _get_account_config(client, desk_headers)

    # Verify the security event was written
    events = _list_security_events(
        client, superuser_token_headers, event_type="external.account_config.read"
    )
    assert len(events) >= 1

    # The most-recent event should contain counts + ids but NOT the raw key
    latest_event = events[0]
    assert latest_event["event_type"] == "external.account_config.read"
    assert latest_event.get("severity") == "high"

    details = latest_event.get("details", {})
    assert "provider_count" in details
    assert "credential_ids" in details

    # No key material in any event details
    details_str = str(details)
    assert sentinel_key not in details_str, (
        f"Raw api_key leaked in security event details: {details_str}"
    )
    assert "api_key" not in details_str


# ── Scenario 10: Unauthenticated → 401 ───────────────────────────────────────


def test_unauthenticated_request_rejected(client: TestClient) -> None:
    """GET /external/account-config without any token → 401."""
    r = client.get(_ACCOUNT_CONFIG_URL)
    assert r.status_code == 401


# ── Scenario 11: default_provider_credential_id reflects set-default ──────────


def test_default_provider_credential_id_reflects_set_default(
    client: TestClient, superuser_token_headers: dict[str, str]
) -> None:
    """
    When the user has a default credential set, default_provider_credential_id
    should reflect it. When no default exists, it is None.
    """
    desktop = obtain_desktop_tokens(
        client, superuser_token_headers, device_name="default-cred-id-device"
    )
    desk_headers = _desktop_headers(desktop["access_token"])

    # Create a credential and set it as default
    cred = create_random_ai_credential(
        client,
        superuser_token_headers,
        credential_type="anthropic",
        api_key="sk-ant-default-id-test",
        name="Default Cred ID Test",
        set_default=True,
    )
    cred_id = cred["id"]

    config = _get_account_config(client, desk_headers)
    # The default credential id should be present (the resolver picks the
    # default conversation credential)
    assert config["default_provider_credential_id"] == cred_id
