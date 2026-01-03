from uuid import UUID
from datetime import datetime
from sqlmodel import Session, select
from app.models import UserWorkspace, UserWorkspaceCreate, UserWorkspaceUpdate


class UserWorkspaceService:
    @staticmethod
    def create_workspace(session: Session, user_id: UUID, data: UserWorkspaceCreate) -> UserWorkspace:
        """Create new user workspace"""
        workspace = UserWorkspace.model_validate(data, update={"user_id": user_id})
        session.add(workspace)
        session.commit()
        session.refresh(workspace)
        return workspace

    @staticmethod
    def get_workspace(session: Session, workspace_id: UUID) -> UserWorkspace | None:
        """Get workspace by ID"""
        return session.get(UserWorkspace, workspace_id)

    @staticmethod
    def get_user_workspaces(session: Session, user_id: UUID, skip: int = 0, limit: int = 100) -> list[UserWorkspace]:
        """Get all workspaces for a user"""
        statement = select(UserWorkspace).where(UserWorkspace.user_id == user_id).offset(skip).limit(limit)
        workspaces = session.exec(statement).all()
        return list(workspaces)

    @staticmethod
    def count_user_workspaces(session: Session, user_id: UUID) -> int:
        """Count workspaces for a user"""
        statement = select(UserWorkspace).where(UserWorkspace.user_id == user_id)
        workspaces = session.exec(statement).all()
        return len(list(workspaces))

    @staticmethod
    def update_workspace(session: Session, workspace_id: UUID, data: UserWorkspaceUpdate) -> UserWorkspace | None:
        """Update workspace"""
        workspace = session.get(UserWorkspace, workspace_id)
        if not workspace:
            return None

        update_dict = data.model_dump(exclude_unset=True)
        workspace.sqlmodel_update(update_dict)
        workspace.updated_at = datetime.utcnow()

        session.add(workspace)
        session.commit()
        session.refresh(workspace)
        return workspace

    @staticmethod
    def delete_workspace(session: Session, workspace_id: UUID) -> bool:
        """Delete workspace"""
        workspace = session.get(UserWorkspace, workspace_id)
        if not workspace:
            return False

        session.delete(workspace)
        session.commit()
        return True
