"""Unit tests for model_health_service.py — classification matrix.

Covers the full status classification for every meaningful (override, discovered_models)
combination. All DB interactions are mocked via MagicMock session + stubbed objects.

Coverage:
  1.  Retired override        → retired_override / frozen_override cause + edit CTA
  2.  Stale default (no discovery) → unknown_model / stale_default cause + restart CTA
  3.  Regression: undated catalog default WITH dated discovered snapshot → ok (no false flag)
  4.  claude-code tier words  → always ok (regardless of discovery data)
  5.  openai_compatible       → always ok
  6.  Credential present, discovered_models=None → unverified (no alarm)
  7.  Credential missing entirely → ok (no alarm, no crash)
  8.  has_warning roll-up     → True when any mode is flagged
  9.  Both modes ok           → has_warning=False
  10. evaluate_environment exception safety — never raises, degrades to healthy
  11. evaluate_environment with pre-loaded agent → owner_id used directly
  12. OpenAI default not in discovered list → unknown_model/stale_default
  13. Google default not in discovered list → unknown_model/stale_default
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from app.models.credentials.ai_credential import AICredential, AICredentialType
from app.models.environments.environment import (
    AgentEnvironment,
    ModelHealthMode,
    ModelHealthPublic,
)
from app.services.environments.model_health_service import (
    CAUSE_FROZEN_OVERRIDE,
    CAUSE_STALE_DEFAULT,
    STATUS_OK,
    STATUS_RETIRED_OVERRIDE,
    STATUS_UNKNOWN_MODEL,
    STATUS_UNVERIFIED,
    evaluate_environment,
)


# ---------------------------------------------------------------------------
# Helpers — build lightweight stub objects
# ---------------------------------------------------------------------------

def _env(
    agent_id: uuid.UUID | None = None,
    sdk_conversation: str | None = "claude-code/anthropic",
    sdk_building: str | None = "claude-code/anthropic",
    override_conversation: str | None = None,
    override_building: str | None = None,
    conv_cred_id: uuid.UUID | None = None,
    build_cred_id: uuid.UUID | None = None,
) -> MagicMock:
    """Build a minimal AgentEnvironment mock for testing.

    SQLModel table models require a live DB session for SQLAlchemy
    instrumentation; MagicMock(spec=AgentEnvironment) satisfies attribute
    access without touching the database.
    """
    e = MagicMock(spec=AgentEnvironment)
    e.id = uuid.uuid4()
    e.agent_id = agent_id or uuid.uuid4()
    e.agent_sdk_conversation = sdk_conversation
    e.agent_sdk_building = sdk_building
    e.model_override_conversation = override_conversation
    e.model_override_building = override_building
    e.conversation_ai_credential_id = conv_cred_id
    e.building_ai_credential_id = build_cred_id
    e.env_name = "test-env"
    e.env_version = "1.0.0"
    e.instance_name = "Test"
    e.type = "docker"
    e.status = "running"
    e.is_active = True
    e.config = {}
    return e


def _agent(owner_id: uuid.UUID | None = None) -> MagicMock:
    """Build a minimal Agent mock."""
    a = MagicMock()
    a.id = uuid.uuid4()
    a.owner_id = owner_id or uuid.uuid4()
    a.name = "Test Agent"
    return a


def _cred(
    cred_type: AICredentialType = AICredentialType.ANTHROPIC,
    discovered_models: list[str] | None = None,
    models_discovery_error: str | None = None,
) -> AICredential:
    """Build a minimal AICredential mock."""
    c = MagicMock(spec=AICredential)
    c.id = uuid.uuid4()
    c.type = cred_type
    c.discovered_models = discovered_models
    c.models_discovered_at = datetime.now(UTC) if discovered_models is not None else None
    c.models_discovery_error = models_discovery_error
    return c


def _session_with(
    agent: Any | None = None,
    cred_for_building: AICredential | None = None,
    cred_for_conversation: AICredential | None = None,
) -> MagicMock:
    """Build a mock session that returns the given agent + credentials."""

    def _get(model_class, pk):
        from app.models.agents.agent import Agent
        from app.models.credentials.ai_credential import AICredential as _AICredential
        if model_class is Agent:
            return agent
        if model_class is _AICredential:
            # Return cred by pk matching
            if cred_for_building and str(pk) == str(cred_for_building.id):
                return cred_for_building
            if cred_for_conversation and str(pk) == str(cred_for_conversation.id):
                return cred_for_conversation
        return None

    session = MagicMock()
    session.get.side_effect = _get
    return session


# ---------------------------------------------------------------------------
# 1. Retired override → retired_override
# ---------------------------------------------------------------------------

class TestRetiredOverride:
    """A frozen model_override pointing at a retired model → STATUS_RETIRED_OVERRIDE."""

    def test_building_override_retired_no_discovery_data(self):
        """Building override is in RETIRED_MODELS; no discovery data → retired_override."""
        retired_id = "claude-3-7-sonnet-20250219"  # In RETIRED_MODELS
        agent = _agent()
        env = _env(
            agent_id=agent.owner_id,  # agent_id is separate from owner_id
            override_building=retired_id,
        )
        env.agent_id = agent.id if hasattr(agent, "id") else uuid.uuid4()
        # No credential linked → session.get(AICredential, None) → None
        session = _session_with(agent=agent)

        result = evaluate_environment(session, env, agent=agent)

        building_mode = next(m for m in result.modes if m.mode == "building")
        assert building_mode.status == STATUS_RETIRED_OVERRIDE
        assert building_mode.cause == CAUSE_FROZEN_OVERRIDE
        assert building_mode.cta is not None
        assert "override" in building_mode.cta.lower() or "restart" in building_mode.cta.lower()
        assert result.has_warning is True

    def test_conversation_override_retired_with_discovery_data(self):
        """Retired override against discovered list → retired_override (not unknown_model)."""
        retired_id = "gpt-4-0613"  # In RETIRED_MODELS
        agent = _agent()
        cred_id = uuid.uuid4()
        # discovered_models does NOT contain the retired_id
        cred = _cred(
            AICredentialType.OPENAI,
            discovered_models=["gpt-5.4-nano", "gpt-5.4-mini"],
        )
        cred.id = cred_id

        env = _env(
            sdk_conversation="opencode/openai",
            sdk_building="opencode/openai",
            override_conversation=retired_id,
            conv_cred_id=cred_id,
        )
        env.agent_id = agent.id if hasattr(agent, "id") else uuid.uuid4()

        session = _session_with(agent=agent, cred_for_conversation=cred)

        result = evaluate_environment(session, env, agent=agent)

        conv_mode = next(m for m in result.modes if m.mode == "conversation")
        assert conv_mode.status == STATUS_RETIRED_OVERRIDE
        assert conv_mode.cause == CAUSE_FROZEN_OVERRIDE
        assert result.has_warning is True


# ---------------------------------------------------------------------------
# 2. Stale default (no override, not in discovered list) → unknown_model
# ---------------------------------------------------------------------------

class TestStalDefault:
    """Resolved catalog default not in discovered list → STATUS_UNKNOWN_MODEL."""

    def test_opencode_anthropic_building_default_not_in_discovered(self):
        """Discovered list lacks the current catalog default → stale_default."""
        agent = _agent()
        cred_id = uuid.uuid4()
        # A discovered list that lacks 'claude-sonnet-4-6' (the current opencode/anthropic/building default)
        cred = _cred(
            AICredentialType.ANTHROPIC,
            discovered_models=["claude-3-5-sonnet-20241022", "claude-3-haiku-20240307"],
        )
        cred.id = cred_id

        env = _env(
            sdk_conversation="opencode/anthropic",
            sdk_building="opencode/anthropic",
            override_building=None,
            build_cred_id=cred_id,
        )
        env.agent_id = uuid.uuid4()

        session = _session_with(agent=agent, cred_for_building=cred)

        result = evaluate_environment(session, env, agent=agent)

        building_mode = next(m for m in result.modes if m.mode == "building")
        assert building_mode.status == STATUS_UNKNOWN_MODEL
        assert building_mode.cause == CAUSE_STALE_DEFAULT
        assert building_mode.cta is not None
        assert result.has_warning is True


# ---------------------------------------------------------------------------
# 3. Regression: undated catalog default WITH dated discovered snapshot → ok
# ---------------------------------------------------------------------------

class TestUndatedDefaultWithDatedSnapshot:
    """KEY REGRESSION: undated catalog default ('claude-sonnet-4-6') must match the
    dated snapshot variant ('claude-sonnet-4-6-20251231') in discovered_models.
    Without the _normalize_model_id stem-matching logic, this would false-flag 'ok'
    models as unknown_model/stale_default."""

    def test_undated_default_matches_dated_discovered_snapshot(self):
        """opencode/anthropic building default 'anthropic/claude-sonnet-4-6' must
        match 'claude-sonnet-4-6-20251231' in discovered_models → ok (no false flag)."""
        agent = _agent()
        cred_id = uuid.uuid4()

        # The catalog default is 'anthropic/claude-sonnet-4-6' (undated).
        # The provider API returns a dated snapshot: 'claude-sonnet-4-6-20251231'.
        # _normalize_model_id should strip the date and provider prefix, making them equal.
        cred = _cred(
            AICredentialType.ANTHROPIC,
            discovered_models=["claude-sonnet-4-6-20251231", "claude-haiku-4-5-20250929"],
        )
        cred.id = cred_id

        env = _env(
            sdk_conversation="opencode/anthropic",
            sdk_building="opencode/anthropic",
            build_cred_id=cred_id,
            conv_cred_id=cred_id,
        )
        env.agent_id = uuid.uuid4()

        session = _session_with(agent=agent, cred_for_building=cred, cred_for_conversation=cred)

        result = evaluate_environment(session, env, agent=agent)

        building_mode = next(m for m in result.modes if m.mode == "building")
        assert building_mode.status == STATUS_OK, (
            f"Undated catalog default should match dated snapshot via stem-normalization. "
            f"Got status={building_mode.status!r}. "
            "This is the P4 false-flag regression."
        )
        assert result.has_warning is False

    def test_undated_default_with_only_same_family_discovered(self):
        """If the discovered list has a DIFFERENT dated snapshot of the same family
        that resolved to a different stem, it should still not flag ok.

        E.g. catalog='claude-sonnet-4-6', discovered=['claude-sonnet-4-5-20250929']
        These differ by generation (4-5 vs 4-6) → NOT the same family → unknown_model.
        """
        agent = _agent()
        cred_id = uuid.uuid4()

        # Different generation: 4-5 vs 4-6. Stems differ → NOT a match.
        cred = _cred(
            AICredentialType.ANTHROPIC,
            discovered_models=["claude-sonnet-4-5-20250929"],
        )
        cred.id = cred_id

        env = _env(
            sdk_building="opencode/anthropic",
            sdk_conversation="opencode/anthropic",
            build_cred_id=cred_id,
        )
        env.agent_id = uuid.uuid4()

        session = _session_with(agent=agent, cred_for_building=cred)

        result = evaluate_environment(session, env, agent=agent)

        building_mode = next(m for m in result.modes if m.mode == "building")
        # Catalog default is claude-sonnet-4-6 (stem: claude-sonnet-4-6)
        # Discovered is claude-sonnet-4-5-20250929 (stem: claude-sonnet-4-5) → NO match
        assert building_mode.status == STATUS_UNKNOWN_MODEL


# ---------------------------------------------------------------------------
# 4. claude-code tier words always ok
# ---------------------------------------------------------------------------

class TestTierWordsAlwaysOk:
    """claude-code tier words (haiku/sonnet/opus) are always STATUS_OK."""

    def test_claude_code_anthropic_conversation_tier_word_ok(self):
        """No override → 'haiku' tier word → ok."""
        agent = _agent()
        env = _env(
            sdk_conversation="claude-code/anthropic",
            sdk_building="claude-code/anthropic",
        )
        env.agent_id = uuid.uuid4()
        session = _session_with(agent=agent)

        result = evaluate_environment(session, env, agent=agent)

        conv_mode = next(m for m in result.modes if m.mode == "conversation")
        build_mode = next(m for m in result.modes if m.mode == "building")
        assert conv_mode.status == STATUS_OK
        assert build_mode.status == STATUS_OK
        assert result.has_warning is False

    def test_claude_code_anthropic_even_with_no_credential(self):
        """Tier words classified ok even when no credential is linked at all."""
        agent = _agent()
        env = _env(sdk_conversation="claude-code/anthropic", sdk_building="claude-code/anthropic")
        env.agent_id = uuid.uuid4()

        # Session returns no credential (get returns None)
        session = MagicMock()
        session.get.return_value = None  # no agent either, pass agent= explicitly

        result = evaluate_environment(session, env, agent=agent)

        for m in result.modes:
            assert m.status == STATUS_OK
        assert result.has_warning is False


# ---------------------------------------------------------------------------
# 5. openai_compatible always ok
# ---------------------------------------------------------------------------

class TestOpenaiCompatibleAlwaysOk:
    """openai_compatible provider → STATUS_OK regardless of model value."""

    def test_openai_compatible_building_ok(self):
        agent = _agent()
        cred_id = uuid.uuid4()
        # Even with a (non-retired) custom model, should be ok
        cred = _cred(AICredentialType.OPENAI_COMPATIBLE, discovered_models=["my-model"])
        cred.id = cred_id

        env = _env(
            sdk_building="opencode/openai_compatible",
            sdk_conversation="opencode/openai_compatible",
            build_cred_id=cred_id,
            conv_cred_id=cred_id,
        )
        env.agent_id = uuid.uuid4()
        session = _session_with(agent=agent, cred_for_building=cred, cred_for_conversation=cred)

        result = evaluate_environment(session, env, agent=agent)

        for m in result.modes:
            assert m.status == STATUS_OK
        assert result.has_warning is False

    def test_openai_compatible_with_override_still_ok(self):
        """Override on openai_compatible is also ok — user owns the model namespace."""
        agent = _agent()
        env = _env(
            sdk_building="opencode/openai_compatible",
            sdk_conversation="opencode/openai_compatible",
            override_building="some-custom-model",
        )
        env.agent_id = uuid.uuid4()
        session = _session_with(agent=agent)

        result = evaluate_environment(session, env, agent=agent)

        for m in result.modes:
            assert m.status == STATUS_OK


# ---------------------------------------------------------------------------
# 6. discovered_models is None → unverified
# ---------------------------------------------------------------------------

class TestUnverifiedWhenNoDiscoveryData:
    """Credential present but discovered_models=None → STATUS_UNVERIFIED (quiet)."""

    def test_credential_present_no_discovery_returns_unverified(self):
        """A non-MINIMAX credential with discovered_models=None → unverified."""
        agent = _agent()
        cred_id = uuid.uuid4()
        # discovered_models=None means never discovered
        cred = _cred(AICredentialType.ANTHROPIC, discovered_models=None)
        cred.id = cred_id

        env = _env(
            sdk_building="opencode/anthropic",
            sdk_conversation="opencode/anthropic",
            build_cred_id=cred_id,
            conv_cred_id=cred_id,
        )
        env.agent_id = uuid.uuid4()
        session = _session_with(agent=agent, cred_for_building=cred, cred_for_conversation=cred)

        result = evaluate_environment(session, env, agent=agent)

        for m in result.modes:
            assert m.status == STATUS_UNVERIFIED, (
                f"Mode {m.mode!r}: expected 'unverified', got {m.status!r}. "
                "A credential with no discovery data should not alarm."
            )
        # unverified is NOT a warning — no false alarm
        assert result.has_warning is False

    def test_minimax_credential_no_discovery_is_ok_not_unverified(self):
        """MiniMax has no list endpoint → discovery is always skipped.
        The service should NOT flag it as unverified but treat it as ok."""
        agent = _agent()
        cred_id = uuid.uuid4()
        cred = _cred(AICredentialType.MINIMAX, discovered_models=None)
        cred.id = cred_id

        env = _env(
            sdk_building="claude-code/minimax",
            sdk_conversation="claude-code/minimax",
            build_cred_id=cred_id,
            conv_cred_id=cred_id,
        )
        env.agent_id = uuid.uuid4()

        # claude-code/minimax uses concrete model IDs from catalog (not tier words)
        session = _session_with(agent=agent, cred_for_building=cred, cred_for_conversation=cred)

        result = evaluate_environment(session, env, agent=agent)

        # MiniMax with no discovery data should not be unverified — ok or at worst ok.
        for m in result.modes:
            assert m.status == STATUS_OK, (
                f"MiniMax with no discovery should be ok (not unverified). Got {m.status!r}."
            )


# ---------------------------------------------------------------------------
# 7. has_warning roll-up
# ---------------------------------------------------------------------------

class TestHasWarningRollup:
    """has_warning is True if and only if any mode is retired_override or unknown_model."""

    def test_has_warning_true_when_building_flagged(self):
        """One flagged mode → has_warning=True."""
        agent = _agent()
        env = _env(
            sdk_building="claude-code/anthropic",
            sdk_conversation="claude-code/anthropic",
            override_building="claude-3-opus-20240229",  # retired
        )
        env.agent_id = uuid.uuid4()
        session = _session_with(agent=agent)

        result = evaluate_environment(session, env, agent=agent)
        assert result.has_warning is True

    def test_has_warning_false_when_both_ok(self):
        """Both modes ok → has_warning=False."""
        agent = _agent()
        env = _env(
            sdk_conversation="claude-code/anthropic",
            sdk_building="claude-code/anthropic",
        )
        env.agent_id = uuid.uuid4()
        session = _session_with(agent=agent)

        result = evaluate_environment(session, env, agent=agent)
        assert result.has_warning is False

    def test_has_warning_false_when_all_unverified(self):
        """unverified is not a warning — has_warning must be False."""
        agent = _agent()
        cred_id = uuid.uuid4()
        cred = _cred(AICredentialType.ANTHROPIC, discovered_models=None)
        cred.id = cred_id

        env = _env(
            sdk_conversation="opencode/anthropic",
            sdk_building="opencode/anthropic",
            conv_cred_id=cred_id,
            build_cred_id=cred_id,
        )
        env.agent_id = uuid.uuid4()
        session = _session_with(agent=agent, cred_for_building=cred, cred_for_conversation=cred)

        result = evaluate_environment(session, env, agent=agent)
        assert result.has_warning is False


# ---------------------------------------------------------------------------
# 8. Exception safety
# ---------------------------------------------------------------------------

class TestEvaluateEnvironmentExceptionSafety:
    """evaluate_environment must never raise; degrades gracefully to healthy roll-up."""

    def test_exception_in_session_get_degrades_to_healthy(self):
        """If session.get raises unexpectedly, result is a healthy roll-up (no crash)."""
        env = _env()
        env.agent_id = uuid.uuid4()

        session = MagicMock()
        session.get.side_effect = RuntimeError("DB connection lost")

        result = evaluate_environment(session, env)

        # Must not raise; returns a safe healthy fallback
        assert isinstance(result, ModelHealthPublic)
        assert result.has_warning is False

    def test_none_agent_returns_healthy(self):
        """If the agent record doesn't exist (orphan env), returns healthy roll-up."""
        env = _env()
        env.agent_id = uuid.uuid4()

        session = MagicMock()
        session.get.return_value = None  # agent not found

        result = evaluate_environment(session, env)
        assert isinstance(result, ModelHealthPublic)
        assert result.has_warning is False


# ---------------------------------------------------------------------------
# 9. Pre-loaded agent bypass
# ---------------------------------------------------------------------------

class TestPreLoadedAgent:
    """Passing agent= to evaluate_environment avoids the session.get(Agent) call."""

    def test_agent_passed_directly_not_re_fetched(self):
        """session.get should NOT be called for Agent when agent= is supplied."""
        agent = _agent()
        env = _env(sdk_conversation="claude-code/anthropic", sdk_building="claude-code/anthropic")
        env.agent_id = uuid.uuid4()

        session = MagicMock()

        result = evaluate_environment(session, env, agent=agent)

        # session.get should only be called for credential lookups, not Agent.
        # Tier words always ok → no credential lookup either.
        assert isinstance(result, ModelHealthPublic)
        # Verify session.get was NOT called with Agent class.
        from app.models.agents.agent import Agent
        for call in session.get.call_args_list:
            assert call.args[0] is not Agent, (
                "session.get(Agent, ...) should not be called when agent= is passed"
            )


# ---------------------------------------------------------------------------
# 10. OpenAI and Google defaults not in discovered list → unknown_model
# ---------------------------------------------------------------------------

class TestOpenaiAndGoogleStalDefault:
    """Catalog defaults not present in discovered list are flagged unknown_model."""

    def test_openai_building_default_not_in_discovered(self):
        """Discovered list doesn't include the catalog building default for opencode/openai."""
        agent = _agent()
        cred_id = uuid.uuid4()
        # gpt-5.4-mini is the catalog building default; it's not in this discovered list.
        cred = _cred(
            AICredentialType.OPENAI,
            discovered_models=["gpt-3.5-turbo", "gpt-4o"],
        )
        cred.id = cred_id

        env = _env(
            sdk_building="opencode/openai",
            sdk_conversation="opencode/openai",
            build_cred_id=cred_id,
        )
        env.agent_id = uuid.uuid4()
        session = _session_with(agent=agent, cred_for_building=cred)

        result = evaluate_environment(session, env, agent=agent)

        build_mode = next(m for m in result.modes if m.mode == "building")
        assert build_mode.status == STATUS_UNKNOWN_MODEL
        assert build_mode.cause == CAUSE_STALE_DEFAULT

    def test_google_building_default_not_in_discovered(self):
        """Catalog google building default (gemini-2.5-pro) not in discovered list → flagged."""
        agent = _agent()
        cred_id = uuid.uuid4()
        # gemini-2.5-pro is the catalog default; not in this discovered list.
        cred = _cred(
            AICredentialType.GOOGLE,
            discovered_models=["gemini-1.5-flash", "gemini-1.0-pro"],
        )
        cred.id = cred_id

        env = _env(
            sdk_building="opencode/google",
            sdk_conversation="opencode/google",
            build_cred_id=cred_id,
        )
        env.agent_id = uuid.uuid4()
        session = _session_with(agent=agent, cred_for_building=cred)

        result = evaluate_environment(session, env, agent=agent)

        build_mode = next(m for m in result.modes if m.mode == "building")
        assert build_mode.status == STATUS_UNKNOWN_MODEL
