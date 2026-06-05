"""
Centralized Model Catalog — single source of truth for default LLM model
selection per (engine, provider, tier).

This module replaces the three scattered hardcoded model maps that previously
lived in ``environment_lifecycle.py`` (the OpenCode ``_default_models`` map,
the MiniMax settings literals) and the claude-code adapter's hardcoded model
choice. It is intentionally **pure**: no DB access, no I/O, no network — just
static data and deterministic functions. This keeps it trivially unit-testable
and safe to import from any layer (lifecycle generation, the claude-code env
plumbing, and the future model-health service).

Tier intent:
- ``conversation`` mode maps to the FAST tier (cheap/low-latency model).
- ``building`` mode maps to the BALANCED tier (more capable default).

For ``claude-code/anthropic`` the catalog stores tier WORDS (``haiku`` /
``sonnet``) rather than concrete dated snapshots — the Claude Code CLI resolves
these to the current model automatically, so they never go stale and are never
flagged as retired (see ``KNOWN_TIER_WORDS``). For all other engine/provider
pairs the catalog stores concrete provider-qualified model IDs.
"""

from __future__ import annotations

import logging
import re
from enum import Enum

logger = logging.getLogger(__name__)


class ModelTier(str, Enum):
    """Capability/cost tier for default model selection.

    Two tiers are used today (conversation→FAST, building→BALANCED). The enum
    intentionally leaves room for a future POWERFUL tier without churn.
    """

    FAST = "fast"
    BALANCED = "balanced"
    # Reserved for future use; not currently populated in DEFAULT_CATALOG.
    POWERFUL = "powerful"


# Per-mode → tier mapping. Conversation is the cheap/fast path; building is the
# more capable default.
MODE_TO_TIER: dict[str, ModelTier] = {
    "conversation": ModelTier.FAST,
    "building": ModelTier.BALANCED,
}


# Single source of truth for default models, keyed by (engine, provider).
#
# - claude-code/anthropic stores TIER WORDS — the CLI auto-resolves them, so
#   they track the current model and are never stale.
# - claude-code/minimax and all opencode/* store concrete model IDs.
# - opencode/openai_compatible has no static default; resolve_model() pulls the
#   model from the credential config (with a sane literal fallback).
DEFAULT_CATALOG: dict[tuple[str, str], dict[ModelTier, str]] = {
    ("claude-code", "anthropic"): {
        ModelTier.FAST: "haiku",
        ModelTier.BALANCED: "sonnet",
    },
    ("claude-code", "minimax"): {
        ModelTier.FAST: "MiniMax-M2.1-lightning",
        ModelTier.BALANCED: "MiniMax-M2.1",
    },
    ("opencode", "anthropic"): {
        ModelTier.FAST: "anthropic/claude-haiku-4-5",
        # Intentional freshness fix: previous live default was
        # "anthropic/claude-sonnet-4-5" (one generation behind).
        ModelTier.BALANCED: "anthropic/claude-sonnet-4-6",
    },
    ("opencode", "openai"): {
        ModelTier.FAST: "openai/gpt-5.4-nano",
        ModelTier.BALANCED: "openai/gpt-5.4-mini",
    },
    ("opencode", "google"): {
        ModelTier.FAST: "google/gemini-2.5-flash",
        ModelTier.BALANCED: "google/gemini-2.5-pro",
    },
    # opencode/openai_compatible has no static catalog entry — the model comes
    # from the user-provided credential config (see resolve_model()).
}


# Fallback literal for openai_compatible when no model is supplied by the
# credential config. Mirrors the prior behavior in environment_lifecycle.py
# (which fell back to "gpt-4").
_OPENAI_COMPATIBLE_FALLBACK = "gpt-4"


# Curated seed list of known-retired concrete model IDs. This is the FALLBACK
# signal used when no per-credential discovery data exists (Phase 3+). Matching
# is lenient (see is_retired): both provider-prefixed (e.g. "anthropic/...")
# and bare forms are recognized.
#
# Tier words (haiku/sonnet/opus) are NEVER listed here — the CLI resolves them
# to the current model, so they are always considered healthy.
RETIRED_MODELS: frozenset[str] = frozenset(
    {
        # Anthropic — superseded snapshots
        "claude-3-5-haiku-20241022",
        "claude-3-5-sonnet-20241022",
        "claude-3-5-sonnet-20240620",
        "claude-3-7-sonnet-20250219",
        "claude-3-opus-20240229",
        "claude-3-sonnet-20240229",
        "claude-3-haiku-20240307",
        "claude-2.1",
        "claude-2.0",
        # OpenAI — retired snapshots / legacy families
        "gpt-3.5-turbo-0301",
        "gpt-3.5-turbo-0613",
        "gpt-4-0314",
        "gpt-4-0613",
        "gpt-4-32k",
        "gpt-4-vision-preview",
        # Google — retired Gemini snapshots
        "gemini-1.0-pro",
        "gemini-1.5-flash",
        "gemini-1.5-pro",
        "gemini-pro",
        "gemini-pro-vision",
    }
)


# claude-code tier words. These are resolved to a concrete model by the Claude
# Code CLI at runtime, so they auto-track the current model and must never be
# flagged as retired.
KNOWN_TIER_WORDS: frozenset[str] = frozenset({"haiku", "sonnet", "opus"})


def _strip_provider_prefix(model: str) -> str:
    """Return the bare model id, dropping any "provider/" prefix.

    e.g. "anthropic/claude-3-opus-20240229" -> "claude-3-opus-20240229".
    A bare id is returned unchanged.
    """
    if "/" in model:
        return model.split("/", 1)[1]
    return model


def is_known_word(model: str) -> bool:
    """True if ``model`` is a claude-code tier word (haiku/sonnet/opus).

    Matching is case-insensitive and tolerant of a provider prefix.
    """
    if not model:
        return False
    bare = _strip_provider_prefix(model).strip().lower()
    return bare in KNOWN_TIER_WORDS


def is_retired(model: str) -> bool:
    """True if ``model`` matches a known-retired id.

    Lenient matching: the model is compared both as-given and with any
    "provider/" prefix stripped, against the bare forms in RETIRED_MODELS.
    Tier words are never considered retired.
    """
    if not model:
        return False
    if is_known_word(model):
        return False
    candidate = model.strip()
    bare = _strip_provider_prefix(candidate)
    return candidate in RETIRED_MODELS or bare in RETIRED_MODELS


def resolve_model(
    engine: str,
    provider: str,
    mode: str,
    override: str | None,
    openai_compatible_model: str | None = None,
) -> str:
    """Resolve the effective model id for a given engine/provider/mode.

    Resolution order:
      1. An explicit, truthy ``override`` is honored verbatim (validation of
         whether the override is still available is Layer 4's job).
      2. ``openai_compatible`` provider → use ``openai_compatible_model`` (the
         model from the credential config), or a sane literal fallback.
      3. Otherwise look up DEFAULT_CATALOG[(engine, provider)][tier], where the
         tier is derived from ``mode`` via MODE_TO_TIER.
      4. Unknown (engine, provider) → fall back to the engine's ``anthropic``
         row and log a warning (no crash).

    Returns a model string appropriate for the engine: a tier word for
    claude-code/anthropic, otherwise a concrete (often provider-qualified) id.
    """
    if override:
        return override

    if provider == "openai_compatible":
        return openai_compatible_model or _OPENAI_COMPATIBLE_FALLBACK

    tier = MODE_TO_TIER.get(mode, ModelTier.BALANCED)

    tier_map = DEFAULT_CATALOG.get((engine, provider))
    if tier_map is None:
        fallback_key = (engine, "anthropic")
        tier_map = DEFAULT_CATALOG.get(fallback_key)
        if tier_map is None:
            # Engine itself is unknown — last-resort to claude-code/anthropic.
            logger.warning(
                "resolve_model: unknown engine '%s' (provider '%s'); "
                "falling back to claude-code/anthropic defaults",
                engine,
                provider,
            )
            tier_map = DEFAULT_CATALOG[("claude-code", "anthropic")]
        else:
            logger.warning(
                "resolve_model: unknown (engine, provider) ('%s', '%s'); "
                "falling back to '%s' anthropic defaults",
                engine,
                provider,
                engine,
            )

    return tier_map[tier]


def infer_tier(provider: str, model_id: str) -> ModelTier | None:  # noqa: ARG001
    """Best-effort, name-based tier inference for a concrete model id.

    Used by discovery (Phase 3+) to map a raw provider model id back to a tier
    (e.g. to suggest a tier-appropriate replacement). Heuristic and brittle by
    design — returns ``None`` when the name is ambiguous rather than guessing.

    ``provider`` is part of the stable signature so callers always pass the
    provider context; it is currently unused (the heuristic is name-only) but
    reserved for future provider-specific rules.

    Rough rules (case-insensitive, on the bare model id):
      - contains haiku / nano / flash / lightning  → FAST
      - contains sonnet / opus / mini / pro → BALANCED
      - a bare family marker (gpt-5) → BALANCED, but a FAST size marker
        (e.g. "nano" in "gpt-5.4-nano") wins over it
      - otherwise → None
    """
    if not model_id:
        return None

    name = _strip_provider_prefix(model_id).strip().lower()
    if not name:
        return None

    # Tokenize on non-alphanumeric separators so markers match whole tokens
    # (avoids e.g. "mini" matching inside "ge-mini" / "gemini").
    tokens = set(re.split(r"[^a-z0-9]+", name))

    # Strong size markers — these take precedence and unambiguously classify.
    fast_markers = {"haiku", "nano", "flash", "lightning"}
    balanced_markers = {"sonnet", "opus", "mini", "pro"}

    has_fast = bool(tokens & fast_markers)
    has_balanced = bool(tokens & balanced_markers)

    # Strong markers first; ambiguous only if both strong classes appear.
    if has_fast and not has_balanced:
        return ModelTier.FAST
    if has_balanced and not has_fast:
        return ModelTier.BALANCED
    if has_fast or has_balanced:
        # Conflicting strong markers — genuinely ambiguous.
        return None

    # No strong size marker — fall back to a generic family marker. A bare
    # "gpt-5" (no nano/mini suffix) is treated as BALANCED.
    if "gpt" in tokens and any(t.startswith("5") for t in tokens):
        return ModelTier.BALANCED
    return None
