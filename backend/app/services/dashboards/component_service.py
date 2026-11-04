"""Agent dashboard components: listing, reading and running them in the environment.

A component is a folder ``dashboards/<name>/`` in the agent (or inside one of
its skills) described by ``component.json``. env-core lists them (with the kit
checker's findings), serves their files and runs their endpoints through its
``/dashboards/*`` routes; this service is the backend's only way to them.

Environment handling follows the webapp: a suspended environment is woken
through ``WebappService.get_public_status`` and the caller is told to come back
(``env_unavailable``), except where the caller must never wake it (an
``interval`` tick, a cached read). A container whose ``/app/core`` predates the
routes answers 404 and surfaces as ``adapter_unsupported``.
"""
import csv
import io
import json
import logging
import time
import uuid
from typing import Any

from sqlmodel import Session, select

from app.models.agents.agent import Agent
from app.models.environments.environment import AgentEnvironment
from app.models.users.user_dashboard import UserDashboard, UserDashboardBlock
from app.services.bundles.install_readiness_gate import InstallReadinessGate
from app.services.environments.adapters.base import (
    EndpointUnsupportedError,
    EnvironmentAdapter,
)
from app.services.environments.environment_service import EnvironmentService
from app.services.webapp.webapp_service import WebappService

logger = logging.getLogger(__name__)

AGENT_COMPONENT_VIEW_TYPE = "agent_component"
LIST_CACHE_TTL_S = 10.0
MAX_MARK_LENGTH = 500
REFRESH_COMMAND_TIMEOUT_S = 120

_STATUS_BY_CODE: dict[str, int] = {
    "invalid_input": 400,
    "not_found": 404,
    "needs_credentials": 409,
    "adapter_unsupported": 409,
    "rate_limited": 429,
    "call_failed": 502,
    "env_unavailable": 503,
}

ADAPTER_UNSUPPORTED_MESSAGE = "Rebuild the environment to show components."


class DashboardComponentError(Exception):
    """A refusal with one of the agent-component error codes."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retry_after_s: int | None = None,
        field: str | None = None,
    ):
        self.code = code
        self.message = message
        self.retry_after_s = retry_after_s
        self.field = field
        super().__init__(message)

    @property
    def status_code(self) -> int:
        return _STATUS_BY_CODE.get(self.code, 400)

    def to_body(self) -> dict:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.retry_after_s is not None:
            error["retry_after_s"] = self.retry_after_s
        if self.field is not None:
            error["field"] = self.field
        return {"error": error}


def not_found(message: str = "Component not found.") -> DashboardComponentError:
    return DashboardComponentError("not_found", message)


def sentence(message: str | None, fallback: str = "The run failed.") -> str:
    """One line of at most 500 characters, for a tile's mark."""
    text = " ".join((message or "").split()) or fallback
    return text[:MAX_MARK_LENGTH]


def _body_message(body: bytes) -> str | None:
    """env-core's refusal sentence from a JSON error body (``message``, ``error`` or ``detail``)."""
    try:
        payload = json.loads(body)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("message", "error", "detail"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict) and isinstance(value.get("message"), str):
            return value["message"]
    return None


# Per worker: environment id → (monotonic time, components). Listing scans
# every component and runs the checker, so a dashboard opening many tiles at
# once lists once.
_list_cache: dict[uuid.UUID, tuple[float, list[dict]]] = {}


class ComponentService:
    """Static-method service over env-core's ``/dashboards/*`` routes."""

    # ── Ownership ─────────────────────────────────────────────────────

    @staticmethod
    def get_owned_agent(session: Session, agent_id: uuid.UUID, user_id: uuid.UUID) -> Agent:
        """The user's own agent; ``not_found`` for anyone else's."""
        agent = session.get(Agent, agent_id)
        if agent is None or agent.owner_id != user_id:
            raise not_found("Agent not found.")
        return agent

    # ── Environment ───────────────────────────────────────────────────

    @staticmethod
    def _active_environment(session: Session, agent: Agent) -> AgentEnvironment:
        environment = (
            session.get(AgentEnvironment, agent.active_environment_id)
            if agent.active_environment_id
            else None
        )
        if environment is None:
            raise DashboardComponentError("env_unavailable", "The agent has no environment.")
        return environment

    @staticmethod
    def _adapter(environment: AgentEnvironment) -> EnvironmentAdapter:
        return EnvironmentService.get_lifecycle_manager().get_adapter(environment)

    @staticmethod
    async def running_environment(
        session: Session, agent: Agent, *, activate: bool
    ) -> tuple[AgentEnvironment, EnvironmentAdapter]:
        """The agent's running environment and its adapter.

        A suspended environment is woken when ``activate`` (the webapp path);
        either way the caller gets ``env_unavailable`` until it runs.
        """
        environment = ComponentService._active_environment(session, agent)
        if environment.status == "running":
            return environment, ComponentService._adapter(environment)
        if activate and environment.status == "suspended":
            status = await WebappService.get_public_status(session, agent, environment)
            if status.get("status") == "error":
                raise DashboardComponentError(
                    "env_unavailable", sentence(status.get("message"), "The environment could not start.")
                )
            raise DashboardComponentError(
                "env_unavailable", "The environment is starting; try again in a moment."
            )
        if environment.status in ("creating", "starting", "activating"):
            raise DashboardComponentError(
                "env_unavailable", "The environment is starting; try again in a moment."
            )
        raise DashboardComponentError(
            "env_unavailable", f"The environment is {environment.status}."
        )

    @staticmethod
    def is_running(session: Session, agent: Agent) -> bool:
        try:
            return ComponentService._active_environment(session, agent).status == "running"
        except DashboardComponentError:
            return False

    @staticmethod
    def touch_activity(session: Session, environment: AgentEnvironment) -> None:
        """Keep the environment awake, as webapp traffic does."""
        WebappService.update_last_activity(session, environment)

    # ── Listing ───────────────────────────────────────────────────────

    @staticmethod
    def clear_cache() -> None:
        _list_cache.clear()

    @staticmethod
    async def list_in_environment(
        environment: AgentEnvironment, adapter: EnvironmentAdapter, *, fresh: bool = False
    ) -> list[dict]:
        """env-core's component list (cached per worker for a few seconds).

        ``fresh`` skips the cache (and refills it): for paths that follow the
        agent's own edits (its tools, manual / after-turn / params recomputes,
        page calls, reference cards).
        """
        cached = None if fresh else _list_cache.get(environment.id)
        if cached is not None and time.monotonic() - cached[0] < LIST_CACHE_TTL_S:
            return cached[1]
        try:
            payload = await adapter.list_dashboard_components()
        except EndpointUnsupportedError:
            raise DashboardComponentError("adapter_unsupported", ADAPTER_UNSUPPORTED_MESSAGE)
        except Exception as e:
            logger.warning("Listing dashboard components failed on env %s: %s", environment.id, e)
            raise DashboardComponentError("call_failed", "The environment did not answer.")
        components = [c for c in (payload or {}).get("components") or [] if isinstance(c, dict)]
        _list_cache[environment.id] = (time.monotonic(), components)
        return components

    @staticmethod
    async def list_components(
        session: Session, agent: Agent, *, activate: bool = True, fresh: bool = False
    ) -> list[dict]:
        environment, adapter = await ComponentService.running_environment(
            session, agent, activate=activate
        )
        return await ComponentService.list_in_environment(environment, adapter, fresh=fresh)

    @staticmethod
    def find(components: list[dict], name: str, skill: str | None) -> dict | None:
        for entry in components:
            if entry.get("name") == name and (entry.get("skill") or None) == (skill or None):
                return entry
        return None

    @staticmethod
    async def get_component(
        session: Session, agent: Agent, name: str, skill: str | None, *, activate: bool = True
    ) -> dict:
        components = await ComponentService.list_components(session, agent, activate=activate)
        entry = ComponentService.find(components, name, skill)
        if entry is None:
            raise not_found()
        return entry

    @staticmethod
    def spec(entry: dict) -> dict:
        """The entry's component.json (``{}`` when it does not parse)."""
        doc = entry.get("component_json")
        return doc if isinstance(doc, dict) else {}

    @staticmethod
    def has_errors(entry: dict) -> bool:
        return any(
            isinstance(f, dict) and f.get("severity") == "error" for f in entry.get("findings") or []
        )

    @staticmethod
    def placements(
        session: Session, user_id: uuid.UUID, agent_id: uuid.UUID
    ) -> dict[tuple[str, str | None], list[dict]]:
        """The user's blocks showing each of the agent's components, by (name, skill)."""
        rows = session.exec(
            select(UserDashboardBlock, UserDashboard)
            .join(UserDashboard, UserDashboard.id == UserDashboardBlock.dashboard_id)  # type: ignore[arg-type]
            .where(
                UserDashboard.owner_id == user_id,
                UserDashboardBlock.agent_id == agent_id,
                UserDashboardBlock.view_type == AGENT_COMPONENT_VIEW_TYPE,
            )
        ).all()
        found: dict[tuple[str, str | None], list[dict]] = {}
        for block, dashboard in rows:
            config = block.config or {}
            key = (str(config.get("component") or ""), config.get("skill") or None)
            found.setdefault(key, []).append(
                {
                    "dashboard_id": dashboard.id,
                    "dashboard_name": dashboard.name,
                    "block_id": block.id,
                    "origin": dashboard.origin,
                }
            )
        return found

    # ── Credentials ───────────────────────────────────────────────────

    @staticmethod
    def missing_credentials(session: Session, agent: Agent, spec: dict) -> list[str]:
        """``requires_credentials`` names the agent cannot use now.

        A name no linked credential resolves to counts as missing.
        """
        required = spec.get("requires_credentials")
        if not isinstance(required, list) or not required:
            return []
        readiness = InstallReadinessGate.service_credential_readiness(session, agent)
        return [
            name for name in required
            if isinstance(name, str) and not readiness.get(name, False)
        ]

    @staticmethod
    def needs_credentials_error(missing: list[str]) -> DashboardComponentError:
        if len(missing) == 1:
            message = f"Needs the {missing[0]} credential."
        else:
            message = f"Needs the {', '.join(missing[:-1])} and {missing[-1]} credentials."
        return DashboardComponentError("needs_credentials", message)

    # ── Running ───────────────────────────────────────────────────────

    @staticmethod
    def require_endpoint(entry: dict, endpoint: str) -> dict:
        """The component's ``endpoints``; ``not_found`` unless ``endpoint`` is one of them.

        Checked before a run is counted, so an undeclared name spends no budget.
        """
        endpoints = ComponentService.spec(entry).get("endpoints")
        if not isinstance(endpoints, dict) or endpoint not in endpoints:
            raise not_found(f"The component has no endpoint {endpoint}.")
        return endpoints

    @staticmethod
    async def run_endpoint(
        adapter: EnvironmentAdapter,
        entry: dict,
        endpoint: str,
        params: dict,
    ) -> Any:
        """Run one of the component's own endpoints; returns its JSON value."""
        endpoints = ComponentService.require_endpoint(entry, endpoint)
        timeout = endpoints[endpoint].get("timeout_s") if isinstance(endpoints[endpoint], dict) else None
        try:
            result = await adapter.run_dashboard_endpoint(
                str(entry.get("name")),
                entry.get("skill") or None,
                endpoint,
                params,
                timeout if isinstance(timeout, int) else None,
            )
        except EndpointUnsupportedError:
            raise DashboardComponentError("adapter_unsupported", ADAPTER_UNSUPPORTED_MESSAGE)
        except Exception as e:
            logger.warning("Dashboard endpoint run failed to reach the environment: %s", e)
            raise DashboardComponentError("call_failed", "The environment did not answer.")
        if result.get("ok"):
            return result.get("value")
        code = result.get("code")
        if code not in ("not_found", "invalid_input"):
            code = "call_failed"
        raise DashboardComponentError(str(code), sentence(result.get("message")))

    @staticmethod
    async def read_source_file(adapter: EnvironmentAdapter, entry: dict) -> Any:
        """The component's ``source.file`` as a view value (no run)."""
        source = ComponentService.spec(entry).get("source") or {}
        path = str(source.get("file") or "")
        try:
            status, _, body = await adapter.read_dashboard_source_file(
                str(entry.get("name")), entry.get("skill") or None
            )
        except Exception as e:
            logger.warning("Reading a dashboard source file failed: %s", e)
            raise DashboardComponentError("call_failed", "The environment did not answer.")
        if status == 404:
            raise DashboardComponentError("call_failed", f"The source file {path} does not exist yet.")
        if status >= 400:
            raise DashboardComponentError(
                "call_failed", sentence(_body_message(body), "The source file could not be read.")
            )
        return ComponentService.parse_source(path, body)

    @staticmethod
    def parse_source(path: str, body: bytes) -> Any:
        """A source file's content as the view's data shape."""
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            raise DashboardComponentError("call_failed", "The source file is not UTF-8 text.")
        lowered = path.lower()
        if lowered.endswith(".md"):
            return {"markdown": text}
        if lowered.endswith(".csv"):
            reader = csv.DictReader(io.StringIO(text))
            columns = [{"key": name} for name in reader.fieldnames or []]
            return {"columns": columns, "rows": [dict(row) for row in reader]}
        try:
            return json.loads(text)
        except ValueError:
            raise DashboardComponentError("call_failed", "The source file is not valid JSON.")

    @staticmethod
    async def run_refresh_command(
        environment: AgentEnvironment, adapter: EnvironmentAdapter, command_ref: str
    ) -> str | None:
        """Run ``refresh.command`` (``/run:<name>``) before the source is read.

        Returns a failure sentence, or None when it ran cleanly. Output is
        never surfaced: it may carry the owner's data.
        """
        from app.services.agents.cli_commands_service import CLICommandsService

        name = command_ref[len("/run:"):] if command_ref.startswith("/run:") else ""
        command = next(
            (c.command for c in CLICommandsService.get_cached_commands(environment) if c.name == name),
            None,
        )
        if command is None:
            return f"The refresh command {command_ref} is not defined in CLI_COMMANDS.yaml."
        try:
            result = await adapter.execute_command(command, timeout=REFRESH_COMMAND_TIMEOUT_S)
        except Exception as e:
            logger.info("Dashboard refresh command failed on env %s: %s", environment.id, e)
            return f"The refresh command {command_ref} failed."
        if result.exit_code != 0:
            return f"The refresh command {command_ref} exited with code {result.exit_code}."
        return None

    @staticmethod
    async def compute_source(
        environment: AgentEnvironment,
        adapter: EnvironmentAdapter,
        entry: dict,
        params: dict,
    ) -> Any:
        """Recompute the component's source: its refresh command, then the endpoint or file."""
        spec = ComponentService.spec(entry)
        refresh = spec.get("refresh") if isinstance(spec.get("refresh"), dict) else {}
        command_ref = refresh.get("command")
        if isinstance(command_ref, str) and command_ref:
            failure = await ComponentService.run_refresh_command(environment, adapter, command_ref)
            if failure:
                raise DashboardComponentError("call_failed", failure)
        source = spec.get("source") if isinstance(spec.get("source"), dict) else {}
        if source.get("endpoint"):
            return await ComponentService.run_endpoint(adapter, entry, str(source["endpoint"]), params)
        if source.get("file"):
            return await ComponentService.read_source_file(adapter, entry)
        raise DashboardComponentError("call_failed", "The component has no source.")

    @staticmethod
    def entry_page(entry: dict) -> str:
        """The page a web view opens; ``invalid_input`` for a native view."""
        spec = ComponentService.spec(entry)
        if spec.get("view") != "html":
            raise DashboardComponentError("invalid_input", "This component has no page.")
        entry_path = spec.get("entry")
        return entry_path if isinstance(entry_path, str) and entry_path else "index.html"
