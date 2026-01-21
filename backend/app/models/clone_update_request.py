import uuid as uuid_module
from datetime import datetime
from typing import TYPE_CHECKING
from sqlmodel import Field, Relationship, SQLModel, Column
from sqlalchemy import JSON

if TYPE_CHECKING:
    from app.models.agent import Agent
    from app.models.user import User


# Update request status constants
class UpdateRequestStatus:
    """Status of a clone update request"""
    PENDING = "pending"
    APPLIED = "applied"
    DISMISSED = "dismissed"


class CloneUpdateRequestBase(SQLModel):
    """Base properties for clone update requests"""
    copy_files_folder: bool = False
    rebuild_environment: bool = False


class CloneUpdateRequest(CloneUpdateRequestBase, table=True):
    """Database model for clone update request records.

    When an owner pushes updates to clones, a record is created for each clone
    with the specific actions to be performed.
    """
    __tablename__ = "clone_update_request"

    id: uuid_module.UUID = Field(default_factory=uuid_module.uuid4, primary_key=True)

    # The clone agent that should receive the update
    clone_agent_id: uuid_module.UUID = Field(foreign_key="agent.id", nullable=False)

    # The parent agent that is pushing the update
    parent_agent_id: uuid_module.UUID = Field(foreign_key="agent.id", nullable=False)

    # The user who pushed the update (owner of parent agent)
    pushed_by_user_id: uuid_module.UUID = Field(foreign_key="user.id", nullable=False)

    # Actions to perform
    copy_files_folder: bool = Field(default=False)
    rebuild_environment: bool = Field(default=False)

    # Status tracking
    status: str = Field(default="pending")  # "pending" | "applied" | "dismissed"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    applied_at: datetime | None = Field(default=None)
    dismissed_at: datetime | None = Field(default=None)

    # Relationships
    clone_agent: "Agent" = Relationship(
        sa_relationship_kwargs={"foreign_keys": "[CloneUpdateRequest.clone_agent_id]"}
    )
    parent_agent: "Agent" = Relationship(
        sa_relationship_kwargs={"foreign_keys": "[CloneUpdateRequest.parent_agent_id]"}
    )
    pushed_by_user: "User" = Relationship(
        sa_relationship_kwargs={"foreign_keys": "[CloneUpdateRequest.pushed_by_user_id]"}
    )


class CloneUpdateRequestPublic(CloneUpdateRequestBase):
    """Public representation of an update request (for API responses)"""
    id: uuid_module.UUID
    clone_agent_id: uuid_module.UUID
    parent_agent_id: uuid_module.UUID
    parent_agent_name: str | None = None  # Resolved from parent_agent
    pushed_by_email: str | None = None  # Resolved from pushed_by_user
    copy_files_folder: bool
    rebuild_environment: bool
    status: str
    created_at: datetime
    applied_at: datetime | None = None
    dismissed_at: datetime | None = None


class CloneUpdateRequestsPublic(SQLModel):
    """List response for clone update requests"""
    data: list[CloneUpdateRequestPublic]
    count: int


class PushUpdateActionsRequest(SQLModel):
    """Request body for pushing updates to clones"""
    copy_files_folder: bool = False
    rebuild_environment: bool = False
