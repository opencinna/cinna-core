import os
import re
from datetime import datetime
from pathlib import Path
from typing import Iterator
import unicodedata

from app.core.config import settings
from app.models.files.file_upload import FileUpload


class FileStorageService:
    """Low-level file storage operations (abstraction for future cloud storage)"""

    @staticmethod
    def sanitize_filename(filename: str) -> str:
        """
        Sanitize filename to prevent security issues.

        - Remove path separators and dangerous characters
        - Normalize unicode
        - Limit length to 255 characters
        - Preserve extension
        """
        # Normalize unicode (é → e, etc.)
        filename = unicodedata.normalize("NFKD", filename)
        filename = filename.encode("ascii", "ignore").decode("ascii")

        # Remove path separators and dangerous chars
        filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", filename)

        # Replace spaces with underscores
        filename = filename.replace(" ", "_")

        # Remove multiple underscores
        filename = re.sub(r"_+", "_", filename)

        # Truncate to 255 chars while preserving extension
        if len(filename) > 255:
            name, ext = os.path.splitext(filename)
            max_name_len = 255 - len(ext)
            filename = name[:max_name_len] + ext

        # Ensure filename is not empty
        if not filename:
            filename = "file"

        return filename

    @staticmethod
    def store_file(
        user_id: str,
        file_id: str,
        filename: str,
        content: bytes,
    ) -> str:
        """
        Store file to disk.

        Path structure: uploads/{user_id}/{year}/{file_id}_{filename}

        Returns: Relative file path (for database record)
        """
        # Sanitize filename
        safe_filename = FileStorageService.sanitize_filename(filename)

        # Create directory structure with year
        current_year = datetime.now().year
        user_dir = Path(settings.UPLOAD_BASE_PATH) / str(user_id)
        year_dir = user_dir / str(current_year)
        year_dir.mkdir(parents=True, exist_ok=True)

        # Write file with file_id prefix
        prefixed_filename = f"{file_id}_{safe_filename}"
        file_path = year_dir / prefixed_filename
        file_path.write_bytes(content)

        # Return relative path
        return f"uploads/{user_id}/{current_year}/{prefixed_filename}"

    @staticmethod
    def get_file_path(file_record: FileUpload) -> Path:
        """
        Convert database file_path to absolute filesystem path.

        Validates path is within upload directory (security check).
        """
        base_path = Path(settings.UPLOAD_BASE_PATH).resolve()
        file_path = (base_path.parent / file_record.file_path).resolve()

        # Security: Ensure path is within base directory
        if not str(file_path).startswith(str(base_path)):
            raise ValueError("Invalid file path")

        return file_path

    @staticmethod
    def stream_file(file_path: Path, chunk_size: int = 65536) -> Iterator[bytes]:
        """
        Stream file content in chunks (64KB default).

        Used for download endpoint.
        """
        with open(file_path, "rb") as f:
            while chunk := f.read(chunk_size):
                yield chunk

    @staticmethod
    def delete_file(file_path: str) -> None:
        """
        Physically delete file from storage.

        Called by garbage collection background job.
        """
        full_path = Path(settings.UPLOAD_BASE_PATH).parent / file_path
        if full_path.exists():
            full_path.unlink()

            # Try to remove empty parent directories
            try:
                full_path.parent.rmdir()  # year directory
                full_path.parent.parent.rmdir()  # user_id directory
            except OSError:
                pass  # Directory not empty, that's ok
