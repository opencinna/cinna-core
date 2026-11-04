"""Component references: ``cinna://component/<agent>/[<skill>/]<name>`` → a card.

The agent segment always carries its prefix (D-C8):

- ``bundle:<bundle_id>`` resolves among the viewer's own agents, the chat's
  agent first; two other installs are ``ambiguous``. A bundle the viewer can
  see in the catalog but has not installed is ``not_installed`` (with its
  catalog name).
- ``id:<manifest id>`` and a bare slug match only the chat's own agent, by its
  workspace ``cinna-agent.json`` ``id`` / ``slug`` (read from the host's copy of
  the workspace, never from the container).

Rendering never wakes an environment (D8): a suspended one is ``env_asleep``.
"""
import json
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlmodel import Session, select

from app.models.agents.agent import Agent
from app.models.bundles.agent_bundle import AgentBundle, BundleRunsOn
from app.models.environments.environment import AgentEnvironment
from app.models.sessions.session import Session as ChatSession
from app.models.users.user import User
from app.services.bundles.catalog_service import CatalogService
from app.services.dashboards.component_service import ComponentService, DashboardComponentError
from app.services.environments.adapters.base import LocalFilesAccessInterface
from app.services.environments.environment_service import EnvironmentService

logger = logging.getLogger(__name__)

REF_PREFIX = "cinna://component/"
MAX_REF_LENGTH = 512
MANIFEST_PATH = "cinna-agent.json"
_ID_RE = re.compile(r"^id:([0-9a-fA-F-]{36})$")
_BUNDLE_RE = re.compile(r"^bundle:([a-zA-Z0-9][a-zA-Z0-9.\-]{1,253})$")
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_SKILL_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

MESSAGES = {
    "invalid": "This is not a component reference.",
    "ambiguous": "More than one of your agents has this component.",
    "not_installed": "You don't have this agent.",
    "gone": "This component is no longer there.",
    "env_asleep": "Its environment is asleep; Preview wakes it.",
    "adapter_unsupported": "Rebuild the environment to show components.",
}


@dataclass(frozen=True)
class ParsedRef:
    kind: str  # "bundle" | "id" | "slug"
    value: str
    skill: str | None
    name: str


def parse(ref: Any) -> ParsedRef | None:
    """The reference's parts, or None when it breaks the grammar."""
    if not isinstance(ref, str) or len(ref) > MAX_REF_LENGTH or not ref.startswith(REF_PREFIX):
        return None
    parts = ref[len(REF_PREFIX):].split("/")
    if len(parts) == 2:
        segment, skill, name = parts[0], None, parts[1]
    elif len(parts) == 3:
        segment, skill, name = parts
        if not _SKILL_RE.fullmatch(skill):
            return None
    else:
        return None
    if not _NAME_RE.fullmatch(name):
        return None
    if match := _BUNDLE_RE.fullmatch(segment):
        return ParsedRef("bundle", match.group(1), skill, name)
    if match := _ID_RE.fullmatch(segment):
        return ParsedRef("id", match.group(1).lower(), skill, name)
    if _SLUG_RE.fullmatch(segment):
        return ParsedRef("slug", segment, skill, name)
    return None


def _result(ref: str, status: str, **fields: Any) -> dict:
    result: dict[str, Any] = {"ref": ref, "status": status, **fields}
    if status != "ok":
        result.setdefault("message", MESSAGES.get(status))
    return result


class ComponentRefsService:
    @staticmethod
    def chat_agent(db: Session, user: User, session_id: uuid.UUID | None) -> Agent | None:
        """The agent of the user's own chat ``session_id`` (None when absent or not theirs)."""
        if session_id is None:
            return None
        chat = db.get(ChatSession, session_id)
        if chat is None or chat.user_id != user.id or chat.agent_id is None:
            return None
        agent = db.get(Agent, chat.agent_id)
        return agent if agent is not None and agent.owner_id == user.id else None

    @staticmethod
    def _manifest(db: Session, agent: Agent) -> dict:
        """The agent's workspace ``cinna-agent.json`` from the host copy; ``{}`` when unavailable."""
        environment = (
            db.get(AgentEnvironment, agent.active_environment_id) if agent.active_environment_id else None
        )
        if environment is None:
            return {}
        try:
            adapter = EnvironmentService.get_lifecycle_manager().get_adapter(environment)
        except Exception as e:  # an adapter that cannot be built here has no host copy
            logger.debug("No adapter for env %s: %s", environment.id, e)
            return {}
        if not isinstance(adapter, LocalFilesAccessInterface):
            return {}
        path = adapter.get_local_workspace_file_path(MANIFEST_PATH)
        if path is None:
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _bundle_agents(
        db: Session, user: User, parsed: ParsedRef, chat_agent: Agent | None
    ) -> tuple[str, list[Agent], str | None]:
        """``(status, agents, catalog_name)`` for a ``bundle:`` reference."""
        if chat_agent is not None and chat_agent.bundle_id == parsed.value:
            return "ok", [chat_agent], None
        agents = list(
            db.exec(select(Agent).where(Agent.owner_id == user.id, Agent.bundle_id == parsed.value)).all()
        )
        if len(agents) > 1:
            return "ambiguous", agents, None
        if agents:
            return "ok", agents, None
        bundle = db.exec(select(AgentBundle).where(AgentBundle.bundle_id == parsed.value)).first()
        if (
            bundle is not None
            and bundle.runs_on == BundleRunsOn.CLOUD
            and CatalogService.user_can_see(db, bundle, user)
        ):
            return "not_installed", [], bundle.display_name
        return "gone", [], None

    @staticmethod
    async def _resolve_in_agent(
        db: Session,
        user: User,
        ref: str,
        parsed: ParsedRef,
        agent: Agent,
        listings: dict[uuid.UUID, list[dict] | str],
    ) -> dict:
        agent_ref = {"id": agent.id, "name": agent.name}
        if agent.id not in listings:
            if not ComponentService.is_running(db, agent):
                listings[agent.id] = "env_asleep"
            else:
                try:
                    listings[agent.id] = await ComponentService.list_components(db, agent, activate=False, fresh=True)
                except DashboardComponentError as e:
                    listings[agent.id] = "adapter_unsupported" if e.code == "adapter_unsupported" else "env_asleep"
        listing = listings[agent.id]
        if isinstance(listing, str):
            return _result(ref, listing, agent=agent_ref)
        entry = ComponentService.find(listing, parsed.name, parsed.skill)
        if entry is None:
            return _result(ref, "gone", agent=agent_ref)
        spec = ComponentService.spec(entry)
        placements: dict[uuid.UUID, dict] = {}
        for placement in ComponentService.placements(db, user.id, agent.id).get((parsed.name, parsed.skill), []):
            placements.setdefault(
                placement["dashboard_id"],
                {"dashboard_id": placement["dashboard_id"], "name": placement["dashboard_name"]},
            )
        title, view = spec.get("title"), spec.get("view")
        return _result(
            ref,
            "ok",
            agent=agent_ref,
            component={
                "name": parsed.name,
                "skill": parsed.skill,
                "title": title if isinstance(title, str) else None,
                "view": view if isinstance(view, str) else None,
            },
            from_skill=parsed.skill,
            findings=[f for f in entry.get("findings") or [] if isinstance(f, dict)],
            placements=list(placements.values()),
        )

    @staticmethod
    async def resolve(
        db: Session, user: User, refs: list[str], chat_agent: Agent | None
    ) -> list[dict]:
        """One result per reference, in order; nothing is woken or run."""
        listings: dict[uuid.UUID, list[dict] | str] = {}
        manifest: dict | None = None
        results: list[dict] = []
        for ref in refs:
            parsed = parse(ref)
            if parsed is None:
                results.append(_result(str(ref)[:MAX_REF_LENGTH], "invalid"))
                continue
            if parsed.kind == "bundle":
                status, agents, catalog_name = ComponentRefsService._bundle_agents(db, user, parsed, chat_agent)
                if status != "ok":
                    results.append(_result(ref, status, catalog_name=catalog_name))
                    continue
                agent = agents[0]
            else:
                if chat_agent is None:
                    results.append(_result(ref, "gone"))
                    continue
                if manifest is None:
                    manifest = ComponentRefsService._manifest(db, chat_agent)
                key = "id" if parsed.kind == "id" else "slug"
                value = manifest.get(key)
                if not isinstance(value, str) or value.lower() != parsed.value.lower():
                    results.append(_result(ref, "gone"))
                    continue
                agent = chat_agent
            results.append(
                await ComponentRefsService._resolve_in_agent(db, user, ref, parsed, agent, listings)
            )
        return results
