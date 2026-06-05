"""Unit tests for model_catalog.py — pure data + deterministic functions.

No database, no HTTP, no filesystem — all pure Python.

Coverage:
  1. resolve_model — every (engine, provider, mode) with and without override
  2. resolve_model — claude-sonnet-4-6 is the opencode/anthropic/building default
  3. resolve_model — override honored verbatim (no catalog lookup)
  4. resolve_model — openai_compatible uses passed model, falls back to literal
  5. resolve_model — unknown (engine, provider) falls back to engine/anthropic (no crash)
  6. is_retired    — bare and provider/-prefixed forms; tier words never retired
  7. infer_tier    — representative FAST / BALANCED / None cases
  8. infer_tier    — "mini" inside "gemini" must not match the mini BALANCED marker
"""
from __future__ import annotations

import pytest

from app.services.environments.model_catalog import (
    DEFAULT_CATALOG,
    KNOWN_TIER_WORDS,
    RETIRED_MODELS,
    ModelTier,
    infer_tier,
    is_known_word,
    is_retired,
    resolve_model,
)


# ---------------------------------------------------------------------------
# resolve_model — full catalog coverage
# ---------------------------------------------------------------------------

class TestResolveModelCatalog:
    """Every (engine, provider, mode) row in DEFAULT_CATALOG, no override."""

    # ── claude-code / anthropic ────────────────────────────────────────────

    def test_claude_code_anthropic_conversation(self):
        result = resolve_model("claude-code", "anthropic", "conversation", None)
        assert result == "haiku"

    def test_claude_code_anthropic_building(self):
        result = resolve_model("claude-code", "anthropic", "building", None)
        assert result == "sonnet"

    # ── claude-code / minimax ──────────────────────────────────────────────

    def test_claude_code_minimax_conversation(self):
        result = resolve_model("claude-code", "minimax", "conversation", None)
        assert result == "MiniMax-M2.1-lightning"

    def test_claude_code_minimax_building(self):
        result = resolve_model("claude-code", "minimax", "building", None)
        assert result == "MiniMax-M2.1"

    # ── opencode / anthropic ───────────────────────────────────────────────

    def test_opencode_anthropic_conversation(self):
        result = resolve_model("opencode", "anthropic", "conversation", None)
        assert result == "anthropic/claude-haiku-4-5"

    def test_opencode_anthropic_building_is_sonnet_4_6(self):
        """The freshness fix: building default must be claude-sonnet-4-6, not -4-5."""
        result = resolve_model("opencode", "anthropic", "building", None)
        assert result == "anthropic/claude-sonnet-4-6", (
            "opencode/anthropic building default must be claude-sonnet-4-6 "
            "(the P1 freshness fix); got: " + repr(result)
        )

    # ── opencode / openai ──────────────────────────────────────────────────

    def test_opencode_openai_conversation(self):
        result = resolve_model("opencode", "openai", "conversation", None)
        assert result == "openai/gpt-5.4-nano"

    def test_opencode_openai_building(self):
        result = resolve_model("opencode", "openai", "building", None)
        assert result == "openai/gpt-5.4-mini"

    # ── opencode / google ──────────────────────────────────────────────────

    def test_opencode_google_conversation(self):
        result = resolve_model("opencode", "google", "conversation", None)
        assert result == "google/gemini-2.5-flash"

    def test_opencode_google_building(self):
        result = resolve_model("opencode", "google", "building", None)
        assert result == "google/gemini-2.5-pro"


# ---------------------------------------------------------------------------
# resolve_model — override honored verbatim
# ---------------------------------------------------------------------------

class TestResolveModelOverride:
    """An explicit override must be returned verbatim regardless of catalog."""

    def test_override_wins_over_catalog_claude_code_anthropic(self):
        result = resolve_model(
            "claude-code", "anthropic", "building",
            override="claude-opus-4-custom-experimental",
        )
        assert result == "claude-opus-4-custom-experimental"

    def test_override_wins_over_catalog_opencode_anthropic(self):
        result = resolve_model(
            "opencode", "anthropic", "conversation",
            override="anthropic/claude-haiku-4-special",
        )
        assert result == "anthropic/claude-haiku-4-special"

    def test_override_wins_for_openai_compatible(self):
        """Even openai_compatible (which has no catalog entry) respects override."""
        result = resolve_model(
            "opencode", "openai_compatible", "building",
            override="my-custom-model",
            openai_compatible_model="other-model",
        )
        assert result == "my-custom-model"

    def test_empty_string_override_falls_through_to_catalog(self):
        """Empty string is falsy — catalog fallback applies."""
        result = resolve_model("claude-code", "anthropic", "conversation", override="")
        assert result == "haiku"

    def test_none_override_falls_through_to_catalog(self):
        result = resolve_model("opencode", "anthropic", "building", override=None)
        assert result == "anthropic/claude-sonnet-4-6"


# ---------------------------------------------------------------------------
# resolve_model — openai_compatible
# ---------------------------------------------------------------------------

class TestResolveModelOpenaiCompatible:
    """openai_compatible has no catalog entry; uses openai_compatible_model param."""

    def test_uses_passed_model(self):
        result = resolve_model(
            "opencode", "openai_compatible", "conversation",
            override=None,
            openai_compatible_model="mistral-large",
        )
        assert result == "mistral-large"

    def test_falls_back_to_literal_when_no_model_param(self):
        """No model param supplied → literal 'gpt-4' fallback (preserves prior behavior)."""
        result = resolve_model(
            "opencode", "openai_compatible", "building",
            override=None,
            openai_compatible_model=None,
        )
        assert result == "gpt-4"

    def test_conversation_mode_same_behavior(self):
        result = resolve_model(
            "opencode", "openai_compatible", "conversation",
            override=None,
            openai_compatible_model=None,
        )
        assert result == "gpt-4"


# ---------------------------------------------------------------------------
# resolve_model — unknown (engine, provider) fallback
# ---------------------------------------------------------------------------

class TestResolveModelUnknownFallback:
    """Unknown (engine, provider) must not raise; falls back to engine/anthropic row."""

    def test_unknown_provider_falls_back_to_engine_anthropic(self):
        """For a known engine with an unknown provider, fall back to engine/anthropic."""
        result = resolve_model("opencode", "unknown_provider", "building", override=None)
        # Should fall back to opencode/anthropic which yields the BALANCED model
        expected = DEFAULT_CATALOG[("opencode", "anthropic")][ModelTier.BALANCED]
        assert result == expected

    def test_unknown_engine_falls_back_to_claude_code_anthropic(self):
        """For a completely unknown engine, fall back to claude-code/anthropic."""
        result = resolve_model("totally-new-engine", "unknown_provider", "conversation", override=None)
        expected = DEFAULT_CATALOG[("claude-code", "anthropic")][ModelTier.FAST]
        assert result == expected

    def test_unknown_engine_known_anthropic_provider_uses_claude_code_anthropic_fallback(self):
        """Engine row missing → falls back to that engine's anthropic row, then claude-code."""
        result = resolve_model("totally-new-engine", "anthropic", "building", override=None)
        # Engine not in catalog → last resort is claude-code/anthropic BALANCED = "sonnet"
        assert result == "sonnet"

    def test_does_not_raise_for_unknown_pair(self):
        """Critical: must never raise even for completely unknown (engine, provider)."""
        result = resolve_model("future-engine", "future-provider", "building", override=None)
        assert isinstance(result, str)
        assert len(result) > 0


# ---------------------------------------------------------------------------
# is_retired — bare and provider-prefixed forms
# ---------------------------------------------------------------------------

class TestIsRetired:
    """Lenient matching: bare and provider/-prefixed forms both match."""

    def test_bare_anthropic_retired_model(self):
        assert is_retired("claude-3-opus-20240229") is True

    def test_provider_prefixed_anthropic_retired_model(self):
        assert is_retired("anthropic/claude-3-opus-20240229") is True

    def test_bare_openai_retired_model(self):
        assert is_retired("gpt-3.5-turbo-0301") is True

    def test_bare_google_retired_model(self):
        assert is_retired("gemini-1.5-pro") is True

    def test_claude_2_retired(self):
        assert is_retired("claude-2.0") is True
        assert is_retired("claude-2.1") is True

    def test_claude_3_5_haiku_retired(self):
        assert is_retired("claude-3-5-haiku-20241022") is True

    def test_claude_3_7_sonnet_retired(self):
        assert is_retired("claude-3-7-sonnet-20250219") is True

    def test_current_opencode_anthropic_building_default_not_retired(self):
        """claude-sonnet-4-6 (the freshness-fix default) must NOT be retired."""
        assert is_retired("claude-sonnet-4-6") is False
        assert is_retired("anthropic/claude-sonnet-4-6") is False

    def test_current_opencode_anthropic_conversation_not_retired(self):
        assert is_retired("claude-haiku-4-5") is False
        assert is_retired("anthropic/claude-haiku-4-5") is False

    def test_empty_string_not_retired(self):
        assert is_retired("") is False

    def test_arbitrary_unknown_model_not_retired(self):
        assert is_retired("some-future-model-v99") is False

    # Tier words must NEVER be flagged as retired
    def test_haiku_tier_word_not_retired(self):
        assert is_retired("haiku") is False

    def test_sonnet_tier_word_not_retired(self):
        assert is_retired("sonnet") is False

    def test_opus_tier_word_not_retired(self):
        assert is_retired("opus") is False


# ---------------------------------------------------------------------------
# is_known_word
# ---------------------------------------------------------------------------

class TestIsKnownWord:
    """Tier words: haiku, sonnet, opus."""

    def test_haiku_is_known_word(self):
        assert is_known_word("haiku") is True

    def test_sonnet_is_known_word(self):
        assert is_known_word("sonnet") is True

    def test_opus_is_known_word(self):
        assert is_known_word("opus") is True

    def test_concrete_id_not_a_known_word(self):
        assert is_known_word("anthropic/claude-haiku-4-5") is False
        assert is_known_word("claude-3-5-haiku-20241022") is False

    def test_empty_string_not_known_word(self):
        assert is_known_word("") is False

    def test_unrelated_word_not_known_word(self):
        assert is_known_word("gpt-5") is False


# ---------------------------------------------------------------------------
# infer_tier — representative FAST / BALANCED / None cases
# ---------------------------------------------------------------------------

class TestInferTier:
    """Name-based tier inference: haiku/nano/flash/lightning→FAST,
    sonnet/opus/mini/pro/gpt-5→BALANCED, ambiguous→None."""

    # ── FAST markers ──────────────────────────────────────────────────────

    def test_haiku_infers_fast(self):
        assert infer_tier("anthropic", "claude-haiku-4-5") == ModelTier.FAST

    def test_haiku_snapshot_infers_fast(self):
        assert infer_tier("anthropic", "claude-3-5-haiku-20241022") == ModelTier.FAST

    def test_nano_infers_fast(self):
        assert infer_tier("openai", "gpt-5.4-nano") == ModelTier.FAST

    def test_flash_infers_fast(self):
        assert infer_tier("google", "gemini-2.5-flash") == ModelTier.FAST

    def test_lightning_infers_fast(self):
        assert infer_tier("minimax", "MiniMax-M2.1-lightning") == ModelTier.FAST

    # ── BALANCED markers ──────────────────────────────────────────────────

    def test_sonnet_infers_balanced(self):
        assert infer_tier("anthropic", "claude-sonnet-4-6") == ModelTier.BALANCED

    def test_opus_infers_balanced(self):
        assert infer_tier("anthropic", "claude-opus-4") == ModelTier.BALANCED

    def test_mini_infers_balanced(self):
        assert infer_tier("openai", "gpt-5.4-mini") == ModelTier.BALANCED

    def test_pro_infers_balanced(self):
        assert infer_tier("google", "gemini-2.5-pro") == ModelTier.BALANCED

    def test_gpt5_bare_infers_balanced(self):
        """Bare 'gpt-5' family marker (no size suffix) → BALANCED."""
        assert infer_tier("openai", "gpt-5") == ModelTier.BALANCED

    # ── None (ambiguous / unknown) ────────────────────────────────────────

    def test_empty_model_infers_none(self):
        assert infer_tier("anthropic", "") is None

    def test_unknown_model_infers_none(self):
        assert infer_tier("unknown", "some-brand-new-model-v3") is None

    # ── Key regression: "mini" must not match INSIDE "gemini" ────────────

    def test_mini_inside_gemini_does_not_infer_balanced(self):
        """'gemini-2.5-pro' tokenizes to {'gemini', '2', '5', 'pro'}.
        The 'mini' token must NOT appear, so this must be BALANCED (pro), not ambiguous."""
        result = infer_tier("google", "gemini-2.5-pro")
        # 'gemini' tokens: ['gemini', '2', '5', 'pro'] → no 'mini' token → BALANCED
        assert result == ModelTier.BALANCED

    def test_gemini_model_without_pro_or_flash_is_none(self):
        """'gemini-2.5' alone has no tier marker → None."""
        result = infer_tier("google", "gemini-2.5")
        assert result is None

    def test_conflicting_fast_and_balanced_is_none(self):
        """Both fast and balanced markers in one name → ambiguous → None."""
        result = infer_tier("hypothetical", "hypothetical-haiku-pro")
        assert result is None
