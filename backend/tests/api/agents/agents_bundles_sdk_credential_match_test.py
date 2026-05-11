"""SDK ↔ AI credential type alignment — agent bundle tests.

A publisher who pairs an OpenAI credential with a conversation SDK of
``opencode/anthropic`` would ship a bundle that writes the OpenAI key into
``provider.anthropic.options.apiKey`` at install time, then fails at
runtime with HTTP 401 from ``api.anthropic.com``.

These tests pin the three layers of validation that block that mismatch:

  1. ``PATCH /bundles/{uuid}`` rejects a publisher AI credential whose
     ``type`` doesn't match the publisher install's env SDK provider
     (covers conversation + building, and accepts matching pairs).
  2. ``PATCH /agents/{id}/publish-settings`` (pre-publish draft) rejects
     an AI credential whose type doesn't match the env's SDK.
  3. ``POST /agents/{id}/environments`` rejects an env CREATE that pairs
     an SDK provider with a credential of a different type — the root
     cause check that also protects rebuilds via the same code path.

The happy paths (matching types) keep working — exercised by the
existing ``agents_bundles_install_credentials_test.py`` suite, plus a
smoke case in :func:`test_bundle_patch_accepts_matching_credential_type`.

The default test env runs ``claude-code/anthropic`` for both modes (set
by ``EnvironmentService.create_environment`` when no SDK is supplied,
falling through to ``sdk_constants.DEFAULT_SDK``). Mismatch scenarios
exploit that default by trying to pair it with an ``openai``-typed
credential.
"""
import uuid

from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks

API = settings.API_V1_STR


# ── Helpers ──────────────────────────────────────────────────────────────────


def _publish(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
) -> dict:
    """Publish an agent and return the refreshed agent row."""
    r = client.post(
        f"{API}/agents/{agent_id}/publish",
        headers=headers,
        json={},
    )
    assert r.status_code == 200, r.text
    drain_tasks()
    return client.get(f"{API}/agents/{agent_id}", headers=headers).json()


# ── Scenario 1: PATCH /bundles rejects mismatched conversation credential ────


def test_bundle_patch_rejects_openai_cred_with_anthropic_sdk_conversation(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """PATCH /bundles/{uuid} rejects a publisher AI conversation credential
    whose ``type`` doesn't match the publisher install's conversation SDK.

    The default env's ``agent_sdk_conversation`` is ``claude-code/anthropic``
    (the global default applied when ``POST /agents/`` doesn't carry an
    explicit SDK), so an OpenAI-typed credential must be rejected.
    """
    publisher = create_agent_via_api(
        client, superuser_token_headers, name="SdkMatch-Patch-ConvMismatch"
    )
    drain_tasks()
    fresh = _publish(client, superuser_token_headers, publisher["id"])

    openai_cred = create_random_ai_credential(
        client,
        superuser_token_headers,
        credential_type="openai",
        api_key="sk-openai-test-key",
    )

    r = client.patch(
        f"{API}/bundles/{fresh['bundle_uuid']}",
        headers=superuser_token_headers,
        json={
            "publisher_ai_credential_conversation_id": openai_cred["id"],
        },
    )
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "conversation" in detail.lower()
    assert "anthropic" in detail.lower()


# ── Scenario 2: PATCH /bundles accepts matching credential type ──────────────


def test_bundle_patch_accepts_matching_credential_type(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """PATCH /bundles/{uuid} accepts publisher AI credentials whose type
    matches the env's per-mode SDK provider.

    Default env SDKs are both ``claude-code/anthropic`` so two
    Anthropic-typed credentials sail through.
    """
    publisher = create_agent_via_api(
        client, superuser_token_headers, name="SdkMatch-Patch-Match"
    )
    drain_tasks()
    fresh = _publish(client, superuser_token_headers, publisher["id"])

    conv_cred = create_random_ai_credential(
        client, superuser_token_headers, credential_type="anthropic"
    )
    build_cred = create_random_ai_credential(
        client, superuser_token_headers, credential_type="anthropic"
    )

    r = client.patch(
        f"{API}/bundles/{fresh['bundle_uuid']}",
        headers=superuser_token_headers,
        json={
            "publisher_ai_credential_conversation_id": conv_cred["id"],
            "publisher_ai_credential_building_id": build_cred["id"],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["publisher_ai_credential_conversation_id"] == conv_cred["id"]
    assert body["publisher_ai_credential_building_id"] == build_cred["id"]


# ── Scenario 3: PATCH /bundles rejects mismatched building credential ────────


def test_bundle_patch_rejects_anthropic_cred_with_opencode_openai_building_sdk(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Same provider-match check for the building mode.

    The default agent creation leaves ``agent_sdk_building`` as ``None``
    (the simple ``POST /agents/`` flow treats unset building SDK as
    "not needed"), so the test creates a second environment with an
    explicit ``opencode/openai`` building SDK, activates it, and only
    then tries to wire an Anthropic-typed publisher AI credential —
    which must be rejected as a provider mismatch.
    """
    publisher = create_agent_via_api(
        client, superuser_token_headers, name="SdkMatch-Patch-BuildMismatch"
    )
    drain_tasks()
    fresh = _publish(client, superuser_token_headers, publisher["id"])

    openai_default = create_random_ai_credential(
        client,
        superuser_token_headers,
        credential_type="openai",
        api_key="sk-openai-test-key",
        set_default=True,
    )

    create_env = client.post(
        f"{API}/agents/{publisher['id']}/environments",
        headers=superuser_token_headers,
        json={
            "env_name": "python-env-advanced",
            "env_version": "1.0.0",
            "instance_name": "OpenAI Building",
            "type": "docker",
            "config": {},
            "agent_sdk_conversation": "opencode/openai",
            "agent_sdk_building": "opencode/openai",
            "use_default_ai_credentials": False,
            "conversation_ai_credential_id": openai_default["id"],
            "building_ai_credential_id": openai_default["id"],
        },
    )
    assert create_env.status_code == 200, create_env.text
    new_env_id = create_env.json()["id"]
    drain_tasks()

    activate = client.post(
        f"{API}/agents/{publisher['id']}/environments/{new_env_id}/activate",
        headers=superuser_token_headers,
    )
    assert activate.status_code == 200, activate.text
    drain_tasks()

    anthropic_cred = create_random_ai_credential(
        client,
        superuser_token_headers,
        credential_type="anthropic",
    )

    r = client.patch(
        f"{API}/bundles/{fresh['bundle_uuid']}",
        headers=superuser_token_headers,
        json={
            "publisher_ai_credential_building_id": anthropic_cred["id"],
        },
    )
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "building" in detail.lower()
    assert "opencode/openai" in detail


# ── Scenario 4: Pre-publish draft validation ─────────────────────────────────


def test_publish_settings_draft_rejects_mismatched_credential(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """PATCH /agents/{id}/publish-settings rejects an AI credential
    whose type doesn't match the env SDK.

    The draft is stored on the publisher install and replays onto the
    bundle row at the next publish, so this is the same SDK-match check
    as the post-publish bundle PATCH but flowing through a different
    route. The route requires ``is_publisher_install=True`` (set on
    first publish), so we publish once before mutating the draft.
    """
    publisher = create_agent_via_api(
        client, superuser_token_headers, name="SdkMatch-Draft-Mismatch"
    )
    drain_tasks()
    _publish(client, superuser_token_headers, publisher["id"])

    openai_cred = create_random_ai_credential(
        client,
        superuser_token_headers,
        credential_type="openai",
        api_key="sk-openai-test-key",
    )

    r = client.patch(
        f"{API}/agents/{publisher['id']}/publish-settings",
        headers=superuser_token_headers,
        json={
            "ai_credentials": {
                "conversation_credential_id": openai_cred["id"],
                "building_credential_id": None,
            }
        },
    )
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "conversation" in detail.lower()
    assert "anthropic" in detail.lower()


# ── Scenario 5: Pre-publish draft accepts matching credential ────────────────


def test_publish_settings_draft_accepts_matching_credential(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """The draft validator must not over-reject — anthropic-typed
    credentials match the default env SDK and are accepted.
    """
    publisher = create_agent_via_api(
        client, superuser_token_headers, name="SdkMatch-Draft-Match"
    )
    drain_tasks()
    _publish(client, superuser_token_headers, publisher["id"])

    cred = create_random_ai_credential(
        client, superuser_token_headers, credential_type="anthropic"
    )

    r = client.patch(
        f"{API}/agents/{publisher['id']}/publish-settings",
        headers=superuser_token_headers,
        json={
            "ai_credentials": {
                "conversation_credential_id": cred["id"],
                "building_credential_id": cred["id"],
            }
        },
    )
    assert r.status_code == 200, r.text


# ── Scenario 6: Env create rejects mismatched AI credential ──────────────────


def test_env_create_rejects_openai_credential_for_opencode_anthropic_sdk(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """POST /agents/{id}/environments rejects a conversation AI credential
    whose ``type`` doesn't match the requested SDK provider.

    Before this fix, the engine-only compatibility matrix accepted any of
    ``anthropic``, ``openai``, ``openai_compatible``, ``google`` for the
    ``opencode`` engine — so ``opencode/anthropic`` + OpenAI credential
    silently passed and ended up writing an OpenAI key into the Anthropic
    provider slot at runtime. The strict full-SDK match rejects it.
    """
    publisher = create_agent_via_api(
        client, superuser_token_headers, name="SdkMatch-EnvCreate"
    )
    drain_tasks()

    openai_cred = create_random_ai_credential(
        client,
        superuser_token_headers,
        credential_type="openai",
        api_key="sk-openai-test-key",
    )

    r = client.post(
        f"{API}/agents/{publisher['id']}/environments",
        headers=superuser_token_headers,
        json={
            "env_name": "python-env-advanced",
            "env_version": "1.0.0",
            "instance_name": "OpenAI on Anthropic SDK",
            "type": "docker",
            "config": {},
            "agent_sdk_conversation": "opencode/anthropic",
            "agent_sdk_building": "claude-code/anthropic",
            "use_default_ai_credentials": False,
            "conversation_ai_credential_id": openai_cred["id"],
        },
    )
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert "opencode/anthropic" in detail
    assert "openai" in detail.lower()
