from uuid import UUID
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from sqlmodel import Session, select
from sqlalchemy.orm import selectinload

from app.models.users.user_dashboard import (
    UserDashboard,
    UserDashboardBlock,
    UserDashboardCreate,
    UserDashboardUpdate,
    UserDashboardBlockCreate,
    UserDashboardBlockUpdate,
    BlockLayoutUpdate,
    UserDashboardBlockPromptAction,
    UserDashboardBlockPromptActionCreate,
    UserDashboardBlockPromptActionUpdate,
)
from app.models.agents.agent import Agent
from app.models.environments.environment import AgentEnvironment
from app.services.environments.environment_service import EnvironmentService
from app.services.environments.adapters.base import LocalFilesAccessInterface

ALLOWED_VIEW_TYPES = {"webapp", "latest_session", "latest_tasks", "agent_env_file"}
MAX_DASHBOARDS_PER_USER = 10
MAX_BLOCKS_PER_DASHBOARD = 20


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ── Exception hierarchy ──────────────────────────────────────────────────────


class UserDashboardError(Exception):
    """Base exception for user dashboard service errors."""

    def __init__(self, message: str, status_code: int = 400):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class DashboardNotFoundError(UserDashboardError):
    def __init__(self, message: str = "Dashboard not found"):
        super().__init__(message, status_code=404)


class DashboardPermissionError(UserDashboardError):
    def __init__(self, message: str = "Not enough permissions"):
        super().__init__(message, status_code=403)


class BlockNotFoundError(UserDashboardError):
    def __init__(self, message: str = "Block not found"):
        super().__init__(message, status_code=404)


class PromptActionNotFoundError(UserDashboardError):
    def __init__(self, message: str = "Prompt action not found"):
        super().__init__(message, status_code=404)


class DashboardLimitExceededError(UserDashboardError):
    def __init__(self) -> None:
        super().__init__(
            f"Maximum of {MAX_DASHBOARDS_PER_USER} dashboards allowed per user",
            status_code=409,
        )


class BlockLimitExceededError(UserDashboardError):
    def __init__(self) -> None:
        super().__init__(
            f"Maximum of {MAX_BLOCKS_PER_DASHBOARD} blocks allowed per dashboard",
            status_code=409,
        )


class InvalidViewTypeError(UserDashboardError):
    def __init__(self, view_type: str):
        super().__init__(
            f"Invalid view_type '{view_type}'. Must be one of: {', '.join(sorted(ALLOWED_VIEW_TYPES))}",
            status_code=422,
        )


class AgentAccessError(UserDashboardError):
    def __init__(self, message: str = "Agent not found or not accessible"):
        super().__init__(message, status_code=400)


class EnvironmentNotFoundError(UserDashboardError):
    def __init__(self, message: str = "Active environment not found"):
        super().__init__(message, status_code=404)


class EnvironmentNotReadyError(UserDashboardError):
    def __init__(self, status: str):
        super().__init__(
            f"Environment must be running to access this file (current status: {status})",
            status_code=400,
        )


class InvalidPathError(UserDashboardError):
    def __init__(self, message: str = "Invalid file path"):
        super().__init__(message, status_code=400)


class FileNotFoundError_(UserDashboardError):
    def __init__(self, message: str = "File not found"):
        super().__init__(message, status_code=404)


# ── Service ──────────────────────────────────────────────────────────────────


class UserDashboardService:

    # ── Private helpers ──────────────────────────────────────────────────

    @staticmethod
    def _get_block(
        session: Session, dashboard_id: UUID, block_id: UUID
    ) -> UserDashboardBlock:
        """Fetch a block ensuring it belongs to the given dashboard."""
        block = session.exec(
            select(UserDashboardBlock).where(
                UserDashboardBlock.id == block_id,
                UserDashboardBlock.dashboard_id == dashboard_id,
            )
        ).first()
        if not block:
            raise BlockNotFoundError()
        return block

    @staticmethod
    def _get_prompt_action(
        session: Session, block_id: UUID, action_id: UUID
    ) -> UserDashboardBlockPromptAction:
        """Fetch a prompt action ensuring it belongs to the given block."""
        action = session.exec(
            select(UserDashboardBlockPromptAction).where(
                UserDashboardBlockPromptAction.id == action_id,
                UserDashboardBlockPromptAction.block_id == block_id,
            )
        ).first()
        if not action:
            raise PromptActionNotFoundError()
        return action

    # ── Dashboard CRUD ───────────────────────────────────────────────────

    @staticmethod
    def list_dashboards(session: Session, owner_id: UUID) -> list[UserDashboard]:
        """List all dashboards for a user, ordered by sort_order, with blocks and prompt_actions eagerly loaded."""
        statement = (
            select(UserDashboard)
            .where(UserDashboard.owner_id == owner_id)
            .order_by(UserDashboard.sort_order)
            .options(
                selectinload(UserDashboard.blocks).selectinload(UserDashboardBlock.prompt_actions)  # type: ignore[attr-defined]
            )
        )
        return list(session.exec(statement).all())

    @staticmethod
    def create_dashboard(
        session: Session, owner_id: UUID, data: UserDashboardCreate
    ) -> UserDashboard:
        """Create a new dashboard for a user."""
        existing = session.exec(
            select(UserDashboard).where(UserDashboard.owner_id == owner_id)
        ).all()
        if len(existing) >= MAX_DASHBOARDS_PER_USER:
            raise DashboardLimitExceededError()

        sort_order = max((d.sort_order for d in existing), default=-1) + 1
        dashboard = UserDashboard(
            name=data.name,
            description=data.description,
            owner_id=owner_id,
            sort_order=sort_order,
        )
        session.add(dashboard)
        session.commit()
        session.refresh(dashboard)
        # Ensure blocks attribute is loaded (empty list for new dashboard)
        _ = dashboard.blocks
        return dashboard

    @staticmethod
    def get_dashboard(
        session: Session, dashboard_id: UUID, owner_id: UUID
    ) -> UserDashboard:
        """Get a dashboard by ID with blocks and prompt_actions eagerly loaded."""
        statement = (
            select(UserDashboard)
            .where(UserDashboard.id == dashboard_id)
            .options(
                selectinload(UserDashboard.blocks).selectinload(UserDashboardBlock.prompt_actions)  # type: ignore[attr-defined]
            )
        )
        dashboard = session.exec(statement).first()
        if not dashboard:
            raise DashboardNotFoundError()
        if dashboard.owner_id != owner_id:
            raise DashboardPermissionError()
        return dashboard

    @staticmethod
    def update_dashboard(
        session: Session,
        dashboard_id: UUID,
        owner_id: UUID,
        data: UserDashboardUpdate,
    ) -> UserDashboard:
        """Update dashboard metadata."""
        dashboard = UserDashboardService.get_dashboard(session, dashboard_id, owner_id)
        update_dict = data.model_dump(exclude_unset=True)
        dashboard.sqlmodel_update(update_dict)
        dashboard.updated_at = _utc_now()
        session.add(dashboard)
        session.commit()
        session.refresh(dashboard)
        _ = dashboard.blocks
        return dashboard

    @staticmethod
    def delete_dashboard(
        session: Session, dashboard_id: UUID, owner_id: UUID
    ) -> bool:
        """Delete a dashboard."""
        dashboard = UserDashboardService.get_dashboard(session, dashboard_id, owner_id)
        session.delete(dashboard)
        session.commit()
        return True

    # ── Block CRUD ───────────────────────────────────────────────────────

    @staticmethod
    def add_block(
        session: Session,
        dashboard_id: UUID,
        owner_id: UUID,
        data: UserDashboardBlockCreate,
    ) -> UserDashboardBlock:
        """Add a block to a dashboard. Validates limits, agent access, and view type."""
        dashboard = UserDashboardService.get_dashboard(session, dashboard_id, owner_id)

        if len(dashboard.blocks) >= MAX_BLOCKS_PER_DASHBOARD:
            raise BlockLimitExceededError()

        if data.view_type not in ALLOWED_VIEW_TYPES:
            raise InvalidViewTypeError(data.view_type)

        agent = session.get(Agent, data.agent_id)
        if not agent or agent.owner_id != owner_id:
            raise AgentAccessError()

        if data.view_type == "webapp" and not agent.webapp_enabled:
            raise AgentAccessError(
                "Web App is not enabled for this agent. Enable it in agent settings first."
            )

        if data.view_type == "agent_env_file":
            if not agent.active_environment_id:
                raise AgentAccessError(
                    "Agent has no active environment. Create an environment first."
                )

        block = UserDashboardBlock(
            dashboard_id=dashboard_id,
            agent_id=data.agent_id,
            view_type=data.view_type,
            title=data.title,
            show_border=data.show_border,
            show_header=data.show_header,
            grid_x=data.grid_x,
            grid_y=data.grid_y,
            grid_w=data.grid_w,
            grid_h=data.grid_h,
            config=data.config,
        )
        session.add(block)
        session.commit()
        session.refresh(block)
        return block

    @staticmethod
    def update_block(
        session: Session,
        dashboard_id: UUID,
        block_id: UUID,
        owner_id: UUID,
        data: UserDashboardBlockUpdate,
    ) -> UserDashboardBlock:
        """Update block configuration."""
        UserDashboardService.get_dashboard(session, dashboard_id, owner_id)
        block = UserDashboardService._get_block(session, dashboard_id, block_id)

        update_dict = data.model_dump(exclude_unset=True)
        block.sqlmodel_update(update_dict)
        block.updated_at = _utc_now()
        session.add(block)
        session.commit()
        session.refresh(block)
        return block

    @staticmethod
    def delete_block(
        session: Session,
        dashboard_id: UUID,
        block_id: UUID,
        owner_id: UUID,
    ) -> bool:
        """Delete a block from a dashboard."""
        UserDashboardService.get_dashboard(session, dashboard_id, owner_id)
        block = UserDashboardService._get_block(session, dashboard_id, block_id)

        session.delete(block)
        session.commit()
        return True

    @staticmethod
    def update_block_layout(
        session: Session,
        dashboard_id: UUID,
        owner_id: UUID,
        layouts: list[BlockLayoutUpdate],
    ) -> list[UserDashboardBlock]:
        """Bulk update block grid positions in a single transaction."""
        UserDashboardService.get_dashboard(session, dashboard_id, owner_id)

        updated_blocks: list[UserDashboardBlock] = []
        for layout in layouts:
            block = session.exec(
                select(UserDashboardBlock).where(
                    UserDashboardBlock.id == layout.block_id,
                    UserDashboardBlock.dashboard_id == dashboard_id,
                )
            ).first()
            if not block:
                raise BlockNotFoundError(
                    f"Block {layout.block_id} not found in this dashboard"
                )
            block.grid_x = layout.grid_x
            block.grid_y = layout.grid_y
            block.grid_w = layout.grid_w
            block.grid_h = layout.grid_h
            block.updated_at = _utc_now()
            session.add(block)
            updated_blocks.append(block)

        session.commit()
        for block in updated_blocks:
            session.refresh(block)
        return updated_blocks

    # ── Prompt Action CRUD ───────────────────────────────────────────────

    @staticmethod
    def list_prompt_actions(
        session: Session,
        dashboard_id: UUID,
        block_id: UUID,
        owner_id: UUID,
    ) -> list[UserDashboardBlockPromptAction]:
        """List prompt actions for a block, ordered by sort_order."""
        UserDashboardService.get_dashboard(session, dashboard_id, owner_id)
        UserDashboardService._get_block(session, dashboard_id, block_id)
        actions = session.exec(
            select(UserDashboardBlockPromptAction)
            .where(UserDashboardBlockPromptAction.block_id == block_id)
            .order_by(UserDashboardBlockPromptAction.sort_order)  # type: ignore[arg-type]
        ).all()
        return list(actions)

    @staticmethod
    def create_prompt_action(
        session: Session,
        dashboard_id: UUID,
        block_id: UUID,
        owner_id: UUID,
        data: UserDashboardBlockPromptActionCreate,
    ) -> UserDashboardBlockPromptAction:
        """Create a prompt action on a block."""
        UserDashboardService.get_dashboard(session, dashboard_id, owner_id)
        UserDashboardService._get_block(session, dashboard_id, block_id)
        action = UserDashboardBlockPromptAction(
            block_id=block_id,
            prompt_text=data.prompt_text,
            label=data.label,
            sort_order=data.sort_order,
        )
        session.add(action)
        session.commit()
        session.refresh(action)
        return action

    @staticmethod
    def update_prompt_action(
        session: Session,
        dashboard_id: UUID,
        block_id: UUID,
        action_id: UUID,
        owner_id: UUID,
        data: UserDashboardBlockPromptActionUpdate,
    ) -> UserDashboardBlockPromptAction:
        """Update a prompt action."""
        UserDashboardService.get_dashboard(session, dashboard_id, owner_id)
        UserDashboardService._get_block(session, dashboard_id, block_id)
        action = UserDashboardService._get_prompt_action(session, block_id, action_id)
        update_dict = data.model_dump(exclude_unset=True)
        action.sqlmodel_update(update_dict)
        action.updated_at = _utc_now()
        session.add(action)
        session.commit()
        session.refresh(action)
        return action

    @staticmethod
    def delete_prompt_action(
        session: Session,
        dashboard_id: UUID,
        block_id: UUID,
        action_id: UUID,
        owner_id: UUID,
    ) -> bool:
        """Delete a prompt action."""
        UserDashboardService.get_dashboard(session, dashboard_id, owner_id)
        UserDashboardService._get_block(session, dashboard_id, block_id)
        action = UserDashboardService._get_prompt_action(session, block_id, action_id)
        session.delete(action)
        session.commit()
        return True

    # ── Agent Env File methods ───────────────────────────────────────────

    @staticmethod
    def resolve_block_agent_env(
        session: Session,
        dashboard_id: UUID,
        block_id: UUID,
        owner_id: UUID,
    ) -> tuple[UserDashboardBlock, Agent, AgentEnvironment]:
        """
        Verify ownership chain dashboard -> block -> agent -> active env.
        Returns (block, agent, environment).
        """
        UserDashboardService.get_dashboard(session, dashboard_id, owner_id)
        block = UserDashboardService._get_block(session, dashboard_id, block_id)

        if block.view_type != "agent_env_file":
            raise UserDashboardError("Block is not of type agent_env_file")

        agent = session.get(Agent, block.agent_id)
        if not agent or agent.owner_id != owner_id:
            raise DashboardPermissionError()

        if not agent.active_environment_id:
            raise AgentAccessError("Agent has no active environment")

        environment = session.get(AgentEnvironment, agent.active_environment_id)
        if not environment:
            raise EnvironmentNotFoundError()

        return block, agent, environment

    @staticmethod
    def list_agent_env_files(
        session: Session,
        agent_id: UUID,
        owner_id: UUID,
        subfolder: str = "files",
    ) -> list[str]:
        """
        List files available in a workspace subfolder of the agent's active environment.

        Works with just an agent_id (no block required). Used by the Add Block form
        to let users pick a file before the block is created.
        """
        if ".." in subfolder or subfolder.startswith("/"):
            raise InvalidPathError("Invalid subfolder path")

        agent = session.get(Agent, agent_id)
        if not agent or agent.owner_id != owner_id:
            raise AgentAccessError()

        if not agent.active_environment_id:
            raise AgentAccessError("Agent has no active environment")

        environment = session.get(AgentEnvironment, agent.active_environment_id)
        if not environment:
            raise EnvironmentNotFoundError()

        lifecycle_manager = EnvironmentService.get_lifecycle_manager()
        adapter = lifecycle_manager.get_adapter(environment)

        if isinstance(adapter, LocalFilesAccessInterface):
            return adapter.list_local_workspace_files(subfolder)

        return []

    @staticmethod
    def list_env_files(
        session: Session,
        dashboard_id: UUID,
        block_id: UUID,
        owner_id: UUID,
        subfolder: str = "files",
    ) -> list[str]:
        """
        List files available in a workspace subfolder of the agent's environment.

        Uses local filesystem access when the adapter supports it.
        Returns empty list if adapter does not support local file listing.
        """
        if ".." in subfolder or subfolder.startswith("/"):
            raise InvalidPathError("Invalid subfolder path")

        _block, _agent, environment = UserDashboardService.resolve_block_agent_env(
            session, dashboard_id, block_id, owner_id
        )

        lifecycle_manager = EnvironmentService.get_lifecycle_manager()
        adapter = lifecycle_manager.get_adapter(environment)

        if isinstance(adapter, LocalFilesAccessInterface):
            return adapter.list_local_workspace_files(subfolder)

        return []

    @staticmethod
    def get_env_file_local_path(
        session: Session,
        dashboard_id: UUID,
        block_id: UUID,
        owner_id: UUID,
        path: str,
    ) -> Path | None:
        """
        Get local filesystem path for a workspace file, if the adapter supports it.

        Returns the Path if available locally, or None to indicate the caller
        should fall back to remote streaming.
        """
        if not path or ".." in path or path.startswith("/"):
            raise InvalidPathError()

        _block, _agent, environment = UserDashboardService.resolve_block_agent_env(
            session, dashboard_id, block_id, owner_id
        )

        lifecycle_manager = EnvironmentService.get_lifecycle_manager()
        adapter = lifecycle_manager.get_adapter(environment)

        if isinstance(adapter, LocalFilesAccessInterface):
            file_path = adapter.get_local_workspace_file_path(path)
            if file_path is None:
                raise FileNotFoundError_()
            return file_path

        # Not a local adapter — caller should use remote streaming
        if environment.status != "running":
            raise EnvironmentNotReadyError(environment.status)

        return None

    @staticmethod
    def get_env_file_adapter(
        session: Session,
        dashboard_id: UUID,
        block_id: UUID,
        owner_id: UUID,
        path: str,
    ):
        """
        Get the adapter for remote file streaming.

        Only called when get_env_file_local_path returns None (non-local adapter).
        """
        if not path or ".." in path or path.startswith("/"):
            raise InvalidPathError()

        _block, _agent, environment = UserDashboardService.resolve_block_agent_env(
            session, dashboard_id, block_id, owner_id
        )

        lifecycle_manager = EnvironmentService.get_lifecycle_manager()
        return lifecycle_manager.get_adapter(environment)
