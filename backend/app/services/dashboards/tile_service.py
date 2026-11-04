"""Agent-component tiles: cached data, refresh, params, page calls and page state.

A tile is an ``agent_component`` dashboard block. Its last good result is kept
in ``user_dashboard_block_result`` and served first (stale or not); the page
then asks for a refresh with a reason:

- ``open`` / ``reshow`` recompute only when the cache is older than
  ``max_age`` (or was computed with other params);
- ``interval`` never wakes a suspended environment and never extends its
  activity: it answers ``skipped: env_asleep`` instead;
- ``manual`` / ``after_turn`` / ``params`` always recompute.

Every run except ``interval`` keeps the environment awake as webapp traffic
does. A failed run keeps the cached data and records a mark (one sentence).
Params changes and page calls count against the tile's run budget
(``run_budget``); opening and refreshing do not. A schedule-fed tile
(``refresh.schedule``) recomputes lazily on read, once per settled schedule run.
"""
import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlmodel import Session, col, select

from app.models.agents.agent import Agent
from app.models.agents.agent_schedule import AgentSchedule
from app.models.agents.agent_schedule_log import AgentScheduleLog
from app.models.dashboards.dashboard_component import UserDashboardBlockResult
from app.models.environments.environment import AgentEnvironment
from app.models.sessions.session import Session as ChatSession
from app.models.users.user import User
from app.models.users.user_dashboard import UserDashboard, UserDashboardBlock
from app.services.dashboards import component_params, run_budget
from app.services.dashboards.component_service import (
    AGENT_COMPONENT_VIEW_TYPE,
    ComponentService,
    DashboardComponentError,
    not_found,
    sentence,
)
from app.services.environments.adapters.base import EnvironmentAdapter

logger = logging.getLogger(__name__)

DEFAULT_MAX_AGE = timedelta(minutes=15)
# An ``interval`` tick never recomputes a result younger than this, whatever
# the component declares (``while_open_every`` below a minute).
INTERVAL_MIN_AGE = timedelta(seconds=60)
# Refresh reasons that follow the agent's own edits: they read a fresh list.
_FRESH_LIST_REASONS = ("manual", "after_turn", "params")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
MAX_RESULT_BYTES = 5 * 1024 * 1024
# A schedule run is settled once it finished and the session it opened (if any) is idle.
_SETTLED_LOG_STATUSES = ("success", "session_triggered", "error", "skipped")
_BUSY_SESSION_STATES = ("running", "pending_stream")

# Per worker: (block id, params hash) → the in-flight recompute. A duplicate
# across workers is harmless: the result is idempotent and the last write wins.
_inflight: dict[tuple[uuid.UUID, str], "asyncio.Future[tuple[bool, Any]]"] = {}


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def parse_duration(value: Any) -> timedelta | None:
    if not isinstance(value, str) or len(value) < 2 or value[-1] not in _DURATION_UNITS:
        return None
    try:
        amount = int(value[:-1])
    except ValueError:
        return None
    return timedelta(seconds=amount * _DURATION_UNITS[value[-1]]) if amount > 0 else None


class TileService:
    """Static-method service for ``agent_component`` blocks."""

    # ── Lookup ────────────────────────────────────────────────────────

    @staticmethod
    def get_block(
        session: Session, dashboard_id: uuid.UUID, block_id: uuid.UUID, user_id: uuid.UUID
    ) -> UserDashboardBlock:
        """The user's own ``agent_component`` block; ``not_found`` otherwise."""
        dashboard = session.get(UserDashboard, dashboard_id)
        block = session.get(UserDashboardBlock, block_id)
        if (
            dashboard is None
            or dashboard.owner_id != user_id
            or block is None
            or block.dashboard_id != dashboard_id
            or block.view_type != AGENT_COMPONENT_VIEW_TYPE
        ):
            raise not_found("Tile not found.")
        return block

    @staticmethod
    def _config(block: UserDashboardBlock) -> dict:
        return block.config if isinstance(block.config, dict) else {}

    @staticmethod
    def _agent(session: Session, block: UserDashboardBlock) -> Agent:
        agent = session.get(Agent, block.agent_id)
        if agent is None:
            raise not_found("Tile not found.")
        return agent

    @staticmethod
    def _result_row(session: Session, block: UserDashboardBlock) -> UserDashboardBlockResult:
        row = session.get(UserDashboardBlockResult, block.id)
        return row if row is not None else UserDashboardBlockResult(block_id=block.id)

    @staticmethod
    async def _component(
        session: Session,
        agent: Agent,
        block: UserDashboardBlock,
        *,
        activate: bool,
        fresh: bool = False,
    ) -> tuple[AgentEnvironment, EnvironmentAdapter, dict]:
        config = TileService._config(block)
        environment, adapter = await ComponentService.running_environment(
            session, agent, activate=activate
        )
        components = await ComponentService.list_in_environment(environment, adapter, fresh=fresh)
        entry = ComponentService.find(components, str(config.get("component") or ""), config.get("skill"))
        if entry is None:
            raise not_found("The component is no longer in the agent.")
        return environment, adapter, entry

    # ── Freshness ─────────────────────────────────────────────────────

    @staticmethod
    def _max_age(block: UserDashboardBlock, spec: dict | None) -> timedelta:
        override = (TileService._config(block).get("refresh") or {}).get("max_age")
        component_refresh = (spec or {}).get("refresh")
        declared = component_refresh.get("max_age") if isinstance(component_refresh, dict) else None
        return parse_duration(override) or parse_duration(declared) or DEFAULT_MAX_AGE

    @staticmethod
    def _is_stale(
        row: UserDashboardBlockResult, block: UserDashboardBlock, spec: dict | None, current_hash: str | None
    ) -> bool:
        if row.result is None or row.fetched_at is None:
            return True
        if current_hash is not None and row.params_hash != current_hash:
            return True
        return datetime.now(UTC) - _aware(row.fetched_at) > TileService._max_age(block, spec)  # type: ignore[operator]

    @staticmethod
    def _view(
        session: Session,
        block: UserDashboardBlock,
        user: User,
        agent: Agent,
        entry: dict | None,
        *,
        skipped: str | None = None,
    ) -> dict:
        """The tile data response: the cache plus what the page needs to draw it."""
        row = TileService._result_row(session, block)
        spec = ComponentService.spec(entry) if entry is not None else None
        stored = TileService._config(block).get("params") or {}
        if spec is not None:
            resolved = component_params.resolve(spec.get("params"), stored, user.timezone)
            current_hash: str | None = component_params.params_hash(resolved)
            missing = ComponentService.missing_credentials(session, agent, spec)
        else:
            resolved, current_hash, missing = dict(stored), None, []
        data: Any = None
        notice: str | None = None
        if row.result is not None:
            try:
                data = json.loads(row.result)
            except ValueError:
                data = None
            if isinstance(data, dict) and isinstance(data.get("notice"), str):
                notice = data["notice"].splitlines()[0][:500] if data["notice"] else None
        return {
            "data": data,
            "stale": TileService._is_stale(row, block, spec, current_hash),
            "fetched_at": row.fetched_at,
            "mark": row.mark,
            "mark_at": row.mark_at,
            "notice": notice,
            "params_resolved": resolved,
            "missing_credentials": missing,
            "skipped": skipped,
        }

    # ── Recompute ─────────────────────────────────────────────────────

    @staticmethod
    async def _compute_deduped(
        block: UserDashboardBlock,
        environment: AgentEnvironment,
        adapter: EnvironmentAdapter,
        entry: dict,
        params: dict,
        params_hash: str,
    ) -> tuple[bool, Any]:
        """``(ok, value_or_sentence)``; concurrent identical recomputes share one run."""
        key = (block.id, params_hash)
        pending = _inflight.get(key)
        if pending is not None:
            return await asyncio.shield(pending)
        future: asyncio.Future[tuple[bool, Any]] = asyncio.get_running_loop().create_future()
        _inflight[key] = future
        try:
            try:
                value = await ComponentService.compute_source(environment, adapter, entry, params)
                outcome: tuple[bool, Any] = (True, value)
            except DashboardComponentError as e:
                if e.code == "adapter_unsupported":
                    raise
                outcome = (False, e.message)
            future.set_result(outcome)
            return outcome
        except BaseException as e:
            if not future.done():
                future.set_exception(e)
                future.exception()  # mark retrieved: waiters re-raise it themselves
            raise
        finally:
            _inflight.pop(key, None)

    @staticmethod
    def _store(
        session: Session,
        block: UserDashboardBlock,
        ok: bool,
        value: Any,
        params_hash: str,
        consumed_log_id: uuid.UUID | None = None,
    ) -> None:
        row = TileService._result_row(session, block)
        now = datetime.now(UTC)
        if ok:
            text = json.dumps(value, ensure_ascii=False)
            if len(text.encode("utf-8")) > MAX_RESULT_BYTES:
                ok, value = False, "The result is larger than 5 MB."
            else:
                row.result = text
                row.fetched_at = now
                row.params_hash = params_hash
                row.mark = None
                row.mark_at = None
        if not ok:
            row.mark = sentence(str(value))
            row.mark_at = now
        if consumed_log_id is not None:
            row.consumed_schedule_log_id = consumed_log_id
        session.add(row)
        session.commit()

    @staticmethod
    async def _recompute(
        session: Session,
        block: UserDashboardBlock,
        user: User,
        agent: Agent,
        environment: AgentEnvironment,
        adapter: EnvironmentAdapter,
        entry: dict,
        *,
        consumed_log_id: uuid.UUID | None = None,
    ) -> None:
        spec = ComponentService.spec(entry)
        missing = ComponentService.missing_credentials(session, agent, spec)
        if missing:
            raise ComponentService.needs_credentials_error(missing)
        resolved = component_params.resolve(
            spec.get("params"), TileService._config(block).get("params") or {}, user.timezone
        )
        params_hash = component_params.params_hash(resolved)
        ok, value = await TileService._compute_deduped(
            block, environment, adapter, entry, resolved, params_hash
        )
        TileService._store(session, block, ok, value, params_hash, consumed_log_id)

    # ── Schedule-fed tiles ────────────────────────────────────────────

    @staticmethod
    def _unconsumed_schedule_log(
        session: Session, block: UserDashboardBlock, spec: dict
    ) -> uuid.UUID | None:
        """The newest settled run of ``refresh.schedule`` the tile has not recomputed for."""
        refresh = spec.get("refresh") if isinstance(spec.get("refresh"), dict) else {}
        schedule_name = refresh.get("schedule")
        if not isinstance(schedule_name, str) or not schedule_name:
            return None
        schedule = session.exec(
            select(AgentSchedule).where(
                AgentSchedule.agent_id == block.agent_id, AgentSchedule.name == schedule_name
            )
        ).first()
        if schedule is None:
            return None
        logs = session.exec(
            select(AgentScheduleLog)
            .where(
                AgentScheduleLog.schedule_id == schedule.id,
                col(AgentScheduleLog.status).in_(_SETTLED_LOG_STATUSES),
            )
            .order_by(col(AgentScheduleLog.executed_at).desc())
            .limit(5)
        ).all()
        row = TileService._result_row(session, block)
        fetched_at = _aware(row.fetched_at)
        for log in logs:
            if log.session_id is not None:
                chat = session.get(ChatSession, log.session_id)
                if chat is not None and chat.interaction_status in _BUSY_SESSION_STATES:
                    continue  # its session still runs: not settled yet
            if log.id == row.consumed_schedule_log_id:
                return None
            # Only a run without a session finished when it was logged; a
            # session-triggered run settles later, so a recompute during its
            # session says nothing about its output: rely on the consumed id.
            if (
                log.session_id is None
                and fetched_at is not None
                and _aware(log.executed_at) <= fetched_at
            ):
                return None  # the cache was computed after this run already
            return log.id
        return None

    # ── Public API ────────────────────────────────────────────────────

    @staticmethod
    async def get_data(session: Session, block: UserDashboardBlock, user: User) -> dict:
        """The cached data; never wakes the environment.

        A schedule-fed tile whose schedule settled a new run since the last
        recompute recomputes once here, when the environment runs.
        """
        agent = TileService._agent(session, block)
        if not ComponentService.is_running(session, agent):
            return TileService._view(session, block, user, agent, None)
        try:
            environment, adapter, entry = await TileService._component(
                session, agent, block, activate=False
            )
        except DashboardComponentError as e:
            if e.code == "not_found":
                raise
            return TileService._view(session, block, user, agent, None)
        log_id = TileService._unconsumed_schedule_log(session, block, ComponentService.spec(entry))
        if log_id is not None:
            try:
                await TileService._recompute(
                    session, block, user, agent, environment, adapter, entry, consumed_log_id=log_id
                )
            except DashboardComponentError as e:
                logger.info("Schedule-fed recompute skipped for tile %s: %s", block.id, e.code)
        return TileService._view(session, block, user, agent, entry)

    @staticmethod
    async def refresh(session: Session, block: UserDashboardBlock, user: User, reason: str) -> dict:
        """Recompute the tile for ``reason`` (see the module docstring)."""
        agent = TileService._agent(session, block)
        if reason == "interval":
            if not ComponentService.is_running(session, agent):
                return TileService._view(session, block, user, agent, None, skipped="env_asleep")
            environment, adapter, entry = await TileService._component(
                session, agent, block, activate=False
            )
            fetched_at = _aware(TileService._result_row(session, block).fetched_at)
            if fetched_at is not None and datetime.now(UTC) - fetched_at < INTERVAL_MIN_AGE:
                return TileService._view(session, block, user, agent, entry)
        else:
            environment, adapter, entry = await TileService._component(
                session, agent, block, activate=True, fresh=reason in _FRESH_LIST_REASONS
            )
            ComponentService.touch_activity(session, environment)
        if reason in ("open", "reshow"):
            view = TileService._view(session, block, user, agent, entry)
            if not view["stale"]:
                return view
        await TileService._recompute(session, block, user, agent, environment, adapter, entry)
        return TileService._view(session, block, user, agent, entry)

    @staticmethod
    async def set_params(
        session: Session, block: UserDashboardBlock, user: User, params: dict
    ) -> dict:
        """Replace the tile's whole param set and recompute.

        A refusal changes nothing (``invalid_input``); so does an exhausted
        budget (``rate_limited``). A set that resolves to what the tile runs
        with already is stored without a run and without counting.
        """
        agent = TileService._agent(session, block)
        environment, adapter, entry = await TileService._component(
            session, agent, block, activate=True, fresh=True
        )
        spec = ComponentService.spec(entry)
        errors = component_params.validate(spec.get("params"), params)
        if errors:
            raise DashboardComponentError("invalid_input", errors[0].message, field=errors[0].field)
        config = TileService._config(block)
        new_params = component_params.clean(params)
        before = component_params.resolve(spec.get("params"), config.get("params") or {}, user.timezone)
        after = component_params.resolve(spec.get("params"), new_params, user.timezone)
        changed = before != after
        if changed:
            retry_after = run_budget.check_and_record(session, run_budget.block_target(block.id))
            if retry_after is not None:
                raise DashboardComponentError(
                    "rate_limited",
                    "Too many runs for this tile; try again shortly.",
                    retry_after_s=retry_after,
                )
        TileService.store_params(session, block, new_params)
        ComponentService.touch_activity(session, environment)
        if changed or TileService._is_stale(
            TileService._result_row(session, block), block, spec, component_params.params_hash(after)
        ):
            try:
                await TileService._recompute(session, block, user, agent, environment, adapter, entry)
            except DashboardComponentError as e:
                if e.code != "needs_credentials":
                    raise
        return TileService._view(session, block, user, agent, entry)

    @staticmethod
    def store_params(session: Session, block: UserDashboardBlock, params: dict) -> None:
        """Persist the tile's stored param set (cleared values already dropped)."""
        config = dict(TileService._config(block))
        config["params"] = params
        block.config = config
        block.updated_at = datetime.now(UTC)
        session.add(block)
        session.commit()
        session.refresh(block)

    @staticmethod
    async def call(
        session: Session, block: UserDashboardBlock, user: User, endpoint: str, params: dict
    ) -> Any:
        """Run one of the component's endpoints for the page; budgeted, never cached.

        The run gets the tile's resolved params overlaid by the call's own;
        a call param the component declares must fit its spec.
        """
        agent = TileService._agent(session, block)
        environment, adapter, entry = await TileService._component(
            session, agent, block, activate=True, fresh=True
        )
        spec = ComponentService.spec(entry)
        declared = spec.get("params") if isinstance(spec.get("params"), dict) else {}
        checked = {k: v for k, v in params.items() if k in declared}
        errors = component_params.validate(declared, checked)
        if errors:
            raise DashboardComponentError("invalid_input", errors[0].message, field=errors[0].field)
        ComponentService.require_endpoint(entry, endpoint)
        missing = ComponentService.missing_credentials(session, agent, spec)
        if missing:
            raise ComponentService.needs_credentials_error(missing)
        retry_after = run_budget.check_and_record(session, run_budget.block_target(block.id))
        if retry_after is not None:
            raise DashboardComponentError(
                "rate_limited", "Too many runs for this tile; try again shortly.", retry_after_s=retry_after
            )
        ComponentService.touch_activity(session, environment)
        stored = TileService._config(block).get("params") or {}
        run_params = component_params.resolve(
            declared, component_params.merge(stored, checked), user.timezone
        )
        # Keys the component does not declare are the page's own call arguments.
        run_params.update({k: v for k, v in params.items() if k not in declared})
        return await ComponentService.run_endpoint(adapter, entry, endpoint, run_params)

    @staticmethod
    async def frame_entry(session: Session, block: UserDashboardBlock) -> str:
        """The tile's entry page, after waking the environment (``env_unavailable`` until it runs)."""
        agent = TileService._agent(session, block)
        environment, _, entry = await TileService._component(session, agent, block, activate=True)
        ComponentService.touch_activity(session, environment)
        return ComponentService.entry_page(entry)

    @staticmethod
    def set_page_state(
        session: Session,
        block: UserDashboardBlock,
        page_doc: str | None,
        highlight_ready: bool,
        seq: int | None = None,
        if_page_doc: str | None = None,
    ) -> None:
        run_budget.set_page_state(
            session, run_budget.block_target(block.id), page_doc, highlight_ready, seq, if_page_doc
        )
