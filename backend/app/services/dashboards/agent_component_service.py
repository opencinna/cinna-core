"""Agent-scoped component operations (D-C9): list, get, run with explicit params, frame.

These back ``/agents/{agent_id}/dashboards/*``: the core preview, the
desktop's remote tiles and ``cinna dashboards``. No values are cached and no
params are stored; list/get read env-core's per-worker component list cache
unless the caller passes ``fresh`` (the CLI does, right after a push); a run is
budgeted per ``p:<user>:<agent>:<skill|->:<component>:<tile_key|preview>``,
except a source run with empty params (a plain preview), which is not counted.
"""
import uuid
from typing import Any

from sqlmodel import Session

from app.models.dashboards.dashboard_component import (
    DashboardComponentPublic,
    DashboardComponentRunRequest,
    DashboardComponentsPublic,
)
from app.models.users.user import User
from app.services.dashboards import component_params, run_budget
from app.services.dashboards.component_service import (
    ComponentService,
    DashboardComponentError,
    not_found,
)


def _public(entry: dict, placements: list[dict]) -> DashboardComponentPublic:
    spec = ComponentService.spec(entry)
    title = spec.get("title")
    view = spec.get("view")
    return DashboardComponentPublic(
        name=str(entry.get("name") or ""),
        skill=entry.get("skill") or None,
        title=title if isinstance(title, str) else None,
        view=view if isinstance(view, str) else None,
        component_json=entry.get("component_json") if isinstance(entry.get("component_json"), dict) else None,
        findings=[f for f in entry.get("findings") or [] if isinstance(f, dict)],
        has_errors=ComponentService.has_errors(entry),
        size_bytes=int(entry.get("size_bytes") or 0),
        placements=placements,
    )


class AgentComponentService:
    @staticmethod
    async def list_public(
        session: Session, user: User, agent_id: uuid.UUID, *, fresh: bool = False
    ) -> DashboardComponentsPublic:
        """``fresh`` skips the per-worker list cache (and refills it)."""
        agent = ComponentService.get_owned_agent(session, agent_id, user.id)
        components = await ComponentService.list_components(session, agent, fresh=fresh)
        placements = ComponentService.placements(session, user.id, agent.id)
        return DashboardComponentsPublic(
            components=[
                _public(entry, placements.get((str(entry.get("name")), entry.get("skill") or None), []))
                for entry in components
            ]
        )

    @staticmethod
    async def get_public(
        session: Session,
        user: User,
        agent_id: uuid.UUID,
        name: str,
        skill: str | None,
        *,
        fresh: bool = False,
    ) -> DashboardComponentPublic:
        agent = ComponentService.get_owned_agent(session, agent_id, user.id)
        entry = await ComponentService.get_component(session, agent, name, skill, fresh=fresh)
        placements = ComponentService.placements(session, user.id, agent.id)
        return _public(entry, placements.get((name, skill or None), []))

    @staticmethod
    async def run(
        session: Session,
        user: User,
        agent_id: uuid.UUID,
        name: str,
        body: DashboardComponentRunRequest,
    ) -> tuple[Any, dict]:
        """Run the source or an endpoint; returns ``(value, params_resolved)``."""
        agent = ComponentService.get_owned_agent(session, agent_id, user.id)
        environment, adapter = await ComponentService.running_environment(session, agent, activate=True)
        components = await ComponentService.list_in_environment(environment, adapter)
        entry = ComponentService.find(components, name, body.skill)
        if entry is None:
            raise not_found()
        spec = ComponentService.spec(entry)
        declared = spec.get("params") if isinstance(spec.get("params"), dict) else {}

        # A source run takes the tile's params: every key must be declared.
        # An endpoint run also passes the page's own call arguments through.
        checked = body.params if body.endpoint is None else {
            k: v for k, v in body.params.items() if k in declared
        }
        errors = component_params.validate(declared, checked)
        if errors:
            raise DashboardComponentError("invalid_input", errors[0].message, field=errors[0].field)
        missing = ComponentService.missing_credentials(session, agent, spec)
        if missing:
            raise ComponentService.needs_credentials_error(missing)

        if body.endpoint is not None:
            ComponentService.require_endpoint(entry, body.endpoint)
        if body.endpoint is not None or body.params:
            target = run_budget.preview_target(user.id, agent.id, body.skill, name, body.tile_key)
            retry_after = run_budget.check_and_record(session, target)
            if retry_after is not None:
                raise DashboardComponentError(
                    "rate_limited", "Too many runs for this tile; try again shortly.", retry_after_s=retry_after
                )
        ComponentService.touch_activity(session, environment)

        resolved = component_params.resolve(declared, component_params.clean(checked), user.timezone)
        if body.endpoint is None:
            value = await ComponentService.compute_source(environment, adapter, entry, resolved)
            return value, resolved
        run_params = {**resolved, **{k: v for k, v in body.params.items() if k not in declared}}
        value = await ComponentService.run_endpoint(adapter, entry, body.endpoint, run_params)
        return value, resolved

    @staticmethod
    async def frame_entry(
        session: Session, user: User, agent_id: uuid.UUID, name: str, skill: str | None
    ) -> str:
        """The component's entry page, after waking the environment (``env_unavailable`` until it runs)."""
        agent = ComponentService.get_owned_agent(session, agent_id, user.id)
        environment, adapter = await ComponentService.running_environment(session, agent, activate=True)
        components = await ComponentService.list_in_environment(environment, adapter)
        entry = ComponentService.find(components, name, skill)
        if entry is None:
            raise not_found()
        ComponentService.touch_activity(session, environment)
        return ComponentService.entry_page(entry)
