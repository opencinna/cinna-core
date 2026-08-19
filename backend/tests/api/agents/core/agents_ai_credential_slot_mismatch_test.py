"""AI-credential slot-mis-detection regression tests.

Before the fix, ``create_environment`` (``use_default_ai_credentials`` branch)
chose the credential-bag slot by the **SDK id** rather than the credential's
**actual type**. A user whose default *conversation* per-mode credential was an
OpenAI key while the env's conversation SDK was ``claude-code/anthropic`` would
end up with:

    ANTHROPIC_API_KEY=<OpenAI key>        ← wrong!

The fix enforces: file by the credential's ACTUAL type; skip and warn when type
is incompatible with the mode's SDK; do NOT persist the mismatched id in
``conversation_ai_credential_id`` / ``building_ai_credential_id``.

The same guard was added to ``_resolve_assigned_credential`` and the rebuild
SDK-settings-regen block in ``environment_lifecycle.py`` so that a poisoned env
self-heals on the next config regen.

Tests
-----

1. ``test_mismatched_default_conversation_credential_uses_anthropic_key``
   (Primary / headline test)
   User has ``openai`` as per-mode *conversation* default, ``anthropic`` as
   per-mode *building* default, both pointing at the correct respective
   credentials. They create a **claude-code/anthropic for BOTH modes** env with
   ``use_default_ai_credentials=True``.

   Asserts:
   - ``.env`` contains ``ANTHROPIC_API_KEY=<anthropic key>``
   - ``.env`` does NOT contain the OpenAI key in the ``ANTHROPIC_API_KEY`` slot
   - ``conversation_ai_credential_id`` in the API response is ``None``
     (the mismatched OpenAI id must not be persisted)

2. ``test_matching_default_credential_resolves_openai_key_into_correct_slot``
   Regression / happy-path: ``opencode/openai`` env + matching OpenAI default
   credential resolves ``OPENAI_API_KEY`` correctly; ``ANTHROPIC_API_KEY`` is
   not populated with the OpenAI key.

3. ``test_rebuild_of_correctly_created_env_does_not_introduce_openai_key``
   Rebuild self-heal: rebuilding the correctly-created claude|claude env from
   scenario 1 keeps ``ANTHROPIC_API_KEY`` = the Anthropic key and never
   introduces the OpenAI key.
"""

from fastapi.testclient import TestClient

from app.core.config import settings
from app.services.environments.environment_service import EnvironmentService
from tests.utils.agent import create_agent_via_api
from tests.utils.ai_credential import create_random_ai_credential
from tests.utils.background_tasks import drain_tasks

API = settings.API_V1_STR


# ── Helpers ──────────────────────────────────────────────────────────────────


def _create_agent_with_env(client: TestClient, headers: dict[str, str]) -> dict:
    """Create an agent, drain background env-creation tasks, return a refreshed
    agent row that has ``active_environment_id`` populated."""
    agent = create_agent_via_api(client, headers, name="CredSlotTest-Agent")
    drain_tasks()
    r = client.get(f"{API}/agents/{agent['id']}", headers=headers)
    assert r.status_code == 200
    refreshed = r.json()
    assert refreshed["active_environment_id"] is not None
    return refreshed


def _set_per_mode_defaults(
    client: TestClient,
    headers: dict[str, str],
    *,
    conversation_id: str | None = None,
    building_id: str | None = None,
) -> None:
    """PATCH /users/me to set per-mode default credential IDs."""
    payload: dict = {}
    if conversation_id is not None:
        payload["default_ai_credential_conversation_id"] = conversation_id
    if building_id is not None:
        payload["default_ai_credential_building_id"] = building_id
    r = client.patch(f"{API}/users/me", headers=headers, json=payload)
    assert r.status_code == 200, r.text


def _read_env_file(env_id: str) -> str:
    """Read the generated ``.env`` file content for a given environment id."""
    lm = EnvironmentService.get_lifecycle_manager()
    env_file = lm.instances_dir / env_id / ".env"
    assert env_file.exists(), f".env not found at {env_file}"
    return env_file.read_text()


def _create_env_via_api(
    client: TestClient,
    headers: dict[str, str],
    agent_id: str,
    *,
    sdk_conversation: str,
    sdk_building: str,
    use_default_ai_credentials: bool = True,
    conversation_ai_credential_id: str | None = None,
    building_ai_credential_id: str | None = None,
) -> dict:
    """POST /agents/{id}/environments and return the parsed response."""
    payload: dict = {
        "env_name": "python-env-advanced",
        "env_version": "1.0.0",
        "instance_name": "CredSlotTest-Env",
        "type": "docker",
        "config": {},
        "agent_sdk_conversation": sdk_conversation,
        "agent_sdk_building": sdk_building,
        "use_default_ai_credentials": use_default_ai_credentials,
    }
    if conversation_ai_credential_id is not None:
        payload["conversation_ai_credential_id"] = conversation_ai_credential_id
    if building_ai_credential_id is not None:
        payload["building_ai_credential_id"] = building_ai_credential_id
    r = client.post(
        f"{API}/agents/{agent_id}/environments",
        headers=headers,
        json=payload,
    )
    assert r.status_code == 200, r.text
    drain_tasks()
    return r.json()


# ── Scenario 1: Primary / headline test ──────────────────────────────────────


def test_mismatched_default_conversation_credential_uses_anthropic_key(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """OpenAI per-mode default conversation credential is NOT filed into ANTHROPIC_API_KEY
    when creating a claude-code/anthropic environment with use_default_ai_credentials=True.

    Full story:
      1. Create an OpenAI credential and set it as the per-mode conversation default.
      2. Create an Anthropic credential; set it as the anthropic type default and
         as the per-mode building default.
      3. Create a new agent (just for its owner context).
      4. POST a second environment for that agent with both SDK modes set to
         ``claude-code/anthropic`` and ``use_default_ai_credentials=True``.
      5. Assert the generated .env contains the Anthropic key in ANTHROPIC_API_KEY.
      6. Assert the OpenAI key is NOT in the ANTHROPIC_API_KEY line.
      7. Assert the API response does NOT persist the mismatched
         conversation_ai_credential_id.
    """
    openai_key = "sk-openai-mismatched-conv-default-key"
    anthropic_key = "sk-ant-api03-correct-anthropic-key"

    # ── Phase 1: Set up per-mode defaults with intentional mismatch ────────────
    # OpenAI credential → set as per-mode conversation default
    openai_cred = create_random_ai_credential(
        client,
        superuser_token_headers,
        credential_type="openai",
        api_key=openai_key,
        name="CredSlotTest-OpenAI-Conv",
    )
    _set_per_mode_defaults(
        client, superuser_token_headers, conversation_id=openai_cred["id"]
    )

    # Anthropic credential → set as type-level default AND per-mode building default
    anthropic_cred = create_random_ai_credential(
        client,
        superuser_token_headers,
        credential_type="anthropic",
        api_key=anthropic_key,
        name="CredSlotTest-Anthropic-Build",
        set_default=True,
    )
    _set_per_mode_defaults(
        client, superuser_token_headers, building_id=anthropic_cred["id"]
    )

    # ── Phase 2: Create agent + env ────────────────────────────────────────────
    agent = _create_agent_with_env(client, superuser_token_headers)
    agent_id = agent["id"]

    # Create a second environment explicitly configured as claude-code/anthropic
    # for BOTH modes with use_default_ai_credentials=True — the bug scenario.
    env = _create_env_via_api(
        client,
        superuser_token_headers,
        agent_id,
        sdk_conversation="claude-code/anthropic",
        sdk_building="claude-code/anthropic",
        use_default_ai_credentials=True,
    )
    env_id = env["id"]

    # ── Phase 3: Assert .env has the Anthropic key in ANTHROPIC_API_KEY ────────
    env_content = _read_env_file(env_id)

    assert f"ANTHROPIC_API_KEY={anthropic_key}" in env_content, (
        f"Expected ANTHROPIC_API_KEY={anthropic_key!r} in .env, "
        f"but it was missing. .env content:\n{env_content}"
    )

    # The OpenAI key must NOT appear in the ANTHROPIC_API_KEY slot (the bug was
    # writing it there).  We check the specific line rather than the whole file
    # because the OpenAI key legitimately appears in OPENAI_API_KEY= if the bag
    # also resolves it via a separate typed fallback.
    for line in env_content.splitlines():
        if line.startswith("ANTHROPIC_API_KEY="):
            assert openai_key not in line, (
                f"OpenAI key found in ANTHROPIC_API_KEY slot: {line!r}. "
                "This is the bug under test."
            )
            break

    # ── Phase 4: Assert mismatched credential id was NOT persisted ─────────────
    # Fetch the environment via API to check stored credential IDs
    r = client.get(
        f"{API}/environments/{env_id}",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200, r.text
    env_detail = r.json()

    assert env_detail["conversation_ai_credential_id"] is None, (
        f"conversation_ai_credential_id should be None (mismatched OpenAI cred "
        f"must not be persisted), but got {env_detail['conversation_ai_credential_id']!r}."
    )


# ── Scenario 2: Happy path — OpenAI credential resolves correctly ─────────────


def test_matching_default_credential_resolves_openai_key_into_correct_slot(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """A matching OpenAI default credential resolves into OPENAI_API_KEY, not ANTHROPIC_API_KEY.

    Regression / happy-path: ensures the fix didn't over-restrict correct cases.

    Story:
      1. Create an OpenAI credential and set it as both type-level default and
         per-mode conversation default.
      2. Create an agent and a second environment with ``opencode/openai`` for
         BOTH modes and ``use_default_ai_credentials=True``.
      3. Assert OPENAI_API_KEY = the OpenAI key.
      4. Assert ANTHROPIC_API_KEY line does NOT contain the OpenAI key value.
    """
    openai_key = "sk-openai-correct-slot-happy-path-key"

    # ── Phase 1: Create and default OpenAI credential ─────────────────────────
    openai_cred = create_random_ai_credential(
        client,
        superuser_token_headers,
        credential_type="openai",
        api_key=openai_key,
        name="CredSlotTest-OpenAI-Happy",
        set_default=True,
    )
    _set_per_mode_defaults(
        client,
        superuser_token_headers,
        conversation_id=openai_cred["id"],
        building_id=openai_cred["id"],
    )

    # ── Phase 2: Create agent + env with opencode/openai for both modes ────────
    agent = _create_agent_with_env(client, superuser_token_headers)
    agent_id = agent["id"]

    env = _create_env_via_api(
        client,
        superuser_token_headers,
        agent_id,
        sdk_conversation="opencode/openai",
        sdk_building="opencode/openai",
        use_default_ai_credentials=True,
    )
    env_id = env["id"]

    # ── Phase 3: Assert OPENAI_API_KEY is set correctly ────────────────────────
    env_content = _read_env_file(env_id)

    assert f"OPENAI_API_KEY={openai_key}" in env_content, (
        f"Expected OPENAI_API_KEY={openai_key!r} in .env but it was missing. "
        f".env content:\n{env_content}"
    )

    # For opencode/openai envs, the ANTHROPIC_API_KEY slot is either absent
    # (commented out) or empty — the OpenAI key value must never appear there.
    for line in env_content.splitlines():
        if line.startswith("ANTHROPIC_API_KEY="):
            assert openai_key not in line, (
                f"OpenAI key found in ANTHROPIC_API_KEY slot: {line!r}. "
                "Key was mis-filed into the wrong slot."
            )
            break
    # Additionally verify the raw key value is not embedded in any
    # ANTHROPIC_* variable line (catch any future variant of the bug).
    for line in env_content.splitlines():
        if line.startswith("ANTHROPIC_") and not line.startswith("#"):
            assert openai_key not in line, (
                f"OpenAI key leaked into ANTHROPIC_* variable: {line!r}"
            )


# ── Scenario 3: Rebuild self-heal ────────────────────────────────────────────


def test_rebuild_of_correctly_created_env_does_not_introduce_openai_key(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """A rebuild of a correctly-created claude|claude env never writes the OpenAI key
    into ANTHROPIC_API_KEY, even when the user also has an OpenAI per-mode default.

    Story:
      1. Set up the same mismatched per-mode default scenario as the primary test
         (OpenAI conv default, Anthropic build default).
      2. Create an agent + env with claude-code/anthropic for both modes and
         use_default_ai_credentials=True.
      3. Verify .env has the Anthropic key (baseline).
      4. Rebuild the environment (simulates a credential-propagation rebuild).
      5. Verify .env still has the Anthropic key and the OpenAI key has NOT
         been introduced into ANTHROPIC_API_KEY.
    """
    openai_key = "sk-openai-rebuild-mismatch-key"
    anthropic_key = "sk-ant-api03-rebuild-correct-key"

    # ── Phase 1: Set up per-mode defaults ─────────────────────────────────────
    openai_cred = create_random_ai_credential(
        client,
        superuser_token_headers,
        credential_type="openai",
        api_key=openai_key,
        name="CredSlotTest-Rebuild-OpenAI",
    )
    _set_per_mode_defaults(
        client, superuser_token_headers, conversation_id=openai_cred["id"]
    )

    anthropic_cred = create_random_ai_credential(
        client,
        superuser_token_headers,
        credential_type="anthropic",
        api_key=anthropic_key,
        name="CredSlotTest-Rebuild-Anthropic",
        set_default=True,
    )
    _set_per_mode_defaults(
        client, superuser_token_headers, building_id=anthropic_cred["id"]
    )

    # ── Phase 2: Create agent + env ────────────────────────────────────────────
    agent = _create_agent_with_env(client, superuser_token_headers)
    agent_id = agent["id"]

    env = _create_env_via_api(
        client,
        superuser_token_headers,
        agent_id,
        sdk_conversation="claude-code/anthropic",
        sdk_building="claude-code/anthropic",
        use_default_ai_credentials=True,
    )
    env_id = env["id"]

    # ── Phase 3: Baseline — .env has the Anthropic key ─────────────────────────
    env_content = _read_env_file(env_id)
    assert f"ANTHROPIC_API_KEY={anthropic_key}" in env_content, (
        "Baseline check failed: ANTHROPIC_API_KEY should be the Anthropic key "
        f"before rebuild. .env content:\n{env_content}"
    )
    for line in env_content.splitlines():
        if line.startswith("ANTHROPIC_API_KEY="):
            assert openai_key not in line, (
                f"OpenAI key already present in ANTHROPIC_API_KEY before rebuild: {line!r}"
            )
            break

    # ── Phase 4: Rebuild the environment ──────────────────────────────────────
    r = client.post(
        f"{API}/environments/{env_id}/rebuild",
        headers=superuser_token_headers,
    )
    assert r.status_code == 200, r.text
    drain_tasks()

    # ── Phase 5: Post-rebuild — .env still has the Anthropic key ──────────────
    env_content_after = _read_env_file(env_id)

    assert f"ANTHROPIC_API_KEY={anthropic_key}" in env_content_after, (
        f"ANTHROPIC_API_KEY lost the Anthropic key after rebuild. "
        f".env content:\n{env_content_after}"
    )
    for line in env_content_after.splitlines():
        if line.startswith("ANTHROPIC_API_KEY="):
            assert openai_key not in line, (
                f"OpenAI key appeared in ANTHROPIC_API_KEY after rebuild: {line!r}. "
                "The rebuild introduced the mis-filing that the fix should prevent."
            )
            break


# ── Scenario 4: Mixed-SDK per-mode fallback regression (reported bug) ──────────
#
# Reported bug: a NEW agent created from the dashboard wizard with
#   conversation = opencode/openai   (default conversation credential = OpenAI)
#   building     = claude-code/anthropic  (no per-mode building default; the
#                  Anthropic key comes from the *type-level* default credential)
# started up with ``ANTHROPIC_API_KEY=`` empty, and rebuilding did not help.
#
# Root cause: the OpenAI conversation credential is compatible with
# ``opencode/openai`` so its id is PERSISTED as ``conversation_ai_credential_id``.
# The building mode resolves Anthropic from the type-level default at create
# time, which fills the value but persists NO ``building_ai_credential_id``. On
# every reconfigure (start / restart / rebuild) the old all-or-nothing fallback
# gate saw "one mode is pinned" and skipped the profile / type-default fallback
# for BOTH modes — dropping the unpinned building Anthropic key.
#
# The fix scopes the fallback PER MODE: a mode with no usable assigned
# credential still re-resolves its key, even when the other mode is pinned.


def _setup_mixed_sdk_asymmetric_defaults(
    client: TestClient, headers: dict[str, str], *, openai_key: str, anthropic_key: str
) -> dict:
    """Create the asymmetric credential setup behind the reported bug and return
    the created OpenAI / Anthropic credential rows.

    - OpenAI credential: per-mode *conversation* default (compatible with
      ``opencode/openai`` → its id IS persisted on the env).
    - Anthropic credential: *type-level* default only, NO per-mode building
      default (resolved into the value but its id is NOT persisted on the env).
    """
    openai_cred = create_random_ai_credential(
        client,
        headers,
        credential_type="openai",
        api_key=openai_key,
        name="MixedSDK-OpenAI-Conv",
        set_default=True,
    )
    _set_per_mode_defaults(client, headers, conversation_id=openai_cred["id"])

    anthropic_cred = create_random_ai_credential(
        client,
        headers,
        credential_type="anthropic",
        api_key=anthropic_key,
        name="MixedSDK-Anthropic-TypeDefault",
        set_default=True,
    )
    return {"openai": openai_cred, "anthropic": anthropic_cred}


def test_rebuild_preserves_anthropic_when_only_conversation_credential_pinned(
    client: TestClient,
    superuser_token_headers: dict[str, str],
) -> None:
    """Rebuild of a mixed-SDK env keeps the unpinned building Anthropic key."""
    openai_key = "sk-openai-mixed-conv-default-key"
    anthropic_key = "sk-ant-api03-mixed-build-typedefault-key"

    creds = _setup_mixed_sdk_asymmetric_defaults(
        client, superuser_token_headers, openai_key=openai_key, anthropic_key=anthropic_key
    )

    agent = _create_agent_with_env(client, superuser_token_headers)
    agent_id = agent["id"]

    env = _create_env_via_api(
        client,
        superuser_token_headers,
        agent_id,
        sdk_conversation="opencode/openai",
        sdk_building="claude-code/anthropic",
        use_default_ai_credentials=True,
    )
    env_id = env["id"]

    # The asymmetry that triggers the bug: conversation id persisted, building not.
    r = client.get(f"{API}/environments/{env_id}", headers=superuser_token_headers)
    assert r.status_code == 200, r.text
    detail = r.json()
    assert detail["conversation_ai_credential_id"] == creds["openai"]["id"], detail
    assert detail["building_ai_credential_id"] is None, detail

    # Baseline: create-time .env has the Anthropic key.
    assert f"ANTHROPIC_API_KEY={anthropic_key}" in _read_env_file(env_id)

    # Rebuild — the operation the user reported "doesn't help".
    r = client.post(
        f"{API}/environments/{env_id}/rebuild", headers=superuser_token_headers
    )
    assert r.status_code == 200, r.text
    drain_tasks()

    env_after = _read_env_file(env_id)
    assert f"ANTHROPIC_API_KEY={anthropic_key}" in env_after, (
        "ANTHROPIC_API_KEY was dropped on rebuild for the unpinned building mode. "
        f".env content:\n{env_after}"
    )
    # The pinned OpenAI conversation key is still resolved (written to .env and
    # embedded into opencode.json for the opencode/openai conversation mode).
    assert f"OPENAI_API_KEY={openai_key}" in env_after, env_after


def test_reconfigure_preserves_anthropic_when_only_conversation_credential_pinned(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db,
) -> None:
    """The start / reconfigure path (``_update_environment_config``) keeps the
    unpinned building Anthropic key.

    This drives the exact code path that ``start_environment`` runs on auto-start
    (the original symptom: a freshly *started* env had an empty
    ``ANTHROPIC_API_KEY``). We invoke ``_update_environment_config`` directly so
    the test does not depend on a running Docker daemon for the agent container.
    """
    import uuid as _uuid

    from app.models.agents.agent import Agent
    from app.models.environments.environment import AgentEnvironment

    openai_key = "sk-openai-reconfigure-conv-default-key"
    anthropic_key = "sk-ant-api03-reconfigure-build-typedefault-key"

    _setup_mixed_sdk_asymmetric_defaults(
        client, superuser_token_headers, openai_key=openai_key, anthropic_key=anthropic_key
    )

    agent = _create_agent_with_env(client, superuser_token_headers)
    agent_id = agent["id"]

    env = _create_env_via_api(
        client,
        superuser_token_headers,
        agent_id,
        sdk_conversation="opencode/openai",
        sdk_building="claude-code/anthropic",
        use_default_ai_credentials=True,
    )
    env_id = env["id"]

    # Baseline.
    assert f"ANTHROPIC_API_KEY={anthropic_key}" in _read_env_file(env_id)

    # Re-run config generation the way start_environment does (no credential bag
    # passed → resolves purely from stored ids + per-mode fallback).
    lm = EnvironmentService.get_lifecycle_manager()
    env_obj = db.get(AgentEnvironment, _uuid.UUID(env_id))
    agent_obj = db.get(Agent, _uuid.UUID(agent_id))
    assert env_obj is not None and agent_obj is not None
    instance_dir = lm.instances_dir / env_id

    lm._update_environment_config(db, instance_dir, env_obj, agent_obj)

    env_after = _read_env_file(env_id)
    assert f"ANTHROPIC_API_KEY={anthropic_key}" in env_after, (
        "ANTHROPIC_API_KEY was dropped on reconfigure for the unpinned building "
        f"mode. .env content:\n{env_after}"
    )
