"""
Model Discovery Service

Polls each AI provider's native model-listing endpoint using a user's AI
credential and caches the result on the ``AICredential`` row. Different API
keys can see different models, so the available-model list is cached
per-credential rather than per-provider.

Design notes:
- Decryption reuses the existing ``ai_credentials_service.decrypt_credential``
  helper (Fernet) — this module never touches Fernet directly.
- All blocking network I/O runs via ``anyio.to_thread.run_sync`` so it does not
  block the event loop (sync DB I/O on the loop is fine; blocking network is
  offloaded — per the event-handler concurrency convention).
- ``refresh_all_credentials`` is failure-isolated: a per-credential try/except
  ensures one bad key never aborts the batch (mirrors the notification
  dispatcher).
- Secrets are never logged. We log provider + credential id + model count only.
  ``models_discovery_error`` stores a coarse reason code, not a raw error body.
"""
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import anyio
import httpx
from fastapi import HTTPException
from sqlmodel import Session, select

from app.core.config import settings
from app.models.credentials.ai_credential import (
    AICredential,
    AICredentialTestRequest,
    AICredentialTestResult,
    AICredentialType,
)
from app.services.credentials.ai_credentials_service import ai_credentials_service

logger = logging.getLogger(__name__)

# Sentinel reason code recorded when an Anthropic OAuth token is encountered.
OAUTH_TOKEN_UNSUPPORTED = "oauth_token_unsupported"

# Network timeout for native /models calls (cheap GETs).
_HTTP_TIMEOUT_SECONDS = 20.0

# Default OpenAI-compatible model endpoint suffix.
_OPENAI_COMPATIBLE_MODELS_PATH = "/models"

# Coarse reason codes recorded as models_discovery_error / surfaced to the UI.
# A "skip" means the credential is valid/usable but model listing isn't
# applicable; "invalid_key" is a real auth rejection.
SKIP_REASONS = frozenset(
    {OAUTH_TOKEN_UNSUPPORTED, "no_list_endpoint", "no_base_url", "unsupported_type"}
)
ERROR_INVALID_KEY = "invalid_key"


@dataclass
class ProbeResult:
    """Outcome of probing a provider's model list with a raw (decrypted) key.

    DB-free — usable both by the cron (which then persists onto the credential
    row) and by the synchronous "Test Connection" endpoint (which may not have
    a row yet).

    - ``ok``        — the probe completed without a hard auth failure. True for
                      both a successful listing AND a benign skip (OAuth /
                      minimax / openai_compatible-without-base-url). False only
                      on a real auth rejection (``invalid_key``).
    - ``models``    — discovered model ids (empty on skip).
    - ``reason``    — a coarse skip/error code when applicable (one of
                      ``SKIP_REASONS`` or ``invalid_key``), else ``None``.
                      ``None`` reason with ``ok`` and a non-empty list means a
                      clean successful listing.
    """

    ok: bool
    models: list[str]
    reason: str | None = None

    @property
    def is_skip(self) -> bool:
        return self.ok and self.reason in SKIP_REASONS


# ---------------------------------------------------------------------------
# Per-provider blocking listers (run inside anyio worker threads)
# ---------------------------------------------------------------------------

def _list_anthropic_models(api_key: str) -> list[str]:
    """List Anthropic models via GET /v1/models (httpx — the anthropic SDK is
    not a project dependency)."""
    resp = httpx.get(
        "https://api.anthropic.com/v1/models",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        timeout=_HTTP_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    payload = resp.json()
    return [m["id"] for m in payload.get("data", []) if m.get("id")]


def _list_openai_models(api_key: str) -> list[str]:
    """List OpenAI models via GET /v1/models (httpx — avoids depending on the
    openai SDK, which is only present transitively today; consistent with the
    Anthropic / openai_compatible listers)."""
    resp = httpx.get(
        "https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=_HTTP_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    payload = resp.json()
    return [m["id"] for m in payload.get("data", []) if m.get("id")]


def _list_google_models(api_key: str) -> list[str]:
    """List Google models via the google-genai client (ListModels)."""
    from google import genai

    client = genai.Client(api_key=api_key)
    ids: list[str] = []
    for m in client.models.list():
        name = getattr(m, "name", None)
        if name:
            # Names come back as "models/gemini-2.5-pro"; strip the prefix.
            ids.append(name.split("/", 1)[1] if "/" in name else name)
    return ids


def _list_openai_compatible_models(api_key: str, base_url: str) -> list[str]:
    """List models for an OpenAI-compatible endpoint via GET {base_url}/models.

    Assumes the OpenAI response shape ({"data": [{"id": ...}]}). Endpoints that
    differ or are unreachable raise, and the caller records an error + skips."""
    url = base_url.rstrip("/") + _OPENAI_COMPATIBLE_MODELS_PATH
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    resp = httpx.get(url, headers=headers, timeout=_HTTP_TIMEOUT_SECONDS)
    resp.raise_for_status()
    payload = resp.json()
    data = payload.get("data", payload if isinstance(payload, list) else [])
    return [m["id"] for m in data if isinstance(m, dict) and m.get("id")]


# ---------------------------------------------------------------------------
# DB-free provider probe (single code path for cron + Test Connection)
# ---------------------------------------------------------------------------

async def probe_models(
    cred_type: AICredentialType | str,
    api_key: str,
    base_url: str | None = None,
) -> ProbeResult:
    """Probe a provider's native model list with a raw (already-decrypted) key.

    This is the single dispatch path used by BOTH the discovery cron (which
    persists the result onto the credential row) and the synchronous "Test
    Connection" endpoint (which may have no row yet). Pure I/O — no DB access.

    Dispatch by ``cred_type``:
      - anthropic → GET /v1/models (OAuth ``sk-ant-oat*`` → skip
        ``oauth_token_unsupported``).
      - openai → GET /v1/models.
      - google → genai ListModels.
      - openai_compatible → GET ``{base_url}/models`` (no base_url → skip
        ``no_base_url``).
      - minimax → skip ``no_list_endpoint`` (catalog-only).
    Blocking HTTP runs via ``anyio.to_thread``. A 401/403 anywhere maps to a
    non-ok ``invalid_key`` result; other HTTP/transport errors propagate.

    Returns a :class:`ProbeResult`. Never logs the key.
    """
    api_key = api_key or ""
    try:
        if cred_type == AICredentialType.ANTHROPIC:
            if api_key.startswith("sk-ant-oat"):
                return ProbeResult(ok=True, models=[], reason=OAUTH_TOKEN_UNSUPPORTED)
            models = await anyio.to_thread.run_sync(_list_anthropic_models, api_key)

        elif cred_type == AICredentialType.OPENAI:
            models = await anyio.to_thread.run_sync(_list_openai_models, api_key)

        elif cred_type == AICredentialType.GOOGLE:
            models = await anyio.to_thread.run_sync(_list_google_models, api_key)

        elif cred_type == AICredentialType.OPENAI_COMPATIBLE:
            if not base_url:
                return ProbeResult(ok=True, models=[], reason="no_base_url")
            models = await anyio.to_thread.run_sync(
                _list_openai_compatible_models, api_key, base_url
            )

        elif cred_type == AICredentialType.MINIMAX:
            return ProbeResult(ok=True, models=[], reason="no_list_endpoint")

        else:
            return ProbeResult(ok=True, models=[], reason="unsupported_type")

    except httpx.HTTPStatusError as http_err:
        if http_err.response.status_code in (401, 403):
            return ProbeResult(ok=False, models=[], reason=ERROR_INVALID_KEY)
        raise

    # Dedupe while preserving order.
    seen: set[str] = set()
    unique_models = [m for m in models if not (m in seen or seen.add(m))]
    return ProbeResult(ok=True, models=unique_models, reason=None)


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
