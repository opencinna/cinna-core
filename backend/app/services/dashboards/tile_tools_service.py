"""The agent's dashboard tile tools: ``dashboards_tiles`` and ``dashboards_update_tile``.

Called by env-core (``AgentEnvContextDep``) with the chat's ``session_id``.
The scope is re-read on every call: the session exists, belongs to the calling
agent and its owner, and its ``dashboard_id`` points to an existing dashboard
of that owner (else ``no_dashboard``). The tiles are that dashboard's
``agent_component`` blocks of the calling agent; any other id is
``not_found``, never "forbidden".

Refusals use the tools schema's codes only (``not_found``, ``invalid_input``,
``rate_limited``, ``no_dashboard``) and are raised as
``DashboardComponentError``; the route answers them with HTTP 200.

A params change goes through the tile's run budget, is stored, and recomputes
in a background task that then emits ``dashboard_tile_updated``. A highlight
is relayed to the owner's open page (``dashboard_tile_highlight``); the page
holds it. A highlight alone never counts against the budget.
"""
import logging
import uuid
from typing import Any

from sqlmodel import Session, col, select

from app.core import db as core_db
from app.models.agents.agent import Agent
from app.models.events.event import EventType
from app.models.sessions.session import Session as ChatSession
from app.models.users.user import User
from app.models.users.user_dashboard import UserDashboard, UserDashboardBlock
from app.services.dashboards import component_params, dashboard_context, run_budget
from app.services.dashboards.component_service import (
    AGENT_COMPONENT_VIEW_TYPE,
    ComponentService,
    DashboardComponentError,
)
from app.services.dashboards.tile_service import TileService
from app.services.events.event_service import event_service
from app.utils import create_task_with_error_logging

logger = logging.getLogger(__name__)

TILE_VIEWS = ("markdown", "table", "list", "metrics", "html")
NATIVE_HIGHLIGHT_VIEWS = ("list", "table")
MAX_TILE_ID_LENGTH = 128
MAX_HIGHLIGHT_IDS = 500
MAX_HIGHLIGHT_ID_LENGTH = 256
MAX_SELECTION_IDS = 500

NO_DASHBOARD_MESSAGE = "This chat has no dashboard: it was not opened from one, or that dashboard was deleted."
NOT_FOUND_MESSAGE = "No tile with this `tile_id` on this chat's dashboard belongs to you."


def _error(code: str, message: str, **kwargs: Any) -> DashboardComponentError:
    return DashboardComponentError(code, message, **kwargs)


class TileToolsService:
    # ── Scope ─────────────────────────────────────────────────────────

    @staticmethod
    def _scope(db: Session, agent: Agent, owner: User, session_id: str) -> tuple[ChatSession, UserDashboard]:
        try:
            chat = db.get(ChatSession, uuid.UUID(str(session_id)))
        except ValueError:
            chat = None
        if (
            chat is None
            or chat.agent_id != agent.id
            or chat.user_id != owner.id
            or chat.dashboard_id is None
        ):
            raise _error("no_dashboard", NO_DASHBOARD_MESSAGE)
        dashboard = db.get(UserDashboard, chat.dashboard_id)
        if dashboard is None or dashboard.owner_id != owner.id:
            raise _error("no_dashboard", NO_DASHBOARD_MESSAGE)
        return chat, dashboard

    @staticmethod
    def _agent_blocks(db: Session, dashboard: UserDashboard, agent: Agent) -> list[UserDashboardBlock]:
        return list(
            db.exec(
                select(UserDashboardBlock)
                .where(
                    UserDashboardBlock.dashboard_id == dashboard.id,
                    UserDashboardBlock.agent_id == agent.id,
                    UserDashboardBlock.view_type == AGENT_COMPONENT_VIEW_TYPE,
                )
                .order_by(col(UserDashboardBlock.grid_y), col(UserDashboardBlock.grid_x))
            ).all()
        )

    @staticmethod
    def _block(db: Session, dashboard: UserDashboard, agent: Agent, tile_id: Any) -> UserDashboardBlock:
        if not isinstance(tile_id, str) or not tile_id or len(tile_id) > MAX_TILE_ID_LENGTH:
            raise _error("not_found", NOT_FOUND_MESSAGE)
        try:
            block_id = uuid.UUID(tile_id)
        except ValueError:
            raise _error("not_found", NOT_FOUND_MESSAGE)
        block = db.get(UserDashboardBlock, block_id)
        if (
            block is None
            or block.dashboard_id != dashboard.id
            or block.agent_id != agent.id
            or block.view_type != AGENT_COMPONENT_VIEW_TYPE
        ):
            raise _error("not_found", NOT_FOUND_MESSAGE)
        return block

    # ── Components ────────────────────────────────────────────────────

    @staticmethod
    async def _components(db: Session, agent: Agent) -> list[dict] | None:
        """The agent's components without waking it (the caller runs in it); None when unreadable."""
        try:
            return await ComponentService.list_components(db, agent, activate=False, fresh=True)
        except DashboardComponentError as e:
            logger.info("Dashboard tile tools: components of agent %s unreadable (%s)", agent.id, e.code)
            return None

    @staticmethod
    def _entry(components: list[dict] | None, block: UserDashboardBlock) -> dict | None:
        """The block's component when it exists and has a tile view."""
        if components is None:
            return None
        config = block.config if isinstance(block.config, dict) else {}
        entry = ComponentService.find(components, str(config.get("component") or ""), config.get("skill"))
        if entry is None or ComponentService.spec(entry).get("view") not in TILE_VIEWS:
            return None
        return entry

    @staticmethod
    def _can_highlight(db: Session, block: UserDashboardBlock, entry: dict) -> bool:
        if ComponentService.has_errors(entry):
            return False
        view = ComponentService.spec(entry).get("view")
        if view in NATIVE_HIGHLIGHT_VIEWS:
            return True
        if view == "html":
            page_doc, ready = run_budget.get_page_state(db, run_budget.block_target(block.id))
            return page_doc is not None and ready
        return False

    @staticmethod
    def _focused_block_id(focused: dict | None, blocks: list[UserDashboardBlock]) -> uuid.UUID | None:
        """The tile the last context focused: its ``tile_id``, else a unique component match."""
        if not focused:
            return None
        for key in ("tile_id", "block_id"):
            value = focused.get(key)
            if isinstance(value, str):
                for block in blocks:
                    if str(block.id) == value:
                        return block.id
                return None
        component = focused.get("component")
        if not isinstance(component, str):
            return None
        matches = [
            b for b in blocks
            if isinstance(b.config, dict) and b.config.get("component") == component
            and (b.config.get("skill") or None) == (focused.get("skill") or None)
        ]
        return matches[0].id if len(matches) == 1 else None

    # ── dashboards_tiles ──────────────────────────────────────────────

    @staticmethod
    async def tiles(db: Session, agent: Agent, owner: User, session_id: str) -> dict:
        chat, dashboard = TileToolsService._scope(db, agent, owner, session_id)
        blocks = TileToolsService._agent_blocks(db, dashboard, agent)
        components = await TileToolsService._components(db, agent) if blocks else []
        focused = dashboard_context.focused(dashboard_context.last_sent(db, chat.id))
        focused_id = TileToolsService._focused_block_id(focused, blocks)

        tiles: list[dict] = []
        for block in blocks:
            entry = TileToolsService._entry(components, block)
            if entry is None:
                continue  # component gone, unreadable or without a view
            spec = ComponentService.spec(entry)
            config = block.config if isinstance(block.config, dict) else {}
            spec_params = spec.get("params") if isinstance(spec.get("params"), dict) else {}
            title = block.title or spec.get("title") or str(entry.get("name") or "")
            tile: dict[str, Any] = {
                "tile_id": str(block.id),
                "component": str(entry.get("name") or ""),
                "title": str(title),
                "view": spec["view"],
                "params": component_params.resolve(spec_params, config.get("params") or {}, owner.timezone),
                "param_specs": spec_params,
                "can_highlight": TileToolsService._can_highlight(db, block, entry),
            }
            if entry.get("skill"):
                tile["skill"] = entry["skill"]
            if block.id == focused_id and focused is not None:
                if isinstance(focused.get("summary"), str):
                    tile["summary"] = focused["summary"]
                selection = focused.get("selection")
                if isinstance(selection, list):
                    tile["selection"] = [str(i) for i in selection if isinstance(i, (str, int))][:MAX_SELECTION_IDS]
            tiles.append(tile)
        return {"dashboard": {"id": str(dashboard.id), "name": dashboard.name}, "tiles": tiles}

    # ── dashboards_update_tile ────────────────────────────────────────

    @staticmethod
    def _check_input(params: Any, highlight: Any) -> None:
        if params is None and highlight is None:
            raise _error("invalid_input", "Give `params`, `highlight` or both.")
        if params is not None and not isinstance(params, dict):
            raise _error("invalid_input", "`params` must be an object of name → value.", field="params")
        if highlight is not None:
            if not isinstance(highlight, list) or len(highlight) > MAX_HIGHLIGHT_IDS:
                raise _error(
                    "invalid_input", f"`highlight` is a list of at most {MAX_HIGHLIGHT_IDS} ids.", field="highlight"
                )
            if any(
                not isinstance(i, str) or not i or len(i) > MAX_HIGHLIGHT_ID_LENGTH for i in highlight
            ):
                raise _error(
                    "invalid_input",
                    f"Each `highlight` id is a string of 1 to {MAX_HIGHLIGHT_ID_LENGTH} characters.",
                    field="highlight",
                )

    @staticmethod
    async def _emit_highlight(owner: User, dashboard: UserDashboard, block: UserDashboardBlock, ids: list[str]) -> None:
        await event_service.emit_event(
            event_type=EventType.DASHBOARD_TILE_HIGHLIGHT,
            model_id=block.id,
            meta={"dashboard_id": str(dashboard.id), "block_id": str(block.id), "ids": ids},
            user_id=owner.id,
        )

    @staticmethod
    async def update_tile(
        db: Session,
        agent: Agent,
        owner: User,
        session_id: str,
        tile_id: Any,
        params: Any,
        highlight: Any,
    ) -> dict:
        _, dashboard = TileToolsService._scope(db, agent, owner, session_id)
        block = TileToolsService._block(db, dashboard, agent, tile_id)
        try:
            TileToolsService._check_input(params, highlight)
            entry = TileToolsService._entry(await TileToolsService._components(db, agent), block)
            if entry is None:
                raise _error("not_found", NOT_FOUND_MESSAGE)
        except DashboardComponentError:
            if params is not None:
                # A refused params change also drops the agent's highlight.
                await TileToolsService._emit_highlight(owner, dashboard, block, [])
            raise

        spec = ComponentService.spec(entry)
        spec_params = spec.get("params") if isinstance(spec.get("params"), dict) else {}
        config = block.config if isinstance(block.config, dict) else {}
        stored = dict(config.get("params") or {})
        after = component_params.resolve(spec_params, stored, owner.timezone)
        recomputing = False

        if params:
            try:
                after, recomputing = TileToolsService._apply_params(db, block, owner, spec_params, stored, params)
            except DashboardComponentError:
                # A refused or budget-limited change also drops the agent's highlight.
                await TileToolsService._emit_highlight(owner, dashboard, block, [])
                raise
            if recomputing:
                create_task_with_error_logging(
                    TileToolsService._recompute_and_notify(block.id, owner.id, dashboard.id),
                    task_name=f"dashboard_tile_recompute_{block.id}",
                )

        highlighted = False
        if highlight is not None:
            highlighted = TileToolsService._can_highlight(db, block, entry)
            await TileToolsService._emit_highlight(owner, dashboard, block, list(highlight))

        return {"tile_id": str(block.id), "params": after, "recomputing": recomputing, "highlighted": highlighted}

    @staticmethod
    def _apply_params(
        db: Session,
        block: UserDashboardBlock,
        owner: User,
        spec_params: dict,
        stored: dict,
        params: dict,
    ) -> tuple[dict, bool]:
        """Merge ``params`` over the stored set; ``(resolved after, changed)``.

        Echoes (values resolving to what the tile already runs with) are
        dropped first; an unchanged resolved set stores nothing, runs nothing
        and does not count. Otherwise the budget is checked before storing.
        """
        errors = component_params.validate(spec_params, params)
        if errors:
            raise _error("invalid_input", errors[0].message, field=errors[0].field)
        effective = {
            key: value for key, value in params.items()
            if not component_params.is_echo(spec_params, stored, key, value, owner.timezone)
        }
        before = component_params.resolve(spec_params, stored, owner.timezone)
        new_stored = component_params.merge(stored, effective)
        after = component_params.resolve(spec_params, new_stored, owner.timezone)
        if after == before:
            return after, False
        retry_after = run_budget.check_and_record(db, run_budget.block_target(block.id))
        if retry_after is not None:
            raise _error(
                "rate_limited",
                "Too many runs for this tile; try again shortly.",
                retry_after_s=retry_after,
            )
        TileService.store_params(db, block, new_stored)
        return after, True

    @staticmethod
    async def _recompute_and_notify(block_id: uuid.UUID, user_id: uuid.UUID, dashboard_id: uuid.UUID) -> None:
        """Recompute the tile with its new params, then tell the owner's page to refetch."""
        try:
            with core_db.create_session() as db:
                block = db.get(UserDashboardBlock, block_id)
                user = db.get(User, user_id)
                if block is None or user is None:
                    return
                await TileService.refresh(db, block, user, "params")
        except DashboardComponentError as e:
            # The cache keeps its data; the page shows the mark on refetch.
            logger.info("Agent params recompute of tile %s failed: %s", block_id, e.code)
        except Exception:
            logger.exception("Agent params recompute of tile %s raised", block_id)
        # The params are stored either way: the page refetches.
        await event_service.emit_event(
            event_type=EventType.DASHBOARD_TILE_UPDATED,
            model_id=block_id,
            meta={"dashboard_id": str(dashboard_id), "block_id": str(block_id)},
            user_id=user_id,
        )
