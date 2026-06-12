"""
Agent REST API Service — business logic for the producer side of the
``agent_api`` (cinna_api) feature.

A producer agent writes plain decorated Python functions under
``/app/workspace/agent_api/`` and the platform supervises a real FastAPI app
inside its container. This service resolves the producer agent + its running
environment, serves the harvested OpenAPI spec (from cache or a short-lived
import-only harvest — *never* by spawning the serving child), keeps the env
alive for API traffic, auto-activates a suspended producer env, and loads the
declarative ``policy.yaml`` guardrails (fail-closed on parse error).

Phase 1 is producer / owner-only: there are no tokens, no consumer routes, and
no proxy-edge policy enforcement yet (that is Phase 2). The policy is loaded and
cached here because the spec/status surfaces it, but ``enforce_policy`` is not
implemented in this phase.

The environment-resolution rule is inherited verbatim from
``WebappService.resolve_agent_environment()`` so the resolved env is stable
across blue-green swap / rebuild, exactly like the webapp.
"""
import uuid
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import yaml
from sqlmodel import Session

from app.core.db import create_session
from app.models import Agent, AgentEnvironment

if TYPE_CHECKING:
    from app.models import AgentApiToken
from app.services.environments.environment_service import EnvironmentService
from app.services.webapp.webapp_service import (
    WebappService,
    WebappError,
    WebappNotFoundError,
    WebappPermissionError,
    WebappNotAvailableError,
)

logger = logging.getLogger(__name__)

# Request-loop protection (plan §4.5). An agent_api call can hit a producer
# whose handler calls another agent_api (A→B→A). Timeouts compose additively and
# each hop pins a proxy connection open, so we propagate a shrinking deadline and
# cap nesting depth. These are passed as request headers between hops.
DEADLINE_HEADER = "x-cinna-agent-api-deadline-ms"   # remaining budget, ms
HOP_DEPTH_HEADER = "x-cinna-agent-api-hop-depth"     # nested-call counter
MAX_HOP_DEPTH = 4                                     # reject beyond N nested calls
DEFAULT_DEADLINE_MS = 60_000                          # 60 s top-level budget
HOP_DEADLINE_SHRINK_MS = 1_000                        # subtract per hop

# Cold-start grace window. When a consumer hits a suspended/stopped producer
# env, we kick off activation and block the request for up to this long waiting
# for the container to come up, then forward the call — so the first call after
# an idle period just takes a little longer instead of failing. Container starts
# are usually fast; if activation fails or exceeds the budget we return a proper
# 503 (the consumer retries, by which point the env is typically already up).
ACTIVATION_WAIT_SECONDS = 10.0
ACTIVATION_POLL_INTERVAL = 0.5                        # status re-check cadence

# Env statuses that a consumer request can wake on its own — a suspended or
# stopped producer env is auto-activated; any other non-running status
# (creating / error / unknown) is not something we kick off here.
WAKEABLE_ENV_STATUSES = ("suspended", "stopped")

# In-process sliding-window rate-limit state: token_id -> list[monotonic ts].
# A backstop, not the primary defence (the deadline + hop-depth limit are). This
# is per-process; a multi-worker deployment gets per-worker windows, which is an
# acceptable approximation for the backstop role.
_rate_limit_hits: dict[uuid.UUID, list[float]] = {}

# Path of the declarative guardrails file inside the producer workspace.
POLICY_FILE_PATH = "agent_api/policy.yaml"
# Hard cap on the policy file we will read into memory.
MAX_POLICY_BYTES = 64 * 1024

# Policy defaults (see plan §4.2). These are also the fail-closed values used
# when the file is missing or cannot be parsed.
DEFAULT_POLICY: dict = {
    "read_only": True,
    "auth": "required",
    "max_body_bytes": 10 * 1024 * 1024,  # 10 MB
    "rate_limit": "60/min",
    "expose_spec": True,
    "allowed_paths": ["*"],
}
# Policy used when parsing fails — deny everything (fail closed). A parse error
# means we cannot trust the producer's stated guardrails, so we lock the API
# down rather than open it up.
FAIL_CLOSED_POLICY: dict = {
    "read_only": True,
    "allowed_methods": [],  # deny every verb
    "auth": "required",
    "max_body_bytes": 0,
    "rate_limit": "0/min",
    "expose_spec": False,
    "allowed_paths": [],
    "error": "policy.yaml could not be parsed — failing closed (deny-all)",
}


class AgentApiError(Exception):
    """Base exception for agent REST API service errors."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class AgentApiNotFoundError(AgentApiError):
    """Agent (or its environment) does not exist / is not accessible."""

    def __init__(self, message: str = "Agent not found"):
        super().__init__(message, status_code=404)


class AgentApiDisabledError(AgentApiError):
    """The agent REST API feature is disabled for this agent."""

    def __init__(self, message: str = "Agent REST API is disabled for this agent"):
        super().__init__(message, status_code=400)


class AgentApiNotRunningError(AgentApiError):
    """The producer environment is not running (booting / suspended)."""

    def __init__(self, message: str = "Producer environment is not running"):
        super().__init__(message, status_code=503)


class AgentApiAuthError(AgentApiError):
    """Token missing / invalid / revoked / expired."""

    def __init__(self, message: str = "Invalid or expired token"):
        super().__init__(message, status_code=401)


class AgentApiPolicyError(AgentApiError):
    """A request violates the producer's policy.yaml at the proxy edge.

    Carries the specific status (405 / 413 / 429 / 403) and an optional
    ``retry_after`` for rate-limit responses.
    """

    def __init__(self, message: str, status_code: int, retry_after: int | None = None):
        self.retry_after = retry_after
        super().__init__(message, status_code=status_code)


class AgentApiService:
    """Service for the producer side of the agent REST API."""

    # ------------------------------------------------------------------ #
    # Environment resolution                                              #
    # ------------------------------------------------------------------ #

    @staticmethod
    def resolve_producer_environment(
        session: Session,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        is_superuser: bool = False,
        require_agent_api_enabled: bool = True,
    ) -> tuple[Agent, AgentEnvironment]:
        """
        Resolve the producer agent + its active running environment.

        Delegates the env-selection rule verbatim to
        ``WebappService.resolve_agent_environment()`` (do not invent a new one —
        this guarantees the resolved env is the same one the webapp would pick
        and is stable across blue-green swap / rebuild), then re-checks the
        ``agent_api_enabled`` toggle and re-maps the webapp domain exceptions to
        the agent-api exception hierarchy.

        Raises:
            AgentApiNotFoundError: Agent not found or not owned by the user.
            AgentApiDisabledError: ``agent_api_enabled`` is false.
            AgentApiNotRunningError: No active env, or env not running.
        """
        try:
            # We resolve with webapp-enabled check disabled — the agent_api
            # toggle is independent of webapp_enabled — and apply the agent_api
            # gate ourselves below.
            agent, environment = WebappService.resolve_agent_environment(
                session,
                agent_id,
                user_id,
                is_superuser=is_superuser,
                require_webapp_enabled=False,
            )
        except (WebappNotFoundError, WebappPermissionError) as e:
            # 404 for both not-found and permission-denied to avoid leaking
            # existence (matches the webapp/A2A convention).
            raise AgentApiNotFoundError("Agent not found") from e
        except WebappNotAvailableError as e:
            # "No active environment" surfaces here from the webapp service.
            raise AgentApiNotRunningError(str(e)) from e
        except WebappError as e:
            # Env not running / not found.
            raise AgentApiNotRunningError(e.message) from e

        if require_agent_api_enabled and not agent.agent_api_enabled:
            raise AgentApiDisabledError("Agent REST API is disabled for this agent")

        return agent, environment

    @staticmethod
    def resolve_agent_only(
        session: Session,
        agent_id: uuid.UUID,
        user_id: uuid.UUID,
        is_superuser: bool = False,
    ) -> Agent:
        """
        Resolve + ownership-check the agent WITHOUT requiring a running env.

        Used by ``_status``, which must report ``disabled`` / ``not_running``
        without raising on a suspended or absent environment.
        """
        agent = session.get(Agent, agent_id)
        if not agent:
            raise AgentApiNotFoundError("Agent not found")
        if agent.owner_id != user_id and not is_superuser:
            # 404, not 403, to avoid leaking existence.
            raise AgentApiNotFoundError("Agent not found")
        return agent

    # ------------------------------------------------------------------ #
    # Keep-alive / activation                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    def update_last_activity(session: Session, environment: AgentEnvironment) -> None:
        """Bump ``last_activity_at`` so the suspension scheduler respects API traffic."""
        environment.last_activity_at = datetime.now(UTC)
        session.add(environment)
        session.commit()

    @staticmethod
    async def auto_activate_if_wakeable(
        session: Session,
        agent: Agent,
        environment: AgentEnvironment | None,
    ) -> dict:
        """
        Return a readiness status dict for the producer env, auto-activating it
        when suspended or stopped. Reuses ``EnvironmentService.activate_environment``
        (which internally starts the container, handling both the suspended and
        stopped cases) so consumers get the same ``activating`` → ``running``
        semantics as a chat message.

        Returns a dict with ``status`` (running | activating | error) and a
        human-readable ``message``.
        """
        if not environment:
            return {"status": "error", "message": "No active environment"}

        if environment.status == "running":
            return {"status": "running", "message": "Producer environment is running"}

        if environment.status in ("creating", "starting", "activating"):
            return {"status": "activating", "message": "Producer environment is starting"}

        if environment.status in WAKEABLE_ENV_STATUSES:
            try:
                await EnvironmentService.activate_environment(
                    session, agent.id, environment.id
                )
                return {"status": "activating", "message": "Producer environment is waking up"}
            except Exception as e:
                logger.error(
                    "agent_api auto-activation failed for env %s: %s",
                    environment.id, e,
                )
                return {"status": "error", "message": str(e)}

        return {"status": "error", "message": f"Environment status: {environment.status}"}

    @staticmethod
    async def resolve_running_producer_env(
        session: Session,
        agent: Agent,
    ) -> AgentEnvironment:
        """
        Resolve + auto-activate the producer's env on the PRODUCER's behalf.

        The token's owner is the producer, so we resolve as that owner and the
        webapp env-selection rule applies unchanged.

        - **Fast path:** the env is already running → return it immediately.
        - **Cold path:** the env is suspended / stopped / mid-activation → kick
          off activation and block up to ``ACTIVATION_WAIT_SECONDS`` for it to
          come up, then return the now-running env so the caller can forward the
          request. The consumer's first call after an idle period just takes a
          little longer; subsequent calls hit the fast path.

        Raises ``AgentApiNotRunningError`` (503) only when activation errors out
        or the env is still not running after the grace window — the consumer
        retries, by which point the env is typically already up.
        """
        try:
            _agent, environment = AgentApiService.resolve_producer_environment(
                session,
                agent.id,
                agent.owner_id,
                is_superuser=False,
                require_agent_api_enabled=True,
            )
            return environment
        except AgentApiNotRunningError:
            pass

        environment = None
        if agent.active_environment_id:
            environment = session.get(AgentEnvironment, agent.active_environment_id)
        if environment is None:
            raise AgentApiNotRunningError("No active environment")

        # Kick off activation (handles suspended / stopped / already in-progress).
        status = await AgentApiService.auto_activate_if_wakeable(
            session, agent, environment
        )
        if status["status"] == "running":
            return environment
        if status["status"] == "error":
            raise AgentApiNotRunningError(
                status.get("message", "Producer environment is not running")
            )

        # status == "activating" → block briefly for the container to come up.
        return await AgentApiService._wait_for_running_env(session, environment.id)

    @staticmethod
    async def _wait_for_running_env(
        session: Session,
        environment_id: uuid.UUID,
    ) -> AgentEnvironment:
        """
        Block until the env reaches ``running`` (up to ``ACTIVATION_WAIT_SECONDS``).

        Each poll reads the status through a short-lived ``create_session()`` —
        a fresh session per iteration — for two reasons: (1) it does not pin the
        request's pooled connection across the ``asyncio.sleep`` (the connection
        is released before each wait, so concurrent cold starts can't starve the
        pool), and (2) background activation commits its status transitions in
        its own session, which a fresh read observes. This mirrors
        ``SessionService._wait_for_environment_ready``. The status is checked
        once before the first sleep, so an already-ready env returns without a
        polling delay. On success the now-running env is re-read into the
        caller's ``session`` (and ``refresh``ed) so ``update_last_activity`` and
        the adapter lookup operate on a bound, fresh instance.

        Raises ``AgentApiNotRunningError`` (503) if activation fails (env →
        ``error``) or the grace window elapses before the env is running — the
        consumer retries, by which point the env is typically up.
        """
        import asyncio
        import time

        deadline = time.monotonic() + ACTIVATION_WAIT_SECONDS
        while True:
            with create_session() as poll_db:
                env = poll_db.get(AgentEnvironment, environment_id)
                if env is None:
                    raise AgentApiNotRunningError("Producer environment no longer exists")
                status = env.status
                status_message = env.status_message

            if status == "running":
                break
            if status == "error":
                raise AgentApiNotRunningError(
                    status_message or "Producer environment failed to start"
                )
            if time.monotonic() >= deadline:
                raise AgentApiNotRunningError(
                    "Producer environment is still starting; retry shortly"
                )
            await asyncio.sleep(ACTIVATION_POLL_INTERVAL)

        # Re-bind the running env to the request session for the caller.
        env = session.get(AgentEnvironment, environment_id)
        if env is None:
            raise AgentApiNotRunningError("Producer environment no longer exists")
        session.refresh(env)
        return env

    @staticmethod
    async def authorize_consumer_request(
        session: Session,
        agent: Agent,
        token: "AgentApiToken",
        method: str,
        path: str,
        body_size: int,
        incoming_headers: dict,
    ) -> tuple[AgentEnvironment, dict]:
        """
        Authorize a token-authenticated consumer request end-to-end.

        Orchestrates: load the cached policy (fail-closed; defaults when cold) →
        enforce it (method / body / rate / path / hop-depth) → compute the
        propagated deadline + hop-depth headers → resolve + auto-activate the
        producer env. Enforcement runs BEFORE env resolution so a 405/413/429
        never wakes a suspended env.

        Returns ``(environment, hop_headers)`` where ``hop_headers`` must be
        injected into the downstream proxy request.

        Raises ``AgentApiPolicyError`` (405/413/429/403) or
        ``AgentApiNotRunningError`` (503).
        """
        policy = AgentApiService.get_effective_policy(session, agent)

        AgentApiService.enforce_policy(
            policy,
            method=method,
            path=path,
            body_size=body_size,
            token=token,
            hop_depth=AgentApiService.incoming_hop_depth(incoming_headers),
        )

        hop_headers = AgentApiService.next_hop_headers(incoming_headers)
        environment = await AgentApiService.resolve_running_producer_env(session, agent)
        return environment, hop_headers

    @staticmethod
    def get_effective_policy(session: Session, agent: Agent) -> dict:
        """
        Return the policy to enforce for a consumer request, from the env cache.

        Falls back to ``DEFAULT_POLICY`` (read_only=true) when no policy has been
        cached yet. A fail-closed (deny-all) policy is honored as-is.
        """
        environment: AgentEnvironment | None = None
        if agent.active_environment_id:
            environment = session.get(AgentEnvironment, agent.active_environment_id)
        if environment and environment.agent_api_policy_cache:
            return environment.agent_api_policy_cache
        return dict(DEFAULT_POLICY)

    # ------------------------------------------------------------------ #
    # Status                                                              #
    # ------------------------------------------------------------------ #

    @staticmethod
    async def get_status(
        session: Session,
        agent: Agent,
        environment: AgentEnvironment | None,
    ) -> dict:
        """
        Build/run status for the producer API (owner preview).

        Reports ``state`` of ``disabled`` | ``not_running`` | ``running`` |
        ``error``, whether the spec is available, the last boot error, and a
        cached policy summary. Does NOT require ``agent_api_enabled`` (so the UI
        can render the enable CTA) and never spawns the serving child.
        """
        base: dict = {
            "agent_api_enabled": agent.agent_api_enabled,
            "spec_available": False,
            "last_error": None,
            "policy": None,
            # Spec freshness is tracked separately from serving-child health so a
            # stale cached spec is visible instead of masquerading as current:
            # the live ``state`` reflects the serving child, while
            # ``spec_fetched_at`` dates the last successful harvest.
            "spec_fetched_at": None,
        }

        if not agent.agent_api_enabled:
            return {**base, "state": "disabled"}

        # Surface cached values even when the env is not running.
        if environment is not None:
            base["spec_available"] = environment.agent_api_spec_parsed is not None
            base["last_error"] = environment.agent_api_spec_error
            base["policy"] = environment.agent_api_policy_cache
            if environment.agent_api_spec_fetched_at is not None:
                base["spec_fetched_at"] = environment.agent_api_spec_fetched_at.isoformat()

        if not environment or environment.status != "running":
            status_label = environment.status if environment else "no_environment"
            return {**base, "state": "not_running", "env_status": status_label}

        # Env is running — ask env-core for live status (no child spawn).
        try:
            lifecycle = EnvironmentService.get_lifecycle_manager()
            adapter = lifecycle.get_adapter(environment)
            live = await adapter.get_agent_api_status()
        except Exception as e:
            logger.debug("agent_api get_status adapter error for env %s: %s", environment.id, e)
            return {**base, "state": "error", "last_error": str(e)}

        live_state = live.get("state", "running")
        return {
            **base,
            "state": live_state,
            "spec_available": live.get("spec_available", base["spec_available"]),
            "last_error": live.get("last_error") or base["last_error"],
            "policy": live.get("policy") or base["policy"],
            "child_running": live.get("child_running", False),
            "has_app": live.get("has_app", False),
        }

    # ------------------------------------------------------------------ #
    # Spec (cache + import-only harvest; never spawns the serving child)  #
    # ------------------------------------------------------------------ #

    @staticmethod
    async def get_spec(
        session: Session,
        environment: AgentEnvironment,
        *,
        force_refresh: bool = False,
    ) -> dict:
        """
        Return the harvested OpenAPI spec.

        Serves from the ``agent_api_spec_parsed`` cache on the env row when
        present and not forced. Otherwise performs a short-lived import-only
        harvest via env-core (``GET /agent-api/openapi.json``) — which imports
        the modules and calls ``app.openapi()`` *without* spawning the serving
        child — and caches the result.

        Raises:
            AgentApiNotRunningError: env not running and no cached spec.
            AgentApiError: harvest failed.
        """
        if not force_refresh and environment.agent_api_spec_parsed is not None:
            return environment.agent_api_spec_parsed

        if environment.status != "running":
            # Can't harvest from a stopped env, and the cache is cold.
            raise AgentApiNotRunningError(
                f"Producer environment must be running to harvest the spec "
                f"(current: {environment.status})"
            )

        lifecycle = EnvironmentService.get_lifecycle_manager()
        adapter = lifecycle.get_adapter(environment)
        try:
            spec = await adapter.get_agent_api_spec()
        except Exception as e:
            logger.warning(
                "agent_api spec harvest failed for env %s: %s", environment.id, e
            )
            AgentApiService._persist_spec_error(environment, str(e), session)
            raise AgentApiError(f"Failed to harvest agent API spec: {e}", status_code=502) from e

        AgentApiService.cache_spec(session, environment, spec)
        return spec

    @staticmethod
    def cache_spec(
        session: Session,
        environment: AgentEnvironment,
        spec: dict,
    ) -> None:
        """Persist a freshly harvested spec onto the env row (clears prior error)."""
        env = session.get(AgentEnvironment, environment.id)
        if env is None:
            return
        env.agent_api_spec_parsed = spec
        env.agent_api_spec_fetched_at = datetime.now(UTC)
        env.agent_api_spec_error = None
        session.add(env)
        session.commit()
        session.refresh(env)

    @staticmethod
    def _persist_spec_error(
        environment: AgentEnvironment,
        error: str,
        session: Session,
    ) -> None:
        """Persist a harvest/boot error without clobbering a previously good spec."""
        try:
            env = session.get(AgentEnvironment, environment.id)
            if env is None:
                return
            env.agent_api_spec_error = error[:512]
            session.add(env)
            session.commit()
        except Exception as e:  # best-effort
            logger.debug("agent_api _persist_spec_error failed: %s", e)

    @staticmethod
    async def refresh_spec_cache(environment_id: uuid.UUID) -> None:
        """
        Re-harvest + re-cache the spec for an env, opening its own DB session.

        Called from the env-core reload notification handler so the owner sees a
        fresh spec (and any new boot error) without making a request. Best-effort
        — never raises.
        """
        from app.core.db import create_session

        agent_id: uuid.UUID | None = None
        state = "running"
        last_error: str | None = None
        try:
            with create_session() as session:
                env = session.get(AgentEnvironment, environment_id)
                if env is None or env.status != "running":
                    return
                agent_id = env.agent_id
                try:
                    await AgentApiService.get_spec(session, env, force_refresh=True)
                except AgentApiError as e:
                    # error already persisted by get_spec
                    state = "error"
                    last_error = e.message
                # Refresh the policy cache alongside the spec.
                await AgentApiService.load_policy(session, env, force_refresh=True)
                # Capture final error state for the event.
                session.refresh(env)
                if last_error is None and env.agent_api_spec_error:
                    state = "error"
                    last_error = env.agent_api_spec_error
        except Exception as e:
            logger.debug("agent_api refresh_spec_cache swallowed for env %s: %s", environment_id, e)
            return

        # Emit AGENT_API_STATUS_CHANGED so the owner UI updates live.
        if agent_id is not None:
            AgentApiService._fire_status_changed_event(
                agent_id, environment_id, state, last_error
            )

    @staticmethod
    def _fire_status_changed_event(
        agent_id: uuid.UUID,
        environment_id: uuid.UUID,
        state: str,
        last_error: str | None,
    ) -> None:
        """Emit AGENT_API_STATUS_CHANGED via the event bus (best-effort; never raises)."""
        try:
            import asyncio

            from app.core.db import create_session
            from app.models.agents.agent import Agent as AgentModel
            from app.models.events.event import EventType
            from app.services.events.event_service import event_service

            owner_id = None
            with create_session() as sess:
                agent = sess.get(AgentModel, agent_id)
                if agent:
                    owner_id = agent.owner_id
            if owner_id is None:
                return

            async def _emit() -> None:
                await event_service.emit_event(
                    event_type=EventType.AGENT_API_STATUS_CHANGED,
                    model_id=agent_id,
                    user_id=owner_id,
                    meta={
                        "agent_id": str(agent_id),
                        "environment_id": str(environment_id),
                        "state": state,
                        "last_error": last_error,
                    },
                )

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_emit())
            except RuntimeError:
                pass  # no running loop in a sync context
        except Exception as e:
            logger.debug("Failed to emit AGENT_API_STATUS_CHANGED: %s", e)

    # ------------------------------------------------------------------ #
    # Policy (fail-closed parse; cached on the env row)                   #
    # ------------------------------------------------------------------ #

    @staticmethod
    async def load_policy(
        session: Session,
        environment: AgentEnvironment,
        *,
        force_refresh: bool = False,
    ) -> dict:
        """
        Load + cache the producer's ``policy.yaml`` guardrails.

        Reads ``agent_api/policy.yaml`` from the producer workspace, parses it
        with ``yaml.safe_load`` merged over ``DEFAULT_POLICY``, and caches the
        result on ``agent_api_policy_cache``. Parse errors **fail closed**:
        the cached policy becomes ``FAIL_CLOSED_POLICY`` (deny-all) so a broken
        policy never widens access.

        A missing file is treated as "use the defaults" (not an error).

        Enforcement of the cached policy at the proxy edge is done by
        ``enforce_policy`` on the consumer route (Phase 2).
        """
        if not force_refresh and environment.agent_api_policy_cache is not None:
            return environment.agent_api_policy_cache

        if environment.status != "running":
            # Can't read the file; return cache or defaults without persisting.
            return environment.agent_api_policy_cache or dict(DEFAULT_POLICY)

        policy = await AgentApiService._fetch_and_parse_policy(environment)
        AgentApiService._cache_policy(session, environment, policy)
        return policy

    @staticmethod
    async def _fetch_and_parse_policy(environment: AgentEnvironment) -> dict:
        """Fetch ``policy.yaml`` via the adapter and parse it fail-closed."""
        lifecycle = EnvironmentService.get_lifecycle_manager()
        adapter = lifecycle.get_adapter(environment)
        try:
            meta, stream = await adapter.fetch_workspace_item_with_meta(POLICY_FILE_PATH)
        except Exception as e:
            logger.debug("agent_api policy fetch error for env %s: %s", environment.id, e)
            # Adapter error → use defaults (env is up but file unreadable). We do
            # NOT fail closed here because the env-core proxy edge (Phase 2) is
            # the enforcement point; an unreadable file with the env running is
            # treated as "no overrides".
            return dict(DEFAULT_POLICY)

        if not meta.exists:
            # No policy.yaml → defaults apply (read_only=true by default).
            return dict(DEFAULT_POLICY)

        raw = await AgentApiService._consume_stream(stream)
        return AgentApiService.parse_policy(raw)

    @staticmethod
    def parse_policy(raw: str) -> dict:
        """
        Parse a ``policy.yaml`` string into a normalized policy dict.

        Pure function (no I/O). Returns ``DEFAULT_POLICY`` merged with the
        parsed overrides on success, or ``FAIL_CLOSED_POLICY`` on any parse
        error / malformed structure (deny-all).
        """
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as e:
            logger.warning("agent_api policy.yaml parse error: %s", e)
            return dict(FAIL_CLOSED_POLICY)

        if data is None:
            # Empty file → defaults.
            return dict(DEFAULT_POLICY)
        if not isinstance(data, dict):
            logger.warning("agent_api policy.yaml top-level is not a mapping — failing closed")
            return dict(FAIL_CLOSED_POLICY)

        policy = dict(DEFAULT_POLICY)
        # Only copy known keys; ignore unknown keys silently.
        for key in (
            "read_only",
            "allowed_methods",
            "auth",
            "max_body_bytes",
            "rate_limit",
            "expose_spec",
            "allowed_paths",
        ):
            if key in data:
                policy[key] = data[key]
        return policy

    @staticmethod
    def _cache_policy(
        session: Session,
        environment: AgentEnvironment,
        policy: dict,
    ) -> None:
        """Persist the parsed policy onto the env row."""
        try:
            env = session.get(AgentEnvironment, environment.id)
            if env is None:
                return
            env.agent_api_policy_cache = policy
            session.add(env)
            session.commit()
            session.refresh(env)
        except Exception as e:
            logger.debug("agent_api _cache_policy failed: %s", e)

    # ------------------------------------------------------------------ #
    # Helpers                                                             #
    # ------------------------------------------------------------------ #

    @staticmethod
    async def _consume_stream(stream) -> str:
        """Read an async byte stream into a string, capped at MAX_POLICY_BYTES."""
        chunks: list[bytes] = []
        total = 0
        async for chunk in stream:
            total += len(chunk)
            if total > MAX_POLICY_BYTES:
                over = total - MAX_POLICY_BYTES
                safe = chunk[:-over] if over < len(chunk) else b""
                chunks.append(safe)
                break
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", errors="replace")

    # ------------------------------------------------------------------ #
    # Policy enforcement (proxy edge)                                      #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _allowed_methods(policy: dict, token: "AgentApiToken | None") -> set[str]:
        """
        Resolve the effective allowed HTTP method set.

        Precedence: explicit ``allowed_methods`` overrides ``read_only``. A token's
        ``read_only_override`` may only NARROW (force read-only), never widen.
        """
        explicit = policy.get("allowed_methods")
        if isinstance(explicit, list) and explicit:
            methods = {m.upper() for m in explicit}
        elif policy.get("read_only", True):
            methods = {"GET", "HEAD"}
        else:
            methods = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}

        # Token override may only narrow to read-only.
        if token is not None and getattr(token, "read_only_override", False):
            methods &= {"GET", "HEAD"}
        return methods

    @staticmethod
    def _parse_rate_limit(spec: str | None) -> tuple[int, int] | None:
        """Parse ``"60/min"`` → (60, 60s). Returns None if unparseable/empty."""
        if not spec:
            return None
        try:
            count_str, _, window_str = spec.partition("/")
            count = int(count_str.strip())
            window_str = window_str.strip().lower()
            window = {"sec": 1, "second": 1, "min": 60, "minute": 60, "hour": 3600}.get(
                window_str, 60
            )
            return count, window
        except (ValueError, AttributeError):
            return None

    @staticmethod
    def enforce_policy(
        policy: dict,
        method: str,
        path: str,
        body_size: int,
        token: "AgentApiToken | None",
        hop_depth: int = 0,
    ) -> None:
        """
        Enforce the producer's policy at the proxy edge, BEFORE forwarding.

        Raises ``AgentApiPolicyError`` with the appropriate status:
          - 405: method not allowed (read_only / allowed_methods)
          - 413: body over ``max_body_bytes``
          - 429: per-token rate limit exceeded (with retry_after)
          - 403: path not in ``allowed_paths`` (or hop-depth exceeded)

        The token's ``read_only_override`` may only narrow the producer's
        policy, never widen it.
        """
        method = method.upper()

        # Hop-depth limit (request-loop protection backstop).
        if hop_depth > MAX_HOP_DEPTH:
            raise AgentApiPolicyError(
                f"Nested agent_api call depth exceeded ({hop_depth} > {MAX_HOP_DEPTH})",
                status_code=403,
            )

        # Method allowlist / read_only.
        allowed = AgentApiService._allowed_methods(policy, token)
        if method not in allowed:
            raise AgentApiPolicyError(
                f"Method {method} not allowed (allowed: {sorted(allowed)})",
                status_code=405,
            )

        # Body cap (before buffering downstream).
        max_body = policy.get("max_body_bytes", DEFAULT_POLICY["max_body_bytes"])
        try:
            max_body = int(max_body)
        except (ValueError, TypeError):
            max_body = DEFAULT_POLICY["max_body_bytes"]
        if max_body >= 0 and body_size > max_body:
            raise AgentApiPolicyError(
                f"Request body {body_size} exceeds max_body_bytes ({max_body})",
                status_code=413,
            )

        # Path allowlist.
        allowed_paths = policy.get("allowed_paths", ["*"])
        if isinstance(allowed_paths, list) and allowed_paths and "*" not in allowed_paths:
            normalized = path.lstrip("/")
            if not any(normalized.startswith(p.lstrip("/")) for p in allowed_paths):
                raise AgentApiPolicyError(
                    f"Path '{path}' not in allowed_paths",
                    status_code=403,
                )

        # Rate limit (per-token bucket, limit from the producer's policy).
        AgentApiService._enforce_rate_limit(policy, token)

    @staticmethod
    def _enforce_rate_limit(policy: dict, token: "AgentApiToken | None") -> None:
        """Sliding-window per-token rate limit. Raises 429 with retry_after."""
        if token is None:
            return  # anonymous (only if policy.auth != required) — no per-token bucket

        policy_rl = AgentApiService._parse_rate_limit(policy.get("rate_limit"))
        if policy_rl is None:
            return
        count, window = policy_rl
        if count <= 0:
            # Fail-closed policy (rate_limit "0/min") → always deny.
            raise AgentApiPolicyError("Rate limit is zero (deny-all)", status_code=429, retry_after=window)

        import time as _time

        now = _time.monotonic()
        cutoff = now - window
        hits = _rate_limit_hits.setdefault(token.id, [])
        # Drop timestamps outside the window.
        hits[:] = [t for t in hits if t > cutoff]
        if len(hits) >= count:
            retry_after = max(1, int(window - (now - hits[0])))
            raise AgentApiPolicyError(
                f"Rate limit exceeded ({count}/{window}s)",
                status_code=429,
                retry_after=retry_after,
            )
        hits.append(now)

    # ------------------------------------------------------------------ #
    # Request-loop protection (deadline + hop depth)                       #
    # ------------------------------------------------------------------ #

    @staticmethod
    def next_hop_headers(incoming_headers: dict) -> dict:
        """
        Compute the deadline + hop-depth headers to forward to the producer.

        Reads the incoming deadline/hop-depth (if this call is itself nested),
        shrinks the deadline by one hop, increments the depth, and returns the
        headers to inject downstream. Raises ``AgentApiPolicyError`` (403) if the
        budget is already exhausted.
        """
        lower = {k.lower(): v for k, v in incoming_headers.items()}

        # Deadline: inherit remaining budget if present, else start fresh.
        raw_deadline = lower.get(DEADLINE_HEADER)
        try:
            deadline_ms = int(raw_deadline) if raw_deadline is not None else DEFAULT_DEADLINE_MS
        except (ValueError, TypeError):
            deadline_ms = DEFAULT_DEADLINE_MS
        deadline_ms = deadline_ms - HOP_DEADLINE_SHRINK_MS
        if deadline_ms <= 0:
            raise AgentApiPolicyError("agent_api call deadline exhausted", status_code=403)

        # Hop depth: increment.
        raw_depth = lower.get(HOP_DEPTH_HEADER)
        try:
            depth = int(raw_depth) if raw_depth is not None else 0
        except (ValueError, TypeError):
            depth = 0
        depth += 1
        if depth > MAX_HOP_DEPTH:
            raise AgentApiPolicyError(
                f"Nested agent_api call depth exceeded ({depth} > {MAX_HOP_DEPTH})",
                status_code=403,
            )

        return {
            DEADLINE_HEADER: str(deadline_ms),
            HOP_DEPTH_HEADER: str(depth),
        }

    @staticmethod
    def incoming_hop_depth(incoming_headers: dict) -> int:
        """Return the hop-depth declared by the caller (0 if absent/invalid)."""
        lower = {k.lower(): v for k, v in incoming_headers.items()}
        try:
            return int(lower.get(HOP_DEPTH_HEADER, 0))
        except (ValueError, TypeError):
            return 0
