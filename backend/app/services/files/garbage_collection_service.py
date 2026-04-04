import logging
from datetime import UTC, datetime, timedelta

from sqlmodel import Session, select

from app.models.files.file_upload import FileUpload, MessageFile
from app.services.files.file_storage_service import FileStorageService

logger = logging.getLogger(__name__)


class GarbageCollectionService:
    """Background job for cleaning up deleted and abandoned files"""

    @staticmethod
    def cleanup_marked_files(session: Session) -> int:
        """
        Delete files marked for deletion older than 24 hours.

        Returns count of deleted files.
        """
        threshold = datetime.now(UTC) - timedelta(hours=24)

        statement = select(FileUpload).where(
            FileUpload.status == "marked_for_deletion",
            FileUpload.marked_for_deletion_at < threshold,
        )
        files = session.exec(statement).all()

        deleted_count = 0
        for file in files:
            try:
                # Delete from disk
                FileStorageService.delete_file(file.file_path)

                # Delete from database
                session.delete(file)

                deleted_count += 1
            except Exception as e:
                logger.error(f"Failed to delete file {file.id}: {e}")
                continue

        session.commit()
        logger.info(f"Garbage collection: deleted {deleted_count} marked files")
        return deleted_count

    @staticmethod
    def cleanup_abandoned_temp_files(session: Session) -> int:
        """
        Delete temporary files older than 24 hours (never attached to message).

        Returns count of deleted files.
        """
        threshold = datetime.now(UTC) - timedelta(hours=24)

        statement = select(FileUpload).where(
            FileUpload.status == "temporary", FileUpload.uploaded_at < threshold
        )
        files = session.exec(statement).all()

        deleted_count = 0
        for file in files:
            try:
                # Delete from disk
                FileStorageService.delete_file(file.file_path)

                # Delete from database
                session.delete(file)

                deleted_count += 1
            except Exception as e:
                logger.error(f"Failed to delete temp file {file.id}: {e}")
                continue

        session.commit()
        logger.info(
            f"Garbage collection: deleted {deleted_count} abandoned temp files"
        )
        return deleted_count

    @staticmethod
    def cleanup_orphaned_message_files(session: Session) -> int:
        """
        Delete files that are attached but have no message references (message was deleted).

        When a message is deleted, MessageFile junction records are CASCADE deleted,
        but FileUpload records remain orphaned. This cleanup handles those cases.

        Returns count of deleted files.
        """
        # Find FileUpload records with status="attached" that have no MessageFile references
        # Using LEFT JOIN and checking for NULL to find orphaned files
        from sqlalchemy import exists

        statement = select(FileUpload).where(
            FileUpload.status == "attached",
            ~exists(select(1).where(MessageFile.file_id == FileUpload.id)),
        )
        files = session.exec(statement).all()

        deleted_count = 0
        for file in files:
            try:
                # Delete from disk
                FileStorageService.delete_file(file.file_path)

                # Delete from database
                session.delete(file)

                deleted_count += 1
            except Exception as e:
                logger.error(f"Failed to delete orphaned file {file.id}: {e}")
                continue

        session.commit()
        logger.info(
            f"Garbage collection: deleted {deleted_count} orphaned message files"
        )
        return deleted_count
