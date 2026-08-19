"""Regression: bundle install must propagate the publisher's per-mode model overrides.

``InstallService._install_from_revision`` (backend/app/services/bundles/install_service.py)
now forwards ``revision.model_override_conversation`` / ``revision.model_override_building``
into the ``AgentEnvironmentCreate`` it builds for the new install. Previously both fields
were omitted from that call and silently fell back to ``None``, even though publish had
already captured the publisher's per-mode overrides onto the ``AgentBundleRevision`` row
(via ``RevisionFormat.build_manifest`` / ``manifest_to_revision_fields``). Every other
fixture across the bundle test suite sets these fields to ``None``, so nothing previously
exercised the non-null path.

``_install_from_revision`` is the sole bundle-install ``AgentEnvironmentCreate`` call site
(catalog install and git checkout both route through it) — the create-flow path in
``agent_service.py`` is a separate, unaffected call site covered by
``agents_create_flow_test.py``.

Scenario:
  1. Publisher's agent environment is reconfigured with non-null per-mode model
     overrides.
  2. Publish → the revision response carries both non-null overrides.
  3. Bundle is made public.
  4. A different user installs the bundle from the catalog.
  5. The installed agent's environment carries the same non-null overrides
     (regression: previously always ``None``).

A second regression is pinned below: ``InstallService._importable_model_override``
(``install_service.py`` ~L223) drops an imported override for any mode whose
*effective* SDK resolves to the ``openai_compatible`` provider — an
``openai_compatible`` model id names a model inside the endpoint owner's own
namespace (meaningful only against that credential's ``base_url``), so a
publisher-pinned id is not portable to a consumer's own ``openai_compatible``
credential/endpoint. ``model_health_service`` reports ``openai_compatible`` as
always healthy (the model namespace is assumed to belong to whoever owns the
credential), so an imported, non-portable pin would otherwise produce a hard
provider error at first message behind a green health badge. The suppression
is applied per mode independently at the ``_install_from_revision`` call site
(~L364) via ``_importable_model_override(override, effective_sdk)``, using the
mode's SDK id (``"engine/provider"``) — not the credential — to determine the
provider.
"""
from fastapi.testclient import TestClient

from app.core.config import settings
from tests.utils.agent import create_agent_via_api, get_agent
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks
from tests.utils.bundle import (
    install_bundle as _install,
    make_bundle_public as _make_public,
    make_user_and_headers as _make_user_and_headers,
    publish_bundle_revision as _publish_revision,
)
from tests.utils.environment import get_environment
from tests.utils.user import create_random_user, user_authentication_headers

API = settings.API_V1_STR

_MODEL_OVERRIDE_BUILDING = "claude-opus-4"
_MODEL_OVERRIDE_CONVERSATION = "claude-haiku-4-5"

# Model ids scoped to the publisher's own openai_compatible endpoint —
# meaningless once carried over to a consumer's different endpoint/catalogue.
_MODEL_OVERRIDE_OPENAI_COMPATIBLE = "publisher-namespace/custom-model-v1"
_MODEL_OVERRIDE_OPENAI_COMPATIBLE_2 = "publisher-namespace/custom-model-v2"


def _reconfigure_model_overrides(
    client: TestClient,
    headers: dict[str, str],
    env_id: str,
) -> dict:
    """POST /environments/{env_id}/reconfigure with non-null per-mode overrides.

    ``rebuild=False`` persists the config without kicking off a real rebuild —
    the same pattern used in ``tests/api/agent_environments/test_model_health_api.py``.
    """
    r = client.post(
        f"{API}/environments/{env_id}/reconfigure",
        headers=headers,
        json={
            "model_override_conversation": _MODEL_OVERRIDE_CONVERSATION,
            "model_override_building": _MODEL_OVERRIDE_BUILDING,
            "rebuild": False,
        },
    )
    assert r.status_code in (200, 202), (
        f"Reconfigure with model overrides failed: {r.status_code} {r.text}"
    )
    return r.json()


def test_install_propagates_publisher_model_overrides(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    1. Publisher's env gets non-null model overrides via reconfigure.
    2. Publish → revision response carries both non-null overrides.
    3. Bundle made public.
    4. A different user installs it from the catalog.
    5. Installed agent's environment carries the same non-null overrides.
    """
    headers = superuser_token_headers

    # ── Phase 1: Publisher agent with per-mode model overrides ───────────────
    agent = create_agent_via_api(client, headers, name="ModelOverride-Publisher")
    drain_tasks()
    publisher = get_agent(client, headers, agent["id"])
    env_id = publisher["active_environment_id"]
    assert env_id, "created agent must have an active environment"
    _reconfigure_model_overrides(client, headers, env_id)

    # ── Phase 2: Publish → revision carries the overrides ────────────────────
    revision = _publish_revision(client, headers, agent["id"])
    assert revision["model_override_conversation"] == _MODEL_OVERRIDE_CONVERSATION, (
        f"Revision must capture the publisher's conversation-mode override: {revision}"
    )
    assert revision["model_override_building"] == _MODEL_OVERRIDE_BUILDING, (
        f"Revision must capture the publisher's building-mode override: {revision}"
    )

    fresh = get_agent(client, headers, agent["id"])
    bundle_id = fresh["bundle_id"]
    bundle_uuid = fresh["bundle_uuid"]
    assert bundle_id and bundle_uuid, "Expected bundle identifiers after publish"

    # ── Phase 3: Make bundle public ───────────────────────────────────────────
    _make_public(client, headers, bundle_uuid)

    # ── Phase 4: A different user installs it from the catalog ───────────────
    _installer, installer_headers = _make_user_and_headers(client)
    installed = _install(client, installer_headers, bundle_id)
    installed_agent_id = installed["id"]

    # ── Phase 5: Installed environment carries the same overrides ────────────
    installed_agent = get_agent(client, installer_headers, installed_agent_id)
    installed_env_id = installed_agent["active_environment_id"]
    assert installed_env_id, "installed agent must have an active environment"
    installed_env = get_environment(client, installer_headers, installed_env_id)

    assert installed_env["model_override_conversation"] == _MODEL_OVERRIDE_CONVERSATION, (
        "Install must propagate the publisher's conversation-mode model override "
        f"(previously silently dropped to None); got {installed_env}"
    )
    assert installed_env["model_override_building"] == _MODEL_OVERRIDE_BUILDING, (
        "Install must propagate the publisher's building-mode model override "
        f"(previously silently dropped to None); got {installed_env}"
    )


def test_install_suppresses_model_override_for_openai_compatible_mode(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    An ``openai_compatible`` model id only means something against the
    credential's own ``base_url`` — it is not portable to a consumer's
    different endpoint/catalogue, so install must drop it.

    1. Publisher creates a second environment whose conversation SDK is
       ``opencode/openai_compatible``, paired with a matching credential and a
       non-null ``model_override_conversation``, and activates it.
    2. Publish → the revision carries the override *unfiltered* (publish
       itself does not filter; only install does — this pins that the
       suppression lives at the install boundary, not earlier).
    3. Bundle is made public.
    4. A different user (who needs their own ``openai_compatible`` default
       credential, since the revision forces that SDK) installs the bundle.
    5. The installed environment's ``model_override_conversation`` is ``None``
       — the pin did not travel — while the SDK itself still propagates
       normally.
    """
    headers = superuser_token_headers

    # ── Phase 1: Publisher env on opencode/openai_compatible with an override ─
    publisher = create_agent_via_api(
        client, headers, name="ModelOverride-OAICompat-Publisher"
    )
    drain_tasks()

    pub_cred = create_random_ai_credential(
        client,
        headers,
        credential_type="openai_compatible",
        base_url="https://publisher-endpoint.example.com/v1",
        model="publisher-default-model",
    )

    create_env = client.post(
        f"{API}/agents/{publisher['id']}/environments",
        headers=headers,
        json={
            "env_name": "python-env-advanced",
            "env_version": "1.0.0",
            "instance_name": "OpenAI Compatible",
            "type": "docker",
            "config": {},
            "agent_sdk_conversation": "opencode/openai_compatible",
            "use_default_ai_credentials": False,
            "conversation_ai_credential_id": pub_cred["id"],
            "model_override_conversation": _MODEL_OVERRIDE_OPENAI_COMPATIBLE,
        },
    )
    assert create_env.status_code == 200, create_env.text
    new_env_id = create_env.json()["id"]
    drain_tasks()

    activate = client.post(
        f"{API}/agents/{publisher['id']}/environments/{new_env_id}/activate",
        headers=headers,
    )
    assert activate.status_code == 200, activate.text
    drain_tasks()

    # ── Phase 2: Publish → revision carries the raw, unfiltered override ─────
    revision = _publish_revision(client, headers, publisher["id"])
    assert revision["agent_sdk_conversation"] == "opencode/openai_compatible"
    assert revision["model_override_conversation"] == _MODEL_OVERRIDE_OPENAI_COMPATIBLE, (
        "Publish must not filter — suppression is install-only: "
        f"{revision}"
    )

    fresh = get_agent(client, headers, publisher["id"])
    bundle_id = fresh["bundle_id"]
    bundle_uuid = fresh["bundle_uuid"]
    assert bundle_id and bundle_uuid, "Expected bundle identifiers after publish"

    # ── Phase 3: Make bundle public ───────────────────────────────────────────
    _make_public(client, headers, bundle_uuid)

    # ── Phase 4: A different user installs it — needs their own
    # openai_compatible default credential, since the revision's conversation
    # SDK (opencode/openai_compatible) is forced onto the installed env.
    installer = create_random_user(client)
    installer_headers = user_authentication_headers(
        client=client, email=installer["email"], password=installer["_password"]
    )
    create_random_ai_credential(
        client,
        installer_headers,
        credential_type="openai_compatible",
        base_url="https://installer-endpoint.example.com/v1",
        model="installer-default-model",
        set_default=True,
    )
    installed = _install(client, installer_headers, bundle_id)
    installed_agent_id = installed["id"]

    # ── Phase 5: Installed environment does NOT carry the override ───────────
    installed_agent = get_agent(client, installer_headers, installed_agent_id)
    installed_env_id = installed_agent["active_environment_id"]
    assert installed_env_id, "installed agent must have an active environment"
    installed_env = get_environment(client, installer_headers, installed_env_id)

    assert installed_env["agent_sdk_conversation"] == "opencode/openai_compatible", (
        "The SDK itself must still propagate — only the override is suppressed: "
        f"{installed_env}"
    )
    assert installed_env["model_override_conversation"] is None, (
        "openai_compatible model ids are endpoint-local; a publisher's pin must "
        f"NOT travel to the consumer's own endpoint. Got {installed_env}"
    )


def test_install_suppresses_only_the_openai_compatible_mode(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Per-mode independence: one mode resolving to ``openai_compatible`` must
    not suppress the override on the *other* mode.

    The publisher's active environment pairs a plain ``claude-code/anthropic``
    conversation mode (override kept) with an ``opencode/openai_compatible``
    building mode (override dropped) on the SAME environment row — proving
    ``_importable_model_override`` is applied independently per mode at the
    ``_install_from_revision`` call site, not once for the whole install.
    """
    headers = superuser_token_headers

    # ── Phase 1: Publisher env with mixed per-mode SDKs + overrides ──────────
    publisher = create_agent_via_api(
        client, headers, name="ModelOverride-MixedSdk-Publisher"
    )
    drain_tasks()

    anthropic_cred = create_random_ai_credential(
        client, headers, credential_type="anthropic"
    )
    openai_compat_cred = create_random_ai_credential(
        client,
        headers,
        credential_type="openai_compatible",
        base_url="https://publisher-endpoint.example.com/v1",
        model="publisher-default-model",
    )

    create_env = client.post(
        f"{API}/agents/{publisher['id']}/environments",
        headers=headers,
        json={
            "env_name": "python-env-advanced",
            "env_version": "1.0.0",
            "instance_name": "Mixed SDK",
            "type": "docker",
            "config": {},
            "agent_sdk_conversation": "claude-code/anthropic",
            "agent_sdk_building": "opencode/openai_compatible",
            "use_default_ai_credentials": False,
            "conversation_ai_credential_id": anthropic_cred["id"],
            "building_ai_credential_id": openai_compat_cred["id"],
            "model_override_conversation": _MODEL_OVERRIDE_CONVERSATION,
            "model_override_building": _MODEL_OVERRIDE_OPENAI_COMPATIBLE,
        },
    )
    assert create_env.status_code == 200, create_env.text
    new_env_id = create_env.json()["id"]
    drain_tasks()

    activate = client.post(
        f"{API}/agents/{publisher['id']}/environments/{new_env_id}/activate",
        headers=headers,
    )
    assert activate.status_code == 200, activate.text
    drain_tasks()

    # ── Phase 2: Publish → revision carries both raw, unfiltered overrides ───
    revision = _publish_revision(client, headers, publisher["id"])
    assert revision["model_override_conversation"] == _MODEL_OVERRIDE_CONVERSATION
    assert revision["model_override_building"] == _MODEL_OVERRIDE_OPENAI_COMPATIBLE

    fresh = get_agent(client, headers, publisher["id"])
    bundle_id = fresh["bundle_id"]
    bundle_uuid = fresh["bundle_uuid"]
    assert bundle_id and bundle_uuid, "Expected bundle identifiers after publish"

    # ── Phase 3: Make bundle public ───────────────────────────────────────────
    _make_public(client, headers, bundle_uuid)

    # ── Phase 4: A different user installs — needs default credentials for
    # BOTH resolved SDKs (anthropic for conversation, openai_compatible for
    # building), since both are forced onto the installed env by the revision.
    _installer, installer_headers = _make_user_and_headers(client)  # anthropic default
    create_random_ai_credential(
        client,
        installer_headers,
        credential_type="openai_compatible",
        base_url="https://installer-endpoint.example.com/v1",
        model="installer-default-model",
        set_default=True,
    )
    installed = _install(client, installer_headers, bundle_id)
    installed_agent_id = installed["id"]

    # ── Phase 5: Conversation override survives, building override is dropped
    installed_agent = get_agent(client, installer_headers, installed_agent_id)
    installed_env_id = installed_agent["active_environment_id"]
    assert installed_env_id, "installed agent must have an active environment"
    installed_env = get_environment(client, installer_headers, installed_env_id)

    assert installed_env["model_override_conversation"] == _MODEL_OVERRIDE_CONVERSATION, (
        "The non-openai_compatible mode's override must still propagate: "
        f"{installed_env}"
    )
    assert installed_env["model_override_building"] is None, (
        "The openai_compatible mode's override must be suppressed even though "
        f"the sibling mode's override was kept: {installed_env}"
    )


def test_user_set_model_override_on_own_openai_compatible_env_still_sticks(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """
    Guard against the suppression regressing onto the user-facing path.

    ``_importable_model_override`` is scoped to IMPORTED (publisher-authored)
    overrides at the install boundary — it must never be folded into the
    normal environment create/reconfigure flow, or a user's own deliberate
    pin on their own ``openai_compatible`` environment would silently vanish.
    This exercises ``POST /agents/{id}/environments`` (create) and
    ``POST /environments/{id}/reconfigure`` (update) directly — no publish,
    no bundle, no install.
    """
    headers = superuser_token_headers

    agent = create_agent_via_api(
        client, headers, name="ModelOverride-OwnOAICompat-Guard"
    )
    drain_tasks()

    cred = create_random_ai_credential(
        client,
        headers,
        credential_type="openai_compatible",
        base_url="https://user-endpoint.example.com/v1",
        model="user-default-model",
    )

    # ── Phase 1: Create — override sticks on the user's own env ──────────────
    create_env = client.post(
        f"{API}/agents/{agent['id']}/environments",
        headers=headers,
        json={
            "env_name": "python-env-advanced",
            "env_version": "1.0.0",
            "instance_name": "Own OpenAI Compatible",
            "type": "docker",
            "config": {},
            "agent_sdk_conversation": "opencode/openai_compatible",
            "use_default_ai_credentials": False,
            "conversation_ai_credential_id": cred["id"],
            "model_override_conversation": _MODEL_OVERRIDE_OPENAI_COMPATIBLE,
        },
    )
    assert create_env.status_code == 200, create_env.text
    env_id = create_env.json()["id"]
    assert create_env.json()["model_override_conversation"] == _MODEL_OVERRIDE_OPENAI_COMPATIBLE
    drain_tasks()

    fetched = get_environment(client, headers, env_id)
    assert fetched["model_override_conversation"] == _MODEL_OVERRIDE_OPENAI_COMPATIBLE, (
        "A user's own openai_compatible model override must stick on create — "
        f"the install-time suppression must not leak into this path: {fetched}"
    )

    # ── Phase 2: Reconfigure — a changed override still sticks (update path) ─
    reconfigure = client.post(
        f"{API}/environments/{env_id}/reconfigure",
        headers=headers,
        json={
            "agent_sdk_conversation": "opencode/openai_compatible",
            "use_default_ai_credentials": False,
            "conversation_ai_credential_id": cred["id"],
            "model_override_conversation": _MODEL_OVERRIDE_OPENAI_COMPATIBLE_2,
            "rebuild": False,
        },
    )
    assert reconfigure.status_code in (200, 202), reconfigure.text
    assert (
        reconfigure.json()["model_override_conversation"]
        == _MODEL_OVERRIDE_OPENAI_COMPATIBLE_2
    ), (
        "A user's own openai_compatible model override must also stick on "
        f"reconfigure: {reconfigure.json()}"
    )
