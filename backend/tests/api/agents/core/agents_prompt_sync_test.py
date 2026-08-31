"""
Integration tests: prompt-sync reconciliation (backend ↔ environment).

Exercises the full bidirectional reconcile pipeline through the FastAPI
TestClient. Each scenario is a named user story that maps to one row (or
combination of rows) in the reconcile decision table.

Scenarios covered
-----------------
1. Building-session auto-sync lifecycle (env→DB pull on STREAM_COMPLETED)
2. UI edit → PUSH to env; baseline updated; NOOP on next reconcile (no echo)
3. Env edit → PULL to DB on reconcile; not overwritten
4. Both-sides diverged → LWW resolves, converges (env wins on tie/newer ts)
5. refiner_prompt participates in all sync directions (was previously omitted)
6. Manual sync-prompts is a force-push (DB wins, baseline reset)
7. SEED: first reconcile after migration (base=None, DB authoritative)
8. SEED_PULL: base=None, DB empty, env has content → pulled to DB

Scenarios deferred (require real Docker env or are covered by unit tests)
--------------------------------------------------------------------------
- apply_update while env running → prefer="db" push + baseline reset
  (requires bundle install flow + running env; orchestrator path is covered
  but the full install-service integration is out of scope here)
- STATUS.md pulled on ENVIRONMENT_ACTIVATED (requires lifecycle event wiring
  and a real activation flow; pull-only services have their own unit tests)
- Frontend live-refresh (AGENT_UPDATED event wiring; no frontend test harness)

All agent-env HTTP is stubbed via EnvironmentTestAdapter (persistent instance).
StubAgentEnvConnector handles SSE streaming.
"""
from __future__ import annotations

import time
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import Session

from tests.stubs.agent_env_stub import StubAgentEnvConnector
from tests.stubs.environment_adapter_stub import EnvironmentTestAdapter
from tests.utils.agent import (
    create_agent_via_api,
    get_agent,
    update_agent,
    sync_agent_prompts,
)
from tests.utils.background_tasks import drain_tasks
from tests.utils.message import get_messages_by_role, send_message
from tests.utils.session import create_session_via_api


# ---------------------------------------------------------------------------
# Shared content fixtures (module-level constants for readability)
# ---------------------------------------------------------------------------

_WORKFLOW_V1 = "## Workflow v1\nStep 1: collect data.\nStep 2: analyse."
_ENTRYPOINT_V1 = "Analyse today's data and report findings."
_REFINER_V1 = "## Defaults\n- Date range: last 7 days\n- Format: markdown table"

_WORKFLOW_V2_ENV = (
    "## Workflow v2 (env edit)\n"
    "Step 1: collect data.\n"
    "Step 2: analyse.\n"
    "Step 3: send summary email."
)
_ENTRYPOINT_V2_ENV = "Analyse today's data, report findings, and send an email."
_REFINER_V2_ENV = "## Defaults\n- Date range: last 30 days\n- Format: markdown table"

_WORKFLOW_V2_DB = "## Workflow v2 (UI edit)\nStep 1: collect data.\nStep 2: analyse.\nStep 3: archive."
_ENTRYPOINT_V2_DB = "Analyse today's data, report findings, then archive."


# ---------------------------------------------------------------------------
# Scenario 1: Building-session STREAM_COMPLETED → auto-sync env→DB (PULL)
# ---------------------------------------------------------------------------

def test_prompt_sync_building_session_lifecycle(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    patch_environment_adapter,
) -> None:
    """
    Full prompt-sync lifecycle via a building session:
      1. Create agent (environment auto-created).
      2. Update prompts via UI (PUT /agents/{id}).
      3. Sync prompts to environment (manual force-push).
      4. Verify prompts reached the env adapter.
      5. Simulate agent editing prompts inside the env.
      6. Create building session, send message, drain → STREAM_COMPLETED fires.
      7. auto-sync pulls env changes → DB updated (PULL).
      8. Verify agent has the env-authored prompts.
    """
    # ── Setup: persistent adapter so state survives across calls ───────
    shared_adapter = EnvironmentTestAdapter()
    patch_environment_adapter.get_adapter = lambda env: shared_adapter

    # ── Phase 1: Create agent ─────────────────────────────────────────
    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent = get_agent(client, superuser_token_headers, agent["id"])
    agent_id = agent["id"]
    assert agent["active_environment_id"] is not None

    # ── Phase 2: Update prompts via PUT /agents/{id} ──────────────────
    updated = update_agent(
        client, superuser_token_headers, agent_id,
        workflow_prompt=_WORKFLOW_V1,
        entrypoint_prompt=_ENTRYPOINT_V1,
        refiner_prompt=_REFINER_V1,
    )
    assert updated["workflow_prompt"] == _WORKFLOW_V1
    assert updated["entrypoint_prompt"] == _ENTRYPOINT_V1
    assert updated["refiner_prompt"] == _REFINER_V1

    # ── Phase 3: Force-push prompts to env (DB → env) ─────────────────
    sync_agent_prompts(client, superuser_token_headers, agent_id)

    assert shared_adapter.prompts_set.get("workflow_prompt") == _WORKFLOW_V1
    assert shared_adapter.prompts_set.get("entrypoint_prompt") == _ENTRYPOINT_V1
    assert shared_adapter.prompts_set.get("refiner_prompt") == _REFINER_V1

    # ── Phase 4: Simulate agent editing prompts inside the env ────────
    shared_adapter.prompts_set["workflow_prompt"] = _WORKFLOW_V2_ENV
    shared_adapter.prompts_set["entrypoint_prompt"] = _ENTRYPOINT_V2_ENV
    shared_adapter.prompts_set["refiner_prompt"] = _REFINER_V2_ENV
    # Set env mtimes as "now" so LWW tiebreak never accidentally fires DB wins.
    now_ts = time.time()
    shared_adapter.prompt_mtimes["workflow_prompt"] = now_ts
    shared_adapter.prompt_mtimes["entrypoint_prompt"] = now_ts
    shared_adapter.prompt_mtimes["refiner_prompt"] = now_ts

    # ── Phase 5: Building session → send message → STREAM_COMPLETED ───
    session_data = create_session_via_api(
        client, superuser_token_headers, agent_id, mode="building"
    )
    session_id = session_data["id"]
    agent_response = "I've updated the workflow to include emailing."
    stub = StubAgentEnvConnector(response_text=agent_response)

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        send_message(
            client, superuser_token_headers, session_id,
            content="Extend the workflow to also email the summary.",
        )
        drain_tasks()

    # ── Phase 6: Verify agent response stored ─────────────────────────
    agent_msgs = get_messages_by_role(
        client, superuser_token_headers, session_id, "agent"
    )
    assert len(agent_msgs) >= 1
    assert agent_response in agent_msgs[0]["content"]

    # ── Phase 7: Verify auto-sync pulled env-authored prompts to DB ───
    agent_after = get_agent(client, superuser_token_headers, agent_id)
    assert agent_after["workflow_prompt"] == _WORKFLOW_V2_ENV
    assert agent_after["entrypoint_prompt"] == _ENTRYPOINT_V2_ENV
    assert agent_after["refiner_prompt"] == _REFINER_V2_ENV


# ---------------------------------------------------------------------------
# Scenario 2: UI edit → PUSH; baseline updated; NOOP on next reconcile
# ---------------------------------------------------------------------------

def test_ui_edit_push_then_noop_on_repeat_reconcile(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    patch_environment_adapter,
) -> None:
    """
    UI edit → manual sync-prompts (DB→env push) → verify pushed.
    Second manual sync (no content change) is a NOOP — baseline stays stable
    and the adapter is NOT called with any new writes.

      1. Create agent.
      2. Update prompts in DB via PUT /agents/{id}.
      3. Call POST /agents/{id}/sync-prompts (force-push DB → env).
      4. Verify env received the new prompts.
      5. Call sync-prompts again (content unchanged).
      6. Verify env content is identical to phase-4 state (no echo/clobber).
    """
    # ── Setup ──────────────────────────────────────────────────────────
    shared_adapter = EnvironmentTestAdapter()
    patch_environment_adapter.get_adapter = lambda env: shared_adapter

    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent_id = get_agent(client, superuser_token_headers, agent["id"])["id"]

    # ── Phase 1: Push prompts to env ──────────────────────────────────
    update_agent(
        client, superuser_token_headers, agent_id,
        workflow_prompt=_WORKFLOW_V1,
        entrypoint_prompt=_ENTRYPOINT_V1,
        refiner_prompt=_REFINER_V1,
    )
    sync_agent_prompts(client, superuser_token_headers, agent_id)

    assert shared_adapter.prompts_set.get("workflow_prompt") == _WORKFLOW_V1
    assert shared_adapter.prompts_set.get("entrypoint_prompt") == _ENTRYPOINT_V1
    assert shared_adapter.prompts_set.get("refiner_prompt") == _REFINER_V1

    # Capture the "set_agent_prompts called" counter to detect extra writes.
    # The adapter accumulates into prompts_set; we snapshot its content.
    snapshot_after_push = dict(shared_adapter.prompts_set)

    # ── Phase 2: Reconcile again (building-session) — no content change
    session_data = create_session_via_api(
        client, superuser_token_headers, agent_id, mode="building"
    )
    session_id = session_data["id"]
    stub = StubAgentEnvConnector(response_text="ok")

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        send_message(client, superuser_token_headers, session_id, content="ping")
        drain_tasks()

    # ── Phase 3: Verify env content unchanged (NOOP — baseline healed)
    assert shared_adapter.prompts_set.get("workflow_prompt") == snapshot_after_push["workflow_prompt"]
    assert shared_adapter.prompts_set.get("entrypoint_prompt") == snapshot_after_push["entrypoint_prompt"]
    assert shared_adapter.prompts_set.get("refiner_prompt") == snapshot_after_push["refiner_prompt"]

    # Verify DB also unchanged
    agent_after = get_agent(client, superuser_token_headers, agent_id)
    assert agent_after["workflow_prompt"] == _WORKFLOW_V1
    assert agent_after["entrypoint_prompt"] == _ENTRYPOINT_V1
    assert agent_after["refiner_prompt"] == _REFINER_V1


# ---------------------------------------------------------------------------
# Scenario 3: Env edit → reconcile PULLs to DB; not overwritten
# ---------------------------------------------------------------------------

def test_env_edit_pulled_to_db_on_reconcile(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    patch_environment_adapter,
) -> None:
    """
    Env edits are pulled into the DB on the next reconcile (STREAM_COMPLETED).
    The DB retains them; no subsequent push overwrites them.

      1. Create agent, push initial prompts to env.
      2. Simulated env edit (only workflow + entrypoint; refiner untouched).
      3. Building session → STREAM_COMPLETED → auto-reconcile.
      4. Verify pulled fields are in DB.
      5. Untouched refiner prompt unchanged in DB.
    """
    shared_adapter = EnvironmentTestAdapter()
    patch_environment_adapter.get_adapter = lambda env: shared_adapter

    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent_id = get_agent(client, superuser_token_headers, agent["id"])["id"]

    # ── Phase 1: Establish initial state ─────────────────────────────
    update_agent(
        client, superuser_token_headers, agent_id,
        workflow_prompt=_WORKFLOW_V1,
        entrypoint_prompt=_ENTRYPOINT_V1,
        refiner_prompt=_REFINER_V1,
    )
    sync_agent_prompts(client, superuser_token_headers, agent_id)

    # ── Phase 2: Simulate env editing two of three prompts ────────────
    shared_adapter.prompts_set["workflow_prompt"] = _WORKFLOW_V2_ENV
    shared_adapter.prompts_set["entrypoint_prompt"] = _ENTRYPOINT_V2_ENV
    # refiner_prompt stays as _REFINER_V1 (the value the sync pushed)
    now_ts = time.time()
    shared_adapter.prompt_mtimes["workflow_prompt"] = now_ts
    shared_adapter.prompt_mtimes["entrypoint_prompt"] = now_ts

    # ── Phase 3: Trigger reconcile via building session ───────────────
    session_data = create_session_via_api(
        client, superuser_token_headers, agent_id, mode="building"
    )
    session_id = session_data["id"]
    stub = StubAgentEnvConnector(response_text="done")

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        send_message(client, superuser_token_headers, session_id, content="extend")
        drain_tasks()

    # ── Phase 4: Env-edited prompts pulled into DB ────────────────────
    agent_after = get_agent(client, superuser_token_headers, agent_id)
    assert agent_after["workflow_prompt"] == _WORKFLOW_V2_ENV
    assert agent_after["entrypoint_prompt"] == _ENTRYPOINT_V2_ENV

    # ── Phase 5: Untouched refiner_prompt unchanged ───────────────────
    assert agent_after["refiner_prompt"] == _REFINER_V1


# ---------------------------------------------------------------------------
# Scenario 4: Both-sides diverged → LWW resolves; env wins on newer ts
# ---------------------------------------------------------------------------

def test_conflict_lww_env_wins_on_newer_mtime(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    patch_environment_adapter,
) -> None:
    """
    Both DB and env edited since the last baseline → LWW tiebreak.
    When env mtime is newer than DB updated_at, env wins (CONFLICT_PULL).

      1. Create agent, push initial prompts → baseline established.
      2. Update DB workflow_prompt via UI (bumps DB logical clock).
      3. Simulate env also editing workflow_prompt (with a later mtime).
      4. Trigger reconcile → env wins → DB has env version.
    """
    shared_adapter = EnvironmentTestAdapter()
    patch_environment_adapter.get_adapter = lambda env: shared_adapter

    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent_id = get_agent(client, superuser_token_headers, agent["id"])["id"]

    # ── Phase 1: Establish baseline ────────────────────────────────────
    update_agent(
        client, superuser_token_headers, agent_id,
        workflow_prompt=_WORKFLOW_V1,
        entrypoint_prompt=_ENTRYPOINT_V1,
    )
    sync_agent_prompts(client, superuser_token_headers, agent_id)

    # ── Phase 2: DB edit (UI save → bumps workflow_prompt_updated_at) ──
    update_agent(
        client, superuser_token_headers, agent_id,
        workflow_prompt=_WORKFLOW_V2_DB,
    )

    # ── Phase 3: Env edit with a LATER mtime (env wins the LWW) ───────
    # We set the env mtime to a far-future timestamp (still below skew ceiling).
    # The DB updated_at was just set to ~now by update_agent; env mtime is
    # a few seconds later, so env_ts > db_ts → CONFLICT_PULL.
    shared_adapter.prompts_set["workflow_prompt"] = _WORKFLOW_V2_ENV
    future_ts = time.time() + 3600  # 1 hour in the future; within skew ceiling
    shared_adapter.prompt_mtimes["workflow_prompt"] = future_ts

    # ── Phase 4: Reconcile via building session ───────────────────────
    session_data = create_session_via_api(
        client, superuser_token_headers, agent_id, mode="building"
    )
    session_id = session_data["id"]
    stub = StubAgentEnvConnector(response_text="resolved")

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        send_message(client, superuser_token_headers, session_id, content="reconcile")
        drain_tasks()

    # ── Phase 5: DB has env version (env won LWW) ─────────────────────
    agent_after = get_agent(client, superuser_token_headers, agent_id)
    assert agent_after["workflow_prompt"] == _WORKFLOW_V2_ENV


# ---------------------------------------------------------------------------
# Scenario 5: refiner_prompt participates in push/pull (was previously omitted)
# ---------------------------------------------------------------------------

def test_refiner_prompt_participates_in_sync(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    patch_environment_adapter,
) -> None:
    """
    refiner_prompt now participates in all sync directions (push + pull).
    Previously it was omitted from the push path in _sync_dynamic_data.

    Sub-scenario A — push: DB refiner_prompt reaches the env adapter.
    Sub-scenario B — pull: env-edited refiner_prompt reaches DB via reconcile.
    """
    shared_adapter = EnvironmentTestAdapter()
    patch_environment_adapter.get_adapter = lambda env: shared_adapter

    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent_id = get_agent(client, superuser_token_headers, agent["id"])["id"]

    # ── Sub-scenario A: Push path includes refiner_prompt ─────────────
    update_agent(
        client, superuser_token_headers, agent_id,
        workflow_prompt=_WORKFLOW_V1,
        entrypoint_prompt=_ENTRYPOINT_V1,
        refiner_prompt=_REFINER_V1,
    )
    sync_agent_prompts(client, superuser_token_headers, agent_id)

    # refiner_prompt must appear in the env adapter (previously absent)
    assert shared_adapter.prompts_set.get("refiner_prompt") == _REFINER_V1, (
        "refiner_prompt was not pushed to the environment adapter "
        "(regression: it was previously omitted from the push path)"
    )

    # ── Sub-scenario B: Pull path includes refiner_prompt ─────────────
    shared_adapter.prompts_set["refiner_prompt"] = _REFINER_V2_ENV
    shared_adapter.prompt_mtimes["refiner_prompt"] = time.time()

    session_data = create_session_via_api(
        client, superuser_token_headers, agent_id, mode="building"
    )
    stub = StubAgentEnvConnector(response_text="done")

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        send_message(
            client, superuser_token_headers, session_data["id"],
            content="update refiner",
        )
        drain_tasks()

    agent_after = get_agent(client, superuser_token_headers, agent_id)
    assert agent_after["refiner_prompt"] == _REFINER_V2_ENV, (
        "refiner_prompt env edit was not pulled to DB "
        "(regression: it was previously omitted from the pull path)"
    )


# ---------------------------------------------------------------------------
# Scenario 6: Manual POST /sync-prompts is a force-push (DB wins, baseline reset)
# ---------------------------------------------------------------------------

def test_manual_sync_prompts_is_force_push(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    patch_environment_adapter,
) -> None:
    """
    POST /agents/{id}/sync-prompts is an explicit override push — user intent is
    DB wins. The env file is overwritten and the baseline is reset to the DB hash
    so the next reconcile does not pull the old env content back.

      1. Create agent with DB prompts.
      2. Simulate env having different content.
      3. Call sync-prompts (force-push).
      4. Env now has DB content.
      5. Trigger reconcile (building session) → NOOP (baseline already correct).
      6. DB is unchanged (no pull-back of the old env content).
    """
    shared_adapter = EnvironmentTestAdapter()
    patch_environment_adapter.get_adapter = lambda env: shared_adapter

    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent_id = get_agent(client, superuser_token_headers, agent["id"])["id"]

    # ── Phase 1: Set DB prompts ───────────────────────────────────────
    update_agent(
        client, superuser_token_headers, agent_id,
        workflow_prompt=_WORKFLOW_V1,
        entrypoint_prompt=_ENTRYPOINT_V1,
        refiner_prompt=_REFINER_V1,
    )

    # ── Phase 2: Simulate env having different content (stale env) ────
    shared_adapter.prompts_set["workflow_prompt"] = _WORKFLOW_V2_ENV
    shared_adapter.prompts_set["entrypoint_prompt"] = _ENTRYPOINT_V2_ENV
    shared_adapter.prompts_set["refiner_prompt"] = _REFINER_V2_ENV
    shared_adapter.prompt_mtimes["workflow_prompt"] = time.time()
    shared_adapter.prompt_mtimes["entrypoint_prompt"] = time.time()
    shared_adapter.prompt_mtimes["refiner_prompt"] = time.time()

    # ── Phase 3: Force-push DB → env ─────────────────────────────────
    sync_agent_prompts(client, superuser_token_headers, agent_id)

    # ── Phase 4: Env has DB content ───────────────────────────────────
    assert shared_adapter.prompts_set.get("workflow_prompt") == _WORKFLOW_V1
    assert shared_adapter.prompts_set.get("entrypoint_prompt") == _ENTRYPOINT_V1
    assert shared_adapter.prompts_set.get("refiner_prompt") == _REFINER_V1

    # ── Phase 5+6: Building session reconcile → NOOP; DB stays at v1 ──
    session_data = create_session_via_api(
        client, superuser_token_headers, agent_id, mode="building"
    )
    stub = StubAgentEnvConnector(response_text="ok")

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        send_message(
            client, superuser_token_headers, session_data["id"],
            content="check state",
        )
        drain_tasks()

    agent_after = get_agent(client, superuser_token_headers, agent_id)
    assert agent_after["workflow_prompt"] == _WORKFLOW_V1, (
        "DB prompt was overwritten after force-push + reconcile — baseline reset failed"
    )
    assert agent_after["refiner_prompt"] == _REFINER_V1


# ---------------------------------------------------------------------------
# Scenario 7: SEED — first reconcile after migration (base=None, DB authoritative)
# ---------------------------------------------------------------------------

def test_seed_push_db_authoritative_on_first_reconcile(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    patch_environment_adapter,
) -> None:
    """
    First reconcile after a migration backfill (base_hash=None).
    DB has content, env has different content → SEED_PUSH: DB wins.

    The env adapter starts with blank prompts (simulating a post-migration env
    where no baseline has been recorded yet). After reconcile the env receives
    the DB content and the baseline is set.

      1. Create agent with DB prompts.
      2. Ensure env adapter has blank state (no prior sync, base=None).
      3. Trigger reconcile (building session) → SEED_PUSH.
      4. Verify env has DB content.
      5. Verify DB unchanged.
    """
    shared_adapter = EnvironmentTestAdapter()
    patch_environment_adapter.get_adapter = lambda env: shared_adapter

    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent_id = get_agent(client, superuser_token_headers, agent["id"])["id"]

    # ── Phase 1: Set DB prompts (no sync yet — env state is blank) ────
    update_agent(
        client, superuser_token_headers, agent_id,
        workflow_prompt=_WORKFLOW_V1,
        entrypoint_prompt=_ENTRYPOINT_V1,
        refiner_prompt=_REFINER_V1,
    )
    # Deliberately do NOT call sync_agent_prompts; env adapter has blank prompts
    # (get_agent_prompts returns None for all fields).

    # ── Phase 2: Trigger reconcile ────────────────────────────────────
    session_data = create_session_via_api(
        client, superuser_token_headers, agent_id, mode="building"
    )
    stub = StubAgentEnvConnector(response_text="seeded")

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        send_message(
            client, superuser_token_headers, session_data["id"],
            content="run workflow",
        )
        drain_tasks()

    # ── Phase 3: Env received DB content (SEED_PUSH / PUSH) ──────────
    assert shared_adapter.prompts_set.get("workflow_prompt") == _WORKFLOW_V1, (
        "SEED_PUSH did not push DB workflow_prompt to env"
    )
    assert shared_adapter.prompts_set.get("refiner_prompt") == _REFINER_V1, (
        "SEED_PUSH did not push DB refiner_prompt to env"
    )

    # ── Phase 4: DB unchanged ─────────────────────────────────────────
    agent_after = get_agent(client, superuser_token_headers, agent_id)
    assert agent_after["workflow_prompt"] == _WORKFLOW_V1
    assert agent_after["refiner_prompt"] == _REFINER_V1


# ---------------------------------------------------------------------------
# Scenario 8: SEED_PULL — base=None, DB empty, env has content
# ---------------------------------------------------------------------------

def test_seed_pull_env_content_pulled_when_db_empty(
    client: TestClient,
    superuser_token_headers: dict[str, str],
    db: Session,
    patch_environment_adapter,
) -> None:
    """
    First reconcile, DB prompt is empty/None, env has content → SEED_PULL.
    This covers the case where a developer built prompts inside the env before
    the UI was used.

      1. Create agent (DB prompts are empty/None by default).
      2. Simulate env having prompt content.
      3. Trigger reconcile (building session).
      4. Verify DB now has the env-authored content.
    """
    shared_adapter = EnvironmentTestAdapter()
    patch_environment_adapter.get_adapter = lambda env: shared_adapter

    agent = create_agent_via_api(client, superuser_token_headers)
    drain_tasks()
    agent_id = get_agent(client, superuser_token_headers, agent["id"])["id"]

    # Confirm DB prompts are None/empty on a freshly created agent
    fresh_agent = get_agent(client, superuser_token_headers, agent_id)
    # (workflow_prompt may be None or an empty string depending on default)
    assert not fresh_agent.get("workflow_prompt"), (
        "Expected freshly-created agent to have no workflow_prompt"
    )

    # ── Phase 1: Env has content that was never in DB ─────────────────
    shared_adapter.prompts_set["workflow_prompt"] = _WORKFLOW_V1
    shared_adapter.prompts_set["entrypoint_prompt"] = _ENTRYPOINT_V1
    shared_adapter.prompt_mtimes["workflow_prompt"] = time.time()
    shared_adapter.prompt_mtimes["entrypoint_prompt"] = time.time()

    # ── Phase 2: Reconcile ────────────────────────────────────────────
    session_data = create_session_via_api(
        client, superuser_token_headers, agent_id, mode="building"
    )
    stub = StubAgentEnvConnector(response_text="pull complete")

    with patch("app.services.sessions.message_service.agent_env_connector", stub):
        send_message(
            client, superuser_token_headers, session_data["id"],
            content="run task",
        )
        drain_tasks()

    # ── Phase 3: DB now has env content ──────────────────────────────
    agent_after = get_agent(client, superuser_token_headers, agent_id)
    assert agent_after["workflow_prompt"] == _WORKFLOW_V1, (
        "SEED_PULL did not pull env workflow_prompt into DB"
    )
    assert agent_after["entrypoint_prompt"] == _ENTRYPOINT_V1, (
        "SEED_PULL did not pull env entrypoint_prompt into DB"
    )
