"""
Task Attachment models for the task collaboration system.

Task attachments track files attached to tasks — deliverables, reports,
data exports, images. Origin fields record which agent workspace the file
came from (provenance tracking).
"""
import uuid
from datetime import datetime, UTC
from sqlmodel import Field, SQLModel, Index


class TaskAttachmentBase(SQLModel):
    file_name: str = Field(max_length=500)
    content_type: str | None = Field(default=None, max_length=200)


class TaskAttachment(TaskAttachmentBase, table=True):
    __tablename__ = "task_attachment"
    __table_args__ = (
        Index("ix_task_attachment_task_id", "task_id"),
        Index("ix_task_attachment_comment_id", "comment_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    task_id: uuid.UUID = Field(
        foreign_key="input_task.id", nullable=False, ondelete="CASCADE"
    )
    # Optional link to the comment this attachment belongs to
    comment_id: uuid.UUID | None = Field(
        default=None, foreign_key="task_comment.id", ondelete="SET NULL"
    )
    # Storage path on backend (backend/data/uploads/{user_id}/{attachment_id}/{filename})
    file_path: str = Field(max_length=1000)
    file_size: int | None = None

    # Who uploaded it
    uploaded_by_agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent.id", ondelete="SET NULL"
    )
    uploaded_by_user_id: uuid.UUID | None = Field(
        default=None, foreign_key="user.id", ondelete="SET NULL"
    )

    # Origin tracking: where the file originally came from in an agent workspace
    # source_agent_id: the agent whose workspace the file was generated in
    # source_workspace_path: the original path in that workspace (e.g., /app/workspace/output/report.csv)
    source_agent_id: uuid.UUID | None = Field(
        default=None, foreign_key="agent.id", ondelete="SET NULL"
    )
    source_workspace_path: str | None = Field(default=None, max_length=1000)

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# API response schema
class TaskAttachmentPublic(SQLModel):
    id: uuid.UUID
    task_id: uuid.UUID
    comment_id: uuid.UUID | None
    file_name: str
    file_path: str
    file_size: int | None
    content_type: str | None
    uploaded_by_agent_id: uuid.UUID | None
    uploaded_by_user_id: uuid.UUID | None
    source_agent_id: uuid.UUID | None
    source_workspace_path: str | None
    created_at: datetime
    # Resolved display fields
    uploaded_by_name: str | None = None      # Human-readable uploader name
    source_agent_name: str | None = None     # Name of agent that generated the file
    download_url: str | None = None          # Computed download URL


class TaskAttachmentsPublic(SQLModel):
    data: list[TaskAttachmentPublic]
    count: int
