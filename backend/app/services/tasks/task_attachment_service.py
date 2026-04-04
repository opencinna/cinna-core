"""
Task Attachment Service — manages file attachments on input tasks.

Attachments can come from two sources:
1. User uploads (standard multipart upload via API)
2. Agent workspace files (backend fetches from agent-env HTTP API)

Files are persisted in backend storage so they remain accessible even if the
agent environment is stopped or rebuilt.
"""
import logging
import mimetypes
import os
import uuid as uuid_module
from datetime import datetime, UTC
from pathlib import Path
from uuid import UUID

import httpx
from fastapi import HTTPException, UploadFile
from sqlmodel import Session as DBSession, select

from app.core.config import settings
from app.models.tasks.task_attachment import TaskAttachment, TaskAttachmentPublic
from app.models.tasks.input_task import InputTask
from app.models.agents.agent import Agent
from app.models.environments.environment import AgentEnvironment
from app.models.events.event import EventType
from app.services.events.event_service import event_service
from app.utils import create_task_with_error_logging

logger = logging.getLogger(__name__)

# Storage base path for task attachments
# Uses same base as regular uploads: backend/data/uploads/
TASK_ATTACHMENT_BASE_PATH = Path(settings.UPLOAD_BASE_PATH).parent / "uploads"


class TaskAttachmentService:
    """Service for managing task file attachments."""

    @staticmethod
    def _get_storage_path(owner_id: UUID, attachment_id: UUID, filename: str) -> Path:
        """
        Compute the backend storage path for a task attachment.

        Structure: uploads/{owner_id}/task_attachments/{attachment_id}/{filename}
        """
        base = Path(settings.UPLOAD_BASE_PATH)
        return base / str(owner_id) / "task_attachments" / str(attachment_id) / filename

    @staticmethod
    def _to_public(db_session: DBSession, attachment: TaskAttachment) -> TaskAttachmentPublic:
        """Convert a TaskAttachment DB record to public schema."""
        uploaded_by_name = None
        if attachment.uploaded_by_agent_id:
            agent = db_session.get(Agent, attachment.uploaded_by_agent_id)
            if agent:
                uploaded_by_name = agent.name
        elif attachment.uploaded_by_user_id:
            from app.models.users.user import User
            user = db_session.get(User, attachment.uploaded_by_user_id)
            if user:
                uploaded_by_name = user.full_name or user.email

        source_agent_name = None
        if attachment.source_agent_id:
            agent = db_session.get(Agent, attachment.source_agent_id)
            if agent:
                source_agent_name = agent.name

        download_url = f"/api/v1/tasks/{attachment.task_id}/attachments/{attachment.id}/download"

        return TaskAttachmentPublic(
            id=attachment.id,
            task_id=attachment.task_id,
            comment_id=attachment.comment_id,
            file_name=attachment.file_name,
            file_path=attachment.file_path,
            file_size=attachment.file_size,
            content_type=attachment.content_type,
            uploaded_by_agent_id=attachment.uploaded_by_agent_id,
            uploaded_by_user_id=attachment.uploaded_by_user_id,
            source_agent_id=attachment.source_agent_id,
            source_workspace_path=attachment.source_workspace_path,
            created_at=attachment.created_at,
            uploaded_by_name=uploaded_by_name,
            source_agent_name=source_agent_name,
            download_url=download_url,
        )

    @staticmethod
    async def upload_attachment(
        db_session: DBSession,
        task_id: UUID,
        file: UploadFile,
        uploaded_by_user_id: UUID | None = None,
        comment_id: UUID | None = None,
    ) -> TaskAttachment:
        """
        Store an uploaded file and create a TaskAttachment record.

        Args:
            db_session: Database session
            task_id: Target task UUID
            file: FastAPI UploadFile
            uploaded_by_user_id: User performing the upload
            comment_id: Optional comment to link the attachment to

        Returns:
            Created TaskAttachment
        """
        task = db_session.get(InputTask, task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Task not found")

        content = await file.read()
        file_size = len(content)

        if file_size > settings.upload_max_file_size_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File too large. Max: {settings.UPLOAD_MAX_FILE_SIZE_MB}MB",
            )

        filename = file.filename or "attachment"
        content_type = file.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"

        # Generate attachment ID and storage path
        attachment_id = uuid_module.uuid4()
        storage_path = TaskAttachmentService._get_storage_path(task.owner_id, attachment_id, filename)
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        storage_path.write_bytes(content)

        # Relative path for DB record
        relative_path = str(storage_path.relative_to(Path(settings.UPLOAD_BASE_PATH).parent))

        attachment = TaskAttachment(
            id=attachment_id,
            task_id=task_id,
            comment_id=comment_id,
            file_name=filename,
            file_path=relative_path,
            file_size=file_size,
            content_type=content_type,
            uploaded_by_user_id=uploaded_by_user_id,
        )
        db_session.add(attachment)
        db_session.commit()
        db_session.refresh(attachment)

        TaskAttachmentService._emit_attachment_event(db_session, task, attachment)
        logger.info(f"Attachment {attachment_id} uploaded to task {task_id} by user {uploaded_by_user_id}")
        return attachment

    @staticmethod
    def attach_from_workspace(
        db_session: DBSession,
        task_id: UUID,
        agent_id: UUID,
        file_paths: list[str],
        comment_id: UUID | None = None,
    ) -> list[TaskAttachment]:
        """
        Fetch files from an agent workspace and create TaskAttachment records.

        For each workspace path:
        1. Resolve agent's active environment
        2. Call environment HTTP API to download the file
        3. Store file in backend storage
        4. Create TaskAttachment record with origin tracking

        Args:
            db_session: Database session
            task_id: Target task UUID
            agent_id: Agent whose workspace the files come from
            file_paths: List of paths in the agent workspace (e.g., /app/workspace/output.csv)
            comment_id: Optional comment to link attachments to

        Returns:
            List of created TaskAttachment records (may be fewer if some fetches failed)
        """
        task = db_session.get(InputTask, task_id)
        if not task:
            logger.warning(f"Task {task_id} not found for workspace attach")
            return []

        # Get agent's active environment
        agent = db_session.get(Agent, agent_id)
        if not agent or not agent.active_environment_id:
            logger.warning(f"Agent {agent_id} has no active environment, skipping workspace attach")
            return []

        environment = db_session.get(AgentEnvironment, agent.active_environment_id)
        if not environment:
            logger.warning(f"Environment not found for agent {agent_id}")
            return []

        # Get environment URL and auth
        container_name = environment.config.get("container_name", f"agent-{environment.id}")
        port = environment.config.get("port", 8000)
        base_url = f"http://{container_name}:{port}"
        auth_token = environment.config.get("auth_token")
        headers = {"Authorization": f"Bearer {auth_token}"} if auth_token else {}

        created_attachments = []

        for workspace_path in file_paths:
            try:
                # Normalize path to be relative to workspace root
                # Agent may send: ./reports/file.json, /app/workspace/reports/file.json,
                # or reports/file.json — all should resolve to "reports/file.json"
                rel_path = workspace_path
                if rel_path.startswith("/app/workspace/"):
                    rel_path = rel_path[len("/app/workspace/"):]
                if rel_path.startswith("./"):
                    rel_path = rel_path[2:]
                rel_path = rel_path.lstrip("/")

                # Fetch file from agent environment via workspace download endpoint
                with httpx.Client(timeout=30.0) as client:
                    response = client.get(
                        f"{base_url}/workspace/download/{rel_path}",
                        headers=headers,
                    )
                    response.raise_for_status()

                content = response.content
                filename = os.path.basename(workspace_path)
                content_type = (
                    response.headers.get("content-type")
                    or mimetypes.guess_type(filename)[0]
                    or "application/octet-stream"
                )
                # Strip charset etc. from content-type
                content_type = content_type.split(";")[0].strip()

                # Store file in backend storage
                attachment_id = uuid_module.uuid4()
                storage_path = TaskAttachmentService._get_storage_path(
                    task.owner_id, attachment_id, filename
                )
                storage_path.parent.mkdir(parents=True, exist_ok=True)
                storage_path.write_bytes(content)

                relative_path = str(storage_path.relative_to(Path(settings.UPLOAD_BASE_PATH).parent))

                attachment = TaskAttachment(
                    id=attachment_id,
                    task_id=task_id,
                    comment_id=comment_id,
                    file_name=filename,
                    file_path=relative_path,
                    file_size=len(content),
                    content_type=content_type,
                    uploaded_by_agent_id=agent_id,
                    source_agent_id=agent_id,
                    source_workspace_path=workspace_path,
                )
                db_session.add(attachment)
                db_session.commit()
                db_session.refresh(attachment)

                created_attachments.append(attachment)
                TaskAttachmentService._emit_attachment_event(db_session, task, attachment)
                logger.info(
                    f"Workspace file attached: task={task_id} agent={agent_id} "
                    f"path={workspace_path} → {relative_path}"
                )

            except httpx.HTTPStatusError as e:
                logger.warning(
                    f"Failed to fetch workspace file {workspace_path} from agent {agent_id}: "
                    f"HTTP {e.response.status_code}"
                )
            except Exception as e:
                logger.warning(
                    f"Failed to attach workspace file {workspace_path}: {e}",
                    exc_info=True,
                )

        return created_attachments

    @staticmethod
    def _emit_attachment_event(
        db_session: DBSession,
        task: InputTask,
        attachment: TaskAttachment,
    ) -> None:
        """Emit real-time event for attachment added."""
        create_task_with_error_logging(
            event_service.emit_event(
                event_type=EventType.TASK_ATTACHMENT_ADDED,
                model_id=task.id,
                user_id=task.owner_id,
                meta={
                    "task_id": str(task.id),
                    "short_code": task.short_code,
                    "attachment_id": str(attachment.id),
                    "file_name": attachment.file_name,
                }
            ),
            task_name=f"emit_task_attachment_added_{attachment.id}"
        )

    @staticmethod
    def list_attachments(
        db_session: DBSession,
        task_id: UUID,
    ) -> list[TaskAttachmentPublic]:
        """
        List all standalone attachments for a task (not linked to a comment).

        Args:
            db_session: Database session
            task_id: Task UUID

        Returns:
            List of public attachment schemas
        """
        attachments = db_session.exec(
            select(TaskAttachment)
            .where(TaskAttachment.task_id == task_id)
            .order_by(TaskAttachment.created_at.asc())
        ).all()

        return [TaskAttachmentService._to_public(db_session, att) for att in attachments]

    @staticmethod
    def get_download_stream(
        db_session: DBSession,
        task_id: UUID,
        attachment_id: UUID,
        user_id: UUID,
    ) -> tuple[Path, str, str]:
        """
        Get the file path for streaming a task attachment download.

        Args:
            db_session: Database session
            task_id: Task UUID
            attachment_id: Attachment UUID
            user_id: Requesting user (ownership check)

        Returns:
            Tuple of (file_path, filename, content_type)

        Raises:
            HTTPException: 404 if not found, 403 if unauthorized
        """
        attachment = db_session.get(TaskAttachment, attachment_id)
        if not attachment or attachment.task_id != task_id:
            raise HTTPException(status_code=404, detail="Attachment not found")

        task = db_session.get(InputTask, task_id)
        if not task or task.owner_id != user_id:
            raise HTTPException(status_code=403, detail="Not enough permissions")

        # Resolve absolute path
        base = Path(settings.UPLOAD_BASE_PATH).parent
        abs_path = (base / attachment.file_path).resolve()

        # Security: ensure path is inside upload dir
        if not str(abs_path).startswith(str(base.resolve())):
            raise HTTPException(status_code=400, detail="Invalid file path")

        if not abs_path.exists():
            raise HTTPException(status_code=404, detail="File not found on disk")

        content_type = attachment.content_type or "application/octet-stream"
        return abs_path, attachment.file_name, content_type

    @staticmethod
    def delete_attachment(
        db_session: DBSession,
        task_id: UUID,
        attachment_id: UUID,
        user_id: UUID,
    ) -> bool:
        """
        Delete a task attachment.

        Args:
            db_session: Database session
            task_id: Task UUID
            attachment_id: Attachment UUID to delete
            user_id: Requesting user (ownership check)

        Returns:
            True if deleted, False if not found
        """
        attachment = db_session.get(TaskAttachment, attachment_id)
        if not attachment or attachment.task_id != task_id:
            return False

        task = db_session.get(InputTask, task_id)
        if not task or task.owner_id != user_id:
            raise HTTPException(status_code=403, detail="Not enough permissions")

        # Delete file from disk
        try:
            base = Path(settings.UPLOAD_BASE_PATH).parent
            abs_path = (base / attachment.file_path).resolve()
            if abs_path.exists():
                abs_path.unlink()
                # Remove parent directory if empty
                parent = abs_path.parent
                if parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()
        except Exception as e:
            logger.warning(f"Failed to delete attachment file {attachment.file_path}: {e}")

        db_session.delete(attachment)
        db_session.commit()
        return True
