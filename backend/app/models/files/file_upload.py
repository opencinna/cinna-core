import uuid
from datetime import datetime, UTC
from sqlmodel import SQLModel, Field, Column
from sqlalchemy import JSON, Index


class FileUpload(SQLModel, table=True):
    """Database table for file uploads"""

    __tablename__ = "file_uploads"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="user.id", ondelete="CASCADE")

    # File metadata
    filename: str = Field(max_length=255)
    file_path: str = Field(
        max_length=512
    )  # Relative path: uploads/{user_id}/{file_id}/{filename}
    file_size: int  # Bytes
    mime_type: str = Field(max_length=127)

    # Lifecycle tracking
    status: str = Field(
        default="temporary", max_length=31
    )  # "temporary" | "attached" | "marked_for_deletion"

    # Timestamps
    uploaded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    attached_at: datetime | None = None
    marked_for_deletion_at: datetime | None = None

    # Optional metadata
    file_metadata: dict = Field(default_factory=dict, sa_column=Column(JSON))


class MessageFile(SQLModel, table=True):
    """Junction table linking messages to files"""

    __tablename__ = "message_files"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    message_id: uuid.UUID = Field(foreign_key="message.id", ondelete="CASCADE")
    file_id: uuid.UUID = Field(foreign_key="file_uploads.id", ondelete="CASCADE")

    # Agent-env path (where file was stored in container)
    agent_env_path: str | None = Field(default=None, max_length=512)

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class InputTaskFile(SQLModel, table=True):
    """Junction table linking input tasks to files"""

    __tablename__ = "input_task_files"
    __table_args__ = (
        # Indexes for efficient querying
        Index("ix_input_task_files_task_id", "task_id"),
        Index("ix_input_task_files_file_id", "file_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    task_id: uuid.UUID = Field(foreign_key="input_task.id", ondelete="CASCADE")
    file_id: uuid.UUID = Field(foreign_key="file_uploads.id", ondelete="CASCADE")

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# Pydantic schemas (keep existing ones)
class FileUploadPublic(SQLModel):
    """Response schema for file upload"""

    id: uuid.UUID
    filename: str
    file_size: int
    mime_type: str
    status: str
    uploaded_at: datetime


class FileUploadsPublic(SQLModel):
    data: list[FileUploadPublic]
    count: int
