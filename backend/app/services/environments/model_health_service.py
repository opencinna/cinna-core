"""
Model Health Service

Computes a user-facing model-health signal for an agent environment: is each
mode's configured model deprecated, retired, or unavailable to its credential?

This is a *config* problem (fixed by reconfigure/restart or editing the model
override), distinct from the admin-only image-tag ``is_stale`` flag (fixed by a
Docker rebuild).

The evaluation is cheap and read-safe: it reads the central catalog plus the
linked AI credential's already-cached ``discovered_models``. It makes NO live
provider API calls in the request path (discovery is the cron's job, Phase 3).

Classification per mode (priority order):
  1. claude-code tier words (haiku/sonnet/opus) → always ``ok`` (CLI resolves).
  2. If the credential has ``discovered_models`` (most authoritative, per-key):
     the effective model not in the list → flagged.
  3. Else if the model matches ``RETIRED_MODELS`` → flagged.
  4. Else → ``ok`` (or ``unverified`` when there is no discovery data and the
     model is otherwise unknown).

Per-mode status values:
  - ``ok``               — healthy.
  - ``retired_override`` — a frozen ``model_override_*`` points at a
                           retired/unavailable model (cause: ``frozen_override``).
  - ``unknown_model``    — a resolved DEFAULT (no override) is not in the
                           credential's discovered list (cause: ``stale_default``).
  - ``unverified``       — no discovery data / discovery failed; can't confirm,
                           so we don't raise a false alarm.
"""
import logging
import re
import uuid

from sqlmodel import Session

from app.models.agents.agent import Agent
from app.models.credentials.ai_credential import AICredential, AICredentialType
from app.models.environments.environment import (
    AgentEnvironment,
    ModelHealthMode,
    ModelHealthPublic,
)
from app.services.credentials.ai_credentials_service import ai_credentials_service
from app.services.environments.model_catalog import (
    is_known_word,
    is_retired,
    resolve_model,
)
from app.services.environments.sdk_constants import sdk_expected_credential_type

logger = logging.getLogger(__name__)

# Per-mode status codes.
STATUS_OK = "ok"
STATUS_RETIRED_OVERRIDE = "retired_override"
STATUS_UNKNOWN_MODEL = "unknown_model"
STATUS_UNVERIFIED = "unverified"

# Cause codes (drive the CTA copy).
CAUSE_STALE_DEFAULT = "stale_default"
CAUSE_FROZEN_OVERRIDE = "frozen_override"

# Plain-language CTA copy per cause.
_CTA_STALE_DEFAULT = "Restart to use the current model."
_CTA_FROZEN_OVERRIDE = "Edit or clear the model override, then restart."

_MODES = ("conversation", "building")


def _split_engine_provider(sdk_value: str | None) -> tuple[str, str]:
    """Split a full SDK id like ``opencode/anthropic`` into (engine, provider).

    Defaults to claude-code/anthropic for missing/engine-only values, matching
    the lifecycle generation default.
    """
    if not sdk_value:
        return ("claude-code", "anthropic")
    engine, _, provider = sdk_value.partition("/")
    return (engine or "claude-code", provider or "anthropic")


# Trailing dated-snapshot suffix, e.g. "-20250929" on
# "claude-sonnet-4-5-20250929". Native provider APIs return dated snapshots
# while the catalog stores undated family ids (e.g. "claude-sonnet-4-6").
_SNAPSHOT_SUFFIX_RE = re.compile(r"-\d{8}$")


def _normalize_model_id(model: str) -> str:
    """Reduce a model id to a family stem for lenient cross-source matching.

    - Strips any ``provider/`` prefix (catalog ids are provider-qualified; the
      native lists are bare).
    - Strips a trailing ``-YYYYMMDD`` snapshot date so a dated discovered
      snapshot (``claude-sonnet-4-5-20250929``) matches an undated catalog id
      (``claude-sonnet-4-5``) and vice-versa.
    - Lowercases for case-insensitive comparison.

    Example: ``anthropic/claude-sonnet-4-5-20250929`` → ``claude-sonnet-4-5``.
    """
    bare = model.split("/", 1)[1] if "/" in model else model
    bare = _SNAPSHOT_SUFFIX_RE.sub("", bare.strip())
    return bare.lower()


def _model_in_discovered(model: str, discovered: list[str]) -> bool:
    """Family/stem-aware membership check.

    Normalizes both the effective model and every discovered id (drop
    ``provider/`` prefix + trailing ``-YYYYMMDD`` snapshot, lowercase) before
    comparing, so an undated catalog default matches the dated snapshots the
    native provider APIs actually return. Without this, a current healthy
    default would falsely fail the membership check once real discovery data
    exists and get flagged ``unknown_model``/``stale_default``.
    """
    target = _normalize_model_id(model)
    return any(_normalize_model_id(d) == target for d in discovered)


def _linked_credential_for_mode(
    session: Session,
    environment: AgentEnvironment,
    user_id: uuid.UUID,
    sdk_value: str | None,
    mode: str,
) -> AICredential | None:
    """Resolve the AI credential that backs a mode, cheaply.

    Prefers the explicitly linked credential id; falls back to the user's
    type-default for the mode's SDK provider (mirrors the env credential
    resolution fallback, without re-running the full bag logic). Returns None
    when nothing is resolvable (e.g. legacy profile key only).
    """
    cred_id = (
        environment.building_ai_credential_id if mode == "building"
        else environment.conversation_ai_credential_id
    )
    if cred_id is not None:
        cred = session.get(AICredential, cred_id)
        if cred is not None:
            return cred

    # Fall back to the user's type-default for this SDK's required type.
    expected_type = sdk_expected_credential_type(sdk_value)
    if expected_type is None:
        return None
    return ai_credentials_service.get_default_for_type(session, user_id, expected_type)


def _evaluate_mode(
    session: Session,
    environment: AgentEnvironment,
    user_id: uuid.UUID,
    mode: str,
) -> ModelHealthMode:
    """Evaluate model health for a single mode."""
    sdk_value = (
        environment.agent_sdk_building if mode == "building"
        else environment.agent_sdk_conversation
    )
    override = (
        environment.model_override_building if mode == "building"
        else environment.model_override_conversation
    )
    engine, provider = _split_engine_provider(sdk_value)

    effective_model = resolve_model(
        engine=engine,
        provider=provider,
        mode=mode,
        override=override,
        openai_compatible_model=None,
    )

    # 1. claude-code tier words are always healthy — the CLI resolves them.
    if is_known_word(effective_model):
        return ModelHealthMode(mode=mode, model=effective_model, status=STATUS_OK)

    # openai_compatible models live behind a user-provided endpoint we never
    # flag (the user owns the model namespace there).
    if provider == "openai_compatible":
        return ModelHealthMode(mode=mode, model=effective_model, status=STATUS_OK)

    has_override = bool(override)

    # The catalog tier default for this mode (used as the suggested replacement).
    suggested = resolve_model(
        engine=engine, provider=provider, mode=mode, override=None
    )
    # If the tier default itself is a tier word (claude-code), there's no
    # concrete id to suggest.
    if is_known_word(suggested):
        suggested = None

    # 2. Per-credential discovered models (most authoritative, per key).
    credential = _linked_credential_for_mode(
        session, environment, user_id, sdk_value, mode
    )
    discovered = credential.discovered_models if credential else None

    if discovered:
        if _model_in_discovered(effective_model, discovered):
            return ModelHealthMode(mode=mode, model=effective_model, status=STATUS_OK)
        # Not available to this key.
        if has_override:
            return ModelHealthMode(
                mode=mode,
                model=effective_model,
                status=STATUS_RETIRED_OVERRIDE,
                cause=CAUSE_FROZEN_OVERRIDE,
                suggested_model=suggested,
                cta=_CTA_FROZEN_OVERRIDE,
            )
        return ModelHealthMode(
            mode=mode,
            model=effective_model,
            status=STATUS_UNKNOWN_MODEL,
            cause=CAUSE_STALE_DEFAULT,
            suggested_model=suggested,
            cta=_CTA_STALE_DEFAULT,
        )

    # 3. No discovery data — fall back to the curated retired set.
    if is_retired(effective_model):
        if has_override:
            return ModelHealthMode(
                mode=mode,
                model=effective_model,
                status=STATUS_RETIRED_OVERRIDE,
                cause=CAUSE_FROZEN_OVERRIDE,
                suggested_model=suggested,
                cta=_CTA_FROZEN_OVERRIDE,
            )
        return ModelHealthMode(
            mode=mode,
            model=effective_model,
            status=STATUS_UNKNOWN_MODEL,
            cause=CAUSE_STALE_DEFAULT,
            suggested_model=suggested,
            cta=_CTA_STALE_DEFAULT,
        )

    # 4. No discovery data and not in the retired set. If the credential exists
    # but discovery failed/never ran, we cannot confirm availability → quiet
    # "unverified" (no alarm). Otherwise treat as ok.
    if credential is not None and (
        credential.discovered_models is None
    ) and credential.type != AICredentialType.MINIMAX:
        return ModelHealthMode(
            mode=mode, model=effective_model, status=STATUS_UNVERIFIED
        )

    return ModelHealthMode(mode=mode, model=effective_model, status=STATUS_OK)


def evaluate_environment(
    session: Session,
    environment: AgentEnvironment,
    agent: Agent | None = None,
) -> ModelHealthPublic:
    """Compute the per-mode model-health roll-up for an environment.

    Cheap and read-safe (catalog + cached discovered_models only). Owner-scoped
    by construction — it only reads credentials owned by the environment's
    agent owner. Never raises; a failure degrades to a healthy roll-up.

    Per-row DB cost is intentionally small but non-zero: one Agent PK lookup
    (skippable by passing a pre-loaded ``agent`` — the admin list path already
    has it) plus, per mode, at most one credential PK lookup OR one
    type-default SELECT (only when no explicit credential id is linked). No
    live provider API calls. Callers that iterate the whole fleet should pass
    ``agent`` to avoid the re-fetch.
    """
    try:
        if agent is None:
            agent = session.get(Agent, environment.agent_id)
        owner_id = agent.owner_id if agent else None
        if owner_id is None:
            return ModelHealthPublic(has_warning=False, modes=[])

        modes = [
            _evaluate_mode(session, environment, owner_id, mode)
            for mode in _MODES
        ]
        has_warning = any(
            m.status in (STATUS_RETIRED_OVERRIDE, STATUS_UNKNOWN_MODEL)
            for m in modes
        )
        return ModelHealthPublic(has_warning=has_warning, modes=modes)
    except Exception as e:  # noqa: BLE001 — health must never break a response
        logger.warning(
            "Model health evaluation failed for environment %s: %s",
            environment.id, e, exc_info=True,
        )
        return ModelHealthPublic(has_warning=False, modes=[])
