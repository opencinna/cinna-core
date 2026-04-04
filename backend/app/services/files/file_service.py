import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException, UploadFile
from sqlmodel import Session, select

from app.core.config import settings
from app.models.files.file_upload import FileUpload, FileUploadPublic, MessageFile
from app.models.users.user import User
from app.models.sessions.session import SessionMessage, Session as ChatSession
from app.models.environments.environment import AgentEnvironment
from app.services.files.file_storage_service import FileStorageService


class FileService:
    """Business logic for file management"""

    @staticmethod
    async def create_file_upload(
        *,
        session: Session,
        user_id: uuid.UUID,
        file: UploadFile,
    ) -> FileUpload:
        """
        Store file to disk and create database record.

        Validates:
        - File size
        - Mime type
        - User storage quota
        """
        # Read file content
        content = await file.read()
        file_size = len(content)

        # Validate file size
        if file_size > settings.upload_max_file_size_bytes:
            raise HTTPException(
                status_code=400,
                detail=f"File too large. Max size: {settings.UPLOAD_MAX_FILE_SIZE_MB}MB",
            )

        # Validate mime type
        mime_type = file.content_type or "application/octet-stream"
        if mime_type not in settings.allowed_mime_types:
            raise HTTPException(
                status_code=400, detail=f"File type not allowed: {mime_type}"
            )

        # Check user storage quota
        total_size = FileService.get_user_storage_usage(
            session=session, user_id=user_id
        )
        if total_size + file_size > settings.upload_max_user_storage_bytes:
            raise HTTPException(
                status_code=400,
                detail=f"Storage quota exceeded. Max: {settings.UPLOAD_MAX_USER_STORAGE_GB}GB",
            )

        # Generate file ID
        file_id = uuid.uuid4()

        # Store file to disk
        file_path = FileStorageService.store_file(
            user_id=str(user_id),
            file_id=str(file_id),
            filename=file.filename or "file",
            content=content,
        )

        # Create database record
        db_file = FileUpload(
            id=file_id,
            user_id=user_id,
            filename=file.filename or "file",
            file_path=file_path,
            file_size=file_size,
            mime_type=mime_type,
            status="temporary",
        )
        session.add(db_file)
        session.commit()
        session.refresh(db_file)

        return db_file

    @staticmethod
    def get_user_storage_usage(*, session: Session, user_id: uuid.UUID) -> int:
        """Calculate total storage used by user (in bytes)"""
        statement = select(FileUpload).where(
            FileUpload.user_id == user_id,
            FileUpload.status.in_(
                ["temporary", "attached"]
            ),  # Don't count marked for deletion
        )
        files = session.exec(statement).all()
        return sum(f.file_size for f in files)

    @staticmethod
    def mark_files_as_attached(
        *,
        session: Session,
        file_ids: list[uuid.UUID],
    ) -> None:
        """Update file status to 'attached' after successfully sending message"""
        statement = select(FileUpload).where(FileUpload.id.in_(file_ids))
        files = session.exec(statement).all()

        for file in files:
            file.status = "attached"
            file.attached_at = datetime.now(UTC)

        session.commit()

    @staticmethod
    def mark_file_for_deletion(
        *,
        session: Session,
        file_id: uuid.UUID,
        user_id: uuid.UUID,
    ) -> None:
        """Soft delete: update status to 'marked_for_deletion'"""
        file = session.get(FileUpload, file_id)
        if not file:
            raise HTTPException(status_code=404, detail="File not found")

        # Validate ownership
        if file.user_id != user_id:
            raise HTTPException(status_code=403, detail="Not authorized")

        # Mark for deletion
        file.status = "marked_for_deletion"
        file.marked_for_deletion_at = datetime.now(UTC)
        session.commit()

    @staticmethod
    def check_download_permission(
        file: FileUpload,
        current_user: User,
        session: Session,
    ) -> bool:
        """
        Check if user can download file.

        Rules:
        - File owner can download
        - User who owns a session containing this file can download
        """
        # Owner can always download
        if file.user_id == current_user.id:
            return True

        # Check if user owns a session with a message referencing this file
        statement = (
            select(MessageFile)
            .join(SessionMessage, SessionMessage.id == MessageFile.message_id)
            .join(ChatSession, ChatSession.id == SessionMessage.session_id)
            .where(MessageFile.file_id == file.id)
            .where(ChatSession.user_id == current_user.id)
        )
        result = session.exec(statement).first()
        return result is not None

    @staticmethod
    def check_delete_permission(
        file: FileUpload,
        current_user: User,
    ) -> bool:
        """
        Check if user can delete file.

        Rules:
        - File owner only
        """
        return file.user_id == current_user.id

    @staticmethod
    def get_message_files(
        *,
        session: Session,
        message_id: uuid.UUID,
    ) -> list[FileUpload]:
        """Get all files attached to a message"""
        statement = (
            select(FileUpload)
            .join(MessageFile, MessageFile.file_id == FileUpload.id)
            .where(MessageFile.message_id == message_id)
        )
        return list(session.exec(statement).all())

    @staticmethod
    async def upload_files_to_agent_env(
        *,
        session: Session,
        file_ids: list[uuid.UUID],
        environment_id: uuid.UUID,
    ) -> dict[uuid.UUID, str]:
        """
        Upload files to agent-env and return mapping of file_id → agent_path.

        Steps:
        1. Fetch file records and validate they exist
        2. Get environment adapter
        3. Read files from disk
        4. Upload to agent-env via adapter
        5. Return mapping of file_id → agent_env_path

        Returns:
            {
                uuid1: "./uploads/document.pdf",
                uuid2: "./uploads/data.csv"
            }
        """
        # Fetch files
        statement = select(FileUpload).where(FileUpload.id.in_(file_ids))
        files = session.exec(statement).all()

        if len(files) != len(file_ids):
            missing = set(file_ids) - {f.id for f in files}
            raise HTTPException(
                status_code=400,
                detail=f"Files not found: {missing}"
            )

        # Get environment
        environment = session.get(AgentEnvironment, environment_id)
        if not environment:
            raise HTTPException(status_code=404, detail="Environment not found")

        # Check environment is running
        if environment.status != "running":
            raise HTTPException(
                status_code=400,
                detail=f"Environment not running (status: {environment.status})"
            )

        # Get adapter
        from app.services.environments.environment_lifecycle import EnvironmentLifecycleManager
        lifecycle_manager = EnvironmentLifecycleManager()
        adapter = lifecycle_manager.get_adapter(environment)

        # Prepare files for upload
        file_data = []
        for file in files:
            file_path = FileStorageService.get_file_path(file)
            if not file_path.exists():
                raise HTTPException(
                    status_code=500,
                    detail=f"File not found on disk: {file.filename}"
                )
            content = file_path.read_bytes()
            file_data.append((file.filename, content))

        # Upload to agent-env
        try:
            results = await adapter.upload_files_to_agent_env(file_data)
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to upload files to agent environment: {str(e)}"
            )

        # Create mapping: file_id → agent_env_path
        mapping = {}
        for file, result in zip(files, results):
            mapping[file.id] = result['path']

        return mapping
