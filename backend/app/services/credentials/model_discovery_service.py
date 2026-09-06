"""
Model Discovery Service

Polls each AI provider's native model-listing endpoint using a user's AI
credential and caches the result on the ``AICredential`` row. Different API
keys can see different models, so the available-model list is cached
per-credential rather than per-provider.

Design notes:
- Decryption reuses the existing ``ai_credentials_service.decrypt_credential``
  helper (Fernet) — this module never touches Fernet directly.
- **Provider I/O lives in ``app/services/ai_providers/``, not here.** This module
  owns the DB half — which rows to probe and what to persist onto them — and
  delegates every per-provider decision to ``registry.get_adapter(...)``.
  ``probe_models``, ``ProbeResult`` and the reason codes are re-exported so the
  existing import paths keep working; the delegation is genuine, so replacing an
  adapter through ``ai_providers.registry.override_for_tests`` intercepts every
  provider call this module can make.
- All blocking network I/O runs via ``anyio.to_thread.run_sync`` (inside the
  adapters) so it does not block the event loop.
- ``refresh_all_credentials`` is failure-isolated: a per-credential try/except
  ensures one bad key never aborts the batch (mirrors the notification
  dispatcher).
- Secrets are never logged. We log provider + credential id + model count only.
  ``models_discovery_error`` stores a coarse reason code, not a raw error body.
"""
import logging
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlmodel import Session, select

from app.core.config import settings
from app.models.credentials.ai_credential import (
    AICredential,
    AICredentialTestRequest,
    AICredentialTestResult,
    AICredentialType,
)
from app.services.ai_providers import registry
from app.services.ai_providers.base import (
    ERROR_INVALID_KEY,
    OAUTH_TOKEN_UNSUPPORTED,
    SKIP_REASONS,
    UNSUPPORTED_TYPE,
    ProbeResult,
)
from app.services.credentials.ai_credentials_service import ai_credentials_service

logger = logging.getLogger(__name__)

# Re-exported for the modules and tests that have always imported these from
# here. They are DECLARED in ``ai_providers.base`` because the adapters need
# them and this module imports ``ai_credentials_service`` — an adapter reaching
# back here for ``ProbeResult`` would close an import cycle.
__all__ = [
    "ERROR_INVALID_KEY",
    "OAUTH_TOKEN_UNSUPPORTED",
    "SKIP_REASONS",
    "ProbeResult",
    "discover_models_for_credential",
    "dispatch_model_deprecation_notifications",
    "probe_models",
    "refresh_all_credentials",
    "test_connection",
]


# ---------------------------------------------------------------------------
# DB-free provider probe (single code path for cron + Test Connection)
# ---------------------------------------------------------------------------

async def probe_models(
    cred_type: AICredentialType | str,
    api_key: str,
    base_url: str | None = None,
) -> ProbeResult:
    """Probe a provider's native model list with a raw (already-decrypted) key.

    The single dispatch path used by BOTH the discovery cron (which persists the
    result onto the credential row) and the synchronous "Test Connection"
    endpoint (which may have no row yet). Pure I/O — no DB access.

    Dispatch is one registry lookup; what each provider does lives in its
    adapter (``app/services/ai_providers/<provider>.py``). A ``cred_type`` no
    adapter serves is a benign skip, not an error, because the column can hold
    a value the enum no longer has.

    Returns a :class:`ProbeResult`. Never logs the key.
    """
    adapter = registry.find_adapter(cred_type)
    if adapter is None:
        return ProbeResult(ok=True, models=[], reason=UNSUPPORTED_TYPE)
    return await adapter.list_models(api_key or "", base_url)


# ---------------------------------------------------------------------------
# Public service API
# ---------------------------------------------------------------------------

async def discover_models_for_credential(
    session: Session, credential: AICredential
) -> list[str]:
    """Discover the models a single credential's key can access.

    Decrypts the credential then delegates to :func:`probe_models` (the one
    shared dispatch path) and maps the result onto the credential row.

    On a successful listing it caches ``discovered_models`` /
    ``models_discovered_at`` and clears any error. On a benign skip (OAuth /
    minimax / openai_compatible-without-base-url) or an ``invalid_key``
    rejection it records the coarse reason in ``models_discovery_error`` and
    leaves ``discovered_models`` unchanged. Returns the effective model list.

    The caller is responsible for committing the session.
    """
    cred_type = credential.type
    # ``type`` may be a plain str or the AICredentialType enum depending on how
    # the row was loaded; AICredentialType is a str-Enum so == comparisons work
    # either way. Use a plain string form for logging.
    cred_type_label = getattr(cred_type, "value", cred_type)
    data = ai_credentials_service.decrypt_credential(credential)

    result = await probe_models(cred_type, data.api_key or "", data.base_url)

    if result.reason is not None:
        # Skip or invalid_key — record the coarse reason, keep the prior list.
        credential.models_discovery_error = result.reason
        logger.info(
            "Model discovery %s for credential %s (%s): %s",
            "rejected" if result.reason == ERROR_INVALID_KEY else "skipped",
            credential.id, cred_type_label, result.reason,
        )
        session.add(credential)
        return credential.discovered_models or []

    # Success — cache the result and clear any prior error.
    credential.discovered_models = result.models
    credential.models_discovered_at = datetime.now(UTC)
    credential.models_discovery_error = None
    session.add(credential)

    logger.info(
        "Model discovery succeeded for credential %s (%s): %d models",
        credential.id, cred_type_label, len(result.models),
    )
    return result.models


async def test_connection(
    session: Session,
    user_id: uuid.UUID,
    request: AICredentialTestRequest,
) -> AICredentialTestResult:
    """Validate an AI credential and (for the Edit case) force-refresh its
    cached model list.

    Key resolution:
      - Use ``request.api_key`` if provided (the Add form, or an Edit where the
        user re-typed the key).
      - Else, if ``request.credential_id`` is given, decrypt the stored
        credential's key (owner-scoped: ``get_credential`` raises 404 if the
        row is missing / 403 if it isn't the caller's — matching the existing
        AI-credential ownership-guard convention).
      - Else raise HTTP 422 ("no key to test").

    Base URL prefers the request value, else the stored credential's.

    Persistence: when ``credential_id`` is present (Edit), the fresh probe
    result is written onto that row (``discovered_models`` /
    ``models_discovered_at`` / ``models_discovery_error``) — this is the
    "force-refresh model list" behaviour. For the Add case (no row yet) nothing
    is persisted; the probe result is just returned.

    Never logs the key.
    """
    stored: AICredential | None = None
    if request.credential_id is not None:
        # Owner-scoped fetch (404 missing / 403 not owner).
        stored = ai_credentials_service.get_credential(
            session, request.credential_id, user_id
        )

    # Resolve effective key: form value first, else stored credential's key.
    api_key = (request.api_key or "").strip()
    if not api_key and stored is not None:
        api_key = ai_credentials_service.decrypt_credential(stored).api_key or ""
    if not api_key:
        raise HTTPException(status_code=422, detail="No key to test")

    # Resolve effective base_url: request value first, else stored value.
    base_url = request.base_url
    if not base_url and stored is not None:
        base_url = ai_credentials_service.decrypt_credential(stored).base_url

    result = await probe_models(request.type, api_key, base_url)

    # Force-refresh persistence for the Edit case (stored row exists).
    if stored is not None:
        if result.reason is None:
            stored.discovered_models = result.models
            stored.models_discovered_at = datetime.now(UTC)
            stored.models_discovery_error = None
        else:
            # Skip or invalid_key — record the reason, keep the prior list.
            stored.models_discovery_error = result.reason
        session.add(stored)
        session.commit()

    logger.info(
        "AI credential test_connection (%s) for user %s: ok=%s reason=%s count=%d",
        getattr(request.type, "value", request.type),
        user_id, result.ok, result.reason, len(result.models),
    )

    # Split the reason across error/skip_reason for an unambiguous contract:
    # error only on failure, skip_reason only on a benign skip.
    return AICredentialTestResult(
        success=result.ok,
        models=result.models,
        model_count=len(result.models),
        error=None if result.ok else result.reason,
        skip_reason=result.reason if result.ok else None,
    )


async def refresh_all_credentials(session: Session) -> int:
    """Run discovery for every AI credential, failure-isolated per credential.

    One bad key never aborts the batch. Returns the count of credentials for
    which discovery succeeded (i.e. produced a fresh list). Idempotent and safe
    to re-run.
    """
    credentials = session.exec(select(AICredential)).all()
    if not credentials:
        logger.debug("No AI credentials to run model discovery for")
        return 0

    logger.info("Running model discovery for %d AI credentials", len(credentials))

    success_count = 0
    for credential in credentials:
        try:
            await discover_models_for_credential(session, credential)
            # A fresh successful discovery clears the error and stamps the time.
            if credential.models_discovery_error is None:
                success_count += 1
            session.commit()
        except Exception as e:
            session.rollback()
            # Record a coarse failure reason (the exception class name), never
            # the raw API body. Re-fetch and persist on a clean session state.
            try:
                refreshed = session.get(AICredential, credential.id)
                if refreshed is not None:
                    refreshed.models_discovery_error = type(e).__name__
                    session.add(refreshed)
                    session.commit()
            except Exception:
                session.rollback()
            logger.error(
                "Model discovery failed for credential %s (%s): %s",
                credential.id,
                getattr(credential.type, "value", credential.type),
                type(e).__name__,
                exc_info=True,
            )
            continue

    logger.info(
        "Model discovery batch complete: %d/%d credentials refreshed",
        success_count, len(credentials),
    )
    return success_count


# ---------------------------------------------------------------------------
# Model-deprecated notification dispatch (transition-only)
# ---------------------------------------------------------------------------

# Process-local set of environment ids currently in a warning state. Lets us
# fire the model_deprecated notification ONLY on transition into a warning
# (not on every daily cron run for a persistently-broken env). Resets on
# restart — at worst one extra email shortly after a deploy, consistent with
# the notification throttle's documented best-effort semantics.
_warned_env_ids: set[str] = set()


async def dispatch_model_deprecation_notifications(session: Session) -> int:
    """Evaluate model health for every environment and email the owner of any
    environment that has NEWLY transitioned into a warning state.

    Called by the discovery cron AFTER a refresh batch, so the per-credential
    discovered-model cache is fresh. Failure-isolated per environment (like the
    notification dispatcher). Returns the number of notifications dispatched.

    Transition detection is in-memory (``_warned_env_ids``): an env only fires
    when it flips from non-warned to warned. The notification service's
    ``dedup_scope="environment_id"`` throttle is a second line of defense.
    """
    # Imports are local to avoid import cycles (environment_service imports the
    # model_health_service which would otherwise pull this module at import).
    from app.models.agents.agent import Agent
    from app.models.environments.environment import AgentEnvironment
    from app.services.environments.model_health_service import evaluate_environment
    from app.services.notifications.notification_catalog import NotificationType
    from app.services.notifications.notification_service import (
        SystemNotificationService,
    )

    environments = session.exec(select(AgentEnvironment)).all()
    dispatched = 0

    for env in environments:
        env_key = str(env.id)
        try:
            health = evaluate_environment(session, env)
            if not health.has_warning:
                _warned_env_ids.discard(env_key)
                continue

            # Already warned in a prior run → no re-fire (transition-only).
            if env_key in _warned_env_ids:
                continue
            _warned_env_ids.add(env_key)

            agent = session.get(Agent, env.agent_id)
            if agent is None:
                continue

            # Build a plain-language detail line from the flagged modes.
            flagged = [
                m for m in health.modes
                if m.status in ("retired_override", "unknown_model")
            ]
            detail = "; ".join(
                f"{m.mode.capitalize()} is using '{m.model}'. {m.cta or ''}".strip()
                for m in flagged
            ) or "An AI model in this environment needs updating."

            await SystemNotificationService.notify(
                session,
                user_id=agent.owner_id,
                notification_type=NotificationType.MODEL_DEPRECATED,
                context={
                    "project_name": settings.PROJECT_NAME,
                    "agent_name": agent.name or "your agent",
                    "instance_name": env.instance_name or "your environment",
                    "environment_id": env_key,
                    "detail": detail,
                    "link": f"{settings.FRONTEND_HOST}/agents/{agent.id}",
                },
            )
            dispatched += 1
        except Exception as e:
            logger.error(
                "Failed to dispatch model_deprecated notification for env %s: %s",
                env.id, type(e).__name__, exc_info=True,
            )
            continue

    if dispatched:
        logger.info("Dispatched %d model_deprecated notifications", dispatched)
    return dispatched
